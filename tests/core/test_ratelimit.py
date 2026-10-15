from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError

from app.core import register_exception_handlers
from app.core.ratelimit import RateLimit
from app.core.redis import get_redis
from app.middlewares import trace_id_middleware


def _app(redis, default=100, override=None):
    """低阈值小应用:验证限流逻辑本身,不依赖 100 次的真实阈值。"""
    deps = [Depends(RateLimit(override))] if override else []
    app = FastAPI(dependencies=[Depends(RateLimit(default))])
    app.middleware("http")(trace_id_middleware)
    register_exception_handlers(app)
    app.add_api_route("/ping", lambda: {"ok": True}, dependencies=deps)
    app.dependency_overrides[get_redis] = lambda: redis
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_login_sixth_attempt_is_429(client, redis):
    body = {"username": "nobody", "password": "whatever123"}
    for _ in range(5):
        assert (await client.post("/api/v1/auth/login", json=body)).status_code == 401
    r = await client.post("/api/v1/auth/login", json=body)
    assert r.status_code == 429
    assert r.json()["code"] == "TOO_MANY_REQUESTS"
    assert 59 <= int(r.headers["Retry-After"]) <= 60  # 刚发完 5 次,最早一条几乎还没滑出
    assert "ratelimit:login:127.0.0.1:60" in redis.data


async def test_default_limit_blocks_over_threshold(redis):
    async with _app(redis, default=3) as c:
        assert [(await c.get("/ping")).status_code for _ in range(4)] == [200, 200, 200, 429]


async def test_normal_traffic_not_blocked_at_default_100(redis):
    async with _app(redis) as c:  # 真实默认阈值:100 次全放行,第 101 次才拦
        codes = [(await c.get("/ping")).status_code for _ in range(101)]
    assert codes == [200] * 100 + [429]
    assert "ratelimit:ip:127.0.0.1:/ping:60" in redis.data


async def test_route_override_replaces_default(redis):
    # 默认 100、路由覆盖为 2:第 3 次被拦,说明覆盖生效且默认没有重复计数
    async with _app(redis, default=100, override=2) as c:
        assert [(await c.get("/ping")).status_code for _ in range(3)] == [200, 200, 429]
    assert sum(len(v) for v in redis.data.values()) == 2


async def test_fail_open_when_redis_down(redis):
    async def boom(*a, **k):
        raise RedisConnectionError("down")

    redis.eval = boom
    async with _app(redis, default=1) as c:
        assert [(await c.get("/ping")).status_code for _ in range(3)] == [200, 200, 200]


async def test_login_fail_open_when_redis_down(client, redis):
    async def boom(*a, **k):
        raise RedisConnectionError("down")

    redis.eval = boom
    body = {"username": "nobody", "password": "whatever123"}
    codes = [(await client.post("/api/v1/auth/login", json=body)).status_code for _ in range(7)]
    assert codes == [401] * 7  # 照常走登录逻辑,没有 429


async def test_script_error_is_not_swallowed(redis):
    async def broken(*a, **k):
        raise ResponseError("ERR Error running script")

    redis.eval = broken
    async with _app(redis) as c:
        assert (await c.get("/ping")).status_code == 503  # 由 redis_error_handler 报出
