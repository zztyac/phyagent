"""Bounded physical actions and durable Forge lifecycle, independent of any LLM."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

import numpy as np
from pydantic import ValidationError

from g1_tabletop.backends.base import Backend, Feedback
from g1_tabletop.config import Pose, RuntimeConfig
from g1_tabletop.errors import ExecutionError
from g1_tabletop.geometry import check_workspace, distance, matrix, offset, orientation_error
from g1_tabletop.specs import (
    ACTIONS,
    INPUTS,
    ActionOutcome,
    FeedbackValue,
    ObservationResult,
    Pick,
    Place,
    Press,
    tool_specs,
)
from g1_tabletop.store import TERMINAL, InvocationStore


@dataclass
class Observation:
    kind: str
    poses: dict[str, Pose]
    created_monotonic: float
    timestamp: float
    calibration_digest: str
    scene_revision: int


class Runtime:
    def __init__(self, config: RuntimeConfig, backend: Backend, database: Path):
        self.config, self.backend = config, backend
        self.store = InvocationStore(database)
        self.specs = tool_specs(config)
        self.observations: dict[str, Observation] = {}
        self.scene_revision = 0
        self.active_id: str | None = None
        self.task: asyncio.Task | None = None
        self.started = False
        self.restart_occupied = bool(self.store.state("occupied", {}))
        self.closing = False
        self.admission_lock = asyncio.Lock()
        self.persistence_fault = False

    async def start(self):
        try:
            await self.backend.start()
            self.backend.check_health()
        except BaseException:
            try:
                await self.backend.close()
            finally:
                self.store.close()
            raise
        self.started = True

    async def close(self):
        if self.closing:
            return
        self.closing = True
        try:
            try:
                if self.task and not self.task.done():
                    self.store.cancel(self.active_id)
                    # Cooperative checkpoints include local recovery; never cancel a moving task blindly.
                    await self.task
            finally:
                await self.backend.close()
        finally:
            self.started = False
            self.store.close()

    def context(self, tool_id: str) -> dict:
        self._spec(tool_id)
        error = None
        try:
            self._healthy()
        except ExecutionError as exc:
            error = exc.code
        return {
            "tool_id": tool_id,
            "ready": error is None,
            "binding_error": error,
            "mode": self.config.mode,
            "calibration_id": self.config.calibration_id,
            "calibration_digest": self.config.digest,
            "active_invocation": self.active_id,
            "hand_occupied": self.store.state("occupied", {}),
            "frame_id": "g1_base",
            "length_unit": "m",
            "angle_unit": "rad",
            "workspace": self.config.limits.workspace.model_dump(),
            "button_profiles": list(self.config.buttons),
            "grasp_profiles": list(self.config.objects),
            "placement_profiles": list(self.config.placements),
            "postures": list(self.config.postures),
            "result_semantics": "verified_primitive_outcome",
        }

    def _spec(self, tool_id):
        if tool_id not in self.specs:
            raise ExecutionError("TOOL_NOT_FOUND", tool_id, status=404)
        return self.specs[tool_id]

    def _healthy(self):
        if self.persistence_fault:
            raise ExecutionError(
                "PERSISTENCE_FAILURE",
                "execution outcome could not be persisted; ownership retained",
            )
        if not self.started or self.closing:
            raise ExecutionError("NOT_READY", "runtime is not accepting actions")
        self.backend.check_health()
        if self.store.uncertain():
            raise ExecutionError(
                "UNRESOLVED_EXECUTION", "an unknown physical outcome retains robot ownership"
            )
        if self.restart_occupied:
            raise ExecutionError(
                "HOLD_RECONCILIATION_REQUIRED",
                "persisted held object requires operator reconciliation after restart",
            )
        if any(
            held.get("state") != "confirmed" for held in self.store.state("occupied", {}).values()
        ):
            raise ExecutionError(
                "HOLD_UNCONFIRMED",
                "uncertain held/released object blocks both arms until operator reconciliation",
            )

    def _arguments(self, tool_id, arguments):
        self._spec(tool_id)
        try:
            json.dumps(arguments, allow_nan=False)
            return INPUTS[tool_id].model_validate(arguments)
        except (ValidationError, ValueError, TypeError) as exc:
            raise ExecutionError("INVALID_ARGUMENTS", str(exc), status=422) from exc

    async def query(self, tool_id: str, arguments: dict) -> dict:
        params = self._arguments(tool_id, arguments)
        if tool_id in ACTIONS:
            raise ExecutionError("WRONG_SEMANTICS", "Action cannot be invoked as Query", status=400)
        # Diagnostics remain readable even when unknown physical execution blocks new motion.
        if not self.started:
            raise ExecutionError("NOT_READY", "runtime has not started")
        self.backend.check_health()
        if tool_id == "console.read_state":
            if params.button_id not in self.config.buttons:
                raise ExecutionError("TARGET_NOT_FOUND", params.button_id, status=404)
            return FeedbackValue.model_validate(
                asdict(self._fresh_feedback(await self.backend.feedback(params.button_id)))
            ).model_dump()
        kind = {
            "console.observe": "console",
            "objects.observe": "objects",
            "placements.observe": "placements",
        }[tool_id]
        revision = self.scene_revision
        poses = await self.backend.locate(kind)
        if revision != self.scene_revision or self.active_id is not None:
            raise ExecutionError("SCENE_MOVING", "observe after the current action settles")
        identity = "obs_" + uuid4().hex
        now = time.monotonic()
        self.observations = {
            k: v
            for k, v in self.observations.items()
            if now - v.created_monotonic <= self.config.limits.observation_ttl_s
        }
        if len(self.observations) >= 256:
            self.observations.pop(next(iter(self.observations)))
        self.observations[identity] = Observation(
            kind, poses, now, time.time(), self.config.digest, revision
        )
        targets = [
            {
                "target_id": name,
                "label": (
                    self.config.buttons[name].label
                    if kind == "console"
                    else self.config.objects[name].label
                    if kind == "objects"
                    else name
                ),
                "pose": pose.model_dump(),
                "profile_id": name,
            }
            for name, pose in poses.items()
            if not name.startswith("__")
        ]
        return ObservationResult.model_validate(
            {
                "observation_id": identity,
                "targets": targets,
                "frame_id": "g1_base",
                "timestamp": time.time(),
                "calibration_id": self.config.calibration_id,
                "mode": self.config.mode,
            }
        ).model_dump(mode="json")

    def _observation(self, params: Press | Pick | Place) -> Observation:
        observation = self.observations.get(params.observation_id)
        if (
            observation is None
            or time.monotonic() - observation.created_monotonic
            > self.config.limits.observation_ttl_s
        ):
            raise ExecutionError("STALE_OBSERVATION", "request a new observation")
        if (
            observation.calibration_digest != self.config.digest
            or observation.scene_revision != self.scene_revision
        ):
            raise ExecutionError("STALE_OBSERVATION", "calibration or scene changed")
        if isinstance(params, Press):
            target, expected_kind, profiles, profile = (
                params.button_id,
                "console",
                self.config.buttons,
                params.press_profile_id,
            )
        elif isinstance(params, Pick):
            target, expected_kind, profiles, profile = (
                params.object_id,
                "objects",
                self.config.objects,
                params.grasp_profile_id,
            )
        else:
            target, expected_kind, profiles, profile = (
                params.placement_id,
                "placements",
                self.config.placements,
                params.placement_id,
            )
        if (
            observation.kind != expected_kind
            or target not in observation.poses
            or target not in profiles
            or profile != target
        ):
            raise ExecutionError(
                "TARGET_MISMATCH", "target, observation and calibrated profile must agree"
            )
        check_workspace(observation.poses[target], self.config.limits.workspace)
        return observation

    async def submit(
        self, tool_id: str, arguments: dict, caller_id: str, timeout_ms: int | None = None
    ) -> dict:
        if tool_id not in ACTIONS:
            raise ExecutionError(
                "WRONG_SEMANTICS", "only bounded Actions can be admitted", status=400
            )
        params = self._arguments(tool_id, arguments)
        if not isinstance(caller_id, str) or not 1 <= len(caller_id) <= 256:
            raise ExecutionError("INVALID_CALLER", "a nonempty caller_id is required", status=422)
        if timeout_ms is not None and (
            isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0
        ):
            raise ExecutionError(
                "INVALID_TIMEOUT", "timeout_ms must be a positive integer", status=422
            )
        async with self.admission_lock:
            duplicate = self.store.find_caller(caller_id, tool_id, arguments)
            if duplicate:
                return duplicate
            self._healthy()
            if self.active_id is not None:
                raise ExecutionError(
                    "RESOURCE_BUSY", "shared dual-arm workspace is owned by another invocation"
                )
            occupied = params.arm in self.store.state("occupied", {})
            if occupied and not isinstance(params, Place):
                raise ExecutionError("HAND_OCCUPIED", "hand is holding or may be holding an object")
            if isinstance(params, Place) and not occupied:
                raise ExecutionError("HAND_EMPTY", "place requires a recorded held object")
            observation = self._observation(params)
            identity = self.store.create(caller_id, tool_id, arguments)
            self.active_id = identity["invocation_id"]
            self.scene_revision += 1
            duration = self.config.limits.action_timeout_s
            if timeout_ms is not None:
                duration = min(duration, timeout_ms / 1000)
            self.task = asyncio.create_task(
                self._execute(identity["invocation_id"], params, observation, duration)
            )
            return identity

    async def _execute(self, identity, params, observation, duration):
        deadline = time.monotonic() + duration
        outputs: dict = {"mode": self.config.mode, "calibration_id": self.config.calibration_id}

        def check():
            if self.store.get(identity)["cancel_requested"]:
                raise ExecutionError("CANCELLED", "cancellation requested")
            if time.monotonic() >= deadline:
                raise ExecutionError("ACTION_TIMEOUT", "local execution deadline exceeded")
            self.backend.check_health()

        try:
            check()
            if isinstance(params, Press):
                await self._press(identity, params, observation, check, outputs)
            elif isinstance(params, Pick):
                await self._pick(identity, params, observation, check, outputs)
            else:
                await self._place(identity, params, observation, check, outputs)
            check()
            result = {"status": "succeeded", "outputs": outputs, "error": None}
            ActionOutcome.model_validate(result)
        except (Exception, asyncio.CancelledError) as exc:
            fault = (
                exc
                if isinstance(exc, ExecutionError)
                else ExecutionError("EXECUTION_ERROR", str(exc), uncertain=True)
            )
            try:
                stopped = await self.backend.stopped()
            except Exception:
                stopped = False
            status = (
                "unknown"
                if fault.uncertain
                or not stopped
                or (outputs.get("press_attempted") and not outputs.get("trigger_confirmed"))
                else ("cancelled" if fault.code == "CANCELLED" else "failed")
            )
            outputs["stopped_confirmed"] = stopped
            result = {"status": status, "outputs": outputs, "error": fault.payload()}
        try:
            result = ActionOutcome.model_validate(result).model_dump(
                mode="json", exclude_unset=True
            )
            self.store.finish(identity, result)
        except Exception:
            self.persistence_fault = True
            try:
                await self.backend.stopped()
            except Exception:
                pass
            return
        if result["status"] != "unknown":
            self.active_id = None

    def _stage(self, identity, name, check):
        check()
        self.store.stage(identity, name)

    async def _pause(self, duration, check):
        end = time.monotonic() + duration
        while time.monotonic() < end:
            check()
            await asyncio.sleep(min(0.01, max(0, end - time.monotonic())))
        check()

    def _fresh_feedback(self, feedback: Feedback) -> Feedback:
        age = time.time() - feedback.timestamp
        if (
            not np.isfinite(feedback.timestamp)
            or not -0.05 <= age <= self.config.limits.feedback_max_age_s
        ):
            raise ExecutionError("FEEDBACK_STALE", "feedback clock/freshness is invalid")
        return feedback

    async def _press(self, identity, params, observation, check, outputs):
        profile = self.config.buttons[params.button_id]
        before = self._fresh_feedback(await self.backend.feedback(params.button_id))
        outputs["feedback_before"] = asdict(before)
        if params.intent == "ensure_state" and before.state == params.desired_state:
            outputs.update(trigger_confirmed=False, already_satisfied=True)
            return
        if params.intent == "ensure_state" and before.state is None:
            raise ExecutionError(
                "STATE_UNAVAILABLE", "ensure_state requires observable function state"
            )
        self._stage(identity, "pointing_posture", check)
        await self.backend.prepare(params.arm, "point", check)
        await self.backend.hand(params.arm, profile.pointing_posture, check)
        target = observation.poses[params.button_id]
        direction = matrix(observation.poses["__panel__"])[:3, :3] @ profile.press_axis_panel
        precontact = offset(target, direction, -profile.precontact_m)
        check_workspace(precontact, self.config.limits.workspace)
        self._stage(identity, "approach", check)
        await self.backend.move(params.arm, precontact, self.config.limits.max_speed_m_s, check)
        local = await self.backend.locate("console", params.arm)
        if (
            params.button_id not in local
            or distance(local[params.button_id], target) > self.config.limits.max_local_correction_m
            or orientation_error(local[params.button_id], target)
            > self.config.limits.max_local_rotation_rad
        ):
            raise ExecutionError(
                "LOCAL_ALIGNMENT_FAILED", "target is missing or moved beyond local correction bound"
            )
        target = local[params.button_id]
        direction = matrix(local["__panel__"])[:3, :3] @ profile.press_axis_panel
        precontact = offset(target, direction, -profile.precontact_m)
        pressed = offset(target, direction, profile.travel_m)
        for pose in (precontact, pressed):
            check_workspace(pose, self.config.limits.workspace)
        await self.backend.move(params.arm, precontact, profile.speed_m_s, check)
        try:
            self._stage(identity, "press", check)
            outputs["press_attempted"] = True
            await self.backend.move(
                params.arm,
                pressed,
                profile.speed_m_s,
                check,
                purpose="press",
                target_id=params.button_id,
            )
            await self._pause(profile.dwell_s, check)
        finally:
            # Only reverse the validated local approach. Ignore cancellation for bounded release,
            # but retain live health/collision checks and a separate short cleanup deadline.
            cleanup_deadline = time.monotonic() + self.config.limits.cleanup_timeout_s

            def cleanup_check():
                self.backend.check_health()
                if time.monotonic() > cleanup_deadline:
                    raise ExecutionError(
                        "RETRACT_TIMEOUT", "bounded release failed", uncertain=True
                    )

            await self.backend.move(
                params.arm,
                precontact,
                profile.speed_m_s,
                cleanup_check,
                purpose="retract",
                target_id=params.button_id,
            )
            outputs["retracted"] = True
        self._stage(identity, "verify_trigger", check)
        end = time.monotonic() + profile.settle_s
        while True:
            check()
            after = self._fresh_feedback(await self.backend.feedback(params.button_id))
            outputs["feedback_after"] = asdict(after)
            confirmed = (
                after.epoch == before.epoch
                and after.source == before.source
                and after.event_count == before.event_count + 1
                and after.timestamp >= before.timestamp
            )
            if params.intent == "ensure_state":
                confirmed = confirmed and after.state == params.desired_state
            if confirmed:
                outputs["trigger_confirmed"] = True
                return
            if time.monotonic() >= end:
                raise ExecutionError(
                    "TRIGGER_UNCONFIRMED",
                    "no reliable trigger feedback; do not press again automatically",
                )
            await self._pause(0.01, check)

    async def _pick(self, identity, params, observation, check, outputs):
        profile = self.config.objects[params.object_id]
        target = observation.poses[params.object_id]
        self._stage(identity, "pregrasp", check)
        await self.backend.prepare(params.arm, "grasp", check)
        await self.backend.hand(params.arm, profile.open_posture, check)
        pregrasp = offset(target, profile.approach_axis, -profile.pregrasp_m)
        check_workspace(pregrasp, self.config.limits.workspace)
        await self.backend.move(params.arm, pregrasp, self.config.limits.max_speed_m_s, check)
        local = await self.backend.locate("objects", params.arm)
        if (
            params.object_id not in local
            or distance(local[params.object_id], target) > self.config.limits.max_local_correction_m
            or orientation_error(local[params.object_id], target)
            > self.config.limits.max_local_rotation_rad
        ):
            raise ExecutionError(
                "LOCAL_ALIGNMENT_FAILED", "object not available for bounded grasp correction"
            )
        target = local[params.object_id]
        check_workspace(target, self.config.limits.workspace)
        await self.backend.move(params.arm, target, self.config.limits.max_speed_m_s / 2, check)
        self._stage(identity, "close_hand", check)
        # Persist possible occupancy BEFORE sending close, including cancellation/crash windows.
        occupied = self.store.state("occupied", {})
        occupied[params.arm] = {
            "object_id": params.object_id,
            "state": "possible",
            "invocation_id": identity,
            "origin": target.model_dump(),
        }
        self.store.set_state("occupied", occupied)
        await self.backend.hand(
            params.arm, profile.close_posture, check, object_id=params.object_id
        )
        self._stage(identity, "trial_lift", check)
        lifted = offset(target, (0, 0, 1), profile.lift_m)
        check_workspace(lifted, self.config.limits.workspace)
        await self.backend.move(
            params.arm,
            lifted,
            self.config.limits.max_speed_m_s / 2,
            check,
            purpose="lift",
            target_id=params.object_id,
        )
        await self._pause(profile.hold_s, check)
        if not await self.backend.grasp_confirmed(params.object_id, params.arm, target, check):
            raise ExecutionError(
                "GRASP_UNCONFIRMED",
                "trial lift did not establish stable grasp; holding state retained",
            )
        occupied[params.arm]["state"] = "confirmed"
        self.store.set_state("occupied", occupied)
        outputs.update(grasp_confirmed=True, hand_occupied=True, object_id=params.object_id)

    async def _place(self, identity, params, observation, check, outputs):
        occupied = self.store.state("occupied", {})
        held = occupied[params.arm]
        object_id = held["object_id"]
        if held["state"] != "confirmed":
            raise ExecutionError(
                "HOLD_UNCONFIRMED", "cannot place an unconfirmed grasp; reconcile first"
            )
        if not await self.backend.grasp_confirmed(
            object_id, params.arm, Pose.model_validate(held["origin"]), check
        ):
            raise ExecutionError("HOLD_LOST", "held object no longer passes visual/hand checks")
        self._stage(identity, "preplace", check)
        await self.backend.prepare(params.arm, "grasp", check)
        profile = self.config.placements[params.placement_id]
        target = observation.poses[params.placement_id]
        above = offset(target, (0, 0, 1), profile.preplace_m)
        check_workspace(above, self.config.limits.workspace)
        await self.backend.move(params.arm, above, self.config.limits.max_speed_m_s / 2, check)
        local = await self.backend.locate("placements", params.arm)
        if (
            params.placement_id not in local
            or distance(target, local[params.placement_id])
            > self.config.limits.max_local_correction_m
            or orientation_error(target, local[params.placement_id])
            > self.config.limits.max_local_rotation_rad
        ):
            raise ExecutionError(
                "LOCAL_ALIGNMENT_FAILED", "placement target not available for local correction"
            )
        target = local[params.placement_id]
        check_workspace(target, self.config.limits.workspace)
        self._stage(identity, "place", check)
        await self.backend.move(
            params.arm,
            target,
            self.config.limits.max_speed_m_s / 3,
            check,
            purpose="place",
            target_id=params.placement_id,
        )
        self._stage(identity, "release", check)
        await self.backend.hand(params.arm, self.config.objects[object_id].open_posture, check)
        occupied[params.arm]["state"] = "released_unverified"
        self.store.set_state("occupied", occupied)
        above = offset(target, (0, 0, 1), profile.preplace_m)
        await self.backend.move(params.arm, above, self.config.limits.max_speed_m_s / 2, check)
        if not await self.backend.placement_confirmed(object_id, target, check):
            raise ExecutionError("PLACEMENT_UNCONFIRMED", "object placement not confirmed")
        occupied.pop(params.arm)
        self.store.set_state("occupied", occupied)
        outputs.update(
            placement_confirmed=True, hand_occupied=False, object_id=object_id, retracted=True
        )

    def status(self, identity):
        row = self.store.get(identity)
        return {
            "invocation_id": identity,
            "attempt_id": row["attempt_id"],
            "phase": row["phase"],
            "stage": row["stage"],
            "cancel_requested": bool(row["cancel_requested"]),
        }

    def result(self, identity):
        row = self.store.get(identity)
        if row["phase"] not in TERMINAL:
            return {"invocation_id": identity, "status": "pending", "phase": row["phase"]}
        return {
            "invocation_id": identity,
            "attempt_id": row["attempt_id"],
            "status": "available",
            "phase": row["phase"],
            "result": ActionOutcome.model_validate(json.loads(row["result"])).model_dump(
                mode="json", exclude_unset=True
            ),
        }

    async def cancel(self, identity):
        row = self.store.get(identity)
        if row["phase"] in TERMINAL:
            return {
                "invocation_id": identity,
                "cancel_status": "already_terminal",
                "phase": row["phase"],
            }
        self.store.cancel(identity)
        return {"invocation_id": identity, "cancel_status": "accepted"}
