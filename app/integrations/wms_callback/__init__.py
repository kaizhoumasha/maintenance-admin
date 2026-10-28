"""接收 WMS 主动发来的回调(WMS → 本项目),入口是 `POST /api/v1/wms/events`。

这个包是通用机制,只认识信封和 operation 名,不认识任何业务。
领域业务事件写在 `app/businesses/<业务>/callback/` 里，
由所属 composition.ENTRY_MODULES 显式列出，composition 由 catalog.entries() 拥有后启动导入。
Transport 固定事件仍在 wms_transport.callback。
方向相反的机制是 `wms_request`。

## 它负责什么

- 路由 `controllers/wms_events.py`:最多读 256 KiB,超限回 413。每个请求在一个数据库事务里
  调用 `receive`。
- `receive`(receiver.py):校验信封,按 `(operation, operation_id)` 用 `callback_receipt` 做幂等。
  响应码如下。
  - 400:body 不是 JSON,或没有 UUIDv7 的 `operation_id`。
  - 422:未知 operation 或 DTO 不合法。
  - 202 `RECEIVED`:处理成功。
  - 200 `DUPLICATE`:同身份、同内容重发。
  - 409:同身份、不同内容。
  - 503:处理器抛出普通异常,或 PostgreSQL 行锁等待超过 5 秒。整个事务回滚,WMS 应用原身份重发。
- 内容比较先比原文摘要,再比 DTO 规范化后的摘要,所以对端多发的冗余字段不会被当成内容变了。
- 处理器在保存点里执行。返回 Rejected 时它写的行全部回滚,收据里仍保存这次拒绝,
  重发会得到同样的拒绝。
- 快照(snapshots.py):请求体最多保存前 48 KiB 作预览,每分钟最多写 120 条,在后台任务里写,
  失败只记日志,不影响响应。按 `CALLBACK_SNAPSHOT_RETENTION_DAYS` 清理。
- 收据按 `RELIABLE_RETENTION_DAYS` 每天清理。超期后同一身份再来会被当作新消息。

## 它不负责什么

业务规则、事实顺序、重试(由 WMS 重发)和来源认证。当前合同的认证是 NONE,入口只靠网关
限制来源。

## 怎么新增一个 operation

在 `app/businesses/<业务>/callback/` 新建文件，声明 Payload 和异步处理函数，
并在所属 composition.ENTRY_MODULES 中列出入口，composition 由 catalog.entries() 拥有。
文件名统一为 name.lower().replace('.', '_').replace('@', '_') + '.py'，导入期校验。
例如 `transport.task.resulted@v1` 对应 `transport_task_resulted_v1.py`：

    class Done(CallbackPayload):
        task_id: str

    @callback_operation("example.done@v1", data=Done, echo_keys=("task_id",))
    async def done(ctx: CallbackContext, payload: Done):
        return {"task_id": payload.task_id}

可选 title= 声明 Swagger 标题。注册项直接保存函数和模型，Transport 业务共用普通函数。
完整接入步骤与注册清单排障见 docs/integrations/registration.md。

结果类型是 Accepted、Duplicate、Conflict、Rejected,`return` 和 `raise` 效果相同。
写处理函数要注意:

- 不要自己 commit 或 rollback。需要留下的诊断用 `ctx.note(reason_code, ...)`,它随收据
  同事务保存,提交后输出告警日志。
- 对已通过 Payload 校验的消息不要抛异常。冲突、未知任务这类业务情况用返回值表达(通常仍回
  202)。抛异常会让这条消息永远回 503,变成越不过去的毒消息。
- Duplicate 和 Conflict 要在写业务行之前决定,因为它们的写入不会回滚。
- Swagger 示例:给 Payload 加
  `model_config = ConfigDict(json_schema_extra={"examples": [...]})`。
- 只有一两个很小的 operation 时,也可以用单个 `callback.py`，
  两种形式都须在所属 composition.ENTRY_MODULES 中显式列出。
- `dev_callback.py` 只在 `ENVIRONMENT=local` 导入,放联调样例。

注册表是模块级全局状态,同名重复注册会报错并指出双方来源与修复方法。
测试里用 `monkeypatch` 换成空 dict。
Transport 的两个 operation 见 `wms_transport/callback/` 和
docs/superpowers/specs/2026-10-22-transport-callback-design.md。
"""

from .operation import CallbackPayload
from .receiver import MAX_BODY_BYTES, CallbackResponse, receive, unavailable
from .registry import (
    Accepted,
    CallbackContext,
    Conflict,
    Duplicate,
    InvalidPayload,
    Outcome,
    Rejected,
    callback_operation,
)

__all__ = [
    "MAX_BODY_BYTES",
    "Accepted",
    "Conflict",
    "Duplicate",
    "CallbackContext",
    "CallbackPayload",
    "CallbackResponse",
    "InvalidPayload",
    "Outcome",
    "Rejected",
    "callback_operation",
    "receive",
    "unavailable",
]
