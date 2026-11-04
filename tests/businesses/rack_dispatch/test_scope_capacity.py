"""Stopped task dispatch retains only unresolved original rack responsibility."""

import asyncio
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.businesses._components.rack_dispatch.models import RackJob
from app.businesses._components.rack_dispatch.service import pending_message_references
from app.core.identifiers import uuid7
from app.core.strict_json import canonical_digest
from app.models import ReliableMessage, TransportTask, WorkLine
from app.reliable import ScopeRef

from . import TaskGrant, apply_outcome, evidence, origin, respond, store

SCOPE = ScopeRef("owner", "TASK", "old")


@pytest.fixture(params=["sqlite", "postgres"])
def scope_url(request):
    return "sqlite+aiosqlite://" if request.param == "sqlite" else request.getfixturevalue("pg_url")


async def entry(session, dispatch, job):
    return await dispatch.prepare(session, job_id=job, kind="ENTER", source_ref="entry", face="90")


async def old_job(session, dispatch):
    dispatch.task_admission = TaskGrant("old")
    return await dispatch.register(
        session, replace(origin("ROLLER_ORDINARY"), task_id="old"), scope=SCOPE
    )


async def stop(session, dispatch):
    await dispatch.capabilities.wms.outbox.stop_scope(session, SCOPE, reason="TASK_COMPLETED")
    await dispatch.close_scope(session, SCOPE, reason="TASK_COMPLETED")


async def next_job(session, dispatch, *, same_rack=False):
    dispatch.task_admission = TaskGrant()
    return await dispatch.register(
        session,
        replace(
            origin(),
            rack_id="rack" if same_rack else "next-rack",
            source_ref="next",
            cycle_id=str(uuid7()),
        ),
    )


@pytest.mark.parametrize(
    "stage,pending",
    [
        ("candidate", False),
        ("queued", False),
        ("gated", True),
        ("accepted", True),
        ("arrived", True),
    ],
)
async def test_stop_preserves_original_responsibility_and_blocks_new_task(
    scope_url, stage, pending
):
    async with store(scope_url) as (factory, dispatch), factory.begin() as session:
        job = await old_job(session, dispatch)
        action = None
        if stage != "candidate":
            assert await entry(session, dispatch, job)
            action = (await dispatch.progress(session, job_id=job)).actions[-1]
            if stage in ("gated", "accepted", "arrived"):
                message = await session.get(ReliableMessage, action.submit_message_id)
                message.dispatch_started_at = datetime.now(UTC)
                await session.flush()
            if stage == "accepted":
                await respond(
                    session,
                    action.submit_operation_id,
                    202,
                    "RECEIVED",
                    dict(transport_task_id=action.transport_task_id),
                )
            if stage == "arrived":
                await apply_outcome(session, action, "SUCCEEDED")
                await dispatch.advance(
                    session, job_id=job, **evidence(job, demand="CONTINUE"), now_ms=1
                )
        before_messages = await session.scalar(select(func.count()).select_from(ReliableMessage))
        await stop(session, dispatch)
        stopped = await dispatch.progress(session, job_id=job)
        assert (stopped.state, stopped.pending) == ("CLOSED_BY_SCOPE", pending)
        assert stopped.closure_basis["reason"] == "TASK_COMPLETED"
        assert await entry(session, dispatch, job) is None
        for same_rack in (False, True):
            current = await next_job(session, dispatch, same_rack=same_rack)
            if pending:
                assert await entry(session, dispatch, current) is None
            else:
                break
        for now in (2, 3):
            result = await dispatch.advance(session, job_id=job, **evidence(job), now_ms=now)
            assert result.state == "CLOSED_BY_SCOPE"
            assert result.pending is pending
        assert len(result.actions) == (1 if action else 0)
        assert (
            await session.scalar(select(func.count()).select_from(ReliableMessage))
            == before_messages
        )
        if pending:
            assert await pending_message_references(
                session, candidate_ids=(action.submit_message_id,)
            ) == ((12, action.submit_message_id),)
        else:
            assert stopped.closure_basis["responsibility_release"]["kind"] in (
                "NO_PHYSICAL_INTENT",
                "NEVER_DISPATCHED",
            )
            assert await entry(session, dispatch, current)


@pytest.mark.parametrize(
    "fault",
    ["uncertain", "missing_message", "pending_outcome", "frozen_target", "saved_acceptance"],
)
async def test_missing_or_contradictory_never_dispatched_evidence_keeps_protection(
    scope_url, fault
):
    async with store(scope_url) as (factory, dispatch), factory.begin() as session:
        job = await old_job(session, dispatch)
        await entry(session, dispatch, job)
        action = (await dispatch.progress(session, job_id=job)).actions[-1]
        task = await session.scalar(
            select(TransportTask).where(TransportTask.transport_task_id == action.transport_task_id)
        )
        message = await session.get(ReliableMessage, action.submit_message_id)
        if fault == "uncertain":
            message.delivery_uncertain = True
        elif fault == "missing_message":
            await session.delete(message)
        elif fault == "pending_outcome":
            await apply_outcome(session, action, "SUCCEEDED")
            task.pending_outcome = dict(
                revision=2, data={**task.applied_outcome, "outcome_revision": 2}
            )
            task.applied_outcome = task.applied_outcome_revision = task.outcome_status = None
        elif fault == "frozen_target":
            task.frozen = {
                **task.frozen,
                "target": dict(kind="RACK_POSITION", location_code="OTHER"),
            }
        else:
            task.submission_acceptance = {"invalid": True}
        await session.flush()
        await stop(session, dispatch)
        assert (await dispatch.progress(session, job_id=job)).pending
        assert await entry(session, dispatch, await next_job(session, dispatch)) is None
        result = await dispatch.advance(session, job_id=job, **evidence(job), now_ms=10)
        assert result.pending and result.state == "CLOSED_BY_SCOPE"
        assert len(result.actions) == 1


async def departure(session, dispatch, job):
    await entry(session, dispatch, job)
    move = (await dispatch.progress(session, job_id=job)).actions[-1]
    await apply_outcome(session, move, "SUCCEEDED")
    decision = (await dispatch.advance(session, job_id=job, **evidence(job), now_ms=1)).actions[-1]
    await respond(
        session,
        decision.decision_operation_id,
        200,
        "DECIDED",
        dict(result="READY", rack_destination=dict(type="ZONE", location_code="NEXT")),
    )
    depart = (await dispatch.advance(session, job_id=job, **evidence(job), now_ms=2)).actions[-1]
    return move, depart


@pytest.mark.parametrize("invalid", [False, True])
async def test_only_strict_original_departure_acceptance_releases_stopped_position(
    scope_url, invalid
):
    async with store(scope_url) as (factory, dispatch), factory.begin() as session:
        job = await old_job(session, dispatch)
        move, depart = await departure(session, dispatch, job)
        await respond(
            session,
            depart.submit_operation_id,
            202,
            "RECEIVED",
            dict(transport_task_id=depart.transport_task_id),
        )
        accepted = await dispatch.capabilities.transport.get_submission_acceptance(
            session,
            transport_task_id=depart.transport_task_id,
            client_request_id=depart.client_request_id,
            caller=depart.caller,
        )
        saved = {
            **asdict(accepted.basis),
            "attempt_no": 1,
            "lease_id": "original-lease",
            "saved_at": 100,
        }
        saved["evidence_sha256"] = canonical_digest(saved)
        task = await session.scalar(
            select(TransportTask).where(TransportTask.transport_task_id == depart.transport_task_id)
        )
        task.submission_acceptance = saved
        await session.flush()
        await stop(session, dispatch)
        assert (await dispatch.progress(session, job_id=job)).pending
        message = await session.get(ReliableMessage, depart.submit_message_id)
        message.status = "ABANDONED"
        message.next_attempt_at = None
        message.response_raw = message.last_http_status = None
        if invalid:
            task.cycle_id = str(uuid7())
        await session.flush()
        before = await session.scalar(select(func.count()).select_from(ReliableMessage))
        complete = await dispatch.advance(
            session, job_id=job, **evidence(job, demand="UNKNOWN", handoff="UNKNOWN"), now_ms=10
        )
        assert (complete.state, complete.pending) == ("CLOSED_BY_SCOPE", invalid)
        assert complete.closure_basis["reason"] == "TASK_COMPLETED"
        excluded = await dispatch.handed_off_position_sources(
            session, point="P", source_versions={move.id: 1}
        )
        assert excluded == (frozenset() if invalid else frozenset({move.id}))
        if not invalid:
            proof = complete.closure_basis["responsibility_release"]
            assert (proof["kind"], proof["action_id"], proof["completed_at_ms"]) == (
                "DEPARTURE_ACK",
                depart.id,
                10,
            )
            assert complete.completed_at_ms == 10
            assert (
                await dispatch.advance(session, job_id=job, **evidence(job), now_ms=11) == complete
            )
            assert await entry(session, dispatch, await next_job(session, dispatch))
        assert len(complete.actions) == 3
        # The successor creates one submission only after the original responsibility is closed.
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == before + (
            not invalid
        )


async def test_late_arrival_is_consumed_without_departure_decision_or_new_move(scope_url):
    async with store(scope_url) as (factory, dispatch), factory.begin() as session:
        job = await old_job(session, dispatch)
        await entry(session, dispatch, job)
        move = (await dispatch.progress(session, job_id=job)).actions[-1]
        message = await session.get(ReliableMessage, move.submit_message_id)
        message.dispatch_started_at = datetime.now(UTC)
        await session.flush()
        await stop(session, dispatch)
        await apply_outcome(session, move, "SUCCEEDED")
        for now in (10, 11):
            view = await dispatch.advance(session, job_id=job, **evidence(job), now_ms=now)
            assert view.pending and view.state == "CLOSED_BY_SCOPE"
            assert view.position_action_id == move.id
            assert view.actual_position == {"kind": "RACK_POSITION", "location_code": "P"}
            assert len(view.actions) == 1 and view.actions[0].phase == "APPLIED"
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 1
        await dispatch.close_scope(session, SCOPE, reason="LATER_REASON")
        assert (await dispatch.progress(session, job_id=job)).closure_basis[
            "reason"
        ] == "TASK_COMPLETED"


@pytest.mark.parametrize(
    "fault", [None, "uncertain", "other_source", "other_target", "acceptance", "invalid_acceptance"]
)
async def test_stopped_submit_rejection_requires_full_original_no_responsibility_proof(
    scope_url, fault
):
    async with store(scope_url) as (factory, dispatch), factory.begin() as session:
        job = await old_job(session, dispatch)
        await entry(session, dispatch, job)
        action = (await dispatch.progress(session, job_id=job)).actions[-1]
        task = await session.scalar(
            select(TransportTask).where(TransportTask.transport_task_id == action.transport_task_id)
        )
        message = await session.get(ReliableMessage, action.submit_message_id)
        if fault == "acceptance":
            await respond(
                session,
                action.submit_operation_id,
                202,
                "RECEIVED",
                dict(transport_task_id=action.transport_task_id),
            )
            accepted = await dispatch.capabilities.transport.get_submission_acceptance(
                session,
                transport_task_id=action.transport_task_id,
                client_request_id=action.client_request_id,
                caller=action.caller,
            )
            saved = {
                **asdict(accepted.basis),
                "attempt_no": 1,
                "lease_id": "original-lease",
                "saved_at": 100,
            }
            saved["evidence_sha256"] = canonical_digest(saved)
            task.submission_acceptance = saved
            await session.flush()
        await respond(
            session,
            action.submit_operation_id,
            409,
            "CONFLICT",
            {"reason_code": "REFERENCE_CONFLICT", "transport_task_id": action.transport_task_id},
        )
        if fault == "uncertain":
            message.delivery_uncertain = True
        elif fault in ("other_source", "other_target"):
            task.frozen = {
                **task.frozen,
                **(
                    {"source": {"kind": "RACK", "location_code": "another-rack"}}
                    if fault == "other_source"
                    else {"target": {"kind": "RACK_POSITION", "location_code": "OTHER"}}
                ),
            }
            raw = json.loads(message.request_raw)
            raw["data"] = {"transport_task_id": action.transport_task_id, **task.frozen}
            message.request_raw = json.dumps(raw).encode()
        elif fault == "invalid_acceptance":
            task.submission_acceptance = {"invalid": True}
        await session.flush()
        await stop(session, dispatch)
        view = await dispatch.advance(session, job_id=job, **evidence(job), now_ms=10)
        assert view.state == "CLOSED_BY_SCOPE"
        assert view.pending is (fault is not None)
        assert view.closure_basis["reason"] == "TASK_COMPLETED"
        if fault is None:
            assert view.closure_basis["responsibility_release"]["kind"] == "SUBMIT_REJECTION"
        assert len(view.actions) == 1


@pytest.mark.parametrize("fault", ["kind", "action", "version", "cycle", "missing", "none"])
async def test_stopped_handoff_reader_requires_exact_persisted_original_release(scope_url, fault):
    async with store(scope_url) as (factory, dispatch), factory.begin() as session:
        job = await old_job(session, dispatch)
        move, depart = await departure(session, dispatch, job)
        await respond(
            session,
            depart.submit_operation_id,
            202,
            "RECEIVED",
            dict(transport_task_id=depart.transport_task_id),
        )
        accepted = await dispatch.capabilities.transport.get_submission_acceptance(
            session,
            transport_task_id=depart.transport_task_id,
            client_request_id=depart.client_request_id,
            caller=depart.caller,
        )
        saved = {
            **asdict(accepted.basis),
            "attempt_no": 1,
            "lease_id": "original-lease",
            "saved_at": 100,
        }
        saved["evidence_sha256"] = canonical_digest(saved)
        task = await session.scalar(
            select(TransportTask).where(TransportTask.transport_task_id == depart.transport_task_id)
        )
        task.submission_acceptance = saved
        await session.flush()
        await stop(session, dispatch)
        view = await dispatch.advance(session, job_id=job, **evidence(job), now_ms=10)
        assert not view.pending
        row = await session.get(RackJob, job)
        basis = {
            **row.closure_basis,
            "responsibility_release": dict(row.closure_basis["responsibility_release"]),
        }
        proof = basis["responsibility_release"]
        if fault == "kind":
            proof["kind"] = "NEVER_DISPATCHED"
        elif fault == "action":
            proof["action_id"] = move.id
        elif fault == "cycle":
            proof["acceptance_basis"] = {**proof["acceptance_basis"], "cycle_id": str(uuid7())}
        elif fault == "missing":
            del basis["responsibility_release"]
        row.closure_basis = basis
        # Cleanup of inactive transport rows cannot erase the saved release proof.
        for task in await session.scalars(select(TransportTask)):
            await session.delete(task)
        await session.flush()
        excluded = await dispatch.handed_off_position_sources(
            session, point="P", source_versions={move.id: 2 if fault == "version" else 1}
        )
        assert excluded == (frozenset({move.id}) if fault == "none" else frozenset())


@pytest.mark.parametrize("pending", [False, True])
async def test_stopped_job_requires_nonnull_original_stop_reason(scope_url, pending):
    async with store(scope_url) as (factory, dispatch), factory.begin() as session:
        job = await old_job(session, dispatch)
        row = await session.get(RackJob, job)
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                row.state = "CLOSED_BY_SCOPE"
                row.pending = pending
                row.closure_reason = None
                row.closure_basis = dict(reason="TASK_COMPLETED", scope_kind="TASK", scope_id="old")
                await session.flush()


@pytest.mark.parametrize("commit", [False, True])
async def test_scope_close_and_next_entry_share_workline_lock(pg_url, wait_for_lock, commit):
    async with store(pg_url) as (factory, dispatch):
        async with factory.begin() as session:
            original = await old_job(session, dispatch)
            await entry(session, dispatch, original)
            action = (await dispatch.progress(session, job_id=original)).actions[-1]
            message = await session.get(ReliableMessage, action.submit_message_id)
            message.dispatch_started_at = datetime.now(UTC)
            current = await next_job(session, dispatch)
        async with factory() as closing:
            await closing.connection(execution_options={"isolation_level": "SERIALIZABLE"})
            await closing.scalar(select(WorkLine).where(WorkLine.id == 12).with_for_update())
            await stop(closing, dispatch)
            started = asyncio.Event()
            waiter_pid = None

            async def admit():
                nonlocal waiter_pid
                async with factory() as session, session.begin():
                    await session.connection(execution_options={"isolation_level": "SERIALIZABLE"})
                    waiter_pid = await session.scalar(text("SELECT pg_backend_pid()"))
                    started.set()
                    await session.scalar(
                        select(WorkLine).where(WorkLine.id == 12).with_for_update()
                    )
                    return await entry(session, dispatch, current)

            waiting = asyncio.create_task(admit())
            try:
                await started.wait()
                await wait_for_lock(waiting, waiter_pid)
                assert not waiting.done()
                if commit:
                    await closing.commit()
                else:
                    await closing.rollback()
                assert await waiting is None
            finally:
                if not waiting.done():
                    waiting.cancel()
                    await asyncio.gather(waiting, return_exceptions=True)
        async with factory() as session:
            saved = await dispatch.progress(session, job_id=original)
            assert saved.pending
            assert saved.state == ("CLOSED_BY_SCOPE" if commit else "ACTIVE")
            message = await session.get(ReliableMessage, action.submit_message_id)
            assert message.status == ("ABANDONED" if commit else "PENDING")
            assert len((await dispatch.progress(session, job_id=current)).actions) == 0
