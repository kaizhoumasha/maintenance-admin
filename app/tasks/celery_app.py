from celery import Celery
from celery.schedules import crontab

from app.core import get_settings

settings = get_settings()

celery_app = Celery(
    settings.APP_NAME,
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=[
        "app.tasks.user_tasks",
        "app.tasks.audit_tasks",
        "app.tasks.reliable_tasks",
        "app.tasks.callback_tasks",
        "app.tasks.transport_tasks",
    ],
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
        "cleanup-callback-snapshots": {
            "task": "callback.cleanup_snapshots",
            "schedule": crontab(hour=3, minute=30),
        },
        "scan-stuck-transport-tasks": {
            "task": "transport.scan_stuck",
            "schedule": crontab(minute="*/10"),
        },
        "cleanup-callback-receipts": {
            "task": "callback.cleanup_receipts",
            "schedule": crontab(hour=3, minute=45),
        },
        "archive-audit": {"task": "audit.archive_old", "schedule": crontab(hour=3, minute=0)},
        "cleanup-reliable": {
            "task": "reliable.cleanup_terminal",
            "schedule": crontab(hour=3, minute=15),
        },
    },
)
