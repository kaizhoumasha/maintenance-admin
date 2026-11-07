"""跨测试文件共享的辅助函数(夹具放 conftest.py,这里放普通函数)。"""

import asyncio
import base64
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import update

from app.core.security import create_access_token
from app.integrations import history as history_records
from app.integrations.ecs_command import ecs_command
from app.integrations.ecs_command.contracts import ErrorDetail

PAYLOAD = {"username": "kai", "email": "k@example.com", "password": "password123"}


LOGIN = {"username": "root", "password": "password123"}


class OpaqueHistoryAdapter:
    def build_request(self, message, target):
        from app.http_client import HttpRequest

        return HttpRequest("POST", "/opaque", body=message.request_raw)

    def classify_response(self, message, result):
        from app.reliable import Verdict, VerdictKind

        return Verdict(VerdictKind.DELIVERED, response=result.body)


async def read_history_body(session, record_id):
    from app.integrations.history import read_bodies

    assert session.in_transaction(), "read the body before leaving the caller's transaction"
    bodies = await read_bodies(session, record_ids=(record_id,))
    assert record_id in bodies, "stored original body is missing"
    return bodies[record_id].raw_body


async def read_receipts(session):
    from sqlalchemy import select

    from app.integrations.callback_receipts import find_receipt
    from app.models import IntegrationExchange

    identities = list(
        await session.execute(
            select(
                IntegrationExchange.operation,
                IntegrationExchange.operation_id,
            )
            .where(
                IntegrationExchange.role == "RECEIPT",
                IntegrationExchange.receipt_active.is_(True),
            )
            .order_by(IntegrationExchange.receipt_id)
        )
    )
    return [await find_receipt(session, operation, identity) for operation, identity in identities]


async def read_diagnostic_snapshots(session):
    from types import SimpleNamespace

    from sqlalchemy import select

    from app.core.strict_json import json_object
    from app.integrations.history import read_bodies
    from app.models import IntegrationRecord

    records = list(
        await session.scalars(
            select(IntegrationRecord)
            .where(
                IntegrationRecord.evidence_class == "DIAGNOSTIC",
                IntegrationRecord.record_kind == "CALLBACK_RECEIVED",
                IntegrationRecord.body_state != "PURGED",
            )
            .order_by(IntegrationRecord.recorded_at, IntegrationRecord.id)
        )
    )
    if not records:
        return []
    replies = list(
        await session.scalars(
            select(IntegrationRecord).where(
                IntegrationRecord.evidence_class == "DIAGNOSTIC",
                IntegrationRecord.record_kind == "RESPONSE_PREPARED",
                IntegrationRecord.parent_record_id.in_(record.id for record in records),
            )
        )
    )
    bodies = await read_bodies(session, record_ids=tuple(r.id for r in [*records, *replies]))
    by_request = {reply.parent_record_id: reply for reply in replies}
    result = []
    for record in records:
        meta = record.details or {}
        reply = by_request.get(record.id)
        wire = bodies[record.id].raw_body
        reply_wire = bodies[reply.id].raw_body if reply is not None else None
        result.append(
            SimpleNamespace(
                id=record.id,
                request_id=meta.get("request_id"),
                source=meta.get("source"),
                received_at=record.occurred_at,
                body_state=bodies[record.id].body_state,
                body_b64=base64.b64encode((wire or b"")[: 48 * 1024]).decode("ascii"),
                truncated=meta.get("truncated", False),
                outcome=meta.get("outcome"),
                http_status=reply.http_status if reply is not None else None,
                response_body=json_object(reply_wire) if reply_wire else None,
            )
        )
    return result


async def ecs_attempt_evidence(session, command_code):
    from types import SimpleNamespace

    from sqlalchemy import select

    from app.integrations.ecs_command.lifecycle import get_attempt_conclusions
    from app.models import IntegrationExchange, IntegrationRecord

    exchange_id = await session.scalar(
        select(IntegrationExchange.id).where(
            IntegrationExchange.role == "ECS_COMMAND",
            IntegrationExchange.command_code == command_code,
        )
    )
    if exchange_id is None:
        return []
    outcomes = await get_attempt_conclusions(session, exchange_id=exchange_id)
    evidence = []
    for lease_id, outcome in sorted(outcomes.items()):
        record = await session.scalar(
            select(IntegrationRecord)
            .where(
                IntegrationRecord.exchange_id == exchange_id,
                IntegrationRecord.attempt_id == lease_id,
                IntegrationRecord.record_kind == "ECS_ATTEMPT_CONCLUSION",
            )
            .order_by(IntegrationRecord.attempt_revision.desc())
            .limit(1)
        )
        body = (
            await read_history_body(session, record.parent_record_id)
            if (record is not None and record.parent_record_id is not None)
            else None
        )
        evidence.append(
            SimpleNamespace(
                lease_id=lease_id,
                outcome=outcome,
                response_raw=body,
                http_status=record.http_status if record else None,
                late=bool(record.late) if record else False,
            )
        )
    return evidence


async def clear_integration_history(session, exchange_ids):
    from sqlalchemy import delete, or_, select, update

    from app.models import IntegrationExchange, IntegrationRecord, IntegrationReference

    exchange_ids = set(exchange_ids)
    if not exchange_ids:
        return
    while True:
        records = select(IntegrationRecord.id).where(
            IntegrationRecord.exchange_id.in_(exchange_ids)
        )
        dependent = set(
            await session.scalars(
                select(IntegrationRecord.exchange_id).where(
                    or_(
                        IntegrationRecord.parent_record_id.in_(records),
                        IntegrationRecord.body_record_id.in_(records),
                        IntegrationRecord.response_record_id.in_(records),
                    )
                )
            )
        )
        dependent.update(
            await session.scalars(
                select(IntegrationReference.exchange_id).where(
                    IntegrationReference.related_exchange_id.in_(exchange_ids)
                )
            )
        )
        dependent.update(
            await session.scalars(
                select(IntegrationReference.related_exchange_id).where(
                    IntegrationReference.exchange_id.in_(exchange_ids),
                    IntegrationReference.related_exchange_id.is_not(None),
                )
            )
        )
        if dependent <= exchange_ids:
            break
        exchange_ids.update(dependent)
    await session.execute(
        delete(IntegrationReference).where(IntegrationReference.exchange_id.in_(exchange_ids))
    )
    await session.execute(
        update(IntegrationExchange)
        .where(IntegrationExchange.id.in_(exchange_ids))
        .values(request_record_id=None, ack_record_id=None, current_response_record_id=None)
    )
    await session.execute(
        delete(IntegrationRecord).where(IntegrationRecord.exchange_id.in_(exchange_ids))
    )
    await session.execute(
        delete(IntegrationExchange).where(IntegrationExchange.id.in_(exchange_ids))
    )


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


async def run_alembic(url, *arguments, environment=None):
    """Execute one real migration CLI command and preserve its exit status and output."""
    return await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", *arguments],
        cwd=Path(__file__).parents[1],
        env={**os.environ, "DATABASE_URL": url, **(environment or {})},
        capture_output=True,
        text=True,
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
    from app.integrations.callback_receipts import insert_receipt

    normalized = {
        "device_code": event["device_code"],
        "event_type": event["event_type"],
        "timestamp": event["timestamp"],
        "is_debug": event.get("is_debug", False),
        "data": event.get("data", {}),
    }
    async with factory.begin() as session:
        await insert_receipt(
            session,
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
        MessageStore(
            ecs.factory, lifecycles={PROTOCOL: EcsLifecycle(ecs.service)}, records=history_records
        ),
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
    service = TransportService(
        Outbox({target.name: target}, ADAPTERS, records=history_records), target=target.name
    )
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
        position_slots=(
            PositionSlot(position_type="HANDOFF_POSITION", slot_key="INLET", display_name="入口"),
        ),
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
    from app.models import Device, DeviceRoute, IntegrationExchange, ReliableMessage, WorkLine
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
        outbox = Outbox(
            targets, {**ADAPTERS, "ecs-command": EcsCommandAdapter()}, records=history_records
        )
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
                position_slots=(
                    PositionSlot(
                        position_type="HANDOFF_POSITION", slot_key="INLET", display_name="入口"
                    ),
                ),
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
                roots = set(
                    await session.scalars(
                        select(IntegrationExchange.id).where(
                            (IntegrationExchange.owner_domain == state.owner.domain)
                            | IntegrationExchange.target_key.in_(targets)
                            | IntegrationExchange.receipt_id.in_(receipt_ids)
                        )
                    )
                )
                await session.execute(
                    delete(ReliableMessage).where(ReliableMessage.target.in_(targets))
                )
                await clear_integration_history(session, roots)
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
            MessageStore(flow.factory, records=history_records),
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

    from app.models import IntegrationExchange, ReliableMessage, TransportTask

    async with factory.begin() as session:
        await session.execute(
            update(IntegrationExchange)
            .where(
                IntegrationExchange.role == "RECEIPT", IntegrationExchange.receipt_active.is_(True)
            )
            .values(receipt_active=False)
            .where(IntegrationExchange.operation_id.in_(operation_ids))
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
    for constraint in tuple(table.constraints):
        if constraint in table.foreign_key_constraints:
            table.constraints.remove(constraint)
            table.foreign_keys.difference_update(constraint.elements)
        else:
            constraint.name = None
    for index in table.indexes:
        index.name = f"{name[-32:]}_{index.columns.keys()[0]}_idx"
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
async def formal_manual_fixture(
    factory, monkeypatch, *, line_count=2, source_profile=None, evidence_path=None
):
    """Formal runtime with generated or supplied lines and only loopback peers."""
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
        Device,
        DeviceRoute,
        IntegrationExchange,
        IntegrationRecord,
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
    owned_codes = (
        [d["device_code"] for line in source_profile["lines"] for d in line["devices"]]
        if source_profile is not None
        else None
    )

    def owned_device(column):
        return column.in_(owned_codes) if owned_codes is not None else column.like(prefix + "%")

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
        elif operation == "bin.return_batch@v1":
            data = getattr(state, "return_decision", {"result": "NO_BATCH"})
        elif operation == "bin.inbound_batch@v1":
            data = getattr(
                state,
                "inbound_decision",
                {"result": "RACK_FACE_DONE" if source_profile is not None else "NO_BATCH"},
            )
        elif operation == "workline.return_buffer.drain_rack_decide@v1":
            data = getattr(
                state,
                "drain_decision",
                {
                    "result": "WAIT",
                    "reason_code": "NO_DRAIN_RACK_AVAILABLE",
                    "retry_after_ms": 60000,
                },
            )
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
            records=history_records,
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
            profiles = (
                source_profile["lines"] if source_profile is not None else [None] * line_count
            )
            for number, profile in enumerate(profiles, 1):
                if profile is None:
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
                else:
                    devices = {d["role"]: d["device_code"] for d in profile["devices"] if d["role"]}
                    positions = {p["role"]: p["code"] for p in profile["positions"]}
                    line = WorkLine(
                        line_code=profile["code"],
                        line_name=profile["code"],
                        business_code="smt_manual",
                        is_enabled=profile["enabled"],
                        version=profile["version"],
                        device_bindings=devices,
                        position_bindings=positions,
                    )
                session.add(line)
                await session.flush()
                await configure_rack_positions(
                    session,
                    line,
                    five_capacity=next(
                        (p["capacity"] for p in profile["positions"] if p["role"] == "FIVE_RACK"),
                        None,
                    )
                    if profile
                    else None,
                )
                state.lines.append(line.id)
                state.devices.append(devices)
                state.positions.append(positions)
                line_devices = (
                    profile["devices"]
                    if profile is not None
                    else [{"device_code": code} for code in devices.values()]
                )
                for device in line_devices:
                    code = device["device_code"]
                    route = DeviceRoute(id=str(uuid7()), device_code=code, ecs_origin=origin)
                    session.add(route)
                    await session.flush()
                    session.add(
                        Device(
                            device_code=code,
                            display_name=device.get("display_name", code),
                            work_line_id=line.id,
                            current_route_id=route.id,
                        )
                    )

        if source_profile is not None:
            # Validate every active route/target before callbacks can freeze an intent.
            from urllib.parse import urlsplit

            assert urlsplit(origin).hostname == "127.0.0.1"
            assert {target["base_url"] for target in targets.values()} == {origin}
            async with factory() as session:
                assert set(await session.scalars(select(DeviceRoute.ecs_origin))) == {origin}
            state.source_mapping = {
                "checked_at": source_profile["checked_at"],
                "origin": {"source": source_profile["endpoint_base_url"], "test": origin},
                "lines": [
                    {
                        "source_id": p["source_work_line_id"],
                        "local_id": line_id,
                        "code": p["code"],
                        "version": p["version"],
                    }
                    for p, line_id in zip(source_profile["lines"], state.lines, strict=True)
                ],
            }

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
                    workline_code=source_profile["lines"][line]["code"]
                    if source_profile is not None
                    else f"{prefix}-{line + 1}",
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
            if source_profile is not None:
                async with factory() as session:
                    assert set(await session.scalars(select(DeviceRoute.ecs_origin))) == {origin}
                assert all(t["base_url"] == origin for t in state.target_config["targets"].values())
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
            if roller:
                async with factory() as session:
                    return await bin_roller_rows(session, state.roller, key)
            query = smt_manual_query(TABLES, key)
            async with factory() as session:
                return [dict(row) for row in (await session.execute(query)).mappings()]

        async def result(code, *, status="SUCCESS", rollback=False, before_commit=None):
            async with factory() as session:
                command = await session.scalar(
                    select(IntegrationExchange)
                    .where(IntegrationExchange.role == "ECS_COMMAND")
                    .where(IntegrationExchange.command_code == code)
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
            if source_profile is not None:
                evidence_dir, evidence_name = evidence_path
                # Evidence is retained before cleanup even when a capacity assertion fails.
                evidence = {
                    "mapping": state.source_mapping,
                    "requests": state.requests,
                    "observations": getattr(state, "observations", {}),
                }
                async with factory() as session:
                    from dataclasses import asdict

                    from app.businesses._components.bin_roller.service import ROLLER_SOURCES

                    evidence["rack_dispatch/jobs"] = [
                        asdict(j)
                        for line in state.lines
                        for j in await state.roller.rack_dispatch.jobs(
                            session, work_line_id=line, source_kinds=ROLLER_SOURCES
                        )
                    ]
                    for key, table in tables.items():
                        evidence["/".join(key)] = [
                            dict(r) for r in (await session.execute(select(table))).mappings()
                        ]
                    for model in (
                        WorkLine,
                        Device,
                        DeviceRoute,
                        IntegrationExchange,
                        ReliableMessage,
                        TransportTask,
                        IntegrationRecord,
                    ):
                        evidence[model.__tablename__] = [
                            dict(r)
                            for r in (await session.execute(select(model.__table__))).mappings()
                        ]
                (evidence_dir / (evidence_name + ".evidence.json")).write_text(
                    json.dumps(
                        evidence,
                        ensure_ascii=False,
                        indent=2,
                        default=lambda v: v.decode() if isinstance(v, bytes) else str(v),
                    )
                )
            await sender.aclose()
            async with factory.begin() as session:
                history_ids = list(
                    await session.scalars(
                        select(IntegrationExchange.id).where(
                            owned_device(IntegrationExchange.device_code)
                            | IntegrationExchange.operation_id.in_(state.receipts)
                            | IntegrationExchange.target_key.in_(targets)
                        )
                    )
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
                    delete(ReliableMessage).where(
                        ReliableMessage.target.in_(targets)
                        | ReliableMessage.exchange_id.in_(history_ids)
                    )
                )
                await clear_integration_history(session, history_ids)
                await clear_test_rack_jobs(session, state.lines)
                cleanup_tables = list(reversed(tuple(tables.items())))
                cleanup_tables.sort(key=lambda pair: pair[1].name != "bin_roller_bin_return")
                for key, table in cleanup_tables:
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
                await session.execute(delete(Device).where(owned_device(Device.device_code)))
                await session.execute(
                    delete(DeviceRoute).where(owned_device(DeviceRoute.device_code))
                )
                from app.models import WorkLinePosition

                await session.execute(
                    delete(WorkLinePosition).where(WorkLinePosition.work_line_id.in_(state.lines))
                )
                await session.execute(delete(WorkLine).where(WorkLine.id.in_(state.lines)))

            if source_profile is not None:
                async with factory() as session:
                    counts = {
                        "/".join(key): await session.scalar(
                            select(__import__("sqlalchemy").func.count()).select_from(table)
                        )
                        for key, table in tables.items()
                    }
                    pass
                (evidence_dir / (evidence_name + ".cleanup.json")).write_text(
                    json.dumps(counts, indent=2)
                )
                assert all(n == 0 for n in counts.values()), counts


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
    await flow.host.advance("FACE", cycle["source_ref"])
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
    await flow.host.advance("FACE", demand["id"])
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
    from app.businesses._components.bin_roller.request import (
        workline_return_buffer_drain_rack_decide_v1 as drain_contract,
    )
    from app.core.identifiers import uuid7
    from app.reliable import ScopeRef

    line_id = flow.lines[line]
    rack = flow.prefix + f"-return-{line}"
    async with flow.factory.begin() as session:
        repo = flow.roller.repository(line_id)
        candidates = await flow.roller.get_return_candidates(session, work_line_id=line_id)
        assert candidates
        state = await repo.line_state(session, start=True)
        operation_id = str(uuid7())
        message = await flow.host.capabilities.wms.send(
            session,
            drain_contract.DrainRequest(
                workline_code=flow.prefix + f"-{line + 1}", required_slot_count=len(candidates)
            ),
            operation_id=operation_id,
            scope=ScopeRef("smt_manual", "LINE", state["line_run_id"]),
        )
        await repo.update(
            session,
            "workline_state",
            drain_phase="REQUESTED",
            drain_operation_id=operation_id,
            drain_message_id=message.message_id,
            candidate_cursor=0,
        )
    flow.drain_decision = {"result": "READY", "racks": [{"rack_id": rack, "rack_face": ["A"]}]}
    await flow.dispatch()
    await flow.host.advance("DRAIN", state["line_run_id"])
    [cycle] = [
        r for r in await flow.rows("rack_cycle", roller=True) if r["source_ref"] == operation_id
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
    """Read current checkpoints; host tests keep their responsibility-oriented helpers."""
    from sqlalchemy import BigInteger, case, literal, select, union_all

    from app.models import IntegrationExchange

    receipt = (
        select(
            IntegrationExchange.receipt_id.label("id"),
            IntegrationExchange.operation,
            IntegrationExchange.operation_id,
            IntegrationExchange.device_code,
            IntegrationExchange.event_envelope,
            IntegrationExchange.application_ref,
            IntegrationExchange.event_context,
            IntegrationExchange.created_at,
        )
        .where(IntegrationExchange.role == "RECEIPT", IntegrationExchange.receipt_active.is_(True))
        .subquery()
    )
    if key == "scan2_release":
        c = tables["bin_passage"].c
        return (
            select(
                c.id,
                c.owner_domain,
                c.work_line_id,
                c.id.label("arrival_id"),
                (literal("bin_roller:scan2_release:") + c.id).label("call_key"),
                c.release_command_code.label("command_code"),
                (c.closed_at.is_(None) & c.release_consumed_result_id.is_(None)).label("pending"),
                c.release_consumed_result_id.label("consumed_result_id"),
            )
            .where(c.release_command_code.is_not(None))
            .order_by(c.id)
        )
    if key in ("supply_demand", "face_service", "feed"):
        c = tables["face_work"].c
        complete = (c.close_reason == "COMPLETED") | (c.window_kind == "FACE_END")
        query = select(
            *c,
            c.scope_id.label("task_id"),
            c.id.label("demand_id"),
            c.id.label("service_id"),
            c.id.label("feed_id"),
            c.rack_job_id.label("cycle_id"),
            c.id.label("source_ref"),
            c.feed_message_id.label("message_id"),
            c.feed_operation_id.label("operation_id"),
            (c.demand_state == "WITHDRAWN").label("withdrawn"),
            complete.label("feed_complete"),
            c.closed_at.is_(None).label("pending"),
            case(
                (c.closed_at.is_not(None), c.close_reason),
                (c.preparation_action_id.is_not(None), "READY"),
                else_="PENDING",
            ).label("status"),
        )
        if key == "supply_demand":
            query = query.where(c.purpose == "SUPPLY")
        elif key == "feed":
            query = query.where(c.feed_operation_id.is_not(None))
        else:
            query = query.where(c.rack_job_id.is_not(None))
        return query.order_by(c.id)
    if key == "scan_arrival":
        queries = []
        for point, table_key in (
            ("SCAN1", "bin_passage"),
            ("SCAN2", "bin_passage"),
            ("SCAN3", "bin_return"),
            ("SCAN4", "bin_return"),
        ):
            c = tables[table_key].c
            event = c[point.lower() + "_event_id"]
            command = (
                c.release_command_code if point == "SCAN2" else c[point.lower() + "_command_code"]
            )
            consumed = (
                c.release_consumed_result_id
                if point == "SCAN2"
                else c[point.lower() + "_consumed_result_id"]
            )
            reference = receipt.c.application_ref
            queries.append(
                select(
                    c.id,
                    c.owner_domain,
                    c.work_line_id,
                    literal(point).label("point"),
                    c.bin_code,
                    case((c.scan3_event_id.is_not(None), c.id), else_=None).label("round_id")
                    if table_key == "bin_return"
                    else literal(None).label("round_id"),
                    receipt.c.device_code,
                    receipt.c.event_envelope["data"]["bin_code"].as_string().label("raw_bin_code"),
                    receipt.c.event_envelope["timestamp"]
                    .as_string()
                    .cast(BigInteger)
                    .label("scanned_at"),
                    receipt.c.created_at.label("received_at"),
                    receipt.c.operation_id.label("first_digest"),
                    c[point.lower() + "_visit_key"].label("business_key"),
                    command.label("command_code"),
                    consumed.label("consumed_result_id"),
                    (c.closed_at.is_(None) & consumed.is_(None)).label("pending"),
                    reference["separator_command_code"].as_string().label("separator_command_code"),
                    c.release_command_code.label("release_command_code")
                    if table_key == "bin_passage"
                    else literal(None).label("release_command_code"),
                    case((c.production_record_id.is_not(None), "ACCEPTED"), else_="PENDING").label(
                        "delegation_state"
                    )
                    if table_key == "bin_passage"
                    else literal(None).label("delegation_state"),
                )
                .select_from(tables[table_key].join(receipt, receipt.c.id == event))
                .where(event.is_not(None))
            )
        return select(union_all(*queries).subquery()).order_by("received_at", "id")
    if key == "return_round":
        c = tables["bin_return"].c
        return (
            select(
                *c,
                c.id.label("round_id"),
                c.id.label("scan3_arrival_id"),
                c.scan4_event_id.label("scan4_arrival_id"),
                case((c.close_reason == "COMPLETED", "COMPLETED"), else_=c.return_state).label(
                    "status"
                ),
                (~c.is_current).label("removed"),
            )
            .where(c.scan3_event_id.is_not(None))
            .order_by(c.id)
        )
    if key == "scan_fact":
        c = receipt.c
        subjects = union_all(select(tables["bin_passage"].c.id), select(tables["bin_return"].c.id))
        return (
            select(
                c.id,
                c.operation_id.label("digest"),
                c.id.label("receipt_id"),
                c.application_ref["subject_id"].as_string().label("arrival_id"),
                c.event_context["device_role"].as_string().label("point"),
                c.event_context.label("binding_snapshot"),
            )
            .where(
                c.operation == "ecs.event",
                c.application_ref["subject_id"].as_string().in_(subjects),
            )
            .order_by(c.id)
        )
    table = tables[key]
    return select(table).order_by(*table.primary_key.columns)


def copy_roller_tables(metadata, prefix):
    """Copy independent component metadata, remapping only its internal foreign keys."""
    from sqlalchemy import ForeignKeyConstraint

    from app.businesses._components.bin_roller.models import TABLES

    copies = {
        key: table.to_metadata(metadata, name=prefix + key, schema=metadata.schema)
        for key, table in TABLES.items()
    }
    originals = {table.name: key for key, table in TABLES.items()}
    for table in copies.values():
        for constraint in tuple(table.foreign_key_constraints):
            local = [element.parent.name for element in constraint.elements]
            targets = []
            for element in constraint.elements:
                parts = element.target_fullname.split(".")
                key = originals[parts[-2]]
                targets.append(
                    f"{metadata.schema + '.' if metadata.schema else ''}{prefix + key}.{parts[-1]}"
                )
            table.constraints.remove(constraint)
            for element in constraint.elements:
                table.foreign_keys.remove(element)
                element.parent.foreign_keys.remove(element)
            table.append_constraint(
                ForeignKeyConstraint(
                    local, targets, name=constraint.name, ondelete=constraint.ondelete
                )
            )
    return copies


async def bin_roller_rows(session, roller, key):
    """Inspect current responsibilities using their original public allocation/move evidence."""
    from sqlalchemy import select

    from app.businesses._components.bin_roller.feed_flow import read_feed_manifest
    from app.businesses._components.bin_roller.return_flow import (
        read_drain_manifest,
        read_return_manifest,
    )

    if key == "rack_position":
        face_table = roller.tables["face_work"]
        line_ids = set(await session.scalars(select(face_table.c.work_line_id).distinct()))
        state = roller.tables["workline_state"]
        line_ids.update(await session.scalars(select(state.c.work_line_id).distinct()))
        rows = []
        for line in sorted(line_ids):
            jobs = await roller.rack_dispatch.jobs(
                session, work_line_id=line, source_kinds=("ROLLER_ORDINARY", "ROLLER_DRAIN")
            )
            for job in jobs:
                for action in job.actions:
                    if action.actual_position is not None and action.position_revision is not None:
                        rows.append(
                            dict(
                                id=action.id,
                                object_kind="RACK",
                                object_code=job.origin.rack_id,
                                source_kind="RACK",
                                source_id=action.id,
                                source_revision=action.position_revision,
                                location_code=action.actual_position["location_code"],
                                rack_face=action.actual_face,
                                is_current=job.pending and job.position_action_id == action.id,
                            )
                        )
        return rows
    if key not in {
        "feed",
        "feed_segment",
        "face_service",
        "supply_demand",
        "return_opportunity",
        "return_batch",
        "drain",
    }:
        return [
            dict(r)
            for r in (await session.execute(bin_roller_query(roller.tables, key))).mappings()
        ]
    if key == "drain":
        table = roller.tables["workline_state"]
        result = []
        for row in (await session.execute(select(table).order_by(*table.primary_key))).mappings():
            if row["drain_operation_id"] is not None:
                decision = await read_drain_manifest(roller, session, row)
                result.append(
                    dict(
                        row,
                        id=row["line_run_id"],
                        operation_id=row["drain_operation_id"],
                        message_id=row["drain_message_id"],
                        status={"REQUESTED": "PENDING", "WAIT": "WAIT", "USING": "READY"}[
                            row["drain_phase"]
                        ],
                        pending=True,
                        decision=decision,
                        due_at=int(row["drain_retry_at"].timestamp() * 1000)
                        if row["drain_retry_at"]
                        else None,
                    )
                )
        return result
    table = roller.tables["face_work"]
    faces = (await session.execute(select(table).order_by(table.c.id))).mappings().all()
    result = []
    for original in faces:
        face = dict(original)
        job = (
            await roller.rack_dispatch.progress(session, job_id=face["rack_job_id"])
            if face["rack_job_id"]
            else None
        )
        manifest = (
            await read_feed_manifest(roller.capabilities, session, face)
            if face["feed_operation_id"] and roller.capabilities.wms
            else None
        )
        common = dict(
            face,
            task_id=face["scope_id"] if face["purpose"] == "SUPPLY" else None,
            cycle_id=face["rack_job_id"],
            demand_id=face["id"] if face["purpose"] == "SUPPLY" else None,
            service_id=face["id"] if job else None,
            feed_id=face["id"] if face["feed_operation_id"] else None,
            source_ref=face["id"],
            pending=face["closed_at"] is None,
            withdrawn=face["demand_state"] == "WITHDRAWN",
            caller_workline_id=job.origin.caller.workline_id if job and job.origin.caller else None,
            station_id=job.origin.caller.station_id if job and job.origin.caller else None,
            workline_code=job.origin.workline_code if job else None,
            target={
                "kind": "HANDOFF_POSITION",
                "location_code": job.origin.resources["positions"]["INLET"],
            }
            if job and job.origin.resources
            else None,
        )
        complete = manifest is not None and (
            manifest["result"] == "RACK_FACE_DONE"
            or face["feed_cursor"] == len(manifest.get("bins", ()))
        )
        common.update(
            feed_complete=complete,
            manifest=manifest,
            feed_state="COMPLETED"
            if complete
            else "REVIEW"
            if face["review_reason"]
            else "ALLOCATED"
            if manifest
            else "REQUESTED"
            if face["feed_operation_id"]
            else None,
            service_state=face["close_reason"]
            if face["closed_at"]
            else "READY"
            if face["phase"] != "WAIT_RACK"
            else "PENDING",
        )
        if key == "supply_demand" and face["purpose"] == "SUPPLY":
            result.append(common)
        elif key == "face_service" and job:
            result.append(dict(common, status=common["service_state"]))
        elif key == "feed" and face["feed_operation_id"]:
            result.append(
                dict(
                    common,
                    operation_id=face["feed_operation_id"],
                    message_id=face["feed_message_id"],
                    pending=not complete,
                )
            )
        elif key == "return_opportunity" and face["window_id"]:
            result.append(
                dict(common, kind=face["window_kind"], ended=face["closed_at"] is not None)
            )
        elif key == "return_batch" and face["return_operation_id"]:
            decision = await read_return_manifest(roller, session, face)
            view = (
                await roller.capabilities.transport.get_result(
                    session,
                    transport_task_id=face["current_transport_task_id"],
                    client_request_id=face["current_client_request_id"],
                    caller=job.origin.caller,
                )
                if face["current_transport_task_id"]
                else None
            )
            result.append(
                dict(
                    common,
                    opportunity_id=face["id"],
                    members=face["return_members"] or [],
                    transport_operation_id=view.operation_id if view else None,
                    operation_id=face["return_operation_id"],
                    message_id=face["return_message_id"],
                    manifest=decision,
                    client_request_id=face["current_client_request_id"],
                    transport_task_id=face["current_transport_task_id"],
                    status="NO_BATCH"
                    if decision and decision["result"] == "NO_BATCH"
                    else "PENDING",
                    source={
                        "kind": "HANDOFF_POSITION",
                        "location_code": job.origin.resources["positions"]["OUTLET"],
                    },
                )
            )
        elif key == "feed_segment" and face["phase"] == "FEED_MOVE" and face["closed_at"] is None:
            view = await roller.capabilities.transport.get_result(
                session,
                transport_task_id=face["current_transport_task_id"],
                client_request_id=face["current_client_request_id"],
                caller=job.origin.caller,
            )
            frozen = (view.frozen or {}).get("moves", ())
            members = [
                dict(
                    bin_code=m["container_id"],
                    source_locator={
                        "type": "RACK_BIN_SLOT",
                        **{k: v for k, v in m["source"].items() if k != "kind"},
                    },
                )
                for m in frozen
            ]
            outcome_members = (view.outcome or {}).get("results", ())
            status = (
                "SUCCEEDED"
                if outcome_members
                and all(m["status"] == "SUCCEEDED" for m in outcome_members)
                and not view.has_pending_outcome
                else "UNKNOWN"
                if view.has_pending_outcome
                or any(m.get("position_unknown") for m in outcome_members)
                else "FAILED"
                if outcome_members
                else "PENDING"
            )
            result.append(
                dict(
                    common,
                    id=face["step_id"],
                    feed_id=face["id"],
                    segment_index=face["feed_cursor"] // 4 + 1,
                    members=members,
                    client_request_id=face["current_client_request_id"],
                    transport_task_id=face["current_transport_task_id"],
                    operation_id=view.operation_id,
                    consumed_revision=face["consumed_outcome_revision"],
                    status=status,
                    pending=status != "SUCCEEDED",
                )
            )
    return result


async def configure_rack_positions(session, line, *, five_capacity=None, capacity=1):
    from app.models import WorkLinePosition

    seen = set()
    for role in ("FIVE_RACK", "RETURN_RACK", "TRANSFER_RACK", "OUTPUT_RACK"):
        code = line.position_bindings.get(role)
        if code and code not in seen:
            session.add(
                WorkLinePosition(
                    work_line_id=line.id,
                    position_code=code,
                    capacity=five_capacity if role == "FIVE_RACK" else capacity,
                )
            )
            seen.add(code)
    await session.flush()


async def set_position_capacity(session, line_id, value, *, role="FIVE_RACK"):
    from sqlalchemy import select

    from app.models import WorkLine, WorkLinePosition

    line = await session.get(WorkLine, line_id)
    row = await session.scalar(
        select(WorkLinePosition).where(
            WorkLinePosition.work_line_id == line_id,
            WorkLinePosition.position_code == line.position_bindings[role],
        )
    )
    row.capacity = value


def rack_task_admission(production):
    from types import SimpleNamespace

    async def allows_task_entry(session, *, work_line_id, task_id):
        current = await production.read_current_task(session, work_line_id=work_line_id)
        return current.is_valid and current.task_id == task_id

    return SimpleNamespace(allows_task_entry=allows_task_entry)


def fixture_supply_order(tables, *, owner):
    async def read(session, *, work_line_id, task_id):
        from sqlalchemy import select

        from app.businesses._components.bin_roller.ports import SourceFaceRef

        face = tables["face_work"]
        rows = (
            await session.execute(
                select(face)
                .where(
                    face.c.owner_domain == owner,
                    face.c.work_line_id == work_line_id,
                    face.c.scope_id == task_id,
                    face.c.purpose == "SUPPLY",
                )
                .order_by(face.c.plan_revision, face.c.id)
            )
        ).mappings()
        return tuple(SourceFaceRef(r["plan_revision"], r["rack_id"], r["rack_face"]) for r in rows)

    return read


def allocated(flow, **changes):
    return {
        "result": "ALLOCATED",
        "workline_code": flow.workline,
        "rack_id": "RACK-1",
        "bin_code": "BIN-A002",
        "bin_location": "B",
        "cell_id": "BIN-A002-1",
        "cell_index": 7,
        "bin_type": "3",
        "reel_totalthickness": 120,
        **changes,
    }


async def reach_move(flow, *, timestamp=1793206799000, changes=None):
    assert (
        await flow.post(
            flow.event(timestamp=timestamp, changes={"thickness_mm": 20, **(changes or {})})
        )
    ).status_code == 200
    row = (await flow.rows())[-1]
    assert await flow.service.advance(row["id"]) == "AWAITING_VALIDATION_RESULT"
    assert await flow.dispatch() == 1
    assert await flow.service.advance(row["id"]) == "AWAITING_MOVE_RESULT"
    return (await flow.rows())[-1]


async def reach_allocate(flow, *, timestamp=1793206799000, changes=None):
    row = await reach_move(flow, timestamp=timestamp, changes=changes)
    assert await flow.dispatch() == 1
    assert (await flow.result(row)).status_code == 200
    assert await flow.service.scan_pending() >= 1
    assert await flow.dispatch() == 1, "applied original MOVE_FORWARD must enqueue storage_allocate"
    assert (
        json.loads(flow.peer.requests[-1])["operation"]
        == "inbound.coarse_sorting.storage_allocate@v1"
    )
    return row


def enable_racks(flow):
    from dataclasses import replace

    from app.businesses.coarse_sorting import composition
    from app.integrations.wms_request import WmsRequests

    original = flow.service.capabilities.wms.contract_consumers
    wms = WmsRequests(
        flow.outbox,
        owner_domain="coarse_sorting",
        contract_consumers=lambda key: (
            frozenset({"coarse_sorting"}) if key[1] == "rack.departure_decide@v1" else original(key)
        ),
    )
    flow.service = composition.build_rack_service(
        replace(flow.service.capabilities, wms=wms),
        output_locations=flow.output_locations,
        clock=lambda: flow.now,
    )


async def message_history_view(session, message):
    if message is None:
        return None
    from types import SimpleNamespace

    from app.integrations.history import read_bodies

    refs = {
        "request_raw": message.request_record_id,
        "response_raw": message.terminal_response_record_id,
        "last_response_raw": message.last_response_record_id,
    }
    bodies = await read_bodies(
        session, record_ids=tuple(identity for identity in refs.values() if identity)
    )
    values = {c.key: getattr(message, c.key) for c in message.__table__.c}
    values.update(
        {
            name: bodies[identity].raw_body if identity in bodies else None
            for name, identity in refs.items()
        }
    )
    return SimpleNamespace(**values)


async def set_message_bodies(session, message, **values):
    from app.core.identifiers import uuid7
    from app.integrations.history import RecordDraft, write_record

    for name, raw in values.items():
        field = {
            "request_raw": "request_record_id",
            "response_raw": "terminal_response_record_id",
            "last_response_raw": "last_response_record_id",
        }[name]
        identity = (
            None
            if raw is None
            else await write_record(
                session,
                draft=RecordDraft(
                    exchange_id=message.exchange_id,
                    record_kind="REQUEST_REGISTERED"
                    if name == "request_raw"
                    else "HTTP_RESPONSE_OBSERVED",
                    event_key="TEST_SEED:" + str(uuid7()),
                    direction="OUTBOUND" if name == "request_raw" else "INBOUND",
                    raw_body=raw,
                ),
            )
        )
        setattr(message, field, identity)


async def update_message_bodies(session, statement, **values):
    from app.models import ReliableMessage

    names = {"request_raw", "response_raw", "last_response_raw"}
    fields = {key: value for key, value in values.items() if key not in names}
    if not fields:
        fields = {"updated_at": ReliableMessage.updated_at}
    ids = list(await session.scalars(statement.values(**fields).returning(ReliableMessage.id)))
    for identity in ids:
        message = await session.get(ReliableMessage, identity)
        await set_message_bodies(
            session, message, **{key: value for key, value in values.items() if key in names}
        )


async def seed_transport_task(session, task):
    from app.models import IntegrationExchange

    root = IntegrationExchange(
        role="OUTGOING",
        protocol="wms-transport",
        peer_system="WMS",
        direction="OUTBOUND",
        interaction_kind="REQUEST",
        identity_kind="OPERATION_ID",
        identity_value=task.operation_id,
        operation="transport.task.submit@v1",
        operation_id=task.operation_id,
        owner_domain=task.owner_domain,
        created_at=task.created_at,
        coverage="PARTIAL",
    )
    session.add(root)
    await session.flush()
    task.submission_exchange_id = root.id
    session.add(task)
    await session.flush()
    return task


async def seed_reliable_message(session, **values):
    from app.models import ReliableMessage

    raw = {
        name: values.pop(name, None)
        for name in ("request_raw", "response_raw", "last_response_raw")
    }
    message = ReliableMessage(**values)
    from app.integrations.history import register
    from app.reliable import OutgoingMessage

    exchange_id, request_id = await register(
        session,
        message=OutgoingMessage(
            message.target,
            message.operation,
            message.operation_id,
            raw["request_raw"],
            message.business_key,
            message.trace_id,
            message.created_at,
            message.expires_at,
        ),
        protocol="opaque",
    )
    message.exchange_id, message.request_record_id = exchange_id, request_id
    values = {
        name: raw[name] for name in ("response_raw", "last_response_raw") if raw[name] is not None
    }
    session.add(message)
    if values:
        await set_message_bodies(session, message, **values)
    await session.flush()
    return message


def copy_history_tables(metadata):
    from sqlalchemy import Sequence

    from app.models import IntegrationExchange, IntegrationRecord, IntegrationReference
    from app.models.integration_history import RECEIPT_SEQUENCE

    for model in (IntegrationExchange, IntegrationRecord, IntegrationReference):
        if model.__table__.key not in metadata.tables:
            model.__table__.to_metadata(metadata)
    Sequence(RECEIPT_SEQUENCE.name, schema=RECEIPT_SEQUENCE.schema, metadata=metadata)


async def get_receipt_by_id(session, receipt_id):
    from sqlalchemy import select

    from app.integrations.callback_receipts import find_receipt
    from app.models import IntegrationExchange

    original = (
        await session.execute(
            select(IntegrationExchange.operation, IntegrationExchange.operation_id).where(
                IntegrationExchange.role == "RECEIPT",
                IntegrationExchange.receipt_active.is_(True),
                IntegrationExchange.receipt_id == receipt_id,
            )
        )
    ).one_or_none()
    return (
        await find_receipt(session, original.operation, original.operation_id) if original else None
    )


async def set_submission_acceptance(session, task, evidence):
    from app.core.identifiers import uuid7
    from app.integrations.history import RecordDraft, write_record

    for slot, field in (
        ("request", "acceptance_request_record_id"),
        ("response", "acceptance_response_record_id"),
    ):
        identity = await write_record(
            session,
            draft=RecordDraft(
                exchange_id=task.submission_exchange_id,
                record_kind="REQUEST_REGISTERED" if slot == "request" else "HTTP_RESPONSE_OBSERVED",
                event_key="TEST_ACCEPTANCE:" + str(uuid7()),
                direction="OUTBOUND" if slot == "request" else "INBOUND",
                raw_body=evidence[slot + "_raw"].encode(),
            ),
        )
        setattr(task, field, identity)
    task.submission_acceptance = {
        **{k: v for k, v in evidence.items() if k not in {"request_raw", "response_raw"}},
        "format_version": 2,
    }
