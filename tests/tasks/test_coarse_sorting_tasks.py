"""The concrete worker owns its pool and registers work in the shared outbox."""

from contextlib import contextmanager

import pytest
from celery import Celery
from kombu import Connection
from kombu.exceptions import OperationalError
from kombu.transport.memory import Transport

from app.core.identifiers import uuid7


@pytest.fixture
def publisher_app(monkeypatch):
    from app.tasks import coarse_sorting_tasks as tasks

    app = Celery(
        "wakeup-publication-test",
        broker="memory://",
        backend="redis://127.0.0.1:1/0",
        set_as_current=False,
    )
    app.conf.task_default_queue = "wakeup_" + uuid7().hex

    def advance(source, identity):
        pass

    task = app.task(name="coarse_sorting.advance_one", shared=False, lazy=False)(advance)
    monkeypatch.setattr(tasks, "advance_coarse_sorting_one", task)
    try:
        yield app
    finally:
        app.close()


def test_wakeup_publication_does_not_subscribe_to_result_backend(publisher_app, monkeypatch):
    from app.business_wakeups import enqueue_advance

    def forbidden(*args):
        raise AssertionError("wakeup must not access the result backend")

    monkeypatch.setattr(publisher_app.backend, "on_task_call", forbidden)
    enqueue_advance("coarse_sorting", "wms", "123")
    with publisher_app.connection_for_write() as connection:
        queue = connection.SimpleQueue(publisher_app.conf.task_default_queue)
        try:
            message = queue.get(block=False)
            assert message.headers["task"] == "coarse_sorting.advance_one"
            assert message.payload[0] == ["wms", "123"]
            message.ack()
        finally:
            queue.close()


def test_wakeup_publication_attempts_a_failed_connection_once(publisher_app, monkeypatch):
    from app.business_wakeups import enqueue_advance

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
        enqueue_advance("coarse_sorting", "scan", "original-scan")
    assert len(attempts) == 1
    connection = attempts[0]
    assert connection.connect_timeout <= 1
    assert connection.transport_options["socket_connect_timeout"] <= 1
    assert connection.transport_options["socket_timeout"] <= 1


def test_celery_entry_uses_task_factory_and_fixed_schedule(monkeypatch):
    from app.tasks import coarse_sorting_tasks as tasks
    from app.tasks.celery_app import celery_app

    calls = []

    def run(url, work):
        calls.append((url, work))
        return 7

    monkeypatch.setattr(tasks, "run_with_factory", run)
    assert tasks.advance_coarse_sorting.run() == 7
    assert calls[0][0] == tasks.get_settings().DATABASE_URL
    assert "app.tasks.coarse_sorting_tasks" in celery_app.conf.include
    schedule = celery_app.conf.beat_schedule["advance-coarse-sorting"]
    assert schedule == {"task": "coarse_sorting.advance_pending", "schedule": 5.0}
