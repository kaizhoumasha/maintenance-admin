"""FIFO allocation, independent return facts and the current finite LINE run."""

import logging
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.identifiers import uuid7
from app.integrations.wms_transport import BinMove, HandoffPosition, RackBinSlot, TransportCaller
from app.reliable import ScopeRef

from .feed_flow import face_scope, read_decision, valid_move
from .request.bin_return_batch_v1 import DECISION, OPERATION, ReturnBatchRequest
from .request.workline_return_buffer_drain_rack_decide_v1 import DECISION as DRAIN_DECISION
from .request.workline_return_buffer_drain_rack_decide_v1 import OPERATION as DRAIN_OPERATION
from .request.workline_return_buffer_drain_rack_decide_v1 import DrainRequest

logger = logging.getLogger(__name__)


async def read_return_manifest(roller, session, face):
    try:
        decision = await read_decision(
            roller.capabilities,
            session,
            message_id=face["return_message_id"],
            operation=OPERATION,
            operation_id=face["return_operation_id"],
            validator=DECISION,
        )
    except ValueError:
        return None
    if decision and decision["result"] == "READY":
        moves = decision["moves"]
        originals = face["return_members"] or ()
        if len(moves) > len(originals) or any(
            (m["sequence_no"], m["bin_code"], m["target"]["rack_id"], m["target"]["rack_face"])
            != (o["sequence_no"], o["bin_code"], face["rack_id"], face["rack_face"])
            for m, o in zip(moves, originals, strict=False)
        ):
            return None
    return decision


async def release_selection(repo, session, face_id, step_id, *, ids=None):
    table = repo.tables["bin_return"]
    condition = (table.c.selected_face_work_id == face_id) & (table.c.selected_step_id == step_id)
    if ids is not None:
        condition &= table.c.id.in_(ids)
    rows = (
        (await session.execute(select(table).where(*repo.scoped(table), condition)))
        .mappings()
        .all()
    )
    for row in rows:
        values = dict(selected_face_work_id=None, selected_step_id=None)
        if row["closed_at"] is None:
            values["return_state"] = (
                "READY" if row["is_current"] and row["scan4_consumed_result_id"] else "WAITING"
            )
        await repo.update(session, "bin_return", row["id"], **values)
        await repo.close_retired_return(session, row["id"])


async def end_window(roller, session, face):
    repo = roller.repository(face["work_line_id"])
    if face["step_id"]:
        await release_selection(repo, session, face["id"], face["step_id"])
    values = dict(
        step_id=None,
        return_operation_id=None,
        return_message_id=None,
        return_members=None,
        current_client_request_id=None,
        current_transport_task_id=None,
        consumed_outcome_revision=None,
        window_id=None,
        window_kind=None,
        review_reason=None,
    )
    if face["window_kind"] == "SEGMENT":
        await repo.update(session, "face_work", face["id"], phase="WAIT_FEED_DECISION", **values)
    else:
        # Keep the last phase as evidence; closed_at is the only terminal marker.
        await repo.close(session, "face_work", face["id"], "COMPLETED")
        await repo.update(session, "face_work", face["id"], **values)


async def advance_return(roller, session, face_id):
    from .rack_flow import can_use_face

    face = await roller.original_record(session, "face_work", face_id)
    if face["closed_at"]:
        return "CLOSED"
    repo = roller.repository(face["work_line_id"])
    job = await roller.rack_dispatch.progress(session, job_id=face["rack_job_id"])
    if not job or not job.origin.caller or not job.origin.resources:
        return "WAITING_FOR_ORIGINAL_RESOURCES"
    source = job.origin.resources["positions"]["OUTLET"]
    table = repo.tables["bin_return"]
    if face["phase"] == "RETURN_WINDOW":
        candidates = await roller.get_return_candidates(session, work_line_id=repo.work_line_id)
        if not candidates:
            if await roller.return_channel_empty(session, work_line_id=repo.work_line_id):
                await end_window(roller, session, face)
                return "WINDOW_ENDED"
            return "WAITING_FOR_FIFO_HEAD"
        if not await can_use_face(roller, session, face):
            return "WAITING_FOR_ORIGINAL_POSITION"
        step_id, operation_id = str(uuid7()), str(uuid7())
        members = [
            dict(round_id=row["id"], bin_code=row["bin_code"], sequence_no=i)
            for i, row in enumerate(candidates, 1)
        ]
        message = await roller.capabilities.wms.send(
            session,
            ReturnBatchRequest(
                workline_code=job.origin.workline_code,
                rack_id=face["rack_id"],
                rack_face=face["rack_face"],
                return_candidates=[
                    dict(
                        sequence_no=m["sequence_no"],
                        bin_code=m["bin_code"],
                        source=dict(type="HANDOFF_POSITION", location_code=source),
                    )
                    for m in members
                ],
            ),
            operation_id=operation_id,
            scope=face_scope(roller, face),
        )
        await repo.update(
            session,
            "face_work",
            face_id,
            phase="WAIT_RETURN_DECISION",
            step_id=step_id,
            return_operation_id=operation_id,
            return_message_id=message.message_id,
            return_members=members,
        )
        for member in members:
            await repo.update(
                session,
                "bin_return",
                member["round_id"],
                return_state="ALLOCATING",
                selected_face_work_id=face_id,
                selected_step_id=step_id,
            )
        return "WAITING_FOR_ALLOCATION"
    decision = await read_return_manifest(roller, session, face)
    if decision is None:
        return "WAITING_FOR_ALLOCATION"
    if decision["result"] == "NO_BATCH":
        await end_window(roller, session, face)
        return "NO_BATCH"
    moves = decision["moves"]
    members_by_bin = {m["bin_code"]: m for m in face["return_members"]}
    move_codes = [m["bin_code"] for m in moves]
    if (
        len(members_by_bin) != len(face["return_members"])
        or len(set(move_codes)) != len(move_codes)
        or any(code not in members_by_bin for code in move_codes)
    ):
        return "INVALID_RETURN_MEMBERS"
    executed = [members_by_bin[m["bin_code"]] for m in moves]
    expected = [
        {
            "container_id": m["bin_code"],
            "source": {"kind": "HANDOFF_POSITION", "location_code": source},
            "target": {
                "kind": "RACK_BIN_SLOT",
                "rack_id": m["target"]["rack_id"],
                "rack_face": m["target"]["rack_face"],
                "slot_id": m["target"]["slot_id"],
            },
        }
        for m in moves
    ]
    if face["phase"] == "WAIT_RETURN_DECISION":
        originals = (
            (
                await session.execute(
                    select(table).where(
                        *repo.scoped(table), table.c.id.in_([m["round_id"] for m in executed])
                    )
                )
            )
            .mappings()
            .all()
        )
        if len(originals) != len(executed) or any(
            not r["is_current"]
            or r["closed_at"]
            or r["selected_face_work_id"] != face_id
            or r["selected_step_id"] != face["step_id"]
            for r in originals
        ):
            if face["window_kind"] == "SEGMENT":
                await end_window(roller, session, face)
            else:
                await release_selection(repo, session, face_id, face["step_id"])
                await repo.update(
                    session,
                    "face_work",
                    face_id,
                    phase="RETURN_WINDOW",
                    step_id=None,
                    return_operation_id=None,
                    return_message_id=None,
                    return_members=None,
                    current_client_request_id=None,
                    current_transport_task_id=None,
                )
            return "SUPERSEDED_ROUNDS"
        if not await can_use_face(roller, session, face):
            return "WAITING_FOR_ORIGINAL_POSITION"
        client_id = str(uuid7())
        handle = await roller.capabilities.transport.move_bins(
            session,
            client_request_id=client_id,
            caller=job.origin.caller,
            cycle_id=job.id,
            scope=face_scope(roller, face),
            moves=[
                BinMove(
                    m["bin_code"],
                    HandoffPosition(source),
                    RackBinSlot(
                        m["target"]["rack_id"], m["target"]["rack_face"], m["target"]["slot_id"]
                    ),
                )
                for m in moves
            ],
        )
        await release_selection(
            repo,
            session,
            face_id,
            face["step_id"],
            ids=[m["round_id"] for m in face["return_members"][len(moves) :]],
        )
        for member in executed:
            await repo.update(
                session,
                "bin_return",
                member["round_id"],
                return_state="MOVING",
                transport_task_id=handle.transport_task_id,
                client_request_id=handle.client_request_id,
                caller=asdict(job.origin.caller),
                consumed_outcome_revision=None,
            )
        await repo.update(
            session,
            "face_work",
            face_id,
            phase="RETURN_MOVE",
            current_client_request_id=handle.client_request_id,
            current_transport_task_id=handle.transport_task_id,
        )
        return "WAITING_FOR_RETURN_RESULT"
    result = await roller.capabilities.transport.get_result(
        session,
        transport_task_id=face["current_transport_task_id"],
        client_request_id=face["current_client_request_id"],
        caller=job.origin.caller,
    )
    valid = valid_move(
            result,
            owner=repo.owner_domain,
            task_id=face["current_transport_task_id"],
            client_id=face["current_client_request_id"],
            caller=job.origin.caller,
            cycle_id=job.id,
        )
    if not valid:
        logger.warning("bin return result identity pending")
        return "WAITING_FOR_RETURN_RESULT"
    frozen_moves = (result.frozen or {}).get("moves") if result.frozen else None
    if not frozen_moves or sorted(frozen_moves, key=lambda move: move["container_id"]) != sorted(
        expected, key=lambda move: move["container_id"]
    ):
        logger.warning("bin return frozen moves mismatch")
        return "WAITING_FOR_RETURN_RESULT"
    outcomes = result.outcome.get("results") or ()
    if len(outcomes) != len(executed) or {m["container_id"] for m in outcomes} != {
        m["bin_code"] for m in executed
    }:
        logger.warning("bin return outcome members mismatch")
        return "INVALID_RETURN_MEMBERS"
    by_code = {m["container_id"]: m for m in outcomes}
    targets = {m["container_id"]: m["target"] for m in expected}
    all_success = True
    for member in executed:
        row = await repo._one(session, table, table.c.id == member["round_id"])
        if (
            row is None
            or row["transport_task_id"] != result.transport_task_id
            or row["client_request_id"] != result.client_request_id
            or row["caller"] != asdict(job.origin.caller)
        ):
            logger.warning("bin return member reference mismatch")
            return "INVALID_RETURN_REFERENCE"
        outcome = by_code[member["bin_code"]]
        member_facts = (result.member_facts or {}).get(member["bin_code"])
        success = (
            outcome["status"] == "SUCCEEDED"
            and not outcome.get("position_unknown")
            and outcome["final_position"] == targets[member["bin_code"]]
            and (
                member_facts is None
                or member_facts.get("TARGET_PLACED") == targets[member["bin_code"]]
            )
        )
        all_success &= success
        if (
            row["consumed_outcome_revision"] is None
            or result.outcome_revision > row["consumed_outcome_revision"]
        ):
            await repo.update(
                session, "bin_return", row["id"], consumed_outcome_revision=result.outcome_revision
            )
            if success:
                await repo.close(session, "bin_return", row["id"], "COMPLETED")
                await repo.update(
                    session,
                    "bin_return",
                    row["id"],
                    selected_face_work_id=None,
                    selected_step_id=None,
                )
            elif (
                not outcome.get("position_unknown")
                and outcome["final_position"] is not None
                and outcome["final_position"]
                != {"kind": "HANDOFF_POSITION", "location_code": source}
            ):
                await repo.update(session, "bin_return", row["id"], is_current=False)
    if not all_success or result.closure != "CLOSED":
        logger.warning("bin return outcome not closed")
        return "WAITING_FOR_RETURN_RESULT"
    if face["window_kind"] == "SEGMENT":
        await end_window(roller, session, face)
    else:
        await release_selection(repo, session, face_id, face["step_id"])
        await repo.update(
            session,
            "face_work",
            face_id,
            phase="RETURN_WINDOW",
            step_id=None,
            return_operation_id=None,
            return_message_id=None,
            return_members=None,
            current_client_request_id=None,
            current_transport_task_id=None,
        )
    return "RETURN_SUCCEEDED"


async def read_drain_manifest(roller, session, state):
    try:
        return await read_decision(
            roller.capabilities,
            session,
            message_id=state["drain_message_id"],
            operation=DRAIN_OPERATION,
            operation_id=state["drain_operation_id"],
            validator=DRAIN_DECISION,
        )
    except ValueError:
        return None


async def advance_drain(roller, session, work_line_id):
    from .rack_flow import advance_cycle, register_job

    repo = roller.repository(work_line_id)
    state = await repo.line_state(session)
    now = datetime.now(UTC)
    if state["drain_review_reason"] == "LINE_STOPPED":
        return None
    if state["drain_phase"] == "IDLE":
        await repo.finish_line_if_idle(session)
        state = await repo.line_state(session)
        successor = await roller.production.read_successor(session, work_line_id=work_line_id)
        candidates = await roller.return_candidates(session, work_line_id=work_line_id)
        faces = repo.tables["face_work"]
        successor_blocked = successor.state not in {"NONE", "UNKNOWN"} or bool(
            successor.evidence.get("queued_task_ids")
        )
        if (
            successor_blocked
            or not candidates
            or await session.scalar(
                select(faces.c.id).where(*repo.scoped(faces), faces.c.closed_at.is_(None)).limit(1)
            )
        ):
            return None
        resources = await roller.read_resources(session, work_line_id)
        if not resources:
            return None
        state = await repo.line_state(session, start=True)
        operation_id = str(uuid7())
        message = await roller.capabilities.wms.send(
            session,
            DrainRequest(
                workline_code=resources["workline_code"], required_slot_count=len(candidates)
            ),
            operation_id=operation_id,
            scope=ScopeRef(repo.owner_domain, "LINE", state["line_run_id"]),
        )
        await repo.update(
            session,
            "workline_state",
            drain_phase="REQUESTED",
            drain_operation_id=operation_id,
            drain_message_id=message.message_id,
            candidate_cursor=0,
            drain_review_reason=None,
        )
        return None
    decision = await read_drain_manifest(roller, session, state)
    if decision is None:
        return None
    if decision["result"] == "WAIT":
        if state["drain_phase"] == "REQUESTED":
            await repo.update(
                session,
                "workline_state",
                drain_phase="WAIT",
                drain_retry_at=now + timedelta(milliseconds=decision["retry_after_ms"]),
            )
        elif state["drain_retry_at"].replace(tzinfo=UTC) <= now:
            await repo.update(
                session,
                "workline_state",
                drain_phase="IDLE",
                drain_operation_id=None,
                drain_message_id=None,
                drain_retry_at=None,
                candidate_cursor=None,
            )
            return await advance_drain(roller, session, work_line_id)
        return None
    if state["drain_phase"] == "REQUESTED":
        await repo.update(session, "workline_state", drain_phase="USING")
        state = await repo.line_state(session)
    cursor = state["candidate_cursor"]
    while cursor < len(decision["racks"]):
        candidate = decision["racks"][cursor]
        originals = await roller.rack_dispatch.jobs(
            session,
            work_line_id=work_line_id,
            source_kinds=("ROLLER_DRAIN",),
            source_ref=state["drain_operation_id"],
        )
        jobs = [j for j in originals if j.origin.rack_id == candidate["rack_id"]]
        if len(jobs) > 1:
            await repo.update(session, "workline_state", drain_review_reason="POSITION_CONFLICT")
            return None
        if jobs:
            job = jobs[0]
            if job.pending:
                if not job.actions and await roller.return_channel_empty(
                    session, work_line_id=work_line_id
                ):
                    from app.businesses._components.rack_dispatch.contracts import Coverage, FactRef

                    scope = (
                        repo.owner_domain,
                        str(work_line_id),
                        "ROLLER_DRAIN",
                        state["drain_operation_id"],
                        job.origin.rack_id,
                    )
                    withdrawn = await roller.rack_dispatch.withdraw_unused(
                        session,
                        job_id=job.id,
                        source=Coverage(
                            "unused_drain_candidate",
                            scope,
                            FactRef(
                                "drain_decision",
                                ("DRAIN", state["drain_operation_id"]),
                                (str(state["drain_message_id"]), str(state["revision"])),
                            ),
                            (),
                        ),
                    )
                    if not withdrawn:
                        return job.id
                    faces = repo.tables["face_work"]
                    for face_id in await session.scalars(
                        select(faces.c.id).where(
                            *repo.scoped(faces),
                            faces.c.rack_job_id == job.id,
                            faces.c.closed_at.is_(None),
                        )
                    ):
                        await repo.close(session, "face_work", face_id, "WITHDRAWN")
                else:
                    await advance_cycle(roller, session, job.id)
                    return job.id
                if job.pending:
                    await advance_cycle(roller, session, job.id)
            cursor += 1
            await repo.update(session, "workline_state", candidate_cursor=cursor)
            continue
        if not await roller.get_return_candidates(session, work_line_id=work_line_id):
            if await roller.return_channel_empty(session, work_line_id=work_line_id):
                cursor = len(decision["racks"])
                break
            return None
        resources = await roller.read_resources(session, work_line_id)
        if not resources:
            return None
        job_id = await register_job(
            roller,
            session,
            repo,
            "ROLLER_DRAIN",
            state["drain_operation_id"],
            candidate["rack_id"],
            candidate["rack_face"][0],
            None,
            resources,
        )
        await advance_cycle(roller, session, job_id)
        return job_id
    await repo.update(
        session,
        "workline_state",
        drain_phase="IDLE",
        drain_operation_id=None,
        drain_message_id=None,
        drain_retry_at=None,
        candidate_cursor=None,
        drain_review_reason=None,
    )
    await repo.finish_line_if_idle(session)
    return None


async def close_scope(roller, session, scope: ScopeRef, *, reason):
    if scope.owner_domain != roller.context.domain:
        raise ValueError("scope owner mismatch")
    # The host holds WorkLine, devices, rack jobs and original task before this call.
    stops = {
        s.message_id: s
        for s in await roller.capabilities.wms.outbox.stop_scope(session, scope, reason=reason)
    }
    faces = roller.tables["face_work"]
    selected_faces = (
        (
            await session.execute(
                select(faces)
                .where(
                    faces.c.owner_domain == scope.owner_domain,
                    faces.c.scope_id == scope.scope_id,
                    faces.c.purpose == ("SUPPLY" if scope.kind == "TASK" else "DRAIN"),
                    faces.c.closed_at.is_(None),
                )
                .order_by(faces.c.id)
            )
        )
        .mappings()
        .all()
    )
    returns = roller.tables["bin_return"]
    for face in selected_faces:
        repo = roller.repository(face["work_line_id"])
        members = (
            (
                await session.execute(
                    select(returns)
                    .where(*repo.scoped(returns), returns.c.selected_face_work_id == face["id"])
                    .order_by(returns.c.id)
                )
            )
            .mappings()
            .all()
        )
        for member in members:
            values = dict(selected_face_work_id=None, selected_step_id=None)
            if member["closed_at"] is None:
                if member["transport_task_id"] is None:
                    values["return_state"] = (
                        "READY"
                        if member["is_current"] and member["scan4_consumed_result_id"]
                        else "WAITING"
                    )
                else:
                    result = await roller.capabilities.transport.get_result(
                        session,
                        transport_task_id=member["transport_task_id"],
                        client_request_id=member["client_request_id"],
                        caller=TransportCaller(**member["caller"]),
                    )
                    stop = stops.get(result.submission_message_id)
                    acceptance = await roller.capabilities.transport.get_submission_acceptance(
                        session,
                        transport_task_id=member["transport_task_id"],
                        client_request_id=member["client_request_id"],
                        caller=TransportCaller(**member["caller"]),
                    )
                    if (
                        (
                            (stop and stop.dispatch == "NEVER_DISPATCHED")
                            or result.submission_dispatch_started is False
                        )
                        and acceptance.state == "MISSING"
                        and not result.outcome
                        and not result.has_pending_outcome
                        and not result.member_facts
                        and result.submission_delivery_uncertain is False
                    ):
                        values.update(
                            return_state="READY"
                            if member["is_current"] and member["scan4_consumed_result_id"]
                            else "WAITING",
                            transport_task_id=None,
                            client_request_id=None,
                            caller=None,
                            consumed_outcome_revision=None,
                        )
                    else:
                        values["return_state"] = "WAIT_FACT"
            await repo.update(session, "bin_return", member["id"], **values)
            if values.get("return_state") == "WAIT_FACT":
                await consume_return_fact(roller, session, return_id=member["id"])
            await repo.close_retired_return(session, member["id"])
        await repo.close(session, "face_work", face["id"], reason)
        await repo.update(
            session,
            "face_work",
            face["id"],
            step_id=None,
            return_members=None,
            window_id=None,
            window_kind=None,
            current_client_request_id=None,
            current_transport_task_id=None,
        )
    passages = roller.tables["bin_passage"]
    rows = (
        (
            await session.execute(
                select(passages)
                .where(
                    passages.c.owner_domain == scope.owner_domain,
                    passages.c.scope_kind == scope.kind,
                    passages.c.scope_id == scope.scope_id,
                    passages.c.closed_at.is_(None),
                )
                .order_by(passages.c.id)
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        await roller.repository(row["work_line_id"]).close(
            session, "bin_passage", row["id"], reason
        )
    await roller.rack_dispatch.close_scope(session, scope, reason=reason)
    if scope.kind == "LINE":
        rows = (
            (
                await session.execute(
                    select(returns).where(
                        returns.c.owner_domain == scope.owner_domain,
                        returns.c.scope_id == scope.scope_id,
                        returns.c.closed_at.is_(None),
                    )
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            repo = roller.repository(row["work_line_id"])
            if row["transport_task_id"]:
                await repo.update(
                    session,
                    "bin_return",
                    row["id"],
                    return_state="WAIT_FACT",
                    selected_face_work_id=None,
                    selected_step_id=None,
                )
            # Unknown physical observations continue to block their FIFO head under the same run.
        states = roller.tables["workline_state"]
        stopped_lines = tuple(
            await session.scalars(
                select(states.c.work_line_id).where(
                    states.c.owner_domain == scope.owner_domain,
                    states.c.line_run_id == scope.scope_id,
                )
            )
        )
        for line_id in stopped_lines:
            await roller.repository(line_id).update(
                session,
                "workline_state",
                line_run_id=None,
                drain_phase="IDLE",
                drain_operation_id=None,
                drain_message_id=None,
                drain_retry_at=None,
                candidate_cursor=None,
                drain_review_reason="LINE_STOPPED",
            )


async def consume_return_fact(roller, session, *, return_id):
    row = await roller.original_record(session, "bin_return", return_id)
    if row["closed_at"] or row["return_state"] != "WAIT_FACT":
        return
    caller = TransportCaller(**row["caller"])
    result = await roller.capabilities.transport.get_result(
        session,
        transport_task_id=row["transport_task_id"],
        client_request_id=row["client_request_id"],
        caller=caller,
    )
    if not valid_move(
        result,
        owner=roller.context.domain,
        task_id=row["transport_task_id"],
        client_id=row["client_request_id"],
        caller=caller,
    ):
        return
    if (
        row["consumed_outcome_revision"] is not None
        and result.outcome_revision < row["consumed_outcome_revision"]
    ):
        return
    moves = [
        m for m in (result.frozen or {}).get("moves", ()) if m["container_id"] == row["bin_code"]
    ]
    outcomes = [
        m for m in result.outcome.get("results", ()) if m["container_id"] == row["bin_code"]
    ]
    if len(moves) != 1 or len(outcomes) != 1:
        return
    outcome, move = outcomes[0], moves[0]
    repo = roller.repository(row["work_line_id"])
    if (
        row["consumed_outcome_revision"] is None
        or result.outcome_revision > row["consumed_outcome_revision"]
    ):
        await repo.update(
            session, "bin_return", row["id"], consumed_outcome_revision=result.outcome_revision
        )
    if outcome.get("position_unknown") or outcome["final_position"] is None:
        return
    if outcome["final_position"] != move["source"]:
        await repo.close(session, "bin_return", row["id"], "EXITED")
    elif not row["is_current"]:
        await repo.close(session, "bin_return", row["id"], "SUPERSEDED")
    elif row["scan4_consumed_result_id"]:
        await repo.update(
            session,
            "bin_return",
            row["id"],
            return_state="READY",
            transport_task_id=None,
            client_request_id=None,
            caller=None,
            consumed_outcome_revision=None,
        )
