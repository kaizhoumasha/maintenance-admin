"""Small domain objects for deterministic AUTO queue ordering and cancellation."""

from dataclasses import dataclass


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
        if task.status in {"COMPLETED", "CANCELLED"}:
            return
        task.status = "CANCELLED"
        task.pending = False
        task.cancel_reason = self.reason
        task.closed_at_ms = task.closed_at_ms or (
            closed_at_ms if closed_at_ms is not None else task.issued_timestamp
        )
