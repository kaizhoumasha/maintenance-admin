"""Redis 滑动窗口限流:一个 Lua 脚本一次往返完成清理、计数、写入,保证原子。"""

import uuid

from fastapi import Request
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.core.errors import AppError, ErrorCode
from app.core.logger import logger
from app.core.redis import RedisDep

# 用 Redis 的 TIME 而不是应用时钟,多实例部署不受时钟偏差影响;被拒绝的请求不入账,
# 否则持续被拒的客户端会一直续期自己的封禁。返回 {是否放行, 需等待秒数}
_SCRIPT = """
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
local window = tonumber(ARGV[1]) * 1000
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, now - window)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  return {0, math.ceil((tonumber(oldest[2]) + window - now) / 1000)}
end
redis.call('ZADD', KEYS[1], now, ARGV[3])
redis.call('EXPIRE', KEYS[1], ARGV[1])
return {1, 0}
"""

WINDOW = 60  # 秒;key 里带窗口是为了日后改窗口不会和旧数据混用


class RateLimit:
    """
    限流依赖。全局默认挂在 app 上;路由要覆盖就在装饰器里再放一个同 name 的实例,
    默认那个会自动跳过。不同 name(如登录的 "login")与默认限流叠加生效。
    """

    def __init__(self, limit: int, name: str = "ip", per_route: bool = True):
        self.limit, self.name, self.per_route = limit, name, per_route

    async def __call__(self, request: Request, redis: RedisDep) -> None:
        route = request.scope["route"]
        # 路由里有同名实例即覆盖,全局默认让路;全局依赖是否出现在 route.dependencies 取决于注册方式
        overrides = [
            d.dependency
            for d in route.dependencies
            if isinstance(d.dependency, RateLimit) and d.dependency.name == self.name
        ]
        if overrides and overrides[-1] is not self:
            return
        # shortcut: 取对端地址,反向代理后所有人共用一个 IP;部署时改为解析可信的 X-Forwarded-For
        ip = request.client.host if request.client else "unknown"
        key = f"{self.name}:{ip}" + (f":{route.path}" if self.per_route else "")
        try:
            allowed, retry_after = await redis.eval(
                _SCRIPT,
                1,
                f"ratelimit:{key}:{WINDOW}",
                WINDOW,
                self.limit,
                uuid.uuid4().hex,
            )
        except (RedisConnectionError, RedisTimeoutError):
            # fail-open:限流是保护不是门禁,Redis 挂了不能把整个 API 一起拖下水;
            # 只放行连接/超时,脚本报错(ResponseError)是 bug,要暴露而不是静默放行
            logger.warning("ratelimit unavailable, fail-open | %s", key, exc_info=True)
            return
        if not allowed:
            raise AppError(
                ErrorCode.TOO_MANY_REQUESTS,
                "请求过于频繁，请稍后重试",
                headers={"Retry-After": str(retry_after)},
            )
