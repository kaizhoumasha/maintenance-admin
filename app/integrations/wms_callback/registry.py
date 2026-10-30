"""WMS 回调 operation 的注册表:通用接收只认「operation 名 → 处理器」,不认识任何业务。

沿用 `core/dev_scenarios.py` 的注册做法:重复注册报错并指出两个处理器;注册表是模块级全局状态,
测试用 `monkeypatch` 替换成空 dict 隔离。装饰器只注册映射,不带 `ack_mode` 等元数据(信封合同 §6)。
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.registration import (
    operation_source,
    validate_function,
)


class InvalidPayload(Exception):
    """`parse` 判定业务 DTO 非法;`reason_code` 进入 422 响应的 `data.reason_code`。"""

    def __init__(self, reason_code: str = "INVALID_EVIDENCE"):
        super().__init__(reason_code)
        self.reason_code = reason_code


class Outcome(Exception):
    """处理结果的基类。四种结果既可以 `return`,也可以在任意深的调用里 `raise`,效果相同。"""


class Accepted(Outcome):
    """202 RECEIVED:业务事实已保存(或已登记待后台处理),`data` 原样回给对端。"""

    def __init__(self, data: dict | None = None):
        super().__init__()
        self.data = data or {}


class Duplicate(Outcome):
    """200 DUPLICATE:新的消息身份,但业务上等于已接收过。需在写业务行之前决定。"""

    def __init__(self, data: dict | None = None):
        super().__init__()
        self.data = data or {}


class Conflict(Outcome):
    """409 CONFLICT:与已保存的业务事实矛盾。需在写业务行之前决定。"""

    def __init__(self, data: dict | None = None):
        super().__init__()
        self.data = data or {}


class Rejected(Outcome):
    """422 REJECTED:业务上不接受;平台会回滚处理器写入的所有行。"""

    def __init__(self, reason_code: str = "INVALID_EVIDENCE"):
        super().__init__(reason_code)
        self.reason_code = reason_code


OUTCOMES = (Accepted, Duplicate, Conflict, Rejected)


@dataclass
class _CallbackBusiness:
    business_code: str | None = None
    component_key: str | None = None


@dataclass(frozen=True)
class CallbackContext:
    session: AsyncSession
    receipt_id: int
    operation_id: str
    timestamp: int
    parsed: dict
    notes: list = field(default_factory=list)
    runtime: object | None = field(default=None, kw_only=True)
    registration_source: object | None = field(default=None, kw_only=True)
    operation: str | None = field(default=None, kw_only=True)
    _business_binding: _CallbackBusiness = field(default_factory=_CallbackBusiness, kw_only=True)

    @property
    def business_code(self) -> str | None:
        return self._business_binding.business_code

    @property
    def component_key(self) -> str | None:
        return self._business_binding.component_key

    def note(self, reason_code: str, **detail) -> None:
        """附上诊断:返回 202 但没有(完全)应用的事实(未知任务、事实矛盾等),随收据同事务持久化。"""
        self.notes.append({"reason_code": reason_code, **detail})


if TYPE_CHECKING:
    from .operation import Registered


_registry: dict[str, "Registered"] = {}


def callback_operation(
    name: str,
    *,
    data: type[BaseModel],
    title: str | None = None,
    echo_keys: tuple[str, ...] = (),
):
    """注册 async handler(ctx, payload);返回原函数，Payload 校验及结果转换由注册项完成。"""

    def register(target):
        source = operation_source(name, target)
        if name in _registry:
            raise ValueError(
                f"duplicate operation {name!r}: {_registry[name].registration_source.location} "
                f"and {source.location}; 修复: 删除重复声明或更正 operation，使每个名称只注册一次"
            )
        from .operation import Registered

        validate_function(target, data)
        _registry[name] = Registered(target, data, title, echo_keys, source)
        return target

    return register


def lookup(name: str) -> "Registered | None":
    return _registry.get(name)


def operations() -> dict[str, "Registered"]:
    return dict(_registry)
