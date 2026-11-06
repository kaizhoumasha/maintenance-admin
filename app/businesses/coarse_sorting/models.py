"""A coarse sorting job and the original external actions it owns."""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

JOB_STATES = (
    "AWAITING_VALIDATION",
    "AWAITING_MOVE",
    "AWAITING_ALLOCATION",
    "AWAITING_PUT",
    "AWAITING_REPORT",
    "AWAITING_NG",
    "COMPLETED",
)
ACTION_KINDS = (
    "VALIDATE_BIND",
    "MOVE_FORWARD",
    "ALLOCATE_STORAGE",
    "PUT_TO_BIN",
    "REPORT_COMPLETION",
)


class CoarseSortingJob(Base):
    __tablename__ = "coarse_sorting_job"
    __table_args__ = (
        CheckConstraint(f"state IN {JOB_STATES}", name="state"),
        CheckConstraint(
            "(rack_position_code IS NULL) = (rack_binding_version IS NULL)", name="position"
        ),
        CheckConstraint(
            "state NOT IN ('AWAITING_PUT', 'AWAITING_REPORT', 'COMPLETED') "
            "OR (current_action_id IS NOT NULL AND allocation_action_id IS NOT NULL)",
            name="allocation",
        ),
        CheckConstraint(
            "state NOT IN ('AWAITING_REPORT', 'COMPLETED') OR placed_at_ms IS NOT NULL",
            name="placement",
        ),
        CheckConstraint("(state = 'COMPLETED') = (completed_at IS NOT NULL)", name="completion"),
        Index("coarse_sorting_job_state_id_idx", "state", "id"),
        Index("coarse_sorting_job_line_state_idx", "work_line_id", "state"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    event_digest: Mapped[str] = mapped_column(String(64), unique=True)
    # Keep the first-stage projection and frozen legacy facts through the staged rollout.
    receipt_id: Mapped[int]
    work_line_id: Mapped[int]
    workline_code: Mapped[str]
    device_id: Mapped[int]
    device_code: Mapped[str]
    device_role: Mapped[str]
    device_version: Mapped[int]
    work_line_version: Mapped[int]
    binding_revision: Mapped[str]
    timestamp: Mapped[int] = mapped_column(BigInteger)
    payload: Mapped[dict] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    pending: Mapped[bool]
    wait_reason: Mapped[str]
    validation_operation_id: Mapped[str | None]
    validation_message_id: Mapped[int | None]
    validation_response: Mapped[dict | None] = mapped_column(JSON)
    binding_id: Mapped[str | None]
    conveyor_device_id: Mapped[int | None]
    conveyor_device_code: Mapped[str | None]
    conveyor_device_version: Mapped[int | None]
    command_code: Mapped[str | None]
    move_result: Mapped[dict | None] = mapped_column(JSON)

    state: Mapped[str] = mapped_column(String(32), server_default="AWAITING_VALIDATION")
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rack_position_code: Mapped[str | None]
    rack_binding_version: Mapped[int | None]
    current_action_id: Mapped[str | None] = mapped_column(
        ForeignKey("coarse_sorting_action.id", use_alter=True, ondelete="RESTRICT")
    )
    allocation_action_id: Mapped[str | None] = mapped_column(
        ForeignKey("coarse_sorting_action.id", use_alter=True, ondelete="RESTRICT")
    )
    placed_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# Existing projection and consumers still address the accepted record as a scan.
CoarseSortingScan = CoarseSortingJob


class CoarseSortingAction(Base):
    __tablename__ = "coarse_sorting_action"
    __table_args__ = (
        CheckConstraint(f"kind IN {ACTION_KINDS}", name="kind"),
        CheckConstraint(
            "round_no >= 1 AND (kind = 'ALLOCATE_STORAGE' OR round_no = 1)", name="round"
        ),
        CheckConstraint(
            "(kind = 'ALLOCATE_STORAGE') = (rack_generation_no IS NOT NULL) "
            "AND (rack_generation_no IS NULL OR rack_generation_no >= 0)",
            name="generation",
        ),
        CheckConstraint(
            "((kind IN ('VALIDATE_BIND', 'ALLOCATE_STORAGE', 'REPORT_COMPLETION')) "
            "AND operation IS NOT NULL AND operation_id IS NOT NULL AND message_id IS NOT NULL "
            "AND command_owner IS NULL AND call_key IS NULL "
            "AND command_code IS NULL AND device_code IS NULL) OR "
            "((kind IN ('MOVE_FORWARD', 'PUT_TO_BIN')) "
            "AND operation IS NULL AND operation_id IS NULL AND message_id IS NULL "
            "AND command_owner IS NOT NULL AND call_key IS NOT NULL "
            "AND command_code IS NOT NULL AND device_code IS NOT NULL "
            "AND resource_snapshot IS NOT NULL)",
            name="protocol",
        ),
        UniqueConstraint(
            "job_id", "kind", "round_no", name="coarse_sorting_action_job_kind_round_key"
        ),
        UniqueConstraint("operation", "operation_id", name="coarse_sorting_action_wms_key"),
        UniqueConstraint("command_owner", "call_key", name="coarse_sorting_action_ecs_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("coarse_sorting_job.id", ondelete="RESTRICT"))
    kind: Mapped[str] = mapped_column(String(32))
    round_no: Mapped[int]
    predecessor_action_id: Mapped[str | None] = mapped_column(
        ForeignKey("coarse_sorting_action.id", ondelete="RESTRICT")
    )
    rack_generation_no: Mapped[int | None]
    request_data: Mapped[dict] = mapped_column(JSON(none_as_null=True))
    resource_snapshot: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    operation: Mapped[str | None]
    operation_id: Mapped[str | None] = mapped_column(String(36))
    message_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    command_owner: Mapped[str | None]
    call_key: Mapped[str | None]
    command_code: Mapped[str | None] = mapped_column(unique=True)
    device_code: Mapped[str | None]
    evidence_history: Mapped[list] = mapped_column(JSON(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
