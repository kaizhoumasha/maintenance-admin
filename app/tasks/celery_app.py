from celery import Celery

from app.core import get_settings

settings = get_settings()

celery_app = Celery(
    "maintenance-admin",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.tasks.user_tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_track_started=True,
)
