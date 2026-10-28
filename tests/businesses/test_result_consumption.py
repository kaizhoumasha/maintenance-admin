"""Isolated PostgreSQL: real receivers → durable acceptance → independent business advance."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import event, select, text, update

from app.integrations.wms_callback import receive
from app.main import app
from app.models import CallbackReceipt
from tests.helpers import (
    FlowMove,
    business_flow_fixture,
    flow_request,
    result_task,
    transport_callback,
)


@pytest.fixture
async def pg_flow(pg_sessions, monkeypatch):
    async with business_flow_fixture(pg_sessions, monkeypatch) as flow:
        from tests.helpers import cleanup_flow_results

        await flow.configure()
        tasks, operations = [], set()

        async def task(**kwargs):
            created = await result_task(flow.factory, **kwargs)
            tasks.append(created)
            return created

        async def callback(task, **kwargs):
            await transport_callback(flow.factory, task, owned_operations=operations, **kwargs)

        flow.task, flow.callback, flow.result_operations = task, callback, operations
        try:
            yield flow
        finally:
            await cleanup_flow_results(flow.factory, tasks, operations)


async def accept(flow, *, kind="wms", predecessors=()):
    from httpx import ASGITransport, AsyncClient

    path, body = flow_request(flow, kind=kind, predecessors=predecessors)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(path, json=body)
    assert response.status_code == (200 if kind == "ecs" else 202), response.text
    return body["data"]["execution_id"], response


@pytest.mark.parametrize("kind", ["ecs", "wms"])
async def test_accepted_execution_advances_in_its_own_snapshot_transaction(pg_flow, kind):
    flow = pg_flow
    service = flow.runtime.services["seam_a"]
    statements, connections, advancement_sessions = [], [], []
    engine = flow.factory.kw["bind"].sync_engine

    def sql(conn, cursor, statement, parameters, context, executemany):
        statements.append((conn, statement))

    event.listen(engine, "before_cursor_execute", sql)
    try:
        identity, ack = await accept(flow, kind=kind)
        acceptance_statements = list(statements)
        statements.clear()

        async def observe(stage, attempt, session, evidence):
            if stage == "opened":
                advancement_sessions.append(session)
                connection = await session.connection()
                connections.append(connection)
                assert await connection.get_isolation_level() == "REPEATABLE READ"
                assert await session.scalar(text("SHOW transaction_isolation")) == "repeatable read"

        assert await service.advance(identity, hook=observe) == "advanced"
        assert len(advancement_sessions) == 1
        assert not advancement_sessions[0].in_transaction() and connections[0].closed
        assert acceptance_statements and statements
        assert not any("SHOW transaction_isolation" in sql for _, sql in acceptance_statements)
        first_sql = statements[0][1]
        assert "SHOW transaction_isolation" in first_sql
        assert not {c for c, _ in statements} & {c for c, _ in acceptance_statements}
        row = (await flow.rows())[0]
        assert row["done"] and not row["pending"] and row["receipt_id"]
        async with flow.factory() as session:
            receipt = await session.get(CallbackReceipt, row["receipt_id"])
            assert receipt.status_code == ack.status_code
    finally:
        event.remove(engine, "before_cursor_execute", sql)


@pytest.mark.parametrize("kind", ["ecs", "wms"])
async def test_acceptance_receipt_pending_and_state_roll_back_together(pg_flow, monkeypatch, kind):
    flow = pg_flow
    service = flow.runtime.services["seam_a"]
    original = service.accept

    async def fail(ctx, payload):
        await original(ctx, payload)
        raise RuntimeError("acceptance storage failed")

    monkeypatch.setattr(service, "accept", fail)
    path, body = flow_request(flow, kind=kind)
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post(path, json=body)).status_code == 503
        assert await flow.rows() == []
        async with flow.factory() as session:
            # Only this attempted identity; prior tests' receipts cannot affect the assertion.
            assert not await session.scalar(
                select(CallbackReceipt.id).where(CallbackReceipt.id.in_(flow.receipt_ids))
            )
        monkeypatch.setattr(service, "accept", original)
        assert (await client.post(path, json=body)).status_code == (200 if kind == "ecs" else 202)
    row = (await flow.rows())[0]
    assert row["pending"] and not row["done"] and row["next_intent"] is None


async def test_business_runtime_consumption_recovery(pg_flow):
    flow = pg_flow
    task = await flow.task()
    identity, ack = await accept(flow, predecessors=[task])
    # Lose notification and caller-held ID; discover pending original IDs after restart.
    del identity
    service = flow.rebuild().services["seam_a"]
    recovered = await service.pending_execution_ids()
    assert recovered == ((await flow.rows())[0]["id"],)
    identity = recovered[0]
    assert await service.advance(identity) == "waiting"
    await flow.callback(task)

    async def crash(stage, attempt, session, evidence):
        if stage == "written":
            raise RuntimeError("crash before commit")

    with pytest.raises(RuntimeError, match="before commit"):
        await service.advance(identity, hook=crash)
    row = (await flow.rows())[0]
    assert row["pending"] and not row["done"] and row["evidence"] is row["next_intent"] is None
    async with flow.factory() as session:
        assert await flow.commands.get_by_call(session, owner=flow.owner, call_key=identity) is None
        assert (
            await session.get(CallbackReceipt, row["receipt_id"])
        ).status_code == ack.status_code

    async def lost_caller(stage, attempt, session, evidence):
        if stage == "committed":
            raise RuntimeError("caller died after commit")

    with pytest.raises(RuntimeError, match="after commit"):
        await service.advance(identity, hook=lost_caller)
    restarted = flow.rebuild().services["seam_a"]
    assert await restarted.pending_execution_ids() == ()
    assert await restarted.advance(identity) == "done"
    row = (await flow.rows())[0]
    assert row["evidence"][task.handle.transport_task_id]["revision"] == 1
    assert row["evidence"][task.handle.transport_task_id]["closure"] == "CLOSED"
    async with flow.factory() as session:
        original = await flow.commands.get_by_call(session, owner=flow.owner, call_key=identity)
        assert original.command_code == row["next_intent"]["command_code"]


async def test_result_receive_must_commit_before_independent_advance(pg_flow):
    flow = pg_flow
    task = await flow.task()
    identity, _ = await accept(flow, predecessors=[task])
    from app.core.identifiers import uuid7

    operation_id = str(uuid7())
    flow.result_operations.add(operation_id)
    raw = json.dumps(
        dict(
            operation="transport.task.resulted@v1",
            operation_id=operation_id,
            timestamp=1,
            data=dict(
                transport_task_id=task.handle.transport_task_id,
                kind="RACK_MOVE",
                rack_id="R1",
                outcome_revision=1,
                status="SUCCEEDED",
                final_position=dict(kind="RACK_POSITION", location_code="TO"),
            ),
        )
    ).encode()
    async with flow.factory() as receiving, receiving.begin():
        assert (await receive(receiving, raw)).status_code == 202
        assert await flow.runtime.services["seam_a"].advance(identity) == "waiting"
    assert await flow.runtime.services["seam_a"].advance(identity) == "advanced"


@pytest.mark.parametrize("exhaust", [False, True])
async def test_serialization_failure_reopens_and_rereads_complete_business_group(pg_flow, exhaust):
    flow = pg_flow
    a = await flow.task(bins=True)
    b = await flow.task(bins=True, caller=a.caller, cycle_id=a.cycle_id)
    for task in (a, b):
        await flow.callback(task, bins=True)
    identity, _ = await accept(flow, predecessors=[a, b])
    sessions, observations = [], []

    async def collide(stage, attempt, session, evidence):
        if stage == "opened":
            sessions.append(session)
        if stage == "located" and (attempt == 0 or exhaust):
            async with flow.factory.begin() as writer:
                await writer.execute(
                    update(flow.table).where(flow.table.c.id == identity).values(touch=attempt + 1)
                )
            if attempt == 0:
                await flow.callback(a, bins=True, revision=2, known=False)
                await flow.callback(b, bins=True, revision=2, known=True)
        if stage == "read":
            observations.append(evidence)

    outcome = await flow.runtime.services["seam_a"].advance(identity, hook=collide)
    assert outcome == ("retry_later" if exhaust else "waiting")
    assert len(sessions) == (3 if exhaust else 2)
    assert len({id(s) for s in sessions}) == len(sessions)
    assert all(not s.in_transaction() for s in sessions)
    row = (await flow.rows())[0]
    assert row["pending"] and not row["done"] and row["evidence"] is row["next_intent"] is None
    if not exhaust:
        assert len(observations) == 1
        facts = observations[0]
        assert set(facts) == {a.handle.transport_task_id, b.handle.transport_task_id}
        assert (
            facts[a.handle.transport_task_id]["revision"]
            == facts[b.handle.transport_task_id]["revision"]
            == 2
        )
        assert facts[a.handle.transport_task_id]["closure"] == "UNDETERMINED"
        assert facts[b.handle.transport_task_id]["closure"] == "CLOSED"
        assert all(
            f["members"] == ["C1"] and f["task_closure"] == "UNDETERMINED" for f in facts.values()
        )
    await flow.callback(a, bins=True, revision=3)
    assert await flow.rebuild().services["seam_a"].advance(identity) == "advanced"
    evidence = (await flow.rows())[0]["evidence"]
    assert {t: f["revision"] for t, f in evidence.items()} == {
        a.handle.transport_task_id: 3,
        b.handle.transport_task_id: 2,
    }


async def _competitor_outcomes(tasks, *, wait_seconds=10):
    _, pending = await asyncio.wait(tasks, timeout=wait_seconds)
    if pending:
        raise TimeoutError("competitor outcome wait timed out")
    return [task.result() for task in tasks]


@asynccontextmanager
async def _competing_tasks(release, *, cleanup_timeout=5):
    tasks = []
    try:
        yield tasks
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=cleanup_timeout)
            if pending:
                raise TimeoutError(f"competitor cleanup timed out: {len(pending)} task(s) pending")
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        ("initial_timeout", TimeoutError),
        ("body_error", RuntimeError),
        ("cancelled", asyncio.CancelledError),
    ],
)
async def test_competitors_are_drained_when_initial_wait_or_body_fails(failure, error):
    release, started, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    task = None

    async def holder():
        try:
            started.set()
            await release.wait()
        finally:
            closed.set()

    with pytest.raises(error):
        async with _competing_tasks(release) as tasks:
            task = asyncio.create_task(holder())
            tasks.append(task)
            await asyncio.wait_for(started.wait(), 1)
            if failure == "initial_timeout":
                await asyncio.wait_for(asyncio.Event().wait(), 0)
            if failure == "body_error":
                raise RuntimeError("competitor observation failed")
            raise asyncio.CancelledError
    assert release.is_set() and closed.is_set()
    assert task.done() and task.cancelled()


async def test_competitor_cleanup_deadline_reports_pending_tasks_without_hanging():
    release, started, cancelled, finish = (asyncio.Event() for _ in range(4))
    task = None

    async def slow_cancellation():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await finish.wait()

    async def exercise():
        nonlocal task
        async with _competing_tasks(release, cleanup_timeout=0.01) as tasks:
            task = asyncio.create_task(slow_cancellation())
            tasks.append(task)
            await asyncio.wait_for(started.wait(), 1)

    try:
        with pytest.raises(TimeoutError, match="competitor cleanup timed out"):
            await asyncio.wait_for(exercise(), 1)
        assert release.is_set() and cancelled.is_set()
        assert not task.done()
    finally:
        finish.set()
        if task is not None:
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
    assert task.done()


async def test_outcome_wait_reaches_cleanup_when_child_delays_cancellation():
    release, started, cancelled, finish = (asyncio.Event() for _ in range(4))
    child = None

    async def slow_cancellation():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await finish.wait()

    async def exercise():
        nonlocal child
        async with _competing_tasks(release, cleanup_timeout=0.01) as tasks:
            child = asyncio.create_task(slow_cancellation())
            tasks.append(child)
            await asyncio.wait_for(started.wait(), 1)
            await _competitor_outcomes(tasks, wait_seconds=0.01)

    run = asyncio.create_task(exercise())
    try:
        done, _ = await asyncio.wait({run}, timeout=0.5)
        assert run in done, "outcome wait did not reach bounded cleanup"
        with pytest.raises(TimeoutError, match="competitor cleanup timed out") as failure:
            run.result()
        assert "competitor outcome wait timed out" in str(failure.value.__context__)
        assert release.is_set() and cancelled.is_set()
    finally:
        finish.set()
        await asyncio.wait_for(asyncio.gather(run, return_exceptions=True), 1)
        if child is not None:
            await asyncio.wait_for(asyncio.gather(child, return_exceptions=True), 1)
    assert run.done() and child.done()


async def test_duplicate_wakeups_and_config_update_serialize_without_cross_line(
    pg_flow, monkeypatch, record_property
):
    flow = pg_flow
    identity, _ = await accept(flow)
    service = flow.runtime.services["seam_a"]
    from app.models import WorkLine

    locked, release = asyncio.Event(), asyncio.Event()
    backend_attempts = []

    async def record_backend(name, attempt, session):
        pid = await session.scalar(text("SELECT pg_backend_pid()"))
        backend_attempts.append((name, attempt, pid))

    async def pause(stage, attempt, session, evidence):
        if stage == "opened":
            await record_backend("first", attempt, session)
        if stage == "read" and attempt == 0:
            locked.set()
            await release.wait()

    async def observe_competing(stage, attempt, session, evidence):
        if stage == "opened":
            await record_backend("advance", attempt, session)

    from app.integrations.ecs_command.lifecycle import EcsLifecycle
    from app.reliable.store import MessageStore

    async with flow.factory.begin() as session:
        await flow.commands.create(
            session,
            owner=flow.owner,
            call_key="existing-send-gate",
            device_code=flow.codes[0],
            timeout_ms=100,
            command=FlowMove(position="POSITION_0"),
        )
    store = MessageStore(flow.factory, lifecycles={"ecs-command": EcsLifecycle(flow.commands)})
    claims = await store.claim(1, {"seam-ecs": flow.targets["seam-ecs"]})
    assert len(claims) == 1
    async with _competing_tasks(release) as tasks:
        first = asyncio.create_task(service.advance(identity, hook=pause))
        tasks.append(first)
        await asyncio.wait_for(locked.wait(), 5)

        async def update_config():
            from app.businesses.catalog import get_definition
            from app.schemas.workline import WorkLineUpdate
            from app.services.asset_audit import AssetActor
            from app.services.workline_service import update_workline

            async with flow.factory() as session:
                await record_backend("config", 0, session)
                await update_workline(
                    session,
                    flow.line_ids[0],
                    WorkLineUpdate(
                        line_name="updated",
                        version=1,
                    ),
                    actor=AssetActor(None, "seam-concurrent", None, None),
                    definition_lookup=get_definition,
                )

        original_authorize = flow.commands.routes.authorize_frozen

        async def observe_gate(session, frozen):
            await record_backend("gate", 0, session)
            return await original_authorize(session, frozen)

        monkeypatch.setattr(flow.commands.routes, "authorize_frozen", observe_gate)
        competing = asyncio.create_task(service.advance(identity, hook=observe_competing))
        tasks.append(competing)
        changed = asyncio.create_task(update_config())
        tasks.append(changed)
        gated = asyncio.create_task(store.before_send(claims[0], flow.targets["seam-ecs"]))
        tasks.append(gated)
        async with flow.factory() as observer, asyncio.timeout(5):
            while True:
                initial = {name: pid for name, attempt, pid in backend_attempts if attempt == 0}
                waiting = list(
                    (
                        await observer.execute(
                            text(
                                "SELECT pid, query, pg_blocking_pids(pid) AS blockers "
                                "FROM pg_stat_activity WHERE datname=current_database() "
                                "AND wait_event_type='Lock'"
                            )
                        )
                    ).mappings()
                )
                observed = {row["pid"]: row for row in waiting}
                if set(initial) == {"first", "advance", "config", "gate"} and all(
                    initial[name] in observed for name in ("advance", "config", "gate")
                ):
                    break
                assert not any(task.done() for task in tasks), backend_attempts
                # pg_stat_activity snapshots refresh when the observer transaction ends.
                await observer.rollback()
        assert len(set(initial.values())) == 4, backend_attempts
        lock_waits = []
        for name in ("advance", "config", "gate"):
            row = observed[initial[name]]
            assert row["blockers"] and "work_lines" in row["query"].lower()
            assert "for update" in row["query"].lower()
            lock_waits.append((name, row["pid"], row["blockers"]))
        assert not any(task.done() for task in tasks)
        release.set()
        outcomes = await _competitor_outcomes(tasks)
    record_property("backend_attempts", json.dumps(backend_attempts))
    record_property("observed_lock_waits", json.dumps(lock_waits))
    assert outcomes[:2] == ["advanced", "done"]
    async with flow.factory() as session:
        gated_command = await flow.commands.get_by_call(
            session, owner=flow.owner, call_key="existing-send-gate"
        )
        if outcomes[3]:
            assert gated_command.status == "DISPATCHING"
        else:
            assert gated_command.hold_reason == "AUTHORIZATION_REVOKED"
    row = (await flow.rows())[0]
    assert row["next_intent"]["device_code"] == flow.codes[0]
    async with flow.factory() as session:
        assert (await session.get(WorkLine, flow.line_ids[0])).line_name == "updated"
        assert await flow.commands.get_by_call(session, owner=flow.owner, call_key=identity)


async def test_runtime_snapshot_keeps_full_101_member_versions(pg_flow, monkeypatch):
    flow = pg_flow
    a = await flow.task(bins=True)
    b = await flow.task(bins=True, caller=a.caller, cycle_id=a.cycle_id)
    await flow.callback(a, bins=True)
    await flow.callback(b, bins=True, known=False)
    fillers = []
    for _ in range(99):
        task = await flow.task(caller=a.caller, cycle_id=a.cycle_id)
        await flow.callback(task)
        fillers.append(task)
    tasks = [a, *fillers, b]
    identity, _ = await accept(flow, predecessors=tasks)
    observations = []

    split_seen = []

    async def change_after_group_read(stage, attempt, session, evidence):
        if stage == "predecessor_part" and len(evidence) == 100:
            split_seen.append(set(evidence))
            assert a.handle.transport_task_id in evidence
            assert b.handle.transport_task_id not in evidence
            await flow.callback(a, bins=True, revision=2, known=False)
            await flow.callback(b, bins=True, revision=2)
        if stage == "read":
            observations.append(evidence)

    service = flow.runtime.services["seam_a"]
    reader = service.capabilities.transport
    original_batch, original_single = reader.list_unclosed_predecessors, reader.get_result
    read_calls = []

    async def batch(session, **kwargs):
        read_calls.append(("batch", kwargs["predecessor_ids"]))
        return await original_batch(session, **kwargs)

    async def single(session, **kwargs):
        read_calls.append(("single", kwargs["transport_task_id"]))
        return await original_single(session, **kwargs)

    monkeypatch.setattr(reader, "list_unclosed_predecessors", batch)
    monkeypatch.setattr(reader, "get_result", single)
    assert await service.advance(identity, hook=change_after_group_read) == "waiting"
    assert read_calls == [
        ("batch", tuple(t.handle.transport_task_id for t in tasks[:100])),
        ("single", b.handle.transport_task_id),
        ("batch", (b.handle.transport_task_id,)),
    ]
    assert len(split_seen) == 1

    def assert_group(facts, *, a_revision, b_revision, a_closed, b_closed):
        ids = {t.handle.transport_task_id for t in tasks}
        assert set(facts) == ids and len(facts) == 101
        for task in tasks:
            fact = facts[task.handle.transport_task_id]
            revision = a_revision if task is a else b_revision if task is b else 1
            closure = a_closed if task is a else b_closed if task is b else True
            members = ["C1"] if task is a or task is b else []
            assert fact["revision"] == fact["basis"]["outcome_revision"] == revision
            assert fact["basis"]["transport_task_id"] == task.handle.transport_task_id
            assert fact["members"] == members
            assert tuple(fact["basis"]["member_ids"]) == tuple(members)
            assert fact["basis"]["rack_id"] == "R1" and fact["basis"]["container_id"] is None
            assert fact["closure"] == ("CLOSED" if closure else "UNDETERMINED")
            assert fact["task_closure"] == ("UNDETERMINED" if task is a or task is b else "CLOSED")

    assert_group(observations[0], a_revision=1, b_revision=1, a_closed=True, b_closed=False)
    fresh = []

    async def observe(stage, attempt, session, evidence):
        if stage == "read":
            fresh.append(evidence)

    assert await service.advance(identity, hook=observe) == "waiting"
    assert_group(fresh[0], a_revision=2, b_revision=2, a_closed=False, b_closed=True)
    await flow.callback(a, bins=True, revision=3)
    assert await service.advance(identity) == "advanced"
    saved = (await flow.rows())[0]["evidence"]
    assert_group(saved, a_revision=3, b_revision=2, a_closed=True, b_closed=True)


@pytest.mark.parametrize("save_first", [False, True])
async def test_wms_consumption_survives_cleanup_only_with_saved_business_evidence(
    pg_flow, save_first
):
    from httpx import ASGITransport, AsyncClient

    from tests.helpers import clean_flow_message, flow_decision

    flow = pg_flow
    path, body = flow_request(flow, kind="wms")
    identity = body["data"]["execution_id"]
    request = await flow_decision(flow, identity)
    body["data"]["wms_request"] = request
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post(path, json=body)).status_code == 202
    service = flow.runtime.services["seam_a"]
    if save_first:
        assert await service.advance(identity) == "advanced"
        fact = (await flow.rows())[0]["evidence"]["wms"]
        assert fact == {
            **request,
            "availability": "FOUND",
            "code": "DECIDED",
            "data": {"allow": True},
        }
    await clean_flow_message(flow.factory, request["message_id"])
    async with flow.factory() as session:
        assert (
            await service.capabilities.wms.get_result(session, **request)
        ).availability == "NOT_FOUND"
    assert await flow.rebuild().services["seam_a"].advance(identity) == (
        "done" if save_first else "waiting"
    )
    row = (await flow.rows())[0]
    assert row["done"] is save_first and row["pending"] is not save_first
    assert bool(row["next_intent"]) is save_first


async def test_serialization_after_intent_rolls_back_and_rechecks_all_results(pg_flow):
    flow = pg_flow
    a = await flow.task(bins=True)
    b = await flow.task(bins=True, caller=a.caller, cycle_id=a.cycle_id)
    for task in (a, b):
        await flow.callback(task, bins=True)
    identity, _ = await accept(flow, predecessors=[a, b])
    guard, _ = await accept(flow)
    attempts, observations = [], []

    async def invalidate(stage, attempt, session, evidence):
        if stage == "opened":
            attempts.append(session)
        if stage == "read":
            observations.append(evidence)
        if stage == "written" and attempt == 0:
            assert await session.scalar(
                select(flow.table.c.done).where(flow.table.c.id == identity)
            )
            async with flow.factory.begin() as writer:
                await writer.execute(
                    update(flow.table).where(flow.table.c.id == guard).values(touch=1)
                )
            await flow.callback(a, bins=True, revision=2, known=False)
            await flow.callback(b, bins=True, revision=2)
            # This row changed after the RR snapshot: PostgreSQL raises a genuine 40001 here.
            await session.execute(
                update(flow.table).where(flow.table.c.id == guard).values(touch=2)
            )

    assert await flow.runtime.services["seam_a"].advance(identity, hook=invalidate) == "waiting"
    assert len(attempts) == 2 and attempts[0] is not attempts[1]
    assert len(observations) == 2
    ids = {a.handle.transport_task_id, b.handle.transport_task_id}
    assert set(observations[0]) == set(observations[1]) == ids
    assert all(f["revision"] == 1 for f in observations[0].values())
    assert all(f["revision"] == 2 for f in observations[1].values())
    assert observations[1][a.handle.transport_task_id]["closure"] == "UNDETERMINED"
    assert observations[1][b.handle.transport_task_id]["closure"] == "CLOSED"
    row = next(r for r in await flow.rows() if r["id"] == identity)
    assert row["pending"] and not row["done"] and row["evidence"] is row["next_intent"] is None
    async with flow.factory() as session:
        assert await flow.commands.get_by_call(session, owner=flow.owner, call_key=identity) is None
    await flow.callback(a, bins=True, revision=3)
    assert await flow.rebuild().services["seam_a"].advance(identity) == "advanced"


async def test_original_pending_execution_does_not_follow_reassigned_device(pg_flow):
    from app.businesses.catalog import get_definition
    from app.models import Device
    from app.schemas.device import DeviceAssignment, DeviceAssignmentsIn
    from app.schemas.workline import WorkLineUpdate
    from app.services.asset_audit import AssetActor
    from app.services.device_service import assign_devices
    from app.services.workline_service import update_workline

    flow = pg_flow
    original, _ = await accept(flow)
    actor = AssetActor(None, "reassign-seam", None, None)
    async with flow.factory() as session:
        for line_id in flow.line_ids[:2]:
            await update_workline(
                session,
                line_id,
                WorkLineUpdate(
                    line_name="unbound",
                    version=1,
                    device_bindings={},
                ),
                actor=actor,
                definition_lookup=get_definition,
            )
        device = await session.scalar(select(Device).where(Device.device_code == flow.codes[0]))
        await assign_devices(
            session,
            DeviceAssignmentsIn(
                items=[DeviceAssignment(device_id=device.id, version=device.version)],
                work_line_id=flow.line_ids[1],
            ),
            actor=actor,
        )
        await update_workline(
            session,
            flow.line_ids[1],
            WorkLineUpdate(
                line_name="rebound",
                version=2,
                device_bindings={"SEAM_A": flow.codes[0]},
            ),
            actor=actor,
            definition_lookup=get_definition,
        )
    service = flow.rebuild().services["seam_a"]
    assert await service.advance(original) == "ORIGINAL_BINDING_CHANGED"
    row = next(r for r in await flow.rows() if r["id"] == original)
    assert row["line_id"] == flow.line_ids[0] and row["pending"]
    assert row["next_intent"] is row["evidence"] is None
    async with flow.factory() as session:
        assert await flow.commands.get_by_call(session, owner=flow.owner, call_key=original) is None
    new_identity, _ = await accept(flow)
    assert new_identity != original
    assert await service.advance(new_identity) == "advanced"
    new = next(r for r in await flow.rows() if r["id"] == new_identity)
    assert new["line_id"] == flow.line_ids[1]
    assert new["next_intent"]["position"] == "POSITION_1"


async def test_partial_transport_callback_cleanup_keeps_unrelated_receipts(
    pg_sessions, monkeypatch
):
    from app.integrations import wms_callback
    from tests.helpers import cleanup_flow_results

    sentinel = await result_task(pg_sessions)
    sentinel_ops = await transport_callback(pg_sessions, sentinel)
    owned = await result_task(pg_sessions, bins=True)
    operations = set()
    original_receive = wms_callback.receive
    calls = 0

    async def second_fails(session, raw, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("after first callback commit")
        return await original_receive(session, raw, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(wms_callback, "receive", second_fails)
            with pytest.raises(RuntimeError, match="first callback commit"):
                await transport_callback(pg_sessions, owned, bins=True, owned_operations=operations)
        assert calls == 2 and len(operations) == 2
        async with pg_sessions() as session:
            assert (
                len(
                    list(
                        await session.scalars(
                            select(CallbackReceipt.id).where(
                                CallbackReceipt.operation_id.in_(operations)
                            )
                        )
                    )
                )
                == 1
            )
        await cleanup_flow_results(pg_sessions, [owned], operations)
        async with pg_sessions() as session:
            assert not await session.scalar(
                select(CallbackReceipt.id).where(CallbackReceipt.operation_id.in_(operations))
            )
            assert await session.scalar(
                select(CallbackReceipt.id).where(CallbackReceipt.operation_id.in_(sentinel_ops))
            )
            result = await owned.service.get_result(
                session,
                transport_task_id=owned.handle.transport_task_id,
                client_request_id=owned.handle.client_request_id,
                caller=owned.caller,
            )
            assert result.availability == "NOT_FOUND"
    finally:
        # Cleanup also succeeds if the assertion/fault injection itself failed.
        await cleanup_flow_results(pg_sessions, [owned], operations)
        await cleanup_flow_results(pg_sessions, [sentinel], sentinel_ops)


@pytest.mark.parametrize("kind", ["ecs", "wms"])
@pytest.mark.parametrize("repeated", ["identical", "request_id", "cycle_id"])
async def test_repeated_predecessor_is_rejected_before_business_acceptance(pg_flow, kind, repeated):
    from httpx import ASGITransport, AsyncClient

    from app.core.identifiers import uuid7

    flow = pg_flow
    task = await flow.task()
    await flow.callback(task)
    path, body = flow_request(flow, kind=kind, predecessors=[task, task])
    if repeated != "identical":
        body["data"]["predecessors"][1][repeated] = str(uuid7())
    identity = body["data"]["execution_id"]
    service = flow.runtime.services["seam_a"]
    if kind == "wms":
        flow.result_operations.add(body["operation_id"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(path, json=body)
        assert response.status_code == (400 if kind == "ecs" else 422), response.text
        assert await flow.rows() == []
        assert await service.pending_execution_ids() == ()
        async with flow.factory() as session:
            assert (
                await flow.commands.get_by_call(session, owner=flow.owner, call_key=identity)
                is None
            )
        # A new message can correct the list while preserving the unaccepted execution identity.
        body["data"]["predecessors"].pop()
        if kind == "wms":
            body["operation_id"] = str(uuid7())
        response = await client.post(path, json=body)
        assert response.status_code == (200 if kind == "ecs" else 202), response.text
    assert await service.advance(identity) == "advanced"
    assert await flow.rebuild().services["seam_a"].advance(identity) == "done"
    assert len(await flow.rows()) == 1
