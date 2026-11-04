"""One current face step; original allocations and moves remain in public storage."""

from app.core.identifiers import uuid7
from app.integrations.wms_transport import BinMove, HandoffPosition, RackBinSlot
from app.reliable import ScopeRef

from .request.bin_inbound_batch_v1 import DECISION, OPERATION, InboundBatch


async def read_decision(capabilities, session, *, message_id, operation, operation_id, validator):
    if message_id is None or operation_id is None:
        return None
    result = await capabilities.wms.get_result(
        session, message_id=message_id, operation=operation, operation_id=operation_id
    )
    if (
        result.availability != "FOUND"
        or result.message_id != message_id
        or result.operation != operation
        or result.operation_id != operation_id
        or result.response_state != "VALID"
        or (result.http_status, result.response_code) != (200, "DECIDED")
    ):
        return None
    return validator.validate_python(result.response_data).model_dump(mode="json")


async def read_feed_manifest(capabilities, session, face):
    try:
        manifest = await read_decision(
            capabilities,
            session,
            message_id=face["feed_message_id"],
            operation=OPERATION,
            operation_id=face["feed_operation_id"],
            validator=DECISION,
        )
    except ValueError:
        return None
    if manifest and any(
        (m["source_locator"]["rack_id"], m["source_locator"]["rack_face"])
        != (face["rack_id"], face["rack_face"])
        for m in manifest.get("bins", ())
    ):
        return None
    return manifest


def face_scope(roller, face):
    return ScopeRef(
        roller.context.domain, "TASK" if face["purpose"] == "SUPPLY" else "LINE", face["scope_id"]
    )


async def start_feed(roller, session, *, demand_id):
    from .rack_flow import can_use_face

    face = await roller.original_record(session, "face_work", demand_id)
    if face["closed_at"] or face["purpose"] != "SUPPLY":
        return None
    if face["feed_operation_id"]:
        return face["id"]
    current = await roller.production.read_current_task(session, work_line_id=face["work_line_id"])
    if not current.is_valid or current.task_id != face["scope_id"]:
        return None
    if not await can_use_face(roller, session, face):
        return None
    operation_id = str(uuid7())
    message = await roller.capabilities.wms.send(
        session,
        InboundBatch(
            task_id=face["scope_id"], rack_id=face["rack_id"], rack_face=face["rack_face"]
        ),
        operation_id=operation_id,
        scope=face_scope(roller, face),
    )
    await roller.repository(face["work_line_id"]).update(
        session,
        "face_work",
        face["id"],
        feed_operation_id=operation_id,
        feed_message_id=message.message_id,
        feed_cursor=0,
        phase="WAIT_FEED_DECISION",
    )
    return face["id"]


async def advance_face(roller, session, face_id):
    from .rack_flow import can_use_face
    from .return_flow import advance_return

    face = await roller.original_record(session, "face_work", face_id)
    if face["closed_at"]:
        return "CLOSED"
    repo = roller.repository(face["work_line_id"])
    if face["phase"] == "WAIT_RACK":
        return "WAITING_FOR_RACK"
    if face["phase"] in ("RETURN_WINDOW", "WAIT_RETURN_DECISION", "RETURN_MOVE"):
        return await advance_return(roller, session, face_id)
    manifest = await read_feed_manifest(roller.capabilities, session, face)
    if manifest is None:
        return "WAITING_FOR_ALLOCATION"
    if manifest["result"] == "RACK_FACE_DONE":
        await repo.update(
            session,
            "face_work",
            face_id,
            phase="RETURN_WINDOW",
            window_id=str(uuid7()),
            window_kind="FACE_END",
        )
        return "RETURN_WINDOW"
    members = manifest["bins"][face["feed_cursor"] : face["feed_cursor"] + 4]
    if not members:
        return "INVALID_FEED_CURSOR"
    job = await roller.rack_dispatch.progress(session, job_id=face["rack_job_id"])
    if not job or not job.origin.caller or not job.origin.resources:
        return "WAITING_FOR_ORIGINAL_RESOURCES"
    target = {
        "kind": "HANDOFF_POSITION",
        "location_code": job.origin.resources["positions"]["INLET"],
    }
    if face["phase"] == "WAIT_FEED_DECISION":
        if not await can_use_face(roller, session, face):
            return "WAITING_FOR_ORIGINAL_POSITION"
        client_id, step_id = str(uuid7()), str(uuid7())
        handle = await roller.capabilities.transport.move_bins(
            session,
            client_request_id=client_id,
            caller=job.origin.caller,
            cycle_id=job.id,
            scope=face_scope(roller, face),
            moves=[
                BinMove(
                    m["bin_code"],
                    RackBinSlot(
                        m["source_locator"]["rack_id"],
                        m["source_locator"]["rack_face"],
                        m["source_locator"]["slot_id"],
                    ),
                    HandoffPosition(target["location_code"]),
                )
                for m in members
            ],
        )
        await repo.update(
            session,
            "face_work",
            face_id,
            phase="FEED_MOVE",
            step_id=step_id,
            current_client_request_id=handle.client_request_id,
            current_transport_task_id=handle.transport_task_id,
            consumed_outcome_revision=None,
            review_reason=None,
        )
        return "WAITING_FOR_FEED_RESULT"
    result = await roller.capabilities.transport.get_result(
        session,
        transport_task_id=face["current_transport_task_id"],
        client_request_id=face["current_client_request_id"],
        caller=job.origin.caller,
    )
    if not valid_move(
        result,
        owner=repo.owner_domain,
        task_id=face["current_transport_task_id"],
        client_id=face["current_client_request_id"],
        caller=job.origin.caller,
        cycle_id=job.id,
    ):
        return "WAITING_FOR_FEED_RESULT"
    expected = [
        {
            "container_id": m["bin_code"],
            "source": {
                "kind": "RACK_BIN_SLOT",
                "rack_id": m["source_locator"]["rack_id"],
                "rack_face": m["source_locator"]["rack_face"],
                "slot_id": m["source_locator"]["slot_id"],
            },
            "target": target,
        }
        for m in members
    ]
    if result.frozen is None or result.frozen.get("moves") != expected:
        return "INVALID_FEED_MEMBERS"
    if (
        face["consumed_outcome_revision"] is not None
        and result.outcome_revision <= face["consumed_outcome_revision"]
    ):
        return "WAITING_FOR_FEED_RESULT"
    await repo.update(
        session, "face_work", face_id, consumed_outcome_revision=result.outcome_revision
    )
    outcomes = result.outcome.get("results") or ()
    if (
        len(outcomes) != len(members)
        or {m["container_id"] for m in outcomes} != {m["bin_code"] for m in members}
        or not all(
            m["status"] == "SUCCEEDED"
            and not m.get("position_unknown")
            and m["final_position"] == target
            for m in outcomes
        )
        or result.closure != "CLOSED"
    ):
        return "WAITING_FOR_FEED_RESULT"
    cursor = face["feed_cursor"] + len(members)
    await repo.update(
        session,
        "face_work",
        face_id,
        phase="RETURN_WINDOW",
        feed_cursor=cursor,
        window_id=str(uuid7()),
        window_kind="FACE_END" if cursor == len(manifest["bins"]) else "SEGMENT",
        step_id=None,
        current_client_request_id=None,
        current_transport_task_id=None,
        consumed_outcome_revision=None,
        review_reason=None,
    )
    return "RETURN_WINDOW"


def valid_move(result, *, owner, task_id, client_id, caller, cycle_id=None):
    return bool(
        result.availability == "FOUND"
        and result.owner_domain == owner
        and result.transport_task_id == task_id
        and result.client_request_id == client_id
        and (str(result.workline_id), result.station_id)
        == (str(caller.workline_id), caller.station_id)
        and (cycle_id is None or result.cycle_id == cycle_id)
        and result.kind == "BIN_MOVE"
        and not result.has_pending_outcome
        and result.outcome is not None
        and result.outcome_revision is not None
        and result.outcome.get("transport_task_id") == task_id
        and result.outcome.get("kind") == "BIN_MOVE"
        and result.outcome.get("outcome_revision") == result.outcome_revision
    )
