"""One ordered AUTO admission lock set; a changed set requires a fresh transaction."""

from sqlalchemy import select

from app.businesses._components.bin_roller.service import RackLockSetChanged
from app.models import Device, WorkLine
from app.services.device_bindings import resolve_binding

from .models import (
    AutoBinWork,
    AutoInboundDecisionRound,
    AutoInboundPlate,
    AutoInboundSourceMember,
    AutoOutboundDecisionRound,
    AutoOutboundPlate,
    AutoOutboundSourceMember,
    AutoSupplyMember,
    AutoTask,
    AutoTaskInput,
    AutoTaskRequest,
)


async def lock_line(session, dispatch, work_line_id, *, include_job_ids=(), roller=None):
    await session.execute(select(WorkLine.id).where(WorkLine.id == work_line_id).with_for_update())
    if dispatch is None:
        return {}
    devices = tuple(
        await session.scalars(
            select(Device)
            .where(Device.work_line_id == work_line_id)
            .order_by(Device.id)
            .with_for_update()
        )
    )
    bindings = {
        device.device_code: await resolve_binding(session, device.device_code) for device in devices
    }
    jobs = await dispatch.admission_jobs(session, work_line_id=work_line_id)
    original = tuple(sorted(job.id for job in jobs))
    job_ids = tuple(sorted(set(original) | set(include_job_ids)))
    await dispatch.lock_admission_jobs(session, job_ids=job_ids)
    current = await dispatch.admission_jobs(session, work_line_id=work_line_id)
    if tuple(sorted(job.id for job in current)) != original:
        raise RackLockSetChanged("original AUTO dispatch jobs changed during lock acquisition")
    tasks = tuple(
        await session.scalars(
            select(AutoTask.task_id)
            .where(AutoTask.work_line_id == work_line_id)
            .order_by(AutoTask.task_id)
            .with_for_update()
        )
    )
    for model in (
        AutoTaskInput,
        AutoSupplyMember,
        AutoInboundSourceMember,
        AutoOutboundSourceMember,
        AutoBinWork,
        AutoInboundPlate,
        AutoOutboundPlate,
        AutoInboundDecisionRound,
        AutoOutboundDecisionRound,
        AutoTaskRequest,
    ):
        condition = (
            model.work_line_id == work_line_id if model is AutoBinWork else model.task_id.in_(tasks)
        )
        await session.execute(
            select(model.id).where(condition).order_by(model.id).with_for_update()
        )
    if roller is not None:
        await roller.lock_production_subjects(session, work_line_id, jobs)
    await dispatch.lock_admission_actions(session, job_ids=job_ids)
    current = await dispatch.admission_jobs(session, work_line_id=work_line_id)
    if tuple(sorted(job.id for job in current)) != original:
        raise RackLockSetChanged("original AUTO dispatch jobs changed during lock acquisition")
    return bindings
