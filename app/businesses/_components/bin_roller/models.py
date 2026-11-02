"""Component facts; loaded only through a consuming business's MODEL_MODULES."""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Scope:
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_domain: Mapped[str] = mapped_column(String)
    work_line_id: Mapped[int]


def _unique_where(name, *columns, condition):
    predicate = text(condition)
    return Index(name, *columns, unique=True, sqlite_where=predicate, postgresql_where=predicate)


class ScanFact(Scope, Base):
    __tablename__ = "bin_roller_scan_fact"
    __table_args__ = (UniqueConstraint("digest"),)
    digest: Mapped[str] = mapped_column(String(64))
    receipt_id: Mapped[int]
    arrival_id: Mapped[str]
    point: Mapped[str]
    device_code: Mapped[str]
    raw_bin_code: Mapped[str]
    bin_code: Mapped[str | None]
    scanned_at: Mapped[int] = mapped_column(BigInteger)
    binding_snapshot: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))


class ScanArrival(Scope, Base):
    __tablename__ = "bin_roller_scan_arrival"
    __table_args__ = (
        UniqueConstraint("owner_domain", "work_line_id", "business_key"),
        _unique_where(
            "bin_roller_release_record",
            "release_record_id",
            condition="release_record_id IS NOT NULL",
        ),
        _unique_where(
            "bin_roller_round_root",
            "round_id",
            condition="point = 'SCAN3' AND round_id IS NOT NULL",
        ),
        Index(
            "bin_roller_release_work",
            "owner_domain",
            "work_line_id",
            "release_state",
            "release_record_id",
        ),
        Index("bin_roller_point_round", "owner_domain", "work_line_id", "round_id", "point"),
        CheckConstraint(
            "(point = 'SCAN2' AND delegation_state IS NOT NULL AND delegation_state IN "
            "('PENDING','ACCEPTED'))"
            " OR (point != 'SCAN2' AND delegation_state IS NULL)",
            name="delegation",
        ),
        CheckConstraint(
            "(release_record_id IS NULL AND release_call_key IS NULL AND release_state IS NULL "
            "AND release_command_code IS NULL AND release_consumed_result_id IS NULL) OR "
            "(point = 'SCAN2' AND release_record_id IS NOT NULL AND release_call_key IS NOT NULL "
            "AND release_state IS NOT NULL AND (release_state = 'PENDING' OR "
            "(release_state = 'CONSUMED' AND release_command_code IS NOT NULL "
            "AND release_command_code != '' AND release_consumed_result_id IS NOT NULL "
            "AND release_consumed_result_id != '')))",
            name="release",
        ),
        CheckConstraint(
            "(round_id IS NULL AND return_state IS NULL AND removed IS NULL AND "
            "first_scan4_arrival_id IS NULL) OR "
            "(round_id IS NOT NULL AND ((point = 'SCAN3' AND bin_code IS NOT NULL AND "
            "return_state IS NOT NULL AND removed IS NOT NULL) "
            "OR (point = 'SCAN4' AND return_state IS NULL AND removed IS NULL AND "
            "first_scan4_arrival_id IS NULL)))",
            name="round",
        ),
    )
    business_key: Mapped[str] = mapped_column(String(64))
    first_digest: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    point: Mapped[str]
    device_code: Mapped[str]
    raw_bin_code: Mapped[str]
    bin_code: Mapped[str | None]
    scanned_at: Mapped[int] = mapped_column(BigInteger)
    previous_arrival_id: Mapped[str | None]
    separator_command_code: Mapped[str | None]
    round_id: Mapped[str | None]
    return_state: Mapped[str | None]
    removed: Mapped[bool | None]
    first_scan4_arrival_id: Mapped[str | None]
    delegation_state: Mapped[str | None]
    release_record_id: Mapped[str | None]
    release_call_key: Mapped[str | None]
    release_command_code: Mapped[str | None]
    release_state: Mapped[str | None]
    release_consumed_result_id: Mapped[str | None]
    pending: Mapped[bool] = mapped_column(default=True)
    command_code: Mapped[str | None]
    consumed_result_id: Mapped[str | None]


class RackPositionFact(Scope, Base):
    __tablename__ = "bin_roller_position_fact"
    __table_args__ = (
        UniqueConstraint(
            "owner_domain", "work_line_id", "source_kind", "source_id", "source_revision"
        ),
        Index(
            "bin_roller_current_object",
            "owner_domain",
            "work_line_id",
            "object_kind",
            "object_code",
            unique=True,
            sqlite_where=text("is_current"),
            postgresql_where=text("is_current"),
        ),
        Index(
            "bin_roller_current_position",
            "owner_domain",
            "work_line_id",
            "object_kind",
            "location_code",
            unique=True,
            sqlite_where=text("is_current"),
            postgresql_where=text("is_current"),
        ),
    )
    object_kind: Mapped[str]
    object_code: Mapped[str]
    location_code: Mapped[str]
    rack_face: Mapped[str | None]
    source_kind: Mapped[str]
    source_id: Mapped[str]
    source_revision: Mapped[int] = mapped_column(BigInteger)
    is_current: Mapped[bool]


class RackCycle(Scope, Base):
    __tablename__ = "bin_roller_rack_cycle"
    __table_args__ = (UniqueConstraint("owner_domain", "work_line_id", "source_ref", "rack_id"),)
    rack_id: Mapped[str]
    purpose: Mapped[str]
    task_id: Mapped[str | None]
    source_ref: Mapped[str]
    resources: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    status: Mapped[str] = mapped_column(default="ACTIVE")
    pending: Mapped[bool] = mapped_column(default=True)
    release_basis: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))


class RackAction(Scope, Base):
    __tablename__ = "bin_roller_rack_action"
    __table_args__ = (
        UniqueConstraint("owner_domain", "work_line_id", "cycle_id", "kind", "source_ref"),
        UniqueConstraint("client_request_id"),
        UniqueConstraint("transport_task_id"),
        Index(
            "bin_roller_cycle_entry_departure",
            "owner_domain",
            "work_line_id",
            "cycle_id",
            "kind",
            unique=True,
            sqlite_where=text("kind IN ('CTU01', 'CTU03')"),
            postgresql_where=text("kind IN ('CTU01', 'CTU03')"),
        ),
    )
    cycle_id: Mapped[str]
    kind: Mapped[str]
    source_ref: Mapped[str]
    client_request_id: Mapped[str]
    transport_task_id: Mapped[str | None]
    operation_id: Mapped[str | None]
    station_id: Mapped[str | None]
    caller_workline_id: Mapped[str]
    target: Mapped[dict] = mapped_column(JSON)
    target_face: Mapped[str | None]
    status: Mapped[str] = mapped_column(default="PENDING")
    pending: Mapped[bool] = mapped_column(default=True)
    consumed_revision: Mapped[int | None] = mapped_column(BigInteger)
    position_revision: Mapped[int | None] = mapped_column(BigInteger)
    acceptance_basis: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))


class FaceWork(Scope, Base):
    __tablename__ = "bin_roller_face_work"
    __table_args__ = (
        _unique_where("bin_roller_demand_id", "demand_id", condition="demand_id IS NOT NULL"),
        _unique_where("bin_roller_service_id", "service_id", condition="service_id IS NOT NULL"),
        _unique_where("bin_roller_feed_id", "feed_id", condition="feed_id IS NOT NULL"),
        _unique_where(
            "bin_roller_supply_face",
            "owner_domain",
            "work_line_id",
            "task_id",
            "plan_revision",
            "rack_id",
            "rack_face",
            condition="purpose = 'SUPPLY'",
        ),
        _unique_where(
            "bin_roller_cycle_service",
            "owner_domain",
            "work_line_id",
            "cycle_id",
            "source_ref",
            condition="service_id IS NOT NULL",
        ),
        CheckConstraint(
            "(purpose = 'SUPPLY' AND demand_id IS NOT NULL AND id = demand_id AND "
            "task_id IS NOT NULL AND plan_revision IS NOT NULL AND change_id IS NOT NULL AND "
            "demand_state IS NOT NULL AND demand_state IN ('ACTIVE','WITHDRAWN')) OR "
            "(purpose = 'DRAIN' AND service_id IS NOT NULL AND id = service_id AND "
            "demand_id IS NULL "
            "AND demand_state IS NULL AND change_id IS NULL AND feed_id IS NULL)",
            name="purpose",
        ),
        CheckConstraint(
            "(service_id IS NULL AND source_ref IS NULL AND service_state IS NULL) OR "
            "(service_id IS NOT NULL AND cycle_id IS NOT NULL AND source_ref IS NOT NULL "
            "AND (purpose != 'SUPPLY' OR source_ref = demand_id) AND service_state IS NOT NULL "
            "AND service_state IN ('PENDING','READY','COMPLETED','WITHDRAWN') "
            "AND (service_state != 'WITHDRAWN' OR "
            "(feed_id IS NULL AND (purpose = 'DRAIN' OR demand_state = 'WITHDRAWN'))))",
            name="service",
        ),
        CheckConstraint(
            "(feed_id IS NULL AND feed_state IS NULL AND caller_workline_id IS NULL AND "
            "station_id IS NULL "
            "AND target IS NULL AND operation_id IS NULL AND message_id IS NULL AND "
            "manifest IS NULL) OR "
            "(feed_id IS NOT NULL AND service_id IS NOT NULL AND operation_id IS NOT NULL AND "
            "caller_workline_id IS NOT NULL AND target IS NOT NULL AND feed_state IS NOT NULL AND "
            "feed_state IN ('REQUESTED','ALLOCATED','REVIEW','COMPLETED'))",
            name="feed",
        ),
    )
    purpose: Mapped[str] = mapped_column(primary_key=True)
    demand_id: Mapped[str | None]
    service_id: Mapped[str | None]
    feed_id: Mapped[str | None]
    task_id: Mapped[str | None]
    change_id: Mapped[str | None]
    plan_revision: Mapped[int | None]
    rack_id: Mapped[str]
    rack_face: Mapped[str]
    cycle_id: Mapped[str | None]
    source_ref: Mapped[str | None]
    demand_state: Mapped[str | None]
    service_state: Mapped[str | None]
    feed_state: Mapped[str | None]
    caller_workline_id: Mapped[str | None]
    station_id: Mapped[str | None]
    target: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    operation_id: Mapped[str | None]
    message_id: Mapped[int | None]
    manifest: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))


class FeedSegment(Scope, Base):
    __tablename__ = "bin_roller_feed_segment"
    __table_args__ = (UniqueConstraint("owner_domain", "work_line_id", "feed_id", "segment_index"),)
    feed_id: Mapped[str]
    segment_index: Mapped[int]
    members: Mapped[list] = mapped_column(JSON)
    client_request_id: Mapped[str]
    transport_task_id: Mapped[str | None]
    operation_id: Mapped[str | None]
    consumed_revision: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(default="PENDING")
    pending: Mapped[bool] = mapped_column(default=True)


class ReturnOpportunity(Scope, Base):
    __tablename__ = "bin_roller_return_opportunity"
    __table_args__ = (UniqueConstraint("owner_domain", "work_line_id", "service_id", "source_ref"),)
    service_id: Mapped[str]
    source_ref: Mapped[str]
    kind: Mapped[str]
    ended: Mapped[bool] = mapped_column(default=False)
    end_reason: Mapped[str | None]
    end_evidence: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))


class ReturnBatch(Scope, Base):
    __tablename__ = "bin_roller_return_batch"
    __table_args__ = (
        UniqueConstraint("owner_domain", "work_line_id", "opportunity_id", "source_ref"),
    )
    opportunity_id: Mapped[str]
    source_ref: Mapped[str]
    operation_id: Mapped[str]
    message_id: Mapped[int | None]
    members: Mapped[list] = mapped_column(JSON)
    manifest: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    workline_code: Mapped[str]
    caller_workline_id: Mapped[str]
    station_id: Mapped[str | None]
    source: Mapped[dict] = mapped_column(JSON)
    transport_operation_id: Mapped[str | None]
    consumed_revision: Mapped[int | None] = mapped_column(BigInteger)
    client_request_id: Mapped[str | None]
    transport_task_id: Mapped[str | None]
    status: Mapped[str] = mapped_column(default="PENDING")
    pending: Mapped[bool] = mapped_column(default=True)


class Decision(Scope, Base):
    __tablename__ = "bin_roller_decision"
    __table_args__ = (
        UniqueConstraint("kind", "operation_id"),
        _unique_where(
            "bin_roller_drain_input",
            "owner_domain",
            "work_line_id",
            "input_kind",
            "input_ref",
            condition="kind = 'DRAIN'",
        ),
        _unique_where(
            "bin_roller_departure_source",
            "owner_domain",
            "work_line_id",
            "cycle_id",
            "source_ref",
            condition="kind = 'DEPARTURE'",
        ),
        CheckConstraint(
            "(kind = 'DRAIN' AND input_kind IS NOT NULL AND input_ref IS NOT NULL "
            "AND unused_racks IS NOT NULL AND cycle_id IS NULL AND source_ref IS NULL) OR "
            "(kind = 'DEPARTURE' AND input_kind IS NULL AND input_ref IS NULL AND "
            "input_snapshot IS NULL "
            "AND unused_racks IS NULL AND cycle_id IS NOT NULL AND source_ref IS NOT "
            "NULL AND request IS NOT NULL)",
            name="subject",
        ),
    )
    kind: Mapped[str] = mapped_column(primary_key=True)
    cycle_id: Mapped[str | None]
    source_ref: Mapped[str | None]
    input_kind: Mapped[str | None]
    input_ref: Mapped[str | None]
    input_snapshot: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    request: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    unused_racks: Mapped[list | None] = mapped_column(JSON(none_as_null=True))
    operation_id: Mapped[str]
    message_id: Mapped[int | None]
    decision: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    due_at: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(default="PENDING")
    pending: Mapped[bool] = mapped_column(default=True)


TABLES = {
    "scan_fact": ScanFact.__table__,
    "scan_arrival": ScanArrival.__table__,
    "position_fact": RackPositionFact.__table__,
    "rack_cycle": RackCycle.__table__,
    "rack_action": RackAction.__table__,
    "face_work": FaceWork.__table__,
    "feed_segment": FeedSegment.__table__,
    "return_opportunity": ReturnOpportunity.__table__,
    "return_batch": ReturnBatch.__table__,
    "decision": Decision.__table__,
}
