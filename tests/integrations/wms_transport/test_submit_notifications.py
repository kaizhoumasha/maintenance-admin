"""Native submit acceptance wakes only its original business after commit."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from sqlalchemy import event, select

from app import business_wakeups
from app.businesses import catalog
from app.businesses.runtime import BusinessRuntime
from app.core.identifiers import uuid7
from app.http_client import DeliveryState, HttpResult
from app.integrations.wms_transport import (
    ADAPTERS,
    Rack,
    RackPosition,
    TransportCaller,
    TransportService,
)
from app.models import ReliableMessage, TransportTask
from app.reliable.config import ReliableConfig
from app.reliable.store import AttemptOutcome, MessageStore
from app.reliable.types import FailReason, MessageStatus


@pytest.mark.parametrize("status,code", [(202, "RECEIVED"), (200, "DUPLICATE")])
async def test_application_sender_wakes_original_submit_owner(
    pg_transport_rig, pg_url, monkeypatch, status, code
):
    import app.main as main
    from app.reliable_runtime import reliable_lifespan

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
        message = await session.scalar(
            select(ReliableMessage).where(ReliableMessage.operation_id == original.operation_id)
        )
        frozen = message.request_raw
    service.owner_domain = "current"
    notices = []
    monkeypatch.setattr(catalog, "definitions", lambda: ())
    monkeypatch.setattr(
        catalog, "progressions", lambda: {"original": "scan_pending", "current": "scan_pending"}
    )
    monkeypatch.setattr(business_wakeups, "enqueue_business", notices.append)

    class Peer:
        async def send(self, request):
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
    application = FastAPI()
    application.state.ecs_commands = SimpleNamespace()
    application.state.business_runtime = BusinessRuntime({}, {})
    async with main.lifespan(application):
        async with asyncio.timeout(5):
            while True:
                async with rig.factory() as session:
                    message = await session.scalar(
                        select(ReliableMessage).where(ReliableMessage.operation_id == identity[0])
                    )
                    if message.status == "DONE":
                        break
                await asyncio.sleep(0.01)
        assert notices == ["original"]
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
            assert original.applied_outcome_revision is None


@pytest.fixture
async def submit(transport_storage, transport_request, monkeypatch):
    rig = transport_storage
    notices = []
    monkeypatch.setattr(
        catalog, "progressions", lambda: {"original": "scan_pending", "current": "scan_pending"}
    )
    monkeypatch.setattr(business_wakeups, "enqueue_business", notices.append)
    service = TransportService(
        rig.outbox, target=rig.target.name, owner_domain="original", clock=rig.clock
    )
    async with rig.factory.begin() as session:
        handle = await service.move_rack(session, **transport_request)
    store = MessageStore(
        rig.factory,
        clock=rig.clock,
        on_result=business_wakeups.wms_result_committed,
        on_delivered=business_wakeups.transport_submit_delivered,
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


@pytest.mark.parametrize("status,code", [(202, "RECEIVED"), (200, "DUPLICATE")])
async def test_current_acceptance_notifies_once_without_applied_outcome(submit, status, code):
    flow = submit
    outcome = flow.outcome(status, code)
    original = flow.claim.message
    assert await flow.store.finish(flow.claim, outcome)
    assert flow.notices == ["original"]
    assert not await flow.store.finish(flow.claim, outcome)
    assert flow.notices == ["original"]
    async with flow.rig.factory() as session:
        task = await session.scalar(
            select(TransportTask).where(
                TransportTask.transport_task_id == flow.handle.transport_task_id
            )
        )
        message = await session.get(ReliableMessage, flow.claim.message_id)
        assert message.status == "DONE" and message.response_raw == outcome.result.body
        assert message.operation_id == original.operation_id
        assert message.business_key == original.business_key
        assert message.request_raw == original.request_raw
        assert task.applied_outcome_revision is None and task.owner_domain == "original"


@pytest.mark.parametrize(
    "case", ["task_id", "operation_id", "unowned", "unknown_owner", "lost_lease"]
)
async def test_acceptance_never_guesses_original_owner(submit, case):
    flow = submit
    async with flow.rig.factory.begin() as session:
        task = await session.scalar(
            select(TransportTask).where(
                TransportTask.transport_task_id == flow.handle.transport_task_id
            )
        )
        if case == "task_id":
            task.transport_task_id = "transport-" + str(uuid7())
        elif case == "operation_id":
            task.operation_id = str(uuid7())
        elif case == "unowned":
            task.owner_domain = None
        elif case == "unknown_owner":
            task.owner_domain = "removed"
        else:
            message = await session.get(ReliableMessage, flow.claim.message_id)
            message.lease_id = "new-lease"
    assert await flow.store.finish(flow.claim, flow.outcome()) is (case != "lost_lease")
    assert flow.notices == []


@pytest.mark.parametrize("case", ["unknown", "unavailable", "failed", "expired"])
async def test_unaccepted_submit_never_notifies_owner(submit, case):
    flow = submit
    if case == "expired":
        flow.rig.clock.advance(3600)
        outcome = AttemptOutcome(MessageStatus.EXPIRED)
    else:
        status, code = {
            "unknown": (202, "BOGUS"),
            "unavailable": (503, "UNAVAILABLE"),
            "failed": (422, "REJECTED"),
        }[case]
        outcome = flow.outcome(
            status, code, **({"reason_code": "INVALID_DATA"} if case == "failed" else {})
        )
        if case == "failed":
            outcome = AttemptOutcome(
                MessageStatus.FAILED,
                fail_reason=FailReason.REJECTED,
                result=outcome.result,
                verdict=outcome.verdict,
            )
        else:
            outcome = AttemptOutcome(
                MessageStatus.PENDING,
                result=outcome.result,
                verdict=outcome.verdict,
                next_attempt_at=flow.rig.clock(),
            )
    assert await flow.store.finish(flow.claim, outcome)
    assert flow.notices == []


@pytest.mark.parametrize("end", ["commit", "rollback", "savepoint_rollback", "commit_failure"])
async def test_submit_notice_waits_for_successful_outer_commit(submit, end):
    flow = submit
    async with flow.rig.factory() as session:
        await session.begin()
        begin_nested = session.begin_nested
        session.begin = begin_nested

        @asynccontextmanager
        async def factory():
            yield session

        flow.store.session_factory = factory
        if end == "savepoint_rollback":

            def reject_savepoint(_):
                raise ValueError("savepoint rejected")

            event.listen(session.sync_session, "before_commit", reject_savepoint, once=True)
            with pytest.raises(ValueError, match="savepoint rejected"):
                await flow.store.finish(flow.claim, flow.outcome())
            await session.commit()
        else:
            assert await flow.store.finish(flow.claim, flow.outcome())
            assert flow.notices == []
            if end == "rollback":
                await session.rollback()
            elif end == "commit_failure":

                def reject_outer(_):
                    raise ValueError("outer commit rejected")

                event.listen(session.sync_session, "before_commit", reject_outer, once=True)
                with pytest.raises(ValueError, match="outer commit rejected"):
                    await session.commit()
                await session.rollback()
            else:
                await session.commit()
    assert flow.notices == (["original"] if end == "commit" else [])
    async with flow.rig.factory() as reader:
        row = await reader.get(ReliableMessage, flow.claim.message_id)
        assert row.status == ("DONE" if end == "commit" else "SENDING")
        assert (row.response_raw is not None) is (end == "commit")
