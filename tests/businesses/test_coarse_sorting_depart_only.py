"""Only-depart scheduling and independent idle release through original public ports."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.businesses._components.rack_dispatch.contracts import (
    Coverage,
    DirectHandoff,
    FactRef,
    JobOrigin,
    PointDemand,
    PointRef,
)
from app.businesses._components.rack_dispatch.service import RackDispatch
from app.businesses.coarse_sorting.models import RackPreparation
from app.core.identifiers import uuid7
from app.integrations.wms_transport import TransportCaller, TransportService
from app.models import WorkLine
from app.reliable import ScopeRef
from tests.helpers import (
    allocated,
    deliver_transport_callback,
    enable_racks,
    reach_allocate,
    reach_move,
)


async def fresh_allocate(flow, *, timestamp=1793206799001):
    assert (
        await flow.post(flow.event(timestamp=timestamp, changes={"thickness_mm": 20}))
    ).status_code == 200
    row = (await flow.rows())[-1]
    assert await flow.service.advance(row["id"]) == "AWAITING_VALIDATION_RESULT"
    await flow.dispatch()
    assert await flow.service.advance(row["id"]) == "AWAITING_MOVE_RESULT"
    row = (await flow.rows())[-1]
    await flow.dispatch()
    assert (await flow.result(row)).status_code == 200
    assert await flow.service.advance(row["id"]) == "AWAITING_ALLOCATION_RESULT"
    await flow.dispatch()
    return row


async def depart_only(flow):
    flow.peer.allocate = {
        "result": "DEPART_REQUIRED",
        "workline_code": flow.workline,
        "current_rack": {"rack_id": "OLD"},
    }
    row = await reach_allocate(flow)
    enable_racks(flow)
    assert await flow.service.advance(row["id"]) == "AWAITING_RACK"
    row = (await flow.rows())[0]
    return row, row["rack_preparation_id"]


async def accept_departure(flow, prep, destination="ZONE"):
    flow.peer.departure = {
        "result": "READY",
        "rack_destination": {"type": destination, "location_code": "DEST"},
    }
    assert await flow.service.advance_preparation(prep) == "AWAITING_DEPARTURE_RESULT"
    await flow.dispatch()
    assert await flow.service.advance_preparation(prep) == "AWAITING_RACK_ACCEPTANCE"
    await flow.dispatch()
    assert await flow.service.advance_preparation(prep) == "DEPART_ACCEPTED"


@pytest.mark.parametrize("stopped", [False, True], ids=["active", "closed-by-scope"])
async def test_wms_presence_waits_for_another_owners_original_entry(rack, stopped):
    _, prep = await depart_only(rack)
    line_id = rack.line_ids[0]
    scope = ScopeRef("smt_manual", "LINE", str(line_id))
    other = RackDispatch(
        scope.owner_domain,
        replace(
            rack.service.capabilities,
            transport=TransportService(rack.outbox, owner_domain=scope.owner_domain),
        ),
    )
    async with rack.factory.begin() as session:
        line = await session.get(WorkLine, line_id)
        point = line.position_bindings["OUTPUT_RACK"]
        line.business_code = scope.owner_domain
        await session.flush()
        job_id = await other.register(
            session,
            JobOrigin(
                "ROLLER_DRAIN",
                "other-entry",
                line_id,
                "OTHER",
                None,
                line.line_code,
                point,
                "90",
                TransportCaller(line.line_code, point),
                str(uuid7()),
                {"positions": {"OUTPUT_RACK": point}},
            ),
            scope=scope,
        )
        assert await other.prepare(
            session, job_id=job_id, kind="ENTER", source_ref="other-entry", face="90"
        )
        line.business_code = "coarse_sorting"

    rack.peer.transport_status, rack.peer.transport_code = 409, "CONFLICT"
    rack.peer.departure = {
        "result": "READY",
        "rack_destination": {"type": "ZONE", "location_code": "DEST"},
    }
    before = len(rack.peer.requests)
    if not stopped:
        assert await rack.service.advance_preparation(prep) == "WMS_PRESENCE_UNPROVED"
    assert await rack.dispatch() == 1
    async with rack.factory.begin() as session:
        if stopped:
            await rack.outbox.stop_scope(session, scope, reason="LINE_STOPPED")
            await other.close_scope(session, scope, reason="LINE_STOPPED")
        original = await other.progress(session, job_id=job_id)
        assert original.state == ("CLOSED_BY_SCOPE" if stopped else "ACTIVE")
        assert original.pending and original.actual_position is None
        assert original.position_action_id is None
        assert (
            original.actions[0].transport_task_id
            == rack.peer.transports[0]["data"]["transport_task_id"]
        )

    assert await rack.service.advance_preparation(prep) == "WMS_PRESENCE_UNPROVED"
    assert await rack.dispatch() == 0
    assert len(rack.peer.requests) == before
    assert len(rack.peer.transports) == 1
    async with rack.factory() as session:
        (adoption,) = await rack.service.rack_dispatch.jobs(
            session, work_line_id=line_id, source_kinds=("COARSE_WMS_PRESENT",)
        )
        assert not adoption.actions

    ref = PointRef(scope.owner_domain, line_id, job_id, "OTHER", point)
    coverage = Coverage(
        "line",
        (scope.owner_domain, str(line_id), job_id, "OTHER", point),
        FactRef("scope", (scope.scope_id,), ("1",)),
        (),
    )
    async with rack.factory.begin() as session:
        released = await other.advance(
            session,
            job_id=job_id,
            demand=PointDemand(ref, "NONE", (coverage,)),
            handoff=DirectHandoff(ref, "CLEAR", (coverage,)),
            now_ms=1,
        )
        assert not released.pending and released.actions == original.actions
    rack.peer.transport_status, rack.peer.transport_code = 202, "RECEIVED"
    await accept_departure(rack, prep)
    assert [item["data"]["rack_id"] for item in rack.peer.transports] == ["OTHER", "OLD"]


@pytest.mark.parametrize("destination", ["ZONE", "RACK_POSITION"])
@pytest.mark.parametrize("status,code", [(202, "RECEIVED"), (200, "DUPLICATE")])
async def test_only_depart_acceptance_ends_chain_and_reallocates_each_original_binding(
    rack, destination, status, code
):
    flow = rack
    row, prep = await depart_only(flow)
    flow.peer.transport_status, flow.peer.transport_code = status, code
    second = await reach_move(flow, timestamp=1793206799001)
    await flow.dispatch()
    assert (await flow.result(second)).status_code == 200
    assert await flow.service.advance(second["id"]) == "AWAITING_RACK"
    await accept_departure(flow, prep, destination)
    assert len(flow.peer.transports) == 1
    wire = flow.peer.transports[0]["data"]
    assert wire["rack_id"] == "OLD" and wire["rcs_template_id"] == "F01"
    async with flow.factory() as session:
        saved = (await session.execute(select(RackPreparation))).scalar_one()
        assert saved.mode == "DEPART_ONLY" and saved.incoming_rack_id is None
        actions = list(
            (
                await session.execute(
                    select(flow.actions).where(flow.actions.c.preparation_id == prep)
                )
            ).mappings()
        )
    assert len(actions) == 2
    by_kind = {action["kind"]: action for action in actions}
    assert by_kind.keys() == {"DEPARTURE_DECIDE", "MOVE_RACK_OUT"}
    assert by_kind["MOVE_RACK_OUT"]["predecessor_action_id"] == by_kind["DEPARTURE_DECIDE"]["id"]
    for item in (row, second):
        assert await flow.service.advance(item["id"]) == "AWAITING_ALLOCATION_RESULT"
        action = [a for a in await flow.action_rows(item["id"]) if a["kind"] == "ALLOCATE_STORAGE"][
            -1
        ]
        assert action["request_data"] == {
            "workline_code": flow.workline,
            "binding_id": "BIND-ORIGINAL",
        }
        assert action["input_preparation_id"] == prep
    assert await flow.dispatch() == 2
    assert len(flow.peer.transports) == 1


async def test_depart_missing_acceptance_keeps_original_action(rack):
    row, prep = await depart_only(rack)
    rack.peer.transport_id = "WRONG"
    rack.peer.departure = {
        "result": "READY",
        "rack_destination": {"type": "ZONE", "location_code": "STORE"},
    }
    await rack.service.advance_preparation(prep)
    await rack.dispatch()
    await rack.service.advance_preparation(prep)
    await rack.dispatch()
    for _ in range(2):
        assert await rack.service.advance_preparation(prep) == "AWAITING_RACK_ACCEPTANCE"
    assert len(rack.peer.transports) == 1
    assert await rack.service.advance(row["id"]) == "AWAITING_RACK"


async def test_depart_acceptance_survives_original_late_failure_and_wait_requery(rack):
    row, prep = await depart_only(rack)
    await accept_departure(rack, prep)
    wire = rack.peer.transports[0]["data"]
    await deliver_transport_callback(
        rack.factory,
        "transport.task.resulted@v1",
        {
            "transport_task_id": wire["transport_task_id"],
            "kind": "RACK_MOVE",
            "outcome_revision": 1,
            "rack_id": "OLD",
            "status": "FAILED",
            "position_unknown": True,
            "failure_code": "POSITION_UNKNOWN",
        },
    )
    async with rack.factory() as session:
        action = (
            (
                await session.execute(
                    select(rack.actions).where(
                        rack.actions.c.preparation_id == prep,
                        rack.actions.c.kind == "MOVE_RACK_OUT",
                    )
                )
            )
            .mappings()
            .one()
        )
        result = await rack.service.rack_dispatch.final_result(
            session, job_id=action["rack_job_id"], action_id=action["rack_action_id"]
        )
        assert result.outcome["status"] == "FAILED"
    assert await rack.service.advance_preparation(prep) == "DEPART_ACCEPTED"
    assert await rack.service.advance(row["id"]) == "AWAITING_ALLOCATION_RESULT"
    rack.peer.allocate = {"result": "WAIT", "workline_code": rack.workline}
    await rack.dispatch()
    assert await rack.service.advance(row["id"]) == "NO_AVAILABLE_RACK"
    rack.now += timedelta(seconds=6)
    assert await rack.service.advance(row["id"]) == "AWAITING_ALLOCATION_RESULT"
    assert len(rack.peer.transports) == 1


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("kind", ["ALLOCATED", "DEPART_REQUIRED", "REPLACE_REQUIRED"])
async def test_departed_rack_cannot_be_allocated_or_departed_again(rack, fresh, kind):
    row, prep = await depart_only(rack)
    await accept_departure(rack, prep)
    decision = allocated(rack)
    decision["rack_id"] = "OLD"
    if kind != "ALLOCATED":
        decision = {
            "result": kind,
            "workline_code": rack.workline,
            "current_rack": {"rack_id": "OLD"},
        }
        if kind == "REPLACE_REQUIRED":
            decision["incoming_rack"] = {"rack_id": "NEW", "target_face": "90"}
    rack.peer.allocate = decision
    if fresh:
        row = await fresh_allocate(rack)
    else:
        await rack.service.advance(row["id"])
        await rack.dispatch()
    assert await rack.service.advance(row["id"]) == "RACK_STATE_CONFLICT"
    assert len(rack.peer.transports) == 1
    assert not any(a["kind"] == "PUT_TO_BIN" for a in await rack.action_rows(row["id"]))


async def test_simultaneous_only_depart_decisions_share_one_preparation(rack):
    rack.peer.allocate = {
        "result": "DEPART_REQUIRED",
        "workline_code": rack.workline,
        "current_rack": {"rack_id": "OLD"},
    }
    rows = [await reach_allocate(rack, timestamp=1793206799000 + i) for i in range(2)]
    enable_racks(rack)
    if rack.factory.kw["bind"].dialect.name == "postgresql":
        await asyncio.gather(*(rack.service.advance(row["id"]) for row in rows))
    else:
        for row in rows:
            await rack.service.advance(row["id"])
    rows = await rack.rows()
    assert {row["state"] for row in rows} == {"AWAITING_RACK"}
    assert len({row["rack_preparation_id"] for row in rows}) == 1


async def placed_with_original_rack(flow, *, finish=True):
    flow.peer.allocate = {
        "result": "REPLENISH_REQUIRED",
        "workline_code": flow.workline,
        "incoming_rack": {"rack_id": "NEW", "target_face": "90"},
    }
    row = await reach_allocate(flow)
    enable_racks(flow)
    await flow.service.advance(row["id"])
    prep = (await flow.rows())[0]["rack_preparation_id"]
    await flow.service.advance_preparation(prep)
    await flow.dispatch()
    wire = flow.peer.transports[-1]["data"]
    await deliver_transport_callback(
        flow.factory,
        "transport.task.resulted@v1",
        {
            "transport_task_id": wire["transport_task_id"],
            "kind": "RACK_MOVE",
            "outcome_revision": 1,
            "rack_id": "NEW",
            "status": "SUCCEEDED",
            "final_position": wire["target"],
            "arrival_face": "90",
        },
    )
    assert await flow.service.advance_preparation(prep) == "READY"
    flow.peer.allocate = allocated(flow, rack_id="NEW")
    await flow.service.advance(row["id"])
    await flow.dispatch()
    assert await flow.service.advance(row["id"]) == "AWAITING_PUT_RESULT"
    await flow.dispatch()
    if finish:
        put = json.loads(flow.peer.commands[-1])
        assert (
            await flow.result(
                row,
                command_code=put["command_code"],
                device_code=flow.output,
                data={"actual_qty": 1, "location": "STATION_OUTPUT1"},
            )
        ).status_code == 200
        assert await flow.service.advance(row["id"]) == "AWAITING_REPORT_RESULT"
    async with flow.factory() as session:
        public = (
            await flow.service.rack_dispatch.jobs(
                session, work_line_id=flow.line_ids[0], source_kinds=("COARSE_INCOMING",)
            )
        )[0]
    return row, prep, public.id


async def idle_depart(flow, public_id):
    flow.peer.departure = {
        "result": "READY",
        "rack_destination": {"type": "ZONE", "location_code": "STORE"},
    }
    await flow.service.advance_idle_rack(public_id)
    await flow.dispatch()
    await flow.service.advance_idle_rack(public_id)
    await flow.dispatch()
    await flow.service.advance_idle_rack(public_id)
    async with flow.factory() as session:
        return await flow.service.rack_dispatch.progress(session, job_id=public_id)


async def test_idle_rack_departs_without_preparation_while_placed_report_recovers(rack):
    row, prep, public_id = await placed_with_original_rack(rack)
    rack.peer.report_code = None
    public = await idle_depart(rack, public_id)
    assert public.closure_reason == "DEPARTURE_ACK" and not public.pending
    assert [a.kind for a in public.actions] == ["ENTER", "DEPARTURE_DECIDE", "DEPART"]
    assert (await rack.rows())[0]["state"] == "AWAITING_REPORT"
    async with rack.factory() as session:
        assert len(list(await session.scalars(select(RackPreparation)))) == 1
    assert len(rack.peer.transports) == 2
    assert await rack.service.advance_preparation(prep) == "READY"


@pytest.mark.parametrize("demand", ["put", "scan", "allocation", "missing_pointer"])
async def test_idle_release_retains_uncertain_demand_and_original_direct_responsibility(
    rack, demand
):
    row, _, public_id = await placed_with_original_rack(rack, finish=demand != "put")
    if demand == "scan":
        assert (await rack.post(rack.event(timestamp=1793206799001))).status_code == 200
    elif demand == "allocation":
        rack.peer.allocate = {"result": "WAIT", "workline_code": rack.workline}
        await fresh_allocate(rack)
    elif demand == "missing_pointer":
        async with rack.factory.begin() as session:
            await session.execute(
                rack.table.update()
                .where(rack.table.c.id == row["id"])
                .values(allocation_action_id=rack.table.c.current_action_id)
            )
    public = await idle_depart(rack, public_id)
    assert public.pending
    assert not any(a.kind == "DEPARTURE_DECIDE" for a in public.actions)
    assert len(rack.peer.transports) == 1


async def test_idle_acceptance_fences_new_reel_old_rack_allocation(rack):
    _, _, public_id = await placed_with_original_rack(rack)
    public = await idle_depart(rack, public_id)
    assert not public.pending
    rack.peer.allocate = allocated(rack, rack_id="NEW")
    row = await fresh_allocate(rack)
    assert await rack.service.advance(row["id"]) == "RACK_STATE_CONFLICT"
    assert len(rack.peer.transports) == 2


async def test_pending_sweep_independently_advances_idle_rack(rack):
    await placed_with_original_rack(rack)
    rack.peer.report_code = None
    rack.peer.departure = {
        "result": "READY",
        "rack_destination": {"type": "ZONE", "location_code": "STORE"},
    }
    for _ in range(3):
        await rack.service.scan_pending(limit=1)
        await rack.dispatch()
    assert len(rack.peer.transports) == 2


@pytest.mark.parametrize("change", ["point", "output_version", "disabled"])
async def test_idle_departure_requires_current_original_point_authority(rack, change):
    from app.models import Device, WorkLine

    _, _, public_id = await placed_with_original_rack(rack)
    async with rack.factory.begin() as session:
        line = await session.get(WorkLine, rack.line_ids[0])
        if change == "point":
            line.position_bindings = {**line.position_bindings, "OUTPUT_RACK": "OTHER"}
        elif change == "disabled":
            line.is_enabled = False
        else:
            output = await session.scalar(select(Device).where(Device.device_code == rack.output))
            output.version += 1
    public = await idle_depart(rack, public_id)
    assert public.pending
    assert not any(a.kind == "DEPARTURE_DECIDE" for a in public.actions)


async def test_accepted_departure_can_replenish_same_rack_only_after_new_original_arrival(rack):
    row, prep = await depart_only(rack)
    await accept_departure(rack, prep)
    rack.peer.allocate = {
        "result": "REPLENISH_REQUIRED",
        "workline_code": rack.workline,
        "incoming_rack": {"rack_id": "OLD", "target_face": "180"},
    }
    await rack.service.advance(row["id"])
    await rack.dispatch()
    assert await rack.service.advance(row["id"]) == "AWAITING_RACK"
    new_prep = (await rack.rows())[0]["rack_preparation_id"]
    assert new_prep != prep
    assert await rack.service.advance_preparation(new_prep) == "AWAITING_RACK_RESULT"
    await rack.dispatch()
    assert await rack.service.advance_preparation(new_prep) == "AWAITING_RACK_RESULT"
    assert await rack.service.advance(row["id"]) == "AWAITING_RACK"
    wire = rack.peer.transports[-1]["data"]
    await deliver_transport_callback(
        rack.factory,
        "transport.task.resulted@v1",
        {
            "transport_task_id": wire["transport_task_id"],
            "kind": "RACK_MOVE",
            "outcome_revision": 1,
            "rack_id": "OLD",
            "status": "SUCCEEDED",
            "final_position": wire["target"],
            "arrival_face": "180",
        },
    )
    assert await rack.service.advance_preparation(new_prep) == "READY"
    rack.peer.allocate = allocated(rack, rack_id="OLD")
    await rack.service.advance(row["id"])
    await rack.dispatch()
    assert await rack.service.advance(row["id"]) == "AWAITING_PUT_RESULT"
    assert len(rack.peer.transports) == 2


@pytest.mark.parametrize("exhaust", [False, True])
async def test_idle_serialization_conflict_retries_entire_transaction(rack, monkeypatch, exhaust):
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.businesses._components.rack_dispatch.models import RackJob

    if rack.factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL serialization contract")
    _, _, public_id = await placed_with_original_rack(rack)
    execute, sessions = AsyncSession.execute, []

    async def collide(session, statement, *args, **kwargs):
        result = await execute(session, statement, *args, **kwargs)
        if (
            getattr(statement, "is_select", False)
            and getattr(statement, "_for_update_arg", None) is None
            and RackJob.__table__ in statement.get_final_froms()
            and (await session.connection())
            .sync_connection.get_execution_options()
            .get("isolation_level")
            == "SERIALIZABLE"
            and session not in sessions
        ):
            sessions.append(session)
            if len(sessions) == 1 or exhaust:
                async with rack.factory.begin() as writer:
                    await writer.execute(
                        RackJob.__table__.update()
                        .where(RackJob.id == public_id)
                        .values(review_reason=f"WRITE_{len(sessions)}")
                    )
        return result

    monkeypatch.setattr(AsyncSession, "execute", collide)
    if exhaust:
        with pytest.raises(DBAPIError) as error:
            await rack.service.advance_idle_rack(public_id)
        assert error.value.orig.sqlstate == "40001"
    else:
        await rack.service.advance_idle_rack(public_id)
    assert len(sessions) == (3 if exhaust else 2)
    assert len({id(session) for session in sessions}) == len(sessions)
    assert all(not session.in_transaction() for session in sessions)
    async with rack.factory() as session:
        public = await rack.service.rack_dispatch.progress(session, job_id=public_id)
        assert sum(a.kind == "DEPARTURE_DECIDE" for a in public.actions) == (0 if exhaust else 1)


async def test_idle_accepted_original_allows_same_rack_after_distinct_reentry(rack):
    _, old_prep, original = await placed_with_original_rack(rack)
    assert not (await idle_depart(rack, original)).pending
    rack.peer.allocate = {
        "result": "REPLENISH_REQUIRED",
        "workline_code": rack.workline,
        "incoming_rack": {"rack_id": "NEW", "target_face": "180"},
    }
    row = await fresh_allocate(rack)
    assert await rack.service.advance(row["id"]) == "AWAITING_RACK"
    prep = (await rack.rows())[-1]["rack_preparation_id"]
    assert prep != old_prep
    await rack.service.advance_preparation(prep)
    await rack.dispatch()
    assert await rack.service.advance(row["id"]) == "AWAITING_RACK"
    wire = rack.peer.transports[-1]["data"]
    await deliver_transport_callback(
        rack.factory,
        "transport.task.resulted@v1",
        {
            "transport_task_id": wire["transport_task_id"],
            "kind": "RACK_MOVE",
            "outcome_revision": 1,
            "rack_id": "NEW",
            "status": "SUCCEEDED",
            "final_position": wire["target"],
            "arrival_face": "180",
        },
    )
    assert await rack.service.advance_preparation(prep) == "READY"
    rack.peer.allocate = allocated(rack, rack_id="NEW")
    await rack.service.advance(row["id"])
    await rack.dispatch()
    assert await rack.service.advance(row["id"]) == "AWAITING_PUT_RESULT"
    assert len(rack.peer.transports) == 3


async def test_late_only_depart_decision_is_retained_without_restarting_old_rack_chain(rack):
    rack.peer.allocate = {
        "result": "DEPART_REQUIRED",
        "workline_code": rack.workline,
        "current_rack": {"rack_id": "OLD"},
    }
    first = await reach_allocate(rack)
    second = await fresh_allocate(rack)
    enable_racks(rack)
    assert await rack.service.advance(second["id"]) == "AWAITING_RACK"
    prep = (await rack.rows())[-1]["rack_preparation_id"]
    await accept_departure(rack, prep)
    assert await rack.service.advance(first["id"]) == "AWAITING_RACK"
    action = [a for a in await rack.action_rows(first["id"]) if a["kind"] == "ALLOCATE_STORAGE"][0]
    assert action["evidence_history"][-1]["data"]["result"] == "DEPART_REQUIRED"
    assert action["result_preparation_id"] == prep
    assert await rack.service.advance(first["id"]) == "AWAITING_ALLOCATION_RESULT"
    assert len(rack.peer.transports) == 1
