from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.identifiers import uuid7
from app.database import create_database_engine as create_async_engine
from app.http_client import HttpRequest
from app.integrations import history as history_records
from app.reliable import MessageDraft, Verdict, VerdictKind
from app.reliable.config import TargetConfig
from app.reliable.testing import FakeClock
from tests.helpers import run_alembic
from tests.migration_helpers import isolated_schema_database


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def pg_url(pg_url):
    # Maintenance scans the whole store, including rows belonging to other targets.
    async with isolated_schema_database(pg_url) as (url, _):
        result = await run_alembic(url, "upgrade", "head")
        assert result.returncode == 0, result.stdout + result.stderr
        yield url


def make_rig(factory, engine, name):
    from app.reliable import Outbox
    from app.reliable.store import MessageStore

    clock = FakeClock()
    target = TargetConfig(name, "http://example.test", "script")
    adapter = ScriptedAdapter()
    targets, adapters = {name: target}, {"script": adapter}
    return SimpleNamespace(
        engine=engine,
        factory=factory,
        clock=clock,
        target=target,
        targets=targets,
        adapter=adapter,
        adapters=adapters,
        outbox=Outbox(targets, adapters, clock=clock, records=history_records),
        store=MessageStore(
            factory, clock=clock, uniform=lambda low, high: high, records=history_records
        ),
        draft=lambda identity="id", **kw: MessageDraft(
            name, "opaque", identity, kw.pop("request_raw", b"\x00opaque"), **kw
        ),
    )


class ScriptedAdapter:
    def __init__(self):
        self.builds = []
        self.verdict = None
        self.changes = {}
        self.error = None

    def build_request(self, message, target):
        self.builds.append(message)
        if self.error:
            raise self.error
        values = {
            "method": "POST",
            "path": "/opaque",
            "body": message.request_raw,
            "headers": (("X-Operation-Id", message.operation_id),),
        }
        return HttpRequest(**(values | self.changes))

    def classify_response(self, message, result):
        return self.verdict or Verdict(VerdictKind.DELIVERED, response=result.body)


@pytest.fixture
async def rig(tmp_path):
    from app.models import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'reliable.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield make_rig(factory, engine, "peer")
    finally:
        await engine.dispose()


@pytest.fixture
async def pg_rig(pg_sessions):
    from app.models import ReliableMessage

    rig = make_rig(pg_sessions, pg_sessions.kw["bind"], "r-" + uuid7().hex)
    try:
        yield rig
    finally:
        async with pg_sessions() as session:
            await session.execute(
                delete(ReliableMessage).where(ReliableMessage.target == rig.target.name)
            )
            await session.commit()


@pytest.fixture(params=["rig", "pg_rig"])
def storage(request):
    return request.getfixturevalue(request.param)


@pytest.fixture
def delivered():
    from app.http_client import DeliveryState, HttpResult
    from app.reliable.store import AttemptOutcome
    from app.reliable.types import MessageStatus

    return AttemptOutcome(
        MessageStatus.DONE,
        result=HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 200, (), b"ok"),
        verdict=Verdict(VerdictKind.DELIVERED, response=b"ok"),
    )
