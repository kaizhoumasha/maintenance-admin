"""Original return batches and drain supply; rack motion belongs to rack_flow."""

from time import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identifiers import uuid7
from app.integrations.wms_transport import (
    BinMove,
    HandoffPosition,
    RackBinSlot,
    TransportCaller,
    TransportTaskResult,
)


async def summarize_drain(roller, session, drain_id):
    drain = await roller.original_record(session, "drain", drain_id)
    if drain["status"] != "READY":
        return drain
    repo = roller.repository(drain["work_line_id"])
    cycles, drains = repo.tables["rack_cycle"], repo.tables["drain"]
    originals = (
        (
            await session.execute(
                select(cycles).where(
                    *repo.scoped(cycles),
                    cycles.c.purpose == "DRAIN",
                    cycles.c.source_ref == drain_id,
                )
            )
        )
        .mappings()
        .all()
    )
    used = {c["rack_id"] for c in originals}
    unused = drain["unused_racks"]
    if await roller.return_channel_empty(session, work_line_id=repo.work_line_id):
        unused = [c["rack_id"] for c in drain["decision"]["racks"] if c["rack_id"] not in used]
    complete = all(c["status"] == "RELEASED" for c in originals) and all(
        c["rack_id"] in used or c["rack_id"] in unused for c in drain["decision"]["racks"]
    )
    if complete or unused != drain["unused_racks"]:
        await session.execute(
            drains.update()
            .where(*repo.scoped(drains), drains.c.id == drain_id)
            .values(
                unused_racks=unused,
                status="COMPLETED" if complete else "READY",
                pending=not complete,
            )
        )
    return await repo._one(session, drains, drains.c.id == drain_id)


async def advance_drain(roller, session, drain_id):
    from .request.workline_return_buffer_drain_rack_decide_v1 import DECISION, OPERATION

    drain = await roller.original_record(session, "drain", drain_id)
    repo = roller.repository(drain["work_line_id"])
    table = repo.tables["drain"]
    if drain["decision"] is None:
        result = await roller.capabilities.wms.get_result(
            session,
            message_id=drain["message_id"],
            operation=OPERATION,
            operation_id=drain["operation_id"],
        )
        if (
            result.availability != "FOUND"
            or result.response_state != "VALID"
            or result.message_id != drain["message_id"]
            or result.operation != OPERATION
            or result.operation_id != drain["operation_id"]
            or (result.http_status, result.response_code) != (200, "DECIDED")
        ):
            return None
        try:
            decision = DECISION.validate_python(result.response_data).model_dump(mode="json")
        except ValueError:
            return None
        await session.execute(
            table.update()
            .where(*repo.scoped(table), table.c.id == drain_id)
            .values(
                decision=decision,
                status=decision["result"],
                due_at=int(time() * 1000) + decision["retry_after_ms"]
                if decision["result"] == "WAIT"
                else None,
            )
        )
    drain = await summarize_drain(roller, session, drain_id)
    if drain["status"] != "READY":
        return None
    cycles = repo.tables["rack_cycle"]
    original = (
        (
            await session.execute(
                select(cycles).where(
                    *repo.scoped(cycles),
                    cycles.c.purpose == "DRAIN",
                    cycles.c.source_ref == drain_id,
                )
            )
        )
        .mappings()
        .all()
    )
    if any(c["status"] != "RELEASED" for c in original):
        return None
    if not await roller.get_return_candidates(session, work_line_id=repo.work_line_id):
        return None
    used = {c["rack_id"] for c in original} | set(drain["unused_racks"])
    candidate = next((r for r in drain["decision"]["racks"] if r["rack_id"] not in used), None)
    if candidate is None:
        return None
    resources = await roller.read_resources(session, repo.work_line_id)
    if resources is None:
        return None
    cycle = await repo.create_cycle(
        session,
        cycle_id=str(uuid7()),
        rack_id=candidate["rack_id"],
        purpose="DRAIN",
        task_id=None,
        source_ref=drain_id,
        resources=resources,
    )
    return cycle["id"]


async def register_drain(roller, session, repo, previous, candidates, successor):
    table = repo.tables["drain"]
    from .request.workline_return_buffer_drain_rack_decide_v1 import DrainRequest

    resources = await roller.read_resources(session, repo.work_line_id)
    if resources is None:
        return None
    payload = DrainRequest(
        workline_code=resources["workline_code"], required_slot_count=len(candidates)
    )
    record_id = str(uuid7())
    input_kind = (
        "WAIT"
        if previous and previous["status"] == "WAIT"
        else "COMPLETED"
        if previous
        else "FIFO_OBSERVATION"
    )
    input_ref = previous["id"] if previous else record_id
    row = await repo._register(
        session,
        table,
        dict(
            id=record_id,
            **repo.scope,
            input_kind=input_kind,
            input_ref=input_ref,
            input_snapshot=dict(round_id=candidates[0]["id"], successor=successor.evidence),
            request=payload.model_dump(mode="json"),
            operation_id=str(uuid7()),
        ),
        ("input_kind", "input_ref"),
        (),
    )
    if row["message_id"] is None:
        ref = await roller.capabilities.wms.send(session, payload, operation_id=row["operation_id"])
        await session.execute(
            table.update()
            .where(*repo.scoped(table), table.c.id == row["id"])
            .values(message_id=ref.message_id)
        )
    if previous and previous["status"] == "WAIT":
        await session.execute(
            table.update()
            .where(*repo.scoped(table), table.c.id == previous["id"])
            .values(pending=False)
        )
    return None


async def end_opportunity(repo, session, opportunity, reason, evidence):
    table = repo.tables["return_opportunity"]
    if not opportunity["ended"]:
        await session.execute(
            table.update()
            .where(*repo.scoped(table), table.c.id == opportunity["id"])
            .values(ended=True, end_reason=reason, end_evidence=evidence)
        )


async def start_return_batch(
    roller,
    session: AsyncSession,
    *,
    opportunity_id: str,
    source_ref: str,
    workline_code: str,
    caller: TransportCaller,
    source: HandoffPosition,
) -> str | None:
    from .request.outbound_bin_return_batch_v1 import ReturnBatchRequest

    opportunity = await roller.original_record(session, "return_opportunity", opportunity_id)
    repo = roller.repository(opportunity["work_line_id"])
    batches = repo.tables["return_batch"]
    original = await repo._one(
        session,
        batches,
        batches.c.opportunity_id == opportunity_id,
        batches.c.source_ref == source_ref,
    )
    if original is not None:
        return original["id"]
    if opportunity["ended"]:
        return None
    if source_ref != opportunity_id:
        previous = await repo._one(
            session, batches, batches.c.id == source_ref, batches.c.opportunity_id == opportunity_id
        )
        if opportunity["kind"] == "SEGMENT" or previous is None or previous["pending"]:
            raise ValueError("continuation requires this opportunity's original consumed batch")
    current = await repo._one(
        session, batches, batches.c.opportunity_id == opportunity_id, batches.c.pending.is_(True)
    )
    if current is not None:
        return current["id"]
    service = await roller.original_record(session, "face_service", opportunity["service_id"])
    cycle = await roller.original_record(session, "rack_cycle", service["cycle_id"])
    if service["work_line_id"] != repo.work_line_id or cycle["work_line_id"] != repo.work_line_id:
        raise ValueError("return opportunity must retain its original line")
    if service["status"] != "READY" or cycle["status"] != "ACTIVE":
        return None
    candidates = await roller.get_return_candidates(session, work_line_id=repo.work_line_id)
    if not candidates:
        await record_return_outcome(
            roller,
            session,
            service_id=service["id"],
            opportunity_id=opportunity_id,
            batch_id=None,
            reason="NO_CANDIDATES",
            evidence={"source_ref": source_ref},
        )
        return None
    payload = ReturnBatchRequest(
        workline_code=workline_code,
        rack_id=cycle["rack_id"],
        rack_face=service["rack_face"],
        return_candidates=[
            dict(
                sequence_no=i,
                bin_code=r["bin_code"],
                source=dict(type="HANDOFF_POSITION", location_code=source.code),
            )
            for i, r in enumerate(candidates, 1)
        ],
    )
    batch = await repo._register(
        session,
        batches,
        dict(
            id=str(uuid7()),
            **repo.scope,
            opportunity_id=opportunity_id,
            source_ref=source_ref,
            operation_id=str(uuid7()),
            workline_code=workline_code,
            caller_workline_id=caller.workline_id,
            station_id=caller.station_id,
            source={"kind": "HANDOFF_POSITION", "location_code": source.code},
            members=[dict(round_id=r["id"], bin_code=r["bin_code"]) for r in candidates],
        ),
        ("opportunity_id", "source_ref"),
        (),
    )
    if batch["message_id"] is None:
        reference = await roller.capabilities.wms.send(
            session, payload, operation_id=batch["operation_id"]
        )
        await session.execute(
            batches.update()
            .where(*repo.scoped(batches), batches.c.id == batch["id"])
            .values(message_id=reference.message_id)
        )
        rounds = repo.tables["return_round"]
        await session.execute(
            rounds.update()
            .where(
                *repo.scoped(rounds),
                rounds.c.id.in_([m["round_id"] for m in batch["members"]]),
                rounds.c.removed.is_(False),
                rounds.c.status != "COMPLETED",
            )
            .values(status="ALLOCATING")
        )
    return batch["id"]


async def consume_return_result(
    roller, session: AsyncSession, *, batch_id: str, result: TransportTaskResult
) -> str:
    batch = await roller.original_record(session, "return_batch", batch_id)
    repo = roller.repository(batch["work_line_id"])
    opportunity = await roller.original_record(
        session, "return_opportunity", batch["opportunity_id"]
    )
    service = await roller.original_record(session, "face_service", opportunity["service_id"])
    if batch["status"] == "SUCCEEDED":
        return "SUCCEEDED"
    if (
        batch["manifest"] is None
        or batch["manifest"]["result"] != "READY"
        or result.availability != "FOUND"
        or result.owner_domain != repo.owner_domain
        or result.transport_task_id != batch["transport_task_id"]
        or result.client_request_id != batch["client_request_id"]
        or result.operation_id != batch["transport_operation_id"]
        or result.workline_id != batch["caller_workline_id"]
        or result.station_id != batch["station_id"]
        or result.cycle_id != service["cycle_id"]
        or result.kind != "BIN_MOVE"
    ):
        return ""
    outcome = result.outcome
    if (
        outcome is None
        or result.has_pending_outcome
        or result.outcome_revision is None
        or outcome["transport_task_id"] != batch["transport_task_id"]
        or outcome["kind"] != "BIN_MOVE"
        or outcome["outcome_revision"] != result.outcome_revision
    ):
        return ""
    moves = batch["manifest"]["moves"]
    targets = {
        m["bin_code"]: dict(
            kind="RACK_BIN_SLOT",
            rack_id=m["target"]["rack_id"],
            rack_face=m["target"]["rack_face"],
            slot_id=m["target"]["slot_id"],
        )
        for m in moves
    }
    members = outcome.get("results")
    if (
        not members
        or len(members) != len(targets)
        or {m["container_id"] for m in members} != set(targets)
    ):
        return ""
    if any(
        m["status"] == "SUCCEEDED"
        and (
            m.get("position_unknown")
            or m["final_position"] != targets[m["container_id"]]
            or (result.member_facts or {}).get(m["container_id"], {}).get("TARGET_PLACED")
            != targets[m["container_id"]]
        )
        for m in members
    ):
        return ""
    if (
        batch["consumed_revision"] is not None
        and result.outcome_revision <= batch["consumed_revision"]
    ):
        return ""
    successful = all(m["status"] == "SUCCEEDED" for m in members)
    status = (
        "SUCCEEDED"
        if successful
        else "UNKNOWN"
        if any(m.get("position_unknown") for m in members)
        else "FAILED"
    )
    batches = repo.tables["return_batch"]
    await session.execute(
        batches.update()
        .where(*repo.scoped(batches), batches.c.id == batch_id)
        .values(status=status, pending=not successful, consumed_revision=result.outcome_revision)
    )
    departed = {
        m["container_id"]
        for m in members
        if m["final_position"] is not None
        and not m.get("position_unknown")
        and m["final_position"] != batch["source"]
    }
    member_ids = [m["round_id"] for m in batch["members"] if m["bin_code"] in departed]
    arrivals, positions = repo.tables["scan_arrival"], repo.tables["position_fact"]
    original_attempts = select(arrivals.c.id).where(
        *repo.scoped(arrivals),
        arrivals.c.return_round_id.in_(member_ids),
        arrivals.c.point == "SCAN4",
        arrivals.c.command_code.is_not(None),
    )
    await session.execute(
        positions.update()
        .where(
            *repo.scoped(positions),
            positions.c.object_kind == "BIN",
            positions.c.source_kind == "SCAN",
            positions.c.source_id.in_(original_attempts),
            positions.c.is_current.is_(True),
        )
        .values(is_current=False)
    )
    if successful:
        rounds = repo.tables["return_round"]
        await session.execute(
            rounds.update()
            .where(
                *repo.scoped(rounds),
                rounds.c.id.in_(member_ids),
                rounds.c.removed.is_(False),
                rounds.c.status != "COMPLETED",
            )
            .values(status="COMPLETED")
        )
        await record_return_outcome(
            roller,
            session,
            service_id=service["id"],
            opportunity_id=opportunity["id"],
            batch_id=batch_id,
            reason="RETURN_SUCCEEDED",
            evidence={
                "batch_id": batch_id,
                "transport_task_id": result.transport_task_id,
                "outcome_revision": result.outcome_revision,
            },
        )
    return status


async def advance_return_batch(roller, session: AsyncSession, batch_id: str) -> str:
    from .request.outbound_bin_return_batch_v1 import DECISION, OPERATION

    batch = await roller.original_record(session, "return_batch", batch_id)
    if not batch["pending"]:
        return batch["status"]
    repo = roller.repository(batch["work_line_id"])
    opportunity = await roller.original_record(
        session, "return_opportunity", batch["opportunity_id"]
    )
    service = await roller.original_record(session, "face_service", opportunity["service_id"])
    cycle = await roller.original_record(session, "rack_cycle", service["cycle_id"])
    batches, rounds = repo.tables["return_batch"], repo.tables["return_round"]
    if batch["manifest"] is None:
        result = await roller.capabilities.wms.get_result(
            session,
            message_id=batch["message_id"],
            operation=OPERATION,
            operation_id=batch["operation_id"],
        )
        if not (
            result.availability == "FOUND"
            and result.message_id == batch["message_id"]
            and result.operation == OPERATION
            and result.operation_id == batch["operation_id"]
            and result.response_state == "VALID"
            and result.http_status == 200
            and result.response_code == "DECIDED"
        ):
            return "WAITING_FOR_ALLOCATION"
        try:
            manifest = DECISION.validate_python(result.response_data).model_dump(mode="json")
            moves = manifest.get("moves", [])
            if len(moves) > len(batch["members"]) or any(
                m["bin_code"] != original["bin_code"]
                or (m["target"]["rack_id"], m["target"]["rack_face"])
                != (cycle["rack_id"], service["rack_face"])
                for m, original in zip(moves, batch["members"], strict=False)
            ):
                return "INVALID_ALLOCATION"
        except ValueError:
            return "INVALID_ALLOCATION"
        no_batch = manifest["result"] == "NO_BATCH"
        await session.execute(
            batches.update()
            .where(*repo.scoped(batches), batches.c.id == batch_id)
            .values(
                manifest=manifest,
                status="NO_BATCH" if no_batch else "PENDING",
                pending=not no_batch,
            )
        )
        tail = batch["members"] if no_batch else batch["members"][len(moves) :]
        await session.execute(
            rounds.update()
            .where(
                *repo.scoped(rounds),
                rounds.c.id.in_([m["round_id"] for m in tail]),
                repo.current_round_condition(),
                rounds.c.removed.is_(False),
                rounds.c.status != "COMPLETED",
            )
            .values(status="PENDING")
        )
        if no_batch:
            await record_return_outcome(
                roller,
                session,
                service_id=service["id"],
                opportunity_id=opportunity["id"],
                batch_id=batch_id,
                reason="NO_BATCH",
                evidence={
                    "batch_id": batch_id,
                    "message_id": batch["message_id"],
                    "operation_id": batch["operation_id"],
                },
            )
            return "NO_BATCH"
        batch = await roller.original_record(session, "return_batch", batch_id)
    if batch["transport_task_id"] is None:
        member_ids = {m["round_id"] for m in batch["members"][: len(batch["manifest"]["moves"])]}
        current_ids = set(
            await session.scalars(
                select(rounds.c.id).where(
                    *repo.scoped(rounds),
                    rounds.c.id.in_(member_ids),
                    repo.current_round_condition(),
                    rounds.c.removed.is_(False),
                    rounds.c.status != "COMPLETED",
                )
            )
        )
        if current_ids != member_ids:
            await session.execute(
                batches.update()
                .where(*repo.scoped(batches), batches.c.id == batch_id)
                .values(pending=False)
            )
            await session.execute(
                rounds.update()
                .where(*repo.scoped(rounds), rounds.c.id.in_(current_ids))
                .values(status="PENDING")
            )
            if opportunity["kind"] == "SEGMENT":
                await end_opportunity(
                    repo,
                    session,
                    opportunity,
                    "SUPERSEDED_ROUNDS",
                    {"batch_id": batch_id, "round_ids": sorted(member_ids - current_ids)},
                )
            return "SUPERSEDED_ROUNDS"
        client_request_id = batch["client_request_id"] or str(uuid7())
        handle = await roller.capabilities.transport.move_bins(
            session,
            client_request_id=client_request_id,
            caller=TransportCaller(batch["caller_workline_id"], batch["station_id"]),
            cycle_id=service["cycle_id"],
            moves=[
                BinMove(
                    m["bin_code"],
                    HandoffPosition(batch["source"]["location_code"]),
                    RackBinSlot(
                        m["target"]["rack_id"], m["target"]["rack_face"], m["target"]["slot_id"]
                    ),
                )
                for m in batch["manifest"]["moves"]
            ],
        )
        view = await roller.capabilities.transport.get_result(
            session,
            transport_task_id=handle.transport_task_id,
            client_request_id=handle.client_request_id,
            caller=TransportCaller(batch["caller_workline_id"], batch["station_id"]),
        )
        await session.execute(
            batches.update()
            .where(*repo.scoped(batches), batches.c.id == batch_id)
            .values(
                client_request_id=handle.client_request_id,
                transport_task_id=handle.transport_task_id,
                transport_operation_id=view.operation_id,
            )
        )
        await session.execute(
            rounds.update()
            .where(
                *repo.scoped(rounds),
                rounds.c.id.in_(
                    [m["round_id"] for m in batch["members"][: len(batch["manifest"]["moves"])]]
                ),
                rounds.c.removed.is_(False),
                rounds.c.status != "COMPLETED",
            )
            .values(status="IN_TRANSIT")
        )
        return "WAITING_FOR_RETURN_RESULT"
    result = await roller.capabilities.transport.get_result(
        session,
        transport_task_id=batch["transport_task_id"],
        client_request_id=batch["client_request_id"],
        caller=TransportCaller(batch["caller_workline_id"], batch["station_id"]),
    )
    return (
        await consume_return_result(roller, session, batch_id=batch_id, result=result)
        or "WAITING_FOR_RETURN_RESULT"
    )


async def record_return_outcome(
    roller,
    session: AsyncSession,
    *,
    service_id: str,
    opportunity_id: str,
    batch_id: str | None,
    reason: str,
    evidence: dict,
) -> str:
    opportunity = await roller.original_record(session, "return_opportunity", opportunity_id)
    if opportunity["service_id"] != service_id:
        return ""
    repo = roller.repository(opportunity["work_line_id"])
    batches = repo.tables["return_batch"]
    if batch_id is None:
        if reason not in {"NO_CANDIDATES", "BUFFER_EMPTY"}:
            return ""
        if (
            await repo._one(
                session,
                batches,
                batches.c.opportunity_id == opportunity_id,
                batches.c.pending.is_(True),
            )
            is not None
        ):
            return ""
        if reason == "BUFFER_EMPTY":
            if not await roller.return_channel_empty(session, work_line_id=repo.work_line_id):
                return ""
        elif await roller.get_return_candidates(session, work_line_id=repo.work_line_id):
            return ""
    else:
        batch = await repo._one(
            session, batches, batches.c.id == batch_id, batches.c.opportunity_id == opportunity_id
        )
        if batch is None or evidence.get("batch_id", batch_id) != batch_id:
            return ""
        if reason == "RETURN_SUCCEEDED":
            if (
                batch["status"] != "SUCCEEDED"
                or batch["pending"]
                or evidence.get("transport_task_id") != batch["transport_task_id"]
                or evidence.get("outcome_revision") != batch["consumed_revision"]
            ):
                return ""
        elif reason == "NO_BATCH":
            if (
                batch["status"] != "NO_BATCH"
                or batch["manifest"] != {"result": "NO_BATCH"}
                or evidence.get("message_id") != batch["message_id"]
                or evidence.get("operation_id") != batch["operation_id"]
            ):
                return ""
        else:
            return ""
    if reason != "RETURN_SUCCEEDED" or opportunity["kind"] == "SEGMENT":
        await end_opportunity(repo, session, opportunity, reason, evidence)
    return opportunity_id
