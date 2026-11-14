import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select, update

from app.core.clock import unix_ms
from app.core.config import get_settings
from app.core.identifiers import uuid7
from app.integrations.wms_request import WmsRequests
from app.integrations.wms_request.operation import lookup
from app.models import ReliableMessage
from tests.helpers import read_history_body, update_message_bodies

OP = "example.decision.ask@v1"


async def registered(sessions, wms):
    identity = str(uuid7())
    async with sessions.begin() as session:
        ref = await wms.send(
            session, lookup(OP).data_model(task_id="T", qty=1), operation_id=identity
        )
    return ref.message_id, identity


async def test_request_result_uses_original_message_identity(sessions, wms):
    mid, oid = await registered(sessions, wms)
    reader = WmsRequests()  # no configured target needed for reading
    async with sessions() as session:
        found = await reader.get_result(session, message_id=mid, operation=OP, operation_id=oid)
        assert found.availability == "FOUND" and found.target == "wms-decisions"
        assert found.delivery_status == "PENDING" and found.response_state == "NONE"
        for op, identity in [("other.op@v1", oid), (OP, str(uuid7()))]:
            wrong = await reader.get_result(
                session, message_id=mid, operation=op, operation_id=identity
            )
            assert wrong.availability == "IDENTITY_MISMATCH" and wrong.target is None
        missing = await reader.get_result(
            session, message_id=mid + 1, operation=OP, operation_id=oid
        )
        assert missing.availability == "NOT_FOUND" and missing.delivery_status is None


async def test_cutover_reads_original_terminal_response_and_received_time(
    sessions, wms, monkeypatch
):
    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", True)
    mid, oid = await registered(sessions, wms)
    received = datetime(2026, 11, 13, 1, 2, 3, 456000, tzinfo=UTC)
    async with sessions.begin() as session:
        row = await session.get(ReliableMessage, mid)
        row.status = "DONE"
        row.last_http_status = 200
        row.last_verdict = "DELIVERED"
        row.terminal_response_wire = json.dumps(
            dict(operation_id=oid, code="DECIDED", timestamp=10, data={"ok": True})
        ).encode()
        row.terminal_response_at = received
    async with sessions() as session:
        result = await wms.get_result(session, message_id=mid, operation=OP, operation_id=oid)
        assert (result.availability, result.delivery_status, result.response_state) == (
            "FOUND",
            "DONE",
            "VALID",
        )
        assert (result.http_status, result.response_code, result.response_data) == (
            200,
            "DECIDED",
            {"ok": True},
        )
        assert result.response_timestamp == 10
        assert result.response_received_at_ms == unix_ms(received)


@pytest.mark.parametrize("raw", [b"{}", b"[]", b'{"operation":"other.op@v1"}'])
async def test_invalid_original_request_cannot_expose_a_response(sessions, wms, raw):
    mid, oid = await registered(sessions, wms)
    async with sessions.begin() as session:
        await update_message_bodies(
            session, update(ReliableMessage).where(ReliableMessage.id == mid), request_raw=raw
        )
    async with sessions() as session:
        result = await wms.get_result(session, message_id=mid, operation=OP, operation_id=oid)
        assert result.availability == "INVALID"
        assert result.response_state == "UNAVAILABLE" and result.response_data is None
        assert result.target is None and result.delivery_status is None


@pytest.mark.parametrize(
    "status,http,code,data,expected",
    [
        ("DONE", 200, "DECIDED", {"ok": True}, "VALID"),
        ("DONE", 202, "PREPARE_ACCEPTED", {}, "VALID"),
        (
            "DONE",
            202,
            "PREPARE_ACCEPTED",
            {"task_id": "PICK-001", "workline_code": "KT16"},
            "VALID",
        ),
        ("FAILED", 422, "REJECTED", {"reason_code": "INVALID_DATA"}, "VALID"),
        ("FAILED", 409, "CONFLICT", {"reason_code": "STATE_CONFLICT"}, "VALID"),
        ("SENDING", 503, "UNAVAILABLE", {}, "NONE"),
        ("PENDING", 429, "BUSY", {"retry_after_ms": 10}, "NONE"),
        ("DONE", 200, "RECORDED", {}, "INVALID"),
        ("DONE", 200, "DECIDED", {}, "VALID"),
        ("EXPIRED", None, None, {}, "NONE"),
    ],
)
async def test_delivery_terminal_is_not_business_completion(
    sessions, wms, status, http, code, data, expected
):
    mid, oid = await registered(sessions, wms)
    raw = json.dumps(dict(operation_id=oid, code=code, timestamp=10, data=data)).encode()
    terminal = status in {"DONE", "FAILED"}
    async with sessions.begin() as session:
        await update_message_bodies(
            session,
            update(ReliableMessage).where(ReliableMessage.id == mid),
            status=status,
            last_http_status=http,
            last_verdict="PEER_TERMINAL" if status == "FAILED" else "DELIVERED",
            fail_reason=code if status == "FAILED" else None,
            response_raw=raw if terminal else None,
            last_response_raw=None if terminal else raw,
        )
    async with sessions() as session:
        result = await wms.get_result(session, message_id=mid, operation=OP, operation_id=oid)
        assert result.response_state == expected
        assert result.delivery_terminal == (status in {"DONE", "FAILED", "EXPIRED"})
        assert result.response_data == (data if expected == "VALID" else None)
        assert not hasattr(result, "completed")
        if expected == "VALID":
            with pytest.raises(FrozenInstanceError):
                result.response_code = "OTHER"
            result.response_data["mutated"] = True
            again = await wms.get_result(session, message_id=mid, operation=OP, operation_id=oid)
            assert "mutated" not in again.response_data


async def test_missing_terminal_response_and_retention_never_mean_success(sessions, wms):
    mid, oid = await registered(sessions, wms)
    async with sessions.begin() as session:
        await session.execute(
            update(ReliableMessage).where(ReliableMessage.id == mid).values(status="DONE")
        )
    async with sessions() as session:
        result = await wms.get_result(session, message_id=mid, operation=OP, operation_id=oid)
        assert result.response_state == "UNAVAILABLE"
    async with sessions.begin() as session:
        await session.execute(delete(ReliableMessage).where(ReliableMessage.id == mid))
    async with sessions() as session:
        assert (
            await wms.get_result(session, message_id=mid, operation=OP, operation_id=oid)
        ).availability == "NOT_FOUND"


async def test_reads_do_not_flush_or_reuse_old_orm_values(sessions, wms):
    mid, oid = await registered(sessions, wms)
    async with sessions() as session:
        row = await session.get(ReliableMessage, mid)
        row.status = "DONE"  # pending local change must not flush or replace stored projection
        result = await wms.get_result(session, message_id=mid, operation=OP, operation_id=oid)
        assert result.delivery_status == "PENDING" and row in session.dirty
        assert not session.new
        await session.rollback()
    async with sessions() as session:
        assert await session.scalar(select(ReliableMessage.status)) == "PENDING"


@pytest.mark.parametrize("code", ["RECORDED", "DUPLICATE"])
async def test_fact_receipt_is_a_protocol_response(sessions, wms, code):
    op = "example.fact.report@v1"
    oid = str(uuid7())
    async with sessions.begin() as session:
        ref = await wms.send(session, lookup(op).data_model(task_id="T", qty=1), operation_id=oid)
        await update_message_bodies(
            session,
            update(ReliableMessage).where(ReliableMessage.id == ref.message_id),
            status="DONE",
            last_verdict="DELIVERED",
            last_http_status=200,
            response_raw=json.dumps(
                dict(operation_id=oid, code=code, timestamp=1, data={})
            ).encode(),
        )
    async with sessions() as session:
        r = await wms.get_result(session, message_id=ref.message_id, operation=op, operation_id=oid)
        assert r.response_state == "VALID" and r.response_code == code and r.response_data == {}


@pytest.mark.parametrize(
    "change",
    [
        {"operation_id": "wrong"},
        {"timestamp": True},
        {"data": []},
        {"extra": "invalid"},
    ],
)
async def test_invalid_terminal_envelope_is_not_a_decision(sessions, wms, change):
    mid, oid = await registered(sessions, wms)
    raw = json.dumps(dict(operation_id=oid, code="DECIDED", timestamp=1, data={}) | change).encode()
    async with sessions.begin() as session:
        await update_message_bodies(
            session,
            update(ReliableMessage).where(ReliableMessage.id == mid),
            status="DONE",
            last_verdict="DELIVERED",
            last_http_status=200,
            response_raw=raw,
        )
    async with sessions() as session:
        r = await wms.get_result(session, message_id=mid, operation=OP, operation_id=oid)
        assert r.response_state == "INVALID" and r.response_data is None


async def test_local_error_never_exposes_stale_response(sessions, wms):
    mid, oid = await registered(sessions, wms)
    async with sessions.begin() as session:
        await update_message_bodies(
            session,
            update(ReliableMessage).where(ReliableMessage.id == mid),
            status="FAILED",
            fail_reason="LOCAL_ERROR",
            last_response_raw=b'{"code":"DECIDED"}',
        )
    async with sessions() as session:
        r = await wms.get_result(session, message_id=mid, operation=OP, operation_id=oid)
        assert r.delivery_terminal and r.fail_reason == "LOCAL_ERROR"
        assert r.response_state == "NONE" and r.response_data is None


@pytest.mark.parametrize("number", ["1e400", "-1e400"])
async def test_overflowed_terminal_response_is_invalid_and_keeps_original_bytes(
    sessions, wms, number
):
    mid, oid = await registered(sessions, wms)
    raw = json.dumps(dict(operation_id=oid, code="DECIDED", timestamp=1, data={})).encode()
    raw = raw.replace(b'"data": {}', b'"data": {"nested": [' + number.encode() + b"]}")
    async with sessions.begin() as session:
        await update_message_bodies(
            session,
            update(ReliableMessage).where(ReliableMessage.id == mid),
            status="DONE",
            last_verdict="DELIVERED",
            last_http_status=200,
            response_raw=raw,
        )
    async with sessions() as session:
        result = await wms.get_result(session, message_id=mid, operation=OP, operation_id=oid)
        assert result.response_state == "INVALID" and result.response_data is None
        assert (
            await read_history_body(
                session,
                await session.scalar(
                    select(ReliableMessage.terminal_response_record_id).where(
                        ReliableMessage.id == mid
                    )
                ),
            )
            == raw
        )


async def test_result_reader_uses_only_original_terminal_reference(sessions, wms):
    from app.integrations.history import RecordDraft, write_record

    mid, oid = await registered(sessions, wms)
    raw = json.dumps(
        dict(operation_id=oid, code="DECIDED", timestamp=10, data={"ok": True})
    ).encode()
    async with sessions.begin() as session:
        row = await session.get(ReliableMessage, mid)
        reply = await write_record(
            session,
            draft=RecordDraft(
                exchange_id=row.exchange_id,
                record_kind="HTTP_RESPONSE_OBSERVED",
                event_key="test-response",
                direction="INBOUND",
                raw_body=raw,
            ),
        )
        row.status, row.last_verdict, row.last_http_status = "DONE", "DELIVERED", 200
        row.last_response_record_id = reply
    async with sessions() as session:
        result = await wms.get_result(session, message_id=mid, operation=OP, operation_id=oid)
        assert result.response_state == "UNAVAILABLE"
    async with sessions.begin() as session:
        row = await session.get(ReliableMessage, mid)
        row.terminal_response_record_id = reply
        row.request_raw, row.response_raw = b"obsolete", b"obsolete"
    async with sessions() as session:
        result = await wms.get_result(session, message_id=mid, operation=OP, operation_id=oid)
        assert result.response_state == "VALID" and result.response_data == {"ok": True}
