from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.businesses._components.bin_roller.models import TABLES
from app.database import create_database_engine


def test_only_four_current_checkpoint_tables_exist():
    assert set(TABLES) == {"bin_passage", "bin_return", "face_work", "workline_state"}


async def test_current_return_and_scope_constraints(tmp_path):
    from sqlalchemy import MetaData, text

    metadata = MetaData()
    tables = {key: table.to_metadata(metadata, schema=None) for key, table in TABLES.items()}
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path / 'four.db'}")
    async with engine.begin() as connection:
        await connection.execute(text("PRAGMA foreign_keys=ON"))
        await connection.run_sync(metadata.create_all)
    factory = async_sessionmaker(engine)
    returns = tables["bin_return"]
    base = dict(
        owner_domain="owner",
        work_line_id=1,
        scope_id="line-run",
        bin_code="B1",
        scan3_event_id=1,
        scan3_visit_key="visit1",
        is_current=True,
        return_state="WAITING",
    )
    try:
        async with factory.begin() as session:
            await session.execute(returns.insert().values(id="r1", **base))
        async with factory() as session:
            with pytest.raises(IntegrityError):
                await session.execute(
                    returns.insert().values(
                        id="r2", **{**base, "scan3_event_id": 2, "scan3_visit_key": "visit2"}
                    )
                )
            await session.rollback()
        async with factory.begin() as session:
            await session.execute(
                returns.update()
                .where(returns.c.id == "r1")
                .values(closed_at=datetime.now(UTC), close_reason="TAKEN_OVER")
            )
            await session.execute(
                returns.insert().values(
                    id="r2", **{**base, "scan3_event_id": 2, "scan3_visit_key": "visit2"}
                )
            )
        async with factory() as session:
            with pytest.raises(IntegrityError):
                await session.execute(
                    returns.update().where(returns.c.id == "r2").values(return_state="MOVING")
                )
            await session.rollback()
        async with factory.begin() as session:
            await session.execute(
                returns.update()
                .where(returns.c.id == "r2")
                .values(
                    return_state="WAIT_FACT",
                    transport_task_id="t1",
                    client_request_id="c1",
                    caller={"workline_id": "1", "station_id": None},
                )
            )
    finally:
        await engine.dispose()
