"""Face selection delegates rack execution and original position evidence to RackDispatch."""

import json
from time import time

from sqlalchemy import select

from app.businesses._components.rack_dispatch.contracts import (
    Coverage,
    DirectHandoff,
    FactRef,
    JobOrigin,
    PointDemand,
    PointRef,
)
from app.core.identifiers import uuid7
from app.integrations.wms_transport import TransportCaller
from app.reliable import ScopeRef


async def register_job(
    roller, session, repo, kind, source_ref, rack, face, task_id, resources, *, cycle_id=None
):
    if kind == "ROLLER_ORDINARY":
        scope = ScopeRef(repo.owner_domain, "TASK", task_id)
    else:
        line = await repo.line_state(session, start=True)
        scope = ScopeRef(repo.owner_domain, "LINE", line["line_run_id"])
    return await roller.rack_dispatch.register(
        session,
        JobOrigin(
            kind,
            source_ref,
            repo.work_line_id,
            rack,
            task_id,
            resources["workline_code"],
            resources["positions"]["FIVE_RACK"],
            face,
            TransportCaller(resources["caller_workline_id"], resources["station_id"]),
            cycle_id or str(uuid7()),
            resources,
        ),
        scope=scope,
    )


async def select_supply_cycle(roller, session, demand_id):
    face = await roller.original_record(session, "face_work", demand_id)
    if face["closed_at"] or face["purpose"] != "SUPPLY":
        return None
    if face["rack_job_id"]:
        return face["rack_job_id"]
    current = await roller.production.read_current_task(session, work_line_id=face["work_line_id"])
    if (
        not current.is_valid
        or current.task_id != face["scope_id"]
        or face["demand_state"] == "WITHDRAWN"
    ):
        return None
    repo = roller.repository(face["work_line_id"])
    jobs = await roller.rack_dispatch.jobs(
        session,
        work_line_id=repo.work_line_id,
        source_kinds=("ROLLER_ORDINARY",),
        task_id=face["scope_id"],
        pending=True,
    )
    matching = [j for j in jobs if j.origin.rack_id == face["rack_id"]]
    if len(matching) > 1:
        return None
    if matching:
        job_id = (
            matching[0].id
            if matching[0].state == "ACTIVE" and not matching[0].release_requested
            else None
        )
    else:
        resources = await roller.read_resources(session, repo.work_line_id)
        if not resources:
            return None
        table = repo.tables["face_work"]
        candidates = (
            (
                await session.execute(
                    select(table).where(
                        *repo.scoped(table),
                        table.c.purpose == "SUPPLY",
                        table.c.scope_id == face["scope_id"],
                        table.c.rack_id == face["rack_id"],
                        table.c.rack_job_id.is_(None),
                        table.c.closed_at.is_(None),
                        table.c.demand_state == "ACTIVE",
                    )
                )
            )
            .mappings()
            .all()
        )
        ordered = await ordered_supply_faces(roller, session, repo, face["scope_id"], candidates)
        if not ordered:
            return None
        first = ordered[0]
        job_id = await register_job(
            roller,
            session,
            repo,
            "ROLLER_ORDINARY",
            first["id"],
            first["rack_id"],
            first["rack_face"],
            first["scope_id"],
            resources,
        )
    if job_id:
        table = repo.tables["face_work"]
        siblings = await session.scalars(
            select(table.c.id)
            .where(
                *repo.scoped(table),
                table.c.purpose == "SUPPLY",
                table.c.scope_id == face["scope_id"],
                table.c.rack_id == face["rack_id"],
                table.c.rack_job_id.is_(None),
                table.c.closed_at.is_(None),
                table.c.demand_state == "ACTIVE",
            )
            .order_by(table.c.id)
        )
        for sibling_id in siblings:
            await repo.bind_supply(session, demand_id=sibling_id, cycle_id=job_id)
    return job_id


async def ordered_supply_faces(roller, session, repo, task_id, rows):
    order = await roller.production.read_supply_order(
        session, work_line_id=repo.work_line_id, task_id=task_id
    )
    indices = {(f.plan_revision, f.rack_id, f.rack_face): n for n, f in enumerate(order)}
    if any((r["plan_revision"], r["rack_id"], r["rack_face"]) not in indices for r in rows):
        return []
    return sorted(rows, key=lambda r: indices[r["plan_revision"], r["rack_id"], r["rack_face"]])


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


def applied_preparation(job):
    if not job or job.state != "ACTIVE" or job.review_reason or not job.actions:
        return None
    move = job.actions[-1]
    if (
        move.id != job.current_action_id
        or move.id != job.position_action_id
        or move.kind not in ("ENTER", "ROTATE")
        or move.phase != "APPLIED"
        or not move.outcome
        or move.outcome["status"] != "SUCCEEDED"
        or move.target_face != job.actual_face
        or move.actual_face != job.actual_face
        or move.target != {"kind": "RACK_POSITION", "location_code": job.origin.point}
        or move.actual_position != move.target
        or job.actual_position != move.target
        or move.position_revision != move.outcome_revision
    ):
        return None
    return move


async def can_use_face(roller, session, face):
    if face["closed_at"] or not face["rack_job_id"] or not face["preparation_action_id"]:
        return False
    job = await roller.rack_dispatch.progress(session, job_id=face["rack_job_id"])
    if not job or not job.origin.point:
        return False
    prepared = await roller.rack_dispatch.get_point_preparations(
        session, work_line_id=face["work_line_id"], point=job.origin.point
    )
    return (
        len(prepared) == 1
        and prepared[0].id == job.id
        and job.position_action_id == face["preparation_action_id"]
        and job.actual_face == face["rack_face"]
        and job.origin.rack_id == face["rack_id"]
        and job.origin.task_id == (face["scope_id"] if face["purpose"] == "SUPPLY" else None)
    )


async def evaluate_roller_point(roller, session, job):
    repo = roller.repository(job.origin.work_line_id)
    point = PointRef(
        repo.owner_domain, repo.work_line_id, job.id, job.origin.rack_id, job.origin.point
    )
    scope = (
        repo.owner_domain,
        str(repo.work_line_id),
        job.id,
        job.origin.rack_id,
        job.origin.point,
    )
    ordinary = job.origin.source_kind == "ROLLER_ORDINARY"
    if ordinary:
        task = await roller.production.read_task_coverage(
            session, point=point, task_id=job.origin.task_id
        )
        if task is None:
            return PointDemand(point, "UNKNOWN", ()), DirectHandoff(point, "UNKNOWN", ())
        anchor = task.anchor
        coverage = [task]
    else:
        state = await repo.line_state(session)
        if state["drain_operation_id"] != job.origin.source_ref or state["drain_phase"] != "USING":
            return PointDemand(point, "UNKNOWN", ()), DirectHandoff(point, "UNKNOWN", ())
        anchor = row_fact("workline_state", repo.tables["workline_state"], state)
        coverage = []
    table = repo.tables["face_work"]
    condition = table.c.rack_job_id == job.id
    if ordinary:
        condition |= (
            (table.c.purpose == "SUPPLY")
            & (table.c.scope_id == job.origin.task_id)
            & (table.c.rack_id == job.origin.rack_id)
            & table.c.rack_job_id.is_(None)
        )
    faces = (
        (
            await session.execute(
                select(table).where(*repo.scoped(table), condition).order_by(table.c.id)
            )
        )
        .mappings()
        .all()
    )
    coverage.append(
        Coverage("face_work", scope, anchor, tuple(row_fact("face_work", table, f) for f in faces))
    )
    if ordinary and not any(f["id"] == job.origin.source_ref for f in faces):
        return PointDemand(point, "UNKNOWN", tuple(coverage)), DirectHandoff(
            point, "UNKNOWN", tuple(coverage)
        )
    active = [
        f
        for f in faces
        if f["closed_at"] is None and f.get("demand_state") != "WITHDRAWN"
    ]
    if (
        ordinary
        and job.actions
        and job.actions[-1].outcome
        and job.actions[-1].outcome["status"] in ("FAILED", "CANCELLED")
    ):
        for face in active:
            if face["demand_state"] == "WITHDRAWN" and not face["feed_operation_id"]:
                await repo.close(session, "face_work", face["id"], "WITHDRAWN")
        active = [f for f in active if f["demand_state"] != "WITHDRAWN" or f["feed_operation_id"]]
    if ordinary:
        current = await roller.production.read_current_task(session, work_line_id=repo.work_line_id)
        if (
            active
            and current.task_id == job.origin.task_id
            and not current.is_valid
            and not any(f["preparation_action_id"] or f["feed_operation_id"] for f in active)
        ):
            return PointDemand(point, "UNKNOWN", tuple(coverage)), DirectHandoff(
                point, "UNKNOWN", tuple(coverage)
            )
        active = [
            f
            for f in active
            if (current.is_valid and current.task_id == job.origin.task_id)
            or f["feed_operation_id"]
            or f["preparation_action_id"]
        ]
    remaining = bool(active)
    if not ordinary:
        from .return_flow import read_drain_manifest

        state = await repo.line_state(session)
        decision = await read_drain_manifest(roller, session, state)
        candidate = next(
            (c for c in (decision or {}).get("racks", ()) if c["rack_id"] == job.origin.rack_id),
            None,
        )
        if candidate is None:
            return PointDemand(point, "UNKNOWN", tuple(coverage)), DirectHandoff(
                point, "UNKNOWN", tuple(coverage)
            )
        done = {f["rack_face"] for f in faces if f["close_reason"] == "COMPLETED"}
        remaining |= bool(
            set(candidate["rack_face"]) - done
        ) and not await roller.return_channel_empty(session, work_line_id=repo.work_line_id)
    if ordinary and task.rows and not remaining:
        return PointDemand(point, "NONE", tuple(coverage)), DirectHandoff(
            point, "CLEAR", tuple(coverage)
        )
    responsibility = any(f["phase"] != "WAIT_RACK" for f in active)
    return PointDemand(point, "CONTINUE" if remaining else "NONE", tuple(coverage)), DirectHandoff(
        point, "BLOCKED" if responsibility else "CLEAR", tuple(coverage)
    )


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
    from .feed_flow import advance_face, start_feed
    from .return_flow import read_drain_manifest

    job = await roller.rack_dispatch.progress(session, job_id=cycle_id)
    if not job or job.origin.source_kind not in ("ROLLER_ORDINARY", "ROLLER_DRAIN"):
        raise ValueError("original roller rack job missing")
    if not job.pending:
        return cycle_progress(job)
    if job.state == "CLOSED_BY_SCOPE":
        point = PointRef(
            roller.context.domain,
            job.origin.work_line_id,
            job.id,
            job.origin.rack_id,
            job.origin.point,
        )
        stopped = await roller.rack_dispatch.advance(
            session,
            job_id=job.id,
            demand=PointDemand(point, "UNKNOWN", ()),
            handoff=DirectHandoff(point, "UNKNOWN", ()),
            now_ms=int(time() * 1000),
        )
        return cycle_progress(stopped)
    repo = roller.repository(job.origin.work_line_id)
    table = repo.tables["face_work"]
    ordinary = job.origin.source_kind == "ROLLER_ORDINARY"
    if (
        not ordinary
        and not job.actions
        and await roller.return_channel_empty(session, work_line_id=repo.work_line_id)
    ):
        state = await repo.line_state(session)
        if state["drain_phase"] == "USING" and state["drain_operation_id"] == job.origin.source_ref:
            from .return_flow import advance_drain

            await advance_drain(roller, session, repo.work_line_id)
            return cycle_progress(await roller.rack_dispatch.progress(session, job_id=job.id))
    demand, handoff = await evaluate_roller_point(roller, session, job)
    if demand.state == "UNKNOWN":
        return "WAITING_FOR_ORIGINAL_FACTS"
    job = await roller.rack_dispatch.advance(
        session, job_id=job.id, demand=demand, handoff=handoff, now_ms=int(time() * 1000)
    )
    if not job.pending or job.state != "ACTIVE" or job.release_requested:
        if not job.pending and not ordinary:
            state = await repo.line_state(session)
            if (
                state["drain_phase"] == "USING"
                and state["drain_operation_id"] == job.origin.source_ref
            ):
                from .return_flow import advance_drain

                await advance_drain(roller, session, repo.work_line_id)
        return cycle_progress(job)
    faces = (
        (
            await session.execute(
                select(table)
                .where(*repo.scoped(table), table.c.rack_job_id == job.id)
                .order_by(table.c.id)
            )
        )
        .mappings()
        .all()
    )
    active = [f for f in faces if f["closed_at"] is None]
    if ordinary:
        current = await roller.production.read_current_task(session, work_line_id=repo.work_line_id)
        active = [
            f
            for f in active
            if (current.is_valid and current.task_id == job.origin.task_id)
            or f["feed_operation_id"]
            or f["preparation_action_id"]
        ]
    if not active:
        if ordinary:
            current = await roller.production.read_current_task(
                session, work_line_id=repo.work_line_id
            )
            candidates = (
                (
                    await session.execute(
                        select(table).where(
                            *repo.scoped(table),
                            table.c.purpose == "SUPPLY",
                            table.c.scope_id == job.origin.task_id,
                            table.c.rack_id == job.origin.rack_id,
                            table.c.rack_job_id.is_(None),
                            table.c.closed_at.is_(None),
                            table.c.demand_state == "ACTIVE",
                        )
                    )
                )
                .mappings()
                .all()
            )
            ordered = await ordered_supply_faces(
                roller, session, repo, job.origin.task_id, candidates
            )
            candidate = ordered[0] if ordered else None
            if candidate and current.is_valid and current.task_id == job.origin.task_id:
                await repo.bind_supply(session, demand_id=candidate["id"], cycle_id=job.id)
                active = [await repo.demand(session, candidate["id"])]
        else:
            state = await repo.line_state(session)
            decision = await read_drain_manifest(roller, session, state)
            candidate = next(
                (
                    c
                    for c in (decision or {}).get("racks", ())
                    if c["rack_id"] == job.origin.rack_id
                ),
                None,
            )
            remaining = [
                f
                for f in (candidate or {}).get("rack_face", ())
                if f not in {r["rack_face"] for r in faces}
            ]
            if remaining and not await roller.return_channel_empty(
                session, work_line_id=repo.work_line_id
            ):
                face_id = str(uuid7())
                await session.execute(
                    table.insert().values(
                        id=face_id,
                        **repo.scope,
                        scope_id=state["line_run_id"],
                        purpose="DRAIN",
                        rack_id=job.origin.rack_id,
                        rack_face=remaining[0],
                        rack_job_id=job.id,
                        phase="WAIT_RACK",
                    )
                )
                active = [await repo._one(session, table, table.c.id == face_id)]
    started = [
        face for face in active if face["phase"] != "WAIT_RACK" or face["preparation_action_id"]
    ]
    if len(started) > 1:
        return "FACE_SELECTION_CONFLICT"
    if started:
        active = started
    if not active:
        return cycle_progress(job)
    face = active[0]
    if face["phase"] != "WAIT_RACK":
        progress = await advance_face(roller, session, face["id"])
        updated = await roller.original_record(session, "face_work", face["id"])
        if updated["close_reason"] == "COMPLETED":
            return await advance_cycle(roller, session, cycle_id)
        return progress
    move = applied_preparation(job)
    if move and move.target_face == face["rack_face"]:
        if face["preparation_action_id"] != move.id:
            if face["preparation_action_id"] is not None or move.source_ref == face["id"]:
                return "WAITING_FOR_ORIGINAL_POSITION"
            previous_face = await repo._one(session, table, table.c.id == move.source_ref)
            if not previous_face or previous_face["close_reason"] != "COMPLETED":
                return "WAITING_FOR_ORIGINAL_POSITION"
            await repo.update(session, "face_work", face["id"], preparation_action_id=move.id)
        face = await repo._one(session, table, table.c.id == face["id"])
        if not await can_use_face(roller, session, face):
            return "WAITING_FOR_ORIGINAL_POSITION"
        if ordinary:
            await start_feed(roller, session, demand_id=face["id"])
        else:
            await repo.update(
                session,
                "face_work",
                face["id"],
                phase="RETURN_WINDOW",
                window_id=str(uuid7()),
                window_kind="PURE_RETURN",
            )
        progress = await advance_face(roller, session, face["id"])
        updated = await roller.original_record(session, "face_work", face["id"])
        if updated["close_reason"] == "COMPLETED":
            return await advance_cycle(roller, session, cycle_id)
        return progress
    if not job.actions:
        kind = "ENTER"
    elif move:
        if ordinary:
            current = await roller.production.read_current_task(
                session, work_line_id=repo.work_line_id
            )
            if not current.is_valid or current.task_id != job.origin.task_id:
                return "WAITING_FOR_CURRENT_TASK"
        kind = "ROTATE"
    else:
        return "WAITING_FOR_ORIGINAL_RESULT"
    action_id = await roller.rack_dispatch.prepare(
        session, job_id=job.id, kind=kind, source_ref=face["id"], face=face["rack_face"]
    )
    if action_id:
        await repo.update(session, "face_work", face["id"], preparation_action_id=action_id)
    return "WAITING_FOR_RACK"
