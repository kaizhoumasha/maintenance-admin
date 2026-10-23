import json

import pytest
from sqlalchemy import select

from app.core.identifiers import uuid7
from app.integrations.wms_request import InvalidRequestPayload, UnknownOperation
from app.models import ReliableMessage
from app.reliable import DuplicateMessage, MessageStatus

FACT, DECISION = "example.fact.report@v1", "example.decision.ask@v1"
DATA = {"task_id": "T-1", "qty": 2}


async def rows(sessions):
    async with sessions() as session:
        return list(await session.scalars(select(ReliableMessage).order_by(ReliableMessage.id)))


async def test_operation_is_routed_to_its_endpoint_target_in_the_callers_transaction(
    sessions, wms, clock
):
    fact_id, decision_id = str(uuid7(clock())), str(uuid7(clock()))
    async with sessions() as session, session.begin():
        reference = await wms.send(session, FACT, DATA, operation_id=fact_id)
        await wms.send(session, DECISION, DATA, operation_id=decision_id)
    assert reference.status == MessageStatus.PENDING
    fact, decision = await rows(sessions)
    assert (fact.target, decision.target) == ("wms-facts", "wms-decisions")
    envelope = json.loads(fact.request_raw)
    assert list(envelope) == ["operation_id", "operation", "timestamp", "data"]
    assert envelope["data"] == DATA and envelope["operation_id"] == fact_id
    assert fact.expires_at is None  # 事实默认不过期


async def test_rollback_leaves_no_message(sessions, wms, clock):
    with pytest.raises(RuntimeError):
        async with sessions() as session, session.begin():
            await wms.send(session, FACT, DATA, operation_id=str(uuid7(clock())))
            raise RuntimeError("business rejected")
    assert await rows(sessions) == []


async def test_operation_declared_ttl_sets_expiry(sessions, wms, clock):
    async with sessions() as session, session.begin():
        await wms.send(session, "example.fact.timed@v1", DATA, operation_id=str(uuid7(clock())))
    (row,) = await rows(sessions)
    assert row.expires_at is not None


@pytest.mark.parametrize(
    "operation,data,operation_id,error",
    [
        ("example.missing.op@v1", DATA, None, UnknownOperation),
        (FACT, {"task_id": "T-1"}, None, InvalidRequestPayload),  # 缺字段
        (FACT, {**DATA, "extra": 1}, None, InvalidRequestPayload),  # 多余字段
        (FACT, {"task_id": "T-1", "qty": "2"}, None, InvalidRequestPayload),  # 类型不严格
        (FACT, {"task_id": "\ud800", "qty": 1}, None, InvalidRequestPayload),  # 非法 UTF-8
        (FACT, DATA, "not-a-uuid", InvalidRequestPayload),
    ],
)
async def test_invalid_requests_never_reach_the_outbox(
    sessions, wms, clock, operation, data, operation_id, error
):
    operation_id = operation_id or str(uuid7(clock()))
    with pytest.raises(error):
        async with sessions() as session, session.begin():
            await wms.send(session, operation, data, operation_id=operation_id)
    assert await rows(sessions) == []


async def test_reentry_with_the_same_payload_returns_the_original_reference(sessions, wms, clock):
    """调用方重入时时间戳已变,但 Payload 相同,应当得到原消息而不是冲突。"""
    operation_id = str(uuid7(clock()))
    async with sessions() as session, session.begin():
        first = await wms.send(session, FACT, DATA, operation_id=operation_id)
    clock.advance(30)
    async with sessions() as session, session.begin():
        again = await wms.send(session, FACT, DATA, operation_id=operation_id)
    assert again.message_id == first.message_id
    (row,) = await rows(sessions)
    assert json.loads(row.request_raw)["data"] == DATA


async def test_same_identity_with_changed_content_is_a_duplicate_message(sessions, wms, clock):
    operation_id = str(uuid7(clock()))
    async with sessions() as session, session.begin():
        await wms.send(session, FACT, DATA, operation_id=operation_id)
    clock.advance(30)
    with pytest.raises(DuplicateMessage):
        async with sessions() as session, session.begin():
            await wms.send(session, FACT, {**DATA, "qty": 3}, operation_id=operation_id)


async def test_missing_endpoint_target_is_reported(sessions, wms, clock):
    wms.outbox.targets.pop("wms-facts")
    with pytest.raises(RuntimeError, match="wms-facts"):
        async with sessions() as session, session.begin():
            await wms.send(session, FACT, DATA, operation_id=str(uuid7(clock())))
