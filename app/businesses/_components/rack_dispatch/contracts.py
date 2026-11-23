"""Original host facts and read-only dispatch projections."""

from dataclasses import dataclass
from typing import Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.wms_transport import TransportCaller


@dataclass(frozen=True, slots=True)
class PointRef:
    owner_domain: str
    work_line_id: int
    job_id: str
    rack_id: str
    point: str


@dataclass(frozen=True, slots=True)
class FactRef:
    kind: str
    key: tuple[str, ...]
    version: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Coverage:
    collection: str
    scope: tuple[str, ...]
    anchor: FactRef
    rows: tuple[FactRef, ...]


@dataclass(frozen=True, slots=True)
class PointDemand:
    point: PointRef
    state: Literal["CONTINUE", "NONE", "UNKNOWN"]
    coverage: tuple[Coverage, ...]


@dataclass(frozen=True, slots=True)
class DirectHandoff:
    point: PointRef
    state: Literal["CLEAR", "BLOCKED", "UNKNOWN"]
    coverage: tuple[Coverage, ...]


@dataclass(frozen=True, slots=True)
class JobOrigin:
    source_kind: str
    source_ref: str
    work_line_id: int
    rack_id: str
    task_id: str | None
    workline_code: str | None
    point: str | None
    initial_face: str | None
    caller: TransportCaller | None
    cycle_id: str
    resources: dict | None


@dataclass(frozen=True, slots=True)
class ActionProgress:
    id: str
    job_id: str
    kind: str
    source_ref: str
    phase: str
    completion_rule: str | None
    previous_action_id: str | None
    caller: TransportCaller | None
    template: str | None
    source: dict | None
    target: dict | None
    target_face: str | None
    client_request_id: str | None
    transport_task_id: str | None
    submit_operation_id: str | None
    submit_message_id: int | None
    submit_target: str | None
    outcome: dict | None
    outcome_revision: int | None
    position_observation: dict | None
    observation_revision: int | None
    actual_position: dict | None
    actual_face: str | None
    position_revision: int | None
    pending_revision: int | None
    acceptance_basis: dict | None
    accepted_at_ms: int | None
    operation: str | None
    decision_operation_id: str | None
    message_id: int | None
    request: dict | None
    decision: dict | None
    decision_consumed_at_ms: int | None
    due_at_ms: int | None
    basis: dict | None


@dataclass(frozen=True, slots=True)
class RackProgress:
    id: str
    admission_order: int | None
    origin: JobOrigin
    state: str
    pending: bool
    release_requested: bool
    release_basis: dict | None
    facts_revision: int
    review_reason: str | None
    actual_position: dict | None
    actual_face: str | None
    position_action_id: str | None
    closure_reason: str | None
    closure_basis: dict | None
    completed_at_ms: int | None
    current_action_id: str | None
    actions: tuple[ActionProgress, ...]


class TaskAdmission(Protocol):
    async def allows_task_entry(
        self, session: AsyncSession, *, work_line_id: int, task_id: str
    ) -> bool: ...
