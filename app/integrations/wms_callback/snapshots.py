import asyncio
import base64
import logging
from collections.abc import Callable
from datetime import datetime
from time import monotonic

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CallbackSnapshot

from .receiver import CallbackResponse

logger = logging.getLogger(__name__)
SNAPSHOTS_PER_MINUTE = 120
SNAPSHOT_BODY_BYTES = 64 * 1024
SNAPSHOT_PREVIEW_BYTES = SNAPSHOT_BODY_BYTES // 4 * 3  # base64 后恰好不超过 64 KiB
SNAPSHOT_TIMEOUT_S = 2
_window_start = 0.0
_written = 0
_dropped = 0


def allow_snapshot() -> bool:
    global _window_start, _written, _dropped
    now = monotonic()
    if now - _window_start >= 60:
        if _dropped:
            logger.warning("callback snapshots dropped count=%s", _dropped)
        _window_start, _written, _dropped = now, 0, 0
    if _written >= SNAPSHOTS_PER_MINUTE:
        _dropped += 1
        if _dropped == 1:
            logger.warning("callback snapshot rate limit reached")
        return False
    _written += 1
    return True


async def record_snapshot(
    factory: Callable[[], AsyncSession],
    *,
    preview: bytes,
    truncated: bool,
    response: CallbackResponse,
    request_id: str,
    received_at: datetime,
) -> None:
    try:
        async with asyncio.timeout(SNAPSHOT_TIMEOUT_S), factory() as session, session.begin():
            session.add(
                CallbackSnapshot(
                    request_id=request_id,
                    received_at=received_at,
                    body_b64=base64.b64encode(preview).decode("ascii"),
                    truncated=truncated,
                    http_status=response.status_code,
                    response_body=response.body,
                    outcome=response.outcome,
                )
            )
    except Exception as error:
        logger.warning("callback snapshot failed error_type=%s", type(error).__name__)
