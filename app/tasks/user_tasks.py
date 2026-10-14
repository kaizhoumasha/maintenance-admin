from app.core import logger
from app.tasks.celery_app import celery_app


@celery_app.task(name="users.welcome")
def send_welcome(username: str) -> str:
    logger.info("发送欢迎通知: %s", username)
    return f"welcomed:{username}"
