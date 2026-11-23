"""Explicitly provisioned decision storage for the local random X-Ray Mock."""

import asyncio

from sqlalchemy import JSON, BigInteger, MetaData, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.database import SCHEMA
from app.models.base import NAMING_CONVENTION


class MockBase(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA, naming_convention=NAMING_CONVENTION)


class MockDecision(MockBase):
    __tablename__ = "xray_mock_decisions"

    operation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_digest: Mapped[str] = mapped_column(String(64))
    timestamp: Mapped[int] = mapped_column(BigInteger)
    data: Mapped[dict] = mapped_column(JSON)
    workline_code: Mapped[str] = mapped_column(String(50))
    original_barcode: Mapped[str | None] = mapped_column(Text)
    issued_spkg: Mapped[str | None] = mapped_column(String(24), unique=True)


async def initialize() -> None:
    from app.core import get_settings
    from app.core.database import engine

    settings = get_settings()
    if settings.ENVIRONMENT != "local" or not any(
        "random" in targets for targets in settings.XRAY_WMS_MOCK_TARGETS.values()
    ):
        raise SystemExit("X-Ray mock storage requires an enabled local random mock")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(MockBase.metadata.create_all)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(initialize())
