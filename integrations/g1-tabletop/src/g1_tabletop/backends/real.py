"""Real SDK execution with calibrated vision, sampled collision checks and feedback.

No SDK, camera, or model is loaded until explicit real-mode startup. The caller
must supply a commissioned configuration; this module contains no guessed gains.
"""

from __future__ import annotations

import asyncio
import math
import sys
import threading
import time
from urllib.parse import quote

import httpx
import numpy as np
from pydantic import Field, StrictBool, StrictInt

from g1_tabletop.backends.base import Feedback
from g1_tabletop.config import StrictModel
from g1_tabletop.drivers.brainco import BrainCo
from g1_tabletop.drivers.realsense import RealSenseCameras
from g1_tabletop.drivers.unitree import UnitreeArm
from g1_tabletop.errors import ExecutionError
from g1_tabletop.geometry import check_workspace, compose, distance, interpolate, orientation_error
from g1_tabletop.kinematics import Kinematics
from g1_tabletop.perception import detect_markers


class FeedbackDocument(StrictModel):
    button_id: str
    event_count: StrictInt = Field(ge=0)
    state: StrictBool | None
    timestamp: float
    source: str = Field(min_length=1)
    epoch: str = Field(min_length=1)


def joint_segment(start, end, config):
    """Zero endpoint velocity/acceleration quintic with bounded peak joint speed."""
    delta = float(np.max(np.abs(np.asarray(end) - start)))
    limits = config.limits
    duration = max(
        1.875 * delta / limits.joint_speed_rad_s,
        math.sqrt(5.774 * delta / limits.joint_acceleration_rad_s2),
        1 / config.hardware.control_hz,
    )
    steps = max(
        1,
        math.ceil(duration * config.hardware.control_hz),
        math.ceil(1.875 * delta / min(limits.joint_step_rad, 0.002)),
    )
    for index in range(1, steps + 1):
        t = index / steps
        s = 10 * t**3 - 15 * t**4 + 6 * t**5
        yield np.asarray(start) + s * (np.asarray(end) - start)


class RealBackend:
    def __init__(self, config):
        if config.mode != "real" or not config.commissioned or config.hardware is None:
            raise ValueError("real backend requires commissioned real configuration")
        self.config = config
        self.arm = UnitreeArm(config)
        self.hands = BrainCo(config)
        self.cameras = RealSenseCameras(config)
        self.kinematics = None
        self.model_lock = threading.RLock()
        self.monitor_task = None
        self.client = httpx.AsyncClient(timeout=2, follow_redirects=False, trust_env=False)
        self.console_targets = {}
        self.object_targets = {}
        self.placement_targets = {}
        self.active = False

    async def start(self):
        if sys.platform != "linux":
            raise ExecutionError(
                "PLATFORM_UNSUPPORTED", "real runtime requires a commissioned Linux host"
            )
        self.kinematics = await asyncio.to_thread(Kinematics, self.config)
        await asyncio.to_thread(self.arm.connect)
        await asyncio.to_thread(self.hands.connect)
        await asyncio.to_thread(self.cameras.connect)
        deadline = time.monotonic() + 10
        while True:
            try:
                self.check_health()
                break
            except ExecutionError:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(0.05)
        with self.model_lock:
            self.kinematics.check_collision(self.arm.sample().q)
        self.active = True
        self.monitor_task = asyncio.create_task(self._monitor())

    async def _monitor(self):
        while self.active:
            try:
                self.check_health()
                self.arm.heartbeat()
            except ExecutionError as exc:
                self.arm.fault = exc
            await asyncio.sleep(0.02)

    def check_health(self):
        self.arm.sample()
        for arm in ("left", "right"):
            self.hands.state(arm)
        for source in self.config.cameras:
            self.cameras.frame(source)

    async def prepare(self, arm, tool_frame, check):
        check()
        with self.model_lock:
            self.kinematics.select_tcp(arm, tool_frame)
            current = self.arm.sample().q
            self.kinematics.check_collision(current)
            if not self.arm.enabled:
                self.arm.acquire(self.kinematics.gravity(current))
        while self.arm.weight < 1:
            check()
            await asyncio.sleep(0.02)

    def _locate(self, kind, arm, frame=None):
        source = "head" if arm is None else f"{arm}_wrist"
        camera_config = self.config.cameras[source]
        frame = self.cameras.frame(source) if frame is None else frame
        if camera_config.parent == "base":
            camera_pose = camera_config.parent_to_camera
        else:
            side = camera_config.parent.split("_")[0]
            sample = self.arm.sample_at(frame.timestamp, camera_config.max_timestamp_skew_s)
            with self.model_lock:
                camera_pose = compose(
                    self.kinematics.fk(sample.q, side, wrist=True), camera_config.parent_to_camera
                )
        if kind == "console":
            definitions = {"panel": self.config.panel_marker}
        elif kind == "placements":
            definitions = {name: place.marker for name, place in self.config.placements.items()}
        else:
            definitions = {name: obj.marker for name, obj in self.config.objects.items()}
        targets = detect_markers(
            frame.rgb,
            definitions,
            frame.camera_matrix,
            frame.distortion,
            camera_pose,
            camera_config.reprojection_error_px,
        )
        if kind == "console":
            if "panel" not in targets:
                raise ExecutionError("TARGET_NOT_VISIBLE", f"panel marker missing in {source}")
            panel = targets["panel"]
            return {
                name: compose(panel, button.pose_in_panel)
                for name, button in self.config.buttons.items()
            } | {"__panel__": panel}
        return targets

    async def locate(self, kind, arm=None):
        self.check_health()
        targets = await asyncio.to_thread(self._locate, kind, arm)
        if kind == "console":
            self.console_targets = targets
        elif kind == "placements":
            self.placement_targets = targets
        else:
            self.object_targets = targets
        return targets

    async def hand(self, arm, posture, check, *, object_id=None):
        check()
        definition = self.config.postures[posture]
        self.hands.command(arm, definition)
        for _ in range(5):
            check()
            await asyncio.sleep(0.02)
        deadline = time.monotonic() + 10
        while True:
            check()
            positions, current = self.hands.state(arm)
            reached = np.max(np.abs(positions - definition.q)) < definition.tolerance
            contact = (
                object_id is not None
                and np.max(np.abs(current)) >= self.config.objects[object_id].min_current_a
            )
            if reached or contact:
                break
            if time.monotonic() >= deadline:
                raise ExecutionError(
                    "HAND_TIMEOUT", "hand did not reach posture or calibrated grasp contact"
                )
            await asyncio.sleep(0.02)
        if object_id:
            with self.model_lock:
                self.kinematics.attach_payload(arm, self.config.objects[object_id])
        elif posture in {obj.open_posture for obj in self.config.objects.values()}:
            with self.model_lock:
                self.kinematics.detach_payload(arm)

    def _plan(self, arm, goal, speed, contact, lifting, stop):
        with self.model_lock:
            motors = self.arm.sample().q.copy()
            start = self.kinematics.fk(motors, arm)
            delta = distance(start, goal)
            if delta > self.config.limits.max_translation_m:
                raise ExecutionError(
                    "TRANSLATION_LIMIT", "requested move exceeds local motion limit"
                )
            self.kinematics.check_collision(motors, contact=contact, lifting=lifting)
            count = max(
                1, math.ceil(delta / 0.02), math.ceil(orientation_error(start, goal) / 0.05)
            )
            plan = []
            for index in range(1, count + 1):
                if stop.is_set():
                    raise ExecutionError("PLAN_CANCELLED", "planning interrupted")
                pose = interpolate(start, goal, index / count)
                check_workspace(pose, self.config.limits.workspace)
                solution = self.kinematics.solve(motors, arm, pose)
                samples = list(joint_segment(motors, solution, self.config))
                # Cartesian speed is bounded as well as joint speed; add interpolation samples if needed.
                previous_pose = self.kinematics.fk(motors, arm)
                minimum = math.ceil(
                    1.875 * distance(previous_pose, pose) / speed * self.config.hardware.control_hz
                )
                if minimum > len(samples):
                    samples = [
                        motors + (10 * t**3 - 15 * t**4 + 6 * t**5) * (solution - motors)
                        for t in np.linspace(0, 1, minimum + 1)[1:]
                    ]
                previous_xyz = np.array(previous_pose.position)
                for sample in samples:
                    if stop.is_set():
                        raise ExecutionError("PLAN_CANCELLED", "planning interrupted")
                    tcp = self.kinematics.fk(sample, arm)
                    check_workspace(tcp, self.config.limits.workspace)
                    if (
                        np.linalg.norm(np.array(tcp.position) - previous_xyz)
                        * self.config.hardware.control_hz
                        > speed * 1.05
                    ):
                        raise ExecutionError(
                            "CARTESIAN_SPEED", "IK interpolation exceeds Cartesian velocity bound"
                        )
                    self.kinematics.check_collision(sample, contact=contact, lifting=lifting)
                    plan.append((sample[15:29].copy(), self.kinematics.gravity(sample)))
                    previous_xyz = np.array(tcp.position)
                motors = solution
            return plan

    async def move(self, arm, pose, speed, check, *, purpose="approach", target_id=None):
        check()
        check_workspace(pose, self.config.limits.workspace)
        if not 0 < speed <= self.config.limits.max_speed_m_s:
            raise ExecutionError("SPEED_LIMIT", "speed exceeds commissioned bound")
        contact = None
        lifting = None
        if purpose in {"press", "retract"} and target_id in self.console_targets:
            contact = (arm, self.console_targets[target_id], self.config.buttons[target_id])
        if purpose == "lift" and target_id in self.object_targets:
            lifting = (arm, self.object_targets[target_id], self.config.objects[target_id])
        if purpose == "place" and target_id in self.placement_targets:
            lifting = (arm, self.placement_targets[target_id], self.config.placements[target_id])
        stop = threading.Event()
        plan_task = asyncio.create_task(
            asyncio.to_thread(self._plan, arm, pose, speed, contact, lifting, stop)
        )
        try:
            while not plan_task.done():
                check()
                await asyncio.sleep(0.01)
            plan = await plan_task
        except BaseException:
            stop.set()
            await asyncio.gather(plan_task, return_exceptions=True)
            raise
        dt = 1 / self.config.hardware.control_hz
        for q, tau in plan:
            begin = time.monotonic()
            check()
            with self.model_lock:
                self.kinematics.check_collision(
                    self.arm.sample().q, contact=contact, lifting=lifting
                )
            self.arm.command(q, tau)
            await asyncio.sleep(max(dt - (time.monotonic() - begin), 0))
        deadline = time.monotonic() + 3
        while True:
            check()
            with self.model_lock:
                actual = self.kinematics.fk(self.arm.sample().q, arm)
            if (
                distance(actual, pose) <= self.config.limits.position_tolerance_m
                and orientation_error(actual, pose) <= self.config.limits.orientation_tolerance_rad
            ):
                return
            if time.monotonic() > deadline:
                raise ExecutionError("POSE_NOT_REACHED", "measured TCP did not settle at target")
            await asyncio.sleep(0.02)

    async def feedback(self, button_id):
        key = self.config.buttons[button_id].feedback_key
        try:
            response = await self.client.get(
                self.config.hardware.feedback_url.rstrip("/") + "/buttons/" + quote(key, safe="")
            )
            response.raise_for_status()
            document = FeedbackDocument.model_validate(response.json())
            if document.button_id != key:
                raise ValueError("feedback target mismatch")
            if document.source != self.config.hardware.feedback_source:
                raise ValueError("feedback source differs from commissioned source")
            return Feedback(
                document.event_count,
                document.state,
                document.timestamp,
                document.source,
                document.epoch,
            )
        except (httpx.HTTPError, ValueError) as exc:
            raise ExecutionError("FEEDBACK_UNAVAILABLE", str(exc)) from exc

    async def grasp_confirmed(self, object_id, arm, before, check):
        profile = self.config.objects[object_id]
        relative_positions = []
        last_seen = 0.0
        for _ in range(3):
            check()
            frame = self.cameras.frame("head")
            if frame.timestamp <= last_seen:
                await asyncio.sleep(0.08)
                frame = self.cameras.frame("head")
            if frame.timestamp <= last_seen:
                return False
            targets = await asyncio.to_thread(self._locate, "objects", None, frame)
            if object_id not in targets:
                return False
            object_pose = targets[object_id]
            if object_pose.position[2] - before.position[2] < profile.lift_m * 0.8:
                return False
            sample = self.arm.sample_at(
                frame.timestamp, self.config.cameras["head"].max_timestamp_skew_s
            )
            with self.model_lock:
                tcp = self.kinematics.fk(sample.q, arm)
            relative_positions.append(np.array(object_pose.position) - tcp.position)
            _, currents = self.hands.state(arm)
            if np.max(np.abs(currents)) < profile.min_current_a:
                return False
            last_seen = frame.timestamp
            await asyncio.sleep(0.08)
        return (
            max(np.linalg.norm(p - relative_positions[0]) for p in relative_positions)
            <= self.config.limits.position_tolerance_m * 2
        )

    async def stopped(self):
        try:
            if self.arm.enabled:
                self.arm.hold()
            self.hands.hold()
            deadline = time.monotonic() + 1
            stable = 0
            last_arm_stamp = None
            previous_hands = {}
            while time.monotonic() < deadline:
                sample = self.arm.sample(allow_fault=True)
                hand_stable = True
                for arm in self.hands.commanded:
                    q, _, stamp = self.hands.sample(arm, allow_overcurrent=True)
                    previous = previous_hands.get(arm)
                    if (
                        previous is None
                        or stamp <= previous[1]
                        or np.max(np.abs(q - previous[0])) > 0.002
                    ):
                        hand_stable = False
                    previous_hands[arm] = (q, stamp)
                fresh = sample.monotonic != last_arm_stamp
                stable = (
                    stable + 1
                    if fresh and hand_stable and np.max(np.abs(sample.dq[15:29])) < 0.03
                    else 0
                )
                last_arm_stamp = sample.monotonic
                if stable >= 5:
                    return True
                await asyncio.sleep(0.02)
            return False
        except ExecutionError:
            return False

    async def placement_confirmed(self, object_id, target, check):
        last_seen = 0.0
        for _ in range(3):
            check()
            frame = self.cameras.frame("head")
            if frame.timestamp <= last_seen:
                return False
            targets = await asyncio.to_thread(self._locate, "objects", None, frame)
            if (
                object_id not in targets
                or distance(targets[object_id], target)
                > self.config.limits.position_tolerance_m * 2
            ):
                return False
            last_seen = frame.timestamp
            await asyncio.sleep(0.08)
        return True

    async def image(self, source):
        import cv2

        frame = self.cameras.frame(source)
        success, data = cv2.imencode(".png", frame.rgb)
        if not success:
            raise ExecutionError("IMAGE_ENCODING", "camera image could not be encoded")
        return data.tobytes(), frame.timestamp

    async def close(self):
        self.active = False
        if self.monitor_task:
            await self.monitor_task
        try:
            await self.stopped()
        finally:
            try:
                await self.client.aclose()
            finally:
                try:
                    await asyncio.to_thread(self.cameras.close)
                finally:
                    try:
                        self.hands.close()
                    finally:
                        self.arm.close()
