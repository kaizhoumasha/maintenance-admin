"""任务一旦收到回调,说明 WMS 已接收提交,结束该任务出站提交消息的发送义务(设计 §6.4)。

用现有 `Outbox.settle`(PENDING/SENDING/STALLED → DONE),不改发送逻辑;不论回调内容是否
与冻结任务相符,它都是对端已知该任务的证据。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.reliable import ReliableMessage
from app.reliable import Outbox

from .protocol import OPERATION

# settle 只读消息身份,不依赖目标配置,所以空配置的 Outbox 即可
_outbox = Outbox({}, {})


async def settle_submitted(session: AsyncSession, transport_task_id: str) -> int:
    """锁住该任务的未结束提交消息并逐条 settle;返回结束的条数。"""
    rows = await session.scalars(
        select(ReliableMessage)
        .where(
            ReliableMessage.business_key == transport_task_id,
            ReliableMessage.operation == OPERATION,
            ReliableMessage.status.in_(("PENDING", "SENDING", "STALLED")),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    settled = 0
    for message in rows.all():
        if await _outbox.settle(session, message.target, message.operation, message.operation_id):
            settled += 1
    return settled
