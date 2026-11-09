import json
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy import text

from app.integrations import history as history_records
from tests.helpers import run_alembic as migrate

ACTIVE = "019a1111-2222-7333-8444-555555555561"
CANCELLED = "019a1111-2222-7333-8444-555555555562"
ORDINARY = "019a1111-2222-7333-8444-555555555563"
ACK = "019a1111-2222-7333-8444-555555555564"
ACTIVE_TRANSPORT = "transport-019a1111-2222-7333-8444-555555555565"
UNBOUND_CYCLE = "019a1111-2222-7333-8444-555555555551"
UNBOUND_CLIENT = "019a1111-2222-7333-8444-555555555552"


async def assert_four_table_cutover_refused(url, engine):
    """Historical migration identity remains valid; the new runtime refuses old active work."""
    result = await migrate(url, "upgrade", "rollerfoundation")
    assert result.returncode == 0, result.stderr
    await engine.dispose()
    async with engine.connect() as connection:
        before = await snapshot(
            connection,
            ("transport_task", "reliable_message", "rack_dispatch_job", "rack_dispatch_action"),
        )
    result = await migrate(url, "upgrade", "rollerfour")
    assert result.returncode != 0 and "BIN four-table cutover refused" in result.stderr
    async with engine.connect() as connection:
        assert await snapshot(connection, tuple(before)) == before
        assert await connection.scalar(
            text("select to_regclass('enterprise_backend.bin_roller_scan_arrival')")
        )
        assert not await connection.scalar(
            text("select to_regclass('enterprise_backend.bin_roller_bin_passage')")
        )


async def isolate_retained_manual_runtime(url, engine):
    """This disposable fixture has mixed sample data; only the MANUAL history is under test."""
    async with engine.begin() as connection:
        metadata = sa.MetaData(schema="enterprise_backend")
        await connection.run_sync(metadata.reflect)
        tables = {t.name: t for t in metadata.tables.values()}
        # No application migration performs this fixture setup or rewrites shared native evidence.
        for name, table in tables.items():
            if name.startswith("bin_roller_"):
                await connection.execute(table.delete())
        jobs, actions = tables["rack_dispatch_job"], tables["rack_dispatch_action"]
        roller_jobs = sa.select(jobs.c.id).where(
            jobs.c.source_kind.in_(("ROLLER_ORDINARY", "ROLLER_DRAIN"))
        )
        action_ids = list(
            await connection.scalars(
                sa.select(actions.c.id)
                .where(actions.c.job_id.in_(roller_jobs))
                .order_by(actions.c.ordinal.desc())
            )
        )
        for identity in action_ids:
            await connection.execute(actions.delete().where(actions.c.id == identity))
        await connection.execute(jobs.delete().where(jobs.c.id.in_(roller_jobs)))
        members = tables["smt_manual_task_member"]
        await connection.execute(members.delete().where(members.c.kind == "BIN"))
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from scripts.backfill_integration_history import WRITERS, WriterEvidence, reconcile

    result = await migrate(url, "upgrade", "integrationhistoryexpand")
    assert result.returncode == 0, result.stderr
    await engine.dispose()
    evidence = {**dict.fromkeys(WRITERS, "DRAINED"), "attestation": "isolated-manual-migration"}
    async with async_sessionmaker(engine).begin() as session:
        await session.execute(text("SET LOCAL search_path TO enterprise_backend"))
        report = await reconcile(
            session, phase="final", writer_evidence=WriterEvidence.from_dict(evidence)
        )
        assert report.eligible, report
    result = await migrate(url, "upgrade", "integrationhistoryauthority")
    assert result.returncode == 0, result.stderr
    with TemporaryDirectory() as directory:
        proof = Path(directory) / "contract.json"
        proof.write_text(json.dumps({**evidence, "authority_acceptance": "isolated-manual-proof"}))
        result = await migrate(
            url,
            "-x",
            f"integration_history_contract_evidence={proof}",
            "upgrade",
            "sorting_task_authority",
        )
        assert result.returncode == 0, result.stdout + result.stderr
    await engine.dispose()


async def test_empty_cutover_round_trip(schema_database):
    url, engine = schema_database
    for direction, revision in (
        ("upgrade", "racksubmitack"),
        ("upgrade", "sharedrackdispatch"),
        ("downgrade", "racksubmitack"),
    ):
        result = await migrate(url, direction, revision)
        assert result.returncode == 0, result.stderr
        async with engine.connect() as conn:
            assert (
                await conn.scalar(
                    text("select version_num from enterprise_backend.alembic_version")
                )
                == revision
            )
            table = "rack_dispatch_job" if revision == "sharedrackdispatch" else "smt_manual_rack"
            assert await conn.scalar(
                text("select to_regclass(:name)"), {"name": "enterprise_backend." + table}
            )


# Use the actual previous revision and reflect it, independently of current business metadata.
async def old_store(schema_database, *, multi_rotation=True, runtime_subject=None):
    from sqlalchemy import MetaData

    url, engine = schema_database
    result = await migrate(url, "upgrade", "racksubmitack")
    assert result.returncode == 0, result.stdout + result.stderr
    async with engine.begin() as conn:
        metadata = MetaData(schema="enterprise_backend")
        await conn.run_sync(metadata.reflect)
        tables = {t.name: t for t in metadata.tables.values()}
        for t in tables.values():
            for c in t.c:
                if isinstance(c.type, sa.JSON):
                    c.type.none_as_null = True
        await seed(conn, tables, multi_rotation, runtime_subject)
    return url, engine, tables


async def seed(conn, t, multi_rotation, runtime_subject):
    async def insert(table, **values):
        await conn.execute(t[table].insert().values(**values))

    def location_for(job):
        code = "P" if runtime_subject in (None, job) else "P-" + job
        return {"type": "RACK_POSITION", "location_code": code}

    def rack_for(job):
        return "R" if runtime_subject in (None, job) else "R-" + job

    await insert(
        "work_lines",
        id=7,
        line_code="L",
        line_name="Original line",
        business_code="smt_manual",
        position_bindings={"FIVE_RACK": "P", "RETURN_RACK": "P", "TRANSFER_RACK": "P"},
    )
    scope = dict(owner_domain="smt_manual", work_line_id=7)
    loc = {"type": "RACK_POSITION", "location_code": "P"}
    dest = {"type": "ZONE", "location_code": "Z"}
    request = dict(workline_code="L", rack_id="R", current_location=loc, current_face="A")
    ready = dict(result="READY", rack_destination=dest)
    wait = dict(result="WAIT", retry_after_ms=100)
    instant = datetime(2026, 1, 1, tzinfo=UTC)

    def point(value):
        return {"kind": value["type"], "location_code": value["location_code"]}

    async def message(op, oid, mid, payload, owner, target, ack=False):
        wire = {
            "operation_id": oid,
            "operation": op,
            "timestamp": int(instant.timestamp() * 1000),
            "data": payload,
        }
        response = (
            json.dumps(
                {
                    "operation_id": oid,
                    "code": "RECEIVED" if owner else "RECORDED",
                    "timestamp": 12,
                    "data": {"transport_task_id": owner} if owner else {},
                }
            ).encode()
            if ack
            else None
        )
        await insert(
            "reliable_message",
            id=mid,
            operation=op,
            operation_id=oid,
            target=target,
            business_key=owner,
            request_raw=json.dumps(wire, separators=(",", ":")).encode(),
            response_raw=response,
            last_http_status=(202 if owner else 200) if ack else None,
            last_verdict="DELIVERED" if ack else None,
            next_attempt_at=instant,
            attempts=int(ack),
            status="DONE" if ack else "PENDING",
            delivery_uncertain=False,
            expires_at=instant + timedelta(hours=1),
            created_at=instant,
            updated_at=instant,
        )

    cycles = {"active": ACTIVE, "cancelled": CANCELLED, "ordinary": ORDINARY, "ack": ACK}

    async def move(
        job,
        kind,
        ref,
        number,
        *,
        roller=False,
        status="SUCCEEDED",
        with_message=False,
        ack=False,
        facing="A",
    ):
        i = job + "-" + kind + (facing if kind == "ROTATE" else "")
        from app.core.identifiers import uuid7

        transport_id = ACTIVE_TRANSPORT if job == "active" else "tt-" + i
        client_id = str(uuid7()) if job == "active" else "client-" + i
        operation_id = str(uuid7()) if job not in ("manual", "roller") else "op-" + i
        k = "RACK_ROTATE" if kind == "ROTATE" else "RACK_MOVE"
        original_point = location_for(job)
        rack = rack_for(job)
        target = dest if kind == "DEPART" else original_point
        face = None if kind == "DEPART" else facing
        template = (
            {"ENTER": "CTU01", "ROTATE": "CTU02", "DEPART": "CTU03"}[kind] if roller else "F01"
        )
        source = (
            point(original_point) if kind != "ENTER" else {"kind": "RACK", "location_code": rack}
        )
        frozen = dict(
            kind=k, rack_id=rack, rcs_template_id=template, source=source, target=point(target)
        )
        if face:
            frozen["target_face"] = face
        rev = 1 if status in ("SUCCEEDED", "CANCELLED") else None
        actual = point(dest if status == "CANCELLED" else target) if rev else None
        outcome = (
            dict(
                transport_task_id=transport_id,
                kind=k,
                rack_id=rack,
                status=status,
                outcome_revision=1,
                final_position=actual,
                arrival_face=None if status == "CANCELLED" else face,
                position_unknown=None,
                results=None,
                failure_code=None,
            )
            if rev
            else None
        )
        from app.integrations.wms_transport.types import TransportCaller
        from app.integrations.wms_transport.wire import input_digest

        await insert(
            "transport_task",
            client_request_id=client_id,
            transport_task_id=transport_id,
            operation_id=operation_id,
            input_digest=input_digest(
                "rotate_rack" if kind == "ROTATE" else "move_rack",
                TransportCaller("caller", None),
                frozen,
            ),
            kind=k,
            workline_id="caller",
            owner_domain="smt_manual",
            created_at=instant,
            cycle_id=cycles.get(job, job),
            frozen=frozen,
            applied_outcome=outcome,
            outcome_status=status if rev else None,
            applied_outcome_revision=rev,
        )
        if with_message:
            await message(
                "transport.task.submit@v1",
                operation_id,
                number,
                {"transport_task_id": transport_id, **frozen},
                transport_id,
                "historical-wms",
                ack=ack,
            )
        common = dict(
            id=i,
            source_ref=cycles.get(ref, ref),
            client_request_id=client_id,
            transport_task_id=transport_id,
            target_face=face,
        )
        if roller:
            # The old writer had not yet linked the already submitted original DEPART.
            if ack:
                common["transport_task_id"] = None
            basis = (
                {
                    "transport_task_id": transport_id,
                    "client_request_id": client_id,
                    "operation_id": operation_id,
                    "outcome_revision": 1,
                }
                if kind == "DEPART" and status == "SUCCEEDED"
                else None
            )
            await insert(
                "bin_roller_rack_action",
                **scope,
                **common,
                cycle_id=cycles.get(job, job),
                kind=template,
                target=point(target),
                operation_id=None if ack else operation_id,
                caller_workline_id="caller",
                status="ACCEPTED" if basis else status,
                pending=not rev,
                consumed_revision=rev if kind != "DEPART" else None,
                position_revision=rev if kind != "DEPART" else None,
                acceptance_basis=basis,
            )
            if kind != "DEPART" and rev:
                await insert(
                    "bin_roller_position_fact",
                    **scope,
                    id="pos-" + i,
                    object_kind="RACK",
                    object_code=rack,
                    location_code=original_point["location_code"],
                    rack_face=face,
                    source_kind="RACK",
                    source_id=i,
                    source_revision=rev,
                    is_current=False,
                )
            return basis
        await insert(
            "smt_manual_rack_action",
            **common,
            rack_work_id=cycles.get(job, job),
            kind=kind,
            target=target,
            state=status,
            outcome_revision=rev,
            actual_position=actual,
            actual_face=face if rev and status != "CANCELLED" else None,
        )

    for n, (job, state, role) in enumerate(
        (
            ("manual", "DEPARTED", "RETURN"),
            ("cancelled", "ENTRY_CANCELLED", "RETURN"),
            ("active", "ACTIVE", "TRANSFER"),
        )
    ):
        loc, rack = location_for(job), rack_for(job)
        task = "task-" + job
        target = None if role == "RETURN" else dict(rack_id=rack, rack_face="A")
        await insert(
            "smt_manual_task",
            task_id=task,
            work_line_id=7,
            workline_code="L",
            issued_operation_id="issue-" + job,
            issued_timestamp=5,
            issued_data={},
            queue_revision=1,
            dispatch_sequence=n,
            task_type="MANUAL",
            business_type="outbound",
            status="EXECUTING" if job == "active" else "COMPLETED",
            plan_revision=1,
            prepare_accepted=True,
            received_work=[],
            target_rack=target,
        )
        grant = {"task_id": task, "plan_revision": 1}
        if target:
            grant["target_rack"] = target
        else:
            grant["added_direct_picks"] = [
                {
                    "source_locator": {
                        "type": "RACK_SLOT",
                        "rack_id": rack,
                        "rack_face": "A",
                        "slot_id": "S",
                    }
                }
            ]
        await insert(
            "smt_manual_input",
            operation="sorting.task.plan_delta@v1",
            operation_id="plan-" + job,
            task_id=task,
            work_line_id=7,
            timestamp=6,
            payload=grant,
            state="APPLIED",
            is_plan_record=True,
            plan_revision=1,
        )
        if role == "RETURN":
            await insert(
                "smt_manual_task_member",
                id="member-" + job,
                task_id=task,
                plan_revision=1,
                kind="DIRECT",
                rack_id=rack,
                rack_face="A",
                slot_id="S",
                member_order=0,
                status="CANCELLED" if job == "cancelled" else "COMPLETED",
                rack_work_id=cycles.get(job, job),
            )
        await insert(
            "smt_manual_rack",
            id=cycles.get(job, job),
            task_id=task,
            work_line_id=7,
            workline_code="L",
            rack_id=rack,
            role=role,
            location_code=loc["location_code"],
            initial_face="A",
            caller_workline_id="caller",
            state=state,
            pending=True,
            departure_operation_id="a-ready" if job == "manual" else None,
        )
        await move(
            job,
            "ENTER",
            job,
            n + 1,
            status="CANCELLED"
            if job == "cancelled"
            else "PENDING"
            if job == "active"
            else "SUCCEEDED",
        )
    loc, rack = location_for("manual"), rack_for("manual")
    request = dict(workline_code="L", rack_id=rack, current_location=loc, current_face="A")
    report = dict(
        task_id="task-manual",
        transport_task_id="tt-manual-ENTER",
        rack_id=rack,
        arrival_face="A",
        final_position=loc,
        outcome_revision=1,
    )
    await message(
        "outbound.return_rack.arrival_report@v1",
        "report",
        31,
        report,
        None,
        "old-exclusive-facts",
        ack=True,
    )
    await insert(
        "smt_manual_request",
        kind="RACK_ARRIVAL_REPORT",
        operation="outbound.return_rack.arrival_report@v1",
        operation_id="report",
        message_id=31,
        task_id="task-manual",
        work_line_id=7,
        rack_work_id="manual",
        action_id="manual-ENTER",
        request_snapshot=report,
        state="CONSUMED",
        response={"code": "RECORDED", "timestamp": 12, "data": {}},
    )
    for oid, prev, result, saved in (
        ("z-wait", None, wait, 10),
        ("b-wait", "z-wait", wait, 2000000000000),
        ("a-ready", "b-wait", ready, 2000000000101),
    ):
        await insert(
            "smt_manual_request",
            operation="rack.departure_decide@v1",
            kind="RACK_DEPARTURE",
            operation_id=oid,
            previous_operation_id=prev,
            message_id={"z-wait": 51, "b-wait": 52, "a-ready": 53}[oid],
            work_line_id=7,
            task_id="task-manual",
            rack_work_id="manual",
            request_snapshot=request,
            basis={"source": "original"},
            response=result,
            decision_code=result["result"],
            state="WAITING" if result == wait else "CONSUMED",
            saved_at_ms=saved,
            due_at_ms=saved + 100 if result == wait else None,
        )
    await message(
        "rack.departure_decide@v1",
        "a-ready",
        53,
        request,
        "business:smt_manual",
        "old-wms-decisions",
    )
    await conn.execute(
        t["reliable_message"]
        .update()
        .where(t["reliable_message"].c.id == 53)
        .values(
            status="DONE",
            last_http_status=200,
            last_verdict="DELIVERED",
            response_raw=json.dumps(
                dict(operation_id="a-ready", code="DECIDED", timestamp=7, data=ready)
            ).encode(),
        )
    )
    await move("manual", "DEPART", "a-ready", 99)
    resources = dict(
        workline_code="L",
        positions={"FIVE_RACK": "P"},
        caller_workline_id="caller",
        station_id=None,
    )
    for job, state in (
        ("roller", "RELEASED"),
        ("ack", "ACTIVE"),
        ("unused", "WITHDRAWN"),
        (UNBOUND_CYCLE, "ACTIVE"),
    ):
        loc, rack = location_for(job), rack_for(job)
        request = dict(workline_code="L", rack_id=rack, current_location=loc, current_face="A")
        resources = {**resources, "positions": {"FIVE_RACK": loc["location_code"]}}
        drain = "drain-" + job
        await insert(
            "bin_roller_decision",
            **scope,
            kind="DRAIN",
            id=drain,
            input_ref=drain,
            input_kind="FIFO_OBSERVATION",
            unused_racks=[rack] if job == "unused" else [],
            operation_id="drain-op-" + job,
            status="READY",
            pending=False,
            decision={
                "result": "READY",
                "racks": [
                    {
                        "rack_id": rack,
                        "rack_face": (
                            ["A", "Z", "B"] if job == "roller" and multi_rotation else ["A"]
                        ),
                    }
                ],
            },
        )
        basis = None
        if job in ("roller", "ack"):
            await move(job, "ENTER", job, 80, roller=True)
            if job == "roller" and multi_rotation:
                for face in ("Z", "B"):
                    sid = "service-" + face
                    await insert(
                        "bin_roller_face_work",
                        **scope,
                        purpose="DRAIN",
                        id=sid,
                        service_id=sid,
                        cycle_id=cycles.get(job, job),
                        source_ref=drain + ":" + face,
                        service_state="COMPLETED",
                        rack_id=rack,
                        rack_face=face,
                    )
                    await move(job, "ROTATE", sid, 81, roller=True, facing=face)
            await insert(
                "bin_roller_decision",
                **scope,
                id="dec-" + job,
                kind="DEPARTURE",
                cycle_id=cycles.get(job, job),
                source_ref=cycles.get(job, job),
                operation_id="op-dec-" + job,
                message_id=41 if job == "roller" else 42,
                request={**request, "current_face": "B"}
                if job == "roller" and multi_rotation
                else request,
                decision=ready,
                status="READY",
                pending=True,
            )
            basis = await move(
                job,
                "DEPART",
                "dec-" + job,
                87,
                roller=True,
                status="SUCCEEDED" if job == "roller" else "PENDING",
                with_message=job == "ack",
                ack=job == "ack",
            )
        if job == UNBOUND_CYCLE:
            await insert(
                "bin_roller_rack_action",
                **scope,
                id="client-only",
                cycle_id=cycles.get(job, job),
                kind="CTU01",
                source_ref=cycles.get(job, job),
                client_request_id=UNBOUND_CLIENT,
                caller_workline_id="caller",
                target=point(loc),
                target_face="A",
                status="PENDING",
                pending=True,
            )
        await insert(
            "bin_roller_rack_cycle",
            **scope,
            id=cycles.get(job, job),
            source_ref=drain,
            rack_id=rack,
            purpose="DRAIN",
            status=state,
            pending=state == "ACTIVE",
            resources=resources if job != "unused" else None,
            release_basis=basis,
        )
    loc, rack = location_for("roller"), rack_for("roller")
    await insert(
        "bin_roller_face_work",
        **scope,
        purpose="DRAIN",
        id="service",
        service_id="service",
        cycle_id="roller",
        source_ref="drain-roller:A",
        service_state="COMPLETED",
        rack_id=rack,
        rack_face="A",
    )
    await insert(
        "bin_roller_return_opportunity",
        **scope,
        id="ret",
        service_id="service",
        source_ref="service",
        kind="PURE_RETURN",
        ended=True,
        end_reason="EMPTY",
        end_evidence={"original": 1},
    )
    await insert(
        "bin_roller_return_batch",
        **scope,
        id="batch",
        opportunity_id="ret",
        source_ref="ret",
        operation_id="ret-op",
        members=[],
        manifest={"original": 2},
        workline_code="L",
        caller_workline_id="caller",
        source=point(loc),
        status="COMPLETED",
        pending=False,
    )
    loc, rack = location_for("ordinary"), rack_for("ordinary")
    resources = {**resources, "positions": {"FIVE_RACK": loc["location_code"]}}

    await insert(
        "bin_roller_rack_cycle",
        **scope,
        id=ORDINARY,
        source_ref="demand",
        rack_id=rack,
        task_id="task-active",
        purpose="ORDINARY",
        resources=resources,
        status="ACTIVE",
        pending=True,
    )
    await insert(
        "bin_roller_face_work",
        **scope,
        purpose="SUPPLY",
        id="demand",
        demand_id="demand",
        service_id="supplying",
        cycle_id=ORDINARY,
        source_ref="demand",
        change_id="plan-active",
        task_id="task-active",
        plan_revision=1,
        service_state="READY",
        demand_state="ACTIVE",
        rack_id=rack,
        rack_face="A",
        feed_id="feed",
        feed_state="ALLOCATED",
        operation_id="feed-original",
        caller_workline_id="caller",
        target=loc,
        manifest={"original": 2},
    )
    await move("ordinary", "ENTER", "supplying", 92, roller=True)
    await insert(
        "bin_roller_feed_segment",
        **scope,
        id="segment",
        feed_id="feed",
        segment_index=1,
        members=[
            {
                "bin_code": "box",
                "source_locator": {"rack_id": rack, "rack_face": "A", "slot_id": "S"},
            }
        ],
        client_request_id="feed-client",
        transport_task_id="box-original",
        operation_id="box-submit",
        consumed_revision=1,
        status="SUCCEEDED",
        pending=False,
    )
    await insert(
        "transport_task",
        client_request_id="feed-client",
        transport_task_id="box-original",
        operation_id="box-submit",
        owner_domain="smt_manual",
        cycle_id=ORDINARY,
        kind="BIN_MOVE",
        workline_id="caller",
        created_at=instant,
        input_digest="a" * 64,
        frozen={
            "kind": "BIN_MOVE",
            "moves": [
                {
                    "container_id": "box",
                    "source": {
                        "kind": "RACK_BIN_SLOT",
                        "rack_id": rack,
                        "rack_face": "A",
                        "slot_id": "S",
                    },
                    "target": {"kind": "HANDOFF_POSITION", "location_code": "out"},
                }
            ],
        },
    )
    await insert(
        "bin_roller_return_opportunity",
        **scope,
        id="next",
        service_id="supplying",
        source_ref="SEGMENT:segment",
        kind="FEED",
        ended=False,
    )


async def snapshot(conn, names):
    rows = {}
    for name in names:
        result = (await conn.execute(text(f"select * from enterprise_backend.{name}"))).mappings()
        additive = {
            "scope_owner",
            "scope_kind",
            "scope_id",
            "dispatch_started_at",
            "event_envelope",
            "event_context",
            "application_ref",
        }
        rows[name] = sorted(
            ({k: v for k, v in r.items() if k not in additive or v is not None} for r in result),
            key=lambda r: repr(sorted(r.items())),
        )
    return rows


async def test_original_history_survives_without_dispatch(schema_database):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    url, engine, _ = await old_store(schema_database)
    untouched = (
        "transport_task",
        "reliable_message",
        "bin_roller_feed_segment",
        "bin_roller_return_opportunity",
        "bin_roller_return_batch",
        "bin_roller_position_fact",
    )
    async with engine.connect() as conn:
        before = await snapshot(conn, untouched)
    result = await migrate(url, "upgrade", "sharedrackdispatch")
    assert result.returncode == 0, result.stdout + result.stderr
    async with engine.connect() as conn:
        assert await snapshot(conn, untouched) == before
    result = await migrate(url, "upgrade", "rollerfoundation")
    assert result.returncode == 0, result.stderr
    await engine.dispose()
    async with engine.connect() as conn:
        before = await snapshot(conn, untouched)
    async with async_sessionmaker(engine)() as db:
        dispatch = runtime(engine).rack_dispatch
        job = await dispatch.progress(db, job_id="manual")
        assert job.state == "COMPLETED" and job.completed_at_ms is None
        assert job.origin.source_ref == "member-manual" and job.origin.cycle_id == "manual"
        assert [a.id for a in job.actions] == [
            "manual-ENTER",
            "z-wait",
            "b-wait",
            "a-ready",
            "manual-DEPART",
        ]
        assert [a.due_at_ms for a in job.actions[1:3]] == [110, 2000000000100]
        assert job.actions[-1].phase == "LEGACY_CLOSED" and job.actions[-1].accepted_at_ms is None
        assert job.actions[0].outcome_revision == 1 and job.actions[0].submit_message_id is None
        roller = await dispatch.progress(db, job_id="roller")
        assert [a.target_face for a in roller.actions[:3]] == ["A", "Z", "B"]
        assert roller.actions[2].previous_action_id == roller.actions[1].id
        assert (
            roller.origin.task_id is None
            and roller.actions[-1].completion_rule == "LEGACY_RECORDED_CLOSURE"
        )
        assert roller.actions[-1].acceptance_basis is None
        pending = await dispatch.progress(db, job_id=ACK)
        assert pending.state == "RELEASING" and pending.actions[-1].submit_message_id == 87
        assert pending.actions[-1].phase == "REGISTERED"
        original = (
            await db.execute(
                text(
                    "SELECT operation_id, status FROM enterprise_backend.reliable_message "
                    "WHERE id=87"
                )
            )
        ).one()
        assert original.status == "DONE"
        assert original.operation_id == next(
            row["operation_id"] for row in before["reliable_message"] if row["id"] == 87
        )
        unused = await dispatch.progress(db, job_id="unused")
        assert (
            unused.state == "WITHDRAWN" and unused.origin.resources is unused.origin.caller is None
        )
        unbound = await dispatch.progress(db, job_id=UNBOUND_CYCLE)
        assert (
            unbound.actions[0].client_request_id == UNBOUND_CLIENT
            and unbound.actions[0].transport_task_id is None
        )
        cancelled = await dispatch.progress(db, job_id=CANCELLED)
        assert (
            cancelled.state == "NOT_ARRIVED"
            and cancelled.actions[0].outcome["status"] == "CANCELLED"
        )
    async with engine.connect() as conn:
        assert await snapshot(conn, untouched) == before
        for table in (
            "smt_manual_rack",
            "smt_manual_rack_action",
            "bin_roller_rack_cycle",
            "bin_roller_rack_action",
        ):
            assert (
                await conn.scalar(
                    text("select to_regclass(:n)"), {"n": "enterprise_backend." + table}
                )
                is None
            )
        assert tuple(
            (
                await conn.execute(
                    text(
                        "select preparation_action_id,arrival_report_operation_id from "
                        "enterprise_backend.smt_manual_task_member where id='member-manual'"
                    )
                )
            ).one()
        ) == ("manual-ENTER", "report")
        assert (
            await conn.scalar(
                text(
                    "select preparation_action_id from "
                    "enterprise_backend.bin_roller_face_work where id='service'"
                )
            )
            == "roller-ENTER"
        )
        assert tuple(
            (
                await conn.execute(
                    text(
                        "select target_job_id,target_action_id from "
                        "enterprise_backend.smt_manual_input where operation_id='plan-active'"
                    )
                )
            ).one()
        ) == (ACTIVE, "active-ENTER")
    result = await migrate(url, "downgrade", "racksubmitack")
    assert result.returncode != 0
    assert "capacity preflight: admission orders exist" in result.stdout + result.stderr
    async with engine.connect() as conn:
        assert await snapshot(conn, untouched) == before
        assert (
            await conn.scalar(text("select count(*) from enterprise_backend.rack_dispatch_job"))
            == 8
        )


@pytest.mark.parametrize(
    "table,statement",
    [
        pytest.param(
            "smt_manual_rack",
            "with a as (update smt_manual_rack_action set rack_work_id='opaque', "
            f"source_ref='opaque' where rack_work_id='{ACTIVE}'), t as (update transport_task "
            f"set cycle_id='opaque' where cycle_id='{ACTIVE}') update smt_manual_rack "
            f"set id='opaque' where id='{ACTIVE}'",
            id="invalid-pending-predecessor-cycle",
        ),
        pytest.param(
            "transport_task",
            "update transport_task set operation_id='bad' "
            f"where transport_task_id='{ACTIVE_TRANSPORT}'",
            id="invalid-pending-operation",
        ),
        pytest.param(
            "bin_roller_position_fact",
            "update bin_roller_position_fact set rack_face='B' where id='pos-ack-ENTER'",
            id="contradictory-face",
        ),
        pytest.param(
            "bin_roller_position_fact",
            "update bin_roller_position_fact set location_code='X' where id='pos-ack-ENTER'",
            id="contradictory-code",
        ),
        pytest.param(
            "reliable_message",
            "update reliable_message set response_raw=convert_to("
            "replace(convert_from(response_raw,'UTF8'),'\"Z\"','\"OTHER\"'),'UTF8') where id=53",
            id="conflicting-retained-ready",
        ),
        pytest.param(
            "bin_roller_rack_action",
            "update bin_roller_rack_action set client_request_id='invalid' where id='client-only'",
            id="invalid-unbound-client",
        ),
        pytest.param(
            "bin_roller_rack_cycle",
            "with changed as (update bin_roller_rack_action set cycle_id='bad',"
            "source_ref='bad' where id='client-only') update bin_roller_rack_cycle set id='bad' "
            f"where id='{UNBOUND_CYCLE}'",
            id="invalid-unbound-cycle",
        ),
        (
            "bin_roller_face_work",
            "update bin_roller_face_work set source_ref='wrong' where id='service'",
        ),
        ("bin_roller_decision", "update bin_roller_decision set message_id=41 where id='dec-ack'"),
        ("bin_roller_feed_segment", "update bin_roller_feed_segment set feed_id='lost'"),
        (
            "bin_roller_return_opportunity",
            "update bin_roller_return_opportunity set source_ref='SEGMENT:lost' where id='next'",
        ),
        ("smt_manual_rack", f"update smt_manual_rack set id='roller' where id='{ACTIVE}'"),
        (
            "smt_manual_task_member",
            "update smt_manual_task_member set rack_work_id='lost' where id='member-manual'",
        ),
        (
            "transport_task",
            "update transport_task set owner_domain='other' where "
            "client_request_id='client-manual-ENTER'",
        ),
        (
            "smt_manual_request",
            "update smt_manual_request set previous_operation_id='a-ready' where "
            "operation_id='z-wait'",
        ),
        (
            "smt_manual_request",
            "update smt_manual_request set previous_operation_id='z-wait' where "
            "operation_id='a-ready'",
        ),
        (
            "smt_manual_request",
            "update smt_manual_request set previous_operation_id=null where operation_id='a-ready'",
        ),
        (
            "reliable_message",
            "update reliable_message set business_key='business:foreign' where id=31",
        ),
        (
            "bin_roller_decision",
            "update bin_roller_decision set source_ref='dec-ack' where id='dec-roller'",
        ),
        (
            "smt_manual_rack_action",
            "update smt_manual_rack_action set id='dec-roller' where id='active-ENTER'",
        ),
        (
            "bin_roller_position_fact",
            "update bin_roller_position_fact set work_line_id=18 where id='pos-roller-ENTER'",
        ),
        ("bin_roller_return_batch", "update bin_roller_return_batch set opportunity_id='absent'"),
        (
            "bin_roller_decision",
            "update bin_roller_decision set unused_racks='[\"unknown\"]' where id='drain-unused'",
        ),
    ],
)
async def test_ambiguous_history_rolls_back_before_removal(schema_database, table, statement):
    url, engine, _ = await old_store(schema_database, multi_rotation=False)
    async with engine.begin() as conn:
        await conn.execute(text("set local search_path to enterprise_backend"))
        await conn.execute(text(statement))
        names = {
            table,
            "smt_manual_rack",
            "smt_manual_rack_action",
            "bin_roller_rack_cycle",
            "bin_roller_rack_action",
            "reliable_message",
            "alembic_version",
        }
        before = await snapshot(conn, names)
    result = await migrate(url, "upgrade", "sharedrackdispatch")
    assert result.returncode != 0 and "shared rack cutover:" in result.stderr
    async with engine.connect() as conn:
        assert await snapshot(conn, names) == before
        assert (
            await conn.scalar(text("select to_regclass('enterprise_backend.rack_dispatch_job')"))
            is None
        )


async def test_stored_submit_evidence_guards_even_an_empty_rack_downgrade(schema_database):
    url, engine = schema_database
    result = await migrate(url, "upgrade", "sharedrackdispatch")
    assert result.returncode == 0, result.stdout + result.stderr
    async with engine.begin() as conn:
        await conn.execute(
            text("""insert into enterprise_backend.transport_task
            (client_request_id,transport_task_id,operation_id,kind,input_digest,workline_id,
             created_at,submission_acceptance)
            values ('original','transport','operation','RACK_MOVE','digest','line',
                    now(),'{"original": true}')""")
        )
    result = await migrate(url, "downgrade", "racksubmitack")
    assert result.returncode != 0 and "downgrade would discard" in result.stderr
    async with engine.connect() as conn:
        assert await conn.scalar(
            text("select submission_acceptance from enterprise_backend.transport_task")
        ) == {"original": True}
        assert await conn.scalar(
            text("select to_regclass('enterprise_backend.rack_dispatch_action')")
        )


@pytest.mark.parametrize("complete", [True, False])
async def test_pruned_consumed_report_requires_complete_native_proof(schema_database, complete):
    url, engine, t = await old_store(schema_database)
    async with engine.begin() as conn:
        await conn.execute(t["reliable_message"].delete().where(t["reliable_message"].c.id == 31))
        if not complete:
            await conn.execute(
                t["smt_manual_request"]
                .update()
                .where(t["smt_manual_request"].c.operation_id == "report")
                .values(response=None)
            )
        before = await snapshot(conn, ("smt_manual_request", "reliable_message", "transport_task"))
    result = await migrate(url, "upgrade", "sharedrackdispatch")
    assert (result.returncode == 0) == complete, result.stdout + result.stderr
    async with engine.connect() as conn:
        after = await snapshot(conn, ("smt_manual_request", "reliable_message", "transport_task"))
        assert (
            after["reliable_message"] == before["reliable_message"]
            and after["transport_task"] == before["transport_task"]
        )
        assert [r for r in after["smt_manual_request"] if r["operation_id"] == "report"] == [
            r for r in before["smt_manual_request"] if r["operation_id"] == "report"
        ]
        if complete:
            assert (
                await conn.scalar(
                    text(
                        "select arrival_report_operation_id from "
                        "enterprise_backend.smt_manual_task_member where id='member-manual'"
                    )
                )
                == "report"
            )
        else:
            assert (
                await conn.scalar(
                    text("select version_num from enterprise_backend.alembic_version")
                )
                == "racksubmitack"
            )


def runtime(engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.businesses.runtime import build_runtime
    from app.integrations.ecs_command.service import EcsCommands
    from app.integrations.wms_request import ADAPTERS as REQUEST_ADAPTERS
    from app.integrations.wms_transport import ADAPTERS
    from app.reliable import Outbox
    from app.reliable.config import TargetConfig

    outbox = Outbox(
        {
            "original": TargetConfig("original", "http://127.0.0.1", "wms-transport"),
            "decisions": TargetConfig("decisions", "http://127.0.0.1", "wms-decisions"),
        },
        {**ADAPTERS, **REQUEST_ADAPTERS},
        records=history_records,
    )
    return build_runtime(
        outbox=outbox,
        ecs_commands=EcsCommands(outbox, None),
        session_factory=async_sessionmaker(engine),
    ).services["smt_manual"]


@pytest.mark.parametrize(
    "permission",
    ["active", "missing", "empty_drain", "cancelled", "disabled", "conflict", "elsewhere"],
)
async def test_old_entry_refuses_four_table_cutover_under_each_host_context(
    schema_database, permission
):
    url, engine, t = await old_store(schema_database)
    async with engine.begin() as conn:
        await conn.execute(
            t["work_lines"]
            .update()
            .where(t["work_lines"].c.id == 7)
            .values(
                line_code="L",
                line_name="L",
                business_code="smt_manual",
                position_bindings={"FIVE_RACK": "P"},
                is_enabled=permission != "disabled",
            )
        )
        if permission in ("conflict", "elsewhere"):
            await retained_observation(conn, t, "P" if permission == "conflict" else "other-point")
        if permission != "empty_drain":
            cycle, faces = t["bin_roller_rack_cycle"], t["bin_roller_face_work"]
            await conn.execute(
                cycle.update()
                .where(cycle.c.id == UNBOUND_CYCLE)
                .values(
                    source_ref="original-demand",
                    purpose="ORDINARY",
                    task_id="task-active",
                    rack_id="incoming",
                )
            )
            await conn.execute(
                t["smt_manual_task_member"]
                .insert()
                .values(
                    id="member-incoming",
                    slot_id="S",
                    task_id="task-active",
                    plan_revision=1,
                    kind="BIN",
                    rack_id="incoming",
                    rack_face="A",
                    member_order=10,
                    status="PENDING",
                )
            )
            await conn.execute(
                faces.insert().values(
                    owner_domain="smt_manual",
                    work_line_id=7,
                    purpose="SUPPLY",
                    id="original-demand",
                    demand_id="original-demand",
                    service_id="original-service",
                    cycle_id=UNBOUND_CYCLE,
                    source_ref="original-demand",
                    change_id="plan-active",
                    task_id="task-active",
                    plan_revision=1,
                    service_state="PENDING",
                    demand_state="WITHDRAWN" if permission == "cancelled" else "ACTIVE",
                    rack_id="incoming",
                    rack_face="A",
                )
            )
    result = await migrate(url, "upgrade", "sharedrackdispatch")
    assert result.returncode == 0, result.stderr
    await assert_four_table_cutover_refused(url, engine)


@pytest.mark.parametrize("evidence", ["point", "resource", "observation", "configured"])
async def test_cancelled_entry_stays_pending_at_other_protected_location(schema_database, evidence):
    url, engine, t = await old_store(schema_database, runtime_subject="cancelled")
    async with engine.begin() as conn:
        await conn.execute(
            t["work_lines"]
            .update()
            .where(t["work_lines"].c.id == 7)
            .values(
                position_bindings={
                    "FIVE_RACK": "P-active",
                    "RETURN_RACK": "P-manual",
                    "TRANSFER_RACK": "P-active",
                }
            )
        )
        # The still-needed DIRECT grant has its own line and a provable original member.
        for key in ("smt_manual_task", "smt_manual_rack", "smt_manual_input"):
            table = t[key]
            values = {"work_line_id": 8}
            if key != "smt_manual_input":
                values["workline_code"] = "L8"
            if key == "smt_manual_task":
                values["status"] = "EXECUTING"
            await conn.execute(
                table.update().where(table.c.task_id == "task-cancelled").values(**values)
            )
        await conn.execute(
            t["smt_manual_task_member"]
            .update()
            .where(t["smt_manual_task_member"].c.id == "member-cancelled")
            .values(status="PENDING")
        )
        await conn.execute(
            t["work_lines"]
            .insert()
            .values(
                id=8,
                line_code="L8",
                line_name="L8",
                business_code="smt_manual",
                position_bindings={"RETURN_RACK": "P"},
            )
        )
        if evidence == "configured":
            await conn.execute(
                t["work_lines"]
                .update()
                .where(t["work_lines"].c.id == 7)
                .values(
                    line_code="L",
                    line_name="L",
                    business_code="smt_manual",
                    position_bindings={"RETURN_RACK": "Z"},
                )
            )
        elif evidence == "point":
            await conn.execute(
                t["smt_manual_rack"]
                .insert()
                .values(
                    id="protected",
                    task_id="task-active",
                    work_line_id=7,
                    workline_code="L",
                    rack_id="other",
                    role="RETURN",
                    location_code="Z",
                    initial_face="A",
                    caller_workline_id="caller",
                    state="ACTIVE",
                    pending=True,
                )
            )
        elif evidence == "resource":
            cycle = t["bin_roller_rack_cycle"]
            resources = await conn.scalar(
                sa.select(cycle.c.resources).where(cycle.c.id == ORDINARY)
            )
            await conn.execute(
                cycle.update()
                .where(cycle.c.id == ORDINARY)
                .values(
                    resources={**resources, "positions": {**resources["positions"], "INLET": "Z"}}
                )
            )
        else:
            await conn.execute(
                t["transport_task"]
                .update()
                .where(t["transport_task"].c.transport_task_id == ACTIVE_TRANSPORT)
                .values(
                    position_observation=dict(
                        transport_task_id=ACTIVE_TRANSPORT,
                        kind="RACK_MOVE",
                        rack_id="R",
                        outcome_revision=2,
                        final_position={"kind": "RACK_POSITION", "location_code": "Z"},
                        arrival_face="A",
                    )
                )
            )
    result = await migrate(url, "upgrade", "sharedrackdispatch")
    assert result.returncode == 0, result.stdout + result.stderr
    async with engine.connect() as conn:
        (job,) = [
            r
            for r in (await snapshot(conn, ("rack_dispatch_job",)))["rack_dispatch_job"]
            if r["id"] == CANCELLED
        ]
        assert (
            job["pending"]
            and job["review_reason"] == "LEGACY_ENTRY_CANCELLED"
            and job["closure_reason"] is None
        )

    if evidence == "resource":
        # The isolated supported-runtime case uses a retained MANUAL job's frozen resource.
        async with engine.begin() as conn:
            metadata = sa.MetaData(schema="enterprise_backend")
            await conn.run_sync(metadata.reflect, only=["rack_dispatch_job"])
            jobs = metadata.tables["enterprise_backend.rack_dispatch_job"]
            resource = await conn.scalar(sa.select(jobs.c.resources).where(jobs.c.id == ORDINARY))
            await conn.execute(jobs.update().where(jobs.c.id == ACTIVE).values(resources=resource))
    await isolate_retained_manual_runtime(url, engine)
    host = runtime(engine)
    assert await host.advance("MANUAL_RACK", CANCELLED) == "POSITION_CONFLICT"
    if evidence == "configured":
        async with engine.begin() as conn:
            await conn.execute(
                t["work_lines"]
                .update()
                .where(t["work_lines"].c.id == 7)
                .values(position_bindings={})
            )
            await conn.execute(
                text(
                    "DELETE FROM enterprise_backend.workline_positions "
                    "WHERE work_line_id=7 AND position_code='Z'"
                )
            )
        assert await host.advance("MANUAL_RACK", CANCELLED) == "NOT_ARRIVED"


async def test_unconsumed_bound_transfer_result_advances_original_manual_job(schema_database):
    url, engine, t = await old_store(schema_database)
    async with engine.begin() as conn:
        tt = t["transport_task"]
        outcome = dict(
            transport_task_id=ACTIVE_TRANSPORT,
            kind="RACK_MOVE",
            rack_id="R",
            status="SUCCEEDED",
            outcome_revision=1,
            final_position={"kind": "RACK_POSITION", "location_code": "P"},
            arrival_face="A",
            position_unknown=None,
            results=None,
            failure_code=None,
        )
        await conn.execute(
            tt.update()
            .where(tt.c.transport_task_id == ACTIVE_TRANSPORT)
            .values(applied_outcome=outcome, applied_outcome_revision=1, outcome_status="SUCCEEDED")
        )
    result = await migrate(url, "upgrade", "sharedrackdispatch")
    assert result.returncode == 0, result.stderr
    await isolate_retained_manual_runtime(url, engine)
    host = runtime(engine)
    async with host.capabilities.session_factory() as db:
        before = await host.rack_dispatch.progress(db, job_id=ACTIVE)
        assert before.actions[0].outcome_revision is None
    await host.advance("MANUAL_RACK", ACTIVE)
    async with host.capabilities.session_factory() as db:
        job = await host.rack_dispatch.progress(db, job_id=ACTIVE)
        assert job.position_action_id == "active-ENTER" and job.actual_face == "A"
        assert job.actions[0].outcome_revision == 1 and job.actions[0].phase == "APPLIED"
        assert job.actions[0].submit_operation_id == before.actions[0].submit_operation_id


async def test_old_roller_position_preserves_original_kind_before_cutover_refusal(schema_database):
    url, engine, t = await old_store(schema_database)
    async with engine.begin() as conn:
        move, task, decision, position = (
            t[k]
            for k in (
                "bin_roller_rack_action",
                "transport_task",
                "bin_roller_decision",
                "bin_roller_position_fact",
            )
        )
        await conn.execute(move.delete().where(move.c.id == "ack-DEPART"))
        await conn.execute(decision.delete().where(decision.c.id == "dec-ack"))
        await conn.execute(task.delete().where(task.c.transport_task_id == "tt-ack-DEPART"))
        await conn.execute(t["reliable_message"].delete().where(t["reliable_message"].c.id == 87))
        await conn.execute(move.update().where(move.c.id == "ack-ENTER").values(status="CANCELLED"))
        await conn.execute(
            position.update()
            .where(position.c.id == "pos-ack-ENTER")
            .values(location_code="EXTERNAL", rack_face=None)
        )
        original = await conn.scalar(
            sa.select(task.c.applied_outcome).where(task.c.transport_task_id == "tt-ack-ENTER")
        )
        await conn.execute(
            task.update()
            .where(task.c.transport_task_id == "tt-ack-ENTER")
            .values(
                applied_outcome={
                    **original,
                    "status": "CANCELLED",
                    "final_position": {"kind": "ZONE", "location_code": "EXTERNAL"},
                    "arrival_face": None,
                },
                outcome_status="CANCELLED",
            )
        )
        before = await snapshot(conn, ("bin_roller_position_fact", "transport_task"))
    result = await migrate(url, "upgrade", "sharedrackdispatch")
    assert result.returncode == 0, result.stderr
    async with engine.connect() as conn:
        assert await snapshot(conn, tuple(before)) == before
    await assert_four_table_cutover_refused(url, engine)


async def test_fallback_return_keeps_later_member_unbound(schema_database):
    url, engine, t = await old_store(schema_database)
    async with engine.begin() as conn:
        await conn.execute(
            t["work_lines"]
            .update()
            .where(t["work_lines"].c.id == 7)
            .values(
                line_code="L",
                line_name="L",
                business_code="smt_manual",
                position_bindings={"RETURN_RACK": "RETURN"},
            )
        )
        await conn.execute(
            t["smt_manual_rack"]
            .insert()
            .values(
                id="fallback",
                task_id="task-active",
                work_line_id=7,
                workline_code="L",
                rack_id="other",
                role="RETURN",
                location_code="RETURN",
                initial_face="A",
                caller_workline_id="caller",
                state="ACTIVE",
                pending=True,
            )
        )
    result = await migrate(url, "upgrade", "sharedrackdispatch")
    assert result.returncode == 0, result.stderr
    await isolate_retained_manual_runtime(url, engine)
    host = runtime(engine)
    async with engine.begin() as conn:
        await conn.execute(
            t["smt_manual_input"]
            .insert()
            .values(
                operation="sorting.task.plan_delta@v1",
                operation_id="later-plan",
                task_id="task-active",
                work_line_id=7,
                timestamp=7,
                payload={
                    "task_id": "task-active",
                    "plan_revision": 2,
                    "added_direct_picks": [
                        {
                            "source_locator": {
                                "type": "RACK_SLOT",
                                "rack_id": "other",
                                "rack_face": "A",
                                "slot_id": "S",
                            }
                        }
                    ],
                },
                state="APPLIED",
                is_plan_record=True,
                plan_revision=2,
            )
        )
        await conn.execute(
            t["smt_manual_task"]
            .update()
            .where(t["smt_manual_task"].c.task_id == "task-active")
            .values(plan_revision=2)
        )
        await conn.execute(
            t["smt_manual_task_member"]
            .insert()
            .values(
                id="later",
                task_id="task-active",
                plan_revision=2,
                kind="DIRECT",
                rack_id="other",
                rack_face="A",
                slot_id="S",
                member_order=0,
                status="PENDING",
            )
        )
    async with host.capabilities.session_factory.begin() as db:
        legacy = await host.rack_dispatch.progress(db, job_id="fallback")
        assert legacy.origin.source_ref == "legacy/manual/fallback" and legacy.pending
        await host.manual_racks.reconcile_task(db, task_id="task-active")
        member = (
            (
                await db.execute(
                    sa.select(t["smt_manual_task_member"]).where(
                        t["smt_manual_task_member"].c.id == "later"
                    )
                )
            )
            .mappings()
            .one()
        )
        assert member["rack_work_id"] is None


@pytest.mark.parametrize(
    ("kind", "case"),
    [
        ("ROTATE", "active"),
        ("DEPART", "active"),
        ("DEPART", "failed_at_point"),
        ("DEPART", "cancelled_at_point"),
        ("DEPART", "failed_with_return"),
        ("DEPART", "cancelled_with_return"),
        ("DEPART", "success_with_return"),
        ("ROTATE", "withdrawn"),
        ("ROTATE", "conflict"),
    ],
)
async def test_old_followup_move_refuses_four_table_cutover_under_each_host_context(
    schema_database, kind, case
):
    url, engine, t = await old_store(schema_database)
    cycle = ORDINARY if kind == "ROTATE" else ACK
    client = "019a1111-2222-7333-8444-555555555589"
    async with engine.begin() as conn:
        line, move, position = (
            t[n] for n in ("work_lines", "bin_roller_rack_action", "bin_roller_position_fact")
        )
        await conn.execute(
            line.update()
            .where(line.c.id == 7)
            .values(
                line_code="L",
                line_name="L",
                business_code="smt_manual",
                is_enabled=False,
                position_bindings={"FIVE_RACK": "P"},
            )
        )
        if kind == "DEPART":
            await conn.execute(
                t["transport_task"]
                .delete()
                .where(t["transport_task"].c.transport_task_id == "tt-ack-DEPART")
            )
            await conn.execute(
                t["reliable_message"].delete().where(t["reliable_message"].c.id == 87)
            )
            await conn.execute(
                move.update()
                .where(move.c.id == "ack-DEPART")
                .values(client_request_id=client, transport_task_id=None, operation_id=None)
            )
            prior = "pos-ack-ENTER"
        else:
            faces = t["bin_roller_face_work"]
            await conn.execute(
                faces.update()
                .where(faces.c.id == "demand")
                .values(service_state="COMPLETED", feed_state="COMPLETED")
            )
            await conn.execute(
                t["bin_roller_return_opportunity"]
                .update()
                .where(t["bin_roller_return_opportunity"].c.id == "next")
                .values(ended=True)
            )
            await conn.execute(
                faces.insert().values(
                    purpose="SUPPLY",
                    id="turn-grant",
                    demand_id="turn-grant",
                    service_id="turn-service",
                    source_ref="turn-grant",
                    plan_revision=2,
                    change_id="turn-plan",
                    task_id="task-active",
                    cycle_id=cycle,
                    rack_id="R",
                    rack_face="B",
                    service_state="PENDING",
                    demand_state="ACTIVE",
                    owner_domain="smt_manual",
                    work_line_id=7,
                )
            )
            await conn.execute(
                move.insert().values(
                    id="local-rotate",
                    owner_domain="smt_manual",
                    work_line_id=7,
                    cycle_id=cycle,
                    kind="CTU02",
                    source_ref="turn-service",
                    client_request_id=client,
                    target={"kind": "RACK_POSITION", "location_code": "P"},
                    target_face="B",
                    caller_workline_id="caller",
                    status="PENDING",
                    pending=True,
                )
            )
            prior = "pos-ordinary-ENTER"
        if case.startswith(("failed_", "cancelled_")):
            status = "FAILED" if case.startswith("failed_") else "CANCELLED"
            face = "B" if status == "FAILED" else None
            tt = t["transport_task"]
            task = tt.c.transport_task_id == "tt-ack-ENTER"
            outcome = await conn.scalar(sa.select(tt.c.applied_outcome).where(task))
            await conn.execute(
                tt.update()
                .where(task)
                .values(
                    outcome_status=status,
                    applied_outcome={
                        **outcome,
                        "status": status,
                        "arrival_face": face,
                        "failure_code": "MANUAL_ABORTED" if status == "FAILED" else None,
                    },
                )
            )
            await conn.execute(move.update().where(move.c.id == "ack-ENTER").values(status=status))
            await conn.execute(
                position.update().where(position.c.id == prior).values(rack_face=face)
            )
            dec = t["bin_roller_decision"]
            request = await conn.scalar(sa.select(dec.c.request).where(dec.c.id == "dec-ack"))
            await conn.execute(
                dec.update()
                .where(dec.c.id == "dec-ack")
                .values(request={**request, "current_face": face})
            )
        if case == "conflict":
            await retained_observation(conn, t, "P")
        await conn.execute(position.update().where(position.c.id == prior).values(is_current=True))
    result = await migrate(url, "upgrade", "sharedrackdispatch")
    assert result.returncode == 0, result.stderr
    await assert_four_table_cutover_refused(url, engine)


async def retained_observation(conn, t, point):
    task = t["transport_task"]
    original = await conn.scalar(
        sa.select(task.c.frozen).where(task.c.transport_task_id == ACTIVE_TRANSPORT)
    )
    await conn.execute(
        task.update()
        .where(task.c.transport_task_id == ACTIVE_TRANSPORT)
        .values(
            position_observation=dict(
                transport_task_id=ACTIVE_TRANSPORT,
                kind="RACK_MOVE",
                rack_id=original["rack_id"],
                status="SUCCEEDED",
                outcome_revision=2,
                final_position={"kind": "RACK_POSITION", "location_code": point},
                arrival_face="B",
                failure_code=None,
                position_unknown=None,
                results=None,
            )
        )
    )


@pytest.mark.parametrize("contested", [True, False])
async def test_old_ready_feed_with_position_observation_refuses_cutover(schema_database, contested):
    url, engine, t = await old_store(schema_database)
    async with engine.begin() as conn:
        await retained_observation(conn, t, "P" if contested else "elsewhere")
        cycles = t["bin_roller_rack_cycle"]
        config = await conn.scalar(sa.select(cycles.c.resources).where(cycles.c.id == ORDINARY))
        await conn.execute(
            cycles.update()
            .where(cycles.c.id == ORDINARY)
            .values(
                resources={
                    **config,
                    "positions": {**config["positions"], "INLET": "IN", "OUTLET": "OUT"},
                }
            )
        )
        for key, ident in (
            ("bin_roller_return_opportunity", "next"),
            ("bin_roller_feed_segment", "segment"),
        ):
            await conn.execute(t[key].delete().where(t[key].c.id == ident))
        await conn.execute(
            t["transport_task"]
            .delete()
            .where(t["transport_task"].c.transport_task_id == "box-original")
        )
        face = t["bin_roller_face_work"]
        await conn.execute(
            face.update()
            .where(face.c.id == "demand")
            .values(
                manifest={
                    "result": "READY",
                    "bins": [
                        {
                            "bin_code": "box",
                            "source_locator": {"rack_id": "R", "rack_face": "A", "slot_id": "S"},
                        }
                    ],
                }
            )
        )
        pos = t["bin_roller_position_fact"]
        await conn.execute(
            pos.update().where(pos.c.id == "pos-ordinary-ENTER").values(is_current=True)
        )
    result = await migrate(url, "upgrade", "sharedrackdispatch")
    assert result.returncode == 0, result.stderr
    await assert_four_table_cutover_refused(url, engine)


@pytest.mark.parametrize("ack", [True, False])
async def test_old_departure_retains_opaque_preparation_before_cutover_refusal(
    schema_database, ack
):
    url, engine, t = await old_store(schema_database)
    async with engine.begin() as conn:
        await retained_observation(conn, t, "P")
        tt, m = t["transport_task"], t["bin_roller_rack_action"]
        await conn.execute(
            tt.update()
            .where(tt.c.transport_task_id == "tt-ack-ENTER")
            .values(operation_id="old-entry-op")
        )
        await conn.execute(
            m.update().where(m.c.id == "ack-ENTER").values(operation_id="old-entry-op")
        )
        if not ack:
            wire = t["reliable_message"]
            await conn.execute(
                wire.update()
                .where(wire.c.id == 87)
                .values(
                    response_raw=None,
                    last_http_status=None,
                    last_verdict=None,
                    status="PENDING",
                    attempts=0,
                )
            )
        before = await snapshot(conn, ("transport_task", "reliable_message"))
    result = await migrate(url, "upgrade", "sharedrackdispatch")
    assert result.returncode == 0, result.stderr
    async with engine.connect() as conn:
        assert await snapshot(conn, tuple(before)) == before
    await assert_four_table_cutover_refused(url, engine)


async def test_bound_departure_with_unreadable_original_operation_refuses_cutover(schema_database):
    url, engine, t = await old_store(schema_database)
    async with engine.begin() as conn:
        tt = t["transport_task"]
        await conn.execute(t["reliable_message"].delete().where(t["reliable_message"].c.id == 87))
        await conn.execute(
            tt.update()
            .where(tt.c.transport_task_id == "tt-ack-DEPART")
            .values(operation_id="opaque-departure")
        )
        names = ("transport_task", "bin_roller_rack_action", "reliable_message", "alembic_version")
        before = await snapshot(conn, names)
    result = await migrate(url, "upgrade", "sharedrackdispatch")
    assert result.returncode != 0 and "shared rack cutover:" in result.stderr
    async with engine.connect() as conn:
        assert await snapshot(conn, names) == before
