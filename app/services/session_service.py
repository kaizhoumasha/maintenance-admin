from redis.asyncio import Redis

from app.core import AppError, ErrorCode, get_settings
from app.core.security import create_access_token, create_refresh_token
from app.models import User


def _refresh_key(jti: str) -> str:
    return f"refresh:{jti}"


def _sessions_key(user_id: str) -> str:
    return f"user_refresh:{user_id}"


async def issue_tokens(redis: Redis, user: User) -> tuple[str, str]:
    """签发 (access, refresh) 并登记 refresh 会话。单点用户会先踢掉其旧会话。"""
    sub = str(user.id)
    ttl = get_settings().REFRESH_TOKEN_EXPIRE_SECONDS
    # shortcut: 读旧会话与写入之间有竞态,并发登录的单点用户可能多留一个会话;需严格时改 Lua
    old = set() if user.allow_multi_login else await redis.smembers(_sessions_key(sub))
    refresh, jti = create_refresh_token(sub)
    async with redis.pipeline() as pipe:  # MULTI/EXEC:全部写入或全部不写,不留孤儿 jti
        if not user.allow_multi_login:
            pipe.delete(_sessions_key(sub), *(_refresh_key(j) for j in old))
        pipe.set(_refresh_key(jti), sub, ex=ttl)
        pipe.sadd(_sessions_key(sub), jti)
        pipe.expire(_sessions_key(sub), ttl)
        await pipe.execute()
    return create_access_token(sub), refresh


async def claim_refresh(redis: Redis, payload: dict) -> None:
    """轮换的第一步:原子取走并删除旧 jti,并发重放同一个 refresh 只有一个能成功。"""
    sub = await redis.getdel(_refresh_key(payload["jti"]))
    if sub != payload["sub"]:
        raise AppError(ErrorCode.UNAUTHORIZED, "凭证无效")
    await redis.srem(_sessions_key(sub), payload["jti"])


async def revoke_refresh(redis: Redis, payload: dict) -> None:
    await redis.delete(_refresh_key(payload["jti"]))
    await redis.srem(_sessions_key(payload["sub"]), payload["jti"])


async def revoke_all(redis: Redis, user_id: str) -> None:
    jtis = await redis.smembers(_sessions_key(user_id))
    await redis.delete(_sessions_key(user_id), *(_refresh_key(j) for j in jtis))
