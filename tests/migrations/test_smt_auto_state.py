"""S1 migration shape and constraint coverage without formal business assembly."""

import pytest
from sqlalchemy import inspect

from app.businesses.smt_auto.models import TABLES


@pytest.mark.parametrize(
    "state_migration",
    [("SMT_AUTO_MIGRATION_DATABASE_URL", "sqlite", "rollerfour")],
    indirect=True,
)
async def test_smt_auto_migration_has_all_tables_constraints_and_indexes(state_migration):
    store = state_migration
    await store.migrate("upgrade", "head")
    async with store.engine.connect() as connection:

        def check(sync):
            schema = None if sync.dialect.name == "sqlite" else "enterprise_backend"
            inspector = inspect(sync)
            names = set(inspector.get_table_names(schema=schema))
            assert {table.name for table in TABLES.values()} <= names
            for table in TABLES.values():
                checks = {
                    item["name"]
                    for item in inspector.get_check_constraints(table.name, schema=schema)
                }
                expected = {
                    item.name
                    for item in table.constraints
                    if item.__class__.__name__ == "CheckConstraint"
                }
                assert expected <= checks
                unique = {
                    tuple(item["column_names"])
                    for item in inspector.get_unique_constraints(table.name, schema=schema)
                }
                expected_unique = {
                    tuple(column.name for column in item.columns)
                    for item in table.constraints
                    if item.__class__.__name__ == "UniqueConstraint"
                }
                assert expected_unique <= unique
                assert inspector.get_pk_constraint(table.name, schema=schema)[
                    "constrained_columns"
                ] == [column.name for column in table.primary_key.columns]
            indexes = {
                item["name"] for item in inspector.get_indexes("smt_auto_task", schema=schema)
            }
            assert {"smt_auto_task_queue", "smt_auto_task_execution_slot"} <= indexes

        await connection.run_sync(check)
