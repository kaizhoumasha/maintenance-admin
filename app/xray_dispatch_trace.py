"""Correlate X-Ray scheduling milestones with the original Celery task."""

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

logger = logging.getLogger(__name__)
TASK = "business.advance_pending"
SOURCE = "xray_dispatch_source"
TRIGGER = "xray_trigger_operation_id"


def _identity(value):
    if isinstance(value, str) and len(value) == 36:
        try:
            return str(UUID(value))
        except ValueError:
            pass
    return None


@dataclass(frozen=True)
class DispatchTrace:
    task_id: str
    source: str
    trigger_operation_id: str | None = None

    @classmethod
    def read(cls, task_id, headers):
        headers = headers or {}
        source = headers.get(SOURCE)
        task_id = _identity(task_id)
        if task_id and source in ("immediate", "beat"):
            return cls(task_id, source, _identity(headers.get(TRIGGER)))
        return None

    def log(self, stage, *, wms_operation_id=None, command_code=None):
        at_utc = datetime.now(UTC).isoformat(timespec="milliseconds")
        wms_operation_id = _identity(wms_operation_id)
        command_code = _identity(command_code)
        logger.info(
            "xray_dispatch stage=%s task_id=%s source=%s at_utc=%s "
            "trigger_operation_id=%s wms_operation_id=%s command_code=%s",
            stage,
            self.task_id,
            self.source,
            at_utc,
            _identity(self.trigger_operation_id) or "-",
            wms_operation_id or "-",
            command_code or "-",
            extra={"stage": stage, "task_id": self.task_id, "source": self.source},
        )


current_trace: ContextVar[DispatchTrace | None] = ContextVar("xray_dispatch_trace", default=None)


@contextmanager
def track_worker(trace):
    token = current_trace.set(trace)
    try:
        if trace:
            trace.log("worker_started")
        yield
        if trace:
            trace.log("worker_finished")
    finally:
        current_trace.reset(token)
