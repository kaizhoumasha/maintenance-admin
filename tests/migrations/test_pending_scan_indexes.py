import pytest
from sqlalchemy import inspect

INDEXES = {
    "smt_auto_bin_work": {
        "smt_auto_bin_work_pending_idx",
        "smt_auto_bin_work_release_idx",
    },
    "smt_auto_inbound_plate": {
        "smt_auto_inbound_plate_pending_idx",
        "smt_auto_inbound_plate_report_idx",
    },
    "smt_auto_outbound_plate": {
        "smt_auto_outbound_plate_pending_idx",
        "smt_auto_outbound_plate_report_idx",
    },
    "smt_auto_outbound_source_member": {"smt_auto_outbound_source_member_terminal_idx"},
    "coarse_sorting_job": {"coarse_sorting_job_pending_due_idx"},
}


@pytest.mark.parametrize(
    "state_migration",
    [("SMT_AUTO_MIGRATION_DATABASE_URL", backend) for backend in ("sqlite", "postgres")],
    indirect=True,
    ids=["sqlite", "postgres"],
)
async def test_pending_scan_indexes_exist(state_migration):
    async with state_migration.engine.connect() as connection:

        def names(sync):
            inspector = inspect(sync)
            schema = "enterprise_backend" if sync.dialect.name == "postgresql" else None
            return {
                table: {item["name"] for item in inspector.get_indexes(table, schema=schema)}
                for table in INDEXES
            }

        actual = await connection.run_sync(names)

        def coarse_index(sync):
            schema = "enterprise_backend" if sync.dialect.name == "postgresql" else None
            return next(
                item
                for item in inspect(sync).get_indexes("coarse_sorting_job", schema=schema)
                if item["name"] == "coarse_sorting_job_pending_due_idx"
            )

        coarse = await connection.run_sync(coarse_index)

    for table, expected in INDEXES.items():
        assert expected <= actual[table]
    if state_migration.engine.dialect.name == "sqlite":
        assert str(coarse["dialect_options"]["sqlite_where"]).strip() == "pending IS 1"
