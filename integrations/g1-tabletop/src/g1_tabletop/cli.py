"""Offline demo, configuration checks and explicit local Gateway startup."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

from g1_tabletop.backends.mock import MockBackend
from g1_tabletop.config import export_schema, load_config, mock_config
from g1_tabletop.errors import ExecutionError
from g1_tabletop.gateway import create_app
from g1_tabletop.runtime import Runtime


async def reconcile(config, state_dir: Path, operator_note: str):
    """Explicit maintenance command. No Agent tool and no motion/open-hand commands."""
    if config.mode == "real":
        from g1_tabletop.backends.real import RealBackend

        backend = RealBackend(config)
    else:
        backend = MockBackend(config)
    runtime = Runtime(config, backend, state_dir / config.mode / "runtime.sqlite3")
    await runtime.start()
    try:
        evidence = {
            "timestamp": time.time(),
            "mode": config.mode,
            "calibration_digest": config.digest,
        }
        if config.mode == "real":
            import numpy as np

            if backend.arm.enabled:
                raise ExecutionError(
                    "RECONCILIATION_MOVING", "maintenance must not acquire arm control"
                )
            for _ in range(10):
                sample = backend.arm.sample()
                if np.max(np.abs(sample.dq[15:29])) > 0.03:
                    raise ExecutionError(
                        "RECONCILIATION_MOVING", "operator must stop robot before reconciliation"
                    )
                for arm in ("left", "right"):
                    q, _ = backend.hands.state(arm)
                    posture = config.postures[config.maintenance_open_posture]
                    if np.max(np.abs(q - posture.q)) >= posture.tolerance:
                        raise ExecutionError(
                            "HAND_NOT_OPEN",
                            "operator must remove objects and open both hands before reconciliation",
                        )
                await asyncio.sleep(0.02)
            evidence["motor_q"] = sample.q.tolist()
        image, stamp = await backend.image("head")
        image_path = state_dir / config.mode / f"reconciliation-{time.time_ns()}.png"
        image_path.write_bytes(image)
        evidence.update(
            image_sha256=hashlib.sha256(image).hexdigest(),
            image_timestamp=stamp,
            image_path=str(image_path.resolve()),
        )
        runtime.store.reconcile_empty(operator_note, evidence)
        return evidence
    finally:
        await runtime.close()


async def demo(state_dir: Path):
    config = mock_config()
    runtime = Runtime(config, MockBackend(config), state_dir / "runtime.sqlite3")
    await runtime.start()
    results = []
    try:
        for query, tool_id, fields in (
            (
                "console.observe",
                "console.press_button",
                {"button_id": "button_01", "press_profile_id": "button_01", "arm": "right"},
            ),
            (
                "objects.observe",
                "manipulation.pick",
                {"object_id": "block_01", "grasp_profile_id": "block_01", "arm": "left"},
            ),
            (
                "placements.observe",
                "manipulation.place",
                {"placement_id": "zone_01", "arm": "left"},
            ),
        ):
            observation = await runtime.query(query, {})
            admission = await runtime.submit(
                tool_id,
                dict(fields, observation_id=observation["observation_id"]),
                f"demo:{tool_id}",
            )
            await runtime.task
            result = runtime.result(admission["invocation_id"])
            results.append({"tool_id": tool_id, **result})
            if result["phase"] != "succeeded":
                raise RuntimeError(json.dumps(result, ensure_ascii=False))
        return {"mode": "mock", "physical_hardware_used": False, "results": results}
    finally:
        await runtime.close()


def _env_path(variable: str | None) -> Path | None:
    if variable is None:
        return None
    value = os.environ.get(variable)
    if not value:
        raise ValueError(f"required environment variable is missing: {variable}")
    return Path(value).expanduser().resolve()


def doctor(path: Path) -> dict:
    config = load_config(path)
    modules = ["numpy", "pydantic", "fastapi", "httpx", "uvicorn"]
    if config.mode == "real":
        modules += ["unitree_sdk2py", "pyrealsense2", "pinocchio", "coal", "cv2"]
    missing = [name for name in modules if importlib.util.find_spec(name) is None]
    if config.mode == "real":
        from g1_tabletop.kinematics import verified_file

        verified_file(config.hardware.urdf, config.hardware.urdf_sha256)
        verified_file(config.hardware.srdf, config.hardware.srdf_sha256)
        if sys.platform != "linux":
            missing.append("commissioned Linux host")
    return {
        "config_valid": True,
        "mode": config.mode,
        "calibration_digest": config.digest,
        "missing_dependencies": missing,
        "devices_connected": False,
        "ready_for_device_checks": not missing,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo", help="run button, pick and place against a fresh mock fixture")
    marker = commands.add_parser(
        "marker", help="generate a printable ArUco PNG; measure the printed black square"
    )
    marker.add_argument("--id", type=int, required=True)
    marker.add_argument("--pixels", type=int, default=600)
    marker.add_argument("--output", type=Path, required=True)
    calibration = commands.add_parser(
        "calibrate", help="offline wrist/base camera calibration from paired poses"
    )
    calibration.add_argument("--mount", choices=["wrist", "base"], required=True)
    calibration.add_argument("--input", type=Path, required=True)
    calibration.add_argument("--output", type=Path, required=True)
    bridge = commands.add_parser(
        "feedback-bridge", help="read-only adapter for externally produced sensor records"
    )
    bridge.add_argument("--source-file", type=Path, required=True)
    bridge.add_argument("--port", type=int, default=19003)
    export = commands.add_parser("init-mock", help="write synthetic configuration")
    export.add_argument("output", type=Path)
    schema = commands.add_parser(
        "schema", help="export complete JSON schema for real site configuration"
    )
    schema.add_argument("output", type=Path)
    check = commands.add_parser(
        "doctor", help="check configuration/dependencies without connecting devices"
    )
    check.add_argument("--config", type=Path, required=True)
    recovery = commands.add_parser(
        "reconcile-empty",
        help="operator-only maintenance after independently stopping robot and removing held objects",
    )
    recovery.add_argument("--config", type=Path, required=True)
    recovery.add_argument("--state-dir", type=Path, required=True)
    recovery.add_argument("--operator-note", required=True)
    recovery.add_argument("--confirm-stopped-and-empty", action="store_true", required=True)
    serve = commands.add_parser("serve", help="serve local Forge Tool API")
    sources = serve.add_mutually_exclusive_group(required=True)
    sources.add_argument("--config", type=Path)
    sources.add_argument("--config-env")
    state = serve.add_mutually_exclusive_group(required=True)
    state.add_argument("--state-dir", type=Path)
    state.add_argument("--state-dir-env")
    serve.add_argument("--port", type=int, default=19002)
    serve.add_argument("--require-mode", choices=["mock", "real"])
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            with tempfile.TemporaryDirectory(prefix="g1-tabletop-demo-") as directory:
                print(json.dumps(asyncio.run(demo(Path(directory))), ensure_ascii=False, indent=2))
        elif args.command in {"marker", "calibrate"}:
            from g1_tabletop.calibration import calibrate, write_marker

            if args.output.exists():
                raise ValueError("output exists; choose a new artifact path")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            if args.command == "marker":
                write_marker(args.output, args.id, args.pixels)
            else:
                args.output.write_text(
                    calibrate(args.input, args.mount).model_dump_json(indent=2), encoding="utf-8"
                )
            print(args.output)
        elif args.command == "feedback-bridge":
            import uvicorn

            from g1_tabletop.feedback_bridge import create_feedback_app

            if not 1024 <= args.port <= 65535:
                raise ValueError("port must be between 1024 and 65535")
            uvicorn.run(
                create_feedback_app(args.source_file), host="127.0.0.1", port=args.port, workers=1
            )
        elif args.command == "init-mock":
            if args.output.exists():
                raise ValueError("output already exists; choose a new configuration path")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(mock_config().model_dump_json(indent=2), encoding="utf-8")
            print(args.output)
        elif args.command == "schema":
            args.output.parent.mkdir(parents=True, exist_ok=True)
            export_schema(args.output)
        elif args.command == "doctor":
            result = doctor(args.config)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ready_for_device_checks"] else 2
        elif args.command == "reconcile-empty":
            if len(args.operator_note.strip()) < 12:
                raise ValueError(
                    "operator note must describe the physical inspection (at least 12 characters)"
                )
            result = asyncio.run(
                reconcile(load_config(args.config), args.state_dir, args.operator_note)
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            import uvicorn

            config = load_config(args.config or _env_path(args.config_env))
            if args.require_mode and config.mode != args.require_mode:
                raise ValueError("configuration mode does not match the selected Skill profile")
            if not 1024 <= args.port <= 65535:
                raise ValueError("port must be between 1024 and 65535")
            state_dir = args.state_dir or _env_path(args.state_dir_env)
            if config.mode == "mock":
                backend = MockBackend(config)
            else:
                from g1_tabletop.backends.real import RealBackend

                backend = RealBackend(config)
            runtime = Runtime(config, backend, state_dir / config.mode / "runtime.sqlite3")
            server = uvicorn.Server(
                uvicorn.Config(
                    create_app(runtime),
                    host="127.0.0.1",
                    port=args.port,
                    log_level="info",
                    workers=1,
                )
            )
            if os.environ.get("DORA_NODE_CONFIG"):
                # Dora owns this process lifetime; draining events honors STOP without abrupt SDK teardown.
                from dora import Node

                node = Node()

                def watch_dora():
                    for event in node:
                        if event["type"] == "STOP":
                            server.should_exit = True
                            return
                    server.should_exit = True

                threading.Thread(target=watch_dora, name="dora-stop", daemon=True).start()
            server.run()
        return 0
    except (ValueError, OSError, RuntimeError, ExecutionError) as exc:
        print(f"g1-tabletop: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
