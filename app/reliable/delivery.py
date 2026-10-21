import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta

from app.http_client import DeliveryState, HttpClient, HttpResult, validate_request

from .config import TargetConfig
from .policy import retry_delay_s
from .store import AttemptOutcome, Claim, MessageStore, utc_datetime
from .types import FailReason, MessageStatus, ProtocolAdapter, Verdict, VerdictKind

logger = logging.getLogger(__name__)


async def send_one(
    claim: Claim,
    *,
    store: MessageStore,
    targets: Mapping[str, TargetConfig],
    adapters: Mapping[str, ProtocolAdapter],
    clients: Mapping[str, HttpClient],
    clock: Callable[[], datetime],
    uniform: Callable[[float, float], float],
) -> None:
    result = verdict = None
    target = targets.get(claim.message.target)
    try:
        if target is None:
            raise ValueError("missing target")
        adapter = adapters[target.protocol]
        client = clients[target.name]
        request = adapter.build_request(claim.message, target)
        if request.body != claim.message.request_raw:
            raise ValueError("adapter changed frozen body")
        validate_request(target.http_config(), request)
        now = utc_datetime(clock())
        if claim.message.expires_at is not None and claim.message.expires_at <= now:
            outcome = AttemptOutcome(MessageStatus.EXPIRED)
        else:
            result = await client.send(request)
            if not isinstance(result, HttpResult):
                raise ValueError("invalid HTTP result")
            if result.delivery_state == DeliveryState.HTTP_RESPONSE_COMPLETE:
                if not isinstance(result.body, bytes) or len(result.body) > target.max_body_bytes:
                    raise ValueError("invalid complete response")
                verdict = adapter.classify_response(claim.message, result)
                if not isinstance(verdict, Verdict):
                    raise ValueError("invalid adapter verdict")
                if verdict.kind == VerdictKind.DELIVERED and verdict.response != result.body:
                    raise ValueError("adapter changed response bytes")
            if verdict is not None and verdict.kind == VerdictKind.DELIVERED:
                outcome = AttemptOutcome(MessageStatus.DONE, result=result, verdict=verdict)
            elif verdict is not None and verdict.kind == VerdictKind.PEER_TERMINAL:
                outcome = AttemptOutcome(MessageStatus.FAILED, verdict.reason, result, verdict)
            else:
                # 确认未送达只有两种:请求未发出,或对端明确未接纳;其余(含未知)都不计数。
                definite = result.delivery_state == DeliveryState.NOT_SENT or (
                    verdict is not None and verdict.kind == VerdictKind.NOT_RECEIVED
                )
                uncertain = claim.delivery_uncertain or not definite
                count = claim.not_received_count + definite
                budget = target.max_not_received_attempts
                if budget is not None and definite and not uncertain and count >= budget:
                    outcome = AttemptOutcome(
                        MessageStatus.STALLED,
                        result=result,
                        verdict=verdict,
                        not_received_count=count,
                        delivery_uncertain=False,
                    )
                else:
                    hint = verdict.retry_after_ms if verdict else None
                    delay = retry_delay_s(claim.attempt_no, target, hint, uniform)
                    outcome = AttemptOutcome(
                        MessageStatus.PENDING,
                        result=result,
                        verdict=verdict,
                        next_attempt_at=utc_datetime(clock()) + timedelta(seconds=delay),
                        not_received_count=count,
                        delivery_uncertain=uncertain,
                    )
    except Exception:
        outcome = AttemptOutcome(
            MessageStatus.FAILED,
            FailReason.LOCAL_ERROR,
            result=result if isinstance(result, HttpResult) else None,
        )
    changed = await store.finish(claim, outcome)
    if not changed:
        return
    context = dict(
        target=claim.message.target,
        operation=claim.message.operation,
        operation_id=claim.message.operation_id,
        attempts=claim.attempt_no,
        message_trace_id=claim.message.trace_id,
    )
    if outcome.fail_reason == FailReason.CONTRACT_VIOLATION:
        logger.error("reliable peer contract violation %r", context, extra=context)
    if outcome.status == MessageStatus.STALLED:
        context["not_received_attempts"] = outcome.not_received_count
        logger.warning("reliable stalled %r", context, extra=context)
    if outcome.status == MessageStatus.PENDING:
        if (
            verdict
            and verdict.retry_after_ms is not None
            and verdict.retry_after_ms / 1000 > target.max_retry_after_s
        ):
            logger.warning("reliable retry hint capped %r", context, extra=context)
        if claim.attempt_no >= target.alert_after_attempts:
            logger.warning("reliable retry threshold %r", context, extra=context)
