"""有界单次 HTTP,重试和协议判断由调用者负责。"""

import asyncio
import logging
import socket
import ssl
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

import httpcore
import httpx
from anyio import wait_writable
from anyio.abc import ByteStream, SocketAttribute
from anyio.lowlevel import checkpoint

from .types import (
    ClientClosedError,
    DeliveryState,
    FailureKind,
    HttpClientConfig,
    HttpRequest,
    HttpResult,
)
from .validation import build_request

logger = logging.getLogger("app.http_client")
_http_call_active = ContextVar("http_client_call_active", default=False)
_before_wire_write: ContextVar[
    Callable[
        [],
        Awaitable[
            tuple[
                float | None,
                Callable[[], None] | None,
                Callable[[], Awaitable[None]] | None,
                Callable[[], None],
            ]
        ],
    ]
    | None
] = ContextVar("http_client_before_wire_write", default=None)
_check_wire: ContextVar[
    tuple[Callable[[], None], Callable[[], Awaitable[None]] | None, Callable[[], None]] | None
] = ContextVar("http_client_check_wire", default=None)


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


class _GuardedWriteError(Exception):
    pass


class _GuardedSocketStream(ByteStream):
    def __init__(self, stream: ByteStream):
        self.stream = stream

    @property
    def extra_attributes(self):
        return self.stream.extra_attributes

    async def receive(self, max_bytes=65536):
        return await self.stream.receive(max_bytes)

    async def send(self, item):
        checks = _check_wire.get()
        if checks is None:
            await self.stream.send(item)
            return
        check, recheck, note_write = checks
        await checkpoint()
        raw = self.stream.extra(SocketAttribute.raw_socket)
        try:
            # Keep pending bytes in this task until the socket accepts them.
            # The duplicate fd has its own readiness registration, separate from AnyIO's reader.
            with socket.socket(fileno=socket.dup(raw.fileno())) as writer:
                writer.setblocking(False)
                pending = memoryview(item)
                while pending:
                    check()
                    sent = 0
                    try:
                        sent = writer.send(pending)
                    except BlockingIOError:
                        await wait_writable(writer)
                    else:
                        if not sent:
                            raise _GuardedWriteError
                        note_write()
                        pending = pending[sent:]
                    if pending and recheck is not None:
                        check()
                        await recheck()
                        if sent:
                            # A successful probe cannot authorize the remainder after a short write.
                            check()
                            raise _GuardedWriteError
        except OSError as error:
            raise _GuardedWriteError from error

    async def send_eof(self):
        await self.stream.send_eof()

    async def aclose(self):
        await self.stream.aclose()


class _GuardedStream(httpcore.AsyncNetworkStream):
    def __init__(self, stream: httpcore.AsyncNetworkStream):
        self.stream = stream

    async def read(self, max_bytes, timeout=None):  # noqa: ASYNC109
        return await self.stream.read(max_bytes, timeout)

    async def write(self, buffer, timeout=None):  # noqa: ASYNC109
        before_write = _before_wire_write.get()
        if not buffer or before_write is None:
            await self.stream.write(buffer, timeout)
            return
        remaining, check_wire, recheck_wire, note_write = await before_write()
        if remaining is not None:
            timeout = min(timeout, remaining) if timeout is not None else remaining
        deadline = time.monotonic() + timeout if timeout is not None else None

        def check():
            if deadline is not None and time.monotonic() >= deadline:
                raise httpcore.WriteTimeout("write deadline elapsed")
            if check_wire is not None:
                try:
                    check_wire()
                except TimeoutError as error:
                    raise httpcore.WriteTimeout("write deadline elapsed") from error

        token = _check_wire.set((check, recheck_wire, note_write))
        try:
            await self.stream.write(buffer, timeout)
        finally:
            _check_wire.reset(token)

    async def aclose(self):
        await self.stream.aclose()

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):  # noqa: ASYNC109
        return _GuardedStream(await self.stream.start_tls(ssl_context, server_hostname, timeout))

    def get_extra_info(self, info):
        return self.stream.get_extra_info(info)


class _GuardedBackend(httpcore.AnyIOBackend):
    async def connect_tcp(self, *args, **kwargs):
        stream = await super().connect_tcp(*args, **kwargs)
        # Keep the guarded writer below the optional TLS stream.
        stream._stream = _GuardedSocketStream(stream._stream)
        return _GuardedStream(stream)


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
        self._wire_guarded = transport is None
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
                verify=tls_context, trust_env=False, http2=False, retries=0
            )
            # HTTPX exposes the transport; HTTP Core exposes the pool's network backend.
            transport._pool = httpcore.AsyncConnectionPool(
                ssl_context=tls_context,
                http2=False,
                retries=0,
                max_connections=config.max_connections,
                max_keepalive_connections=config.max_keepalive_connections,
                keepalive_expiry=config.keepalive_expiry_s,
                network_backend=_GuardedBackend(),
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

    async def send(self, request: HttpRequest) -> HttpResult:
        if self._closed:
            raise ClientClosedError("HttpClient is closed")
        self._bind_loop()
        started = self._clock()
        loop_started = asyncio.get_running_loop().time()
        built = build_request(self._config, request)
        headers = self._client.headers.copy()
        headers.update(built.headers)
        built.headers = headers
        remaining = self._config.total_timeout_s - (self._clock() - started)
        response = None
        status = None
        response_at = None
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
            before_write = request.before_write

            def note_write():
                nonlocal state
                if state == DeliveryState.NOT_SENT:
                    state = DeliveryState.DELIVERY_UNKNOWN
                    if request.on_wire_write is not None:
                        request.on_wire_write()

            async def check_write():
                remaining = await before_write() if before_write is not None else None
                if not wire_guarded:
                    note_write()
                return remaining, request.check_wire, request.recheck_wire, note_write

            has_write_guard = before_write is not None or request.check_wire is not None
            wire_guarded = has_write_guard and self._wire_guarded
            trace_guarded = (
                has_write_guard
                and not wire_guarded
                and type(self._transport) is httpx.AsyncHTTPTransport
            )
            if trace_guarded:

                async def trace(event, _info):
                    if event.endswith(
                        (".send_request_headers.started", ".send_request_body.started")
                    ):
                        await check_write()
                        if request.check_wire is not None:
                            request.check_wire()

                built.extensions["trace"] = trace
            write_token = _before_wire_write.set(check_write if wire_guarded else None)
            log_token = _http_call_active.set(True)
            try:
                async with asyncio.timeout_at(loop_started + self._config.total_timeout_s):
                    if not (wire_guarded or trace_guarded):
                        await check_write()
                        if request.check_wire is not None:
                            request.check_wire()
                    # 直接调用单次 transport,避免 AsyncClient 记录含查询串的完整 URL。
                    response = await self._transport.handle_async_request(built)
                    response.request = built
                    response_at = datetime.now(UTC)
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
            except _GuardedWriteError:
                # HTTP Core may treat socket write errors as a reason to wait for an early reply.
                failure = FailureKind.WRITE_ERROR
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
                    _before_wire_write.reset(write_token)
        result = HttpResult(state, failure, status, headers, body, response_at, datetime.now(UTC))
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

    async def aclose(self, *, timeout_s: float | None = None) -> None:
        """默认共享等待者不取消底层关闭；独占所有者可设期限并终止挂起的关闭。"""
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
        if timeout_s is None:
            await asyncio.shield(self._close_task)
        else:
            # 独占资源的有界关闭必须终止底层任务；默认共享等待者仍保持 shield 语义。
            async with asyncio.timeout(timeout_s):
                await self._close_task


def _observe_close_failure(task: asyncio.Task[None]) -> None:
    # 等待者取消后仍取走关闭异常,后续 aclose 会再次报告或重试。
    if not task.cancelled():
        task.exception()
