"""Schema changes retain scans and refuse to discard original outbound evidence."""

import runpy
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.util import CommandError

from app.database import create_database_engine
from tests.helpers import run_alembic
from tests.migration_helpers import isolated_schema_database, source_table

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations/versions"
FIELDS = (
    "validation_operation_id",
    "validation_message_id",
    "validation_response",
    "binding_id",
    "conveyor_device_id",
    "conveyor_device_code",
    "conveyor_device_version",
    "command_code",
    "move_result",
)


@pytest.mark.parametrize("evidence", [None, *FIELDS])
def test_forward_migration_preserves_scans_and_guards_evidence(evidence):
    original = runpy.run_path(str(MIGRATIONS / "2026-10-29_coarse_sorting_scan.py"))
    migration = runpy.run_path(str(MIGRATIONS / "2026-10-29_coarse_sorting_move_forward.py"))
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                original["upgrade"]()
                table = sa.Table("coarse_sorting_scan", sa.MetaData(), autoload_with=connection)
                values = {
                    "id": "original",
                    "event_digest": "digest",
                    "receipt_id": 1,
                    "work_line_id": 1,
                    "workline_code": "COARSE-A",
                    "device_id": 1,
                    "device_code": "SCAN-A",
                    "device_role": "INPUT_ARM",
                    "device_version": 1,
                    "work_line_version": 1,
                    "binding_revision": "original",
                    "timestamp": 1,
                    "payload": {"barcode": "raw"},
                    "received_at": datetime(2026, 10, 29),
                    "pending": True,
                    "wait_reason": "AWAITING_VALIDATION_STAGE",
                }
                connection.execute(table.insert().values(**values))
                migration["upgrade"]()
                upgraded = sa.Table("coarse_sorting_scan", sa.MetaData(), autoload_with=connection)
                row = connection.execute(sa.select(upgraded)).mappings().one()
                assert {name: row[name] for name in values} == values
                assert all(row[field] is None for field in FIELDS)
                if evidence:
                    column = upgraded.c[evidence]
                    value = (
                        {}
                        if isinstance(column.type, sa.JSON)
                        else 1
                        if isinstance(column.type, sa.Integer)
                        else "frozen"
                    )
                    connection.execute(upgraded.update().values({evidence: value}))
                    with pytest.raises(CommandError, match="evidence.*destructive downgrade"):
                        migration["downgrade"]()
                    assert connection.execute(sa.select(column)).one()[0] == value
                else:
                    migration["downgrade"]()
                    restored = sa.Table(
                        "coarse_sorting_scan", sa.MetaData(), autoload_with=connection
                    )
                    assert connection.execute(sa.select(restored)).mappings().one() == values
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_job_migration_preserves_original_scan_and_action_identity(pg_sessions):
    engine = pg_sessions.kw["bind"]
    async with engine.connect() as connection, connection.begin() as transaction:

        def check(sync):
            schema = "enterprise_backend"
            assert sa.inspect(sync).has_table("coarse_sorting_job", schema=schema)
            migration = runpy.run_path(str(MIGRATIONS / "2026-11-06_coarse_sorting_job.py"))
            second = runpy.run_path(str(MIGRATIONS / "2026-11-06_coarse_sorting_ng.py"))
            third = runpy.run_path(str(MIGRATIONS / "2026-11-06_coarse_rack_preparation.py"))
            put_report = runpy.run_path(str(MIGRATIONS / "2026-11-24_coarse_put_failure.py"))
            with Operations.context(MigrationContext.configure(sync)):
                put_report["downgrade"]()
                third["downgrade"]()
                second["downgrade"]()
                migration["downgrade"]()
                legacy = sa.Table(
                    "coarse_sorting_scan", sa.MetaData(), schema=schema, autoload_with=sync
                )
                values = {
                    "id": "original",
                    "event_digest": "original-digest",
                    "receipt_id": 91,
                    "work_line_id": 1,
                    "workline_code": "COARSE-A",
                    "device_id": 1,
                    "device_code": "SCAN-A",
                    "device_role": "INPUT_ARM",
                    "device_version": 1,
                    "work_line_version": 1,
                    "binding_revision": "original",
                    "timestamp": 1,
                    "payload": {"barcode": "raw", "reel_diameter": 7, "reel_thickness": 20},
                    "received_at": datetime(2026, 10, 29),
                    "pending": True,
                    "wait_reason": "AWAITING_ALLOCATION_STAGE",
                    "validation_operation_id": "original-validation-id",
                    "validation_message_id": 7,
                    "validation_response": {"response_data": {"result": "OK", "binding_id": "B"}},
                    "binding_id": "B",
                    "conveyor_device_id": 2,
                    "conveyor_device_code": "CONVEYOR",
                    "conveyor_device_version": 1,
                    "command_code": "original-command",
                    "move_result": None,
                }
                sync.execute(legacy.insert().values(**values))
                sync.execute(
                    legacy.insert().values(
                        **{
                            **values,
                            "id": "missing-move",
                            "event_digest": "missing-move-digest",
                            "validation_operation_id": "missing-move-validation",
                            "validation_message_id": 8,
                            "wait_reason": "ORIGINAL_BINDING_CHANGED",
                            "command_code": None,
                        }
                    )
                )
                migration["upgrade"]()
                job = sa.Table(
                    "coarse_sorting_job", sa.MetaData(), schema=schema, autoload_with=sync
                )
                action = sa.Table(
                    "coarse_sorting_action", sa.MetaData(), schema=schema, autoload_with=sync
                )
                row = sync.execute(sa.select(job).where(job.c.id == "original")).mappings().one()
                assert row["event_digest"] == "original-digest"
                assert row["payload"] == values["payload"]
                assert row["binding_id"] == "B"
                assert row["state"] == "AWAITING_ALLOCATION"
                assert row["move_result"] is None
                actions = {
                    record["kind"]: record
                    for record in sync.execute(
                        sa.select(action).where(action.c.job_id == "original")
                    ).mappings()
                }
                validation = actions["VALIDATE_BIND"]
                assert validation["operation_id"] == "original-validation-id"
                assert validation["message_id"] == 7
                move = actions["MOVE_FORWARD"]
                assert move["predecessor_action_id"] == validation["id"]
                assert move["command_code"] == "original-command"
                assert move["call_key"] == "original:MOVE_FORWARD"
                assert move["evidence_history"] == []
                assert row["current_action_id"] == move["id"]

                missing = (
                    sync.execute(sa.select(job).where(job.c.id == "missing-move")).mappings().one()
                )
                assert missing["state"] == "AWAITING_MOVE"
                assert missing["command_code"] is None
                assert missing["binding_id"] == "B"
                assert sync.execute(
                    sa.select(action.c.kind).where(action.c.job_id == missing["id"])
                ).scalars().all() == ["VALIDATE_BIND"]

        await connection.run_sync(check)
        await transaction.rollback()


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_put_failure_migration_preserves_original_jobs_and_guards_evidence(
    backend, tmp_path, request
):
    from app.core.strict_json import canonical_digest

    previous = "ecs_request_projection_v1"
    now = datetime(2026, 11, 24, tzinfo=UTC)
    async with AsyncExitStack() as databases:
        if backend == "postgres":
            url, engine = await databases.enter_async_context(
                isolated_schema_database(request.getfixturevalue("pg_url"))
            )
        else:
            url = f"sqlite+aiosqlite:///{tmp_path / 'coarse-put-failure.db'}"
            engine = create_database_engine(url)
            databases.push_async_callback(engine.dispose)

        result = await run_alembic(url, "upgrade", previous)
        assert result.returncode == 0, result.stdout + result.stderr
        async with engine.begin() as connection:
            job = await source_table(connection, "coarse_sorting_job")
            action = await source_table(connection, "coarse_sorting_action")
            for number, (state, pending, wait_reason) in enumerate(
                (
                    ("AWAITING_PUT", False, "PUT_RECONCILIATION_REQUIRED"),
                    ("AWAITING_REPORT", True, "AWAITING_REPORT_RESULT"),
                    ("COMPLETED", False, "COMPLETED"),
                ),
                start=1,
            ):
                job_id, allocation_id, put_id = (
                    f"original-{name}-{number}" for name in ("job", "allocation", "put")
                )
                await connection.execute(
                    job.insert().values(
                        id=job_id,
                        event_digest=f"original-digest-{number}",
                        receipt_id=number,
                        work_line_id=1,
                        workline_code="COARSE-A",
                        device_id=1,
                        device_code="SCAN-A",
                        device_role="SCAN1",
                        device_version=1,
                        work_line_version=1,
                        binding_revision="original",
                        timestamp=number,
                        payload={"barcode": f"original-barcode-{number}"},
                        received_at=now,
                        pending=pending,
                        wait_reason=wait_reason,
                    )
                )
                original_action = action.insert().values(
                    job_id=job_id, round_no=1, request_data={}, evidence_history=[], created_at=now
                )
                await connection.execute(
                    original_action.values(
                        id=allocation_id,
                        kind="ALLOCATE_STORAGE",
                        rack_generation_no=0,
                        operation="inbound.coarse_sorting.storage_allocate@v1",
                        operation_id=f"original-operation-{number}",
                        message_id=number,
                    )
                )
                result = {
                    "command_code": f"original-command-{number}",
                    "device_code": "OUTPUT-A",
                    "result": "SUCCESS",
                    "finish_time": number,
                    "data": {"actual_qty": 1, "location": "ORIGINAL-DROP"},
                    "error_detail": None,
                }
                proof = {
                    "applied_put": {
                        "owner_domain": "coarse_sorting",
                        "call_key": f"original-call-{number}",
                        "command_code": result["command_code"],
                        "task_type": "PUT_TO_BIN",
                        "device_code": result["device_code"],
                    },
                    "command_code": result["command_code"],
                    "digest": canonical_digest(result),
                    "data": result,
                }
                await connection.execute(
                    original_action.values(
                        id=put_id,
                        kind="PUT_TO_BIN",
                        predecessor_action_id=allocation_id,
                        command_owner="coarse_sorting",
                        call_key=f"original-call-{number}",
                        command_code=result["command_code"],
                        device_code=result["device_code"],
                        resource_snapshot={
                            "device_code": result["device_code"],
                            "role": "OUTPUT_ARM",
                            "completion_location": "ORIGINAL-DROP",
                        },
                        evidence_history=[] if state == "AWAITING_PUT" else [proof],
                    )
                )
                current_id = put_id
                if state != "AWAITING_PUT":
                    current_id = f"original-report-{number}"
                    await connection.execute(
                        original_action.values(
                            id=current_id,
                            kind="REPORT_COMPLETION",
                            predecessor_action_id=put_id,
                            operation="inbound.coarse_sorting.execution_report@v1",
                            operation_id=f"original-report-operation-{number}",
                            message_id=number + 10,
                        )
                    )
                await connection.execute(
                    job.update()
                    .where(job.c.id == job_id)
                    .values(
                        state=state,
                        current_action_id=current_id,
                        allocation_action_id=allocation_id,
                        placed_at_ms=number if state != "AWAITING_PUT" else None,
                        completed_at=now if state == "COMPLETED" else None,
                    )
                )
            original = (
                (await connection.execute(sa.select(job).order_by(job.c.id))).mappings().all()
            )
            original_actions = (
                (await connection.execute(sa.select(action).order_by(action.c.id))).mappings().all()
            )

        for number in (2, 3):
            put_id = f"original-put-{number}"
            (saved,) = next(row for row in original_actions if row["id"] == put_id)[
                "evidence_history"
            ]
            contradictory = {
                **saved["data"],
                "data": {**saved["data"]["data"], "pick_and_put_result": "PUT_FAILED"},
            }
            proof = {**saved, "data": contradictory, "digest": canonical_digest(contradictory)}
            put_action = action.update().where(action.c.id == put_id)
            async with engine.begin() as connection:
                await connection.execute(put_action.values(evidence_history=[proof]))
            refused = await run_alembic(url, "upgrade", "coarse_put_failure_v1")
            assert refused.returncode != 0, refused.stdout + refused.stderr
            assert f"original-job-{number}" in refused.stdout + refused.stderr
            async with engine.begin() as connection:
                preserved_job = await source_table(connection, "coarse_sorting_job")
                assert "put_result" not in preserved_job.c
                assert "report_status" not in preserved_job.c
                assert (
                    await connection.execute(sa.select(job).order_by(job.c.id))
                ).mappings().all() == original
                assert await connection.scalar(
                    sa.select(action.c.evidence_history).where(action.c.id == put_id)
                ) == [proof]
                await connection.execute(put_action.values(evidence_history=[saved]))

        result = await run_alembic(url, "upgrade", "coarse_put_failure_v1")
        assert result.returncode == 0, result.stdout + result.stderr
        async with engine.begin() as connection:
            current_job = await source_table(connection, "coarse_sorting_job")
            current = (
                (await connection.execute(sa.select(current_job).order_by(current_job.c.id)))
                .mappings()
                .all()
            )
            assert (
                await connection.execute(sa.select(job).order_by(job.c.id))
            ).mappings().all() == original
            assert [(row["put_result"], row["report_status"]) for row in current] == [
                (None, None),
                ("SUCCESS", "PENDING"),
                ("SUCCESS", "COMPLETED"),
            ]
            assert (
                await connection.execute(sa.select(action).order_by(action.c.id))
            ).mappings().all() == original_actions
            pending_report = current_job.update().where(current_job.c.id == "original-job-2")
            await connection.execute(pending_report.values(current_action_id="original-put-2"))

        refused = await run_alembic(url, "downgrade", previous)
        assert refused.returncode != 0
        assert "PUT report has no original action" in refused.stdout + refused.stderr
        async with engine.begin() as connection:
            await connection.execute(pending_report.values(current_action_id="original-report-2"))
        downgrade = await run_alembic(url, "downgrade", previous)
        assert downgrade.returncode == 0, downgrade.stdout + downgrade.stderr
        async with engine.connect() as connection:
            assert (
                await connection.execute(sa.select(job).order_by(job.c.id))
            ).mappings().all() == original
        upgrade = await run_alembic(url, "upgrade", "coarse_put_failure_v1")
        assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr

        async with engine.begin() as connection:
            fail_put = (
                current_job.update()
                .where(current_job.c.id == "original-job-1")
                .values(
                    state="AWAITING_REPORT",
                    put_result="FAILED",
                    report_status="PENDING",
                    pending=True,
                )
            )
            await connection.execute(fail_put)
            for changes, check in (
                ({"current_action_id": None}, "allocation"),
                ({"allocation_action_id": None}, "allocation"),
                ({"completed_at": now}, "completion"),
                ({"placed_at_ms": 1}, "placement"),
                ({"put_result": None}, "put_result"),
                ({"put_result": "UNKNOWN"}, "put_result"),
                ({"put_result": "SUCCESS"}, "placement"),
                ({"report_status": None}, "report_status"),
                ({"report_status": "COMPLETED"}, "report_status"),
                ({"state": "COMPLETED", "completed_at": now}, "report_status"),
            ):
                with pytest.raises(
                    sa.exc.IntegrityError, match=f"coarse_sorting_job_{check}_check"
                ):
                    async with connection.begin_nested():
                        await connection.execute(fail_put.values(**changes))

        refused = await run_alembic(url, "downgrade", previous)
        assert refused.returncode != 0
        assert "failed PUT evidence exists" in refused.stdout + refused.stderr
        async with engine.begin() as connection:
            await connection.execute(
                fail_put.values(
                    state="COMPLETED", report_status="COMPLETED", pending=False, completed_at=now
                )
            )
        refused = await run_alembic(url, "downgrade", previous)
        assert refused.returncode != 0
        assert "failed PUT evidence exists" in refused.stdout + refused.stderr
        async with engine.connect() as connection:
            closed = (
                (
                    await connection.execute(
                        sa.select(current_job).where(current_job.c.id == "original-job-1")
                    )
                )
                .mappings()
                .one()
            )
            assert (closed["state"], closed["put_result"], closed["report_status"]) == (
                "COMPLETED",
                "FAILED",
                "COMPLETED",
            )
            assert closed["completed_at"] is not None
            assert closed["placed_at_ms"] is None
