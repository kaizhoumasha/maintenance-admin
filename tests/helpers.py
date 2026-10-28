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
    from types import SimpleNamespace

    from sqlalchemy import JSON, Boolean, Column, Integer, MetaData, String, Table, delete, select

    from app.businesses import catalog
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
        monkeypatch.setattr(callbacks, "_registry", dict(callbacks._registry))
        monkeypatch.setattr(requests, "_registry", dict(requests._registry))
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
                return FlowService(code, definition, capabilities, factory, table)

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
                    build_service=factory_service,
                )
            )
        request_operation("seam.decision@v1", Endpoint.DECISIONS)(Decision)
        monkeypatch.setattr(catalog, "entries", lambda: tuple(entries))
        monkeypatch.setattr(catalog, "_validated_entries", None)
        catalog.validate_business_contracts()

        def rebuild():
            return build_runtime(outbox=outbox, ecs_commands=commands)

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
