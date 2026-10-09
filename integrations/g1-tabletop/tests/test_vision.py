import numpy as np
import pytest

from g1_tabletop.config import Marker, Pose
from g1_tabletop.errors import ExecutionError
from g1_tabletop.geometry import as_pose, matrix, orientation_error
from g1_tabletop.perception import calibrate_hand_eye, detect_markers, solve_marker

cv2 = pytest.importorskip("cv2")


def transform(rotation, translation):
    result = np.eye(4)
    result[:3, :3] = cv2.Rodrigues(np.array(rotation, dtype=float))[0]
    result[:3, 3] = translation
    return result


def test_marker_pose_recovers_known_projected_geometry():
    k = np.array([[800.0, 0, 320], [0, 800, 240], [0, 0, 1]])
    points = np.array([[-0.02, 0.02, 0], [0.02, 0.02, 0], [0.02, -0.02, 0], [-0.02, -0.02, 0]])
    rotation, translation = np.array([2.8, 0.3, 0.1]), np.array([0.03, -0.02, 0.35])
    corners, _ = cv2.projectPoints(points, rotation, translation, k, np.zeros(5))
    pose = solve_marker(corners, k, np.zeros(5), 0.04, 1)
    np.testing.assert_allclose(pose.position, translation, atol=1e-6)
    assert orientation_error(pose, as_pose(transform(rotation, translation))) < 1e-5


def test_blank_image_has_no_targets_and_nonfinite_calibration_is_rejected():
    assert (
        detect_markers(
            np.full((400, 400), 255, np.uint8),
            {"panel": Marker(marker_id=0, size_m=0.04)},
            np.eye(3),
            np.zeros(5),
            Pose(),
            2,
        )
        == {}
    )
    with pytest.raises(ExecutionError, match="INVALID_INTRINSICS"):
        solve_marker(np.full((4, 2), np.nan), np.eye(3), np.zeros(5), 0.04, 2)


def test_hand_eye_recovers_known_mount_and_rejects_unexcited_samples():
    rng = np.random.default_rng(721)
    wrist_camera = transform([0.1, -0.2, 0.08], [0.025, 0.01, 0.04])
    base_target = transform([0.3, 0.1, 0.5], [0.5, 0.2, 0.1])
    wrists = [transform(rng.uniform(-0.8, 0.8, 3), rng.uniform(0.1, 0.4, 3)) for _ in range(16)]
    targets = [np.linalg.inv(w @ wrist_camera) @ base_target for w in wrists]
    found = calibrate_hand_eye([as_pose(w) for w in wrists], [as_pose(t) for t in targets])
    np.testing.assert_allclose(matrix(found), wrist_camera, atol=1e-7)
    with pytest.raises(ValueError, match="eight"):
        calibrate_hand_eye([Pose()], [Pose()])
    with pytest.raises(ValueError, match="two distinct axes"):
        calibrate_hand_eye([Pose()] * 8, [Pose()] * 8)
