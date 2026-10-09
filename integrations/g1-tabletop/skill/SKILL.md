---
name: g1-tabletop
description: Operate a calibrated G1 EDU console-button and tabletop pick/place runtime without a VLA policy.
metadata: {"PhyAgentOS":{"always":false,"requires":{"runtime":["g1-tabletop"]}}}
---

# G1 console and tabletop operations

Use this Skill only when its Runtime has been explicitly started and the required Tool contexts are ready.
Read contexts to determine mock/real mode, available target/profile IDs, calibration, workspace and hand occupancy.
Mock observations and results are synthetic and must never be described as physical robot validation.

Activate this Skill as primary in the current turn and create one task-bound Forge AgentTask before operation.
For the initial verified primitives use audit verification, or off for offline conformance tests without a model service.
Record the user's goal and explicit outcome criteria. A successful Query only means the read succeeded.

1. Use forge_tool_context, then task-bound forge_tool_query with console.observe, objects.observe or placements.observe.
2. Select a returned target ID and calibrated profile ID. Do not invent coordinates, gains, hand commands or limits.
3. Use forge_tool_start_action with console.press_button, manipulation.pick or manipulation.place.
4. Retain invocation_id and attempt_id. Read forge_tool_action_status and forge_tool_action_result until a settled result.
5. Finalize only after all task-owned actions settle. Report execution facts and actual evidence; HTTP 202 is admission only.

Press accepts press_once, or ensure_state with an explicitly requested boolean desired_state and available feedback.
Success requires confirmed trigger or an already satisfied state, plus bounded release/retraction where motion occurred.
TRIGGER_UNCONFIRMED, FEEDBACK_UNAVAILABLE, unknown or transport timeout must never cause blind repeated pressing.
Read the actual state again before deciding whether any further action is justified. A toggle may already have activated.

Pick requires a configured object and fresh observation. Success leaves the selected hand occupied.
Use a freshly observed placement zone with manipulation.place to release it through the verified placement sequence.
Do not operate an occupied hand on another target. Possible/released-unverified occupancy requires inspection/reconciliation.
Observe again after each physical action; references from an earlier scene revision are invalid.

Cancellation is a request, not proof of physical stop. Continue reading the same invocation until its state is known.
Never resubmit a timed-out request with a new caller identity. Do not bypass Gateway arbitration using shell or SDK scripts.
Unknown execution retains ownership and needs operator reconciliation outside the Agent workflow.

The local runtime owns control-rate feedback, collision checks, watchdogs and bounded cleanup. Do not issue per-joint commands.
The configured workspace must already account for the console installation and robot posture; this Skill does not move the base.
