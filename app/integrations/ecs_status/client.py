"""Single-request Status protocol; persistence and scheduling belong to callers."""

import asyncio
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from ipaddress import ip_address

import httpx

from app.http_client import (
    AsyncHttpClient,
    DeliveryState,
    HttpClient,
    HttpClientConfig,
    HttpRequest,
)
from app.strict_json import canonical_digest, json_object

from .contracts import EcsStatusResponse, StatusQueryError, StatusQueryIn, StatusQueryResult

logger = logging.getLogger(__name__)
STATUS_PATH = "/api/v1/device/status"


def utc_now() -> datetime:
    return datetime.now(UTC)


def normalize_status_origin(value: str) -> str:
    try:
        if not isinstance(value, str) or any(ord(c) <= 32 or ord(c) == 127 for c in value):
            raise ValueError
        url = httpx.URL(value)
        if (
            url.raw_path not in (b"", b"/")
            or url.query
            or url.fragment
            or url.userinfo
            or url.port is not None
            and not 1 <= url.port <= 65535
        ):
            raise ValueError
        address = ip_address(url.host)
        if address.is_unspecified or address.is_multicast or getattr(address, "scope_id", None):
            raise ValueError
        origin = str(url.copy_with(host=address.compressed, path="")).rstrip("/")
        HttpClientConfig(origin, "ecs-status")
        return origin
    except (ValueError, httpx.InvalidURL):
        raise ValueError(
            "ECS Status requires an HTTP(S) IP origin without credentials or path"
        ) from None


def _retry_after(headers: tuple[tuple[str, str], ...], now: datetime) -> datetime | None:
    values = [value.strip() for key, value in headers if key.lower() == "retry-after"]
    if len(values) != 1:
        return None
    try:
        value = values[0]
        if re.fullmatch(r"[0-9]+", value):
            return now + timedelta(seconds=int(value))
        target = parsedate_to_datetime(value)
        if target.tzinfo is not None and target > now:
            return target.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        pass
    return None


async def query(
    ecs_origin: str,
    *,
    device_code: str | None = None,
    client_factory: Callable[[HttpClientConfig], HttpClient] = AsyncHttpClient,
    clock: Callable[[], datetime] = utc_now,
) -> StatusQueryResult:
    """Query one explicit ECS once; return provenance without registering or storing devices."""
    origin = normalize_status_origin(ecs_origin)
    inputs = StatusQueryIn(ecs_origin=origin, device_code=device_code)
    config = HttpClientConfig(origin, "ecs-status", total_timeout_s=5)
    client = client_factory(config)
    try:
        result = await client.send(
            HttpRequest(
                "GET",
                STATUS_PATH,
                query=() if inputs.device_code is None else (("device_code", inputs.device_code),),
            )
        )
        completed = clock()

        def error(kind: str, retry_at: datetime | None = None) -> StatusQueryError:
            return StatusQueryError(
                ecs_origin=origin,
                requested_device_code=device_code,
                completed_at=completed,
                error_kind=kind,
                http_status=result.status_code,
                retry_after_at=retry_at,
            )

        if result.delivery_state != DeliveryState.HTTP_RESPONSE_COMPLETE:
            raise error(result.failure_kind.value if result.failure_kind else "TRANSPORT_ERROR")
        status = result.status_code
        if status == 429:
            retry_at = _retry_after(result.headers, completed)
            raise error("RATE_LIMITED" if retry_at is not None else "INVALID_RETRY_AFTER", retry_at)
        if status in {401, 403}:
            raise error("ACCESS_DENIED")
        if status is not None and 300 <= status < 400:
            raise error("REDIRECT_REJECTED")
        if status != 200:
            raise error("NOT_FOUND" if status == 404 and device_code is not None else "HTTP_ERROR")
        content_types = [
            value.split(";", 1)[0].strip().lower()
            for key, value in result.headers
            if key.lower() == "content-type"
        ]
        try:
            if content_types != ["application/json"] or result.body is None:
                raise ValueError
            document = json_object(result.body)
            if document is None:
                raise ValueError
            canonical_digest(document)
            parsed = EcsStatusResponse.model_validate_json(result.body)
            if device_code is not None and parsed.devices:
                if len(parsed.devices) != 1 or parsed.devices[0].device.device_code != device_code:
                    raise ValueError
        except (ValueError, RecursionError):
            raise error("INVALID_RESPONSE") from None
        return StatusQueryResult(
            ecs_origin=origin,
            requested_device_code=device_code,
            fetched_at=completed,
            devices=parsed.devices,
        )
    finally:
        try:
            if isinstance(client, AsyncHttpClient):
                await client.aclose(timeout_s=1)
            else:
                async with asyncio.timeout(1):
                    await client.aclose()
        except Exception:
            logger.warning("ECS Status client cleanup failed")
