"""跨测试文件共享的辅助函数(夹具放 conftest.py,这里放普通函数)。"""

import base64
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from app.core.security import create_access_token
from app.integrations.ecs_command import ecs_command
from app.integrations.ecs_command.contracts import ErrorDetail

PAYLOAD = {"username": "kai", "email": "k@example.com", "password": "password123"}


LOGIN = {"username": "root", "password": "password123"}


METADATA_ONLY_GUARD = """
import importlib.abc
import socket
import httpx
import redis
import redis.asyncio
import sqlalchemy
import sqlalchemy.ext.asyncio
from unittest.mock import patch

def forbidden(*args, **kwargs):
    raise AssertionError("runtime resource construction or network connection")

class BlockRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith((
            "app.core.database", "app.core.redis", "app.core.deps",
            "app.core.logger", "app.main", "app.controllers", "app.tasks",
            "app.reliable_runtime", "app.businesses.runtime",
        )):
            raise AssertionError("runtime import: " + fullname)
        if not globals().get("allow_ecs_definition_imports", False) and fullname.startswith((
            "app.integrations.ecs_command.runtime", "app.integrations.ecs_command.roles",
            "app.integrations.ecs_command.dev",
        )):
            raise AssertionError("runtime import: " + fullname)

sys.meta_path.insert(0, BlockRuntime())
def block_execution(frame, event, arg):
    if event != "call":
        return
    module = frame.f_globals.get("__name__") or ""
    name = frame.f_code.co_name
    if (module, name) in {
        ("app.integrations.ecs_command.runtime", "configure_ecs"),
        ("app.integrations.ecs_command.dev", "configure_local_ecs"),
        ("app.integrations.ecs_command.roles", "__init__"),
        ("app.integrations.ecs_command.service", "__init__"),
        ("app.integrations.ecs_command.dev", "__init__"),
    } or (module.startswith(("app.businesses.", "app.integrations.legacy."))
          and name in {"build_service", "handle"}) or (
        module.startswith("app.") and name.startswith("replay")
    ):
        forbidden()
sys.setprofile(block_execution)
for target, name in (
    (sqlalchemy, "create_engine"),
    (sqlalchemy.engine, "create_engine"),
    (sqlalchemy.ext.asyncio, "create_async_engine"),
    (sqlalchemy.engine.Engine, "__init__"),
    (sqlalchemy.MetaData, "create_all"),
    (sqlalchemy.Table, "create"),
    (httpx.Client, "__init__"),
    (httpx.AsyncClient, "__init__"),
    (redis.Redis, "__init__"),
    (redis.asyncio.Redis, "__init__"),
    (socket.socket, "connect"),
    (socket.socket, "connect_ex"),
    (socket, "create_connection"),
):
    patch.object(target, name, forbidden).start()

import app.database
app.database.create_database_engine = forbidden
from app.core import config
config.get_settings = forbidden
from app.businesses import catalog
catalog.load_business_models = forbidden
"""


def write_python_modules(root, modules):
    """Write real temporary package files for isolated import/discovery tests."""
    for relative, source in modules.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)


def run_isolated_module(module, arguments, *, cwd, bootstrap="", environment=None):
    """Run a module once with an explicit project path and no inherited app settings."""
    project_root = Path(__file__).resolve().parents[1]
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys\n"
            f"sys.path.insert(0, {str(project_root)!r})\n"
            + bootstrap
            + f"\nsys.argv = {[module, *arguments]!r}\n"
            + f"import runpy\nrunpy.run_module({module!r}, run_name='__main__')\n",
        ],
        cwd=cwd,
        env={"PATH": os.environ["PATH"], **(environment or {})},
        capture_output=True,
        text=True,
        timeout=30,
    )


async def ecs_binding(session, device_code):
    """Explicit local binding DTO for receiver contract tests; no database lookup."""
    from app.integrations.ecs_callback.binding import DeviceBinding

    role = "EXAMPLE_SCAN" if device_code == "LOCAL_EXAMPLE_SCAN" else "SCAN1"
    return DeviceBinding(
        device_id=1,
        device_code=device_code,
        device_version=1,
        work_line_id=1,
        work_line_version=1,
        device_role=role,
        device_bindings={role: device_code},
        position_bindings={},
    )


async def legacy_ecs_receipt(factory, event, *, created_at=None):
    """Persist an authentic pre-upgrade identity, independently of the current envelope."""
    from datetime import UTC, datetime

    from app.core.strict_json import canonical_digest
    from app.models import CallbackReceipt

    normalized = {
        "device_code": event["device_code"],
        "event_type": event["event_type"],
        "timestamp": event["timestamp"],
        "is_debug": event.get("is_debug", False),
        "data": event.get("data", {}),
    }
    async with factory.begin() as session:
        receipt = CallbackReceipt(
            operation="ecs.event",
            operation_id=canonical_digest(normalized),
            raw_digest=canonical_digest(event),
            compare_mode="RAW",
            status_code=200,
            body={"code": 200, "message": "ACK"},
            diagnostic=[
                {
                    "reason_code": "UNREGISTERED_EVENT_TYPE",
                    "device_code": event["device_code"],
                    "event_type": event["event_type"],
                    "data": normalized,
                }
            ],
            resp_timestamp=1,
            created_at=created_at or datetime.now(UTC),
        )
        session.add(receipt)


async def add_bound_device(session, code, origin, *, role="SCAN1", line_code=None):
    from app.core.identifiers import uuid7
    from app.models import Device, DeviceRoute, WorkLine

    line = WorkLine(
        line_code=line_code or "T" + uuid7().hex,
        line_name="Test line",
        device_bindings={role: code},
        position_bindings={"INLET": "P1"},
    )
    route = DeviceRoute(id=str(uuid7()), device_code=code, ecs_origin=origin)
    session.add_all([line, route])
    await session.flush()
    device = Device(
        device_code=code, display_name=code, current_route_id=route.id, work_line_id=line.id
    )
    session.add(device)
    await session.flush()
    return line, device, route


def ecs_status_document(*codes):
    """Fresh Status wire document shared by protocol and debug-HTTP tests."""
    return {
        "devices": [
            {
                "device": {
                    "device_code": code,
                    "device_name": "分拣机械臂",
                    "device_type": "ROBOTIC_ARM",
                    "role": "SORTER_ARM",
                    "supported_commands": ["PICK_AND_PUT"],
                    "supported_events": ["SCAN_COMPLETED"],
                },
                "state": {
                    "device_code": code,
                    "mode": "AUTO",
                    "status": "OFFLINE",
                    "is_online": False,
                    "current_command_code": None,
                    "scenario": "error",
                    "updated_at": 1793029419104,
                },
            }
            for code in codes or ("ARM03",)
        ]
    }


async def login(client, creds=LOGIN):
    """登录并返回 (access_token, refresh cookie 值)。"""
    r = await client.post("/api/v1/auth/login", json=creds)
    assert "refresh_token" not in r.json()  # refresh 只在 HttpOnly cookie 里
    return r.json()["access_token"], client.cookies.get("refresh_token")


def use_refresh(client, token):
    """让 client 之后只带指定的 refresh cookie(模拟另一台设备/旧 token)。"""
    client.cookies.clear()
    if token:
        client.cookies.set("refresh_token", token, path="/api/v1/auth")


async def refresh(client, token):
    use_refresh(client, token)
    return await client.post("/api/v1/auth/refresh")


def bearer(user_id: int | str) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(str(user_id))}"}


async def make_user_with_perms(client, admin, perms):
    """建一个只拥有 perms 的用户(经 API 走一遍 角色→权限→用户),返回其 id。"""
    role = (
        await client.post("/api/v1/roles", json={"name": "r", "permissions": perms}, headers=admin)
    ).json()
    uid = (await client.post("/api/v1/users", json=PAYLOAD, headers=admin)).json()["id"]
    r = await client.put(
        f"/api/v1/users/{uid}/roles", json={"role_ids": [role["id"]]}, headers=admin
    )
    assert r.status_code == 204
    return uid


def raw_cursor(payload: Any) -> str:
    """按对外约定(base64url、无填充的 JSON)手工构造游标,用来造非法或边界输入。"""
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


class EcsResultPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    qty: int


@ecs_command(
    "MOVE",
    device_roles=("SCAN1",),
    annex_id="test",
    annex_version="1",
    result=EcsResultPayload,
    error=ErrorDetail,
    timeout_min_ms=1,
    timeout_max_ms=60000,
)
class EcsMove(EcsResultPayload):
    pass


@ecs_command(
    "OTHER",
    device_roles=("SCAN1",),
    annex_id="other",
    annex_version="1",
    result=EcsResultPayload,
    error=ErrorDetail,
    timeout_min_ms=1,
    timeout_max_ms=60000,
)
class EcsOther(EcsResultPayload):
    pass


class TrialPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    qty: int


ecs_command(
    "MOVE",
    device_roles=("SCAN1",),
    annex_id="trial",
    annex_version="1",
    result=TrialPayload,
    error=ErrorDetail,
    timeout_min_ms=1,
    timeout_max_ms=60000,
)(TrialPayload)


def ecs_history_annexes():
    from app.integrations.ecs_command import compile_commands

    return compile_commands(TrialPayload)[1]


def ecs_protocol_annexes():
    from app.integrations.ecs_command.dev import PROTOCOL_ANNEX, PROTOCOL_MODELS

    return {PROTOCOL_ANNEX: PROTOCOL_MODELS}


def reset_ecs_assembly(app, monkeypatch):
    for key, value in (
        ("ecs_commands", None),
        ("reliable_lifecycles", {}),
        ("reliable_client_provider", None),
        (
            "reliable_adapters",
            {k: v for k, v in app.state.reliable_adapters.items() if k != "ecs-command"},
        ),
    ):
        monkeypatch.setattr(app.state, key, value, raising=False)


def simulator_client_factory(origins):
    from app.http_client import AsyncHttpClient
    from app.integrations.ecs_command.dev import debug_origin
    from app.reliable.client_provider import PoolUnavailable

    allowed = frozenset(debug_origin(origin) for origin in origins)

    def factory(config):
        if debug_origin(config.base_url) not in allowed:
            raise PoolUnavailable("Test ECS destination is outside this run")
        return AsyncHttpClient(config)

    return factory


@asynccontextmanager
async def simulator_sender(ecs, origins):
    from app.integrations.ecs_command.clients import DynamicClients
    from app.integrations.ecs_command.lifecycle import EcsLifecycle
    from app.integrations.ecs_command.protocol import PROTOCOL
    from app.reliable.sender import Sender
    from app.reliable.store import MessageStore

    sender = Sender(
        MessageStore(ecs.factory, lifecycles={PROTOCOL: EcsLifecycle(ecs.service)}),
        ecs.targets,
        ecs.adapters,
        {},
        provider=DynamicClients(factory=simulator_client_factory(origins)),
    )
    try:
        yield sender
    finally:
        await sender.aclose()


@asynccontextmanager
async def loopback_app(app):
    import asyncio
    import socket

    import uvicorn

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


async def result_task(factory, *, bins=False, cycle_id=None, caller=None):
    """Create a local outbox obligation; never construct a sender or HTTP client."""
    from types import SimpleNamespace

    from app.core.identifiers import uuid7
    from app.integrations.wms_transport import (
        ADAPTERS,
        PROTOCOL,
        BinMove,
        HandoffPosition,
        RackBinSlot,
        RackPosition,
        TransportCaller,
        TransportService,
    )
    from app.reliable import Outbox
    from app.reliable.config import TargetConfig

    caller = caller or TransportCaller("result-" + str(uuid7()), "station")
    cycle_id = cycle_id or str(uuid7())
    target = TargetConfig("result-" + str(uuid7()), "http://example.test", PROTOCOL)
    service = TransportService(Outbox({target.name: target}, ADAPTERS), target=target.name)
    async with factory.begin() as session:
        args = dict(client_request_id=str(uuid7()), caller=caller, cycle_id=cycle_id)
        if bins:
            handle = await service.move_bins(
                session,
                **args,
                moves=[
                    BinMove("C1", HandoffPosition("SCAN"), RackBinSlot("R1", "90", "S1")),
                    BinMove("C2", HandoffPosition("SCAN2"), RackBinSlot("R2", "90", "S2")),
                ],
            )
        else:
            handle = await service.move_rack(
                session,
                **args,
                rack_id="R1",
                source=RackPosition("FROM"),
                target=RackPosition("TO"),
                target_face="90",
            )
    return SimpleNamespace(
        service=service, handle=handle, caller=caller, cycle_id=cycle_id, target=target.name
    )


async def transport_callback(
    factory, task, *, revision=1, known=True, bins=False, owned_operations=None
):
    """Apply real strict callbacks in independent transactions, including required placement."""
    from app.core.identifiers import uuid7
    from app.integrations.wms_callback import receive

    task_id = task.handle.transport_task_id
    operation_ids = []

    async def send(operation, data):
        operation_id = str(uuid7())
        operation_ids.append(operation_id)
        if owned_operations is not None:
            owned_operations.add(operation_id)
        raw = json.dumps(
            dict(operation=operation, operation_id=operation_id, timestamp=1, data=data)
        ).encode()
        async with factory.begin() as session:
            response = await receive(session, raw)
            assert response.status_code == 202, response

    if bins:
        position = dict(kind="RACK_BIN_SLOT", rack_id="R1", rack_face="90", slot_id="S1")
        if known:
            await send(
                "transport.task.member_position_changed@v1",
                dict(
                    transport_task_id=task_id,
                    container_id="C1",
                    milestone="TARGET_PLACED",
                    final_position=position,
                ),
            )
        first = (
            dict(container_id="C1", status="SUCCEEDED", final_position=position)
            if known
            else dict(
                container_id="C1",
                status="FAILED",
                position_unknown=True,
                failure_code="POSITION_UNKNOWN",
            )
        )
        data = dict(
            transport_task_id=task_id,
            kind="BIN_MOVE",
            outcome_revision=revision,
            results=[
                first,
                dict(
                    container_id="C2",
                    status="FAILED",
                    position_unknown=True,
                    failure_code="POSITION_UNKNOWN",
                ),
            ],
        )
    else:
        data = dict(
            transport_task_id=task_id,
            kind="RACK_MOVE",
            rack_id="R1",
            outcome_revision=revision,
            status="SUCCEEDED",
            final_position=dict(kind="RACK_POSITION", location_code="TO"),
        )
    await send("transport.task.resulted@v1", data)
    return operation_ids


async def deliver_transport_callback(factory, operation, data):
    from app.core.identifiers import uuid7
    from app.integrations.wms_callback import receive

    raw = json.dumps(
        dict(operation=operation, operation_id=str(uuid7()), timestamp=1, data=data)
    ).encode()
    async with factory.begin() as session:
        response = await receive(session, raw)
        assert response.status_code == 202, response


def business_composition(code="test_sorting", role="TEST_SCAN"):
    """Metadata-only test business; loading entries or constructing services must fail."""
    from types import ModuleType

    from app.businesses.definitions import BusinessDefinition, DeviceRole, InitialEntry

    entry = ModuleType(f"app.businesses.{code}.composition")
    entry.DEFINITION = BusinessDefinition(
        code=code,
        display_name="测试业务",
        device_roles=(DeviceRole(role_key=role, display_name="测试扫码"),),
        initial_entries=(InitialEntry(kind="ecs_event", name="SCAN_COMPLETED", device_role=role),),
    )
    entry.ENTRY_MODULES = (f"app.businesses.{code}.ecs_event",)
    entry.COMMANDS = ()

    def build_service(capabilities):
        raise AssertionError("listing must not call business factories")

    entry.build_service = build_service
    return entry


def workline_business_definition(code="test_sorting"):
    from app.businesses.definitions import (
        BusinessDefinition,
        DeviceRole,
        InitialEntry,
        PositionSlot,
    )

    return BusinessDefinition(
        code=code,
        display_name="测试分拣",
        device_roles=(
            DeviceRole(role_key="SCAN1", display_name="扫码"),
            DeviceRole(role_key="SCAN2", display_name="可选扫码", required=False),
        ),
        position_slots=(PositionSlot(slot_key="INLET", display_name="入口"),),
        initial_entries=(
            InitialEntry(kind="ecs_event", name="SCAN_COMPLETED", device_role="SCAN1"),
        ),
    )


def workline_definition_lookup(code):
    if code not in ("test_sorting", "other_sorting"):
        raise LookupError(code)
    return workline_business_definition(code)


class FlowPredecessor(BaseModel):
    """Only the test business owns this dependency contract."""

    model_config = ConfigDict(strict=True, extra="forbid")
    task_id: str
    request_id: str
    caller_workline: str
    caller_station: str | None
    cycle_id: str
    rack_id: str = "R1"


class FlowWmsRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    message_id: int
    operation: str
    operation_id: str


class FlowAcceptance(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    execution_id: str
    device_code: str
    predecessors: list[FlowPredecessor] = []
    wms_request: FlowWmsRequest | None = None

    @model_validator(mode="after")
    def unique_predecessors(self):
        ids = [ref.task_id for ref in self.predecessors]
        if len(ids) != len(set(ids)):
            raise ValueError("each predecessor task identity must occur once")
        return self


class FlowScanA(FlowAcceptance):
    token: str


class FlowScanB(FlowAcceptance):
    count: int


def flow_request(flow, *, index=0, kind="ecs", business=None, execution_id=None, predecessors=()):
    from app.core.identifiers import uuid7

    code = business or ("seam_b" if index == 2 else "seam_a")
    data = dict(
        execution_id=execution_id or str(uuid7()),
        device_code=flow.codes[index],
        predecessors=[
            dict(
                task_id=t.handle.transport_task_id,
                request_id=t.handle.client_request_id,
                caller_workline=t.caller.workline_id,
                caller_station=t.caller.station_id,
                cycle_id=t.cycle_id,
                rack_id="R1",
            )
            for t in predecessors
        ],
        **({"count": 7} if code == "seam_b" else {"token": "A"}),
    )
    if kind == "ecs":
        return "/api/v1/callback/event", dict(
            device_code=flow.codes[index], event_type="SCAN_COMPLETED", timestamp=1, data=data
        )
    return "/api/v1/wms/events", dict(
        operation=f"{code}.accept@v1", operation_id=str(uuid7()), timestamp=1, data=data
    )


class FlowService:
    """Concrete test-only business; session factory is injected, active sessions never retained."""

    def __init__(self, code, definition, capabilities, factory, table):
        self.code, self.definition = code, definition
        self.capabilities, self.factory, self.table = capabilities, factory, table

    async def accept(self, ctx, payload):
        from sqlalchemy import select

        from app.integrations.wms_callback import Accepted, Duplicate, Rejected
        from app.services.device_bindings import resolve_binding
        from app.services.workline_service import business_configuration_issue

        binding = (
            ctx.binding
            if hasattr(ctx, "binding")
            else await resolve_binding(ctx.session, payload.device_code)
        )
        issue = await business_configuration_issue(
            ctx.session, binding, self.code, definition_lookup=lambda _: self.definition
        )
        if issue:
            ctx.note(issue)
            return Rejected(issue)
        existing = await ctx.session.scalar(
            select(self.table.c.id).where(self.table.c.id == payload.execution_id)
        )
        if existing:
            return Duplicate()
        # The receiver owns this transaction, including its original receipt and ACK.
        await ctx.session.execute(
            self.table.insert().values(
                id=payload.execution_id,
                business=self.code,
                receipt_id=ctx.receipt_id,
                device_code=binding.device_code,
                device_id=binding.device_id,
                device_role=binding.device_role,
                line_id=binding.work_line_id,
                accepted_payload=payload.model_dump(mode="json"),
                pending=True,
                done=False,
                touch=0,
            )
        )
        return Accepted()

    async def pending_execution_ids(self):
        """A rebuilt concrete service discovers its own durable pending identities."""
        from sqlalchemy import select

        async with self.factory() as session:
            return tuple(
                await session.scalars(
                    select(self.table.c.id)
                    .where(self.table.c.business == self.code, self.table.c.pending.is_(True))
                    .order_by(self.table.c.id)
                )
            )

    async def advance(self, execution_id, *, attempts=3, hook=None):
        from sqlalchemy import select, text
        from sqlalchemy.exc import DBAPIError

        from app.integrations.ecs_callback.binding import DeviceBinding
        from app.integrations.ecs_command import CommandOwner
        from app.integrations.wms_transport import TransportCaller
        from app.models import Device, WorkLine
        from app.services.workline_service import business_configuration_issue

        table = self.table
        for attempt in range(attempts):
            try:
                async with self.factory() as session, session.begin():
                    if session.get_bind().dialect.name == "postgresql":
                        await session.connection(
                            execution_options={"isolation_level": "REPEATABLE READ"}
                        )
                        assert await session.scalar(text("SHOW transaction_isolation")) == (
                            "repeatable read"
                        )
                    if hook:
                        await hook("opened", attempt, session, None)
                    located = (
                        (await session.execute(select(table).where(table.c.id == execution_id)))
                        .mappings()
                        .one()
                    )
                    if hook:
                        await hook("located", attempt, session, None)
                    # Do not move an accepted execution with its device.
                    line = await session.scalar(
                        select(WorkLine)
                        .where(WorkLine.id == located["line_id"])
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                    device = await session.scalar(
                        select(Device)
                        .where(Device.id == located["device_id"])
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                    if (
                        line is None
                        or device is None
                        or device.device_code != located["device_code"]
                        or device.work_line_id != located["line_id"]
                        or line.device_bindings.get(located["device_role"])
                        != located["device_code"]
                    ):
                        return "ORIGINAL_BINDING_CHANGED"
                    binding = DeviceBinding(
                        device_id=device.id,
                        device_code=device.device_code,
                        device_version=device.version,
                        work_line_id=line.id,
                        work_line_version=line.version,
                        device_role=located["device_role"],
                        device_bindings=dict(line.device_bindings),
                        position_bindings=dict(line.position_bindings),
                    )
                    issue = await business_configuration_issue(
                        session, binding, self.code, definition_lookup=lambda _: self.definition
                    )
                    if issue:
                        return issue
                    row = (
                        (
                            await session.execute(
                                select(table).where(table.c.id == execution_id).with_for_update()
                            )
                        )
                        .mappings()
                        .one()
                    )
                    if row["business"] != self.code:
                        raise ValueError("execution belongs to another business")
                    if row["done"]:
                        return "done"
                    evidence, closed = {}, True
                    scopes = {}
                    for ref in row["accepted_payload"]["predecessors"]:
                        scope = (
                            ref["caller_workline"],
                            ref["caller_station"],
                            ref["cycle_id"],
                            ref["rack_id"],
                        )
                        scopes.setdefault(scope, []).append(ref)
                    for (workline, station, cycle, rack), refs in scopes.items():
                        caller = TransportCaller(workline, station)
                        for offset in range(0, len(refs), 100):
                            part = refs[offset : offset + 100]
                            if len(part) == 1:
                                ref = part[0]
                                result = await self.capabilities.transport.get_result(
                                    session,
                                    transport_task_id=ref["task_id"],
                                    client_request_id=ref["request_id"],
                                    caller=caller,
                                )
                                closed &= result.availability == "FOUND"
                            ids = tuple(ref["task_id"] for ref in part)
                            group = await self.capabilities.transport.list_unclosed_predecessors(
                                session,
                                caller=caller,
                                cycle_id=cycle,
                                predecessor_ids=ids,
                                rack_id=rack,
                            )
                            assert group.requested_ids == ids
                            for ref, item in zip(part, group.items, strict=True):
                                evidence[ref["task_id"]] = dict(
                                    revision=item.outcome_revision,
                                    members=list(item.member_ids),
                                    closure=item.closure,
                                    task_closure=item.task_closure,
                                    basis=item.closure_basis,
                                )
                                closed &= item.client_request_id == ref["request_id"]
                            closed &= group.complete and not group.unresolved
                            if hook:
                                await hook("predecessor_part", attempt, session, evidence)
                    request = row["accepted_payload"].get("wms_request")
                    if request:
                        response = await self.capabilities.wms.get_result(session, **request)
                        evidence["wms"] = dict(
                            **request,
                            availability=response.availability,
                            code=response.response_code,
                            data=response.response_data,
                        )
                        closed &= (
                            response.availability == "FOUND"
                            and response.response_state == "VALID"
                            and response.response_code == "DECIDED"
                            and response.response_data.get("allow") is True
                        )
                    if hook:
                        await hook("read", attempt, session, evidence)
                    if not closed:
                        return "waiting"
                    position = binding.position_bindings["INLET"]
                    command = await self.capabilities.ecs.create(
                        session,
                        owner=CommandOwner("public-seam-test"),
                        call_key=execution_id,
                        device_code=binding.device_code,
                        timeout_ms=100,
                        command=FlowMove(position=position),
                    )
                    await session.execute(
                        table.update()
                        .where(table.c.id == execution_id)
                        .values(
                            done=True,
                            pending=False,
                            evidence=evidence,
                            next_intent=dict(
                                command_code=command.command_code,
                                device_code=binding.device_code,
                                position=position,
                            ),
                        )
                    )
                    if hook:
                        await hook("written", attempt, session, evidence)
                if hook:
                    await hook("committed", attempt, session, None)
                return "advanced"
            except DBAPIError as error:
                if getattr(error.orig, "sqlstate", None) != "40001":
                    raise
        return "retry_later"


class FlowMove(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    position: str


ecs_command(
    "MOVE",
    device_roles=("SEAM_A", "SEAM_B"),
    annex_id="seam",
    annex_version="1",
    result=FlowMove,
    error=ErrorDetail,
    timeout_min_ms=1,
    timeout_max_ms=1000,
)(FlowMove)


@asynccontextmanager
async def business_flow_fixture(factory, monkeypatch):
    """Own only this temporary table; production Base and Alembic stay untouched."""
    from types import MappingProxyType, SimpleNamespace

    from sqlalchemy import JSON, Boolean, Column, Integer, MetaData, String, Table, delete, select

    from app.businesses import catalog
    from app.businesses.components import ComponentDefinition
    from app.businesses.definitions import (
        BusinessDefinition,
        DeviceRole,
        InitialEntry,
        PositionSlot,
    )
    from app.businesses.runtime import build_runtime
    from app.database import SCHEMA
    from app.integrations.ecs_callback import registry as ecs
    from app.integrations.ecs_command import CommandOwner, EcsCommands, compile_commands
    from app.integrations.ecs_command.lifecycle import EcsLifecycle
    from app.integrations.ecs_command.protocol import EcsCommandAdapter
    from app.integrations.wms_callback import registry as callbacks
    from app.integrations.wms_request import ADAPTERS, Endpoint, RequestPayload, request_operation
    from app.integrations.wms_request import operation as requests
    from app.main import app
    from app.models import (
        CallbackReceipt,
        Device,
        DeviceRoute,
        EcsCommand,
        EcsSendEvidence,
        ReliableMessage,
        WorkLine,
    )
    from app.reliable import Outbox
    from app.reliable.config import TargetConfig
    from app.services.device_bindings import resolve_binding
    from app.services.device_routes import DeviceRoutes

    table = Table(
        "business_seam_test_execution",
        MetaData(schema=SCHEMA),
        Column("id", String, primary_key=True),
        Column("business", String, nullable=False),
        Column("receipt_id", Integer, nullable=False),
        Column("line_id", Integer, nullable=False),
        Column("device_code", String, nullable=False),
        Column("device_id", Integer, nullable=False),
        Column("device_role", String, nullable=False),
        Column("accepted_payload", JSON),
        Column("pending", Boolean, nullable=False),
        Column("done", Boolean, nullable=False),
        Column("touch", Integer, nullable=False),
        Column("evidence", JSON),
        Column("next_intent", JSON),
    )
    engine = factory.kw["bind"]
    async with engine.begin() as connection:
        await connection.run_sync(table.drop, checkfirst=True)
        await connection.run_sync(table.create)
    try:

        class Decision(RequestPayload):
            execution_id: str

        shared_request_module = "app.businesses._components.flow_requests.request.seam_decision_v1"
        Decision.__module__ = shared_request_module
        component = ComponentDefinition(
            key="flow_requests",
            entry_modules=(shared_request_module,),
            required_methods=("send", "get_result"),
        )

        targets = {
            "seam-ecs": TargetConfig("seam-ecs", None, "ecs-command", address_mode="SNAPSHOT"),
            "seam-wms": TargetConfig("seam-wms", "http://127.0.0.1:45873", "wms-decisions"),
        }
        outbox = Outbox(targets, {**ADAPTERS, "ecs-command": EcsCommandAdapter()})
        bindings, annexes = compile_commands(FlowMove)
        commands = EcsCommands(
            outbox,
            "seam-ecs",
            DeviceRoutes(
                bindings,
                annexes=annexes,
            ),
            annexes,
        )
        outbox.lifecycles = {"ecs-command": EcsLifecycle(commands)}
        monkeypatch.setattr(ecs, "_registry", {})
        monkeypatch.setattr(
            callbacks,
            "_registry",
            {
                name: item
                for name, item in callbacks._registry.items()
                if not item.registration_source.module.startswith("app.businesses.")
            },
        )
        monkeypatch.setattr(requests, "_registry", {})
        entries, receipt_ids = [], set()
        for code, role, payload in (
            ("seam_a", "SEAM_A", FlowScanA),
            ("seam_b", "SEAM_B", FlowScanB),
        ):
            definition = BusinessDefinition(
                code=code,
                display_name=code,
                device_roles=(DeviceRole(role_key=role, display_name=role),),
                position_slots=(PositionSlot(slot_key="INLET", display_name="入口"),),
                initial_entries=(
                    InitialEntry(kind="ecs_event", name="SCAN_COMPLETED", device_role=role),
                    InitialEntry(kind="wms_callback", name=f"{code}.accept@v1"),
                ),
            )

            def factory_service(capabilities, code=code, definition=definition):
                service = FlowService(code, definition, capabilities, factory, table)
                service.components = MappingProxyType({"flow_requests": capabilities.wms})
                return service

            def handlers(code):
                async def on_event(ctx, data):
                    from app.businesses.runtime import require_runtime

                    receipt_ids.add(ctx.receipt_id)
                    await require_runtime(ctx).services[code].accept(ctx, data)

                async def on_callback(ctx, data):
                    from app.businesses.runtime import require_runtime

                    receipt_ids.add(ctx.receipt_id)
                    return await require_runtime(ctx).services[code].accept(ctx, data)

                return on_event, on_callback

            event_handler, callback_handler = handlers(code)
            # Distinct test sources let actual catalog crosschecks verify package ownership.
            event_handler.__module__ = f"tests.{code}.ecs_event"
            callback_handler.__module__ = f"tests.{code}.callback"
            ecs.ecs_event("SCAN_COMPLETED", device_role=role, data=payload)(event_handler)
            callbacks.callback_operation(f"{code}.accept@v1", data=payload)(callback_handler)
            entries.append(
                SimpleNamespace(
                    __name__=f"tests.{code}.composition",
                    DEFINITION=definition,
                    ENTRY_MODULES=(),
                    COMMANDS=(FlowMove,),
                    COMPONENTS=(component,),
                    build_service=factory_service,
                )
            )
        monkeypatch.setattr(catalog, "entries", lambda: tuple(entries))
        request_operation("seam.decision@v1", Endpoint.DECISIONS)(Decision)
        monkeypatch.setattr(catalog, "_validated_entries", None)
        catalog.validate_business_contracts()

        def rebuild():
            return build_runtime(outbox=outbox, ecs_commands=commands, session_factory=factory)

        runtime = rebuild()
        monkeypatch.setattr(app.state, "business_runtime", runtime, raising=False)
        monkeypatch.setattr(app.state, "callback_session_factory", factory, raising=False)
        monkeypatch.setattr(app.state, "ecs_event_binding_resolver", resolve_binding)
        state = SimpleNamespace(
            factory=factory,
            table=table,
            runtime=runtime,
            commands=commands,
            targets=targets,
            outbox=outbox,
            owner=CommandOwner("public-seam-test"),
            rebuild=rebuild,
            codes=[],
            line_ids=[],
            route_ids=[],
            receipt_ids=receipt_ids,
            origins=["http://127.0.0.1:45871", "http://127.0.0.1:45872", "http://127.0.0.1:45874"],
        )

        async def configure(client=None, admin=None):
            from app.core.identifiers import uuid7

            for index in range(3):
                code = "SEAM_" + uuid7().hex
                state.codes.append(code)
                business = "seam_b" if index == 2 else "seam_a"
                role = "SEAM_B" if index == 2 else "SEAM_A"
                if client:
                    response = await client.post(
                        "/api/v1/work-lines",
                        headers=admin,
                        json=dict(
                            line_code=code,
                            line_name=code,
                            business_code=business,
                            position_bindings={"INLET": f"POSITION_{index}"},
                        ),
                    )
                    assert response.status_code == 201, response.text
                    line_id = int(response.json()["id"])
                    state.line_ids.append(line_id)
                    route_id = str(uuid7())
                    state.route_ids.append(route_id)
                    async with factory.begin() as session:
                        route = DeviceRoute(
                            id=route_id, device_code=code, ecs_origin=state.origins[index]
                        )
                        session.add(route)
                        await session.flush()
                        session.add(
                            Device(
                                device_code=code,
                                display_name=code,
                                current_route_id=route.id,
                                work_line_id=line_id,
                            )
                        )
                    updated = await client.put(
                        f"/api/v1/work-lines/{line_id}",
                        headers=admin,
                        json=dict(
                            line_name=code,
                            version=1,
                            device_bindings={role: code},
                        ),
                    )
                    assert updated.status_code == 200, updated.text
                else:
                    async with factory.begin() as session:
                        line, _, route = await add_bound_device(
                            session, code, state.origins[index], role=role
                        )
                        line.business_code = business
                        line.position_bindings = {"INLET": f"POSITION_{index}"}
                        line_id = line.id
                        state.line_ids.append(line_id)
                        state.route_ids.append(route.id)

        async def rows():
            async with factory() as session:
                return list((await session.execute(select(table).order_by(table.c.id))).mappings())

        state.configure, state.rows = configure, rows
        try:
            yield state
        finally:
            async with factory.begin() as session:
                await session.execute(
                    delete(CallbackReceipt).where(CallbackReceipt.id.in_(receipt_ids))
                )
                await session.execute(
                    delete(EcsSendEvidence).where(
                        EcsSendEvidence.command_code.in_(
                            select(EcsCommand.command_code).where(
                                EcsCommand.owner_domain == state.owner.domain
                            )
                        )
                    )
                )
                await session.execute(
                    delete(EcsCommand).where(EcsCommand.owner_domain == state.owner.domain)
                )
                await session.execute(
                    delete(ReliableMessage).where(ReliableMessage.target.in_(targets))
                )
                await session.execute(delete(Device).where(Device.device_code.in_(state.codes)))
                await session.execute(
                    delete(WorkLine).where(
                        WorkLine.id.in_(state.line_ids) | WorkLine.line_code.in_(state.codes)
                    )
                )
                await session.execute(
                    delete(DeviceRoute).where(DeviceRoute.id.in_(state.route_ids))
                )
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(table.drop, checkfirst=True)


async def flow_decision(flow, identity):
    """Receive a terminal WMS response over loopback with the one real Sender."""
    import asyncio
    from dataclasses import replace

    from fastapi import FastAPI

    from app.core.identifiers import uuid7
    from app.http_client import AsyncHttpClient, HttpClientConfig
    from app.integrations.wms_request.operation import lookup
    from app.reliable.sender import Sender
    from app.reliable.store import MessageStore

    peer = FastAPI()

    @peer.post("/api/v1/wes/decisions")
    async def decide(body: dict):
        return dict(
            operation_id=body["operation_id"], code="DECIDED", timestamp=1, data={"allow": True}
        )

    async with loopback_app(peer) as origin:
        target = replace(flow.targets["seam-wms"], base_url=origin)
        flow.targets["seam-wms"] = target
        service = flow.runtime.services["seam_a"].capabilities.wms
        operation_id = str(uuid7())
        async with flow.factory.begin() as session:
            ref = await service.send(
                session,
                lookup("seam.decision@v1").data_model(execution_id=identity),
                operation_id=operation_id,
            )
        sender = Sender(
            MessageStore(flow.factory),
            {target.name: target},
            flow.outbox.adapters,
            {target.name: AsyncHttpClient(HttpClientConfig(origin, "seam-wms"))},
        )
        try:
            assert await sender.tick(maintain=False) == 1
            await asyncio.gather(*tuple(sender.inflight))
        finally:
            await sender.aclose()
    return dict(message_id=ref.message_id, operation="seam.decision@v1", operation_id=operation_id)


async def clean_flow_message(factory, message_id):
    """Test infrastructure ages one owned message and runs the existing terminal cleaner."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from app.models import ReliableMessage
    from app.reliable.store import cleanup_terminal

    now = datetime.now(UTC)
    async with factory.begin() as session:
        await session.execute(
            update(ReliableMessage)
            .where(ReliableMessage.id == message_id)
            .values(
                updated_at=now - timedelta(days=2),
            )
        )
        await cleanup_terminal(session, now=now, retention_days=1)


async def cleanup_flow_results(factory, tasks, operation_ids):
    """Remove only identities explicitly created by one public-seam fixture."""
    from sqlalchemy import delete

    from app.models import CallbackReceipt, ReliableMessage, TransportTask

    async with factory.begin() as session:
        await session.execute(
            delete(CallbackReceipt).where(CallbackReceipt.operation_id.in_(operation_ids))
        )
        await session.execute(
            delete(ReliableMessage).where(ReliableMessage.target.in_(t.target for t in tasks))
        )
        await session.execute(
            delete(TransportTask).where(
                TransportTask.transport_task_id.in_(t.handle.transport_task_id for t in tasks)
            )
        )


def coarse_sorting_scan_table(name):
    from sqlalchemy import MetaData

    from app.businesses.coarse_sorting.models import CoarseSortingScan
    from app.database import SCHEMA

    table = CoarseSortingScan.__table__.to_metadata(MetaData(schema=SCHEMA), name=name)
    for constraint in table.constraints:
        constraint.name = None
    return table


def coarse_sorting_composition(table):
    """Use the deployed declarations with an independently owned test table."""
    from types import ModuleType

    from app.businesses.coarse_sorting import composition
    from app.businesses.coarse_sorting.service import CoarseSortingService

    entry = ModuleType(composition.__name__)
    entry.DEFINITION = composition.DEFINITION
    entry.ENTRY_MODULES = composition.ENTRY_MODULES
    entry.MODEL_MODULES = ()
    entry.COMMANDS = composition.COMMANDS
    entry.PROGRESSION = composition.PROGRESSION

    def build_service(capabilities):
        from app.businesses.coarse_sorting.request.inbound_coarse_sorting_validate_bind_v1 import (
            ValidateBind,
        )

        return CoarseSortingService(table, capabilities, ValidateBind)

    entry.build_service = build_service
    return entry


@asynccontextmanager
async def formal_manual_fixture(factory, monkeypatch, *, line_count=2):
    """Formal catalog/default tables with bound lines and only loopback peers."""
    import asyncio
    import json
    from types import SimpleNamespace

    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from sqlalchemy import and_, delete, select

    from app import business_wakeups
    from app.businesses import catalog
    from app.businesses._components.bin_roller.models import TABLES as ROLLER_TABLES
    from app.businesses.smt_manual.models import TABLES
    from app.core import get_settings
    from app.core.identifiers import uuid7
    from app.http_client import AsyncHttpClient
    from app.integrations.ecs_callback.receiver import receive_ecs_event
    from app.integrations.ecs_command.clients import DynamicClients
    from app.integrations.ecs_command.result import receive_ecs_result
    from app.integrations.wms_callback.receiver import receive
    from app.models import (
        CallbackReceipt,
        Device,
        DeviceRoute,
        EcsCommand,
        EcsResultEvidence,
        EcsSendEvidence,
        ReliableMessage,
        TransportTask,
        WorkLine,
    )
    from app.reliable.sender import Sender
    from app.reliable.store import MessageStore
    from app.services.device_bindings import resolve_binding
    from app.tasks.business_tasks import _runtime

    prefix = "m6-" + uuid7().hex
    state = SimpleNamespace(
        factory=factory,
        prefix=prefix,
        lines=[],
        devices=[],
        positions=[],
        receipts=set(),
        requests=[],
        notices=[],
        admission={},
        transport_code="RECEIVED",
        transport_http=202,
        confirmation={"result": "BUSINESS_IN_PROGRESS", "retry_after_ms": 60000},
    )
    tables = {
        **{("host", k): v for k, v in TABLES.items()},
        **{("roller", k): v for k, v in ROLLER_TABLES.items()},
    }
    async with factory() as session:
        before = {
            key: set((await session.execute(select(*table.primary_key.columns))).all())
            for key, table in tables.items()
        }
    peer = FastAPI()

    @peer.post("/{path:path}")
    async def reply(path: str, request: Request):
        body = await request.json()
        state.requests.append((path, body))
        operation = body.get("operation")
        if path == "api/v1/device/command":
            return {"code": 200, "message": "ACK"}
        if operation == "transport.task.submit@v1":
            return JSONResponse(
                dict(
                    operation_id=body["operation_id"],
                    code=state.transport_code,
                    timestamp=1,
                    data={"transport_task_id": body["data"]["transport_task_id"]},
                ),
                status_code=state.transport_http,
            )
        code, status, data = "DECIDED", 200, {}
        if operation == "sorting.task.prepare@v1":
            code, status = "PREPARE_ACCEPTED", 202
        elif operation == "manual_bin.work_admission_decide@v1":
            data = state.admission.get(body["data"]["bin_code"], {"result": "NO_WORK"})
        elif operation == "sorting.task.completion_confirm@v1":
            data = state.confirmation
        elif operation == "rack.departure_decide@v1":
            data = getattr(state, "departure_decision", {"result": "WAIT", "retry_after_ms": 60000})
        elif operation == "outbound.bin.return_batch@v1":
            data = getattr(state, "return_decision", {"result": "NO_BATCH"})
        elif operation == "outbound.bin.inbound_batch@v1":
            data = getattr(state, "inbound_decision", {"result": "NO_BATCH"})
        elif operation == "workline.return_buffer.drain_rack_decide@v1":
            data = {
                "result": "WAIT",
                "reason_code": "NO_DRAIN_RACK_AVAILABLE",
                "retry_after_ms": 60000,
            }
        elif path.endswith("facts"):
            code = "RECORDED"
        return JSONResponse(
            dict(operation_id=body["operation_id"], code=code, timestamp=1, data=data),
            status_code=status,
        )

    async with loopback_app(peer) as origin:
        state.origin = origin
        targets = {
            prefix + "-" + protocol: {"base_url": origin, "protocol": protocol}
            for protocol in ("wms-facts", "wms-decisions", "wms-transport")
        }
        state.target_config = {"targets": targets}
        monkeypatch.setattr(get_settings(), "RELIABLE_TARGETS", state.target_config)
        state.runtime = _runtime(factory)
        state.host = state.runtime.services["smt_manual"]
        state.roller = state.host.components["bin_roller"]
        commands = state.host.capabilities.ecs
        outbox = commands.outbox
        state.store = MessageStore(
            factory,
            lifecycles=outbox.lifecycles,
            on_result=business_wakeups.wms_result_committed,
            on_delivered=business_wakeups.transport_submit_delivered,
        )
        sender = Sender(
            state.store,
            outbox.targets,
            outbox.adapters,
            {
                name: AsyncHttpClient(target.http_config())
                for name, target in outbox.targets.items()
                if target.address_mode == "STATIC"
            },
            provider=DynamicClients(factory=simulator_client_factory([origin])),
        )
        state.sender = sender
        async with factory.begin() as session:
            for number in range(1, line_count + 1):
                devices = {
                    point: f"{prefix}-{number}-{point}"
                    for point in ("SCAN1", "SCAN2", "SCAN3", "SCAN4")
                }
                positions = {
                    slot: f"{prefix}-{number}-{slot}"
                    for slot in ("FIVE_RACK", "INLET", "OUTLET", "RETURN_RACK", "TRANSFER_RACK")
                }
                line = WorkLine(
                    line_code=f"{prefix}-{number}",
                    line_name="M6 manual",
                    business_code="smt_manual",
                    device_bindings=devices,
                    position_bindings=positions,
                )
                session.add(line)
                await session.flush()
                state.lines.append(line.id)
                state.devices.append(devices)
                state.positions.append(positions)
                for code in devices.values():
                    route = DeviceRoute(id=str(uuid7()), device_code=code, ecs_origin=origin)
                    session.add(route)
                    await session.flush()
                    session.add(
                        Device(
                            device_code=code,
                            display_name=code,
                            work_line_id=line.id,
                            current_route_id=route.id,
                        )
                    )

        def envelope(operation, data, *, identity=None, timestamp=100):
            identity = identity or str(uuid7())
            state.receipts.add(identity)
            return json.dumps(
                dict(operation=operation, operation_id=identity, timestamp=timestamp, data=data)
            ).encode()

        async def callback(operation, data, *, rollback=False, before_commit=None, identity=None):
            async with factory() as session:
                response = await receive(
                    session,
                    envelope(operation, data, identity=identity),
                    runtime=state.runtime,
                    on_business_committed=business_wakeups.notify_business_committed,
                    on_owner_committed=business_wakeups.notify_business_committed,
                )
                if before_commit:
                    await before_commit()
                await session.rollback() if rollback else await session.commit()
                return response

        async def issue(task, *, line=0, direction="inbound"):
            return await callback(
                "sorting.task.issued@v1",
                dict(
                    task_id=task,
                    task_type="MANUAL",
                    business_type=direction,
                    workline_code=f"{prefix}-{line + 1}",
                    queue_revision=1,
                    dispatch_sequence=1,
                ),
            )

        async def scan(point, raw="A000000001-C", *, line=0, timestamp=10, rollback=False):
            async with factory() as session:
                response = await receive_ecs_event(
                    session,
                    json.dumps(
                        dict(
                            device_code=state.devices[line][point],
                            event_type="SCAN_COMPLETED",
                            timestamp=timestamp,
                            data={"bin_code": raw},
                        )
                    ).encode(),
                    resolve_binding=resolve_binding,
                    resolve_input_target=catalog.input_target,
                    runtime=state.runtime,
                    on_business_committed=business_wakeups.notify_business_committed,
                )
                state.receipts.add(response.digest)
                await session.rollback() if rollback else await session.commit()
                return response

        async def dispatch():
            total = 0
            for _ in range(100):
                count = await sender.tick(maintain=False)
                if sender.inflight:
                    await asyncio.gather(*tuple(sender.inflight))
                total += count
                if not count:
                    return total
            raise AssertionError("loopback sender did not drain its bounded fixture work")

        async def rows(key, *, roller=False):
            if not roller and key in {"rack", "rack_action", "rack_departure"}:
                async with factory() as session:
                    return await manual_dispatch_rows(
                        session, state.roller.rack_dispatch, key, state.lines
                    )

            if roller and key in ("rack_cycle", "rack_action", "departure"):
                async with factory() as session:
                    records = [
                        row
                        for line in state.lines
                        for row in await roller_dispatch_rows(session, state.roller, key, line)
                    ]
                    return sorted(records, key=lambda row: row["id"])
            query = (
                bin_roller_query(ROLLER_TABLES, key) if roller else smt_manual_query(TABLES, key)
            )
            async with factory() as session:
                return [dict(row) for row in (await session.execute(query)).mappings()]

        async def result(code, *, status="SUCCESS", rollback=False, before_commit=None):
            async with factory() as session:
                command = await session.scalar(
                    select(EcsCommand).where(EcsCommand.command_code == code)
                )
                raw = json.dumps(
                    dict(
                        command_code=code,
                        device_code=command.device_code,
                        result=status,
                        finish_time=1,
                        data={"actual_qty": 1},
                        error_detail={"code": "DEVICE_FAILURE", "msg": "local mock failure"}
                        if status == "FAILED"
                        else None,
                    )
                ).encode()
                response = await receive_ecs_result(
                    session, raw, commands, on_result=business_wakeups.ecs_result_committed
                )
                if before_commit:
                    await before_commit()
                await session.rollback() if rollback else await session.commit()
                return response

        state.callback, state.issue, state.scan = callback, issue, scan
        state.dispatch, state.rows, state.result, state.envelope = dispatch, rows, result, envelope
        try:
            yield state
        finally:
            await sender.aclose()
            async with factory.begin() as session:
                codes = select(EcsCommand.command_code).where(
                    EcsCommand.device_code.like(prefix + "%")
                )
                await session.execute(
                    delete(EcsResultEvidence).where(EcsResultEvidence.command_code.in_(codes))
                )
                await session.execute(
                    delete(EcsSendEvidence).where(EcsSendEvidence.command_code.in_(codes))
                )
                await session.execute(
                    delete(ReliableMessage).where(ReliableMessage.operation_id.in_(codes))
                )
                await session.execute(
                    delete(EcsCommand).where(EcsCommand.device_code.like(prefix + "%"))
                )
                await session.execute(
                    delete(TransportTask).where(
                        TransportTask.operation_id.in_(
                            select(ReliableMessage.operation_id).where(
                                ReliableMessage.target.in_(targets)
                            )
                        )
                    )
                )
                await session.execute(
                    delete(ReliableMessage).where(ReliableMessage.target.in_(targets))
                )
                await session.execute(
                    delete(CallbackReceipt).where(
                        (CallbackReceipt.operation_id.in_(state.receipts))
                        | CallbackReceipt.device_code.like(prefix + "%")
                    )
                )
                await clear_test_rack_jobs(session, state.lines)
                for key, table in reversed(tuple(tables.items())):
                    current = (await session.execute(select(*table.primary_key.columns))).all()
                    for row in set(current) - before[key]:
                        await session.execute(
                            table.delete().where(
                                and_(
                                    *(
                                        column == value
                                        for column, value in zip(
                                            table.primary_key.columns, row, strict=True
                                        )
                                    )
                                )
                            )
                        )
                await session.execute(delete(Device).where(Device.device_code.like(prefix + "%")))
                await session.execute(
                    delete(DeviceRoute).where(DeviceRoute.device_code.like(prefix + "%"))
                )
                await session.execute(delete(WorkLine).where(WorkLine.id.in_(state.lines)))


async def clear_test_rack_jobs(session, lines):
    from sqlalchemy import delete, select

    from app.businesses._components.rack_dispatch.models import RackAction, RackJob

    # Delete descendants before their original, retained predecessors.
    actions = await session.scalars(
        select(RackAction.id)
        .where(RackAction.work_line_id.in_(lines), RackAction.owner_domain == "smt_manual")
        .order_by(RackAction.ordinal.desc())
    )
    for action_id in actions:
        await session.execute(delete(RackAction).where(RackAction.id == action_id))
    await session.execute(
        delete(RackJob).where(RackJob.work_line_id.in_(lines), RackJob.owner_domain == "smt_manual")
    )


async def manual_dispatch_rows(session, dispatch, key, lines):
    from dataclasses import asdict

    from app.businesses.smt_manual.manual_rack_flow import MANUAL_SOURCES

    jobs = [
        j
        for line in lines
        for j in await dispatch.jobs(session, work_line_id=line, source_kinds=MANUAL_SOURCES)
    ]
    if key == "rack":
        return [
            dict(
                asdict(j.origin),
                id=j.id,
                role=j.origin.source_kind.removeprefix("MANUAL_"),
                state=j.state,
                pending=j.pending,
                location_code=j.origin.point,
            )
            for j in jobs
        ]
    return [
        dict(
            asdict(a),
            rack_work_id=j.id,
            request_snapshot=a.request,
            operation_id=a.decision_operation_id,
        )
        for j in jobs
        for a in j.actions
        if (key == "rack_departure") == (a.kind == "DEPARTURE_DECIDE")
    ]


async def roller_dispatch_rows(session, roller, key, line_id=1):
    from dataclasses import asdict

    jobs = await roller.rack_dispatch.jobs(
        session, work_line_id=line_id, source_kinds=("ROLLER_ORDINARY", "ROLLER_DRAIN")
    )
    if key == "rack_cycle":
        return [
            dict(
                id=j.id,
                rack_id=j.origin.rack_id,
                purpose=j.origin.source_kind.removeprefix("ROLLER_"),
                task_id=j.origin.task_id,
                work_line_id=j.origin.work_line_id,
                owner_domain=roller.context.domain,
                resources=j.origin.resources,
                source_ref=j.origin.source_ref,
                status=(
                    "RELEASED"
                    if j.state == "COMPLETED"
                    else "ACTIVE"
                    if j.state == "RELEASING"
                    else j.state
                ),
                pending=j.pending,
                release_basis=j.closure_basis["acceptance_basis"]
                if j.closure_reason == "DEPARTURE_ACK"
                else None,
            )
            for j in jobs
        ]
    result = []
    for j in jobs:
        for a in j.actions:
            if (key == "departure") != (a.kind == "DEPARTURE_DECIDE"):
                continue
            result.append(
                dict(
                    asdict(a),
                    cycle_id=j.id,
                    kind="DEPARTURE" if key == "departure" else a.template,
                    work_line_id=j.origin.work_line_id,
                    owner_domain=roller.context.domain,
                    operation_id=a.decision_operation_id
                    if key == "departure"
                    else a.submit_operation_id,
                    caller_workline_id=a.caller.workline_id if a.caller else None,
                    station_id=a.caller.station_id if a.caller else None,
                    status=a.phase if a.phase != "APPLIED" else a.outcome["status"],
                    pending=j.pending
                    if key == "departure"
                    else a.phase not in ("ACCEPTED", "APPLIED"),
                    due_at=a.due_at_ms,
                    consumed_revision=a.outcome_revision,
                )
            )
    return sorted(result, key=lambda a: a["id"])


async def manual_source_departure(flow, entry):
    """Reach one unsent CTU03 using the original CTU01 Result and WMS decisions."""
    cycle = next(
        r for r in await flow.rows("rack_cycle", roller=True) if r["id"] == entry["cycle_id"]
    )
    flow.inbound_decision = {"result": "RACK_FACE_DONE"}
    flow.departure_decision = dict(
        result="READY", rack_destination=dict(type="ZONE", location_code="LOCAL-RETURN")
    )
    response = await flow.callback(
        "transport.task.resulted@v1",
        dict(
            transport_task_id=entry["transport_task_id"],
            kind="RACK_MOVE",
            rack_id=cycle["rack_id"],
            outcome_revision=1,
            status="SUCCEEDED",
            final_position=entry["target"],
            arrival_face=entry["target_face"],
        ),
    )
    assert response.status_code == 202
    await flow.host.advance("RACK_CYCLE", entry["cycle_id"])
    await flow.dispatch()
    await flow.host.advance("FEED", cycle["source_ref"])
    await flow.dispatch()
    await flow.host.advance("RACK_CYCLE", entry["cycle_id"])
    return next(
        r
        for r in await flow.rows("rack_action", roller=True)
        if r["cycle_id"] == entry["cycle_id"] and r["kind"] == "CTU03"
    )


async def manual_departure_obligation(flow, *, line=0):
    """Reach CTU03 through one real source plan, original arrival and WMS decisions."""
    task = f"{flow.prefix}-old-task-{line}"
    assert (await flow.issue(task, line=line)).status_code == 202
    await flow.host.advance("TASK", task)
    await flow.dispatch()
    await flow.host.advance("TASK", task)
    response = await flow.callback(
        "sorting.task.plan_delta@v1",
        dict(
            task_id=task,
            plan_revision=1,
            added_bin_source_racks=[
                dict(rack_id=f"{flow.prefix}-old-rack-{line}", rack_face=["A"])
            ],
        ),
    )
    assert response.status_code == 202
    [demand] = [
        row for row in await flow.rows("supply_demand", roller=True) if row["task_id"] == task
    ]
    await flow.host.advance("FEED", demand["id"])
    [entry] = [
        row
        for row in await flow.rows("rack_action", roller=True)
        if row["kind"] == "CTU01" and row["work_line_id"] == flow.lines[line]
    ]
    departure = await manual_source_departure(flow, entry)
    [cycle] = [
        row for row in await flow.rows("rack_cycle", roller=True) if row["id"] == entry["cycle_id"]
    ]
    return cycle, departure


async def manual_return_opportunity(flow, *, line=0):
    """A controlled original drain selection enters and serves its face through real Transport."""
    from app.core.identifiers import uuid7

    decision_id = str(uuid7())
    line_id = flow.lines[line]
    rack = flow.prefix + "-return"
    async with flow.factory.begin() as session:
        repo = flow.roller.repository(line_id)
        candidates = await flow.roller.get_return_candidates(session, work_line_id=line_id)
        assert candidates
        await session.execute(
            repo.tables["decision"]
            .insert()
            .values(
                id=decision_id,
                kind="DRAIN",
                unused_racks=[],
                **repo.scope,
                input_kind="FIFO_OBSERVATION",
                input_ref=decision_id,
                input_snapshot={"round_id": candidates[0]["id"]},
                operation_id=str(uuid7()),
                status="READY",
                decision={"result": "READY", "racks": [{"rack_id": rack, "rack_face": ["A"]}]},
            )
        )
    await flow.host.advance("DRAIN", decision_id)
    [cycle] = [
        r for r in await flow.rows("rack_cycle", roller=True) if r["source_ref"] == decision_id
    ]
    [entry] = [
        r for r in await flow.rows("rack_action", roller=True) if r["cycle_id"] == cycle["id"]
    ]
    response = await flow.callback(
        "transport.task.resulted@v1",
        dict(
            transport_task_id=entry["transport_task_id"],
            kind="RACK_MOVE",
            rack_id=rack,
            outcome_revision=1,
            status="SUCCEEDED",
            final_position=entry["target"],
            arrival_face="A",
        ),
    )
    assert response.status_code == 202
    await flow.host.advance("RACK_CYCLE", cycle["id"])
    [batch] = [
        r
        for r in await flow.rows("return_batch", roller=True)
        if r["work_line_id"] == line_id and r["pending"]
    ]
    return batch["id"]


def smt_manual_query(tables, key):
    """Select the concrete input/request category a behavioral fixture needs."""
    from sqlalchemy import select

    from app.businesses.smt_manual.models import (
        BIN_COMPLETION_OPERATION,
        RACK_COMPLETION_OPERATIONS,
    )

    request_kinds = {
        "confirmation": "COMPLETION_CONFIRM",
        "admission": "BIN_ADMISSION",
        "rack_report": "RACK_ARRIVAL_REPORT",
        "rack_departure": "RACK_DEPARTURE",
    }
    if key in request_kinds:
        table = tables["request"]
        return (
            select(table).where(table.c.kind == request_kinds[key]).order_by(table.c.operation_id)
        )
    if key in {"completion", "rack_completion", "plan", "input"}:
        table = tables["input"]
        predicate = {
            "completion": table.c.operation == BIN_COMPLETION_OPERATION,
            "rack_completion": table.c.operation.in_(RACK_COMPLETION_OPERATIONS),
            "plan": table.c.is_plan_record,
            "input": table.c.operation.like("sorting.task.%"),
        }[key]
        return select(table).where(predicate).order_by(table.c.operation_id)
    table = tables[key]
    return select(table).order_by(*table.primary_key.columns)


def bin_roller_query(tables, key):
    """Inspect one original responsibility and identity in the compact component tables."""
    from sqlalchemy import select

    if key == "drain":
        t = tables["decision"]
        return select(t).where(t.c.kind == key.upper()).order_by(t.c.id)
    if key in {"scan2_release", "return_round"}:
        c = tables["scan_arrival"].c
        if key == "scan2_release":
            return (
                select(
                    c.release_record_id.label("id"),
                    c.owner_domain,
                    c.work_line_id,
                    c.id.label("arrival_id"),
                    c.release_call_key.label("call_key"),
                    c.release_command_code.label("command_code"),
                    (c.release_state == "PENDING").label("pending"),
                    c.release_consumed_result_id.label("consumed_result_id"),
                )
                .where(c.release_record_id.is_not(None))
                .order_by(c.release_record_id)
            )
        return (
            select(
                c.round_id.label("id"),
                c.owner_domain,
                c.work_line_id,
                c.id.label("scan3_arrival_id"),
                c.first_scan4_arrival_id.label("scan4_arrival_id"),
                c.bin_code,
                c.return_state.label("status"),
                c.removed,
            )
            .where(c.point == "SCAN3", c.round_id.is_not(None))
            .order_by(c.round_id)
        )
    if key in {"supply_demand", "face_service", "feed"}:
        c = tables["face_work"].c
        if key == "supply_demand":
            return (
                select(
                    c.demand_id.label("id"),
                    c.owner_domain,
                    c.work_line_id,
                    c.task_id,
                    c.change_id,
                    c.plan_revision,
                    c.rack_id,
                    c.rack_face,
                    c.cycle_id,
                    c.service_id,
                    (c.demand_state == "WITHDRAWN").label("withdrawn"),
                )
                .where(c.demand_id.is_not(None))
                .order_by(c.demand_id)
            )
        if key == "feed":
            return (
                select(
                    c.feed_id.label("id"),
                    c.owner_domain,
                    c.work_line_id,
                    c.service_id,
                    c.caller_workline_id,
                    c.station_id,
                    c.target,
                    c.operation_id,
                    c.message_id,
                    c.manifest,
                    (c.feed_state == "COMPLETED").label("feed_complete"),
                    (c.feed_state != "COMPLETED").label("pending"),
                )
                .where(c.feed_id.is_not(None))
                .order_by(c.feed_id)
            )
        return (
            select(
                c.service_id.label("id"),
                c.owner_domain,
                c.work_line_id,
                c.task_id,
                c.plan_revision,
                c.cycle_id,
                c.source_ref,
                c.rack_face,
                c.service_state.label("status"),
                c.service_state.in_(("PENDING", "READY")).label("pending"),
            )
            .where(c.service_id.is_not(None))
            .order_by(c.service_id)
        )
    table = tables[key]
    return select(table).order_by(*table.primary_key.columns)
