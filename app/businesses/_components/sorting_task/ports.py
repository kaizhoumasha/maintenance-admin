"""Original host task identity and transaction-owned task input ports."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.wms_callback import CallbackContext, CallbackResponse, Outcome

from .contracts import Cancel, Issued, PlanDelta, QueueChanged


@dataclass(frozen=True, slots=True)
class TaskOwnerRef:
    task_id: str
    business_code: str
    task_type: Literal["MANUAL", "AUTO"]
    business_type: Literal["inbound", "outbound"]
    work_line_id: int
    workline_code: str


class TaskHost(Protocol):
    async def read_task_owner(
        self, session: AsyncSession, *, task_id: str
    ) -> TaskOwnerRef | None: ...

    async def accept_task_issued(self, ctx: CallbackContext, payload: Issued) -> Outcome: ...

    async def accept_task_queue_changed(
        self, ctx: CallbackContext, payload: QueueChanged
    ) -> Outcome: ...

    async def accept_task_plan_delta(self, ctx: CallbackContext, payload: PlanDelta) -> Outcome: ...

    async def accept_task_cancel(self, ctx: CallbackContext, payload: Cancel) -> Outcome: ...

    async def recheck_task_plan(
        self, ctx: CallbackContext, payload: PlanDelta, original_response: Mapping
    ) -> Outcome | None: ...


class TaskArchiveGuard(Protocol):
    async def archive_guard(
        self, ctx: CallbackContext, payload: Issued | QueueChanged | PlanDelta | Cancel
    ) -> CallbackResponse | None: ...
