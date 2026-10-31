"""Component facts; loaded only through a consuming business's MODEL_MODULES."""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Scope:
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_domain: Mapped[str] = mapped_column(String)
    work_line_id: Mapped[int]


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
    __table_args__ = (UniqueConstraint("owner_domain", "work_line_id", "business_key"),)
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
    return_round_id: Mapped[str | None]
    delegated: Mapped[bool] = mapped_column(default=False)
    pending: Mapped[bool] = mapped_column(default=True)
    command_code: Mapped[str | None]
    consumed_result_id: Mapped[str | None]


class Scan2Release(Scope, Base):
    __tablename__ = "bin_roller_scan2_release"
    __table_args__ = (UniqueConstraint("owner_domain", "work_line_id", "arrival_id"),)
    arrival_id: Mapped[str]
    call_key: Mapped[str]
    command_code: Mapped[str | None]
    pending: Mapped[bool] = mapped_column(default=True)
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


class RackFaceService(Scope, Base):
    __tablename__ = "bin_roller_face_service"
    __table_args__ = (UniqueConstraint("owner_domain", "work_line_id", "cycle_id", "source_ref"),)
    cycle_id: Mapped[str]
    source_ref: Mapped[str]
    rack_face: Mapped[str]
    task_id: Mapped[str | None]
    plan_revision: Mapped[int | None]
    status: Mapped[str] = mapped_column(default="PENDING")
    pending: Mapped[bool] = mapped_column(default=True)


class SupplyDemand(Scope, Base):
    __tablename__ = "bin_roller_supply_demand"
    __table_args__ = (
        UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "task_id",
            "plan_revision",
            "rack_id",
            "rack_face",
        ),
    )
    task_id: Mapped[str]
    change_id: Mapped[str]
    plan_revision: Mapped[int]
    rack_id: Mapped[str]
    rack_face: Mapped[str]
    cycle_id: Mapped[str | None]
    service_id: Mapped[str | None]
    withdrawn: Mapped[bool] = mapped_column(default=False)


class Feed(Scope, Base):
    __tablename__ = "bin_roller_feed"
    __table_args__ = (UniqueConstraint("owner_domain", "work_line_id", "service_id"),)
    service_id: Mapped[str]
    caller_workline_id: Mapped[str]
    station_id: Mapped[str | None]
    target: Mapped[dict] = mapped_column(JSON)
    operation_id: Mapped[str]
    message_id: Mapped[int | None]
    manifest: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    feed_complete: Mapped[bool] = mapped_column(default=False)
    pending: Mapped[bool] = mapped_column(default=True)


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


class ReturnRound(Scope, Base):
    __tablename__ = "bin_roller_return_round"
    __table_args__ = (UniqueConstraint("owner_domain", "work_line_id", "scan3_arrival_id"),)
    scan3_arrival_id: Mapped[str]
    scan4_arrival_id: Mapped[str | None]
    bin_code: Mapped[str]
    status: Mapped[str] = mapped_column(default="PENDING")
    removed: Mapped[bool] = mapped_column(default=False)


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


class Drain(Scope, Base):
    __tablename__ = "bin_roller_drain"
    __table_args__ = (UniqueConstraint("owner_domain", "work_line_id", "input_kind", "input_ref"),)
    input_kind: Mapped[str]
    input_ref: Mapped[str]
    input_snapshot: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    request: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    unused_racks: Mapped[list] = mapped_column(JSON, default=list)
    operation_id: Mapped[str] = mapped_column(unique=True)
    message_id: Mapped[int | None]
    decision: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    due_at: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(default="PENDING")
    pending: Mapped[bool] = mapped_column(default=True)


class Departure(Scope, Base):
    __tablename__ = "bin_roller_departure"
    __table_args__ = (UniqueConstraint("owner_domain", "work_line_id", "cycle_id", "source_ref"),)
    cycle_id: Mapped[str]
    source_ref: Mapped[str]
    operation_id: Mapped[str] = mapped_column(unique=True)
    message_id: Mapped[int | None]
    request: Mapped[dict] = mapped_column(JSON)
    decision: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    due_at: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(default="PENDING")
    pending: Mapped[bool] = mapped_column(default=True)


TABLES = {
    "scan_fact": ScanFact.__table__,
    "scan_arrival": ScanArrival.__table__,
    "scan2_release": Scan2Release.__table__,
    "position_fact": RackPositionFact.__table__,
    "rack_cycle": RackCycle.__table__,
    "rack_action": RackAction.__table__,
    "face_service": RackFaceService.__table__,
    "supply_demand": SupplyDemand.__table__,
    "feed": Feed.__table__,
    "feed_segment": FeedSegment.__table__,
    "return_round": ReturnRound.__table__,
    "return_opportunity": ReturnOpportunity.__table__,
    "return_batch": ReturnBatch.__table__,
    "drain": Drain.__table__,
    "departure": Departure.__table__,
}
