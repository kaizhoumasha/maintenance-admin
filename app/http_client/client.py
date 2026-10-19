"""有界单次 HTTP,重试和协议判断由调用者负责。"""

import asyncio
import logging
import re
import ssl
import time
from collections.abc import Callable
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import unquote

import httpx

from .types import (
    ClientClosedError,
    DeliveryState,
    FailureKind,
    HttpClientConfig,
    HttpRequest,
    HttpResult,
)

logger = logging.getLogger("app.http_client")
_http_call_active = ContextVar("http_client_call_active", default=False)


class _HttpCallLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _http_call_active.get()


# httpcore DEBUG 会输出响应凭据;只过滤本调用上下文,不更改其他调用的日志级别。
for _name in (
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
):
    logging.getLogger(_name).addFilter(_HttpCallLogFilter())

_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_OWNED_HEADERS = {"host", "content-length", "transfer-encoding", "accept-encoding"}
_ERRORS = {
    httpx.PoolTimeout: FailureKind.POOL_TIMEOUT,
    httpx.ConnectTimeout: FailureKind.CONNECT_TIMEOUT,
    httpx.ConnectError: FailureKind.CONNECT_ERROR,
    httpx.WriteTimeout: FailureKind.WRITE_TIMEOUT,
    httpx.WriteError: FailureKind.WRITE_ERROR,
    httpx.ReadTimeout: FailureKind.READ_TIMEOUT,
    httpx.ReadError: FailureKind.READ_ERROR,
    httpx.RemoteProtocolError: FailureKind.REMOTE_PROTOCOL_ERROR,
}
_NOT_SENT = {FailureKind.POOL_TIMEOUT, FailureKind.CONNECT_TIMEOUT, FailureKind.CONNECT_ERROR}


class AsyncHttpClient:
    def __init__(
        self,
        config: HttpClientConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._config = config
        self._clock = clock
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        if transport is None:
            tls_context = config.tls_context
            if tls_context is None:
                # HTTP 不加载 CA;HTTPS 缺少信任根时仍保持证书校验,不回退环境配置。
                tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                if httpx.URL(config.base_url).scheme == "https":
                    paths = ssl.get_default_verify_paths()
                    cafile = paths.openssl_cafile
                    capath = paths.openssl_capath
                    cafile = cafile if cafile and Path(cafile).is_file() else None
                    capath = capath if capath and Path(capath).is_dir() else None
                    if cafile is not None or capath is not None:
                        tls_context = ssl.create_default_context(cafile=cafile, capath=capath)
            transport = httpx.AsyncHTTPTransport(
                verify=tls_context,
                trust_env=False,
                http2=False,
                retries=0,
                limits=httpx.Limits(
                    max_connections=config.max_connections,
                    max_keepalive_connections=config.max_keepalive_connections,
                    keepalive_expiry=config.keepalive_expiry_s,
                ),
            )
        self._transport = transport
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            http2=False,
        )

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("HttpClient cannot be shared across event loops")
        self._loop = loop

    def _build_request(self, request: HttpRequest) -> httpx.Request:
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
        if len(request.body) > self._config.max_request_body_bytes:
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
            built = self._client.build_request(
                request.method,
                path,
                params=request.query or None,
                headers=(*request.headers, ("Accept-Encoding", "identity")),
                content=request.body,
            )
        except (httpx.InvalidURL, UnicodeError):
            raise ValueError("invalid request encoding") from None
        base = self._client.base_url
        if (built.url.scheme, built.url.host, built.url.port) != (
            base.scheme,
            base.host,
            base.port,
        ):
            raise ValueError("request origin differs from base_url")
        return built

    async def send(self, request: HttpRequest) -> HttpResult:
        if self._closed:
            raise ClientClosedError("HttpClient is closed")
        self._bind_loop()
        started = self._clock()
        loop_started = asyncio.get_running_loop().time()
        built = self._build_request(request)
        remaining = self._config.total_timeout_s - (self._clock() - started)
        response = None
        status = None
        headers = ()
        body = None
        failure = None
        state = DeliveryState.NOT_SENT
        if remaining <= 0:
            failure = FailureKind.TOTAL_TIMEOUT
        else:
            built.extensions["timeout"] = {
                phase: min(getattr(self._config, f"{phase}_timeout_s"), remaining)
                for phase in ("connect", "read", "write", "pool")
            }
            log_token = _http_call_active.set(True)
            try:
                async with asyncio.timeout_at(loop_started + self._config.total_timeout_s):
                    state = DeliveryState.DELIVERY_UNKNOWN
                    # 直接调用单次 transport,避免 AsyncClient 记录含查询串的完整 URL。
                    response = await self._transport.handle_async_request(built)
                    response.request = built
                    status = response.status_code
                    headers = tuple(response.headers.multi_items())
                    encodings = response.headers.get_list("content-encoding")
                    if any(v.strip().lower() != "identity" for v in encodings):
                        failure = FailureKind.CONTENT_ENCODING_UNSUPPORTED
                    elif response.is_stream_consumed:
                        # 注入传输可能预载正文;只在已确认 identity 后使用缓存。
                        cached = response.content
                        if len(cached) > self._config.max_response_body_bytes:
                            failure = FailureKind.RESPONSE_TOO_LARGE
                        else:
                            body = cached
                            state = DeliveryState.HTTP_RESPONSE_COMPLETE
                    else:
                        content = bytearray()
                        # 直接读原始流,避免 aiter_raw 在 EOF 自动关闭并覆盖完整结果。
                        async for chunk in response.stream:
                            if len(content) + len(chunk) > self._config.max_response_body_bytes:
                                failure = FailureKind.RESPONSE_TOO_LARGE
                                break
                            content.extend(chunk)
                        if failure is None:
                            body = bytes(content)
                            state = DeliveryState.HTTP_RESPONSE_COMPLETE
            except TimeoutError:
                failure = FailureKind.TOTAL_TIMEOUT
            except (httpx.TransportError, httpx.StreamError) as exc:
                failure = next(
                    (kind for cls, kind in _ERRORS.items() if isinstance(exc, cls)),
                    FailureKind.TRANSPORT_ERROR,
                )
                if failure in _NOT_SENT:
                    state = DeliveryState.NOT_SENT
            finally:
                try:
                    if response is not None:
                        try:
                            # 已过总期限仍尝试清理,但宽限最多 1s,不无限延长调用。
                            async with asyncio.timeout(min(1.0, self._config.total_timeout_s)):
                                await response.aclose()
                        except Exception:
                            logger.warning(
                                "http_client response cleanup failed",
                                extra={"destination_key": self._config.destination_key},
                            )
                finally:
                    _http_call_active.reset(log_token)
        result = HttpResult(state, failure, status, headers, body)
        logger.log(
            logging.DEBUG if failure is None else logging.WARNING,
            "http_client request",
            extra={
                "method": request.method,
                "destination_key": self._config.destination_key,
                "elapsed_ms": (self._clock() - started) * 1000,
                "status_code": status,
                "delivery_state": state.value,
                "failure_kind": failure.value if failure else None,
            },
        )
        return result

    async def aclose(self) -> None:
        self._bind_loop()
        self._closed = True
        if self._close_task is None or (
            self._close_task.done()
            and (self._close_task.cancelled() or self._close_task.exception() is not None)
        ):
            # AsyncClient 在关闭失败前已标为 CLOSED;重试须直接释放其 transport。
            close = self._transport.aclose if self._client.is_closed else self._client.aclose
            self._close_task = asyncio.create_task(close())
            self._close_task.add_done_callback(_observe_close_failure)
        await asyncio.shield(self._close_task)


def _observe_close_failure(task: asyncio.Task[None]) -> None:
    # 等待者取消后仍取走关闭异常,后续 aclose 会再次报告或重试。
    if not task.cancelled():
        task.exception()
