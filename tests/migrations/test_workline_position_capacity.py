import json

import pytest
from sqlalchemy import inspect, text

from tests.helpers import run_alembic

STORES = [
    ("POSITION_CAPACITY_MIGRATION_DATABASE_URL", backend, "sharedrackdispatch")
    for backend in ("sqlite", "postgres")
]


@pytest.mark.parametrize("state_migration", STORES, indirect=True)
@pytest.mark.parametrize("limit", [None, 3])
async def test_position_upgrade_preserves_limit_aliases_and_other_defaults(state_migration, limit):
    store = state_migration
    try:
        async with store.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO work_lines "
                    "(line_code,line_name,business_code,position_bindings,five_rack_request_limit) "
                    "VALUES ('POS_OLD','Original','smt_manual',:bindings,:limit)"
                ),
                {
                    "bindings": json.dumps(
                        {"FIVE_RACK": "OLD_P", "RETURN_RACK": "OLD_P", "TRANSFER_RACK": "OLD_T"}
                    ),
                    "limit": limit,
                },
            )
        await store.migrate("upgrade", "positioncapacity")
        async with store.engine.connect() as conn:
            names = await conn.run_sync(
                lambda c: inspect(c).get_table_names(
                    schema="enterprise_backend" if store.backend == "postgres" else None
                )
            )
            assert "workline_positions" in names
            assert (
                await conn.execute(
                    text(
                        "SELECT position_code,capacity FROM workline_positions "
                        "ORDER BY position_code"
                    )
                )
            ).all() == [("OLD_P", limit), ("OLD_T", 1)]
            assert "five_rack_request_limit" not in await conn.run_sync(
                lambda c: [
                    x["name"]
                    for x in inspect(c).get_columns(
                        "work_lines",
                        schema="enterprise_backend" if store.backend == "postgres" else None,
                    )
                ]
            )
        await store.migrate("downgrade", "sharedrackdispatch")
        async with store.engine.connect() as conn:
            assert (
                await conn.scalar(
                    text("SELECT five_rack_request_limit FROM work_lines WHERE line_code='POS_OLD'")
                )
                == limit
            )
    finally:
        async with store.engine.begin() as conn:
            if "workline_positions" in await conn.run_sync(
                lambda c: inspect(c).get_table_names(
                    schema="enterprise_backend" if store.backend == "postgres" else None
                )
            ):
                await conn.execute(text("DELETE FROM workline_positions"))
            await conn.execute(text("DELETE FROM work_lines WHERE line_code='POS_OLD'"))
        await store.migrate("upgrade", "positioncapacity")


@pytest.mark.parametrize("state_migration", STORES, indirect=True)
async def test_position_preflight_refuses_unattributed_limit_before_ddl(state_migration):
    store = state_migration
    try:
        async with store.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO work_lines "
                    "(line_code,line_name,business_code,five_rack_request_limit) "
                    "VALUES ('POS_BAD','Bad','smt_manual',3)"
                )
            )
        before = await store.schema_snapshot("work")
        result = await run_alembic(
            store.engine.url.render_as_string(hide_password=False), "upgrade", "positioncapacity"
        )
        assert result.returncode != 0
        assert "capacity preflight" in result.stdout + result.stderr
        assert await store.schema_snapshot("work") == before
    finally:
        async with store.engine.begin() as conn:
            await conn.execute(text("DELETE FROM work_lines WHERE line_code='POS_BAD'"))
        await store.migrate("upgrade", "positioncapacity")


@pytest.mark.parametrize("state_migration", STORES, indirect=True)
@pytest.mark.parametrize("capacity", [None, 2])
async def test_downgrade_refuses_unrepresentable_capacity_before_ddl(state_migration, capacity):
    store = state_migration
    try:
        async with store.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO work_lines "
                    "(line_code,line_name,business_code,position_bindings) "
                    "VALUES ('POS_LOSS','Original','smt_manual',:bindings)"
                ),
                {"bindings": json.dumps({"RETURN_RACK": "POS_LOSS_RETURN"})},
            )
        await store.migrate("upgrade", "positioncapacity")
        async with store.engine.begin() as conn:
            await conn.execute(
                text("UPDATE workline_positions SET capacity=:value"), {"value": capacity}
            )
        before = await store.schema_snapshot("work")
        result = await run_alembic(
            store.engine.url.render_as_string(hide_password=False),
            "downgrade",
            "sharedrackdispatch",
        )
        assert result.returncode != 0
        assert "capacity preflight" in result.stdout + result.stderr
        assert await store.schema_snapshot("work") == before
        async with store.engine.connect() as conn:
            assert await conn.scalar(text("SELECT capacity FROM workline_positions")) == capacity
    finally:
        async with store.engine.begin() as conn:
            await conn.execute(text("DELETE FROM workline_positions"))
            await conn.execute(text("DELETE FROM work_lines WHERE line_code='POS_LOSS'"))


@pytest.mark.parametrize("state_migration", STORES, indirect=True)
async def test_upgrade_preserves_deleted_workline_identity_highwater(state_migration):
    store = state_migration
    try:
        async with store.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO work_lines (id,line_code,line_name) "
                    "VALUES (800001,'POS_HIGH','High')"
                )
            )
            await conn.execute(text("DELETE FROM work_lines WHERE line_code='POS_HIGH'"))
        await store.migrate("upgrade", "positioncapacity")
        if store.backend == "sqlite":
            async with store.engine.begin() as conn:
                identity = await conn.scalar(
                    text(
                        "INSERT INTO work_lines (line_code,line_name) "
                        "VALUES ('POS_NEXT','Next') RETURNING id"
                    )
                )
                assert identity > 800001
    finally:
        async with store.engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM work_lines WHERE line_code IN ('POS_HIGH','POS_NEXT')")
            )
        await store.migrate("upgrade", "positioncapacity")


@pytest.mark.parametrize("state_migration", STORES, indirect=True)
async def test_original_intents_survive_upgrade_and_order_uses_registration_sequence(
    state_migration,
):
    from datetime import UTC, datetime

    from app.businesses._components.rack_dispatch.models import RackAction, RackJob
    from app.businesses.smt_manual.models import ManualTask
    from app.models import TransportTask
    from tests.migration_helpers import source_table

    store = state_migration
    now = datetime(2026, 11, 3, tzinfo=UTC)
    original_tables = (
        "rack_dispatch_job",
        "rack_dispatch_action",
        "transport_task",
        "reliable_message",
    )

    async def snapshot(conn):
        return {
            name: [
                dict(row)
                for row in (
                    await conn.execute(text(f"SELECT * FROM {name} ORDER BY id"))
                ).mappings()
            ]
            for name in original_tables
        }

    try:
        async with store.engine.begin() as conn:
            reliable = await source_table(conn, "reliable_message")
            line = await conn.scalar(
                text(
                    "INSERT INTO work_lines (line_code,line_name,business_code,position_bindings) "
                    "VALUES ('POS_ORIG','Original','smt_manual',:bindings) RETURNING id"
                ),
                {"bindings": json.dumps({"RETURN_RACK": "ORIGINAL_POINT"})},
            )
            await conn.execute(
                ManualTask.__table__.insert().values(
                    task_id="POS_TASK",
                    task_type="MANUAL",
                    business_type="outbound",
                    work_line_id=line,
                    workline_code="POS_ORIG",
                    issued_operation_id="issued",
                    issued_timestamp=1,
                    issued_data={},
                    queue_revision=1,
                    dispatch_sequence=1,
                )
            )
            # Deliberately reverse lexical job/action IDs against original Transport registration.
            for n, job in enumerate(("z-original", "a-original"), 1):
                await conn.execute(
                    RackJob.__table__.insert().values(
                        id=job,
                        owner_domain="smt_manual",
                        work_line_id=line,
                        source_kind="MANUAL_RETURN",
                        source_ref=job,
                        rack_id=f"R{n}",
                        original_cycle_id=job,
                        task_id="POS_TASK",
                        point="ORIGINAL_POINT",
                        caller={"workline_id": "caller", "station_id": "station"},
                        resources={"original": job, "positions": {"RETURN_RACK": "ORIGINAL_POINT"}},
                    )
                )
                message = await conn.scalar(
                    reliable.insert()
                    .values(
                        target="original-peer",
                        operation="transport.task.submit@v1",
                        operation_id=job,
                        request_raw=("frozen bytes " + job).encode(),
                        next_attempt_at=None,
                        created_at=now,
                        updated_at=now,
                        status="HELD",
                        attempts=0,
                        pause_reason="ORIGINAL",
                    )
                    .returning(reliable.c.id)
                )
                await conn.execute(
                    TransportTask.__table__.insert().values(
                        client_request_id=job,
                        transport_task_id=job,
                        operation_id=job,
                        kind="RACK_MOVE",
                        input_digest="0" * 64,
                        workline_id="caller",
                        station_id="station",
                        owner_domain="smt_manual",
                        cycle_id=job,
                        created_at=now,
                        frozen={"rack_id": f"R{n}", "original": job},
                        submission_acceptance={"original": job},
                        pending_outcome={"original": job},
                    )
                )
                await conn.execute(
                    RackAction.__table__.insert().values(
                        id=job,
                        job_id=job,
                        owner_domain="smt_manual",
                        work_line_id=line,
                        ordinal=1,
                        kind="ENTER",
                        source_ref=job,
                        completion_rule="APPLIED_RESULT",
                        client_request_id=job,
                        transport_task_id=job,
                        submit_operation_id=job,
                        submit_message_id=message,
                        submit_target="original-peer",
                        target={"kind": "RACK_POSITION", "location_code": "ORIGINAL_POINT"},
                        target_face="A",
                        basis={"original": job},
                    )
                )
            before = await snapshot(conn)
        await store.migrate("upgrade", "positioncapacity")
        await store.engine.dispose()
        async with store.engine.connect() as conn:
            after = await snapshot(conn)
            orders = {r.pop("admission_order"): r["id"] for r in after["rack_dispatch_job"]}
            assert orders == {1: "z-original", 2: "a-original"}
            assert after == before
            assert (
                await conn.execute(text("SELECT position_code,capacity FROM workline_positions"))
            ).all() == [("ORIGINAL_POINT", 1)]
        # A populated stable order cannot be silently discarded by a downgrade.
        result = await run_alembic(
            store.engine.url.render_as_string(hide_password=False),
            "downgrade",
            "sharedrackdispatch",
        )
        assert result.returncode != 0
        assert "admission orders exist" in result.stdout + result.stderr
    finally:
        async with store.engine.begin() as conn:
            for table in (
                "rack_dispatch_action",
                "rack_dispatch_job",
                "transport_task",
                "reliable_message",
                "smt_manual_task",
            ):
                await conn.execute(text(f"DELETE FROM {table}"))
            names = await conn.run_sync(
                lambda c: inspect(c).get_table_names(
                    schema="enterprise_backend" if store.backend == "postgres" else None
                )
            )
            if "workline_positions" in names:
                await conn.execute(text("DELETE FROM workline_positions"))
            await conn.execute(text("DELETE FROM work_lines WHERE line_code='POS_ORIG'"))
        await store.migrate("upgrade", "positioncapacity")


@pytest.mark.parametrize("state_migration", STORES, indirect=True)
async def test_unproved_original_task_is_rejected_without_touching_job_or_schema(state_migration):
    from app.businesses._components.rack_dispatch.models import RackJob

    store = state_migration
    try:
        async with store.engine.begin() as conn:
            line = await conn.scalar(
                text(
                    "INSERT INTO work_lines (line_code,line_name,business_code,position_bindings) "
                    "VALUES ('POS_UNPROVED','Original','smt_manual',:bindings) RETURNING id"
                ),
                {"bindings": json.dumps({"RETURN_RACK": "UNPROVED_POINT"})},
            )
            await conn.execute(
                RackJob.__table__.insert().values(
                    id="unproved",
                    owner_domain="smt_manual",
                    work_line_id=line,
                    source_kind="MANUAL_RETURN",
                    source_ref="original",
                    rack_id="R1",
                    original_cycle_id="unproved",
                    task_id="MISSING_ORIGINAL_TASK",
                    point="UNPROVED_POINT",
                )
            )
            original = (
                await conn.execute(text("SELECT * FROM rack_dispatch_job WHERE id='unproved'"))
            ).one()
        before = await store.schema_snapshot("rack_dispatch")
        result = await run_alembic(
            store.engine.url.render_as_string(hide_password=False), "upgrade", "positioncapacity"
        )
        assert result.returncode != 0
        assert "unproved original task" in result.stdout + result.stderr
        assert await store.schema_snapshot("rack_dispatch") == before
        async with store.engine.connect() as conn:
            assert (
                await conn.execute(text("SELECT * FROM rack_dispatch_job WHERE id='unproved'"))
            ).one() == original
    finally:
        async with store.engine.begin() as conn:
            await conn.execute(text("DELETE FROM rack_dispatch_job WHERE id='unproved'"))
            await conn.execute(text("DELETE FROM work_lines WHERE line_code='POS_UNPROVED'"))
        await store.migrate("upgrade", "positioncapacity")
