"""WES → WMS 出站端点(decisions / facts)的 operation 注册:端点先建立,具体 operation 逐个确认后再加。

新增一个 operation:在 `app/integrations/<业务>/request.py`(自动发现)里

    @request_operation("域.名称@v1", Endpoint.FACTS)
    class MyFact(RequestPayload):            # 严格类型、字段闭集
        task_id: str

调用方再用 `WmsRequests.send(session, "域.名称@v1", {...}, operation_id=...)` 在自己的事务里入队。
"""

import json
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from functools import cached_property

from pydantic import BaseModel, ConfigDict

from app.integrations.registration import (
    RegistrationSource,
    operation_source,
)


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


@dataclass(frozen=True)
class Registered:
    name: str
    endpoint: Endpoint
    data_model: type[RequestPayload]
    title: str | None
    ttl: timedelta | None
    registration_source: RegistrationSource

    @cached_property
    def payload_schema(self) -> dict:
        return self.data_model.model_json_schema()

    @property
    def example(self) -> dict | None:
        examples = self.payload_schema.get("examples")
        return examples[0] if examples else None

    def describe(self) -> dict:
        """目录、联调下拉等所有展示的唯一来源,可直接序列化。"""
        ttl = self.ttl
        return {
            "name": self.name,
            "endpoint": self.endpoint.value,
            "title": self.title or self.name,
            "ttl_seconds": None if ttl is None else ttl.total_seconds(),
            "payload_schema": self.payload_schema,
            "example": self.example,
        }


_registry: dict[str, Registered] = {}


def request_operation(
    name: str,
    endpoint: Endpoint,
    *,
    title: str | None = None,
    ttl: timedelta | None = None,
):
    def register(cls):
        source = operation_source(name, cls)
        if not isinstance(endpoint, Endpoint):
            raise TypeError(
                f"{source.location}: endpoint must be an Endpoint; "
                "修复: 使用 Endpoint.FACTS 或 DECISIONS"
            )
        if name in _registry:
            raise ValueError(
                f"duplicate operation {name!r}: {_registry[name].registration_source.location} "
                f"and {source.location}; 修复: 删除重复声明或更正 operation，使每个名称只注册一次"
            )
        if not (isinstance(cls, type) and issubclass(cls, RequestPayload)):
            raise TypeError(f"{source.location}: 修复: 装饰 RequestPayload 模型")
        item = Registered(name, endpoint, cls, title, ttl, source)
        if (example := item.example) is not None:
            try:  # 示例不合法要在导入期暴露,不能等到有人打开 Swagger
                cls.model_validate_json(json.dumps(example, allow_nan=False))
            except (ValueError, TypeError) as error:
                raise ValueError(
                    f"{source.location}: example is invalid: {error}; "
                    "修复: 修改 Payload 的 json_schema_extra.examples，使其满足严格 JSON 字段约束"
                ) from None
        _registry[name] = item
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
    return dict(sorted(_loaded().items()))
