"""Route committed inputs to catalog-owned business pending entries."""

from app.businesses import catalog
from app.integrations.registration import RegistrationSource
from app.integrations.wms_request.operation import operations


def enqueue_business(business: str) -> None:
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
    with app.connection_for_write(connect_timeout=1, transport_options=options) as connection:
        with connection.Producer() as producer:
            advance_business_pending.apply_async(
                args=(business,), producer=producer, retry=False, ignore_result=True
            )


def event_committed(source: RegistrationSource) -> None:
    business = catalog.progression_owner(source)
    if business is not None:
        enqueue_business(business)


def wms_result_committed(operation: str) -> None:
    registered = operations().get(operation)
    if registered is not None:
        event_committed(registered.registration_source)


def ecs_result_committed(owner_domain: str) -> None:
    if owner_domain in catalog.progressions():
        enqueue_business(owner_domain)
