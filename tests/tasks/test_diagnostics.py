import logging

import pytest
from celery.app.log import TaskFormatter
from sqlalchemy.exc import ProgrammingError

from app.core.logger import DevFormatter
from app.tasks.celery_app import celery_app


@celery_app.task(name="tests.diagnostic_failure")
def fail_task(payload, password):
    driver = RuntimeError("column reliable_message.delivery_snapshot does not exist")
    driver.sqlstate = "42703"
    raise ProgrammingError("private_sql", {"password": password}, driver)


@celery_app.task(bind=True, name="tests.diagnostic_retry", max_retries=1)
def retry_task(self, password):
    if not self.request.retries:
        raise self.retry(exc=RuntimeError(password), countdown=0)
    return "ok"


@pytest.mark.parametrize("throws", [None, ()])
def test_celery_failure_logs_diagnostics_without_arguments_or_exception_text(
    caplog, monkeypatch, throws
):
    monkeypatch.setattr(fail_task, "throws", throws)
    with caplog.at_level(logging.INFO):
        result = fail_task.apply(args=("private_payload",), kwargs={"password": "private_password"})
    assert result.failed() and isinstance(result.result, ProgrammingError)
    with pytest.raises(ProgrammingError):
        result.get()
    records = [r for r in caplog.records if getattr(r, "component", None) == "celery"]
    assert len(records) == 1
    assert records[0].task == "tests.diagnostic_failure" and records[0].task_id == result.id
    assert records[0].phase == "execute"
    rendered = "\n".join(DevFormatter(color=False).format(r) for r in caplog.records)
    assert "missing=reliable_message.delivery_snapshot" in rendered
    assert "sqlstate=42703" in rendered and "Traceback" in rendered
    assert "private_" not in caplog.text and "private_" not in rendered
    assert all("private_" not in str(getattr(r, "data", {})) for r in caplog.records)
    assert not any("Signal handler" in r.getMessage() for r in caplog.records)
    assert "private_" not in "\n".join(
        TaskFormatter(use_color=False).format(r) for r in caplog.records
    )


def test_celery_retry_keeps_retry_then_success_without_leaking_error(caplog):
    with caplog.at_level(logging.INFO):
        result = retry_task.apply(kwargs={"password": "private_retry_password"})
    assert result.successful() and result.get() == "ok"
    assert "private_retry_password" not in caplog.text
    assert any(getattr(r, "component", None) == "celery" for r in caplog.records)


@celery_app.task(name="tests.diagnostic_expected", throws=(ValueError,))
def expected_failure():
    raise ValueError("private_expected_error")


def test_celery_expected_failure_stays_informational(caplog):
    with caplog.at_level(logging.INFO):
        result = expected_failure.apply()
    assert result.failed() and isinstance(result.result, ValueError)
    records = [r for r in caplog.records if getattr(r, "task", None) == "tests.diagnostic_expected"]
    assert records and all(r.levelno == logging.INFO for r in records)
    assert "private_" not in caplog.text


def test_real_worker_failure_and_retry_preserve_outcomes(caplog):
    import os
    import uuid

    from celery import Celery
    from celery.contrib.testing.app import setup_default_app
    from celery.contrib.testing.worker import start_worker
    from celery.result import EagerResult

    url = os.getenv("TEST_REDIS_URL")
    if not url:
        pytest.skip("explicit disposable Redis required for worker validation")
    lab = Celery("diagnostic-worker-test", broker=url, backend=url, set_as_current=False)
    queue = "diagnostic-" + uuid.uuid4().hex
    lab.conf.update(task_default_queue=queue, worker_hijack_root_logger=False)

    @lab.task(name="tests.worker_failure", throws=None, shared=False)
    def worker_failure(password):
        raise RuntimeError(password)

    @lab.task(bind=True, name="tests.worker_retry", max_retries=1, shared=False)
    def worker_retry(self, password):
        if not self.request.retries:
            raise self.retry(exc=RuntimeError(password), countdown=0)
        return self.request.retries

    with setup_default_app(lab), caplog.at_level(logging.INFO):
        with start_worker(
            lab,
            pool="solo",
            queues=[queue],
            perform_ping_check=False,
            loglevel="INFO",
            shutdown_timeout=10,
        ):
            failed = worker_failure.delay("private_worker_password")
            assert not isinstance(failed, EagerResult)
            assert isinstance(failed.get(timeout=10, propagate=False), RuntimeError)
            assert failed.failed()
            retried = worker_retry.delay("private_worker_retry_password")
            assert retried.get(timeout=10) == 1 and retried.successful()
    failures = [r for r in caplog.records if getattr(r, "task_id", None) == failed.id]
    retries = [r for r in caplog.records if getattr(r, "task_id", None) == retried.id]
    assert failures and retries
    assert any(r.phase == "execute" for r in failures)
    assert any(r.phase == "retry" for r in retries)
    assert "private_worker" not in caplog.text
    assert "private_worker" not in "\n".join(
        TaskFormatter(use_color=False).format(r) for r in caplog.records
    )


def test_worker_framework_error_does_not_leak_raw_exception(caplog):
    logger = logging.getLogger("celery.worker.request")
    try:
        raise RuntimeError("private_worker_exception") from ValueError("private_chain")
    except RuntimeError as error:
        logger.error("Task handler raised error: %r", error, exc_info=True)
    record = next(r for r in caplog.records if r.name == logger.name)
    assert record.component == "celery" and record.error_type == "RuntimeError"
    assert "Traceback" in record.getMessage() and "private_" not in caplog.text
    assert "private_" not in DevFormatter(color=False).format(record)
