import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta

from app.diagnostics import log_exception
from app.http_client import DeliveryState, FailureKind, HttpClient, HttpResult, validate_request

from .config import TargetConfig
from .lifecycle import EvidencePersistenceError, PreDispatchTimeout, PreDispatchUnavailable
from .policy import retry_delay_s
from .store import AttemptOutcome, Claim, MessageStore, utc_datetime
from .types import (
    RETRY_ACK_TIMEOUT,
    FailReason,
    MessageStatus,
    ProtocolAdapter,
    Verdict,
    VerdictKind,
)

logger = logging.getLogger(__name__)


def _guarded_request(request, before_write, check_wire, deadline, clock, note_write=None):
    if before_write is None:
        return request

    async def authorize_write():
        if deadline <= utc_datetime(clock()):
            raise TimeoutError("HTTP write deadline elapsed")
        await before_write()
        remaining = (deadline - utc_datetime(clock())).total_seconds()
        if remaining <= 0:
            raise TimeoutError("HTTP write deadline elapsed")
        return remaining

    def verify_write():
        if check_wire is not None:
            check_wire()
        if deadline <= utc_datetime(clock()):
            raise TimeoutError("HTTP write deadline elapsed")

    return replace(
        request,
        before_write=authorize_write,
        check_wire=verify_write,
        recheck_wire=before_write,
        on_wire_write=note_write,
    )


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
    observe_delivered: Callable[[Claim, AttemptOutcome], Awaitable[None]] | None = None,
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
            phase = "before_send"
            until = await store.before_send(replace(claim, request=request), target)
            if until is None:
                return
            phase = "send_guard"
            hold_reason = None
            before_write = None
            deadline = min(until, claim.message.expires_at or until)
            try:
                async with store.snapshots.http_guard(claim, target.protocol) as guard:
                    hold_reason, before_write, check_wire, note_write = guard
                    if hold_reason or deadline <= utc_datetime(store.clock()):
                        result = HttpResult(DeliveryState.NOT_SENT, None, None, (), None)
                    else:
                        phase = "send"
                        result = await client.send(
                            _guarded_request(
                                request, before_write, check_wire, deadline, store.clock, note_write
                            )
                        )
            except PreDispatchUnavailable:
                result = HttpResult(DeliveryState.NOT_SENT, None, None, (), None)
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
            if hold_reason:
                outcome = AttemptOutcome(
                    MessageStatus.STALLED, result=result, pause_reason=hold_reason
                )
            elif (
                before_write is not None and result.delivery_state == DeliveryState.DELIVERY_UNKNOWN
            ):
                outcome = AttemptOutcome(
                    MessageStatus.STALLED,
                    result=result,
                    pause_reason="DELIVERY_UNKNOWN",
                    delivery_uncertain=True,
                )
            elif (
                result.delivery_state == DeliveryState.NOT_SENT
                and claim.message.expires_at is not None
                and claim.message.expires_at <= utc_datetime(store.clock())
            ):
                outcome = AttemptOutcome(MessageStatus.EXPIRED, result=result)
            elif verdict is not None and verdict.kind == VerdictKind.DELIVERED:
                outcome = AttemptOutcome(MessageStatus.DONE, result=result, verdict=verdict)
            elif verdict is not None and verdict.kind == VerdictKind.PEER_TERMINAL:
                outcome = AttemptOutcome(MessageStatus.FAILED, verdict.reason, result, verdict)
            else:
                # 确认未送达只有两种:请求未发出,或对端明确未接纳;其余(含未知)都不计数。
                definite = result.delivery_state == DeliveryState.NOT_SENT or (
                    verdict is not None and verdict.kind == VerdictKind.NOT_RECEIVED
                )
                scoped = claim.message.scope is not None
                uncertain = not definite or (not scoped and claim.delivery_uncertain)
                count = claim.not_received_count + definite
                budget = target.max_not_received_attempts
                if (
                    not scoped
                    and budget is not None
                    and definite
                    and not uncertain
                    and count >= budget
                ):
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
                        retry_budget=budget if scoped else None,
                    )
    except EvidencePersistenceError:
        raise
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
    if observe_delivered is not None and outcome.status == MessageStatus.DONE:
        try:
            await observe_delivered(claim, outcome)
        except Exception as error:
            log_exception(
                logger,
                "Delivered evidence unavailable",
                error,
                component="reliable",
                phase="observe_delivered",
                context={"message_id": claim.message_id},
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
    hold_reason = None
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
            phase = "check_dispatch"
            if not await store.snapshots.check_dispatch(claim):
                return
            phase = "before_send"
            try:
                until = await store.before_send(replace(claim, request=request), target)
            except PreDispatchTimeout:
                result = HttpResult(DeliveryState.NOT_SENT, None, None, (), None)
            else:
                if not until:
                    phase = "acquire_client"
                    return
                gated = True
                phase = "send_guard"
                try:
                    async with store.snapshots.http_guard(claim) as guard:
                        hold_reason, before_write, check_wire, note_write = guard
                        deadline = min(until, claim.message.expires_at or until)
                        if hold_reason or deadline <= utc_datetime(clock()):
                            result = HttpResult(DeliveryState.NOT_SENT, None, None, (), None)
                        else:
                            phase = "send"
                            result = await client.send(
                                _guarded_request(
                                    request, before_write, check_wire, deadline, clock, note_write
                                )
                            )
                except PreDispatchUnavailable:
                    result = HttpResult(DeliveryState.NOT_SENT, None, None, (), None)
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
        if hold_reason:
            outcome = AttemptOutcome(MessageStatus.HELD, result=result, pause_reason=hold_reason)
        elif verdict and verdict.kind == VerdictKind.DELIVERED:
            outcome = AttemptOutcome(MessageStatus.DONE, result=result, verdict=verdict)
        elif verdict and verdict.kind == VerdictKind.PEER_TERMINAL:
            outcome = AttemptOutcome(MessageStatus.FAILED, verdict.reason, result, verdict)
        elif verdict and verdict.kind == VerdictKind.HOLD:
            retry_expired = await store.snapshots.should_retry_expired(claim)
            if verdict.pause_reason == "DELIVERY_UNKNOWN" and retry_expired:
                now = utc_datetime(clock())
                outcome = AttemptOutcome(
                    MessageStatus.PENDING,
                    result=result,
                    verdict=verdict,
                    next_attempt_at=now + timedelta(seconds=5),
                    next_expires_at=now + timedelta(seconds=300),
                    delivery_uncertain=True,
                )
            else:
                outcome = AttemptOutcome(
                    MessageStatus.HELD,
                    result=result,
                    verdict=verdict,
                    pause_reason=verdict.pause_reason,
                    delivery_uncertain=True
                    if verdict.pause_reason != "CONTRACT_NON_COMPLIANT"
                    else None,
                )
        elif definite and (gated or not claim.delivery_uncertain):
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
                if await store.snapshots.should_retry_expired(claim):
                    outcome = AttemptOutcome(
                        MessageStatus.PENDING,
                        result=result,
                        verdict=verdict,
                        next_attempt_at=now + timedelta(seconds=5),
                        next_expires_at=now + timedelta(seconds=300),
                        delivery_uncertain=result.delivery_state != DeliveryState.NOT_SENT,
                    )
                else:
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
        elif (
            claim.message.delivery_snapshot.unknown_policy == RETRY_ACK_TIMEOUT
            and result.delivery_state == DeliveryState.DELIVERY_UNKNOWN
            and result.failure_kind == FailureKind.READ_TIMEOUT
            and result.status_code in (None, 200)
            and claim.message.expires_at is not None
            and (now := utc_datetime(clock())) < claim.message.expires_at
        ):
            outcome = AttemptOutcome(
                MessageStatus.PENDING,
                result=result,
                next_attempt_at=now
                + timedelta(seconds=retry_delay_s(claim.attempt_no, target, None, uniform)),
                delivery_uncertain=True,
            )
        else:
            if await store.snapshots.should_retry_expired(claim):
                now = utc_datetime(clock())
                outcome = AttemptOutcome(
                    MessageStatus.PENDING,
                    result=result,
                    verdict=verdict,
                    next_attempt_at=now + timedelta(seconds=5),
                    next_expires_at=now + timedelta(seconds=300),
                    delivery_uncertain=True,
                )
            else:
                outcome = AttemptOutcome(
                    MessageStatus.HELD,
                    result=result,
                    verdict=verdict,
                    pause_reason="DELIVERY_UNKNOWN",
                    delivery_uncertain=True,
                )
    except EvidencePersistenceError:
        raise
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
