"""Shared AUTO fact reads; callers retain their transactions and business decisions."""

from app.core.strict_json import canonical_digest
from app.integrations.ecs_command.contracts import ResultEnvelope


async def observe_arm_result(ecs, session, plate, phase, *, owner):
    view = await ecs.get_by_call(session, owner=owner, call_key=getattr(plate, f"{phase}_call_key"))
    if (
        view is None
        or view.command_code != getattr(plate, f"{phase}_command_code")
        or view.result is None
    ):
        return
    result = ResultEnvelope.model_validate(view.result).model_dump()
    setattr(plate, f"{phase}_result_snapshot", result)
    setattr(plate, f"{phase}_result_ref", canonical_digest(result))


async def read_decision(requests, session, round_):
    result = await requests.get_result(
        session,
        message_id=round_.message_id,
        operation=round_.operation,
        operation_id=round_.operation_id,
    )
    if (
        (result.message_id, result.operation, result.operation_id)
        != (round_.message_id, round_.operation, round_.operation_id)
        or result.availability != "FOUND"
        or result.response_state != "VALID"
        or (result.http_status, result.response_code) != (200, "DECIDED")
    ):
        return None
    return result


async def arrival_is_open(roller, session, work):
    if roller is None:
        return False
    original = await roller.original_arrival(session, work.arrival_id)
    return (
        original.get("work_line_id") == work.work_line_id
        and original.get("task_id") == work.task_id
        and original.get("bin_code") == work.bin_code
        and original.get("raw_bin_code") == work.raw_bin_code
        and original.get("device_code") == work.device_code
        and original.get("scanned_at") == work.scanned_at
        and original.get("closed_at") is None
        and original.get("scan2_event_id") is not None
        and original.get("release_command_code") is None
    )
