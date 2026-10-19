import asyncio

import httpx
import pytest

from app.http_client import (
    AsyncHttpClient,
    ClientClosedError,
    DeliveryState,
    FailureKind,
    HttpClientConfig,
    HttpRequest,
)


async def reply(raw, writer, stats):
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
    await writer.drain()


async def test_connections_reused_environment_ignored_and_close_is_idempotent(
    http_server, monkeypatch
):
    for name in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy"]:
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/ca.pem")
    async with http_server(reply) as (url, stats):
        client = AsyncHttpClient(HttpClientConfig(url, "local"))
        for _ in range(2):
            result = await client.send(HttpRequest("GET", "/"))
            assert result.body == b"ok"
        assert stats == {"connections": 1, "requests": 2}
        await client.aclose()
        await client.aclose()
        with pytest.raises(ClientClosedError):
            await client.send(HttpRequest("GET", "/"))
        assert stats["requests"] == 2


@pytest.mark.parametrize(
    "total,read,kind", [(0.08, 1, FailureKind.TOTAL_TIMEOUT), (1, 0.04, FailureKind.READ_TIMEOUT)]
)
async def test_real_timeout_stops_waiting_and_releases_connection(http_server, total, read, kind):
    async def blocked(raw, writer, stats):
        if stats["requests"] == 1:
            await asyncio.Event().wait()
        await reply(raw, writer, stats)

    async with http_server(blocked) as (url, stats):
        client = AsyncHttpClient(
            HttpClientConfig(
                url,
                "local",
                total_timeout_s=total,
                read_timeout_s=read,
                max_connections=1,
                max_keepalive_connections=1,
            )
        )
        try:
            async with asyncio.timeout(2):
                result = await client.send(HttpRequest("GET", "/"))
            assert result.failure_kind == kind
            assert result.delivery_state == DeliveryState.DELIVERY_UNKNOWN
            assert (await client.send(HttpRequest("GET", "/"))).body == b"ok"
        finally:
            await client.aclose()


async def test_pool_timeout_is_not_sent_and_cancel_does_not_leak_pool(http_server):
    entered = asyncio.Event()

    async def blocked(raw, writer, stats):
        if stats["requests"] == 1:
            entered.set()
            await asyncio.Event().wait()
        await reply(raw, writer, stats)

    async with http_server(blocked) as (url, stats):
        client = AsyncHttpClient(
            HttpClientConfig(
                url, "local", max_connections=1, max_keepalive_connections=1, pool_timeout_s=0.04
            )
        )
        pending = asyncio.create_task(client.send(HttpRequest("GET", "/")))
        try:
            async with asyncio.timeout(2):
                await entered.wait()
                result = await client.send(HttpRequest("GET", "/"))
            assert result.failure_kind == FailureKind.POOL_TIMEOUT
            assert result.delivery_state == DeliveryState.NOT_SENT
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert (await client.send(HttpRequest("GET", "/"))).body == b"ok"
        finally:
            pending.cancel()
            await client.aclose()


async def test_slow_chunks_are_bounded_by_total_deadline(http_server):
    async def trickle(raw, writer, stats):
        writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
        for _ in range(100):
            writer.write(b"1\r\nx\r\n")
            await writer.drain()
            # Timed events simulate a slow peer; no wall-time equality assertions.
            gate = asyncio.Event()
            timer = asyncio.get_running_loop().call_later(0.01, gate.set)
            try:
                await gate.wait()
            finally:
                timer.cancel()

    async with http_server(trickle) as (url, stats):
        client = AsyncHttpClient(
            HttpClientConfig(url, "local", read_timeout_s=0.2, total_timeout_s=0.07)
        )
        try:
            async with asyncio.timeout(2):
                result = await client.send(HttpRequest("GET", "/"))
            assert result.failure_kind == FailureKind.TOTAL_TIMEOUT
            assert result.status_code == 200
            assert result.body is None
        finally:
            await client.aclose()


@pytest.mark.parametrize(
    "headers,body,kind",
    [
        (
            b"Transfer-Encoding: chunked\r\n",
            b"5\r\n12345\r\n0\r\n\r\n",
            FailureKind.RESPONSE_TOO_LARGE,
        ),
        (b"Content-Length: 999\r\n", b"12345", FailureKind.RESPONSE_TOO_LARGE),
        (b"Content-Length: 9\r\n", b"1", FailureKind.REMOTE_PROTOCOL_ERROR),
        (
            b"Content-Encoding: gzip\r\nContent-Length: 3\r\n",
            b"zip",
            FailureKind.CONTENT_ENCODING_UNSUPPORTED,
        ),
    ],
)
async def test_real_stream_errors_preserve_headers_and_free_pool(http_server, headers, body, kind):
    async def response(raw, writer, stats):
        if stats["requests"] > 1:
            await reply(raw, writer, stats)
            return
        writer.write(b"HTTP/1.1 200 OK\r\n" + headers + b"\r\n" + body)
        await writer.drain()
        writer.close()

    async with http_server(response) as (url, stats):
        client = AsyncHttpClient(
            HttpClientConfig(
                url,
                "local",
                max_connections=1,
                max_keepalive_connections=1,
                max_response_body_bytes=4,
            )
        )
        try:
            result = await client.send(HttpRequest("GET", "/"))
            assert result.failure_kind == kind
            assert result.delivery_state == DeliveryState.DELIVERY_UNKNOWN
            assert result.status_code == 200
            assert result.body is None
            assert (await client.send(HttpRequest("GET", "/"))).body == b"ok"
        finally:
            await client.aclose()


async def test_deadline_expired_before_transport_is_not_sent():
    times = iter([0.0, 11.0, 11.0])
    calls = []

    async def handle(req):
        calls.append(req)
        return httpx.Response(200)

    client = AsyncHttpClient(
        HttpClientConfig("http://example.test", "test"),
        transport=httpx.MockTransport(handle),
        clock=lambda: next(times),
    )
    try:
        result = await client.send(HttpRequest("GET", "/"))
        assert result.failure_kind == FailureKind.TOTAL_TIMEOUT
        assert result.delivery_state == DeliveryState.NOT_SENT
        assert calls == []
    finally:
        await client.aclose()


async def test_remaining_deadline_caps_phase_timeouts():
    times = iter([0.0, 8.0, 8.0])
    captured = []

    async def handle(req):
        captured.append(req.extensions["timeout"])
        return httpx.Response(204, stream=httpx.ByteStream(b""))

    client = AsyncHttpClient(
        HttpClientConfig("http://example.test", "test"),
        transport=httpx.MockTransport(handle),
        clock=lambda: next(times),
    )
    try:
        assert (await client.send(HttpRequest("GET", "/"))).body == b""
        assert captured == [dict(connect=2.0, read=2.0, write=2.0, pool=2.0)]
    finally:
        await client.aclose()


async def test_connect_refusal_does_not_prove_business_failure():
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    client = AsyncHttpClient(HttpClientConfig(f"http://127.0.0.1:{port}", "local"))
    try:
        result = await client.send(HttpRequest("GET", "/"))
        assert result.delivery_state == DeliveryState.NOT_SENT
        assert result.failure_kind == FailureKind.CONNECT_ERROR
    finally:
        await client.aclose()


async def test_real_redirect_does_not_issue_a_second_request(http_server):
    async def redirect(raw, writer, stats):
        writer.write(b"HTTP/1.1 302 Found\r\nLocation: /next\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()

    async with http_server(redirect) as (url, stats):
        client = AsyncHttpClient(HttpClientConfig(url, "local"))
        try:
            result = await client.send(HttpRequest("GET", "/"))
            assert result.status_code == 302
            assert result.body == b""
            assert stats["requests"] == 1
        finally:
            await client.aclose()


async def test_cancellation_during_body_read_releases_pool(http_server):
    body_started = asyncio.Event()

    async def partial(raw, writer, stats):
        if stats["requests"] == 1:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nx")
            await writer.drain()
            body_started.set()
            await asyncio.Event().wait()
        await reply(raw, writer, stats)

    async with http_server(partial) as (url, stats):
        client = AsyncHttpClient(
            HttpClientConfig(url, "local", max_connections=1, max_keepalive_connections=1)
        )
        pending = asyncio.create_task(client.send(HttpRequest("GET", "/")))
        try:
            async with asyncio.timeout(2):
                await body_started.wait()
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
                assert (await client.send(HttpRequest("GET", "/"))).body == b"ok"
        finally:
            pending.cancel()
            await client.aclose()


async def test_cancelled_close_can_be_awaited_again_without_restarting_transport():
    class ClosingTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = 0
            self.closed = False

        async def aclose(self):
            self.calls += 1
            self.started.set()
            await self.release.wait()
            self.closed = True

    transport = ClosingTransport()
    client = AsyncHttpClient(HttpClientConfig("http://example.test", "test"), transport=transport)
    pending = asyncio.create_task(client.aclose())
    await transport.started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    try:
        with pytest.raises(ClientClosedError):
            await client.send(HttpRequest("GET", "/"))
        transport.release.set()
        await client.aclose()
        assert transport.closed
        assert transport.calls == 1
    finally:
        transport.release.set()


async def test_concurrent_close_waits_for_the_same_completion():
    class ClosingTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = 0

        async def aclose(self):
            self.calls += 1
            self.started.set()
            await self.release.wait()

    transport = ClosingTransport()
    client = AsyncHttpClient(HttpClientConfig("http://example.test", "test"), transport=transport)
    first = asyncio.create_task(client.aclose())
    await transport.started.wait()
    second = asyncio.create_task(client.aclose())
    try:
        done, _ = await asyncio.wait([second], timeout=0.02)
        assert not done
    finally:
        transport.release.set()
        await asyncio.gather(first, second)
    assert transport.calls == 1


async def test_failed_close_can_retry_resource_release():
    class FailingTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.calls = 0
            self.closed = False

        async def aclose(self):
            self.calls += 1
            if self.calls == 1:
                raise httpx.CloseError("close failed")
            self.closed = True

    transport = FailingTransport()
    client = AsyncHttpClient(HttpClientConfig("http://example.test", "test"), transport=transport)
    with pytest.raises(httpx.CloseError):
        await client.aclose()
    await client.aclose()
    assert transport.closed
    assert transport.calls == 2
