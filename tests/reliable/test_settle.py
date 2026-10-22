"""settle:对端已通过其他渠道确认接收时,结束未完成消息的发送义务。"""

import pytest

from app.models import ReliableMessage
from app.reliable.store import AttemptOutcome
from app.reliable.types import MessageStatus


async def enqueue(rig):
    async with rig.factory() as session:
        result = await rig.outbox.enqueue(session, rig.draft())
        await session.commit()
        return result.message_id


async def row(rig, message_id):
    async with rig.factory() as session:
        return await session.get(ReliableMessage, message_id, populate_existing=True)


async def settle(rig):
    async with rig.factory() as session:
        settled = await rig.outbox.settle(session, rig.target.name, "opaque", "id")
        await session.commit()
        return settled


@pytest.mark.parametrize("status", ["PENDING", "STALLED"])
async def test_open_message_becomes_done_without_a_response(storage, status):
    rig = storage
    message_id = await enqueue(rig)
    if status == "STALLED":
        async with rig.factory() as session:
            await session.execute(
                ReliableMessage.__table__.update()
                .where(ReliableMessage.id == message_id)
                .values(status="STALLED", next_attempt_at=None)
            )
            await session.commit()
    assert await settle(rig)
    done = await row(rig, message_id)
    assert (done.status, done.response_raw, done.last_verdict) == ("DONE", None, None)
    assert done.next_attempt_at is not None
    assert not await rig.store.claim(1, rig.targets)  # 不再被领取发送
    assert not await settle(rig)  # 已是终态


async def test_late_result_of_an_in_flight_attempt_is_ignored(storage):
    rig = storage
    message_id = await enqueue(rig)
    (claim,) = await rig.store.claim(1, rig.targets)
    assert (await row(rig, message_id)).status == "SENDING"
    assert await settle(rig)
    outcome = AttemptOutcome(MessageStatus.PENDING, next_attempt_at=rig.clock())
    assert not await rig.store.finish(claim, outcome)
    assert (await row(rig, message_id)).status == "DONE"


async def test_unknown_message_is_not_settled(storage):
    async with storage.factory() as session:
        assert not await storage.outbox.settle(session, storage.target.name, "opaque", "missing")
