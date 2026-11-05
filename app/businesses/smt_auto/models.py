"""Object models for the shared SMT automatic task foundation."""

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
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
    first_plan_wait_ms: Mapped[int | None] = mapped_column(BigInteger)
    first_plan_deadline_ms: Mapped[int | None] = mapped_column(BigInteger)
    confirmation_next_check_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    confirmation_basis: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    closed_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    cancel_reason: Mapped[str | None] = mapped_column(String(200))
    direct_obligations_closed_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    rack_retention_basis: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
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
    target_job_id: Mapped[str | None] = mapped_column(String(100))
    target_preparation_action_id: Mapped[str | None] = mapped_column(String(100))
    source_rack_job_id: Mapped[str | None] = mapped_column(String(100))
    source_preparation_action_id: Mapped[str | None] = mapped_column(String(100))
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
    rack_job_id: Mapped[str | None] = mapped_column(String(100))
    preparation_action_id: Mapped[str | None] = mapped_column(String(100))
    rack_array_index: Mapped[int]
    withdrawn_by_input_id: Mapped[int | None] = mapped_column(ForeignKey("smt_auto_task_input.id"))
    withdrawn_at_ms: Mapped[int | None] = mapped_column(BigInteger)


class AutoOutboundSourceMember(Base):
    __tablename__ = "smt_auto_outbound_source_member"
    __table_args__ = (
        UniqueConstraint(
            "task_id", "rack_id", "rack_face", name="smt_auto_outbound_source_member_identity"
        ),
        UniqueConstraint("first_work_plan_round_id", name="auto_source_first_round_unique"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("smt_auto_task.task_id"))
    plan_input_id: Mapped[int] = mapped_column(ForeignKey("smt_auto_task_input.id"))
    plan_revision: Mapped[int] = mapped_column(BigInteger)
    rack_id: Mapped[str] = mapped_column(String(100))
    rack_face: Mapped[str] = mapped_column(String(50))
    rack_job_id: Mapped[str | None] = mapped_column(String(100))
    preparation_action_id: Mapped[str | None] = mapped_column(String(100))
    arrived_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    first_work_plan_round_id: Mapped[int | None] = mapped_column(
        ForeignKey("smt_auto_outbound_decision_round.id")
    )
    terminal_basis: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    terminal_at_ms: Mapped[int | None] = mapped_column(BigInteger)
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


class AutoInboundPlate(Base):
    __tablename__ = "smt_auto_inbound_plate"
    __table_args__ = (
        UniqueConstraint("id", "task_id", name="inbound_plate_task"),
        CheckConstraint("grant_kind IN ('READY','NEXT','RETRY')", name="inbound_plate_grant"),
        CheckConstraint(
            "(grant_kind = 'NEXT' AND grant_parent_plate_id IS NOT NULL AND "
            "grant_round_id IS NULL) OR (grant_kind IN ('READY','RETRY') AND "
            "grant_round_id IS NOT NULL AND grant_parent_plate_id IS NULL)",
            name="inbound_plate_grant_origin",
        ),
        ForeignKeyConstraint(
            ["grant_round_id", "task_id"],
            ["smt_auto_inbound_decision_round.id", "smt_auto_inbound_decision_round.task_id"],
            name="inbound_plate_round_fk",
        ),
        ForeignKeyConstraint(
            ["grant_parent_plate_id", "task_id"],
            ["smt_auto_inbound_plate.id", "smt_auto_inbound_plate.task_id"],
            name="inbound_plate_parent_fk",
        ),
        CheckConstraint("bin_work_id IS NOT NULL", name="inbound_plate_subject"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("smt_auto_task.task_id"))
    bin_work_id: Mapped[int | None] = mapped_column(ForeignKey("smt_auto_bin_work.id"))
    source_rack_job_id: Mapped[str | None] = mapped_column(String(100))
    source_preparation_action_id: Mapped[str | None] = mapped_column(String(100))
    target_rack_job_id: Mapped[str | None] = mapped_column(String(100))
    target_preparation_action_id: Mapped[str | None] = mapped_column(String(100))
    grant_kind: Mapped[str] = mapped_column(String(20))
    grant_round_id: Mapped[int | None] = mapped_column(unique=True)
    grant_parent_plate_id: Mapped[int | None] = mapped_column(unique=True)
    grant_snapshot: Mapped[dict] = mapped_column(JSON)
    source_locator: Mapped[dict] = mapped_column(JSON)
    source_params: Mapped[dict] = mapped_column(JSON)
    source_arrival_snapshot: Mapped[dict] = mapped_column(JSON)
    pick_call_key: Mapped[str] = mapped_column(String(200), unique=True)
    pick_command_code: Mapped[str | None] = mapped_column(String(200))
    pick_result_ref: Mapped[str | None] = mapped_column(String(200))
    barcode: Mapped[str | None] = mapped_column(String(1000))
    put_call_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    put_command_code: Mapped[str | None] = mapped_column(String(200), unique=True)
    put_result_ref: Mapped[str | None] = mapped_column(String(200))
    report_operation_id: Mapped[str | None] = mapped_column(String(200), unique=True)
    pick_result_snapshot: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    empty_observation: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    target_snapshot: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    next_source_action: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    put_result_snapshot: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    report_data: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    report_response: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    source_closed_basis: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    physical_closure_basis: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    pick_consumed_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    scanned_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    put_consumed_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    report_message_id: Mapped[int | None] = mapped_column(BigInteger)
    report_consumed_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    direct_obligations_closed_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    next_check_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    final_decision_round_id: Mapped[int | None] = mapped_column(
        ForeignKey("smt_auto_inbound_decision_round.id")
    )
    pending: Mapped[bool] = mapped_column(Boolean, default=True)


class AutoInboundDecisionRound(Base):
    __tablename__ = "smt_auto_inbound_decision_round"
    __table_args__ = (
        UniqueConstraint("id", "task_id", name="inbound_round_task"),
        UniqueConstraint("operation", "operation_id", name="inbound_round_operation"),
        UniqueConstraint("task_id", "stage_key", "round_no", name="inbound_round_stage"),
        CheckConstraint(
            "purpose IN ('WORK_PLAN','MATERIAL_DECIDE','EMPTY_DECIDE') AND round_no >= 1",
            name="inbound_round_purpose",
        ),
        CheckConstraint(
            "(bin_work_id IS NOT NULL AND source_preparation_action_id IS NULL AND "
            "plate_id IS NULL AND purpose = 'WORK_PLAN') OR (bin_work_id IS NULL AND "
            "source_preparation_action_id IS NOT NULL AND plate_id IS NULL AND purpose "
            "= 'WORK_PLAN') OR (bin_work_id IS NULL AND source_preparation_action_id IS "
            "NULL AND plate_id IS NOT NULL AND purpose IN "
            "('MATERIAL_DECIDE','EMPTY_DECIDE'))",
            name="inbound_round_subject",
        ),
        ForeignKeyConstraint(
            ["previous_round_id", "task_id"],
            ["smt_auto_inbound_decision_round.id", "smt_auto_inbound_decision_round.task_id"],
            name="inbound_round_previous_fk",
        ),
        Index(
            "inbound_round_unresolved",
            "task_id",
            "stage_key",
            unique=True,
            postgresql_where=text("unresolved"),
            sqlite_where=text("unresolved = 1"),
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("smt_auto_task.task_id"))
    purpose: Mapped[str] = mapped_column(String(30))
    bin_work_id: Mapped[int | None] = mapped_column(ForeignKey("smt_auto_bin_work.id"))
    source_preparation_action_id: Mapped[str | None] = mapped_column(String(100))
    plate_id: Mapped[int | None] = mapped_column(ForeignKey("smt_auto_inbound_plate.id"))
    trigger_plate_id: Mapped[int | None] = mapped_column(ForeignKey("smt_auto_inbound_plate.id"))
    stage_key: Mapped[str] = mapped_column(String(200))
    round_no: Mapped[int]
    previous_round_id: Mapped[int | None] = mapped_column(unique=True)
    operation: Mapped[str] = mapped_column(String(200))
    operation_id: Mapped[str] = mapped_column(String(200))
    message_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    request_data: Mapped[dict] = mapped_column(JSON)
    source_rack_bindings: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    response_snapshot: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    response_recorded_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    consumed_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    unresolved: Mapped[bool] = mapped_column(Boolean, default=True)
    wait_recorded_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    retry_after_ms: Mapped[int | None] = mapped_column(BigInteger)
    next_eligible_at_ms: Mapped[int | None] = mapped_column(BigInteger)


class AutoOutboundPlate(Base):
    __tablename__ = "smt_auto_outbound_plate"
    __table_args__ = (
        UniqueConstraint("id", "task_id", name="outbound_plate_task"),
        CheckConstraint("grant_kind IN ('READY','NEXT','RETRY')", name="outbound_plate_grant"),
        CheckConstraint(
            "(grant_kind = 'NEXT' AND grant_parent_plate_id IS NOT NULL AND "
            "grant_round_id IS NULL) OR (grant_kind IN ('READY','RETRY') AND "
            "grant_round_id IS NOT NULL AND grant_parent_plate_id IS NULL)",
            name="outbound_plate_grant_origin",
        ),
        ForeignKeyConstraint(
            ["grant_round_id", "task_id"],
            ["smt_auto_outbound_decision_round.id", "smt_auto_outbound_decision_round.task_id"],
            name="outbound_plate_round_fk",
        ),
        ForeignKeyConstraint(
            ["grant_parent_plate_id", "task_id"],
            ["smt_auto_outbound_plate.id", "smt_auto_outbound_plate.task_id"],
            name="outbound_plate_parent_fk",
        ),
        CheckConstraint(
            "(bin_work_id IS NOT NULL AND source_member_id IS NULL) OR (bin_work_id IS "
            "NULL AND source_member_id IS NOT NULL)",
            name="outbound_plate_subject",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("smt_auto_task.task_id"))
    bin_work_id: Mapped[int | None] = mapped_column(ForeignKey("smt_auto_bin_work.id"))
    source_rack_job_id: Mapped[str | None] = mapped_column(String(100))
    source_preparation_action_id: Mapped[str | None] = mapped_column(String(100))
    target_rack_job_id: Mapped[str | None] = mapped_column(String(100))
    target_preparation_action_id: Mapped[str | None] = mapped_column(String(100))
    source_member_id: Mapped[int | None] = mapped_column(
        ForeignKey("smt_auto_outbound_source_member.id")
    )
    grant_kind: Mapped[str] = mapped_column(String(20))
    grant_round_id: Mapped[int | None] = mapped_column(unique=True)
    grant_parent_plate_id: Mapped[int | None] = mapped_column(unique=True)
    grant_snapshot: Mapped[dict] = mapped_column(JSON)
    source_locator: Mapped[dict] = mapped_column(JSON)
    source_params: Mapped[dict] = mapped_column(JSON)
    source_arrival_snapshot: Mapped[dict] = mapped_column(JSON)
    pick_call_key: Mapped[str] = mapped_column(String(200), unique=True)
    pick_command_code: Mapped[str | None] = mapped_column(String(200))
    pick_result_ref: Mapped[str | None] = mapped_column(String(200))
    barcode: Mapped[str | None] = mapped_column(String(1000))
    put_call_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    put_command_code: Mapped[str | None] = mapped_column(String(200), unique=True)
    put_result_ref: Mapped[str | None] = mapped_column(String(200))
    report_operation_id: Mapped[str | None] = mapped_column(String(200), unique=True)
    pick_result_snapshot: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    empty_observation: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    target_snapshot: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    next_source_action: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    put_result_snapshot: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    report_data: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    report_response: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    source_closed_basis: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    physical_closure_basis: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    pick_consumed_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    scanned_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    put_consumed_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    report_message_id: Mapped[int | None] = mapped_column(BigInteger)
    report_consumed_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    direct_obligations_closed_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    next_check_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    final_decision_round_id: Mapped[int | None] = mapped_column(
        ForeignKey("smt_auto_outbound_decision_round.id")
    )
    pending: Mapped[bool] = mapped_column(Boolean, default=True)


class AutoOutboundDecisionRound(Base):
    __tablename__ = "smt_auto_outbound_decision_round"
    __table_args__ = (
        UniqueConstraint("id", "task_id", name="outbound_round_task"),
        UniqueConstraint("operation", "operation_id", name="outbound_round_operation"),
        UniqueConstraint("task_id", "stage_key", "round_no", name="outbound_round_stage"),
        CheckConstraint(
            "purpose IN ('WORK_PLAN','MATERIAL_DECIDE','EMPTY_DECIDE') AND round_no >= 1",
            name="outbound_round_purpose",
        ),
        CheckConstraint(
            "(bin_work_id IS NOT NULL AND source_preparation_action_id IS NULL AND "
            "plate_id IS NULL AND purpose = 'WORK_PLAN') OR (bin_work_id IS NULL AND "
            "source_preparation_action_id IS NOT NULL AND plate_id IS NULL AND purpose "
            "= 'WORK_PLAN') OR (bin_work_id IS NULL AND source_preparation_action_id IS "
            "NULL AND plate_id IS NOT NULL AND purpose IN "
            "('MATERIAL_DECIDE','EMPTY_DECIDE'))",
            name="outbound_round_subject",
        ),
        ForeignKeyConstraint(
            ["previous_round_id", "task_id"],
            ["smt_auto_outbound_decision_round.id", "smt_auto_outbound_decision_round.task_id"],
            name="outbound_round_previous_fk",
        ),
        Index(
            "outbound_round_unresolved",
            "task_id",
            "stage_key",
            unique=True,
            postgresql_where=text("unresolved"),
            sqlite_where=text("unresolved = 1"),
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("smt_auto_task.task_id"))
    purpose: Mapped[str] = mapped_column(String(30))
    bin_work_id: Mapped[int | None] = mapped_column(ForeignKey("smt_auto_bin_work.id"))
    source_preparation_action_id: Mapped[str | None] = mapped_column(String(100))
    plate_id: Mapped[int | None] = mapped_column(ForeignKey("smt_auto_outbound_plate.id"))
    trigger_plate_id: Mapped[int | None] = mapped_column(ForeignKey("smt_auto_outbound_plate.id"))
    stage_key: Mapped[str] = mapped_column(String(200))
    round_no: Mapped[int]
    previous_round_id: Mapped[int | None] = mapped_column(unique=True)
    operation: Mapped[str] = mapped_column(String(200))
    operation_id: Mapped[str] = mapped_column(String(200))
    message_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    request_data: Mapped[dict] = mapped_column(JSON)
    source_rack_bindings: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    response_snapshot: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    response_recorded_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    consumed_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    unresolved: Mapped[bool] = mapped_column(Boolean, default=True)
    wait_recorded_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    retry_after_ms: Mapped[int | None] = mapped_column(BigInteger)
    next_eligible_at_ms: Mapped[int | None] = mapped_column(BigInteger)


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
        "inbound_plate": AutoInboundPlate.__table__,
        "inbound_decision_round": AutoInboundDecisionRound.__table__,
        "outbound_plate": AutoOutboundPlate.__table__,
        "outbound_decision_round": AutoOutboundDecisionRound.__table__,
    }.items()
}
