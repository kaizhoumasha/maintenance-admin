from datetime import timedelta
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import create_database_engine
from app.integrations.ecs_command.routing import RouteAuthorization, RouteSnapshot
from app.models import Base


class Payload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    qty: int


class Routes:
    origin = "http://ecs.test"
    authorization = RouteAuthorization.ALLOW
    calls = 0

    async def resolve(self, session, device_code, task_type):
        self.calls += 1
        return RouteSnapshot(
            device_code, self.origin, "route-1", "v1", "test", "1", ("MOVE",), timedelta(minutes=5)
        )

    async def authorize_frozen(self, session, route):
        return self.authorization


@pytest.fixture(params=["sqlite", "postgres"])
async def ecs(request):
    from app.integrations.ecs_command.contracts import DeviceAnnex, ErrorDetail
    from app.integrations.ecs_command.protocol import PROTOCOL, EcsCommandAdapter
    from app.integrations.ecs_command.service import CommandOwner, EcsCommands
    from app.reliable import Outbox
    from app.reliable.config import TargetConfig

    if request.param == "postgres":
        engine = create_database_engine(request.getfixturevalue("pg_url"))
        factory = async_sessionmaker(engine, expire_on_commit=False)
    else:
        engine = create_database_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
    if request.param == "postgres":
        from sqlalchemy import delete

        from app.models import ReliableMessage

        async with factory.begin() as session:
            await session.execute(
                delete(ReliableMessage).where(ReliableMessage.target == "old-static")
            )
    target = TargetConfig("ecs", None, PROTOCOL, address_mode="SNAPSHOT")
    targets = {"ecs": target}
    adapters = {PROTOCOL: EcsCommandAdapter()}
    routes = Routes()
    outbox = Outbox(targets, adapters)
    service = EcsCommands(
        outbox, "ecs", routes, {("test", "1"): DeviceAnnex(Payload, Payload, ErrorDetail, 1, 60000)}
    )
    from app.integrations.ecs_command.lifecycle import EcsLifecycle

    outbox.lifecycles = {PROTOCOL: EcsLifecycle(service)}
    try:
        yield SimpleNamespace(
            engine=engine,
            factory=factory,
            service=service,
            owner=CommandOwner("test"),
            routes=routes,
            outbox=outbox,
            targets=targets,
            adapters=adapters,
        )
    finally:
        if request.param == "postgres":
            from sqlalchemy import delete

            from app.models import (
                CallbackReceipt,
                EcsCommand,
                EcsResultEvidence,
                EcsSendEvidence,
                ReliableMessage,
            )

            async with factory.begin() as session:
                for model in (EcsSendEvidence, EcsResultEvidence, EcsCommand):
                    await session.execute(delete(model))
                await session.execute(
                    delete(ReliableMessage).where(ReliableMessage.target.in_(("ecs", "old-static")))
                )
                await session.execute(
                    delete(CallbackReceipt).where(CallbackReceipt.operation == "ecs.result")
                )
        await engine.dispose()
