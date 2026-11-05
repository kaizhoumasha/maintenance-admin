"""Horizontal point checkpoints linked to original public EVENT and command evidence."""

import re

from sqlalchemy import select

from app.core.identifiers import uuid7
from app.core.strict_json import canonical_digest
from app.integrations.ecs_callback import (
    EcsEventContext,
    EventApplicationRef,
    get_event,
    list_point_events,
)
from app.integrations.ecs_command.contracts import CommandView, ResultEnvelope
from app.reliable import ScopeRef

from .contracts import ScanCompletedData
from .ecs_command import MoveForward, MoveLeft, MoveRight
from .ports import Scan2Arrival

_BIN = re.compile(r"A[0-9]{9}\Z")


def bin_identity(raw: str) -> str | None:
    if len(raw) < 3 or raw[-2:] not in {"-A", "-B", "-C", "-D"}:
        return None
    code = raw[:-2]
    return code if _BIN.fullmatch(code) else None


async def get_scan_result(roller, session, code: str | None) -> CommandView | None:
    if code is None:
        return None
    return await roller.capabilities.ecs.get_command(
        session, owner=roller.context, command_code=code
    )


def applied_result(
    view: CommandView | None, code: str | None, device_code: str
) -> ResultEnvelope | None:
    if (
        view is None
        or view.result is None
        or view.hold_reason
        or view.needs_reconciliation
        or view.status not in {"SUCCEEDED", "FAILED"}
    ):
        return None
    result = ResultEnvelope.model_validate(view.result)
    if (
        view.command_code != code
        or result.command_code != code
        or result.device_code != device_code
        or view.status != ("SUCCEEDED" if result.result == "SUCCESS" else "FAILED")
    ):
        return None
    return result


async def point_event(session, row, point):
    result = await get_event(session, event_id=row[point.lower() + "_event_id"])
    return result.record if result.availability == "FOUND" else None


async def point_view(roller, session, row, point):
    event = await point_event(session, row, point)
    code_key = "release_command_code" if point == "SCAN2" else point.lower() + "_command_code"
    view = await get_scan_result(roller, session, row[code_key])
    return event, view


async def accept_scan(roller, ctx: EcsEventContext, payload: ScanCompletedData):
    if ctx.business_code != roller.context.domain:
        raise ValueError("scan business does not match component owner")
    repo = roller.repository(ctx.work_line_id)
    # The receiver owns WorkLine/source Device; acquire original jobs before host/checkpoints.
    jobs = await roller.lock_jobs_on_line(ctx.session, ctx.work_line_id)
    current = await roller.production.resolve_scan2_task(ctx.session, work_line_id=ctx.work_line_id)
    await roller.lock_production_subjects(ctx.session, ctx.work_line_id, jobs)
    await repo.line_state(ctx.session)
    # Version the point collection so a waiting Repeatable Read receiver retries its old snapshot.
    await repo.update(ctx.session, "workline_state")
    await roller.rack_dispatch.lock_actions(ctx.session, job_ids=tuple(job.id for job in jobs))
    point = ctx.device_role
    key = "bin_passage" if point in ("SCAN1", "SCAN2") else "bin_return"
    table = repo.tables[key]
    prefix = point.lower()
    previous_events = await list_point_events(
        ctx.session,
        owner_domain=repo.owner_domain,
        work_line_id=repo.work_line_id,
        point=point,
        before_event_id=ctx.receipt_id,
        limit=1,
    )
    previous_event = previous_events[0] if previous_events else None
    previous = None
    separator = None
    if previous_event and previous_event.application_ref:
        previous = await repo._one(
            ctx.session, table, table.c.id == previous_event.application_ref.subject_id
        )
    same = (
        previous_event is not None
        and previous_event.envelope["device_code"] == ctx.device_code
        and previous_event.envelope["data"].get("bin_code") == payload.bin_code
    )
    if (
        same
        and previous is not None
        and key == "bin_passage"
        and previous["closed_at"] is not None
        and previous["scope_kind"] == "TASK"
        and current.is_valid
        and current.task_id is not None
        and current.task_id != previous["scope_id"]
    ):
        # A confirmed new WMS task starts independently; the closed task is never reparented.
        same = False
    if same and previous is not None:
        code = previous["release_command_code" if point == "SCAN2" else prefix + "_command_code"]
        view = await get_scan_result(roller, ctx.session, code)
        applied = applied_result(view, code, ctx.device_code)
        if applied is not None:
            separator = code
        old_separator = previous_event.application_ref.separator_command_code
        if separator is None or separator == old_separator:
            ctx.bind_application(
                EventApplicationRef(
                    key.upper(),
                    previous["id"],
                    previous_event.event_id,
                    previous_event.application_ref.predecessor_event_id,
                    old_separator,
                )
            )
            return
    else:
        separator = None
    predecessor_event_id = previous_event.event_id if previous_event else None
    visit_key = canonical_digest(
        dict(
            point=point,
            predecessor_event_id=predecessor_event_id,
            separator_command_code=separator,
            device_code=ctx.device_code,
            raw_bin_code=payload.bin_code,
        )
    )
    existing = await repo._one(ctx.session, table, table.c[prefix + "_visit_key"] == visit_key)
    if existing:
        ctx.bind_application(
            EventApplicationRef(
                key.upper(),
                existing["id"],
                existing[prefix + "_event_id"],
                predecessor_event_id,
                separator,
            )
        )
        return
    bin_code = bin_identity(payload.bin_code)
    task_scope = point in ("SCAN1", "SCAN2") and current.is_valid and current.task_id is not None
    if task_scope:
        scope = ScopeRef(repo.owner_domain, "TASK", current.task_id)
    else:
        line = await repo.line_state(ctx.session, start=True)
        scope = ScopeRef(repo.owner_domain, "LINE", line["line_run_id"])
    row = None
    if point == "SCAN2" and task_scope and bin_code is not None:
        candidates = (
            await ctx.session.execute(
                select(table)
                .where(
                    *repo.scoped(table),
                    table.c.scope_kind == "TASK",
                    table.c.scope_id == current.task_id,
                    table.c.bin_code == bin_code,
                    table.c.closed_at.is_(None),
                    table.c.scan1_event_id.is_not(None),
                    table.c.scan2_event_id.is_(None),
                    table.c.scan1_command_code.is_not(None),
                )
                .order_by(table.c.id)
                .with_for_update()
            )
        ).mappings()
        eligible = []
        for candidate in candidates:
            original = await get_event(ctx.session, event_id=candidate["scan1_event_id"])
            command = await get_scan_result(roller, ctx.session, candidate["scan1_command_code"])
            if (
                original.availability == "FOUND"
                and command
                and command.task_type == "MOVE_FORWARD"
                and command.device_code == original.record.envelope["device_code"]
            ):
                eligible.append(candidate)
        if len(eligible) == 1:
            row = eligible[0]
    valid_return = bin_code is not None and payload.bin_code.endswith("-B")
    if point == "SCAN4" and valid_return:
        roots = (
            (
                await ctx.session.execute(
                    select(table)
                    .where(
                        *repo.scoped(table),
                        table.c.bin_code == bin_code,
                        table.c.scan3_event_id.is_not(None),
                        table.c.is_current.is_(True),
                        table.c.closed_at.is_(None),
                        table.c.return_state != "OBSERVED",
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        if len(roots) == 1:
            root = roots[0]
            if root["scan4_event_id"] is None:
                row = root
            elif (
                root["selected_face_work_id"] is None
                and root["transport_task_id"] is None
                and root["return_state"] in ("WAITING", "READY")
            ):
                event, view = await point_view(roller, ctx.session, root, point)
                applied = (
                    applied_result(view, root["scan4_command_code"], event.envelope["device_code"])
                    if event
                    else None
                )
                if applied and applied.result == "FAILED":
                    row = root
                else:
                    ctx.bind_application(
                        EventApplicationRef(
                            key.upper(),
                            root["id"],
                            root["scan4_event_id"],
                            predecessor_event_id,
                            separator,
                        )
                    )
                    return
            else:
                ctx.bind_application(
                    EventApplicationRef(
                        key.upper(),
                        root["id"],
                        root["scan4_event_id"],
                        predecessor_event_id,
                        separator,
                    )
                )
                return
    if point == "SCAN3" and valid_return:
        previous_rounds = (
            (
                await ctx.session.execute(
                    select(table)
                    .where(
                        *repo.scoped(table),
                        table.c.bin_code == bin_code,
                        table.c.is_current.is_(True),
                        table.c.closed_at.is_(None),
                    )
                    .order_by(table.c.id)
                )
            )
            .mappings()
            .all()
        )
        for previous_round in previous_rounds:
            await repo.update(ctx.session, key, previous_round["id"], is_current=False)
            pending_direction = False
            for original_point in ("scan3", "scan4"):
                code = previous_round[original_point + "_command_code"]
                if code and previous_round[original_point + "_consumed_result_id"] is None:
                    original_view = await get_scan_result(roller, ctx.session, code)
                    pending_direction |= (
                        original_view is None or original_view.delivery_status != "ABANDONED"
                    )
            if (
                previous_round["selected_face_work_id"] is None
                and previous_round["transport_task_id"] is None
                and not pending_direction
            ):
                await repo.close(ctx.session, key, previous_round["id"], "SUPERSEDED")
    values = {prefix + "_event_id": ctx.receipt_id, prefix + "_visit_key": visit_key}
    if key == "bin_return" and point == "SCAN4":
        values.update(scan4_command_code=None, scan4_consumed_result_id=None)
        if row is None or row["first_scan4_event_id"] is None:
            receipt = await get_event(ctx.session, event_id=ctx.receipt_id)
            if receipt.availability != "FOUND":
                raise ValueError("original event unavailable during acceptance")
            values.update(
                first_scan4_event_id=ctx.receipt_id,
                first_scan4_received_at=receipt.record.received_at,
                fifo_visit_key=visit_key,
            )
    if row is None:
        values.update(id=str(uuid7()), **repo.scope, scope_id=scope.scope_id, bin_code=bin_code)
        if key == "bin_passage":
            values["scope_kind"] = scope.kind
        else:
            values.update(
                is_current=True,
                return_state="WAITING" if point == "SCAN3" and valid_return else "OBSERVED",
            )
        await ctx.session.execute(table.insert().values(**values))
        row_id = values["id"]
    else:
        row_id = row["id"]
        await repo.update(ctx.session, key, row_id, **values)
    row = await repo._one(ctx.session, table, table.c.id == row_id)
    ctx.bind_application(
        EventApplicationRef(key.upper(), row_id, ctx.receipt_id, predecessor_event_id, separator)
    )
    if point == "SCAN2":
        production_id = await roller.production.accept_scan2(
            ctx,
            Scan2Arrival(
                row_id, ctx.work_line_id, ctx.device_code, payload.bin_code, bin_code, ctx.timestamp
            ),
        )
        if not production_id:
            raise ValueError("production acceptance must return its original record")
        await repo.update(ctx.session, key, row_id, production_record_id=production_id)
    elif point == "SCAN1":
        command = MoveForward() if valid_return and task_scope else MoveRight()
        await create_direction(roller, ctx.session, repo, row, point, ctx.device_code, command)
    elif point == "SCAN3":
        await create_direction(
            roller,
            ctx.session,
            repo,
            row,
            point,
            ctx.device_code,
            MoveForward() if valid_return else MoveLeft(),
        )
    elif row["scan3_event_id"] is not None and valid_return:
        await create_direction(
            roller, ctx.session, repo, row, point, ctx.device_code, MoveForward()
        )
    jobs = await roller.rack_dispatch.jobs(
        ctx.session,
        work_line_id=ctx.work_line_id,
        source_kinds=("ROLLER_ORDINARY", "ROLLER_DRAIN"),
        pending=True,
    )
    await roller.rack_dispatch.note_facts_changed(ctx.session, job_ids=tuple(j.id for j in jobs))
    if key == "bin_return" and point == "SCAN4" and row["return_state"] == "READY":
        from .return_flow import advance_drain

        await advance_drain(roller, ctx.session, ctx.work_line_id)


async def create_direction(roller, session, repo, row, point, device_code, command):
    key = "bin_passage" if point in ("SCAN1", "SCAN2") else "bin_return"
    column = "release_command_code" if point == "SCAN2" else point.lower() + "_command_code"
    if row[column] is not None:
        return row[column]
    if row["closed_at"] is not None:
        raise ValueError("closed scope cannot create a direction")
    scope = ScopeRef(
        repo.owner_domain, row["scope_kind"] if key == "bin_passage" else "LINE", row["scope_id"]
    )
    view = await roller.capabilities.ecs.create(
        session,
        owner=roller.context,
        call_key=f"bin_roller:{point.lower()}:{row['id']}:{row[point.lower() + '_event_id']}",
        device_code=device_code,
        timeout_ms=30000,
        command=command,
        scope=scope,
    )
    await repo.update(session, key, row["id"], **{column: view.command_code})
    return view.command_code


async def release_scan2(roller, session, *, arrival_id):
    row = await roller.original_record(session, "bin_passage", arrival_id, for_update=True)
    if row["closed_at"] is not None:
        raise ValueError("closed passage cannot release")
    event = await point_event(session, row, "SCAN2")
    if not event or not row["production_record_id"]:
        raise ValueError("original SCAN2 production acceptance required")
    if row["release_command_code"]:
        return row["release_command_code"]
    view = await roller.capabilities.ecs.create(
        session,
        owner=roller.context,
        call_key=f"bin_roller:scan2_release:{arrival_id}",
        device_code=event.envelope["device_code"],
        timeout_ms=30000,
        command=MoveForward(),
        scope=ScopeRef(roller.context.domain, row["scope_kind"], row["scope_id"]),
    )
    await roller.repository(row["work_line_id"]).update(
        session, "bin_passage", row["id"], release_command_code=view.command_code
    )
    return view.command_code


async def consume_scan_result(roller, session, work):
    key = "bin_passage" if work.kind in ("PASSAGE", "SCAN2_RELEASE") else "bin_return"
    row = await roller.original_record(session, key, work.record_id)
    if row["closed_at"] is not None:
        return "CLOSED"
    repo = roller.repository(row["work_line_id"])
    points = (
        ("SCAN2",)
        if work.kind == "SCAN2_RELEASE"
        else ("SCAN1",)
        if key == "bin_passage"
        else ("SCAN3", "SCAN4")
    )
    status = "WAITING"
    for point in points:
        code_column = (
            "release_command_code" if point == "SCAN2" else point.lower() + "_command_code"
        )
        consumed_column = (
            "release_consumed_result_id"
            if point == "SCAN2"
            else point.lower() + "_consumed_result_id"
        )
        if row[code_column] is None or row[consumed_column] is not None:
            continue
        event, view = await point_view(roller, session, row, point)
        result = (
            applied_result(view, row[code_column], event.envelope["device_code"]) if event else None
        )
        if result is None:
            continue
        values = {consumed_column: canonical_digest(result.model_dump())}
        if (
            key == "bin_return"
            and point == "SCAN4"
            and row["return_state"] == "WAITING"
            and result.result == "SUCCESS"
        ):
            values["return_state"] = "READY"
        await repo.update(session, key, row["id"], **values)
        status = result.result
    updated = await repo._one(session, repo.tables[key], repo.tables[key].c.id == row["id"])
    if (
        key == "bin_passage"
        and updated["scope_kind"] == "LINE"
        and updated["scan2_event_id"] is None
        and updated["scan1_consumed_result_id"]
    ):
        await repo.close(session, key, row["id"], "FINISHED")
    elif (
        key == "bin_return"
        and updated["return_state"] == "OBSERVED"
        and updated["scan3_event_id"]
        and updated["scan3_consumed_result_id"]
        and updated["first_scan4_event_id"] is None
    ):
        await repo.close(session, key, row["id"], "FINISHED")
    if key == "bin_return":
        await repo.close_retired_return(session, row["id"])
    await repo.finish_line_if_idle(session)
    return status
