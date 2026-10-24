from datetime import timedelta
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update
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


class Reference:
    """Test-only caller transactions; no production device directory or business model."""

    def __init__(self, ecs, tables):
        self.ecs = ecs
        self.routes, self.business, self.calls, self.consumptions = tables

    async def resolve(self, session, device_code, task_type):
        result = await session.execute(
            select(self.routes)
            .where(
                self.routes.c.device == device_code,
                self.routes.c.effective.is_(True),
                self.routes.c.revoked.is_(False),
            )
            .with_for_update()
        )
        rows = result.mappings().all()
        if len(rows) != 1 or task_type != "MOVE":
            raise ValueError("one authorized effective route required")
        row = rows[0]
        return RouteSnapshot(
            device_code,
            row["origin"],
            row["id"],
            "1",
            "test",
            "1",
            ("MOVE",),
            timedelta(minutes=5),
        )

    async def authorize_frozen(self, session, route):
        result = await session.execute(
            select(self.routes).where(self.routes.c.id == route["route_id"]).with_for_update()
        )
        row = result.mappings().one_or_none()
        if row is None or route["version"] != "1":
            return RouteAuthorization.UNKNOWN
        return RouteAuthorization.REVOKED if row["revoked"] else RouteAuthorization.ALLOW

    async def add_route(self, session, name, *, effective):
        await session.execute(
            self.routes.insert().values(
                id=name,
                device="D1",
                origin=f"http://{name.lower()}.test",
                effective=effective,
                revoked=False,
            )
        )

    async def switch_route(self, session):
        await session.execute(
            update(self.routes).where(self.routes.c.id == "A").values(effective=False, revoked=True)
        )
        await session.execute(
            update(self.routes).where(self.routes.c.id == "B").values(effective=True)
        )

    async def state(self, session, *, lock=False):
        query = select(self.business).where(self.business.c.id == 1)
        if lock:
            query = query.with_for_update()
        return (await session.execute(query)).mappings().one()

    async def register(self, session, *, key="call"):
        state = await self.state(session, lock=True)
        prior = (
            (await session.execute(select(self.calls).where(self.calls.c.call_key == key)))
            .mappings()
            .one_or_none()
        )
        if prior is None and (state["state"] != "READY" or state["binding"] != "D1"):
            raise ValueError("current reference permission required")
        command = await self.ecs.service.create(
            session,
            owner=self.ecs.owner,
            call_key=key,
            device_code="D1",
            task_type="MOVE",
            timeout_ms=30000,
            params={"qty": 1},
        )
        if prior is None:
            await session.execute(
                self.calls.insert().values(
                    call_key=key,
                    command_code=command.command_code,
                    binding=state["binding"],
                    version=state["version"],
                )
            )
        return command

    async def consume(self, session, code, *, owner=None):
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        state = await self.state(session, lock=True)
        view = await self.ecs.service.get_command(
            session,
            owner=owner or self.ecs.owner,
            command_code=code,
        )
        call = (
            (await session.execute(select(self.calls).where(self.calls.c.command_code == code)))
            .mappings()
            .one_or_none()
        )
        if (
            view is None
            or not view.result
            or view.needs_reconciliation
            or view.hold_reason
            or call is None
            or state["state"] != "READY"
            or state["binding"] != call["binding"]
            or state["version"] != call["version"]
        ):
            return False
        insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
        winner = await session.scalar(
            insert(self.consumptions)
            .values(
                consumer="reference",
                command_code=code,
            )
            .on_conflict_do_nothing()
            .returning(self.consumptions.c.command_code)
        )
        if winner is None:
            return False
        await session.execute(
            update(self.business)
            .where(self.business.c.id == 1)
            .values(
                state="SUCCEEDED" if view.result["result"] == "SUCCESS" else "FAILED",
            )
        )
        return True

    async def change(self, session, kind):
        state = await self.state(session, lock=True)
        if state["state"] != "READY":
            return False
        values = {"version": state["version"] + 1}
        if kind == "cancel":
            values["state"] = "CANCELLED"
        elif kind == "rebind":
            values["binding"] = "D2"
        await session.execute(update(self.business).where(self.business.c.id == 1).values(**values))
        return True

    @staticmethod
    def result_body(code, result="SUCCESS", *, finish_time=1):
        body = dict(
            command_code=code,
            device_code="D1",
            result=result,
            finish_time=finish_time,
            data={"qty": 1},
        )
        if result == "FAILED":
            body["error_detail"] = {"code": "TEST_FAILURE", "msg": "test failure"}
        return body

    async def apply_result(self, code, result="SUCCESS", *, finish_time=1):
        import json

        from app.integrations.ecs_command.lifecycle import EcsLifecycle
        from app.integrations.ecs_command.protocol import PROTOCOL
        from app.integrations.ecs_command.result import receive_ecs_result
        from app.reliable.store import MessageStore

        store = MessageStore(
            self.ecs.factory, lifecycles={PROTOCOL: EcsLifecycle(self.ecs.service)}
        )
        claims = await store.claim(1, self.ecs.targets)
        if claims:
            assert await store.before_send(claims[0], self.ecs.targets["ecs"])
        body = self.result_body(code, result, finish_time=finish_time)
        async with self.ecs.factory.begin() as session:
            return await receive_ecs_result(session, json.dumps(body).encode(), self.ecs.service)


@pytest.fixture
async def reference(ecs):
    from sqlalchemy import Boolean, CheckConstraint, Column, Integer, MetaData, String, Table

    metadata = MetaData()
    routes = Table(
        "ecs_reference_route",
        metadata,
        Column("id", String, primary_key=True),
        Column("device", String, nullable=False),
        Column("origin", String, nullable=False),
        Column("effective", Boolean, nullable=False),
        Column("revoked", Boolean, nullable=False),
    )
    business = Table(
        "ecs_reference_business",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("state", String, nullable=False),
        Column("binding", String, nullable=False),
        Column("version", Integer, nullable=False),
        CheckConstraint("state IN ('READY','CANCELLED','SUCCEEDED','FAILED')"),
    )
    calls = Table(
        "ecs_reference_call",
        metadata,
        Column("call_key", String, primary_key=True),
        Column("command_code", String, nullable=False, unique=True),
        Column("binding", String, nullable=False),
        Column("version", Integer, nullable=False),
    )
    consumptions = Table(
        "ecs_reference_consumption",
        metadata,
        Column("consumer", String, primary_key=True),
        Column("command_code", String, primary_key=True),
    )
    async with ecs.engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    ref = Reference(ecs, (routes, business, calls, consumptions))
    try:
        async with ecs.factory.begin() as session:
            await ref.add_route(session, "A", effective=True)
            await session.execute(
                business.insert().values(id=1, state="READY", binding="D1", version=1)
            )
        ecs.service.routes = ref
        yield ref
    finally:
        async with ecs.engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)


@pytest.fixture
async def loopback_server():
    import asyncio
    import socket
    from contextlib import asynccontextmanager

    import uvicorn

    @asynccontextmanager
    async def serve(app):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                loop="asyncio",
                lifespan="off",
                log_level="critical",
                access_log=False,
                ws="none",
            )
        )
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        await task
                        raise RuntimeError("loopback server exited before startup")
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            try:
                await asyncio.wait_for(task, 5)
            finally:
                sock.close()

    return serve
