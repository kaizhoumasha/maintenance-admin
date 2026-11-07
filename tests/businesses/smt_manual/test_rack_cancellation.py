import pytest
from sqlalchemy import update


async def cancel_direct(flow, *, faces=("A",), revision=1):
    return await flow.deliver(
        flow.message(
            "cancel",
            dict(
                task_id="T1",
                cancel_scope="PLAN_MEMBERS",
                direct_pick_sources=[
                    dict(plan_revision=revision, rack_id="DIRECT", rack_face=face) for face in faces
                ],
            ),
        )
    )


@pytest.mark.parametrize("line", [1, 2])
@pytest.mark.parametrize("role", ["RETURN", "TRANSFER"])
@pytest.mark.parametrize("same_source", [True, False])
async def test_departure_callback_alone_does_not_close_original_job(
    manual_racks, line, role, same_source
):
    flow = manual_racks
    await flow.ready(line=line)
    await flow.plan(target=dict(rack_id="TRANSFER", rack_face="B") if role == "TRANSFER" else None)
    await flow.reconcile()
    work = next(r for r in await flow.rows("rack") if r["role"] == role)
    entry = next(a for a in await flow.rows("rack_action") if a["rack_work_id"] == work["id"])
    await flow.rack_outcome(entry, face="B" if role == "TRANSFER" else "A")
    await flow.deliver(flow.notification())
    async with flow.factory.begin() as session:
        await session.execute(update(flow.tables["task"]).values(status="COMPLETED"))
    await flow.rack_advance(work["id"])
    await flow.rack_response(
        "rack_departure",
        dict(result="READY", rack_destination=dict(type="ZONE", location_code="STORAGE")),
    )
    await flow.rack_advance(work["id"])
    departure = next(a for a in await flow.rows("rack_action") if a["kind"] == "DEPART")
    await flow.rack_outcome(departure, location=work["location_code"] if same_source else "STORE")
    state = await flow.rack_advance(work["id"])
    assert state == "WAITING_FOR_DEPARTURE_RESULT"
    await flow.acknowledge(departure)
    assert await flow.rack_advance(work["id"]) == "COMPLETED"
    assert len([a for a in await flow.rows("rack_action") if a["rack_work_id"] == work["id"]]) == 2


@pytest.mark.parametrize("outside", [True, False])
@pytest.mark.parametrize("cancelled", [True, False])
async def test_cancelled_entry_keeps_unfulfilled_work_and_only_releases_outside_reservation(
    manual_racks, manual_completion_tools, outside, cancelled
):
    flow = manual_racks
    await flow.ready()
    await flow.plan()
    await flow.reconcile()
    original = (await flow.rows("rack_action"))[0]
    if cancelled:
        assert (await cancel_direct(flow)).status_code == 202
    assert (
        await flow.rack_outcome(status="CANCELLED", location="STORE" if outside else "RETURN")
    ).status_code == 202
    state = await flow.rack_advance()
    work = (await flow.rows("rack"))[0]
    assert (work["state"] == "NOT_ARRIVED") is outside
    assert state == (
        "NOT_ARRIVED" if outside else "WAITING_FOR_DEPARTURE" if cancelled else "RECOVERY_REQUIRED"
    )
    assert await flow.rows("rack_report") == []
    await flow.confirm()
    assert bool(await flow.confirmations()) is cancelled
    if cancelled and not outside:
        assert (await flow.rows("rack_departure"))[0]["request_snapshot"]["current_face"] is None
    for _ in range(2):
        await flow.reconcile()
        await flow.rack_advance()
    assert len(await flow.rows("rack_action")) == 1
    assert (await flow.rows("rack_action"))[0]["transport_task_id"] == original["transport_task_id"]
    # A distinct later member may own a successor after the old entry closed.
    await flow.plan(revision=2)
    await flow.reconcile()
    assert len(await flow.rows("rack_action")) == (2 if outside else 1)
    assert (
        next(m for m in await flow.rows("member") if m["plan_revision"] == 2)["status"] == "PENDING"
    )


@pytest.mark.parametrize("location", ["TRANSFER", "FIVE", "IN", "OUT"])
async def test_cancelled_entry_at_another_managed_point_keeps_resource_protection(
    manual_racks, manual_completion_tools, location
):
    flow = manual_racks
    await flow.ready()
    await flow.plan()
    await flow.reconcile()
    await cancel_direct(flow)
    await flow.rack_outcome(status="CANCELLED", location=location)
    assert await flow.rack_advance() == "POSITION_CONFLICT"
    assert (await flow.rows("rack"))[0]["state"] == "ACTIVE"
    await flow.confirm()
    assert await flow.confirmations() == []
    assert await flow.rows("rack_departure") == []


@pytest.mark.parametrize("location", ["TRANSFER", "STORE"])
async def test_cancelled_transfer_entry_never_fulfills_target_obligation(
    manual_racks, manual_completion_tools, location
):
    flow = manual_racks
    await flow.ready()
    await flow.plan(faces=(), target=dict(rack_id="TRANSFER", rack_face="B"))
    await flow.reconcile()
    await flow.rack_outcome(status="CANCELLED", location=location)
    assert await flow.rack_advance() == (
        "WAITING_FOR_DEPARTURE" if location == "TRANSFER" else "NOT_ARRIVED"
    )
    await flow.confirm()
    assert await flow.confirmations() == []
    assert await flow.rows("rack_report") == []
    assert bool(await flow.rows("rack_departure")) is (location == "TRANSFER")


async def test_started_direct_work_survives_member_cancellation(manual_racks):
    flow = manual_racks
    await flow.ready()
    await flow.plan(faces=("A", "B"))
    await flow.reconcile()
    async with flow.factory.begin() as session:
        members = flow.tables["member"]
        await session.execute(
            members.update().where(members.c.rack_face == "A").values(status="STARTED")
        )
    await cancel_direct(flow, faces=("A", "B"))
    assert {m["rack_face"]: m["status"] for m in await flow.rows("member")} == {
        "A": "STARTED",
        "B": "CANCELLED",
    }
    await flow.rack_outcome(status="CANCELLED")
    assert await flow.rack_advance() == "RECOVERY_REQUIRED"
    assert await flow.rows("rack_departure") == []


async def test_newer_observation_prevents_cancelled_entry_release_without_applying_it(manual_racks):
    flow = manual_racks
    await flow.ready()
    await flow.plan()
    await flow.reconcile()
    await cancel_direct(flow)
    assert (await flow.rack_outcome(revision=2, location="TRANSFER", face="A")).status_code == 202
    assert (await flow.rack_outcome(status="CANCELLED", location="STORE")).status_code == 202
    assert await flow.rack_advance() == "POSITION_CONFLICT"
    work = (await flow.rows("rack"))[0]
    assert work["state"] == "ACTIVE" and work["pending"]
    action = (await flow.rows("rack_action"))[0]
    assert action["outcome_revision"] == 1 and action["observation_revision"] == 2
    assert await flow.rows("rack_report") == []
    assert await flow.rows("rack_departure") == []


async def test_cancelled_rack_does_not_block_a_different_authorized_source(manual_racks):
    flow = manual_racks
    await flow.ready()
    await flow.plan()
    await flow.reconcile()
    await flow.rack_outcome(status="CANCELLED", location="STORE")
    await flow.rack_advance()
    await flow.deliver(
        flow.message(
            "plan_delta",
            dict(
                task_id="T1",
                plan_revision=2,
                added_direct_picks=[
                    dict(
                        source_locator=dict(
                            type="RACK_SLOT", rack_id="DIFFERENT", rack_face="A", slot_id="S2"
                        )
                    )
                ],
            ),
        )
    )
    await flow.reconcile()
    assert {r["rack_id"] for r in await flow.rows("rack")} == {"DIRECT", "DIFFERENT"}
    assert len(await flow.rows("rack_action")) == 2


async def test_started_member_prevents_departure_until_original_completion(manual_racks):
    flow = manual_racks
    await flow.ready()
    await flow.plan(faces=("A", "B"))
    await flow.reconcile()
    async with flow.factory.begin() as session:
        members = flow.tables["member"]
        await session.execute(
            members.update().where(members.c.rack_face == "A").values(status="STARTED")
        )
    await cancel_direct(flow, faces=("A", "B"))
    await flow.rack_outcome(face="A")
    assert await flow.rack_advance() == "WORKING"
    assert await flow.rows("rack_departure") == []
    await flow.deliver(flow.notification(face="A"))
    assert (
        next(m for m in await flow.rows("member") if m["rack_face"] == "A")["status"] == "COMPLETED"
    )
    assert await flow.rack_advance() == "WAITING_FOR_DEPARTURE"


@pytest.mark.parametrize("cancel_before_arrival", [True, False])
async def test_all_member_cancellation_preserves_original_success_and_report_obligation(
    manual_racks, manual_completion_tools, cancel_before_arrival
):
    flow = manual_racks
    await flow.ready()
    await flow.plan(faces=("A", "B"))
    await flow.reconcile()
    entry = (await flow.rows("rack_action"))[0]
    if cancel_before_arrival:
        assert (await cancel_direct(flow, faces=("A", "B"))).status_code == 202
        for _ in range(2):
            await flow.reconcile()
            assert await flow.rack_advance() == "WAITING_FOR_ENTRY"
        assert await flow.rows("rack_action") == [entry]
        assert await flow.rows("rack_departure") == []
    assert (await flow.rack_outcome(entry, face="A")).status_code == 202
    if not cancel_before_arrival:
        assert await flow.rack_advance() == "WORKING"
        assert (await cancel_direct(flow, faces=("A", "B"))).status_code == 202
    assert await flow.rack_advance() == "WAITING_FOR_DEPARTURE"
    assert {m["status"] for m in await flow.rows("member")} == {"CANCELLED"}
    report = (await flow.rows("rack_report"))[0]
    assert report["state"] == "REQUESTED" and report["action_id"] == entry["id"]
    await flow.confirm()
    assert await flow.confirmations() == []
    await flow.rack_response("rack_departure", dict(result="WAIT", retry_after_ms=100))
    assert await flow.rack_advance(now_ms=1000) == "WAITING_FOR_DEPARTURE"
    assert await flow.rack_advance(now_ms=1099) == "WAITING_FOR_DEPARTURE"
    assert len(await flow.rows("rack_departure")) == 1
    assert await flow.rack_advance(now_ms=1100) == "WAITING_FOR_DEPARTURE"
    await flow.rack_response(
        "rack_departure",
        dict(result="READY", rack_destination=dict(type="ZONE", location_code="STORE")),
    )
    for _ in range(2):
        await flow.reconcile()
        assert await flow.rack_advance(now_ms=1101) == "WAITING_FOR_DEPARTURE_RESULT"
    actions = await flow.rows("rack_action")
    assert [(a["kind"], a["transport_task_id"]) for a in actions if a["kind"] == "ENTER"] == [
        ("ENTER", entry["transport_task_id"])
    ]
    assert len([a for a in actions if a["kind"] == "DEPART"]) == 1
    assert await flow.rows("rack_report") == [report]
    await flow.rack_response("rack_report", {}, code="RECORDED")
    assert await flow.consume_report() == "CONSUMED"
    await flow.confirm()
    assert len(await flow.confirmations()) == 1


@pytest.mark.parametrize("cancelled_face", ["A", "B"])
async def test_partial_member_cancellation_continues_remaining_original_face_work(
    manual_racks, cancelled_face
):
    flow = manual_racks
    await flow.ready()
    await flow.plan(faces=("A", "B"))
    await flow.reconcile()
    entry = (await flow.rows("rack_action"))[0]
    assert (await cancel_direct(flow, faces=(cancelled_face,))).status_code == 202
    assert await flow.rack_advance() == "WAITING_FOR_ENTRY"
    assert (await flow.rack_outcome(entry, face="A")).status_code == 202
    if cancelled_face == "A":
        assert await flow.rack_advance() == "WAITING_FOR_ROTATION"
        assert (await flow.rack_outcome(face="B")).status_code == 202
    assert await flow.rack_advance() == "WORKING"
    remaining_face = "B" if cancelled_face == "A" else "A"
    assert {m["rack_face"]: m["status"] for m in await flow.rows("member")} == {
        cancelled_face: "CANCELLED",
        remaining_face: "PENDING",
    }
    for _ in range(2):
        await flow.reconcile()
        assert await flow.rack_advance() == "WORKING"
    assert await flow.rows("rack_departure") == []
    assert [
        a["transport_task_id"] for a in await flow.rows("rack_action") if a["kind"] == "ENTER"
    ] == [entry["transport_task_id"]]
    assert (await flow.deliver(flow.notification(face=remaining_face))).status_code == 202
    assert await flow.rack_advance() == "WAITING_FOR_DEPARTURE"


@pytest.mark.parametrize(
    "status", ["CANCELLED_INSIDE", "CANCELLED_OUTSIDE", "SUCCEEDED", "PENDING"]
)
@pytest.mark.parametrize("consume", [True, False])
async def test_mixed_plan_target_cancellation_vetoes_completed_real_supply(
    manual_production_supply, status, consume
):
    flow = manual_production_supply
    await flow.ready()
    await flow.bin_plan(target_rack=dict(rack_id="TRANSFER", rack_face="B"))
    await flow.reconcile()
    entry = (await flow.rows("rack_action"))[0]
    demand_id = await flow.start_supply(feed=True)
    await flow.withdraw_supply()
    await flow.feed_success(demand_id)
    async with flow.factory() as session:
        progress = await flow.roller.get_supply_progress(
            session, work_line_id=flow.lines[0], task_id="T1"
        )
        assert progress[0].feed_complete and not progress[0].pending
    await flow.scan()
    arrival = (await flow.rows("manual_bin"))[0]["arrival_id"]
    await flow.decision(dict(result="NO_WORK"))
    await flow.advance("bin", arrival)
    cancelled = status.startswith("CANCELLED")
    if status != "PENDING":
        assert (
            await flow.rack_outcome(
                entry,
                status="CANCELLED" if cancelled else "SUCCEEDED",
                location="STORE" if status == "CANCELLED_OUTSIDE" else "TRANSFER",
                face=None if cancelled else "B",
            )
        ).status_code == 202
    if consume:
        await flow.rack_advance()
    await flow.confirm()
    assert bool(await flow.confirmations()) is not cancelled
    assert len(await flow.rows("rack_action")) == 1
    assert (await flow.rows("rack_action"))[0]["transport_task_id"] == entry["transport_task_id"]


async def test_repeated_outside_cancel_wakeups_preserve_original_action(manual_racks, monkeypatch):

    flow = manual_racks
    await flow.ready()
    await flow.plan()
    await flow.reconcile()
    await flow.rack_outcome(status="CANCELLED", location="STORE")
    assert await flow.rack_advance() == "NOT_ARRIVED"

    original = await flow.rows("rack_action")
    for _ in range(2):
        assert await flow.rack_advance() == "NOT_ARRIVED"
    assert await flow.rows("rack_action") == original
