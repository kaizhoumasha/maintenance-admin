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
