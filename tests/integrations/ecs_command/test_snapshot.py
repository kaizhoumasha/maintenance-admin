from dataclasses import replace

import pytest
from sqlalchemy import select

from app.models import ReliableMessage
from app.reliable import MessageDraft
from app.reliable.types import DeliverySnapshot, DuplicateMessage


async def test_snapshot_is_part_of_identity_and_hold_is_not_resumable(ecs):
    async with ecs.factory.begin() as session:
        command = await ecs.service.create(
            session,
            owner=ecs.owner,
            call_key="call",
            device_code="D1",
            task_type="MOVE",
            timeout_ms=30000,
            params={"qty": 1},
        )
    async with ecs.factory.begin() as session:
        row = await session.scalar(select(ReliableMessage))
        draft = MessageDraft(
            row.target,
            row.operation,
            row.operation_id,
            row.request_raw,
            delivery_snapshot=DeliverySnapshot(**row.delivery_snapshot),
        )
        with pytest.raises(DuplicateMessage):
            await ecs.outbox.enqueue(
                session,
                replace(
                    draft,
                    delivery_snapshot=replace(draft.delivery_snapshot, origin="http://other.test"),
                ),
            )
        assert await ecs.outbox.hold(
            session, "ecs", "ecs.command", command.command_code, reason="DELIVERY_UNKNOWN"
        )
        assert not await ecs.outbox.resume(session, "ecs", "ecs.command", command.command_code)
        await session.refresh(row)
        assert row.status == "HELD" and row.next_attempt_at is None
