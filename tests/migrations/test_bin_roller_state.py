import pytest
from sqlalchemy import inspect

from app.businesses._components.bin_roller.models import TABLES


@pytest.mark.parametrize(
    "state_migration",
    [("BIN_ROLLER_MIGRATION_DATABASE_URL", b) for b in ("sqlite", "postgres")],
    indirect=True,
)
async def test_component_tables_and_unique_constraints_are_migrated(state_migration):
    async with state_migration.engine.connect() as connection:

        def check(sync):
            inspector = inspect(sync)
            schema = "enterprise_backend" if sync.dialect.name == "postgresql" else None
            names = inspector.get_table_names(schema=schema)
            assert {n for n in names if n.startswith("bin_roller_")} == {
                t.name for t in TABLES.values()
            }
            for table in TABLES.values():
                assert {c["name"] for c in inspector.get_columns(table.name, schema=schema)} == set(
                    table.c.keys()
                )
                actual = {
                    tuple(c["column_names"])
                    for c in inspector.get_unique_constraints(table.name, schema=schema)
                }
                expected = {
                    tuple(c.name for c in item.columns)
                    for item in table.constraints
                    if item.__class__.__name__ == "UniqueConstraint"
                }
                assert actual == expected
                columns = inspector.get_columns(table.name, schema=schema)
                assert {c["name"]: c["nullable"] for c in columns} == {
                    c.name: c.nullable for c in table.c
                }
                assert tuple(
                    inspector.get_pk_constraint(table.name, schema=schema)["constrained_columns"]
                ) == tuple(table.primary_key.columns.keys())
                indexes = inspector.get_indexes(table.name, schema=schema)
                assert {
                    i["name"]: (tuple(i["column_names"]), bool(i["unique"]))
                    for i in indexes
                    if not i.get("duplicates_constraint")
                } == {i.name: (tuple(i.columns.keys()), bool(i.unique)) for i in table.indexes}
                assert {
                    c["name"] for c in inspector.get_check_constraints(table.name, schema=schema)
                } == {
                    c.name for c in table.constraints if c.__class__.__name__ == "CheckConstraint"
                }

        await connection.run_sync(check)
