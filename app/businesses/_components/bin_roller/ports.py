from dataclasses import dataclass
from typing import Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class CurrentTask:
    task_id: str | None
    is_valid: bool


@dataclass(frozen=True, slots=True)
class SourceFaceRef:
    plan_revision: int
    rack_id: str
    rack_face: str


@dataclass(frozen=True, slots=True)
class SupplyChange:
    work_line_id: int
    task_id: str
    change_id: str
    added: tuple[SourceFaceRef, ...]
    withdrawn: tuple[SourceFaceRef, ...]


@dataclass(frozen=True, slots=True)
class SupplyProgress:
    task_id: str
    source: SourceFaceRef
    feed_complete: bool
    pending: bool
    original_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SuccessorContext:
    state: Literal["UNKNOWN", "CHECKING", "AVAILABLE", "NONE"]
    evidence: dict


@dataclass(frozen=True, slots=True)
class Scan2Arrival:
    arrival_id: str
    work_line_id: int
    device_code: str
    raw_bin_code: str
    bin_code: str | None
    scanned_at: int


@dataclass(frozen=True, slots=True)
class RollerWorkRef:
    kind: Literal["SCAN", "SCAN2_RELEASE", "FEED", "RETURN_BATCH", "RACK_CYCLE", "DRAIN"]
    record_id: str


class Production(Protocol):
    async def read_current_task(
        self, session: AsyncSession, *, work_line_id: int
    ) -> CurrentTask: ...

    async def read_successor(
        self, session: AsyncSession, *, work_line_id: int
    ) -> SuccessorContext: ...

    async def accept_scan2(self, ctx, arrival: Scan2Arrival) -> None: ...
