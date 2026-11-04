import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.businesses._components.rack_dispatch.models import RackAction, RackJob
from app.core.identifiers import uuid7
from app.models import WorkLine, WorkLinePosition

from . import TaskGrant, origin, store


@pytest.mark.parametrize(
    "mode", ["drain-decisions", "task-and-drain", "task-last-slot", "task-cross-role"]
)
async def test_same_point_entry_is_serialized_across_independent_transactions(pg_url, mode):
    async with store(pg_url) as (factory, dispatch):
        dispatch.task_admission = TaskGrant()
        async with factory.begin() as session:
            if mode in {"task-last-slot", "task-cross-role"}:
                position = await session.scalar(select(WorkLinePosition))
                position.capacity = 1
            first = await dispatch.register(
                session,
                replace(
                    origin(
                        "ROLLER_ORDINARY"
                        if mode == "task-cross-role"
                        else "MANUAL_RETURN"
                        if mode == "task-last-slot"
                        else "ROLLER_DRAIN"
                    ),
                    rack_id="FIRST",
                    source_ref="decision-1",
                ),
            )
            other = await dispatch.register(
                session,
                replace(
                    origin("MANUAL_RETURN" if mode != "drain-decisions" else "ROLLER_DRAIN"),
                    rack_id="OTHER",
                    source_ref="decision-2",
                    cycle_id=str(uuid7()),
                ),
            )
        barrier = asyncio.Barrier(2)
        retries = []

        async def admit(job):
            for attempt in range(3):
                try:
                    async with factory() as session, session.begin():
                        await session.connection(
                            execution_options={"isolation_level": "SERIALIZABLE"}
                        )
                        await session.scalar(select(RackJob.id).where(RackJob.id == job))
                        if attempt == 0:
                            await barrier.wait()
                        await session.scalar(
                            select(WorkLine).where(WorkLine.id == 12).with_for_update()
                        )
                        jobs = await dispatch.admission_jobs(session, work_line_id=12)
                        await dispatch.lock_admission_jobs(
                            session, job_ids=tuple(j.id for j in jobs)
                        )
                        await dispatch.lock_admission_actions(
                            session, job_ids=tuple(j.id for j in jobs)
                        )
                        return await dispatch.prepare(
                            session, job_id=job, kind="ENTER", source_ref="entry", face="90"
                        )
                except DBAPIError as error:
                    if getattr(error.orig, "sqlstate", None) != "40001" or attempt == 2:
                        raise
                    retries.append(job)

        results = await asyncio.gather(admit(first), admit(other))
        assert sum(result is not None for result in results) == 1
        # An ordered waiter can decline before the winner writes, without a retry.
        if mode not in {"task-last-slot", "task-cross-role"}:
            assert retries
        async with factory() as session:
            actions = list(await session.scalars(select(RackAction)))
            assert len(actions) == 1
            assert actions[0].transport_task_id


async def test_capacity_update_and_last_slot_share_workline_serialization(pg_url):
    async with store(pg_url) as (factory, dispatch):
        dispatch.task_admission = TaskGrant()
        async with factory.begin() as session:
            position = await session.scalar(select(WorkLinePosition))
            position.capacity = 2
            first = await dispatch.register(session, origin())
            assert await dispatch.prepare(
                session, job_id=first, kind="ENTER", source_ref="entry", face="90"
            )
            waiting = await dispatch.register(
                session,
                replace(origin(), rack_id="SECOND", source_ref="second", cycle_id=str(uuid7())),
            )
        snapshotted, updated = asyncio.Event(), asyncio.Event()
        retries = []

        async def enter_waiter():
            for attempt in range(3):
                try:
                    async with factory() as session, session.begin():
                        await session.connection(
                            execution_options={"isolation_level": "SERIALIZABLE"}
                        )
                        await session.scalar(select(WorkLine.id).where(WorkLine.id == 12))
                        if attempt == 0:
                            snapshotted.set()
                            await updated.wait()
                        await session.scalar(
                            select(WorkLine).where(WorkLine.id == 12).with_for_update()
                        )
                        jobs = await dispatch.admission_jobs(session, work_line_id=12)
                        await dispatch.lock_admission_jobs(
                            session, job_ids=tuple(j.id for j in jobs)
                        )
                        await dispatch.lock_admission_actions(
                            session, job_ids=tuple(j.id for j in jobs)
                        )
                        return await dispatch.prepare(
                            session, job_id=waiting, kind="ENTER", source_ref="entry", face="90"
                        )
                except DBAPIError as error:
                    if getattr(error.orig, "sqlstate", None) != "40001" or attempt == 2:
                        raise
                    retries.append(attempt)

        async def reduce():
            await snapshotted.wait()
            async with factory.begin() as session:
                line = await session.scalar(
                    select(WorkLine).where(WorkLine.id == 12).with_for_update()
                )
                position = await session.scalar(select(WorkLinePosition))
                position.capacity = 1
                line.version += 1
            updated.set()

        admitted, _ = await asyncio.gather(enter_waiter(), reduce())
        assert admitted is None and retries
        async with factory.begin() as session:
            assert len(list(await session.scalars(select(RackAction)))) == 1
            position = await session.scalar(select(WorkLinePosition))
            position.capacity = 3
            await session.flush()
            assert await dispatch.prepare(
                session, job_id=waiting, kind="ENTER", source_ref="entry", face="90"
            )
            assert len(list(await session.scalars(select(RackAction)))) == 2
