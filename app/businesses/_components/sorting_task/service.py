"""Stateless delegation; only compiled consumers may resolve original ownership."""

from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.businesses import catalog
from app.businesses.runtime import bind_callback_business, require_component, require_runtime
from app.integrations.wms_callback import (
    CallbackContext,
    CallbackResponse,
    Conflict,
    Outcome,
    Rejected,
    Unavailable,
)
from app.models import WorkLine

from .contracts import Cancel, Issued, PlanDelta, QueueChanged, validate_task_branch
from .ports import TaskHost, TaskOwnerRef

_METHODS = {
    "sorting.task.issued@v1": "accept_task_issued",
    "sorting.task.queue_changed@v1": "accept_task_queue_changed",
    "sorting.task.plan_delta@v1": "accept_task_plan_delta",
    "sorting.task.cancel@v1": "accept_task_cancel",
}


class SortingTask:
    def __init__(self, business_code: str, production: TaskHost):
        self.business_code = business_code
        self.production = production

    async def read_task_owner(self, session: AsyncSession, *, task_id: str) -> TaskOwnerRef | None:
        return await self.production.read_task_owner(session, task_id=task_id)

    async def accept(
        self, ctx: CallbackContext, payload: Issued | QueueChanged | PlanDelta | Cancel
    ) -> Outcome:
        if ctx.operation not in _METHODS:
            raise Rejected("INVALID_DATA")
        return await getattr(self.production, _METHODS[ctx.operation])(ctx, payload)

    async def recheck(
        self, ctx: CallbackContext, payload: PlanDelta, original_response: Mapping
    ) -> Outcome | None:
        return await self.production.recheck_task_plan(ctx, payload, original_response)

    async def archive_guard(
        self, ctx: CallbackContext, payload: Issued | QueueChanged | PlanDelta | Cancel
    ) -> CallbackResponse | None:
        guard = getattr(self.production, "archive_guard", None)
        return None if guard is None else await guard(ctx, payload)


async def _resolve(ctx: CallbackContext, payload, *, validate_branch: bool = True) -> SortingTask:
    runtime = require_runtime(ctx)
    key = ("wms_callback", ctx.operation, None)
    consumers = catalog.contract_consumers(key)
    owners = []
    for business in sorted(consumers):
        target = catalog.input_target(business, key)
        parts = getattr(runtime.services.get(business), "components", None)
        if (
            target is None
            or target.source != ctx.registration_source
            or target.component_key != "sorting_task"
            or not isinstance(parts, Mapping)
            or "sorting_task" not in parts
        ):
            raise Unavailable()
        owner = await parts["sorting_task"].read_task_owner(ctx.session, task_id=payload.task_id)
        if owner is not None:
            if (
                not isinstance(owner, TaskOwnerRef)
                or owner.task_id != payload.task_id
                or owner.business_code != business
                or owner.task_type not in {"MANUAL", "AUTO"}
                or owner.business_type not in {"inbound", "outbound"}
            ):
                raise Unavailable()
            owners.append(owner)
    if len(owners) > 1:
        raise Unavailable()
    if not owners and ctx.original_response is not None:
        original = ctx.original_response
        if original["code"] == "REJECTED":
            raise Rejected(original["data"]["reason_code"])
        if original["code"] == "CONFLICT" and (
            ctx.operation != "sorting.task.plan_delta@v1"
            or original["data"] == {"reason_code": "REFERENCE_CONFLICT"}
        ):
            raise Conflict(dict(original["data"]))
        raise Unavailable()
    if owners:
        owner = owners[0]
        business = owner.business_code
        if validate_branch:
            validate_task_branch(
                payload, task_type=owner.task_type, business_type=owner.business_type
            )
    elif isinstance(payload, Issued):
        line = await ctx.session.scalar(
            select(WorkLine).where(WorkLine.line_code == payload.workline_code)
        )
        if line is None:
            raise Conflict({"reason_code": "REFERENCE_CONFLICT"})
        business = line.business_code
        if business not in consumers:
            raise Unavailable()
        if validate_branch:
            validate_task_branch(
                payload, task_type=payload.task_type, business_type=payload.business_type
            )
    else:
        raise Conflict({"reason_code": "REFERENCE_CONFLICT"})
    try:
        bind_callback_business(ctx, business)
    except Rejected:
        raise Unavailable() from None
    return require_component(ctx, "sorting_task")


async def accept(ctx: CallbackContext, payload) -> Outcome:
    return await (await _resolve(ctx, payload)).accept(ctx, payload)


def _same_refusal(original: Mapping, refusal: Conflict | Rejected) -> bool:
    code = "CONFLICT" if isinstance(refusal, Conflict) else "REJECTED"
    data = refusal.data if isinstance(refusal, Conflict) else {"reason_code": refusal.reason_code}
    return original["code"] == code and original["data"] == data


async def recheck(
    ctx: CallbackContext, payload: PlanDelta, original_response: Mapping
) -> Outcome | None:
    reference_refusal = original_response["code"] == "CONFLICT" and original_response["data"] == {
        "reason_code": "REFERENCE_CONFLICT"
    }
    try:
        component = await _resolve(ctx, payload, validate_branch=not reference_refusal)
    except (Conflict, Rejected) as refusal:
        if _same_refusal(original_response, refusal):
            return None
        raise
    if reference_refusal:
        return None
    return await component.recheck(ctx, payload, original_response)


async def archive_guard(ctx: CallbackContext, payload) -> CallbackResponse | None:
    original = ctx.original_response
    definite_refusal = original is not None and (
        (
            original["code"] in {"CONFLICT", "REJECTED"}
            and ctx.operation != "sorting.task.plan_delta@v1"
        )
        or (
            original["code"] == "CONFLICT"
            and original["data"] == {"reason_code": "REFERENCE_CONFLICT"}
        )
        or (original["code"] == "REJECTED" and original["data"] == {"reason_code": "INVALID_DATA"})
    )
    try:
        # Later acceptance validation cannot replace a saved definite refusal or its host guard.
        component = await _resolve(ctx, payload, validate_branch=not definite_refusal)
    except (Conflict, Rejected) as refusal:
        # Let acceptance save first refusals; the receiver alone replays historical timestamps.
        if ctx.original_response is None or _same_refusal(ctx.original_response, refusal):
            return None
        raise Unavailable() from None
    return await component.archive_guard(ctx, payload)
