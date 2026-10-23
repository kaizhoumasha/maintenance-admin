import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from celery.schedules import crontab
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database import create_database_engine as create_async_engine
from app.models import AuditLog, AuditLogArchive, Base
from app.tasks import audit_tasks
from app.tasks.celery_app import celery_app


def test_archive_task_registered_and_scheduled_daily_at_3() -> None:
    assert "audit.archive_old" in celery_app.tasks  # 钉住 include 接线
    entry = celery_app.conf.beat_schedule["archive-audit"]
    assert entry["task"] == "audit.archive_old"
    assert entry["schedule"] == crontab(hour=3, minute=0)
    assert celery_app.conf.timezone == "Asia/Shanghai"


@pytest.fixture
def eager_celery():
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    yield
    celery_app.conf.task_always_eager = False
    celery_app.conf.task_eager_propagates = False


async def _prepare(url: str, ages: list[int]) -> None:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(UTC)
    async with async_sessionmaker(engine)() as s:
        s.add_all(
            AuditLog(action="a", trace_id="t", created_at=now - timedelta(days=d)) for d in ages
        )
        await s.commit()
    await engine.dispose()


async def _counts(url: str) -> tuple[int, int]:
    engine = create_async_engine(url)
    async with async_sessionmaker(engine)() as s:
        src = await s.scalar(select(func.count()).select_from(AuditLog))
        arc = await s.scalar(select(func.count()).select_from(AuditLogArchive))
    await engine.dispose()
    return src, arc


def test_archive_task_end_to_end(tmp_path, monkeypatch, eager_celery, caplog) -> None:
    """走真实任务入口(自建 engine + asyncio.run),用文件库代替内存库以便跨 engine 共享。"""
    url = f"sqlite+aiosqlite:///{tmp_path / 't.db'}"
    monkeypatch.setattr(
        audit_tasks,
        "get_settings",
        lambda: SimpleNamespace(DATABASE_URL=url, AUDIT_RETENTION_DAYS=90),
    )
    asyncio.run(_prepare(url, [100, 95, 10]))

    assert audit_tasks.archive_old.delay().get() == 2
    assert asyncio.run(_counts(url)) == (1, 2)
    assert audit_tasks.archive_old.delay().get() == 0  # 幂等
    assert asyncio.run(_counts(url)) == (1, 2)
