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
from functools import cached_property
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
    """严格按 JSON 类型校验,不接受多余字段(合同 REQ-COM-16)。

    字段尽量用 JSON 原生类型(str、int、float、bool、list、dict、Literal、嵌套模型)。datetime 等写成
    str 再自行解析最省事:直接调用 `send` 传 Python 对象可以通过校验,但联调界面的 JSON 请求体要先
    把 ISO 字符串校验成对象,两条路径都能走通的前提是类型能经过 JSON 往返。
    """

    model_config = ConfigDict(strict=True, extra="forbid")


class RequestOperation:
    title: ClassVar[str | None] = None  # Swagger 里的标题,缺省用 operation 名
    Payload: ClassVar[type[RequestPayload]]
    # 默认不过期:事实是已发生的业务事实,丢弃等于丢数据;需要时效的 operation 自行声明
    ttl: ClassVar[timedelta | None] = None

    def __init__(self) -> None:
        payload = getattr(type(self), "Payload", None)
        if not (isinstance(payload, type) and issubclass(payload, BaseModel)):
            raise TypeError(
                f"{type(self).__qualname__} must define a Payload(RequestPayload) class"
            )

    @cached_property
    def payload_schema(self) -> dict:
        return self.Payload.model_json_schema()

    @property
    def example(self) -> dict | None:
        examples = self.payload_schema.get("examples")
        return examples[0] if examples else None


@dataclass(frozen=True)
class Registered:
    name: str
    endpoint: Endpoint
    operation: RequestOperation

    def describe(self) -> dict:
        """目录、联调下拉等所有展示的唯一来源,可直接序列化。"""
        ttl = self.operation.ttl
        return {
            "name": self.name,
            "endpoint": self.endpoint.value,
            "title": self.operation.title or self.name,
            "ttl_seconds": None if ttl is None else ttl.total_seconds(),
            "payload_schema": self.operation.payload_schema,
            "example": self.operation.example,
        }


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
        operation = cls()
        if (example := operation.example) is not None:
            try:  # 示例不合法要在导入期暴露,不能等到有人打开 Swagger
                operation.Payload.model_validate(example)
            except ValueError as error:
                raise ValueError(f"{cls.__qualname__}: example is invalid: {error}") from None
        _registry[name] = Registered(name, endpoint, operation)
        return cls

    return register


def _loaded() -> dict[str, Registered]:
    # 读注册表前保证业务包的 request 模块已导入:发送器重启后可能先于任何 `send` 就要读它
    from .discovery import load_operation_modules

    load_operation_modules()
    return _registry


def lookup(name: str) -> Registered | None:
    return _loaded().get(name)


def operations() -> dict[str, Registered]:
    return dict(_loaded())
