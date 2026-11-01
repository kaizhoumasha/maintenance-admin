"""WMS 回调的数据模型与注册项;统一校验、回显及业务结果转换。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, ValidationError

from app.integrations.registration import RegistrationSource

from .registry import Accepted, CallbackContext, InvalidPayload, Outcome

if TYPE_CHECKING:
    from .receiver import CallbackResponse

ECHO_MAX_CHARS = 256


def _echoable(value: object) -> bool:
    if not isinstance(value, str) or not 0 < len(value) <= ECHO_MAX_CHARS:
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:  # 孤立代理字符无法渲染进 HTTP 响应
        return False
    return True


class CallbackPayload(BaseModel):
    """业务字段的声明式模型:严格按 JSON 类型校验(不把 "1" 当成 1),冗余字段被忽略。

    类型用 JSON 原生类型(str/int/float/bool/list/dict、Literal、嵌套模型);需要日期等写成 str
    再自行解析。要给 Swagger 一个示例,在模型里加
    `model_config = ConfigDict(json_schema_extra={"examples": [{...}]})`。
    """

    model_config = ConfigDict(strict=True, extra="ignore")


@dataclass(frozen=True)
class CallbackErrors:
    invalid_data: str
    invalid_envelope: str
    identity_conflict: str


@dataclass(frozen=True)
class Registered:
    func: Callable[[CallbackContext, BaseModel], Awaitable[Outcome | dict | None]]
    data_model: type[BaseModel]
    title: str | None
    echo_keys: tuple[str, ...]
    registration_source: RegistrationSource
    errors: CallbackErrors | None = None
    recheck: Callable[[CallbackContext, BaseModel, Mapping], Awaitable[Outcome | None]] | None = (
        None
    )
    archive_guard: (
        Callable[[CallbackContext, BaseModel], Awaitable[CallbackResponse | None]] | None
    ) = None

    @property
    def example(self) -> dict | None:
        examples = self.data_model.model_json_schema().get("examples")
        return examples[0] if examples else None

    def echo(self, data: dict) -> dict:
        """被拒绝的消息也尽量回显 `echo_keys`:非空、不超长、可按 UTF-8 编码的字符串。
        无认证入口不能把任意长度或无法渲染的内容原样写进收据再回给对端。"""
        return {key: data[key] for key in self.echo_keys if _echoable(data.get(key))}

    def parse(self, data: dict) -> dict:
        try:
            return self.data_model.model_validate(data).model_dump(mode="json")
        except ValidationError:
            raise InvalidPayload(
                self.errors.invalid_data if self.errors else "INVALID_EVIDENCE"
            ) from None

    async def handle(self, ctx: CallbackContext) -> Outcome:
        # ctx.parsed 是自己的规范化输出,二次校验只为得到类型化对象,按宽松模式还原 JSON 值
        payload = self.data_model.model_validate(ctx.parsed, strict=False)
        result = await self.func(ctx, payload)
        if result is None:
            return Accepted()
        if isinstance(result, dict):
            return Accepted(result)
        return result
