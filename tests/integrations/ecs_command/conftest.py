from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import create_database_engine
from app.integrations.ecs_command import PROTOCOL_RETRY_WINDOW
from app.integrations.ecs_command.routing import RouteAuthorization, RouteSnapshot
from app.models import Base
from tests.helpers import EcsMove, EcsOther


class Routes:
    origin = "http://ecs.test"
    authorization = RouteAuthorization.ALLOW
    calls = 0

    async def resolve_declared(self, session, device_code, declaration):
        self.calls += 1
        return RouteSnapshot(
            device_code, self.origin, "route-1", "v1", "test", "1", ("MOVE",), PROTOCOL_RETRY_WINDOW
        )

    async def authorize_frozen(self, session, route):
        return self.authorization


@pytest.fixture(params=["sqlite", "postgres"])
async def ecs(request):
    from app.integrations.ecs_command import compile_commands
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
    service = EcsCommands(outbox, "ecs", routes, compile_commands(EcsMove)[1])
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
                    delete(ReliableMessage).where(
                        ReliableMessage.target.in_(("ecs", "old-static", "ecs-local"))
                    )
                )
                await session.execute(
                    delete(CallbackReceipt).where(CallbackReceipt.operation == "ecs.result")
                )
        await engine.dispose()


class Reference:
    """Test-only caller transactions; no production device directory or business model."""

    def __init__(self, ecs, routes, actions, current_business):
        self.ecs, self.owner = ecs, ecs.owner
        self.routes, self.actions = routes, actions
        self.current_business = current_business

    async def resolve_declared(self, session, device_code, declaration):
        task_type = declaration.bindings[0].task_type
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
            PROTOCOL_RETRY_WINDOW,
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

    async def state(self, session, *, action_id=1, lock=False):
        query = select(self.actions).where(self.actions.c.id == action_id)
        if lock:
            query = query.with_for_update()
        return (await session.execute(query)).mappings().one_or_none()

    async def register(
        self,
        session,
        *,
        action_id,
        business_key="B1",
        device_code="D1",
        task_type="MOVE",
        timeout_ms=30000,
        params=None,
    ):
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        from app.reliable.lifecycle import ensure_savepoint_transaction

        params = {"qty": 1} if params is None else params
        row = await self.state(session, action_id=action_id)
        if row is None or row["command_code"] is None:
            fact = await self.current_business(session, business_key=business_key)
            if not fact or fact["can_register"] is not True or fact["device_code"] != device_code:
                raise ValueError("current reference permission required")
        else:
            fact = None
        await ensure_savepoint_transaction(session)
        async with session.begin_nested():
            if row is None:
                insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
                await session.execute(
                    insert(self.actions)
                    .values(
                        id=action_id,
                        owner_domain=self.owner.domain,
                        business_key=business_key,
                        device_code=fact["device_code"],
                        version=fact["version"],
                        state="READY",
                    )
                    .on_conflict_do_nothing(index_elements=["id"])
                )
            row = await self.state(session, action_id=action_id, lock=True)
            if row["owner_domain"] != self.owner.domain or row["business_key"] != business_key:
                raise ValueError("business identity has different input")
            if row["command_code"] is None:
                if row["state"] != "READY":
                    raise ValueError("current reference permission required")
            command = await self.ecs.service.create(
                session,
                owner=self.owner,
                call_key=f"action:{action_id}",
                device_code=device_code,
                timeout_ms=timeout_ms,
                command=(EcsMove if task_type == "MOVE" else EcsOther).model_validate(params),
            )
            if row["command_code"] is None:
                await session.execute(
                    update(self.actions)
                    .where(self.actions.c.id == action_id)
                    .values(
                        device_code=fact["device_code"],
                        version=fact["version"],
                        intent_device_code=fact["device_code"],
                        intent_version=fact["version"],
                        timeout_ms=timeout_ms,
                        task_type=task_type,
                        params=params,
                        command_code=command.command_code,
                        state="WAITING_RESULT",
                    )
                )
        return command

    async def consume(self, session, *, action_id):
        row = await self.state(session, action_id=action_id)
        if row is None or row["owner_domain"] != self.owner.domain:
            return False
        fact = await self.current_business(session, business_key=row["business_key"])
        row = await self.state(session, action_id=action_id, lock=True)
        if not fact or fact["can_register"] is not True or row["state"] != "WAITING_RESULT":
            return False
        if (
            row["device_code"] != row["intent_device_code"]
            or row["version"] != row["intent_version"]
            or fact["device_code"] != row["intent_device_code"]
            or fact["version"] != row["intent_version"]
        ):
            return False
        view = await self.ecs.service.get_command(
            session,
            owner=self.owner,
            command_code=row["command_code"],
        )
        if view is None or not view.result or view.hold_reason or view.needs_reconciliation:
            return False
        await session.execute(
            update(self.actions)
            .where(self.actions.c.id == action_id)
            .values(
                state="SUCCEEDED" if view.result["result"] == "SUCCESS" else "FAILED",
            )
        )
        return True

    async def change(self, session, *, action_id, kind):
        row = await self.state(session, action_id=action_id)
        if row is None or row["owner_domain"] != self.owner.domain:
            return False
        fact = await self.current_business(session, business_key=row["business_key"])
        row = await self.state(session, action_id=action_id, lock=True)
        if fact is None or row["state"] not in ("READY", "WAITING_RESULT"):
            return False
        values = {"version": row["version"] + 1}
        if kind == "cancel":
            values["state"] = "CANCELLED"
        elif kind == "rebind":
            values["device_code"] = "D2"
        elif kind != "version":
            raise ValueError("unknown business change")
        await session.execute(
            update(self.actions).where(self.actions.c.id == action_id).values(**values)
        )
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
    from sqlalchemy import JSON

    actions = Table(
        "ecs_reference_action",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("owner_domain", String, nullable=False),
        Column("business_key", String, nullable=False),
        Column("device_code", String, nullable=False),
        Column("version", Integer, nullable=False),
        Column("task_type", String),
        Column("timeout_ms", Integer),
        Column("params", JSON),
        Column("command_code", String, unique=True),
        Column("intent_device_code", String),
        Column("intent_version", Integer),
        Column("state", String, nullable=False),
        CheckConstraint("state IN ('READY','WAITING_RESULT','CANCELLED','SUCCEEDED','FAILED')"),
    )
    facts = {"B1": {"can_register": True, "device_code": "D1", "version": 1}}
    reads = []

    async def current_business(session, *, business_key):
        reads.append(business_key)
        return facts.get(business_key)

    async with ecs.engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    ref = Reference(ecs, routes, actions, current_business)
    ref.facts, ref.fact_reads = facts, reads
    try:
        async with ecs.factory.begin() as session:
            await ref.add_route(session, "A", effective=True)
            await session.execute(
                actions.insert().values(
                    id=1,
                    owner_domain=ecs.owner.domain,
                    business_key="B1",
                    state="READY",
                    device_code="D1",
                    version=1,
                )
            )
        ecs.service.routes = ref
        yield ref
    finally:
        async with ecs.engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)


@pytest.fixture
async def loopback_server():
    from tests.helpers import loopback_app

    return loopback_app


@pytest.fixture
def ecs_simulators(monkeypatch, loopback_server):
    from fastapi import FastAPI, Request

    import app.main as main
    from app.integrations.ecs_command.contracts import CommandEnvelope
    from tests.helpers import simulator_client_factory

    @asynccontextmanager
    async def peers(*, history_annexes=None):
        origins, hits, raw_requests = [], [], {}
        async with AsyncExitStack() as stack:
            for _ in range(2):
                peer = FastAPI()

                @peer.post("/api/v1/device/command")
                async def acknowledge(body: CommandEnvelope, request: Request):
                    hits.append(body.model_dump())
                    raw_requests[body.command_code] = await request.body()
                    return {"code": 200, "message": "ACK"}

                origins.append(await stack.enter_async_context(loopback_server(peer)))
            factory = simulator_client_factory(origins)
            configure = main.configure_local_ecs

            def assemble(application, *, environment, routes, annexes=None):
                return configure(
                    application,
                    environment=environment,
                    history_annexes=history_annexes,
                    client_factory=factory,
                    routes=routes,
                    annexes=annexes,
                )

            with monkeypatch.context() as patch:
                patch.setattr(main, "configure_local_ecs", assemble)
                yield SimpleNamespace(origins=origins, hits=hits, raw_requests=raw_requests)

    return peers
