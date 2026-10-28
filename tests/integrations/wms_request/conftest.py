from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import create_database_engine
from app.integrations import discovery
from app.integrations.wms_request import (
    ADAPTERS,
    DECISIONS_PROTOCOL,
    FACTS_PROTOCOL,
    Endpoint,
    RequestPayload,
    WmsRequests,
    request_operation,
)
from app.integrations.wms_request import operation as registry
from app.models import Base
from app.reliable import Outbox
from app.reliable.config import TargetConfig
from app.reliable.testing import FakeClock


class Report(RequestPayload):
    task_id: str
    qty: int


class Decision(Report):
    pass


class TimedReport(Report):
    pass


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    """注册表是模块级全局状态;每个测试从空表开始并登记自己的示例 operation。"""
    monkeypatch.setattr(registry, "_registry", {})
    # 发现函数带缓存且导入即注册:在被替换的注册表里首次调用会让真实 operation 永久丢失
    monkeypatch.setattr(discovery, "load_formal_modules", lambda **kwargs: None)
    request_operation("example.fact.report@v1", Endpoint.FACTS)(Report)
    request_operation("example.decision.ask@v1", Endpoint.DECISIONS)(Decision)
    request_operation("example.fact.timed@v1", Endpoint.FACTS, ttl=timedelta(minutes=5))(
        TimedReport
    )


@pytest.fixture
async def sessions():
    engine = create_database_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def wms(clock):
    targets = {
        "wms-facts": TargetConfig("wms-facts", "http://example.test", FACTS_PROTOCOL),
        "wms-decisions": TargetConfig("wms-decisions", "http://example.test", DECISIONS_PROTOCOL),
    }
    return WmsRequests(Outbox(targets, ADAPTERS, clock=clock), clock=clock)
