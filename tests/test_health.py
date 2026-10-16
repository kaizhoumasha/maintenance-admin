import pytest

from app.core.database import get_session
from app.main import app
from app.services import health_service


async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_ready_ok(client):
    r = await client.get("/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_ready_redis_down(client, redis):
    async def boom():
        raise ConnectionError("down")

    redis.ping = boom
    r = await client.get("/ready")
    assert r.status_code == 503
    assert r.json()["code"] == "DATABASE_UNAVAILABLE"
    assert r.json()["detail"] == "redis"


async def test_ready_db_down(client):
    class BrokenSession:
        async def execute(self, *a, **k):
            raise ConnectionError("down")

    async def broken():
        yield BrokenSession()

    app.dependency_overrides[get_session] = broken  # client fixture 结束时统一清理
    r = await client.get("/ready")
    assert r.status_code == 503
    assert r.json()["detail"] == "db"


async def test_cors_preflight_allows_configured_origin(client):
    r = await client.options(
        "/api/v1/auth/login",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"},
    )
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert r.headers["access-control-allow-credentials"] == "true"


async def test_cors_rejects_unknown_origin(client):
    r = await client.get("/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in r.headers


@pytest.mark.parametrize("origin", ["http://localhost:5173", "http://evil.example"])
async def test_cors_on_unhandled_error(client, monkeypatch, origin):
    async def boom(*args):
        raise RuntimeError("internal failure")

    monkeypatch.setattr(health_service, "ensure_ready", boom)
    r = await client.get("/ready", headers={"Origin": origin})
    assert r.status_code == 500 and r.json()["code"] == "INTERNAL_ERROR"
    assert r.headers["X-Trace-Id"] == r.json()["trace_id"]
    if origin == "http://localhost:5173":
        assert r.headers["access-control-allow-origin"] == origin
        assert r.headers["access-control-allow-credentials"] == "true"
        assert "X-Trace-Id" in r.headers["access-control-expose-headers"]
        assert "Origin" in r.headers["vary"]
    else:
        assert "access-control-allow-origin" not in r.headers


async def test_lifespan_releases_pools(monkeypatch):
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncEngine

    calls: list[str] = []

    async def dispose(self) -> None:
        calls.append("engine")

    async def aclose(self) -> None:
        calls.append("redis")

    monkeypatch.setattr(AsyncEngine, "dispose", dispose)
    monkeypatch.setattr(Redis, "aclose", aclose)
    async with app.router.lifespan_context(app):
        assert calls == []
    assert calls == ["engine", "redis"]
