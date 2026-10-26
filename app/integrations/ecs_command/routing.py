from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Protocol

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.http_client import HttpClientConfig


def normalize_origin(value: str) -> str:
    url = httpx.URL(value)
    if url.raw_path not in (b"", b"/") or url.query or url.fragment or url.userinfo:
        raise ValueError("ECS route requires an origin without path, credentials or query")
    origin = str(url.copy_with(path="")).rstrip("/")
    HttpClientConfig(origin, "ecs")
    return origin


def retry_window_error():
    return AppError(
        ErrorCode.DATABASE_UNAVAILABLE,
        "ECS retry_window must be exactly 300 seconds; "
        "route providers must use PROTOCOL_RETRY_WINDOW",
    )


class RouteAuthorization(StrEnum):
    ALLOW = "ALLOW"
    REVOKED = "REVOKED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class RouteSnapshot:
    device_code: str
    origin: str
    route_id: str
    version: str
    annex_id: str
    annex_version: str
    commands: tuple[str, ...]
    retry_window: timedelta

    def __post_init__(self):
        object.__setattr__(self, "origin", normalize_origin(self.origin))
        if not isinstance(self.retry_window, timedelta) or self.retry_window <= timedelta(0):
            raise retry_window_error()


class DeviceRouteProvider(Protocol):
    async def resolve(
        self, session: AsyncSession, device_code: str, task_type: str
    ) -> RouteSnapshot: ...
    async def authorize_frozen(self, session: AsyncSession, route: dict) -> RouteAuthorization: ...


class UnconfiguredRoutes:
    async def resolve(
        self, session: AsyncSession, device_code: str, task_type: str
    ) -> RouteSnapshot:
        raise ValueError("ECS route and annex must be explicitly configured")

    async def authorize_frozen(self, session: AsyncSession, route: dict) -> RouteAuthorization:
        return RouteAuthorization.UNKNOWN
