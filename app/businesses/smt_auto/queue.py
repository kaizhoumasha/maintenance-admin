"""Small domain objects for deterministic AUTO queue ordering and cancellation."""

from dataclasses import dataclass

from app.integrations.wms_callback import Conflict


@dataclass(frozen=True, order=True)
class QueueKey:
    dispatch_sequence: int
    issued_timestamp: int
    task_id: str


@dataclass(frozen=True)
class Cancellation:
    task_id: str
    reason: str

    def apply(self, task, *, closed_at_ms: int | None = None) -> None:
        if task.status == "CANCELLED":
            return
        if task.status not in {None, "QUEUED", "PREPARING"} or task.plan_revision not in {None, 0}:
            raise Conflict({"reason_code": "STATE_CONFLICT"})
        # An original prepare may still be unresolved after scope closure.
        task.pending = task.status == "PREPARING" and task.pending
        task.status = "CANCELLED"
        task.cancel_reason = self.reason
        task.closed_at_ms = task.closed_at_ms or (
            closed_at_ms if closed_at_ms is not None else task.issued_timestamp
        )
