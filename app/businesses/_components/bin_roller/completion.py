"""Shared production completion checks; physical release Results retain their own recovery."""

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence

from .ports import Scan2Arrival, SourceFaceRef, SupplyProgress


async def supply_bypass_evidence(
    roller,
    session,
    *,
    work_line_id: int,
    task_id: str,
    progress: Sequence[SupplyProgress],
    production_codes: Iterable[str],
    cancellations: Mapping[SourceFaceRef, tuple[int | str, int]],
    manual_bypasses: Mapping[SourceFaceRef, Mapping] | None = None,
) -> list[list] | None:
    """Return evidence for actual fed boxes accounted for outside SCAN2."""
    if any(p.fed_bin_count != len(p.fed_bin_codes) for p in progress):
        return None
    supplied = Counter(code for p in progress for code in p.fed_bin_codes)
    production = Counter(production_codes)
    missing = supplied - production
    bypasses = []
    for fact in progress:
        cancellation = cancellations.get(fact.source)
        if cancellation is None:
            continue
        original_id, withdrawn_at_ms = cancellation
        for code in fact.fed_bin_codes:
            if code not in missing or supplied[code] != 1:
                continue
            evidence = (manual_bypasses or {}).get(fact.source)
            bypass = None
            if roller is not None:
                bypass = await roller.cancelled_bin_bypass(
                    session,
                    work_line_id=work_line_id,
                    task_id=task_id,
                    bin_code=code,
                    withdrawn_at_ms=withdrawn_at_ms,
                )
            if bypass is not None:
                bypasses.append([original_id, *bypass])
                del missing[code]
                continue
            if (
                isinstance(evidence, Mapping)
                and evidence.get("kind") == "MANUAL_SCAN1_SCAN2_BYPASS"
                and evidence.get("bin_code") == code
                and evidence.get("from_point") == "SCAN1"
                and evidence.get("to_point") == "SCAN2"
                and isinstance(evidence.get("recorded_at_ms"), int)
                and evidence["recorded_at_ms"] >= withdrawn_at_ms
            ):
                bypasses.append([original_id, "MANUAL_SCAN1_SCAN2_BYPASS", dict(evidence)])
                del missing[code]
    return None if missing else bypasses


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
