import logging

import pytest

from app.tasks.celery_app import celery_app
from app.tasks.user_tasks import send_welcome


@pytest.fixture(autouse=True)
def _eager_celery() -> None:
    """测试内任务同步执行，不起 worker；结束后恢复配置。"""
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    yield
    celery_app.conf.task_always_eager = False
    celery_app.conf.task_eager_propagates = False


def test_welcome_task_registered() -> None:
    # 钉住 include=["app.tasks.user_tasks"] 接线正确，
    # 配错了 worker 启动时找不到任务，这个测试先红
    assert "users.welcome" in celery_app.tasks


def test_welcome_task_returns_value() -> None:
    result = send_welcome.delay("kai")
    assert result.successful()
    assert result.get() == "welcomed:kai"


def test_welcome_task_logs_username(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        send_welcome.delay("kai")
    assert any("kai" in rec.getMessage() for rec in caplog.records)
