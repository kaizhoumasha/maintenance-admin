from celery import Celery
from celery.schedules import crontab

from app.core import get_settings

settings = get_settings()

celery_app = Celery(
    "maintenance-admin",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.tasks.user_tasks", "app.tasks.audit_tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_track_started=True,
    # crontab 按上面的 timezone(Asia/Shanghai)解释
    beat_schedule={
        "archive-audit": {"task": "audit.archive_old", "schedule": crontab(hour=3, minute=0)}
    },
)
