import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from pydantic import ConfigDict
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.businesses._components.sorting_task.contracts import Cancel
from app.core.config import get_settings
from app.core.identifiers import uuid7
from app.http_client import DeliveryState, HttpResult
from app.integrations import history
from app.integrations.ecs_callback.receiver import receive_ecs_event
from app.integrations.ecs_command import PROTOCOL_RETRY_WINDOW, compile_commands
from app.integrations.ecs_command.lifecycle import EcsLifecycle
from app.integrations.ecs_command.protocol import PROTOCOL, EcsCommandAdapter
from app.integrations.ecs_command.result import receive_ecs_result
from app.integrations.ecs_command.routing import RouteAuthorization, RouteSnapshot
from app.integrations.ecs_command.service import CommandOwner, EcsCommands
from app.integrations.runtime_authority import require_reconciled
from app.integrations.wms_callback import CallbackErrors, callback_operation, receive, registry
from app.integrations.wms_transport import (
    BinMove,
    HandoffPosition,
    Rack,
    RackBinSlot,
    RackPosition,
    TransportCaller,
    TransportService,
)
from app.integrations.wms_transport.acceptance import submission_observer
from app.integrations.wms_transport.adapter import TransportSubmitAdapter
from app.integrations.wms_transport.protocol import PROTOCOL as TRANSPORT_PROTOCOL
from app.integrations.wms_transport.wire import bin_move_data
from app.models import CallbackReceipt, IntegrationExchange, IntegrationRecord, TransportTask
from app.reliable import Outbox, Verdict, VerdictKind
from app.reliable.config import TargetConfig
from app.reliable.store import AttemptOutcome, MessageStore
from app.reliable.types import MessageStatus
from app.services.http_exchange_diagnostics import diagnostics
from tests.helpers import EcsMove, run_alembic, seed_transport_task

RESULTED = "transport.task.resulted@v1"
POSITION = "transport.task.member_position_changed@v1"


def wire(operation, identity, data):
    return json.dumps(
        dict(operation=operation, operation_id=identity, timestamp=1, data=data)
    ).encode()


class Routes:
    async def resolve_declared(self, session, device_code, declaration):
        return RouteSnapshot(
            device_code,
            "http://ecs.test",
            "route-1",
            "v1",
            "test",
            "1",
            ("MOVE",),
            PROTOCOL_RETRY_WINDOW,
        )

    async def authorize_frozen(self, session, route):
        return RouteAuthorization.ALLOW


def ecs(factory):
    target = TargetConfig("ecs", None, PROTOCOL, address_mode="SNAPSHOT")
    targets = {"ecs": target}
    adapter = EcsCommandAdapter()
    outbox = Outbox(targets, {PROTOCOL: adapter}, records=history)
    service = EcsCommands(outbox, "ecs", Routes(), compile_commands(EcsMove)[1])
    outbox.lifecycles = {PROTOCOL: EcsLifecycle(service)}
    store = MessageStore(factory, lifecycles=outbox.lifecycles, records=history)
    return service, store, adapter, targets


async def command_at_gate(factory, service, store, adapter, targets, call):
    async with factory.begin() as session:
        command = await service.create(
            session,
            owner=CommandOwner("test"),
            call_key=call,
            device_code="D1",
            timeout_ms=30000,
            command=EcsMove.model_validate({"qty": 1}),
        )
    (claim,) = await store.claim(1, targets)
    request = adapter.build_request(claim.message, targets["ecs"])
    assert await store.before_send(replace(claim, request=request), targets["ecs"])
    return command, claim


def result_wire(command):
    return json.dumps(
        dict(
            command_code=command.command_code,
            device_code="D1",
            result="SUCCESS",
            finish_time=1,
            data={"qty": 1},
        )
    ).encode()


async def seed_transport(factory):
    slot = RackBinSlot("R1", "90", "S1")
    location = dict(kind="RACK_BIN_SLOT", rack_id="R1", rack_face="90", slot_id="S1")
    task_id = "transport-" + str(uuid7())
    async with factory.begin() as session:
        await seed_transport_task(
            session,
            TransportTask(
                client_request_id=str(uuid7()),
                transport_task_id=task_id,
                operation_id=str(uuid7()),
                kind="BIN_MOVE",
                input_digest="a" * 64,
                workline_id="line",
                created_at=datetime.now(UTC),
                frozen=bin_move_data([BinMove("C1", HandoffPosition("CNV1"), slot)]),
            ),
        )
    original = dict(
        transport_task_id=task_id,
        kind="BIN_MOVE",
        outcome_revision=1,
        results=[dict(container_id="C1", status="SUCCEEDED", final_position=location)],
    )
    trigger = dict(
        transport_task_id=task_id,
        container_id="C1",
        milestone="TARGET_PLACED",
        final_position=location,
    )
    return task_id, wire(RESULTED, str(uuid7()), original), wire(POSITION, str(uuid7()), trigger)


async def save_transport_acceptance(factory):
    target = TargetConfig("transport", "http://transport.test", TRANSPORT_PROTOCOL)
    targets = {target.name: target}
    adapter = TransportSubmitAdapter()
    outbox = Outbox(targets, {TRANSPORT_PROTOCOL: adapter}, records=history)
    service = TransportService(outbox, target=target.name, owner_domain="original")
    caller = TransportCaller("L1")
    request_id = str(uuid7())
    async with factory.begin() as session:
        handle = await service.move_rack(
            session,
            client_request_id=request_id,
            caller=caller,
            rack_id="R1",
            source=Rack("R1"),
            target=RackPosition("DEST"),
            rcs_template_id="CTU03",
        )
    store = MessageStore(factory, records=history)
    (claim,) = await store.claim(1, targets)
    request = adapter.build_request(claim.message, target)
    assert await store.before_send(replace(claim, request=request), target)
    body = json.dumps(
        dict(
            operation_id=claim.message.operation_id,
            code="RECEIVED",
            timestamp=1,
            data=dict(transport_task_id=handle.transport_task_id),
        )
    ).encode()
    result = HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 202, (), body)
    outcome = AttemptOutcome(
        MessageStatus.DONE,
        result=result,
        verdict=Verdict(VerdictKind.DELIVERED, response=body),
    )
    await submission_observer(targets, factory, lambda _: None)(claim, outcome)
    assert await store.finish(claim, outcome)
    async with factory() as session:
        acceptance = await service.get_submission_acceptance(
            session,
            transport_task_id=handle.transport_task_id,
            client_request_id=request_id,
            caller=caller,
        )
    assert acceptance.state == "ACCEPTED"
    return service, handle.transport_task_id, request_id, caller, acceptance


@pytest.mark.parametrize(
    "state_migration",
    [("TEST_HISTORY_MIGRATION_DATABASE_URL", "postgresql", "http_exchange_runtime_expand")],
    indirect=True,
)
async def test_upgrade_requires_fenced_cutover_and_reconciles_messages_added_between_revisions(
    state_migration, monkeypatch
):
    from app.reliable.types import OutgoingMessage
    from app.tasks._db import run_with_factory

    engine = state_migration.engine
    factory = async_sessionmaker(engine)
    now = datetime.now(UTC)
    operation_id = str(uuid7())
    raw = b'{"data":{"frozen":1}}'
    async with factory.begin() as session:
        exchange_id, request_id = await history.register(
            session,
            message=OutgoingMessage("probe", "probe@v1", operation_id, raw, None, None, now, None),
            protocol="wms-decisions",
        )
        await session.execute(
            text("""
            INSERT INTO reliable_message
                (target,operation,operation_id,status,attempts,next_attempt_at,created_at,
                 updated_at,exchange_id,request_record_id)
            VALUES ('probe','probe@v1',:op,'PENDING',0,:now,:now,:now,:exchange,:request)
        """),
            dict(op=operation_id, now=now, exchange=exchange_id, request=request_id),
        )
    url = engine.url.render_as_string(hide_password=False)
    plain = await run_alembic(url, "upgrade", "head")
    assert plain.returncode
    assert "requires -x fence_legacy_writes=true" in plain.stdout + plain.stderr

    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", True)

    async def forbidden(_factory):
        pytest.fail("worker entered business work before database admission")

    import asyncio

    with pytest.raises(RuntimeError, match="successful reconciliation"):
        await asyncio.to_thread(run_with_factory, url, forbidden)
    async with engine.begin() as writer:
        await writer.execute(text("SELECT 1"))
        busy = await run_alembic(url, "-x", "fence_legacy_writes=true", "upgrade", "head")
        assert busy.returncode
        assert "draining existing database transactions" in busy.stdout + busy.stderr
    accepted = await run_alembic(url, "-x", "fence_legacy_writes=true", "upgrade", "head")
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    await require_reconciled(factory)
    wrong_mode = await run_alembic(
        url, "upgrade", "head", environment={"INTEGRATION_RUNTIME_AUTHORITY": "false"}
    )
    assert wrong_mode.returncode
    assert "database requires INTEGRATION_RUNTIME_AUTHORITY=true" in (
        wrong_mode.stdout + wrong_mode.stderr
    )
    async with factory() as session:
        assert await session.scalar(text("SELECT frozen_request_wire FROM reliable_message")) == raw


@pytest.mark.parametrize(
    "state_migration",
    [("TEST_HISTORY_MIGRATION_DATABASE_URL", "postgresql", "http_exchange_runtime_expand")],
    indirect=True,
)
@pytest.mark.parametrize(
    "damage",
    [
        "receipt",
        "ecs",
        "transport",
        "transport_missing",
        "transport_legacy_missing",
        "transport_applied_missing",
        "transport_payload",
        "retention",
        "static_opening",
        "static_opening_unscoped",
        "frozen_request",
    ],
)
async def test_runtime_reconciliation_blocks_activation(state_migration, damage, monkeypatch):
    factory = async_sessionmaker(state_migration.engine, expire_on_commit=False)
    if damage == "receipt":
        raw = wire("unknown.test@v1", str(uuid7()), {})
        async with factory.begin() as session:
            result = await receive(session, raw)
            root = await session.get(IntegrationExchange, result.exchange_id)
            record = await session.get(IntegrationRecord, root.current_response_record_id)
            record.body_sha256 = "0" * 64
        expected = "receipt body conflict"
    elif damage == "ecs":
        service, store, adapter, targets = ecs(factory)
        command, _ = await command_at_gate(factory, service, store, adapter, targets, "damaged")
        async with factory.begin() as session:
            result = await receive_ecs_result(session, result_wire(command), service)
            assert result.status_code == 200
            root = await session.get(IntegrationExchange, result.exchange_id)
            root.applied_result_id = "f" * 64
        expected = "ECS applied result missing or conflicting"
    elif damage in {"static_opening", "static_opening_unscoped", "frozen_request"}:
        target = TargetConfig("probe", "http://probe.test", "wms-decisions")
        from tests.helpers import OpaqueHistoryAdapter

        adapter = OpaqueHistoryAdapter()
        outbox = Outbox({target.name: target}, {target.protocol: adapter}, records=history)
        from app.reliable.types import MessageDraft, ScopeRef

        async with factory.begin() as session:
            reference = await outbox.enqueue(
                session,
                MessageDraft(
                    target=target.name,
                    operation="probe@v1",
                    operation_id=str(uuid7()),
                    request_raw=b"{}",
                    scope=(
                        None
                        if damage == "static_opening_unscoped"
                        else ScopeRef("test", "TASK", "original")
                    ),
                ),
            )
            if damage == "frozen_request":
                await session.execute(
                    text("UPDATE reliable_message SET frozen_request_wire=:wire WHERE id=:id"),
                    {"wire": b"corrupt", "id": reference.message_id},
                )
            else:
                await session.execute(
                    text("""
                    UPDATE reliable_message SET status='SENDING', lease_id='original',
                        lease_until=now(), last_attempt_at=now(),
                        dispatch_started_at=CASE WHEN scope_owner IS NOT NULL THEN now() END
                    WHERE id=:id
                """),
                    {"id": reference.message_id},
                )
        expected = (
            "static opening missing or ambiguous"
            if damage.startswith("static_opening")
            else "message frozen request conflict"
        )
    elif damage == "retention":
        raw = wire("unknown.test@v1", str(uuid7()), {})
        async with factory.begin() as session:
            result = await receive(session, raw)
            await history.link_receipt(
                session,
                receipt_id=result.receipt_id,
                reference_kind="BUSINESS_ACTION",
                namespace="unknown-owner",
                value="pending-action",
                relation_role="OWNER",
                at=datetime.now(UTC),
            )
            await session.execute(
                text("UPDATE integration_reference SET namespace='' WHERE exchange_id=:root"),
                {"root": result.exchange_id},
            )
        expected = "retention owner identity missing"
    else:
        task_id, raw, trigger = await seed_transport(factory)
        async with factory.begin() as session:
            result = await receive(session, raw)
            assert result.status_code == 202
            original = await session.get(IntegrationExchange, result.exchange_id)
            value = json.loads(raw)["data"]["transport_task_id"] + ":1"
            if damage == "transport":
                await history.write_reference(
                    session,
                    exchange_id=original.id,
                    reference_kind="TRANSPORT_TASK",
                    namespace="transport-outcome",
                    value=value,
                    relation_role="RESULT_SOURCE",
                    created_at=datetime.now(UTC),
                )
            elif damage in (
                "transport_missing",
                "transport_legacy_missing",
                "transport_applied_missing",
            ):
                if damage == "transport_applied_missing":
                    assert (await receive(session, trigger)).status_code == 202
                    await session.execute(
                        text("""
                            DELETE FROM integration_reference WHERE source_record_id IN (
                                SELECT id FROM integration_record WHERE record_kind='RESULT_APPLIED'
                            )
                        """)
                    )
                    await session.execute(
                        text("DELETE FROM integration_record WHERE record_kind='RESULT_APPLIED'")
                    )
                if damage == "transport_legacy_missing":
                    await session.execute(
                        text(
                            "UPDATE transport_task SET pending_result_source_unknown=NULL "
                            "WHERE transport_task_id=:id"
                        ),
                        {"id": task_id},
                    )
                await session.execute(
                    text(
                        "DELETE FROM integration_reference WHERE reference_value=:value "
                        "AND relation_role='RESULT_SOURCE'"
                    ),
                    {"value": value},
                )
            else:
                from app.core.strict_json import canonical_digest

                task = await session.scalar(
                    select(TransportTask).where(TransportTask.transport_task_id == task_id)
                )
                changed = task.pending_outcome["data"] | {"results": []}
                task.pending_outcome = {"revision": 1, "data": changed}
                task.outcome_digests = {"1": canonical_digest(changed)}
        expected = {
            "transport": "Transport result source not unique",
            "transport_missing": "Transport pending result source missing",
            "transport_legacy_missing": "Transport pending result source missing",
            "transport_applied_missing": "Transport applied evidence missing",
            "transport_payload": "Transport pending result source payload conflict",
        }[damage]
    failed = await run_alembic(
        state_migration.engine.url.render_as_string(hide_password=False),
        "-x",
        "fence_legacy_writes=true",
        "upgrade",
        "head",
    )
    assert failed.returncode != 0
    assert expected in failed.stdout + failed.stderr
    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", True)
    with pytest.raises(RuntimeError, match="successful reconciliation"):
        await require_reconciled(factory)
    async with factory() as session:
        assert await session.scalar(text("SELECT to_regclass('callback_receipt')")) is None


@pytest.mark.parametrize(
    "state_migration",
    [("TEST_HISTORY_MIGRATION_DATABASE_URL", "postgresql", "http_exchange_runtime_expand")],
    indirect=True,
)
async def test_replay_and_recovery_with_generic_history_inaccessible(state_migration, monkeypatch):
    factory = async_sessionmaker(state_migration.engine, expire_on_commit=False)
    service, store, adapter, targets = ecs(factory)
    command, claim = await command_at_gate(factory, service, store, adapter, targets, "live")
    closed_command, _ = await command_at_gate(factory, service, store, adapter, targets, "closed")
    transport, accepted_id, request_id, caller, acceptance = await save_transport_acceptance(
        factory
    )
    task_id, original, trigger = await seed_transport(factory)
    applied_task_id, applied_original, applied_trigger = await seed_transport(factory)
    unknown_task_id, unknown_original, _ = await seed_transport(factory)
    operation = "sorting.task.cancel@v1"
    wms_body = dict(task_id="TASK", cancel_scope="TASK", diagnostic_note="old")
    wms_operation_id = str(uuid7())
    wms_wire = wire(operation, wms_operation_id, wms_body)
    monkeypatch.setattr(registry, "_registry", dict(registry._registry))
    registry._registry.pop(operation, None)

    class LegacyCancel(Cancel):
        model_config = ConfigDict(extra="forbid")

    @callback_operation(
        operation,
        data=LegacyCancel,
        errors=CallbackErrors("INVALID_DATA", "INVALID_ENVELOPE", "IDEMPOTENCY_CONFLICT"),
    )
    async def unexpected_handler(ctx, data):
        pytest.fail("an original payload rejection must not run the handler")

    closed_result = json.loads(result_wire(closed_command)) | {
        "result": "FAILED",
        "error_detail": {"code": "E", "msg": "original error"},
    }
    protected_wire = wire("unknown.test@v1", str(uuid7()), {})
    event_wire = b'{"device_code":"D1","event_type":"UNREGISTERED","timestamp":1,"data":{}}'
    async with factory.begin() as session:
        saved_result = await receive_ecs_result(
            session, json.dumps(closed_result).encode(), service
        )
        assert saved_result.status_code == 200
        event = await receive_ecs_event(session, event_wire)
        assert event.status_code == 200
        wms = await receive(session, wms_wire)
        assert wms.status_code == 422 and wms.body["data"] == {"reason_code": "INVALID_DATA"}
        protected = await receive(session, protected_wire)
        await history.link_receipt(
            session,
            receipt_id=protected.receipt_id,
            reference_kind="BUSINESS_ACTION",
            namespace="unknown-owner",
            value="pending-action",
            relation_role="OWNER",
            at=datetime.now(UTC),
        )
        pending = await receive(session, original)
        assert pending.status_code == 202
        applied_receipt = await receive(session, applied_original)
        trigger_receipt = await receive(session, applied_trigger)
        assert applied_receipt.status_code == trigger_receipt.status_code == 202
        from app.core.strict_json import canonical_digest
        from migrations.integration_history_v1 import SOURCES, snapshot_version

        unknown_task = await session.scalar(
            select(TransportTask).where(TransportTask.transport_task_id == unknown_task_id)
        )
        data = json.loads(unknown_original)["data"]
        unknown_task.pending_outcome = {"revision": 1, "data": data}
        unknown_task.outcome_digests = {"1": canonical_digest(data)}
        root = await session.get(IntegrationExchange, unknown_task.submission_exchange_id)
        root.capture_origin = "BACKFILL"
        root.coverage = "LEGACY"
        version = snapshot_version(
            {name: getattr(unknown_task, name) for name in SOURCES["transport_task"].split()}
        )
        await history.write_record(
            session,
            draft=history.RecordDraft(
                exchange_id=root.id,
                record_kind="LEGACY_SNAPSHOT",
                event_key=f"SOURCE:transport_task:{unknown_task.id}:{version}",
                direction="INTERNAL",
                capture_origin="BACKFILL",
                source_kind="transport_task",
                source_key=str(unknown_task.id),
                source_version=version,
            ),
        )

    migration = await run_alembic(
        state_migration.engine.url.render_as_string(hide_password=False),
        "-x",
        "fence_legacy_writes=true",
        "upgrade",
        "head",
    )
    assert migration.returncode == 0, migration.stdout + migration.stderr
    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", True)
    await require_reconciled(factory)
    registration = registry.lookup(operation)
    monkeypatch.setitem(registry._registry, operation, replace(registration, data_model=Cancel))
    with pytest.raises(DBAPIError, match="writes retired"):
        async with factory.begin() as session:
            await session.execute(text("UPDATE integration_exchange SET coverage=coverage"))
    async with factory.begin() as session:
        for table in ("integration_reference", "integration_record", "integration_exchange"):
            await session.execute(text(f"ALTER TABLE {table} RENAME TO hidden_{table}"))
    async with factory() as session:
        ecs_diagnostic = await diagnostics(
            session, peer="ECS", reference=command.command_code, limit=20, before=None
        )
        assert ecs_diagnostic["items"] == []
        assert ecs_diagnostic["runtime"][0]["dispatch_started_at"] is not None
        transport_diagnostic = await diagnostics(
            session, peer="TRANSPORT", reference=task_id, limit=20, before=None
        )
        assert transport_diagnostic["items"] == []
        assert transport_diagnostic["runtime"][0]["pending_result_receipt_id"] == pending.receipt_id
    async with factory.begin() as session:
        replay = await receive(session, wms_wire)
        assert (replay.receipt_id, replay.status_code, replay.prepared_body) == (
            wms.receipt_id,
            wms.status_code,
            wms.prepared_body,
        )
        changed = wire(operation, wms_operation_id, wms_body | {"diagnostic_note": "changed"})
        assert (await receive(session, changed)).status_code == 409
        event_replay = await receive_ecs_event(session, event_wire)
        assert event_replay.receipt_id == event.receipt_id
        assert event_replay.prepared_body == event.prepared_body
        position = await receive(session, trigger)
        assert position.status_code == 202 and position.receipt_id > pending.receipt_id
        result = await receive_ecs_result(session, result_wire(command), service)
        assert result.status_code == 200
        extended = closed_result | {
            "error_detail": closed_result["error_detail"] | {"diagnostic_note": "changed"}
        }
        old_result = await receive_ecs_result(session, json.dumps(extended).encode(), service)
        assert old_result.digest == saved_result.digest
        assert old_result.prepared_body == saved_result.prepared_body
    async with factory.begin() as session:
        replay = await receive_ecs_result(session, result_wire(command), service)
        assert (replay.status_code, replay.prepared_body) == (200, result.prepared_body)
        assert (
            await service.get_command(
                session,
                owner=CommandOwner("test"),
                command_code=command.command_code,
            )
        ).status == "SUCCEEDED"
        task = await session.scalar(
            select(TransportTask).where(TransportTask.transport_task_id == task_id)
        )
        assert task.pending_result_receipt_id is None
        assert task.applied_result_receipt_id == pending.receipt_id
        assert task.applied_trigger_receipt_id == position.receipt_id
        old_task = await session.scalar(
            select(TransportTask).where(TransportTask.transport_task_id == applied_task_id)
        )
        assert old_task.applied_result_receipt_id == applied_receipt.receipt_id
        assert old_task.applied_trigger_receipt_id == trigger_receipt.receipt_id
        unknown_task = await session.scalar(
            select(TransportTask).where(TransportTask.transport_task_id == unknown_task_id)
        )
        assert unknown_task.pending_outcome["revision"] == 1
        assert unknown_task.pending_result_receipt_id is None
        assert unknown_task.pending_result_source_unknown is True
        assert (
            await transport.get_submission_acceptance(
                session,
                transport_task_id=accepted_id,
                client_request_id=request_id,
                caller=caller,
            )
            == acceptance
        )
    from app.integrations.http_exchanges import reconcile_outbound
    from app.models import EcsHttpExchange
    from app.tasks.callback_tasks import run_receipt_cleanup

    await reconcile_outbound(factory)
    async with factory() as session:
        outbound = await session.scalar(
            select(EcsHttpExchange).where(EcsHttpExchange.message_id == claim.message_id)
        )
        assert (outbound.close_reason, outbound.response_wire) == ("SETTLED_BY_CALLBACK", None)
        identity, closed_at = outbound.id, outbound.finished_at
    ack = HttpResult(
        DeliveryState.HTTP_RESPONSE_COMPLETE,
        None,
        200,
        (("content-type", "application/json"),),
        b'{"code":200,"message":"ACK"}',
    )
    outcome = AttemptOutcome(
        MessageStatus.DONE,
        result=ack,
        verdict=Verdict(VerdictKind.DELIVERED, response=ack.body),
    )
    assert not await store.finish(claim, outcome)
    async with factory.begin() as session:
        await session.execute(
            text("DELETE FROM ecs_http_exchange WHERE message_id=:id"), {"id": claim.message_id}
        )
    await reconcile_outbound(factory)
    async with factory() as session:
        outbound = await session.get(EcsHttpExchange, identity)
        assert outbound.finished_at == closed_at
        assert outbound.response_wire == ack.body and outbound.late_response_at is not None
    fresh, fresh_claim = await command_at_gate(factory, service, store, adapter, targets, "fresh")
    assert await store.finish(fresh_claim, outcome)
    async with factory.begin() as session:
        applied = await receive_ecs_result(session, result_wire(fresh), service)
        assert applied.status_code == 200
    await run_receipt_cleanup(factory, 1, now=datetime.now(UTC) + timedelta(days=2))
    async with factory() as session:
        assert not (await session.get(CallbackReceipt, wms.receipt_id)).active
        assert (await session.get(CallbackReceipt, protected.receipt_id)).active
        assert (await session.get(CallbackReceipt, pending.receipt_id)).active


@pytest.mark.parametrize(
    "state_migration",
    [("TEST_HISTORY_MIGRATION_DATABASE_URL", "postgresql", "http_exchange_runtime_expand")],
    indirect=True,
)
@pytest.mark.parametrize(
    "shape,invalid",
    [
        ("live", None),
        ("backfill", None),
        ("live", "lease"),
        ("live", "status"),
        ("live", "conclusion"),
        ("live", "conflicting_conclusion"),
        ("live", "body"),
        ("live", "envelope"),
        ("backfill", "source"),
        ("backfill", "reference"),
        ("backfill", "status"),
    ],
)
async def test_ecs_ack_cutover_checks_original_provenance(state_migration, shape, invalid):
    from app.models import EcsCommand, EcsSendEvidence

    factory = async_sessionmaker(state_migration.engine, expire_on_commit=False)
    service, store, adapter, targets = ecs(factory)
    command, claim = await command_at_gate(factory, service, store, adapter, targets, "ack")
    wire = b'{"code":200,"message":"ACK"}'
    response = HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 200, (), wire)
    assert await store.finish(
        claim,
        AttemptOutcome(
            MessageStatus.DONE,
            result=response,
            verdict=Verdict(VerdictKind.DELIVERED, response=wire),
        ),
    )
    async with factory.begin() as session:
        root = await session.scalar(
            select(IntegrationExchange).where(
                IntegrationExchange.command_code == command.command_code
            )
        )
        ack = await session.get(IntegrationRecord, root.ack_record_id)
        if shape == "live":
            assert ack.record_kind == "SEND_ATTEMPT_FINISHED"
            observation = await session.get(IntegrationRecord, ack.body_record_id)
            if invalid == "lease":
                observation.attempt_id = "different-lease"
            elif invalid == "status":
                observation.http_status = 201
            elif invalid == "body":
                observation.body_sha256 = "0" * 64
            elif invalid == "envelope":
                wrong_body = b'{"code":200,"message":"ERROR","message":"ACK"}'
                observation.raw_body = wrong_body
                observation.stored_bytes = len(wrong_body)
                observation.body_sha256 = sha256(wrong_body).hexdigest()
            elif invalid in {"conclusion", "conflicting_conclusion"}:
                conclusion = await session.scalar(
                    select(IntegrationRecord).where(IntegrationRecord.parent_record_id == ack.id)
                )
                if invalid == "conclusion":
                    await session.delete(conclusion)
                else:
                    conclusion.ecs_attempt_outcome = "REJECTED"
        else:
            root.capture_origin = "BACKFILL"
            root.coverage = "LEGACY"
            version = sha256(b"archived command source").hexdigest()
            request = await session.get(IntegrationRecord, root.request_record_id)
            request.capture_origin = "BACKFILL"
            request.source_kind = "ecs_command:request"
            request.source_key = root.command_code
            request.source_version = version
            root.ack_record_id = await history.write_record(
                session,
                draft=history.RecordDraft(
                    exchange_id=root.id,
                    record_kind="HTTP_RESPONSE_OBSERVED",
                    event_key="BACKFILL:ACK",
                    direction="INBOUND",
                    capture_origin="BACKFILL",
                    http_status=200 if invalid == "status" else None,
                    raw_body=wire,
                    source_kind="ecs_command:ack",
                    source_key=root.command_code,
                    source_version=version,
                ),
            )
            if invalid != "source":
                await history.write_record(
                    session,
                    draft=history.RecordDraft(
                        exchange_id=root.id,
                        record_kind="LEGACY_SNAPSHOT",
                        event_key="SOURCE:COMMAND",
                        direction="INTERNAL",
                        capture_origin="BACKFILL",
                        source_kind="ecs_command",
                        source_key=root.command_code,
                        source_version=version,
                    ),
                )
            if invalid != "reference":
                await history.write_reference(
                    session,
                    exchange_id=root.id,
                    reference_kind="LEGACY_SOURCE",
                    namespace="ecs_command",
                    value=root.command_code,
                    relation_role="ORIGINAL_SOURCE",
                    created_at=datetime.now(UTC),
                )
        original_ack = root.ack_record_id

    url = state_migration.engine.url.render_as_string(hide_password=False)
    result = await run_alembic(url, "-x", "fence_legacy_writes=true", "upgrade", "head")
    if invalid:
        assert result.returncode != 0
        assert "ECS ACK" in result.stdout + result.stderr
        async with factory() as session:
            assert await session.scalar(text("SELECT version_num FROM alembic_version")) == (
                "http_exchange_runtime_expand"
            )
        return
    assert result.returncode == 0, result.stdout + result.stderr
    async with factory() as session:
        stored = await session.get(EcsCommand, command.command_code)
        assert (stored.ack_record_id, stored.ack_wire) == (original_ack, wire)
        if shape == "live":
            evidence = await session.get(EcsSendEvidence, (command.command_code, claim.lease_id, 1))
            assert (evidence.outcome, evidence.http_status, evidence.response_wire) == (
                "ACK",
                200,
                wire,
            )
        else:
            assert not await session.scalar(
                select(EcsSendEvidence).where(EcsSendEvidence.observation_id == original_ack)
            )


@pytest.mark.parametrize(
    "state_migration",
    [("TEST_HISTORY_MIGRATION_DATABASE_URL", "postgresql", "http_exchange_runtime_expand")],
    indirect=True,
)
@pytest.mark.parametrize(
    "proof",
    [
        "live",
        "archived",
        "missing",
        "wrong_command",
        "wrong_exchange",
        "wrong_digest",
        "wrong_record",
        "missing_after",
        "duplicate",
        "conflicting",
        "missing_receipt",
        "wrong_receipt_attempt",
    ],
)
async def test_ecs_result_reset_cutover_requires_unique_audited_evidence(
    state_migration, proof, monkeypatch
):
    from app.models import AuditLog, AuditLogArchive, EcsCommand, EcsResultEvidence

    factory = async_sessionmaker(state_migration.engine, expire_on_commit=False)
    service, store, adapter, targets = ecs(factory)
    command, _ = await command_at_gate(factory, service, store, adapter, targets, "reset")
    raw = result_wire(command)
    async with factory.begin() as session:
        original = await receive_ecs_result(session, raw, service)
        assert (original.status_code, original.pure_replay) == (200, False)
        root = await session.get(IntegrationExchange, original.exchange_id)
        fact = await session.scalar(
            select(IntegrationRecord).where(
                IntegrationRecord.exchange_id == root.id, IntegrationRecord.applied.is_(True)
            )
        )
        root.applied_result_id = None
        root.status = "HELD"
        root.hold_reason = "OPERATOR_SUPERSEDED"
        root.blocked = True
        details = {
            "command_code": "different" if proof == "wrong_command" else command.command_code,
            "exchange_id": str(uuid7() if proof == "wrong_exchange" else root.id),
            "retained_result_record_id": str(uuid7() if proof == "wrong_record" else fact.id),
            "before": {
                "applied_result_id": "f" * 64 if proof == "wrong_digest" else original.digest
            },
            "after": {} if proof == "missing_after" else {"applied_result_id": None},
        }
        audit = dict(
            id=121,
            action="ops:ecs-result:reset",
            resource="ecs-command",
            detail=details,
            trace_id="result-reset-test",
            created_at=datetime.now(UTC),
        )
        if proof == "archived":
            session.add(AuditLogArchive(**audit))
        elif proof != "missing":
            session.add(AuditLog(**audit))
        if proof in {"duplicate", "conflicting"}:
            if proof == "conflicting":
                audit = {**audit, "id": 122, "detail": {**details, "before": {}}}
            session.add(AuditLogArchive(**audit))
        if proof in {"missing_receipt", "wrong_receipt_attempt"}:
            receipt = await session.scalar(
                select(IntegrationExchange).where(
                    IntegrationExchange.receipt_id == original.receipt_id
                )
            )
            if proof == "missing_receipt":
                receipt.receipt_active = False
            else:
                response = await session.get(IntegrationRecord, receipt.current_response_record_id)
                response.attempt_id = str(uuid7())

    url = state_migration.engine.url.render_as_string(hide_password=False)
    result = await run_alembic(url, "-x", "fence_legacy_writes=true", "upgrade", "head")
    if proof not in {"live", "archived"}:
        assert result.returncode != 0
        assert "ECS result reset" in result.stdout + result.stderr
        async with factory() as session:
            assert await session.scalar(text("SELECT version_num FROM alembic_version")) == (
                "http_exchange_runtime_expand"
            )
        return

    assert result.returncode == 0, result.stdout + result.stderr
    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", True)
    await require_reconciled(factory)
    async with factory.begin() as session:
        stored = await session.get(EcsCommand, command.command_code)
        evidence = await session.get(EcsResultEvidence, (command.command_code, original.digest))
        receipt = await session.get(CallbackReceipt, original.receipt_id)
        assert (stored.applied_result_id, stored.status, stored.blocked) == (None, "HELD", True)
        assert (evidence.applied, evidence.receipt_id, evidence.response_wire) == (
            True,
            original.receipt_id,
            original.prepared_body,
        )
        assert (receipt.active, receipt.status_code, receipt.reply_wire) == (
            True,
            200,
            original.prepared_body,
        )
        replay = await receive_ecs_result(session, raw, service)
        assert (replay.pure_replay, replay.receipt_id, replay.prepared_body) == (
            True,
            original.receipt_id,
            original.prepared_body,
        )
