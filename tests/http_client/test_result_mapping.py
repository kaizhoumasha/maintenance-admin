import httpx
import pytest

from app.http_client import (
    AsyncHttpClient,
    DeliveryState,
    FailureKind,
    HttpClientConfig,
    HttpRequest,
)


@pytest.mark.parametrize("status", [200, 204, 302, 400, 429, 500, 503])
async def test_http_response_is_not_a_business_result(status):
    seen = []

    async def handle(request):
        seen.append(request)
        return httpx.Response(
            status,
            headers=[
                ("location", "/again"),
                ("x-test", "a"),
                ("x-test", "b"),
                ("retry-after", "3"),
            ],
            stream=httpx.ByteStream(b"\xffraw"),
        )

    client = AsyncHttpClient(
        HttpClientConfig("http://example.test/base", "test"), transport=httpx.MockTransport(handle)
    )
    try:
        result = await client.send(
            HttpRequest("POST", "/submit", query=(("a", "1"), ("a", "2")), body=b"\x00exact")
        )
        assert result.delivery_state == DeliveryState.HTTP_RESPONSE_COMPLETE
        assert result.status_code == status
        assert result.body == b"\xffraw"
        assert result.failure_kind is None
        assert [v for k, v in result.headers if k == "x-test"] == ["a", "b"]
        assert len(seen) == 1
        assert seen[0].content == b"\x00exact"
        assert seen[0].headers["accept-encoding"] == "identity"
        assert seen[0].url.params.get_list("a") == ["1", "2"]
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "error,kind,state",
    [
        (httpx.PoolTimeout, "POOL_TIMEOUT", "NOT_SENT"),
        (httpx.ConnectTimeout, "CONNECT_TIMEOUT", "NOT_SENT"),
        (httpx.ConnectError, "CONNECT_ERROR", "NOT_SENT"),
        (httpx.WriteTimeout, "WRITE_TIMEOUT", "DELIVERY_UNKNOWN"),
        (httpx.WriteError, "WRITE_ERROR", "DELIVERY_UNKNOWN"),
        (httpx.ReadTimeout, "READ_TIMEOUT", "DELIVERY_UNKNOWN"),
        (httpx.ReadError, "READ_ERROR", "DELIVERY_UNKNOWN"),
        (httpx.RemoteProtocolError, "REMOTE_PROTOCOL_ERROR", "DELIVERY_UNKNOWN"),
        (httpx.ProxyError, "TRANSPORT_ERROR", "DELIVERY_UNKNOWN"),
    ],
)
async def test_transport_failure_preserves_delivery_certainty(error, kind, state):
    seen = []

    async def handle(request):
        seen.append(request)
        raise error("sensitive exception content")

    client = AsyncHttpClient(
        HttpClientConfig("http://example.test", "test"), transport=httpx.MockTransport(handle)
    )
    try:
        result = await client.send(HttpRequest("GET", "/"))
        assert result.delivery_state == DeliveryState(state)
        assert result.failure_kind == FailureKind(kind)
        assert result.body is None
        assert len(seen) == 1
    finally:
        await client.aclose()
