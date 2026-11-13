"""Independent evidence of the original WMS submission ACK."""

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from functools import partial
from hashlib import sha256
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.strict_json import canonical_digest, json_object
from app.http_client import DeliveryState, HttpResult
from app.integrations import history
from app.integrations.runtime_authority import enabled
from app.integrations.wms_envelope import outgoing_message, stored_response
from app.models import TransportTask
from app.reliable import OutgoingMessage, VerdictKind
from app.reliable.config import TargetConfig
from app.reliable.lifecycle import database_failure_code
from app.reliable.store import AttemptOutcome, Claim, utc_datetime
from app.transactions import after_commit

from .adapter import TransportSubmitAdapter
from .protocol import OPERATION, PROTOCOL
from .results import read_rows, validate_frozen
from .types import TransportCaller, validate_text
from .wire import freeze_request, input_digest, timestamp_ms


@dataclass(frozen=True, slots=True)
class AcceptanceBasis:
    owner_domain: str | None
    caller: TransportCaller
    cycle_id: str | None
    kind: str
    transport_task_id: str
    client_request_id: str
    operation: str
    operation_id: str
    message_id: int
    target: str
    business_key: str
    request_timestamp: int
    request_sha256: str
    request_raw: str
    http_status: int
    response_code: str
    accepted_at: int
    response_raw: str
    attempt_no: int | None
    lease_id: str | None
    saved_at: int | None

    @property
    def frozen(self) -> dict:
        data = json_object(self.request_raw.encode())["data"]
        del data["transport_task_id"]
        return data


@dataclass(frozen=True, slots=True)
class SubmissionAcceptance:
    state: Literal["ACCEPTED", "MISSING", "INVALID", "IDENTITY_MISMATCH"]
    basis: AcceptanceBasis | None = None


def _basis(task, message, message_id, response, *, attempt_no=None, lease_id=None, saved_at=None):
    task_id = task["transport_task_id"]
    timestamp = timestamp_ms(utc_datetime(task["created_at"], known_sqlite_utc=True))
    frozen = validate_frozen(task)
    caller = TransportCaller(task["workline_id"], task["station_id"])
    method = {
        "RACK_MOVE": "move_rack",
        "RACK_ROTATE": "rotate_rack",
        "BIN_MOVE": "move_bins",
        "BIN_EXCHANGE": "exchange_bins",
    }[task["kind"]]
    original = freeze_request(task["operation_id"], task_id, timestamp, frozen)
    if (
        input_digest(method, caller, frozen) != task["input_digest"]
        or message.operation != OPERATION
        or message.operation_id != task["operation_id"]
        or message.business_key != task_id
        or message.request_raw != original
        or type(message_id) is not int
        or message_id <= 0
    ):
        raise ValueError("Original submission mismatch")
    validate_text(message.target)
    if saved_at is not None or lease_id is not None or attempt_no is not None:
        validate_text(lease_id)
        if (
            type(attempt_no) is not int
            or attempt_no < 1
            or type(saved_at) is not int
            or saved_at < 0
        ):
            raise ValueError("Invalid attempt identity")
    if type(response.status_code) is not int:
        raise ValueError("Invalid HTTP status")
    if TransportSubmitAdapter().classify_response(message, response).kind != VerdictKind.DELIVERED:
        raise ValueError("Invalid transport acceptance")
    ack = json_object(response.body)
    return AcceptanceBasis(
        owner_domain=task["owner_domain"],
        caller=caller,
        cycle_id=task["cycle_id"],
        kind=task["kind"],
        transport_task_id=task_id,
        client_request_id=task["client_request_id"],
        operation=message.operation,
        operation_id=message.operation_id,
        message_id=message_id,
        target=message.target,
        business_key=message.business_key,
        request_timestamp=timestamp,
        request_sha256=sha256(original).hexdigest(),
        request_raw=original.decode(),
        http_status=response.status_code,
        response_code=ack["code"],
        accepted_at=ack["timestamp"],
        response_raw=response.body.decode(),
        attempt_no=attempt_no,
        lease_id=lease_id,
        saved_at=saved_at,
    )


def _saved(task, evidence):
    if not isinstance(evidence, dict) or evidence["saved_at"] is None:
        raise ValueError("Missing recorded attempt")
    if evidence.get("format_version") not in {None, 2}:
        raise ValueError("Unknown acceptance format")
    original = evidence
    evidence = {
        k: v
        for k, v in evidence.items()
        if k not in {"format_version", "request_raw", "response_raw"}
    }
    for side in ("request", "response"):
        key = side + "_raw"
        evidence[key] = (
            original[key]
            if enabled()
            else history.projected_body(task, name="acceptance_" + side).decode()
        )
    message = OutgoingMessage(
        evidence["target"],
        OPERATION,
        evidence["operation_id"],
        evidence["request_raw"].encode(),
        evidence["business_key"],
        None,
        task["created_at"],
        None,
    )
    response = HttpResult(
        DeliveryState.HTTP_RESPONSE_COMPLETE,
        None,
        evidence["http_status"],
        (),
        evidence["response_raw"].encode(),
    )
    basis = _basis(
        task,
        message,
        evidence["message_id"],
        response,
        attempt_no=evidence["attempt_no"],
        lease_id=evidence["lease_id"],
        saved_at=evidence["saved_at"],
    )
    snapshot = dict(evidence)
    digest = snapshot.pop("evidence_sha256")
    if not canonical_digest(asdict(basis)) == digest == canonical_digest(snapshot):
        raise ValueError("Recorded acceptance mismatch")
    return basis


async def get_submission_acceptance(
    session: AsyncSession,
    *,
    transport_task_id: str,
    client_request_id: str,
    caller: TransportCaller,
    owner: str | None,
) -> SubmissionAcceptance:
    if not isinstance(caller, TransportCaller):
        raise ValueError("caller must be a TransportCaller")
    rows = await read_rows(session, (transport_task_id,), check_conflicts=True)
    task, submissions = rows.get(transport_task_id, (None, []))
    if task is None:
        return SubmissionAcceptance("MISSING")
    if (
        task["client_request_id"],
        task["owner_domain"],
        task["workline_id"],
        task["station_id"],
    ) != (client_request_id, owner, caller.workline_id, caller.station_id):
        return SubmissionAcceptance("IDENTITY_MISMATCH")
    try:
        if task["has_acceptance"]:
            basis = _saved(task, task["submission_acceptance"])
        else:
            if not submissions:
                return SubmissionAcceptance("MISSING")
            (row,) = submissions
            if (
                row["operation_id"] != task["operation_id"]
                or row["business_key"] != transport_task_id
                or row["delivery_snapshot"] is not None
            ):
                return SubmissionAcceptance("INVALID")
            if row["response_raw"] is None:
                return SubmissionAcceptance("MISSING")
            message = outgoing_message(row)
            if stored_response(message, row, TransportSubmitAdapter())["response_state"] != "VALID":
                return SubmissionAcceptance("INVALID")
            result = HttpResult(
                DeliveryState.HTTP_RESPONSE_COMPLETE,
                None,
                row["last_http_status"],
                (),
                row["response_raw"],
            )
            basis = _basis(task, message, row["id"], result)
        return SubmissionAcceptance("ACCEPTED", basis)
    except (ValueError, KeyError, TypeError, AttributeError):
        return SubmissionAcceptance("INVALID")


def submission_observer(
    targets: Mapping[str, TargetConfig],
    session_factory: async_sessionmaker[AsyncSession],
    business_committed: Callable[[str], None],
):
    transport_targets = {
        name for name, t in targets.items() if t.protocol == PROTOCOL and t.address_mode == "STATIC"
    }

    async def observe(claim: Claim, outcome: AttemptOutcome) -> None:
        message = claim.message
        if (
            message.target not in transport_targets
            or message.delivery_snapshot is not None
            or message.operation != OPERATION
            or message.business_key is None
        ):
            return
        if (
            outcome.status != "DONE"
            or outcome.result is None
            or outcome.verdict is None
            or outcome.verdict.kind != VerdictKind.DELIVERED
            or outcome.verdict.response != outcome.result.body
        ):
            return
        task_table = TransportTask.__table__
        identity = task_table.c.transport_task_id == message.business_key
        query = select(
            task_table, task_table.c.submission_acceptance.is_not(None).label("has_acceptance")
        ).where(identity, task_table.c.operation_id == message.operation_id)
        for attempt in range(3):
            committing = False
            try:
                async with session_factory() as session, session.begin():
                    if session.get_bind().dialect.name == "postgresql":
                        await session.connection(
                            execution_options={"isolation_level": "REPEATABLE READ"}
                        )
                    task = (await session.execute(query.with_for_update())).mappings().one_or_none()
                    if task is None:
                        return
                    basis = _basis(
                        task,
                        message,
                        claim.message_id,
                        outcome.result,
                        attempt_no=claim.attempt_no,
                        lease_id=claim.lease_id,
                        saved_at=timestamp_ms(datetime.now(UTC)),
                    )
                    if task["has_acceptance"]:
                        rows = await read_rows(
                            session, (message.business_key,), check_conflicts=True
                        )
                        saved_task, _ = rows[message.business_key]
                        existing = _saved(saved_task, task["submission_acceptance"])
                        if (existing.message_id, existing.target, existing.request_sha256) != (
                            basis.message_id,
                            basis.target,
                            basis.request_sha256,
                        ):
                            raise ValueError("Conflicting original submission")
                        return
                    # Read immutable submit identity without locking its delivery row.
                    rows = await read_rows(session, (message.business_key,), check_conflicts=True)
                    _, submissions = rows[message.business_key]
                    (original,) = submissions
                    if (
                        original["id"] != claim.message_id
                        or original["target"] != message.target
                        or original["operation_id"] != message.operation_id
                        or original["business_key"] != message.business_key
                        or original["request_raw"] != message.request_raw
                        or original["delivery_snapshot"] is not None
                    ):
                        raise ValueError("Claim does not identify the original submission")
                    snapshot = asdict(basis)
                    snapshot["evidence_sha256"] = canonical_digest(snapshot)
                    if not enabled():
                        del snapshot["request_raw"], snapshot["response_raw"]
                    snapshot["format_version"] = 2
                    response_id = await history.observe_response(
                        session, claim=claim, outcome=outcome, at=datetime.now(UTC)
                    )
                    await session.execute(
                        task_table.update()
                        .where(identity)
                        .values(
                            submission_acceptance=snapshot,
                            acceptance_request_record_id=claim.message.request_record_id,
                            acceptance_response_record_id=response_id,
                        )
                    )
                    if basis.owner_domain is not None:
                        after_commit(session, partial(business_committed, basis.owner_domain))
                    committing = True
                return
            except Exception as error:
                if database_failure_code(error) in {"40001", "40P01"}:
                    if attempt < 2:
                        continue
                elif committing:
                    # A lost commit reply can mean success. Verify it in a new transaction.
                    async with session_factory() as reader:
                        rows = await read_rows(
                            reader, (message.business_key,), check_conflicts=True
                        )
                        task, _ = rows.get(message.business_key, (None, []))
                        if task is not None and task["submission_acceptance"] is not None:
                            if _saved(task, task["submission_acceptance"]) == basis:
                                return
                raise

    return observe
