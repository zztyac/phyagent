"""Exercise physical-outcome and lifecycle contracts without robot hardware."""

import asyncio

import pytest

from g1_tabletop.backends.mock import MockBackend
from g1_tabletop.config import mock_config
from g1_tabletop.errors import ExecutionError
from g1_tabletop.runtime import Runtime


@pytest.fixture
async def runtime(tmp_path):
    config = mock_config()
    backend = MockBackend(config)
    instance = Runtime(config, backend, tmp_path / "runtime.sqlite3")
    await instance.start()
    yield instance
    await instance.close()


async def press_args(runtime):
    observation = await runtime.query("console.observe", {})
    return {
        "button_id": "button_01",
        "observation_id": observation["observation_id"],
        "arm": "right",
        "press_profile_id": "button_01",
        "intent": "press_once",
    }


async def settled(runtime, identity):
    for _ in range(500):
        result = runtime.status(identity["invocation_id"])
        if result["phase"] in {"succeeded", "failed", "cancelled", "unknown"}:
            return runtime.result(identity["invocation_id"])["result"]
        await asyncio.sleep(0.002)
    pytest.fail("invocation did not settle")


async def test_press_requires_actual_trigger_and_retract(runtime):
    identity = await runtime.submit("console.press_button", await press_args(runtime), "press-1")
    result = await settled(runtime, identity)
    assert result["status"] == "succeeded"
    assert result["outputs"]["trigger_confirmed"] is True
    assert result["outputs"]["retracted"] is True
    assert runtime.backend.press_count == 1


async def test_missing_feedback_never_reports_success_or_represses(runtime):
    runtime.backend.faults.add("missing_trigger")
    identity = await runtime.submit("console.press_button", await press_args(runtime), "press-2")
    result = await settled(runtime, identity)
    assert result["status"] == "unknown"
    assert result["error"]["code"] == "TRIGGER_UNCONFIRMED"
    assert runtime.backend.press_count == 1


async def test_duplicate_request_reuses_identity_and_physical_effect(runtime):
    arguments = await press_args(runtime)
    first = await runtime.submit("console.press_button", arguments, "same-caller")
    duplicate = await runtime.submit("console.press_button", arguments, "same-caller")
    assert first == duplicate
    await settled(runtime, first)
    assert runtime.backend.press_count == 1
    changed = dict(arguments, arm="left")
    with pytest.raises(ExecutionError, match="IDEMPOTENCY_CONFLICT"):
        await runtime.submit("console.press_button", changed, "same-caller")


async def test_expired_observation_never_moves(runtime):
    arguments = await press_args(runtime)
    runtime.observations[arguments["observation_id"]].created_monotonic -= 100
    with pytest.raises(ExecutionError, match="STALE_OBSERVATION"):
        await runtime.submit("console.press_button", arguments, "stale")
    assert runtime.backend.moves == []


async def test_empty_grasp_is_failure(runtime):
    runtime.backend.faults.add("empty_grasp")
    observation = await runtime.query("objects.observe", {})
    identity = await runtime.submit(
        "manipulation.pick",
        {
            "object_id": "block_01",
            "observation_id": observation["observation_id"],
            "arm": "left",
            "grasp_profile_id": "block_01",
        },
        "empty-pick",
    )
    result = await settled(runtime, identity)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "GRASP_UNCONFIRMED"


async def test_cross_tool_resource_conflict(runtime):
    runtime.backend.delay_s = 0.05
    observation = await runtime.query("objects.observe", {})
    first = await runtime.submit("console.press_button", await press_args(runtime), "busy-press")
    with pytest.raises(ExecutionError, match="RESOURCE_BUSY"):
        await runtime.submit(
            "manipulation.pick",
            {
                "object_id": "block_01",
                "observation_id": observation["observation_id"],
                "arm": "left",
                "grasp_profile_id": "block_01",
            },
            "busy-pick",
        )
    await settled(runtime, first)


async def test_cancel_is_acknowledgement_then_observed_terminal(runtime):
    runtime.backend.delay_s = 0.03
    identity = await runtime.submit("console.press_button", await press_args(runtime), "cancel")
    response = await runtime.cancel(identity["invocation_id"])
    assert response["cancel_status"] == "accepted"
    result = await settled(runtime, identity)
    assert result["status"] == "cancelled"
    assert runtime.backend.press_count == 0


async def test_pick_then_place_releases_recorded_occupancy(runtime):
    observation = await runtime.query("objects.observe", {})
    identity = await runtime.submit(
        "manipulation.pick",
        {
            "object_id": "block_01",
            "observation_id": observation["observation_id"],
            "arm": "left",
            "grasp_profile_id": "block_01",
        },
        "pick-to-place",
    )
    assert (await settled(runtime, identity))["status"] == "succeeded"
    assert runtime.store.state("occupied")["left"]["state"] == "confirmed"
    observation = await runtime.query("placements.observe", {})
    identity = await runtime.submit(
        "manipulation.place",
        {
            "placement_id": "zone_01",
            "observation_id": observation["observation_id"],
            "arm": "left",
        },
        "place",
    )
    result = await settled(runtime, identity)
    assert result["status"] == "succeeded"
    assert result["outputs"]["placement_confirmed"]
    assert runtime.store.state("occupied") == {}
