from app.core import SessionDep, error_responses
from app.core.redis import RedisDep
from app.core.routing import new_router
from app.services import health_service

router = new_router(tags=["health"])


@router.get("/ready", responses=error_responses(503))
async def ready(session: SessionDep, redis: RedisDep) -> dict[str, str]:
    """就绪探针:数据库和 Redis 都可用才返回 200。"""
    await health_service.ensure_ready(session, redis)
    return {"status": "ok"}
