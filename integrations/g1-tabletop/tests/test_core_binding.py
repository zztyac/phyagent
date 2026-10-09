import asyncio
import importlib.util
import socket
from pathlib import Path

import httpx
import uvicorn

from g1_tabletop.backends.mock import MockBackend
from g1_tabletop.config import mock_config
from g1_tabletop.gateway import create_app
from g1_tabletop.runtime import Runtime

CORE = Path(__file__).resolve().parents[3]
builder_spec = importlib.util.spec_from_file_location(
    "g1_bundle_builder", CORE / "integrations/g1-tabletop/scripts/build_bundle.py"
)
builder = importlib.util.module_from_spec(builder_spec)
builder_spec.loader.exec_module(builder)


async def test_actual_skill_activation_task_binding_execution_and_finalization(
    tmp_path, monkeypatch
):
    import shutil

    from PhyAgentOS.agent.experience.activation import SkillActivationManager
    from PhyAgentOS.agent.experience.store import ExperienceStore
    from PhyAgentOS.config.schema import ForgeConfig
    from PhyAgentOS.forge.binding import ForgeSkillBindingResolver
    from PhyAgentOS.forge.task import AgentTaskCoordinator, AgentTaskStatus
    from PhyAgentOS.forge.tool_client import ForgeToolClient
    from PhyAgentOS.skill_runtime.catalog import SkillCatalog
    from PhyAgentOS.skill_runtime.integration import ActiveRuntimeRegistry, ActiveSkillRuntime
    from PhyAgentOS.verification.contracts import TaskVerificationContract

    built = tmp_path / "built"
    builder.build(built, "http://127.0.0.1:18080")
    skills = tmp_path / "skills"
    shutil.copytree(built / "skill-source", skills / "g1-tabletop")
    config = mock_config()
    robot = Runtime(config, MockBackend(config), tmp_path / "robot.sqlite3")
    await robot.start()
    client = ForgeToolClient(
        "http://127.0.0.1:19002",
        transport=httpx.ASGITransport(app=create_app(robot, manage_lifespan=False)),
    )
    invocations, sessions, bindings = set(), set(), set()
    active = ActiveSkillRuntime(
        skill_name="g1-tabletop",
        skill_version="0.1.0",
        profile="mock",
        runtime_instance_id="test-runtime",
        gateway_url="http://127.0.0.1:19002",
        gateway_identity="test-gateway",
        client=client,
        invocation_ids=invocations,
        session_ids=sessions,
        task_binding_ids=bindings,
    )
    resolver = ForgeSkillBindingResolver(
        ActiveRuntimeRegistry(active), catalog=SkillCatalog(skills)
    )
    workspace = tmp_path / "workspace"
    monkeypatch.setattr("PhyAgentOS.agent.skills.get_config_path", lambda: tmp_path / "config.json")
    activation = SkillActivationManager(
        workspace=workspace,
        store=ExperienceStore(workspace),
        runtime_availability_provider=lambda name: name == "g1-tabletop",
        binding_resolver=resolver,
    )
    activation.begin_turn("cli:test", "press synthetic button")
    activated, _, _ = await activation.activate(
        session_key="cli:test", name="g1-tabletop", role="primary"
    )
    coordinator = AgentTaskCoordinator(
        workspace=workspace,
        config=ForgeConfig(),
        client=client,
        binding_resolver=resolver,
        activation_manager=activation,
        runtime_invocation_ids=invocations,
        runtime_session_ids=sessions,
        runtime_task_binding_ids=bindings,
    )

    async def no_capture(_):
        pass

    monkeypatch.setattr(coordinator, "_capture_before", no_capture)
    monkeypatch.setattr(coordinator, "_capture_after", no_capture)
    try:
        task = await coordinator.create_task(
            task_description="press synthetic button",
            verification=TaskVerificationContract(mode="off"),
            activation_id=activated.activation_id,
            origin_session_key="cli:test",
        )
        assert task.primary_skill_binding.skill_name == "g1-tabletop"
        observation = (await coordinator.invoke_query(task.task_id, "console.observe", {}))["data"]
        accepted = await coordinator.start_action(
            task.task_id,
            "console.press_button",
            {
                "observation_id": observation["observation_id"],
                "button_id": "button_01",
                "press_profile_id": "button_01",
                "arm": "right",
            },
        )
        identity = accepted["data"]["invocation_id"]
        assert identity in invocations
        await robot.task
        outcome = await client.invocation_result(identity)
        coordinator.observe_action(task.task_id, identity, outcome)
        assert outcome["data"]["result"]["outputs"]["trigger_confirmed"]
        result = await coordinator.finalize_task(task.task_id)
        assert result.status == AgentTaskStatus.SUCCEEDED
        assert not invocations and not bindings and robot.backend.press_count == 1
    finally:
        await client.close()
        await robot.close()


async def test_live_websockets_feed_core_observation_collector(tmp_path):
    from PhyAgentOS.forge.observation import ForgeObservationCollector

    config = mock_config()
    robot = Runtime(config, MockBackend(config), tmp_path / "ws.sqlite3")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(robot), host="127.0.0.1", log_level="error", ws="websockets-sansio"
        )
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    collector = ForgeObservationCollector(
        f"http://127.0.0.1:{port}",
        required_image_sources=["head", "left_wrist", "right_wrist"],
        require_state=True,
        max_artifact_bytes=100000,
    )
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        await collector.start()
        snapshot = await collector.wait_for_before(3)
        assert set(snapshot.images) == {"head", "left_wrist", "right_wrist"}
        assert snapshot.state.payload["mode"] == "mock"
        assert all(im.data.startswith(b"\x89PNG\r\n\x1a\n") for im in snapshot.images.values())
    finally:
        await collector.close()
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()
