import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.clock import unix_ms
from app.core.errors import AppError, ErrorCode
from app.core.identifiers import uuid7
from app.integrations.wms_callback import receive
from app.integrations.wms_transport.callback_samples import sample_callbacks
from app.models import CallbackReceipt
from app.models.transport import TransportTask
from app.schemas.dev_callback import DevCallbackOut, DevCallbackScenario, DevTaskStateOut


async def _task(factory: async_sessionmaker, transport_task_id: str) -> TransportTask:
    async with factory() as session:
        task = await session.scalar(
            select(TransportTask).where(TransportTask.transport_task_id == transport_task_id)
        )
    if task is None:
        raise AppError(ErrorCode.NOT_FOUND, "没有该 transport_task_id 的任务")
    return task


async def task_state(factory: async_sessionmaker, transport_task_id: str) -> DevTaskStateOut:
    task = await _task(factory, transport_task_id)
    return DevTaskStateOut(**{name: getattr(task, name) for name in DevTaskStateOut.model_fields})


async def deliver(
    factory: async_sessionmaker, transport_task_id: str, scenario: DevCallbackScenario
) -> list[DevCallbackOut]:
    """把样例回调逐条送进真实的接收管线(与 WMS 的 HTTP 回调同一入口 `receive`)。"""
    task = await _task(factory, transport_task_id)
    if task.frozen is None:
        raise AppError(ErrorCode.INVALID_REQUEST, "该任务没有冻结事实,无法构造样例回调")
    revision = (task.applied_outcome_revision or 0) + 1
    items = []
    for operation, data in sample_callbacks(
        scenario.value, task.kind, transport_task_id, task.frozen, revision
    ):
        envelope = dict(
            operation_id=str(uuid7()),
            operation=operation,
            timestamp=unix_ms(datetime.now(UTC)),
            data=data,
        )
        async with factory() as session, session.begin():
            result = await receive(session, json.dumps(envelope).encode())
        async with factory() as session:
            diagnostic = await session.scalar(
                select(CallbackReceipt.diagnostic).where(
                    CallbackReceipt.operation == operation,
                    CallbackReceipt.operation_id == envelope["operation_id"],
                )
            )
        items.append(
            DevCallbackOut(
                operation=operation,
                operation_id=envelope["operation_id"],
                status_code=result.status_code,
                outcome=result.outcome,
                request=envelope,
                response=result.body,
                diagnostic=diagnostic,
            )
        )
    return items
