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
        "http://user@example.test",
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
        {"path": "/x?inline=1"},
        {"path": "/x?inline=1", "query": (("q", "1"),)},
        {"path": "/../admin"},
        {"path": "../admin"},
        {"path": "/a/./b"},
        {"path": "/a/.."},
        {"path": "."},
        {"path": "/a/%2e%2e/b"},
        {"path": "/%2E%2E/admin"},
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
    from app.http_client import validate_request

    config = HttpClientConfig("http://example.test", "test", max_request_body_bytes=4)
    request = HttpRequest(**({"method": "POST", "path": "/"} | changes))
    with pytest.raises((ValueError, TypeError)) as validation_error:
        validate_request(config, request)
    calls = []

    async def handle(req):
        calls.append(req)
        return httpx.Response(200)

    client = AsyncHttpClient(
        HttpClientConfig("http://example.test", "test", max_request_body_bytes=4),
        transport=httpx.MockTransport(handle),
    )
    try:
        with pytest.raises(type(validation_error.value)):
            await client.send(request)
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


def test_validate_without_client_or_event_loop(monkeypatch):
    from app.http_client import validate_request

    def forbidden(*args, **kwargs):
        raise AssertionError("validation must not construct a client")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    monkeypatch.setattr(httpx, "Client", forbidden)
    config = HttpClientConfig("http://example.test/prefix", "test", max_request_body_bytes=4)
    assert (
        validate_request(
            config, HttpRequest("POST", "/x%20y", query=(("q", "1"), ("q", "2")), body=b"1234")
        )
        is None
    )


@pytest.mark.parametrize("headers", [(), (("Accept", "custom/type"), ("User-Agent", "caller"))])
async def test_send_preserves_default_headers_and_caller_overrides(headers):
    seen = []

    async def handle(request):
        seen.append(request)
        return httpx.Response(200, content=b"ok")

    transport = httpx.MockTransport(handle)
    async with httpx.AsyncClient(
        base_url="http://example.test/prefix", transport=transport
    ) as reference:
        expected = reference.build_request(
            "POST", "/opaque", headers=(*headers, ("Accept-Encoding", "identity")), content=b"raw"
        )
    client = AsyncHttpClient(
        HttpClientConfig("http://example.test/prefix", "test"), transport=transport
    )
    try:
        await client.send(HttpRequest("POST", "/opaque", headers=headers, body=b"raw"))
        assert seen[0].headers == expected.headers
        assert seen[0].url == expected.url and seen[0].content == expected.content
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


async def test_missing_system_ca_keeps_https_verification_enabled(monkeypatch, tmp_path):
    import ssl

    paths = ssl.get_default_verify_paths()._replace(
        openssl_cafile=str(tmp_path / "missing.pem"),
        openssl_capath=str(tmp_path / "missing-certs"),
    )
    monkeypatch.setattr(ssl, "get_default_verify_paths", lambda: paths)
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "environment.pem"))
    contexts = []

    def build_transport(**kwargs):
        contexts.append(kwargs["verify"])
        return httpx.MockTransport(lambda req: httpx.Response(204))

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", build_transport)
    client = AsyncHttpClient(HttpClientConfig("https://example.test", "test"))
    try:
        assert contexts[0].verify_mode == ssl.CERT_REQUIRED
        assert contexts[0].check_hostname
        assert contexts[0].cert_store_stats()["x509_ca"] == 0
    finally:
        await client.aclose()


async def test_base_prefix_cannot_be_escaped_but_dots_inside_segments_are_fine():
    seen = []

    async def handle(req):
        seen.append(req.url.path)
        return httpx.Response(204, stream=httpx.ByteStream(b""))

    client = AsyncHttpClient(
        HttpClientConfig("http://example.test/prefix", "test"),
        transport=httpx.MockTransport(handle),
    )
    try:
        for escaping in ("/../admin", "../admin", "/a/../../admin"):
            with pytest.raises(ValueError):
                await client.send(HttpRequest("GET", escaping))
        assert seen == []
        await client.send(HttpRequest("GET", "/v1.0/a..b/..c/d.."))
        assert seen == ["/prefix/v1.0/a..b/..c/d.."]
    finally:
        await client.aclose()
