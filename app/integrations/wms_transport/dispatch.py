from contextlib import asynccontextmanager

from sqlalchemy import select

from app.models import ReliableMessage, TransportTask
from app.reliable.lifecycle import (
    EvidencePersistenceError,
    PreDispatchUnavailable,
    evidence_transaction,
    http_connection_guard,
    pre_dispatch_unavailable,
    transaction_limits,
)


class TransportDispatch:
    def __init__(self, authorize):
        self.authorize = authorize

    @asynccontextmanager
    async def http_guard(self, factory, claim):
        has_started_writing = None
        try:
            async with evidence_transaction(factory) as session:
                message = claim.message
                task = await session.scalar(
                    select(TransportTask).where(
                        TransportTask.transport_task_id == message.business_key,
                        TransportTask.operation_id == message.operation_id,
                        TransportTask.submission_exchange_id == message.exchange_id,
                    )
                )
                if task is None:
                    yield "TRANSPORT_DISPATCH_EVIDENCE_MISSING", None, None, None
                    return
                authorize = self.authorize(task)
                if authorize is not None:
                    if claim.delivery_uncertain:
                        uncertain = await session.scalar(
                            select(ReliableMessage.delivery_uncertain).where(
                                ReliableMessage.id == claim.message_id,
                                ReliableMessage.lease_id == claim.lease_id,
                                ReliableMessage.status == "SENDING",
                            )
                        )
                        if uncertain is not False:
                            yield "DELIVERY_UNKNOWN", None, None, None
                            return
                    await transaction_limits(session)
                    reason = await authorize(session)
                    if reason is not None:
                        yield reason, None, None, None
                        return
                    async with http_connection_guard(session) as guard:
                        before_write, check_wire, has_started_writing, note_write = guard
                        yield None, before_write, check_wire, note_write
                    return
            yield None, None, None, None
        except EvidencePersistenceError as error:
            if (
                has_started_writing is None or not has_started_writing()
            ) and pre_dispatch_unavailable(error):
                raise PreDispatchUnavailable("Transport guard interrupted before write") from error
            raise
