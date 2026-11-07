import pytest


@pytest.mark.parametrize("days", [0, -1])
async def test_cleanup_rejects_invalid_retention_before_opening_storage(days):
    from app.tasks.reliable_tasks import run_cleanup

    with pytest.raises(ValueError, match="retention_days"):
        await run_cleanup(None, days)


def test_terminal_cleanup_registered_without_sender_task():
    from app.tasks import reliable_tasks
    from app.tasks.celery_app import celery_app

    assert reliable_tasks.cleanup_terminal.name == "reliable.cleanup_terminal"
    entries = [
        entry
        for entry in celery_app.conf.beat_schedule.values()
        if entry["task"] == "reliable.cleanup_terminal"
    ]
    assert len(entries) == 1
    from celery.schedules import crontab

    assert entries[0]["schedule"] == crontab(hour=3, minute=15)
    assert not any(
        name.startswith("reliable.") and name != "reliable.cleanup_terminal"
        for name in celery_app.tasks
    )


async def test_cleanup_task_commits_on_real_database(pg_sessions, pg_url, monkeypatch):
    import asyncio
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from sqlalchemy import delete, select

    from app.core.identifiers import uuid7
    from app.models import ReliableMessage
    from app.tasks import reliable_tasks
    from tests.helpers import clear_integration_history, seed_reliable_message

    roots = []
    marker = "task-" + uuid7().hex
    old = datetime.now(UTC) - timedelta(days=100)
    try:
        async with pg_sessions() as session:
            for status in ("DONE", "PENDING", "SENDING"):
                message = await seed_reliable_message(
                    session,
                    target=marker,
                    operation="opaque",
                    operation_id=status,
                    request_raw=b"x",
                    status=status,
                    attempts=0,
                    next_attempt_at=old,
                    created_at=old,
                    updated_at=old,
                )
                roots.append(message.exchange_id)
            await session.commit()
        monkeypatch.setattr(
            reliable_tasks,
            "get_settings",
            lambda: SimpleNamespace(DATABASE_URL=pg_url, RELIABLE_RETENTION_DAYS=90),
        )
        assert (
            await asyncio.to_thread(lambda: reliable_tasks.cleanup_terminal.apply(throw=True).get())
            == 1
        )
        async with pg_sessions() as session:
            states = await session.scalars(
                select(ReliableMessage.status).where(ReliableMessage.target == marker)
            )
            assert set(states) == {"PENDING", "SENDING"}
    finally:
        async with pg_sessions() as session:
            await session.execute(delete(ReliableMessage).where(ReliableMessage.target == marker))
            await clear_integration_history(session, roots)
            await session.commit()
