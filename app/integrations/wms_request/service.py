import json
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import get_settings
from app.core.clock import unix_ms
from app.core.identifiers import validate_uuid7
from app.core.strict_json import canonical_digest, json_object
from app.models import ReliableMessage
from app.reliable import DuplicateMessage, EnqueueResult, MessageDraft, MessageStatus, Outbox
from app.reliable.config import parse_reliable_config

from .adapter import ADAPTERS, PROTOCOL_OF
from .operation import InvalidRequestPayload, UnknownOperation, lookup


class WmsRequests:
    """在调用方事务内校验并冻结一条出站消息后入队;不提交、不发网。

    `operation_id` 由调用方生成并随业务记录保存;同一身份的重复调用必须复用原值。入队后请求字节
    即冻结:同身份、同 Payload 的重复调用返回原消息引用(时间戳不参与比较),同身份改换 Payload
    抛 `DuplicateMessage`。
    """

    def __init__(
        self,
        outbox: Outbox | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._outbox, self.clock = outbox, clock

    @property
    def outbox(self) -> Outbox:
        if self._outbox is None:
            targets = parse_reliable_config(get_settings()).targets
            self._outbox = Outbox(targets, ADAPTERS, clock=self.clock)
        return self._outbox

    def target(self, protocol: str) -> str:
        names = [name for name, item in self.outbox.targets.items() if item.protocol == protocol]
        if len(names) != 1:
            raise RuntimeError(
                f"RELIABLE_TARGETS 需要恰好一个 protocol={protocol} 的目标,当前有 {names or '无'}"
            )
        return names[0]

    async def send(
        self,
        session: AsyncSession,
        operation: str,
        data: dict,
        *,
        operation_id: str,
    ) -> EnqueueResult:
        registered = lookup(operation)
        if registered is None:
            raise UnknownOperation(f"operation {operation!r} is not registered")
        try:
            validate_uuid7(operation_id)
            payload = registered.operation.Payload.model_validate(data).model_dump(mode="json")
            raw = json.dumps(
                {
                    "operation_id": operation_id,
                    "operation": operation,
                    "timestamp": unix_ms(self.clock()),
                    "data": payload,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except ValueError as error:  # 含 pydantic ValidationError 与 UnicodeError
            raise InvalidRequestPayload(str(error)) from None
        draft = MessageDraft(
            self.target(PROTOCOL_OF[registered.endpoint]),
            operation,
            operation_id,
            raw,
            ttl=registered.operation.ttl,
        )
        try:
            return await self.outbox.enqueue(session, draft)
        except DuplicateMessage:
            # 同身份重入:时间戳每次不同,所以只在 Payload 相同时返回原引用,否则才是真冲突
            existing = await session.scalar(
                select(ReliableMessage)
                .where(
                    ReliableMessage.target == draft.target,
                    ReliableMessage.operation == operation,
                    ReliableMessage.operation_id == operation_id,
                )
                .execution_options(populate_existing=True)
            )
            sent = json_object(existing.request_raw) if existing is not None else None
            if sent is None or canonical_digest(sent.get("data")) != canonical_digest(payload):
                raise
            return EnqueueResult(existing.id, MessageStatus(existing.status))


# 外部调用的统一入口:无需任何接线,首次使用时从配置惰性取得 Outbox 与目标。
wms_requests = WmsRequests()
