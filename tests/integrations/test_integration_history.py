from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.core.identifiers import uuid7
from app.integrations import history as history_records
from app.models import IntegrationExchange, IntegrationRecord, ReliableMessage


async def test_core_record_and_outbox_rollback_together(callback_store):
    from app.reliable import MessageDraft, Outbox
    from app.reliable.config import TargetConfig
    from tests.helpers import OpaqueHistoryAdapter

    outbox = Outbox(
        {"history": TargetConfig("history", "http://example.test", "opaque")},
        {"opaque": OpaqueHistoryAdapter()},
        records=history_records,
    )
    async with callback_store() as session:
        await outbox.enqueue(session, MessageDraft("history", "opaque", "original", b"frozen"))
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IntegrationRecord)
                .join(IntegrationExchange, IntegrationExchange.id == IntegrationRecord.exchange_id)
                .where(IntegrationExchange.target_key == "history")
            )
            == 1
        )
        await session.rollback()
    async with callback_store() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ReliableMessage)
                .where(ReliableMessage.target == "history")
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IntegrationExchange)
                .where(IntegrationExchange.target_key == "history")
            )
            == 0
        )


async def test_duplicate_fact_requires_equal_content(callback_store):
    from app.integrations.history import (
        EvidenceIntegrityError,
        RecordDraft,
        read_bodies,
        write_record,
    )

    exchange_id = uuid7()
    async with callback_store.begin() as session:
        session.add(
            IntegrationExchange(
                id=exchange_id,
                role="DIAGNOSTIC",
                protocol="ecs-event",
                direction="INBOUND",
                interaction_kind="EVENT",
                identity_kind="DIAGNOSTIC",
                identity_value="test",
                created_at=datetime(2026, 11, 7, tzinfo=UTC),
            )
        )
        await session.flush()
        draft = RecordDraft(
            exchange_id=exchange_id,
            record_kind="CALLBACK_RECEIVED",
            event_key="original",
            direction="INBOUND",
            raw_body=b"\x00original\xff",
        )
        first = await write_record(session, draft=draft)
        assert await write_record(session, draft=draft) == first
        with pytest.raises(EvidenceIntegrityError):
            await write_record(session, draft=replace(draft, raw_body=b"different"))
        bodies = await read_bodies(session, record_ids=(first,))
        assert bodies[first].raw_body == b"\x00original\xff"
        assert bodies[first].stored_bytes == 10
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IntegrationRecord)
                .where(IntegrationRecord.exchange_id == exchange_id)
            )
            == 1
        )


async def test_unknown_body_and_confirmed_empty_body_stay_distinct(callback_store):
    from app.integrations.history import RecordDraft, read_bodies, write_record

    async with callback_store.begin() as session:
        exchange = IntegrationExchange(
            role="DIAGNOSTIC",
            protocol="ecs-event",
            direction="INBOUND",
            interaction_kind="EVENT",
            identity_kind="DIAGNOSTIC",
            identity_value="body",
            created_at=datetime(2026, 11, 7, tzinfo=UTC),
        )
        session.add(exchange)
        await session.flush()
        draft = RecordDraft(
            exchange_id=exchange.id,
            record_kind="CALLBACK_RECEIVED",
            event_key="unknown",
            direction="INBOUND",
        )
        missing = await write_record(session, draft=draft)
        empty = await write_record(session, draft=replace(draft, event_key="empty", raw_body=b""))
        bodies = await read_bodies(session, record_ids=(missing, empty))
        assert bodies[missing].raw_body is None
        assert bodies[missing].body_state == "UNAVAILABLE"
        assert bodies[empty].raw_body == b""
        assert bodies[empty].body_state == "FULL"


async def test_body_reference_cannot_form_chain(callback_store):
    from app.integrations.history import EvidenceIntegrityError, RecordDraft, write_record

    async with callback_store.begin() as session:
        exchange = IntegrationExchange(
            role="DIAGNOSTIC",
            protocol="ecs-event",
            direction="INBOUND",
            interaction_kind="EVENT",
            identity_kind="DIAGNOSTIC",
            identity_value="references",
            created_at=datetime(2026, 11, 7, tzinfo=UTC),
        )
        session.add(exchange)
        await session.flush()
        draft = RecordDraft(
            exchange_id=exchange.id,
            record_kind="CALLBACK_RECEIVED",
            event_key="canonical",
            direction="INBOUND",
            raw_body=b"canonical",
        )
        original = await write_record(session, draft=draft)
        reference = await write_record(
            session,
            draft=replace(
                draft,
                event_key="reference",
                raw_body=None,
                body_record_id=original,
            ),
        )
        with pytest.raises(EvidenceIntegrityError):
            await write_record(
                session,
                draft=replace(
                    draft,
                    event_key="chain",
                    raw_body=None,
                    body_record_id=reference,
                ),
            )


async def test_metadata_projection_does_not_persist_unknown_fields(callback_store):
    from app.integrations.history import RecordDraft, write_record

    async with callback_store.begin() as session:
        exchange = IntegrationExchange(
            role="DIAGNOSTIC",
            protocol="ecs-event",
            direction="INBOUND",
            interaction_kind="EVENT",
            identity_kind="DIAGNOSTIC",
            identity_value="projection",
            created_at=datetime(2026, 11, 7, tzinfo=UTC),
        )
        session.add(exchange)
        await session.flush()
        record_id = await write_record(
            session,
            draft=RecordDraft(
                exchange_id=exchange.id,
                record_kind="CALLBACK_RECEIVED",
                event_key="projection",
                direction="INBOUND",
                details={"authorization": "must not persist"},
            ),
        )
        record = await session.get(IntegrationRecord, record_id)
        assert record.details == {"projection_state": "UNAVAILABLE"}


async def test_receipt_history_does_not_keep_expired_acceptance_slot(callback_store):
    import json

    from app.integrations.ecs_callback.receiver import receive_ecs_event

    raw = json.dumps(
        dict(device_code="history", event_type="UNKNOWN", timestamp=1, data={})
    ).encode()
    async with callback_store.begin() as session:
        assert (await receive_ecs_event(session, raw)).status_code == 200
        first = await session.scalar(
            select(IntegrationExchange).where(
                IntegrationExchange.role == "RECEIPT", IntegrationExchange.device_code == "history"
            )
        )
        assert first is not None
        first_id = first.receipt_id
        first.receipt_active = False
    async with callback_store.begin() as session:
        assert (await receive_ecs_event(session, raw)).status_code == 200
        active = await session.scalar(
            select(IntegrationExchange).where(
                IntegrationExchange.role == "RECEIPT",
                IntegrationExchange.device_code == "history",
                IntegrationExchange.receipt_active.is_(True),
            )
        )
        assert active.receipt_id > first_id
        assert await session.get(IntegrationExchange, first.id) is not None


async def test_legacy_preview_does_not_invent_receipt_association(callback_store):
    from app.integrations.wms_callback.receiver import CallbackResponse
    from app.integrations.wms_callback.snapshots import record_snapshot

    await record_snapshot(
        callback_store,
        preview=b'{"x',
        truncated=True,
        response=CallbackResponse(400, None, "invalid"),
        request_id="reused-trace",
        received_at=datetime(2026, 11, 7, tzinfo=UTC),
        source=None,
    )
    async with callback_store() as session:
        diagnostic = await session.scalar(
            select(IntegrationRecord).where(
                IntegrationRecord.record_kind == "CALLBACK_RECEIVED",
                IntegrationRecord.details["request_id"].as_string() == "reused-trace",
            )
        )
        assert diagnostic is not None
        assert diagnostic.raw_body == b'{"x'
        assert diagnostic.body_state == "PREVIEW"
        root = await session.get(IntegrationExchange, diagnostic.exchange_id)
        assert root.role == "DIAGNOSTIC"
        assert root.receipt_id is None
        assert diagnostic.details["source"] is None


async def test_diagnostic_body_cannot_become_authoritative_reply(callback_store):
    from app.integrations.history import EvidenceIntegrityError, RecordDraft, write_record

    async with callback_store.begin() as session:
        root = IntegrationExchange(
            role="DIAGNOSTIC",
            protocol="ecs-event",
            direction="INBOUND",
            interaction_kind="EVENT",
            identity_kind="DIAGNOSTIC",
            identity_value="authority",
            created_at=datetime(2026, 11, 7, tzinfo=UTC),
        )
        session.add(root)
        await session.flush()
        diagnostic = await write_record(
            session,
            draft=RecordDraft(
                exchange_id=root.id,
                record_kind="RESPONSE_PREPARED",
                evidence_class="DIAGNOSTIC",
                event_key="diagnostic",
                direction="OUTBOUND",
                raw_body=b'{"code":200}',
            ),
        )
        with pytest.raises(EvidenceIntegrityError):
            await write_record(
                session,
                draft=RecordDraft(
                    exchange_id=root.id,
                    record_kind="RESPONSE_PREPARED",
                    event_key="core",
                    direction="OUTBOUND",
                    body_record_id=diagnostic,
                ),
            )


async def test_consumption_history_commits_with_action_once_per_source(callback_store):
    from app.reliable import MessageDraft, Outbox
    from app.reliable.config import TargetConfig
    from tests.helpers import OpaqueHistoryAdapter

    outbox = Outbox(
        {
            "history-consumption": TargetConfig(
                "history-consumption", "http://example.test", "opaque"
            )
        },
        {"opaque": OpaqueHistoryAdapter()},
        records=history_records,
    )
    async with callback_store.begin() as session:
        message = await outbox.enqueue(
            session, MessageDraft("history-consumption", "opaque", str(uuid7()), b"frozen")
        )
        row = await session.get(ReliableMessage, message.message_id)
        reply_id = await history_records.write_record(
            session,
            draft=history_records.RecordDraft(
                exchange_id=row.exchange_id,
                record_kind="HTTP_RESPONSE_OBSERVED",
                event_key="reply",
                direction="INBOUND",
                raw_body=b"terminal",
            ),
        )
        row.terminal_response_record_id = reply_id
        root_id = row.exchange_id
    source = history_records.SourceRef("WMS_MESSAGE", str(message.message_id))
    now = datetime.now(UTC)
    async with callback_store() as session:
        await history_records.record_consumption(
            session, owner_domain="test", action_id="rolled-back", source=source, consumed_at=now
        )
        await session.rollback()
    async with callback_store.begin() as session:
        for action in ("a", "a", "b"):
            await history_records.record_consumption(
                session, owner_domain="test", action_id=action, source=source, consumed_at=now
            )
    async with callback_store() as session:
        rows = list(
            await session.scalars(
                select(IntegrationRecord).where(
                    IntegrationRecord.exchange_id == root_id,
                    IntegrationRecord.record_kind == "BUSINESS_CONSUMED",
                )
            )
        )
        assert {r.details["action_id"] for r in rows} == {"a", "b"}
        assert all(r.parent_record_id == reply_id for r in rows)


async def test_original_message_reference_survives_runtime_cleanup(callback_store):
    from sqlalchemy import delete

    from app.models import IntegrationReference
    from app.reliable import MessageDraft, Outbox
    from app.reliable.config import TargetConfig
    from tests.helpers import OpaqueHistoryAdapter

    outbox = Outbox(
        {
            "history-message-ref": TargetConfig(
                "history-message-ref", "http://example.test", "opaque"
            )
        },
        {"opaque": OpaqueHistoryAdapter()},
        records=history_records,
    )
    async with callback_store.begin() as session:
        message = await outbox.enqueue(
            session, MessageDraft("history-message-ref", "opaque", str(uuid7()), b"frozen")
        )
        row = await session.get(ReliableMessage, message.message_id)
        root_id = row.exchange_id
        await session.execute(
            delete(ReliableMessage).where(ReliableMessage.id == message.message_id)
        )
    async with callback_store() as session:
        refs = list(
            await session.scalars(
                select(IntegrationReference).where(
                    IntegrationReference.reference_kind == "RELIABLE_MESSAGE",
                    IntegrationReference.namespace == "id",
                    IntegrationReference.reference_value == str(message.message_id),
                )
            )
        )
        assert len(refs) == 1 and refs[0].exchange_id == root_id


async def test_prepared_reply_keeps_protocol_code_after_body_expiry(callback_store):
    from app.integrations.callback_receipts import insert_receipt

    async with callback_store.begin() as session:
        receipt = await insert_receipt(
            session,
            operation="history.code",
            operation_id=str(uuid7()),
            raw_digest="a" * 64,
            compare_mode="RAW",
            status_code=200,
            body={"code": 0},
            resp_timestamp=1,
            created_at=datetime(2026, 11, 7, tzinfo=UTC),
            raw_body=b"request",
        )
        root = await session.scalar(
            select(IntegrationExchange).where(IntegrationExchange.receipt_id == receipt)
        )
        record = await session.get(IntegrationRecord, root.current_response_record_id)
        record.raw_body, record.body_state = None, "PURGED"
    async with callback_store() as session:
        record = await session.get(IntegrationRecord, root.current_response_record_id)
        assert record.protocol_code == "0"
        from app.schemas.integration_history import ExchangeListParams
        from app.services.integration_history_service import list_exchanges

        page = await list_exchanges(session, ExchangeListParams(identity_value=root.identity_value))
        assert page["items"][0]["protocol_result"]["code"] == "0"


async def test_interrupted_response_keeps_headers_without_claiming_body(callback_store):
    from types import SimpleNamespace

    from app.http_client import DeliveryState, FailureKind, HttpResult

    now = datetime(2026, 11, 7, tzinfo=UTC)
    async with callback_store.begin() as session:
        root = IntegrationExchange(
            role="OUTGOING",
            protocol="opaque",
            direction="OUTBOUND",
            interaction_kind="REQUEST",
            identity_kind="OPERATION_ID",
            identity_value="interrupted-response",
            created_at=now,
        )
        session.add(root)
        await session.flush()
        finish = await history_records.finished(
            session,
            claim=SimpleNamespace(
                lease_id="original-lease", message=SimpleNamespace(exchange_id=root.id)
            ),
            outcome=SimpleNamespace(
                result=HttpResult(
                    DeliveryState.DELIVERY_UNKNOWN, FailureKind.READ_TIMEOUT, 503, (), None
                ),
                verdict=None,
                status="PENDING",
                pause_reason=None,
                fail_reason=None,
            ),
            at=now,
            late=False,
        )
        conclusion = await session.get(IntegrationRecord, finish)
        assert conclusion.application_state == "PENDING"
        assert (await history_records.read_bodies(session, record_ids=(finish,)))[
            finish
        ].body_state == "UNAVAILABLE"
        response = await session.scalar(
            select(IntegrationRecord).where(
                IntegrationRecord.exchange_id == root.id,
                IntegrationRecord.record_kind == "HTTP_RESPONSE_OBSERVED",
            )
        )
        assert response.http_status == 503 and response.network_state == "DELIVERY_UNKNOWN"
        assert response.raw_body is None
