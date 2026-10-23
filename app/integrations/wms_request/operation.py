"""WES → WMS 出站端点(decisions / facts)的 operation 注册:端点先建立,具体 operation 逐个确认后再加。

新增一个 operation:在 `app/integrations/<业务>/request.py`(自动发现)里

    @request_operation("域.名称@v1", Endpoint.FACTS)
    class MyFact(RequestOperation):
        class Payload(RequestPayload):       # 字段闭集;WMS 拒绝未知字段,这里同样禁止多余键
            task_id: str

调用方再用 `WmsRequests.send(session, "域.名称@v1", {...}, operation_id=...)` 在自己的事务里入队。
"""

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from app.integrations.wms_callback.registry import MAX_NAME_CHARS, NAME_PATTERN


class Endpoint(StrEnum):
    DECISIONS = "decisions"
    FACTS = "facts"


class UnknownOperation(ValueError):
    pass


class InvalidRequestPayload(ValueError):
    pass


class RequestPayload(BaseModel):
    """严格按 JSON 类型校验,不接受多余字段(合同 REQ-COM-16)。"""

    model_config = ConfigDict(strict=True, extra="forbid")


class RequestOperation:
    Payload: ClassVar[type[RequestPayload]]
    # 默认不过期:事实是已发生的业务事实,丢弃等于丢数据;需要时效的 operation 自行声明
    ttl: ClassVar[timedelta | None] = None

    def __init__(self) -> None:
        payload = getattr(type(self), "Payload", None)
        if not (isinstance(payload, type) and issubclass(payload, BaseModel)):
            raise TypeError(
                f"{type(self).__qualname__} must define a Payload(RequestPayload) class"
            )


@dataclass(frozen=True)
class Registered:
    endpoint: Endpoint
    operation: RequestOperation


_registry: dict[str, Registered] = {}


def request_operation(name: str, endpoint: Endpoint):
    def register(cls):
        if len(name) > MAX_NAME_CHARS or not NAME_PATTERN.fullmatch(name):
            raise ValueError(f"invalid operation name {name!r}")
        if not isinstance(endpoint, Endpoint):
            raise TypeError("endpoint must be an Endpoint")
        if name in _registry:
            raise ValueError(
                f"duplicate operation {name!r}: {type(_registry[name].operation).__module__}."
                f"{type(_registry[name].operation).__qualname__} and "
                f"{cls.__module__}.{cls.__qualname__}"
            )
        _registry[name] = Registered(endpoint, cls())
        return cls

    return register


def lookup(name: str) -> Registered | None:
    return _registry.get(name)


def operations() -> dict[str, Registered]:
    return dict(_registry)
