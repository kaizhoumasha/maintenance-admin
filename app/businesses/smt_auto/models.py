"""Object models for the shared SMT automatic task foundation."""

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class AutoTask(Base):
    __tablename__ = "smt_auto_task"
    __table_args__ = (
        CheckConstraint("task_type = 'AUTO'", name="smt_auto_task_type"),
        CheckConstraint(
            "business_type IN ('inbound', 'outbound')", name="smt_auto_task_business_type"
        ),
        CheckConstraint(
            "status IN ('QUEUED','PREPARING','EXECUTING','COMPLETED','CANCELLED')",
            name="smt_auto_task_status",
        ),
        CheckConstraint(
            "queue_revision >= 1 AND plan_revision >= 0", name="smt_auto_task_revisions"
        ),
        Index(
            "smt_auto_task_execution_slot",
            "work_line_id",
            unique=True,
            postgresql_where=text("status IN ('PREPARING','EXECUTING')"),
            sqlite_where=text("status IN ('PREPARING','EXECUTING')"),
        ),
        Index(
            "smt_auto_task_queue",
            "work_line_id",
            "dispatch_sequence",
            "issued_timestamp",
            "task_id",
        ),
    )

    task_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    task_type: Mapped[str] = mapped_column(String(20), default="AUTO")
    business_type: Mapped[str] = mapped_column(String(20))
    work_line_id: Mapped[int]
    workline_code: Mapped[str] = mapped_column(String(100))
    issued_operation_id: Mapped[str] = mapped_column(String(200))
    issued_timestamp: Mapped[int] = mapped_column(BigInteger)
    issued_data: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="QUEUED")
    queue_revision: Mapped[int] = mapped_column(BigInteger, default=1)
    dispatch_sequence: Mapped[int] = mapped_column(BigInteger)
    not_before_ms: Mapped[int | None] = mapped_column(BigInteger)
    plan_revision: Mapped[int] = mapped_column(BigInteger, default=0)
    prepare_accepted_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    confirmation_next_check_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    confirmation_basis: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    closed_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    cancel_reason: Mapped[str | None] = mapped_column(String(200))
    direct_obligations_closed_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    details_cleaned_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    details_cleaned: Mapped[bool] = mapped_column(Boolean, default=False)
    pending: Mapped[bool] = mapped_column(Boolean, default=True)
    next_check_at_ms: Mapped[int | None] = mapped_column(BigInteger)

    inputs: Mapped[list["AutoTaskInput"]] = relationship(back_populates="task")
    requests: Mapped[list["AutoTaskRequest"]] = relationship(back_populates="task")


class AutoTaskInput(Base):
    __tablename__ = "smt_auto_task_input"
    __table_args__ = (
        UniqueConstraint("operation", "operation_id", name="smt_auto_input_identity"),
        Index(
            "smt_auto_input_plan",
            "task_id",
            "applied_plan_revision",
            unique=True,
            postgresql_where=text("applied_plan_revision IS NOT NULL"),
            sqlite_where=text("applied_plan_revision IS NOT NULL"),
        ),
        CheckConstraint(
            "disposition IN ('BLOCKED','REJECTED','APPLIED','DUPLICATE')",
            name="smt_auto_input_disposition",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("smt_auto_task.task_id"), index=True)
    operation: Mapped[str] = mapped_column(String(200))
    operation_id: Mapped[str] = mapped_column(String(200))
    request_timestamp: Mapped[int] = mapped_column(BigInteger)
    first_received_at_ms: Mapped[int] = mapped_column(BigInteger)
    effective_digest: Mapped[str] = mapped_column(String(128))
    contract_version: Mapped[str] = mapped_column(String(50))
    effective_request: Mapped[dict] = mapped_column(JSON)
    disposition: Mapped[str] = mapped_column(String(20))
    block_reason: Mapped[str | None] = mapped_column(String(100))
    business_outcome: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    outcome_recorded_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    requested_revision: Mapped[int | None] = mapped_column(BigInteger)
    applied_plan_revision: Mapped[int | None] = mapped_column(BigInteger)
    applied_queue_revision: Mapped[int | None] = mapped_column(BigInteger)
    canonical_input_id: Mapped[int | None] = mapped_column(ForeignKey("smt_auto_task_input.id"))
    supply_change_id: Mapped[str | None] = mapped_column(String(200))
    supply_change: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))

    task: Mapped[AutoTask] = relationship(back_populates="inputs")


class AutoTaskRequest(Base):
    __tablename__ = "smt_auto_task_request"
    __table_args__ = (
        UniqueConstraint("operation", "operation_id", name="smt_auto_request_identity"),
        UniqueConstraint("message_id", name="smt_auto_request_message"),
        CheckConstraint("purpose IN ('PREPARE','COMPLETION')", name="smt_auto_request_purpose"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("smt_auto_task.task_id"), index=True)
    purpose: Mapped[str] = mapped_column(String(20))
    operation: Mapped[str] = mapped_column(String(200))
    operation_id: Mapped[str] = mapped_column(String(200))
    message_id: Mapped[int] = mapped_column(BigInteger)
    requested_plan_revision: Mapped[int | None] = mapped_column(BigInteger)
    request_data: Mapped[dict] = mapped_column(JSON)
    response_snapshot: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    response_recorded_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    consumed_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    unresolved: Mapped[bool] = mapped_column(Boolean, default=True)
    completion_result: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    retry_after_ms: Mapped[int | None] = mapped_column(BigInteger)
    next_eligible_at_ms: Mapped[int | None] = mapped_column(BigInteger)

    task: Mapped[AutoTask] = relationship(back_populates="requests")


class AutoSupplyMember(Base):
    __tablename__ = "smt_auto_supply_member"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "plan_revision",
            "rack_id",
            "rack_face",
            name="smt_auto_supply_member_identity",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("smt_auto_task.task_id"))
    plan_input_id: Mapped[int] = mapped_column(ForeignKey("smt_auto_task_input.id"))
    plan_revision: Mapped[int] = mapped_column(BigInteger)
    rack_id: Mapped[str] = mapped_column(String(100))
    rack_face: Mapped[str | None] = mapped_column(String(50))
    rack_array_index: Mapped[int]
    face_array_index: Mapped[int | None]
    withdrawn_by_input_id: Mapped[int | None] = mapped_column(ForeignKey("smt_auto_task_input.id"))
    withdrawn_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    supply_change_id: Mapped[str] = mapped_column(String(200))


class AutoInboundSourceMember(Base):
    __tablename__ = "smt_auto_inbound_source_member"
    __table_args__ = (
        UniqueConstraint(
            "task_id", "plan_revision", "rack_id", name="smt_auto_inbound_source_member_identity"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("smt_auto_task.task_id"))
    plan_input_id: Mapped[int] = mapped_column(ForeignKey("smt_auto_task_input.id"))
    plan_revision: Mapped[int] = mapped_column(BigInteger)
    rack_id: Mapped[str] = mapped_column(String(100))
    rack_array_index: Mapped[int]
    withdrawn_by_input_id: Mapped[int | None] = mapped_column(ForeignKey("smt_auto_task_input.id"))
    withdrawn_at_ms: Mapped[int | None] = mapped_column(BigInteger)


class AutoOutboundSourceMember(Base):
    __tablename__ = "smt_auto_outbound_source_member"
    __table_args__ = (
        UniqueConstraint(
            "task_id", "rack_id", "rack_face", name="smt_auto_outbound_source_member_identity"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("smt_auto_task.task_id"))
    plan_input_id: Mapped[int] = mapped_column(ForeignKey("smt_auto_task_input.id"))
    plan_revision: Mapped[int] = mapped_column(BigInteger)
    rack_id: Mapped[str] = mapped_column(String(100))
    rack_face: Mapped[str] = mapped_column(String(50))
    rack_array_index: Mapped[int]
    withdrawn_by_input_id: Mapped[int | None] = mapped_column(ForeignKey("smt_auto_task_input.id"))
    withdrawn_at_ms: Mapped[int | None] = mapped_column(BigInteger)


class AutoBinWork(Base):
    __tablename__ = "smt_auto_bin_work"
    __table_args__ = (
        UniqueConstraint("work_line_id", "arrival_id", name="smt_auto_bin_work_arrival"),
        CheckConstraint(
            "association IN ('TASK','NO_TASK','EXCEPTION')", name="smt_auto_bin_work_association"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    work_line_id: Mapped[int]
    arrival_id: Mapped[str] = mapped_column(String(200))
    device_code: Mapped[str] = mapped_column(String(100))
    raw_bin_code: Mapped[str] = mapped_column(String(100))
    bin_code: Mapped[str | None] = mapped_column(String(100))
    scanned_at: Mapped[int] = mapped_column(BigInteger)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("smt_auto_task.task_id"))
    business_type: Mapped[str | None] = mapped_column(String(20))
    association: Mapped[str] = mapped_column(String(20))
    production_hold_reason: Mapped[str | None] = mapped_column(String(100))
    exception_basis: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    terminal_basis: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    terminal_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    release_decided_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    release_command_code: Mapped[str | None] = mapped_column(String(100))
    release_result_ref: Mapped[str | None] = mapped_column(String(200))
    release_result_snapshot: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    release_consumed_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    pending: Mapped[bool] = mapped_column(Boolean, default=True)
    next_check_at_ms: Mapped[int | None] = mapped_column(BigInteger)


TABLES = {
    name: table
    for name, table in {
        "task": AutoTask.__table__,
        "input": AutoTaskInput.__table__,
        "request": AutoTaskRequest.__table__,
        "supply_member": AutoSupplyMember.__table__,
        "inbound_source_member": AutoInboundSourceMember.__table__,
        "outbound_source_member": AutoOutboundSourceMember.__table__,
        "bin_work": AutoBinWork.__table__,
    }.items()
}
