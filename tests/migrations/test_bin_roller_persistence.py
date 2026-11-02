"""Restore original public work IDs, frozen evidence, SQL nulls and the original schema."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.businesses._components.bin_roller.ports import RollerWorkRef, SourceFaceRef, SupplyChange
from app.businesses._components.bin_roller.service import BinRoller
from app.integrations.ecs_command.service import CommandOwner, EcsCommands

OWNER = "bin-roller-migration-fixture"
OLD = "smtmanualcompact"
NEW = "binrollercompact"
MIGRATIONS = [("BIN_ROLLER_MIGRATION_DATABASE_URL", d, OLD) for d in ("sqlite", "postgres")]


async def tables(store):
    async with store.engine.connect() as c:

        def reflect(sync):
            schema = "enterprise_backend" if sync.dialect.name == "postgresql" else None
            md = sa.MetaData(schema=schema)
            md.reflect(sync)
            for table in md.tables.values():
                for col in table.c:
                    if isinstance(col.type, sa.JSON):
                        col.type.none_as_null = True
            return {t.name: t for t in md.tables.values()}

        return await c.run_sync(reflect)


async def snapshot(store, source, *, platform=False):
    async with store.engine.connect() as c:
        result = {}
        for name, table in source.items():
            if not (
                name in {"ecs_command", "reliable_message", "transport_task"}
                if platform
                else name.startswith("bin_roller_")
            ):
                continue
            query = sa.select(
                table,
                *(
                    col.is_(None).label(col.name + "_sql_null")
                    for col in table.c
                    if isinstance(col.type, sa.JSON)
                ),
            )
            result[name] = (
                (await c.execute(query.order_by(*table.primary_key.columns))).mappings().all()
            )
        return result


async def populate(store, source):
    async with store.engine.begin() as c:
        scope = dict(owner_domain=OWNER, work_line_id=1)
        now = datetime(2026, 11, 2, tzinfo=UTC)

        async def put(key, id, **values):
            await c.execute(source["bin_roller_" + key].insert().values(**(scope | values), id=id))

        for i, (point, rid, box) in enumerate(
            (
                ("SCAN2", None, "B"),
                ("SCAN2", None, "C"),
                ("SCAN3", "old", "B"),
                ("SCAN4", "old", "B"),
                ("SCAN4", "old", "B"),
                ("SCAN3", "new", "B"),
                ("SCAN4", "new", "B"),
            ),
            1,
        ):
            await put(
                "scan_arrival",
                f"a{i}",
                point=point,
                first_digest=f"d{i}",
                business_key=f"k{i}",
                received_at=now + timedelta(seconds=i),
                device_code=point,
                raw_bin_code=box + "-B",
                bin_code=box,
                scanned_at=i,
                return_round_id=rid,
                delegated=point == "SCAN2",
                pending=point != "SCAN2",
                command_code=f"cmd{i}",
                consumed_result_id="original-result" if i == 4 else None,
            )
            await put(
                "scan_fact",
                f"fact{i}",
                point=point,
                digest=f"d{i}",
                receipt_id=i,
                arrival_id=f"a{i}",
                device_code=point,
                raw_bin_code=box + "-B",
                bin_code=box,
                scanned_at=i,
            )
        await put(
            "scan_fact",
            "duplicate-business-arrival",
            point="SCAN4",
            digest="another",
            receipt_id=20,
            arrival_id="a7",
            device_code="SCAN4",
            raw_bin_code="B-B",
            bin_code="B",
            scanned_at=20,
            binding_snapshot={"generation": 1},
        )
        for i, release in ((1, "z-release"), (2, "a-release")):
            await put(
                "scan2_release",
                release,
                arrival_id=f"a{i}",
                call_key=f"frozen:{i}",
                command_code=f"cmd{i}",
                pending=True,
            )
        for rid, third, fourth in (("old", "a3", "a4"), ("new", "a6", "a7")):
            await put(
                "return_round",
                rid,
                scan3_arrival_id=third,
                scan4_arrival_id=fourth,
                bin_code="B",
                status="PENDING",
                removed=False,
            )
        for purpose, id, ref in (("ORDINARY", "cycle", "d1"), ("DRAIN", "drain-cycle", "same")):
            await put(
                "rack_cycle",
                id,
                rack_id="R",
                purpose=purpose,
                task_id="T" if purpose == "ORDINARY" else None,
                source_ref=ref,
                resources={"positions": {"FIVE_RACK": "F"}},
                status="ACTIVE" if purpose == "ORDINARY" else "RELEASED",
                pending=purpose == "ORDINARY",
                release_basis=None
                if purpose == "ORDINARY"
                else {"submit": "ACCEPTED", "transport": "original-ctu03"},
            )
        await put(
            "rack_cycle",
            "wait-cycle",
            rack_id="R2",
            purpose="DRAIN",
            task_id=None,
            source_ref="same",
            resources={"positions": {"FIVE_RACK": "F"}},
            status="ACTIVE",
            pending=True,
        )
        for i, face in enumerate("ABA", 1):
            await put(
                "supply_demand",
                f"d{i}",
                task_id="T",
                change_id=f"c{i}",
                plan_revision=i,
                rack_id="R",
                rack_face=face,
                cycle_id="cycle",
                service_id=f"s{i}",
                withdrawn=i == 3,
            )
            await put(
                "face_service",
                f"s{i}",
                cycle_id="cycle",
                source_ref=f"d{i}",
                rack_face=face,
                task_id="T",
                plan_revision=i,
                status="READY" if i == 3 else "COMPLETED",
                pending=i == 3,
            )
        await put(
            "supply_demand",
            "unselected",
            task_id="T",
            change_id="c4",
            plan_revision=4,
            rack_id="R",
            rack_face="A",
            withdrawn=True,
        )
        await put(
            "supply_demand",
            "new-demand",
            task_id="T",
            change_id="c5",
            plan_revision=5,
            rack_id="R",
            rack_face="A",
            withdrawn=False,
        )
        await put(
            "face_service",
            "d1",
            cycle_id="drain-cycle",
            source_ref="same:A",
            rack_face="A",
            status="COMPLETED",
            pending=False,
        )
        await put(
            "face_service",
            "wait-service",
            cycle_id="wait-cycle",
            source_ref="same:B",
            rack_face="B",
            status="COMPLETED",
            pending=False,
        )
        bins = [
            {
                "bin_code": f"A{i:09}",
                "source_locator": {"rack_id": "R", "rack_face": "A", "slot_id": str(i)},
            }
            for i in range(1, 6)
        ]
        for i in (1, 3):
            await put(
                "feed",
                f"f{i}",
                service_id=f"s{i}",
                caller_workline_id="W",
                target={"location_code": "IN"},
                operation_id=f"in-{i}",
                message_id=101 if i == 3 else 102,
                manifest=sa.JSON.NULL if i == 3 else {"result": "READY", "bins": bins},
                feed_complete=i == 1,
                pending=i != 1,
            )
        for i, kind, ref, cycle in (
            (1, "CTU01", "s1", "cycle"),
            (2, "CTU02", "s2", "cycle"),
            (3, "CTU02", "s3", "cycle"),
            (4, "CTU03", "accepted-depart", "drain-cycle"),
            (5, "CTU01", "d1", "drain-cycle"),
            (6, "CTU01", "wait-service", "wait-cycle"),
        ):
            await put(
                "rack_action",
                f"act{i}",
                cycle_id=cycle,
                kind=kind,
                source_ref=ref,
                client_request_id=f"client{i}",
                transport_task_id="original-ctu03" if i == 4 else f"tr{i}",
                operation_id=f"tr-op{i}",
                caller_workline_id="W",
                target={"kind": "ZONE", "location_code": "STORAGE"}
                if i == 4
                else {"kind": "RACK_POSITION", "location_code": "F"},
                target_face=None if i == 4 else ("B" if i in (2, 6) else "A"),
                status="ACCEPTED" if i == 4 else "SUCCEEDED",
                pending=False,
                acceptance_basis={"accepted": True} if i == 4 else None,
            )
        for rack, action, current in (("R", "act5", False), ("R2", "act6", True)):
            await put(
                "position_fact",
                "position-" + rack,
                object_kind="RACK",
                object_code=rack,
                location_code="F",
                rack_face="B" if current else "A",
                source_kind="RACK",
                source_id=action,
                source_revision=1,
                is_current=current,
            )
        for i in (1, 2):
            await put(
                "feed_segment",
                f"seg{i}",
                feed_id="f1",
                segment_index=i,
                members=bins[(i - 1) * 4 : i * 4],
                client_request_id=f"orig{i}",
                transport_task_id=f"bintr{i}",
                status="SUCCEEDED",
                pending=False,
                consumed_revision=i,
            )
            await put(
                "return_opportunity",
                f"opp{i}",
                service_id="s1",
                source_ref=f"SEGMENT:seg{i}",
                kind="SEGMENT" if i == 1 else "FACE_END",
                ended=i == 1,
                end_evidence={"original": i} if i == 1 else None,
            )
            await put(
                "return_batch",
                f"batch{i}",
                opportunity_id="opp2",
                source_ref=f"batch{i - 1}" if i > 1 else "opp2",
                operation_id=f"batch-op{i}",
                message_id=200 + i,
                members=[{"bin_code": "B", "round_id": "old" if i == 1 else "new"}],
                workline_code="W",
                caller_workline_id="W",
                source={"location_code": "OUT"},
                transport_task_id=f"return{i}",
                status="SUCCEEDED" if i == 1 else "PENDING",
                consumed_revision=1 if i == 1 else None,
                pending=i == 2,
            )
        await put(
            "drain",
            "previous-wait",
            input_kind="FIFO_OBSERVATION",
            input_ref="previous-wait",
            input_snapshot={"round_id": "old"},
            request=None,
            unused_racks=[],
            operation_id="drain-prior",
            decision={"result": "WAIT", "retry_after_ms": 1000},
            due_at=123456,
            status="WAIT",
            pending=False,
        )
        await put(
            "drain",
            "same",
            input_kind="WAIT",
            input_ref="previous-wait",
            input_snapshot=sa.JSON.NULL,
            request=None,
            unused_racks=[],
            operation_id="same-op",
            decision={
                "result": "READY",
                "racks": [
                    {"rack_id": "R", "rack_face": ["A"]},
                    {"rack_id": "R2", "rack_face": ["B"]},
                ],
            },
            status="READY",
            pending=True,
        )
        await put(
            "departure",
            "accepted-depart",
            cycle_id="drain-cycle",
            source_ref="d1",
            operation_id="accepted-decision",
            request={"rack_id": "R"},
            decision={
                "result": "READY",
                "rack_destination": {"type": "ZONE", "location_code": "STORAGE"},
            },
            status="READY",
            pending=False,
        )
        await put(
            "departure",
            "same",
            cycle_id="wait-cycle",
            source_ref="wait-service",
            operation_id="same-op",
            request={"frozen": "rack"},
            decision={"result": "WAIT"},
            due_at=987654321,
            status="WAIT",
            pending=True,
        )
        await c.execute(
            source["reliable_message"]
            .insert()
            .values(
                id=101,
                target="roller-migration",
                operation="outbound.bin.inbound_batch@v1",
                operation_id="in-3",
                request_raw=b"frozen",
                delivery_snapshot={"origin": "http://127.0.0.1:1"},
                status="HELD",
                attempts=1,
                not_received_count=0,
                delivery_uncertain=True,
                created_at=now,
                updated_at=now,
            )
        )
        await c.execute(
            source["ecs_command"]
            .insert()
            .values(
                command_code="cmd1",
                owner_domain=OWNER,
                call_key="frozen:1",
                call_key_digest="f" * 64,
                input_raw=b"original",
                device_code="SCAN2",
                task_type="MOVE_FORWARD",
                route={},
                request_raw=b"frozen ECS",
                target="roller-migration",
                delivery_snapshot={},
                expires_at=now + timedelta(seconds=300),
                status="UNKNOWN",
                delivery_status="UNKNOWN",
                blocked=True,
                needs_reconciliation=True,
                created_at=now,
            )
        )
        await c.execute(
            source["transport_task"]
            .insert()
            .values(
                client_request_id="client4",
                transport_task_id="original-ctu03",
                operation_id="tr-op4",
                kind="RACK_MOVE",
                input_digest="a" * 64,
                workline_id="W",
                owner_domain=OWNER,
                created_at=now,
                cycle_id="drain-cycle",
                frozen={"original": "transport"},
            )
        )


@pytest.fixture
async def populated_roller(state_migration):
    store = state_migration
    source = await tables(store)
    try:
        await populate(store, source)
        yield store, source
    finally:
        current = await tables(store)
        async with store.engine.begin() as c:
            for name, t in current.items():
                if name.startswith("bin_roller_"):
                    await c.execute(
                        t.delete().where(t.c.owner_domain.in_((OWNER, OWNER + "-other")))
                    )
            for name, column, identity in (
                ("reliable_message", "target", "roller-migration"),
                ("ecs_command", "command_code", "cmd1"),
                ("transport_task", "transport_task_id", "original-ctu03"),
            ):
                t = source[name]
                await c.execute(t.delete().where(t.c[column] == identity))


@pytest.mark.parametrize("state_migration", MIGRATIONS, indirect=True)
async def test_original_pending_and_frozen_effects_survive_round_trip(populated_roller):
    store, old = populated_roller
    baseline = await snapshot(store, old)
    frozen = await snapshot(store, old, platform=True)
    schema = await store.schema_snapshot("bin_roller_")
    assert len(schema) == 15
    await store.migrate("upgrade", NEW)
    new = await tables(store)
    assert len(await store.schema_snapshot("bin_roller_")) == 10
    assert len(await store.schema_snapshot("smt_manual_")) == 7
    assert await snapshot(store, new, platform=True) == frozen
    roller = BinRoller(
        CommandOwner(OWNER),
        SimpleNamespace(ecs=EcsCommands(None, None)),
        SimpleNamespace(),
        tables={
            k.removeprefix("bin_roller_"): v for k, v in new.items() if k.startswith("bin_roller_")
        },
    )
    async with async_sessionmaker(store.engine)() as session:
        for kind, expect in (
            ("SCAN2_RELEASE", ["a-release", "z-release"]),
            ("FEED", ["d2", "d3", "new-demand"]),
            ("DRAIN", ["same"]),
        ):
            upper = await roller.get_pending_upper_id(session, kind=kind)
            assert upper == expect[-1]
            page = await roller.list_pending(
                session, kind=kind, after_id=None, upper_id=upper, limit=1
            )
            tail = await roller.list_pending(
                session, kind=kind, after_id=page[0].record_id, upper_id=upper, limit=10
            )
            assert [w.record_id for w in (*page, *tail)] == expect
        assert (
            await roller.advance(session, RollerWorkRef("SCAN2_RELEASE", "z-release")) == "WAITING"
        )
        original = await roller.get_scan2_release_result(session, arrival_id="a1")
        assert original.command_code == "cmd1" and original.status == "UNKNOWN"
        assert (await roller.original_record(session, "departure", "same"))["due_at"] == 987654321
        assert (await roller.original_record(session, "drain", "same"))["status"] == "READY"
        assert (await roller.original_record(session, "drain", "previous-wait"))["due_at"] == 123456
        assert (await roller.repository(1).position_at(session, "RACK", "F"))["object_code"] == "R2"
        assert (await roller.original_record(session, "face_service", "d1"))["purpose"] == "DRAIN"
        assert (await roller.original_record(session, "supply_demand", "d1"))["purpose"] == "SUPPLY"
        progress = await roller.get_supply_progress(session, work_line_id=1, task_id="T")
        assert [
            (p.feed_complete, p.pending)
            for p in sorted(progress, key=lambda p: p.source.plan_revision)
        ] == [(True, False), (False, True), (False, True), (False, False), (False, True)]
        assert {"d3", "s3", "f3", "101", "in-3", "cycle"} <= set(
            next(p for p in progress if p.source.plan_revision == 3).original_ids
        )
    await store.migrate("downgrade", OLD)
    restored = await tables(store)
    assert await store.schema_snapshot("bin_roller_") == schema
    assert await snapshot(store, restored) == baseline
    assert await snapshot(store, restored, platform=True) == frozen
    await store.migrate("upgrade", NEW)
    # An object first created by the compact runtime can also return to the old code.
    async with async_sessionmaker(store.engine).begin() as session:
        await roller.apply_supply_change(
            session, SupplyChange(2, "new-task", "changed", (SourceFaceRef(6, "R2", "B"),), ())
        )
        entered = (await roller.get_supply_progress(session, work_line_id=2, task_id="new-task"))[0]
        assert entered.pending and not entered.feed_complete
    await store.migrate("downgrade", OLD)
    old_demand = (await snapshot(store, old))["bin_roller_supply_demand"]
    saved = next(r for r in old_demand if r["task_id"] == "new-task")
    assert (saved["id"], saved["work_line_id"], saved["service_id"], saved["withdrawn"]) == (
        entered.original_ids[0],
        2,
        None,
        False,
    )
    await store.migrate("upgrade", NEW)


@pytest.mark.parametrize("state_migration", MIGRATIONS, indirect=True)
@pytest.mark.parametrize(
    "key, original, values, identity",
    [
        pytest.param("supply_demand", "d3", dict(service_id=None), "s3", id="unmatched-service"),
        pytest.param(
            "drain",
            "same",
            dict(owner_domain=OWNER + "-other"),
            "drain-cycle",
            id="wrong-drain-owner",
        ),
        pytest.param(
            "rack_action", "act4", dict(cycle_id="cycle"), "act4", id="wrong-departure-cycle"
        ),
        pytest.param(
            "return_round", "old", dict(scan3_arrival_id="missing"), "old", id="orphan-round"
        ),
        pytest.param(
            "return_opportunity",
            "opp2",
            dict(source_ref="SEGMENT:missing"),
            "opp2",
            id="opportunity-predecessor",
        ),
        pytest.param(
            "return_batch", "batch2", dict(source_ref="missing"), "batch2", id="batch-predecessor"
        ),
        pytest.param(
            "drain",
            "same",
            dict(input_snapshot={"round_id": "missing"}),
            "same",
            id="drain-predecessor",
        ),
    ],
)
async def test_orphan_identity_aborts_before_schema_switch(
    populated_roller, key, original, values, identity
):
    store, source = populated_roller
    table = source["bin_roller_" + key]
    async with store.engine.begin() as c:
        await c.execute(table.update().where(table.c.id == original).values(**values))
    schema = await store.schema_snapshot("bin_roller_")
    baseline = await snapshot(store, source)
    frozen = await snapshot(store, source, platform=True)
    with pytest.raises(AssertionError, match=identity):
        await store.migrate("upgrade", NEW)
    assert await store.schema_snapshot("bin_roller_") == schema
    assert await snapshot(store, source) == baseline
    assert await snapshot(store, source, platform=True) == frozen


@pytest.mark.parametrize("state_migration", MIGRATIONS, indirect=True)
async def test_downgrade_refuses_unrepresentable_drain_face_before_switch(populated_roller):
    store, _ = populated_roller
    await store.migrate("upgrade", NEW)
    new = await tables(store)
    face = new["bin_roller_face_work"]
    async with store.engine.begin() as c:
        await c.execute(
            face.update()
            .where(face.c.purpose == "DRAIN", face.c.service_id == "d1")
            .values(rack_id="different-rack")
        )
    baseline = await snapshot(store, new)
    schema = await store.schema_snapshot("bin_roller_")
    with pytest.raises(AssertionError, match="d1"):
        await store.migrate("downgrade", OLD)
    assert await store.schema_snapshot("bin_roller_") == schema
    assert await snapshot(store, new) == baseline


@pytest.fixture(params=("upgrade", "downgrade"))
async def migration_side(populated_roller, request):
    store, old = populated_roller
    if request.param == "downgrade":
        await store.migrate("upgrade", NEW)
        return store, await tables(store), True
    return store, old, False


async def assert_history_rejected(side, ids):
    store, source, compact = side
    schema = await store.schema_snapshot("bin_roller_")
    business = await snapshot(store, source)
    frozen = await snapshot(store, source, platform=True)
    revision = OLD if compact else NEW
    with pytest.raises(AssertionError) as failure:
        await store.migrate("downgrade" if compact else "upgrade", revision)
    assert any(
        line.strip().startswith("ValueError: BIN_Roller migration: invalid ")
        and line.strip().split(" for original ID ")[-1] in ids
        for line in str(failure.value).splitlines()
    ), str(failure.value)
    assert "private-test-evidence" not in str(failure.value)
    assert await store.schema_snapshot("bin_roller_") == schema
    assert await snapshot(store, source) == business
    assert await snapshot(store, source, platform=True) == frozen
    async with store.engine.connect() as c:
        assert await c.scalar(sa.select(source["alembic_version"].c.version_num)) == (
            NEW if compact else OLD
        )


@pytest.mark.parametrize("state_migration", MIGRATIONS, indirect=True)
@pytest.mark.parametrize("first", [None, "a5"], ids=["missing", "later-member"])
async def test_bound_scan4_requires_earliest_bound_arrival(migration_side, first):
    store, source, compact = migration_side
    t = source["bin_roller_scan_arrival" if compact else "bin_roller_return_round"]
    async with store.engine.begin() as c:
        await c.execute(
            t.update()
            .where(t.c.id == ("a3" if compact else "old"))
            .values({("first_scan4_arrival_id" if compact else "scan4_arrival_id"): first})
        )
    await assert_history_rejected(migration_side, {"old"})


@pytest.mark.parametrize("state_migration", MIGRATIONS, indirect=True)
@pytest.mark.parametrize("chain", ["scan", "drain", "departure", "batch", "unknown-drain-root"])
async def test_predecessor_must_reach_its_original_root(migration_side, chain):
    store, source, compact = migration_side
    async with store.engine.begin() as c:
        if chain == "scan":
            t = source["bin_roller_scan_arrival"]
            await c.execute(t.update().where(t.c.id == "a1").values(previous_arrival_id="a1"))
            ids = {"a1"}
        elif chain in {"drain", "unknown-drain-root"}:
            t = source["bin_roller_decision" if compact else "bin_roller_drain"]
            q = t.update().where(t.c.id == "previous-wait")
            if compact:
                q = q.where(t.c.kind == "DRAIN")
            await c.execute(
                q.values(
                    input_kind="WAIT" if chain == "drain" else "FIFO_OBSERVATION",
                    input_ref="same" if chain == "drain" else "private-test-evidence",
                )
            )
            ids = {"previous-wait", "same"}
        elif chain == "departure":
            t = source["bin_roller_decision" if compact else "bin_roller_departure"]
            q = t.update().where(t.c.id == "accepted-depart")
            if compact:
                q = q.where(t.c.kind == "DEPARTURE")
            await c.execute(q.values(source_ref="accepted-depart"))
            ids = {"accepted-depart"}
        else:
            t = source["bin_roller_return_batch"]
            await c.execute(t.update().where(t.c.id == "batch1").values(source_ref="batch2"))
            ids = {"batch1", "batch2"}
    await assert_history_rejected(migration_side, ids)


@pytest.mark.parametrize("state_migration", MIGRATIONS, indirect=True)
@pytest.mark.parametrize(
    "damage", ["wrong-first-fact", "missing-rack-action", "foreign-scan-arrival"]
)
async def test_first_scan_and_position_keep_their_own_source(migration_side, damage):
    store, source, _ = migration_side
    async with store.engine.begin() as c:
        if damage == "wrong-first-fact":
            t = source["bin_roller_scan_arrival"]
            await c.execute(t.update().where(t.c.id == "a1").values(first_digest="d6"))
            ids = {"a1"}
        elif damage == "missing-rack-action":
            t = source["bin_roller_position_fact"]
            await c.execute(
                t.update().where(t.c.id == "position-R2").values(source_id="private-test-evidence")
            )
            ids = {"position-R2"}
        else:
            t = source["bin_roller_position_fact"]
            await c.execute(
                t.insert().values(
                    id="bad-scan-position",
                    owner_domain=OWNER,
                    work_line_id=2,
                    object_kind="BIN",
                    object_code="B",
                    location_code="SCAN4",
                    source_kind="SCAN",
                    source_id="a4",
                    source_revision=1,
                    is_current=False,
                )
            )
            ids = {"bad-scan-position"}
    await assert_history_rejected(migration_side, ids)


@pytest.mark.parametrize("state_migration", MIGRATIONS, indirect=True)
@pytest.mark.parametrize("missing", ["command", "result"])
async def test_consumed_release_needs_stored_command_and_result(migration_side, missing):
    store, source, compact = migration_side
    command = None if missing == "command" else "cmd1"
    result = None if missing == "result" else "saved-result"
    async with store.engine.begin() as c:
        if compact:
            # Seed corrupt history independently of the compact write constraint.
            t = source["bin_roller_scan_arrival"]
            constraint = "bin_roller_scan_arrival_release_check"
            if store.backend == "postgres":
                definition = await c.scalar(
                    sa.text(
                        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conrelid='enterprise_backend.bin_roller_scan_arrival'::regclass "
                        "AND conname=:name"
                    ),
                    dict(name=constraint),
                )
                await c.exec_driver_sql(f"ALTER TABLE {t.fullname} DROP CONSTRAINT {constraint}")
            else:
                await c.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            await c.execute(
                t.update()
                .where(t.c.id == "a1")
                .values(
                    release_state="CONSUMED",
                    release_command_code=command,
                    release_consumed_result_id=result,
                )
            )
            if store.backend == "postgres":
                await c.exec_driver_sql(
                    f"ALTER TABLE {t.fullname} ADD CONSTRAINT {constraint} {definition} NOT VALID"
                )
            else:
                await c.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
        else:
            t = source["bin_roller_scan2_release"]
            await c.execute(
                t.update()
                .where(t.c.id == "z-release")
                .values(pending=False, command_code=command, consumed_result_id=result)
            )
    try:
        await assert_history_rejected(migration_side, {"z-release"})
    finally:
        if compact and store.backend == "postgres":
            # Restore validation after checking the intentionally invalid historical row.
            async with store.engine.begin() as c:
                await c.execute(
                    t.update()
                    .where(t.c.id == "a1")
                    .values(
                        release_state="PENDING",
                        release_command_code="cmd1",
                        release_consumed_result_id=None,
                    )
                )
                await c.exec_driver_sql(
                    f"ALTER TABLE {t.fullname} VALIDATE CONSTRAINT {constraint}"
                )


@pytest.mark.parametrize("state_migration", MIGRATIONS, indirect=True)
async def test_compact_write_rejects_unsubstantiated_consumed_release(populated_roller):
    store, _ = populated_roller
    await store.migrate("upgrade", NEW)
    t = (await tables(store))["bin_roller_scan_arrival"]
    async with store.engine.begin() as c:
        for command, result in (
            (None, "saved-result"),
            ("cmd1", None),
            ("", "saved-result"),
            ("cmd1", ""),
        ):
            with pytest.raises(sa.exc.IntegrityError):
                async with c.begin_nested():
                    await c.execute(
                        t.update()
                        .where(t.c.id == "a1")
                        .values(
                            release_state="CONSUMED",
                            release_command_code=command,
                            release_consumed_result_id=result,
                        )
                    )
        original = (await c.execute(sa.select(t).where(t.c.id == "a1"))).mappings().one()
        assert (
            original["release_record_id"] == "z-release" and original["release_state"] == "PENDING"
        )


@pytest.mark.parametrize("state_migration", MIGRATIONS, indirect=True)
async def test_typed_roots_results_and_observed_mismatches_round_trip(populated_roller):
    store, old = populated_roller
    async with store.engine.begin() as c:
        dep = old["bin_roller_departure"]
        # An initial departure may share its string identity with its own cycle or face.
        await c.execute(
            dep.update()
            .where(dep.c.id == "accepted-depart")
            .values(id="drain-cycle", source_ref="drain-cycle")
        )
        await c.execute(dep.update().where(dep.c.id == "same").values(id="wait-service"))
        action = old["bin_roller_rack_action"]
        await c.execute(
            action.update().where(action.c.id == "act4").values(source_ref="drain-cycle")
        )
        await c.execute(action.update().where(action.c.id == "act6").values(status="FAILED"))
        pos = old["bin_roller_position_fact"]
        await c.execute(
            pos.update()
            .where(pos.c.id == "position-R2")
            .values(location_code="OTHER", rack_face="A")
        )
        await c.execute(
            pos.insert().values(
                id="scan-history",
                owner_domain=OWNER,
                work_line_id=1,
                object_kind="BIN",
                object_code="B",
                location_code="SCAN4",
                source_kind="SCAN",
                source_id="a4",
                source_revision=0,
                is_current=False,
            )
        )
        release = old["bin_roller_scan2_release"]
        await c.execute(
            release.update()
            .where(release.c.id == "a-release")
            .values(pending=False, consumed_result_id="saved-result")
        )
        a = old["bin_roller_scan_arrival"]
        first_time = await c.scalar(sa.select(a.c.received_at).where(a.c.id == "a4"))
        await c.execute(
            a.update()
            .where(a.c.id == "a5")
            .values(received_at=first_time, previous_arrival_id="a4")
        )
    business = await snapshot(store, old)
    frozen = await snapshot(store, old, platform=True)
    await store.migrate("upgrade", NEW)
    compact = await tables(store)
    async with store.engine.connect() as c:
        a = compact["bin_roller_scan_arrival"]
        assert await c.scalar(sa.select(a.c.first_scan4_arrival_id).where(a.c.id == "a3")) == "a4"
        saved = (await c.execute(sa.select(a).where(a.c.id == "a2"))).mappings().one()
        assert (
            saved["release_state"],
            saved["release_command_code"],
            saved["release_consumed_result_id"],
        ) == ("CONSUMED", "cmd2", "saved-result")
        assert await c.scalar(sa.select(a.c.release_state).where(a.c.id == "a1")) == "PENDING"
    await store.migrate("downgrade", OLD)
    assert await snapshot(store, old) == business
    assert await snapshot(store, old, platform=True) == frozen
