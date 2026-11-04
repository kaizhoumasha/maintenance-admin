"""Four current scheduling checkpoints; raw events and execution belong to public ports."""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Scope:
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_domain: Mapped[str]
    work_line_id: Mapped[int]
    scope_id: Mapped[str]
    revision: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_reason: Mapped[str | None]


def _unique(name, *columns, where):
    return Index(
        name, *columns, unique=True, sqlite_where=text(where), postgresql_where=text(where)
    )


def _common(name):
    return (
        UniqueConstraint("id", "owner_domain", "work_line_id", name=name + "_scope"),
        CheckConstraint(
            (
                "(closed_at IS NULL AND close_reason IS NULL) OR (closed_at IS NOT NULL AND "
                "close_reason IS NOT NULL)"
            ),
            name="closure",
        ),
        CheckConstraint("scope_id != '' AND revision >= 0", name="identity"),
        Index(name + "_work", "owner_domain", "work_line_id", "closed_at", "id"),
    )


class BinPassage(Scope, Base):
    __tablename__ = "bin_roller_bin_passage"
    __table_args__ = (
        *_common(__tablename__),
        CheckConstraint("scope_kind IN ('TASK','LINE')", name="scope_kind"),
        CheckConstraint(
            (
                "(scan1_event_id IS NULL) = (scan1_visit_key IS NULL) AND (scan2_event_id IS "
                "NULL) = (scan2_visit_key IS NULL) AND (scan1_event_id IS NOT NULL OR "
                "scan2_event_id IS NOT NULL)"
            ),
            name="events",
        ),
        CheckConstraint(
            (
                "(scan1_consumed_result_id IS NULL OR scan1_command_code IS NOT NULL) AND "
                "(release_consumed_result_id IS NULL OR release_command_code IS NOT NULL)"
            ),
            name="consumption",
        ),
        *(
            _unique(
                f"bin_roller_{key}_key",
                "owner_domain",
                "work_line_id",
                key,
                where=f"{key} IS NOT NULL",
            )
            for key in (
                "scan1_visit_key",
                "scan2_visit_key",
                "scan1_event_id",
                "scan2_event_id",
                "release_command_code",
            )
        ),
        Index(
            "bin_roller_passage_predecessor",
            "owner_domain",
            "work_line_id",
            "scope_id",
            "bin_code",
            sqlite_where=text(
                "scope_kind='TASK' AND closed_at IS NULL AND scan1_event_id IS NOT NULL "
                "AND scan2_event_id IS NULL"
            ),
            postgresql_where=text(
                "scope_kind='TASK' AND closed_at IS NULL AND scan1_event_id IS NOT NULL "
                "AND scan2_event_id IS NULL"
            ),
        ),
    )
    scope_kind: Mapped[str]
    bin_code: Mapped[str | None]
    scan1_event_id: Mapped[int | None] = mapped_column(BigInteger)
    scan1_visit_key: Mapped[str | None]
    scan2_event_id: Mapped[int | None] = mapped_column(BigInteger)
    scan2_visit_key: Mapped[str | None]
    scan1_command_code: Mapped[str | None]
    scan1_consumed_result_id: Mapped[str | None]
    production_record_id: Mapped[str | None]
    release_command_code: Mapped[str | None]
    release_consumed_result_id: Mapped[str | None]


class FaceWork(Scope, Base):
    __tablename__ = "bin_roller_face_work"
    __table_args__ = (
        *_common(__tablename__),
        CheckConstraint(
            (
                "(purpose='SUPPLY' AND plan_revision IS NOT NULL AND change_id IS NOT NULL "
                "AND demand_state IS NOT NULL AND demand_state IN ('ACTIVE','WITHDRAWN')) OR "
                "(purpose='DRAIN' AND plan_revision IS NULL AND change_id IS NULL AND "
                "demand_state IS NULL AND feed_cursor IS NULL AND feed_operation_id IS NULL "
                "AND feed_message_id IS NULL)"
            ),
            name="purpose",
        ),
        CheckConstraint(
            (
                "phase IN "
                "('WAIT_RACK','WAIT_FEED_DECISION','FEED_MOVE','RETURN_WINDOW','WAIT_RETURN_D"
                "ECISION','RETURN_MOVE')"
            ),
            name="phase",
        ),
        CheckConstraint(
            (
                "(window_id IS NULL AND window_kind IS NULL) OR (window_id IS NOT NULL AND "
                "window_kind IS NOT NULL AND window_kind IN "
                "('SEGMENT','FACE_END','PURE_RETURN'))"
            ),
            name="window",
        ),
        CheckConstraint(
            (
                "closed_at IS NOT NULL OR ((phase IN "
                "('FEED_MOVE','WAIT_RETURN_DECISION','RETURN_MOVE') AND step_id IS NOT NULL) "
                "OR (phase NOT IN ('FEED_MOVE','WAIT_RETURN_DECISION','RETURN_MOVE') AND "
                "step_id IS NULL))"
            ),
            name="step",
        ),
        CheckConstraint(
            (
                "closed_at IS NOT NULL OR phase NOT IN ('FEED_MOVE','RETURN_MOVE') OR "
                "(current_client_request_id IS NOT NULL AND current_transport_task_id IS NOT "
                "NULL)"
            ),
            name="transport",
        ),
        CheckConstraint("feed_cursor IS NULL OR feed_cursor >= 0", name="cursor"),
        _unique(
            "bin_roller_supply_face",
            "owner_domain",
            "work_line_id",
            "scope_id",
            "plan_revision",
            "rack_id",
            "rack_face",
            where="purpose='SUPPLY'",
        ),
        _unique("bin_roller_drain_face", "rack_job_id", "rack_face", where="purpose='DRAIN'"),
        _unique(
            "bin_roller_face_step", "step_id", where="step_id IS NOT NULL AND closed_at IS NULL"
        ),
        UniqueConstraint(
            "id", "step_id", "owner_domain", "work_line_id", name="bin_roller_face_step_scope"
        ),
    )
    purpose: Mapped[str]
    plan_revision: Mapped[int | None]
    change_id: Mapped[str | None]
    rack_id: Mapped[str]
    rack_face: Mapped[str]
    rack_job_id: Mapped[str | None]
    preparation_action_id: Mapped[str | None]
    demand_state: Mapped[str | None]
    phase: Mapped[str] = mapped_column(default="WAIT_RACK", server_default="WAIT_RACK")
    feed_operation_id: Mapped[str | None]
    feed_message_id: Mapped[int | None] = mapped_column(BigInteger)
    feed_cursor: Mapped[int | None]
    window_id: Mapped[str | None]
    window_kind: Mapped[str | None]
    step_id: Mapped[str | None]
    return_operation_id: Mapped[str | None]
    return_message_id: Mapped[int | None] = mapped_column(BigInteger)
    return_members: Mapped[list | None] = mapped_column(JSON(none_as_null=True))
    current_client_request_id: Mapped[str | None]
    current_transport_task_id: Mapped[str | None]
    consumed_outcome_revision: Mapped[int | None] = mapped_column(BigInteger)
    review_reason: Mapped[str | None]


class BinReturn(Scope, Base):
    __tablename__ = "bin_roller_bin_return"
    __table_args__ = (
        *_common(__tablename__),
        CheckConstraint(
            "return_state IN ('OBSERVED','WAITING','READY','ALLOCATING','MOVING','WAIT_FACT')",
            name="state",
        ),
        CheckConstraint(
            (
                "(scan3_event_id IS NULL) = (scan3_visit_key IS NULL) AND (scan4_event_id IS "
                "NULL) = (scan4_visit_key IS NULL) AND (scan3_event_id IS NOT NULL OR "
                "scan4_event_id IS NOT NULL)"
            ),
            name="events",
        ),
        CheckConstraint(
            (
                "(first_scan4_event_id IS NULL AND first_scan4_received_at IS NULL AND "
                "fifo_visit_key IS NULL) OR (first_scan4_event_id IS NOT NULL AND "
                "first_scan4_received_at IS NOT NULL AND fifo_visit_key IS NOT NULL)"
            ),
            name="fifo",
        ),
        CheckConstraint(
            "(selected_face_work_id IS NULL) = (selected_step_id IS NULL)", name="selection"
        ),
        CheckConstraint(
            (
                "closed_at IS NOT NULL OR return_state NOT IN ('ALLOCATING','MOVING') OR "
                "selected_face_work_id IS NOT NULL"
            ),
            name="selected_execution",
        ),
        CheckConstraint(
            (
                "closed_at IS NOT NULL OR return_state NOT IN ('MOVING','WAIT_FACT') OR "
                "(transport_task_id IS NOT NULL AND client_request_id IS NOT NULL AND caller "
                "IS NOT NULL)"
            ),
            name="transport",
        ),
        CheckConstraint(
            "closed_at IS NOT NULL OR return_state != 'WAIT_FACT' OR selected_face_work_id IS NULL",
            name="fact_only",
        ),
        CheckConstraint(
            (
                "(scan3_consumed_result_id IS NULL OR scan3_command_code IS NOT NULL) AND "
                "(scan4_consumed_result_id IS NULL OR scan4_command_code IS NOT NULL)"
            ),
            name="consumption",
        ),
        ForeignKeyConstraint(
            ["selected_face_work_id", "selected_step_id", "owner_domain", "work_line_id"],
            [
                "bin_roller_face_work.id",
                "bin_roller_face_work.step_id",
                "bin_roller_face_work.owner_domain",
                "bin_roller_face_work.work_line_id",
            ],
            ondelete="RESTRICT",
        ),
        _unique(
            "bin_roller_current_return_root",
            "owner_domain",
            "work_line_id",
            "bin_code",
            where=(
                "is_current AND closed_at IS NULL AND bin_code IS NOT NULL AND "
                "scan3_event_id IS NOT NULL AND return_state != 'OBSERVED'"
            ),
        ),
        *(
            _unique(
                f"bin_roller_{key}_key",
                "owner_domain",
                "work_line_id",
                key,
                where=f"{key} IS NOT NULL",
            )
            for key in ("scan3_visit_key", "scan4_visit_key")
        ),
        Index(
            "bin_roller_fifo",
            "owner_domain",
            "work_line_id",
            "first_scan4_received_at",
            "fifo_visit_key",
        ),
        Index("bin_roller_return_selection", "selected_face_work_id", "selected_step_id"),
    )
    bin_code: Mapped[str | None]
    scan3_event_id: Mapped[int | None] = mapped_column(BigInteger)
    scan3_visit_key: Mapped[str | None]
    scan3_command_code: Mapped[str | None]
    scan3_consumed_result_id: Mapped[str | None]
    first_scan4_event_id: Mapped[int | None] = mapped_column(BigInteger)
    first_scan4_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fifo_visit_key: Mapped[str | None]
    scan4_event_id: Mapped[int | None] = mapped_column(BigInteger)
    scan4_visit_key: Mapped[str | None]
    scan4_command_code: Mapped[str | None]
    scan4_consumed_result_id: Mapped[str | None]
    is_current: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    return_state: Mapped[str] = mapped_column(default="OBSERVED", server_default="OBSERVED")
    selected_face_work_id: Mapped[str | None]
    selected_step_id: Mapped[str | None]
    transport_task_id: Mapped[str | None]
    client_request_id: Mapped[str | None]
    caller: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    consumed_outcome_revision: Mapped[int | None] = mapped_column(BigInteger)


class WorklineState(Base):
    __tablename__ = "bin_roller_workline_state"
    __table_args__ = (
        CheckConstraint("drain_phase IN ('IDLE','REQUESTED','WAIT','USING')", name="phase"),
        CheckConstraint(
            (
                "(drain_phase='IDLE' AND drain_operation_id IS NULL AND drain_message_id IS "
                "NULL AND drain_retry_at IS NULL AND candidate_cursor IS NULL) OR "
                "(drain_phase!='IDLE' AND line_run_id IS NOT NULL AND drain_operation_id IS "
                "NOT NULL AND drain_message_id IS NOT NULL AND candidate_cursor IS NOT NULL "
                "AND candidate_cursor >= 0 AND ((drain_phase='WAIT' AND drain_retry_at IS "
                "NOT NULL) OR (drain_phase!='WAIT' AND drain_retry_at IS NULL)))"
            ),
            name="drain",
        ),
    )
    owner_domain: Mapped[str] = mapped_column(primary_key=True)
    work_line_id: Mapped[int] = mapped_column(primary_key=True)
    line_run_id: Mapped[str | None]
    revision: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )
    drain_phase: Mapped[str] = mapped_column(default="IDLE", server_default="IDLE")
    drain_operation_id: Mapped[str | None]
    drain_message_id: Mapped[int | None] = mapped_column(BigInteger)
    drain_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    candidate_cursor: Mapped[int | None]
    drain_review_reason: Mapped[str | None]


TABLES = {
    "bin_passage": BinPassage.__table__,
    "bin_return": BinReturn.__table__,
    "face_work": FaceWork.__table__,
    "workline_state": WorklineState.__table__,
}
