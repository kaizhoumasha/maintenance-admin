"""One selected original face's frozen allocation and ordered feed segments."""

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

from .rack_flow import can_use_face
from .request.outbound_bin_inbound_batch_v1 import DECISION, OPERATION, InboundBatch


async def start_feed(
    roller,
    session: AsyncSession,
    *,
    demand_id: str,
    caller: TransportCaller,
    target: HandoffPosition,
) -> str | None:
    """The rack lifecycle selects the original ready face in its caller transaction."""
    demand = await roller.original_record(session, "supply_demand", demand_id, for_update=True)
    repo = roller.repository(demand["work_line_id"])
    if demand["feed_id"] is not None:
        return demand["feed_id"]
    if demand["demand_state"] == "WITHDRAWN" and not await repo.supply_started(session, demand):
        return None
    if not await can_use_face(roller, session, demand):
        return None
    feeds = repo.tables["face_work"]
    feed_id, operation_id = str(uuid7()), str(uuid7())
    reference = await roller.capabilities.wms.send(
        session,
        InboundBatch(
            task_id=demand["task_id"], rack_id=demand["rack_id"], rack_face=demand["rack_face"]
        ),
        operation_id=operation_id,
    )
    await session.execute(
        feeds.update()
        .where(*repo.scoped(feeds), feeds.c.demand_id == demand_id)
        .values(
            feed_id=feed_id,
            feed_state="REQUESTED",
            operation_id=operation_id,
            message_id=reference.message_id,
            caller_workline_id=caller.workline_id,
            station_id=caller.station_id,
            target={"kind": "HANDOFF_POSITION", "location_code": target.code},
        )
    )
    return feed_id


async def opportunity(repo, session, *, feed, source_ref: str, kind: str) -> str:
    row = await repo._register(
        session,
        repo.tables["return_opportunity"],
        dict(
            id=str(uuid7()),
            **repo.scope,
            service_id=feed["service_id"],
            source_ref=source_ref,
            kind=kind,
            ended=False,
        ),
        ("service_id", "source_ref"),
        ("kind",),
    )
    return row["id"]


async def record_feed_success(
    roller, session: AsyncSession, *, feed_id: str, segment_id: str, result: TransportTaskResult
) -> str:
    feed = await roller.original_record(session, "feed", feed_id)
    repo = roller.repository(feed["work_line_id"])
    segments = repo.tables["feed_segment"]
    segment = await repo._one(
        session, segments, segments.c.id == segment_id, segments.c.feed_id == feed_id
    )
    if segment is None or (
        result.availability != "FOUND"
        or result.owner_domain != repo.owner_domain
        or result.transport_task_id != segment["transport_task_id"]
        or result.client_request_id != segment["client_request_id"]
        or result.operation_id != segment["operation_id"]
        or result.workline_id != feed["caller_workline_id"]
        or result.station_id != feed["station_id"]
        or result.cycle_id != feed["cycle_id"]
        or result.kind != "BIN_MOVE"
    ):
        return ""
    outcome = result.outcome
    if (
        outcome is None
        or result.has_pending_outcome
        or (
            outcome["transport_task_id"] != segment["transport_task_id"]
            or outcome["kind"] != "BIN_MOVE"
            or outcome["outcome_revision"] != result.outcome_revision
        )
    ):
        return ""
    members = outcome.get("results")
    if (
        not members
        or len(members) != len(segment["members"])
        or {member["container_id"] for member in members}
        != {member["bin_code"] for member in segment["members"]}
    ):
        return ""
    if any(
        member["status"] == "SUCCEEDED"
        and (member.get("position_unknown") or member["final_position"] != feed["target"])
        for member in members
    ):
        return ""
    successful = all(
        member["status"] == "SUCCEEDED"
        and not member.get("position_unknown")
        and member["final_position"] == feed["target"]
        for member in members
    )
    if (
        segment["consumed_revision"] is None
        or result.outcome_revision > segment["consumed_revision"]
    ):
        await session.execute(
            segments.update()
            .where(*repo.scoped(segments), segments.c.id == segment_id)
            .values(
                status="UNKNOWN"
                if any(member.get("position_unknown") for member in members)
                else "SUCCEEDED"
                if successful
                else "FAILED",
                pending=not successful,
                consumed_revision=result.outcome_revision,
            )
        )
    elif result.outcome_revision < segment["consumed_revision"]:
        return ""
    if not successful or feed["feed_state"] == "REVIEW":
        faces = repo.tables["face_work"]
        later = select(segments.c.id).where(
            *repo.scoped(segments),
            segments.c.feed_id == feed_id,
            segments.c.segment_index > segment["segment_index"],
        )
        await session.execute(
            faces.update()
            .where(
                *repo.scoped(faces),
                faces.c.feed_id == feed_id,
                faces.c.feed_state != "COMPLETED",
                ~later.exists(),
            )
            .values(feed_state="ALLOCATED" if successful else "REVIEW")
        )
    if not successful:
        return ""
    last = segment["segment_index"] * 4 >= len(feed["manifest"]["bins"])
    if last:
        feeds = repo.tables["face_work"]
        await session.execute(
            feeds.update()
            .where(*repo.scoped(feeds), feeds.c.feed_id == feed_id)
            .values(feed_state="COMPLETED")
        )
    return await opportunity(
        repo,
        session,
        feed=feed,
        source_ref="SEGMENT:" + segment_id,
        kind="FACE_END" if last else "SEGMENT",
    )


async def advance_feed(roller, session: AsyncSession, demand_id: str) -> str:
    demand = await roller.original_record(session, "supply_demand", demand_id)
    repo = roller.repository(demand["work_line_id"])
    feeds, segments = repo.tables["face_work"], repo.tables["feed_segment"]
    feed = demand
    if feed["feed_id"] is None:
        if demand["demand_state"] == "WITHDRAWN" and not await repo.supply_started(session, demand):
            return "WITHDRAWN"
        return "WAITING_FOR_SELECTED_FACE"
    if feed["manifest"] is None:
        result = await roller.capabilities.wms.get_result(
            session,
            message_id=feed["message_id"],
            operation=OPERATION,
            operation_id=feed["operation_id"],
        )
        if not (
            result.availability == "FOUND"
            and result.message_id == feed["message_id"]
            and result.operation == OPERATION
            and result.operation_id == feed["operation_id"]
            and result.response_state == "VALID"
            and result.http_status == 200
            and result.response_code == "DECIDED"
        ):
            return "WAITING_FOR_ALLOCATION"
        try:
            manifest = DECISION.validate_python(result.response_data).model_dump(mode="json")
            if any(
                (m["source_locator"]["rack_id"], m["source_locator"]["rack_face"])
                != (demand["rack_id"], demand["rack_face"])
                for m in manifest.get("bins", [])
            ):
                return "INVALID_ALLOCATION"
        except ValueError:
            return "INVALID_ALLOCATION"
        await session.execute(
            feeds.update()
            .where(*repo.scoped(feeds), feeds.c.feed_id == feed["feed_id"])
            .values(manifest=manifest, feed_state="ALLOCATED")
        )
        feed = await repo._one(session, feeds, feeds.c.feed_id == feed["feed_id"])
    if feed["manifest"]["result"] == "RACK_FACE_DONE":
        await session.execute(
            feeds.update()
            .where(*repo.scoped(feeds), feeds.c.feed_id == feed["feed_id"])
            .values(feed_state="COMPLETED")
        )
        return await opportunity(
            repo, session, feed=feed, source_ref="FEED:" + feed["feed_id"], kind="FACE_END"
        )
    existing = (
        (
            await session.execute(
                select(segments)
                .where(*repo.scoped(segments), segments.c.feed_id == feed["feed_id"])
                .order_by(segments.c.segment_index)
            )
        )
        .mappings()
        .all()
    )
    if existing:
        current = existing[-1]
        result = await roller.capabilities.transport.get_result(
            session,
            transport_task_id=current["transport_task_id"],
            client_request_id=current["client_request_id"],
            caller=TransportCaller(feed["caller_workline_id"], feed["station_id"]),
        )
        original_opportunity = await record_feed_success(
            roller, session, feed_id=feed["feed_id"], segment_id=current["id"], result=result
        )
        if not original_opportunity:
            return "WAITING_FOR_FEED_RESULT"
        table = repo.tables["return_opportunity"]
        original = await repo._one(session, table, table.c.id == original_opportunity)
        if original["kind"] == "FACE_END" or not original["ended"]:
            return original_opportunity
    return await prepare_feed(roller, session, demand_id)


async def prepare_feed(roller, session: AsyncSession, demand_id: str) -> str:
    """Register only the ready original segment; result consumption stays in advance_feed."""
    demand = await roller.original_record(session, "supply_demand", demand_id)
    repo = roller.repository(demand["work_line_id"])
    segments = repo.tables["feed_segment"]
    feed = demand
    if feed["feed_id"] is None or feed["manifest"] is None or feed["feed_state"] == "COMPLETED":
        return "WAITING_FOR_FEED"
    current = (
        (
            await session.execute(
                select(segments)
                .where(*repo.scoped(segments), segments.c.feed_id == feed["feed_id"])
                .order_by(segments.c.segment_index.desc())
                .limit(1)
            )
        )
        .mappings()
        .one_or_none()
    )
    index = 1
    if current is not None:
        opportunities = repo.tables["return_opportunity"]
        original = await repo._one(
            session,
            opportunities,
            opportunities.c.service_id == feed["service_id"],
            opportunities.c.source_ref == "SEGMENT:" + current["id"],
        )
        if current["status"] != "SUCCEEDED" or original is None or not original["ended"]:
            return current["id"]
        if original["kind"] == "FACE_END":
            return original["id"]
        index = current["segment_index"] + 1
    if not await can_use_face(roller, session, feed):
        return "WAITING_FOR_ORIGINAL_POSITION"
    members = feed["manifest"]["bins"][(index - 1) * 4 : index * 4]
    segment = await repo._register(
        session,
        segments,
        dict(
            id=str(uuid7()),
            **repo.scope,
            feed_id=feed["feed_id"],
            segment_index=index,
            members=members,
            client_request_id=str(uuid7()),
        ),
        ("feed_id", "segment_index"),
        ("members",),
    )
    if segment["transport_task_id"] is None:
        handle = await roller.capabilities.transport.move_bins(
            session,
            client_request_id=segment["client_request_id"],
            caller=TransportCaller(feed["caller_workline_id"], feed["station_id"]),
            cycle_id=feed["cycle_id"],
            moves=[
                BinMove(
                    m["bin_code"],
                    RackBinSlot(
                        m["source_locator"]["rack_id"],
                        m["source_locator"]["rack_face"],
                        m["source_locator"]["slot_id"],
                    ),
                    HandoffPosition(feed["target"]["location_code"]),
                )
                for m in segment["members"]
            ],
        )
        view = await roller.capabilities.transport.get_result(
            session,
            transport_task_id=handle.transport_task_id,
            client_request_id=handle.client_request_id,
            caller=TransportCaller(feed["caller_workline_id"], feed["station_id"]),
        )
        await session.execute(
            segments.update()
            .where(*repo.scoped(segments), segments.c.id == segment["id"])
            .values(transport_task_id=handle.transport_task_id, operation_id=view.operation_id)
        )
    return segment["id"]
