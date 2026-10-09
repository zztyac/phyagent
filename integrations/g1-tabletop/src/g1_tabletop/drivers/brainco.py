"""BrainCo Revo2 via Unitree brainco_hand_service, six normalized actuators per hand."""

import threading
import time

import numpy as np

from g1_tabletop.errors import ExecutionError


class BrainCo:
    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.states = {}
        self.publishers, self.subscribers = {}, {}
        self.commanded = set()

    def connect(self):
        from unitree_sdk2py.core.channel import ChannelPublisher, ChannelSubscriber
        from unitree_sdk2py.idl.default import unitree_go_msg_dds__MotorCmd_
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import MotorCmds_, MotorStates_

        self.command_type, self.motor_factory = MotorCmds_, unitree_go_msg_dds__MotorCmd_
        for arm in ("left", "right"):
            publisher = ChannelPublisher(f"rt/brainco/{arm}/cmd", MotorCmds_)
            publisher.Init()
            self.publishers[arm] = publisher
            subscriber = ChannelSubscriber(f"rt/brainco/{arm}/state", MotorStates_)
            subscriber.Init(lambda message, side=arm: self._receive(side, message), 1)
            self.subscribers[arm] = subscriber

    def _receive(self, arm, message):
        if len(message.states) != 6:
            return
        q = np.array([m.q for m in message.states], dtype=float)
        # Upstream tau_est is current / 1000, not measured joint torque.
        current_a = np.array([m.tau_est for m in message.states], dtype=float)
        if (
            not np.isfinite(q).all()
            or not np.isfinite(current_a).all()
            or np.any(q < 0)
            or np.any(q > 1)
        ):
            return
        with self.lock:
            self.states[arm] = (q, current_a, time.monotonic())

    def sample(self, arm, *, allow_overcurrent=False):
        with self.lock:
            state = self.states.get(arm)
            if state is None or time.monotonic() - state[2] > self.config.limits.state_max_age_s:
                raise ExecutionError("HAND_STATE_STALE", f"{arm} hand state is missing/stale")
            if not allow_overcurrent and np.any(
                np.abs(state[1]) > self.config.hardware.hand_current_limit_a
            ):
                raise ExecutionError(
                    "HAND_OVERCURRENT", f"{arm} hand exceeded commissioned current limit"
                )
            return state[0].copy(), state[1].copy(), state[2]

    def state(self, arm):
        q, current, _ = self.sample(arm)
        return q, current

    def command(self, arm, posture):
        self.state(arm)
        # Track ownership before writing: a transport error cannot disprove delivery.
        self.commanded.add(arm)
        self._write(arm, posture.q, posture.speed)

    def _write(self, arm, q, speed):
        message = self.command_type()
        message.cmds = [self.motor_factory() for _ in range(6)]
        for index, motor in enumerate(message.cmds):
            motor.q, motor.dq = float(q[index]), float(speed)
        if self.publishers[arm].Write(message) is False:
            raise ExecutionError(
                "HAND_WRITE_FAILED", "BrainCo command write failed", uncertain=True
            )

    def hold(self):
        """Stop only hands previously commanded here; never open or release an object."""
        for arm in self.commanded:
            q, _, _ = self.sample(arm, allow_overcurrent=True)
            # q=current is the stop target; zero speed semantics are firmware-dependent.
            self._write(arm, q, 0.1)

    def close(self):
        for channel in [*self.subscribers.values(), *self.publishers.values()]:
            channel.Close()
