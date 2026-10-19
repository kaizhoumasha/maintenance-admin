"""单次 HTTP 的公开合同,不包含协议或业务语义。"""

import math
import re
import ssl
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import httpx


class DeliveryState(StrEnum):
    NOT_SENT = "NOT_SENT"
    DELIVERY_UNKNOWN = "DELIVERY_UNKNOWN"
    HTTP_RESPONSE_COMPLETE = "HTTP_RESPONSE_COMPLETE"


class FailureKind(StrEnum):
    POOL_TIMEOUT = "POOL_TIMEOUT"
    CONNECT_TIMEOUT = "CONNECT_TIMEOUT"
    CONNECT_ERROR = "CONNECT_ERROR"
    WRITE_TIMEOUT = "WRITE_TIMEOUT"
    WRITE_ERROR = "WRITE_ERROR"
    READ_TIMEOUT = "READ_TIMEOUT"
    READ_ERROR = "READ_ERROR"
    REMOTE_PROTOCOL_ERROR = "REMOTE_PROTOCOL_ERROR"
    TOTAL_TIMEOUT = "TOTAL_TIMEOUT"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    CONTENT_ENCODING_UNSUPPORTED = "CONTENT_ENCODING_UNSUPPORTED"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"


class ClientClosedError(RuntimeError):
    pass


@dataclass(frozen=True)
class HttpRequest:
    method: str
    path: str
    query: tuple[tuple[str, str], ...] = ()
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""


@dataclass(frozen=True)
class HttpResult:
    delivery_state: DeliveryState
    failure_kind: FailureKind | None
    status_code: int | None
    headers: tuple[tuple[str, str], ...]
    body: bytes | None


@dataclass(frozen=True)
class HttpClientConfig:
    base_url: str
    destination_key: str
    connect_timeout_s: float = 5.0
    read_timeout_s: float = 5.0
    write_timeout_s: float = 5.0
    pool_timeout_s: float = 5.0
    total_timeout_s: float = 10.0
    max_connections: int = 100
    max_keepalive_connections: int = 20
    keepalive_expiry_s: float = 5.0
    max_request_body_bytes: int = 262144
    max_response_body_bytes: int = 262144
    tls_context: ssl.SSLContext | None = None

    def __post_init__(self):
        for name in (
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
        ):
            value = getattr(self, name)
            integer = name.startswith("max_")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be a number")
            if integer and not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value <= 0 or not math.isfinite(value):
                raise ValueError(f"{name} must be positive and finite")
        if self.max_keepalive_connections > self.max_connections:
            raise ValueError("keepalive connections exceed pool size")
        if not isinstance(self.base_url, str):
            raise TypeError("base_url must be a string")
        try:
            url = httpx.URL(self.base_url)
        except httpx.InvalidURL:
            raise ValueError("invalid base_url") from None
        if (
            url.scheme not in {"http", "https"}
            or not url.host
            or url.userinfo
            or url.query
            or url.fragment
        ):
            raise ValueError("invalid base_url")
        if not isinstance(self.destination_key, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9_.-]{0,63}", self.destination_key
        ):
            raise ValueError("invalid destination_key")
        if self.tls_context is not None:
            if not isinstance(self.tls_context, ssl.SSLContext):
                raise TypeError("tls_context must be an SSLContext")
            if (
                not self.tls_context.check_hostname
                or self.tls_context.verify_mode != ssl.CERT_REQUIRED
            ):
                raise ValueError("tls_context must verify certificates and hostnames")


class HttpClient(Protocol):
    async def send(self, request: HttpRequest) -> HttpResult: ...

    async def aclose(self) -> None: ...
