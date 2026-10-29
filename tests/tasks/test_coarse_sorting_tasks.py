"""The concrete worker owns its pool and registers work in the shared outbox."""


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
