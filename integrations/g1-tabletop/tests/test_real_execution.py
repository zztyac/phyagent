import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from g1_tabletop.backends.real import RealBackend
from g1_tabletop.config import Pose
from g1_tabletop.drivers.realsense import Frame, RealSenseCameras
from g1_tabletop.drivers.unitree import JointSample, UnitreeArm
from g1_tabletop.errors import ExecutionError


class LinearTestKinematics:
    """Simple Cartesian plant for testing execution timing/guards, not a substitute for native IK tests."""

    def __init__(self):
        self.checked = []

    def fk(self, q, arm, *, wrist=False):
        return Pose(position=tuple(np.array([0.4, 0, 0.4]) + q[15:18]))

    def solve(self, q, arm, pose):
        result = q.copy()
        result[15:18] = np.array(pose.position) - [0.4, 0, 0.4]
        return result

    def check_collision(self, q, **kwargs):
        self.checked.append(q.copy())

    def gravity(self, q):
        return np.zeros(14)


@pytest.fixture
async def backend(hardware_config):
    backend = RealBackend(hardware_config)
    yield backend
    await backend.client.aclose()


async def test_real_feedback_checks_target_source_and_document_types(backend):
    document = {
        "button_id": "button_01",
        "event_count": 1,
        "state": True,
        "timestamp": time.time(),
        "source": "test-harness",
        "epoch": "boot",
    }
    await backend.client.aclose()
    backend.client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=document))
    )
    result = await backend.feedback("button_01")
    assert result.event_count == 1 and result.source == "test-harness"
    for key, bad in (
        ("source", "wrong-device"),
        ("button_id", "wrong-button"),
        ("event_count", True),
        ("state", "true"),
        ("timestamp", float("nan")),
    ):
        previous = document[key]
        document[key] = bad
        with pytest.raises((ExecutionError, ValueError)):
            await backend.feedback("button_01")
        document[key] = previous


async def test_planned_move_rechecks_actual_state_and_reaches_measured_goal(backend):
    backend.kinematics = LinearTestKinematics()
    current = np.zeros(29)
    backend.arm.sample = lambda **_: JointSample(
        current.copy(), np.zeros(29), 3, time.monotonic(), time.time()
    )
    writes = []

    def command(q, tau):
        writes.append(q.copy())
        current[15:29] = q

    backend.arm.command = command
    goal = Pose(position=(0.403, 0, 0.4))
    await backend.move("left", goal, 0.04, lambda: None)
    assert len(writes) > 2
    assert backend.kinematics.fk(current, "left").position == pytest.approx(goal.position)
    assert len(backend.kinematics.checked) > len(writes)
    with pytest.raises(ExecutionError, match="SPEED_LIMIT"):
        await backend.move("left", goal, 1, lambda: None)
    stop = threading.Event()
    stop.set()
    with pytest.raises(ExecutionError, match="PLAN_CANCELLED"):
        backend._plan("left", goal, 0.04, None, None, stop)
    with pytest.raises(ExecutionError, match="TRANSLATION_LIMIT"):
        backend._plan("left", Pose(position=(1.9, 0, 0.4)), 0.04, None, None, threading.Event())


async def test_localization_uses_supplied_image_and_synchronized_wrist_pose(backend, monkeypatch):
    stamp = time.time()
    frame = Frame(
        np.zeros((10, 10, 3), dtype=np.uint8),
        np.zeros((10, 10)),
        np.eye(3),
        np.zeros(5),
        stamp,
        time.monotonic(),
    )
    backend.kinematics = LinearTestKinematics()
    backend.arm.sample_at = lambda timestamp, skew: JointSample(
        np.zeros(29), np.zeros(29), 3, time.monotonic(), timestamp
    )

    def no_fetch(_):
        pytest.fail("localization must use the supplied exposure, not fetch another frame")

    backend.cameras.frame = no_fetch

    def detect(image, definitions, k, distortion, camera_pose, error):
        assert image is frame.rgb
        return {"panel": camera_pose}

    monkeypatch.setattr("g1_tabletop.backends.real.detect_markers", detect)
    targets = backend._locate("console", "left", frame)
    assert targets["button_01"].position == (0.4, 0, 0.4)
    backend.check_health = lambda: None
    backend.cameras.frame = lambda _: frame
    assert "button_01" in await backend.locate("console", "left")


async def test_grasp_confirmation_requires_fresh_frames_lift_and_current(backend):
    backend.kinematics = LinearTestKinematics()

    def fresh_frame(_):
        return SimpleNamespace(timestamp=time.time())

    backend.cameras.frame = fresh_frame
    backend.arm.sample_at = lambda timestamp, skew: JointSample(
        np.zeros(29), np.zeros(29), 3, time.monotonic(), timestamp
    )
    backend.hands.state = lambda _: (np.zeros(6), np.full(6, 0.02))
    backend._locate = lambda *args: {"block_01": Pose(position=(0.4, 0, 0.45))}
    before = Pose(position=(0.4, 0, 0.4))
    assert await backend.grasp_confirmed("block_01", "left", before, lambda: None)
    backend.hands.state = lambda _: (np.zeros(6), np.zeros(6))
    assert not await backend.grasp_confirmed("block_01", "left", before, lambda: None)
    fixed = SimpleNamespace(timestamp=time.time())
    backend.cameras.frame = lambda _: fixed
    assert not await backend.placement_confirmed(
        "block_01", Pose(position=(0.4, 0, 0.45)), lambda: None
    )


def test_lowstate_validates_imu_torque_posture_and_sync(hardware_config):
    arm = UnitreeArm(hardware_config)
    message = SimpleNamespace(
        motor_state=[SimpleNamespace(q=0, dq=0, tau_est=0) for _ in range(29)],
        mode_machine=3,
        imu_state=SimpleNamespace(quaternion=[1, 0, 0, 0]),
    )
    arm._receive(message)
    assert arm.sample().q.shape == (29,)
    assert arm.sample_at(time.time(), 0.1).mode_machine == 3
    with pytest.raises(ExecutionError, match="CAMERA_STATE_SKEW"):
        arm.sample_at(0, 0.001)
    reference = arm.sample()
    for modified, code in (
        (replace(reference, imu_wxyz=(0.99, 0.14, 0, 0)), "BASE_ATTITUDE_CHANGED"),
        (replace(reference, q=np.ones(29)), "BASE_POSTURE_CHANGED"),
        (replace(reference, tau_est=np.full(29, 100)), "MEASURED_TORQUE_LIMIT"),
        (replace(reference, monotonic=0), "STATE_STALE"),
    ):
        arm.samples.append(modified)
        with pytest.raises(ExecutionError, match=code):
            arm.sample()
    message.imu_state.quaternion = [float("nan")] * 4
    arm._receive(message)
    assert arm.fault.code == "INVALID_LOWSTATE"


def test_camera_freshness_and_errors_are_never_ignored(hardware_config):
    cameras = RealSenseCameras(hardware_config)
    with pytest.raises(ExecutionError, match="CAMERA_STALE"):
        cameras.frame("head")
    frame = Frame(
        np.zeros((2, 2, 3)), np.zeros((2, 2)), np.eye(3), np.zeros(5), time.time(), time.monotonic()
    )
    cameras.frames["head"] = frame
    assert cameras.frame("head") is frame
    cameras.errors["head"] = "USB disconnected"
    with pytest.raises(ExecutionError, match="USB disconnected"):
        cameras.frame("head")
    cameras.close()
