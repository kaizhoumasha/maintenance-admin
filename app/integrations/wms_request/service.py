import json
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import get_settings
from app.core.clock import unix_ms
from app.core.identifiers import validate_uuid7
from app.integrations import history as history_records
from app.integrations.model_payload import validated_model_payload
from app.models import IntegrationRecord, ReliableMessage
from app.reliable import EnqueueResult, MessageDraft, MessageStatus, Outbox
from app.reliable.config import parse_reliable_config, target_config_hint, targets_for_protocol
from app.reliable.types import REPLACEMENT_PENDING

from .adapter import ADAPTERS, PROTOCOL_OF
from .operation import InvalidRequestPayload, UnknownOperation, operations
from .results import WmsRequestResult, get_result


class WmsRequests:
    """在调用方事务内校验并冻结一条出站消息后入队;不提交、不发网。

    `operation_id` 由调用方生成并随业务记录保存;同一身份的重复调用必须复用原值。入队后请求字节
    即冻结:同身份、同 Payload 的重复调用返回原消息引用(时间戳不参与比较,由适配器的
    `same_request` 判定),同身份改换 Payload 抛 `DuplicateMessage`。
    """

    def __init__(
        self,
        outbox: Outbox | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        owner_domain: str | None = None,
        contract_consumers: Callable[[tuple[str, str, str | None]], frozenset[str]] | None = None,
    ):
        self._outbox, self.clock = outbox, clock
        self.owner_domain, self.contract_consumers = owner_domain, contract_consumers

    @property
    def outbox(self) -> Outbox:
        if self._outbox is None:
            targets = parse_reliable_config(get_settings()).targets
            self._outbox = Outbox(targets, ADAPTERS, clock=self.clock, records=history_records)
        return self._outbox

    def target(self, protocol: str, *, explicit: str | None = None) -> str:
        if explicit is not None:
            config = self.outbox.targets.get(explicit)
            if (
                config is None
                or config.protocol != protocol
                or not config.explicit_only
                or config.address_mode != "STATIC"
            ):
                raise ValueError(
                    f"Expected explicit-only static target {explicit!r} for {protocol}"
                )
            return explicit
        names = targets_for_protocol(self.outbox.targets, protocol)
        if len(names) != 1:
            raise RuntimeError(
                f"RELIABLE_TARGETS 需要恰好一个 protocol={protocol} 的目标,当前有 {names or '无'}"
                + target_config_hint(self.outbox.targets, protocol)
            )
        return names[0]

    async def get_result(
        self, session: AsyncSession, *, message_id: int, operation: str, operation_id: str
    ) -> WmsRequestResult:
        return await get_result(
            session, message_id=message_id, operation=operation, operation_id=operation_id
        )

    async def _lock_original(self, session, *, message_id, operation, operation_id):
        if self.owner_domain is None:
            raise InvalidRequestPayload("replacement requires an original business owner")
        return await session.scalar(
            select(ReliableMessage)
            .where(
                ReliableMessage.id == message_id,
                ReliableMessage.operation == operation,
                ReliableMessage.operation_id == operation_id,
                ReliableMessage.business_key == f"business:{self.owner_domain}",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def fence_replacement(self, session, *, message_id, operation, operation_id):
        """Prevent a fresh attempt once the original is known to be unaccepted."""
        old = await self._lock_original(
            session, message_id=message_id, operation=operation, operation_id=operation_id
        )
        if (
            old is not None
            and old.delivery_snapshot is None
            and old.status in (MessageStatus.PENDING, MessageStatus.SENDING)
            and old.pause_reason in (None, REPLACEMENT_PENDING)
        ):
            old.pause_reason = REPLACEMENT_PENDING
            if (
                old.status == MessageStatus.SENDING
                and old.lease_id is not None
                and not old.delivery_uncertain
                and old.attempts == old.not_received_count
            ):
                opened = await session.scalar(
                    select(IntegrationRecord.id)
                    .where(
                        IntegrationRecord.exchange_id == old.exchange_id,
                        IntegrationRecord.record_kind == "SEND_ATTEMPT_OPENED",
                        IntegrationRecord.attempt_id == old.lease_id,
                    )
                    .limit(1)
                )
                if opened is None:
                    # The sender and recovery both use the same message lock.
                    old.status = MessageStatus.PENDING
                    old.lease_id = old.lease_until = None
                    old.next_attempt_at = old.updated_at = self.clock()

    async def replace_unaccepted(
        self,
        session: AsyncSession,
        payload: BaseModel,
        *,
        message_id: int,
        operation: str,
        operation_id: str,
        replacement_operation_id: str,
    ) -> EnqueueResult | None:
        """Stop the original static delivery and enqueue its replacement atomically."""
        old = await self._lock_original(
            session, message_id=message_id, operation=operation, operation_id=operation_id
        )
        if (
            old is None
            or old.delivery_snapshot is not None
            or old.status
            not in (MessageStatus.PENDING, MessageStatus.STALLED, MessageStatus.EXPIRED)
            or old.lease_id is not None
            or old.lease_until is not None
            or old.delivery_uncertain
            or old.terminal_response_record_id is not None
            or old.attempts != old.not_received_count
        ):
            return None
        unexpected = await session.scalar(
            select(IntegrationRecord.id)
            .where(
                IntegrationRecord.exchange_id == old.exchange_id,
                IntegrationRecord.record_kind == "HTTP_RESPONSE_OBSERVED",
                or_(
                    IntegrationRecord.protocol_verdict.is_(None),
                    IntegrationRecord.protocol_verdict != "NOT_RECEIVED",
                ),
            )
            .limit(1)
        )
        if unexpected is not None:
            return None
        ref = await self.send(session, payload, operation_id=replacement_operation_id)
        new = await session.get(ReliableMessage, ref.message_id)
        if new.id == old.id or new.operation != old.operation or new.target != old.target:
            raise InvalidRequestPayload("replacement changed the original operation or target")
        now = self.clock()
        old.status = MessageStatus.ABANDONED
        old.pause_reason = "SUPERSEDED_BY_SCAN_CORRECTION"
        old.next_attempt_at = None
        old.updated_at = now
        record = await history_records.write_record(
            session,
            draft=history_records.RecordDraft(
                exchange_id=old.exchange_id,
                record_kind="DELIVERY_TRANSITION",
                event_key=f"SCAN_CORRECTION:{replacement_operation_id}",
                direction="INTERNAL",
                occurred_at=now,
                application_state="ABANDONED",
                details={"pause_reason": old.pause_reason, "request_id": replacement_operation_id},
            ),
        )
        await history_records.write_reference(
            session,
            exchange_id=old.exchange_id,
            reference_kind="EXCHANGE",
            namespace="integration",
            value=str(new.exchange_id),
            relation_role="SUPERSEDED_BY",
            created_at=now,
            source_record_id=record,
            related_exchange_id=new.exchange_id,
        )
        return ref

    async def send(
        self,
        session: AsyncSession,
        payload: BaseModel,
        *,
        operation_id: str,
        scope=None,
        explicit_target: str | None = None,
    ) -> EnqueueResult:
        if scope is not None and scope.owner_domain != self.owner_domain:
            raise InvalidRequestPayload("request scope owner mismatch")
        matches = [item for item in operations().values() if item.data_model is type(payload)]
        if len(matches) > 1:
            raise InvalidRequestPayload("payload model has multiple registered operations")
        if not matches:
            raise UnknownOperation(f"payload model {type(payload).__name__!r} is not registered")
        registered = matches[0]
        operation = registered.name
        shared = (
            not registered.registration_source.local_only
            and registered.registration_source.module.startswith("app.businesses._components.")
        )
        if self.owner_domain is not None:
            if self.contract_consumers is None or self.owner_domain not in self.contract_consumers(
                ("wms_request", operation, None)
            ):
                raise InvalidRequestPayload("business is not an authorized request consumer")
        elif shared:
            raise InvalidRequestPayload("shared formal request requires a bound business")
        elif registered.requires_business_owner:
            raise InvalidRequestPayload("request requires a bound business")
        business_key = None if self.owner_domain is None else "business:" + self.owner_domain
        try:
            validate_uuid7(operation_id)
            data = validated_model_payload(payload)
            raw = json.dumps(
                {
                    "operation_id": operation_id,
                    "operation": operation,
                    "timestamp": unix_ms(self.clock()),
                    "data": data,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except ValueError as error:  # 含 pydantic ValidationError 与 UnicodeError
            raise InvalidRequestPayload(str(error)) from None
        draft = MessageDraft(
            self.target(PROTOCOL_OF[registered.endpoint], explicit=explicit_target),
            operation,
            operation_id,
            raw,
            ttl=registered.ttl,
            business_key=business_key,
            scope=scope,
            owner_domain=self.owner_domain,
        )
        reference = await self.outbox.enqueue(session, draft)
        if reference.business_key != business_key:
            raise InvalidRequestPayload(
                "request identity already has a different original business"
            )
        return reference


# 外部调用的统一入口:无需任何接线,首次使用时从配置惰性取得 Outbox 与目标。
wms_requests = WmsRequests()
