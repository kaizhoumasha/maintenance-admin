"""S1 task-domain operations; no runtime or external integration assembly."""

from .queue import QueueKey


class AutoTaskQueue:
    """Encapsulates queue eligibility and stable ordering for AUTO tasks."""

    def eligible(self, tasks, *, now_ms: int):
        return sorted(
            (
                task
                for task in tasks
                if getattr(task, "status", None) in (None, "QUEUED")
                and (task.not_before_ms is None or task.not_before_ms <= now_ms)
            ),
            key=lambda task: QueueKey(task.dispatch_sequence, task.issued_timestamp, task.task_id),
        )
