from fastapi import APIRouter

from app.core import ErrorResponse, SessionDep
from app.core.redis import RedisDep
from app.services import health_service

router = APIRouter(tags=["health"])


@router.get("/ready", responses={503: {"model": ErrorResponse}})
async def ready(session: SessionDep, redis: RedisDep) -> dict[str, str]:
    """就绪探针:数据库和 Redis 都可用才返回 200。"""
    await health_service.ensure_ready(session, redis)
    return {"status": "ok"}
