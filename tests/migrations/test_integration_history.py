from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

import pytest
from sqlalchemy import MetaData, Table, Uuid, inspect, select, text
from sqlalchemy.exc import IntegrityError

from tests.helpers import run_alembic

pytestmark = pytest.mark.parametrize(
    "state_migration",
    [
        ("TEST_HISTORY_MIGRATION_DATABASE_URL", "sqlite", "coarserackstage3"),
        ("TEST_HISTORY_MIGRATION_DATABASE_URL", "postgres", "coarserackstage3"),
    ],
    indirect=True,
)

NOW = datetime(2026, 11, 7, tzinfo=UTC)


async def table(connection, name):
    schema = "enterprise_backend" if connection.dialect.name == "postgresql" else None
    reflected = await connection.run_sync(
        lambda c: Table(name, MetaData(), schema=schema, autoload_with=c)
    )
    if name in {
        "integration_exchange",
        "integration_exchange_summary",
        "integration_record_summary",
    }:
        reflected.c.id.type = Uuid()
    if name == "integration_record_summary":
        reflected.c.exchange_id.type = Uuid()
    return reflected


@pytest.fixture(autouse=True)
async def clean_owned_migration_rows(state_migration):
    yield
    async with state_migration.engine.begin() as connection:
        schema = "enterprise_backend" if connection.dialect.name == "postgresql" else None
        tables = await connection.run_sync(lambda c: inspect(c).get_table_names(schema=schema))
        if "integration_exchange" in tables:
            await connection.execute(text("DELETE FROM transport_task"))
            await connection.execute(text("DELETE FROM reliable_message"))
            await connection.execute(
                text(
                    "UPDATE integration_exchange SET request_record_id=NULL,ack_record_id=NULL,"
                    "current_response_record_id=NULL"
                )
            )
            await connection.execute(text("DELETE FROM integration_reference"))
            await connection.execute(text("DELETE FROM integration_record"))
        for name in (
            "reliable_message",
            "integration_exchange",
            "ecs_send_evidence",
            "ecs_result_evidence",
            "ecs_command",
            "callback_receipt",
        ):
            if name in tables:
                await connection.execute(text(f"DELETE FROM {name}"))
        if "other_history_system" in tables:
            await connection.execute(text("DROP TABLE other_history_system"))


async def seed_receipt(connection, event_id):
    receipt = await table(connection, "callback_receipt")
    await connection.execute(
        receipt.insert().values(
            id=event_id,
            operation="ecs.event",
            operation_id=f"original-{event_id}",
            raw_digest="a" * 64,
            compare_mode="RAW",
            status_code=200,
            body={"code": 0},
            resp_timestamp=1,
            created_at=NOW,
        )
    )


def exchange_values(identity, *, role="RECEIPT", active=True):
    return dict(
        id=UUID(int=identity),
        schema_version=1,
        role=role,
        peer_system="ECS",
        protocol="ecs-event" if role == "RECEIPT" else "ecs-command",
        direction="INBOUND" if role == "RECEIPT" else "OUTBOUND",
        interaction_kind="EVENT" if role == "RECEIPT" else "COMMAND",
        identity_kind="EVENT_DIGEST" if role == "RECEIPT" else "COMMAND_CODE",
        identity_value="original",
        operation="ecs.event" if role == "RECEIPT" else None,
        operation_id="original" if role == "RECEIPT" else None,
        receipt_id=identity if role == "RECEIPT" else None,
        receipt_active=active if role == "RECEIPT" else None,
        raw_digest="a" * 64 if role == "RECEIPT" else None,
        compare_mode="RAW" if role == "RECEIPT" else None,
        command_code="00000000-0000-7000-8000-000000000001" if role == "ECS_COMMAND" else None,
        owner_domain="test" if role == "ECS_COMMAND" else None,
        call_key="original" if role == "ECS_COMMAND" else None,
        call_key_digest="b" * 64 if role == "ECS_COMMAND" else None,
        input_raw=b"original" if role == "ECS_COMMAND" else None,
        status="QUEUED" if role == "ECS_COMMAND" else None,
        delivery_status="PENDING" if role == "ECS_COMMAND" else None,
        created_at=NOW,
        recorded_at=NOW,
        capture_origin="BACKFILL",
        coverage="LEGACY",
    )


async def test_receipt_sequence_survives_deleted_highwater(state_migration):
    state = state_migration
    async with state.engine.begin() as connection:
        await seed_receipt(connection, 7)
        if state.backend == "postgres":
            await connection.execute(
                text("SELECT setval(pg_get_serial_sequence('callback_receipt','id'), 500)")
            )
    await state.migrate("upgrade", "integrationhistoryexpand")
    async with state.engine.begin() as connection:
        if state.backend == "postgres":
            next_id = await connection.scalar(text("SELECT nextval('integration_receipt_id_seq')"))
        else:
            await connection.execute(
                text("INSERT INTO _integration_receipt_sequence (id) VALUES (500)")
            )
            await connection.execute(text("DELETE FROM _integration_receipt_sequence"))
    async with state.engine.begin() as connection:
        if state.backend == "sqlite":
            next_id = (
                await connection.execute(
                    text("INSERT INTO _integration_receipt_sequence DEFAULT VALUES")
                )
            ).lastrowid
        assert next_id > 500
        assert await connection.scalar(text("SELECT id FROM callback_receipt")) == 7


async def test_role_uniqueness_keeps_command_and_receipt_lifetimes_separate(state_migration):
    state = state_migration
    await state.migrate("upgrade", "integrationhistoryexpand")
    async with state.engine.begin() as connection:
        exchange = await table(connection, "integration_exchange")
        await connection.execute(exchange.insert().values(**exchange_values(1)))
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(exchange.insert().values(**exchange_values(2)))
        await connection.execute(exchange.update().values(receipt_active=False))
        await connection.execute(exchange.insert().values(**exchange_values(2)))
        await connection.execute(exchange.insert().values(**exchange_values(3, role="ECS_COMMAND")))
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                duplicate = exchange_values(4, role="ECS_COMMAND")
                duplicate["command_code"] = "00000000-0000-7000-8000-000000000002"
                await connection.execute(exchange.insert().values(**duplicate))
        assert len(list(await connection.scalars(select(exchange.c.id)))) == 3


async def test_expand_preserves_original_rows_and_foreign_schema(state_migration):
    state = state_migration
    async with state.engine.begin() as connection:
        await seed_receipt(connection, 9)
        command = await table(connection, "ecs_command")
        await connection.execute(
            command.insert().values(
                command_code="00000000-0000-7000-8000-000000000009",
                owner_domain="original",
                call_key="original",
                call_key_digest="a" * 64,
                input_raw=b"original input",
                device_code="original-device",
                task_type="MOVE",
                route={"original": True},
                request_raw=b"original frozen bytes",
                target="ecs",
                delivery_snapshot={"original": True},
                expires_at=NOW,
                status="RECONCILING",
                delivery_status="HELD",
                blocked=False,
                needs_reconciliation=True,
                created_at=NOW,
            )
        )
        await connection.execute(text("CREATE TABLE other_history_system (payload text)"))
        await connection.execute(text("INSERT INTO other_history_system VALUES ('keep me')"))
    await state.migrate("upgrade", "integrationhistoryexpand")
    async with state.engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT payload FROM other_history_system")) == "keep me"
        )
        assert await connection.scalar(text("SELECT id FROM callback_receipt")) == 9
        assert (
            await connection.scalar(text("SELECT request_raw FROM ecs_command"))
            == b"original frozen bytes"
        )
        assert await connection.scalar(text("SELECT count(*) FROM integration_exchange")) == 0
    await state.migrate("downgrade", "coarserackstage3")
    async with state.engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT payload FROM other_history_system")) == "keep me"
        )
        assert await connection.scalar(text("SELECT id FROM callback_receipt")) == 9


async def test_authority_refuses_unmapped_runtime_without_dropping_original(state_migration):
    state = state_migration
    await state.migrate("upgrade", "integrationhistoryexpand")
    async with state.engine.begin() as connection:
        message = await table(connection, "reliable_message")
        await connection.execute(
            message.insert().values(
                target="original",
                operation="opaque",
                operation_id="original",
                request_raw=b"original bytes",
                status="PENDING",
                attempts=0,
                next_attempt_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    result = await run_alembic(
        state.engine.url.render_as_string(hide_password=False),
        "upgrade",
        "integrationhistoryauthority",
    )
    assert result.returncode != 0
    assert "unmapped original identities" in result.stdout + result.stderr
    async with state.engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT request_raw FROM reliable_message"))
            == b"original bytes"
        )
        assert (
            await connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "integrationhistoryexpand"
        )


async def test_sql_summaries_keep_evidence_dimensions_without_bodies(state_migration):
    await state_migration.migrate("upgrade", "integrationhistoryexpand")
    async with state_migration.engine.begin() as c:
        root = await table(c, "integration_exchange")
        await c.execute(root.insert().values(**exchange_values(901, role="ECS_COMMAND")))
        schema = "enterprise_backend" if c.dialect.name == "postgresql" else None
        views = await c.run_sync(lambda sync: inspect(sync).get_view_names(schema=schema))
        assert {"integration_exchange_summary", "integration_record_summary"} <= set(views)
        summary = await table(c, "integration_exchange_summary")
        rows = (await c.execute(select(summary))).mappings().all()
        assert len(rows) == 1
        assert rows[0]["delivery_state"] == "PENDING"
        assert rows[0]["application_state"] == "QUEUED"
        assert rows[0]["coverage"] == "LEGACY"
        for name in ("integration_exchange_summary", "integration_record_summary"):
            view = await table(c, name)
            assert not {
                "raw_body",
                "route",
                "input_raw",
                "effective_payload",
                "delivery_snapshot",
                "details",
            } & set(view.c.keys())


@pytest.fixture
async def contract_evidence(tmp_path):
    import json

    path = tmp_path / "contract-evidence.json"
    path.write_text(
        json.dumps(
            {
                **{
                    name: "DRAINED"
                    for name in (
                        "api",
                        "sender",
                        "worker",
                        "beat",
                        "background",
                        "one_off",
                        "manual",
                        "database_transactions",
                    )
                },
                "attestation": "isolated-contract-drill",
                "authority_acceptance": "task-9-software-only",
            }
        )
    )
    return path


async def prepare_contract(state):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from scripts.backfill_integration_history import (
        SOURCES,
        WriterEvidence,
        backfill_batch,
        reconcile,
    )
    from tests.migration_helpers import seed_command_message

    await state.migrate("upgrade", "integrationhistoryexpand")
    factory = async_sessionmaker(state.engine, expire_on_commit=False)
    code, digest = await seed_command_message(factory)
    async with state.engine.begin() as c:
        await seed_receipt(c, 501)
        receipt = await table(c, "callback_receipt")
        await c.execute(
            receipt.update()
            .where(receipt.c.id == 501)
            .values(
                operation="ecs.event",
                device_code="D1",
                event_envelope={
                    "device_code": "D1",
                    "event_type": "SCAN_COMPLETED",
                    "timestamp": 1,
                    "data": {"bin_code": "B1"},
                },
                event_context={
                    "owner_domain": "coarse_sorting",
                    "work_line_id": 1,
                    "device_id": 1,
                    "device_role": "SCAN1",
                    "device_version": 1,
                    "work_line_version": 1,
                    "component_key": None,
                },
                application_ref={
                    "subject_kind": "BIN_PASSAGE",
                    "subject_id": "original",
                    "canonical_event_id": 501,
                },
            )
        )
    async with factory.begin() as session:
        for source in SOURCES:
            assert (
                await backfill_batch(session, source=source, after=None, limit=500)
            ).conflicts == 0
        evidence = WriterEvidence.from_dict(
            {
                **{
                    name: "DRAINED"
                    for name in (
                        "api",
                        "sender",
                        "worker",
                        "beat",
                        "background",
                        "one_off",
                        "manual",
                        "database_transactions",
                    )
                },
                "attestation": "isolated-final-verify",
            }
        )
        assert (await reconcile(session, phase="final", writer_evidence=evidence)).eligible
    await state.migrate("upgrade", "integrationhistoryauthority")
    return factory, code, digest


async def contract_migrate(state, evidence):
    url = state.engine.url.render_as_string(hide_password=False)
    return await run_alembic(
        url,
        "-x",
        f"integration_history_contract_evidence={evidence}",
        "upgrade",
        "integrationhistorycontract",
    )


@pytest.mark.parametrize(
    "damage",
    [
        "unmapped",
        "bytes",
        "input",
        "writer",
        "external_fk",
        "diagnostic_authority",
        "missing_source",
    ],
)
async def test_contract_refuses_unmigrated_or_inconsistent_evidence(
    state_migration, contract_evidence, damage
):
    import json

    from app.models import IntegrationExchange, IntegrationRecord

    state = state_migration
    factory, code, _ = await prepare_contract(state)
    async with factory.begin() as session:
        root = await session.scalar(
            select(IntegrationExchange).where(IntegrationExchange.command_code == code)
        )
        if damage == "unmapped":
            receipt = await table(await session.connection(), "callback_receipt")
            await session.execute(
                receipt.insert().values(
                    id=502,
                    operation="late",
                    operation_id="late",
                    raw_digest="a" * 64,
                    compare_mode="RAW",
                    status_code=200,
                    body={},
                    resp_timestamp=1,
                    created_at=NOW,
                )
            )
        elif damage == "bytes":
            record = await session.get(IntegrationRecord, root.request_record_id)
            record.raw_body = b"changed-but-same-size"
            record.stored_bytes = len(record.raw_body)
            record.body_sha256 = sha256(record.raw_body).hexdigest()
        elif damage == "missing_source":
            await session.execute(text("DELETE FROM ecs_command"))
        elif damage == "diagnostic_authority":
            record = await session.get(IntegrationRecord, root.request_record_id)
            record.evidence_class = "DIAGNOSTIC"
        elif damage == "input":
            root.input_raw = b"changed-input"
        elif damage == "external_fk":
            await session.execute(
                text(
                    "CREATE TABLE unexpected_history_reader (id integer PRIMARY KEY, "
                    "receipt_id integer REFERENCES callback_receipt(id))"
                )
            )
    if damage == "writer":
        value = json.loads(contract_evidence.read_text())
        value["sender"] = "UNKNOWN"
        contract_evidence.write_text(json.dumps(value))
    refused = await contract_migrate(state, contract_evidence)
    assert refused.returncode != 0
    assert "refusing integration history contraction" in refused.stdout + refused.stderr
    async with state.engine.begin() as c:
        assert (
            await c.scalar(text("SELECT version_num FROM alembic_version"))
            == "integrationhistoryauthority"
        )
        assert await c.scalar(text("SELECT count(*) FROM ecs_command")) == (
            0 if damage == "missing_source" else 1
        )
        if damage == "external_fk":
            await c.execute(text("DROP TABLE unexpected_history_reader"))


async def test_contract_keeps_numeric_event_reads_and_long_result_replay(
    state_migration, contract_evidence
):
    import json
    from types import SimpleNamespace

    from app.integrations import history
    from app.integrations.callback_receipts import (
        find_receipt,
        insert_receipt,
        save_receipt_response,
    )
    from app.integrations.ecs_command.result import receive_ecs_result
    from app.integrations.ecs_command.results import get_command
    from app.models import IntegrationExchange

    state = state_migration
    factory, code, digest = await prepare_contract(state)
    async with factory.begin() as session:
        root = await session.scalar(
            select(IntegrationExchange).where(IntegrationExchange.command_code == code)
        )
        # Legitimate A-side progress must not be compared to the retired mutable command cache.
        root.ack_record_id = await history.write_record(
            session,
            draft=history.RecordDraft(
                exchange_id=root.id,
                record_kind="HTTP_RESPONSE_OBSERVED",
                event_key="live-ack",
                direction="INBOUND",
                raw_body=b"new-ack",
            ),
        )
        await save_receipt_response(
            session,
            receipt_id=501,
            status_code=200,
            prepared_body=b'{"code":"RECHECKED"}',
            resp_timestamp=2,
            attempt_id="live-recheck",
            received_at=NOW,
        )
        new_id = await insert_receipt(
            session,
            operation="live@v1",
            operation_id="live",
            raw_digest="b" * 64,
            compare_mode="RAW",
            status_code=200,
            body={"code": "LIVE"},
            resp_timestamp=2,
            created_at=NOW,
            raw_body=b"live-input",
        )
        assert new_id > 501
    result = await contract_migrate(state, contract_evidence)
    assert result.returncode == 0, result.stdout + result.stderr
    async with factory() as session:
        assert (await find_receipt(session, "live@v1", "live")).id == new_id
        row = await session.scalar(
            select(IntegrationExchange).where(IntegrationExchange.receipt_id == 501)
        )
        old = await find_receipt(session, row.operation, row.operation_id)
        assert old.id == 501 and old.body == {"code": "RECHECKED"}
        from app.integrations.ecs_callback.results import get_event

        event = await get_event(session, event_id=501)
        assert event.availability == "FOUND" and event.record.event_id == 501
        assert event.record.application_ref.canonical_event_id == 501
        view = await get_command(session, owner_domain="coarse_sorting", command_code=code)
        assert view.ack_received and view.status == "SUCCEEDED"
        core = view.result
    async with factory.begin() as session:
        reply = await receive_ecs_result(
            session, json.dumps(core).encode(), SimpleNamespace(annexes={})
        )
        assert reply.status_code == 200 and reply.body == {"code": 200, "message": "ACK"}
    async with state.engine.connect() as c:
        schema = "enterprise_backend" if c.dialect.name == "postgresql" else None
        names = await c.run_sync(lambda sync: inspect(sync).get_table_names(schema=schema))
        assert not {
            "ecs_command",
            "ecs_send_evidence",
            "ecs_result_evidence",
            "callback_receipt",
            "callback_snapshot",
        } & set(names)
        message = await table(c, "reliable_message")
        assert not {"request_raw", "response_raw", "last_response_raw"} & set(message.c.keys())
        assert await c.scalar(text("SELECT count(*) FROM integration_exchange_summary")) >= 3
    refused = await run_alembic(
        state.engine.url.render_as_string(hide_password=False),
        "downgrade",
        "integrationhistoryauthority",
    )
    assert refused.returncode != 0
    async with factory() as session:
        assert (await find_receipt(session, "live@v1", "live")).id == new_id


async def test_contract_maintenance_fence_covers_new_authoritative_writers(
    state_migration, contract_evidence
):
    from sqlalchemy.exc import DBAPIError

    from migrations.integration_history_contract_v2 import maintenance_gate

    if state_migration.backend != "postgres":
        pytest.skip("PostgreSQL maintenance table-lock contract")
    factory, _, _ = await prepare_contract(state_migration)
    async with state_migration.engine.begin() as connection:
        await connection.run_sync(
            lambda bind: maintenance_gate(bind, contract_evidence, populated=True)
        )
        with pytest.raises(DBAPIError):
            async with factory.begin() as writer:
                await writer.execute(text("SET LOCAL lock_timeout='100ms'"))
                await writer.execute(text("UPDATE integration_exchange SET coverage='PARTIAL'"))
    async with state_migration.engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM integration_exchange WHERE coverage='PARTIAL'")
            )
            == 0
        )


@pytest.mark.parametrize("headers_only", [False, True])
async def test_contract_preserves_live_unavailable_response_observation(
    state_migration, contract_evidence, headers_only
):
    import asyncio

    from app.http_client import DeliveryState, FailureKind, HttpResult
    from app.integrations import history
    from app.models import IntegrationRecord, ReliableMessage
    from app.reliable import MessageDraft, Outbox
    from app.reliable.config import TargetConfig
    from app.reliable.sender import Sender
    from app.reliable.store import MessageStore
    from app.reliable.testing import ScriptedHttpClient
    from tests.helpers import OpaqueHistoryAdapter

    state = state_migration
    factory, _, _ = await prepare_contract(state)
    target = TargetConfig("live-attempt", "http://example.test", "opaque")
    adapters = {"opaque": OpaqueHistoryAdapter()}
    outbox = Outbox({target.name: target}, adapters, records=history)
    async with factory.begin() as session:
        handle = await outbox.enqueue(
            session, MessageDraft(target.name, "opaque", "no-response", b"original-wire")
        )
    peer = ScriptedHttpClient(
        HttpResult(
            DeliveryState.DELIVERY_UNKNOWN if headers_only else DeliveryState.NOT_SENT,
            FailureKind.READ_TIMEOUT if headers_only else FailureKind.CONNECT_ERROR,
            503 if headers_only else None,
            (),
            None,
        )
    )
    sender = Sender(
        MessageStore(factory, records=history), outbox.targets, adapters, {target.name: peer}
    )
    try:
        assert await sender.tick() == 1
        await asyncio.wait_for(asyncio.gather(*tuple(sender.inflight)), 10)
    finally:
        await sender.aclose()
    async with factory() as session:
        original = await session.get(ReliableMessage, handle.message_id)
        assert original.status == "PENDING"
        observation_id = original.last_response_record_id
        observation = await session.get(IntegrationRecord, observation_id)
        assert observation.record_kind == "SEND_ATTEMPT_FINISHED"
        assert observation.body_state == "UNAVAILABLE" and observation.raw_body is None
        attempts, uncertain = original.attempts, original.delivery_uncertain
    result = await contract_migrate(state, contract_evidence)
    assert result.returncode == 0, result.stdout + result.stderr
    async with factory() as session:
        original = await session.get(ReliableMessage, handle.message_id)
        assert original.status == "PENDING" and original.attempts == attempts
        assert original.delivery_uncertain == uncertain
        assert original.last_response_record_id == observation_id
        body = (await history.read_bodies(session, record_ids=(observation_id,)))[observation_id]
        assert body.body_state == "UNAVAILABLE" and body.raw_body is None


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("damaged_payload", [False, True])
async def test_contract_checks_live_transport_adopted_payload(
    state_migration, contract_evidence, pending, damaged_payload
):
    import json

    from app.core.identifiers import uuid7
    from app.integrations import history
    from app.integrations.wms_callback import receive
    from app.integrations.wms_transport import (
        ADAPTERS,
        PROTOCOL,
        BinMove,
        HandoffPosition,
        RackBinSlot,
        RackPosition,
        TransportCaller,
        TransportService,
    )
    from app.models import IntegrationRecord, TransportTask
    from app.reliable import Outbox
    from app.reliable.config import TargetConfig

    state = state_migration
    factory, _, _ = await prepare_contract(state)
    target = TargetConfig("live-transport", "http://example.test", PROTOCOL)
    outbox = Outbox({target.name: target}, ADAPTERS, records=history)
    service = TransportService(outbox, target=target.name)
    async with factory.begin() as session:
        if pending:
            handle = await service.move_bins(
                session,
                client_request_id=str(uuid7()),
                caller=TransportCaller("line"),
                moves=[BinMove("C1", HandoffPosition("FROM"), RackBinSlot("R1", "90", "S1"))],
            )
        else:
            handle = await service.move_rack(
                session,
                client_request_id=str(uuid7()),
                caller=TransportCaller("line"),
                rack_id="R1",
                source=RackPosition("FROM"),
                target=RackPosition("TO"),
                target_face="90",
            )
    position = {"kind": "RACK_BIN_SLOT", "rack_id": "R1", "rack_face": "90", "slot_id": "S1"}
    outcome = dict(transport_task_id=handle.transport_task_id, outcome_revision=1)
    outcome.update(
        dict(
            kind="BIN_MOVE",
            results=[dict(container_id="C1", status="SUCCEEDED", final_position=position)],
        )
        if pending
        else dict(
            kind="RACK_MOVE",
            rack_id="R1",
            status="SUCCEEDED",
            final_position={"kind": "RACK_POSITION", "location_code": "TO"},
        )
    )

    async def callback(operation, data):
        async with factory.begin() as session:
            response = await receive(
                session,
                json.dumps(
                    dict(operation=operation, operation_id=str(uuid7()), timestamp=1, data=data)
                ).encode(),
            )
            assert response.status_code == 202

    await callback("transport.task.resulted@v1", outcome)
    if pending:
        async with factory() as session:
            task = await session.scalar(
                select(TransportTask).where(
                    TransportTask.transport_task_id == handle.transport_task_id
                )
            )
            assert task.applied_outcome_revision is None and task.pending_outcome
        await callback(
            "transport.task.member_position_changed@v1",
            dict(
                transport_task_id=handle.transport_task_id,
                container_id="C1",
                milestone="TARGET_PLACED",
                final_position=position,
            ),
        )
    async with factory() as session:
        task = await session.scalar(
            select(TransportTask).where(TransportTask.transport_task_id == handle.transport_task_id)
        )
        assert task.applied_outcome_revision == 1
        adopted = task.applied_outcome
        if not pending:
            assert adopted["arrival_face"] == "90"
    if damaged_payload:
        async with factory.begin() as session:
            fact = await session.scalar(
                select(IntegrationRecord).where(
                    IntegrationRecord.source_key == handle.transport_task_id,
                    IntegrationRecord.record_kind == "RESULT_APPLIED",
                )
            )
            fact.effective_payload = {"different": "adopted-fact"}
    result = await contract_migrate(state, contract_evidence)
    if damaged_payload:
        assert result.returncode != 0
        async with factory() as session:
            task = await session.scalar(
                select(TransportTask).where(
                    TransportTask.transport_task_id == handle.transport_task_id
                )
            )
            assert task.applied_outcome == adopted and task.applied_outcome_revision == 1
        return
    assert result.returncode == 0, result.stdout + result.stderr
    async with factory() as session:
        fact = await session.scalar(
            select(IntegrationRecord).where(
                IntegrationRecord.source_key == handle.transport_task_id,
                IntegrationRecord.record_kind == "RESULT_APPLIED",
            )
        )
        assert fact.effective_payload == adopted
        assert fact.outcome_revision == 1


async def test_delivered_wms_summary_keeps_original_response_source(state_migration, monkeypatch):
    import asyncio
    import json

    from app.core.identifiers import uuid7
    from app.http_client import DeliveryState, HttpResult
    from app.integrations import history
    from app.integrations.wms_request import (
        ADAPTERS,
        FACTS_PROTOCOL,
        Endpoint,
        RequestPayload,
        WmsRequests,
        request_operation,
    )
    from app.integrations.wms_request import operation as registry
    from app.models import IntegrationExchange, ReliableMessage
    from app.reliable import Outbox
    from app.reliable.config import TargetConfig
    from app.reliable.sender import Sender
    from app.reliable.store import MessageStore
    from app.reliable.testing import ScriptedHttpClient
    from app.services.integration_history_service import get_exchange

    state = state_migration
    factory, code, _ = await prepare_contract(state)
    monkeypatch.setattr(registry, "_registry", dict(registry._registry))

    class Report(RequestPayload):
        quantity: int

    request_operation("history.source.fact@v1", Endpoint.FACTS)(Report)
    target = TargetConfig("history-facts", "http://example.test", FACTS_PROTOCOL)
    outbox = Outbox({target.name: target}, ADAPTERS, records=history)
    identity = str(uuid7())
    async with factory.begin() as session:
        handle = await WmsRequests(outbox).send(session, Report(quantity=1), operation_id=identity)
    raw = json.dumps(dict(operation_id=identity, code="RECORDED", timestamp=1, data={})).encode()
    peer = ScriptedHttpClient(HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 200, (), raw))
    sender = Sender(
        MessageStore(factory, records=history), outbox.targets, ADAPTERS, {target.name: peer}
    )
    try:
        assert await sender.tick() == 1
        await asyncio.wait_for(asyncio.gather(*tuple(sender.inflight)), 10)
    finally:
        await sender.aclose()
    async with factory() as session:
        message = await session.get(ReliableMessage, handle.message_id)
        assert message.status == "DONE" and message.last_response_record_id is None
        source = message.terminal_response_record_id
        assert source is not None
        api = await get_exchange(session, message.exchange_id)
        assert api["protocol_result"]["source_record_id"] == source
        sql = await table(await session.connection(), "integration_exchange_summary")
        value = await session.scalar(
            select(sql.c.protocol_source_record_id).where(sql.c.id == message.exchange_id)
        )
        assert str(value).replace("-", "") == source.hex
        assert (await history.read_bodies(session, record_ids=(source,)))[source].raw_body == raw

    async with factory.begin() as session:
        command = await session.scalar(
            select(IntegrationExchange).where(IntegrationExchange.command_code == code)
        )
        acknowledgement = await history.write_record(
            session,
            draft=history.RecordDraft(
                exchange_id=command.id,
                record_kind="HTTP_RESPONSE_OBSERVED",
                event_key="late-ack-source",
                direction="INBOUND",
                raw_body=b"late-original-ack",
            ),
        )
        command.ack_record_id = acknowledgement
        command.current_response_record_id = None
        message = await session.scalar(
            select(ReliableMessage).where(ReliableMessage.exchange_id == command.id)
        )
        message.last_response_record_id = message.terminal_response_record_id = None
        command_id = command.id
    async with factory() as session:
        assert (await get_exchange(session, command_id))["protocol_result"][
            "source_record_id"
        ] == acknowledgement
        sql = await table(await session.connection(), "integration_exchange_summary")
        sql_source = await session.scalar(
            select(sql.c.protocol_source_record_id).where(sql.c.id == command_id)
        )
        assert str(sql_source).replace("-", "") == acknowledgement.hex


async def test_sql_and_api_share_canonical_body_descriptors(state_migration):
    from app.integrations import history
    from app.models import IntegrationExchange, IntegrationRecord
    from app.schemas.integration_history import RecordListParams
    from app.services.integration_history_service import list_records

    state = state_migration
    factory, _, _ = await prepare_contract(state)
    async with factory.begin() as session:
        root = IntegrationExchange(
            role="DIAGNOSTIC",
            protocol="opaque",
            direction="INBOUND",
            interaction_kind="CALLBACK",
            identity_kind="DIAGNOSTIC",
            identity_value="descriptors",
            created_at=NOW,
        )
        session.add(root)
        await session.flush()
        canonical = await history.write_record(
            session,
            draft=history.RecordDraft(
                exchange_id=root.id,
                record_kind="CALLBACK_RECEIVED",
                evidence_class="DIAGNOSTIC",
                event_key="original",
                direction="INBOUND",
                raw_body=b"recorded-bytes",
            ),
        )
        await history.write_record(
            session,
            draft=history.RecordDraft(
                exchange_id=root.id,
                record_kind="RESPONSE_PREPARED",
                evidence_class="DIAGNOSTIC",
                event_key="borrowed",
                direction="OUTBOUND",
                body_record_id=canonical,
            ),
        )
        root_id = root.id

    async def compare():
        async with factory() as session:
            api = {
                item["id"]: item
                for item in (await list_records(session, root_id, RecordListParams()))["items"]
            }
            view = await table(await session.connection(), "integration_record_summary")
            rows = (
                (await session.execute(select(view).where(view.c.exchange_id == root_id)))
                .mappings()
                .all()
            )
            for row in rows:
                identity = UUID(str(row["id"]))
                assert (row["body_state"], row["body_sha256"], row["stored_bytes"]) == tuple(
                    api[identity][name] for name in ("body_state", "body_sha256", "stored_bytes")
                )

    await compare()
    # Historical tombstones remain readable even if an old borrowed descriptor still exists.
    async with factory.begin() as session:
        original = await session.get(IntegrationRecord, canonical)
        original.raw_body, original.body_state = None, "PURGED"
    await compare()
