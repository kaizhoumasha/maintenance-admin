import asyncio

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import get_settings
from app.core.identifiers import uuid7
from app.http_client import DeliveryState, FailureKind, HttpResult
from app.integrations import history
from app.integrations.runtime_authority import require_reconciled
from app.integrations.wms_transport import (
    PROTOCOL,
    Rack,
    RackPosition,
    TransportCaller,
    TransportService,
    TransportSubmitAdapter,
)
from app.models import (
    IntegrationRecord,
    ReliableDeliveryAttempt,
    ReliableMessage,
    TransportHttpExchange,
    TransportTask,
)
from app.reliable import Outbox, ScopeRef
from app.reliable.config import TargetConfig
from app.reliable.sender import Sender
from app.reliable.store import MessageStore
from app.reliable.testing import FakeClock, ScriptedHttpClient
from scripts.retire_http_exchange_legacy import CHECKS, PROOFS, WRITERS, retire
from tests.helpers import run_alembic


@pytest.mark.parametrize(
    "state_migration",
    [
        ("", "sqlite", "http_exchange_runtime_expand"),
        ("TEST_HISTORY_MIGRATION_DATABASE_URL", "postgresql", "http_exchange_runtime_expand"),
    ],
    indirect=True,
    ids=["sqlite", "postgresql"],
)
async def test_upgrade_closes_only_transport_with_complete_unsent_history(
    state_migration, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "INTEGRATION_RUNTIME_AUTHORITY", False)
    engine = state_migration.engine
    factory = async_sessionmaker(engine, expire_on_commit=False)
    clock = FakeClock()
    target = TargetConfig("transport", "http://wms.test", PROTOCOL, fixed_retry_interval_s=1)
    targets = {target.name: target}
    adapters = {PROTOCOL: TransportSubmitAdapter()}
    outbox = Outbox(targets, adapters, clock=clock, records=history)
    service = TransportService(outbox, target=target.name, owner_domain="roller", clock=clock)
    caller = TransportCaller("L1")
    cycle_id = str(uuid7())
    # The current sender reads the new column. Restore the old gate state below before cutover.
    async with engine.begin() as connection:
        for column in (
            "dispatch_attempt_count INT",
            "undispatched_attempts INT NOT NULL DEFAULT 0",
            "settled_retry_budget INT",
            "dispatch_retry_limit INT",
            "budget_checked_lease_id VARCHAR(64)",
            "scope_stopped_lease_id VARCHAR(64)",
        ):
            await connection.execute(text(f"ALTER TABLE reliable_message ADD {column}"))
        await connection.execute(
            text(
                "ALTER TABLE reliable_delivery_attempt "
                "ADD counts_toward_retry_budget BOOLEAN NOT NULL DEFAULT TRUE"
            )
        )
    complete = ("complete", "complete_retry")
    cases = {}
    for case in (
        *complete,
        "missing_open",
        "missing_attempt",
        "extra_finish",
        "wrong_gate",
        "wrong_operation",
        "unknown",
        "accepted",
        "positions",
    ):
        scope = ScopeRef("roller", "TASK", case)
        async with factory.begin() as session:
            handle = await service.move_rack(
                session,
                client_request_id=str(uuid7()),
                caller=caller,
                rack_id="R1",
                source=Rack("R1"),
                target=RackPosition("DEST"),
                rcs_template_id="CTU03",
                cycle_id=cycle_id,
                scope=scope,
            )
        result = (
            HttpResult(DeliveryState.DELIVERY_UNKNOWN, FailureKind.READ_TIMEOUT, None, (), None)
            if case == "unknown"
            else HttpResult(DeliveryState.NOT_SENT, FailureKind.CONNECT_ERROR, None, (), None)
        )
        attempts = 2 if case == "complete_retry" else 1
        client = ScriptedHttpClient(*([result] * attempts))
        sender = Sender(
            MessageStore(factory, clock=clock, records=history),
            targets,
            adapters,
            {target.name: client},
            clock=clock,
        )
        try:
            for _ in range(attempts):
                assert await sender.tick() == 1
                await asyncio.wait_for(asyncio.gather(*tuple(sender.inflight)), 5)
                clock.advance(1)
        finally:
            await sender.aclose()
        assert len(client.requests) == attempts
        async with factory.begin() as session:
            (stop,) = await outbox.stop_scope(session, scope, reason="STOPPED")
            message = await session.get(ReliableMessage, stop.message_id)
            journals = tuple(
                await session.scalars(
                    select(ReliableDeliveryAttempt)
                    .where(ReliableDeliveryAttempt.message_id == message.id)
                    .order_by(ReliableDeliveryAttempt.started_at)
                )
            )
            assert len(journals) == attempts
            if case == "unknown":
                assert message.dispatch_started_at == journals[0].started_at
            first = journals[0]
            message.dispatch_started_at = (
                message.updated_at if case == "wrong_gate" else first.started_at
            )
            if case == "complete":
                # The old journal stored the target key as its peer endpoint.
                first.peer_endpoint = message.target
            cases[case] = (handle, message.id, message.operation_id, tuple(j.id for j in journals))
            if case == "missing_open":
                await session.execute(
                    delete(IntegrationRecord).where(IntegrationRecord.id == first.id)
                )
            if case == "extra_finish":
                await history.write_record(
                    session,
                    draft=history.RecordDraft(
                        exchange_id=first.exchange_id,
                        record_kind="SEND_ATTEMPT_FINISHED",
                        event_key=f"FINISH:{first.lease_id}:STALLED:None:0",
                        direction="INTERNAL",
                        attempt_id=first.lease_id,
                        network_state="NOT_SENT",
                        application_state="STALLED",
                        late=False,
                    ),
                )
    async with engine.begin() as connection:
        for column in (
            "dispatch_attempt_count",
            "undispatched_attempts",
            "settled_retry_budget",
            "dispatch_retry_limit",
            "budget_checked_lease_id",
            "scope_stopped_lease_id",
        ):
            await connection.execute(text(f"ALTER TABLE reliable_message DROP COLUMN {column}"))
        await connection.execute(
            text("ALTER TABLE reliable_delivery_attempt DROP COLUMN counts_toward_retry_budget")
        )
    url = engine.url.render_as_string(hide_password=False)
    cutover = await run_alembic(
        url, "-x", "fence_legacy_writes=true", "upgrade", "smt_auto_outbound_v1"
    )
    assert cutover.returncode == 0, cutover.stdout + cutover.stderr
    monkeypatch.setattr(settings, "INTEGRATION_RUNTIME_AUTHORITY", True)
    async with factory.begin() as session:
        await session.execute(
            delete(ReliableDeliveryAttempt).where(
                ReliableDeliveryAttempt.id == cases["missing_attempt"][3][0]
            )
        )
        await session.execute(
            update(ReliableDeliveryAttempt)
            .where(ReliableDeliveryAttempt.id == cases["wrong_operation"][3][0])
            .values(operation_id=str(uuid7()))
        )
        for case, changes in (
            ("accepted", {"submission_acceptance": {"historic": "unverified"}}),
            ("positions", {"member_positions": {"C1": {"TARGET_PLACED": "DEST"}}}),
        ):
            await session.execute(
                update(TransportTask)
                .where(TransportTask.transport_task_id == cases[case][0].transport_task_id)
                .values(**changes)
            )

    task_ids = tuple(case[0].transport_task_id for case in cases.values())
    async with factory() as session:
        before = await service.list_unclosed_predecessors(
            session, caller=caller, cycle_id=cycle_id, predecessor_ids=task_ids, rack_id="R1"
        )
        assert len(before.unresolved) == len(cases)

    upgrade = await run_alembic(
        url, "upgrade", "head", environment={"INTEGRATION_RUNTIME_AUTHORITY": "true"}
    )
    assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr
    await engine.dispose()
    await require_reconciled(factory)
    async with factory() as session:
        after = await service.list_unclosed_predecessors(
            session, caller=caller, cycle_id=cycle_id, predecessor_ids=task_ids, rack_id="R1"
        )
        for closed in after.items[: len(complete)]:
            assert closed.reason == "SUBMIT_NEVER_DISPATCHED"
            assert closed.closure == "CLOSED"
        assert after.unresolved == before.items[len(complete) :]
        for case, (handle, message_id, operation_id, journal_ids) in cases.items():
            stored = await session.get(ReliableMessage, message_id)
            assert (stored.operation_id, stored.business_key) == (
                operation_id,
                handle.transport_task_id,
            )
            assert (stored.status, stored.pause_reason, stored.lease_id) == (
                "ABANDONED",
                "STOPPED",
                None,
            )
            assert (stored.dispatch_started_at is None) == (case in complete)
            expected_count = len(journal_ids) if case in complete else None
            assert stored.dispatch_attempt_count == expected_count
            assert list(
                await session.scalars(
                    select(ReliableDeliveryAttempt.id)
                    .where(ReliableDeliveryAttempt.message_id == message_id)
                    .order_by(ReliableDeliveryAttempt.started_at)
                )
            ) == ([] if case == "missing_attempt" else list(journal_ids))
    assert await MessageStore(factory, clock=clock, records=history).claim(1, targets) == ()


@pytest.mark.parametrize(
    "state_migration,retire_legacy",
    [
        (("", "sqlite", "http_exchange_runtime_expand"), False),
        (
            ("TEST_HISTORY_MIGRATION_DATABASE_URL", "postgresql", "http_exchange_runtime_expand"),
            False,
        ),
        (
            ("TEST_HISTORY_MIGRATION_DATABASE_URL", "postgresql", "http_exchange_runtime_expand"),
            True,
        ),
    ],
    indirect=["state_migration"],
    ids=["sqlite", "postgresql", "postgresql-retired"],
)
async def test_upgrade_recovers_original_runtime_not_sent_submit(
    state_migration, retire_legacy, monkeypatch
):
    engine = state_migration.engine
    url = engine.url.render_as_string(hide_password=False)
    cutover = await run_alembic(
        url, "-x", "fence_legacy_writes=true", "upgrade", "smt_auto_outbound_v1"
    )
    assert cutover.returncode == 0, cutover.stdout + cutover.stderr
    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    clock = FakeClock()
    target = TargetConfig("transport", "http://wms.test", PROTOCOL, fixed_retry_interval_s=1)
    targets = {target.name: target}
    adapters = {PROTOCOL: TransportSubmitAdapter()}
    outbox = Outbox(targets, adapters, clock=clock, records=history)
    service = TransportService(outbox, target=target.name, owner_domain="roller", clock=clock)
    caller = TransportCaller("L1")
    cycle_id = str(uuid7())
    async with engine.begin() as connection:
        # Temporarily let the current ORM run against the installed predecessor.
        for column in (
            "dispatch_attempt_count INT",
            "undispatched_attempts INT NOT NULL DEFAULT 0",
            "settled_retry_budget INT",
            "dispatch_retry_limit INT",
            "budget_checked_lease_id VARCHAR(64)",
            "scope_stopped_lease_id VARCHAR(64)",
        ):
            await connection.execute(text(f"ALTER TABLE reliable_message ADD {column}"))
        await connection.execute(
            text(
                "ALTER TABLE reliable_delivery_attempt "
                "ADD counts_toward_retry_budget BOOLEAN NOT NULL DEFAULT TRUE"
            )
        )
    store = MessageStore(factory, clock=clock, records=history)
    closed = ("complete",) if retire_legacy else ("complete", "complete_retry")
    cases = {}
    for case in (
        "complete",
        "complete_retry",
        "wrong_request",
        "wrong_hash",
        "response_projection",
        "legacy_pointer",
    ):
        scope = ScopeRef("roller", "TASK", case)
        async with factory.begin() as session:
            handle = await service.move_rack(
                session,
                client_request_id=str(uuid7()),
                caller=caller,
                rack_id="R1",
                source=Rack("R1"),
                target=RackPosition("DEST"),
                rcs_template_id="CTU03",
                cycle_id=cycle_id,
                scope=scope,
            )
        count = 2 if case == "complete_retry" else 1
        result = HttpResult(DeliveryState.NOT_SENT, FailureKind.CONNECT_ERROR, None, (), None)
        client = ScriptedHttpClient(*([result] * count))
        sender = Sender(store, targets, adapters, {target.name: client}, clock=clock)
        try:
            for _ in range(count):
                assert await sender.tick() == 1
                await asyncio.wait_for(asyncio.gather(*tuple(sender.inflight)), 5)
                clock.advance(1)
        finally:
            await sender.aclose()
        assert len(client.requests) == count
        async with factory.begin() as session:
            (stop,) = await outbox.stop_scope(session, scope, reason="STOPPED")
            message = await session.get(ReliableMessage, stop.message_id)
            attempts = (
                await session.scalars(
                    select(ReliableDeliveryAttempt)
                    .where(ReliableDeliveryAttempt.message_id == message.id)
                    .order_by(ReliableDeliveryAttempt.started_at)
                )
            ).all()
            assert len(attempts) == message.attempts == message.not_received_count == count
            assert message.last_response_record_id is None
            assert await session.get(IntegrationRecord, message.request_record_id) is None
            for attempt in attempts:
                assert (attempt.attempt_state, attempt.close_reason) == ("NOT_SENT", "FINISHED")
                attempt.peer_endpoint = target.name  # The predecessor stores the target key.
            attempt = attempts[0]
            message.dispatch_started_at = attempt.started_at
            message.dispatch_attempt_count = None
            # The predecessor retained the final settled lease's dispatch flag on scope stop.
            message.lease_not_started = False
            if case == "wrong_request":
                attempt.request_record_id = uuid7()
            elif case == "wrong_hash":
                message.frozen_request_sha256 = "0" * 64
            elif case == "response_projection":
                session.add(
                    TransportHttpExchange(
                        id=attempt.id,
                        transport_task_id=handle.transport_task_id,
                        task_match_state="MATCHED",
                        initiator="WES",
                        interaction_kind="SUBMIT",
                        traffic_source=attempt.traffic_source,
                        started_at=attempt.started_at,
                        finished_at=attempt.finished_at,
                        response_observed_at=attempt.finished_at,
                        http_method=attempt.http_method,
                        endpoint_path=attempt.endpoint_path,
                        request_wire_state="UNAVAILABLE",
                        response_wire_state="UNAVAILABLE",
                        message_id=message.id,
                        lease_id=attempt.lease_id,
                        attempt_state="RESPONSE_INCOMPLETE",
                        http_status=503,
                    )
                )
            elif case == "legacy_pointer":
                message.last_response_record_id = uuid7()
            cases[case] = (handle.transport_task_id, message, attempts)
    task_ids = tuple(task_id for task_id, _, _ in cases.values())
    async with factory() as session:
        before = await service.list_unclosed_predecessors(
            session, caller=caller, cycle_id=cycle_id, predecessor_ids=task_ids, rack_id="R1"
        )
        assert len(before.unresolved) == len(cases)
        assert before.unresolved[0].reason == "DELIVERY_OR_RESULT_UNCERTAIN"
    async with engine.begin() as connection:
        for column in (
            "dispatch_attempt_count",
            "undispatched_attempts",
            "settled_retry_budget",
            "dispatch_retry_limit",
            "budget_checked_lease_id",
            "scope_stopped_lease_id",
        ):
            await connection.execute(text(f"ALTER TABLE reliable_message DROP COLUMN {column}"))
        await connection.execute(
            text("ALTER TABLE reliable_delivery_attempt DROP COLUMN counts_toward_retry_budget")
        )
        if retire_legacy:
            evidence = (
                dict.fromkeys(WRITERS, "DRAINED")
                | dict.fromkeys(CHECKS, "RETIRED")
                | dict.fromkeys(PROOFS, "isolated-fixture")
            )
            await retire(connection, evidence, apply=True)

    upgrade = await run_alembic(
        url,
        "upgrade",
        "head",
        environment={"INTEGRATION_RUNTIME_AUTHORITY": "true"},
    )
    assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr
    await engine.dispose()
    await require_reconciled(factory)
    async with factory() as session:
        after = await service.list_unclosed_predecessors(
            session, caller=caller, cycle_id=cycle_id, predecessor_ids=task_ids, rack_id="R1"
        )
        assert after.unresolved == before.unresolved[len(closed) :]
        for item in after.items[: len(closed)]:
            assert item.reason == "SUBMIT_NEVER_DISPATCHED"
        for case, (task_id, message, attempts) in cases.items():
            stored = await session.get(ReliableMessage, message.id)
            assert (stored.exchange_id, stored.request_record_id, stored.operation_id) == (
                message.exchange_id,
                message.request_record_id,
                message.operation_id,
            )
            assert stored.business_key == task_id
            for attempt in attempts:
                journal = await session.get(ReliableDeliveryAttempt, (stored.id, attempt.lease_id))
                assert journal.id == attempt.id and journal.counts_toward_retry_budget
            assert (stored.status, stored.pause_reason, stored.lease_id) == (
                "ABANDONED",
                "STOPPED",
                None,
            )
            assert stored.dispatch_started_at == (
                None if case in closed else attempts[0].started_at
            )
            assert stored.dispatch_attempt_count == (len(attempts) if case in closed else None)
    assert await store.claim(1, targets) == ()
