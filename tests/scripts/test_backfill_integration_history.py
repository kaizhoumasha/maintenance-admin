from datetime import UTC, datetime
from hashlib import sha256

import pytest
from sqlalchemy import delete, inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database import SCHEMA
from app.models import IntegrationExchange, IntegrationRecord, IntegrationReference
from tests.migration_helpers import seed_command_message, seed_transport_identity_case, source_table

NOW = datetime(2026, 11, 7, tzinfo=UTC)
pytestmark = pytest.mark.parametrize(
    "state_migration",
    [
        ("TEST_HISTORY_MIGRATION_DATABASE_URL", backend, "integrationhistoryexpand")
        for backend in ("sqlite", "postgres")
    ],
    indirect=True,
)


@pytest.fixture
async def legacy(state_migration):
    engine = state_migration.engine
    async with engine.begin() as c:
        receipt = await source_table(c, "callback_receipt")
        await c.execute(
            receipt.insert().values(
                id=41,
                operation="event@v1",
                operation_id="original",
                raw_digest="a" * 64,
                compare_mode="RAW",
                status_code=202,
                body={"code": "ACCEPTED"},
                resp_timestamp=7,
                created_at=NOW,
                application_ref={
                    "business_code": "coarse_sorting",
                    "record_kind": "SCAN",
                    "record_id": "original",
                },
            )
        )
    yield async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as c:
        schema = SCHEMA if c.dialect.name == "postgresql" else None
        tables = await c.run_sync(lambda bind: inspect(bind).get_table_names(schema=schema))
        preparations = await source_table(c, "coarse_sorting_rack_preparation")
        actions = await source_table(c, "coarse_sorting_action")
        await c.execute(preparations.update().values(current_action_id=None))
        await c.execute(
            actions.update().values(input_preparation_id=None, result_preparation_id=None)
        )
        await c.execute(actions.delete().where(actions.c.preparation_id.is_not(None)))
        await c.execute(preparations.delete())
        for name in (
            "coarse_sorting_action",
            "coarse_sorting_job",
            "transport_task",
            "reliable_message",
            "ecs_send_evidence",
            "ecs_result_evidence",
            "ecs_command",
            "callback_receipt",
            "callback_snapshot",
        ):
            if name in tables:
                t = await source_table(c, name)
                await c.execute(delete(t))
        await c.execute(
            text(
                "UPDATE integration_exchange SET request_record_id=NULL, "
                "ack_record_id=NULL, current_response_record_id=NULL"
            )
        )
        await c.execute(text("DELETE FROM integration_reference"))
        await c.execute(text("DELETE FROM integration_record"))
        await c.execute(text("DELETE FROM integration_exchange"))


async def test_backfill_resume_is_idempotent_without_sending(legacy, monkeypatch):
    import socket

    from scripts.backfill_integration_history import backfill_batch

    calls = []
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: calls.append(a))
    async with legacy.begin() as s:
        first = await backfill_batch(s, source="callback_receipt", after=None, limit=1)
    # Simulates commit followed by loss of the checkpoint: repeat the committed batch.
    async with legacy.begin() as s:
        second = await backfill_batch(s, source="callback_receipt", after=None, limit=1)
        rows = list(await s.scalars(select(IntegrationExchange)))
        assert first.copied == 1 and second.copied == 0 and second.reused == 1
        assert len(rows) == 1 and rows[0].receipt_id == 41
        assert rows[0].created_at.replace(tzinfo=UTC) == NOW
        assert not calls
        reply = await s.get(IntegrationRecord, rows[0].current_response_record_id)
        assert reply.body_state == "LEGACY_REENCODED"
        assert reply.raw_body == b'{"code":"ACCEPTED"}'


async def test_final_reconcile_rereads_changed_old_rows(legacy):
    from scripts.backfill_integration_history import backfill_batch, reconcile

    async with legacy.begin() as s:
        await backfill_batch(s, source="callback_receipt", after=None, limit=10)
    async with legacy.begin() as s:
        t = await source_table(await s.connection(), "callback_receipt")
        await s.execute(t.update().values(status_code=200, body={"code": "APPLIED"}))
    async with legacy.begin() as s:
        report = await reconcile(s, phase="final", writer_evidence=None)
        root = await s.scalar(
            select(IntegrationExchange).where(IntegrationExchange.receipt_id == 41)
        )
        reply = await s.get(IntegrationRecord, root.current_response_record_id)
        assert root.status_code == 200 and reply.raw_body == b'{"code":"APPLIED"}'
        assert not report.eligible
        assert report.writer_status in (
            {"UNKNOWN", "RUNNING"} if s.bind.dialect.name == "postgresql" else {"UNKNOWN"}
        )
        assert report.identity_mismatches == report.byte_mismatches == 0


@pytest.mark.parametrize("status", ["UNKNOWN", "RUNNING"])
async def test_remaining_writer_refuses_authority_switch(legacy, status):
    from scripts.backfill_integration_history import WriterEvidence, reconcile

    evidence = WriterEvidence.from_dict(
        {
            "api": "DRAINED",
            "sender": status,
            "worker": "DRAINED",
            "beat": "DRAINED",
            "background": "DRAINED",
            "one_off": "DRAINED",
            "manual": "DRAINED",
            "database_transactions": "DRAINED",
            "attestation": "operator-check-001",
        }
    )
    async with legacy.begin() as s:
        report = await reconcile(s, phase="final", writer_evidence=evidence)
        assert not report.eligible
        assert report.writer_status in (
            {status, "RUNNING"} if s.bind.dialect.name == "postgresql" else {status}
        )


async def test_legacy_bytes_and_unknown_links_keep_their_limits(legacy):
    from scripts.backfill_integration_history import backfill_batch, reconcile

    async with legacy.begin() as s:
        t = await source_table(await s.connection(), "callback_snapshot")
        await s.execute(
            t.insert(),
            [
                dict(
                    id=i,
                    request_id="same-trace",
                    received_at=NOW,
                    body_b64=body,
                    truncated=False,
                    http_status=200,
                    response_body={"ok": True},
                    outcome="received",
                    source=None,
                )
                for i, body in ((1, "eA=="), (2, "!!!"))
            ],
        )
        batch = await backfill_batch(s, source="callback_snapshot", after=None, limit=10)
        assert batch.conflicts == 1
        roots = list(
            await s.scalars(
                select(IntegrationExchange).where(IntegrationExchange.role == "DIAGNOSTIC")
            )
        )
        assert len(roots) == 2 and all(r.peer_system == "WMS" for r in roots)
        bodies = list(
            await s.scalars(
                select(IntegrationRecord).where(
                    IntegrationRecord.record_kind == "CALLBACK_RECEIVED"
                )
            )
        )
        assert {r.body_state for r in bodies} == {"PREVIEW", "UNAVAILABLE"}
        preview = next(r for r in bodies if r.raw_body is not None)
        assert preview.raw_body == b"x" and preview.body_sha256 == sha256(b"x").hexdigest()
        assert not list(
            await s.scalars(
                select(IntegrationReference).where(
                    IntegrationReference.reference_kind == "EXCHANGE"
                )
            )
        )
        report = await reconcile(s, phase="precopy", writer_evidence=None)
        assert not report.eligible and report.byte_mismatches >= 1


@pytest.mark.parametrize(
    "conclusion,response_raw",
    [("NOT_RECEIVED", b"reject"), ("NOT_RECEIVED", None), ("UNKNOWN", None)],
)
async def test_command_result_and_per_lease_conclusion_survive_copy(
    legacy, state_migration, conclusion, response_raw
):
    from app.integrations.ecs_command.lifecycle import get_attempt_conclusions
    from scripts.backfill_integration_history import (
        SOURCES,
        backfill_batch,
        reconcile,
    )

    command_code, digest = await seed_command_message(legacy)
    from scripts.backfill_integration_history import source_table as old_table

    async with legacy.begin() as session:
        sends = await old_table(session, "ecs_send_evidence")
        await session.execute(
            sends.update()
            .where(sends.c.command_code == command_code)
            .values(outcome=conclusion, response_raw=response_raw)
        )
        for source in SOURCES:
            batch = await backfill_batch(session, source=source, after=None, limit=500)
            assert batch.conflicts == 0
        root = await session.scalar(
            select(IntegrationExchange).where(IntegrationExchange.command_code == command_code)
        )
        assert root.input_raw == b"original-input" and root.applied_result_id == digest
        assert root.expires_at.replace(tzinfo=UTC) == NOW
        assert await get_attempt_conclusions(session, exchange_id=root.id) == {
            "original-lease": conclusion
        }
        result = await session.scalar(
            select(IntegrationRecord).where(IntegrationRecord.semantic_digest == digest)
        )
        assert result.applied and result.body_state == "LEGACY_REENCODED"
        # Copy integrity does not require quiescence; the final writer gate is tested separately.
        report = await reconcile(session, phase="precopy", writer_evidence=None)
        assert report.identity_mismatches == report.byte_mismatches == report.missing_refs == 0
        assert not report.eligible
    await state_migration.migrate("upgrade", "integrationhistoryauthority")
    from app.integrations.ecs_command.results import get_command

    async with legacy() as session:
        view = await get_command(session, owner_domain="coarse_sorting", command_code=command_code)
        assert view.status == "SUCCEEDED" and view.result["result"] == "SUCCESS"
        import json
        from types import SimpleNamespace

        from app.integrations.ecs_command.result import receive_ecs_result

        replay = await receive_ecs_result(
            session, json.dumps(view.result).encode(), SimpleNamespace(annexes={})
        )
        assert replay.status_code == 200 and replay.pure_replay
        assert replay.prepared_body == b'{"code":200,"message":"ACK"}'


async def test_command_and_outbox_byte_conflict_blocks_switch(legacy):
    from scripts.backfill_integration_history import WriterEvidence, reconcile

    await seed_command_message(legacy, mismatch=True)
    async with legacy.begin() as session:
        evidence = WriterEvidence.from_dict(
            {
                **{
                    k: "DRAINED"
                    for k in (
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
                "attestation": "operator-001",
            }
        )
        report = await reconcile(session, phase="final", writer_evidence=evidence)
        assert not report.eligible and report.byte_mismatches > 0
        message = await source_table(await session.connection(), "reliable_message")
        assert await session.scalar(select(message.c.request_raw)) == b"different-wire"
        assert await session.scalar(select(message.c.exchange_id)) is None


async def test_final_handoff_preserves_allocated_highwater_and_json_event_refs(legacy):
    from scripts.backfill_integration_history import reconcile

    async with legacy.begin() as session:
        t = await source_table(await session.connection(), "callback_receipt")
        await session.execute(t.update().values(event_context={"canonical_event_id": 9001}))
        if session.bind.dialect.name == "postgresql":
            await session.execute(text("SELECT setval('callback_receipt_id_seq', 8000)"))
    async with legacy.begin() as session:
        report = await reconcile(session, phase="final", writer_evidence=None)
        assert report.sequence_highwater >= 9001
        if session.bind.dialect.name == "postgresql":
            allocated = await session.scalar(text("SELECT nextval('integration_receipt_id_seq')"))
        else:
            allocated = (
                await session.execute(
                    text("INSERT INTO _integration_receipt_sequence DEFAULT VALUES")
                )
            ).lastrowid
        assert allocated > 9001


async def test_other_database_transaction_blocks_attested_final(legacy):
    from scripts.backfill_integration_history import WRITERS, WriterEvidence, reconcile

    if legacy.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL transaction inventory")
    async with legacy() as other, other.begin():
        await other.execute(text("SELECT 1"))
        async with legacy.begin() as session:
            report = await reconcile(
                session,
                phase="final",
                writer_evidence=WriterEvidence.from_dict(
                    {**{k: "DRAINED" for k in WRITERS}, "attestation": "operator-001"}
                ),
            )
            assert report.writer_status == "RUNNING" and not report.eligible


async def test_transport_acceptance_copy_keeps_legacy_source_and_public_basis(legacy):
    from dataclasses import asdict
    from types import SimpleNamespace

    from app.core.identifiers import uuid7
    from app.core.strict_json import canonical_digest
    from app.http_client import DeliveryState, HttpResult
    from app.integrations.wms_transport.acceptance import _basis, get_submission_acceptance
    from app.integrations.wms_transport.types import RackPosition, TransportCaller
    from app.integrations.wms_transport.wire import (
        freeze_request,
        input_digest,
        move_rack_data,
        timestamp_ms,
    )
    from scripts.backfill_integration_history import backfill_batch

    frozen = move_rack_data("R", RackPosition("A"), RackPosition("B"))
    operation, task_id, client = (str(uuid7()) for _ in range(3))
    caller = TransportCaller("line", None)
    task = dict(
        id=101,
        client_request_id=client,
        transport_task_id=task_id,
        operation_id=operation,
        kind="RACK_MOVE",
        input_digest=input_digest("move_rack", caller, frozen),
        workline_id="line",
        station_id=None,
        owner_domain="coarse_sorting",
        created_at=NOW,
        cycle_id=None,
        frozen=frozen,
    )
    raw = freeze_request(operation, task_id, timestamp_ms(NOW), frozen)
    response = (
        '{"operation_id":"'
        + operation
        + '","code":"RECEIVED","timestamp":100,"data":{"transport_task_id":"'
        + task_id
        + '"}}'
    ).encode()
    message = SimpleNamespace(
        target="wms",
        operation="transport.task.submit@v1",
        operation_id=operation,
        business_key=task_id,
        request_raw=raw,
    )
    basis = _basis(
        task,
        message,
        71,
        HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 202, (), response),
        attempt_no=1,
        lease_id="original-lease",
        saved_at=100,
    )
    snapshot = asdict(basis)
    snapshot["evidence_sha256"] = canonical_digest(snapshot)
    async with legacy.begin() as session:
        t = await source_table(await session.connection(), "transport_task")
        await session.execute(t.insert().values(**task, submission_acceptance=snapshot))
        result = await backfill_batch(session, source="transport_task", after=None, limit=500)
        assert result.conflicts == 0
        assert await session.scalar(select(t.c.submission_acceptance)) == snapshot
        view = await get_submission_acceptance(
            session,
            transport_task_id=task_id,
            client_request_id=client,
            caller=caller,
            owner="coarse_sorting",
        )
        assert view.state == "ACCEPTED" and asdict(view.basis) == asdict(basis)
    from scripts.backfill_integration_history import WRITERS, WriterEvidence, reconcile

    async with legacy.begin() as session:
        t = await source_table(await session.connection(), "transport_task")
        await session.execute(
            t.update().values(submission_acceptance={**snapshot, "evidence_sha256": "0" * 64})
        )
    async with legacy.begin() as session:
        report = await reconcile(
            session,
            phase="final",
            writer_evidence=WriterEvidence.from_dict(
                {**{k: "DRAINED" for k in WRITERS}, "attestation": "operator-001"}
            ),
        )
        assert not report.eligible and report.byte_mismatches > 0


async def test_authority_rejects_changed_frozen_input_even_with_existing_mapping(
    legacy, state_migration
):
    from scripts.backfill_integration_history import reconcile
    from tests.helpers import run_alembic

    await seed_command_message(legacy)
    async with legacy.begin() as session:
        await reconcile(session, phase="final", writer_evidence=None)
    async with legacy.begin() as session:
        await session.execute(
            text("UPDATE integration_exchange SET input_raw=:raw WHERE role='ECS_COMMAND'"),
            {"raw": b"corrupt"},
        )
    result = await run_alembic(
        state_migration.engine.url.render_as_string(hide_password=False),
        "upgrade",
        "integrationhistoryauthority",
    )
    assert result.returncode != 0 and "original" in result.stdout + result.stderr


async def test_authority_rejects_changed_source_before_old_cursor(legacy, state_migration):
    from scripts.backfill_integration_history import reconcile
    from tests.helpers import run_alembic

    async with legacy.begin() as session:
        await reconcile(session, phase="final", writer_evidence=None)
    async with legacy.begin() as session:
        t = await source_table(await session.connection(), "callback_receipt")
        await session.execute(t.update().values(body={"code": "CHANGED_AFTER_VERIFY"}))
    result = await run_alembic(
        state_migration.engine.url.render_as_string(hide_password=False),
        "upgrade",
        "integrationhistoryauthority",
    )
    assert result.returncode != 0 and "original" in result.stdout + result.stderr


async def test_missing_original_applied_result_cannot_become_authoritative(legacy):
    from scripts.backfill_integration_history import WRITERS, WriterEvidence, reconcile

    await seed_command_message(legacy)
    async with legacy.begin() as session:
        await session.execute(text("DELETE FROM ecs_result_evidence"))
        report = await reconcile(
            session,
            phase="final",
            writer_evidence=WriterEvidence.from_dict(
                {**{k: "DRAINED" for k in WRITERS}, "attestation": "operator-001"}
            ),
        )
        assert not report.eligible


async def test_cli_resume_keeps_safe_progress_and_requires_final_inventory(
    legacy, state_migration, tmp_path
):
    import asyncio
    import json
    import os
    import sys

    from scripts.backfill_integration_history import WRITERS

    state = tmp_path / "progress.json"
    env = {
        **os.environ,
        "DATABASE_URL": state_migration.engine.url.render_as_string(hide_password=False),
    }

    async def invoke(*args):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "scripts.backfill_integration_history",
            *args,
            "--state",
            str(state),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
        return process.returncode, stdout.decode(), stderr.decode()

    for _ in range(2):
        code, output, _ = await invoke("copy", "--source", "callback_receipt", "--batch-size", "1")
        assert code == 0, output
    checkpoint = state.read_text()
    assert (
        "ACCEPTED" not in checkpoint
        and "password" not in checkpoint
        and "database" not in checkpoint
    )
    code, output, _ = await invoke("verify", "--phase", "final")
    assert code == 2 and json.loads(output.splitlines()[-1])["writer_status"] == "UNKNOWN"
    proof = tmp_path / "writers.json"
    proof.write_text(
        json.dumps({**{k: "DRAINED" for k in WRITERS}, "attestation": "operator-check-001"})
    )
    code, output, _ = await invoke("verify", "--phase", "final", "--writer-evidence", str(proof))
    assert code == 0 and json.loads(output.splitlines()[-1])["eligible"] is True, output
    await state_migration.migrate("upgrade", "integrationhistoryauthority")


async def test_legacy_consumption_uses_original_saved_result_identity(legacy):
    from scripts.backfill_integration_history import SOURCES, backfill_batch

    command_code, digest = await seed_command_message(legacy)
    async with legacy.begin() as session:
        job = await source_table(await session.connection(), "coarse_sorting_job")
        await session.execute(
            job.insert().values(
                id="job-1",
                event_digest="a" * 64,
                receipt_id=41,
                work_line_id=1,
                workline_code="line",
                device_id=1,
                device_code="D1",
                device_role="SCAN",
                device_version=1,
                work_line_version=1,
                binding_revision="1",
                timestamp=1,
                payload={},
                received_at=NOW,
                pending=True,
                wait_reason="AWAITING_VALIDATION",
            )
        )
        original_result = await source_table(await session.connection(), "ecs_result_evidence")
        core = await session.scalar(select(original_result.c.core))
        action = await source_table(await session.connection(), "coarse_sorting_action")
        await session.execute(
            action.insert().values(
                id="action-1",
                job_id="job-1",
                kind="MOVE_FORWARD",
                round_no=1,
                request_data={},
                resource_snapshot={},
                command_owner="coarse_sorting",
                call_key="original-call",
                command_code=command_code,
                device_code="D1",
                created_at=NOW,
                last_consumed_at=NOW,
                evidence_history=[
                    {
                        "command_code": command_code,
                        "digest": digest,
                        "data": core,
                        "consumed_at": NOW.isoformat(),
                    }
                ],
            )
        )
        for source in SOURCES:
            assert (
                await backfill_batch(session, source=source, after=None, limit=500)
            ).conflicts == 0
        result = await session.scalar(
            select(IntegrationRecord).where(IntegrationRecord.semantic_digest == digest)
        )
        consumed = await session.scalar(
            select(IntegrationRecord).where(IntegrationRecord.record_kind == "BUSINESS_CONSUMED")
        )
        assert consumed.parent_record_id == result.id
        assert consumed.occurred_at.replace(tzinfo=UTC) == NOW
        assert consumed.capture_origin == "BACKFILL"


async def test_changed_pending_transport_fact_does_not_duplicate_original_adopted_revision(legacy):
    from scripts.backfill_integration_history import backfill_batch

    async with legacy.begin() as session:
        t = await source_table(await session.connection(), "transport_task")
        await session.execute(
            t.insert().values(
                id=101,
                client_request_id="client",
                transport_task_id="transport",
                operation_id="operation",
                kind="RACK_MOVE",
                input_digest="b" * 64,
                workline_id="line",
                created_at=NOW,
                frozen={"kind": "RACK_MOVE"},
                applied_outcome_revision=1,
                applied_outcome={"original": "fact"},
                outcome_status="SUCCEEDED",
            )
        )
        assert (
            await backfill_batch(session, source="transport_task", after=None, limit=500)
        ).conflicts == 0
    async with legacy.begin() as session:
        t = await source_table(await session.connection(), "transport_task")
        await session.execute(t.update().values(pending_outcome={"revision": 2}))
        assert (
            await backfill_batch(session, source="transport_task", after=None, limit=500)
        ).conflicts == 0
        records = list(
            await session.scalars(
                select(IntegrationRecord).where(
                    IntegrationRecord.record_kind == "RESULT_APPLIED",
                    IntegrationRecord.outcome_revision == 1,
                )
            )
        )
        assert len(records) == 1


async def test_copy_never_overwrites_a_concurrently_bound_live_instance(legacy, monkeypatch):
    from scripts import backfill_integration_history as history

    if legacy.kw["bind"].dialect.name != "postgresql":
        pytest.skip("independent PostgreSQL transactions")
    async with legacy.begin() as session:
        root = IntegrationExchange(
            role="OUTGOING",
            protocol="wms-facts",
            peer_system="WMS",
            direction="OUTBOUND",
            interaction_kind="REQUEST",
            identity_kind="OPERATION_ID",
            identity_value="operation",
            operation="op",
            operation_id="operation",
            created_at=NOW,
        )
        session.add(root)
        await session.flush()
        live_id = root.id
        t = await source_table(await session.connection(), "reliable_message")
        await session.execute(
            t.insert().values(
                id=72,
                target="wms",
                operation="op",
                operation_id="operation",
                request_raw=b"wire",
                status="PENDING",
                attempts=0,
                next_attempt_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    original = history._guarded_bind

    async def race(session, table, row, values, **kwargs):
        async with legacy.begin() as other:
            await other.execute(
                text("UPDATE reliable_message SET exchange_id=:root WHERE id=72"), {"root": live_id}
            )
        return await original(session, table, row, values, **kwargs)

    monkeypatch.setattr(history, "_guarded_bind", race)
    async with legacy.begin() as session:
        result = await history.backfill_batch(
            session, source="reliable_message", after=None, limit=500
        )
        assert result.conflicts == 1
        t = await source_table(await session.connection(), "reliable_message")
        assert str(await session.scalar(select(t.c.exchange_id))) == str(live_id)


async def test_ecs_outbox_source_requires_its_original_command_mapping(legacy):
    from scripts.backfill_integration_history import backfill_batch

    await seed_command_message(legacy)
    async with legacy.begin() as session:
        result = await backfill_batch(session, source="reliable_message", after=None, limit=500)
        assert result.conflicts == 1
        assert not list(
            await session.scalars(
                select(IntegrationExchange).where(IntegrationExchange.role == "OUTGOING")
            )
        )


async def test_backfill_cannot_restore_old_response_after_new_authority(legacy, state_migration):
    from app.integrations.history import RecordDraft, write_record
    from scripts.backfill_integration_history import backfill_batch, reconcile

    async with legacy.begin() as session:
        await reconcile(session, phase="final", writer_evidence=None)
    await state_migration.migrate("upgrade", "integrationhistoryauthority")
    async with legacy.begin() as session:
        root = await session.scalar(
            select(IntegrationExchange).where(IntegrationExchange.receipt_id == 41)
        )
        live = await write_record(
            session,
            draft=RecordDraft(
                exchange_id=root.id,
                record_kind="RESPONSE_PREPARED",
                event_key="new-authority",
                direction="OUTBOUND",
                raw_body=b"new-authoritative-reply",
            ),
        )
        root.current_response_record_id, root.status_code = live, 200
    async with legacy.begin() as session:
        with pytest.raises(ValueError, match="expand"):
            await backfill_batch(session, source="callback_receipt", after=None, limit=500)
        root = await session.scalar(
            select(IntegrationExchange).where(IntegrationExchange.receipt_id == 41)
        )
        assert root.current_response_record_id == live and root.status_code == 200


async def test_cleanup_preserves_uncut_sources_and_only_removes_verified_old_copies(
    legacy, state_migration, monkeypatch
):
    from datetime import timedelta

    from app.businesses.smt_auto import retention as auto_retention
    from app.tasks.callback_tasks import run_cleanup, run_history_cleanup, run_receipt_cleanup
    from app.tasks.reliable_tasks import run_cleanup as reliable_cleanup
    from scripts.backfill_integration_history import SOURCES, backfill_batch

    async def no_auto_references(*args, **kwargs):
        return ()

    # This fixture stops at the historical receipt cutover, before the current SMT task schema.
    monkeypatch.setattr(auto_retention, "wms_message_references", no_auto_references)
    old = NOW - timedelta(days=100)
    async with legacy.begin() as session:
        connection = await session.connection()
        receipt = await source_table(connection, "callback_receipt")
        await session.execute(receipt.update().values(created_at=old))
        snapshot = await source_table(connection, "callback_snapshot")
        await session.execute(
            snapshot.insert().values(
                id=1,
                request_id="same-trace",
                received_at=old,
                body_b64="eA==",
                truncated=False,
                http_status=200,
                response_body={},
                outcome="received",
            )
        )
        message = await source_table(connection, "reliable_message")
        await session.execute(
            message.insert().values(
                id=72,
                target="wms",
                operation="op",
                operation_id="original-message",
                request_raw=b"wire",
                status="DONE",
                attempts=1,
                next_attempt_at=NOW,
                created_at=old,
                updated_at=old,
            )
        )
        for source in ("callback_receipt", "callback_snapshot"):
            await backfill_batch(session, source=source, after=None, limit=500)
    for cleanup, days in (
        (run_cleanup, 7),
        (run_receipt_cleanup, 90),
        (run_history_cleanup, 90),
        (reliable_cleanup, 90),
    ):
        assert await cleanup(legacy, days, now=NOW) == 0
    async with legacy.begin() as session:
        message = await source_table(await session.connection(), "reliable_message")
        assert await session.scalar(select(message.c.id)) == 72
        for source in SOURCES:
            assert (
                await backfill_batch(session, source=source, after=None, limit=500)
            ).conflicts == 0
    await state_migration.migrate("upgrade", "integrationhistoryauthority")
    async with legacy.begin() as session:
        snapshot = await source_table(await session.connection(), "callback_snapshot")
        await session.execute(snapshot.update().values(body_b64="eQ=="))
    assert await run_cleanup(legacy, 7, now=NOW) == 0
    async with legacy.begin() as session:
        snapshot = await source_table(await session.connection(), "callback_snapshot")
        assert await session.scalar(select(snapshot.c.id)) == 1
        await session.execute(snapshot.update().values(body_b64="eA=="))
    assert await run_cleanup(legacy, 7, now=NOW) == 1
    async with legacy() as session:
        snapshot = await source_table(await session.connection(), "callback_snapshot")
        assert await session.scalar(select(snapshot.c.id)) is None
    assert await run_receipt_cleanup(legacy, 90, now=NOW) == 1
    assert await reliable_cleanup(legacy, 90, now=NOW) == 1
    assert await run_history_cleanup(legacy, 90, now=NOW) == 3
    async with legacy() as session:
        receipt = await source_table(await session.connection(), "callback_receipt")
        assert await session.scalar(select(receipt.c.id)) is None


async def test_original_coarse_scan_without_an_action_keeps_its_proven_job_owner(
    legacy, state_migration
):
    from datetime import timedelta

    from app.tasks.callback_tasks import run_receipt_cleanup
    from scripts.backfill_integration_history import backfill_batch

    key = "00000000-0000-7000-8000-000000000041"
    async with legacy.begin() as session:
        connection = await session.connection()
        line = await source_table(connection, "work_lines")
        line_id = (
            await session.execute(
                line.insert()
                .values(
                    line_code="old-coarse", line_name="Original", business_code="coarse_sorting"
                )
                .returning(line.c.id)
            )
        ).scalar_one()
        receipt = await source_table(connection, "callback_receipt")
        await session.execute(
            receipt.update().values(
                operation="ecs.event", operation_id="e" * 64, created_at=NOW - timedelta(days=100)
            )
        )
        job = await source_table(connection, "coarse_sorting_job")
        await session.execute(
            job.insert().values(
                id=key,
                event_digest="e" * 64,
                receipt_id=41,
                work_line_id=line_id,
                workline_code="old-coarse",
                device_id=1,
                device_code="D",
                device_role="SCAN1",
                device_version=1,
                work_line_version=1,
                binding_revision="1",
                timestamp=1,
                payload={},
                received_at=NOW,
                pending=True,
                wait_reason="WAIT",
            )
        )
        await backfill_batch(session, source="callback_receipt", after=None, limit=500)
        refs = list(
            await session.scalars(
                select(IntegrationReference.reference_value).where(
                    IntegrationReference.reference_kind == "BUSINESS_TASK",
                    IntegrationReference.namespace == "coarse_sorting",
                )
            )
        )
        assert refs == [key]
    try:
        await state_migration.migrate("upgrade", "integrationhistoryauthority")
        assert await run_receipt_cleanup(legacy, 90, now=NOW) == 0
        async with legacy() as session:
            root = await session.scalar(
                select(IntegrationExchange).where(IntegrationExchange.receipt_id == 41)
            )
            assert root.receipt_active
    finally:
        async with legacy.begin() as session:
            job = await source_table(await session.connection(), "coarse_sorting_job")
            await session.execute(job.delete().where(job.c.id == key))
            line = await source_table(await session.connection(), "work_lines")
            await session.execute(line.delete().where(line.c.id == line_id))


async def test_migrated_pending_command_keeps_its_original_send_gate(
    legacy, state_migration, tmp_path
):
    import json
    from datetime import timedelta
    from types import SimpleNamespace

    from app.integrations import history
    from app.integrations.ecs_command.lifecycle import EcsLifecycle
    from app.integrations.ecs_command.routing import RouteAuthorization
    from app.reliable.config import TargetConfig
    from app.reliable.store import MessageStore
    from migrations.integration_history_contract_v2 import WRITERS
    from scripts.backfill_integration_history import SOURCES, backfill_batch
    from tests.helpers import run_alembic

    code, _ = await seed_command_message(legacy)
    snapshot = {"origin": "http://ecs.test", "protocol": "ecs-command", "unknown_policy": "HOLD"}
    async with legacy.begin() as session:
        await session.execute(text("DELETE FROM ecs_send_evidence"))
        await session.execute(text("DELETE FROM ecs_result_evidence"))
        command = await source_table(await session.connection(), "ecs_command")
        await session.execute(
            command.update().values(
                status="QUEUED",
                delivery_status="PENDING",
                applied_result_id=None,
                delivery_snapshot=snapshot,
                expires_at=NOW + timedelta(seconds=300),
            )
        )
        message = await source_table(await session.connection(), "reliable_message")
        await session.execute(
            message.update().values(
                status="PENDING",
                delivery_snapshot=snapshot,
                response_raw=None,
                expires_at=NOW + timedelta(seconds=300),
            )
        )
        for source in SOURCES:
            assert (
                await backfill_batch(session, source=source, after=None, limit=500)
            ).conflicts == 0
    # The current Sender expects the expanded schema even in legacy authority mode.
    evidence = tmp_path / "contract-evidence.json"
    evidence.write_text(
        json.dumps(
            dict.fromkeys(WRITERS, "DRAINED")
            | {"attestation": "isolated", "authority_acceptance": "isolated"}
        )
    )
    result = await run_alembic(
        state_migration.engine.url.render_as_string(hide_password=False),
        "-x",
        f"integration_history_contract_evidence={evidence}",
        "upgrade",
        "http_exchange_runtime_expand",
    )
    assert result.returncode == 0, result.stdout + result.stderr

    class Routes:
        async def authorize_frozen(self, session, route):
            return RouteAuthorization.ALLOW

    target = TargetConfig("ecs", None, "ecs-command", address_mode="SNAPSHOT")
    store = MessageStore(
        legacy,
        records=history,
        lifecycles={"ecs-command": EcsLifecycle(SimpleNamespace(routes=Routes()))},
        clock=lambda: NOW,
    )
    claims = await store.claim(1, {"ecs": target})
    assert len(claims) == 1 and claims[0].message.operation_id == code
    assert claims[0].message.request_raw == b"original-wire"
    assert await store.before_send(claims[0], target)


async def test_legacy_delivery_summary_survives_runtime_cleanup(legacy):
    from scripts.backfill_integration_history import SOURCES, backfill_batch

    await seed_command_message(legacy)
    async with legacy.begin() as session:
        for source in SOURCES:
            assert (
                await backfill_batch(session, source=source, after=None, limit=500)
            ).conflicts == 0
        source = await source_table(await session.connection(), "reliable_message")
        original = (await session.execute(select(source))).mappings().one()
        marker = await session.scalar(
            select(IntegrationRecord).where(
                IntegrationRecord.source_kind == "reliable_message",
                IntegrationRecord.source_key == str(original["id"]),
            )
        )
        await session.execute(delete(source))
        assert marker.details["attempt_count"] == original["attempts"]
        assert marker.details["recorded_delivery_state"] == original["status"]


async def test_backfilled_preparation_uses_the_live_exact_reference_query(legacy):
    from app.businesses.coarse_sorting.models import (
        CoarseSortingAction,
        CoarseSortingJob,
        RackPreparation,
    )
    from app.core.identifiers import uuid7
    from app.schemas.integration_history import ExchangeListParams
    from app.services.integration_history_service import list_exchanges
    from scripts.backfill_integration_history import SOURCES, backfill_batch

    key, preparation = str(uuid7()), str(uuid7())
    identities = {mid: str(uuid7()) for mid in (901, 902)}
    async with legacy.begin() as session:
        reliable = await source_table(await session.connection(), "reliable_message")
        for mid in (901, 902):
            await session.execute(
                reliable.insert().values(
                    id=mid,
                    target="original-decisions",
                    operation="legacy.operation",
                    operation_id=identities[mid],
                    request_raw=b"original-wire",
                    status="PENDING",
                    attempts=0,
                    next_attempt_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        session.add(
            CoarseSortingJob(
                id=key,
                event_digest=key,
                receipt_id=41,
                work_line_id=1,
                workline_code="L",
                device_id=1,
                device_code="D",
                device_role="SCAN1",
                device_version=1,
                work_line_version=1,
                binding_revision="1",
                timestamp=1,
                payload={},
                received_at=NOW,
                pending=True,
                wait_reason="WAIT",
            )
        )
        await session.flush()
        session.add(
            CoarseSortingAction(
                id=key,
                job_id=key,
                kind="ALLOCATE_STORAGE",
                round_no=1,
                rack_generation_no=1,
                operation="legacy.operation",
                operation_id=identities[901],
                message_id=901,
                request_data={},
                evidence_history=[],
                created_at=NOW,
            )
        )
        await session.flush()
        session.add(
            RackPreparation(
                id=preparation,
                work_line_id=1,
                workline_code="L",
                rack_position_code="P",
                rack_binding_version=1,
                generation_no=1,
                mode="DEPART_ONLY",
                old_rack_id="OLD",
                source_allocation_action_id=key,
                predecessor_action_ids=[],
                state="AWAITING_DEPARTURE",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await session.flush()
        session.add(
            CoarseSortingAction(
                id=str(uuid7()),
                preparation_id=preparation,
                kind="DEPARTURE_DECIDE",
                round_no=1,
                operation="legacy.operation",
                operation_id=identities[902],
                message_id=902,
                request_data={},
                evidence_history=[],
                created_at=NOW,
            )
        )
        await session.flush()
        for source in SOURCES:
            assert (
                await backfill_batch(session, source=source, after=None, limit=500)
            ).conflicts == 0
    async with legacy() as session:
        page = await list_exchanges(
            session,
            ExchangeListParams(
                reference_kind="BUSINESS_PREPARATION",
                reference_namespace="coarse_sorting",
                reference_value=preparation,
            ),
        )
        assert len(page["items"]) == 1 and page["items"][0]["capture_origin"] == "BACKFILL"


@pytest.mark.parametrize("original_present", [False, True])
async def test_transport_copy_requires_original_message_identity(legacy, original_present):
    from scripts.backfill_integration_history import backfill_batch

    messages = [(71, "other-peer", "other.operation@v1", "other-task")]
    if original_present:
        messages.append((72, "original-peer", "transport.task.submit@v1", "original-transport"))
    task = await seed_transport_identity_case(legacy, messages)
    async with legacy.begin() as session:
        await backfill_batch(session, source="reliable_message", after=None, limit=500)
        result = await backfill_batch(session, source="transport_task", after=None, limit=500)
        assert result.conflicts == 0
        table = await source_table(await session.connection(), "transport_task")
        table.c.submission_exchange_id.type = IntegrationExchange.__table__.c.id.type
        identity = await session.scalar(select(table.c.submission_exchange_id))
        root = await session.get(IntegrationExchange, identity)
        assert root.operation == "transport.task.submit@v1"
        assert root.operation_id == task["operation_id"]
        if original_present:
            assert root.target_key == "original-peer"
        else:
            assert root.identity_kind == "TRANSPORT_TASK_ID"
            assert root.identity_value == "original-transport"


async def test_transport_copy_refuses_ambiguous_targets(legacy):
    from scripts.backfill_integration_history import backfill_batch

    await seed_transport_identity_case(
        legacy,
        [
            (71, "first-peer", "transport.task.submit@v1", "original-transport"),
            (72, "second-peer", "transport.task.submit@v1", "original-transport"),
        ],
    )
    async with legacy.begin() as session:
        await backfill_batch(session, source="reliable_message", after=None, limit=500)
        result = await backfill_batch(session, source="transport_task", after=None, limit=500)
        assert result.conflicts == 1
        table = await source_table(await session.connection(), "transport_task")
        table.c.submission_exchange_id.type = IntegrationExchange.__table__.c.id.type
        assert await session.scalar(select(table.c.submission_exchange_id)) is None


@pytest.mark.parametrize("proof", ["authority", "contract"])
async def test_transport_identity_proof_rejects_wrong_original_association(legacy, proof):
    from alembic.util import CommandError
    from sqlalchemy import update

    from migrations.integration_history_contract_v2 import validate_contract
    from migrations.integration_history_v1 import validate_originals
    from scripts.backfill_integration_history import SOURCES, backfill_batch

    await seed_transport_identity_case(
        legacy, [(71, "other-peer", "other.operation@v1", "other-task")]
    )
    async with legacy.begin() as session:
        for source in SOURCES:
            assert (
                await backfill_batch(session, source=source, after=None, limit=500)
            ).conflicts == 0
        table = await source_table(await session.connection(), "transport_task")
        table.c.submission_exchange_id.type = IntegrationExchange.__table__.c.id.type
        bound = await session.scalar(select(table.c.submission_exchange_id))
        wrong = await session.scalar(
            select(IntegrationExchange.id).where(
                IntegrationExchange.operation == "other.operation@v1"
            )
        )
        await session.execute(
            update(IntegrationRecord)
            .where(IntegrationRecord.exchange_id == bound)
            .values(exchange_id=wrong)
        )
        await session.execute(
            update(IntegrationReference)
            .where(IntegrationReference.exchange_id == bound)
            .values(exchange_id=wrong)
        )
        await session.execute(table.update().values(submission_exchange_id=wrong))
    async with legacy.begin() as session:
        connection = await session.connection()
        with pytest.raises(CommandError):
            await connection.run_sync(
                validate_originals if proof == "authority" else validate_contract
            )


async def test_transport_copy_rechecks_existing_binding(legacy):
    from scripts.backfill_integration_history import backfill_batch

    await seed_transport_identity_case(
        legacy,
        [
            (71, "other-peer", "other.operation@v1", "other-task"),
            (72, "original-peer", "transport.task.submit@v1", "original-transport"),
        ],
    )
    async with legacy.begin() as session:
        await backfill_batch(session, source="reliable_message", after=None, limit=500)
        await backfill_batch(session, source="transport_task", after=None, limit=500)
        wrong = await session.scalar(
            select(IntegrationExchange.id).where(
                IntegrationExchange.operation == "other.operation@v1"
            )
        )
        table = await source_table(await session.connection(), "transport_task")
        table.c.submission_exchange_id.type = IntegrationExchange.__table__.c.id.type
        await session.execute(table.update().values(submission_exchange_id=wrong))
    async with legacy.begin() as session:
        assert (
            await backfill_batch(session, source="transport_task", after=None, limit=500)
        ).conflicts == 1


@pytest.mark.parametrize("damage", [None, "target", "message", "missing"])
async def test_transport_copy_checks_recorded_acceptance_identity(legacy, damage):
    import json
    from dataclasses import asdict
    from types import SimpleNamespace

    from app.core.strict_json import canonical_digest
    from app.http_client import DeliveryState, HttpResult
    from app.integrations.wms_transport.acceptance import _basis
    from app.integrations.wms_transport.wire import freeze_request
    from scripts.backfill_integration_history import backfill_batch

    task = await seed_transport_identity_case(
        legacy,
        [
            (71, "other-peer", "other.operation@v1", "other-task"),
            (72, "original-peer", "transport.task.submit@v1", "original-transport"),
            (73, "second-peer", "transport.task.submit@v1", "original-transport"),
        ],
    )
    raw = freeze_request(
        task["operation_id"], "original-transport", int(NOW.timestamp() * 1000), task["frozen"]
    )
    message = SimpleNamespace(
        target="original-peer",
        operation="transport.task.submit@v1",
        operation_id=task["operation_id"],
        business_key="original-transport",
        request_raw=raw,
    )
    ack = json.dumps(
        dict(
            operation_id=task["operation_id"],
            code="RECEIVED",
            timestamp=1,
            data=dict(transport_task_id="original-transport"),
        )
    ).encode()
    snapshot = asdict(
        _basis(
            task,
            message,
            72,
            HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 202, (), ack),
            attempt_no=1,
            lease_id="original-lease",
            saved_at=1,
        )
    )
    if damage == "target":
        snapshot["target"] = "unrecorded-peer"
    if damage == "message":
        snapshot["message_id"] = 71
    if damage == "missing":
        snapshot["message_id"] = None
    snapshot["evidence_sha256"] = canonical_digest(snapshot)
    async with legacy.begin() as session:
        table = await source_table(await session.connection(), "transport_task")
        await session.execute(table.update().values(submission_acceptance=snapshot))
        await backfill_batch(session, source="reliable_message", after=None, limit=500)
        result = await backfill_batch(session, source="transport_task", after=None, limit=500)
        assert result.conflicts == (0 if damage is None else 1)
        if damage is None:
            table.c.submission_exchange_id.type = IntegrationExchange.__table__.c.id.type
            root = await session.get(
                IntegrationExchange, await session.scalar(select(table.c.submission_exchange_id))
            )
            assert root.target_key == "original-peer"
            from migrations.integration_history_contract_v2 import validate_contract
            from migrations.integration_history_v1 import validate_originals

            await backfill_batch(session, source="callback_receipt", after=None, limit=500)
            connection = await session.connection()
            await connection.run_sync(validate_originals)
            messages = await source_table(connection, "reliable_message")
            await session.execute(messages.delete())
            await connection.run_sync(validate_contract)


async def test_contract_keeps_original_transport_after_message_cleanup(legacy):
    from migrations.integration_history_contract_v2 import validate_contract
    from migrations.integration_history_v1 import validate_originals
    from scripts.backfill_integration_history import SOURCES, backfill_batch

    await seed_transport_identity_case(
        legacy, [(72, "original-peer", "transport.task.submit@v1", "original-transport")]
    )
    async with legacy.begin() as session:
        for source in SOURCES:
            assert (
                await backfill_batch(session, source=source, after=None, limit=500)
            ).conflicts == 0
        connection = await session.connection()
        await connection.run_sync(validate_originals)
        messages = await source_table(connection, "reliable_message")
        await session.execute(messages.delete())
        await connection.run_sync(validate_contract)
