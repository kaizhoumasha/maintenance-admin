import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.http_client import AsyncHttpClient, ClientClosedError, HttpRequest
from app.integrations.ecs_status import CAPABILITY_NAME, StatusQueryError, query
from tests.helpers import loopback_app

NOW = datetime(2026, 10, 27, tzinfo=UTC)
ORIGIN = "http://127.0.0.1:8765"


@pytest.mark.parametrize("device_code", [None, "ARM03"])
async def test_query_preserves_origin_and_request_scope(status_document, http_factory, device_code):
    async def handle(request):
        return httpx.Response(200, json={**status_document(), "ecs_origin": "http://bad.test"})

    factory, configs, clients, requests = http_factory(handle)
    result = await query(
        ORIGIN + "/", device_code=device_code, client_factory=factory, clock=lambda: NOW
    )
    assert result.capability_name == CAPABILITY_NAME
    assert result.ecs_origin == ORIGIN
    assert result.requested_device_code == device_code
    assert result.fetched_at == NOW
    assert result.devices[0].state.is_online is False
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/api/v1/device/status"
    assert list(requests[0].url.params.multi_items()) == (
        [] if device_code is None else [("device_code", device_code)]
    )
    assert configs[0].total_timeout_s == 5
    assert configs[0].max_response_body_bytes == 262144
    with pytest.raises(ClientClosedError):
        await clients[0].send(HttpRequest("GET", "/"))


@pytest.mark.parametrize("device_code", [None, "ARM03"])
async def test_empty_response_is_a_valid_observation(http_factory, device_code):
    async def handle(request):
        return httpx.Response(200, json={"devices": []})

    factory, *_ = http_factory(handle)
    result = await query(ORIGIN, device_code=device_code, client_factory=factory)
    assert result.devices == ()


@pytest.mark.parametrize(
    ("status", "expected"),
    [(301, "REDIRECT_REJECTED"), (401, "ACCESS_DENIED"), (503, "HTTP_ERROR"), (204, "HTTP_ERROR")],
)
async def test_http_errors_are_safe_single_attempts(http_factory, status, expected):
    async def handle(request):
        return httpx.Response(status, text="secret-password", headers={"Location": "http://bad"})

    factory, _, clients, requests = http_factory(handle)
    with pytest.raises(StatusQueryError) as caught:
        await query(ORIGIN, client_factory=factory, clock=lambda: NOW)
    error = caught.value
    assert (error.ecs_origin, error.completed_at, error.http_status) == (ORIGIN, NOW, status)
    assert error.error_kind == expected
    assert "secret" not in str(error) + repr(error)
    assert len(requests) == 1
    with pytest.raises(ClientClosedError):
        await clients[0].send(HttpRequest("GET", "/"))


@pytest.mark.parametrize("device_code,kind", [(None, "HTTP_ERROR"), ("ARM03", "NOT_FOUND")])
async def test_not_found_preserves_query_scope(http_factory, device_code, kind):
    async def handle(request):
        return httpx.Response(404, content=b"private error")

    factory, *_ = http_factory(handle)
    with pytest.raises(StatusQueryError) as caught:
        await query(ORIGIN, device_code=device_code, client_factory=factory)
    assert caught.value.error_kind == kind
    assert caught.value.requested_device_code == device_code


@pytest.mark.parametrize(
    ("header", "delay", "kind"),
    [
        ("0", 0, "RATE_LIMITED"),
        ("120", 120, "RATE_LIMITED"),
        ("2147483648", 2147483648, "RATE_LIMITED"),
        ("Tue, 27 Oct 2026 00:01:00 GMT", 60, "RATE_LIMITED"),
        ("Mon, 26 Oct 2026 23:59:00 GMT", None, "INVALID_RETRY_AFTER"),
        ("99999999999999999999999", None, "INVALID_RETRY_AFTER"),
        ("bad", None, "INVALID_RETRY_AFTER"),
        (None, None, "INVALID_RETRY_AFTER"),
    ],
)
async def test_retry_after_is_returned_without_waiting(http_factory, header, delay, kind):
    async def handle(request):
        return httpx.Response(429, headers={} if header is None else {"Retry-After": header})

    factory, _, _, requests = http_factory(handle)
    with pytest.raises(StatusQueryError) as caught:
        await query(ORIGIN, client_factory=factory, clock=lambda: NOW)
    assert caught.value.error_kind == kind
    assert caught.value.retry_after_at == (
        None if delay is None else NOW + timedelta(seconds=delay)
    )
    assert len(requests) == 1


async def test_duplicate_retry_after_is_invalid(http_factory):
    async def handle(request):
        return httpx.Response(429, headers=[("Retry-After", "5"), ("retry-after", "10")])

    factory, *_ = http_factory(handle)
    with pytest.raises(StatusQueryError) as caught:
        await query(ORIGIN, client_factory=factory)
    assert caught.value.error_kind == "INVALID_RETRY_AFTER"


@pytest.mark.parametrize(
    "invalid", ["duplicate", "identity", "multiple", "nan", "overflow", "utf8"]
)
async def test_untrusted_response_is_rejected_atomically(status_document, http_factory, invalid):
    document = status_document()
    if invalid == "identity":
        document["devices"][0]["state"]["device_code"] = "ARM04"
    if invalid == "multiple":
        document = status_document("ARM03", "ARM04")
    raw = json.dumps(document).encode()
    if invalid == "duplicate":
        raw = b'{"devices":[],"devices":[]}'
    if invalid in {"nan", "overflow"}:
        raw = b'{"devices":[],"extension":' + (b"NaN" if invalid == "nan" else b"1e999") + b"}"
    if invalid == "utf8":
        raw = b"\xff"

    async def handle(request):
        return httpx.Response(200, content=raw, headers={"Content-Type": "application/json"})

    factory, *_ = http_factory(handle)
    with pytest.raises(StatusQueryError) as caught:
        await query(ORIGIN, device_code="ARM03", client_factory=factory)
    assert caught.value.error_kind == "INVALID_RESPONSE"


async def test_size_limit_and_transport_failure_are_classified(http_factory):
    async def oversized(request):
        return httpx.Response(200, content=b" " * 262145)

    async def timeout(request):
        raise httpx.ReadTimeout("secret remote address")

    for handler, expected in [(oversized, "RESPONSE_TOO_LARGE"), (timeout, "READ_TIMEOUT")]:
        factory, _, clients, requests = http_factory(handler)
        with pytest.raises(StatusQueryError) as caught:
            await query(ORIGIN, client_factory=factory)
        assert caught.value.error_kind == expected
        assert len(requests) == 1
        with pytest.raises(ClientClosedError):
            await clients[0].send(HttpRequest("GET", "/"))


async def test_cancel_closes_client_and_does_not_become_a_status(http_factory):
    started = asyncio.Event()

    async def wait(request):
        started.set()
        await asyncio.Event().wait()

    factory, _, clients, _ = http_factory(wait)
    task = asyncio.create_task(query(ORIGIN, client_factory=factory))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ClientClosedError):
        await clients[0].send(HttpRequest("GET", "/"))


async def test_hung_client_close_leaves_no_background_task():
    class HangingClose(httpx.AsyncBaseTransport):
        def __init__(self):
            self.close_task = None
            self.finished = asyncio.Event()

        async def handle_async_request(self, request):
            return httpx.Response(200, json={"devices": []})

        async def aclose(self):
            self.close_task = asyncio.current_task()
            try:
                await asyncio.Event().wait()
            finally:
                self.finished.set()

    transport = HangingClose()
    try:
        result = await query(
            ORIGIN, client_factory=lambda config: AsyncHttpClient(config, transport=transport)
        )
        assert result.devices == ()
        assert transport.finished.is_set(), "query must not abandon its own close task"
        assert transport.close_task.done()
    finally:
        if transport.close_task is not None and not transport.close_task.done():
            transport.close_task.cancel()
            await asyncio.gather(transport.close_task, return_exceptions=True)


@pytest.mark.parametrize("first_limited", [False, True])
async def test_two_loopback_ecs_keep_independent_sources(status_document, first_limited):
    first, second = FastAPI(), FastAPI()

    @first.get("/api/v1/device/status")
    async def first_status():
        if first_limited:
            return JSONResponse({}, status_code=429, headers={"Retry-After": "30"})
        return status_document("ARM03", "ARM04")

    @second.get("/api/v1/device/status")
    async def second_status():
        return status_document("ARM03", "ARM05")

    async with loopback_app(first) as a, loopback_app(second) as b:

        def factory(config):
            assert config.base_url in {a, b}, "test must not query any other destination"
            return AsyncHttpClient(config)

        results = await asyncio.gather(
            query(a, client_factory=factory),
            query(b, client_factory=factory),
            return_exceptions=True,
        )
    if first_limited:
        assert isinstance(results[0], StatusQueryError)
        assert results[0].ecs_origin == a
        assert results[0].error_kind == "RATE_LIMITED"
        assert results[1].ecs_origin == b
        assert [d.device.device_code for d in results[1].devices] == ["ARM03", "ARM05"]
        return
    assert [result.ecs_origin for result in results] == [a, b]
    assert [[d.device.device_code for d in result.devices] for result in results] == [
        ["ARM03", "ARM04"],
        ["ARM03", "ARM05"],
    ]
