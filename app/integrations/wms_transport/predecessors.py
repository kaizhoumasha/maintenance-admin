"""Explicit, bounded predecessor projection within the caller's database snapshot."""

from copy import deepcopy
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from .results import project, read_rows
from .types import TransportCaller, require_uuid7, validate_text


@dataclass(frozen=True, slots=True, kw_only=True)
class PredecessorItem:
    transport_task_id: str
    closure: str
    task_closure: str
    reason: str
    member_ids: tuple[str, ...] = ()
    client_request_id: str | None = None
    outcome_revision: int | None = None
    closure_basis: dict | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PredecessorQueryResult:
    caller: TransportCaller
    cycle_id: str
    rack_id: str | None
    container_id: str | None
    requested_ids: tuple[str, ...]
    items: tuple[PredecessorItem, ...]
    complete: bool

    @property
    def unresolved(self) -> tuple[PredecessorItem, ...]:
        return tuple(item for item in self.items if item.closure != "CLOSED")


async def list_unclosed_predecessors(
    session: AsyncSession,
    *,
    caller: TransportCaller,
    cycle_id: str,
    predecessor_ids: tuple[str, ...],
    rack_id: str | None = None,
    container_id: str | None = None,
) -> PredecessorQueryResult:
    if not isinstance(caller, TransportCaller):
        raise ValueError("caller must be a TransportCaller")
    require_uuid7(cycle_id)
    if (
        not isinstance(predecessor_ids, tuple)
        or len(predecessor_ids) > 100
        or any(not isinstance(i, str) or not i for i in predecessor_ids)
        or len(set(predecessor_ids)) != len(predecessor_ids)
    ):
        raise ValueError("expected at most 100 unique explicit predecessor ids")
    if (rack_id is None) == (container_id is None):
        raise ValueError("exactly one resource is required")
    validate_text(rack_id if rack_id is not None else container_id)
    rows = await read_rows(session, predecessor_ids)
    items = []
    for task_id in predecessor_ids:
        task, submissions = rows.get(task_id, (None, []))
        result = project(
            task,
            submissions,
            task_id=task_id,
            client_request_id=task["client_request_id"] if task else "",
            caller=caller,
        )
        members, matches = (), False
        reason = result.closure_reason
        closure = result.closure
        if task and result.availability == "FOUND":
            frozen = task["frozen"]
            if "moves" in frozen:
                members = tuple(
                    m["container_id"]
                    for m in frozen["moves"]
                    if (
                        m["container_id"] == container_id
                        if container_id is not None
                        else any(m[end].get("rack_id") == rack_id for end in ("source", "target"))
                    )
                )
                matches = bool(members)
            else:
                matches = rack_id is not None and frozen["rack_id"] == rack_id
            if task["cycle_id"] != cycle_id or not matches:
                closure, reason = "UNDETERMINED", "SCOPE_MISMATCH"
            elif result.outcome is not None and result.outcome["results"] is not None:
                selected = [r for r in result.outcome["results"] if r["container_id"] in members]
                closure = (
                    "UNDETERMINED" if any(r["position_unknown"] for r in selected) else "CLOSED"
                )
                reason = "UNKNOWN_POSITION" if closure == "UNDETERMINED" else "APPLIED_RESULT"
        basis = deepcopy(result.closure_basis)
        if basis is not None:
            basis.update(rack_id=rack_id, container_id=container_id, member_ids=members)
        items.append(
            PredecessorItem(
                transport_task_id=task_id,
                client_request_id=(
                    result.client_request_id if result.availability == "FOUND" else None
                ),
                closure=closure,
                task_closure=result.closure,
                reason=reason,
                member_ids=members,
                outcome_revision=result.outcome_revision,
                closure_basis=basis,
            )
        )
    return PredecessorQueryResult(
        caller=caller,
        cycle_id=cycle_id,
        rack_id=rack_id,
        container_id=container_id,
        requested_ids=predecessor_ids,
        items=tuple(items),
        complete=all(item.closure != "UNDETERMINED" for item in items),
    )
