"""Stable Forge ToolSpecs, derived from strict input contracts."""

from typing import Literal

from pydantic import Field, StrictBool, model_validator

from g1_tabletop.config import Arm, Pose, StrictModel


class Empty(StrictModel):
    pass


class ReadState(StrictModel):
    button_id: str = Field(min_length=1, max_length=128)


class Press(ReadState):
    observation_id: str = Field(min_length=1, max_length=128)
    arm: Arm
    press_profile_id: str = Field(min_length=1, max_length=128)
    intent: Literal["press_once", "ensure_state"] = "press_once"
    desired_state: StrictBool | None = None

    @model_validator(mode="after")
    def intent_fields(self):
        if (self.intent == "ensure_state") != (self.desired_state is not None):
            raise ValueError("desired_state is required only for ensure_state")
        return self


class Pick(StrictModel):
    object_id: str = Field(min_length=1, max_length=128)
    observation_id: str = Field(min_length=1, max_length=128)
    arm: Arm
    grasp_profile_id: str = Field(min_length=1, max_length=128)


class Place(StrictModel):
    placement_id: str = Field(min_length=1, max_length=128)
    observation_id: str = Field(min_length=1, max_length=128)
    arm: Arm


INPUTS = {
    "console.observe": Empty,
    "console.read_state": ReadState,
    "console.press_button": Press,
    "objects.observe": Empty,
    "manipulation.pick": Pick,
    "placements.observe": Empty,
    "manipulation.place": Place,
}
ACTIONS = {"console.press_button", "manipulation.pick", "manipulation.place"}


class FeedbackValue(StrictModel):
    event_count: int = Field(ge=0)
    state: bool | None
    timestamp: float
    source: str
    epoch: str


class Target(StrictModel):
    target_id: str
    label: str
    pose: Pose
    profile_id: str


class ObservationResult(StrictModel):
    observation_id: str
    targets: list[Target]
    frame_id: Literal["g1_base"]
    timestamp: float
    calibration_id: str
    mode: Literal["mock", "real"]


class ActionOutputs(StrictModel):
    # Restart results may lack live configuration; they never claim successful execution.
    mode: Literal["mock", "real"] | None = None
    calibration_id: str | None = None
    operator_reconciliation: dict | None = None
    trigger_confirmed: bool | None = None
    press_attempted: bool | None = None
    already_satisfied: bool | None = None
    retracted: bool | None = None
    feedback_before: FeedbackValue | None = None
    feedback_after: FeedbackValue | None = None
    grasp_confirmed: bool | None = None
    hand_occupied: bool | None = None
    object_id: str | None = None
    placement_confirmed: bool | None = None
    stopped_confirmed: bool | None = None


class ActionError(StrictModel):
    code: str
    message: str
    retryable: Literal[False] = False


class ActionOutcome(StrictModel):
    status: Literal["succeeded", "failed", "cancelled", "unknown"]
    outputs: ActionOutputs
    error: ActionError | None

    @model_validator(mode="after")
    def terminal_consistency(self):
        if (self.status == "succeeded") != (self.error is None):
            raise ValueError("only succeeded outcomes may omit an error")
        if self.status == "succeeded" and (
            self.outputs.mode is None or self.outputs.calibration_id is None
        ):
            raise ValueError("successful outcomes require execution provenance")
        return self


def tool_specs(config):
    descriptions = {
        "console.observe": "Observe calibrated console targets. Returns fresh observation references; does not move the robot.",
        "console.read_state": "Read actual console feedback; a successful query does not mean the button has triggered.",
        "console.press_button": "Press a calibrated button once, release, retract and verify feedback. Never repeat an uncertain press.",
        "objects.observe": "Observe configured marker-bearing objects and available grasp profiles.",
        "manipulation.pick": "Grasp a configured object, trial-lift and verify stable holding. Success leaves the hand occupied.",
        "placements.observe": "Observe calibrated placement zones without moving the robot.",
        "manipulation.place": "Place the currently held object in a calibrated zone, release, retract and verify placement.",
    }
    result = {}
    for tool_id, model in INPUTS.items():
        if not config.objects and tool_id in {"objects.observe", "manipulation.pick"}:
            continue
        if not config.placements and tool_id in {"placements.observe", "manipulation.place"}:
            continue
        namespace, operation = tool_id.split(".", 1)
        result[tool_id] = {
            "tool_id": tool_id,
            "implementation_id": "g1-tabletop-v1",
            "endpoint_id": namespace,
            "operation": operation,
            "semantics": "action" if tool_id in ACTIONS else "query",
            "description": descriptions[tool_id],
            "input_schema": model.model_json_schema(),
            "output_schema": (
                ActionOutcome
                if tool_id in ACTIONS
                else FeedbackValue
                if tool_id == "console.read_state"
                else ObservationResult
            ).model_json_schema(),
            "robot_frame_profile": {
                "base_frame": "g1_base",
                "tool_frame": "calibrated_tcp",
                "length_unit": "m",
                "angle_unit": "rad",
            },
        }
    return result
