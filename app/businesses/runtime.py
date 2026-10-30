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
    if ecs_commands is None and any(
        entry.COMMANDS or any(c.commands for c in catalog.components(entry)) for entry in entries
    ):
        raise ValueError("Business commands require the application ECS capability")
    if not callable(session_factory):
        raise ValueError("Business runtime requires an application session factory")
    services, progressions = {}, {}
    for entry in entries:
        capabilities = Capabilities(
            ecs_commands,
            WmsRequests(
                outbox,
                owner_domain=entry.DEFINITION.code,
                contract_consumers=catalog.contract_consumers,
            ),
            TransportService(outbox, owner_domain=entry.DEFINITION.code),
            session_factory,
        )
        service = entry.build_service(capabilities)
        if service is None:
            raise ValueError(f"{entry.DEFINITION.code}: business factory must return a service")
        declared_components = catalog.components(entry)
        if declared_components:
            parts = getattr(service, "components", None)
            if not isinstance(parts, Mapping) or set(parts) != {c.key for c in declared_components}:
                raise ValueError(
                    f"{entry.DEFINITION.code}: service components must match declarations"
                )
            for component in declared_components:
                part = parts[component.key]
                if part is None or any(
                    not callable(getattr(part, name, None)) for name in component.required_methods
                ):
                    raise ValueError(
                        f"{entry.DEFINITION.code}: component {component.key} methods missing"
                    )
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
        definitions={d.code: d for d in catalog.definitions()},
        services=services,
        progressions=progressions,
    )


def require_runtime(ctx) -> BusinessRuntime:
    runtime = getattr(ctx, "runtime", None)
    if not isinstance(runtime, BusinessRuntime):
        raise RuntimeError("BusinessRuntime must be supplied by trusted application assembly")
    return runtime


def require_component(ctx, key: str) -> object:
    runtime = require_runtime(ctx)
    business = getattr(ctx, "business_code", None)
    if not business or getattr(ctx, "component_key", None) != key:
        raise RuntimeError("A component requires the receiver-resolved business target")
    service = runtime.services.get(business)
    parts = getattr(service, "components", None)
    if not isinstance(parts, Mapping) or key not in parts:
        raise RuntimeError("Business component is not assembled")
    return parts[key]


def bind_callback_business(ctx, business_code: str) -> None:
    """Bind a shared callback to the business established from its original record."""
    from app.integrations.wms_callback.registry import Rejected

    runtime = require_runtime(ctx)
    target = catalog.input_target(business_code, ("wms_callback", ctx.operation, None))
    if (
        target is None
        or target.source != ctx.registration_source
        or business_code not in runtime.services
        or (ctx.business_code is not None and ctx.business_code != business_code)
    ):
        raise Rejected("INVALID_EVIDENCE")
    ctx._business_binding.business_code = business_code
    ctx._business_binding.component_key = target.component_key
