"""Stateless business services assembled from the application's existing capabilities."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from inspect import iscoroutinefunction, signature

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.businesses import catalog
from app.businesses.definitions import BusinessDefinition
from app.integrations.ecs_command import EcsCommands
from app.integrations.wms_request import WmsRequests
from app.integrations.wms_transport import TransportService


@dataclass(frozen=True)
class Capabilities:
    ecs: EcsCommands | None
    wms: WmsRequests
    transport: TransportService
    session_factory: async_sessionmaker[AsyncSession]


@dataclass(frozen=True)
class BusinessRuntime:
    definitions: Mapping[str, BusinessDefinition]
    services: Mapping[str, object]
    progressions: Mapping[str, Callable[[], Awaitable[int]]] = field(default_factory=dict)


def build_runtime(*, outbox, ecs_commands, session_factory) -> BusinessRuntime:
    entries = catalog.validated_entries()
    if not entries:
        return BusinessRuntime(definitions={}, services={})
    if outbox is None:
        raise ValueError("Business runtime requires the application outbox")
    if ecs_commands is None and any(entry.COMMANDS for entry in entries):
        raise ValueError("Business commands require the application ECS capability")
    if not callable(session_factory):
        raise ValueError("Business runtime requires an application session factory")
    capabilities = Capabilities(
        ecs_commands,
        WmsRequests(outbox),
        TransportService(outbox),
        session_factory,
    )
    services, progressions = {}, {}
    for entry in entries:
        service = entry.build_service(capabilities)
        if service is None:
            raise ValueError(f"{entry.DEFINITION.code}: business factory must return a service")
        services[entry.DEFINITION.code] = service
        method = getattr(entry, "PROGRESSION", None)
        if method is not None:
            pending = getattr(service, method, None)
            if not iscoroutinefunction(pending):
                raise ValueError(f"{entry.DEFINITION.code}: PROGRESSION requires an async method")
            try:
                signature(pending).bind()
            except TypeError as error:
                raise ValueError(
                    f"{entry.DEFINITION.code}: PROGRESSION must be callable without arguments"
                ) from error
            progressions[entry.DEFINITION.code] = pending
    return BusinessRuntime(
        definitions={entry.DEFINITION.code: entry.DEFINITION for entry in entries},
        services=services,
        progressions=progressions,
    )


def require_runtime(ctx) -> BusinessRuntime:
    runtime = getattr(ctx, "runtime", None)
    if not isinstance(runtime, BusinessRuntime):
        raise RuntimeError("BusinessRuntime must be supplied by trusted application assembly")
    return runtime
