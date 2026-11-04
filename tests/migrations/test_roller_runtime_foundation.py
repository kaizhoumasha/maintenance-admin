from sqlalchemy import MetaData, Table, inspect, select

from app.database import create_database_engine
from tests.helpers import run_alembic


async def test_foundation_adds_nullable_evidence_and_refuses_lossy_downgrade(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'foundation.db'}"
    for revision in ("sharedrackdispatch", "rollerfoundation"):
        result = await run_alembic(url, "upgrade", revision)
        assert result.returncode == 0, result.stdout + result.stderr
    engine = create_database_engine(url)
    try:
        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda c: inspect(c).get_columns("callback_receipt")
            )
            assert {c["name"] for c in columns if c["name"].startswith("event_")} == {
                "event_envelope",
                "event_context",
            }
        down = await run_alembic(url, "downgrade", "sharedrackdispatch")
        assert down.returncode == 0, down.stdout + down.stderr
        up = await run_alembic(url, "upgrade", "rollerfoundation")
        assert up.returncode == 0, up.stdout + up.stderr
        async with engine.begin() as connection:
            from datetime import UTC, datetime

            receipt = await connection.run_sync(
                lambda c: Table("callback_receipt", MetaData(), autoload_with=c)
            )
            await connection.execute(
                receipt.insert().values(
                    operation="ecs.event",
                    operation_id="a" * 64,
                    raw_digest="b" * 64,
                    compare_mode="RAW",
                    status_code=200,
                    body={"code": 200},
                    resp_timestamp=1,
                    created_at=datetime.now(UTC),
                    event_envelope={"original": True},
                )
            )
        refused = await run_alembic(url, "downgrade", "sharedrackdispatch")
        assert refused.returncode != 0
        async with engine.connect() as connection:
            assert await connection.scalar(select(receipt.c.event_envelope)) == {"original": True}
    finally:
        await engine.dispose()
