import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta

from app.diagnostics import log_exception
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
    provider=None,
) -> None:
    if claim.message.delivery_snapshot is not None:
        await send_snapshot(
            claim,
            store=store,
            targets=targets,
            adapters=adapters,
            provider=provider,
            clock=clock,
            uniform=uniform,
        )
        return
    result = verdict = None
    target = targets.get(claim.message.target)
    phase = "prepare"
    try:
        if target is None:
            raise ValueError("missing target")
        adapter = adapters[target.protocol]
        client = clients[target.name]
        phase = "build_request"
        request = adapter.build_request(claim.message, target)
        if request.body != claim.message.request_raw:
            raise ValueError("adapter changed frozen body")
        validate_request(target.http_config(), request)
        phase = "apply_outcome"
        now = utc_datetime(clock())
        if claim.message.expires_at is not None and claim.message.expires_at <= now:
            outcome = AttemptOutcome(MessageStatus.EXPIRED)
        else:
            phase = "send"
            result = await client.send(request)
            if not isinstance(result, HttpResult):
                raise ValueError("invalid HTTP result")
            if result.delivery_state == DeliveryState.HTTP_RESPONSE_COMPLETE:
                phase = "classify_response"
                if not isinstance(result.body, bytes) or len(result.body) > target.max_body_bytes:
                    raise ValueError("invalid complete response")
                verdict = adapter.classify_response(claim.message, result)
                if not isinstance(verdict, Verdict):
                    raise ValueError("invalid adapter verdict")
                if verdict.kind == VerdictKind.DELIVERED and verdict.response != result.body:
                    raise ValueError("adapter changed response bytes")
            phase = "apply_outcome"
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
    except Exception as error:
        log_exception(
            logger,
            "reliable delivery failed",
            error,
            component="reliable",
            phase=phase,
            context={"message_id": claim.message_id, "target": claim.message.target},
        )
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


async def send_snapshot(claim, *, store, targets, adapters, provider, clock, uniform):
    from .client_provider import PoolUnavailable

    result = verdict = None
    gated = False
    pre_reason = "CONFIG_DRIFT"
    phase = "prepare"
    try:
        target = targets[claim.message.target]
        effective = target.effective(claim.message.delivery_snapshot)
        pre_reason = "ASSEMBLY_UNAVAILABLE"
        adapter = adapters[claim.message.delivery_snapshot.protocol]
        if provider is None:
            raise ValueError("dynamic client provider unavailable")
        pre_reason = "PRE_GATE_VALIDATION"
        phase = "build_request"
        request = adapter.build_request(claim.message, effective)
        if request.body != claim.message.request_raw:
            raise ValueError("adapter changed frozen bytes")
        validate_request(effective.http_config(), request)
        phase = "acquire_client"
        async with provider.borrow(effective.http_config()) as client:
            phase = "before_send"
            until = await store.before_send(claim, target)
            if not until:
                phase = "acquire_client"
                return
            gated = True
            now = utc_datetime(clock())
            if until <= now or (
                claim.message.expires_at is not None and claim.message.expires_at <= now
            ):
                result = HttpResult(DeliveryState.NOT_SENT, None, None, (), None)
            else:
                phase = "send"
                result = await client.send(request)
            if not isinstance(result, HttpResult):
                raise ValueError("invalid HTTP result")
            if result.delivery_state == DeliveryState.HTTP_RESPONSE_COMPLETE:
                phase = "classify_response"
                if not isinstance(result.body, bytes) or len(result.body) > target.max_body_bytes:
                    raise ValueError("invalid complete response")
                verdict = adapter.classify_response(claim.message, result)
                if not isinstance(verdict, Verdict):
                    raise ValueError("invalid verdict")
                if verdict.kind == VerdictKind.DELIVERED and verdict.response != result.body:
                    raise ValueError("adapter changed response")
            phase = "acquire_client"
        phase = "apply_outcome"
        definite = result.delivery_state == DeliveryState.NOT_SENT or (
            verdict and verdict.kind == VerdictKind.NOT_RECEIVED
        )
        if verdict and verdict.kind == VerdictKind.DELIVERED:
            outcome = AttemptOutcome(MessageStatus.DONE, result=result, verdict=verdict)
        elif verdict and verdict.kind == VerdictKind.PEER_TERMINAL:
            outcome = AttemptOutcome(MessageStatus.FAILED, verdict.reason, result, verdict)
        elif verdict and verdict.kind == VerdictKind.HOLD:
            outcome = AttemptOutcome(
                MessageStatus.HELD,
                result=result,
                verdict=verdict,
                pause_reason=verdict.pause_reason,
                delivery_uncertain=True
                if verdict.pause_reason != "CONTRACT_NON_COMPLIANT"
                else None,
            )
        elif definite:
            hint = verdict.retry_after_ms if verdict else None
            now = utc_datetime(clock())
            if hint is not None and hint / 1000 > target.max_retry_after_s:
                outcome = AttemptOutcome(
                    MessageStatus.HELD,
                    result=result,
                    verdict=verdict,
                    pause_reason="CONTRACT_NON_COMPLIANT",
                )
            elif claim.message.expires_at is not None and claim.message.expires_at <= now:
                outcome = AttemptOutcome(MessageStatus.EXPIRED, result=result, verdict=verdict)
            else:
                delay = retry_delay_s(claim.attempt_no, target, None, uniform)
                delay = max(delay, (hint or 0) / 1000)
                outcome = AttemptOutcome(
                    MessageStatus.PENDING,
                    result=result,
                    verdict=verdict,
                    next_attempt_at=now + timedelta(seconds=delay),
                    not_received_count=claim.not_received_count + 1,
                )
        else:
            outcome = AttemptOutcome(
                MessageStatus.HELD,
                result=result,
                verdict=verdict,
                pause_reason="DELIVERY_UNKNOWN",
                delivery_uncertain=True,
            )
    except PoolUnavailable:
        outcome = AttemptOutcome(
            MessageStatus.PENDING, next_attempt_at=utc_datetime(clock()) + timedelta(seconds=1)
        )
    except Exception as error:
        log_exception(
            logger,
            "reliable delivery failed",
            error,
            component="reliable",
            phase=phase,
            context={"message_id": claim.message_id, "target": claim.message.target},
        )
        outcome = AttemptOutcome(
            MessageStatus.HELD,
            result=result if isinstance(result, HttpResult) else None,
            pause_reason="DELIVERY_UNKNOWN" if gated else pre_reason,
            delivery_uncertain=True if gated else None,
        )
    await store.finish(claim, outcome)
