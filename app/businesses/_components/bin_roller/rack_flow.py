"""BIN_Roller owns face, feed and return responsibilities for each shared rack job."""

import json
from time import time
from typing import TYPE_CHECKING

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.businesses._components.rack_dispatch.contracts import (
    Coverage,
    DirectHandoff,
    FactRef,
    JobOrigin,
    PointDemand,
    PointRef,
    RackProgress,
)
from app.core.identifiers import uuid7
from app.integrations.wms_transport import HandoffPosition, TransportCaller
from app.models import WorkLine

if TYPE_CHECKING:
    from .service import BinRoller


async def register_job(
    roller, session, repo, kind, source_ref, rack, face, task_id, resources, *, cycle_id=None
):
    return await roller.rack_dispatch.register(
        session,
        JobOrigin(
            kind,
            source_ref,
            repo.work_line_id,
            rack,
            task_id,
            resources["workline_code"] if resources else None,
            resources["positions"]["FIVE_RACK"] if resources else None,
            face,
            TransportCaller(resources["caller_workline_id"], resources["station_id"])
            if resources
            else None,
            cycle_id or str(uuid7()),
            resources,
        ),
    )


async def select_supply_cycle(roller, session, demand_id):
    demand = await roller.original_record(session, "supply_demand", demand_id)
    if demand["cycle_id"]:
        return demand["cycle_id"]
    if demand["demand_state"] == "WITHDRAWN":
        return None
    repo = roller.repository(demand["work_line_id"])
    current = await roller.production.read_current_task(session, work_line_id=repo.work_line_id)
    if not current.is_valid or current.task_id != demand["task_id"]:
        return None
    jobs = await roller.rack_dispatch.jobs(
        session,
        work_line_id=repo.work_line_id,
        source_kinds=("ROLLER_ORDINARY",),
        task_id=demand["task_id"],
        pending=True,
    )
    for job in jobs:
        if job.origin.rack_id == demand["rack_id"]:
            return job.id if job.state == "ACTIVE" else None
    resources = await roller.read_resources(session, repo.work_line_id)
    if resources is None:
        return None
    faces = repo.tables["face_work"]
    first = (
        (
            await session.execute(
                select(faces)
                .where(
                    *repo.scoped(faces),
                    faces.c.purpose == "SUPPLY",
                    faces.c.task_id == demand["task_id"],
                    faces.c.rack_id == demand["rack_id"],
                    faces.c.demand_state == "ACTIVE",
                    faces.c.cycle_id.is_(None),
                    faces.c.service_id.is_(None),
                )
                .order_by(faces.c.id)
                .limit(1)
            )
        )
        .mappings()
        .one()
    )
    job_id = await register_job(
        roller,
        session,
        repo,
        "ROLLER_ORDINARY",
        first["id"],
        first["rack_id"],
        first["rack_face"],
        first["task_id"],
        resources,
    )
    await repo.bind_supply(session, demand_id=first["id"], cycle_id=job_id)
    return job_id


def row_fact(name, table, row):
    keys = tuple(col.name for col in table.primary_key)
    return FactRef(
        name,
        tuple(str(row[k]) for k in keys),
        tuple(
            json.dumps(row[c.name], sort_keys=True, default=str, ensure_ascii=False)
            for c in table.c
            if c.name not in keys
        ),
    )


async def evaluate_roller_point(
    roller: "BinRoller", session: AsyncSession, job: RackProgress
) -> tuple[PointDemand, DirectHandoff]:
    origin = job.origin
    if not origin.point:
        raise ValueError("roller point evaluation requires the original bound point")
    repo = roller.repository(origin.work_line_id)
    point = PointRef(repo.owner_domain, repo.work_line_id, job.id, origin.rack_id, origin.point)
    scope = (point.owner_domain, str(point.work_line_id), job.id, origin.rack_id, origin.point)
    ordinary = origin.source_kind == "ROLLER_ORDINARY"
    task = await roller.production.read_current_task(session, work_line_id=repo.work_line_id)
    successor = await roller.production.read_successor(session, work_line_id=repo.work_line_id)
    if ordinary:
        task_coverage = await roller.production.read_task_coverage(
            session, point=point, task_id=origin.task_id
        )
        anchor = task_coverage.anchor if task_coverage else None
    else:
        drains = repo.tables["decision"]
        decision = await repo._one(
            session, drains, drains.c.kind == "DRAIN", drains.c.id == origin.source_ref
        )
        anchor = (
            row_fact("drain_decision", drains, decision)
            if decision and decision["decision"] and decision["decision"]["result"] == "READY"
            else None
        )
    if not anchor:
        return PointDemand(point, "UNKNOWN", ()), DirectHandoff(point, "UNKNOWN", ())
    coverage = [task_coverage] if ordinary else []

    async def rows(name, *criteria):
        table = repo.tables[name]
        originals = (
            (
                await session.execute(
                    select(table).where(*repo.scoped(table), *criteria).order_by(*table.primary_key)
                )
            )
            .mappings()
            .all()
        )
        coverage.append(
            Coverage(name, scope, anchor, tuple(row_fact(name, table, r) for r in originals))
        )
        return originals

    faces = repo.tables["face_work"]
    valid_range = faces.c.cycle_id == job.id
    if ordinary:
        valid_range |= (
            faces.c.cycle_id.is_(None)
            & (faces.c.task_id == origin.task_id)
            & (faces.c.rack_id == origin.rack_id)
        )
    members = await rows("face_work", valid_range)
    own_actions = {a.id for a in job.actions}
    preparations = {(a.id, a.target_face) for a in job.actions if a.kind in ("ENTER", "ROTATE")}
    if (
        ordinary
        and not any(
            r["purpose"] == "SUPPLY" and r["demand_id"] == origin.source_ref for r in members
        )
        or any(
            r["rack_id"] != origin.rack_id
            or r["task_id"] != origin.task_id
            or (
                r["preparation_action_id"]
                and (r["preparation_action_id"], r["rack_face"]) not in preparations
            )
            for r in members
        )
    ):
        return PointDemand(point, "UNKNOWN", tuple(coverage)), DirectHandoff(
            point, "UNKNOWN", tuple(coverage)
        )
    segments = await rows(
        "feed_segment",
        repo.tables["feed_segment"].c.feed_id.in_([r["feed_id"] for r in members if r["feed_id"]]),
    )
    opportunities = await rows(
        "return_opportunity",
        repo.tables["return_opportunity"].c.service_id.in_(
            [r["service_id"] for r in members if r["service_id"]]
        ),
    )
    batch_table = repo.tables["return_batch"]
    opportunity_ids = {o["id"] for o in opportunities}
    selected_batches = batch_table.c.opportunity_id.in_(opportunity_ids)
    if not ordinary:
        selected_batches |= batch_table.c.pending
    line_batches = await rows("return_batch", selected_batches)
    batches = [row for row in line_batches if row["opportunity_id"] in opportunity_ids]
    drains = repo.tables["decision"]
    await rows("decision", drains.c.pending | (drains.c.id == origin.source_ref))
    fifo = await repo.return_queue(session)
    arrivals = repo.tables["scan_arrival"]
    rounds = {r["id"] for r in fifo if r["id"]} | {
        m["round_id"] for batch in line_batches for m in batch["members"]
    }
    await rows(
        "scan_arrival",
        arrivals.c.point.in_(("SCAN3", "SCAN4")),
        arrivals.c.round_id.in_(rounds)
        | arrivals.c.id.in_([r["fifo_arrival_id"] for r in fifo])
        | (
            repo.current_round_condition()
            & arrivals.c.removed.is_(False)
            & (arrivals.c.return_state != "COMPLETED")
        ),
    )
    # A FIFO entry can be pending before its SCAN3/SCAN4 pair is fully associated.
    coverage.append(
        Coverage(
            "return_queue",
            scope,
            anchor,
            tuple(
                FactRef(
                    "fifo",
                    (r["fifo_arrival_id"],),
                    (json.dumps(dict(r), sort_keys=True, default=str),),
                )
                for r in fifo
            ),
        )
    )
    positions_table = repo.tables["position_fact"]
    rack_positions = (positions_table.c.object_kind == "RACK") & (
        (positions_table.c.object_code == origin.rack_id)
        | ((positions_table.c.location_code == origin.point) & positions_table.c.is_current)
    )
    bins = {m["bin_code"] for record in (*segments, *batches) for m in record["members"]}
    positions = await rows(
        "position_fact",
        rack_positions
        | (
            (positions_table.c.object_kind == "BIN")
            & positions_table.c.object_code.in_(bins)
            & positions_table.c.is_current
        ),
    )
    coverage.append(
        Coverage(
            "current_task",
            scope,
            anchor,
            (FactRef("task", (task.task_id,), (str(task.is_valid),)),) if task.task_id else (),
        )
    )
    original_successor = successor.evidence
    successor_keys = [
        original_successor[k]
        for k in (
            "task_id",
            "prepare_operation_id",
            "confirmation_operation_id",
            "cancel_operation_id",
        )
        if original_successor.get(k)
    ]
    successor_keys.extend(original_successor.get("queued_task_ids", ()))
    coverage.append(
        Coverage(
            "successor",
            scope,
            anchor,
            (
                FactRef(
                    "successor",
                    tuple(successor_keys) or (str(repo.work_line_id),),
                    (successor.state, json.dumps(original_successor, sort_keys=True)),
                ),
            ),
        )
    )
    feeding = any(r["feed_id"] and r["feed_state"] != "COMPLETED" for r in members)
    open_opportunity = any(not r["ended"] for r in opportunities)
    responsibility = feeding or open_opportunity or any(r["pending"] for r in (*segments, *batches))
    failed = {
        a.id
        for a in job.actions
        if a.phase == "APPLIED" and a.outcome and a.outcome["status"] in ("FAILED", "CANCELLED")
    }
    remaining = any(
        r["service_state"] not in ("COMPLETED", "WITHDRAWN")
        and (
            r["cycle_id"] == job.id
            and (
                r["demand_state"] == "ACTIVE"
                or (r["preparation_action_id"] and r["preparation_action_id"] not in failed)
                or r["feed_id"]
            )
            or r["cycle_id"] is None
            and task.is_valid
            and task.task_id == origin.task_id
            and r["demand_state"] == "ACTIVE"
            and job.state == "ACTIVE"
        )
        for r in members
    )
    if not ordinary:
        candidate = next(
            (r for r in decision["decision"]["racks"] if r["rack_id"] == origin.rack_id), None
        )
        if not candidate:
            return PointDemand(point, "UNKNOWN", tuple(coverage)), DirectHandoff(
                point, "UNKNOWN", tuple(coverage)
            )
        done = {r["rack_face"] for r in members if r["service_state"] == "COMPLETED"}
        remaining = bool(
            not await roller.return_channel_empty(session, work_line_id=repo.work_line_id)
            and set(candidate["rack_face"]) - done
            and origin.rack_id not in decision["unused_racks"]
        )
    if ordinary and task_coverage.rows and not remaining:
        return PointDemand(point, "UNKNOWN", tuple(coverage)), DirectHandoff(
            point, "UNKNOWN", tuple(coverage)
        )
    conflict = any(
        r["object_kind"] == "RACK" and r["is_current"] and r["source_id"] not in own_actions
        for r in positions
    )
    return PointDemand(point, "CONTINUE" if remaining else "NONE", tuple(coverage)), DirectHandoff(
        point, "BLOCKED" if responsibility or conflict else "CLEAR", tuple(coverage)
    )


async def refresh_job(roller, session, job):
    demand, handoff = await evaluate_roller_point(roller, session, job)
    job = await roller.rack_dispatch.advance(
        session, job_id=job.id, demand=demand, handoff=handoff, now_ms=int(time() * 1000)
    )
    repo = roller.repository(job.origin.work_line_id)
    for action in job.actions:
        if action.actual_position and action.position_revision is not None:
            await repo.record_position(
                session,
                object_kind="RACK",
                object_code=job.origin.rack_id,
                location_code=action.actual_position["location_code"],
                rack_face=action.actual_face,
                source_kind="RACK",
                source_id=action.id,
                source_revision=action.position_revision,
                history_only=not job.pending or action.id != job.position_action_id,
            )
    if not job.pending:
        positions = repo.tables["position_fact"]
        await session.execute(
            positions.update()
            .where(
                *repo.scoped(positions),
                positions.c.source_kind == "RACK",
                positions.c.source_id.in_([a.id for a in job.actions]),
                positions.c.is_current,
            )
            .values(is_current=False)
        )
    return job, demand.state


def cycle_progress(job):
    if job.closure_reason == "DEPARTURE_ACK":
        return "RELEASED"
    if job.state == "RELEASING":
        if job.review_reason in (
            "INVALID_DEPARTURE_REQUEST",
            "INVALID_DEPARTURE_DECISION",
            "INVALID_DEPARTURE_DESTINATION",
        ):
            return "INVALID_DEPARTURE"
        return (
            "WAITING_FOR_DEPARTURE_ACCEPTANCE"
            if job.actions[-1].kind == "DEPART"
            else "WAITING_FOR_DEPARTURE"
        )
    return job.state


async def advance_cycle(roller, session, cycle_id):
    from .feed_flow import opportunity, prepare_feed, start_feed
    from .service import ROLLER_SOURCES

    job = await roller.rack_dispatch.progress(session, job_id=cycle_id)
    if job is None or job.origin.source_kind not in ROLLER_SOURCES:
        raise ValueError("original roller job is missing")
    repo = roller.repository(job.origin.work_line_id)
    services = repo.tables["face_work"]
    ordinary = job.origin.source_kind == "ROLLER_ORDINARY"
    if not job.pending:
        return cycle_progress(job)
    if (
        not ordinary
        and not job.actions
        and not await roller.get_return_candidates(session, work_line_id=repo.work_line_id)
    ):
        from .return_flow import summarize_drain

        await summarize_drain(roller, session, job.origin.source_ref)
        return (await roller.rack_dispatch.progress(session, job_id=cycle_id)).state
    if not job.origin.resources:
        resources = await roller.read_resources(session, repo.work_line_id)
        if resources is None:
            return "WAITING_FOR_RACK"
        await register_job(
            roller,
            session,
            repo,
            job.origin.source_kind,
            job.origin.source_ref,
            job.origin.rack_id,
            job.origin.initial_face,
            job.origin.task_id,
            resources,
            cycle_id=job.id,
        )
        job = await roller.rack_dispatch.progress(session, job_id=cycle_id)
    if ordinary:
        failed = [
            a.id
            for a in job.actions
            if a.phase == "APPLIED" and a.outcome and a.outcome["status"] in ("FAILED", "CANCELLED")
        ]
        cancelled = [
            *repo.scoped(services),
            services.c.cycle_id == cycle_id,
            services.c.demand_state == "WITHDRAWN",
            services.c.feed_id.is_(None),
            (
                services.c.preparation_action_id.is_(None)
                | services.c.preparation_action_id.in_(failed)
            ),
        ]
        await session.execute(
            services.update()
            .where(*cancelled, services.c.service_state.in_(("PENDING", "READY")))
            .values(service_state="WITHDRAWN")
        )
        await session.execute(
            services.update()
            .where(*cancelled, services.c.service_id.is_(None))
            .values(cycle_id=None)
        )
    job, demand_state = await refresh_job(roller, session, job)
    if demand_state == "UNKNOWN":
        return "WAITING_FOR_ORIGINAL_FACTS"
    if job.state != "ACTIVE" or (job.release_requested and not job.actions):
        return cycle_progress(job)
    if (
        job.position_action_id
        and job.actual_position
        and job.actual_position["location_code"] == job.origin.point
    ):
        position = await repo.current_position(session, "RACK", job.origin.rack_id)
        if not position or (
            position["source_id"],
            position["location_code"],
            position["rack_face"],
        ) != (job.position_action_id, job.origin.point, job.actual_face):
            return "WAITING_FOR_ORIGINAL_POSITION"
    while True:
        service = await repo._one(
            session,
            services,
            services.c.cycle_id == cycle_id,
            services.c.service_state.in_(("PENDING", "READY")),
        )
        if service is None:
            if job.release_requested:
                job, _ = await refresh_job(roller, session, job)
                return cycle_progress(job)
            completed = (
                (
                    await session.execute(
                        select(services)
                        .where(
                            *repo.scoped(services),
                            services.c.cycle_id == cycle_id,
                            services.c.service_id.is_not(None),
                        )
                        .order_by(services.c.service_id)
                    )
                )
                .mappings()
                .all()
            )
            demand = None
            if ordinary:
                current_task = await roller.production.read_current_task(
                    session, work_line_id=repo.work_line_id
                )
                demands = services
                query = select(demands).where(
                    *repo.scoped(demands),
                    demands.c.purpose == "SUPPLY",
                    demands.c.task_id == job.origin.task_id,
                    demands.c.rack_id == job.origin.rack_id,
                    demands.c.service_id.is_(None),
                    or_(
                        demands.c.cycle_id == cycle_id,
                        demands.c.cycle_id.is_(None)
                        & (current_task.is_valid and current_task.task_id == job.origin.task_id),
                    ),
                    or_(demands.c.demand_state == "ACTIVE", demands.c.cycle_id == cycle_id),
                )
                demand = (
                    (await session.execute(query.order_by(demands.c.id).limit(1)))
                    .mappings()
                    .one_or_none()
                )
                if demand is not None:
                    source_ref, face = demand["id"], demand["rack_face"]
            else:
                drain = await roller.original_record(session, "drain", job.origin.source_ref)
                candidate = next(
                    c for c in drain["decision"]["racks"] if c["rack_id"] == job.origin.rack_id
                )
                remaining = [
                    f
                    for f in candidate["rack_face"]
                    if f not in {s["rack_face"] for s in completed}
                ]
                if remaining and (
                    not completed
                    or not await roller.return_channel_empty(
                        session, work_line_id=repo.work_line_id
                    )
                ):
                    face = remaining[0]
                    source_ref = drain["id"] + ":" + face
                else:
                    remaining = []
            if (ordinary and demand is None) or (not ordinary and not remaining):
                job, _ = await refresh_job(roller, session, job)
                return cycle_progress(job)
            service_id = str(uuid7())
            if demand is not None:
                await repo.bind_supply(
                    session, demand_id=demand["demand_id"], cycle_id=cycle_id, service_id=service_id
                )
                service = await repo.demand(session, demand["demand_id"])
            else:
                service = await repo._register(
                    session,
                    services,
                    dict(
                        **repo.scope,
                        id=service_id,
                        purpose="DRAIN",
                        service_id=service_id,
                        cycle_id=cycle_id,
                        source_ref=source_ref,
                        rack_id=job.origin.rack_id,
                        rack_face=face,
                        task_id=job.origin.task_id,
                        service_state="PENDING",
                    ),
                    ("cycle_id", "source_ref"),
                    ("purpose", "rack_face", "task_id"),
                )
        resources = job.origin.resources
        entry = next((a for a in job.actions if a.kind == "ENTER"), None)
        if service["service_state"] == "READY" and job.review_reason:
            return "WAITING_FOR_ORIGINAL_POSITION"
        if service["service_state"] != "READY":
            if entry is None:
                line = await session.get(WorkLine, repo.work_line_id, populate_existing=True)
                if not line or not line.is_enabled or line.business_code != repo.owner_domain:
                    return "WAITING_FOR_RACK"
                if line.five_rack_request_limit is not None:
                    active = await roller.rack_dispatch.jobs(
                        session,
                        work_line_id=repo.work_line_id,
                        source_kinds=ROLLER_SOURCES,
                        pending=True,
                    )
                    if (
                        sum(any(a.template == "CTU01" for a in j.actions) for j in active)
                        >= line.five_rack_request_limit
                    ):
                        return "WAITING_FOR_RACK"
                kind = "ENTER"
            else:
                move = next((a for a in job.actions if a.id == job.position_action_id), None)
                if (
                    not move
                    or job.review_reason
                    or move.phase != "APPLIED"
                    or move.outcome["status"] != "SUCCEEDED"
                    or move.actual_position != move.target
                    or move.position_revision != move.outcome_revision
                    or move.target_face != move.actual_face
                ):
                    return "WAITING_FOR_ORIGINAL_POSITION"
                if service["preparation_action_id"] and move.id != service["preparation_action_id"]:
                    return "WAITING_FOR_ORIGINAL_POSITION"
                kind = "ROTATE" if job.actual_face != service["rack_face"] else None
            action_id = (
                (
                    await roller.rack_dispatch.prepare(
                        session,
                        job_id=job.id,
                        kind=kind,
                        source_ref=service["service_id"],
                        face=service["rack_face"],
                    )
                )
                if kind
                else move.id
            )
            if action_id and not service["preparation_action_id"]:
                await session.execute(
                    services.update()
                    .where(*repo.scoped(services), services.c.service_id == service["service_id"])
                    .values(preparation_action_id=action_id)
                )
            if kind:
                return "WAITING_FOR_RACK"
            await session.execute(
                services.update()
                .where(*repo.scoped(services), services.c.service_id == service["service_id"])
                .values(service_state="READY")
            )
        caller = job.origin.caller
        if ordinary:
            feed_id = await start_feed(
                roller,
                session,
                demand_id=service["source_ref"],
                caller=caller,
                target=HandoffPosition(resources["positions"]["INLET"]),
            )
            await prepare_feed(roller, session, service["source_ref"])
            feed = await roller.original_record(session, "feed", feed_id)
        else:
            await opportunity(
                repo,
                session,
                feed=dict(service_id=service["service_id"]),
                source_ref=service["service_id"],
                kind="PURE_RETURN",
            )
        opportunities = repo.tables["return_opportunity"]
        current = await repo._one(
            session,
            opportunities,
            opportunities.c.service_id == service["service_id"],
            opportunities.c.ended.is_(False),
        )
        if current is not None:
            batches = repo.tables["return_batch"]
            saved = (
                (
                    await session.execute(
                        select(batches)
                        .where(*repo.scoped(batches), batches.c.opportunity_id == current["id"])
                        .order_by(batches.c.id.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .one_or_none()
            )
            source_ref = saved["id"] if saved and not saved["pending"] else current["id"]
            batch_id = await roller.start_return_batch(
                session,
                opportunity_id=current["id"],
                source_ref=source_ref,
                workline_code=resources["workline_code"],
                caller=caller,
                source=HandoffPosition(resources["positions"]["OUTLET"]),
            )
            if batch_id is not None:
                return "WAITING_FOR_RETURN"
            unresolved = await repo._one(
                session, opportunities, opportunities.c.id == current["id"]
            )
            if not unresolved["ended"]:
                return "WAITING_FOR_RETURN"
            if current["kind"] == "SEGMENT":
                await prepare_feed(roller, session, service["source_ref"])
                return "WAITING_FOR_FEED"
        if ordinary and feed["feed_state"] != "COMPLETED":
            return "WAITING_FOR_FEED"
        await session.execute(
            services.update()
            .where(*repo.scoped(services), services.c.service_id == service["service_id"])
            .values(service_state="COMPLETED")
        )
