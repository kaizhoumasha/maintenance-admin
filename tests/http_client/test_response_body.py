import httpx
import pytest

from app.http_client import (
    AsyncHttpClient,
    DeliveryState,
    FailureKind,
    HttpClientConfig,
    HttpRequest,
)


class TrackedStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("headers", [[], [("content-length", "1")], [("content-length", "999999")]])
@pytest.mark.parametrize("size", [262144, 262145])
async def test_body_limit_uses_actual_stream_bytes(headers, size):
    stream = TrackedStream([b"x" * 262144, b"x" * (size - 262144), b""])

    async def handle(req):
        return httpx.Response(200, headers=headers, stream=stream)

    client = AsyncHttpClient(
        HttpClientConfig("http://example.test", "test"), transport=httpx.MockTransport(handle)
    )
    try:
        result = await client.send(HttpRequest("GET", "/"))
        assert stream.closed
        assert result.status_code == 200
        if size == 262144:
            assert result.delivery_state == DeliveryState.HTTP_RESPONSE_COMPLETE
            assert result.body == b"x" * size
        else:
            assert result.failure_kind == FailureKind.RESPONSE_TOO_LARGE
            assert result.body is None
            assert stream.reads == 2
    finally:
        await client.aclose()


@pytest.mark.parametrize("encoding", ["gzip", "br", "gzip, identity"])
async def test_encoding_rejected_before_body_is_read(encoding):
    stream = TrackedStream([b"not compressed"])

    async def handle(req):
        return httpx.Response(200, headers={"content-encoding": encoding}, stream=stream)

    client = AsyncHttpClient(
        HttpClientConfig("http://example.test", "test"), transport=httpx.MockTransport(handle)
    )
    try:
        result = await client.send(HttpRequest("GET", "/"))
        assert result.failure_kind == FailureKind.CONTENT_ENCODING_UNSUPPORTED
        assert result.delivery_state == DeliveryState.DELIVERY_UNKNOWN
        assert stream.reads == 0
        assert stream.closed
    finally:
        await client.aclose()


@pytest.mark.parametrize("close_mode", ["block", "error"])
async def test_cleanup_cannot_hide_timeout_result(close_mode):
    import asyncio

    class SlowStream(httpx.AsyncByteStream):
        def __init__(self):
            self.close_started = asyncio.Event()
            self.close_cancelled = asyncio.Event()

        async def __aiter__(self):
            yield b"x"
            await asyncio.Event().wait()

        async def aclose(self):
            self.close_started.set()
            if close_mode == "error":
                raise httpx.ReadError("secret close failure")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.close_cancelled.set()
                raise

    stream = SlowStream()
    client = AsyncHttpClient(
        HttpClientConfig("http://example.test", "test", total_timeout_s=0.02),
        transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=stream)),
    )
    try:
        async with asyncio.timeout(0.3):
            result = await client.send(HttpRequest("GET", "/"))
        assert result.failure_kind == FailureKind.TOTAL_TIMEOUT
        assert result.delivery_state == DeliveryState.DELIVERY_UNKNOWN
        assert result.status_code == 200
        assert result.body is None
        assert stream.close_started.is_set()
        if close_mode == "block":
            assert stream.close_cancelled.is_set()
    finally:
        await client.aclose()


@pytest.mark.parametrize("close_mode", ["error", "block"])
async def test_complete_response_survives_cleanup_failure(close_mode, caplog):
    import asyncio

    class CompleteStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"\xff"
            yield b"ok"

        async def aclose(self):
            if close_mode == "error":
                raise httpx.CloseError("secret-close-error")
            await asyncio.Event().wait()

    client = AsyncHttpClient(
        HttpClientConfig("http://example.test", "test", total_timeout_s=0.05),
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, headers={"x-result": "kept"}, stream=CompleteStream())
        ),
    )
    try:
        async with asyncio.timeout(0.5):
            result = await client.send(HttpRequest("GET", "/"))
        assert result.delivery_state == DeliveryState.HTTP_RESPONSE_COMPLETE
        assert result.failure_kind is None
        assert result.status_code == 200
        assert ("x-result", "kept") in result.headers
        assert result.body == b"\xffok"
        assert "http_client response cleanup failed" in caplog.text
        assert "secret-close-error" not in caplog.text
    finally:
        await client.aclose()


@pytest.mark.parametrize("size", [0, 262144, 262145])
async def test_preloaded_identity_response_is_bounded(size):
    client = AsyncHttpClient(
        HttpClientConfig("http://example.test", "test"),
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b"x" * size)),
    )
    try:
        result = await client.send(HttpRequest("GET", "/"))
        if size <= 262144:
            assert result.delivery_state == DeliveryState.HTTP_RESPONSE_COMPLETE
            assert result.body == b"x" * size
        else:
            assert result.failure_kind == FailureKind.RESPONSE_TOO_LARGE
            assert result.body is None
    finally:
        await client.aclose()


async def test_preloaded_decoded_compressed_response_is_not_treated_as_raw():
    import gzip

    response = httpx.Response(
        200, headers={"content-encoding": "gzip"}, content=gzip.compress(b"plain")
    )
    client = AsyncHttpClient(
        HttpClientConfig("http://example.test", "test"),
        transport=httpx.MockTransport(lambda req: response),
    )
    try:
        result = await client.send(HttpRequest("GET", "/"))
        assert result.failure_kind == FailureKind.CONTENT_ENCODING_UNSUPPORTED
        assert result.body is None
    finally:
        await client.aclose()


async def test_cleanup_error_does_not_replace_caller_cancellation(caplog):
    import asyncio

    reading = asyncio.Event()

    class BrokenCloseStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            reading.set()
            await asyncio.Event().wait()
            yield b""

        async def aclose(self):
            raise httpx.CloseError("secret-cookie")

    client = AsyncHttpClient(
        HttpClientConfig("http://example.test", "test"),
        transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=BrokenCloseStream())),
    )
    pending = asyncio.create_task(client.send(HttpRequest("GET", "/")))
    try:
        async with asyncio.timeout(1):
            await reading.wait()
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        assert "secret-cookie" not in caplog.text
    finally:
        pending.cancel()
        await client.aclose()
