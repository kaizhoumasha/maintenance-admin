"""Local commissioning with request origins, the built-in protocol contract and permission."""

from collections.abc import Mapping
from dataclasses import asdict
from datetime import timedelta
from ipaddress import ip_address

import httpx
from pydantic import ConfigDict, RootModel
from sqlalchemy import select

from app.core.errors import AppError, ErrorCode
from app.http_client import AsyncHttpClient
from app.integrations.ecs_command.contracts import (
    PROTOCOL_RETRY_WINDOW,
    DeviceAnnex,
    ErrorDetail,
    merge_annexes,
)
from app.integrations.ecs_command.routing import RouteAuthorization, RouteSnapshot, normalize_origin
from app.integrations.ecs_command.runtime import check_assembly, configure_ecs
from app.integrations.ecs_command.service import EcsCommands
from app.models import EcsCommand


def debug_origin(value: str) -> str:
    try:
        url = httpx.URL(value)
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError
        origin = normalize_origin(value)
        address = ip_address(httpx.URL(origin).host)
        if address.is_unspecified or address.is_multicast or getattr(address, "scope_id", None):
            raise ValueError
        return str(httpx.URL(origin).copy_with(host=address.compressed))
    except (ValueError, httpx.InvalidURL):
        raise ValueError(
            "ecs_url requires an HTTP(S) IP origin without credentials or path"
        ) from None


class ProtocolObject(RootModel[dict]):
    model_config = ConfigDict(strict=True)


PROTOCOL_ANNEX = ("ecs.dev.protocol", "1")
PROTOCOL_MODELS = DeviceAnnex(ProtocolObject, ProtocolObject, ErrorDetail, 0, 2**63 - 1)


class DevEcsRoutes:
    POLICY = "dev.ecs.network"
    VERSION = "3"

    def __init__(self, *, origin=None):
        self.origin = origin

    def for_origin(self, origin):
        return DevEcsRoutes(origin=origin)

    async def resolve(self, session, device_code, task_type):
        return RouteSnapshot(
            device_code,
            self.origin,
            self.POLICY,
            self.VERSION,
            *PROTOCOL_ANNEX,
            (task_type,),
            PROTOCOL_RETRY_WINDOW,
        )

    async def authorize_frozen(self, session, route):
        if (route.get("route_id"), route.get("version")) != (self.POLICY, self.VERSION):
            return RouteAuthorization.UNKNOWN
        try:
            original = RouteSnapshot(
                route["device_code"],
                debug_origin(route["origin"]),
                self.POLICY,
                self.VERSION,
                *PROTOCOL_ANNEX,
                tuple(route["commands"]),
                timedelta(seconds=route["retry_window"]),
            )
            expected = asdict(original)
            expected["retry_window"] = original.retry_window.total_seconds()
            if len(original.commands) != 1 or {**route, "commands": original.commands} != expected:
                return RouteAuthorization.UNKNOWN
        except (AppError, KeyError, TypeError, ValueError, OverflowError):
            return RouteAuthorization.UNKNOWN
        return RouteAuthorization.ALLOW


class LocalEcsRoutes:
    def __init__(self, formal, debug):
        self.formal, self.debug = formal, debug

    @property
    def bindings(self):
        return self.formal.bindings

    async def resolve(self, session, device_code, task_type):
        return await self.formal.resolve(session, device_code, task_type)

    def for_origin(self, origin):
        return self.debug.for_origin(origin)

    async def authorize_frozen(self, session, route):
        if str(route.get("route_id", "")).startswith("device.route."):
            return await self.formal.authorize_frozen(session, route)
        return await self.debug.authorize_frozen(session, route)


async def get_debug_command(session, *, service, owner, command_code):
    command = await service.get_command(session, owner=owner, command_code=command_code)
    if command is None:
        return None
    row = await session.get(EcsCommand, command.command_code)
    return command, row.route["origin"]


async def create_debug_command(
    session,
    *,
    service,
    owner,
    client_request_id,
    ecs_url,
    device_code,
    task_type,
    timeout_ms,
    params,
    authorize_send,
):
    try:
        origin = debug_origin(ecs_url)
    except ValueError:
        raise AppError(ErrorCode.INVALID_REQUEST, "Invalid ecs_url IP origin") from None
    existing = await service.get_by_call(session, owner=owner, call_key=client_request_id)
    context = service
    if existing is None:
        routes = service.routes.for_origin(origin)
        await authorize_send()
        context = EcsCommands(
            service.outbox, service.target, routes, service.annexes, clock=service.clock
        )
    try:
        command = await context.create(
            session,
            owner=owner,
            call_key=client_request_id,
            device_code=device_code,
            task_type=task_type,
            timeout_ms=timeout_ms,
            params=params,
        )
    except ValueError as error:
        if str(error) == "business identity has different input":
            raise AppError(
                ErrorCode.CONFLICT, "client_request_id already has different input"
            ) from None
        raise AppError(
            ErrorCode.INVALID_REQUEST, "Invalid ECS command for protocol contract"
        ) from None
    row = await session.scalar(
        select(EcsCommand).where(EcsCommand.command_code == command.command_code)
    )
    if row.route["origin"] != origin:
        raise AppError(ErrorCode.CONFLICT, "client_request_id already has a different ecs_url")
    return command, row.route["origin"]


def configure_local_ecs(
    app,
    *,
    environment,
    history_annexes=None,
    client_factory=AsyncHttpClient,
    routes=None,
    annexes=None,
):
    if environment != "local":
        if (
            history_annexes
            or annexes
            or client_factory is not AsyncHttpClient
            or routes is not None
        ):
            raise ValueError("ECS debug configuration requires ENVIRONMENT=local")
        return
    check_assembly(app)
    if history_annexes is not None and not isinstance(history_annexes, Mapping):
        raise ValueError("history_annexes must be a mapping of original contracts")
    merged = merge_annexes({}, history_annexes or {}, source="history_annexes")
    merged = merge_annexes(merged, annexes or {}, source="formal annexes")
    for key, models in merged.items():
        if (
            not isinstance(key, tuple)
            or len(key) != 2
            or any(
                not isinstance(value, str) or not value.strip() or "\x00" in value for value in key
            )
            or not isinstance(models, DeviceAnnex)
            or key == PROTOCOL_ANNEX
        ):
            raise ValueError("Invalid history_annexes; ecs.dev.protocol/1 is reserved")
    merged[PROTOCOL_ANNEX] = PROTOCOL_MODELS
    return configure_ecs(
        app,
        routes=LocalEcsRoutes(routes, DevEcsRoutes()) if routes is not None else DevEcsRoutes(),
        annexes=merged,
        client_factory=client_factory,
    )
