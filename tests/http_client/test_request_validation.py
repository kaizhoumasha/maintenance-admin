import math

import httpx
import pytest

from app.http_client import AsyncHttpClient, HttpClientConfig, HttpRequest


@pytest.mark.parametrize(
    "field",
    [
        "connect_timeout_s",
        "read_timeout_s",
        "write_timeout_s",
        "pool_timeout_s",
        "total_timeout_s",
        "keepalive_expiry_s",
        "max_connections",
        "max_keepalive_connections",
        "max_request_body_bytes",
        "max_response_body_bytes",
    ],
)
@pytest.mark.parametrize("value", [0, -1, True, math.inf, math.nan])
def test_invalid_resource_limits(field, value):
    with pytest.raises((ValueError, TypeError)):
        HttpClientConfig("http://example.test", "test", **{field: value})


def test_pool_relationship():
    with pytest.raises(ValueError):
        HttpClientConfig(
            "http://example.test", "test", max_connections=1, max_keepalive_connections=2
        )


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.test",
        "http://u:p@example.test",
        "http://example.test?q=s",
        "http://example.test/#x",
    ],
)
def test_invalid_base_url(url):
    with pytest.raises(ValueError):
        HttpClientConfig(url, "test")


@pytest.mark.parametrize(
    "changes",
    [
        {"method": "get"},
        {"method": "GET\r\nx"},
        {"path": "https://other.test/x"},
        {"path": "//other.test/x"},
        {"path": "\\other.test"},
        {"path": "/x#frag"},
        {"headers": (("Host", "other"),)},
        {"headers": (("Content-Length", "1"),)},
        {"headers": (("Transfer-Encoding", "chunked"),)},
        {"headers": (("Accept-Encoding", "gzip"),)},
        {"headers": (("bad name", "x"),)},
        {"headers": (("X-Test", "a\nb"),)},
        {"headers": (("", "x"),)},
        {"query": (("x", "a\rb"),)},
        {"body": "not-bytes"},
        {"body": b"12345"},
    ],
)
async def test_invalid_request_never_reaches_transport(changes):
    calls = []

    async def handle(req):
        calls.append(req)
        return httpx.Response(200)

    client = AsyncHttpClient(
        HttpClientConfig("http://example.test", "test", max_request_body_bytes=4),
        transport=httpx.MockTransport(handle),
    )
    try:
        with pytest.raises((ValueError, TypeError)):
            await client.send(HttpRequest(**({"method": "POST", "path": "/"} | changes)))
        assert calls == []
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "field",
    [
        "max_connections",
        "max_keepalive_connections",
        "max_request_body_bytes",
        "max_response_body_bytes",
    ],
)
def test_integer_limits_reject_fractional_numbers(field):
    with pytest.raises(TypeError):
        HttpClientConfig("http://example.test", "test", **{field: 1.5})


@pytest.mark.parametrize("key", ["", "http://host", "line\nbreak", "A", "x" * 65])
def test_destination_key_is_non_sensitive_identifier(key):
    with pytest.raises(ValueError):
        HttpClientConfig("http://example.test", key)


async def test_exact_request_limit_and_relative_prefix():
    seen = []

    async def handle(req):
        seen.append(req)
        return httpx.Response(204, stream=httpx.ByteStream(b""))

    client = AsyncHttpClient(
        HttpClientConfig("http://example.test/prefix", "test", max_request_body_bytes=4),
        transport=httpx.MockTransport(handle),
    )
    try:
        assert (await client.send(HttpRequest("POST", "x", body=b"1234"))).body == b""
        assert seen[0].url.path == "/prefix/x"
        assert seen[0].content == b"1234"
    finally:
        await client.aclose()


def test_tls_context_cannot_disable_certificate_validation():
    import ssl

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with pytest.raises(ValueError):
        HttpClientConfig("https://example.test", "test", tls_context=context)


async def test_private_ca_context_is_explicitly_used(monkeypatch):
    import ssl

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    options = []

    def build_transport(**kwargs):
        options.append(kwargs)
        return httpx.MockTransport(lambda req: httpx.Response(204, stream=httpx.ByteStream(b"")))

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", build_transport)
    client = AsyncHttpClient(HttpClientConfig("https://example.test", "test", tls_context=context))
    try:
        assert (await client.send(HttpRequest("GET", "/"))).body == b""
        assert options[0]["verify"] is context
        assert options[0]["trust_env"] is False
        assert options[0]["http2"] is False
        assert options[0]["retries"] == 0
    finally:
        await client.aclose()
