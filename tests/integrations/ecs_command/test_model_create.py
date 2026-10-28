"""Model creation preserves legacy identity and requires the exact active role contract."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, field_serializer
from sqlalchemy import func, select

from app.core.identifiers import uuid7
from app.integrations.ecs_command import (
    RoleRoutes,
    compile_commands,
    ecs_command,
)
from app.integrations.ecs_command.contracts import ErrorDetail
from app.integrations.ecs_command.dev import DevEcsRoutes, LocalEcsRoutes, configure_local_ecs
from app.integrations.ecs_command.result import receive_ecs_result
from app.models import EcsCommand, ReliableMessage
from tests.helpers import ecs_binding


class Item(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    qty: int


class Outcome(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    qty: int


@ecs_command(
    "MOVE",
    device_role="SCAN1",
    annex_id="model",
    annex_version="1",
    result=Outcome,
    error=ErrorDetail,
    timeout_min_ms=1,
    timeout_max_ms=60000,
)
class Move(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", serialize_by_alias=True)
    qty: int = Field(alias="quantity")
    items: list[Item] = Field(default_factory=list)
    label: str = "move"
    optional: str | None = None

    @field_serializer("label")
    def serialize_label(self, value):
        return value.upper()


@pytest.fixture
async def declared(ecs):
    bindings, annexes = compile_commands(Move)
    state = SimpleNamespace(
        binding=await ecs_binding(None, "D1"),
        origin="http://127.0.0.1:45871",
        route_id="device.route." + str(uuid7()),
        calls=0,
    )

    async def binding(session, code):
        state.calls += 1
        return state.binding

    async def route(session, bound):
        return state.origin, state.route_id

    state.routes = RoleRoutes(
        bindings, annexes=annexes, resolve_binding=binding, resolve_route=route
    )
    state.binding_resolver, state.route_resolver = binding, route
    state.annexes = annexes
    ecs.service.routes, ecs.service.annexes = state.routes, dict(annexes)
    return state


def arguments(ecs, **changes):
    return (
        dict(
            owner=ecs.owner,
            call_key="model-call",
            device_code="D1",
            timeout_ms=30000,
            command=Move(quantity=1),
        )
        | changes
    )


async def assert_empty(ecs, session):
    assert await ecs.service.get_by_call(session, owner=ecs.owner, call_key="model-call") is None
    for model in (EcsCommand, ReliableMessage):
        assert await session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize("first_model", [False, True])
async def test_model_and_legacy_calls_share_original_bytes_and_deadline(ecs, declared, first_model):
    item = Item(qty=2)
    model = arguments(ecs, command=Move(quantity=1, items=[item]))
    legacy = {k: v for k, v in model.items() if k != "command"} | dict(
        task_type="MOVE",
        params={"quantity": 1, "items": [{"qty": 2}], "label": "MOVE", "optional": None},
    )
    async with ecs.factory.begin() as session:
        first = await ecs.service.create(session, **(model if first_model else legacy))
        row = await session.get(EcsCommand, first.command_code)
        frozen = (row.input_raw, row.request_raw, row.route, row.delivery_snapshot, row.expires_at)
        assert json.loads(row.request_raw)["params"] == legacy["params"]
        assert row.expires_at - row.created_at == timedelta(seconds=300)
    declared.binding = None
    declared.origin = "http://127.0.0.1:45872"
    calls = declared.calls
    async with ecs.factory.begin() as session:
        second = await ecs.service.create(session=session, **(legacy if first_model else model))
        assert second.command_code == first.command_code
        assert declared.calls == calls
        assert model["command"].label == "move" and model["command"].items[0] is item
        row = await session.get(EcsCommand, first.command_code)
        assert (
            row.input_raw,
            row.request_raw,
            row.route,
            row.delivery_snapshot,
            row.expires_at,
        ) == frozen
        assert (await session.scalar(select(ReliableMessage))).expires_at == frozen[-1]
        with pytest.raises(ValueError, match="different input"):
            await ecs.service.create(session, **arguments(ecs, command=Move(quantity=2)))
        assert declared.calls == calls
        assert await session.scalar(select(func.count()).select_from(EcsCommand)) == 1
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 1


@pytest.mark.parametrize("first_model", [False, True])
async def test_model_creation_preserves_actual_values_with_reciprocal_aliases(
    ecs, declared, first_model
):
    @ecs_command(
        "MOVE",
        device_role="SCAN1",
        annex_id="reciprocal",
        annex_version="1",
        result=Outcome,
        error=ErrorDetail,
        timeout_min_ms=1,
        timeout_max_ms=60000,
    )
    class Params(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid", serialize_by_alias=True)
        first: int = Field(alias="second")
        second: int = Field(alias="first")
        item: Item

    bindings, annexes = compile_commands(Params)
    ecs.service.routes = RoleRoutes(
        bindings,
        annexes=annexes,
        resolve_binding=declared.binding_resolver,
        resolve_route=declared.route_resolver,
    )
    ecs.service.annexes = annexes
    item = Item(qty=3)
    payload = Params(second=11, first=22, item=item)
    model = arguments(ecs, command=payload)
    legacy = {key: value for key, value in model.items() if key != "command"} | dict(
        task_type="MOVE", params={"second": 11, "first": 22, "item": {"qty": 3}}
    )
    async with ecs.factory.begin() as session:
        first = await ecs.service.create(session, **(model if first_model else legacy))
        row = await session.get(EcsCommand, first.command_code)
        assert json.loads(row.request_raw)["params"] == {
            "second": 11,
            "first": 22,
            "item": {"qty": 3},
        }
        frozen = (row.input_raw, row.request_raw, row.route, row.delivery_snapshot, row.expires_at)
    async with ecs.factory.begin() as session:
        again = await ecs.service.create(session, **(legacy if first_model else model))
        assert again.command_code == first.command_code
        row = await session.get(EcsCommand, first.command_code)
        assert (
            row.input_raw,
            row.request_raw,
            row.route,
            row.delivery_snapshot,
            row.expires_at,
        ) == frozen
        assert await session.scalar(select(func.count()).select_from(EcsCommand)) == 1
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 1
    assert (payload.first, payload.second) == (11, 22)
    assert payload.item is item and item.qty == 3


@pytest.mark.parametrize("timeout_ms", [True, "30000", -1, 0, 60001])
async def test_model_timeout_rejection_leaves_no_obligation(ecs, declared, timeout_ms):
    async with ecs.factory.begin() as session:
        with pytest.raises(ValueError):
            await ecs.service.create(session, **arguments(ecs, timeout_ms=timeout_ms))
        await assert_empty(ecs, session)


@pytest.mark.parametrize(
    "shape", ["mixed-task", "mixed-params", "missing", "missing-params", "missing-task"]
)
async def test_create_rejects_mixed_and_incomplete_shapes(ecs, declared, shape):
    values = arguments(ecs)
    if shape == "mixed-task":
        values["task_type"] = "MOVE"
    elif shape == "mixed-params":
        values["params"] = None
    else:
        values.pop("command")
        if shape == "missing-params":
            values["task_type"] = "MOVE"
        if shape == "missing-task":
            values["params"] = {}
    async with ecs.factory.begin() as session:
        with pytest.raises(TypeError):
            await ecs.service.create(session, **values)
        await assert_empty(ecs, session)


@pytest.mark.parametrize(
    "invalid",
    ["undeclared", "subclass", "construct", "field", "nested", "extra", "cycle", "not-model"],
)
async def test_model_creation_revalidates_exact_original_model(ecs, declared, invalid):
    command = Move(quantity=1, items=[Item(qty=2)])
    if invalid == "undeclared":
        command = Item(qty=1)
    elif invalid == "subclass":

        class Child(Move):
            pass

        command = Child(quantity=1)
    elif invalid == "construct":
        command = Move.model_construct(qty="1")
    elif invalid == "field":
        command.qty = True
    elif invalid == "nested":
        command.items[0].qty = "2"
    elif invalid == "extra":
        command.items[0].__dict__["untrusted"] = 1
    elif invalid == "cycle":
        command.items.append(command.items)
    else:
        command = {}
    async with ecs.factory.begin() as session:
        with pytest.raises(ValueError):
            await ecs.service.create(session, **arguments(ecs, command=command))
        await assert_empty(ecs, session)


async def test_model_creation_rolls_back_with_caller_transaction(ecs, declared):
    async with ecs.factory() as session:
        await ecs.service.create(session, **arguments(ecs))
        await session.rollback()
    async with ecs.factory() as session:
        await assert_empty(ecs, session)


@pytest.mark.parametrize(
    "mismatch",
    [
        "role",
        "device",
        "missing",
        "task",
        "annex",
        "params",
        "result",
        "error",
        "timeout",
        "service-annex",
        "service-missing",
    ],
)
async def test_model_creation_requires_exact_active_role_and_contract(ecs, declared, mismatch):
    bindings, annexes = compile_commands(Move)
    binding = bindings[0]
    if mismatch == "role":
        declared.binding = replace(declared.binding, device_role="SCAN4")
        bindings = (replace(binding, device_role="SCAN4"),)
    elif mismatch == "device":
        declared.binding = replace(declared.binding, device_code="D2")
    elif mismatch == "missing":
        bindings = ()
    elif mismatch == "task":
        bindings = (replace(binding, task_type="OTHER"),)
    elif mismatch == "annex":
        bindings = (replace(binding, annex_version="2"),)
        annexes = {("model", "2"): annexes["model", "1"]}
    elif mismatch in {"params", "result", "error", "timeout", "service-annex"}:

        class OtherError(ErrorDetail):
            note: str = "detail"

        changes = {
            "params": dict(params_model=Outcome),
            "result": dict(result_model=Item),
            "error": dict(error_model=OtherError),
            "timeout": dict(timeout_max_ms=59999),
            "service-annex": dict(result_model=Item),
        }[mismatch]
        altered = {("model", "1"): replace(annexes["model", "1"], **changes)}
        if mismatch == "service-annex":
            ecs.service.annexes = altered
        else:
            annexes = altered
    elif mismatch == "service-missing":
        ecs.service.annexes = {}
    ecs.service.routes = RoleRoutes(
        bindings,
        annexes=annexes,
        resolve_binding=declared.binding_resolver,
        resolve_route=declared.route_resolver,
    )
    async with ecs.factory.begin() as session:
        with pytest.raises(ValueError):
            await ecs.service.create(session, **arguments(ecs))
        await assert_empty(ecs, session)
        assert declared.calls == 1


@pytest.mark.parametrize("routes", ["ordinary", "debug", "local-ordinary"])
async def test_model_creation_requires_explicit_declared_route_capability(ecs, declared, routes):
    ecs.service.routes = {
        "ordinary": ecs.routes,
        "debug": DevEcsRoutes().for_origin("http://127.0.0.1:45871"),
        "local-ordinary": LocalEcsRoutes(ecs.routes, DevEcsRoutes()),
    }[routes]
    async with ecs.factory.begin() as session:
        with pytest.raises(ValueError, match="declared|declaration"):
            await ecs.service.create(session, **arguments(ecs))
        await assert_empty(ecs, session)


async def test_shared_manual_bindings_keep_legacy_creation(ecs, declared):
    bindings, annexes = compile_commands(Move)
    ecs.service.routes = RoleRoutes(
        (*bindings, replace(bindings[0], device_role="SCAN4")),
        annexes=annexes,
        resolve_binding=declared.binding_resolver,
        resolve_route=declared.route_resolver,
    )
    declared.binding = replace(declared.binding, device_role="SCAN4")
    async with ecs.factory.begin() as session:
        with pytest.raises(ValueError):
            await ecs.service.create(session, **arguments(ecs))
        command = await ecs.service.create(
            session,
            **{k: v for k, v in arguments(ecs).items() if k != "command"},
            task_type="MOVE",
            params={"quantity": 1},
        )
        assert (
            await ecs.service.get_by_call(session, owner=ecs.owner, call_key="model-call")
        ) == command


async def test_local_model_and_debug_commands_use_one_sender(ecs, declared):
    from app.http_client import DeliveryState, HttpResult
    from app.integrations.ecs_command.clients import DynamicClients
    from app.integrations.ecs_command.dev import create_debug_command
    from app.integrations.ecs_command.lifecycle import EcsLifecycle
    from app.integrations.ecs_command.protocol import PROTOCOL
    from app.reliable.config import ReliableConfig
    from app.reliable.sender import Sender
    from app.reliable.store import MessageStore
    from app.reliable.testing import ScriptedHttpClient

    service = configure_local_ecs(
        FastAPI(), environment="local", routes=declared.routes, annexes=declared.annexes
    )
    service.target, service.outbox = "ecs", ecs.outbox
    async with ecs.factory.begin() as session:
        formal = await service.create(session, **arguments(ecs))

        async def authorized():
            pass

        debug, origin = await create_debug_command(
            session,
            service=service,
            owner=ecs.owner,
            client_request_id="debug-call",
            ecs_url="http://127.0.0.1:45872",
            device_code="D2",
            task_type="CUSTOM",
            timeout_ms=0,
            params={"custom": None},
            authorize_send=authorized,
        )
        assert origin == "http://127.0.0.1:45872"
        rows = (await session.scalars(select(EcsCommand))).all()
        frozen = {row.command_code: row.request_raw for row in rows}
        assert {row.route["origin"] for row in rows} == {declared.origin, origin}
        assert {row.target for row in rows} == {"ecs"}
    ack = HttpResult(
        DeliveryState.HTTP_RESPONSE_COMPLETE,
        None,
        200,
        (("content-type", "application/json"),),
        b'{"code":200,"message":"ACK"}',
    )
    client = ScriptedHttpClient(ack, ack)
    sender = Sender(
        MessageStore(ecs.factory, lifecycles={PROTOCOL: EcsLifecycle(service)}),
        ecs.targets,
        ecs.adapters,
        {},
        provider=DynamicClients(factory=lambda _: client),
        budget=ReliableConfig(ecs.targets, claim_batch_size=1),
    )
    try:
        for _ in range(2):
            assert await sender.tick() == 1
            async with asyncio.timeout(5):
                await asyncio.gather(*list(sender.inflight))
        assert {json.loads(request.body)["command_code"] for request in client.requests} == {
            formal.command_code,
            debug.command_code,
        }
        assert all(
            request.body == frozen[json.loads(request.body)["command_code"]]
            for request in client.requests
        )
        async with ecs.factory() as session:
            assert (
                await service.get_by_call(session, owner=ecs.owner, call_key="model-call")
            ).ack_received
            assert (
                await service.get_by_call(session, owner=ecs.owner, call_key="debug-call")
            ).ack_received
    finally:
        await sender.aclose()


@pytest.mark.parametrize("change", ["version", "rebound", "revoked"])
async def test_restart_keeps_original_result_contract_after_current_route_changes(
    ecs, declared, change
):
    from app.integrations.ecs_command.lifecycle import EcsLifecycle
    from app.integrations.ecs_command.protocol import PROTOCOL
    from app.reliable.store import MessageStore

    async with ecs.factory.begin() as session:
        original = await ecs.service.create(session, **arguments(ecs))
        row = await session.get(EcsCommand, original.command_code)
        frozen = (row.input_raw, row.request_raw, row.route, row.delivery_snapshot, row.expires_at)
    store = MessageStore(ecs.factory, lifecycles={PROTOCOL: EcsLifecycle(ecs.service)})
    claim = (await store.claim(1, ecs.targets))[0]
    assert await store.before_send(claim, ecs.targets["ecs"])

    class OutcomeV2(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")
        token: str

    @ecs_command(
        "MOVE",
        device_role="SCAN1",
        annex_id="model",
        annex_version="2",
        result=OutcomeV2,
        error=ErrorDetail,
        timeout_min_ms=1,
        timeout_max_ms=60000,
    )
    class MoveV2(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")
        token: str

    bindings, active = compile_commands(MoveV2)
    if change == "rebound":
        declared.binding = replace(declared.binding, device_role="SCAN4", device_version=2)
    elif change == "revoked":
        declared.binding = None
    declared.origin = "http://127.0.0.1:45872"
    routes = RoleRoutes(
        bindings,
        annexes=active,
        resolve_binding=declared.binding_resolver,
        resolve_route=declared.route_resolver,
    )
    service = configure_local_ecs(
        FastAPI(),
        environment="local",
        routes=routes,
        annexes=active,
        history_annexes=declared.annexes,
    )
    service.outbox, service.target = ecs.outbox, "ecs"
    async with ecs.factory.begin() as session:
        with pytest.raises(ValueError):
            await service.create(session, **arguments(ecs, call_key="new-v1"))
        replay = await service.create(session, **arguments(ecs))
        assert replay.command_code == original.command_code
        v2 = arguments(ecs, call_key="new-v2", command=MoveV2(token="v2"))
        if change == "version":
            current = await service.create(session, **v2)
            assert current.command_code != original.command_code
        else:
            with pytest.raises(ValueError):
                await service.create(session, **v2)
    raw = json.dumps(
        dict(
            command_code=original.command_code,
            device_code="D1",
            result="SUCCESS",
            finish_time=1,
            data={"qty": 7},
        )
    ).encode()
    async with ecs.factory.begin() as session:
        assert (await receive_ecs_result(session, raw, service)).status_code == 200
        assert (await receive_ecs_result(session, raw, service)).status_code == 200
        conflicting = json.loads(raw) | {"data": {"qty": 8}}
        assert (
            await receive_ecs_result(session, json.dumps(conflicting).encode(), service)
        ).status_code == 409
    async with ecs.factory() as session:
        view = await service.get_by_call(session, owner=ecs.owner, call_key="model-call")
        assert view.status == "SUCCEEDED" and view.result["data"] == {"qty": 7}
        row = await session.get(EcsCommand, original.command_code)
        assert (
            row.input_raw,
            row.request_raw,
            row.route,
            row.delivery_snapshot,
            row.expires_at,
        ) == frozen
        assert row.ack_raw is None


@pytest.mark.parametrize("routes", ["ordinary", "debug", "local-ordinary"])
async def test_existing_model_call_bypasses_current_declared_capability(ecs, declared, routes):
    async with ecs.factory.begin() as session:
        original = await ecs.service.create(session, **arguments(ecs))
    ecs.service.annexes = {}
    ecs.service.routes = {
        "ordinary": ecs.routes,
        "debug": DevEcsRoutes(),
        "local-ordinary": LocalEcsRoutes(ecs.routes, DevEcsRoutes()),
    }[routes]
    async with ecs.factory.begin() as session:
        replay = await ecs.service.create(session, **arguments(ecs))
        assert replay.command_code == original.command_code
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 1
        with pytest.raises(ValueError, match="different input"):
            await ecs.service.create(session, **arguments(ecs, timeout_ms=True))


async def test_non_migratable_dataclass_guidance_names_legacy_ecs_arguments(ecs, declared):
    from pydantic.dataclasses import dataclass

    @dataclass(config=ConfigDict(strict=True, extra="forbid"))
    class FrozenField:
        qty: int = Field(frozen=True)

    @ecs_command(
        "MOVE",
        device_role="SCAN1",
        annex_id="frozen-field",
        annex_version="1",
        result=Outcome,
        error=ErrorDetail,
        timeout_min_ms=1,
        timeout_max_ms=60000,
    )
    class Params(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")
        data: FrozenField

    async with ecs.factory.begin() as session:
        with pytest.raises(ValueError, match="legacy task_type/params"):
            await ecs.service.create(session, **arguments(ecs, command=Params(data=FrozenField(1))))
        await assert_empty(ecs, session)
