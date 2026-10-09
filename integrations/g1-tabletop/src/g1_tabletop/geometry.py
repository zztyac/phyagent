"""SE(3), bounded trajectories and workspace checks, with explicit SI units."""

import math

import numpy as np

from g1_tabletop.config import Bounds, Pose
from g1_tabletop.errors import ExecutionError


def matrix(pose: Pose) -> np.ndarray:
    x, y, z, w = pose.quaternion_xyzw
    result = np.eye(4)
    result[:3, :3] = [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]
    result[:3, 3] = pose.position
    return result


def as_pose(transform: np.ndarray) -> Pose:
    transform = np.asarray(transform, dtype=float)
    if transform.shape != (4, 4) or not np.isfinite(transform).all():
        raise ExecutionError("INVALID_TRANSFORM", "expected finite 4x4 matrix")
    rotation = transform[:3, :3]
    if not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-6) or not np.isclose(
        np.linalg.det(rotation), 1
    ):
        raise ExecutionError("INVALID_TRANSFORM", "rotation is not in SO(3)")
    if not np.allclose(transform[3], [0, 0, 0, 1]):
        raise ExecutionError("INVALID_TRANSFORM", "invalid homogeneous row")
    # Eigenvector method is stable at rotations near pi.
    r = rotation
    k = (
        np.array(
            [
                [
                    r[0, 0] - r[1, 1] - r[2, 2],
                    r[0, 1] + r[1, 0],
                    r[0, 2] + r[2, 0],
                    r[2, 1] - r[1, 2],
                ],
                [
                    r[0, 1] + r[1, 0],
                    r[1, 1] - r[0, 0] - r[2, 2],
                    r[1, 2] + r[2, 1],
                    r[0, 2] - r[2, 0],
                ],
                [
                    r[0, 2] + r[2, 0],
                    r[1, 2] + r[2, 1],
                    r[2, 2] - r[0, 0] - r[1, 1],
                    r[1, 0] - r[0, 1],
                ],
                [r[2, 1] - r[1, 2], r[0, 2] - r[2, 0], r[1, 0] - r[0, 1], np.trace(r)],
            ]
        )
        / 3
    )
    _, vectors = np.linalg.eigh(k)
    q = vectors[:, -1]
    if q[3] < 0:
        q = -q
    return Pose(position=tuple(transform[:3, 3]), quaternion_xyzw=tuple(q))


def compose(left: Pose, right: Pose) -> Pose:
    return as_pose(matrix(left) @ matrix(right))


def offset(pose: Pose, direction, distance: float) -> Pose:
    xyz = np.asarray(pose.position) + np.asarray(direction) * distance
    return pose.model_copy(update={"position": tuple(float(v) for v in xyz)})


def distance(a: Pose, b: Pose) -> float:
    return float(np.linalg.norm(np.asarray(a.position) - b.position))


def orientation_error(a: Pose, b: Pose) -> float:
    dot = abs(float(np.dot(a.quaternion_xyzw, b.quaternion_xyzw)))
    return 2 * math.acos(min(1.0, dot))


def check_workspace(pose: Pose, bounds: Bounds) -> None:
    if not all(
        lo <= p <= hi for lo, p, hi in zip(bounds.lower, pose.position, bounds.upper, strict=True)
    ):
        raise ExecutionError("OUTSIDE_WORKSPACE", "TCP target exceeds calibrated workspace")


def interpolate(start: Pose, end: Pose, fraction: float) -> Pose:
    t = float(np.clip(fraction, 0, 1))
    # Smoothstep gives zero endpoint speed. Caller accounts for its 1.5 peak derivative.
    s = t * t * (3 - 2 * t)
    a, b = np.array(start.quaternion_xyzw), np.array(end.quaternion_xyzw)
    dot = float(a @ b)
    if dot < 0:
        b, dot = -b, -dot
    if dot > 0.9995:
        q = a + s * (b - a)
    else:
        theta = math.acos(min(dot, 1))
        q = (math.sin((1 - s) * theta) * a + math.sin(s * theta) * b) / math.sin(theta)
    q /= np.linalg.norm(q)
    xyz = (1 - s) * np.array(start.position) + s * np.array(end.position)
    return Pose(position=tuple(xyz), quaternion_xyzw=tuple(q))
