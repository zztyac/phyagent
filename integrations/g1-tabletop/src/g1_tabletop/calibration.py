"""Offline calibration calculations; these utilities never connect to or move a robot."""

from pathlib import Path

import numpy as np
from pydantic import Field

from g1_tabletop.config import Pose, StrictModel
from g1_tabletop.geometry import as_pose, matrix, orientation_error
from g1_tabletop.perception import calibrate_hand_eye


class CalibrationPair(StrictModel):
    base_to_mount: Pose
    camera_to_target: Pose


class CalibrationDataset(StrictModel):
    pairs: list[CalibrationPair] = Field(min_length=3)


def calibrate(path: Path, mount: str) -> Pose:
    """For wrist, mount=wrist; for base, base_to_mount is the surveyed base_to_target."""
    data = CalibrationDataset.model_validate_json(path.read_text(encoding="utf-8"))
    if mount == "wrist":
        return calibrate_hand_eye(
            [p.base_to_mount for p in data.pairs], [p.camera_to_target for p in data.pairs]
        )
    if mount != "base":
        raise ValueError("mount must be wrist or base")
    estimates = [
        as_pose(matrix(p.base_to_mount) @ np.linalg.inv(matrix(p.camera_to_target)))
        for p in data.pairs
    ]
    quaternions = np.array([p.quaternion_xyzw for p in estimates])
    _, vectors = np.linalg.eigh(quaternions.T @ quaternions)
    mean = Pose(
        position=tuple(np.mean([p.position for p in estimates], axis=0)),
        quaternion_xyzw=tuple(vectors[:, -1]),
    )
    if any(
        np.linalg.norm(np.array(p.position) - mean.position) > 0.005
        or orientation_error(p, mean) > 0.03
        for p in estimates
    ):
        raise ValueError("static camera residual exceeds 5mm / 0.03rad")
    return mean


def write_marker(path: Path, marker_id: int, pixels: int):
    import cv2

    if not 0 <= marker_id < 50 or not 100 <= pixels <= 4000:
        raise ValueError("marker ID must be 0..49 and pixels 100..4000")
    marker = cv2.aruco.generateImageMarker(
        cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50), marker_id, pixels
    )
    border = max(20, pixels // 5)
    marker = cv2.copyMakeBorder(
        marker, border, border, border, border, cv2.BORDER_CONSTANT, value=255
    )
    if path.suffix.lower() != ".png" or not cv2.imwrite(str(path), marker):
        raise ValueError("marker output must be a writable .png path")
