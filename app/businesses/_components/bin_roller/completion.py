"""Shared production completion checks; physical release Results retain their own recovery."""

from collections.abc import Sequence

from .ports import Scan2Arrival, SupplyProgress


def supply_accounted_for(progress: Sequence[SupplyProgress], *, production_count: int) -> bool:
    return (
        all(p.fed_bin_count is not None for p in progress)
        and sum(p.fed_bin_count for p in progress) <= production_count
    )


async def release_intent(roller, session, *, arrival: Scan2Arrival, task_id, command_code):
    if not command_code:
        return None
    try:
        scan = await roller.original_arrival(session, arrival.arrival_id)
    except ValueError:
        return None
    if (
        scan["point"] != "SCAN2"
        or scan["scope_kind"] != ("TASK" if task_id is not None else "LINE")
        or scan["task_id"] != task_id
        or scan["production_record_id"] != arrival.arrival_id
        or scan["release_command_code"] != command_code
        or any(
            scan[key] != getattr(arrival, key)
            for key in ("work_line_id", "device_code", "raw_bin_code", "bin_code", "scanned_at")
        )
    ):
        return None
    return scan
