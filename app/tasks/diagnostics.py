"""Celery 失败/重试诊断;只观察信号和日志,不修改任务结果或重试。"""

import logging

from celery import signals
from celery.app.trace import LOG_RETRY

from app.diagnostics import diagnose, diagnostic_text, log_exception, safe_traceback

logger = logging.getLogger(__name__)


def _task_failed(sender=None, task_id=None, exception=None, **kwargs):
    if isinstance(exception, BaseException):
        log_exception(
            logger,
            "celery task failed",
            exception,
            component="celery",
            phase="execute",
            level=logging.INFO if isinstance(exception, sender.throws or ()) else logging.ERROR,
            context={"task": sender.name, "task_id": task_id},
        )


def _task_retry(sender=None, request=None, reason=None, **kwargs):
    if isinstance(reason, BaseException):
        cause = getattr(reason, "exc", None) or reason
        log_exception(
            logger,
            "celery task retry",
            reason,
            component="celery",
            phase="retry",
            level=logging.INFO,
            diagnostic=diagnose(cause),
            context={"task": sender.name, "task_id": request.id},
        )


class _SafeCeleryException(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        data = getattr(record, "data", {})
        has_error_data = isinstance(data, dict) and "exc" in data
        if has_error_data:
            # 这两种事件已由原始异常信号输出;框架副本含参数和异常原文。
            if (
                data.get("description") in {"raised expected", "raised unexpected"}
                or record.msg == LOG_RETRY
            ):
                return False
        error = record.exc_info[1] if record.exc_info else None
        if error is None and not has_error_data:
            return True
        if isinstance(getattr(error, "exc", None), BaseException):
            error = error.exc
        fields = (
            diagnose(error)
            if isinstance(error, BaseException)
            else {"error_type": "TaskControlError", "origin": "unavailable"}
        )
        fields.update(component="celery", phase="framework")
        if isinstance(data, dict):
            for key, name in [("name", "task"), ("id", "task_id")]:
                if key in data:
                    fields[name] = data[key]
        record.msg = "celery task exception " + diagnostic_text(fields)
        if isinstance(error, BaseException):
            record.msg += "\n" + safe_traceback(error, record.exc_info[2])
        record.args = ()
        record.exc_info = record.exc_text = None
        record.data = fields
        record.__dict__.update(fields)
        return True


def install_celery_diagnostics() -> None:
    signals.task_failure.connect(_task_failed, weak=False, dispatch_uid="safe-task-failure")
    signals.task_retry.connect(_task_retry, weak=False, dispatch_uid="safe-task-retry")
    for name in ("celery.app.trace", "celery.worker.request"):
        target = logging.getLogger(name)
        if not any(isinstance(f, _SafeCeleryException) for f in target.filters):
            target.addFilter(_SafeCeleryException())
