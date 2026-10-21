from sqlalchemy import func, select

from app.models import ReliableMessage, TransportTask


async def counts(session, rig):
    tasks = await session.scalar(
        select(func.count())
        .select_from(TransportTask)
        .where(TransportTask.workline_id == rig.target.name)
    )
    messages = await session.scalar(
        select(func.count())
        .select_from(ReliableMessage)
        .where(ReliableMessage.target == rig.target.name)
    )
    return tasks, messages
