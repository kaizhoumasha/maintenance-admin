import itertools

from sqlalchemy import func, select

from app.core.identifiers import uuid7
from app.integrations.wms_transport import TransportCaller
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


async def frozen_requests(session, rig):
    """该夹具目标下已冻结的请求字节,按入队先后。"""
    rows = await session.scalars(
        select(ReliableMessage)
        .where(ReliableMessage.target == rig.target.name)
        .order_by(ReliableMessage.id)
    )
    return [row.request_raw for row in rows]


def caller(rig, station=None):
    return TransportCaller(rig.target.name, station)


def deterministic_uuid7(rig):
    """固定时钟下按 1、2、3… 递增的 UUIDv7 工厂,让整份冻结字节可以逐字比较。"""
    counter = itertools.count(1)
    return lambda: uuid7(rig.clock(), randbits=lambda width: next(counter))


def golden_head(operation: bytes, task: bytes) -> bytes:
    """确定性夹具下请求字节的公共前缀,止于 transport_task_id 之后的逗号。"""
    return (
        b'{"operation_id":"019b76da-a800-7000-8000-%s","operation":"transport.task.submit@v1",'
        b'"timestamp":1767225600000,"data":{"transport_task_id":'
        b'"transport-019b76da-a800-7000-8000-%s",'
    ) % (operation, task)
