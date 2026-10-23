from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class DevCallbackScenario(StrEnum):
    succeeded = "succeeded"  # 料箱:各成员 TARGET_PLACED 后给成功结果;货架:成功结果
    position_unknown = "position_unknown"  # 失败且位置未知,任务状态为 UNKNOWN


class DevCallbackBody(BaseModel):
    scenario: DevCallbackScenario


class DevCallbackOut(BaseModel):
    operation: str
    operation_id: str
    status_code: int
    outcome: str
    request: dict[str, Any]
    response: dict[str, Any] | None
    diagnostic: list[dict[str, Any]] | None


class DevTaskStateOut(BaseModel):
    transport_task_id: str
    kind: str
    cycle_id: str | None
    outcome_status: str | None
    applied_outcome_revision: int | None
    applied_outcome: dict[str, Any] | None
    member_positions: dict[str, Any] | None
    pending_outcome: dict[str, Any] | None
    frozen: dict[str, Any] | None
    created_at: datetime
