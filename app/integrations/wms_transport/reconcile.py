"""出站消息停在 STALLED、但任务已有权威结果时,用现有 Outbox.resume 放回 PENDING(设计 §6.4)。

不改基础层、不新增重试调度、无定时对账:消息按原身份重发,WMS 去重回 DUPLICATE 后正常结束。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.reliable import ReliableMessage
from app.reliable import Outbox

from .protocol import OPERATION

# resume 只读消息身份,不依赖目标配置,所以空配置的 Outbox 即可
_outbox = Outbox({}, {})


async def resume_stalled(session: AsyncSession, transport_task_id: str) -> int:
    """恢复该任务的 STALLED 提交消息;先锁消息行,再 resume。"""
    rows = await session.scalars(
        select(ReliableMessage)
        .where(
            ReliableMessage.business_key == transport_task_id,
            ReliableMessage.operation == OPERATION,
            ReliableMessage.status == "STALLED",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    resumed = 0
    for message in rows.all():
        if await _outbox.resume(session, message.target, message.operation, message.operation_id):
            resumed += 1
    return resumed
