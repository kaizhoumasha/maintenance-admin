"""Original WMS evidence for retained AUTO responsibilities."""

from sqlalchemy import select, union_all

from .models import (
    AutoInboundDecisionRound,
    AutoInboundPlate,
    AutoOutboundDecisionRound,
    AutoOutboundPlate,
    AutoTask,
    AutoTaskRequest,
)


async def wms_message_references(session, *, candidate_ids, line_ids=None):
    queries = []
    for message_id in (
        AutoTaskRequest.message_id,
        AutoInboundDecisionRound.message_id,
        AutoOutboundDecisionRound.message_id,
        AutoInboundPlate.report_message_id,
        AutoOutboundPlate.report_message_id,
    ):
        query = (
            select(AutoTask.work_line_id, message_id)
            .join(AutoTask, message_id.class_.task_id == AutoTask.task_id)
            .where(message_id.in_(candidate_ids))
        )
        if line_ids is not None:
            query = query.where(AutoTask.work_line_id.in_(line_ids))
        queries.append(query)
    # Completed BIN work is also reread against original WMS results during later arrivals.
    return tuple(await session.execute(union_all(*queries)))
