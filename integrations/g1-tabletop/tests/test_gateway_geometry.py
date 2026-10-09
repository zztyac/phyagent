import asyncio

import httpx
import numpy as np
import pytest
from pydantic import ValidationError

from g1_tabletop.backends.mock import MockBackend
from g1_tabletop.config import Pose, RuntimeConfig, mock_config
from g1_tabletop.gateway import create_app
from g1_tabletop.geometry import as_pose, compose, interpolate, matrix
from g1_tabletop.runtime import Runtime


@pytest.fixture
async def api(tmp_path):
    config = mock_config()
    runtime = Runtime(config, MockBackend(config), tmp_path / "api.sqlite3")
    await runtime.start()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, manage_lifespan=False)),
        base_url="http://test",
    ) as client:
        yield client, runtime
    await runtime.close()


async def test_real_forge_client_can_press_and_observe_lifecycle(api):
    from PhyAgentOS.forge.tool_client import ForgeToolClient

    http, runtime = api
    client = ForgeToolClient(
        "http://test", transport=httpx.ASGITransport(app=create_app(runtime, manage_lifespan=False))
    )
    tools = (await client.list_tools())["data"]["tools"]
    assert any(t["tool_id"] == "console.press_button" for t in tools)
    observation = (await client.invoke_query_tool("console.observe", {}, caller_id="observe"))[
        "data"
    ]
    admission = (
        await client.invoke_action(
            "console.press_button",
            {
                "button_id": "button_01",
                "observation_id": observation["observation_id"],
                "arm": "right",
                "press_profile_id": "button_01",
            },
            caller_id="press",
        )
    )["data"]
    for _ in range(200):
        status = (await client.invocation_status(admission["invocation_id"]))["data"]
        if status["phase"] == "succeeded":
            break
        await asyncio.sleep(0.005)
    result = (await client.invocation_result(admission["invocation_id"]))["data"]
    assert result["result"]["outputs"]["trigger_confirmed"] is True
    assert runtime.backend.press_count == 1
    await client.close()


async def test_schema_errors_and_wrong_routes_never_move(api):
    client, runtime = api
    response = await client.post(
        "/tools/console.press_button:invoke",
        json={
            "caller_id": "bad",
            "arguments": {"arm": "right", "joint_positions": [999]},
        },
    )
    assert response.status_code == 422
    assert response.json()["ok"] is False
    response = await client.post(
        "/tools/console/press_button:invoke", json={"caller_id": "wrong", "arguments": {}}
    )
    assert response.status_code in {400, 422}
    assert runtime.backend.moves == []


@pytest.mark.parametrize(
    "quaternion", [(1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0), (0, 0, 0, 1), (0.5, 0.5, 0.5, 0.5)]
)
def test_transform_roundtrip_handles_half_turns(quaternion):
    pose = Pose(position=(0.3, -0.2, 0.1), quaternion_xyzw=quaternion)
    np.testing.assert_allclose(matrix(as_pose(matrix(pose))), matrix(pose), atol=1e-12)


def test_composition_rotates_translation_and_interpolation_is_bounded():
    rotation = Pose(quaternion_xyzw=(0, 0, 1, 0))
    translation = Pose(position=(1, 0, 0))
    assert compose(rotation, translation).position == (-1, 0, 0)
    start, end = Pose(), Pose(position=(1, 0, 0), quaternion_xyzw=(0, 0, 1, 0))
    for t in np.linspace(0, 1, 51):
        point = interpolate(start, end, t)
        assert 0 <= point.position[0] <= 1
        assert np.isclose(np.linalg.norm(point.quaternion_xyzw), 1)


@pytest.mark.parametrize(
    "mutation",
    [
        {"mode": "real"},
        {"commissioned": True, "mode": "real"},
        {"unrecognized": 1},
        {"mock_panel_pose": {"position": [float("nan"), 0, 0]}},
    ],
)
def test_invalid_or_uncommissioned_config_is_rejected(mutation):
    config = mock_config().model_dump()
    config.update(mutation)
    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate(config)
