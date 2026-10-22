"""出站消息停在 STALLED、但任务已有权威结果时,用现有 Outbox.resume 放回 PENDING(设计 §6.4)。

不改基础层、不新增重试调度:消息按原身份重发,WMS 去重回 DUPLICATE 后正常结束。
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.reliable import ReliableMessage
from app.reliable import Outbox

from .protocol import OPERATION

# resume 只读消息身份,不依赖目标配置,所以空配置的 Outbox 即可
_outbox = Outbox({}, {})


async def resume_stalled(
    session: AsyncSession, transport_task_id: str, *, cooldown: timedelta | None = None
) -> int:
    """恢复该任务的 STALLED 提交消息;先锁消息行、在锁下复查状态与冷却,再 resume。

    `cooldown` 为 None 表示不受冷却限制(结果刚应用时调用一次);定时对账传入冷却时间。
    """
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
        if cooldown is not None:
            updated = message.updated_at
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=UTC)
            if updated > datetime.now(UTC) - cooldown:
                continue
        if await _outbox.resume(session, message.target, message.operation, message.operation_id):
            resumed += 1
    return resumed
