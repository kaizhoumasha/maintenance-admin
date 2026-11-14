"""Original receipt identities live on active receipt-role exchanges."""

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.identifiers import uuid7
from app.core.strict_json import json_object
from app.integrations import history
from app.integrations.runtime_authority import enabled
from app.models import CallbackReceipt, IntegrationExchange, IntegrationRecord
from app.models.integration_history import RECEIPT_SEQUENCE
from app.reliable.lifecycle import ensure_savepoint_transaction

LOCK_TIMEOUT = "5s"


@dataclass(frozen=True)
class ReceiptView:
    id: int
    exchange_id: UUID
    operation: str
    operation_id: str
    device_code: str | None
    raw_digest: str
    effective_digest: str | None
    compare_mode: str
    status_code: int
    body: dict
    prepared_body: bytes
    resp_timestamp: int
    created_at: datetime
    event_envelope: dict | None
    event_context: dict | None
    application_ref: dict | None
    diagnostic: list | None
    reply_attempt_id: str | None


async def capture_receipt_request(
    session,
    *,
    root,
    raw_body: bytes | None,
    attempt_id: str,
    received_at: datetime,
    reference_record_id: UUID | None = None,
) -> UUID:
    key = f"RECEIVE:{attempt_id}"
    existing = await session.scalar(
        select(IntegrationRecord).where(
            IntegrationRecord.exchange_id == root.id,
            IntegrationRecord.event_key == key,
        )
    )
    candidate = (
        existing.body_record_id if existing else (reference_record_id or root.request_record_id)
    )
    reference = (
        await history.matching_body_reference(
            session,
            record_id=candidate,
            raw_body=raw_body,
        )
        if raw_body is not None
        else reference_record_id
    )
    record_id = await history.write_record(
        session,
        draft=history.RecordDraft(
            exchange_id=root.id,
            record_kind="CALLBACK_RECEIVED",
            event_key=key,
            attempt_id=attempt_id,
            direction="INBOUND",
            occurred_at=existing.occurred_at if existing else received_at,
            body_record_id=reference,
            raw_body=raw_body if reference is None else None,
        ),
    )
    if root.request_record_id is None:
        root.request_record_id = record_id
    return record_id


async def save_receipt_response(
    session,
    *,
    receipt_id: int,
    status_code: int,
    prepared_body: bytes,
    resp_timestamp: int,
    attempt_id: str,
    received_at: datetime,
    raw_body: bytes | None = None,
    reference_record_id: UUID | None = None,
    owner_domain: str | None = None,
    make_current: bool = True,
) -> UUID:
    if enabled():
        receipt = await session.get(CallbackReceipt, receipt_id, with_for_update=True)
        if receipt is None or not receipt.active:
            raise history.EvidenceIntegrityError("original receipt unavailable")
        if make_current:
            receipt.owner_domain = receipt.owner_domain or owner_domain
            receipt.reply_wire = prepared_body
            receipt.reply_sha256 = sha256(prepared_body).hexdigest()
            receipt.reply_attempt_id = attempt_id
            receipt.status_code = status_code
            receipt.resp_timestamp = resp_timestamp
        return receipt.id
    root = await session.scalar(
        select(IntegrationExchange).where(
            IntegrationExchange.role == "RECEIPT",
            IntegrationExchange.receipt_active.is_(True),
            IntegrationExchange.receipt_id == receipt_id,
        )
    )
    if root is None:
        raise history.EvidenceIntegrityError("original receipt unavailable")
    parent = root.request_record_id
    if raw_body is not None:
        parent = await capture_receipt_request(
            session,
            root=root,
            raw_body=raw_body,
            attempt_id=attempt_id,
            received_at=received_at,
        )
    reference = await history.matching_body_reference(
        session,
        record_id=reference_record_id or root.current_response_record_id,
        raw_body=prepared_body,
    )
    response_id = await history.write_record(
        session,
        draft=history.RecordDraft(
            exchange_id=root.id,
            record_kind="RESPONSE_PREPARED",
            event_key=f"REPLY:{attempt_id}",
            attempt_id=attempt_id,
            parent_record_id=parent,
            direction="OUTBOUND",
            occurred_at=received_at,
            http_status=status_code,
            protocol_code=history.reply_protocol_code(prepared_body),
            body_record_id=reference,
            raw_body=prepared_body if reference is None else None,
        ),
    )
    if make_current:
        if root.current_response_record_id is None and root.owner_domain is None:
            root.owner_domain = owner_domain
        root.current_response_record_id = response_id
        root.status_code = status_code
        root.resp_timestamp = resp_timestamp
    return root.id


async def insert_receipt(
    session: AsyncSession,
    *,
    lock_timeout: str = LOCK_TIMEOUT,
    raw_body: bytes | None = None,
    prepared_body: bytes | None = None,
    attempt_id: str | None = None,
    peer_system: str | None = None,
    protocol: str = "callback",
    interaction_kind: str = "CALLBACK",
    defer_response: bool = False,
    request_record_id: UUID | None = None,
    response_record_id: UUID | None = None,
    message_type: str | None = None,
    **values,
) -> int | None:
    postgresql = session.get_bind().dialect.name == "postgresql"
    if postgresql:
        await session.execute(text(f"SET LOCAL lock_timeout = '{lock_timeout}'"))
        receipt_id = await session.scalar(RECEIPT_SEQUENCE.next_value())
    else:
        await ensure_savepoint_transaction(session)
        receipt_id = await session.scalar(
            text("INSERT INTO _integration_receipt_sequence DEFAULT VALUES RETURNING id")
        )
        await session.execute(
            text("DELETE FROM _integration_receipt_sequence WHERE id=:id"), {"id": receipt_id}
        )
    body = values.pop("body")
    attempt_id = attempt_id or str(uuid7())
    insert = pg_insert if postgresql else sqlite_insert
    if enabled():
        wire = (
            None
            if defer_response
            else history.encode_reply(body)
            if prepared_body is None
            else prepared_body
        )
        return await session.scalar(
            insert(CallbackReceipt)
            .values(
                **values,
                id=uuid7(),
                receipt_id=receipt_id,
                protocol=protocol,
                active=True,
                reply_wire=wire,
                reply_sha256=sha256(wire).hexdigest() if wire is not None else None,
                reply_attempt_id=attempt_id if wire is not None else None,
            )
            .on_conflict_do_nothing(
                index_elements=["operation", "operation_id"], index_where=text("active")
            )
            .returning(CallbackReceipt.receipt_id)
        )
    identity_kind = "EVENT_DIGEST" if protocol == "ecs-event" else "OPERATION_ID"
    exchange_id = await session.scalar(
        insert(IntegrationExchange)
        .values(
            **values,
            message_type=message_type,
            role="RECEIPT",
            peer_system=peer_system,
            protocol=protocol,
            direction="INBOUND",
            interaction_kind=interaction_kind,
            identity_kind=identity_kind,
            identity_value=values["operation_id"],
            receipt_id=receipt_id,
            receipt_active=True,
            coverage="DECLARED" if raw_body is not None or request_record_id else "PARTIAL",
        )
        .on_conflict_do_nothing(
            index_elements=["operation", "operation_id"],
            index_where=text("role='RECEIPT' AND receipt_active"),
        )
        .returning(IntegrationExchange.id)
    )
    if exchange_id is None:
        return None
    root = await session.get(IntegrationExchange, exchange_id)
    await capture_receipt_request(
        session,
        root=root,
        raw_body=raw_body,
        attempt_id=attempt_id,
        received_at=values["created_at"],
        reference_record_id=request_record_id,
    )
    if not defer_response:
        await save_receipt_response(
            session,
            receipt_id=receipt_id,
            status_code=values["status_code"],
            prepared_body=history.encode_reply(body) if prepared_body is None else prepared_body,
            resp_timestamp=values["resp_timestamp"],
            attempt_id=attempt_id,
            received_at=values["created_at"],
            reference_record_id=response_record_id,
        )
    return receipt_id


async def find_receipt(
    session: AsyncSession, operation: str, operation_id: str, *, for_update: bool = False
) -> ReceiptView | None:
    if enabled():
        statement = select(CallbackReceipt).where(
            CallbackReceipt.active.is_(True),
            CallbackReceipt.operation == operation,
            CallbackReceipt.operation_id == operation_id,
        )
        if for_update:
            if session.get_bind().dialect.name == "postgresql":
                await session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
            statement = statement.with_for_update().execution_options(populate_existing=True)
        receipt = await session.scalar(statement)
        if receipt is None:
            return None
        wire = receipt.reply_wire
        if wire is None or sha256(wire).hexdigest() != receipt.reply_sha256:
            raise history.EvidenceIntegrityError("original receipt reply integrity mismatch")
        return _receipt_view(receipt, wire, receipt.reply_attempt_id)
    reply = aliased(IntegrationRecord)
    statement = (
        select(IntegrationExchange, reply.attempt_id)
        .outerjoin(reply, reply.id == IntegrationExchange.current_response_record_id)
        .where(
            IntegrationExchange.role == "RECEIPT",
            IntegrationExchange.receipt_active.is_(True),
            IntegrationExchange.operation == operation,
            IntegrationExchange.operation_id == operation_id,
        )
    )
    if for_update:
        if session.get_bind().dialect.name == "postgresql":
            await session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
        statement = statement.with_for_update(of=IntegrationExchange).execution_options(
            populate_existing=True
        )
    row = (await session.execute(statement)).one_or_none()
    if row is None:
        return None
    root, reply_attempt_id = row
    bodies = await history.read_bodies(session, record_ids=(root.current_response_record_id,))
    stored = bodies.get(root.current_response_record_id)
    return _receipt_view(root, stored.raw_body if stored else None, reply_attempt_id)


def _receipt_view(root, wire, reply_attempt_id):
    body = json_object(wire) if wire is not None else None
    if body is None:
        raise history.EvidenceIntegrityError("original receipt reply unavailable")
    return ReceiptView(
        root.receipt_id,
        root.id,
        root.operation,
        root.operation_id,
        root.device_code,
        root.raw_digest,
        root.effective_digest,
        root.compare_mode,
        root.status_code,
        body,
        wire,
        root.resp_timestamp,
        root.created_at,
        root.event_envelope,
        root.event_context,
        root.application_ref,
        root.diagnostic,
        reply_attempt_id,
    )


def receipt_model():
    return CallbackReceipt if enabled() else IntegrationExchange


def active_receipt(table):
    if table is CallbackReceipt:
        return table.active.is_(True)
    return (table.role == "RECEIPT") & table.receipt_active.is_(True)
