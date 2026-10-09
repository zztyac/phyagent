from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from g1_tabletop.config import Arm, Pose

Checkpoint = Callable[[], None]


@dataclass(frozen=True)
class Feedback:
    event_count: int
    state: bool | None
    timestamp: float
    source: str
    epoch: str


class Backend(Protocol):
    async def start(self) -> None: ...
    async def close(self) -> None: ...
    def check_health(self) -> None: ...
    async def locate(self, kind: str, arm: Arm | None = None) -> dict[str, Pose]: ...
    async def prepare(self, arm: Arm, tool_frame: str, check: Checkpoint) -> None: ...
    async def hand(
        self, arm: Arm, posture: str, check: Checkpoint, *, object_id: str | None = None
    ) -> None: ...
    async def move(
        self,
        arm: Arm,
        pose: Pose,
        speed: float,
        check: Checkpoint,
        *,
        purpose: str = "approach",
        target_id: str | None = None,
    ) -> None: ...
    async def feedback(self, button_id: str) -> Feedback: ...
    async def grasp_confirmed(
        self, object_id: str, arm: Arm, before: Pose, check: Checkpoint
    ) -> bool: ...
    async def placement_confirmed(
        self, object_id: str, target: Pose, check: Checkpoint
    ) -> bool: ...
    async def stopped(self) -> bool: ...
    async def image(self, source: str) -> tuple[bytes, float]: ...
