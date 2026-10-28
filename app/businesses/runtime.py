"""Stateless business services assembled from the application's existing capabilities."""

from collections.abc import Mapping
from dataclasses import dataclass

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


@dataclass(frozen=True)
class BusinessRuntime:
    definitions: Mapping[str, BusinessDefinition]
    services: Mapping[str, object]


def build_runtime(*, outbox, ecs_commands) -> BusinessRuntime:
    entries = catalog.validated_entries()
    if not entries:
        return BusinessRuntime(definitions={}, services={})
    if outbox is None:
        raise ValueError("Business runtime requires the application outbox")
    if ecs_commands is None and any(entry.COMMANDS for entry in entries):
        raise ValueError("Business commands require the application ECS capability")
    capabilities = Capabilities(ecs_commands, WmsRequests(outbox), TransportService(outbox))
    services = {}
    for entry in entries:
        service = entry.build_service(capabilities)
        if service is None:
            raise ValueError(f"{entry.DEFINITION.code}: business factory must return a service")
        services[entry.DEFINITION.code] = service
    return BusinessRuntime(
        definitions={entry.DEFINITION.code: entry.DEFINITION for entry in entries},
        services=services,
    )


def require_runtime(ctx) -> BusinessRuntime:
    runtime = getattr(ctx, "runtime", None)
    if not isinstance(runtime, BusinessRuntime):
        raise RuntimeError("BusinessRuntime must be supplied by trusted application assembly")
    return runtime
