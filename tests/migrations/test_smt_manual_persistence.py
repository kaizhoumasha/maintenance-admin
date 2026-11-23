"""Populated conversion keeps each original input, request and unresolved obligation."""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

OLD = "0177313880f9"
NEW = "smtmanualcompact"
OPERATION_RENAME = "smtmanualoprename"
BIN_NOTICE = "manual_bin.work_completed@v1"
RACK_NOTICE = "outbound.manual_rack.direct_pick_completed@v1"
PLAN = "sorting.task.plan_delta@v1"


async def tables(store):
    async with store.engine.connect() as connection:

        def reflect(sync):
            schema = "enterprise_backend" if sync.dialect.name == "postgresql" else None
            metadata = sa.MetaData(schema=schema)
            metadata.reflect(sync)
            # Reflection loses JSON's Python None policy; copied SQL NULL must stay SQL NULL.
            for table in metadata.tables.values():
                for column in table.c:
                    if isinstance(column.type, sa.JSON):
                        column.type.none_as_null = True
            return {table.name: table for table in metadata.tables.values()}

        return await connection.run_sync(reflect)


async def snapshot(store, source):
    async with store.engine.connect() as connection:
        return {
            name: [
                dict(row)
                for row in (
                    await connection.execute(sa.select(table).order_by(*table.primary_key.columns))
                ).mappings()
            ]
            for name, table in source.items()
            if name.startswith("smt_manual_")
        }


async def platform_snapshot(store, source):
    async with store.engine.connect() as c:
        return {
            name: [
                dict(row)
                for row in (
                    await c.execute(
                        sa.select(source[name]).order_by(*source[name].primary_key.columns)
                    )
                ).mappings()
            ]
            for name in ("reliable_message", "ecs_command", "transport_task")
        }


@pytest.mark.parametrize(
    "state_migration",
    [
        ("SMT_MANUAL_MIGRATION_DATABASE_URL", backend, "rollerfour")
        for backend in ("sqlite", "postgres")
    ],
    indirect=True,
)
async def test_operation_rename_migrates_existing_inputs_and_supports_new_identity(state_migration):
    store = state_migration
    source = await tables(store)
    inputs = source["smt_manual_input"]
    legacy = {
        "operation": "outbound.manual_rack.direct_pick_completed@v1",
        "operation_id": "legacy-rack-completion",
        "receipt_id": 1,
        "task_id": "T1",
        "work_line_id": 1,
        "timestamp": 1,
        "payload": {"task_id": "T1", "rack_id": "R1", "rack_face": "A"},
        "state": "APPLIED",
        "reason": None,
        "is_plan_record": False,
        "plan_revision": 1,
        "rack_id": "R1",
        "rack_face": "A",
        "bin_code": None,
        "arrival_id": None,
        "completed_at": 1,
        "completion_result": None,
        "original_operation_id": None,
    }
    async with store.engine.begin() as connection:
        await connection.execute(inputs.insert().values(**legacy))

    await store.migrate("upgrade", OPERATION_RENAME)
    current = await tables(store)
    async with store.engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    sa.select(current["smt_manual_input"]).where(
                        current["smt_manual_input"].c.operation_id == legacy["operation_id"]
                    )
                )
            )
            .mappings()
            .one()
        )
        assert row["operation"] == "manual_rack.direct_pick_completed@v1"

        await connection.commit()

    new_row = dict(legacy, operation="manual_rack.direct_pick_completed@v1", operation_id="new")
    async with store.engine.begin() as connection:
        await connection.execute(current["smt_manual_input"].insert().values(**new_row))

    await store.migrate("downgrade", "rollerfour")
    restored = await tables(store)
    async with store.engine.connect() as connection:
        operations = await connection.scalars(
            sa.select(restored["smt_manual_input"].c.operation).order_by(
                restored["smt_manual_input"].c.operation_id
            )
        )
        assert list(operations) == [
            "outbound.manual_rack.direct_pick_completed@v1",
            "outbound.manual_rack.direct_pick_completed@v1",
        ]


async def cleanup_platform(store):
    source = await tables(store)
    async with store.engine.begin() as c:
        await c.execute(
            source["reliable_message"]
            .delete()
            .where(source["reliable_message"].c.target == "smt-compact-migration")
        )
        await c.execute(
            source["ecs_command"]
            .delete()
            .where(source["ecs_command"].c.command_code == "original-command")
        )
        await c.execute(
            source["transport_task"]
            .delete()
            .where(source["transport_task"].c.transport_task_id == "original-transport")
        )


async def populate(store):
    source = await tables(store)
    await cleanup_platform(store)
    async with store.engine.begin() as c:
        now = datetime(2026, 11, 2, tzinfo=UTC)
        for message_id, operation_id in (
            (101, "prepare1"),
            (102, "prepare2"),
            (103, "prepare3"),
            (201, "confirm1"),
            (202, "confirm2"),
            (203, "confirm3"),
            (301, "admit"),
            (501, "report"),
            (601, "departure"),
        ):
            await c.execute(
                source["reliable_message"]
                .insert()
                .values(
                    id=message_id,
                    target="smt-compact-migration",
                    operation="original.operation",
                    operation_id=operation_id,
                    request_raw=b"original frozen message bytes",
                    delivery_snapshot={"origin": "http://127.0.0.1:1"},
                    status="HELD",
                    attempts=1,
                    not_received_count=0,
                    delivery_uncertain=True,
                    next_attempt_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
        await c.execute(
            source["ecs_command"]
            .insert()
            .values(
                command_code="original-command",
                owner_domain="smt_manual",
                call_key="original-call",
                call_key_digest="a" * 64,
                input_raw=b"original input",
                device_code="SCAN2",
                task_type="MOVE_FORWARD",
                route={"frozen": True},
                request_raw=b"original frozen ECS bytes",
                target="smt-compact-migration",
                delivery_snapshot={"frozen": True},
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
                client_request_id="original-client",
                transport_task_id="original-transport",
                operation_id="original-transport-op",
                kind="RACK_MOVE",
                input_digest="b" * 64,
                workline_id="-1",
                owner_domain="smt_manual",
                created_at=now,
                cycle_id="rack",
                frozen={"original": "frozen transport"},
                pending_outcome={"original": "pending"},
            )
        )

        async def put(name, **values):
            await c.execute(source["smt_manual_" + name].insert().values(**values))

        for n, status in enumerate(("EXECUTING", "COMPLETED", "QUEUED"), 1):
            await put(
                "task",
                task_id=f"T{n}",
                task_type="MANUAL",
                business_type="outbound",
                work_line_id=-n,
                workline_code=f"LINE{n}",
                issued_operation_id=f"issue{n}",
                issued_timestamp=10,
                issued_data={"original": n},
                queue_revision=1,
                dispatch_sequence=n,
                status=status,
                plan_revision=1 if n == 1 else 0,
                prepare_operation_id=f"prepare{n}",
                prepare_message_id=100 + n,
                prepare_accepted=n != 3,
                prepare_response_timestamp=12 if n != 3 else None,
                prepare_accepted_at_ms=20 if n != 3 else None,
                confirmation_operation_id=f"confirm{n}",
                received_work=[["SCAN2", f"A{n}"]],
            )
            await put(
                "task_confirmation",
                operation_id=f"confirm{n}",
                task_id=f"T{n}",
                message_id=200 + n,
                plan_revision=1,
                basis={"original": n},
                state=("BUSINESS_IN_PROGRESS", "COMPLETED", "PENDING")[n - 1],
                response={
                    "code": "DECIDED",
                    "timestamp": 30,
                    "data": {"result": "BUSINESS_IN_PROGRESS", "retry_after_ms": 999},
                }
                if n == 1
                else {"code": "DECIDED", "timestamp": 31, "data": {"result": "COMPLETED"}},
                wait_until_ms=1029 if n == 1 else None,
            )
        plan = {"task_id": "T1", "plan_revision": 1, "added_bin_source_racks": []}
        for op, data, applied, reason in (
            ("plan-original", plan, True, None),
            ("plan-repeat", plan, True, None),
            (
                "plan-conflict",
                {**plan, "added_bin_source_racks": [{"rack_id": "other"}]},
                False,
                None,
            ),
            ("plan-future", {**plan, "plan_revision": 3}, False, "MISSING_REVISION"),
        ):
            await put(
                "task_input",
                operation=PLAN,
                operation_id=op,
                task_id="T1",
                timestamp=10,
                data=data,
                applied=applied,
                unapplied_reason=reason,
            )
        await put(
            "task_plan", task_id="T1", plan_revision=1, operation_id="plan-original", data=plan
        )
        await put(
            "task_member",
            id="member",
            task_id="T1",
            plan_revision=1,
            kind="DIRECT",
            rack_id="R",
            rack_face="A",
            slot_id="S",
            member_order=0,
            status="PENDING",
            rack_work_id="rack",
        )
        await put(
            "bin",
            arrival_id="A1",
            work_line_id=-1,
            device_code="SCAN2",
            raw_bin_code="B-C",
            bin_code="B",
            scanned_at=5,
            task_id="T1",
            state="WAIT",
            pending=True,
            admission_operation_id="admit",
            command_code="original-command",
        )
        await put(
            "admission",
            operation_id="admit",
            arrival_id="A1",
            message_id=301,
            response={
                "code": "DECIDED",
                "timestamp": 8,
                "data": {"result": "WAIT", "retry_after_ms": 900},
            },
            saved_at_ms=10,
            wait_until_ms=910,
        )
        for op, state, previous in (
            ("notice", "REVIEW", None),
            ("notice-repeat", "DUPLICATE", "notice"),
        ):
            await put(
                "completion",
                operation_id=op,
                receipt_id=401,
                work_line_id=-1,
                task_id="T1",
                bin_code="B",
                result="NORMAL",
                completed_at=6,
                timestamp=7,
                data={"task_id": "T1", "bin_code": "B", "result": "NORMAL", "completed_at": 6},
                state=state,
                arrival_id="A1",
                reason="RESULT_CONFLICT",
                original_operation_id=previous,
            )
        await put(
            "rack_completion",
            operation_id="early",
            operation=RACK_NOTICE,
            receipt_id=402,
            task_id="T1",
            rack_id="R",
            plan_revision=4,
            rack_face="A",
            completed_at=9,
            timestamp=10,
            data={
                "task_id": "T1",
                "rack_id": "R",
                "plan_revision": 4,
                "rack_face": "A",
                "completed_at": 9,
            },
            applied=False,
        )
        await put(
            "rack",
            id="rack",
            task_id="T1",
            work_line_id=-1,
            workline_code="LINE1",
            rack_id="R",
            role="RETURN",
            location_code="RETURN",
            initial_face="A",
            caller_workline_id="-1",
            state="ACTIVE",
            pending=True,
        )
        await put(
            "rack_action",
            id="action",
            rack_work_id="rack",
            kind="ENTER",
            source_ref="rack",
            client_request_id="original-client",
            transport_task_id="original-transport",
            target={"type": "RACK_POSITION", "location_code": "RETURN"},
            target_face="A",
            state="PENDING",
        )
        await put(
            "rack_report",
            operation_id="report",
            rack_work_id="rack",
            action_id="action",
            message_id=501,
            recorded=False,
        )
        await put(
            "rack_departure",
            operation_id="departure",
            rack_work_id="rack",
            message_id=601,
            request={"current_location": {"type": "RACK_POSITION", "location_code": "RETURN"}},
            basis={"action_id": "action", "revision": 1, "task_status": "EXECUTING"},
            decision={"result": "WAIT", "retry_after_ms": 500},
            wait_until_ms=1500,
        )
    return source


@pytest.mark.parametrize(
    "state_migration",
    [("SMT_MANUAL_MIGRATION_DATABASE_URL", b, OLD) for b in ("sqlite", "postgres")],
    indirect=True,
)
async def test_populated_convergence_preserves_originals_and_reverses(state_migration):
    store = state_migration
    source = await populate(store)
    original_schema = await store.schema_snapshot("smt_manual_")
    assert len(original_schema) == 13
    before = await snapshot(store, source)
    frozen = await platform_snapshot(store, source)
    await store.migrate("upgrade", NEW)
    current = await tables(store)
    assert len([n for n in current if n.startswith("smt_manual_")]) == 7
    assert len([n for n in current if n.startswith(("smt_manual_", "bin_roller_"))]) == 22
    assert "smt_manual_input" in current and "smt_manual_request" in current
    assert await platform_snapshot(store, current) == frozen
    async with store.engine.connect() as c:
        inputs = [
            dict(r) for r in (await c.execute(sa.select(current["smt_manual_input"]))).mappings()
        ]
        assert len(inputs) == 7
        assert [r["operation_id"] for r in inputs if r["is_plan_record"]] == ["plan-original"]
        assert next(r for r in inputs if r["operation_id"] == "plan-future")["state"] == "DEFERRED"
        assert next(r for r in inputs if r["operation_id"] == "notice")["state"] == "REVIEW"
        assert next(r for r in inputs if r["operation_id"] == "early")["state"] == "PENDING"
        requests = {
            r["operation_id"]: dict(r)
            for r in (await c.execute(sa.select(current["smt_manual_request"]))).mappings()
        }
        assert len(requests) == 9
        assert requests["admit"]["due_at_ms"] == 910
        assert requests["admit"]["request_snapshot"] is None
        assert requests["confirm1"]["basis"] == {"original": 1}
        assert requests["confirm1"]["due_at_ms"] == 1029
        assert requests["confirm2"]["state"] == "CONSUMED"
        assert requests["confirm2"]["response"] == {
            "code": "DECIDED",
            "timestamp": 31,
            "data": {"result": "COMPLETED"},
        }
        assert requests["confirm2"]["basis"] == {"original": 2}
        assert requests["confirm3"]["state"] == "REQUESTED"
        assert requests["confirm3"]["decision_code"] == "COMPLETED"
        assert requests["confirm3"]["response"] == requests["confirm2"]["response"]
        assert requests["confirm3"]["basis"] == {"original": 3}
        assert requests["departure"]["due_at_ms"] == 1500
        assert requests["prepare1"]["request_snapshot"] is None
        assert requests["report"]["action_id"] == "action"
        for old in ("task_member", "bin", "rack_action"):
            table = current["smt_manual_" + old]
            rows = [dict(r) for r in (await c.execute(sa.select(table))).mappings()]
            assert rows == before["smt_manual_" + old]
    await store.migrate("downgrade", OLD)
    assert await store.schema_snapshot("smt_manual_") == original_schema
    assert await snapshot(store, await tables(store)) == before
    assert await platform_snapshot(store, await tables(store)) == frozen


@pytest.mark.parametrize(
    "state_migration",
    [("SMT_MANUAL_MIGRATION_DATABASE_URL", b, OLD) for b in ("sqlite", "postgres")],
    indirect=True,
)
@pytest.mark.parametrize(
    "broken",
    [
        "plan",
        "plan_payload",
        "missing_plan",
        "task_plan",
        "member_plan",
        "completion_original",
        "completion_subject",
        "bin_completion",
        "bin_completion_subject",
        "admission",
        "report",
        "duplicate_report",
    ],
)
async def test_orphaned_originals_fail_without_replacing_old_tables(state_migration, broken):
    store = state_migration
    source = await populate(store)
    async with store.engine.begin() as c:
        if broken == "missing_plan":
            await c.execute(source["smt_manual_task_plan"].delete())
        elif broken == "task_plan":
            await c.execute(
                source["smt_manual_task"]
                .update()
                .where(source["smt_manual_task"].c.task_id == "T1")
                .values(plan_revision=5)
            )
        elif broken == "member_plan":
            await c.execute(source["smt_manual_task_member"].update().values(plan_revision=2))
        elif broken == "completion_original":
            await c.execute(
                source["smt_manual_completion"]
                .update()
                .where(source["smt_manual_completion"].c.operation_id == "notice-repeat")
                .values(original_operation_id="missing")
            )
        elif broken == "completion_subject":
            await c.execute(
                source["smt_manual_completion"]
                .update()
                .where(source["smt_manual_completion"].c.operation_id == "notice-repeat")
                .values(task_id="T2", work_line_id=-2, arrival_id=None)
            )
        elif broken in {"bin_completion", "bin_completion_subject"}:
            await c.execute(
                source["smt_manual_bin"]
                .update()
                .values(
                    completed_operation_id="missing"
                    if broken == "bin_completion"
                    else "notice-repeat",
                    completed_result="NORMAL",
                )
            )
        elif broken == "plan":
            await c.execute(source["smt_manual_task_plan"].update().values(operation_id="missing"))
        elif broken == "plan_payload":
            await c.execute(source["smt_manual_task_plan"].update().values(data={"conflict": True}))
        elif broken == "duplicate_report":
            await c.execute(
                source["smt_manual_rack_report"]
                .insert()
                .values(
                    operation_id="duplicate-report",
                    rack_work_id="rack",
                    action_id="action",
                    message_id=502,
                    recorded=False,
                )
            )
        elif broken == "admission":
            await c.execute(source["smt_manual_admission"].update().values(arrival_id="missing"))
        else:
            await c.execute(source["smt_manual_rack_report"].update().values(action_id="missing"))
    before = await snapshot(store, source)
    original_schema = await store.schema_snapshot("smt_manual_")
    with pytest.raises(AssertionError) as failure:
        await store.migrate("upgrade", NEW)
    diagnostic = str(failure.value)
    assert "SMT persistence: invalid" in diagnostic
    assert "INSERT INTO" not in diagnostic and "current_location" not in diagnostic
    if broken == "duplicate_report":
        assert "arrival report action" in diagnostic and "action" in diagnostic
    assert await snapshot(store, await tables(store)) == before
    assert await store.schema_snapshot("smt_manual_") == original_schema


@pytest.mark.parametrize(
    "state_migration",
    [("SMT_MANUAL_MIGRATION_DATABASE_URL", b, OLD) for b in ("sqlite", "postgres")],
    indirect=True,
)
async def test_original_plan_and_request_subject_constraints(state_migration):
    from sqlalchemy.exc import IntegrityError

    store = state_migration
    await store.migrate("downgrade", OLD)
    await populate(store)
    await store.migrate("upgrade", NEW)
    current = await tables(store)
    async with store.engine.begin() as c:
        inputs, requests = current["smt_manual_input"], current["smt_manual_request"]
        original = dict(
            (await c.execute(sa.select(inputs).where(inputs.c.is_plan_record))).mappings().one()
        )
        admission = dict(
            (await c.execute(sa.select(requests).where(requests.c.kind == "BIN_ADMISSION")))
            .mappings()
            .one()
        )
        invalid = [
            (inputs, original | {"operation_id": "second-original"}),
            (inputs, original | {"operation_id": "wrong-operation", "operation": BIN_NOTICE}),
            (requests, admission | {"operation_id": "wrong-kind", "kind": "PREPARE"}),
            (requests, admission | {"operation_id": "wrong-subject", "arrival_id": None}),
            (requests, admission | {"operation_id": "wrong-action", "action_id": "action"}),
            (requests, admission | {"operation_id": "wrong-revision", "plan_revision": 1}),
            (requests, admission | {"operation_id": "wrong-basis", "basis": {"foreign": True}}),
            (requests, admission | {"operation_id": "missing-deadline", "due_at_ms": None}),
        ]
        notice = dict(
            (await c.execute(sa.select(inputs).where(inputs.c.operation_id == "notice")))
            .mappings()
            .one()
        )
        rack_notice = dict(
            (await c.execute(sa.select(inputs).where(inputs.c.operation_id == "early")))
            .mappings()
            .one()
        )
        departure = dict(
            (await c.execute(sa.select(requests).where(requests.c.kind == "RACK_DEPARTURE")))
            .mappings()
            .one()
        )
        for index, changes in enumerate(
            (
                {"receipt_id": None},
                {"bin_code": None},
                {"completed_at": None},
                {"completion_result": None},
                {"rack_face": "A"},
                {"plan_revision": 1},
                {"state": "DEFERRED"},
            )
        ):
            invalid.append((inputs, notice | {"operation_id": f"invalid-bin-{index}"} | changes))
        for index, changes in enumerate(
            (
                {"receipt_id": None},
                {"rack_id": None},
                {"rack_face": None},
                {"completed_at": None},
                {"plan_revision": None},
                {"bin_code": "B"},
                {"operation": "outbound.return_rack.task_completed@v1"},
            )
        ):
            invalid.append(
                (inputs, rack_notice | {"operation_id": f"invalid-rack-{index}"} | changes)
            )
        invalid.append((inputs, original | {"operation_id": "task-with-bin", "bin_code": "B"}))
        invalid.append(
            (
                requests,
                departure
                | {"operation_id": "departure-without-snapshot", "request_snapshot": None},
            )
        )
        confirmation = dict(
            (await c.execute(sa.select(requests).where(requests.c.operation_id == "confirm2")))
            .mappings()
            .one()
        )
        invalid.append(
            (
                requests,
                confirmation | {"operation_id": "consumed-without-decision", "decision_code": None},
            )
        )
        for table, values in invalid:
            with pytest.raises(IntegrityError):
                async with c.begin_nested():
                    await c.execute(table.insert().values(**values))


@pytest.mark.parametrize(
    "state_migration",
    [("SMT_MANUAL_MIGRATION_DATABASE_URL", b, OLD) for b in ("sqlite", "postgres")],
    indirect=True,
)
async def test_new_input_and_request_attempts_split_back_with_original_ids(state_migration):
    store = state_migration
    await store.migrate("downgrade", OLD)
    await populate(store)
    await store.migrate("upgrade", NEW)
    current = await tables(store)
    async with store.engine.begin() as c:
        inputs, requests = current["smt_manual_input"], current["smt_manual_request"]
        review = dict(
            (await c.execute(sa.select(inputs).where(inputs.c.operation_id == "notice")))
            .mappings()
            .one()
        )
        await c.execute(
            inputs.insert().values(
                **(
                    review
                    | {
                        "operation_id": "new-review",
                        "receipt_id": 499,
                        "original_operation_id": "notice",
                        "state": "REVIEW",
                        "completion_result": "NG",
                        "payload": review["payload"] | {"result": "NG"},
                    }
                )
            )
        )
        admission = dict(
            (await c.execute(sa.select(requests).where(requests.c.kind == "BIN_ADMISSION")))
            .mappings()
            .one()
        )
        await c.execute(
            requests.insert().values(
                **(
                    admission
                    | {
                        "operation_id": "new-admit",
                        "message_id": 399,
                        "previous_operation_id": "admit",
                        "due_at_ms": 2000,
                        "request_snapshot": {"task_id": "T1", "bin_code": "B", "scanned_at": 5},
                    }
                )
            )
        )
        bins = current["smt_manual_bin"]
        await c.execute(
            bins.update()
            .where(bins.c.arrival_id == "A1")
            .values(admission_operation_id="new-admit")
        )
    await store.migrate("downgrade", OLD)
    legacy = await tables(store)
    async with store.engine.connect() as c:
        notice = (
            (
                await c.execute(
                    sa.select(legacy["smt_manual_completion"]).where(
                        legacy["smt_manual_completion"].c.operation_id == "new-review"
                    )
                )
            )
            .mappings()
            .one()
        )
        assert notice["receipt_id"] == 499 and notice["original_operation_id"] == "notice"
        assert notice["state"] == "REVIEW" and notice["result"] == "NG"
        assert notice["arrival_id"] == "A1"
        attempts = (await c.execute(sa.select(legacy["smt_manual_admission"]))).mappings().all()
        assert {r["operation_id"] for r in attempts} == {"admit", "new-admit"}
        assert (
            next(r for r in attempts if r["operation_id"] == "new-admit")["wait_until_ms"] == 2000
        )
        assert (
            await c.scalar(sa.select(legacy["smt_manual_bin"].c.admission_operation_id))
            == "new-admit"
        )
    await store.migrate("upgrade", NEW)
    inputs = (await tables(store))["smt_manual_input"]
    async with store.engine.connect() as c:
        history = {
            r["operation_id"]: (
                r["state"],
                r["completion_result"],
                r["original_operation_id"],
                r["arrival_id"],
            )
            for r in (
                await c.execute(sa.select(inputs).where(inputs.c.operation == BIN_NOTICE))
            ).mappings()
        }
        assert history == {
            "notice": ("REVIEW", "NORMAL", None, "A1"),
            "notice-repeat": ("DUPLICATE", "NORMAL", "notice", "A1"),
            "new-review": ("REVIEW", "NG", "notice", "A1"),
        }


@pytest.mark.parametrize(
    "state_migration",
    [("SMT_MANUAL_MIGRATION_DATABASE_URL", b, OLD) for b in ("sqlite", "postgres")],
    indirect=True,
)
async def test_nullable_results_keep_sql_null_distinct_from_json_null(state_migration):
    store = state_migration
    source = await populate(store)
    pairs = (
        ("task_confirmation", "response", "confirm3"),
        ("admission", "response", "admit"),
        ("rack_departure", "decision", "departure"),
    )
    async with store.engine.begin() as c:
        for name, column, identity in pairs:
            table = source["smt_manual_" + name]
            original = dict(
                (await c.execute(sa.select(table).where(table.c.operation_id == identity)))
                .mappings()
                .one()
            )
            await c.execute(
                table.update().where(table.c.operation_id == identity).values(**{column: sa.null()})
            )
            await c.execute(
                table.insert().values(
                    **(original | {"operation_id": identity + "-json", column: sa.JSON.NULL})
                )
            )
    async with store.engine.begin() as c:
        await c.execute(
            source["smt_manual_task_confirmation"]
            .update()
            .where(source["smt_manual_task_confirmation"].c.operation_id == "confirm3")
            .values(basis=sa.JSON.NULL)
        )
    await store.migrate("upgrade", NEW)
    current = (await tables(store))["smt_manual_request"]
    async with store.engine.connect() as c:
        assert (
            await c.scalar(
                sa.select(current.c.basis.is_(None)).where(current.c.operation_id == "confirm3")
            )
            is False
        )
        for _, _, identity in pairs:
            assert (
                await c.scalar(
                    sa.select(current.c.response.is_(None)).where(
                        current.c.operation_id == identity
                    )
                )
                is True
            )
            assert (
                await c.scalar(
                    sa.select(current.c.response.is_(None)).where(
                        current.c.operation_id == identity + "-json"
                    )
                )
                is False
            )
    await store.migrate("downgrade", OLD)
    source = await tables(store)
    async with store.engine.connect() as c:
        legacy_confirmation = source["smt_manual_task_confirmation"]
        assert (
            await c.scalar(
                sa.select(legacy_confirmation.c.basis.is_(None)).where(
                    legacy_confirmation.c.operation_id == "confirm3"
                )
            )
            is False
        )
        for name, column, identity in pairs:
            table = source["smt_manual_" + name]
            assert (
                await c.scalar(
                    sa.select(table.c[column].is_(None)).where(table.c.operation_id == identity)
                )
                is True
            )
            assert (
                await c.scalar(
                    sa.select(table.c[column].is_(None)).where(
                        table.c.operation_id == identity + "-json"
                    )
                )
                is False
            )


@pytest.mark.parametrize(
    "state_migration",
    [("SMT_MANUAL_MIGRATION_DATABASE_URL", b, OLD) for b in ("sqlite", "postgres")],
    indirect=True,
)
@pytest.mark.parametrize("representable", [True, False])
async def test_departure_pointer_downgrade_preserves_old_max_or_fails_before_ddl(
    state_migration, representable
):
    from app.core.identifiers import uuid7

    store = state_migration
    source = await populate(store)
    clock = datetime(2026, 11, 2, tzinfo=UTC)
    high = str(uuid7(clock, randbits=lambda _: (1 << 74) - 1))
    low = str(uuid7(clock, randbits=lambda _: 0))
    async with store.engine.begin() as c:
        await c.execute(source["smt_manual_rack_departure"].update().values(operation_id=high))
    await store.migrate("upgrade", NEW)
    current = await tables(store)
    async with store.engine.begin() as c:
        requests = current["smt_manual_request"]
        original = dict(
            (await c.execute(sa.select(requests).where(requests.c.operation_id == high)))
            .mappings()
            .one()
        )
        await c.execute(
            requests.insert().values(
                **(
                    original
                    | {
                        "operation_id": low,
                        "message_id": 602,
                        "state": "REQUESTED",
                        "response": None,
                        "decision_code": None,
                        "due_at_ms": None,
                    }
                )
            )
        )
        await c.execute(
            current["smt_manual_rack"]
            .update()
            .values(departure_operation_id=high if representable else low)
        )
    if representable:
        await store.migrate("downgrade", OLD)
        legacy = (await tables(store))["smt_manual_rack_departure"]
        async with store.engine.connect() as c:
            assert (
                await c.scalar(
                    sa.select(sa.func.max(legacy.c.operation_id)).where(
                        legacy.c.rack_work_id == "rack"
                    )
                )
                == high
            )
        await store.migrate("upgrade", NEW)
        rack = (await tables(store))["smt_manual_rack"]
        async with store.engine.connect() as c:
            assert await c.scalar(sa.select(rack.c.departure_operation_id)) == high
    else:
        before = await snapshot(store, current)
        shape = await store.schema_snapshot("smt_manual_")
        with pytest.raises(AssertionError, match="downgrade departure.*rack"):
            await store.migrate("downgrade", OLD)
        assert await snapshot(store, await tables(store)) == before
        assert await store.schema_snapshot("smt_manual_") == shape
        async with store.engine.begin() as c:
            await c.execute(current["smt_manual_rack"].update().values(departure_operation_id=high))


@pytest.mark.parametrize(
    "state_migration",
    [("SMT_MANUAL_MIGRATION_DATABASE_URL", b, OLD) for b in ("sqlite", "postgres")],
    indirect=True,
)
@pytest.mark.parametrize(
    "state,has_response", [("CONFLICT", True), ("CONFLICT", False), ("COMPLETED", False)]
)
async def test_confirmation_keeps_saved_decision_or_explicit_local_state(
    state_migration, state, has_response
):
    store = state_migration
    source = await populate(store)
    response = (
        {"code": "DECIDED", "timestamp": 41, "data": {"result": "COMPLETED"}}
        if has_response
        else None
    )
    async with store.engine.begin() as c:
        await c.execute(
            source["smt_manual_task_confirmation"]
            .update()
            .where(source["smt_manual_task_confirmation"].c.operation_id == "confirm2")
            .values(state=state, response=response)
        )
    await store.migrate("upgrade", NEW)
    requests = (await tables(store))["smt_manual_request"]
    async with store.engine.connect() as c:
        row = (
            (await c.execute(sa.select(requests).where(requests.c.operation_id == "confirm2")))
            .mappings()
            .one()
        )
        assert row["state"] == ("CONSUMED" if state == "COMPLETED" else "CONFLICT")
        assert row["decision_code"] == (
            "COMPLETED" if has_response or state == "COMPLETED" else None
        )
        assert row["response"] == response and row["basis"] == {"original": 2}
    await store.migrate("downgrade", OLD)
    legacy = (await tables(store))["smt_manual_task_confirmation"]
    async with store.engine.connect() as c:
        row = (
            (await c.execute(sa.select(legacy).where(legacy.c.operation_id == "confirm2")))
            .mappings()
            .one()
        )
        assert row["state"] == state and row["response"] == response


@pytest.mark.parametrize(
    "state_migration",
    [("SMT_MANUAL_MIGRATION_DATABASE_URL", b, OLD) for b in ("sqlite", "postgres")],
    indirect=True,
)
async def test_applied_completion_keeps_bin_original_binding_through_migration(state_migration):
    store = state_migration
    source = await populate(store)
    async with store.engine.begin() as c:
        notices = source["smt_manual_completion"]
        await c.execute(
            notices.update()
            .where(notices.c.operation_id == "notice")
            .values(state="APPLIED", reason=None)
        )
        await c.execute(
            source["smt_manual_bin"]
            .update()
            .values(
                state="RELEASED",
                pending=False,
                completed_operation_id="notice",
                completed_result="NORMAL",
            )
        )
        await c.execute(
            source["smt_manual_admission"]
            .update()
            .values(
                response={
                    "code": "DECIDED",
                    "timestamp": 8,
                    "data": {"result": "WORK_REQUIRED", "task_id": "T1"},
                },
                wait_until_ms=None,
            )
        )
    before = await snapshot(store, source)
    await store.migrate("upgrade", NEW)
    inputs = (await tables(store))["smt_manual_input"]
    async with store.engine.connect() as c:
        row = (
            (await c.execute(sa.select(inputs).where(inputs.c.operation_id == "notice")))
            .mappings()
            .one()
        )
        assert row["state"] == "APPLIED" and row["arrival_id"] == "A1"
    await store.migrate("downgrade", OLD)
    assert await snapshot(store, await tables(store)) == before


@pytest.mark.parametrize(
    "state_migration",
    [("SMT_MANUAL_MIGRATION_DATABASE_URL", b, OLD) for b in ("sqlite", "postgres")],
    indirect=True,
)
@pytest.mark.parametrize(
    "direction,broken",
    [(d, b) for d in ("upgrade", "downgrade") for b in ("cycle", "box", "confirmation")]
    + [("downgrade", "prepare")],
)
async def test_conflicting_originals_are_rejected_before_ddl(state_migration, direction, broken):
    store = state_migration
    source = await populate(store)
    legacy = direction == "upgrade"
    if not legacy:
        await store.migrate("upgrade", NEW)
        source = await tables(store)
    table = source[
        "smt_manual_"
        + (
            ("completion" if legacy else "input")
            if broken in {"cycle", "box"}
            else ("task_confirmation" if legacy else "request")
        )
    ]
    target, current = (NEW, OLD) if legacy else (OLD, NEW)
    shape = await store.schema_snapshot("smt_manual_")
    cases = (
        ["extra", "missing", "message", "owner", "accepted", "accepted_at", "timestamp", "snapshot"]
        if broken == "prepare"
        else [broken]
    )
    for case in cases:
        ids = (
            ("notice", "notice-repeat")
            if case in {"cycle", "box"}
            else ("confirm2",)
            if case == "confirmation"
            else ("prepare1", "extra-prepare")
        )
        clause = table.c.operation_id.in_(ids)
        async with store.engine.begin() as conn:
            originals = [
                dict(r) for r in (await conn.execute(sa.select(table).where(clause))).mappings()
            ]
            row = originals[0]
            statement = table.update().where(clause)
            if case == "cycle":
                statement = statement.where(table.c.operation_id == "notice").values(
                    original_operation_id="notice-repeat"
                )
            elif case == "box":
                payload = "data" if legacy else "payload"
                statement = statement.values(
                    bin_code="X", **{payload: row[payload] | {"bin_code": "X"}}
                )
            elif case == "confirmation":
                statement = statement.values(
                    response={
                        "code": "DECIDED",
                        "timestamp": 31,
                        "data": {"result": "PLAN_REVISION_STALE", "current_plan_revision": 2},
                    }
                )
            elif case == "extra":
                statement = table.insert().values(
                    **(row | {"operation_id": "extra-prepare", "message_id": 911})
                )
            elif case == "missing":
                statement = table.delete().where(clause)
            elif case == "message":
                statement = statement.values(message_id=911)
            elif case == "owner":
                statement = statement.values(work_line_id=-2)
            elif case == "accepted":
                statement = statement.values(state="REQUESTED", decision_code=None)
            elif case == "accepted_at":
                statement = statement.values(saved_at_ms=111)
            elif case == "timestamp":
                statement = statement.values(
                    response={"code": "PREPARE_ACCEPTED", "timestamp": 111, "data": {}}
                )
            else:
                statement = statement.values(
                    request_snapshot={"task_id": "T2", "workline_code": "LINE1"}
                )
            await conn.execute(statement)
        before = await snapshot(store, source)
        try:
            with pytest.raises(AssertionError) as failure:
                await store.migrate(direction, target)
            diagnostic = str(failure.value)
            assert "SMT persistence: invalid" in diagnostic
            assert ("extra-prepare" if case == "extra" else ids[0]) in diagnostic
            assert await snapshot(store, await tables(store)) == before
            assert await store.schema_snapshot("smt_manual_") == shape
            async with store.engine.connect() as conn:
                assert (
                    await conn.scalar(sa.text("SELECT version_num FROM alembic_version")) == current
                )
        finally:
            if table.name in await tables(store):
                async with store.engine.begin() as conn:
                    await conn.execute(table.delete().where(clause))
                    await conn.execute(table.insert(), originals)
