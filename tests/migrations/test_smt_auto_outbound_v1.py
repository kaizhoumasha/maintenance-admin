"""The outbound work-plan cutover preserves unrelated rows and refuses active evidence."""

import pytest
from sqlalchemy import text

PREVIOUS = "http_exchange_initiator"


@pytest.mark.parametrize(
    "state_migration",
    [("SMT_AUTO_MIGRATION_DATABASE_URL", backend, PREVIOUS) for backend in ("sqlite", "postgres")],
    indirect=True,
    ids=["sqlite", "postgres"],
)
async def test_outbound_v1_migration_refuses_old_work_and_preserves_other_tasks(state_migration):
    store = state_migration
    async with store.engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO sorting_task (task_id,task_type,business_type,work_line_id,"
                "workline_code,issued_operation_id,issued_timestamp,issued_data,dispatch_sequence,"
                "status) VALUES ('out','AUTO','outbound',1,'OUT','out',1,'{}',1,'EXECUTING'),"
                "('in','AUTO','inbound',2,'IN','in',1,'{}',1,'EXECUTING'),"
                "('manual','MANUAL','outbound',3,'MAN','manual',1,'{}',1,'EXECUTING')"
            )
        )
    before = await store.schema_snapshot("smt_auto")
    with pytest.raises(AssertionError, match="outbound work is still pending"):
        await store.migrate("upgrade", "head")
    assert await store.schema_snapshot("smt_auto") == before

    async with store.engine.begin() as connection:
        await connection.execute(
            text("UPDATE sorting_task SET status='COMPLETED',pending=FALSE WHERE task_id='out'")
        )
        await connection.execute(
            text(
                "INSERT INTO smt_auto_bin_work (id,work_line_id,arrival_id,device_code,"
                "raw_bin_code,bin_code,scanned_at,task_id,business_type,association,pending)"
                " VALUES (1,1,'old','SCAN2','BIN-C','BIN',1,'out','outbound','TASK',FALSE),"
                "(2,2,'inbound','SCAN2','IN-C','IN',1,'in','inbound','TASK',TRUE),"
                "(3,1,'no-task','SCAN2','NO-C','NO',1,NULL,NULL,'NO_TASK',FALSE)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO smt_auto_outbound_plate (id,task_id,bin_work_id,grant_kind,"
                "grant_parent_plate_id,grant_snapshot,source_locator,source_params,"
                "source_arrival_snapshot,pick_call_key,pending)"
                " VALUES (1,'out',1,'NEXT',1,'{}','{}','{}','{}','old-pick',FALSE)"
            )
        )
    with pytest.raises(AssertionError, match="legacy NEXT grants"):
        await store.migrate("upgrade", "head")
    assert await store.schema_snapshot("smt_auto") == before

    async with store.engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO smt_auto_outbound_decision_round (id,task_id,purpose,bin_work_id,"
                "stage_key,round_no,operation,operation_id,message_id,request_data,"
                "response_snapshot,consumed_at_ms,unresolved)"
                " VALUES (1,'out','WORK_PLAN',1,'bin:1:first',1,'outbound.bin.work_plan@v1',"
                " 'old-ready',1,'{}',:ready,1,FALSE)"
            ),
            {"ready": '{"data":{"result":"READY"}}'},
        )
        await connection.execute(
            text(
                "UPDATE smt_auto_outbound_plate SET grant_kind='READY',grant_round_id=1,"
                "grant_parent_plate_id=NULL WHERE id=1"
            )
        )
        await connection.execute(
            text(
                "UPDATE smt_auto_bin_work SET release_command_code='old-release',"
                "release_consumed_at_ms=2 WHERE id=1"
            )
        )
    with pytest.raises(AssertionError, match="legacy outbound BIN history"):
        await store.migrate("upgrade", "head")
    assert await store.schema_snapshot("smt_auto") == before

    async with store.engine.begin() as connection:
        await connection.execute(text("DELETE FROM smt_auto_outbound_plate WHERE id=1"))
    with pytest.raises(AssertionError, match="legacy outbound BIN history"):
        await store.migrate("upgrade", "head")
    assert await store.schema_snapshot("smt_auto") == before

    async with store.engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM smt_auto_outbound_decision_round WHERE bin_work_id=1")
        )
        await connection.execute(text("DELETE FROM smt_auto_bin_work WHERE id=1"))
    await store.migrate("upgrade", "head")
    after = await store.schema_snapshot("smt_auto")
    assert set(before) == set(after)
    for table, column in (
        ("smt_auto_bin_work", "normal_plan_due"),
        ("smt_auto_outbound_source_member", "normal_plan_due"),
        ("smt_auto_outbound_decision_round", "normal_continuation"),
    ):
        flag = next(item for item in after[table]["columns"] if item["name"] == column)
        assert flag["nullable"] is False
        assert flag["default"] is not None
    assert {"grant_parent_plate_id", "next_source_action"}.isdisjoint(
        item["name"] for item in after["smt_auto_outbound_plate"]["columns"]
    )
    assert after["smt_auto_inbound_plate"] == before["smt_auto_inbound_plate"]
    async with store.engine.begin() as connection:
        assert await connection.scalar(text("SELECT COUNT(*) FROM sorting_task")) == 3
        assert (
            await connection.execute(
                text("SELECT arrival_id, normal_plan_due FROM smt_auto_bin_work ORDER BY id")
            )
        ).all() == [("inbound", False), ("no-task", False)]
        await connection.execute(
            text("UPDATE smt_auto_bin_work SET normal_plan_due=TRUE WHERE id=2")
        )
    with pytest.raises(AssertionError, match="new continuation evidence"):
        await store.migrate("downgrade", PREVIOUS)
    assert await store.schema_snapshot("smt_auto") == after
