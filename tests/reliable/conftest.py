import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.http_client import HttpRequest
from app.reliable import MessageDraft, Verdict, VerdictKind
from app.reliable.config import TargetConfig
from app.reliable.testing import FakeClock


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
        outbox=Outbox(targets, adapters, clock=clock),
        store=MessageStore(factory, clock=clock, uniform=lambda low, high: high),
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
async def rig():
    from app.models.reliable import ReliableMessage

    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(ReliableMessage.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield make_rig(factory, engine, "peer")
    finally:
        await engine.dispose()


@pytest.fixture
async def pg_rig(pg_sessions):
    from app.models import ReliableMessage

    rig = make_rig(pg_sessions, pg_sessions.kw["bind"], "r-" + uuid.uuid4().hex)
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
