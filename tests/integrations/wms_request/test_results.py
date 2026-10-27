import json
from dataclasses import FrozenInstanceError

import pytest
from sqlalchemy import delete, select, update

from app.core.identifiers import uuid7
from app.integrations.wms_request import WmsRequests
from app.models import ReliableMessage

OP = "example.decision.ask@v1"


async def registered(sessions, wms):
    identity = str(uuid7())
    async with sessions.begin() as session:
        ref = await wms.send(session, OP, {"task_id": "T", "qty": 1}, operation_id=identity)
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


@pytest.mark.parametrize("raw", [b"{}", b"[]", b'{"operation":"other.op@v1"}'])
async def test_invalid_original_request_cannot_expose_a_response(sessions, wms, raw):
    mid, oid = await registered(sessions, wms)
    async with sessions.begin() as session:
        await session.execute(
            update(ReliableMessage).where(ReliableMessage.id == mid).values(request_raw=raw)
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
        await session.execute(
            update(ReliableMessage)
            .where(ReliableMessage.id == mid)
            .values(
                status=status,
                last_http_status=http,
                last_verdict="PEER_TERMINAL" if status == "FAILED" else "DELIVERED",
                fail_reason=code if status == "FAILED" else None,
                response_raw=raw if terminal else None,
                last_response_raw=None if terminal else raw,
            )
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
        ref = await wms.send(session, op, {"task_id": "T", "qty": 1}, operation_id=oid)
        await session.execute(
            update(ReliableMessage)
            .where(ReliableMessage.id == ref.message_id)
            .values(
                status="DONE",
                last_verdict="DELIVERED",
                last_http_status=200,
                response_raw=json.dumps(
                    dict(operation_id=oid, code=code, timestamp=1, data={})
                ).encode(),
            )
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
        await session.execute(
            update(ReliableMessage)
            .where(ReliableMessage.id == mid)
            .values(status="DONE", last_verdict="DELIVERED", last_http_status=200, response_raw=raw)
        )
    async with sessions() as session:
        r = await wms.get_result(session, message_id=mid, operation=OP, operation_id=oid)
        assert r.response_state == "INVALID" and r.response_data is None


async def test_local_error_never_exposes_stale_response(sessions, wms):
    mid, oid = await registered(sessions, wms)
    async with sessions.begin() as session:
        await session.execute(
            update(ReliableMessage)
            .where(ReliableMessage.id == mid)
            .values(
                status="FAILED", fail_reason="LOCAL_ERROR", last_response_raw=b'{"code":"DECIDED"}'
            )
        )
    async with sessions() as session:
        r = await wms.get_result(session, message_id=mid, operation=OP, operation_id=oid)
        assert r.delivery_terminal and r.fail_reason == "LOCAL_ERROR"
        assert r.response_state == "NONE" and r.response_data is None
