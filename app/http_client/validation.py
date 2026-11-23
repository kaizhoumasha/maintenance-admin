import re
from urllib.parse import unquote

import httpx

from .types import HttpClientConfig, HttpRequest

_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_OWNED_HEADERS = {"host", "content-length", "transfer-encoding", "accept-encoding"}


def build_request(config: HttpClientConfig, request: HttpRequest) -> httpx.Request:
    if not isinstance(request, HttpRequest):
        raise TypeError("request must be HttpRequest")
    if (
        not isinstance(request.method, str)
        or not _TOKEN.fullmatch(request.method)
        or request.method != request.method.upper()
    ):
        raise ValueError("invalid method")
    if not isinstance(request.body, bytes):
        raise TypeError("body must be bytes")
    if len(request.body) > config.max_request_body_bytes:
        raise ValueError("request body limit exceeded")
    if not isinstance(request.path, str):
        raise TypeError("path must be a string")
    path = request.path
    decoded_path = unquote(path)
    if (
        any(ord(c) < 32 or ord(c) == 127 for c in path + decoded_path)
        or "\\" in decoded_path
        or decoded_path.startswith("//")
        or "#" in path
        or "?" in path
        # 点段会被规范化而逃出 base_url 前缀;百分号编码形式服务端同样可能解码。
        or any(segment in {".", ".."} for segment in decoded_path.split("/"))
    ):
        raise ValueError("invalid path")
    try:
        relative = httpx.URL(path)
    except httpx.InvalidURL:
        raise ValueError("invalid path") from None
    if relative.scheme or relative.host or relative.userinfo:
        raise ValueError("path must be relative to base_url")
    for pairs, is_header in ((request.headers, True), (request.query, False)):
        if not isinstance(pairs, tuple):
            raise TypeError("headers and query must be tuples")
        for pair in pairs:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise TypeError("invalid name/value pair")
            name, value = pair
            if not isinstance(name, str) or not isinstance(value, str):
                raise TypeError("names and values must be strings")
            if not name or any(c in name + value for c in "\r\n\x00"):
                raise ValueError("invalid name/value pair")
            if is_header:
                if not _TOKEN.fullmatch(name) or name.lower() in _OWNED_HEADERS:
                    raise ValueError("invalid or client-owned header")
                if any(ord(c) < 32 and c != "\t" or ord(c) >= 127 for c in value):
                    raise ValueError("invalid header value")
    try:
        base = httpx.URL(config.base_url)
        if not base.raw_path.endswith(b"/"):
            base = base.copy_with(raw_path=base.raw_path + b"/")
        url = base.copy_with(raw_path=base.raw_path + relative.raw_path.lstrip(b"/"))
        built = httpx.Request(
            request.method,
            url,
            params=request.query or None,
            headers=(*request.headers, ("Accept-Encoding", "identity")),
            content=request.body,
        )
    except (httpx.InvalidURL, UnicodeError):
        raise ValueError("invalid request encoding") from None
    if (built.url.scheme, built.url.host, built.url.port) != (
        base.scheme,
        base.host,
        base.port,
    ):
        raise ValueError("request origin differs from base_url")
    return built


def validate_request(config: HttpClientConfig, request: HttpRequest) -> None:
    build_request(config, request)
