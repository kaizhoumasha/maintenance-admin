import asyncio

import pytest
from sqlalchemy import select

from app.models import WorkLine
from app.services.device_bindings import resolve_binding
from tests.helpers import add_bound_device


async def test_binding_resolution_uses_committed_role_after_waiting_for_workline(
    asset_pg, wait_for_lock
):
    factory, code = asset_pg
    async with factory.begin() as session:
        line, _, _ = await add_bound_device(
            session, code, "http://127.0.0.1:45831", line_code=code + "-line"
        )
        line_id = line.id

    async def resolve():
        async with factory.begin() as session:
            return await resolve_binding(session, code)

    pending = None
    try:
        async with factory.begin() as writer:
            line = await writer.scalar(
                select(WorkLine).where(WorkLine.id == line_id).with_for_update()
            )
            line.device_bindings = {"SCAN4": code}
            line.version += 1
            pending = asyncio.create_task(resolve())
            await wait_for_lock(pending)
            assert not pending.done()
        binding = await asyncio.wait_for(pending, 5)
        assert binding.device_role == "SCAN4" and binding.work_line_version == 2
    finally:
        if pending and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("change", ["transfer", "delete"])
async def test_membership_change_during_resolution_rolls_back_without_a_receipt(
    asset_pg, wait_for_lock, change
):
    import json

    from app.core.errors import AppError
    from app.integrations.ecs_callback.envelope import EcsEventEnvelope
    from app.integrations.ecs_callback.receiver import event_identity, receive_ecs_event
    from app.models import CallbackReceipt, Device

    factory, code = asset_pg
    async with factory.begin() as session:
        line, device, _ = await add_bound_device(
            session, code, "http://127.0.0.1:45832", line_code=code + "-line"
        )
        line_id, device_id = line.id, device.id

    async def receive():
        async with factory.begin() as session:
            return await receive_ecs_event(
                session,
                json.dumps(
                    {
                        "device_code": code,
                        "event_type": "SCAN_COMPLETED",
                        "timestamp": 1,
                    }
                ).encode(),
                resolve_binding=resolve_binding,
            )

    pending = None
    try:
        async with factory.begin() as writer:
            line = await writer.get(WorkLine, line_id, with_for_update=True)
            line.device_bindings = {}
            line.version += 1
            device = await writer.get(Device, device_id, with_for_update=True)
            if change == "delete":
                await writer.delete(device)
            else:
                device.work_line_id = None
                device.version += 1
            pending = asyncio.create_task(receive())
            await wait_for_lock(pending)
            assert not pending.done()
        with pytest.raises(AppError) as conflict:
            await asyncio.wait_for(pending, 5)
        assert conflict.value.code == "CONFLICT"
        async with factory() as session:
            identity = event_identity(
                EcsEventEnvelope(
                    device_code=code,
                    event_type="SCAN_COMPLETED",
                    timestamp=1,
                )
            )
            assert (
                await session.scalar(
                    select(CallbackReceipt.id).where(
                        CallbackReceipt.operation == "ecs.event",
                        CallbackReceipt.operation_id == identity,
                    )
                )
                is None
            )
    finally:
        if pending and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
