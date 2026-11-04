import pytest
from sqlalchemy import func, select

from app.businesses._components.rack_dispatch.contracts import JobOrigin
from app.core.identifiers import uuid7
from app.models import ReliableMessage
from app.reliable import ScopeRef
from app.reliable.store import MessageStore


@pytest.mark.parametrize("stage", ["decision", "queued", "gated", "accepted"])
async def test_task_close_stops_only_its_execution_and_keeps_independent_return(
    roller_current, stage
):
    flow = roller_current
    face_id = await flow.ready_supply_current(count=5)
    feed = (await flow.current_rows("face_work"))[0]
    await flow.current_bins(2)
    await flow.complete_transport(feed["current_transport_task_id"])
    await flow.advance_current("FACE", face_id)
    face = (await flow.current_rows("face_work"))[0]
    if stage != "decision":
        await flow.decide_current(
            face["return_message_id"],
            dict(
                result="READY",
                moves=[
                    dict(
                        sequence_no=i,
                        bin_code=m["bin_code"],
                        target=dict(
                            type="RACK_BIN_SLOT", rack_id="R1", rack_face="A", slot_id=f"R{i}"
                        ),
                    )
                    for i, m in enumerate(face["return_members"], 1)
                ],
            ),
        )
        await flow.advance_current("FACE", face_id)
    face = (await flow.current_rows("face_work"))[0]
    original_rows = await flow.current_rows("bin_return")
    async with flow.factory() as session:
        before_count = await session.scalar(select(func.count()).select_from(ReliableMessage))
        view = (
            await flow.roller.capabilities.transport.get_result(
                session,
                transport_task_id=face["current_transport_task_id"],
                client_request_id=face["current_client_request_id"],
                caller=(
                    await flow.roller.rack_dispatch.progress(session, job_id=face["rack_job_id"])
                ).origin.caller,
            )
            if stage != "decision"
            else None
        )
    if stage == "gated":
        store = MessageStore(flow.factory, lifecycles=flow.ecs.outbox.lifecycles)
        claims = await store.claim(100, flow.ecs.outbox.targets)
        claim = next(c for c in claims if c.message_id == view.submission_message_id)
        assert await store.before_send(claim, flow.ecs.outbox.targets[claim.message.target])
    elif stage == "accepted":
        import json

        async with flow.factory.begin() as session:
            message = await session.get(ReliableMessage, view.submission_message_id)
            message.status, message.last_http_status, message.last_verdict = (
                "DONE",
                202,
                "DELIVERED",
            )
            message.response_raw = json.dumps(
                dict(
                    operation_id=view.operation_id,
                    code="RECEIVED",
                    timestamp=2,
                    data=dict(transport_task_id=view.transport_task_id),
                )
            ).encode()
    async with flow.factory.begin() as session:
        scope = ScopeRef("first", "TASK", "task-original")
        await flow.roller.close_scope(session, scope, reason="TASK_COMPLETED")
    after = (await flow.current_rows("face_work"))[0]
    assert after["closed_at"] and after["close_reason"] == "TASK_COMPLETED"
    assert after["step_id"] is None and after["return_members"] is None
    rows = await flow.current_rows("bin_return")
    assert {r["scope_id"] for r in rows} == {r["scope_id"] for r in original_rows}
    assert all(r["selected_face_work_id"] is None and r["selected_step_id"] is None for r in rows)
    assert {r["return_state"] for r in rows} == (
        {"READY"} if stage in ("decision", "queued") else {"WAIT_FACT"}
    )
    for _ in range(2):
        assert await flow.advance_current("FACE", face_id) == "CLOSED"
    async with flow.factory() as session:
        assert (
            await session.scalar(select(func.count()).select_from(ReliableMessage)) == before_count
        )
        assert (
            await flow.roller.rack_dispatch.progress(session, job_id=face["rack_job_id"])
        ).state == "CLOSED_BY_SCOPE"
    if stage in ("gated", "accepted"):
        await flow.complete_transport(face["current_transport_task_id"])
        for row in rows:
            await flow.advance_current("RETURN", row["id"])
        assert all(r["close_reason"] == "EXITED" for r in await flow.current_rows("bin_return"))
        async with flow.factory() as session:
            assert (
                await session.scalar(select(func.count()).select_from(ReliableMessage))
                == before_count
            )


async def test_task_close_does_not_close_line_rack_or_unscoped_fact(roller_current):
    flow = roller_current
    async with flow.factory.begin() as session:
        repo = flow.roller.repository(1)
        state = await repo.line_state(session, start=True)
        line_scope = ScopeRef("first", "LINE", state["line_run_id"])
        job_id = await flow.roller.rack_dispatch.register(
            session,
            JobOrigin(
                "ROLLER_DRAIN",
                str(uuid7()),
                1,
                "D1",
                None,
                "LINE-1",
                "FIVE",
                "A",
                None,
                str(uuid7()),
                None,
            ),
            scope=line_scope,
        )
        # The scope filter must leave an independent outgoing obligation alone.
        from types import SimpleNamespace

        from app.http_client import HttpRequest
        from app.reliable import MessageDraft
        from app.reliable.config import TargetConfig

        flow.ecs.outbox.targets["independent"] = TargetConfig(
            "independent", "http://peer.test", "fixture"
        )
        flow.ecs.outbox.adapters["fixture"] = SimpleNamespace(
            build_request=lambda message, target: HttpRequest(
                "POST", "/fact", body=message.request_raw
            )
        )
        original = await flow.ecs.outbox.enqueue(
            session, MessageDraft("independent", "fact", str(uuid7()), b"{}")
        )
        await flow.roller.close_scope(
            session, ScopeRef("first", "TASK", "task-original"), reason="TASK_CANCELLED"
        )
        assert (await flow.roller.rack_dispatch.progress(session, job_id=job_id)).pending
        assert (await session.get(ReliableMessage, original.message_id)).status == "PENDING"


async def test_new_wms_task_starts_independently_after_closed_task_at_same_point(roller_current):
    flow = roller_current
    await flow.scan("SCAN1", "A000000001-B", timestamp=10)
    original = (await flow.current_rows("bin_passage"))[0]
    async with flow.factory.begin() as session:
        await flow.roller.close_scope(
            session, ScopeRef("first", "TASK", "task-original"), reason="TASK_CANCELLED"
        )
    # Exact original retries retain their original ACK, even after a new task exists.
    flow.production.current_task = "new-wms-task"
    await flow.scan("SCAN1", "A000000001-B", timestamp=10)
    assert len(await flow.current_rows("bin_passage")) == 1
    await flow.scan("SCAN1", "A000000001-B", timestamp=20)
    rows = await flow.current_rows("bin_passage")
    assert len(rows) == 2
    old = next(r for r in rows if r["id"] == original["id"])
    new = next(r for r in rows if r["id"] != original["id"])
    assert old["scope_id"] == "task-original" and old["closed_at"]
    assert (
        new["scope_id"] == "new-wms-task" and new["scan1_command_code"] != old["scan1_command_code"]
    )


async def test_existing_final_source_fact_restores_independent_eligibility_without_old_successors(
    roller_return,
):
    flow = roller_return
    await flow.bins(1)
    async with flow.factory.begin() as session:
        face_id = await flow.selected(session)
        await flow.start(session, face_id)
        await flow.decide(session, face_id)
        await flow.consume_return(session, face_id)
    await flow.success(face_id, status="FAILED")
    async with flow.factory.begin() as session:
        await flow.consume_return(session, face_id)
        before = await session.scalar(select(func.count()).select_from(ReliableMessage))
        await flow.roller.close_scope(
            session, ScopeRef("first", "TASK", "T1"), reason="TASK_COMPLETED"
        )
        row = (await flow.rows(session, "bin_return"))[0]
        assert row["return_state"] == "READY" and row["transport_task_id"] is None
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == before


@pytest.mark.parametrize("fact_before_close", [False, True])
@pytest.mark.parametrize("outcome_status", ["FAILED", "UNKNOWN"])
async def test_superseded_wait_fact_closes_only_on_known_original_final_position(
    roller_return, fact_before_close, outcome_status
):
    from app.integrations.wms_transport import TransportCaller

    flow = roller_return
    await flow.bins(1)
    old = (await flow.current_rows("bin_return"))[0]
    await flow.result(old["scan3_command_code"], "SCAN3")
    async with flow.factory.begin() as session:
        face_id = await flow.selected(session)
        await flow.start(session, face_id)
        await flow.decide(session, face_id)
        await flow.consume_return(session, face_id)
        original = await flow.roller.original_record(session, "bin_return", old["id"])
        view = await flow.roller.capabilities.transport.get_result(
            session,
            transport_task_id=original["transport_task_id"],
            client_request_id=original["client_request_id"],
            caller=TransportCaller(**original["caller"]),
        )
    store = MessageStore(flow.factory, lifecycles=flow.ecs.outbox.lifecycles)
    claims = await store.claim(100, flow.ecs.outbox.targets)
    claim = next(c for c in claims if c.message_id == view.submission_message_id)
    assert await store.before_send(claim, flow.ecs.outbox.targets[claim.message.target])
    await flow.scan("SCAN3", old["bin_code"] + "-B", timestamp=100)
    if fact_before_close:
        await flow.success(face_id, status=outcome_status)
        async with flow.factory.begin() as session:
            await flow.consume_return(session, face_id)
    async with flow.factory.begin() as session:
        await flow.roller.close_scope(
            session, ScopeRef("first", "TASK", "T1"), reason="TASK_COMPLETED"
        )
        before = await session.scalar(select(func.count()).select_from(ReliableMessage))
        if not fact_before_close:
            waiting = await flow.roller.original_record(session, "bin_return", old["id"])
            assert waiting["return_state"] == "WAIT_FACT" and waiting["closed_at"] is None
    if not fact_before_close:
        await flow.complete_transport(original["transport_task_id"], status=outcome_status)
    await flow.advance_current("RETURN", old["id"])
    async with flow.factory() as session:
        retired = await flow.roller.original_record(session, "bin_return", old["id"])
        assert not retired["is_current"] and retired["consumed_outcome_revision"] == 1
        if outcome_status == "FAILED":
            assert retired["close_reason"] == "SUPERSEDED" and retired["closed_at"] is not None
        else:
            assert retired["return_state"] == "WAIT_FACT" and retired["closed_at"] is None
        pending = await flow.roller.list_pending(
            session,
            kind="RETURN",
            after_id=None,
            upper_id=await flow.roller.get_pending_upper_id(session, kind="RETURN"),
            limit=100,
        )
        assert (old["id"] in {work.record_id for work in pending}) == (outcome_status == "UNKNOWN")
        assert retired["transport_task_id"] == original["transport_task_id"]
        assert retired["client_request_id"] == original["client_request_id"]
        assert retired["caller"] == original["caller"]
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == before


async def test_line_stop_closes_pending_drain_even_without_open_return_rows(roller_lifecycle):
    flow = roller_lifecycle
    await flow.bins(1)
    flow.successor = "NONE"
    root = (await flow.all_rows("bin_return"))[0]
    await flow.advance("RETURN", root["id"])
    state = (await flow.all_rows("workline_state"))[0]
    await flow.decide_current(
        state["drain_message_id"], dict(result="READY", racks=[dict(rack_id="D1", rack_face=["A"])])
    )
    async with flow.factory.begin() as session:
        await flow.roller.repository(1).close(session, "bin_return", root["id"], "EXITED")
        await flow.roller.close_scope(
            session, ScopeRef("first", "LINE", state["line_run_id"]), reason="LINE_STOPPED"
        )
    assert await flow.advance("DRAIN", state["line_run_id"]) == "CLOSED"
    after = (await flow.all_rows("workline_state"))[0]
    assert after["drain_phase"] == "IDLE" and after["drain_message_id"] is None
    assert after["drain_review_reason"] == "LINE_STOPPED" and after["line_run_id"] is None
    assert not await flow.all_rows("rack_action")
