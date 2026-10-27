"""Test-only acceptance services use existing rows as durable acceptance/pending evidence."""

from dataclasses import replace

import pytest
from sqlalchemy import delete, select

from app.businesses.runtime import BusinessRuntime, require_runtime
from app.core.identifiers import uuid7
from app.integrations.ecs_callback import CallbackPayload
from app.integrations.ecs_callback import registry as ecs
from app.integrations.wms_callback import Accepted, Conflict, Rejected
from app.integrations.wms_callback import registry as wms
from app.main import app
from app.models import CallbackReceipt, Device, DeviceRoute, WorkLine
from app.services.device_bindings import resolve_binding
from app.services.workline_service import business_configuration_issue
from tests.helpers import add_bound_device, workline_business_definition


class Payload(CallbackPayload):
    device_code: str


@pytest.fixture
async def entry_setup(callback_store, monkeypatch):
    definition = replace(workline_business_definition(), position_slots=())
    monkeypatch.setattr(ecs, "_registry", {})
    monkeypatch.setattr(wms, "_registry", {})
    monkeypatch.setattr(app.state, "ecs_event_binding_resolver", resolve_binding)
    codes, ids, routes = [], [], []
    async with callback_store.begin() as session:
        for _ in range(2):
            code = "ENTRY_" + uuid7().hex
            line, device, route = await add_bound_device(session, code, "http://127.0.0.1:45871")
            line.business_code = definition.code
            line.position_bindings = {}
            codes.append(code)
            ids.append(line.id)
            routes.append(route.id)
    state = {"definition": definition, "codes": codes, "ids": ids, "calls": [], "mode": "accept"}

    class Acceptance:
        async def accept(self, ctx, payload):
            session = ctx.session
            binding = (
                ctx.binding
                if hasattr(ctx, "binding")
                else await resolve_binding(session, payload.device_code)
            )
            issue = await business_configuration_issue(
                session,
                binding,
                definition.code,
                definition_lookup=lambda code: state["definition"],
            )
            if issue:
                ctx.note(issue)
                return Accepted()
            # Guard the acceptance scope; the receiver retains control of its transaction/savepoint.
            with monkeypatch.context() as guard:

                def forbidden(*args, **kwargs):
                    raise AssertionError("acceptance must not advance results or own a transaction")

                for name in ("begin", "begin_nested", "commit", "rollback", "connection"):
                    guard.setattr(session, name, forbidden)
                row = await session.get(WorkLine, binding.work_line_id)
                trigger = ctx.digest if hasattr(ctx, "digest") else ctx.operation_id
                row.description = "accepted:" + trigger
                row.zone_name = "pending:" + str(ctx.receipt_id)
                await session.flush()
                state["calls"].append((session, binding, trigger))
                if state["mode"] == "transient":
                    raise RuntimeError("temporary business storage failure")
                if state["mode"] == "reject":
                    return Rejected()
                if state["mode"] == "conflict":
                    return Conflict()
                return Accepted()

    runtime = BusinessRuntime(
        definitions={definition.code: definition}, services={definition.code: Acceptance()}
    )
    monkeypatch.setattr(app.state, "business_runtime", runtime, raising=False)

    @ecs.ecs_event("SCAN_COMPLETED", device_role="SCAN1", data=Payload)
    async def scan(ctx, payload):
        assert ctx.runtime is runtime
        await require_runtime(ctx).services[definition.code].accept(ctx, payload)

    @wms.callback_operation("entry.accept@v1", data=Payload)
    async def callback(ctx, payload):
        assert ctx.runtime is runtime
        return await require_runtime(ctx).services[definition.code].accept(ctx, payload)

    try:
        yield state
    finally:
        async with callback_store.begin() as session:
            await session.execute(delete(Device).where(Device.device_code.in_(codes)))
            await session.execute(delete(WorkLine).where(WorkLine.id.in_(ids)))
            await session.execute(delete(DeviceRoute).where(DeviceRoute.id.in_(routes)))


def request(kind, code, *, timestamp=1):
    if kind == "ecs":
        return "/api/v1/callback/event", dict(
            device_code=code,
            event_type="SCAN_COMPLETED",
            timestamp=timestamp,
            data={"device_code": code},
        )
    return "/api/v1/wms/events", dict(
        operation="entry.accept@v1",
        operation_id=str(uuid7()),
        timestamp=timestamp,
        data={"device_code": code},
    )


async def stored(factory, state):
    async with factory() as session:
        receipts = list(await session.scalars(select(CallbackReceipt)))
        lines = list(
            await session.scalars(
                select(WorkLine).where(WorkLine.id.in_(state["ids"])).order_by(WorkLine.id)
            )
        )
        return receipts, lines


@pytest.mark.parametrize("kind", ["ecs", "wms"])
async def test_acceptance_never_starts_result_advancement(
    kind, entry_setup, callback_store, client
):
    state = entry_setup
    for index, code in enumerate(state["codes"]):
        path, body = request(kind, code, timestamp=index + 1)
        response = await client.post(path, json=body)
        assert response.status_code == (200 if kind == "ecs" else 202), response.text
        assert (await client.post(path, json=body)).status_code == 200
    receipts, lines = await stored(callback_store, state)
    assert len(receipts) == len(state["calls"]) == 2
    assert [call[1].work_line_id for call in state["calls"]] == state["ids"]
    assert state["calls"][0][0] is not state["calls"][1][0]
    assert {line.description for line in lines} == {
        "accepted:" + receipt.operation_id for receipt in receipts
    }
    assert {line.zone_name for line in lines} == {
        "pending:" + str(receipt.id) for receipt in receipts
    }


async def test_ecs_business_diagnostic_acks_without_execution(entry_setup, callback_store, client):
    state = entry_setup
    async with callback_store.begin() as session:
        line = await session.get(WorkLine, state["ids"][0])
        line.business_code = None
    path, body = request("ecs", state["codes"][0])
    response = await client.post(path, json=body)
    assert response.status_code == 200 and response.json() == {"code": 200, "message": "ACK"}
    receipts, lines = await stored(callback_store, state)
    assert receipts[0].diagnostic == [{"reason_code": "WORKLINE_BUSINESS_MISMATCH"}]
    assert not state["calls"]
    assert all(line.description is line.zone_name is None for line in lines)


@pytest.mark.parametrize("kind", ["ecs", "wms"])
async def test_transient_failure_rolls_back_receipt_and_business(
    kind, entry_setup, callback_store, client
):
    state = entry_setup
    state["mode"] = "transient"
    path, body = request(kind, state["codes"][0])
    assert (await client.post(path, json=body)).status_code == 503
    receipts, lines = await stored(callback_store, state)
    assert receipts == []
    assert all(line.description is line.zone_name is None for line in lines)
    state["mode"] = "accept"
    assert (await client.post(path, json=body)).status_code == (200 if kind == "ecs" else 202)


@pytest.mark.parametrize("mode,status,persisted", [("reject", 422, False), ("conflict", 409, True)])
async def test_wms_business_keeps_rejected_conflict_contract(
    mode, status, persisted, entry_setup, callback_store, client
):
    state = entry_setup
    state["mode"] = mode
    path, body = request("wms", state["codes"][0])
    response = await client.post(path, json=body)
    assert response.status_code == status, response.text
    receipts, lines = await stored(callback_store, state)
    assert len(receipts) == 1
    assert bool(lines[0].description) is persisted
    assert bool(lines[0].zone_name) is persisted
    assert (await client.post(path, json=body)).status_code == status
    assert len(state["calls"]) == 1
