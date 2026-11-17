"""Transport result projections. Reads do not apply callbacks or settle delivery."""

from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Literal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.strict_json import canonical_digest, json_object
from app.integrations import history
from app.integrations.runtime_authority import enabled
from app.integrations.wms_envelope import (
    outgoing_message,
    response_projection,
    response_row,
    stored_response,
)
from app.models import (
    IntegrationExchange,
    IntegrationRecord,
    IntegrationReference,
    ReliableMessage,
    TransportTask,
)

from .adapter import TransportSubmitAdapter, is_ctu03_storage_rejection
from .callback.payloads import RackPositionRef, Resulted
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
    has_submission_acceptance: bool | None = None
    submission_message_id: int | None = None
    submission_target: str | None = None
    submission_delivery_status: str | None = None
    submission_delivery_uncertain: bool | None = None
    submission_dispatch_started: bool | None = None
    submission_fail_reason: str | None = None
    submission_response_state: str = "UNAVAILABLE"
    submission_http_status: int | None = None
    submission_response_code: str | None = None
    submission_response_timestamp: int | None = None
    submission_response_data: dict | None = None
    frozen: dict | None = None
    outcome: dict | None = None
    outcome_revision: int | None = None
    arrival_face_reported: bool = False
    position_observation: dict | None = None
    member_facts: dict | None = None
    has_pending_outcome: bool = False
    pending_revision: int | None = None
    pending_result_receipt_id: int | None = None
    applied_result_receipt_id: int | None = None
    applied_trigger_receipt_id: int | None = None
    closure: str = "UNDETERMINED"
    closure_reason: str = "TASK_NOT_FOUND"
    closure_basis: dict | None = None

    @property
    def rack_already_in_storage(self) -> bool:
        # Earlier delivery uncertainty can coexist with a later verified location.
        return (
            self.availability == "FOUND"
            and self.submission_state == "AVAILABLE"
            and self.submission_response_state == "VALID"
            and (self.submission_http_status, self.submission_response_code) == (422, "REJECTED")
            and self.has_submission_acceptance is False
            and self.outcome is None
            and self.position_observation is None
            and not self.has_pending_outcome
            and not self.member_facts
            and is_ctu03_storage_rejection(
                self.frozen or {}, (self.submission_response_data or {}).get("reason_code")
            )
        )


async def read_rows(
    session: AsyncSession, ids: tuple[str, ...], *, check_conflicts: bool = False
) -> dict:
    """One statement includes all original submission matches, including ambiguity."""
    if not ids:
        return {}
    task, message = TransportTask.__table__, ReliableMessage.__table__
    match_identity = or_ if check_conflicts else and_
    message_join, columns = response_projection(message)
    columns.update({n: message.c[n] for n in ("dispatch_started_at", "scope_owner")})
    if check_conflicts:
        columns["delivery_snapshot"] = message.c.delivery_snapshot
    fields = {
        "submission_acceptance",
        "pending_result_source_unknown",
        "pending_result_receipt_id",
        "applied_result_receipt_id",
        "applied_trigger_receipt_id",
    }
    if enabled():
        fields = {"submission_acceptance"}
    task_columns = [c for c in task.c if c.name not in fields]
    task_columns.append(task.c.submission_acceptance.is_not(None).label("has_acceptance"))
    if check_conflicts:
        task_columns.append(task.c.submission_acceptance)
    acceptance_columns = {}
    task_from = task
    if check_conflicts and not enabled():
        for name, reference in (
            ("acceptance_request", task.c.acceptance_request_record_id),
            ("acceptance_response", task.c.acceptance_response_record_id),
        ):
            task_from, body_columns = history.body_projection(task_from, reference, name=name)
            acceptance_columns.update(body_columns)
    query = (
        select(
            *[c.label("task_" + c.name) for c in task_columns],
            *acceptance_columns.values(),
            *[c.label("submit_" + n) for n, c in columns.items()],
        )
        .select_from(
            task_from.outerjoin(
                message_join,
                and_(
                    message.c.operation == OPERATION,
                    match_identity(
                        message.c.operation_id == task.c.operation_id,
                        message.c.business_key == task.c.transport_task_id,
                    ),
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
            task_row = {c.name: row["task_" + c.name] for c in task_columns}
            task_row.update({n: row[n] for n in acceptance_columns})
            grouped[key] = (task_row, [])
        if row["submit_id"] is not None:
            grouped[key][1].append(response_row({n: row["submit_" + n] for n in columns}))
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


class _StoredZone(RackPositionRef):
    kind: Literal["ZONE"]


class _StoredRackFailure(Resulted):
    final_position: _StoredZone


def validate_outcome(task: dict, raw: dict, revision: int, *, applied: bool) -> tuple[dict, str]:
    # Older applied rack failures can locate the rack in a zone. Callback admission stays separate.
    model = (
        _StoredRackFailure
        if applied
        and task["kind"] == "RACK_MOVE"
        and raw.get("status") in ("FAILED", "CANCELLED")
        and (raw.get("final_position") or {}).get("kind") == "ZONE"
        else Resulted
    )
    data = model.model_validate(raw).model_dump(mode="json")
    if (
        (model is _StoredRackFailure and raw != data)
        or type(revision) is not int
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


def validate_position_observation(task: dict, raw: dict) -> dict:
    if "arrival_receipt_id" in raw:
        payload = dict(raw)
        receipt_id = payload.pop("arrival_receipt_id")
        revision = payload["outcome_revision"]
        data, status = validate_outcome(task, payload, revision, applied=False)
        if (
            type(receipt_id) is not int
            or receipt_id < 1
            or task["outcome_status"] != "CANCELLED"
            or task["frozen"].get("rcs_template_id") not in ("CTU01", "CTU02")
            or revision <= task["applied_outcome_revision"]
            or status != "SUCCEEDED"
            or data != payload
            or data["final_position"] != task["frozen"]["target"]
        ):
            raise ValueError("invalid late rack arrival")
        return raw
    payload = dict(raw)
    if "observation_receipt_id" in payload:
        receipt_id = payload.pop("observation_receipt_id")
        if type(receipt_id) is not int or receipt_id < 1:
            raise ValueError("invalid position observation receipt")
    data = Resulted.model_validate(payload).model_dump(mode="json")
    frozen = task["frozen"]
    if (
        payload != data
        or task["kind"] not in {"RACK_MOVE", "RACK_ROTATE"}
        or data["transport_task_id"] != task["transport_task_id"]
        or data["kind"] != task["kind"]
        or data["rack_id"] != frozen["rack_id"]
        or data["status"] != "SUCCEEDED"
        or data["final_position"] is None
        or data["position_unknown"]
        or not (
            (
                frozen["target"]["kind"] == "RACK_POSITION"
                and data["final_position"] != frozen["target"]
            )
            or (
                frozen.get("target_face")
                and data["arrival_face"]
                and data["arrival_face"] != frozen["target_face"]
            )
        )
    ):
        raise ValueError("invalid rack position observation")
    return raw


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
        observation = task.get("position_observation")
        if observation is not None:
            observation = validate_position_observation(task, observation)
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
        has_submission_acceptance=task["has_acceptance"],
        frozen=deepcopy(task["frozen"]),
        outcome=deepcopy(outcome),
        position_observation=deepcopy(observation),
        outcome_revision=task["applied_outcome_revision"],
        arrival_face_reported=bool(
            outcome
            and outcome.get("arrival_face")
            and isinstance(task.get("outcome_digests"), dict)
            and task["outcome_digests"].get(str(task["applied_outcome_revision"]))
            == canonical_digest(outcome)
        ),
        member_facts=deepcopy(facts or {}),
        has_pending_outcome=pending is not None,
        pending_revision=pending["revision"] if pending else None,
        pending_result_receipt_id=task.get("pending_result_receipt_id"),
        applied_result_receipt_id=task.get("applied_result_receipt_id"),
        applied_trigger_receipt_id=task.get("applied_trigger_receipt_id"),
    )
    if len(submissions) > 1:
        result = replace(result, submission_state="AMBIGUOUS")
    elif submissions:
        row = submissions[0]
        response = stored_response(outgoing_message(row), row, TransportSubmitAdapter())
        raw = json_object(row["request_raw"]) if row["request_raw"] is not None else None
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
            submission_dispatch_started=(row["dispatch_started_at"] is not None)
            if row["scope_owner"] is not None
            else None,
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
    elif result.submission_delivery_uncertain:
        reason = "DELIVERY_OR_RESULT_UNCERTAIN"
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
    elif result.submission_delivery_status in {
        "HELD",
        "FAILED",
        "EXPIRED",
        "ABANDONED",
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


async def latest_rack_arrival_cycle(
    session: AsyncSession,
    *,
    owner_domain: str,
    workline_id: str,
    point: str,
    templates: tuple[str, ...],
    after_receipt_id: int | None = None,
) -> str | None:
    """Read the original receipt order of an applied rack arrival, including closed cycles."""
    task = TransportTask.__table__
    receipt_id = task.c.applied_result_receipt_id
    if not enabled():
        receipt_id = (
            select(func.min(IntegrationExchange.receipt_id))
            .select_from(IntegrationRecord)
            .join(
                IntegrationReference, IntegrationReference.source_record_id == IntegrationRecord.id
            )
            .join(
                IntegrationExchange,
                IntegrationExchange.id == IntegrationReference.related_exchange_id,
            )
            .where(
                IntegrationRecord.exchange_id == task.c.submission_exchange_id,
                IntegrationRecord.record_kind == "RESULT_APPLIED",
                IntegrationRecord.outcome_revision == task.c.applied_outcome_revision,
                IntegrationReference.relation_role == "RESULT_SOURCE",
                IntegrationExchange.role == "RECEIPT",
                IntegrationExchange.operation == "transport.task.resulted@v1",
            )
            .having(func.count(IntegrationExchange.receipt_id) == 1)
            .correlate(task)
            .scalar_subquery()
        )
    late_receipt = task.c.position_observation["arrival_receipt_id"].as_integer()
    position_order = func.coalesce(late_receipt, receipt_id)
    newer = (
        position_order > after_receipt_id
        if after_receipt_id is not None
        else position_order.is_not(None)
    )
    query = (
        select(
            task.c.kind,
            task.c.cycle_id,
            task.c.transport_task_id,
            task.c.frozen,
            task.c.applied_outcome,
            task.c.applied_outcome_revision,
            task.c.outcome_status,
            task.c.position_observation,
        )
        .where(
            task.c.owner_domain == owner_domain,
            task.c.workline_id == workline_id,
            task.c.kind.in_(("RACK_MOVE", "RACK_ROTATE")),
            task.c.cycle_id.is_not(None),
            task.c.frozen["rcs_template_id"].as_string().in_(templates),
            task.c.frozen["target"]["kind"].as_string() == "RACK_POSITION",
            task.c.frozen["target"]["location_code"].as_string() == point,
            or_(
                task.c.outcome_status == "SUCCEEDED",
                and_(
                    task.c.outcome_status == "CANCELLED",
                    or_(
                        late_receipt.is_not(None),
                        and_(
                            task.c.applied_outcome["final_position"]["kind"].as_string()
                            == "RACK_POSITION",
                            task.c.applied_outcome["final_position"]["location_code"].as_string()
                            == point,
                        ),
                    ),
                ),
            ),
            newer,
        )
        .order_by(position_order.desc())
        .limit(1)
    )
    with session.no_autoflush:
        row = (await session.execute(query)).mappings().one_or_none()
    if row is None:
        return None
    try:
        validate_frozen(row)
        outcome, status = validate_outcome(
            row, row.applied_outcome, row.applied_outcome_revision, applied=True
        )
        if row.position_observation and "arrival_receipt_id" in row.position_observation:
            outcome = validate_position_observation(row, row.position_observation)
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    actual = {"kind": "RACK_POSITION", "location_code": point}
    return (
        row.cycle_id
        if status == row.outcome_status and outcome["final_position"] == actual
        else None
    )


async def late_rack_arrival_cycles(session: AsyncSession, *, owner_domain: str) -> tuple[str, ...]:
    task = TransportTask.__table__
    with session.no_autoflush:
        return tuple(
            await session.scalars(
                select(task.c.cycle_id)
                .where(
                    task.c.owner_domain == owner_domain,
                    task.c.cycle_id.is_not(None),
                    task.c.outcome_status == "CANCELLED",
                    task.c.position_observation["arrival_receipt_id"].as_integer().is_not(None),
                )
                .distinct()
            )
        )
