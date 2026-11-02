import asyncio
import os
import subprocess
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

MIGRATIONS = [
    ("BIN_ROLLER_MIGRATION_DATABASE_URL", backend, "binrollercompact")
    for backend in ("sqlite", "postgres")
]


@pytest.mark.parametrize("state_migration", MIGRATIONS, indirect=True)
async def test_optional_limit_upgrade_preserves_lines_and_protects_configuration(state_migration):
    store = state_migration
    try:
        async with store.engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO work_lines (line_code,line_name,business_code,version) "
                    "VALUES ('CAPACITY_OLD','Original','smt_manual',7)"
                )
            )
        await store.migrate("upgrade", "fiveracklimit")
        async with store.engine.begin() as connection:
            original = (
                await connection.execute(
                    text(
                        "SELECT line_code,line_name,business_code,version,five_rack_request_limit "
                        "FROM work_lines WHERE line_code='CAPACITY_OLD'"
                    )
                )
            ).one()
            assert tuple(original) == ("CAPACITY_OLD", "Original", "smt_manual", 7, None)
            await connection.execute(
                text(
                    "UPDATE work_lines SET five_rack_request_limit=3 WHERE line_code='CAPACITY_OLD'"
                )
            )
            configured = dict(
                (
                    await connection.execute(
                        text("SELECT * FROM work_lines WHERE line_code='CAPACITY_OLD'")
                    )
                )
                .mappings()
                .one()
            )
        configured_schema = await store.schema_snapshot("work_lines")
        refused = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", "downgrade", "binrollercompact"],
            env={
                **os.environ,
                "DATABASE_URL": store.engine.url.render_as_string(hide_password=False),
            },
            capture_output=True,
            text=True,
        )
        assert refused.returncode != 0
        assert "configured rack request limits exist" in refused.stdout + refused.stderr
        assert await store.schema_snapshot("work_lines") == configured_schema
        async with store.engine.connect() as connection:
            assert (
                dict(
                    (
                        await connection.execute(
                            text("SELECT * FROM work_lines WHERE line_code='CAPACITY_OLD'")
                        )
                    )
                    .mappings()
                    .one()
                )
                == configured
            )
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "fiveracklimit"
            )
        invalid = (0, -1, 2147483648, *([1.5] if store.backend == "sqlite" else []))
        for value in invalid:
            with pytest.raises(DBAPIError):
                async with store.engine.begin() as connection:
                    await connection.execute(
                        text(
                            "UPDATE work_lines SET five_rack_request_limit=:limit "
                            "WHERE line_code='CAPACITY_OLD'"
                        ),
                        {"limit": value},
                    )
        async with store.engine.begin() as connection:
            assert (
                await connection.scalar(
                    text(
                        "SELECT five_rack_request_limit FROM work_lines "
                        "WHERE line_code='CAPACITY_OLD'"
                    )
                )
                == 3
            )
            await connection.execute(
                text(
                    "UPDATE work_lines SET five_rack_request_limit=NULL "
                    "WHERE line_code='CAPACITY_OLD'"
                )
            )
        await store.migrate("downgrade", "binrollercompact")
        async with store.engine.begin() as connection:
            assert (
                await connection.execute(
                    text(
                        "SELECT line_code,line_name,business_code,version "
                        "FROM work_lines WHERE line_code='CAPACITY_OLD'"
                    )
                )
            ).one() == ("CAPACITY_OLD", "Original", "smt_manual", 7)
    finally:
        await store.migrate("upgrade", "head")
        async with store.engine.begin() as connection:
            await connection.execute(text("DELETE FROM work_lines WHERE line_code='CAPACITY_OLD'"))


@pytest.mark.parametrize("state_migration", [MIGRATIONS[0]], indirect=True)
@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
async def test_sqlite_limit_migration_preserves_deleted_identity_high_water(
    state_migration, direction
):
    store = state_migration
    try:
        if direction == "downgrade":
            await store.migrate("upgrade", "fiveracklimit")
        async with store.engine.begin() as connection:
            highest = await connection.scalar(
                text("SELECT COALESCE(MAX(id),0)+1000 FROM work_lines")
            )
            await connection.execute(
                text(
                    "INSERT INTO work_lines (id,line_code,line_name) "
                    "VALUES (:id,'CAPACITY_DELETED_HIGH','Deleted')"
                ),
                {"id": highest},
            )
            await connection.execute(text("DELETE FROM work_lines WHERE id=:id"), {"id": highest})
        await store.migrate(
            direction, "fiveracklimit" if direction == "upgrade" else "binrollercompact"
        )
        async with store.engine.begin() as connection:
            next_id = await connection.scalar(
                text(
                    "INSERT INTO work_lines (line_code,line_name) "
                    "VALUES ('CAPACITY_AFTER','After') RETURNING id"
                )
            )
            assert next_id > highest
    finally:
        await store.migrate("upgrade", "head")
        async with store.engine.begin() as connection:
            await connection.execute(
                text(
                    "DELETE FROM work_lines WHERE line_code IN "
                    "('CAPACITY_DELETED_HIGH','CAPACITY_AFTER')"
                )
            )


@pytest.mark.parametrize("state_migration", MIGRATIONS, indirect=True)
@pytest.mark.parametrize("purpose", ["SUPPLY", "DRAIN"])
async def test_withdrawn_unstarted_service_preserves_data_and_refuses_lossy_downgrade(
    state_migration,
    purpose,
):
    store = state_migration

    async def original():
        async with store.engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text("SELECT * FROM bin_roller_face_work WHERE id='CAPACITY_DEMAND'")
                    )
                )
                .mappings()
                .one()
            )
            return dict(row)

    try:
        async with store.engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO bin_roller_rack_cycle "
                    "(id,owner_domain,work_line_id,rack_id,purpose,task_id,"
                    "source_ref,status,pending) VALUES "
                    "('CAPACITY_CYCLE','capacity',1,'R1',:cycle_purpose,:task,"
                    "'CAPACITY_DEMAND','ACTIVE',true)"
                ),
                {
                    "cycle_purpose": "ORDINARY" if purpose == "SUPPLY" else "DRAIN",
                    "task": "T1" if purpose == "SUPPLY" else None,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO bin_roller_face_work "
                    "(purpose,id,demand_id,service_id,owner_domain,work_line_id,task_id,change_id,"
                    "plan_revision,rack_id,rack_face,cycle_id,source_ref,"
                    "demand_state,service_state) "
                    "VALUES (:purpose,'CAPACITY_DEMAND',:demand,:service,'capacity',"
                    "1,:task,:change,:revision,'R1','A','CAPACITY_CYCLE','CAPACITY_DEMAND',:state,'PENDING')"
                ),
                {
                    "purpose": purpose,
                    "demand": "CAPACITY_DEMAND" if purpose == "SUPPLY" else None,
                    "service": "CAPACITY_SERVICE" if purpose == "SUPPLY" else "CAPACITY_DEMAND",
                    "task": "T1" if purpose == "SUPPLY" else None,
                    "change": "CHANGE" if purpose == "SUPPLY" else None,
                    "revision": 1 if purpose == "SUPPLY" else None,
                    "state": "ACTIVE" if purpose == "SUPPLY" else None,
                },
            )
        old = await original()
        old_schema = await store.schema_snapshot("bin_roller_")
        await store.migrate("upgrade", "fiveracklimit")
        assert await original() == old
        await store.migrate("downgrade", "binrollercompact")
        assert await original() == old
        assert await store.schema_snapshot("bin_roller_") == old_schema
        await store.migrate("upgrade", "fiveracklimit")
        try:
            async with store.engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE bin_roller_face_work SET demand_state=:state,"
                        "service_state='WITHDRAWN' "
                        "WHERE id='CAPACITY_DEMAND'"
                    ),
                    {"state": "WITHDRAWN" if purpose == "SUPPLY" else None},
                )
        except DBAPIError:
            accepted = False
        else:
            accepted = True
        assert accepted, "unstarted service cancellation must preserve its original identity"
        saved = await original()
        current_schema = await store.schema_snapshot("bin_roller_")
        refused = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", "downgrade", "binrollercompact"],
            env={
                **os.environ,
                "DATABASE_URL": store.engine.url.render_as_string(hide_password=False),
            },
            capture_output=True,
            text=True,
        )
        assert refused.returncode != 0
        assert "withdrawn unstarted face services exist" in refused.stdout + refused.stderr
        assert saved["service_id"] in refused.stdout + refused.stderr
        assert await original() == saved
        assert await store.schema_snapshot("bin_roller_") == current_schema
        async with store.engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "fiveracklimit"
            )
    finally:
        async with store.engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM bin_roller_face_work WHERE id='CAPACITY_DEMAND'")
            )
            await connection.execute(
                text("DELETE FROM bin_roller_rack_cycle WHERE id='CAPACITY_CYCLE'")
            )
        await store.migrate("upgrade", "head")
