"""Shared original dispatch jobs and their ordered actions."""

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def _unique(name, *columns, where):
    predicate = text(where)
    return Index(name, *columns, unique=True, sqlite_where=predicate, postgresql_where=predicate)


class RackJob(Base):
    __tablename__ = "rack_dispatch_job"
    __table_args__ = (
        UniqueConstraint("owner_domain", "work_line_id", "source_kind", "source_ref", "rack_id"),
        UniqueConstraint("id", "owner_domain", "work_line_id", name="rack_dispatch_job_scope"),
        Index("rack_dispatch_job_work", "owner_domain", "work_line_id", "pending", "id"),
        _unique(
            "rack_dispatch_job_admission_order",
            "work_line_id",
            "task_id",
            "point",
            "admission_order",
            where="task_id IS NOT NULL AND admission_order IS NOT NULL",
        ),
        Index(
            "rack_dispatch_job_admission_scan",
            "work_line_id",
            "task_id",
            "point",
            "pending",
            "admission_order",
        ),
        CheckConstraint(
            "(admission_order IS NULL OR admission_order > 0) AND (task_id IS "
            "NOT NULL OR admission_order IS NULL)",
            name="admission_order",
        ),
        Index(
            "rack_dispatch_job_source_work",
            "owner_domain",
            "work_line_id",
            "source_kind",
            "pending",
            "id",
        ),
        CheckConstraint(
            "(scope_owner IS NULL AND scope_kind IS NULL AND scope_id IS NULL) OR "
            "(scope_owner IS NOT NULL AND scope_owner != '' AND scope_kind IS NOT NULL AND "
            "scope_kind IN ('TASK','LINE') AND scope_id IS NOT NULL AND scope_id != '')",
            name="execution_scope",
        ),
        Index("rack_dispatch_job_execution_scope", "scope_owner", "scope_kind", "scope_id"),
        CheckConstraint(
            "source_kind IN ('MANUAL_RETURN','MANUAL_TRANSFER','ROLLER_ORDINARY','ROLLER_DRAIN',"
            "'AUTO_INBOUND_SOURCE','AUTO_OUTBOUND_RETURN','AUTO_OUTBOUND_TARGET',"
            "'COARSE_INCOMING','COARSE_WMS_PRESENT')",
            name="source_kind",
        ),
        CheckConstraint(
            "(state IN ('ACTIVE','RELEASING') AND pending AND closure_reason IS NULL AND "
            "closure_basis IS NULL) OR "
            "(NOT pending AND closure_reason IS NOT NULL AND closure_basis IS NOT NULL AND "
            "((state='COMPLETED' AND (closure_reason='LEGACY_CLOSED' OR "
            "(closure_reason='DEPARTURE_ACK' AND completed_at_ms IS NOT NULL))) OR "
            "(state IN ('WITHDRAWN','NOT_ARRIVED') AND closure_reason=state))) OR "
            "(state='CLOSED_BY_SCOPE' AND closure_reason IS NOT NULL AND closure_reason=state "
            "AND closure_basis IS NOT NULL)",
            name="closure",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_domain: Mapped[str]
    work_line_id: Mapped[int]
    scope_owner: Mapped[str | None] = mapped_column(Text)
    scope_kind: Mapped[str | None] = mapped_column(String(4))
    scope_id: Mapped[str | None] = mapped_column(Text)
    source_kind: Mapped[str]
    source_ref: Mapped[str]
    rack_id: Mapped[str]
    original_cycle_id: Mapped[str]
    task_id: Mapped[str | None]
    admission_order: Mapped[int | None] = mapped_column(BigInteger)
    workline_code: Mapped[str | None]
    point: Mapped[str | None]
    initial_face: Mapped[str | None]
    caller: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    resources: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    state: Mapped[str] = mapped_column(default="ACTIVE")
    pending: Mapped[bool] = mapped_column(default=True)
    current_action_id: Mapped[str | None]
    release_requested: Mapped[bool] = mapped_column(default=False)
    release_basis: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    facts_revision: Mapped[int] = mapped_column(BigInteger, default=0)
    review_reason: Mapped[str | None]
    actual_position: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    actual_face: Mapped[str | None]
    position_action_id: Mapped[str | None]
    closure_reason: Mapped[str | None]
    closure_basis: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    completed_at_ms: Mapped[int | None] = mapped_column(BigInteger)


class RackAction(Base):
    __tablename__ = "rack_dispatch_action"
    __table_args__ = (
        ForeignKeyConstraint(
            ["job_id", "owner_domain", "work_line_id"],
            [
                "rack_dispatch_job.id",
                "rack_dispatch_job.owner_domain",
                "rack_dispatch_job.work_line_id",
            ],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("job_id", "kind", "source_ref", name="rack_dispatch_action_origin"),
        UniqueConstraint("job_id", "ordinal", name="rack_dispatch_action_order"),
        UniqueConstraint("job_id", "id", name="rack_dispatch_action_ref"),
        UniqueConstraint("job_id", "id", "phase", name="rack_dispatch_action_decision_ref"),
        ForeignKeyConstraint(
            ["job_id", "previous_action_id"],
            ["rack_dispatch_action.job_id", "rack_dispatch_action.id"],
            ondelete="RESTRICT",
            name="rack_dispatch_action_predecessor",
        ),
        ForeignKeyConstraint(
            ["job_id", "previous_action_id", "previous_decision_phase"],
            [
                "rack_dispatch_action.job_id",
                "rack_dispatch_action.id",
                "rack_dispatch_action.phase",
            ],
            ondelete="RESTRICT",
            name="rack_dispatch_action_decision_predecessor",
        ),
        _unique(
            "rack_dispatch_action_single_move", "job_id", "kind", where="kind IN ('ENTER','DEPART')"
        ),
        _unique(
            "rack_dispatch_action_root",
            "job_id",
            where="kind='DEPARTURE_DECIDE' AND previous_action_id IS NULL",
        ),
        _unique(
            "rack_dispatch_action_wait_child",
            "job_id",
            "previous_action_id",
            where="kind='DEPARTURE_DECIDE' AND previous_action_id IS NOT NULL",
        ),
        UniqueConstraint("client_request_id"),
        UniqueConstraint("transport_task_id"),
        UniqueConstraint("submit_operation_id"),
        UniqueConstraint("operation", "decision_operation_id"),
        CheckConstraint(
            "(kind='DEPARTURE_DECIDE' AND completion_rule IS NULL AND phase IN "
            "('REGISTERED','REVIEW','READY','WAIT')) OR "
            "(kind IN ('ENTER','ROTATE') AND completion_rule IS NOT NULL AND "
            "completion_rule='APPLIED_RESULT' AND phase IN ('REGISTERED','APPLIED','REVIEW')) OR "
            "(kind='ROTATE' AND phase='WITHDRAWN' AND completion_rule IS NOT NULL AND "
            "completion_rule='APPLIED_RESULT' AND client_request_id IS NOT NULL AND "
            "COALESCE(basis ->> 'registration_at_cutover','')='LOCAL_ONLY' AND "
            "COALESCE(basis ->> 'withdrawal_reason','')='NO_FACE_DEMAND' AND "
            "transport_task_id IS NULL AND submit_message_id IS NULL AND "
            "submit_operation_id IS NULL AND submit_target IS NULL AND "
            "outcome_revision IS NULL AND outcome IS NULL AND position_observation IS NULL AND "
            "observation_revision IS NULL AND pending_revision IS NULL AND "
            "position_revision IS NULL AND actual_position IS NULL AND actual_face IS NULL AND "
            "acceptance_basis IS NULL AND accepted_at_ms IS NULL) OR "
            "(kind='DEPART' AND completion_rule IS NOT NULL AND "
            "((completion_rule='ACCEPTED_DEPARTURE' AND phase IN "
            "('REGISTERED','ACCEPTED','REVIEW')) OR "
            "(completion_rule='LEGACY_RECORDED_CLOSURE' AND phase='LEGACY_CLOSED' AND basis IS "
            "NOT NULL)))",
            name="rule_phase",
        ),
        CheckConstraint(
            "(kind='DEPARTURE_DECIDE' AND ((previous_action_id IS NULL AND "
            "previous_decision_phase IS NULL) OR "
            "(previous_action_id IS NOT NULL AND previous_decision_phase IS NOT NULL AND "
            "previous_decision_phase='WAIT'))) OR "
            "(kind='DEPART' AND ((phase='LEGACY_CLOSED' AND previous_decision_phase IS NULL) OR "
            "(previous_action_id IS NOT NULL AND previous_decision_phase IS NOT NULL AND "
            "previous_decision_phase='READY'))) OR "
            "(kind IN ('ENTER','ROTATE') AND previous_decision_phase IS NULL)",
            name="predecessor",
        ),
        CheckConstraint(
            "phase!='WAIT' OR (COALESCE(decision ->> 'result', '')='WAIT' AND due_at_ms IS NOT "
            "NULL)",
            name="wait_decision",
        ),
        CheckConstraint(
            "phase!='READY' OR COALESCE(decision ->> 'result', '')='READY'", name="ready_decision"
        ),
        CheckConstraint(
            "phase!='APPLIED' OR (outcome_revision IS NOT NULL AND outcome IS NOT NULL)",
            name="applied_result",
        ),
        CheckConstraint(
            "phase!='ACCEPTED' OR (acceptance_basis IS NOT NULL AND accepted_at_ms IS NOT NULL)",
            name="departure_acceptance",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str]
    owner_domain: Mapped[str]
    work_line_id: Mapped[int]
    ordinal: Mapped[int]
    kind: Mapped[str]
    source_ref: Mapped[str]
    phase: Mapped[str] = mapped_column(default="REGISTERED")
    completion_rule: Mapped[str | None]
    previous_action_id: Mapped[str | None]
    previous_decision_phase: Mapped[str | None]
    caller: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    template: Mapped[str | None]
    source: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    target: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    target_face: Mapped[str | None]
    client_request_id: Mapped[str | None]
    transport_task_id: Mapped[str | None]
    submit_operation_id: Mapped[str | None]
    submit_message_id: Mapped[int | None] = mapped_column(BigInteger)
    submit_target: Mapped[str | None]
    outcome: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    outcome_revision: Mapped[int | None] = mapped_column(BigInteger)
    position_observation: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    observation_revision: Mapped[int | None] = mapped_column(BigInteger)
    actual_position: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    actual_face: Mapped[str | None]
    position_revision: Mapped[int | None] = mapped_column(BigInteger)
    pending_revision: Mapped[int | None] = mapped_column(BigInteger)
    acceptance_basis: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    accepted_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    operation: Mapped[str | None]
    decision_operation_id: Mapped[str | None]
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    request: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    decision: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    decision_consumed_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    due_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    basis: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
