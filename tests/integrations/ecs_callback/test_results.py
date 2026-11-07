import json

from sqlalchemy import select

from app.integrations.ecs_callback import registry
from app.integrations.ecs_callback.receiver import receive_ecs_event
from app.integrations.wms_callback import CallbackPayload
from app.models import IntegrationExchange
from tests.helpers import ecs_binding


async def test_original_event_and_application_are_atomic_and_detached(callback_store, monkeypatch):
    from app.integrations.ecs_callback.results import (
        EventApplicationRef,
        get_event,
        list_point_events,
    )

    monkeypatch.setattr(registry, "_registry", {})

    async def handler(ctx, data):
        ctx.bind_application(EventApplicationRef("BIN_PASSAGE", "p1", ctx.receipt_id, None, None))

    registry.ecs_event("SCAN_COMPLETED", device_role="SCAN1", data=CallbackPayload)(handler)
    envelope = {
        "device_code": "D1",
        "event_type": "SCAN_COMPLETED",
        "timestamp": 1,
        "data": {"bin_code": "B1"},
    }
    async with callback_store.begin() as session:
        await receive_ecs_event(session, json.dumps(envelope).encode(), resolve_binding=ecs_binding)
        row = await session.scalar(
            select(IntegrationExchange).where(
                IntegrationExchange.role == "RECEIPT", IntegrationExchange.receipt_active.is_(True)
            )
        )
        event_id = row.receipt_id
        assert row.event_envelope == envelope
    async with callback_store() as session:
        event = await get_event(session, event_id=event_id)
        assert event.availability == "FOUND"
        assert event.record.application_ref.subject_id == "p1"
        assert "device_bindings" not in event.record.context
        event.record.envelope["data"]["bin_code"] = "changed"
        assert (await get_event(session, event_id=event_id)).record.envelope == envelope
        binding = await ecs_binding(session, "D1")
        events = await list_point_events(
            session,
            owner_domain=binding.business_code,
            work_line_id=binding.work_line_id,
            point="SCAN1",
        )
        assert [record.event_id for record in events] == [event_id]
        assert (await get_event(session, event_id=event_id + 100)).availability == "NOT_FOUND"
    async with callback_store.begin() as session:
        row = await session.scalar(
            select(IntegrationExchange).where(
                IntegrationExchange.role == "RECEIPT", IntegrationExchange.receipt_id == event_id
            )
        )
        row.event_envelope = None
    async with callback_store() as session:
        assert (await get_event(session, event_id=event_id)).availability == "UNAVAILABLE"
