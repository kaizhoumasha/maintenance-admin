import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, func, select


def records(metadata, name="records"):
    return Table(
        name,
        metadata,
        Column("id", Integer, primary_key=True),
        Column("value", String, nullable=False),
    )


async def test_reused_schema_resets_committed_rows_sequences_and_added_tables(
    postgres_tables, pg_url
):
    metadata = MetaData()
    table = records(metadata)
    extra_metadata = MetaData()
    extra = records(extra_metadata, "added_records")
    async with postgres_tables(pg_url, "reset", metadata) as engine:
        async with engine.begin() as connection:
            await connection.run_sync(extra_metadata.create_all)
            assert (
                await connection.scalar(table.insert().values(value="first").returning(table.c.id))
                == 1
            )
            await connection.execute(extra.insert().values(value="added"))
        async with engine.connect() as observer:
            assert await observer.scalar(select(func.count()).select_from(table)) == 1

    async with postgres_tables(pg_url, "reset", metadata) as engine:
        async with engine.begin() as connection:
            assert await connection.scalar(select(func.count()).select_from(table)) == 0
            assert await connection.scalar(select(func.count()).select_from(extra)) == 0
            assert (
                await connection.scalar(table.insert().values(value="next").returning(table.c.id))
                == 1
            )


async def test_reset_preserves_rows_in_other_owned_schemas(postgres_tables, pg_url):
    metadata = MetaData()
    table = records(metadata)
    async with postgres_tables(pg_url, "untouched", metadata) as other:
        async with other.begin() as connection:
            await connection.execute(table.insert().values(value="keep"))
        async with postgres_tables(pg_url, "selected", metadata) as engine:
            async with engine.begin() as connection:
                await connection.execute(table.insert().values(value="remove"))
        async with postgres_tables(pg_url, "selected", metadata) as engine:
            async with engine.connect() as connection:
                assert await connection.scalar(select(func.count()).select_from(table)) == 0
        async with other.connect() as connection:
            assert await connection.scalar(select(table.c.value)) == "keep"


async def test_failed_case_cannot_leave_committed_rows_for_the_next_use(postgres_tables, pg_url):
    metadata = MetaData()
    table = records(metadata)
    with pytest.raises(RuntimeError, match="failed case"):
        async with postgres_tables(pg_url, "failure", metadata) as engine:
            async with engine.begin() as connection:
                await connection.execute(table.insert().values(value="committed before failure"))
            raise RuntimeError("failed case")
    async with postgres_tables(pg_url, "failure", metadata) as engine:
        async with engine.connect() as connection:
            assert await connection.scalar(select(func.count()).select_from(table)) == 0
