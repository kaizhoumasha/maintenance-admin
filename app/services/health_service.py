from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import AppError, ErrorCode


async def ensure_ready(session: AsyncSession, redis: Redis) -> None:
    """数据库和 Redis 都可用才返回,否则抛 503,detail 里列出不可用的依赖。"""
    failed = []
    # 探针要的是"能不能用",任何异常都算不可用
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        failed.append("db")
    try:
        await redis.ping()
    except Exception:
        failed.append("redis")
    if failed:
        raise AppError(ErrorCode.DATABASE_UNAVAILABLE, "依赖不可用", ",".join(failed))
