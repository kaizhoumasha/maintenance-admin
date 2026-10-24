"""示例 callback operation `example.echo@v1`:照着它就能写一个真实的 WMS 回调业务。

本文件名是 `dev_callback.py`,只在 `ENVIRONMENT=local` 被自动发现,生产不暴露;它不写业务表。
联调:在 Swagger 的 `POST /api/v1/wms/events` 选 `example.echo@v1` 示例,改 `data.outcome`
看四种响应。

新增一个真实 operation(通常只新建一个文件,不改控制器、不改 `main.py`)
=========================================================================
1. 新建 `app/integrations/<业务>/callback/<域>_<名称>_v1.py`
   (所有环境自动发现),照下面声明 Payload 和处理函数。
2. `@callback_operation("域.名称@vN", data=Payload)` 注册:小写字母/数字/下划线,
   点分层,`@v` 加版本号,如
   `transport.task.resulted@v1`;字段语义不兼容变化时升版本,不要在同一版本上悄悄改字段规则。
3. 用 `Payload(CallbackPayload)` 声明业务字段:类型校验、默认值、丢弃冗余字段、422 响应、Swagger
   示例(`json_schema_extra` 的 `examples`)都由它生成,不用手写校验。
4. 实现 `async def handler(ctx, payload)`,在里面写业务;`payload` 是类型化的 Payload 对象。

平台已经替你做了(handler 里不要重复):信封与大小校验、同一 `operation + operation_id` 的重试
(内容相同重放首次响应、不同返回 409,只有「第一次到达」的消息才会进入 handler)、收据与响应落库、
意外异常时整体回滚并返回 503。

handler 的约定
--------------
- 返回 `dict` 或 `None` = 接纳(202),dict 作为 `data` 回给对端。要提前结束,`raise` 或 `return`
  下面任一结果(在多深的调用里 raise 都行):
    Accepted(data)     202  业务事实已保存或已登记待后台处理
    Duplicate(data)    200  新消息身份,但业务上已经接收过(如同一任务的同一版本)
    Conflict(data)     409  与已保存的业务事实矛盾
    Rejected("原因码") 422  业务上不接受,handler 里写过的行会被平台回滚
- 写业务行用 `ctx.session`,**不要 commit/rollback**;`ctx.receipt_id` 可作业务行的外键,另有
  `ctx.operation_id`、`ctx.timestamp`。判定 Duplicate/Conflict 要在写业务行之前(先查,或用
  `INSERT ... ON CONFLICT DO NOTHING RETURNING` 抢唯一键,写法见
  `wms_transport/service.py::_register`)。
- 别吞数据库异常:任何异常都会整体回滚并返回 503,对端会原样重试。
- handler 只做校验和落库,要快。耗时的事(调其他系统、复杂计算)写一条待处理行,交给 Celery 扫描;
  不要在这里发 HTTP,也不要产生无法随事务回滚的外部副作用。

测试怎么写:参考 `tests/controllers/test_wms_events.py`,`monkeypatch` 隔离注册表后注册你的函数,
通过 HTTP 入口 POST 信封,断言状态码、响应体、业务行;只固化长期合同,不测私有函数。
"""

from typing import Literal

from pydantic import ConfigDict, Field

from .operation import CallbackPayload
from .registry import CallbackContext, Conflict, Duplicate, Rejected, callback_operation

OPERATION = "example.echo@v1"


class EchoPayload(CallbackPayload):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"message": "hello", "outcome": "accept"}]}
    )

    message: str = Field(min_length=1, max_length=200)
    outcome: Literal["accept", "duplicate", "conflict", "reject"] = "accept"


@callback_operation(OPERATION, data=EchoPayload, title="示例 operation(仅 local)")
async def echo(ctx: CallbackContext, payload: EchoPayload):
    result = {"echo": payload.message}
    # 真实业务在 ctx.session 写业务行;示例只演示四种结果。
    match payload.outcome:
        case "duplicate":
            raise Duplicate(result)
        case "conflict":
            raise Conflict(result)
        case "reject":
            raise Rejected("EXAMPLE_REJECTED")
    return result
