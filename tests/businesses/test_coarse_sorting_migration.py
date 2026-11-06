"""Schema changes retain scans and refuse to discard original outbound evidence."""

import runpy
from datetime import datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.util import CommandError

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
            with Operations.context(MigrationContext.configure(sync)):
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
