"""Route committed inputs to catalog-owned business pending entries."""

import logging
from functools import partial
from time import time_ns

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.businesses import catalog
from app.integrations.registration import RegistrationSource
from app.integrations.wms_request.operation import operations
from app.integrations.wms_transport import OPERATION
from app.models.transport import TransportTask
from app.reliable import OutgoingMessage
from app.transactions import after_commit

logger = logging.getLogger(__name__)


def enqueue_business(
    business: str, operation: str | None = None, operation_id: str | None = None
) -> None:
    if business not in catalog.progressions():
        raise ValueError("Business progression is not declared")
    from app.tasks.business_tasks import advance_business_pending

    app = advance_business_pending.app
    options = {
        **app.conf.broker_transport_options,
        "max_retries": 0,
        "socket_connect_timeout": 1,
        "socket_timeout": 1,
    }
    args = (
        (business,)
        if operation is None
        else (business, operation, operation_id, time_ns() // 1_000_000)
    )
    with app.connection_for_write(connect_timeout=1, transport_options=options) as connection:
        with connection.Producer() as producer:
            advance_business_pending.apply_async(
                args=args, producer=producer, retry=False, ignore_result=True
            )


def event_committed(source: RegistrationSource) -> None:
    business = catalog.progression_owner(source)
    if business is not None:
        enqueue_business(business)


def wms_result_committed(operation: str, business_key: str | None = None) -> None:
    registered = operations().get(operation)
    if registered is None:
        return
    source = registered.registration_source
    if business_key is None:
        if source.local_only:
            return
        business = catalog.progression_owner(source)
    elif isinstance(business_key, str) and business_key.startswith("business:"):
        business = business_key.removeprefix("business:")
        if business not in catalog.contract_consumers(("wms_request", operation, None)):
            business = None
    else:
        business = None
    if business is None or business not in catalog.progressions():
        logger.warning(
            "WMS result business association cannot be routed: operation=%s", registered.name
        )
        return
    notify_business_committed(business)


async def wms_delivery_committed(session: AsyncSession, message: OutgoingMessage) -> bool:
    """Send material decisions to the owning pending entry with their original identity."""
    if (
        message.business_key != "business:smt_auto"
        or message.operation
        not in (
            "outbound.bin.material.decide@v1",
            "outbound.rackslot.material.decide@v1",
        )
        or "smt_auto" not in catalog.progressions()
        or "smt_auto" not in catalog.contract_consumers(("wms_request", message.operation, None))
    ):
        return False
    after_commit(
        session, partial(enqueue_business, "smt_auto", message.operation, message.operation_id)
    )
    return True


def ecs_result_committed(owner_domain: str) -> None:
    if owner_domain in catalog.progressions():
        enqueue_business(owner_domain)


def ecs_ack_committed(owner_domain: str, task_type: str) -> None:
    if owner_domain == "smt_auto" and task_type == "PICK_AND_PUT":
        notify_business_committed(owner_domain)


def notify_business_committed(business_code: str) -> None:
    if business_code in catalog.progressions():
        enqueue_business(business_code)


async def transport_submit_delivered(session: AsyncSession, message: OutgoingMessage) -> None:
    """Legacy finish hook. Application senders record Transport evidence before finish."""
    if message.operation != OPERATION or message.business_key is None:
        return
    owner = await session.scalar(
        select(TransportTask.owner_domain).where(
            TransportTask.transport_task_id == message.business_key,
            TransportTask.operation_id == message.operation_id,
        )
    )
    if owner is not None and owner in catalog.progressions():
        after_commit(session, partial(notify_business_committed, owner))
