"""联调控制台的场景注册表:业务接入层用 `@dev_scenario` 登记,统一入口按它生成 Swagger 下拉。

只依赖 pydantic(业务包允许导入 core,但不得导入 fastapi);路由在 controllers/dev.py。
"""

import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, get_type_hints

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model
from sqlalchemy.ext.asyncio import AsyncSession

from .errors import ErrorCode
from .identifiers import uuid7

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


class DevRequest(BaseModel):
    """所有场景请求体的基类。"""

    model_config = ConfigDict(extra="forbid")

    client_request_id: str | None = Field(
        None, description="不传由服务端生成 uuid7;重复传同一个可验证幂等重放"
    )

    def request_id(self) -> str:
        return self.client_request_id or str(uuid7())


@dataclass(frozen=True)
class DevOutcome:
    business_key: str  # 状态查询用,对应 ReliableMessage.business_key
    client_request_id: str


type DevHandler = Callable[[AsyncSession, Any], Awaitable[DevOutcome]]


@dataclass(frozen=True)
class DevScenario:
    name: str
    title: str
    model: type[DevRequest]  # 已带 scenario 判别字段
    handler: DevHandler
    example: dict[str, Any]
    errors: dict[type[Exception], ErrorCode] = field(default_factory=dict)


_registry: dict[str, DevScenario] = {}


def _where(handler: DevHandler) -> str:
    return f"{handler.__module__}.{handler.__qualname__}"


def _request_type(handler: DevHandler) -> type[DevRequest]:
    if not inspect.iscoroutinefunction(handler):
        raise ValueError(f"{_where(handler)}: handler must be an async function")
    params = list(inspect.signature(handler).parameters.values())
    if len(params) != 2:
        raise ValueError(f"{_where(handler)}: handler must take (session, req)")
    model = get_type_hints(handler).get(params[1].name)
    if not (isinstance(model, type) and issubclass(model, DevRequest)):
        raise ValueError(f"{_where(handler)}: second parameter must be annotated with a DevRequest")
    if "scenario" in model.model_fields:
        raise ValueError(f"{_where(handler)}: request model must not define 'scenario'")
    return model


def dev_scenario(
    name: str,
    title: str,
    *,
    example: dict[str, Any],
    errors: dict[type[Exception], ErrorCode] | None = None,
):
    """登记一个场景;非法名字、重名、非法示例在导入期直接抛 ValueError。"""

    def decorate(handler: DevHandler) -> DevHandler:
        if not NAME_PATTERN.fullmatch(name):
            raise ValueError(f"{_where(handler)}: invalid scenario name {name!r}")
        if name in _registry:
            raise ValueError(
                f"duplicate dev scenario {name!r}: {_where(_registry[name].handler)} "
                f"and {_where(handler)}"
            )
        base = _request_type(handler)
        model = create_model(
            "Dev" + "".join(part.title() for part in re.split(r"[._]", name)),
            __base__=base,
            scenario=(Literal[name], name),
        )
        try:
            model.model_validate({"scenario": name, **example})
        except ValidationError as error:
            raise ValueError(f"{_where(handler)}: example is invalid: {error}") from None
        _registry[name] = DevScenario(name, title, model, handler, example, errors or {})
        return handler

    return decorate


def scenarios() -> list[DevScenario]:
    return [_registry[name] for name in sorted(_registry)]
