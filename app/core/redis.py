from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from redis.asyncio import Redis

from app.core.config import get_settings


# 认证用到 GETDEL,需要 Redis >= 6.2
@lru_cache
def get_redis() -> Redis:
    return Redis.from_url(get_settings().REDIS_URL, decode_responses=True)


RedisDep = Annotated[Redis, Depends(get_redis)]
