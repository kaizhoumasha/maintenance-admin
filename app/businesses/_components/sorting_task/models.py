"""One durable task identity and execution slot for SMT sorting."""

from sqlalchemy import JSON, BigInteger, Boolean, CheckConstraint, Computed, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SortingTaskRecord(Base):
    __tablename__ = "sorting_task"
    __table_args__ = (
        CheckConstraint("task_type IN ('MANUAL', 'AUTO')", name="sorting_task_type"),
        CheckConstraint(
            "business_type IN ('inbound', 'outbound')", name="sorting_task_business_type"
        ),
        CheckConstraint(
            "status IN ('QUEUED','PREPARING','EXECUTING','COMPLETED','CANCELLED')",
            name="sorting_task_status",
        ),
        CheckConstraint(
            "queue_revision >= 1 AND plan_revision >= 0", name="sorting_task_revisions"
        ),
        Index(
            "sorting_task_execution_slot",
            "work_line_id",
            unique=True,
            postgresql_where=text("status IN ('PREPARING','EXECUTING')"),
            sqlite_where=text("status IN ('PREPARING','EXECUTING')"),
        ),
        Index(
            "sorting_task_queue", "work_line_id", "dispatch_sequence", "issued_timestamp", "task_id"
        ),
    )

    task_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    task_type: Mapped[str] = mapped_column(String(20))
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

    owner_domain: Mapped[str] = mapped_column(
        String(20),
        Computed(
            "CASE task_type WHEN 'MANUAL' THEN 'smt_manual' WHEN 'AUTO' THEN 'smt_auto' END",
            persisted=True,
        ),
    )
    # Existing AUTO detail rows can reference only AUTO tasks after the tables merge.
    auto_task_id: Mapped[str | None] = mapped_column(
        String(100),
        Computed("CASE WHEN task_type = 'AUTO' THEN task_id END", persisted=True),
        unique=True,
    )

    prepare_operation_id: Mapped[str | None]
    prepare_message_id: Mapped[int | None] = mapped_column(BigInteger)
    prepare_accepted: Mapped[bool] = mapped_column(default=False)
    prepare_response_timestamp: Mapped[int | None] = mapped_column(BigInteger)
    confirmation_operation_id: Mapped[str | None]
    received_work: Mapped[list] = mapped_column(JSON, default=list)
    target_rack: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))

    __mapper_args__ = {"polymorphic_on": task_type}
