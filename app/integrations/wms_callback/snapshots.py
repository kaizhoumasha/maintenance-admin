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
_windows: dict[str, list] = {}  # 来源 -> [窗口起点, 已写入, 已丢弃]


def allow_snapshot(source: str = "wms") -> bool:
    now = monotonic()
    window = _windows.setdefault(source, [0.0, 0, 0])
    if now - window[0] >= 60:
        if window[2]:
            logger.warning("callback snapshots dropped source=%s count=%s", source, window[2])
        window[:] = [now, 0, 0]
    if window[1] >= SNAPSHOTS_PER_MINUTE:
        window[2] += 1
        if window[2] == 1:
            logger.warning("callback snapshot rate limit reached source=%s", source)
        return False
    window[1] += 1
    return True


async def record_snapshot(
    factory: Callable[[], AsyncSession],
    *,
    preview: bytes,
    truncated: bool,
    response: CallbackResponse,
    request_id: str,
    received_at: datetime,
    source: str | None = None,
) -> None:
    try:
        async with asyncio.timeout(SNAPSHOT_TIMEOUT_S), factory() as session, session.begin():
            session.add(
                CallbackSnapshot(
                    source=source,
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
