import json

import pytest
from pydantic import Field
from sqlalchemy import func, select

from app.integrations.ecs_callback import CallbackPayload, registry
from app.integrations.ecs_callback.binding import DeviceBinding
from app.integrations.ecs_callback.receiver import receive_ecs_event
from app.models import CallbackReceipt


class BinScan(CallbackPayload):
    bin_code: str = Field(min_length=1)


class ExitScan(CallbackPayload):
    exit_code: str = Field(min_length=1)


async def test_shared_event_dispatches_by_role_and_replays_before_binding_lookup(
    callback_store, monkeypatch
):
    monkeypatch.setattr(registry, "_registry", {})
    handled = []

    @registry.ecs_event("SCAN_COMPLETED", device_role="SCAN1", data=BinScan)
    async def entry(ctx, payload):
        handled.append((ctx.work_line_id, ctx.device_role, payload.bin_code))

    @registry.ecs_event("SCAN_COMPLETED", device_role="SCAN4", data=ExitScan)
    async def exit(ctx, payload):
        handled.append((ctx.work_line_id, ctx.device_role, payload.exit_code))

    async def resolve(session, code):
        return DeviceBinding(
            device_id=1,
            device_code=code,
            device_version=1,
            work_line_id=4,
            work_line_version=1,
            device_role="SCAN1" if code == "STATION_SCAN13" else "SCAN4",
            device_bindings={},
            position_bindings={},
        )

    raw = json.dumps(
        {
            "device_code": "STATION_SCAN13",
            "event_type": "SCAN_COMPLETED",
            "timestamp": 1,
            "data": {"bin_code": "B1"},
        }
    ).encode()
    async with callback_store.begin() as session:
        first = await receive_ecs_event(session, raw, resolve_binding=resolve)
        assert first.status_code == 200
    raw_exit = json.dumps(
        {
            "device_code": "STATION_SCAN16",
            "event_type": "SCAN_COMPLETED",
            "timestamp": 1,
            "data": {"exit_code": "RETURN"},
        }
    ).encode()
    async with callback_store.begin() as session:
        assert (
            await receive_ecs_event(session, raw_exit, resolve_binding=resolve)
        ).status_code == 200
    assert handled == [(4, "SCAN1", "B1"), (4, "SCAN4", "RETURN")]

    async def forbidden(*args):
        raise AssertionError("duplicate event must not resolve its current binding")

    async with callback_store.begin() as session:
        replay = await receive_ecs_event(session, raw, resolve_binding=forbidden)
        assert replay.outcome == "duplicate"
        assert await session.scalar(select(func.count()).select_from(CallbackReceipt)) == 2


async def test_unbound_receipt_does_not_apply_after_a_binding_is_added(callback_store, monkeypatch):
    monkeypatch.setattr(registry, "_registry", {})
    applied = []

    @registry.ecs_event("SCAN_COMPLETED", device_role="SCAN1", data=BinScan)
    async def handler(ctx, payload):
        applied.append(payload.bin_code)

    async def unbound(session, code):
        return None

    raw = json.dumps({"device_code": "D1", "event_type": "SCAN_COMPLETED", "timestamp": 1}).encode()
    async with callback_store.begin() as session:
        assert (await receive_ecs_event(session, raw, resolve_binding=unbound)).status_code == 200

    async def failed(*args):
        raise RuntimeError("binding resolver must not run for a durable duplicate")

    async with callback_store.begin() as session:
        replay = await receive_ecs_event(session, raw, resolve_binding=failed)
        assert replay.outcome == "duplicate"
    assert applied == []


async def test_binding_failure_rolls_back_without_acknowledging(callback_store, monkeypatch):
    monkeypatch.setattr(registry, "_registry", {})

    async def failed(*args):
        raise RuntimeError("binding changed")

    raw = json.dumps({"device_code": "D1", "event_type": "SCAN_COMPLETED", "timestamp": 1}).encode()
    async with callback_store() as session:
        with pytest.raises(RuntimeError, match="binding changed"):
            async with session.begin():
                await receive_ecs_event(session, raw, resolve_binding=failed)
    async with callback_store() as session:
        assert await session.scalar(select(func.count()).select_from(CallbackReceipt)) == 0


async def test_resolver_cannot_dispatch_another_devices_binding(callback_store, monkeypatch):
    from tests.helpers import ecs_binding

    monkeypatch.setattr(registry, "_registry", {})

    @registry.ecs_event("SCAN_COMPLETED", device_role="SCAN1", data=BinScan)
    async def handler(ctx, payload):
        pytest.fail("a mismatched device binding must not execute business logic")

    async def wrong_device(session, code):
        return await ecs_binding(session, "OTHER")

    raw = json.dumps(
        {
            "device_code": "D1",
            "event_type": "SCAN_COMPLETED",
            "timestamp": 1,
            "data": {"bin_code": "B1"},
        }
    ).encode()
    async with callback_store() as session:
        with pytest.raises(RuntimeError, match="binding.*device"):
            async with session.begin():
                await receive_ecs_event(session, raw, resolve_binding=wrong_device)
