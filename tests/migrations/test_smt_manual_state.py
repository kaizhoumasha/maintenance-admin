import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.businesses.smt_manual.models import TABLES


@pytest.mark.parametrize(
    "state_migration",
    [("SMT_MANUAL_MIGRATION_DATABASE_URL", b, "0177313880f9") for b in ("sqlite", "postgres")],
    indirect=True,
)
async def test_manual_state_matches_models_and_preserves_platform_data(state_migration):
    store = state_migration
    original_schema = await store.schema_snapshot("smt_manual_")
    assert len(original_schema) == 13
    await store.migrate("upgrade", "head")
    async with store.engine.connect() as connection:

        def check(sync):
            inspector = inspect(sync)
            schema = "enterprise_backend" if sync.dialect.name == "postgresql" else None
            names = inspector.get_table_names(schema=schema)
            assert all(table.name in names for table in TABLES.values())
            for table in TABLES.values():
                assert {c["name"] for c in inspector.get_columns(table.name, schema=schema)} == set(
                    table.c.keys()
                )
                assert {
                    c["name"]: c["nullable"]
                    for c in inspector.get_columns(table.name, schema=schema)
                } == {c.name: c.nullable for c in table.c}
                assert {
                    c["name"] for c in inspector.get_check_constraints(table.name, schema=schema)
                } == {
                    constraint.name
                    for constraint in table.constraints
                    if constraint.__class__.__name__ == "CheckConstraint"
                }
                assert inspector.get_pk_constraint(table.name, schema=schema)[
                    "constrained_columns"
                ] == [c.name for c in table.primary_key.columns]
                assert {
                    tuple(c["column_names"])
                    for c in inspector.get_unique_constraints(table.name, schema=schema)
                } == {
                    tuple(c.name for c in item.columns)
                    for item in table.constraints
                    if item.__class__.__name__ == "UniqueConstraint"
                }
            indexes = inspector.get_indexes(TABLES["task"].name, schema=schema)
            assert any(i["name"] == "sorting_task_execution_slot" and i["unique"] for i in indexes)

        await connection.run_sync(check)
    async with store.engine.begin() as connection:
        table = TABLES["task"]
        values = dict(
            task_type="MANUAL",
            work_line_id=-99,
            workline_code="M5_SLOT",
            issued_timestamp=1,
            issued_data={},
            queue_revision=1,
            dispatch_sequence=1,
        )
        await connection.execute(
            table.insert().values(
                **values,
                task_id="M5_SLOT1",
                business_type="inbound",
                issued_operation_id="slot1",
                status="PREPARING",
            )
        )
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(
                    table.insert().values(
                        **values,
                        task_id="M5_SLOT2",
                        business_type="outbound",
                        issued_operation_id="slot2",
                        status="EXECUTING",
                    )
                )
        await connection.execute(table.delete().where(table.c.task_id == "M5_SLOT1"))
    await store.migrate("downgrade", "0177313880f9")
    assert await store.schema_snapshot("smt_manual_") == original_schema
    await store.migrate("downgrade", "a39a1988a81a")
    async with store.engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO work_lines (line_code,line_name,business_code,is_enabled) "
                "VALUES ('M5_PRESERVE','Original','smt_manual',false)"
            )
        )
    try:
        await store.migrate("upgrade", "ac980eb32c52")
        async with store.engine.connect() as connection:

            def component_only(sync):
                schema = "enterprise_backend" if sync.dialect.name == "postgresql" else None
                names = inspect(sync).get_table_names(schema=schema)
                assert "bin_roller_scan_arrival" in names
                assert "smt_manual_task" not in names

            await connection.run_sync(component_only)
        await store.migrate("upgrade", "head")
        async with store.engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT line_name,is_enabled FROM work_lines WHERE line_code='M5_PRESERVE'"
                    )
                )
            ).one()
            assert row.line_name == "Original" and not row.is_enabled
            assert (
                len(
                    (
                        await connection.execute(text("SELECT version_num FROM alembic_version"))
                    ).all()
                )
                == 1
            )
    finally:
        async with store.engine.begin() as connection:
            await connection.execute(text("DELETE FROM work_lines WHERE line_code='M5_PRESERVE'"))
