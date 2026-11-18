"""Operator-managed durable storage for the local coarse sorting Mock."""

import asyncio

from sqlalchemy import JSON, MetaData, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.database import SCHEMA
from app.models.base import NAMING_CONVENTION

from .contracts import parse_mock_rounds


class MockBase(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA, naming_convention=NAMING_CONVENTION)


class CoarseSortingMockRound(MockBase):
    __tablename__ = "coarse_sorting_mock_round"

    round_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    configuration_digest: Mapped[str] = mapped_column(String(64))
    state: Mapped[dict] = mapped_column(JSON)


async def initialize() -> None:
    from app.core import get_settings
    from app.core.database import engine

    settings = get_settings()
    if settings.ENVIRONMENT != "local" or not settings.COARSE_SORTING_WMS_MOCK:
        raise SystemExit("Coarse sorting Mock storage requires an enabled local round")
    rounds = parse_mock_rounds(settings.COARSE_SORTING_WMS_MOCK)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(MockBase.metadata.create_all)
            recorded = await connection.execute(
                select(
                    CoarseSortingMockRound.round_id, CoarseSortingMockRound.configuration_digest
                ).where(CoarseSortingMockRound.round_id.in_(rounds))
            )
            for round_id, digest in recorded:
                if digest != rounds[round_id].configuration_digest:
                    raise SystemExit(
                        f"Coarse sorting Mock round {round_id} owns a different configuration"
                    )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(initialize())
