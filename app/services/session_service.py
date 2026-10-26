from redis.asyncio import Redis

from app.core import AppError, ErrorCode, get_settings
from app.core.security import create_access_token, create_refresh_token
from app.models import User

# shortcut: 脚本按现有命名拼接 key,仅支持单机 Redis;上 Cluster 时统一 hash tag 并显式传入 key。
_ISSUE_TOKENS = """
local sessions, token = KEYS[1], KEYS[2]
local sub, ttl, jti, old, multi = ARGV[1], ARGV[2], ARGV[3], ARGV[4], ARGV[5]
local remaining = redis.call('PTTL', sessions)
if old ~= '' then
  if redis.call('GET', 'refresh:' .. old) ~= sub then return 0 end
  redis.call('DEL', 'refresh:' .. old)
  redis.call('SREM', sessions, old)
end
for _, member in ipairs(redis.call('SMEMBERS', sessions)) do
  if multi == '0' then redis.call('DEL', 'refresh:' .. member) end
  if multi == '0' or redis.call('EXISTS', 'refresh:' .. member) == 0 then
    redis.call('SREM', sessions, member)
  end
end
redis.call('SET', token, sub, 'EX', ttl)
redis.call('SADD', sessions, jti)
if remaining ~= -1 then
  redis.call('PEXPIRE', sessions, math.max(remaining, tonumber(ttl) * 1000))
end
return 1
"""

_REVOKE_ALL = """
for _, jti in ipairs(redis.call('SMEMBERS', KEYS[1])) do
  redis.call('DEL', 'refresh:' .. jti)
end
return redis.call('DEL', KEYS[1])
"""


def _refresh_key(jti: str) -> str:
    return f"refresh:{jti}"


def _sessions_key(user_id: str) -> str:
    return f"user_refresh:{user_id}"


async def issue_tokens(redis: Redis, user: User, refresh_jti: str | None = None) -> tuple[str, str]:
    """原子签发/轮换会话,与全部撤销互斥;沿用现有 SET 索引并清理过期成员。"""
    sub = str(user.id)
    ttl = get_settings().REFRESH_TOKEN_EXPIRE_SECONDS
    refresh, jti = create_refresh_token(sub)
    issued = await redis.eval(
        _ISSUE_TOKENS,
        2,
        _sessions_key(sub),
        _refresh_key(jti),
        sub,
        ttl,
        jti,
        refresh_jti or "",
        int(user.allow_multi_login),
    )
    if not issued:
        raise AppError(ErrorCode.UNAUTHORIZED, "凭证无效")
    return create_access_token(sub), refresh


async def revoke_refresh(redis: Redis, payload: dict) -> None:
    await redis.delete(_refresh_key(payload["jti"]))
    await redis.srem(_sessions_key(payload["sub"]), payload["jti"])


async def revoke_all(redis: Redis, user_id: str) -> None:
    await redis.eval(_REVOKE_ALL, 1, _sessions_key(user_id))
