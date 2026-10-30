"""Transport result projections. Reads do not apply callbacks or settle delivery."""

from copy import deepcopy
from dataclasses import dataclass, replace

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.strict_json import json_object
from app.integrations.wms_envelope import RESPONSE_COLUMNS, outgoing_message, stored_response
from app.models import ReliableMessage, TransportTask

from .adapter import TransportSubmitAdapter
from .callback.payloads import Resulted
from .protocol import OPERATION
from .types import (
    BinExchangePair,
    BinMove,
    HandoffPosition,
    Rack,
    RackBinSlot,
    RackPosition,
    TransportCaller,
    Zone,
    require_uuid7,
)
from .wire import bin_move_data, exchange_bins_data, move_rack_data, rotate_rack_data


@dataclass(frozen=True, slots=True, kw_only=True)
class TransportTaskResult:
    transport_task_id: str
    client_request_id: str
    workline_id: str
    station_id: str | None
    availability: str
    owner_domain: str | None = None
    operation_id: str | None = None
    cycle_id: str | None = None
    kind: str | None = None
    submission_state: str = "UNAVAILABLE"
    submission_message_id: int | None = None
    submission_target: str | None = None
    submission_delivery_status: str | None = None
    submission_delivery_uncertain: bool | None = None
    submission_fail_reason: str | None = None
    submission_response_state: str = "UNAVAILABLE"
    submission_http_status: int | None = None
    submission_response_code: str | None = None
    submission_response_timestamp: int | None = None
    submission_response_data: dict | None = None
    outcome: dict | None = None
    outcome_revision: int | None = None
    member_facts: dict | None = None
    has_pending_outcome: bool = False
    pending_revision: int | None = None
    closure: str = "UNDETERMINED"
    closure_reason: str = "TASK_NOT_FOUND"
    closure_basis: dict | None = None


async def read_rows(session: AsyncSession, ids: tuple[str, ...]) -> dict:
    """One statement includes all original submission matches, including ambiguity."""
    if not ids:
        return {}
    task, message = TransportTask.__table__, ReliableMessage.__table__
    query = (
        select(
            *[c.label("task_" + c.name) for c in task.c],
            *[message.c[n].label("submit_" + n) for n in RESPONSE_COLUMNS],
        )
        .select_from(
            task.outerjoin(
                message,
                and_(
                    message.c.operation_id == task.c.operation_id,
                    message.c.operation == OPERATION,
                    message.c.business_key == task.c.transport_task_id,
                ),
            )
        )
        .where(task.c.transport_task_id.in_(ids))
    )
    with session.no_autoflush:
        rows = (await session.execute(query)).mappings().all()
    grouped = {}
    for row in rows:
        key = row["task_transport_task_id"]
        if key not in grouped:
            grouped[key] = ({c.name: row["task_" + c.name] for c in task.c}, [])
        if row["submit_id"] is not None:
            grouped[key][1].append({n: row["submit_" + n] for n in RESPONSE_COLUMNS})
    return grouped


def _location(data: dict, *, bins: bool):
    if bins:
        if data["kind"] == "RACK_BIN_SLOT":
            return RackBinSlot(data["rack_id"], data["rack_face"], data["slot_id"])
        if data["kind"] == "HANDOFF_POSITION":
            return HandoffPosition(data["location_code"])
    else:
        classes = {"RACK": Rack, "ZONE": Zone, "RACK_POSITION": RackPosition}
        return classes[data["kind"]](data["location_code"])
    raise ValueError("invalid location")


def validate_frozen(task: dict) -> dict:
    frozen = task["frozen"]
    if not isinstance(frozen, dict) or frozen.get("kind") != task["kind"]:
        raise ValueError("invalid frozen task")
    kind = frozen["kind"]
    if kind in {"BIN_MOVE", "BIN_EXCHANGE"}:
        moves = [
            BinMove(
                m["container_id"],
                _location(m["source"], bins=True),
                _location(m["target"], bins=True),
            )
            for m in frozen["moves"]
        ]
        if kind == "BIN_MOVE":
            normalized = bin_move_data(moves)
        else:
            remaining, pairs = list(moves), []
            while remaining:
                left = remaining.pop(0)
                right = next(
                    (m for m in remaining if m.source == left.target and m.target == left.source),
                    None,
                )
                if right is None:
                    raise ValueError("exchange pair is incomplete")
                remaining.remove(right)
                pairs.append(
                    BinExchangePair(left.bin_code, left.source, right.bin_code, right.source)
                )
            normalized = exchange_bins_data(pairs)
    elif kind == "RACK_MOVE":
        normalized = move_rack_data(
            frozen["rack_id"],
            _location(frozen["source"], bins=False),
            _location(frozen["target"], bins=False),
            frozen.get("target_face"),
            frozen["rcs_template_id"],
        )
    elif kind == "RACK_ROTATE":
        normalized = rotate_rack_data(
            frozen["rack_id"],
            _location(frozen["source"], bins=False),
            frozen["target_face"],
            frozen["rcs_template_id"],
        )
    else:
        raise ValueError("invalid kind")
    if normalized != frozen:
        raise ValueError("frozen task is not canonical")
    return frozen


def validate_outcome(task: dict, raw: dict, revision: int, *, applied: bool) -> tuple[dict, str]:
    data = Resulted.model_validate(raw).model_dump(mode="json")
    if (
        type(revision) is not int
        or data["outcome_revision"] != revision
        or data["transport_task_id"] != task["transport_task_id"]
        or data["kind"] != task["kind"]
    ):
        raise ValueError("outcome identity mismatch")
    frozen = task["frozen"]
    if data["results"] is not None:
        members = {m["container_id"]: m["target"] for m in frozen["moves"]}
        if {r["container_id"] for r in data["results"]} != set(members):
            raise ValueError("outcome members mismatch")
        for r in data["results"]:
            if r["status"] == "SUCCEEDED":
                target = members[r["container_id"]]
                if r["final_position"] != target:
                    raise ValueError("outcome target mismatch")
                if (
                    applied
                    and target["kind"] == "RACK_BIN_SLOT"
                    and (
                        (task["member_positions"] or {})
                        .get(r["container_id"], {})
                        .get("TARGET_PLACED")
                        != target
                    )
                ):
                    raise ValueError("applied result lacks its required milestone")
        status = (
            "UNKNOWN"
            if any(r["position_unknown"] for r in data["results"])
            else "FAILED"
            if any(r["status"] == "FAILED" for r in data["results"])
            else "SUCCEEDED"
        )
    else:
        if data["rack_id"] != frozen["rack_id"]:
            raise ValueError("outcome rack mismatch")
        if data["status"] == "SUCCEEDED":
            if (
                frozen["target"]["kind"] == "RACK_POSITION"
                and data["final_position"] != frozen["target"]
            ):
                raise ValueError("outcome position mismatch")
            if frozen.get("target_face") and data["arrival_face"] != frozen["target_face"]:
                raise ValueError("outcome face mismatch")
        status = "UNKNOWN" if data["position_unknown"] else data["status"]
    return data, status


def has_position_milestones(task: dict) -> bool:
    """Historical execution evidence can contradict a refusal, but cannot prove completion."""
    facts = task["member_positions"] or {}
    for member in task["frozen"].get("moves", []):
        recorded = facts.get(member["container_id"], {})
        if isinstance(recorded, dict) and (
            recorded.get("SOURCE_PICKED") is True
            or recorded.get("TARGET_PLACED") == member["target"]
        ):
            return True
    return False


def project(
    task: dict | None,
    submissions: list[dict],
    *,
    task_id: str,
    client_request_id: str,
    caller: TransportCaller,
) -> TransportTaskResult:
    result = TransportTaskResult(
        transport_task_id=task_id,
        client_request_id=client_request_id,
        workline_id=caller.workline_id,
        station_id=caller.station_id,
        availability="NOT_FOUND",
    )
    if task is None:
        return result
    if (
        task["client_request_id"] != client_request_id
        or task["workline_id"] != caller.workline_id
        or task["station_id"] != caller.station_id
    ):
        return replace(result, availability="IDENTITY_MISMATCH", closure_reason="SCOPE_MISMATCH")
    result = replace(
        result,
        availability="FOUND",
        owner_domain=task["owner_domain"],
        operation_id=task["operation_id"],
        cycle_id=task["cycle_id"],
        kind=task["kind"],
    )
    try:
        require_uuid7(task["operation_id"])
        validate_frozen(task)
        facts = task["member_positions"]
        if facts is not None and not isinstance(facts, dict):
            raise ValueError("invalid member facts")
        outcome, status = None, None
        if task["applied_outcome"] is not None:
            outcome, status = validate_outcome(
                task, task["applied_outcome"], task["applied_outcome_revision"], applied=True
            )
        if status != task["outcome_status"] or (
            outcome is None and task["applied_outcome_revision"] is not None
        ):
            raise ValueError("applied outcome state mismatch")
        pending = task["pending_outcome"]
        if pending is not None:
            validate_outcome(task, pending["data"], pending["revision"], applied=False)
    except (ValueError, KeyError, TypeError, AttributeError):
        return replace(
            result,
            availability="INVALID",
            closure_reason=("MISSING_FROZEN" if task["frozen"] is None else "INVALID_EVIDENCE"),
        )
    result = replace(
        result,
        outcome=deepcopy(outcome),
        outcome_revision=task["applied_outcome_revision"],
        member_facts=deepcopy(facts or {}),
        has_pending_outcome=pending is not None,
        pending_revision=pending["revision"] if pending else None,
    )
    if len(submissions) > 1:
        result = replace(result, submission_state="AMBIGUOUS")
    elif submissions:
        row = submissions[0]
        response = stored_response(outgoing_message(row), row, TransportSubmitAdapter())
        raw = json_object(row["request_raw"])
        valid = (
            raw is not None
            and raw.get("operation_id") == task["operation_id"]
            and raw.get("operation") == OPERATION
            and isinstance(raw.get("data"), dict)
            and raw["data"] == {"transport_task_id": task_id, **task["frozen"]}
        )
        result = replace(
            result,
            submission_state="AVAILABLE"
            if valid and response["response_state"] != "INVALID"
            else "INVALID",
            submission_message_id=row["id"],
            submission_target=row["target"],
            submission_delivery_status=row["status"],
            submission_delivery_uncertain=row["delivery_uncertain"],
            submission_fail_reason=row["fail_reason"],
            **{"submission_" + k: v for k, v in response.items()},
        )
    basis = dict(transport_task_id=task_id, outcome_revision=result.outcome_revision)
    if outcome is not None:
        return replace(
            result,
            closure="UNDETERMINED" if status == "UNKNOWN" else "CLOSED",
            closure_reason="UNKNOWN_POSITION" if status == "UNKNOWN" else "APPLIED_RESULT",
            closure_basis=basis,
        )
    reason = None
    if result.submission_state != "AVAILABLE":
        reason = {
            "UNAVAILABLE": "SUBMISSION_UNAVAILABLE",
            "AMBIGUOUS": "AMBIGUOUS_SUBMISSION",
            "INVALID": "INVALID_EVIDENCE",
        }[result.submission_state]
    elif result.submission_response_state == "VALID" and result.submission_http_status in {
        409,
        422,
    }:
        if pending is not None or has_position_milestones(task):
            reason = "INVALID_EVIDENCE"
        else:
            return replace(
                result,
                closure="CLOSED",
                closure_reason="SUBMIT_REJECTION",
                closure_basis=dict(
                    transport_task_id=task_id,
                    submission_message_id=result.submission_message_id,
                    submission_target=result.submission_target,
                    operation_id=task["operation_id"],
                    http_status=result.submission_http_status,
                    response_code=result.submission_response_code,
                    response_data=deepcopy(result.submission_response_data),
                ),
            )
    elif result.submission_delivery_uncertain or result.submission_delivery_status in {
        "HELD",
        "FAILED",
        "EXPIRED",
    }:
        reason = "DELIVERY_OR_RESULT_UNCERTAIN"
    if reason:
        return replace(result, closure_reason=reason)
    return replace(
        result,
        closure="OPEN",
        closure_reason=(
            "AWAITING_POSITION"
            if pending
            else "AWAITING_RESULT"
            if result.submission_delivery_status == "DONE"
            else "AWAITING_SUBMISSION_OR_RESULT"
        ),
    )


async def get_result(
    session: AsyncSession,
    *,
    transport_task_id: str,
    client_request_id: str,
    caller: TransportCaller,
) -> TransportTaskResult:
    if not isinstance(caller, TransportCaller):
        raise ValueError("caller must be a TransportCaller")
    rows = await read_rows(session, (transport_task_id,))
    task, submissions = rows.get(transport_task_id, (None, []))
    return project(
        task,
        submissions,
        task_id=transport_task_id,
        client_request_id=client_request_id,
        caller=caller,
    )
