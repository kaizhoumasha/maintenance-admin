"""Shared queue update rules for an original task locked by its host."""

from app.integrations.wms_callback import Conflict

from .contracts import QueueChanged


def queue_changes(
    payload: QueueChanged,
    *,
    status: str,
    queue_revision: int,
    dispatch_sequence: int,
    not_before: int | None,
) -> dict[str, int] | Conflict:
    if status != "QUEUED":
        return Conflict({"reason_code": "STATE_CONFLICT"})
    if payload.queue_revision != queue_revision + 1:
        return Conflict({"reason_code": "REVISION_CONFLICT"})
    changes = payload.model_dump(include={"dispatch_sequence", "not_before"})
    current = {"dispatch_sequence": dispatch_sequence, "not_before": not_before}
    if all(current[key] == value for key, value in changes.items()):
        return Conflict({"reason_code": "STATE_CONFLICT"})
    return changes
