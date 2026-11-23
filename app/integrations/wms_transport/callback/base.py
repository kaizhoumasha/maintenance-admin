from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial

from sqlalchemy import select

from app.integrations import history
from app.integrations.runtime_authority import enabled
from app.integrations.wms_callback import Accepted, CallbackContext
from app.models import CallbackReceipt, IntegrationExchange, IntegrationReference
from app.models.transport import TransportTask
from app.transactions import after_commit

from ..settle import settle_submitted
from .apply import TaskCallbacks


async def apply_callback(
    ctx: CallbackContext, apply: Callable[[TaskCallbacks, dict, dict], None]
) -> Accepted | dict:
    data = ctx.parsed  # 保留 Payload 规范化后的 dict
    ack = {"transport_task_id": data["transport_task_id"]}
    task = await ctx.session.scalar(
        select(TransportTask)
        .where(TransportTask.transport_task_id == data["transport_task_id"])
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if task is None:
        ctx.note("UNKNOWN_TASK", transport_task_id=ack["transport_task_id"], data=data)
        return ack
    await settle_submitted(ctx.session, task.transport_task_id)  # 对端已知该任务,不必再重试提交
    if enabled():
        trigger = await ctx.session.get(CallbackReceipt, ctx.receipt_id, with_for_update=True)
        if trigger is not None:
            trigger.owner_domain = trigger.owner_domain or task.owner_domain
        if task.frozen is None:
            ctx.note("FROZEN_MISSING", transport_task_id=task.transport_task_id, data=data)
            return ack
        previous = task.applied_outcome_revision
        previous_pending = task.pending_outcome
        previous_pending_id = task.pending_result_receipt_id
        previous_observation = task.position_observation
        apply(TaskCallbacks(task, ctx), data, ack)
        resulted = ctx.operation == "transport.task.resulted@v1"
        if task.pending_outcome is None:
            task.pending_result_receipt_id = None
            task.pending_result_source_unknown = None
        elif task.pending_outcome != previous_pending:
            task.pending_result_receipt_id = ctx.receipt_id
            task.pending_result_source_unknown = False
        applied = task.applied_outcome_revision
        if applied != previous:
            task.applied_result_receipt_id = ctx.receipt_id if resulted else previous_pending_id
            task.applied_trigger_receipt_id = ctx.receipt_id
        if (
            (applied != previous or task.position_observation != previous_observation)
            and task.owner_domain is not None
            and ctx.on_owner_committed is not None
        ):
            after_commit(ctx.session, partial(ctx.on_owner_committed, task.owner_domain))
        return Accepted(ack)
    trigger = await ctx.session.scalar(
        select(IntegrationExchange).where(
            IntegrationExchange.role == "RECEIPT", IntegrationExchange.receipt_id == ctx.receipt_id
        )
    )
    if trigger is not None:
        if trigger.owner_domain is None:
            trigger.owner_domain = task.owner_domain
        await history.write_reference(
            ctx.session,
            exchange_id=trigger.id,
            reference_kind="TRANSPORT_TASK",
            namespace="id",
            value=task.transport_task_id,
            relation_role="CALLBACK_TARGET",
            source_record_id=trigger.request_record_id,
            created_at=trigger.created_at,
        )
        await history.write_reference(
            ctx.session,
            exchange_id=trigger.id,
            reference_kind="EXCHANGE",
            namespace="integration",
            value=str(task.submission_exchange_id),
            relation_role="CALLBACK_TARGET",
            source_record_id=trigger.request_record_id,
            related_exchange_id=task.submission_exchange_id,
            created_at=trigger.created_at,
        )
    if task.frozen is None:
        ctx.note("FROZEN_MISSING", transport_task_id=ack["transport_task_id"], data=data)
        return ack
    if trigger is not None and ctx.operation == "transport.task.resulted@v1":
        await history.write_reference(
            ctx.session,
            exchange_id=trigger.id,
            reference_kind="TRANSPORT_TASK",
            namespace="transport-outcome",
            value=f"{task.transport_task_id}:{data['outcome_revision']}",
            relation_role="RESULT_SOURCE",
            source_record_id=trigger.request_record_id,
            created_at=datetime.now(UTC),
        )
    previous = task.applied_outcome_revision
    previous_pending = task.pending_outcome
    previous_observation = task.position_observation
    apply(TaskCallbacks(task, ctx), data, ack)  # 判重为 Duplicate/Conflict 时直接抛出
    if task.pending_outcome != previous_pending:
        task.pending_result_source_unknown = None if task.pending_outcome is None else False
    applied = task.applied_outcome_revision
    if applied is not None and (previous is None or applied > previous):
        original = list(
            await ctx.session.scalars(
                select(IntegrationReference).where(
                    IntegrationReference.reference_kind == "TRANSPORT_TASK",
                    IntegrationReference.namespace == "transport-outcome",
                    IntegrationReference.reference_value == f"{task.transport_task_id}:{applied}",
                    IntegrationReference.relation_role == "RESULT_SOURCE",
                )
            )
        )
        source = original[0] if len(original) == 1 else None
        at = datetime.now(UTC)
        record_id = await history.write_record(
            ctx.session,
            draft=history.RecordDraft(
                exchange_id=task.submission_exchange_id,
                record_kind="RESULT_APPLIED",
                event_key=f"APPLIED:{applied}",
                direction="INTERNAL",
                occurred_at=at,
                outcome_revision=applied,
                effective_payload=task.applied_outcome,
                application_state="APPLIED",
                business_result=task.outcome_status,
                parent_record_id=source.source_record_id if source else None,
                source_kind="TRANSPORT_TASK",
                source_key=task.transport_task_id,
                source_version=str(applied),
                details={
                    "trigger_record_id": str(trigger.request_record_id) if trigger else None,
                    "coverage_reason": "ORIGINAL_REFERENCE"
                    if source
                    else "LEGACY_SOURCE_UNAVAILABLE",
                },
            ),
        )
        for role, identity in (
            ("RESULT_SOURCE", source.exchange_id if source else None),
            ("TRIGGER", trigger.id if trigger else None),
        ):
            if identity is not None:
                await history.write_reference(
                    ctx.session,
                    exchange_id=task.submission_exchange_id,
                    reference_kind="EXCHANGE",
                    namespace="integration",
                    value=str(identity),
                    relation_role=role,
                    related_exchange_id=identity,
                    source_record_id=record_id,
                    created_at=at,
                )
    if (
        (
            (applied is not None and (previous is None or applied > previous))
            or task.position_observation != previous_observation
        )
        and task.owner_domain is not None
        and ctx.on_owner_committed is not None
    ):
        after_commit(ctx.session, partial(ctx.on_owner_committed, task.owner_domain))
    return Accepted(ack)
