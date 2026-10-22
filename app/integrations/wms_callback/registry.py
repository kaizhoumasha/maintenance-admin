"""WMS 回调 operation 的注册表:通用接收只认「operation 名 → 处理器」,不认识任何业务。

沿用 `core/dev_scenarios.py` 的注册做法:重复注册报错并指出两个处理器;注册表是模块级全局状态,
测试用 `monkeypatch` 替换成空 dict 隔离。装饰器只注册映射,不带 `ack_mode` 等元数据(信封合同 §6)。
"""

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+@v[1-9][0-9]*$")
MAX_NAME_CHARS = 256


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


@dataclass(frozen=True)
class CallbackContext:
    session: AsyncSession
    receipt_id: int
    operation_id: str
    timestamp: int
    parsed: dict
    notes: list = field(default_factory=list)

    def note(self, reason_code: str, **detail) -> None:
        """附上诊断:返回 202 但没有(完全)应用的事实(未知任务、事实矛盾等),随收据同事务持久化。"""
        self.notes.append({"reason_code": reason_code, **detail})


_registry: dict[str, Any] = {}


def _where(handler: object) -> str:
    return f"{type(handler).__module__}.{type(handler).__qualname__}"


def callback_operation(name: str):
    """注册处理器类。类需提供 `parse(data) -> dict`(纯函数,非法抛 `InvalidPayload`)
    和 `async handle(ctx) -> Outcome`(在保存点内写业务行,不得自行 commit/rollback)。"""

    def register(cls):
        if len(name) > MAX_NAME_CHARS or not NAME_PATTERN.fullmatch(name):
            raise ValueError(f"invalid operation name {name!r}")
        for method in ("parse", "handle"):
            if not callable(getattr(cls, method, None)):
                raise TypeError(f"{cls.__qualname__} must define {method}()")
        if name in _registry:
            raise ValueError(
                f"duplicate operation {name!r}: {_where(_registry[name])} "
                f"and {cls.__module__}.{cls.__qualname__}"
            )
        _registry[name] = cls()
        return cls

    return register


def lookup(name: str):
    return _registry.get(name)


def operations() -> dict[str, Any]:
    return dict(_registry)
