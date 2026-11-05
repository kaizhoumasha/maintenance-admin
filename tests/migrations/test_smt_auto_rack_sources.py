"""The source constraint changes without replacing original rack responsibilities."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

_INSERT = text(
    "INSERT INTO rack_dispatch_job "
    "(id,owner_domain,work_line_id,source_kind,source_ref,rack_id,original_cycle_id,"
    "state,pending,release_requested,facts_revision) "
    "VALUES (:id,:owner,1,:kind,:id,:id,:cycle,'ACTIVE',true,false,3)"
)


@pytest.mark.parametrize(
    "state_migration",
    [
        ("SMT_AUTO_MIGRATION_DATABASE_URL", backend, "smtautoretention")
        for backend in ("sqlite", "postgres")
    ],
    indirect=True,
)
async def test_auto_source_upgrade_preserves_original_jobs_and_guards_downgrade(state_migration):
    store = state_migration
    manual = dict(id="manual", owner="smt_manual", kind="MANUAL_RETURN", cycle="frozen-manual")
    auto = dict(id="auto", owner="smt_auto", kind="AUTO_INBOUND_SOURCE", cycle="frozen-auto")
    async with store.engine.begin() as conn:
        await conn.execute(_INSERT, manual)
        await conn.execute(
            text(
                "INSERT INTO rack_dispatch_action "
                "(id,job_id,owner_domain,work_line_id,ordinal,kind,source_ref,phase,"
                "completion_rule) "
                "VALUES ('original-action','manual','smt_manual',1,1,'ENTER','original',"
                "'REGISTERED','APPLIED_RESULT')"
            )
        )
    try:
        with pytest.raises(IntegrityError):
            async with store.engine.begin() as conn:
                await conn.execute(_INSERT, auto)
        await store.migrate("upgrade", "smtautosources")
        async with store.engine.begin() as conn:
            await conn.execute(_INSERT, auto)
        before = await store.schema_snapshot("rack_dispatch_job")
        with pytest.raises(AssertionError, match="AUTO rack source evidence exists"):
            await store.migrate("downgrade", "smtautoretention")
        assert await store.schema_snapshot("rack_dispatch_job") == before
        async with store.engine.begin() as conn:
            assert (
                await conn.execute(
                    text(
                        "SELECT id, original_cycle_id, facts_revision "
                        "FROM rack_dispatch_job ORDER BY id"
                    )
                )
            ).all() == [("auto", "frozen-auto", 3), ("manual", "frozen-manual", 3)]
            await conn.execute(text("DELETE FROM rack_dispatch_job WHERE id = 'auto'"))
        await store.migrate("downgrade", "smtautoretention")
        with pytest.raises(IntegrityError):
            async with store.engine.begin() as conn:
                await conn.execute(_INSERT, auto)
        async with store.engine.connect() as conn:
            assert (
                await conn.scalar(
                    text("SELECT original_cycle_id FROM rack_dispatch_job WHERE id = 'manual'")
                )
                == "frozen-manual"
            )
            assert (
                await conn.execute(
                    text(
                        "SELECT id, job_id, source_ref FROM rack_dispatch_action "
                        "WHERE id = 'original-action'"
                    )
                )
            ).one() == ("original-action", "manual", "original")
    finally:
        async with store.engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM rack_dispatch_action WHERE id = 'original-action'")
            )
            await conn.execute(text("DELETE FROM rack_dispatch_job WHERE id IN ('auto', 'manual')"))
