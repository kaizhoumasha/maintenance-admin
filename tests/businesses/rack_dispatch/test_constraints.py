import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.businesses._components.rack_dispatch.models import RackAction
from app.core.identifiers import uuid7
from app.models import ReliableMessage

from . import apply_outcome, evidence, origin, respond, store


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_action_graph_and_external_identities_cannot_fork(backend, request):
    url = request.getfixturevalue("pg_url") if backend == "postgres" else "sqlite+aiosqlite://"
    async with store(url) as (factory, dispatch), factory.begin() as s:
        job = await dispatch.register(s, origin())
        other = await dispatch.register(s, replace(origin(), rack_id="rack-2"))

        async def insert(job_id, kind, ordinal, **values):
            op = str(uuid7()) if kind == "DEPARTURE_DECIDE" else None
            record = dict(
                id=str(uuid7()),
                job_id=job_id,
                owner_domain="owner",
                work_line_id=12,
                kind=kind,
                source_ref=str(uuid7()),
                ordinal=ordinal,
                phase="REGISTERED",
                completion_rule={
                    "ENTER": "APPLIED_RESULT",
                    "ROTATE": "APPLIED_RESULT",
                    "DEPART": "ACCEPTED_DEPARTURE",
                }.get(kind),
                operation="rack.departure_decide@v1" if op else None,
                decision_operation_id=op,
            )
            record.update(values)
            await s.execute(RackAction.__table__.insert().values(**record))
            return record["id"]

        async def rejects(*args, **values):
            with pytest.raises(IntegrityError):
                async with s.begin_nested():
                    await insert(*args, **values)

        entry = await insert(
            job,
            "ENTER",
            1,
            client_request_id="req",
            transport_task_id="transport",
            submit_operation_id="submit",
        )
        await rejects(job, "ENTER", 2)
        for field, value in (
            ("client_request_id", "req"),
            ("transport_task_id", "transport"),
            ("submit_operation_id", "submit"),
        ):
            await rejects(other, "ENTER", 1, **{field: value})
        root = await insert(
            job,
            "DEPARTURE_DECIDE",
            2,
            phase="WAIT",
            due_at_ms=100,
            decision={"result": "WAIT", "retry_after_ms": 50},
            decision_operation_id="decision",
        )
        await rejects(job, "DEPARTURE_DECIDE", 3)
        await rejects(other, "DEPARTURE_DECIDE", 1, decision_operation_id="decision")
        child = await insert(
            job,
            "DEPARTURE_DECIDE",
            3,
            previous_action_id=root,
            previous_decision_phase="WAIT",
            phase="READY",
            decision={
                "result": "READY",
                "rack_destination": {"type": "ZONE", "location_code": "Z"},
            },
        )
        await rejects(
            job, "DEPARTURE_DECIDE", 4, previous_action_id=root, previous_decision_phase="WAIT"
        )
        await rejects(
            job, "DEPARTURE_DECIDE", 4, previous_action_id=child, previous_decision_phase="WAIT"
        )
        await rejects(
            other, "DEPARTURE_DECIDE", 1, previous_action_id=root, previous_decision_phase="WAIT"
        )
        await rejects(
            other,
            "ROTATE",
            1,
            phase="WITHDRAWN",
            client_request_id="local-turn",
            basis={"registration_at_cutover": "LOCAL_ONLY", "withdrawal_reason": "NO_FACE_DEMAND"},
            transport_task_id="submitted-turn",
        )
        await rejects(other, "ROTATE", 1, phase="WITHDRAWN", client_request_id="unproven")
        await rejects(other, "ROTATE", 1, previous_action_id=entry)
        await rejects(job, "ROTATE", 4, owner_domain="other")
        await rejects(job, "ROTATE", 4, work_line_id=99)
        await rejects(job, "DEPART", 4, previous_action_id=root, previous_decision_phase="READY")
        await insert(job, "DEPART", 4, previous_action_id=child, previous_decision_phase="READY")
        await rejects(job, "DEPART", 5, previous_action_id=child, previous_decision_phase="READY")
        await rejects(
            other, "DEPARTURE_DECIDE", 1, phase="WAIT", decision={"result": "READY"}, due_at_ms=42
        )
        await rejects(
            other,
            "DEPARTURE_DECIDE",
            1,
            phase="READY",
            decision={"result": "WAIT", "retry_after_ms": 42},
        )


@pytest.mark.parametrize("case", ["compatible", "conflict", "rollback", "wait"])
async def test_contending_advances_preserve_one_original_action(pg_url, wait_for_lock, case):
    async with store(pg_url) as (factory, dispatch):
        async with factory.begin() as setup:
            job = await dispatch.register(setup, origin())
            if case == "wait":
                await dispatch.prepare(
                    setup, job_id=job, kind="ENTER", source_ref="first", face="90"
                )
                move = (await dispatch.progress(setup, job_id=job)).actions[-1]
                await apply_outcome(setup, move, "SUCCEEDED")
                root = (
                    await dispatch.advance(setup, job_id=job, **evidence(job), now_ms=100)
                ).actions[-1]
                await respond(
                    setup,
                    root.decision_operation_id,
                    200,
                    "DECIDED",
                    dict(result="WAIT", retry_after_ms=10),
                )
                assert (
                    await dispatch.advance(setup, job_id=job, **evidence(job), now_ms=100)
                ).actions[-1].due_at_ms == 110

        async def operate(session, first):
            if case == "wait":
                return (
                    (await dispatch.advance(session, job_id=job, **evidence(job), now_ms=110))
                    .actions[-1]
                    .id
                )
            return await dispatch.prepare(
                session,
                job_id=job,
                kind="ENTER",
                source_ref="A" if first else "B",
                face="270" if case == "conflict" and not first else "90",
            )

        pids = asyncio.Queue()

        async def contender():
            for attempt in range(3):
                try:
                    async with factory.begin() as second:
                        await second.connection(
                            execution_options={"isolation_level": "SERIALIZABLE"}
                        )
                        await pids.put(await second.scalar(text("select pg_backend_pid()")))
                        return await operate(second, False)
                except DBAPIError as e:
                    if getattr(e.orig, "sqlstate", None) != "40001" or attempt == 2:
                        raise

        async with factory() as first:
            await first.connection(execution_options={"isolation_level": "SERIALIZABLE"})
            a = await operate(first, True)
            pending = asyncio.create_task(contender())
            await wait_for_lock(pending, await pids.get())
            assert not pending.done()
            if case == "rollback":
                await first.rollback()
            else:
                await first.commit()
        b = await pending
        async with factory.begin() as check:
            actions = (await dispatch.progress(check, job_id=job)).actions
            if case == "rollback":
                assert len(actions) == 1 and b == actions[0].id != a
            elif case == "conflict":
                assert b is None and [v.id for v in actions] == [a]
            elif case == "wait":
                assert a == b == actions[-1].id and [v.kind for v in actions] == [
                    "ENTER",
                    "DEPARTURE_DECIDE",
                    "DEPARTURE_DECIDE",
                ]
                assert actions[-1].previous_action_id == actions[-2].id
            else:
                assert a == b and [v.id for v in actions] == [a]
            assert len(list(await check.scalars(select(ReliableMessage.id)))) == len(actions)
