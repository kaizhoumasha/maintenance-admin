import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from time import monotonic

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identifiers import uuid7
from app.diagnostics import log_exception
from app.integrations import history
from app.models import IntegrationExchange, IntegrationRecord

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
    body_available: bool = True,
    response_scope: str = "HTTP",
) -> None:
    try:
        async with asyncio.timeout(SNAPSHOT_TIMEOUT_S), factory() as session, session.begin():
            attempt_id = response.attempt_id or str(uuid7())
            root = (
                await session.get(IntegrationExchange, response.exchange_id)
                if (response.exchange_id is not None)
                else None
            )
            if root is None:
                peer = (
                    "ECS"
                    if source in {"ecs", "ecs_debug", "ecs_result"}
                    else ("WMS" if source in {None, "wms"} else None)
                )
                root = IntegrationExchange(
                    role="DIAGNOSTIC",
                    peer_system=peer,
                    protocol="ecs-result"
                    if source == "ecs_result"
                    else ("ecs-event" if peer == "ECS" else "wms-callback"),
                    direction="INBOUND",
                    interaction_kind="CALLBACK",
                    identity_kind="DIAGNOSTIC",
                    identity_value=attempt_id,
                    created_at=received_at,
                    coverage="PARTIAL",
                )
                session.add(root)
                await session.flush()
            request_record = await session.scalar(
                select(IntegrationRecord.id)
                .where(
                    IntegrationRecord.exchange_id == root.id,
                    IntegrationRecord.evidence_class == "CORE",
                    IntegrationRecord.record_kind.in_(("CALLBACK_RECEIVED", "RESULT_EVIDENCE")),
                    IntegrationRecord.attempt_id == attempt_id,
                )
                .limit(1)
            )
            request_reference = None
            if request_record is not None and body_available:
                bodies = await history.read_bodies(session, record_ids=(request_record,))
                known = bodies.get(request_record)
                if (
                    known is not None
                    and known.raw_body is not None
                    and (
                        known.raw_body == preview
                        or (truncated and known.raw_body.startswith(preview))
                    )
                ):
                    request_reference = await history.matching_body_reference(
                        session,
                        record_id=request_record,
                        raw_body=known.raw_body,
                    )
            details = dict(
                source=source,
                request_id=request_id,
                truncated=truncated,
                outcome=response.outcome,
                diagnostic=True,
                response_scope=response_scope,
            )
            if not body_available:
                details["coverage_reason"] = "BODY_READ_FAILED"
            incoming_id = await history.write_record(
                session,
                draft=history.RecordDraft(
                    exchange_id=root.id,
                    record_kind="CALLBACK_RECEIVED",
                    evidence_class="DIAGNOSTIC",
                    event_key=f"DIAG_RECEIVE:{attempt_id}",
                    attempt_id=attempt_id,
                    direction="INBOUND",
                    occurred_at=received_at,
                    details=details,
                    body_record_id=request_reference,
                    raw_body=preview if body_available and request_reference is None else None,
                    body_state="UNAVAILABLE"
                    if not body_available
                    else (
                        None
                        if request_reference is not None
                        else "PREVIEW"
                        if truncated
                        else "FULL"
                    ),
                ),
            )
            reply_candidate = await session.scalar(
                select(IntegrationRecord.id)
                .where(
                    IntegrationRecord.exchange_id == root.id,
                    IntegrationRecord.evidence_class == "CORE",
                    IntegrationRecord.record_kind == "RESPONSE_PREPARED",
                    IntegrationRecord.attempt_id == attempt_id,
                )
                .limit(1)
            )
            if reply_candidate is None:
                reply_candidate = root.current_response_record_id
            if root.role == "ECS_COMMAND" and getattr(response, "digest", None):
                reply_candidate = await session.scalar(
                    select(IntegrationRecord.response_record_id).where(
                        IntegrationRecord.exchange_id == root.id,
                        IntegrationRecord.record_kind == "RESULT_EVIDENCE",
                        IntegrationRecord.semantic_digest == response.digest,
                    )
                )
            reply_reference = await history.matching_body_reference(
                session,
                record_id=reply_candidate,
                raw_body=response.prepared_body,
            )
            await history.write_record(
                session,
                draft=history.RecordDraft(
                    exchange_id=root.id,
                    record_kind="RESPONSE_PREPARED",
                    evidence_class="DIAGNOSTIC",
                    event_key=f"DIAG_REPLY:{attempt_id}",
                    attempt_id=attempt_id,
                    direction="OUTBOUND",
                    occurred_at=received_at,
                    parent_record_id=incoming_id,
                    http_status=response.status_code if response_scope == "HTTP" else None,
                    protocol_code=str(response.status_code) if response_scope != "HTTP" else None,
                    details=details,
                    body_record_id=reply_reference,
                    raw_body=response.prepared_body if reply_reference is None else None,
                ),
            )
    except Exception as error:
        log_exception(
            logger,
            "callback snapshot failed",
            error,
            component="callback",
            phase="snapshot",
            level=logging.WARNING,
            context={"source": source, "request_id": request_id},
        )
