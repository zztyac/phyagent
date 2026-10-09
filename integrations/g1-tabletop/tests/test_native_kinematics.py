import numpy as np
import pytest

pytest.importorskip("pinocchio", reason="native Pinocchio tests run in the Linux validation job")
pytest.importorskip("coal")

from g1_tabletop.config import Pose
from g1_tabletop.errors import ExecutionError
from g1_tabletop.geometry import distance, matrix
from g1_tabletop.kinematics import Kinematics


def test_native_mapping_fk_ik_gravity_and_payload(hardware_config):
    kin = Kinematics(hardware_config)
    q = np.zeros(29)
    np.testing.assert_array_equal(kin.to_motors(kin.to_model(q)), q)
    kin.check_collision(q)
    goal_q = q.copy()
    goal_q[15:22] = [0.03, -0.02, 0.02, 0.03, 0, 0, 0]
    goal = kin.fk(goal_q, "left")
    solution = kin.solve(q, "left", goal)
    assert distance(kin.fk(solution, "left"), goal) < hardware_config.limits.position_tolerance_m
    np.testing.assert_array_equal(solution[22:], q[22:])
    before = kin.gravity(q).copy()
    geometry_count = kin.geometry.ngeoms
    kin.select_tcp("left", "grasp")
    for _ in range(2):
        kin.attach_payload("left", hardware_config.objects["block_01"])
        assert not np.allclose(kin.gravity(q), before)
        kin.check_collision(q)
        kin.detach_payload("left")
        assert kin.geometry.ngeoms == geometry_count
        np.testing.assert_allclose(kin.gravity(q), before)
        kin.check_collision(q)

    kin.attach_payload("left", hardware_config.objects["block_01"])
    kin.attach_payload("right", hardware_config.objects["block_01"])
    kin.detach_payload("left")
    kin.check_collision(q)
    kin.detach_payload("right")
    assert kin.geometry.ngeoms == geometry_count


def test_contact_exception_applies_only_to_tip_and_calibrated_button(hardware_config):
    kin = Kinematics(hardware_config)
    q = np.zeros(29)
    target = kin.fk(q, "right")
    box = next(o for o in kin.geometry.geometryObjects if o.name == "env:console")
    transform = matrix(target)
    # Box surface at target, material begins along TCP +Z.
    box.placement = kin.pin.SE3(transform[:3, :3], transform[:3, 3] + transform[:3, 2] * 0.05)
    with pytest.raises(ExecutionError, match="COLLISION"):
        kin.check_collision(q)
    profile = hardware_config.buttons["button_01"]
    kin.check_collision(q, contact=("right", target, profile))
    shifted = Pose(
        position=(target.position[0] + 0.04, *target.position[1:]),
        quaternion_xyzw=target.quaternion_xyzw,
    )
    with pytest.raises(ExecutionError, match="COLLISION"):
        kin.check_collision(q, contact=("right", shifted, profile))


def test_model_hash_change_is_rejected_before_motion(hardware_config):
    from pathlib import Path

    Path(hardware_config.hardware.urdf).write_text("changed")
    with pytest.raises(ExecutionError, match="MODEL_DIGEST"):
        Kinematics(hardware_config)
