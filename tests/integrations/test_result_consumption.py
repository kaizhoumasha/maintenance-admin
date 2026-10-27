"""Isolated PostgreSQL acceptance of reader snapshots and caller-owned consumption.

The action table and advancement entry exist only here; no business runtime is required.
"""

import asyncio
from contextlib import asynccontextmanager, nullcontext

import pytest
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.identifiers import uuid7
from app.database import SCHEMA, create_database_engine
from app.integrations.wms_transport import RackPosition, TransportService
from tests.helpers import result_task, transport_callback

metadata = MetaData(schema=SCHEMA)
actions = Table(
    "outbound_result_test_action",
    metadata,
    Column("id", String, primary_key=True),
    Column("done", Boolean, nullable=False),
    Column("revision", Integer),
    Column("consumed_versions", JSON),
    Column("touch", Integer, nullable=False),
)


@asynccontextmanager
async def action_table_lifecycle(factory):
    """Rebuild only our disposable test table, including legacy schemas from prior runs."""
    engine = factory.kw["bind"]
    async with engine.begin() as connection:
        await connection.run_sync(actions.drop, checkfirst=True)
        await connection.run_sync(actions.create)
    try:
        yield factory
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(actions.drop, checkfirst=True)


@pytest.fixture
async def action_factory(pg_sessions):
    async with action_table_lifecycle(pg_sessions) as factory:
        yield factory


async def add_action(factory):
    identity = str(uuid7())
    async with factory.begin() as session:
        await session.execute(actions.insert().values(id=identity, done=False, touch=0))
    return identity


async def advance(
    factory,
    task,
    action_id,
    *,
    before_lock=None,
    fail_before_commit=False,
    fail_after_commit=False,
    attempts=3,
):
    """Test caller owns transactions; readers only see their supplied session."""
    for attempt in range(attempts):
        try:
            async with factory() as session, session.begin():
                await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                assert await session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
                # Locate original configuration before acquiring the action lock.
                await session.scalar(select(actions.c.id).where(actions.c.id == action_id))
                if before_lock:
                    await before_lock(attempt)
                action = (
                    (
                        await session.execute(
                            select(actions).where(actions.c.id == action_id).with_for_update()
                        )
                    )
                    .mappings()
                    .one()
                )
                if action["done"]:
                    return "done"
                result = await TransportService().get_result(
                    session,
                    transport_task_id=task.handle.transport_task_id,
                    client_request_id=task.handle.client_request_id,
                    caller=task.caller,
                )
                if result.closure != "CLOSED":
                    return result
                await task.service.move_rack(
                    session,
                    client_request_id=action_id,
                    caller=task.caller,
                    rack_id="R1",
                    source=RackPosition("TO"),
                    target=RackPosition("NEXT"),
                )
                await session.execute(
                    update(actions)
                    .where(actions.c.id == action_id)
                    .values(done=True, revision=result.outcome_revision)
                )
                if fail_before_commit:
                    raise RuntimeError("crash before commit")
            if fail_after_commit:
                raise RuntimeError("lost caller after commit")
            return result
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) != "40001":
                raise
    return "retry_later"


async def read_part(session, tasks, mode):
    if mode == "batch":
        first = tasks[0]
        result = await TransportService().list_unclosed_predecessors(
            session,
            caller=first.caller,
            cycle_id=first.cycle_id,
            predecessor_ids=tuple(t.handle.transport_task_id for t in tasks),
            rack_id="R1",
        )
        assert result.requested_ids == tuple(t.handle.transport_task_id for t in tasks)
        assert len(result.items) == len(tasks)
        return {
            r.transport_task_id: (
                r.outcome_revision,
                r.closure,
                r.task_closure,
                r.closure_basis["outcome_revision"],
            )
            for r in result.items
        }
    rows = {}
    for task in tasks:
        r = await TransportService().get_result(
            session,
            transport_task_id=task.handle.transport_task_id,
            client_request_id=task.handle.client_request_id,
            caller=task.caller,
        )
        selected = r.outcome["results"]
        if selected is None:
            closure = r.closure
        else:
            c1 = next(m for m in selected if m["container_id"] == "C1")
            closure = "UNDETERMINED" if c1["position_unknown"] else "CLOSED"
        rows[r.transport_task_id] = (
            r.outcome_revision,
            closure,
            r.closure,
            r.closure_basis["outcome_revision"],
        )
    return rows


@pytest.mark.parametrize("mode", ["batch", "single", "mixed"])
async def test_predecessor_group_uses_one_transaction_snapshot(pg_sessions, mode):
    factory = pg_sessions
    a = await result_task(factory, bins=True)
    b = await result_task(
        factory,
        bins=True,
        caller=a.caller,
        cycle_id=a.cycle_id if mode == "batch" else str(uuid7()),
    )
    await transport_callback(factory, a, bins=True)
    await transport_callback(factory, b, bins=True, known=False)
    fillers = []
    for _ in range(99):
        f = await result_task(factory, caller=a.caller, cycle_id=a.cycle_id)
        await transport_callback(factory, f)
        fillers.append(f)
    first_tasks = [a, *fillers]
    async with factory() as session, session.begin():
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        first = await read_part(session, first_tasks, "single" if mode == "single" else "batch")
        # Reader remains open while separate real callback transactions commit new versions.
        async with asyncio.timeout(10):
            await transport_callback(factory, a, bins=True, revision=2, known=False)
            await transport_callback(factory, b, bins=True, revision=2, known=True)
        last = await read_part(session, [b], "batch" if mode == "batch" else "single")
        assert first[a.handle.transport_task_id] == (1, "CLOSED", "UNDETERMINED", 1)
        assert last[b.handle.transport_task_id] == (1, "UNDETERMINED", "UNDETERMINED", 1)
        assert len(first | last) == 101
        assert all(first[f.handle.transport_task_id] == (1, "CLOSED", "CLOSED", 1) for f in fillers)
    async with factory() as session, session.begin():
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        fresh_a = await read_part(session, [a], "single")
        fresh_b = await read_part(session, [b], "single")
        assert fresh_a[a.handle.transport_task_id] == (2, "UNDETERMINED", "UNDETERMINED", 2)
        assert fresh_b[b.handle.transport_task_id] == (2, "CLOSED", "UNDETERMINED", 2)
    await transport_callback(factory, a, bins=True, revision=3, known=True)
    async with factory() as session, session.begin():
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        assert (await read_part(session, [a], "single"))[a.handle.transport_task_id] == (
            3,
            "CLOSED",
            "UNDETERMINED",
            3,
        )
        assert (await read_part(session, [b], "single"))[b.handle.transport_task_id] == (
            2,
            "CLOSED",
            "UNDETERMINED",
            2,
        )
        for task in [a, b]:
            positive = await task.service.list_unclosed_predecessors(
                session,
                caller=task.caller,
                cycle_id=task.cycle_id,
                predecessor_ids=(task.handle.transport_task_id,),
                rack_id="R1",
            )
            assert positive.complete and positive.unresolved == ()
            assert positive.items[0].member_ids == ("C1",)


async def test_read_committed_is_not_a_group_snapshot(pg_sessions):
    a = await result_task(pg_sessions, bins=True)
    b = await result_task(pg_sessions, bins=True, caller=a.caller, cycle_id=a.cycle_id)
    await transport_callback(pg_sessions, a, bins=True)
    await transport_callback(pg_sessions, b, bins=True, known=False)
    async with pg_sessions() as session, session.begin():
        assert await session.scalar(text("SHOW transaction_isolation")) == "read committed"
        before = await read_part(session, [a], "single")
        await transport_callback(pg_sessions, a, bins=True, revision=2, known=False)
        await transport_callback(pg_sessions, b, bins=True, revision=2)
        after = await read_part(session, [b], "single")
        # Negative control only: an unsafe caller could manufacture all-closed from two instants.
        assert before[a.handle.transport_task_id] == (1, "CLOSED", "UNDETERMINED", 1)
        assert after[b.handle.transport_task_id] == (2, "CLOSED", "UNDETERMINED", 2)


async def test_result_consumption_commit_and_recovery(action_factory):
    from app.models import TransportTask

    factory = action_factory
    task = await result_task(factory)
    await transport_callback(factory, task)
    identity = await add_action(factory)
    with pytest.raises(RuntimeError, match="before commit"):
        await advance(factory, task, identity, fail_before_commit=True)
    async with factory() as session:
        assert not await session.scalar(select(actions.c.done).where(actions.c.id == identity))
        assert not await session.scalar(
            select(TransportTask.id).where(TransportTask.client_request_id == identity)
        )
    with pytest.raises(RuntimeError, match="after commit"):
        await advance(factory, task, identity, fail_after_commit=True)
    assert await advance(factory, task, identity) == "done"
    # A second consumer is allowed to use the same original result.
    another = await add_action(factory)
    await advance(factory, task, another)
    async with factory() as session:
        assert (
            len(
                (
                    await session.scalars(
                        select(TransportTask).where(
                            TransportTask.client_request_id.in_([identity, another])
                        )
                    )
                ).all()
            )
            == 2
        )
        assert await session.scalar(select(actions.c.revision).where(actions.c.id == identity)) == 1


async def test_serialization_failure_restarts_entire_consumption(action_factory):
    factory = action_factory
    task = await result_task(factory, bins=True)
    await transport_callback(factory, task, bins=True)
    identity = await add_action(factory)
    attempts = []

    async def change(attempt):
        attempts.append(attempt)
        if attempt == 0:
            async with factory.begin() as writer:
                await writer.execute(
                    update(actions).where(actions.c.id == identity).values(touch=1)
                )
            await transport_callback(factory, task, bins=True, revision=2, known=False)

    result = await advance(factory, task, identity, before_lock=change)
    assert attempts == [0, 1]
    assert result.outcome_revision == 2 and result.closure == "UNDETERMINED"
    assert result.member_facts["C1"]["TARGET_PLACED"]["rack_id"] == "R1"
    async with factory() as session:
        assert not await session.scalar(select(actions.c.done).where(actions.c.id == identity))


async def test_retry_exhaustion_preserves_pending_action(action_factory):
    factory = action_factory
    task = await result_task(factory)
    await transport_callback(factory, task)
    identity = await add_action(factory)
    attempts = []

    async def collide(attempt):
        attempts.append(attempt)
        async with factory.begin() as writer:
            await writer.execute(
                update(actions).where(actions.c.id == identity).values(touch=attempt + 1)
            )

    assert await advance(factory, task, identity, before_lock=collide) == "retry_later"
    assert attempts == [0, 1, 2]
    async with factory() as session:
        assert not await session.scalar(select(actions.c.done).where(actions.c.id == identity))
    assert (await advance(factory, task, identity)).closure == "CLOSED"


@pytest.mark.parametrize("becomes_unknown", [True, False])
async def test_failure_after_reading_restarts_the_entire_predecessor_group(
    action_factory, becomes_unknown
):
    from app.models import ReliableMessage, TransportTask

    factory = action_factory
    a = await result_task(factory, bins=True)
    b = await result_task(factory, bins=True, caller=a.caller, cycle_id=a.cycle_id)
    for task in (a, b):
        await transport_callback(factory, task, bins=True)
    identity, guard = await add_action(factory), await add_action(factory)
    saved_predecessors = (a.handle.transport_task_id, b.handle.transport_task_id)
    observations, attempt_sessions, failed_sqlstates = [], [], []
    for attempt in range(2):
        try:
            async with factory() as session, session.begin():
                attempt_sessions.append(session)
                await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                action = (
                    (
                        await session.execute(
                            select(actions).where(actions.c.id == identity).with_for_update()
                        )
                    )
                    .mappings()
                    .one()
                )
                assert not action["done"] and action["consumed_versions"] is None
                guard_touch = await session.scalar(
                    select(actions.c.touch).where(actions.c.id == guard)
                )
                # Two bounded reads form one logical group. Each attempt must rebuild both parts.
                group, versions, observed = [], {}, {}
                for task_id in saved_predecessors:
                    q = await a.service.list_unclosed_predecessors(
                        session,
                        caller=a.caller,
                        cycle_id=a.cycle_id,
                        predecessor_ids=(task_id,),
                        rack_id="R1",
                    )
                    assert q.requested_ids == (task_id,) and len(q.items) == 1
                    item = q.items[0]
                    assert item.member_ids == ("C1",) and item.task_closure == "UNDETERMINED"
                    assert item.closure_basis["outcome_revision"] == item.outcome_revision
                    assert item.closure_basis["member_ids"] == ("C1",)
                    group.append(q)
                    versions[item.transport_task_id] = item.outcome_revision
                    observed[item.transport_task_id] = (item.outcome_revision, item.closure)
                assert set(versions) == set(saved_predecessors)
                observations.append(observed)
                if all(q.complete and not q.unresolved for q in group):
                    next_task = await a.service.move_rack(
                        session,
                        client_request_id=identity,
                        caller=a.caller,
                        rack_id="R1",
                        source=RackPosition("TO"),
                        target=RackPosition("NEXT"),
                    )
                    await session.execute(
                        update(actions)
                        .where(actions.c.id == identity)
                        .values(
                            done=True,
                            revision=max(versions.values()),
                            consumed_versions=versions,
                        )
                    )
                    assert (
                        await session.scalar(
                            select(ReliableMessage.id).where(
                                ReliableMessage.business_key == next_task.transport_task_id
                            )
                        )
                        is not None
                    )
                if attempt == 0:
                    assert versions == dict.fromkeys(saved_predecessors, 1)
                    assert await session.scalar(
                        select(actions.c.done).where(actions.c.id == identity)
                    )
                    # Framework callbacks do not touch the business action lock. A separate guard
                    # update forces a real 40001 only after results and intent have been written.
                    async with factory.begin() as writer:
                        await writer.execute(
                            update(actions)
                            .where(actions.c.id == guard)
                            .values(touch=guard_touch + 1)
                        )
                    await transport_callback(
                        factory, a, bins=True, revision=2, known=not becomes_unknown
                    )
                    await transport_callback(factory, b, bins=True, revision=2)
                    await session.execute(
                        update(actions).where(actions.c.id == guard).values(touch=guard_touch + 1)
                    )
                    pytest.fail("expected a real serialization failure after result consumption")
            break
        except DBAPIError as error:
            sqlstate = getattr(error.orig, "sqlstate", None)
            assert attempt == 0 and sqlstate == "40001"
            failed_sqlstates.append(sqlstate)
            # Verify all staged effects were rolled back before opening the retry transaction.
            async with factory() as check:
                row = (
                    (await check.execute(select(actions).where(actions.c.id == identity)))
                    .mappings()
                    .one()
                )
                assert (
                    not row["done"] and row["revision"] is None and row["consumed_versions"] is None
                )
                assert (
                    await check.scalar(
                        select(TransportTask.id).where(TransportTask.client_request_id == identity)
                    )
                    is None
                )
                assert (
                    await check.scalar(
                        select(ReliableMessage.id).where(
                            ReliableMessage.business_key == next_task.transport_task_id
                        )
                    )
                    is None
                )
    assert failed_sqlstates == ["40001"]
    assert len(attempt_sessions) == 2 and attempt_sessions[0] is not attempt_sessions[1]
    assert observations == [
        {task_id: (1, "CLOSED") for task_id in saved_predecessors},
        {
            a.handle.transport_task_id: (2, "UNDETERMINED" if becomes_unknown else "CLOSED"),
            b.handle.transport_task_id: (2, "CLOSED"),
        },
    ]
    async with factory() as session:
        row = (
            (await session.execute(select(actions).where(actions.c.id == identity)))
            .mappings()
            .one()
        )
        next_rows = (
            await session.scalars(
                select(TransportTask).where(TransportTask.client_request_id == identity)
            )
        ).all()
        if becomes_unknown:
            assert not row["done"] and row["consumed_versions"] is None and row["revision"] is None
            assert next_rows == []
            assert (
                await session.scalar(
                    select(ReliableMessage.id).where(
                        ReliableMessage.business_key == next_task.transport_task_id
                    )
                )
                is None
            )
        else:
            assert row["done"] and row["consumed_versions"] == dict.fromkeys(saved_predecessors, 2)
            assert row["revision"] == 2 and len(next_rows) == 1
            assert (
                len(
                    (
                        await session.scalars(
                            select(ReliableMessage.id).where(
                                ReliableMessage.business_key == next_rows[0].transport_task_id
                            )
                        )
                    ).all()
                )
                == 1
            )
    if not becomes_unknown:
        assert await advance(factory, a, identity) == "done"
        async with factory() as session:
            assert (
                len(
                    (
                        await session.scalars(
                            select(TransportTask.id).where(
                                TransportTask.client_request_id == identity
                            )
                        )
                    ).all()
                )
                == 1
            )


@pytest.fixture
async def sqlite_lifecycle_factory():
    engine = create_database_engine("sqlite+aiosqlite://")
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture(params=["sqlite_lifecycle_factory", "pg_sessions"])
def lifecycle_factory(request):
    return request.getfixturevalue(request.param)


async def action_table_exists(factory):
    engine = factory.kw["bind"]
    schema = SCHEMA if engine.dialect.name == "postgresql" else None
    async with engine.connect() as connection:
        return await connection.run_sync(
            lambda conn: inspect(conn).has_table(actions.name, schema=schema)
        )


async def test_action_table_replaces_legacy_schema_and_records(lifecycle_factory):
    factory = lifecycle_factory
    engine = factory.kw["bind"]
    legacy = Table(
        actions.name,
        MetaData(schema=SCHEMA),
        Column("id", String, primary_key=True),
        Column("done", Boolean, nullable=False),
        Column("revision", Integer),
        Column("touch", Integer, nullable=False),
    )
    unrelated = Table(
        "task0_fixture_sentinel_" + uuid7().hex,
        MetaData(schema=SCHEMA),
        Column("value", Integer),
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(actions.drop, checkfirst=True)
            await connection.run_sync(legacy.create)
            await connection.execute(
                legacy.insert().values(id="legacy", done=False, revision=1, touch=1)
            )
            await connection.run_sync(unrelated.create)
            await connection.execute(unrelated.insert().values(value=17))
        async with action_table_lifecycle(factory):
            async with factory.begin() as session:
                assert (await session.execute(select(actions))).all() == []
                await session.execute(
                    actions.insert().values(
                        id="current",
                        done=False,
                        touch=0,
                        consumed_versions={"original-task": 3},
                    )
                )
            async with factory() as session:
                assert await session.scalar(select(actions.c.consumed_versions)) == {
                    "original-task": 3
                }
        assert not await action_table_exists(factory)
        async with factory() as session:
            assert await session.scalar(select(unrelated.c.value)) == 17
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(actions.drop, checkfirst=True)
            await connection.run_sync(unrelated.drop, checkfirst=True)


@pytest.mark.parametrize("body_fails", [False, True])
async def test_action_table_lifecycle_cleans_each_run(lifecycle_factory, body_fails):
    factory = lifecycle_factory
    try:
        for run in range(2):
            expected = (
                pytest.raises(RuntimeError, match="test body failed")
                if body_fails
                else nullcontext()
            )
            with expected:
                async with action_table_lifecycle(factory):
                    async with factory.begin() as session:
                        assert (await session.execute(select(actions))).all() == []
                        await session.execute(
                            actions.insert().values(
                                id=f"run-{run}",
                                done=False,
                                touch=0,
                                consumed_versions={"original-task": run + 1},
                            )
                        )
                    if body_fails:
                        raise RuntimeError("test body failed")
            assert not await action_table_exists(factory)
    finally:
        async with factory.kw["bind"].begin() as connection:
            await connection.run_sync(actions.drop, checkfirst=True)
