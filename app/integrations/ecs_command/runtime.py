"""Application assembly for one ECS channel; all physical origins come from routes."""

from dataclasses import replace

from app.http_client import AsyncHttpClient
from app.integrations.ecs_command.clients import DynamicClients
from app.integrations.ecs_command.lifecycle import EcsLifecycle
from app.integrations.ecs_command.protocol import PROTOCOL, EcsCommandAdapter
from app.integrations.ecs_command.service import EcsCommands
from app.reliable.config import TargetConfig, parse_reliable_config

# Preserve persisted message identities; this internal name is not a user-configured target.
TARGET = "ecs-local"


def check_assembly(app):
    if (
        getattr(app.state, "ecs_commands", None) is not None
        or getattr(app.state, "reliable_client_provider", None) is not None
        or PROTOCOL in getattr(app.state, "reliable_adapters", {})
        or PROTOCOL in getattr(app.state, "reliable_lifecycles", {})
    ):
        raise ValueError("ECS assembly conflict; preserve existing service and mappings")


def configure_ecs(app, *, routes, annexes, client_factory=AsyncHttpClient):
    check_assembly(app)
    for entry in getattr(routes, "bindings", {}).values():
        if (entry.annex_id, entry.annex_version) not in annexes:
            raise ValueError(f"{entry.registration_source.location}: confirmed annex required")
    adapters = dict(getattr(app.state, "reliable_adapters", {}))
    lifecycles = dict(getattr(app.state, "reliable_lifecycles", {}))
    adapters[PROTOCOL] = EcsCommandAdapter()
    service = EcsCommands(None, TARGET, routes, annexes)
    lifecycles[PROTOCOL] = EcsLifecycle(service)
    provider = DynamicClients(factory=client_factory)
    app.state.ecs_commands = service
    app.state.reliable_adapters = adapters
    app.state.reliable_lifecycles = lifecycles
    app.state.reliable_client_provider = provider
    return service


def ecs_runtime_config(settings, service):
    config = parse_reliable_config(settings)
    if any(t.protocol == PROTOCOL for t in config.targets.values()):
        raise ValueError(
            "ECS channel is framework-managed; remove ECS targets from RELIABLE_TARGETS. "
            "Provide device routes or authorized request ecs_url instead"
        )
    if service is None:
        return config
    if service.target in config.targets:
        raise ValueError("ECS channel conflicts with an existing reliable target")
    target = TargetConfig(
        **{
            "timeout_ms": settings.RELIABLE_DEFAULT_TIMEOUT_MS,
            "max_body_bytes": settings.RELIABLE_MAX_BODY_BYTES,
            "max_retry_after_s": settings.RELIABLE_MAX_RETRY_AFTER_S,
            **settings.RELIABLE_TARGETS.get("defaults", {}),
            "name": service.target,
            "base_url": None,
            "protocol": PROTOCOL,
            "address_mode": "SNAPSHOT",
        }
    )
    return replace(config, targets={**config.targets, service.target: target})
