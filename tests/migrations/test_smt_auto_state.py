"""S1 migration shape and constraint coverage without formal business assembly."""

import pytest
from sqlalchemy import inspect, text

from app.businesses.smt_auto.models import TABLES


@pytest.mark.parametrize(
    "state_migration",
    [("SMT_AUTO_MIGRATION_DATABASE_URL", "sqlite", "rollerfour")],
    indirect=True,
)
async def test_smt_auto_migration_has_all_tables_constraints_and_indexes(state_migration):
    store = state_migration
    await store.migrate("upgrade", "head")
    async with store.engine.connect() as connection:

        def check(sync):
            schema = None if sync.dialect.name == "sqlite" else "enterprise_backend"
            inspector = inspect(sync)
            names = set(inspector.get_table_names(schema=schema))
            assert {table.name for table in TABLES.values()} <= names
            for table in TABLES.values():
                checks = {
                    item["name"]
                    for item in inspector.get_check_constraints(table.name, schema=schema)
                }
                expected = {
                    item.name
                    for item in table.constraints
                    if item.__class__.__name__ == "CheckConstraint"
                }
                assert expected <= checks
                unique = {
                    tuple(item["column_names"])
                    for item in inspector.get_unique_constraints(table.name, schema=schema)
                }
                expected_unique = {
                    tuple(column.name for column in item.columns)
                    for item in table.constraints
                    if item.__class__.__name__ == "UniqueConstraint"
                }
                assert expected_unique <= unique
                assert inspector.get_pk_constraint(table.name, schema=schema)[
                    "constrained_columns"
                ] == [column.name for column in table.primary_key.columns]
            indexes = {
                item["name"] for item in inspector.get_indexes(TABLES["task"].name, schema=schema)
            }
            assert {"sorting_task_queue", "sorting_task_execution_slot"} <= indexes

        await connection.run_sync(check)


@pytest.mark.parametrize(
    "state_migration",
    [("SMT_AUTO_MIGRATION_DATABASE_URL", "sqlite", "smtmanualoprename")],
    indirect=True,
)
async def test_auto_foundation_upgrade_and_downgrade_preserve_existing_worklines(state_migration):
    store = state_migration
    async with store.engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO work_lines (line_code, line_name, business_code, is_enabled) "
                "VALUES ('PRESERVED', 'Existing line', 'smt_manual', 0)"
            )
        )
    before = await store.schema_snapshot("work_lines")
    for direction, revision in (("upgrade", "head"), ("downgrade", "smtmanualoprename")):
        await store.migrate(direction, revision)
        assert await store.schema_snapshot("work_lines") == before
        async with store.engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT line_name, business_code, is_enabled FROM work_lines "
                        "WHERE line_code = 'PRESERVED'"
                    )
                )
            ).one()
            assert tuple(row) == ("Existing line", "smt_manual", 0)


@pytest.mark.parametrize(
    "state_migration",
    [("SMT_AUTO_MIGRATION_DATABASE_URL", "sqlite", "smtautofoundation")],
    indirect=True,
)
async def test_auto_rack_fact_downgrade_preserves_original_association_evidence(state_migration):
    store = state_migration
    await store.migrate("upgrade", "smtautofirstplan")
    async with store.engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO smt_auto_task (task_id,business_type,work_line_id,workline_code,"
                "issued_operation_id,issued_timestamp,issued_data,dispatch_sequence) "
                "VALUES ('original','inbound',12,'L','issued',1,'{}',1)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO smt_auto_task_input (task_id,operation,operation_id,request_timestamp,"
                "first_received_at_ms,effective_digest,contract_version,effective_request,disposition,"
                "target_job_id) VALUES ('original','original','input',1,1,'digest','v1','{}',"
                "'APPLIED','original-job')"
            )
        )
    before = await store.schema_snapshot("smt_auto_task_input")
    with pytest.raises(AssertionError, match="original evidence; downgrade refused"):
        await store.migrate("downgrade", "smtautofoundation")
    assert await store.schema_snapshot("smt_auto_task_input") == before
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT target_job_id FROM smt_auto_task_input"))
            == "original-job"
        )


@pytest.mark.parametrize(
    "state_migration",
    [("SMT_AUTO_MIGRATION_DATABASE_URL", "sqlite", "smtautorackfacts")],
    indirect=True,
)
@pytest.mark.parametrize("evidence", ["terminal", "arrival"])
async def test_first_work_plan_downgrade_refuses_original_face_evidence(state_migration, evidence):
    store = state_migration
    await store.migrate("upgrade", "smtautosourcearrival")
    async with store.engine.begin() as connection:
        for sql in (
            "INSERT INTO smt_auto_task (task_id,business_type,work_line_id,workline_code,"
            "issued_operation_id,issued_timestamp,issued_data,dispatch_sequence) "
            "VALUES ('original','outbound',12,'L','issued',1,'{}',1)",
            "INSERT INTO smt_auto_task_input (id,task_id,operation,operation_id,request_timestamp,"
            "first_received_at_ms,effective_digest,contract_version,effective_request,disposition) "
            "VALUES (1,'original','sorting.task.plan_delta@v1','plan',1,1,'d','v1','{}','APPLIED')",
            "INSERT INTO smt_auto_outbound_source_member (task_id,plan_input_id,plan_revision,"
            "rack_id,rack_face,rack_array_index,terminal_basis,terminal_at_ms,arrived_at_ms) "
            + (
                "VALUES ('original',1,1,'rack','A',0,'{}',1,NULL)"
                if evidence == "terminal"
                else "VALUES ('original',1,1,'rack','A',0,NULL,NULL,10)"
            ),
        ):
            await connection.execute(text(sql))
    before = await store.schema_snapshot("smt_auto_outbound_source_member")
    with pytest.raises(AssertionError, match="downgrade refused"):
        await store.migrate("downgrade", "smtautorackfacts")
    assert await store.schema_snapshot("smt_auto_outbound_source_member") == before


@pytest.mark.parametrize(
    "state_migration",
    [("SMT_AUTO_MIGRATION_DATABASE_URL", "sqlite", "smtautosourcearrival")],
    indirect=True,
)
async def test_deadline_downgrade_preserves_original_configuration_evidence(state_migration):
    store = state_migration
    await store.migrate("upgrade", "smtautofirstdeadline")
    async with store.engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO smt_auto_task (task_id,business_type,work_line_id,workline_code,"
                "issued_operation_id,issued_timestamp,issued_data,dispatch_sequence,"
                "first_plan_wait_ms,first_plan_deadline_ms) "
                "VALUES ('original','inbound',12,'L','issued',1,'{}',1,100,200)"
            )
        )
    before = await store.schema_snapshot("smt_auto_task")
    with pytest.raises(
        AssertionError, match="original first-plan deadline exists; downgrade refused"
    ):
        await store.migrate("downgrade", "smtautosourcearrival")
    assert await store.schema_snapshot("smt_auto_task") == before


@pytest.mark.parametrize(
    "state_migration",
    [
        ("SMT_AUTO_MIGRATION_DATABASE_URL", "sqlite", "smtautofirstdeadline"),
        ("SMT_AUTO_MIGRATION_DATABASE_URL", "postgres", "smtautofirstdeadline"),
    ],
    indirect=True,
)
async def test_barcode_migration_preserves_unicode_and_refuses_lossy_downgrade(state_migration):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.businesses.smt_auto.models import (
        AutoBinWork,
        AutoInboundDecisionRound,
        AutoInboundPlate,
    )
    from tests.migration_helpers import source_table

    store = state_migration
    await store.migrate("upgrade", "xray_pending_due")
    barcode = "原始料盘," * 60
    factory = async_sessionmaker(store.engine, expire_on_commit=False)
    async with factory.begin() as session:
        task = await source_table(await session.connection(), "smt_auto_task")
        await session.execute(
            task.insert().values(
                task_id="original",
                business_type="inbound",
                work_line_id=12,
                workline_code="L",
                issued_operation_id="issued",
                issued_timestamp=1,
                issued_data={},
                dispatch_sequence=1,
            )
        )
        work = AutoBinWork(
            work_line_id=12,
            arrival_id="arrival",
            device_code="SCAN2",
            raw_bin_code="BIN-C",
            bin_code="BIN",
            scanned_at=1,
            task_id="original",
            business_type="inbound",
            association="TASK",
        )
        session.add(work)
        await session.flush()
        round_ = AutoInboundDecisionRound(
            task_id="original",
            purpose="WORK_PLAN",
            bin_work_id=work.id,
            stage_key="first",
            round_no=1,
            operation="putaway.work_plan@v1",
            operation_id="plan",
            message_id=1,
            request_data={},
        )
        session.add(round_)
        await session.flush()
        session.add(
            AutoInboundPlate(
                task_id="original",
                bin_work_id=work.id,
                grant_kind="READY",
                grant_round_id=round_.id,
                grant_snapshot={},
                source_locator={},
                source_params={},
                source_arrival_snapshot={},
                pick_call_key="pick",
                barcode=barcode,
            )
        )
    try:
        before = await store.schema_snapshot("smt_auto_inbound_plate")
        with pytest.raises(
            AssertionError, match="raw barcode exceeds 200 code points; downgrade refused"
        ):
            await store.migrate("downgrade", "smtautofirstdeadline")
        assert await store.schema_snapshot("smt_auto_inbound_plate") == before
        async with store.engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT barcode FROM smt_auto_inbound_plate"))
                == barcode
            )
    finally:
        async with store.engine.begin() as connection:
            for table in (
                "smt_auto_inbound_plate",
                "smt_auto_inbound_decision_round",
                "smt_auto_bin_work",
                "smt_auto_task",
            ):
                await connection.execute(text(f"DELETE FROM {table} WHERE task_id = 'original'"))
