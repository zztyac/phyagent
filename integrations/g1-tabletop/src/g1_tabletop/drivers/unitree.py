"""G1-29 arm-SDK DDS adapter, based on Unitree's public message contracts.

Only rt/arm_sdk is supported: this process never writes whole-body rt/lowcmd.
No motion is published by construction/start. acquire() initializes targets from
fresh measured positions before a gradual arm-SDK weight ramp.
"""

from __future__ import annotations

import hashlib
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from g1_tabletop.config import RuntimeConfig
from g1_tabletop.errors import ExecutionError
from g1_tabletop.ownership import FileLease

ARM_MOTORS = tuple(range(15, 29))
JOINT_NAMES = (
    "left_hip_pitch_joint",
    "left_hip_roll_joint",
    "left_hip_yaw_joint",
    "left_knee_joint",
    "left_ankle_pitch_joint",
    "left_ankle_roll_joint",
    "right_hip_pitch_joint",
    "right_hip_roll_joint",
    "right_hip_yaw_joint",
    "right_knee_joint",
    "right_ankle_pitch_joint",
    "right_ankle_roll_joint",
    "waist_yaw_joint",
    "waist_roll_joint",
    "waist_pitch_joint",
    "left_shoulder_pitch_joint",
    "left_shoulder_roll_joint",
    "left_shoulder_yaw_joint",
    "left_elbow_joint",
    "left_wrist_roll_joint",
    "left_wrist_pitch_joint",
    "left_wrist_yaw_joint",
    "right_shoulder_pitch_joint",
    "right_shoulder_roll_joint",
    "right_shoulder_yaw_joint",
    "right_elbow_joint",
    "right_wrist_roll_joint",
    "right_wrist_pitch_joint",
    "right_wrist_yaw_joint",
)


@dataclass(frozen=True)
class JointSample:
    q: np.ndarray
    dq: np.ndarray
    mode_machine: int
    monotonic: float
    wall_time: float
    imu_wxyz: tuple[float, float, float, float] = (1, 0, 0, 0)
    tau_est: np.ndarray | None = None


def validate_arm_target(config: RuntimeConfig, current, target, torque):
    hardware = config.hardware
    q, tau = np.asarray(target, dtype=float), np.asarray(torque, dtype=float)
    if (
        q.shape != (14,)
        or tau.shape != (14,)
        or not np.isfinite(q).all()
        or not np.isfinite(tau).all()
    ):
        raise ExecutionError("INVALID_JOINT_COMMAND", "expected finite arrays of 14 arm joints")
    if np.any(q < hardware.joint_lower_rad) or np.any(q > hardware.joint_upper_rad):
        raise ExecutionError("JOINT_LIMIT", "command exceeds commissioned joint limits")
    if np.any(np.abs(q - current) > config.limits.joint_tracking_rad):
        raise ExecutionError("TRACKING_ERROR", "joint command too far from measured state")
    if np.any(np.abs(tau) > hardware.torque_limit_nm):
        raise ExecutionError("TORQUE_LIMIT", "feedforward exceeds configured torque limits")
    return q, tau


class UnitreeArm:
    def __init__(self, config: RuntimeConfig):
        self.config = config
        self.hardware = config.hardware
        self.lock = threading.RLock()
        self.samples: deque[JointSample] = deque(maxlen=1000)
        self.exit = threading.Event()
        self.thread = None
        self.publisher = None
        self.subscriber = None
        self.enabled = False
        self.fault: ExecutionError | None = None
        self.target = None
        self.torque = np.zeros(14)
        self.last_heartbeat = 0.0
        self.weight = 0.0
        self.lease = None
        self.hold_latched = False
        self.acquired_mode = None

    def connect(self):
        from unitree_sdk2py.core.channel import (
            ChannelFactoryInitialize,
            ChannelPublisher,
            ChannelSubscriber,
        )
        from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowCmd_
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_, LowState_
        from unitree_sdk2py.utils.crc import CRC

        resource = hashlib.sha256(
            f"{self.hardware.domain_id}:{self.hardware.interface}".encode()
        ).hexdigest()[:24]
        self.lease = FileLease(Path(tempfile.gettempdir()) / f"paos-g1-arm-sdk-{resource}.lock")
        ChannelFactoryInitialize(self.hardware.domain_id, self.hardware.interface)
        self.factory, self.crc = unitree_hg_msg_dds__LowCmd_, CRC()
        self.publisher = ChannelPublisher(self.hardware.arm_topic, LowCmd_)
        self.publisher.Init()
        self.subscriber = ChannelSubscriber("rt/lowstate", LowState_)
        self.subscriber.Init(self._receive, 1)
        self.thread = threading.Thread(target=self._loop, name="g1-arm-publisher", daemon=True)
        self.thread.start()

    def _receive(self, message):
        try:
            if len(message.motor_state) < 29:
                raise ValueError("expected at least 29 motor states")
            q = np.array([m.q for m in message.motor_state[:29]], dtype=float)
            dq = np.array([m.dq for m in message.motor_state[:29]], dtype=float)
            tau_est = np.array([m.tau_est for m in message.motor_state[:29]], dtype=float)
            if (
                not np.isfinite(q).all()
                or not np.isfinite(dq).all()
                or not np.isfinite(tau_est).all()
            ):
                raise ValueError("non-finite motor state")
            imu = tuple(float(v) for v in message.imu_state.quaternion)
            if len(imu) != 4 or not np.isfinite(imu).all() or abs(np.linalg.norm(imu) - 1) > 0.01:
                raise ValueError("invalid IMU quaternion")
            sample = JointSample(
                q, dq, int(message.mode_machine), time.monotonic(), time.time(), imu, tau_est
            )
            with self.lock:
                self.samples.append(sample)
        except Exception as exc:
            self.fault = ExecutionError("INVALID_LOWSTATE", str(exc), uncertain=True)

    def sample(self, *, allow_fault=False) -> JointSample:
        with self.lock:
            if self.fault is not None and not allow_fault:
                raise self.fault
            if (
                not self.samples
                or time.monotonic() - self.samples[-1].monotonic
                > self.config.limits.state_max_age_s
            ):
                raise ExecutionError(
                    "STATE_STALE", "G1 lowstate is missing or stale", uncertain=self.enabled
                )
            sample = self.samples[-1]
            if (
                not allow_fault
                and sample.tau_est is not None
                and np.any(np.abs(sample.tau_est[15:29]) > self.hardware.torque_limit_nm)
            ):
                raise ExecutionError(
                    "MEASURED_TORQUE_LIMIT",
                    "measured arm torque estimate exceeds commissioned bound",
                    uncertain=self.enabled,
                )
            attitude_delta = 2 * np.arccos(
                np.clip(
                    abs(np.dot(sample.imu_wxyz, self.hardware.reference_imu_wxyz))
                    / np.linalg.norm(sample.imu_wxyz),
                    0,
                    1,
                )
            )
            if not allow_fault and attitude_delta > self.config.limits.body_drift_rad:
                raise ExecutionError(
                    "BASE_ATTITUDE_CHANGED",
                    "IMU attitude differs from calibrated fixed-base pose",
                    uncertain=self.enabled,
                )
            if not allow_fault and np.any(
                np.abs(sample.q[:15] - self.hardware.reference_body_q)
                > self.config.limits.body_drift_rad
            ):
                raise ExecutionError(
                    "BASE_POSTURE_CHANGED",
                    "legs/waist differ from calibrated pose",
                    uncertain=self.enabled,
                )
            if (
                not allow_fault
                and self.acquired_mode is not None
                and sample.mode_machine != self.acquired_mode
            ):
                raise ExecutionError(
                    "CONTROL_MODE_CHANGED",
                    "G1 control mode changed after acquisition",
                    uncertain=True,
                )
            return sample

    def sample_at(self, timestamp: float, max_skew: float) -> JointSample:
        with self.lock:
            if not self.samples:
                raise ExecutionError("STATE_STALE", "no synchronized joint samples")
            sample = min(self.samples, key=lambda s: abs(s.wall_time - timestamp))
            if abs(sample.wall_time - timestamp) > max_skew:
                raise ExecutionError(
                    "CAMERA_STATE_SKEW", "image and joint receipt timestamps are too far apart"
                )
            return sample

    def acquire(self, gravity):
        sample = self.sample()
        with self.lock:
            q, tau = validate_arm_target(self.config, sample.q[15:29], sample.q[15:29], gravity)
            self.target, self.torque = q.copy(), tau.copy()
            self.acquired_mode = sample.mode_machine
            self.last_heartbeat = time.monotonic()
            self.enabled = True

    def heartbeat(self):
        with self.lock:
            self.last_heartbeat = time.monotonic()

    def command(self, target, torque):
        sample = self.sample()
        q, tau = validate_arm_target(self.config, sample.q[15:29], target, torque)
        with self.lock:
            if not self.enabled:
                raise ExecutionError("CONTROL_NOT_ACQUIRED", "arm ownership has not been acquired")
            if np.any(np.abs(q - self.target) > self.config.limits.joint_step_rad):
                raise ExecutionError("JOINT_STEP_LIMIT", "trajectory step exceeds configured bound")
            self.target, self.torque = q.copy(), tau.copy()

    def hold(self):
        sample = self.sample(allow_fault=True)
        with self.lock:
            self.target = sample.q[15:29].copy()
            self.hold_latched = True
            self.last_heartbeat = time.monotonic()

    def _tick(self, dt):
        if not self.enabled:
            return
        try:
            self.sample()
        except ExecutionError as exc:
            self.fault = self.fault or exc
        sample = self.sample(allow_fault=True)
        with self.lock:
            if self.fault is None and time.monotonic() - self.last_heartbeat > 0.25:
                self.fault = ExecutionError(
                    "CONTROL_WATCHDOG", "execution heartbeat expired", uncertain=True
                )
            if self.fault and not self.hold_latched:
                self.target = sample.q[15:29].copy()
                self.hold_latched = True
            target, torque = self.target.copy(), self.torque.copy()
            self.weight = min(1.0, self.weight + dt)
            weight = self.weight
        # Explicitly initialize ALL unused command slots to disabled; legs and waist are never commanded.
        message = self.factory()
        message.mode_pr, message.mode_machine = 0, sample.mode_machine
        for motor in message.motor_cmd:
            motor.mode = 0
            motor.q = motor.dq = motor.tau = motor.kp = motor.kd = 0.0
        for i, motor_id in enumerate(ARM_MOTORS):
            motor = message.motor_cmd[motor_id]
            motor.mode, motor.q, motor.dq, motor.tau = 1, float(target[i]), 0.0, float(torque[i])
            motor.kp, motor.kd = self.hardware.kp[i], self.hardware.kd[i]
        message.motor_cmd[29].q = weight
        message.crc = self.crc.Crc(message)
        if self.publisher.Write(message) is False:
            raise ExecutionError("DDS_WRITE_FAILED", "arm command write failed", uncertain=True)

    def _loop(self):
        dt = 1 / self.hardware.control_hz
        while not self.exit.is_set():
            begin = time.monotonic()
            try:
                self._tick(dt)
            except Exception as exc:
                self.fault = (
                    exc
                    if isinstance(exc, ExecutionError)
                    else ExecutionError("DDS_FAILURE", str(exc), uncertain=True)
                )
            self.exit.wait(max(0, dt - (time.monotonic() - begin)))

    def close(self):
        # No zero-pose/reset command on disconnect. Hardware stop/watchdog behavior is commissioned separately.
        self.exit.set()
        if self.thread:
            self.thread.join(timeout=2)
            if self.thread.is_alive():
                raise ExecutionError(
                    "CONTROL_THREAD_ALIVE",
                    "publisher did not stop; ownership retained",
                    uncertain=True,
                )
        for channel in (self.subscriber, self.publisher):
            if channel is not None:
                channel.Close()
        if self.lease:
            self.lease.close()
