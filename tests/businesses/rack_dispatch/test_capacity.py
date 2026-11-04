from dataclasses import replace

import pytest
from sqlalchemy import select

from app.core.identifiers import uuid7
from app.models import WorkLinePosition

from . import TaskGrant, origin, store


async def candidate(session, dispatch, number, *, task="task", kind="MANUAL_RETURN"):
    return await dispatch.register(
        session,
        replace(
            origin(kind),
            rack_id=f"R{number}",
            source_ref=f"source-{number}",
            task_id=task,
            cycle_id=str(uuid7()),
        ),
    )


async def enter(session, dispatch, job):
    return await dispatch.prepare(session, job_id=job, kind="ENTER", source_ref="entry", face="90")


async def limit(session, value):
    row = await session.scalar(select(WorkLinePosition).where(WorkLinePosition.work_line_id == 12))
    row.capacity = value
    await session.flush()


@pytest.mark.parametrize("capacity,expected", [(3, 3), (None, 5)])
async def test_capacity_counts_original_entries_across_source_kinds(capacity, expected):
    async with store("sqlite+aiosqlite://") as (factory, dispatch), factory.begin() as session:
        dispatch.task_admission = TaskGrant()
        await limit(session, capacity)
        jobs = [
            await candidate(
                session, dispatch, n, kind="ROLLER_ORDINARY" if n % 2 else "MANUAL_RETURN"
            )
            for n in range(5)
        ]
        admitted = [await enter(session, dispatch, job) for job in jobs]
        assert sum(a is not None for a in admitted) == expected
        assert [await enter(session, dispatch, job) for job in jobs] == admitted


async def test_only_current_task_can_register_new_entry():
    async with store("sqlite+aiosqlite://") as (factory, dispatch), factory.begin() as session:
        dispatch.task_admission = TaskGrant()
        old = await candidate(session, dispatch, 1, task="old")
        assert await enter(session, dispatch, old) is None
        assert not (await dispatch.progress(session, job_id=old)).actions
        current = await candidate(session, dispatch, 2)
        assert await enter(session, dispatch, current)


async def test_old_committed_entry_blocks_next_task_but_unsubmitted_candidate_does_not():
    async with store("sqlite+aiosqlite://") as (factory, dispatch), factory.begin() as session:
        grant = TaskGrant("old")
        dispatch.task_admission = grant
        old = await candidate(session, dispatch, 1, task="old")
        old_entry = await enter(session, dispatch, old)
        assert old_entry
        grant.task = "task"
        await candidate(session, dispatch, 2, task="old")
        current = await candidate(session, dispatch, 3)
        assert await enter(session, dispatch, current) is None
        assert await enter(session, dispatch, old) == old_entry


@pytest.mark.parametrize("capacity", [3, None])
async def test_taskless_drain_is_serial_across_original_decisions(capacity):
    async with store("sqlite+aiosqlite://") as (factory, dispatch), factory.begin() as session:
        dispatch.task_admission = TaskGrant()
        await limit(session, capacity)
        first = await candidate(session, dispatch, 1, task=None, kind="ROLLER_DRAIN")
        other = await candidate(session, dispatch, 2, task=None, kind="ROLLER_DRAIN")
        assert await enter(session, dispatch, first)
        assert await enter(session, dispatch, other) is None
        current = await candidate(session, dispatch, 3)
        assert await enter(session, dispatch, current) is None


async def test_missing_task_is_not_a_taskless_admission_bypass():
    async with store("sqlite+aiosqlite://") as (factory, dispatch), factory.begin() as session:
        dispatch.task_admission = TaskGrant()
        job = await candidate(session, dispatch, 1, task=None)
        assert await enter(session, dispatch, job) is None


@pytest.mark.parametrize("uncertain", [False, True])
async def test_only_strict_original_submit_rejection_releases_entry(uncertain):
    from app.models import ReliableMessage

    from . import evidence, respond

    async with store("sqlite+aiosqlite://") as (factory, dispatch), factory.begin() as session:
        job = await dispatch.register(session, origin())
        assert await enter(session, dispatch, job)
        action = (await dispatch.progress(session, job_id=job)).actions[0]
        await respond(
            session,
            action.submit_operation_id,
            409,
            "CONFLICT",
            {"reason_code": "REFERENCE_CONFLICT", "transport_task_id": action.transport_task_id},
        )
        message = await session.get(ReliableMessage, action.submit_message_id)
        message.delivery_uncertain = uncertain
        await session.flush()
        projection = await dispatch.final_result(session, job_id=job, action_id=action.id)
        assert projection.closure_reason == "SUBMIT_REJECTION", projection
        result = await dispatch.advance(session, job_id=job, **evidence(job), now_ms=10)
        assert (result.state == "NOT_ARRIVED") is (not uncertain)
        assert result.pending is uncertain


async def test_submit_rejection_cannot_close_using_another_predecessor_scope(monkeypatch):
    from . import evidence, respond

    async with store("sqlite+aiosqlite://") as (factory, dispatch), factory.begin() as session:
        job = await dispatch.register(session, origin())
        assert await enter(session, dispatch, job)
        action = (await dispatch.progress(session, job_id=job)).actions[0]
        await respond(
            session,
            action.submit_operation_id,
            409,
            "CONFLICT",
            {"reason_code": "REFERENCE_CONFLICT", "transport_task_id": action.transport_task_id},
        )
        original = dispatch.capabilities.transport.list_unclosed_predecessors

        async def different_scope(session, **scope):
            return replace(await original(session, **scope), cycle_id="another-cycle")

        monkeypatch.setattr(
            dispatch.capabilities.transport, "list_unclosed_predecessors", different_scope
        )
        result = await dispatch.advance(session, job_id=job, **evidence(job), now_ms=10)
        assert result.pending
