import asyncio
import time
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from g1_tabletop.backends.real import RealBackend, joint_segment
from g1_tabletop.drivers.brainco import BrainCo
from g1_tabletop.drivers.unitree import JointSample, UnitreeArm, validate_arm_target
from g1_tabletop.errors import ExecutionError
from g1_tabletop.ownership import FileLease


def sample(q=None):
    return JointSample(
        np.zeros(29) if q is None else q, np.zeros(29), 3, time.monotonic(), time.time()
    )


def test_arm_does_not_publish_until_acquired_and_never_commands_legs(hardware_config):
    arm = UnitreeArm(hardware_config)
    arm.factory = lambda: SimpleNamespace(motor_cmd=[SimpleNamespace() for _ in range(35)])
    arm.crc = SimpleNamespace(Crc=lambda msg: 123)
    arm.publisher = Mock()
    arm._tick(0.004)
    arm.publisher.Write.assert_not_called()
    arm.samples.append(sample())
    arm.acquire(np.zeros(14))
    arm.command(np.full(14, 0.001), np.ones(14))
    arm._tick(0.004)
    msg = arm.publisher.Write.call_args.args[0]
    assert msg.crc == 123 and msg.mode_machine == 3
    assert all(m.mode == 0 and m.kp == 0 and m.tau == 0 for m in msg.motor_cmd[:15])
    assert all(m.mode == 1 and m.q == 0.001 and m.tau == 1 for m in msg.motor_cmd[15:29])
    assert 0 < msg.motor_cmd[29].q < 1
    assert all(m.mode == 0 and m.q == 0 for m in msg.motor_cmd[30:])


def test_watchdog_latches_once_instead_of_following_motion(hardware_config):
    arm = UnitreeArm(hardware_config)
    arm.samples.append(sample())
    arm.acquire(np.zeros(14))
    arm.last_heartbeat = 0
    arm.factory = lambda: SimpleNamespace(motor_cmd=[SimpleNamespace() for _ in range(35)])
    arm.crc, arm.publisher = SimpleNamespace(Crc=lambda _: 0), Mock()
    arm._tick(0.004)
    assert arm.fault.code == "CONTROL_WATCHDOG"
    moved = np.zeros(29)
    moved[15:] = 0.01
    arm.samples.append(sample(moved))
    arm._tick(0.004)
    np.testing.assert_equal(arm.target, np.zeros(14))
    with pytest.raises(ExecutionError, match="CONTROL_WATCHDOG"):
        arm.command(np.zeros(14), np.zeros(14))


@pytest.mark.parametrize(
    "target,torque,code",
    [
        ([float("nan")] * 14, [0] * 14, "INVALID_JOINT_COMMAND"),
        ([0] * 13, [0] * 14, "INVALID_JOINT_COMMAND"),
        ([3] * 14, [0] * 14, "JOINT_LIMIT"),
        ([0.2] * 14, [0] * 14, "TRACKING_ERROR"),
        ([0] * 14, [30] * 14, "TORQUE_LIMIT"),
    ],
)
def test_arm_rejects_invalid_targets(hardware_config, target, torque, code):
    with pytest.raises(ExecutionError, match=code):
        validate_arm_target(hardware_config, np.zeros(14), target, torque)


def test_file_ownership_is_exclusive_and_released(tmp_path):
    lease = FileLease(tmp_path / "robot.lock")
    with pytest.raises(ExecutionError, match="RUNTIME_OWNED"):
        FileLease(tmp_path / "robot.lock")
    lease.close()
    FileLease(tmp_path / "robot.lock").close()


def fake_hands(config):
    hands = BrainCo(config)
    hands.command_type = SimpleNamespace
    hands.motor_factory = SimpleNamespace
    hands.publishers = {side: Mock() for side in ("left", "right")}
    for side in ("left", "right"):
        hands._receive(
            side, SimpleNamespace(states=[SimpleNamespace(q=0.2, tau_est=0.02) for _ in range(6)])
        )
    return hands


def test_hand_hold_is_owned_only_and_allowed_after_overcurrent(hardware_config):
    hands = fake_hands(hardware_config)
    hands.hold()
    hands.publishers["left"].Write.assert_not_called()
    hands.command("left", hardware_config.postures["pinch"])
    hands._receive(
        "left", SimpleNamespace(states=[SimpleNamespace(q=0.4, tau_est=0.6) for _ in range(6)])
    )
    with pytest.raises(ExecutionError, match="HAND_OVERCURRENT"):
        hands.state("left")
    hands.hold()
    msg = hands.publishers["left"].Write.call_args.args[0]
    assert [m.q for m in msg.cmds] == [0.4] * 6
    hands.publishers["right"].Write.assert_not_called()


async def test_cancel_during_hand_motion_sends_measured_hold(hardware_config):
    backend = RealBackend(hardware_config)
    backend.hands = fake_hands(hardware_config)
    backend.arm.sample = lambda **_: sample()
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls > 2:
            raise ExecutionError("CANCELLED", "test")

    try:
        with pytest.raises(ExecutionError, match="CANCELLED"):
            await backend.hand("left", "pinch", check, object_id="block_01")

        async def refresh():
            for _ in range(15):
                backend.hands._receive(
                    "left",
                    SimpleNamespace(
                        states=[SimpleNamespace(q=0.2, tau_est=0.02) for _ in range(6)]
                    ),
                )
                await asyncio.sleep(0.01)

        refreshing = asyncio.create_task(refresh())
        assert await backend.stopped()
        await refreshing
        msg = backend.hands.publishers["left"].Write.call_args.args[0]
        assert [m.q for m in msg.cmds] == [0.2] * 6
    finally:
        await backend.client.aclose()


async def test_stop_cannot_count_same_joint_sample_as_five_samples(hardware_config):
    backend = RealBackend(hardware_config)
    backend.hands = fake_hands(hardware_config)
    fixed = sample()
    backend.arm.sample = lambda **_: fixed
    try:
        assert not await backend.stopped()
    finally:
        await backend.client.aclose()


def test_trajectory_bounds_velocity_and_acceleration(hardware_config):
    start, end = np.zeros(29), np.zeros(29)
    end[15] = 0.4
    trajectory = np.array([start, *joint_segment(start, end, hardware_config)])
    velocity = np.diff(trajectory, axis=0) * hardware_config.hardware.control_hz
    acceleration = np.diff(velocity, axis=0) * hardware_config.hardware.control_hz
    assert np.max(np.abs(velocity)) <= hardware_config.limits.joint_speed_rad_s
    assert np.max(np.abs(acceleration)) <= hardware_config.limits.joint_acceleration_rad_s2 * 1.01
    np.testing.assert_allclose(trajectory[-1], end)
