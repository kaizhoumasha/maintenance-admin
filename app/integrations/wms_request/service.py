import json
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import get_settings
from app.core.clock import unix_ms
from app.core.identifiers import validate_uuid7
from app.reliable import EnqueueResult, MessageDraft, Outbox
from app.reliable.config import parse_reliable_config, target_config_hint, targets_for_protocol

from .adapter import ADAPTERS, PROTOCOL_OF
from .operation import InvalidRequestPayload, UnknownOperation, lookup
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
    ):
        self._outbox, self.clock = outbox, clock

    @property
    def outbox(self) -> Outbox:
        if self._outbox is None:
            targets = parse_reliable_config(get_settings()).targets
            self._outbox = Outbox(targets, ADAPTERS, clock=self.clock)
        return self._outbox

    def target(self, protocol: str) -> str:
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
            payload = registered.data_model.model_validate(data).model_dump(mode="json")
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
            ttl=registered.ttl,
        )
        return await self.outbox.enqueue(session, draft)


# 外部调用的统一入口:无需任何接线,首次使用时从配置惰性取得 Outbox 与目标。
wms_requests = WmsRequests()
