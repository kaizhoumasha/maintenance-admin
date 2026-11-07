from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.integrations.history import RecordDraft, read_bodies, write_record
from app.models import IntegrationExchange, IntegrationRecord

NOW = datetime(2026, 11, 7, tzinfo=UTC)


@pytest.fixture(params=["sqlite", "postgres"])
async def history_store(request, tmp_path, postgres_tables, sqlite_tables):
    from contextlib import AsyncExitStack

    from sqlalchemy import MetaData
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.businesses.catalog import load_business_models
    from app.database import SCHEMA, create_database_engine
    from app.models import Base

    load_business_models()
    async with AsyncExitStack() as stack:
        if request.param == "postgres":
            metadata = MetaData(schema=SCHEMA)
            for table in Base.metadata.tables.values():
                table.to_metadata(metadata)
            engine = await stack.enter_async_context(
                postgres_tables(request.getfixturevalue("pg_url"), "history_retention", metadata)
            )
        else:
            engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path / 'history.db'}")
            stack.push_async_callback(engine.dispose)
            await sqlite_tables(engine)
        yield async_sessionmaker(engine, expire_on_commit=False)


async def diagnostic(factory, *, age, origin="LIVE"):
    at = NOW - timedelta(days=age)
    async with factory.begin() as session:
        root = IntegrationExchange(
            role="DIAGNOSTIC",
            protocol="wms-callback",
            peer_system="WMS",
            direction="INBOUND",
            interaction_kind="CALLBACK",
            identity_kind="REQUEST_ID",
            identity_value=str(age),
            created_at=at,
            capture_origin=origin,
            coverage="LEGACY" if origin == "BACKFILL" else "DECLARED",
        )
        session.add(root)
        await session.flush()
        identity = await write_record(
            session,
            draft=RecordDraft(
                exchange_id=root.id,
                record_kind="CALLBACK_RECEIVED",
                evidence_class="DIAGNOSTIC",
                event_key="original",
                direction="INBOUND",
                occurred_at=at,
                capture_origin=origin,
                raw_body=b"original-preview",
                body_state="PREVIEW",
            ),
        )
        return root.id, identity


async def test_source_deadlines_are_not_reset_by_backfill(history_store):
    from app.tasks.callback_tasks import run_cleanup, run_history_cleanup

    oldest, expired = await diagnostic(history_store, age=91, origin="BACKFILL")
    boundary, boundary_record = await diagnostic(history_store, age=90)
    recent, _ = await diagnostic(history_store, age=7)
    assert await run_cleanup(history_store, 7, now=NOW, batch=1) == 2
    async with history_store() as session:
        record = await session.get(IntegrationRecord, expired)
        assert record.body_state == "PURGED" and record.raw_body is None
        assert record.body_sha256 is not None and record.stored_bytes == len(b"original-preview")
        assert record.details["purged_at"]
    assert await run_history_cleanup(history_store, now=NOW, batch=1) == 1
    async with history_store() as session:
        assert await session.get(IntegrationExchange, oldest) is None
        assert await session.get(IntegrationExchange, boundary)
        assert await session.get(IntegrationExchange, recent)
        assert await session.get(IntegrationRecord, boundary_record)


async def test_protected_first_batch_does_not_starve_later_rows(history_store):
    from app.integrations.history import write_reference
    from app.tasks.callback_tasks import run_history_cleanup

    protected, _ = await diagnostic(history_store, age=101)
    eligible, _ = await diagnostic(history_store, age=100)
    async with history_store.begin() as session:
        await write_reference(
            session,
            exchange_id=protected,
            reference_kind="BUSINESS_ACTION",
            namespace="unknown-owner",
            value="unresolved",
            relation_role="CONSUMED",
            created_at=NOW,
        )
    assert await run_history_cleanup(history_store, now=NOW, batch=1) == 1
    async with history_store() as session:
        assert await session.get(IntegrationExchange, protected)
        assert await session.get(IntegrationExchange, eligible) is None


async def test_expired_receipt_slot_reopens_without_releasing_command_identity(history_store):
    from app.integrations.callback_receipts import insert_receipt
    from app.tasks.callback_tasks import run_history_cleanup, run_receipt_cleanup

    fields = dict(
        operation="unknown@v1",
        operation_id="original",
        raw_digest="a" * 64,
        effective_digest=None,
        compare_mode="RAW",
        status_code=422,
        body={"code": "REJECTED"},
        resp_timestamp=1,
    )
    async with history_store.begin() as session:
        original = await insert_receipt(session, **fields, created_at=NOW - timedelta(days=91))
        command = IntegrationExchange(
            role="ECS_COMMAND",
            protocol="ecs-command",
            peer_system="ECS",
            direction="OUTBOUND",
            interaction_kind="COMMAND",
            identity_kind="COMMAND_CODE",
            identity_value="original-command",
            command_code="original-command",
            owner_domain="owner",
            call_key="original-call",
            call_key_digest="b" * 64,
            input_raw=b"original-input",
            status="RECONCILING",
            delivery_status="HELD",
            created_at=NOW - timedelta(days=100),
        )
        session.add(command)
        await session.flush()
        code, root_id = command.command_code, command.id
    assert await run_receipt_cleanup(history_store, 90, now=NOW) == 1
    async with history_store.begin() as session:
        replacement = await insert_receipt(session, **fields, created_at=NOW)
        assert replacement != original
    await run_history_cleanup(history_store, now=NOW, batch=1)
    async with history_store() as session:
        saved = await session.get(IntegrationExchange, root_id)
        assert saved.command_code == code and saved.call_key == "original-call"


async def test_incoming_core_body_reference_protects_canonical_bytes(history_store):
    from app.tasks.callback_tasks import run_cleanup, run_history_cleanup

    source, original = await diagnostic(history_store, age=100)
    async with history_store.begin() as session:
        # Unprovable legacy relationships retain bytes needed by an existing core reference.
        record = await session.get(IntegrationRecord, original)
        record.evidence_class = "CORE"
    consumer, _ = await diagnostic(history_store, age=1)
    async with history_store.begin() as session:
        await write_record(
            session,
            draft=RecordDraft(
                exchange_id=consumer,
                record_kind="RESPONSE_PREPARED",
                event_key="original-reply",
                direction="OUTBOUND",
                body_record_id=original,
            ),
        )
    await run_cleanup(history_store, 7, now=NOW)
    await run_history_cleanup(history_store, now=NOW, batch=1)
    async with history_store() as session:
        body = (await read_bodies(session, record_ids=(original,)))[original]
        assert body.raw_body == b"original-preview"
        assert await session.get(IntegrationExchange, source)


@pytest.mark.parametrize("kind", ["history", "body"])
async def test_mixed_history_candidates_take_original_worklines_in_global_order(
    history_store, wait_for_lock, kind
):
    import asyncio

    from sqlalchemy import delete

    from app.businesses.coarse_sorting.models import CoarseSortingAction, CoarseSortingJob
    from app.core.identifiers import uuid7
    from app.integrations.callback_receipts import insert_receipt
    from app.models import WorkLine
    from app.tasks.callback_tasks import run_cleanup, run_history_cleanup
    from tests.helpers import seed_reliable_message

    if history_store.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL row lock order")
    key = str(uuid7())
    at = NOW - timedelta(days=100)
    async with history_store.begin() as session:
        low = WorkLine(line_code=key + "-low", line_name="Low")
        high = WorkLine(line_code=key + "-high", line_name="High")
        session.add_all([low, high])
        await session.flush()
        low_id, high_id = low.id, high.id
        receipt = await insert_receipt(
            session,
            operation="ecs.event",
            operation_id=key,
            raw_digest="a" * 64,
            effective_digest=None,
            compare_mode="RAW",
            status_code=200,
            body={},
            resp_timestamp=1,
            created_at=at,
            event_envelope={
                "device_code": "D-high",
                "event_type": "SCAN_COMPLETED",
                "timestamp": 1,
                "data": {},
            },
            event_context={
                "owner_domain": "first",
                "component_key": "bin_roller",
                "work_line_id": high_id,
                "device_id": 1,
                "device_role": "SCAN1",
                "device_version": 1,
                "work_line_version": 1,
            },
        )
        receipt_root = await session.scalar(
            select(IntegrationExchange).where(IntegrationExchange.receipt_id == receipt)
        )
        message = await seed_reliable_message(
            session,
            target="test",
            operation="op",
            operation_id=key,
            request_raw=b"wire",
            status="PENDING",
            attempts=0,
            next_attempt_at=NOW,
            created_at=at,
            updated_at=at,
        )
        session.add(
            CoarseSortingJob(
                id=key,
                event_digest=key,
                receipt_id=1,
                work_line_id=low_id,
                workline_code=key,
                device_id=1,
                device_code="D-low",
                device_role="SCAN1",
                device_version=1,
                work_line_version=1,
                binding_revision="1",
                timestamp=1,
                payload={},
                received_at=at,
                pending=True,
                wait_reason="WAIT",
            )
        )
        await session.flush()
        session.add(
            CoarseSortingAction(
                id=key,
                job_id=key,
                kind="ALLOCATE_STORAGE",
                round_no=1,
                rack_generation_no=1,
                request_data={},
                operation="op",
                operation_id=key,
                message_id=message.id,
                evidence_history=[],
                created_at=at,
            )
        )
        for identity in (receipt_root.id, message.exchange_id):
            await write_record(
                session,
                draft=RecordDraft(
                    exchange_id=identity,
                    record_kind="CALLBACK_RECEIVED",
                    event_key="diagnostic",
                    direction="INBOUND",
                    evidence_class="DIAGNOSTIC",
                    raw_body=b"preview",
                    occurred_at=at,
                ),
            )
    pending = None
    try:
        async with history_store() as holder, holder.begin():
            await holder.execute(select(WorkLine.id).where(WorkLine.id == low_id).with_for_update())
            cleanup = (
                run_history_cleanup(history_store, now=NOW)
                if kind == "history"
                else run_cleanup(history_store, 7, now=NOW)
            )
            pending = asyncio.create_task(cleanup)
            await wait_for_lock(pending)
            assert not pending.done()
            async with history_store() as observer, observer.begin():
                await observer.execute(
                    select(WorkLine.id).where(WorkLine.id == high_id).with_for_update(nowait=True)
                )
        await asyncio.wait_for(pending, 5)
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        async with history_store.begin() as session:
            await session.execute(delete(CoarseSortingAction).where(CoarseSortingAction.id == key))
            await session.execute(delete(CoarseSortingJob).where(CoarseSortingJob.id == key))
            await session.execute(delete(WorkLine).where(WorkLine.id.in_((low_id, high_id))))


@pytest.mark.parametrize("first", ["writer", "cleanup"])
async def test_body_purge_and_new_reference_share_original_lock(history_store, monkeypatch, first):
    import asyncio

    from app.integrations import history
    from app.reliable.lifecycle import EvidencePersistenceError
    from app.tasks.callback_tasks import run_history_cleanup

    if history_store.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL concurrent FK protection")
    source, original = await diagnostic(history_store, age=100)
    consumer, _ = await diagnostic(history_store, age=1)
    async with history_store.begin() as session:
        record = await session.get(IntegrationRecord, original)
        record.evidence_class, record.body_state = "CORE", "FULL"
    read, release = asyncio.Event(), asyncio.Event()
    original_read = history.read_bodies

    original_lock = history._lock_body_reference

    async def pause(session, identity, **options):
        if first == "cleanup" and identity == original:
            read.set()
            await asyncio.wait_for(release.wait(), 5)
        return await original_lock(session, identity, **options)

    monkeypatch.setattr(history, "_lock_body_reference", pause)

    async def write():
        async with history_store.begin() as session:
            root = await session.get(IntegrationExchange, consumer)
            root.owner_domain = "new-consumption"
            await history.write_record(
                session,
                draft=RecordDraft(
                    exchange_id=consumer,
                    record_kind="RESPONSE_PREPARED",
                    event_key="new-core-reference",
                    direction="OUTBOUND",
                    body_record_id=original,
                ),
            )

    if first == "writer":
        await write()
        assert await run_history_cleanup(history_store, now=NOW) == 0
        async with history_store() as session:
            assert (await original_read(session, record_ids=(original,)))[
                original
            ].raw_body == b"original-preview"
    else:
        pending = asyncio.create_task(write())
        try:
            await asyncio.wait_for(read.wait(), 5)
            assert await run_history_cleanup(history_store, now=NOW) == 1
        finally:
            release.set()
        with pytest.raises(EvidencePersistenceError):
            await asyncio.wait_for(pending, 5)
        async with history_store() as session:
            root = await session.get(IntegrationExchange, consumer)
            assert root.owner_domain is None
            assert not await session.scalar(
                select(IntegrationRecord.id).where(
                    IntegrationRecord.event_key == "new-core-reference"
                )
            )


async def test_released_event_is_runtime_missing_while_fresh_history_remains(history_store):
    from app.integrations.callback_receipts import insert_receipt
    from app.integrations.ecs_callback import get_event, list_point_events
    from app.tasks.callback_tasks import run_history_cleanup, run_receipt_cleanup

    async with history_store.begin() as session:
        event = await insert_receipt(
            session,
            operation="ecs.event",
            operation_id="old-event",
            raw_digest="a" * 64,
            effective_digest=None,
            compare_mode="RAW",
            status_code=200,
            body={},
            resp_timestamp=1,
            created_at=NOW - timedelta(days=100),
            raw_body=b"original-event",
            event_envelope={
                "device_code": "D",
                "event_type": "SCAN_COMPLETED",
                "timestamp": 1,
                "data": {},
            },
            event_context={
                "owner_domain": "first",
                "component_key": "bin_roller",
                "work_line_id": 1,
                "device_id": 1,
                "device_role": "SCAN1",
                "device_version": 1,
                "work_line_version": 1,
            },
        )
        root = await session.scalar(
            select(IntegrationExchange).where(IntegrationExchange.receipt_id == event)
        )
        root_id, body_id = root.id, root.request_record_id
        await write_record(
            session,
            draft=RecordDraft(
                exchange_id=root.id,
                record_kind="CALLBACK_RECEIVED",
                event_key="fresh-observation",
                direction="INBOUND",
                evidence_class="DIAGNOSTIC",
                raw_body=b"new-preview",
                occurred_at=NOW,
            ),
        )
    assert await run_receipt_cleanup(history_store, 90, now=NOW) == 1
    assert await run_history_cleanup(history_store, now=NOW) == 0
    async with history_store() as session:
        assert (await get_event(session, event_id=event)).availability == "NOT_FOUND"
        assert (
            await list_point_events(session, owner_domain="first", work_line_id=1, point="SCAN1")
            == ()
        )
        assert await session.get(IntegrationExchange, root_id)
        body = (await read_bodies(session, record_ids=(body_id,)))[body_id]
        assert body.body_state == "PURGED" and body.raw_body is None


async def test_unknown_obligation_is_not_excused_by_another_closed_owner(history_store):
    from sqlalchemy import delete

    from app.businesses.coarse_sorting.models import CoarseSortingAction, CoarseSortingJob
    from app.core.identifiers import uuid7
    from app.integrations.history import link_message, write_reference
    from app.models import WorkLine
    from app.tasks.callback_tasks import run_history_cleanup
    from app.tasks.reliable_tasks import run_cleanup
    from tests.helpers import seed_reliable_message

    key = str(uuid7())
    old = NOW - timedelta(days=100)
    async with history_store.begin() as session:
        line = WorkLine(line_code=key, line_name="Original")
        session.add(line)
        await session.flush()
        line_id = line.id
        message = await seed_reliable_message(
            session,
            target="test",
            operation="op",
            operation_id=key,
            request_raw=b"wire",
            status="DONE",
            attempts=1,
            next_attempt_at=NOW,
            created_at=old,
            updated_at=old,
        )
        root_id = message.exchange_id
        await link_message(session, exchange_id=root_id, message_id=message.id, at=old)
        session.add(
            CoarseSortingJob(
                id=key,
                event_digest=key,
                receipt_id=1,
                work_line_id=line_id,
                workline_code=key,
                device_id=1,
                device_code="D",
                device_role="SCAN1",
                device_version=1,
                work_line_version=1,
                binding_revision="1",
                timestamp=1,
                payload={},
                received_at=old,
                pending=True,
                wait_reason="WAIT",
            )
        )
        await session.flush()
        session.add(
            CoarseSortingAction(
                id=key,
                job_id=key,
                kind="ALLOCATE_STORAGE",
                round_no=1,
                rack_generation_no=1,
                request_data={},
                operation="op",
                operation_id=key,
                message_id=message.id,
                evidence_history=[],
                created_at=old,
            )
        )
        await session.flush()
        job = await session.get(CoarseSortingJob, key)
        (
            job.pending,
            job.state,
            job.current_action_id,
            job.allocation_action_id,
            job.completed_at,
            job.placed_at_ms,
        ) = False, "COMPLETED", key, key, old, 1
        for owner, value in (("coarse_sorting", key), ("unknown-owner", "still-needed")):
            await write_reference(
                session,
                exchange_id=root_id,
                reference_kind="BUSINESS_ACTION",
                namespace=owner,
                value=value,
                relation_role="OWNER",
                created_at=old,
            )
    try:
        assert await run_cleanup(history_store, 90, now=NOW) == 1
        assert await run_history_cleanup(history_store, now=NOW) == 0
        async with history_store() as session:
            assert await session.get(IntegrationExchange, root_id)
    finally:
        async with history_store.begin() as session:
            job = await session.get(CoarseSortingJob, key)
            job.current_action_id = job.allocation_action_id = None
            job.state, job.completed_at, job.placed_at_ms = "AWAITING_VALIDATION", None, None
            await session.flush()
            await session.execute(delete(CoarseSortingAction).where(CoarseSortingAction.id == key))
            await session.execute(delete(CoarseSortingJob).where(CoarseSortingJob.id == key))
            await session.execute(delete(WorkLine).where(WorkLine.id == line_id))


@pytest.mark.parametrize("mode", ["body", "history", "receipt"])
async def test_new_obligation_committed_after_candidate_discovery_is_reread(
    history_store, monkeypatch, mode
):
    import asyncio

    from app.integrations.callback_receipts import insert_receipt
    from app.integrations.history import write_reference
    from app.tasks import callback_tasks as cleanup

    if history_store.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL committed reference race")
    if mode == "receipt":
        async with history_store.begin() as session:
            receipt = await insert_receipt(
                session,
                operation="unknown",
                operation_id="reference-race",
                raw_digest="a" * 64,
                effective_digest=None,
                compare_mode="RAW",
                status_code=200,
                body={},
                resp_timestamp=1,
                created_at=NOW - timedelta(days=100),
                raw_body=b"original",
            )
            root = await session.scalar(
                select(IntegrationExchange).where(IntegrationExchange.receipt_id == receipt)
            )
            root_id, record_id = root.id, root.request_record_id
    else:
        root_id, record_id = await diagnostic(history_store, age=100)
    discovered, release = asyncio.Event(), asyncio.Event()
    original = cleanup._protected_roots

    async def pause(session, roots):
        result = await original(session, roots)
        discovered.set()
        await asyncio.wait_for(release.wait(), 5)
        return result

    monkeypatch.setattr(cleanup, "_protected_roots", pause)
    operation = (
        cleanup.run_cleanup(history_store, 7, now=NOW)
        if mode == "body"
        else cleanup.run_history_cleanup(history_store, now=NOW)
        if mode == "history"
        else cleanup.run_receipt_cleanup(history_store, 90, now=NOW)
    )
    pending = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(discovered.wait(), 5)
        async with history_store.begin() as session:
            await write_reference(
                session,
                exchange_id=root_id,
                reference_kind="BUSINESS_ACTION",
                namespace="unknown-owner",
                value="new-obligation",
                relation_role="OWNER",
                created_at=NOW,
            )
        release.set()
        assert await asyncio.wait_for(pending, 5) == 0
        async with history_store() as session:
            assert await session.get(IntegrationExchange, root_id)
            assert (await read_bodies(session, record_ids=(record_id,)))[
                record_id
            ].raw_body is not None
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_summary_and_result_cost_remain_bounded_with_large_diagnostic_history(history_store):
    import json
    import os
    from hashlib import sha256
    from time import perf_counter

    from sqlalchemy import event, text

    from app.core.identifiers import uuid7
    from app.integrations.ecs_command.results import get_command
    from app.schemas.integration_history import ExchangeListParams
    from app.services.integration_history_service import list_exchanges
    from app.tasks.callback_tasks import run_history_cleanup

    engine = history_store.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("EXPLAIN BUFFERS requires PostgreSQL")
    at = NOW - timedelta(days=100)
    code = str(uuid7())
    async with history_store.begin() as session:
        roots = [
            dict(
                id=uuid7(),
                role="DIAGNOSTIC",
                protocol="wms-callback",
                direction="INBOUND",
                interaction_kind="CALLBACK",
                identity_kind="REQUEST_ID",
                identity_value=f"cost:{n}",
                created_at=NOW if n < 100 else at,
            )
            for n in range(2000)
        ]
        await session.execute(IntegrationExchange.__table__.insert(), roots)
        command = IntegrationExchange(
            role="ECS_COMMAND",
            protocol="ecs-command",
            direction="OUTBOUND",
            interaction_kind="COMMAND",
            identity_kind="COMMAND_CODE",
            identity_value=code,
            command_code=code,
            owner_domain="cost",
            call_key=code,
            call_key_digest=sha256(code.encode()).hexdigest(),
            input_raw=b"original",
            status="QUEUED",
            delivery_status="PENDING",
            created_at=at,
        )
        session.add(command)
        await session.flush()
        payload = os.urandom(1024)
        records = [
            dict(
                id=uuid7(),
                exchange_id=roots[100]["id"] if n < 10000 else command.id,
                record_kind="CALLBACK_RECEIVED",
                evidence_class="DIAGNOSTIC",
                event_key=f"cost:{n}",
                direction="INBOUND",
                occurred_at=at,
                recorded_at=at,
                raw_body=payload,
                body_state="FULL",
                body_sha256=sha256(payload).hexdigest(),
                stored_bytes=len(payload),
            )
            for n in range(20000)
        ]
        await session.execute(IntegrationRecord.__table__.insert(), records)
        await session.execute(text("ANALYZE"))
    statements = []

    def capture(conn, cursor, statement, parameters, context, many):
        if context.compiled is not None and statement.lstrip().upper().startswith("SELECT"):
            statements.append(context.compiled.statement)

    event.listen(engine.sync_engine, "after_cursor_execute", capture)
    try:
        counts = []
        timings = {}
        async with history_store() as session:
            for limit in (20, 100):
                before = len(statements)
                started = perf_counter()
                page = await list_exchanges(
                    session,
                    ExchangeListParams(from_at=NOW - timedelta(days=90), until_at=NOW, limit=limit),
                )
                timings[f"summary_{limit}_ms"] = (perf_counter() - started) * 1000
                assert len(page["items"]) == limit
                counts.append(len(statements) - before)
            before = len(statements)
            started = perf_counter()
            view = await get_command(session, owner_domain="cost", command_code=code)
            timings["result_lookup_ms"] = (perf_counter() - started) * 1000
            assert view.command_code == code and len(statements) - before == 1
        assert counts == [4, 4]
    finally:
        event.remove(engine.sync_engine, "after_cursor_execute", capture)
    plans = []
    async with engine.connect() as connection:
        schema = connection.sync_connection.get_execution_options()["schema_translate_map"][
            "enterprise_backend"
        ]
        for stmt in statements[:4] + statements[-1:]:
            sql = str(
                stmt.compile(dialect=engine.dialect, compile_kwargs={"literal_binds": True})
            ).replace("enterprise_backend.", f"{schema}.")
            plan = await connection.scalar(text("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + sql))
            plans.append(plan)
        for column in ("body_record_id", "response_record_id", "parent_record_id"):
            plan = await connection.scalar(
                text(
                    "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) "
                    f"SELECT id FROM {schema}.integration_record "
                    f"WHERE {column}='{command.id}'::uuid"
                )
            )
            assert plan[0]["Plan"].get("Rows Removed by Filter", 0) <= 1000
            plans.append(plan)
        sizes = (
            (
                await connection.execute(
                    text(
                        "SELECT pg_total_relation_size(:table) AS total, "
                        "pg_indexes_size(:table) AS indexes"
                    ),
                    {"table": f"{schema}.integration_record"},
                )
            )
            .mappings()
            .one()
        )
    started = perf_counter()
    removed = await run_history_cleanup(history_store, now=NOW, batch=100)
    timings["cleanup_1900_roots_ms"] = (perf_counter() - started) * 1000
    assert removed == 1900
    async with history_store() as session:
        assert await session.get(IntegrationExchange, command.id) is not None
    print(
        "HISTORY_COST_REPORT="
        + json.dumps(
            dict(
                rows=dict(roots=2001, diagnostic=20000, body_bytes=20480000),
                query_counts=counts,
                timings=timings,
                record_storage=dict(sizes),
                plans=plans,
            ),
            default=str,
        )
    )


async def test_new_core_body_reference_survives_expiry_during_registration(
    history_store, monkeypatch
):
    import asyncio

    from app.integrations import history
    from app.integrations.callback_receipts import insert_receipt
    from app.tasks.callback_tasks import run_receipt_cleanup

    if history_store.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL concurrent canonical body retention contract")
    async with history_store.begin() as session:
        receipt_id = await insert_receipt(
            session,
            operation="history.borrow",
            operation_id="original",
            raw_digest="a" * 64,
            compare_mode="RAW",
            status_code=200,
            body={"code": 0},
            resp_timestamp=1,
            created_at=NOW - timedelta(days=100),
            raw_body=b"original-core",
        )
        source = await session.scalar(
            select(IntegrationExchange.request_record_id).where(
                IntegrationExchange.receipt_id == receipt_id
            )
        )
    fetched, release = asyncio.Event(), asyncio.Event()
    original_read = history.read_bodies

    async def pause_after_read(session, *, record_ids):
        result = await original_read(session, record_ids=record_ids)
        if record_ids == (source,):
            fetched.set()
            await release.wait()
        return result

    monkeypatch.setattr(history, "read_bodies", pause_after_read)

    async def register_new_fact():
        async with history_store.begin() as session:
            root = IntegrationExchange(
                role="OUTGOING",
                protocol="opaque",
                direction="OUTBOUND",
                interaction_kind="REQUEST",
                identity_kind="OPERATION_ID",
                identity_value="new-owner",
                created_at=NOW,
            )
            session.add(root)
            await session.flush()
            return await history.write_record(
                session,
                draft=RecordDraft(
                    exchange_id=root.id,
                    record_kind="REQUEST_REGISTERED",
                    event_key="new-core",
                    direction="OUTBOUND",
                    body_record_id=source,
                ),
            )

    writer = asyncio.create_task(register_new_fact())
    try:
        await asyncio.wait_for(fetched.wait(), 5)
        assert await asyncio.wait_for(run_receipt_cleanup(history_store, 90, now=NOW), 5) == 1
    finally:
        release.set()
    identity = await asyncio.wait_for(writer, 5)
    async with history_store() as session:
        body = (await original_read(session, record_ids=(identity,)))[identity]
        assert body.body_state == "FULL" and body.raw_body == b"original-core"


@pytest.mark.parametrize("reference", ("body_record_id", "response_record_id"))
async def test_purged_core_body_cannot_supply_new_authority(history_store, reference):
    from app.integrations.callback_receipts import insert_receipt
    from app.integrations.history import EvidenceIntegrityError
    from app.tasks.callback_tasks import run_receipt_cleanup

    async with history_store.begin() as session:
        receipt = await insert_receipt(
            session,
            operation="history.purged",
            operation_id="original",
            raw_digest="a" * 64,
            compare_mode="RAW",
            status_code=200,
            body={"code": 0},
            resp_timestamp=1,
            created_at=NOW - timedelta(days=100),
            raw_body=b"original",
        )
        source = await session.scalar(
            select(
                getattr(
                    IntegrationExchange,
                    "request_record_id"
                    if reference == "body_record_id"
                    else "current_response_record_id",
                )
            ).where(IntegrationExchange.receipt_id == receipt)
        )
    assert await run_receipt_cleanup(history_store, 90, now=NOW) == 1
    async with history_store.begin() as session:
        root = IntegrationExchange(
            role="OUTGOING",
            protocol="opaque",
            direction="OUTBOUND",
            interaction_kind="REQUEST",
            identity_kind="OPERATION_ID",
            identity_value="new",
            created_at=NOW,
        )
        session.add(root)
        await session.flush()
        with pytest.raises(EvidenceIntegrityError):
            await write_record(
                session,
                draft=RecordDraft(
                    exchange_id=root.id,
                    record_kind="RESULT_EVIDENCE"
                    if reference == "response_record_id"
                    else "REQUEST_REGISTERED",
                    event_key="new-core",
                    direction="OUTBOUND",
                    **{reference: source},
                    **(
                        {
                            "semantic_digest": "b" * 64,
                            "effective_payload": {"result": "SUCCESS"},
                            "applied": True,
                        }
                        if reference == "response_record_id"
                        else {}
                    ),
                ),
            )


@pytest.mark.parametrize("state", ["MOVING_IN", "READY", "DEPART_ACCEPTED"])
async def test_preparation_history_uses_one_exact_owner_and_original_closure(history_store, state):
    from sqlalchemy import delete

    from app.businesses.coarse_sorting.models import (
        CoarseSortingAction,
        CoarseSortingJob,
        RackPreparation,
    )
    from app.core.identifiers import uuid7
    from app.integrations.history import write_reference
    from app.models import WorkLine
    from app.schemas.integration_history import ExchangeListParams
    from app.services.integration_history_service import list_exchanges
    from app.tasks.callback_tasks import run_history_cleanup

    key = str(uuid7())
    old = NOW - timedelta(days=100)
    root, _ = await diagnostic(history_store, age=100)
    async with history_store.begin() as session:
        line = WorkLine(line_code=key, line_name="Original")
        session.add(line)
        await session.flush()
        line_id = line.id
        session.add(
            CoarseSortingJob(
                id=key,
                event_digest=key,
                receipt_id=1,
                work_line_id=line_id,
                workline_code=key,
                device_id=1,
                device_code="D",
                device_role="SCAN1",
                device_version=1,
                work_line_version=1,
                binding_revision="1",
                timestamp=1,
                payload={},
                received_at=old,
                pending=True,
                wait_reason="WAIT",
            )
        )
        await session.flush()
        session.add(
            CoarseSortingAction(
                id=key,
                job_id=key,
                kind="ALLOCATE_STORAGE",
                round_no=1,
                rack_generation_no=1,
                request_data={},
                operation="original",
                operation_id=key,
                message_id=1,
                evidence_history=[],
                created_at=old,
            )
        )
        await session.flush()
        session.add(
            RackPreparation(
                id=key,
                work_line_id=line_id,
                workline_code=key,
                rack_position_code="P",
                rack_binding_version=1,
                generation_no=1,
                mode="DEPART_ONLY" if state == "DEPART_ACCEPTED" else "REPLENISH",
                old_rack_id="OLD" if state == "DEPART_ACCEPTED" else None,
                incoming_rack_id=None if state == "DEPART_ACCEPTED" else "NEW",
                incoming_target_face=None if state == "DEPART_ACCEPTED" else "90",
                source_allocation_action_id=key,
                predecessor_action_ids=[],
                state=state,
                created_at=old,
                updated_at=old,
                ready_at=old if state == "READY" else None,
            )
        )
        await session.flush()
        await write_reference(
            session,
            exchange_id=root,
            reference_kind="BUSINESS_PREPARATION",
            namespace="coarse_sorting",
            value=key,
            relation_role="OWNER",
            created_at=old,
        )
    try:
        async with history_store() as session:
            page = await list_exchanges(
                session,
                ExchangeListParams(
                    reference_kind="BUSINESS_PREPARATION",
                    reference_namespace="coarse_sorting",
                    reference_value=key,
                ),
            )
            assert {row["id"] for row in page["items"]} == {root}
        assert await run_history_cleanup(history_store, now=NOW) == (
            0 if state == "MOVING_IN" else 1
        )
        async with history_store() as session:
            assert (await session.get(IntegrationExchange, root) is not None) == (
                state == "MOVING_IN"
            )
    finally:
        async with history_store.begin() as session:
            await session.execute(delete(RackPreparation).where(RackPreparation.id == key))
            await session.execute(delete(CoarseSortingAction).where(CoarseSortingAction.id == key))
            await session.execute(delete(CoarseSortingJob).where(CoarseSortingJob.id == key))
            await session.execute(delete(WorkLine).where(WorkLine.id == line_id))
