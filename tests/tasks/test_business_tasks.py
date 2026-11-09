"""The shared task shell owns its pool and registers work in the shared outbox."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from celery import Celery
from celery.beat import Scheduler
from kombu import Connection
from kombu.exceptions import OperationalError
from kombu.transport.memory import Transport
from sqlalchemy import delete

from app.businesses.smt_manual.models import ManualRequest, ManualTask
from app.core.identifiers import uuid7
from app.models import ReliableMessage, WorkLine


@pytest.fixture
def publisher_app(monkeypatch):
    from app.tasks import business_tasks as tasks

    app = Celery(
        "wakeup-publication-test",
        broker="memory://",
        backend="redis://127.0.0.1:1/0",
        set_as_current=False,
    )
    app.conf.task_default_queue = "wakeup_" + uuid7().hex

    def advance(business):
        pass

    task = app.task(name="business.advance_pending", shared=False, lazy=False)(advance)
    monkeypatch.setattr(tasks, "advance_business_pending", task)
    try:
        yield app
    finally:
        app.close()


def test_wakeup_publication_does_not_subscribe_to_result_backend(publisher_app, monkeypatch):
    from app.business_wakeups import enqueue_business

    def forbidden(*args):
        raise AssertionError("wakeup must not access the result backend")

    monkeypatch.setattr(publisher_app.backend, "on_task_call", forbidden)
    enqueue_business("coarse_sorting")
    with publisher_app.connection_for_write() as connection:
        queue = connection.SimpleQueue(publisher_app.conf.task_default_queue)
        try:
            message = queue.get(block=False)
            assert message.headers["task"] == "business.advance_pending"
            assert message.payload[0] == ["coarse_sorting"]
            message.ack()
        finally:
            queue.close()


def test_wakeup_publication_attempts_a_failed_connection_once(publisher_app, monkeypatch):
    from app.business_wakeups import enqueue_business

    attempts = []

    class Unavailable(Transport):
        def establish_connection(self):
            attempts.append(self.client)
            raise self.connection_errors[0]("synthetic broker unavailable")

    def connection_for_write(**kwargs):
        # Controlled broker default allows a retry; the publisher must override it.
        options = {"max_retries": 1, "interval_start": 0, "interval_step": 0, "interval_max": 0}
        options.update(kwargs.get("transport_options", {}))
        return Connection(
            "memory://",
            transport=Unavailable,
            connect_timeout=kwargs.get("connect_timeout", 1),
            transport_options=options,
        )

    @contextmanager
    def producer_or_acquire(producer=None):
        if producer is not None:
            yield producer
        else:
            with connection_for_write() as connection:
                yield connection.Producer()

    monkeypatch.setattr(publisher_app, "connection_for_write", connection_for_write)
    monkeypatch.setattr(publisher_app, "producer_or_acquire", producer_or_acquire)
    monkeypatch.setattr(publisher_app.backend, "on_task_call", lambda *args: None)
    with pytest.raises(OperationalError):
        enqueue_business("coarse_sorting")
    assert len(attempts) == 1
    connection = attempts[0]
    assert connection.connect_timeout <= 1
    assert connection.transport_options["socket_connect_timeout"] <= 1
    assert connection.transport_options["socket_timeout"] <= 1


def test_celery_entry_uses_task_factory_and_fixed_schedule(monkeypatch):
    from app.tasks import business_tasks as tasks
    from app.tasks.celery_app import celery_app

    calls = []

    def run(url, work):
        calls.append((url, work))
        return 7

    monkeypatch.setattr(tasks, "run_with_factory", run)
    assert tasks.advance_business_pending.run("coarse_sorting") == 7
    assert calls[0][0] == tasks.get_settings().DATABASE_URL
    assert "app.tasks.business_tasks" in celery_app.conf.include


async def test_invalid_xray_configuration_does_not_block_manual_pending(callback_db, monkeypatch):
    from app.tasks import business_tasks as tasks

    settings = tasks.get_settings()
    monkeypatch.setattr(settings, "XRAY_TRANSIT_TARGETS", {"XRAY-01": {"A": {}}})
    monkeypatch.setattr(
        settings,
        "RELIABLE_TARGETS",
        {"targets": {"wms": {"base_url": "http://127.0.0.1:1", "protocol": "wms-decisions"}}},
    )
    task_id = str(uuid7())
    line_code = f"SMT-{task_id}"
    async with callback_db.begin() as session:
        line = WorkLine(line_code=line_code, line_name=line_code, business_code="smt_manual")
        session.add(line)
        await session.flush()
        line_id = line.id
        session.add(
            ManualTask(
                task_id=task_id,
                task_type="MANUAL",
                business_type="outbound",
                work_line_id=line_id,
                workline_code=line_code,
                issued_operation_id=str(uuid7()),
                issued_timestamp=1,
                issued_data={},
                queue_revision=1,
                dispatch_sequence=1,
            )
        )

    try:
        with pytest.raises(ValueError, match="XRAY_TRANSIT_TARGETS"):
            await tasks.advance_pending(callback_db, "xray")

        await tasks.advance_pending(callback_db, "smt_manual")
        async with callback_db() as session:
            task = await session.get(ManualTask, task_id)
            assert task.status == "PREPARING"
            message = await session.get(ReliableMessage, task.prepare_message_id)
            assert message.operation == "sorting.task.prepare@v1"
            assert message.operation_id == task.prepare_operation_id
            assert message.business_key == "business:smt_manual"
    finally:
        async with callback_db.begin() as session:
            await session.execute(delete(ManualRequest).where(ManualRequest.task_id == task_id))
            await session.execute(delete(ManualTask).where(ManualTask.task_id == task_id))
            await session.execute(delete(WorkLine).where(WorkLine.id == line_id))


def assert_business_message_queued(app):
    with app.connection_for_write() as connection:
        queue = connection.SimpleQueue(app.conf.task_default_queue)
        try:
            message = queue.get(block=False)
            assert message.headers["task"] == "business.advance_pending"
            assert message.headers["ignore_result"] is True
            assert message.payload[0] == ["coarse_sorting"]
            message.ack()
        finally:
            queue.close()


@pytest.mark.parametrize("registered", [True, False])
def test_beat_publishes_without_result_backend(publisher_app, monkeypatch, registered):
    from app.tasks.celery_app import celery_app

    def forbidden(*args, **kwargs):
        raise RuntimeError("synthetic result backend unavailable")

    monkeypatch.setattr(publisher_app.backend, "on_task_call", forbidden)
    if not registered:
        publisher_app.tasks.pop("business.advance_pending")
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    monkeypatch.setattr(publisher_app, "now", lambda: now[0])
    scheduler = Scheduler(app=publisher_app, lazy=True)
    entry = scheduler.Entry(
        name="business-pending",
        app=publisher_app,
        last_run_at=now[0],
        **celery_app.conf.beat_schedule["advance-business-coarse_sorting"],
    )
    now[0] += timedelta(seconds=4)
    assert not entry.is_due().is_due
    now[0] += timedelta(seconds=1)
    assert entry.is_due().is_due
    with publisher_app.connection_for_write() as connection, connection.Producer() as producer:
        scheduler.apply_async(entry, producer=producer, advance=False)
    assert_business_message_queued(publisher_app)


def test_shared_task_default_publishes_without_result_backend(publisher_app, monkeypatch):
    from app.tasks.celery_app import celery_app

    original = celery_app.tasks["business.advance_pending"]
    # Copy the real task declaration without rebinding the production task's class/app.
    task = type("IsolatedBusinessPending", (type(original),), {})()
    publisher_app.register_task(task)

    def forbidden(*args, **kwargs):
        raise RuntimeError("synthetic result backend unavailable")

    monkeypatch.setattr(publisher_app.backend, "on_task_call", forbidden)
    with publisher_app.connection_for_write() as connection, connection.Producer() as producer:
        task.apply_async(args=("coarse_sorting",), producer=producer, retry=False)
    assert_business_message_queued(publisher_app)
