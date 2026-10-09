import time
from dataclasses import replace

import pytest

from g1_tabletop.backends.mock import MockBackend
from g1_tabletop.config import mock_config
from g1_tabletop.errors import ExecutionError
from g1_tabletop.runtime import Runtime
from g1_tabletop.specs import ActionOutcome
from g1_tabletop.store import InvocationStore


async def create_runtime(tmp_path):
    config = mock_config()
    runtime = Runtime(config, MockBackend(config), tmp_path / "runtime.sqlite3")
    await runtime.start()
    return runtime


async def arguments(runtime):
    obs = await runtime.query("console.observe", {})
    return {
        "observation_id": obs["observation_id"],
        "button_id": "button_01",
        "press_profile_id": "button_01",
        "arm": "right",
    }


async def test_restart_retains_unknown_and_deduplicates_without_replay(tmp_path):
    runtime = await create_runtime(tmp_path)
    params = await arguments(runtime)
    original = runtime.store.create("crashed", "console.press_button", params)
    await runtime.close()
    runtime = await create_runtime(tmp_path)
    try:
        assert runtime.result(original["invocation_id"])["phase"] == "unknown"
        assert await runtime.submit("console.press_button", params, "crashed") == original
        with pytest.raises(ExecutionError, match="UNRESOLVED_EXECUTION"):
            await runtime.submit("console.press_button", await arguments(runtime), "new")
        assert runtime.backend.press_count == 0
        runtime.store.reconcile_empty("Stopped and inspected; both hands empty.", {"mode": "mock"})
        result = runtime.result(original["invocation_id"])["result"]
        ActionOutcome.model_validate(result)
        assert result["status"] == "failed"
    finally:
        await runtime.close()


async def test_occupied_restart_requires_operator_inspection(tmp_path):
    runtime = await create_runtime(tmp_path)
    runtime.store.set_state("occupied", {"left": {"state": "possible", "object_id": "block_01"}})
    await runtime.close()
    runtime = await create_runtime(tmp_path)
    try:
        with pytest.raises(ExecutionError, match="HOLD_RECONCILIATION_REQUIRED"):
            await runtime.submit("console.press_button", await arguments(runtime), "press")
    finally:
        await runtime.close()


async def test_uncertain_grasp_blocks_other_arm_even_before_restart(tmp_path):
    runtime = await create_runtime(tmp_path)
    try:
        runtime.store.set_state(
            "occupied", {"left": {"state": "possible", "object_id": "block_01"}}
        )
        with pytest.raises(ExecutionError, match="HOLD_UNCONFIRMED"):
            await runtime.submit("console.press_button", await arguments(runtime), "other-arm")
        assert runtime.backend.press_count == 0
    finally:
        await runtime.close()


async def test_ensure_state_does_not_toggle_already_correct_button(tmp_path):
    runtime = await create_runtime(tmp_path)
    try:
        before = await runtime.backend.feedback("button_01")
        params = dict(await arguments(runtime), intent="ensure_state", desired_state=before.state)
        identity = await runtime.submit("console.press_button", params, "ensure")
        await runtime.task
        result = runtime.result(identity["invocation_id"])["result"]
        assert result["status"] == "succeeded" and result["outputs"]["already_satisfied"]
        assert runtime.backend.moves == [] and runtime.backend.press_count == 0
    finally:
        await runtime.close()


@pytest.mark.parametrize("fault", ["stale_feedback", "stale_state", "collision", "missing_trigger"])
async def test_faults_cannot_be_reported_as_success(tmp_path, fault):
    runtime = await create_runtime(tmp_path)
    try:
        params = await arguments(runtime)
        runtime.backend.faults.add(fault)
        try:
            identity = await runtime.submit("console.press_button", params, fault)
        except ExecutionError:
            assert runtime.backend.moves == []
        else:
            await runtime.task
            assert runtime.result(identity["invocation_id"])["phase"] in {"failed", "unknown"}
            assert runtime.backend.press_count <= 1
    finally:
        await runtime.close()


async def test_feedback_epoch_change_never_counts_as_button_event(tmp_path):
    runtime = await create_runtime(tmp_path)
    original = runtime.backend.feedback

    async def feedback(button):
        value = await original(button)
        return replace(value, epoch="new-device") if runtime.backend.press_count else value

    runtime.backend.feedback = feedback
    try:
        identity = await runtime.submit("console.press_button", await arguments(runtime), "epoch")
        await runtime.task
        result = runtime.result(identity["invocation_id"])["result"]
        assert result["status"] != "succeeded" and result["error"]["code"] == "TRIGGER_UNCONFIRMED"
    finally:
        await runtime.close()


async def test_failed_stop_retains_workspace_and_diagnostics(tmp_path):
    runtime = await create_runtime(tmp_path)
    try:
        runtime.backend.faults.update({"missing_trigger", "stop_unknown"})
        identity = await runtime.submit("console.press_button", await arguments(runtime), "unknown")
        await runtime.task
        assert runtime.result(identity["invocation_id"])["phase"] == "unknown"
        assert runtime.active_id == identity["invocation_id"]
        assert (await runtime.query("console.read_state", {"button_id": "button_01"}))[
            "timestamp"
        ] <= time.time()
        with pytest.raises(ExecutionError, match="UNRESOLVED_EXECUTION"):
            await runtime.submit(
                "console.press_button",
                {
                    "observation_id": "ignored",
                    "button_id": "button_01",
                    "press_profile_id": "button_01",
                    "arm": "left",
                },
                "again",
            )
    finally:
        await runtime.close()


async def test_persistence_failure_stops_and_blocks_new_actions(tmp_path, monkeypatch):
    runtime = await create_runtime(tmp_path)

    def fail(*_):
        raise OSError("disk full")

    monkeypatch.setattr(runtime.store, "finish", fail)
    try:
        await runtime.submit("console.press_button", await arguments(runtime), "disk")
        await runtime.task
        assert runtime.persistence_fault and runtime.active_id
        assert runtime.context("console.press_button")["binding_error"] == "PERSISTENCE_FAILURE"
    finally:
        await runtime.close()


async def test_close_cooperatively_cancels_and_releases_database(tmp_path):
    runtime = await create_runtime(tmp_path)
    runtime.backend.delay_s = 0.03
    await runtime.submit("console.press_button", await arguments(runtime), "shutdown")
    await runtime.close()
    assert runtime.task.done()
    store = InvocationStore(tmp_path / "runtime.sqlite3")
    store.close()


def test_database_open_error_does_not_leak_lease(tmp_path, monkeypatch):
    import sqlite3

    def fail(*_):
        raise sqlite3.OperationalError("test open failure")

    original = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", fail)
    with pytest.raises(sqlite3.OperationalError):
        InvocationStore(tmp_path / "state.db")
    monkeypatch.setattr(sqlite3, "connect", original)
    store = InvocationStore(tmp_path / "state.db")
    with pytest.raises(ExecutionError, match="RUNTIME_OWNED"):
        InvocationStore(tmp_path / "state.db")
    store.close()
