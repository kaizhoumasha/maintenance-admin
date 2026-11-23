import logging

import httpx

from app.http_client import AsyncHttpClient, HttpClientConfig, HttpRequest


async def test_request_diagnostics_do_not_leak_secrets(caplog):
    async def handle(request):
        return httpx.Response(200, stream=httpx.ByteStream(b"secret-body"))

    client = AsyncHttpClient(
        HttpClientConfig("http://example.test", "safe"), transport=httpx.MockTransport(handle)
    )
    try:
        with caplog.at_level(logging.DEBUG):
            await client.send(
                HttpRequest(
                    "POST",
                    "/",
                    query=(("key", "secret-query"),),
                    headers=(("Authorization", "secret-token"),),
                    body=b"secret-body",
                )
            )
        assert "secret-" not in caplog.text
        assert "http://example.test" not in caplog.text
        record = next(r for r in caplog.records if r.name == "app.http_client")
        assert record.destination_key == "safe"
    finally:
        await client.aclose()


async def test_network_exception_text_is_not_logged(caplog):
    async def handle(request):
        raise httpx.ReadError("secret-query secret-body secret-token")

    client = AsyncHttpClient(
        HttpClientConfig("http://example.test", "safe"), transport=httpx.MockTransport(handle)
    )
    try:
        with caplog.at_level(logging.DEBUG):
            await client.send(
                HttpRequest("POST", "/", query=(("key", "secret-query"),), body=b"secret-body")
            )
        assert "secret-" not in caplog.text
        record = next(r for r in caplog.records if r.name == "app.http_client")
        assert record.failure_kind == "READ_ERROR"
    finally:
        await client.aclose()


async def test_real_transport_does_not_log_response_credentials(http_server, caplog):
    async def reply(raw, writer, stats):
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n"
            b"Set-Cookie: secret-session\r\nLocation: /?token=secret-redirect\r\n\r\n"
        )
        await writer.drain()

    async with http_server(reply) as (url, stats):
        client = AsyncHttpClient(HttpClientConfig(url, "safe"))
        try:
            with caplog.at_level(logging.DEBUG):
                result = await client.send(HttpRequest("GET", "/"))
            assert result.status_code == 200
            assert "secret-" not in caplog.text
        finally:
            await client.aclose()


async def test_other_callers_httpcore_logs_remain_visible(http_server, caplog):
    import asyncio

    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked(raw, writer, stats):
        entered.set()
        await release.wait()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()

    async with http_server(blocked) as (url, stats):
        client = AsyncHttpClient(HttpClientConfig(url, "safe"))
        try:
            with caplog.at_level(logging.DEBUG):
                pending = asyncio.create_task(client.send(HttpRequest("GET", "/")))
                async with asyncio.timeout(2):
                    await entered.wait()
                logging.getLogger("httpcore.http11").debug("unrelated-call")
                release.set()
                await pending
            assert "unrelated-call" in caplog.text
        finally:
            release.set()
            await client.aclose()
