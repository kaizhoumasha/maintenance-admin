from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, PlainSerializer, field_validator

from app.schemas.query import FilterText

UtcTime = Annotated[
    datetime,
    PlainSerializer(
        lambda value: (
            value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
        ),
        return_type=str,
    ),
]


class HistoryParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(20, ge=1, le=100)
    cursor: str | None = Field(None, max_length=256)
    from_at: AwareDatetime | None = None
    until_at: AwareDatetime | None = None
    filter: FilterText = None

    @field_validator("*")
    @classmethod
    def valid_text(cls, value):
        if isinstance(value, str):
            value.encode("utf-8")
            if "\x00" in value:
                raise ValueError("invalid query text")
        return value


HttpInitiator = Literal["WES", "PEER"]


class ExchangeListParams(HistoryParams):
    peer_system: Literal["ECS", "WMS", "TRANSPORT"] | None = None
    direction: Literal["INBOUND", "OUTBOUND", "INTERNAL"] | None = None
    initiator: HttpInitiator | None = None
    operation: str | None = None
    device_code: str | None = None
    owner_domain: str | None = None
    identity_kind: str | None = None
    identity_value: str | None = None
    reference_kind: str | None = None
    reference_namespace: str | None = None
    reference_value: str | None = None

    @field_validator(
        "operation",
        "device_code",
        "owner_domain",
        "identity_kind",
        "identity_value",
        "reference_kind",
        "reference_namespace",
        "reference_value",
    )
    @classmethod
    def storage_text(cls, value):
        if value is not None:
            value.encode("utf-8")
            if not value or "\x00" in value:
                raise ValueError("invalid original identity")
        return value


class RecordListParams(HistoryParams):
    record_kind: str | None = None
    evidence_class: Literal["CORE", "DIAGNOSTIC"] | None = None


class _ExchangeSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    schema_version: int
    role: str
    peer_system: str | None
    protocol: str
    interaction_kind: str
    operation: str | None
    operation_id: str | None
    message_type: str | None
    identity_kind: str
    identity_value: str
    owner_domain: str | None
    device_code: str | None
    created_at: UtcTime
    recorded_at: UtcTime
    capture_origin: str
    coverage: str
    delivery: dict
    protocol_result: dict
    application: dict
    trace_id: str | None = None
    reason_code: str | None = None
    failure_kind: str | None = None
    close_reason: str | None = None
    late_response_at: UtcTime | None = None


class ExchangeSummary(_ExchangeSummary):
    direction: str


class HttpExchangeSummary(_ExchangeSummary):
    initiator: HttpInitiator


class _ExchangeDetail(_ExchangeSummary):
    command_code: str | None
    receipt_id: int | None
    receipt_active: bool | None
    request_record_id: UUID | None
    ack_record_id: UUID | None
    current_response_record_id: UUID | None
    references: list[dict]
    references_truncated: bool


class ExchangeDetail(_ExchangeDetail):
    direction: str


class HttpExchangeDetail(_ExchangeDetail):
    initiator: HttpInitiator


class _RecordSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    exchange_id: UUID
    record_kind: str
    evidence_class: str
    attempt_id: str | None
    parent_record_id: UUID | None
    occurred_at: UtcTime | None
    recorded_at: UtcTime
    capture_origin: str
    http_status: int | None
    network_state: str | None
    protocol_verdict: str | None
    protocol_code: str | None
    business_result: str | None
    application_state: str | None
    late: bool | None
    outcome_revision: int | None
    body_record_id: UUID | None
    body_state: str
    body_sha256: str | None
    stored_bytes: int | None
    source_kind: str | None
    source_key: str | None
    source_version: str | None
    response_record_id: UUID | None
    metadata: dict | None
    side: Literal["request", "response"] | None = None


class RecordSummary(_RecordSummary):
    direction: str


class HttpRecordSummary(_RecordSummary):
    initiator: HttpInitiator
