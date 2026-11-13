from celery import Celery, bootsteps
from celery.schedules import crontab

from app.businesses import catalog
from app.core import get_settings
from app.integrations.discovery import load_runtime_contracts
from app.tasks._db import check_worker_database
from app.tasks.diagnostics import install_celery_diagnostics
from app.xray_dispatch_trace import SOURCE

install_celery_diagnostics()
settings = get_settings()
load_runtime_contracts(settings)

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
        "app.tasks.business_tasks",
    ],
)


class CheckRuntimeAuthority(bootsteps.StartStopStep):
    def create(self, worker):
        check_worker_database(get_settings().DATABASE_URL)


celery_app.steps["worker"].add(CheckRuntimeAuthority)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_track_started=True,
    # crontab 按上面的 timezone(Asia/Shanghai)解释
    beat_schedule={
        **{
            f"advance-business-{business}": {
                "task": "business.advance_pending",
                "args": (business,),
                "schedule": 5.0,
                "options": {
                    "ignore_result": True,
                    **({"headers": {SOURCE: "beat"}} if business == "xray" else {}),
                },
            }
            for business in catalog.progressions()
        },
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
