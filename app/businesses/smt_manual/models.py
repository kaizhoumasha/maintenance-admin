"""Original manual tasks, accepted inputs and versioned resource members."""

from sqlalchemy import JSON, BigInteger, CheckConstraint, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.businesses._components.sorting_task.models import SortingTaskRecord
from app.models.base import Base


class ManualTask(SortingTaskRecord):
    __mapper_args__ = {"polymorphic_identity": "MANUAL"}


BIN_COMPLETION_OPERATION = "manual_bin.work_completed@v1"
RACK_COMPLETION_OPERATIONS = (
    "manual_rack.direct_pick_completed@v1",
    "outbound.return_rack.task_completed@v1",
)


class ManualInput(Base):
    __tablename__ = "smt_manual_input"
    __table_args__ = (
        CheckConstraint(
            "state IN ('APPLIED','DEFERRED','REJECTED','PENDING','DUPLICATE','REVIEW')",
            name="smt_manual_input_state",
        ),
        CheckConstraint(
            "NOT is_plan_record OR (operation = 'sorting.task.plan_delta@v1' "
            "AND plan_revision IS NOT NULL AND state = 'APPLIED')",
            name="smt_manual_input_plan_record",
        ),
        CheckConstraint(
            "(operation IN "
            "('sorting.task.issued@v1','sorting.task.queue_changed@v1','sorting.task.plan"
            "_delta@v1','sorting.task.cancel@v1') AND state IN "
            "('APPLIED','DEFERRED','REJECTED') AND rack_id IS NULL AND rack_face IS NULL "
            "AND bin_code IS NULL AND arrival_id IS NULL AND completed_at IS NULL AND "
            "completion_result IS NULL AND original_operation_id IS NULL AND ((operation "
            "= 'sorting.task.plan_delta@v1' AND plan_revision IS NOT NULL) OR (operation "
            "!= 'sorting.task.plan_delta@v1' AND plan_revision IS NULL))) OR (operation "
            "= 'manual_bin.work_completed@v1' AND receipt_id IS NOT NULL AND bin_code IS "
            "NOT NULL AND completed_at IS NOT NULL AND completion_result IS NOT NULL AND "
            "plan_revision IS NULL AND rack_id IS NULL AND rack_face IS NULL AND state "
            "IN ('PENDING','APPLIED','DUPLICATE','REVIEW')) OR (operation IN "
            "('manual_rack.direct_pick_completed@v1','outbound.return_rack.task_"
            "completed@v1') AND receipt_id IS NOT NULL AND rack_id IS NOT NULL AND "
            "plan_revision IS NOT NULL AND completed_at IS NOT NULL AND bin_code IS NULL "
            "AND arrival_id IS NULL AND completion_result IS NULL AND "
            "original_operation_id IS NULL AND reason IS NULL AND state IN "
            "('PENDING','APPLIED') AND ((operation = "
            "'manual_rack.direct_pick_completed@v1' AND rack_face IS NOT NULL) "
            "OR (operation = 'outbound.return_rack.task_completed@v1' AND rack_face IS "
            "NULL)))",
            name="smt_manual_input_shape",
        ),
        Index(
            "smt_manual_original_plan",
            "task_id",
            "plan_revision",
            unique=True,
            postgresql_where=text("is_plan_record"),
            sqlite_where=text("is_plan_record"),
        ),
        Index("smt_manual_pending_input", "task_id", "operation", "state"),
    )
    operation: Mapped[str] = mapped_column(String, primary_key=True)
    operation_id: Mapped[str] = mapped_column(String, primary_key=True)
    receipt_id: Mapped[int | None] = mapped_column(BigInteger)
    task_id: Mapped[str]
    work_line_id: Mapped[int]
    timestamp: Mapped[int] = mapped_column(BigInteger)
    payload: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str]
    reason: Mapped[str | None]
    is_plan_record: Mapped[bool] = mapped_column(default=False)
    plan_revision: Mapped[int | None] = mapped_column(BigInteger)
    rack_id: Mapped[str | None]
    rack_face: Mapped[str | None]
    bin_code: Mapped[str | None]
    arrival_id: Mapped[str | None]
    completed_at: Mapped[int | None] = mapped_column(BigInteger)
    completion_result: Mapped[str | None]
    original_operation_id: Mapped[str | None]
    target_job_id: Mapped[str | None]
    target_action_id: Mapped[str | None]


class ManualRequest(Base):
    __tablename__ = "smt_manual_request"
    __table_args__ = (
        CheckConstraint(
            "(kind = 'PREPARE' AND operation = 'sorting.task.prepare@v1') OR "
            "(kind = 'COMPLETION_CONFIRM' AND operation = 'sorting.task.completion_confirm@v1') OR "
            "(kind = 'BIN_ADMISSION' AND operation = 'manual_bin.work_admission_decide@v1') OR "
            "(kind = 'RACK_ARRIVAL_REPORT' AND "
            "operation = 'outbound.return_rack.arrival_report@v1')",
            name="smt_manual_request_kind",
        ),
        CheckConstraint(
            "state IN ('REQUESTED','WAITING','CONSUMED','CONFLICT')",
            name="smt_manual_request_state",
        ),
        CheckConstraint(
            "(kind = 'BIN_ADMISSION' AND arrival_id IS NOT NULL AND rack_work_id IS NULL) OR "
            "(kind = 'RACK_ARRIVAL_REPORT' "
            "AND rack_work_id IS NOT NULL AND arrival_id IS NULL) OR "
            "(kind IN ('PREPARE','COMPLETION_CONFIRM') AND rack_work_id IS NULL AND "
            "arrival_id IS NULL)",
            name="smt_manual_request_subject",
        ),
        CheckConstraint(
            "(kind = 'RACK_ARRIVAL_REPORT' AND action_id IS NOT NULL) OR (kind != "
            "'RACK_ARRIVAL_REPORT' AND action_id IS NULL)",
            name="smt_manual_request_action",
        ),
        CheckConstraint(
            "(kind = 'COMPLETION_CONFIRM' AND plan_revision IS NOT NULL) OR "
            "(kind != 'COMPLETION_CONFIRM' AND plan_revision IS NULL)",
            name="smt_manual_request_revision",
        ),
        CheckConstraint(
            "(kind = 'COMPLETION_CONFIRM' AND basis IS NOT NULL) OR "
            "(kind != 'COMPLETION_CONFIRM' AND basis IS NULL)",
            name="smt_manual_request_basis",
        ),
        CheckConstraint(
            "state != 'WAITING' OR due_at_ms IS NOT NULL", name="smt_manual_request_due"
        ),
        CheckConstraint(
            "kind != 'COMPLETION_CONFIRM' OR state IN ('REQUESTED','CONFLICT') "
            "OR decision_code IS NOT NULL",
            name="smt_manual_confirmation_decision",
        ),
        Index("smt_manual_request_subject_state", "kind", "task_id", "state"),
        Index(
            "smt_manual_report_action",
            "action_id",
            unique=True,
            postgresql_where=text("kind = 'RACK_ARRIVAL_REPORT'"),
            sqlite_where=text("kind = 'RACK_ARRIVAL_REPORT'"),
        ),
    )
    operation: Mapped[str] = mapped_column(String, primary_key=True)
    operation_id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str]
    message_id: Mapped[int] = mapped_column(BigInteger)
    task_id: Mapped[str]
    work_line_id: Mapped[int]
    arrival_id: Mapped[str | None]
    rack_work_id: Mapped[str | None]
    action_id: Mapped[str | None]
    plan_revision: Mapped[int | None] = mapped_column(BigInteger)
    request_snapshot: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    basis: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    response: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    state: Mapped[str] = mapped_column(default="REQUESTED")
    decision_code: Mapped[str | None]
    saved_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    due_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    previous_operation_id: Mapped[str | None]


class TaskMember(Base):
    __tablename__ = "smt_manual_task_member"
    __table_args__ = (
        UniqueConstraint("task_id", "plan_revision", "kind", "rack_id", "rack_face", "slot_id"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    task_id: Mapped[str]
    plan_revision: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str]
    rack_id: Mapped[str]
    rack_face: Mapped[str]
    slot_id: Mapped[str]
    member_order: Mapped[int]
    status: Mapped[str] = mapped_column(default="PENDING")
    cancelled_operation_id: Mapped[str | None]
    rack_work_id: Mapped[str | None]
    preparation_action_id: Mapped[str | None]
    arrival_report_operation_id: Mapped[str | None]


class ManualBin(Base):
    __tablename__ = "smt_manual_bin"
    arrival_id: Mapped[str] = mapped_column(String, primary_key=True)
    work_line_id: Mapped[int]
    device_code: Mapped[str]
    raw_bin_code: Mapped[str]
    bin_code: Mapped[str | None]
    scanned_at: Mapped[int] = mapped_column(BigInteger)
    task_id: Mapped[str | None]
    state: Mapped[str]
    reason: Mapped[str | None]
    pending: Mapped[bool]
    admission_operation_id: Mapped[str | None]
    completed_operation_id: Mapped[str | None]
    completed_result: Mapped[str | None]
    command_code: Mapped[str | None]
    target_job_id: Mapped[str | None]
    target_action_id: Mapped[str | None]


TABLES = {
    "task": ManualTask.__table__,
    "input": ManualInput.__table__,
    "member": TaskMember.__table__,
    "manual_bin": ManualBin.__table__,
    "request": ManualRequest.__table__,
}
