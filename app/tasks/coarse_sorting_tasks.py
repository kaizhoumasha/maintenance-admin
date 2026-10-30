"""Register coarse sorting's next durable intent; the application Sender delivers it."""

from functools import partial

from app.business_wakeups import enqueue_advance
from app.businesses import catalog
from app.businesses.runtime import build_runtime
from app.core import get_settings
from app.integrations.ecs_command import EcsCommands
from app.integrations.ecs_command.lifecycle import EcsLifecycle
from app.integrations.ecs_command.protocol import PROTOCOL, EcsCommandAdapter
from app.integrations.ecs_command.runtime import TARGET, ecs_runtime_config
from app.integrations.wms_request import ADAPTERS as WMS_ADAPTERS
from app.integrations.wms_transport import ADAPTERS as TRANSPORT_ADAPTERS
from app.reliable import Outbox
from app.services.device_routes import DeviceRoutes
from app.tasks._db import run_with_factory
from app.tasks.celery_app import celery_app


def _runtime(factory):
    bindings, annexes = catalog.validate_business_contracts()
    commands = EcsCommands(None, TARGET, DeviceRoutes(bindings, annexes=annexes), annexes)
    config = ecs_runtime_config(get_settings(), commands)
    outbox = Outbox(
        config.targets,
        {**WMS_ADAPTERS, **TRANSPORT_ADAPTERS, PROTOCOL: EcsCommandAdapter()},
        lifecycles={PROTOCOL: EcsLifecycle(commands)},
    )
    commands.outbox = outbox
    return build_runtime(
        outbox=outbox,
        ecs_commands=commands,
        session_factory=factory,
        enqueue_advance=enqueue_advance,
    )


async def advance_pending(factory) -> int:
    runtime = _runtime(factory)
    return await runtime.services["coarse_sorting"].scan_pending()


async def advance_one(factory, source: str, identity: str) -> str:
    runtime = _runtime(factory)
    return await runtime.services["coarse_sorting"].advance_reference(source, identity)


@celery_app.task(name="coarse_sorting.advance_one")
def advance_coarse_sorting_one(source: str, identity: str) -> str:
    return run_with_factory(
        get_settings().DATABASE_URL, partial(advance_one, source=source, identity=identity)
    )


@celery_app.task(name="coarse_sorting.advance_pending")
def advance_coarse_sorting() -> int:
    return run_with_factory(get_settings().DATABASE_URL, advance_pending)
