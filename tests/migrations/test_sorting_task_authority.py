import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError


@pytest.mark.parametrize(
    "state_migration",
    [
        ("TEST_HISTORY_MIGRATION_DATABASE_URL", backend, "xray_pending_due")
        for backend in ("sqlite", "postgres")
    ],
    indirect=True,
)
async def test_sorting_task_migration_preserves_original_work_and_references(state_migration):
    store = state_migration
    async with store.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO smt_manual_task (task_id, task_type, business_type, work_line_id, "
                "workline_code, issued_operation_id, issued_timestamp, issued_data, "
                "queue_revision, dispatch_sequence, not_before, status, plan_revision, "
                "prepare_operation_id, prepare_message_id, prepare_accepted, received_work) "
                "VALUES ('M', 'MANUAL', 'outbound', 1, 'LINE-M', 'issued-m', 1, '{}', "
                "3, 1, 0, 'PREPARING', 0, 'prepare-m', 101, false, '[]')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO smt_auto_task (task_id, task_type, business_type, work_line_id, "
                "workline_code, issued_operation_id, issued_timestamp, issued_data, "
                "queue_revision, dispatch_sequence, status, plan_revision, "
                "closed_at_ms, details_cleaned, details_cleaned_at_ms, pending) "
                "VALUES ('A', 'AUTO', 'inbound', 2, 'LINE-A', 'issued-a', 2, '{}', "
                "1, 1, 'COMPLETED', 1, 50, true, 60, false)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO smt_auto_task_request (task_id, purpose, operation, operation_id, "
                "message_id, request_data, unresolved) VALUES "
                "('A', 'PREPARE', 'sorting.task.prepare@v1', 'prepare-a', 102, '{}', false)"
            )
        )

    await store.migrate("upgrade", "sorting_task_authority")
    async with store.engine.connect() as conn:
        if store.backend == "sqlite":
            await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        rows = (
            await conn.execute(
                text(
                    "SELECT task_id, owner_domain, status, queue_revision, not_before_ms, "
                    "prepare_message_id, details_cleaned_at_ms FROM sorting_task ORDER BY task_id"
                )
            )
        ).all()
        assert rows == [
            ("A", "smt_auto", "COMPLETED", 1, None, None, 60),
            ("M", "smt_manual", "PREPARING", 3, 0, 101, None),
        ]
        assert (await conn.scalar(text("SELECT message_id FROM smt_auto_task_request"))) == 102

        def task_references(sync):
            schema = "enterprise_backend" if sync.dialect.name == "postgresql" else None
            return {
                (fk["referred_table"], tuple(fk["referred_columns"]))
                for fk in inspect(sync).get_foreign_keys("smt_auto_task_request", schema=schema)
            }

        assert await conn.run_sync(task_references) == {("sorting_task", ("auto_task_id",))}
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(
                    text("UPDATE smt_auto_task_request SET task_id = 'M' WHERE task_id = 'A'")
                )
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(
                    text(
                        "UPDATE sorting_task SET status = 'PREPARING', work_line_id = 1 "
                        "WHERE task_id = 'A'"
                    )
                )


@pytest.mark.parametrize(
    "state_migration",
    [
        ("TEST_HISTORY_MIGRATION_DATABASE_URL", backend, "xray_pending_due")
        for backend in ("sqlite", "postgres")
    ],
    indirect=True,
)
async def test_original_prepare_and_receipt_recover_after_cutover(state_migration):
    import json
    from datetime import UTC, datetime

    from sqlalchemy import MetaData, Table, func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.businesses._components.sorting_task.models import SortingTaskRecord
    from app.businesses._components.sorting_task.request.sorting_task_prepare_v1 import Prepare
    from app.businesses.runtime import build_runtime
    from app.businesses.smt_auto.models import AutoTaskRequest
    from app.businesses.smt_manual.models import ManualRequest
    from app.core.identifiers import uuid7
    from app.core.strict_json import canonical_digest
    from app.http_client import DeliveryState, HttpResult
    from app.integrations import history
    from app.integrations.callback_receipts import insert_receipt
    from app.integrations.wms_callback import receive
    from app.integrations.wms_request import ADAPTERS, DECISIONS_PROTOCOL
    from app.models import ReliableMessage, WorkLine
    from app.reliable import Outbox, ScopeRef
    from app.reliable.config import TargetConfig
    from app.reliable.store import AttemptOutcome, MessageStore
    from app.reliable.types import MessageStatus
    from tests.helpers import run_alembic
    from tests.migration_helpers import add_http_runtime_test_schema

    store = state_migration
    await add_http_runtime_test_schema(store.engine)
    factory = async_sessionmaker(store.engine, expire_on_commit=False)
    legacy_shapes = {
        name: (await store.schema_snapshot(name))[name]
        for name in ("smt_manual_task", "smt_auto_task")
    }
    target = TargetConfig("wms", "http://example.test", DECISIONS_PROTOCOL)
    targets = {target.name: target}
    runtime = build_runtime(
        outbox=Outbox(targets, ADAPTERS, records=history),
        ecs_commands=object(),
        session_factory=factory,
    )
    original_receipts, prepares = {}, {}
    async with factory.begin() as session:
        for mode in ("MANUAL", "AUTO"):
            owner = f"smt_{mode.lower()}"
            line = WorkLine(line_code=mode, line_name=mode, business_code=owner)
            session.add(line)
            await session.flush()
            payload = Prepare(task_id=mode, workline_code=mode)
            operation_id = str(uuid7())
            ref = await runtime.services[owner].capabilities.wms.send(
                session, payload, operation_id=operation_id, scope=ScopeRef(owner, "TASK", mode)
            )
            prepares[mode] = (operation_id, ref.message_id)
            issued = {
                "operation": "sorting.task.issued@v1",
                "operation_id": str(uuid7()),
                "timestamp": 1,
                "data": {
                    "task_id": mode,
                    "task_type": mode,
                    "business_type": "outbound",
                    "workline_code": mode,
                    "queue_revision": 1,
                    "dispatch_sequence": 1,
                },
            }
            raw = json.dumps(issued).encode()
            response = dict(
                operation_id=issued["operation_id"], code="RECEIVED", timestamp=123, data={}
            )
            await insert_receipt(
                session,
                operation=issued["operation"],
                operation_id=issued["operation_id"],
                raw_digest=canonical_digest(issued),
                effective_digest=canonical_digest(issued),
                compare_mode="EFFECTIVE",
                status_code=202,
                body=response,
                resp_timestamp=123,
                created_at=datetime.now(UTC),
                raw_body=raw,
                protocol="wms-callback",
                peer_system="WMS",
                owner_domain=owner,
            )
            original_receipts[mode] = (raw, response)

            def legacy_table(sync, owner=owner):
                return Table(
                    f"{owner}_task",
                    MetaData(),
                    schema="enterprise_backend" if sync.dialect.name == "postgresql" else None,
                    autoload_with=sync,
                )

            conn = await session.connection()
            table = await conn.run_sync(legacy_table)
            values = dict(
                task_id=mode,
                task_type=mode,
                business_type="outbound",
                work_line_id=line.id,
                workline_code=mode,
                issued_operation_id=issued["operation_id"],
                issued_timestamp=1,
                issued_data=issued["data"],
                queue_revision=1,
                dispatch_sequence=1,
                status="PREPARING",
                plan_revision=0,
            )
            request = dict(
                task_id=mode,
                operation="sorting.task.prepare@v1",
                operation_id=operation_id,
                message_id=ref.message_id,
            )
            if mode == "MANUAL":
                await session.execute(
                    table.insert().values(
                        **values,
                        prepare_operation_id=operation_id,
                        prepare_message_id=ref.message_id,
                        prepare_accepted=False,
                        received_work=[],
                    )
                )
                session.add(
                    ManualRequest(
                        **request,
                        kind="PREPARE",
                        work_line_id=line.id,
                        request_snapshot=payload.model_dump(),
                    )
                )
            else:
                await session.execute(table.insert().values(**values))
                await session.execute(
                    table.insert().values(
                        values
                        | dict(
                            task_id="ARCHIVED",
                            issued_operation_id=str(uuid7()),
                            business_type="inbound",
                            status="COMPLETED",
                            details_cleaned=True,
                            details_cleaned_at_ms=99,
                            closed_at_ms=20,
                            pending=False,
                            issued_data={},
                        )
                    )
                )
                session.add(
                    AutoTaskRequest(**request, purpose="PREPARE", request_data=payload.model_dump())
                )

    messages = MessageStore(factory, records=history)
    claims = await messages.claim(2, targets)
    assert len(claims) == 2
    for claim in claims:
        raw = json.dumps(
            dict(
                operation_id=claim.message.operation_id,
                code="PREPARE_ACCEPTED",
                timestamp=5,
                data={},
            )
        ).encode()
        response = HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 202, (), raw)
        verdict = ADAPTERS[DECISIONS_PROTOCOL].classify_response(claim.message, response)
        assert await messages.finish(
            claim, AttemptOutcome(MessageStatus.DONE, result=response, verdict=verdict)
        )

    await store.migrate("upgrade", "sorting_task_authority")
    assert await runtime.services["smt_manual"].advance("TASK", "MANUAL") == "PREPARING"
    async with factory.begin() as session:
        auto = runtime.services["smt_auto"]
        assert await auto.advance_prepare(session, task_id="AUTO", now_ms=20) == "PREPARING"
        manual = await session.get(SortingTaskRecord, "MANUAL")
        assert manual.prepare_accepted
        assert (manual.prepare_operation_id, manual.prepare_message_id) == prepares["MANUAL"]
        original = await session.scalar(
            select(AutoTaskRequest).where(AutoTaskRequest.task_id == "AUTO")
        )
        assert (original.operation_id, original.message_id) == prepares["AUTO"]
        assert original.consumed_at_ms == 20
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 2
        for raw, original_response in original_receipts.values():
            replay = await receive(session, raw, runtime=runtime)
            assert replay.status_code == 200
            assert replay.body == {**original_response, "code": "DUPLICATE"}
        reopened = json.loads(original_receipts["AUTO"][0])
        reopened["operation_id"] = str(uuid7())
        reopened["data"].update(task_id="ARCHIVED", business_type="inbound")
        refusal = await receive(session, json.dumps(reopened).encode(), runtime=runtime)
        assert refusal.status_code == 409
        assert refusal.body["data"] == {"reason_code": "STATE_CONFLICT"}
        assert (await session.get(SortingTaskRecord, "ARCHIVED")).status == "COMPLETED"

    url = store.engine.url.render_as_string(hide_password=False)
    for name, shape in legacy_shapes.items():
        assert (await store.schema_snapshot(name))[name] == shape
    divergent = await run_alembic(url, "downgrade", "xray_pending_due")
    assert divergent.returncode != 0
    assert "downgrade would lose data" in (divergent.stdout + divergent.stderr)

    rejected = await run_alembic(url, "upgrade", "sorting_task_cleanup")
    assert rejected.returncode != 0
    assert "sorting_task_cleanup_verified=true" in (rejected.stdout + rejected.stderr)
    cleanup = await run_alembic(
        url, "-x", "sorting_task_cleanup_verified=true", "upgrade", "sorting_task_cleanup"
    )
    assert cleanup.returncode == 0, cleanup.stdout + cleanup.stderr
    async with store.engine.connect() as conn:

        def table_names(sync):
            schema = "enterprise_backend" if sync.dialect.name == "postgresql" else None
            return set(inspect(sync).get_table_names(schema=schema))

        names = await conn.run_sync(table_names)
        assert "sorting_task" in names
        assert not ({"smt_manual_task", "smt_auto_task"} & names)
    async with factory.begin() as session:
        for raw, original_response in original_receipts.values():
            replay = await receive(session, raw, runtime=runtime)
            assert replay.body == {**original_response, "code": "DUPLICATE"}

    await store.migrate("downgrade", "xray_pending_due")
    for name, shape in legacy_shapes.items():
        assert (await store.schema_snapshot(name))[name] == shape
    async with store.engine.connect() as conn:
        manual = (
            await conn.execute(
                text(
                    "SELECT prepare_operation_id, prepare_message_id, prepare_accepted "
                    "FROM smt_manual_task WHERE task_id = 'MANUAL'"
                )
            )
        ).one()
        assert tuple(manual) == (*prepares["MANUAL"], True)
        auto = (
            await conn.execute(
                text(
                    "SELECT operation_id, message_id, consumed_at_ms "
                    "FROM smt_auto_task_request WHERE task_id = 'AUTO'"
                )
            )
        ).one()
        assert tuple(auto) == (*prepares["AUTO"], 20)
