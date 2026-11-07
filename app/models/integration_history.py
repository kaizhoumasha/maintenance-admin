from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    DDL,
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Sequence,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import SCHEMA

from .base import Base


def uuid7():
    from app.core.identifiers import uuid7 as generate

    return generate()


class ExchangeRole(StrEnum):
    ECS_COMMAND = "ECS_COMMAND"
    RECEIPT = "RECEIPT"
    OUTGOING = "OUTGOING"
    DIAGNOSTIC = "DIAGNOSTIC"


class RecordKind(StrEnum):
    REQUEST_REGISTERED = "REQUEST_REGISTERED"
    SEND_ATTEMPT_OPENED = "SEND_ATTEMPT_OPENED"
    HTTP_RESPONSE_OBSERVED = "HTTP_RESPONSE_OBSERVED"
    SEND_ATTEMPT_FINISHED = "SEND_ATTEMPT_FINISHED"
    ECS_ATTEMPT_CONCLUSION = "ECS_ATTEMPT_CONCLUSION"
    CALLBACK_RECEIVED = "CALLBACK_RECEIVED"
    RESPONSE_PREPARED = "RESPONSE_PREPARED"
    RESULT_EVIDENCE = "RESULT_EVIDENCE"
    RESULT_APPLIED = "RESULT_APPLIED"
    RESULT_REJECTED = "RESULT_REJECTED"
    DELIVERY_TRANSITION = "DELIVERY_TRANSITION"
    BUSINESS_CONSUMED = "BUSINESS_CONSUMED"
    LEGACY_SNAPSHOT = "LEGACY_SNAPSHOT"


JSON_VALUE = JSON(none_as_null=True)
METADATA_JSON = JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")
RECEIPT_SEQUENCE = Sequence("integration_receipt_id_seq", schema=SCHEMA, metadata=Base.metadata)


def record_fk():
    return ForeignKey(f"{SCHEMA}.integration_record.id", ondelete="RESTRICT", use_alter=True)


class IntegrationExchange(Base):
    __tablename__ = "integration_exchange"
    __table_args__ = (
        CheckConstraint("schema_version > 0", name="schema_version"),
        CheckConstraint("role IN ('ECS_COMMAND','RECEIPT','OUTGOING','DIAGNOSTIC')", name="role"),
        CheckConstraint("peer_system IS NULL OR peer_system IN ('ECS','WMS')", name="peer"),
        CheckConstraint("direction IN ('INBOUND','OUTBOUND')", name="direction"),
        CheckConstraint(
            "interaction_kind IN ('COMMAND','EVENT','REQUEST','CALLBACK')", name="kind"
        ),
        CheckConstraint("capture_origin IN ('LIVE','BACKFILL')", name="capture_origin"),
        CheckConstraint("coverage IN ('DECLARED','PARTIAL','LEGACY')", name="coverage"),
        CheckConstraint(
            "(role='RECEIPT' AND receipt_id IS NOT NULL AND receipt_active IS NOT NULL "
            "AND operation IS NOT NULL AND operation_id IS NOT NULL AND raw_digest IS NOT NULL "
            "AND compare_mode IS NOT NULL "
            "AND ((compare_mode='RAW' AND effective_digest IS NULL) OR "
            "(compare_mode='EFFECTIVE' AND effective_digest IS NOT NULL))) OR "
            "(role<>'RECEIPT' AND receipt_id IS NULL AND receipt_active IS NULL "
            "AND compare_mode IS NULL)",
            name="receipt",
        ),
        CheckConstraint(
            "(role='ECS_COMMAND' AND command_code IS NOT NULL AND owner_domain IS NOT NULL "
            "AND call_key IS NOT NULL AND call_key_digest IS NOT NULL AND input_raw IS NOT NULL "
            "AND status IS NOT NULL AND delivery_status IS NOT NULL) OR "
            "(role<>'ECS_COMMAND' AND command_code IS NULL AND call_key IS NULL "
            "AND call_key_digest IS NULL AND status IS NULL AND delivery_status IS NULL)",
            name="command",
        ),
        CheckConstraint(
            "status IS NULL OR status IN ('QUEUED','DISPATCHING','WAITING_RESULT',"
            "'RECONCILING','SUCCEEDED','FAILED','REJECTED','EXPIRED','HELD')",
            name="command_status",
        ),
        CheckConstraint(
            "delivery_status IS NULL OR delivery_status IN "
            "('PENDING','SENDING','STALLED','HELD','DONE','FAILED','EXPIRED','ABANDONED')",
            name="command_delivery",
        ),
        UniqueConstraint("command_code"),
        UniqueConstraint("receipt_id"),
        Index(
            "integration_exchange_call_key",
            "owner_domain",
            "call_key_digest",
            unique=True,
            postgresql_where=text("role='ECS_COMMAND'"),
            sqlite_where=text("role='ECS_COMMAND'"),
        ),
        Index(
            "integration_exchange_active_receipt_key",
            "operation",
            "operation_id",
            unique=True,
            postgresql_where=text("role='RECEIPT' AND receipt_active"),
            sqlite_where=text("role='RECEIPT' AND receipt_active"),
        ),
        Index("integration_exchange_time_idx", "created_at", "id"),
        Index("integration_exchange_identity_idx", "identity_value", postgresql_using="hash"),
        Index("integration_exchange_device_idx", "device_code", postgresql_using="hash"),
        *(
            Index(
                f"integration_exchange_{column}_idx",
                column,
                postgresql_where=text(f"{column} IS NOT NULL"),
                sqlite_where=text(f"{column} IS NOT NULL"),
            )
            for column in ("request_record_id", "ack_record_id", "current_response_record_id")
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    role: Mapped[str] = mapped_column(String(16))
    peer_system: Mapped[str | None] = mapped_column(String(3))
    protocol: Mapped[str] = mapped_column(Text)
    direction: Mapped[str] = mapped_column(String(8))
    interaction_kind: Mapped[str] = mapped_column(String(8))
    operation: Mapped[str | None] = mapped_column(Text)
    operation_id: Mapped[str | None] = mapped_column(Text)
    message_type: Mapped[str | None] = mapped_column(Text)
    identity_kind: Mapped[str] = mapped_column(String(32))
    identity_value: Mapped[str] = mapped_column(Text)
    target_key: Mapped[str | None] = mapped_column(Text)
    endpoint_path: Mapped[str | None] = mapped_column(Text)
    owner_domain: Mapped[str | None] = mapped_column(Text)
    device_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    capture_origin: Mapped[str] = mapped_column(String(8), default="LIVE")
    coverage: Mapped[str] = mapped_column(String(8), default="DECLARED")
    request_record_id: Mapped[UUID | None] = mapped_column(Uuid, record_fk())
    ack_record_id: Mapped[UUID | None] = mapped_column(Uuid, record_fk())
    current_response_record_id: Mapped[UUID | None] = mapped_column(Uuid, record_fk())
    command_code: Mapped[str | None] = mapped_column(String(36))
    call_key: Mapped[str | None] = mapped_column(Text)
    call_key_digest: Mapped[str | None] = mapped_column(String(64))
    input_raw: Mapped[bytes | None] = mapped_column(LargeBinary)
    task_type: Mapped[str | None] = mapped_column(Text)
    route: Mapped[dict | None] = mapped_column(JSON_VALUE)
    request_sha256: Mapped[str | None] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(Text)
    delivery_snapshot: Mapped[dict | None] = mapped_column(JSON_VALUE)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str | None] = mapped_column(String(32))
    delivery_status: Mapped[str | None] = mapped_column(String(16))
    dispatch_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hold_reason: Mapped[str | None] = mapped_column(String(64))
    blocked: Mapped[bool | None] = mapped_column(Boolean)
    needs_reconciliation: Mapped[bool | None] = mapped_column(Boolean)
    applied_result_id: Mapped[str | None] = mapped_column(String(64))
    receipt_id: Mapped[int | None] = mapped_column(Integer)
    receipt_active: Mapped[bool | None] = mapped_column(Boolean)
    raw_digest: Mapped[str | None] = mapped_column(String(64))
    effective_digest: Mapped[str | None] = mapped_column(String(64))
    compare_mode: Mapped[str | None] = mapped_column(String(9))
    status_code: Mapped[int | None] = mapped_column(Integer)
    resp_timestamp: Mapped[int | None] = mapped_column(BigInteger)
    event_envelope: Mapped[dict | None] = mapped_column(JSON_VALUE)
    event_context: Mapped[dict | None] = mapped_column(JSON_VALUE)
    application_ref: Mapped[dict | None] = mapped_column(JSON_VALUE)
    diagnostic: Mapped[list | None] = mapped_column(JSON_VALUE)


class IntegrationRecord(Base):
    __tablename__ = "integration_record"
    __table_args__ = (
        UniqueConstraint("exchange_id", "event_key", name="integration_record_event_key"),
        UniqueConstraint("exchange_id", "semantic_digest", name="integration_record_result_key"),
        UniqueConstraint("source_kind", "source_key", "source_version"),
        UniqueConstraint(
            "exchange_id", "attempt_id", "attempt_revision", name="integration_record_revision_key"
        ),
        CheckConstraint(
            "record_kind IN ('REQUEST_REGISTERED','SEND_ATTEMPT_OPENED',"
            "'HTTP_RESPONSE_OBSERVED','SEND_ATTEMPT_FINISHED','ECS_ATTEMPT_CONCLUSION',"
            "'CALLBACK_RECEIVED','RESPONSE_PREPARED','RESULT_EVIDENCE','RESULT_APPLIED',"
            "'RESULT_REJECTED','DELIVERY_TRANSITION','BUSINESS_CONSUMED','LEGACY_SNAPSHOT')",
            name="kind",
        ),
        CheckConstraint("direction IN ('INBOUND','OUTBOUND','INTERNAL')", name="direction"),
        CheckConstraint("capture_origin IN ('LIVE','BACKFILL')", name="capture_origin"),
        CheckConstraint("evidence_class IN ('CORE','DIAGNOSTIC')", name="evidence_class"),
        CheckConstraint(
            "body_state IN ('FULL','PREVIEW','LEGACY_REENCODED','UNAVAILABLE','PURGED')",
            name="body_state",
        ),
        CheckConstraint(
            "raw_body IS NULL OR (body_record_id IS NULL AND body_sha256 IS NOT NULL "
            "AND stored_bytes IS NOT NULL AND stored_bytes=length(raw_body) "
            "AND body_state IN ('FULL','PREVIEW','LEGACY_REENCODED'))",
            name="body",
        ),
        CheckConstraint("body_record_id IS NULL OR body_record_id<>id", name="body_reference"),
        CheckConstraint(
            "record_kind<>'RESULT_EVIDENCE' OR (semantic_digest IS NOT NULL "
            "AND effective_payload IS NOT NULL AND response_record_id IS NOT NULL "
            "AND applied IS NOT NULL)",
            name="result",
        ),
        CheckConstraint(
            "record_kind<>'ECS_ATTEMPT_CONCLUSION' OR (attempt_id IS NOT NULL "
            "AND attempt_revision > 0 AND ecs_attempt_outcome IS NOT NULL)",
            name="attempt",
        ),
        Index("integration_record_time_idx", "exchange_id", "recorded_at", "id"),
        Index("integration_record_attempt_idx", "exchange_id", "attempt_id", "record_kind"),
        Index("integration_record_retention_idx", "evidence_class", "occurred_at", "id"),
        *(
            Index(
                f"integration_record_{column}_idx",
                column,
                postgresql_where=text(f"{column} IS NOT NULL"),
                sqlite_where=text(f"{column} IS NOT NULL"),
            )
            for column in ("body_record_id", "response_record_id", "parent_record_id")
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    exchange_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey(
            f"{SCHEMA}.integration_exchange.id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    record_kind: Mapped[str] = mapped_column(String(32))
    evidence_class: Mapped[str] = mapped_column(String(10), default="CORE")
    event_key: Mapped[str] = mapped_column(String(256))
    attempt_id: Mapped[str | None] = mapped_column(String(64))
    parent_record_id: Mapped[UUID | None] = mapped_column(Uuid, record_fk())
    direction: Mapped[str] = mapped_column(String(8))
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    capture_origin: Mapped[str] = mapped_column(String(8), default="LIVE")
    http_status: Mapped[int | None] = mapped_column(Integer)
    network_state: Mapped[str | None] = mapped_column(String(32))
    protocol_verdict: Mapped[str | None] = mapped_column(String(32))
    protocol_code: Mapped[str | None] = mapped_column(Text)
    business_result: Mapped[str | None] = mapped_column(Text)
    application_state: Mapped[str | None] = mapped_column(Text)
    late: Mapped[bool | None] = mapped_column(Boolean)
    outcome_revision: Mapped[int | None] = mapped_column(BigInteger)
    raw_body: Mapped[bytes | None] = mapped_column(LargeBinary)
    body_record_id: Mapped[UUID | None] = mapped_column(Uuid, record_fk())
    body_sha256: Mapped[str | None] = mapped_column(String(64))
    stored_bytes: Mapped[int | None] = mapped_column(BigInteger)
    body_state: Mapped[str] = mapped_column(String(16), default="UNAVAILABLE")
    details: Mapped[dict | None] = mapped_column("metadata", METADATA_JSON)
    source_kind: Mapped[str | None] = mapped_column(String(64))
    source_key: Mapped[str | None] = mapped_column(Text)
    source_version: Mapped[str | None] = mapped_column(String(64))
    semantic_digest: Mapped[str | None] = mapped_column(String(64))
    effective_payload: Mapped[dict | None] = mapped_column(JSON_VALUE)
    response_record_id: Mapped[UUID | None] = mapped_column(Uuid, record_fk())
    applied: Mapped[bool | None] = mapped_column(Boolean)
    ecs_attempt_outcome: Mapped[str | None] = mapped_column(String(32))
    attempt_revision: Mapped[int | None] = mapped_column(Integer)


class IntegrationReference(Base):
    __tablename__ = "integration_reference"
    __table_args__ = (
        CheckConstraint(
            "(reference_kind='EXCHANGE' AND related_exchange_id IS NOT NULL) OR "
            "(reference_kind<>'EXCHANGE' AND related_exchange_id IS NULL)",
            name="related",
        ),
        Index(
            "integration_reference_source_key",
            "exchange_id",
            "reference_kind",
            "namespace",
            "value_digest",
            "relation_role",
            "source_record_id",
            unique=True,
            postgresql_where=text("source_record_id IS NOT NULL"),
            sqlite_where=text("source_record_id IS NOT NULL"),
        ),
        Index(
            "integration_reference_root_key",
            "exchange_id",
            "reference_kind",
            "namespace",
            "value_digest",
            "relation_role",
            unique=True,
            postgresql_where=text("source_record_id IS NULL"),
            sqlite_where=text("source_record_id IS NULL"),
        ),
        Index("integration_reference_lookup_idx", "reference_kind", "namespace", "value_digest"),
        Index("integration_reference_exchange_id_idx", "exchange_id"),
        *(
            Index(
                f"integration_reference_{column}_idx",
                column,
                postgresql_where=text(f"{column} IS NOT NULL"),
                sqlite_where=text(f"{column} IS NOT NULL"),
            )
            for column in ("source_record_id", "related_exchange_id")
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    exchange_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey(f"{SCHEMA}.integration_exchange.id", ondelete="RESTRICT")
    )
    reference_kind: Mapped[str] = mapped_column(String(32))
    namespace: Mapped[str] = mapped_column(Text)
    reference_value: Mapped[str] = mapped_column(Text)
    value_digest: Mapped[str] = mapped_column(String(64))
    relation_role: Mapped[str] = mapped_column(String(64))
    source_record_id: Mapped[UUID | None] = mapped_column(Uuid, record_fk())
    related_exchange_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey(f"{SCHEMA}.integration_exchange.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


event.listen(
    IntegrationExchange.__table__,
    "after_create",
    DDL(
        "CREATE TABLE IF NOT EXISTS _integration_receipt_sequence "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT)"
    ).execute_if(dialect="sqlite"),
    propagate=True,
)
event.listen(
    IntegrationExchange.__table__,
    "after_drop",
    DDL("DROP TABLE IF EXISTS _integration_receipt_sequence").execute_if(dialect="sqlite"),
    propagate=True,
)
