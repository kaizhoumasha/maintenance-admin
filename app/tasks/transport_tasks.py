import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.pool import NullPool

from app.core import get_settings
from app.database import create_database_engine as create_async_engine
from app.models import ReliableMessage, TransportTask
from app.reliable.config import positive
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)
DEAD_SUBMIT = ("EXPIRED", "FAILED")  # 提交消息已终止且没有被确认接收
SCAN_LIMIT = 200
# 超过这个年龄的无结果任务视为已搁置,不再重复告警(升级前的存量任务、被拒的任务都会永远没有结果)
STUCK_MAX_AGE = timedelta(days=7)


def _reason(task: TransportTask, submit_status: str | None) -> str:
    if task.pending_outcome is not None:
        return "RESULT_WAITING_FOR_POSITIONS"  # 料箱成功结果已到,等不到位置事件
    if task.outcome_status == "UNKNOWN":
        return "OUTCOME_UNKNOWN"
    if submit_status in DEAD_SUBMIT:
        return "SUBMIT_NOT_DELIVERED"  # 提交已过期或被拒,WMS 可能从未收到
    return "RESULT_MISSING"


async def scan_stuck(factory, minutes, *, now=None, limit=SCAN_LIMIT) -> int:
    """只输出结构化告警日志,不改任务状态,也不伪造结果;返回告警条数。

    卡住 = 创建超过 `minutes` 分钟,仍没有确定终态(无结果、结果 UNKNOWN,或料箱成功结果暂存中)。
    阈值按现场 SOP 调整;任务一直卡住会每次扫描重复告警,直到结果到达或超过 `STUCK_MAX_AGE`。
    """
    positive(minutes, "TRANSPORT_STUCK_ALERT_MINUTES")
    positive(limit, "limit")
    current = now or datetime.now(UTC)
    cutoff = current - timedelta(minutes=minutes)
    last_id, total = 0, 0
    while True:
        async with factory() as session:
            tasks = list(
                await session.scalars(
                    select(TransportTask)
                    .where(
                        TransportTask.id > last_id,
                        TransportTask.created_at < cutoff,
                        TransportTask.created_at >= current - STUCK_MAX_AGE,
                        TransportTask.outcome_status.is_(None)
                        | (TransportTask.outcome_status == "UNKNOWN"),
                    )
                    .order_by(TransportTask.id)
                    .limit(limit)
                )
            )
            if not tasks:
                return total
            submissions = select(ReliableMessage.business_key, ReliableMessage.status).where(
                ReliableMessage.business_key.in_([task.transport_task_id for task in tasks]),
                ReliableMessage.operation == "transport.task.submit@v1",
            )
            submit_statuses = dict((await session.execute(submissions)).all())
        for task in tasks:
            created = (
                task.created_at if task.created_at.tzinfo else task.created_at.replace(tzinfo=UTC)
            )
            context = {
                "transport_task_id": task.transport_task_id,
                "cycle_id": task.cycle_id,
                "kind": task.kind,
                "reason": _reason(task, submit_statuses.get(task.transport_task_id)),
                "waiting_minutes": int((current - created).total_seconds() // 60),
            }
            logger.warning("transport task stuck %r", context, extra=context)
        total += len(tasks)
        last_id = tasks[-1].id


@celery_app.task(name="transport.scan_stuck")
def scan_stuck_tasks() -> int:
    settings = get_settings()

    async def execute():
        engine = create_async_engine(
            settings.DATABASE_URL, poolclass=NullPool, hide_parameters=True
        )
        try:
            return await scan_stuck(
                async_sessionmaker(engine), settings.TRANSPORT_STUCK_ALERT_MINUTES
            )
        finally:
            await engine.dispose()

    return asyncio.run(execute())
