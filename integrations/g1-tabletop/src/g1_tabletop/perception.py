"""Calibrated marker localization and offline calibration utilities (OpenCV optional)."""

from __future__ import annotations

import numpy as np

from g1_tabletop.config import Marker, Pose
from g1_tabletop.errors import ExecutionError
from g1_tabletop.geometry import as_pose, compose, matrix, orientation_error


def solve_marker(corners, camera_matrix, distortion, size_m: float, max_error_px: float) -> Pose:
    import cv2

    corners = np.asarray(corners, dtype=np.float64).reshape(4, 2)
    camera_matrix = np.asarray(camera_matrix, dtype=float).reshape(3, 3)
    distortion = np.asarray(distortion, dtype=float)
    if not np.isfinite(corners).all() or not np.isfinite(camera_matrix).all() or size_m <= 0:
        raise ExecutionError("INVALID_INTRINSICS", "invalid calibration or marker data")
    half = size_m / 2
    object_points = np.array(
        [[-half, half, 0], [half, half, 0], [half, -half, 0], [-half, -half, 0]], dtype=float
    )
    count, rotations, translations, _ = cv2.solvePnPGeneric(
        object_points, corners, camera_matrix, distortion, flags=cv2.SOLVEPNP_IPPE_SQUARE
    )
    candidates = []
    for rotation, translation in zip(rotations, translations, strict=True):
        if float(translation[2, 0]) <= 0:
            continue
        projected, _ = cv2.projectPoints(
            object_points, rotation, translation, camera_matrix, distortion
        )
        error = float(np.sqrt(np.mean(np.sum((projected.reshape(4, 2) - corners) ** 2, axis=1))))
        if np.isfinite(error):
            candidates.append((error, rotation, translation))
    if not count or not candidates:
        raise ExecutionError("MARKER_POSE_FAILED", "no positive-depth marker pose")
    error, rotation, translation = min(candidates, key=lambda item: item[0])
    if error > max_error_px:
        raise ExecutionError("REPROJECTION_ERROR", f"marker reprojection error {error:.3f}px")
    # Near-identical reprojection scores with materially different poses are ambiguous.
    if len(candidates) > 1:
        alternate = sorted(candidates, key=lambda item: item[0])[1]
        r1, _ = cv2.Rodrigues(rotation)
        r2, _ = cv2.Rodrigues(alternate[1])
        angle = np.arccos(np.clip((np.trace(r1.T @ r2) - 1) / 2, -1, 1))
        if abs(alternate[0] - error) < 0.05 and angle > 0.15:
            raise ExecutionError(
                "AMBIGUOUS_MARKER",
                "marker pose ambiguous; change viewing angle or use a larger marker",
            )
    transform = np.eye(4)
    transform[:3, :3] = cv2.Rodrigues(rotation)[0]
    transform[:3, 3] = translation.ravel()
    return as_pose(transform)


def detect_markers(
    image,
    definitions: dict[str, Marker],
    camera_matrix,
    distortion,
    camera_in_base: Pose,
    max_error_px: float,
) -> dict[str, Pose]:
    import cv2

    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    detector = cv2.aruco.ArucoDetector(dictionary, cv2.aruco.DetectorParameters())
    corners, identifiers, _ = detector.detectMarkers(image)
    if identifiers is None:
        return {}
    ids = [int(v) for v in identifiers.ravel()]
    if len(ids) != len(set(ids)):
        raise ExecutionError("DUPLICATE_MARKER", "same marker ID appeared more than once")
    detected = {}
    for name, definition in definitions.items():
        if definition.marker_id in ids:
            pose = solve_marker(
                corners[ids.index(definition.marker_id)],
                camera_matrix,
                distortion,
                definition.size_m,
                max_error_px,
            )
            detected[name] = compose(compose(camera_in_base, pose), definition.marker_to_target)
    return detected


def calibrate_hand_eye(base_to_wrist: list[Pose], camera_to_target: list[Pose]) -> Pose:
    """Eye-in-hand calibration with a stationary target; returns wrist-to-camera."""
    import cv2

    if len(base_to_wrist) != len(camera_to_target) or len(base_to_wrist) < 8:
        raise ValueError("at least eight matched calibration poses are required")
    wrists = [matrix(p) for p in base_to_wrist]
    targets = [matrix(p) for p in camera_to_target]
    rotations = np.array([cv2.Rodrigues(t[:3, :3])[0].ravel() for t in wrists])
    if np.linalg.matrix_rank(rotations - rotations.mean(axis=0), tol=0.02) < 2:
        raise ValueError("calibration requires rotations about at least two distinct axes")
    rotation, translation = cv2.calibrateHandEye(
        [t[:3, :3] for t in wrists],
        [t[:3, 3] for t in wrists],
        [t[:3, :3] for t in targets],
        [t[:3, 3] for t in targets],
        method=cv2.CALIB_HAND_EYE_PARK,
    )
    transform = np.eye(4)
    transform[:3, :3], transform[:3, 3] = rotation, translation.ravel()
    result = as_pose(transform)
    # A fixed calibration board should transform to a fixed base pose.
    board_poses = [w @ transform @ t for w, t in zip(wrists, targets, strict=True)]
    positions = np.array([t[:3, 3] for t in board_poses])
    if np.max(np.linalg.norm(positions - positions.mean(axis=0), axis=1)) > 0.01:
        raise ValueError("hand-eye residual exceeds 10mm; inspect pairing and calibration motion")
    if any(orientation_error(as_pose(p), as_pose(board_poses[0])) > 0.03 for p in board_poses):
        raise ValueError("hand-eye angular residual exceeds 0.03rad")
    return result
