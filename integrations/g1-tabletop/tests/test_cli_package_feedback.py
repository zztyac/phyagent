import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import httpx
import pytest

from g1_tabletop.cli import main
from g1_tabletop.config import RuntimeConfig, mock_config
from g1_tabletop.feedback_bridge import create_feedback_app

ROOT = Path(__file__).resolve().parents[1]


def test_cli_mock_generation_doctor_schema_and_demo(tmp_path, capsys):
    config = tmp_path / "mock.json"
    assert main(["init-mock", str(config)]) == 0
    assert main(["init-mock", str(config)]) == 2
    capsys.readouterr()
    assert main(["doctor", "--config", str(config)]) == 0
    assert json.loads(capsys.readouterr().out)["devices_connected"] is False
    assert main(["schema", str(tmp_path / "schema.json")]) == 0
    assert json.loads((tmp_path / "schema.json").read_text())["$defs"]["Hardware"]
    assert main(["demo"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["physical_hardware_used"] is False
    assert [r["phase"] for r in result["results"]] == ["succeeded"] * 3


def test_real_template_cannot_be_armed_and_schema_matches_source():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate_json((ROOT / "config/real.site.template.json").read_text())
    assert (
        json.loads((ROOT / "config/runtime.schema.json").read_text())
        == RuntimeConfig.model_json_schema()
    )
    assert (
        RuntimeConfig.model_validate_json((ROOT / "config/mock.json").read_text()) == mock_config()
    )


def test_button_only_configuration_retains_maintenance_open_posture(hardware_config):
    data = hardware_config.model_dump()
    data.update(objects={}, placements={})
    config = RuntimeConfig.model_validate(data)
    from g1_tabletop.specs import tool_specs

    assert set(tool_specs(config)) == {
        "console.observe",
        "console.read_state",
        "console.press_button",
    }
    assert config.postures[config.maintenance_open_posture].q == (0, 0, 0, 0, 0, 0)


async def test_feedback_bridge_preserves_measurement_time_and_rejects_corruption(tmp_path):
    path = tmp_path / "measurements.json"
    record = {
        "button_id": "a",
        "event_count": 4,
        "state": True,
        "timestamp": time.time() - 10,
        "source": "test-bench",
        "epoch": "boot1",
    }
    path.write_text(json.dumps({"a": record}))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_feedback_app(path)), base_url="http://test"
    ) as client:
        response = await client.get("/buttons/a")
        assert response.json() == record
        assert (await client.get("/buttons/missing")).status_code == 404
        assert (await client.post("/buttons/a")).status_code == 405
        path.write_text("partial JSON")
        assert (await client.get("/buttons/a")).status_code == 503


def builder_module():
    spec = importlib.util.spec_from_file_location("g1_build_test", ROOT / "scripts/build_bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bundle_reproducibility_and_core_registry_contract(tmp_path):
    from PhyAgentOS.skill_runtime.installer import SkillInstaller
    from PhyAgentOS.skill_runtime.registry import StaticPackageIndex
    from PhyAgentOS.skill_runtime.state import RuntimeStateStore

    builder = builder_module()
    first = builder.build(tmp_path / "a", "http://127.0.0.1:18080")
    second = builder.build(tmp_path / "b", "http://127.0.0.1:18080")
    for name in ("node", "skill"):
        assert Path(first[name]).read_bytes() == Path(second[name]).read_bytes()
    manifest = SkillInstaller(
        tmp_path / "skills", state_store=RuntimeStateStore(tmp_path / "state")
    ).install(
        Path(first["skill"]),
        expected_sha256=hashlib.sha256(Path(first["skill"]).read_bytes()).hexdigest(),
    )
    lock = manifest.artifacts.nodes["g1_tabletop"]
    with StaticPackageIndex(first["index"]) as index:
        assert index.node(lock.artifact_id, expected_sha256=lock.sha256).sha256 == lock.sha256
        assert (
            index.skill("g1-tabletop").sha256
            == hashlib.sha256(Path(first["skill"]).read_bytes()).hexdigest()
        )
    with tarfile.open(first["node"]) as archive:
        entry = archive.getmember("g1-tabletop-node/g1-tabletop-node")
        assert entry.mode & 0o111
        assert all(
            not member.name.startswith("/") and ".." not in Path(member.name).parts
            for member in archive.getmembers()
        )
    assert set(manifest.profiles) == {"mock", "real", "real-buttons"}
    with pytest.raises(ValueError, match="already exist"):
        builder.build(tmp_path / "a", "http://127.0.0.1:18080")


@pytest.mark.skipif(
    sys.platform != "linux", reason="Core Node installation enforces the real host platform"
)
def test_linux_node_installer_and_packaged_entrypoint(tmp_path):
    from PhyAgentOS.skill_runtime.installer import NodeInstaller, SkillInstaller
    from PhyAgentOS.skill_runtime.state import RuntimeStateStore

    result = builder_module().build(tmp_path / "build", "http://127.0.0.1:18080")
    manifest = SkillInstaller(
        tmp_path / "skills", state_store=RuntimeStateStore(tmp_path / "state")
    ).install(
        Path(result["skill"]),
        expected_sha256=hashlib.sha256(Path(result["skill"]).read_bytes()).hexdigest(),
    )
    installer = NodeInstaller(
        tmp_path / "runtime", state_store=RuntimeStateStore(tmp_path / "state")
    )
    lock = manifest.artifacts.nodes["g1_tabletop"]
    entrypoint = installer.install(Path(result["node"]), lock)
    assert installer.satisfies(lock)
    environment = dict(
        os.environ, PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
    )
    run = subprocess.run(
        [str(entrypoint), "demo"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert json.loads(run.stdout)["physical_hardware_used"] is False


def test_marker_export_and_static_camera_calibration(tmp_path, capsys):
    pytest.importorskip("cv2")
    from g1_tabletop.config import Pose

    output = tmp_path / "marker.png"
    assert main(["marker", "--id", "0", "--output", str(output)]) == 0
    assert output.read_bytes().startswith(b"\x89PNG")
    dataset = tmp_path / "pairs.json"
    dataset.write_text(
        json.dumps(
            {
                "pairs": [
                    {
                        "base_to_mount": {"position": [0.5, 0.2, 0.4]},
                        "camera_to_target": {"position": [0, 0, 0.3]},
                    }
                ]
                * 3
            }
        )
    )
    calibration = tmp_path / "camera.json"
    assert (
        main(
            ["calibrate", "--mount", "base", "--input", str(dataset), "--output", str(calibration)]
        )
        == 0
    )
    assert Pose.model_validate_json(calibration.read_text()).position == pytest.approx(
        (0.5, 0.2, 0.1)
    )


def test_operator_reconciliation_is_explicit_and_recorded(tmp_path, capsys):
    config = tmp_path / "mock.json"
    config.write_text(mock_config().model_dump_json())
    assert (
        main(
            [
                "reconcile-empty",
                "--config",
                str(config),
                "--state-dir",
                str(tmp_path / "state"),
                "--confirm-stopped-and-empty",
                "--operator-note",
                "Inspected the fixture; both hands are empty.",
            ]
        )
        == 0
    )
    evidence = json.loads(capsys.readouterr().out)
    assert evidence["mode"] == "mock" and Path(evidence["image_path"]).is_file()
