"""Native submit acceptance wakes only its original business after commit."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from sqlalchemy import delete, event, select, text
from sqlalchemy.exc import OperationalError

from app import business_wakeups
from app.businesses import catalog
from app.businesses.runtime import BusinessRuntime
from app.core.identifiers import uuid7
from app.http_client import DeliveryState, HttpResult
from app.integrations import history as history_records
from app.integrations.wms_transport import (
    ADAPTERS,
    Rack,
    RackPosition,
    TransportCaller,
    TransportService,
)
from app.integrations.wms_transport.acceptance import submission_observer
from app.models import ReliableMessage, TransportTask
from app.reliable.config import ReliableConfig
from app.reliable.store import AttemptOutcome, MessageStore
from app.reliable.types import MessageStatus
from tests.helpers import message_history_view


@pytest.mark.parametrize("status,code", [(202, "RECEIVED"), (200, "DUPLICATE")])
@pytest.mark.parametrize("path", ["current", "late", "duplicate_evidence", "rollback"])
async def test_late_ack_survives_settle_and_cleanup(
    pg_transport_rig, pg_url, monkeypatch, status, code, path, rack_failure_callback
):
    import app.main as main
    from app.reliable_runtime import reliable_lifespan

    late = path == "late"
    rig = pg_transport_rig
    rig.clock.current = datetime.now(UTC)
    service = TransportService(rig.outbox, target=rig.target.name, owner_domain="original")

    async with rig.factory.begin() as session:
        handle = await service.move_rack(
            session,
            client_request_id=str(uuid7()),
            caller=TransportCaller(rig.target.name),
            rack_id="R1",
            source=Rack("R1"),
            target=RackPosition("DEST"),
            rcs_template_id="CTU03",
        )
        original = await session.scalar(
            select(TransportTask).where(TransportTask.transport_task_id == handle.transport_task_id)
        )
        identity = (original.operation_id, original.transport_task_id, original.owner_domain)
        message = await message_history_view(
            session,
            await session.scalar(
                select(ReliableMessage).where(ReliableMessage.operation_id == original.operation_id)
            ),
        )
        frozen = message.request_raw
    service.owner_domain = "current"
    notices = []
    monkeypatch.setattr(catalog, "definitions", lambda: ())
    monkeypatch.setattr(
        catalog, "progressions", lambda: {"original": "scan_pending", "current": "scan_pending"}
    )
    monkeypatch.setattr(business_wakeups, "enqueue_business", notices.append)

    calls = 0
    sending, release = asyncio.Event(), asyncio.Event()

    class Peer:
        async def send(self, request):
            nonlocal calls
            calls += 1
            sending.set()
            await release.wait()
            assert request.body == frozen
            data = json.loads(request.body)
            raw = json.dumps(
                dict(
                    operation_id=data["operation_id"],
                    code=code,
                    timestamp=1,
                    data=dict(transport_task_id=handle.transport_task_id),
                )
            ).encode()
            return HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, status, (), raw)

        async def aclose(self):
            pass

    @asynccontextmanager
    async def runtime(config, adapters, **kwargs):
        if path == "duplicate_evidence":
            observe = kwargs["observe_delivered"]

            async def observe_twice(claim, outcome):
                await observe(claim, outcome)
                await observe(claim, outcome)

            kwargs["observe_delivered"] = observe_twice
        async with reliable_lifespan(
            ReliableConfig(rig.outbox.targets),
            ADAPTERS,
            database_url=pg_url,
            client_factory=lambda _: Peer(),
            **kwargs,
        ) as outbox:
            yield outbox

    async def close():
        pass

    def redis():
        return SimpleNamespace(aclose=close)

    redis.cache_clear = lambda: None
    monkeypatch.setattr(main, "engine", SimpleNamespace(dispose=close))
    monkeypatch.setattr(main, "get_redis", redis)
    monkeypatch.setattr(main, "reliable_lifespan", runtime)
    monkeypatch.setattr(main, "ecs_runtime_config", lambda *_: ReliableConfig(rig.outbox.targets))

    def observer_session():
        session = rig.factory()
        if path == "rollback":

            def reject(_):
                raise ConnectionError("commit failed")

            event.listen(session.sync_session, "before_commit", reject, once=True)
        return session

    monkeypatch.setattr(main, "session_factory", observer_session)
    application = FastAPI()
    application.state.ecs_commands = SimpleNamespace()
    application.state.business_runtime = BusinessRuntime({}, {})
    async with main.lifespan(application):
        await asyncio.wait_for(sending.wait(), 5)
        if late:
            async with rig.factory.begin() as callback:
                await rack_failure_callback(rig, callback, handle, "R1")
        release.set()
        async with asyncio.timeout(5):
            while True:
                async with rig.factory() as session:
                    message = await message_history_view(
                        session,
                        await session.scalar(
                            select(ReliableMessage).where(
                                ReliableMessage.operation_id == identity[0]
                            )
                        ),
                    )
                    if message.status == "DONE" and (not late or notices):
                        break
                await asyncio.sleep(0.01)
        assert notices == ([] if path == "rollback" else ["original"])
        async with rig.factory() as session:
            original = await session.scalar(
                select(TransportTask).where(
                    TransportTask.transport_task_id == handle.transport_task_id
                )
            )
            assert (
                original.operation_id,
                original.transport_task_id,
                original.owner_domain,
            ) == identity
            assert (
                message.business_key == handle.transport_task_id and message.request_raw == frozen
            )
            assert original.applied_outcome_revision == (1 if late else None)
            assert (
                message.lease_id is None and message.attempts == (0 if late else 1) and calls == 1
            )
            assert (message.response_raw is None) is late
            await session.execute(delete(ReliableMessage).where(ReliableMessage.id == message.id))
            await session.commit()
            service.owner_domain = "original"
            result = await service.get_submission_acceptance(
                session,
                transport_task_id=handle.transport_task_id,
                client_request_id=handle.client_request_id,
                caller=TransportCaller(rig.target.name),
            )
            if path == "rollback":
                assert result.state == "MISSING" and result.basis is None
            else:
                assert result.state == "ACCEPTED" and result.basis.response_code == code
                snapshot = asdict(result.basis)
                assert snapshot["caller"] == {"workline_id": rig.target.name, "station_id": None}
                assert snapshot["request_sha256"] == sha256(frozen).hexdigest()
                assert snapshot["message_id"] == message.id and snapshot["accepted_at"] == 1
                assert snapshot["request_raw"].encode() == frozen
                assert result.basis.frozen["rcs_template_id"] == "CTU03"
                assert (
                    json.loads(snapshot["response_raw"])["data"]["transport_task_id"]
                    == handle.transport_task_id
                )


@pytest.fixture
async def submit(transport_storage, transport_request):
    rig = transport_storage
    notices = []
    service = TransportService(
        rig.outbox, target=rig.target.name, owner_domain="original", clock=rig.clock
    )
    async with rig.factory.begin() as session:
        handle = await service.move_rack(session, **transport_request)
    store = MessageStore(
        rig.factory,
        clock=rig.clock,
        records=history_records,
    )
    [claim] = await store.claim(1, rig.outbox.targets)

    def outcome(status=202, code="RECEIVED", **data):
        raw = json.dumps(
            dict(
                operation_id=claim.message.operation_id,
                code=code,
                timestamp=1,
                data=dict(transport_task_id=handle.transport_task_id, **data),
            )
        ).encode()
        response = HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, status, (), raw)
        verdict = ADAPTERS[rig.target.protocol].classify_response(claim.message, response)
        return AttemptOutcome(MessageStatus.DONE, result=response, verdict=verdict)

    return SimpleNamespace(
        rig=rig, handle=handle, store=store, claim=claim, notices=notices, outcome=outcome
    )


@pytest.mark.parametrize(
    "case", ["duplicate", "message_id", "target", "wrong_protocol", "wrong_body"]
)
async def test_evidence_requires_original_claim_and_keeps_first_ack(submit, case):
    flow = submit
    observe = submission_observer(flow.rig.outbox.targets, flow.rig.factory, flow.notices.append)
    claim = flow.claim
    if case == "target":
        claim = replace(claim, message=replace(claim.message, target="unconfigured"))
    if case == "message_id":
        claim = replace(claim, message_id=claim.message_id + 20)
    if case == "wrong_protocol":
        observe = submission_observer(
            {claim.message.target: replace(flow.rig.target, protocol="other")},
            flow.rig.factory,
            flow.notices.append,
        )
    if case == "wrong_body":
        claim = replace(
            claim, message=replace(claim.message, request_raw=claim.message.request_raw + b" ")
        )
    if case in {"wrong_body", "message_id"}:
        with pytest.raises(ValueError):
            await observe(claim, flow.outcome())
    else:
        await observe(claim, flow.outcome())
    if case == "duplicate":
        await observe(claim, flow.outcome(200, "DUPLICATE"))
    async with flow.rig.factory() as session:
        task = (
            await session.scalars(
                select(TransportTask).where(
                    TransportTask.transport_task_id == flow.handle.transport_task_id
                )
            )
        ).one()
        if case == "duplicate":
            assert task.submission_acceptance["response_code"] == "RECEIVED"
        else:
            assert task.submission_acceptance is None
    assert flow.notices == (["original"] if case == "duplicate" else [])


@pytest.mark.parametrize("stored", ["task", "historical"])
async def test_original_target_survives_new_send_configuration(submit, transport_request, stored):
    from app.reliable import Outbox

    flow = submit
    if stored == "task":
        await submission_observer(flow.rig.outbox.targets, flow.rig.factory, flow.notices.append)(
            flow.claim, flow.outcome()
        )
    else:
        assert await flow.store.finish(flow.claim, flow.outcome())
    replacement = replace(flow.rig.target, name="replacement")
    service = TransportService(
        Outbox({replacement.name: replacement}, ADAPTERS, records=history_records),
        owner_domain="original",
    )
    args = dict(
        transport_task_id=flow.handle.transport_task_id,
        client_request_id=flow.handle.client_request_id,
        caller=transport_request["caller"],
    )
    async with flow.rig.factory.begin() as session:
        # The unrelated obligation is rolled back after exercising normal send-target resolution.
        before = await service.get_submission_acceptance(session, **args)
        await service.move_rack(
            session, **(transport_request | {"client_request_id": str(uuid7())})
        )
        after = await service.get_submission_acceptance(session, **args)
        assert before == after and after.state == "ACCEPTED"
        assert after.basis.target == flow.rig.target.name
        await session.rollback()


async def test_saved_attempt_identity_is_revalidated_after_outbox_cleanup(
    submit, transport_request
):
    flow = submit
    await submission_observer(flow.rig.outbox.targets, flow.rig.factory, flow.notices.append)(
        flow.claim, flow.outcome()
    )
    async with flow.rig.factory.begin() as session:
        task = await session.scalar(
            select(TransportTask).where(
                TransportTask.transport_task_id == flow.handle.transport_task_id
            )
        )
        task.submission_acceptance = {
            **task.submission_acceptance,
            "message_id": flow.claim.message_id + 1,
        }
        await session.execute(
            delete(ReliableMessage).where(ReliableMessage.id == flow.claim.message_id)
        )
    service = TransportService(
        flow.rig.outbox, target=flow.rig.target.name, owner_domain="original"
    )
    async with flow.rig.factory() as session:
        result = await service.get_submission_acceptance(
            session,
            transport_task_id=flow.handle.transport_task_id,
            client_request_id=flow.handle.client_request_id,
            caller=transport_request["caller"],
        )
        assert result.state == "INVALID" and result.basis is None


@pytest.mark.parametrize(
    "sqlstate,failures", [("40001", 2), ("40P01", 3), ("other", 1), ("lost_reply", 1)]
)
async def test_evidence_transactions_retry_without_changing_the_original_claim(
    submit, sqlstate, failures
):
    flow = submit
    attempts = []
    initial_isolations = []

    class Failure(Exception):
        pass

    @asynccontextmanager
    async def factory():
        async with flow.rig.factory() as session:
            attempts.append(session)
            if session.get_bind().dialect.name == "postgresql":

                @event.listens_for(session.sync_session, "after_begin")
                def capture_isolation(_, transaction, connection):
                    if not transaction.nested:
                        initial_isolations.append(
                            connection.scalar(text("SHOW transaction_isolation"))
                        )

            if len(attempts) <= failures:

                def fail(sync_session):
                    if sync_session.in_nested_transaction():
                        return
                    assert flow.notices == []
                    error = Failure()
                    error.sqlstate = sqlstate
                    raise OperationalError(None, None, error)

                if sqlstate != "lost_reply":
                    event.listen(session.sync_session, "before_commit", fail)
            yield session
            if sqlstate == "lost_reply" and len(attempts) == 1:
                raise ConnectionError("lost commit reply")

    observe = submission_observer(flow.rig.outbox.targets, factory, flow.notices.append)
    if sqlstate in {"other", "40P01"}:
        with pytest.raises(OperationalError):
            await observe(flow.claim, flow.outcome())
    else:
        await observe(flow.claim, flow.outcome())
    if flow.rig.engine.dialect.name == "postgresql" and sqlstate in {"40001", "40P01"}:
        assert initial_isolations == ["repeatable read"] * 3
    accepted = sqlstate in {"40001", "lost_reply"}
    assert len(attempts) == {"40001": 3, "40P01": 3, "other": 2, "lost_reply": 2}[sqlstate]
    assert flow.notices == (["original"] if accepted else [])
    async with flow.rig.factory() as session:
        task = await session.scalar(
            select(TransportTask).where(
                TransportTask.transport_task_id == flow.handle.transport_task_id
            )
        )
        assert (task.submission_acceptance is not None) is accepted
        message = await message_history_view(
            session, await session.get(ReliableMessage, flow.claim.message_id)
        )
        assert message.status == "SENDING" and message.lease_id == flow.claim.lease_id


async def test_acceptance_body_references_preserve_original_basis(submit, transport_request):
    flow = submit
    from app.core.strict_json import canonical_digest

    await submission_observer(flow.rig.outbox.targets, flow.rig.factory, flow.notices.append)(
        flow.claim, flow.outcome()
    )
    async with flow.rig.factory() as session:
        result = await TransportService(
            flow.rig.outbox, owner_domain="original"
        ).get_submission_acceptance(
            session,
            transport_task_id=flow.handle.transport_task_id,
            client_request_id=flow.handle.client_request_id,
            caller=transport_request["caller"],
        )
        assert result.state == "ACCEPTED"
        task = await session.scalar(
            select(TransportTask).where(
                TransportTask.transport_task_id == flow.handle.transport_task_id
            )
        )
        stored = task.submission_acceptance
        assert "request_raw" not in stored and "response_raw" not in stored
        assert stored["format_version"] == 2
        assert stored["evidence_sha256"] == canonical_digest(asdict(result.basis))
        assert result.basis.lease_id == flow.claim.lease_id and result.basis.saved_at is not None


async def test_acceptance_observer_survives_later_finish_failure_without_duplicate_body(
    submit, transport_request, monkeypatch
):
    from app.models import IntegrationRecord

    flow = submit
    outcome = flow.outcome()
    await submission_observer(flow.rig.outbox.targets, flow.rig.factory, flow.notices.append)(
        flow.claim, outcome
    )
    original_finish = history_records.finished

    async def fail(*args, **kwargs):
        raise RuntimeError("injected finish failure")

    monkeypatch.setattr(history_records, "finished", fail)
    with pytest.raises(RuntimeError):
        await flow.store.finish(flow.claim, outcome)
    async with flow.rig.factory() as session:
        task = await session.scalar(
            select(TransportTask).where(
                TransportTask.transport_task_id == flow.handle.transport_task_id
            )
        )
        assert task.submission_acceptance is not None
        before = list(
            await session.scalars(
                select(IntegrationRecord).where(
                    IntegrationRecord.exchange_id == flow.claim.message.exchange_id,
                    IntegrationRecord.record_kind == "HTTP_RESPONSE_OBSERVED",
                )
            )
        )
        assert len(before) == 1 and before[0].raw_body == outcome.result.body
    monkeypatch.setattr(history_records, "finished", original_finish)
    assert await flow.store.finish(flow.claim, outcome)
    async with flow.rig.factory() as session:
        rows = list(
            await session.scalars(
                select(IntegrationRecord).where(
                    IntegrationRecord.exchange_id == flow.claim.message.exchange_id,
                    IntegrationRecord.raw_body == outcome.result.body,
                )
            )
        )
        assert len(rows) == 1 and rows[0].id == before[0].id


async def test_observer_retries_whole_transaction_after_core_record_serialization_failure(
    submit, monkeypatch
):
    flow = submit
    original = history_records.observe_response
    sessions = []

    class Failure(Exception):
        sqlstate = "40001"

    async def fail_twice(session, **kwargs):
        sessions.append(session)
        if len(sessions) <= 2:
            try:
                raise OperationalError(None, None, Failure())
            except OperationalError as error:
                raise history_records.EvidencePersistenceError(
                    "integration evidence unavailable"
                ) from error
        return await original(session, **kwargs)

    monkeypatch.setattr(history_records, "observe_response", fail_twice)
    await submission_observer(flow.rig.outbox.targets, flow.rig.factory, flow.notices.append)(
        flow.claim, flow.outcome()
    )
    assert len(sessions) == 3 and len({id(s) for s in sessions}) == 3
    assert flow.notices == ["original"]


async def test_consumption_keeps_independent_acceptance_source_after_outbox_cleanup(submit):
    from app.models import IntegrationRecord

    flow = submit
    await submission_observer(flow.rig.outbox.targets, flow.rig.factory, flow.notices.append)(
        flow.claim, flow.outcome()
    )
    async with flow.rig.factory.begin() as session:
        task = await session.scalar(
            select(TransportTask).where(
                TransportTask.transport_task_id == flow.handle.transport_task_id
            )
        )
        reply_id = task.acceptance_response_record_id
        await session.execute(
            delete(ReliableMessage).where(ReliableMessage.id == flow.claim.message_id)
        )
        identity = await history_records.record_consumption(
            session,
            owner_domain="original",
            action_id="departure",
            source=history_records.SourceRef("TRANSPORT_TASK", task.transport_task_id),
            consumed_at=datetime.now(UTC),
        )
    async with flow.rig.factory() as session:
        record = await session.get(IntegrationRecord, identity)
        assert record.parent_record_id == reply_id
        assert record.details["coverage_reason"] == "ORIGINAL_REFERENCE"
