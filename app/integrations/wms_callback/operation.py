"""写一个 callback operation 的基类:声明业务字段 + 实现 `execute`,其余由平台完成。

@callback_operation("域.名称@v1")
class MyOperation(CallbackOperation):
    title = "我的业务"                       # 可选,Swagger 示例标题
    class Payload(CallbackPayload):          # 业务字段;校验、规范化、示例都由它生成
        task_id: str
    async def execute(self, ctx, payload):   # 写业务;返回 dict/None 即接纳,或 raise Rejected(...)
        ...
"""

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, ValidationError

from .registry import Accepted, CallbackContext, InvalidPayload, Outcome

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


class CallbackOperation:
    title: ClassVar[str | None] = None
    # 对端要求在 ACK(含 409/422)里回显的 data 键,如 Transport 的 transport_task_id
    echo_keys: ClassVar[tuple[str, ...]] = ()
    Payload: ClassVar[type[CallbackPayload]]

    def __init__(self) -> None:
        payload = getattr(type(self), "Payload", None)
        if not (isinstance(payload, type) and issubclass(payload, BaseModel)):
            raise TypeError(
                f"{type(self).__qualname__} must define a Payload(CallbackPayload) class"
            )
        if type(self).execute is CallbackOperation.execute:
            raise TypeError(f"{type(self).__qualname__} must override execute()")

    @property
    def example(self) -> dict | None:
        examples = self.Payload.model_json_schema().get("examples")
        return examples[0] if examples else None

    def echo(self, data: dict) -> dict:
        """被拒绝的消息也尽量回显 `echo_keys`:非空、不超长、可按 UTF-8 编码的字符串。
        无认证入口不能把任意长度或无法渲染的内容原样写进收据再回给对端。"""
        return {key: data[key] for key in self.echo_keys if _echoable(data.get(key))}

    def parse(self, data: dict) -> dict:
        try:
            return self.Payload.model_validate(data).model_dump(mode="json")
        except ValidationError:
            raise InvalidPayload from None

    async def handle(self, ctx: CallbackContext) -> Outcome:
        # ctx.parsed 是自己的规范化输出,二次校验只为得到类型化对象,按宽松模式还原 JSON 值
        payload = self.Payload.model_validate(ctx.parsed, strict=False)
        result = await self.execute(ctx, payload)
        if result is None:
            return Accepted()
        if isinstance(result, dict):
            return Accepted(result)
        return result

    async def execute(
        self, ctx: CallbackContext, payload: CallbackPayload
    ) -> Outcome | dict | None:
        """在平台打开的事务内写业务。返回 `dict`(作为 `data` 回给对端)或 `None` 表示接纳;
        也可以 `return`/`raise` `Accepted/Duplicate/Conflict/Rejected`。"""
        raise NotImplementedError
