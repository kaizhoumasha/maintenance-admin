"""Offline schema replacement rejects old responsibility before destroying private history."""

from contextlib import AsyncExitStack
from datetime import UTC, datetime

import pytest
from sqlalchemy import MetaData, Table, inspect, select, text

from app.database import SCHEMA, create_database_engine
from tests.helpers import run_alembic
from tests.migration_helpers import isolated_schema_database


@pytest.fixture(params=["sqlite", "postgres"])
async def roller_migration(request, tmp_path):
    async with AsyncExitStack() as databases:
        if request.param == "sqlite":
            url = f"sqlite+aiosqlite:///{tmp_path / 'cutover.db'}"
        else:
            url, _ = await databases.enter_async_context(
                isolated_schema_database(request.getfixturevalue("pg_url"))
            )

        async def migrate(command, revision):
            result = await run_alembic(url, command, revision)
            return result

        result = await migrate("upgrade", "rollerfoundation")
        assert result.returncode == 0, result.stdout + result.stderr
        engine = create_database_engine(url)

        async def table(name):
            async with engine.connect() as connection:
                return await connection.run_sync(
                    lambda c: Table(
                        name,
                        MetaData(),
                        schema=None if c.dialect.name == "sqlite" else SCHEMA,
                        autoload_with=c,
                    )
                )

        try:
            yield engine, migrate, table
        finally:
            await engine.dispose()


def old_scan(identity, *, pending):
    return dict(
        id=identity,
        owner_domain="smt_manual",
        work_line_id=123,
        business_key=identity,
        first_digest="a" * 64,
        received_at=datetime.now(UTC),
        point="SCAN1",
        device_code="old-scan",
        raw_bin_code="NOREAD",
        bin_code=None,
        scanned_at=1,
        pending=pending,
    )


async def test_no_data_copy_and_unrelated_shared_messages_survive(roller_migration):
    engine, migrate, table = roller_migration
    scans, messages = await table("bin_roller_scan_arrival"), await table("reliable_message")
    async with engine.begin() as connection:
        await connection.execute(
            scans.insert().values(**old_scan("discardable-private-history", pending=False))
        )
        message_id = await connection.scalar(
            messages.insert()
            .values(
                target="other-business",
                operation="unrelated",
                operation_id="keep",
                request_raw=b"original",
                status="PENDING",
                attempts=0,
                next_attempt_at=datetime.now(UTC),
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            .returning(messages.c.id)
        )
    try:
        result = await migrate("upgrade", "rollerfour")
        assert result.returncode == 0, result.stdout + result.stderr
        async with engine.connect() as connection:
            names = await connection.run_sync(
                lambda c: inspect(c).get_table_names(
                    schema=None if c.dialect.name == "sqlite" else SCHEMA
                )
            )
            assert {n for n in names if n.startswith("bin_roller_")} == {
                "bin_roller_bin_passage",
                "bin_roller_bin_return",
                "bin_roller_face_work",
                "bin_roller_workline_state",
            }
            assert (
                await connection.scalar(
                    select(messages.c.request_raw).where(messages.c.id == message_id)
                )
                == b"original"
            )
        for name in (
            "bin_roller_bin_passage",
            "bin_roller_bin_return",
            "bin_roller_face_work",
            "bin_roller_workline_state",
        ):
            t = await table(name)
            async with engine.connect() as connection:
                assert await connection.scalar(select(t).limit(1)) is None
        result = await migrate("downgrade", "rollerfoundation")
        assert result.returncode == 0, result.stdout + result.stderr
        async with engine.connect() as connection:
            assert await connection.scalar(select(scans).limit(1)) is None
    finally:
        async with engine.begin() as connection:
            await connection.execute(messages.delete().where(messages.c.id == message_id))


async def test_active_old_scan_refuses_before_schema_mutation(roller_migration):
    engine, migrate, table = roller_migration
    scans = await table("bin_roller_scan_arrival")
    async with engine.begin() as connection:
        await connection.execute(scans.insert().values(**old_scan("old-active", pending=True)))
    try:
        refused = await migrate("upgrade", "rollerfour")
        assert (
            refused.returncode != 0 and "active responsibility" in refused.stdout + refused.stderr
        )
        async with engine.connect() as connection:
            assert (
                await connection.scalar(select(scans.c.id).where(scans.c.id == "old-active"))
                == "old-active"
            )
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "rollerfoundation"
            )
    finally:
        async with engine.begin() as connection:
            await connection.execute(scans.delete().where(scans.c.id == "old-active"))


async def test_new_checkpoint_refuses_downgrade_before_any_drop(roller_migration):
    engine, migrate, table = roller_migration
    result = await migrate("upgrade", "rollerfour")
    assert result.returncode == 0, result.stdout + result.stderr
    state = await table("bin_roller_workline_state")
    async with engine.begin() as connection:
        await connection.execute(
            state.insert().values(
                owner_domain="new",
                work_line_id=123,
                revision=0,
                updated_at=datetime.now(UTC),
                drain_phase="IDLE",
            )
        )
    try:
        refused = await migrate("downgrade", "rollerfoundation")
        assert refused.returncode != 0
        async with engine.connect() as connection:
            assert await connection.scalar(select(state.c.owner_domain)) == "new"
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "rollerfour"
            )
    finally:
        async with engine.begin() as connection:
            await connection.execute(state.delete().where(state.c.owner_domain == "new"))


@pytest.mark.parametrize(
    "kind", ["feed_segment", "return_batch", "return_opportunity", "decision", "face_work"]
)
async def test_each_old_pending_responsibility_refuses_cutover(roller_migration, kind):
    engine, migrate, table = roller_migration
    old = await table("bin_roller_" + kind)
    values = {
        "feed_segment": dict(
            feed_id="f",
            segment_index=1,
            members=[],
            client_request_id="c",
            status="PENDING",
            pending=True,
        ),
        "return_batch": dict(
            opportunity_id="o",
            source_ref="s",
            operation_id="op",
            members=[],
            workline_code="L",
            caller_workline_id="1",
            source={},
            status="PENDING",
            pending=True,
        ),
        "return_opportunity": dict(service_id="f", source_ref="s", kind="SEGMENT", ended=False),
        "decision": dict(
            kind="DRAIN",
            input_kind="NONE",
            input_ref="i",
            unused_racks=[],
            operation_id="op",
            status="PENDING",
            pending=True,
        ),
        "face_work": dict(
            purpose="SUPPLY",
            demand_id="pending",
            task_id="T",
            change_id="c",
            plan_revision=1,
            rack_id="R",
            rack_face="A",
            demand_state="ACTIVE",
            service_id="s",
            cycle_id="j",
            source_ref="pending",
            service_state="PENDING",
        ),
    }[kind]
    async with engine.begin() as connection:
        await connection.execute(
            old.insert().values(id="pending", owner_domain="smt_manual", work_line_id=1, **values)
        )
    try:
        result = await migrate("upgrade", "rollerfour")
        assert result.returncode != 0 and old.name in result.stderr
        async with engine.connect() as connection:
            assert await connection.scalar(select(old.c.id)) == "pending"
    finally:
        async with engine.begin() as connection:
            await connection.execute(old.delete())


@pytest.mark.parametrize(
    "responsibility,expected_table",
    [
        ("rack", "rack_dispatch_job"),
        ("task", "smt_manual_task"),
        ("admission", "smt_manual_bin"),
        ("command_queued", "ecs_command"),
        ("command_reconciliation", "ecs_command"),
        ("command_blocked", "ecs_command"),
        ("transport_missing", "transport_task"),
        ("transport_pending", "transport_task"),
        ("transport_unknown", "transport_task"),
        *(
            ("message_" + state, "reliable_message")
            for state in ("PENDING", "SENDING", "STALLED", "HELD")
        ),
    ],
)
async def test_shared_responsibility_alone_refuses_cutover_without_mutating_originals(
    roller_migration, responsibility, expected_table
):
    engine, migrate, table = roller_migration
    originals = []
    now = datetime.now(UTC)

    async def add(name, **values):
        original = await table(name)
        values = {key: value for key, value in values.items() if value is not None}
        async with engine.begin() as connection:
            identity = (
                await connection.execute(original.insert().values(**values))
            ).inserted_primary_key
            condition = [
                column == value
                for column, value in zip(original.primary_key, identity, strict=True)
            ]
            snapshot = dict(
                (await connection.execute(select(original).where(*condition))).mappings().one()
            )
        originals.append((original, condition, snapshot))

    scan = old_scan("inactive-original", pending=False)
    if responsibility.startswith(("command_", "message_")):
        scan["command_code"] = "original-command"
    await add("bin_roller_scan_arrival", **scan)
    if responsibility == "rack":
        await add(
            "rack_dispatch_job",
            id="original-job",
            owner_domain="smt_manual",
            work_line_id=123,
            source_kind="ROLLER_DRAIN",
            source_ref="original-drain",
            rack_id="R1",
            original_cycle_id="original-cycle",
            state="ACTIVE",
            pending=True,
            release_requested=False,
            facts_revision=0,
        )
    elif responsibility == "task":
        await add(
            "smt_manual_task",
            task_id="original-task",
            task_type="OUTBOUND",
            business_type="SMT",
            work_line_id=123,
            workline_code="L",
            issued_operation_id="original-issued",
            issued_timestamp=1,
            issued_data={"original": True},
            queue_revision=1,
            dispatch_sequence=1,
            status="EXECUTING",
            plan_revision=1,
            prepare_accepted=True,
            received_work=[],
        )
        await add(
            "smt_manual_task_member",
            id="original-member",
            task_id="original-task",
            plan_revision=1,
            kind="BIN",
            rack_id="R1",
            rack_face="A",
            slot_id="S1",
            member_order=1,
            status="PENDING",
        )
    elif responsibility == "admission":
        await add(
            "smt_manual_bin",
            arrival_id=scan["id"],
            work_line_id=123,
            device_code="old-scan",
            raw_bin_code="NOREAD",
            scanned_at=1,
            state="ADMISSION_PENDING",
            pending=True,
        )
    elif responsibility.startswith("command_"):
        await add(
            "ecs_command",
            command_code="original-command",
            owner_domain="smt_manual",
            call_key="original-call",
            call_key_digest="a" * 64,
            input_raw=b"original-input",
            device_code="old-scan",
            task_type="MOVE_RIGHT",
            route={},
            request_raw=b"original-command-bytes",
            target="ecs",
            delivery_snapshot={},
            expires_at=now,
            created_at=now,
            status="QUEUED" if responsibility == "command_queued" else "SUCCEEDED",
            delivery_status="DONE",
            blocked=responsibility == "command_blocked",
            needs_reconciliation=responsibility == "command_reconciliation",
        )
    elif responsibility.startswith("transport_"):
        await add(
            "bin_roller_return_batch",
            id="inactive-batch",
            owner_domain="smt_manual",
            work_line_id=123,
            opportunity_id="ended-window",
            source_ref="original-face",
            operation_id="old-allocation",
            members=[],
            workline_code="L",
            caller_workline_id="123",
            source={},
            client_request_id="original-client",
            status="COMPLETED",
            pending=False,
        )
        await add(
            "transport_task",
            client_request_id="original-client",
            transport_task_id="original-transport",
            operation_id="original-submit",
            kind="BIN_MOVE",
            input_digest="a" * 64,
            workline_id="123",
            owner_domain="smt_manual",
            created_at=now,
            frozen={"original": "frozen"},
            applied_outcome=None
            if responsibility == "transport_missing"
            else {"original": "result"},
            applied_outcome_revision=None if responsibility == "transport_missing" else 1,
            pending_outcome={"original": "pending"}
            if responsibility == "transport_pending"
            else None,
            outcome_status="UNKNOWN" if responsibility == "transport_unknown" else "SUCCEEDED",
        )
    else:
        state = responsibility.removeprefix("message_")
        await add(
            "reliable_message",
            target="ecs",
            operation="ecs.command",
            operation_id="original-command",
            business_key="original-command",
            request_raw=b"original-message-bytes",
            status=state,
            attempts=1,
            next_attempt_at=None if state in ("STALLED", "HELD") else now,
            created_at=now,
            updated_at=now,
        )
    try:
        result = await migrate("upgrade", "rollerfour")
        assert result.returncode != 0
        assert "original active responsibility in " + expected_table in result.stderr
        async with engine.connect() as connection:
            for original, condition, snapshot in originals:
                actual = (
                    (await connection.execute(select(original).where(*condition))).mappings().one()
                )
                assert dict(actual) == snapshot
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "rollerfoundation"
            )
    finally:
        async with engine.begin() as connection:
            for original, condition, _ in reversed(originals):
                await connection.execute(original.delete().where(*condition))


async def test_unscoped_delivery_preserves_foundation_downgrade(roller_migration):
    engine, migrate, table = roller_migration
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    messages = await table("reliable_message")
    async with engine.begin() as connection:
        message_id = await connection.scalar(
            messages.insert()
            .values(
                target="unscoped-peer",
                operation="fact",
                operation_id="original-fact",
                request_raw=b"original-fact-bytes",
                response_raw=b"ack",
                status="DONE",
                attempts=1,
                next_attempt_at=now,
                created_at=now,
                updated_at=now,
                last_http_status=200,
                last_verdict="DELIVERED",
            )
            .returning(messages.c.id)
        )
    try:
        result = await migrate("downgrade", "sharedrackdispatch")
        assert result.returncode == 0, result.stdout + result.stderr
        await engine.dispose()
        async with engine.connect() as connection:
            assert (
                await connection.execute(
                    select(
                        messages.c.request_raw, messages.c.response_raw, messages.c.status
                    ).where(messages.c.id == message_id)
                )
            ).one() == (b"original-fact-bytes", b"ack", "DONE")
    finally:
        result = await migrate("upgrade", "rollerfoundation")
        assert result.returncode == 0, result.stdout + result.stderr
        await engine.dispose()
        async with engine.begin() as connection:
            await connection.execute(messages.delete().where(messages.c.id == message_id))
