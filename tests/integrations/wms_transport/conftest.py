import asyncio
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, event
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.identifiers import uuid7
from app.database import create_database_engine as create_async_engine
from app.integrations.wms_transport import (
    PROTOCOL,
    Rack,
    RackPosition,
    TransportCaller,
    TransportService,
    TransportSubmitAdapter,
)
from app.models import Base, Menu, ReliableMessage, TransportTask
from app.reliable import Outbox
from app.reliable.config import TargetConfig
from app.reliable.testing import FakeClock


def make_transport_rig(factory, engine):
    clock = FakeClock()
    target = TargetConfig(
        "transport-" + uuid7().hex,
        "http://example.test",
        PROTOCOL,
        max_not_received_attempts=3,
        fixed_retry_interval_s=1,
    )
    adapters = {PROTOCOL: TransportSubmitAdapter()}
    outbox = Outbox({target.name: target}, adapters, clock=clock)
    return SimpleNamespace(
        factory=factory,
        engine=engine,
        clock=clock,
        target=target,
        adapters=adapters,
        outbox=outbox,
        service=TransportService(outbox, target=target.name, clock=clock),
    )


@pytest.fixture
async def transport_rig():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)

    # SQLAlchemy 文档的 pysqlite 事务配方:由 SQLAlchemy 显式发 BEGIN,保存点才可靠。
    @event.listens_for(engine.sync_engine, "connect")
    def disable_driver_transactions(dbapi_connection, record):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def begin_explicitly(connection):
        connection.exec_driver_sql("BEGIN")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield make_transport_rig(async_sessionmaker(engine, expire_on_commit=False), engine)
    finally:
        await engine.dispose()


@pytest.fixture
async def pg_transport_rig(pg_sessions):
    rig = make_transport_rig(pg_sessions, pg_sessions.kw["bind"])
    try:
        yield rig
    finally:
        async with rig.factory() as session:
            await session.execute(
                delete(TransportTask).where(TransportTask.workline_id == rig.target.name)
            )
            await session.execute(
                delete(ReliableMessage).where(ReliableMessage.target == rig.target.name)
            )
            await session.execute(delete(Menu).where(Menu.name == rig.target.name))
            await session.commit()


@pytest.fixture(params=["transport_rig", "pg_transport_rig"])
def transport_storage(request):
    return request.getfixturevalue(request.param)


@pytest.fixture
def transport_request(transport_storage):
    return dict(
        client_request_id="01a13b00-cabd-7390-890a-0c093e7c07a1",
        caller=TransportCaller(transport_storage.target.name, "station"),
        rack_id="rack",
        source=Rack("rack"),
        target=RackPosition("destination"),
    )


@pytest.fixture
def transport_http_server():
    @asynccontextmanager
    async def serve(handler):
        tasks, writers, requests = set(), set(), []

        async def connected(reader, writer):
            task = asyncio.current_task()
            tasks.add(task)
            writers.add(writer)
            try:
                while True:
                    headers = await reader.readuntil(b"\r\n\r\n")
                    length = next(
                        int(line.split(b":", 1)[1])
                        for line in headers.split(b"\r\n")
                        if line.lower().startswith(b"content-length:")
                    )
                    body = await reader.readexactly(length)
                    requests.append((headers, body))
                    await handler(body, writer, len(requests))
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writers.discard(writer)
                writer.close()
                with suppress(ConnectionError):
                    await writer.wait_closed()
                tasks.discard(task)

        server = await asyncio.start_server(connected, "127.0.0.1", 0)
        try:
            yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", requests
        finally:
            server.close()
            for writer in tuple(writers):
                writer.close()
            pending = tuple(tasks)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await server.wait_closed()

    return serve
