"""Task inputs retain the shared, frozen sorting-task contracts."""

from typing import Annotated, ClassVar, Literal

from pydantic import Field, TypeAdapter, model_validator

from app.businesses._components.sorting_task.contracts import (
    Cancel,
    Face,
    Identifier,
    Issued,
    PlanDelta,
    Positive,
    PrepareAccepted,
    QueueChanged,
    validate_task_branch,
)
from app.integrations.wms_callback import CallbackPayload


class ManualCompleted(CallbackPayload):
    task_id: Identifier
    bin_code: Identifier
    result: Literal["NORMAL", "NG"]
    completed_at: Positive


class _RackCompletion(CallbackPayload):
    @model_validator(mode="before")
    @classmethod
    def reject_other_completion_fields(cls, value):
        completion_fields = {
            "plan_revision",
            "through_plan_revision",
            "rack_face",
            "bin_code",
            "result",
            "slot_id",
            "slot_ids",
            "slot_results",
            "results",
            "cell_id",
        }
        if isinstance(value, dict) and (value.keys() & completion_fields) - cls.model_fields.keys():
            raise ValueError("field does not apply to this rack completion")
        return value


class DirectPickCompleted(_RackCompletion):
    task_id: Identifier
    plan_revision: Positive
    rack_id: Identifier
    rack_face: Face
    completed_at: Positive


class ReturnRackCompleted(_RackCompletion):
    task_id: Identifier
    through_plan_revision: Positive
    rack_id: Identifier
    completed_at: Positive


class _Decision(CallbackPayload):
    branch_fields: ClassVar[frozenset[str]] = frozenset({"result", "task_id", "retry_after_ms"})

    @model_validator(mode="before")
    @classmethod
    def check_branch(cls, value):
        if isinstance(value, dict) and (value.keys() & cls.branch_fields) - cls.model_fields.keys():
            raise ValueError("field belongs to another admission branch")
        return value


class WorkRequired(_Decision):
    result: Literal["WORK_REQUIRED"]
    task_id: Identifier


class NoWork(_Decision):
    result: Literal["NO_WORK"]


class AdmissionWait(_Decision):
    result: Literal["WAIT"]
    retry_after_ms: Annotated[int, Field(ge=1, le=60000)]


ADMISSION_DECISION = TypeAdapter(
    Annotated[WorkRequired | NoWork | AdmissionWait, Field(discriminator="result")]
)

__all__ = [
    "Cancel",
    "Issued",
    "PlanDelta",
    "PrepareAccepted",
    "QueueChanged",
    "validate_task_branch",
]
