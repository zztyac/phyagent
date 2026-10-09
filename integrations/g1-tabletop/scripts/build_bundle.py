"""Build a deterministic Node, manifest-v2 Skill and schema-v3 local package index."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

import yaml

PROJECT = Path(__file__).resolve().parents[1]
CORE = PROJECT.parents[1]
sys.path.insert(0, str(PROJECT / "src"))
sys.path.insert(0, str(CORE))

from PhyAgentOS.skill_runtime.manifest import SkillManifest  # noqa: E402
from scripts.package_skill import package  # noqa: E402

from g1_tabletop.config import mock_config  # noqa: E402
from g1_tabletop.specs import tool_specs  # noqa: E402


def build(output: Path, base_url: str, *, arch: str = "x86_64") -> dict:
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("base URL must use HTTP(S)")
    if arch not in {"x86_64", "aarch64"}:
        raise ValueError("supported deployment architectures: x86_64, aarch64")
    output.mkdir(parents=True, exist_ok=True)
    node_name = f"g1-tabletop-node-0.1.0-linux-{arch}"
    archive = output / f"{node_name}.tar.gz"
    if any(
        (output / name).exists()
        for name in (archive.name, "g1-tabletop-0.1.0.tar.gz", "index.yaml")
    ):
        raise ValueError("output artifacts already exist; use a new output directory/version")
    with tempfile.TemporaryDirectory(prefix="g1-package-") as directory:
        root = Path(directory)
        files = {
            f"g1-tabletop-node/g1_tabletop/{path.relative_to(PROJECT / 'src/g1_tabletop').as_posix()}": path.read_bytes()
            for path in (PROJECT / "src/g1_tabletop").rglob("*.py")
        }
        files["g1-tabletop-node/g1-tabletop-node"] = (
            "#!/usr/bin/env python3\nimport sys\nfrom pathlib import Path\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parent))\n"
            "from g1_tabletop.cli import main\nraise SystemExit(main())\n"
        ).encode()
        files["g1-tabletop-node/requirements.txt"] = (
            "pydantic>=2.12,<3\nnumpy>=1.26,<3\nfastapi>=0.115,<1\nuvicorn>=0.30,<1\n"
            "httpx>=0.28,<1\nwebsockets>=16,<17\nPyYAML>=6,<7\njsonschema>=4.23,<5\ndora-rs==0.4.1\n"
        ).encode()
        files["g1-tabletop-node/LICENSE"] = (CORE / "LICENSE").read_bytes()
        with archive.open("wb") as raw:
            with gzip.GzipFile(fileobj=raw, filename="", mode="wb", mtime=0) as gz:
                with tarfile.open(fileobj=gz, mode="w") as tar:
                    for name, data in sorted(files.items()):
                        info = tarfile.TarInfo(name)
                        info.size, info.mtime = len(data), 0
                        info.mode = 0o755 if name.endswith("/g1-tabletop-node") else 0o644
                        tar.addfile(info, io.BytesIO(data))
        node_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        skill = root / "g1-tabletop"
        shutil.copytree(PROJECT / "skill", skill)
        (skill / "assets").mkdir()
        (skill / "assets/mock.json").write_text(
            mock_config().model_dump_json(indent=2), encoding="utf-8"
        )
        profiles = {}
        all_tools = list(tool_specs(mock_config()))
        console_tools = [name for name in all_tools if name.startswith("console.")]
        for profile_name in ("mock", "real", "real-buttons"):
            mode = "mock" if profile_name == "mock" else "real"
            path = skill / "profiles" / profile_name
            path.mkdir(parents=True)
            args = (
                "serve --config ${PAOS_SKILL_ROOT}/assets/mock.json"
                if mode == "mock"
                else "serve --config-env G1_REAL_CONFIG"
            )
            dataflow = {
                "nodes": [
                    {
                        "id": "g1_tabletop",
                        "path": "${FORGE_RUNTIME_BIN}/g1-tabletop-node",
                        "args": args
                        + f" --state-dir-env G1_TABLETOP_STATE_DIR --require-mode {mode}",
                        "inputs": {"tick": "dora/timer/millis/100"},
                    }
                ]
            }
            (path / "dataflow.yaml").write_text(
                yaml.safe_dump(dataflow, sort_keys=False), encoding="utf-8"
            )
            profiles[profile_name] = {
                "dataflow": f"profiles/{profile_name}/dataflow.yaml",
                "required_tools": console_tools if profile_name == "real-buttons" else all_tools,
                "startup_timeout_s": 60,
                "required_binaries": ["g1-tabletop-node"],
                "required_assets": ["assets/mock.json"] if mode == "mock" else [],
                "required_environment": ["G1_TABLETOP_STATE_DIR"]
                + (["G1_REAL_CONFIG"] if mode == "real" else []),
            }
        manifest = {
            "manifest_version": 2,
            "name": "g1-tabletop",
            "version": "0.1.0",
            "description": "G1 console press and marker-guided pick/place; no VLA",
            "skill_document": "SKILL.md",
            "gateway_url": "http://127.0.0.1:19002",
            "required_tools": console_tools,
            "profiles": profiles,
            "artifacts": {
                "resolver": "registry",
                "nodes": {
                    "g1_tabletop": {
                        "artifact_id": node_name,
                        "version": "0.1.0",
                        "platform": "linux",
                        "arch": arch,
                        "artifact_type": "directory_tar_gz",
                        "entrypoint": "g1-tabletop-node",
                        "sha256": node_digest,
                    }
                },
            },
        }
        (skill / "skill.yaml").write_text(
            yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
        )
        SkillManifest.from_dict(manifest, bundle_root=skill)
        bundle = package(skill, output)
        shutil.copytree(skill, output / "skill-source")
    entries = []
    for kind, artifact, identity in (
        ("node_bundle", archive, node_name),
        ("skill_bundle", bundle, "g1-tabletop"),
    ):
        entries.append(
            {
                "kind": kind,
                "package_key": identity,
                "artifact_id": identity,
                "version": "0.1.0",
                "direct_download_url": base_url.rstrip("/") + "/" + artifact.name,
                "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "size": artifact.stat().st_size,
            }
        )
    (output / "index.yaml").write_text(
        yaml.safe_dump({"schema_version": 3, "packages": entries}, sort_keys=False),
        encoding="utf-8",
    )
    return {"node": str(archive), "skill": str(bundle), "index": str(output / "index.yaml")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--arch", choices=["x86_64", "aarch64"], default="x86_64")
    args = parser.parse_args()
    print(json.dumps(build(args.output, args.base_url, arch=args.arch), indent=2))


if __name__ == "__main__":
    main()
