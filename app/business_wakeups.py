"""Application-owned routing from committed identities to concrete business tasks."""


def enqueue_advance(business: str, source: str, identity: str) -> None:
    if business != "coarse_sorting":
        raise ValueError("Business advance task is not configured")
    from app.tasks.coarse_sorting_tasks import advance_coarse_sorting_one

    app = advance_coarse_sorting_one.app
    options = {
        **app.conf.broker_transport_options,
        "max_retries": 0,
        "socket_connect_timeout": 1,
        "socket_timeout": 1,
    }
    with app.connection_for_write(connect_timeout=1, transport_options=options) as connection:
        with connection.Producer() as producer:
            advance_coarse_sorting_one.apply_async(
                args=(source, identity), producer=producer, retry=False, ignore_result=True
            )


def wms_result_committed(message_id: int, operation: str, operation_id: str) -> None:
    if operation == "inbound.coarse_sorting.validate_bind@v1":
        enqueue_advance("coarse_sorting", "wms", str(message_id))


def ecs_result_committed(owner_domain: str, command_code: str) -> None:
    if owner_domain == "coarse_sorting":
        enqueue_advance(owner_domain, "ecs", command_code)
