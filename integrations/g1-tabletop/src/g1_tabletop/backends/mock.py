"""Deterministic simulation fixture with explicit failure injection; not robot physics."""

import asyncio
import struct
import time
import zlib

from g1_tabletop.backends.base import Feedback
from g1_tabletop.config import Pose, RuntimeConfig
from g1_tabletop.errors import ExecutionError
from g1_tabletop.geometry import compose


# Synthetic image, independent of optional OpenCV; runtime state identifies simulation.
def _mock_png():
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    pixels = b"".join(b"\0" + bytes((40, 120, 180)) * 64 for _ in range(64))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


MOCK_PNG = _mock_png()


class MockBackend:
    def __init__(self, config: RuntimeConfig):
        if config.mode != "mock":
            raise ValueError("MockBackend only accepts mock configuration")
        self.config = config
        self.faults: set[str] = set()
        self.delay_s = 0.002
        self.moves: list = []
        self.press_count = 0
        self.events = dict.fromkeys(config.buttons, 0)
        self.poses = {
            a: Pose(position=(0.3, 0.15 if a == "left" else -0.15, 0.45)) for a in ("left", "right")
        }
        self.held: dict[str, str] = {}
        self.object_poses = {name: obj.mock_pose for name, obj in config.objects.items()}
        self.active = False

    async def start(self):
        self.active = True

    async def close(self):
        self.active = False

    def check_health(self):
        if not self.active or "stale_state" in self.faults:
            raise ExecutionError("STATE_STALE", "mock device state unavailable")

    async def locate(self, kind, arm=None):
        self.check_health()
        if "target_missing" in self.faults:
            raise ExecutionError("TARGET_NOT_VISIBLE", "mock occlusion")
        if kind == "console":
            return {
                name: compose(self.config.mock_panel_pose, button.pose_in_panel)
                for name, button in self.config.buttons.items()
            } | {"__panel__": self.config.mock_panel_pose}
        if kind == "placements":
            return {name: place.mock_pose for name, place in self.config.placements.items()}
        return dict(self.object_poses)

    async def prepare(self, arm, tool_frame, check):
        check()

    async def hand(self, arm, posture, check, *, object_id=None):
        check()
        await asyncio.sleep(self.delay_s)
        check()
        if object_id and "empty_grasp" not in self.faults:
            self.held[arm] = object_id
        elif posture == "open" and arm in self.held:
            self.object_poses[self.held.pop(arm)] = self.poses[arm]

    async def move(self, arm, pose, speed, check, *, purpose="approach", target_id=None):
        check()
        if "collision" in self.faults:
            raise ExecutionError("COLLISION", "mock obstacle blocks path")
        await asyncio.sleep(self.delay_s)
        check()
        self.moves.append((arm, pose, purpose))
        self.poses[arm] = pose
        if arm in self.held:
            self.object_poses[self.held[arm]] = pose
        if purpose == "press":
            self.press_count += 1
            if "missing_trigger" not in self.faults:
                self.events[target_id] += 1

    async def feedback(self, button_id):
        timestamp = time.time() - (100 if "stale_feedback" in self.faults else 0)
        count = self.events[button_id]
        return Feedback(count, bool(count % 2), timestamp, "mock_switch", "mock-epoch")

    async def grasp_confirmed(self, object_id, arm, before, check):
        check()
        return self.held.get(arm) == object_id and "slip" not in self.faults

    async def stopped(self):
        return "stop_unknown" not in self.faults

    async def placement_confirmed(self, object_id, target, check):
        from g1_tabletop.geometry import distance

        check()
        return (
            "placement_failed" not in self.faults
            and distance(self.object_poses[object_id], target) < 0.003
        )

    async def image(self, source):
        return MOCK_PNG, time.time()
