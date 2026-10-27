import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.core.database import get_session
from app.http_client import AsyncHttpClient
from app.main import app
from app.models import AuditLog, ReliableMessage
from tests.helpers import bearer, ecs_status_document, loopback_app, make_user_with_perms

PATH = "/api/v1/ecs/status"
ORIGIN = "http://127.0.0.1:8765"


@pytest.fixture
def status_mock(monkeypatch):
    requests = []

    def install(handler):
        async def capture(request):
            requests.append(request)
            return await handler(request)

        def factory(config):
            assert config.base_url == ORIGIN, "no non-mock destination allowed"
            return AsyncHttpClient(config, transport=httpx.MockTransport(capture))

        monkeypatch.setattr(app.state, "ecs_status_client_factory", factory, raising=False)
        return requests

    return install


async def test_status_requires_login_and_dedicated_permission(client, admin, status_mock):
    async def forbidden(request):
        raise AssertionError("unauthorized requests must not query ECS")

    requests = status_mock(forbidden)
    assert (await client.get(PATH, params={"ecs_origin": ORIGIN})).status_code == 401
    user = await make_user_with_perms(client, admin, ["admin:user:read"])
    denied = await client.get(PATH, params={"ecs_origin": ORIGIN}, headers=bearer(user))
    assert denied.status_code == 403
    assert denied.json()["detail"] == "ecs:status:query"
    assert requests == []


async def test_swagger_query_returns_source_without_persisting_results(client, admin, status_mock):
    async def handle(request):
        return httpx.Response(200, json={"devices": []})

    requests = status_mock(handle)
    response = await client.get(
        PATH, params={"ecs_origin": ORIGIN + "/", "device_code": "ARM03"}, headers=admin
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["ecs_origin"] == ORIGIN
    assert result["requested_device_code"] == "ARM03"
    assert result["devices"] == []
    assert result["fetched_at"]
    assert result["capability_name"] == "ecs.device.status@v1"
    assert len(requests) == 1
    assert dict(requests[0].url.params) == {"device_code": "ARM03"}
    async for session in app.dependency_overrides[get_session]():
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 0
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "ecs:status:query")
            )
            == 0
        )


@pytest.mark.parametrize(
    "params",
    [
        {"ecs_origin": "http://localhost:8000"},
        {"ecs_origin": ORIGIN, "device_code": " ARM03"},
        {"ecs_origin": ORIGIN, "extra": "ignored?"},
    ],
)
async def test_invalid_inputs_never_send(client, admin, status_mock, params):
    async def forbidden(request):
        raise AssertionError("invalid query must never send")

    requests = status_mock(forbidden)
    response = await client.get(PATH, params=params, headers=admin)
    assert response.status_code == 400
    assert requests == []


async def test_query_uses_real_loopback_http_with_query_only_permission(client, admin, monkeypatch):
    mock = FastAPI()
    received = []

    @mock.get("/api/v1/device/status")
    async def status(device_code: str | None = None):
        received.append(device_code)
        return ecs_status_document(*((device_code,) if device_code else ("ARM03", "ARM04")))

    user = await make_user_with_perms(client, admin, ["ecs:status:query"])
    async with loopback_app(mock) as origin:

        def factory(config):
            assert config.base_url == origin, "only this run's Mock may be queried"
            return AsyncHttpClient(config)

        monkeypatch.setattr(app.state, "ecs_status_client_factory", factory, raising=False)
        for code in (None, "ARM03"):
            params = {"ecs_origin": origin}
            if code is not None:
                params["device_code"] = code
            response = await client.get(PATH, params=params, headers=bearer(user))
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["ecs_origin"] == origin
            assert [item["device"]["device_code"] for item in result["devices"]] == (
                ["ARM03", "ARM04"] if code is None else [code]
            )
    assert received == [None, "ARM03"]


@pytest.mark.parametrize(
    "mode,expected,code",
    [
        ("bad", 502, "UPSTREAM_ERROR"),
        ("timeout", 504, "UPSTREAM_TIMEOUT"),
        ("limited", 429, "TOO_MANY_REQUESTS"),
    ],
)
async def test_upstream_failures_are_safe_and_traceable(
    client, admin, status_mock, mode, expected, code
):
    async def handle(request):
        if mode == "timeout":
            raise httpx.ReadTimeout("secret supplier failure")
        if mode == "limited":
            return httpx.Response(429, headers={"Retry-After": "120"}, text="secret")
        return httpx.Response(200, text="secret invalid JSON")

    requests = status_mock(handle)
    response = await client.get(PATH, params={"ecs_origin": ORIGIN}, headers=admin)
    assert response.status_code == expected, response.text
    assert response.json()["code"] == code
    assert "secret" not in response.text
    detail = json.loads(response.json()["detail"])
    assert detail["ecs_origin"] == ORIGIN
    assert detail["completed_at"]
    if mode == "limited":
        assert int(response.headers["Retry-After"]) >= 119
    assert len(requests) == 1


@pytest.mark.parametrize("environment", ["local", "production", "test"])
def test_formal_status_is_always_mounted_and_swagger_follows_environment(environment):
    script = """
import asyncio
from httpx import ASGITransport, AsyncClient
from app.main import app
from app.core.redis import get_redis
from app.http_client import AsyncHttpClient

class LimitRedis:
    async def eval(self, *args):
        return [1, 0]

async def forbidden(*args, **kwargs):
    raise AssertionError('unauthorized requests must not query ECS')

AsyncHttpClient.send = forbidden
app.dependency_overrides[get_redis] = lambda: LimitRedis()
paths = app.openapi()['paths']
assert '/api/v1/ecs/status' in paths
assert '/api/v1/dev/ecs/status' not in paths
local = __import__('os').environ['ENVIRONMENT'] == 'local'
assert (app.docs_url == '/docs') is local
assert (app.openapi_url == '/openapi.json') is local

async def check():
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/api/v1/ecs/status',
                                    params={'ecs_origin': 'http://127.0.0.1:8765'})
        assert response.status_code == 401, response.text
        assert (await client.get('/docs')).status_code == (200 if local else 404)
        assert (await client.get('/openapi.json')).status_code == (200 if local else 404)
asyncio.run(check())
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env={
            **os.environ,
            "ENVIRONMENT": environment,
            "DATABASE_URL": "sqlite+aiosqlite://",
            "REDIS_URL": "redis://127.0.0.1:1/0",
            "JWT_SECRET_KEY": "isolated-status-environment-test-secret-32",
            "RELIABLE_TARGETS": "{}",
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def test_status_retains_api_request_rate_limit(client, admin, status_mock):
    async def handle(request):
        return httpx.Response(200, json={"devices": []})

    requests = status_mock(handle)
    for _ in range(100):
        response = await client.get(PATH, params={"ecs_origin": ORIGIN}, headers=admin)
        assert response.status_code == 200, response.text
    response = await client.get(PATH, params={"ecs_origin": ORIGIN}, headers=admin)
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0
    assert len(requests) == 100


def test_swagger_documents_query_result_and_errors():
    operation = app.openapi()["paths"][PATH]["get"]
    assert {"ecs_origin", "device_code"} == {item["name"] for item in operation["parameters"]}
    assert {"200", "400", "401", "403", "429", "502", "504"} <= set(operation["responses"])
    assert "422" not in operation["responses"]
