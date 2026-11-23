# 安全日志诊断

`app/diagnostics.py` 是 HTTP、可靠发送器、维护与 Celery 共用的安全异常诊断入口。它不读取配置、不访问数据库、不管理任务状态，也不改变异常传播或重试。

## 记录异常

```python
import logging

from app.diagnostics import log_exception

logger = logging.getLogger(__name__)

try:
    perform_work()
except Exception as error:
    log_exception(
        logger,
        "work failed",
        error,
        component="worker",
        phase="execute",
        context={"task_id": task_id},
    )
    raise
```

消息使用固定描述；不要拼入异常原文、请求体、响应体、SQL、认证头或凭据。捕获、回滚、返回响应和重试仍由原调用者负责；仅为了记日志不能吞掉异常或额外发起重试。

统一字段为 `component`、`phase`、`error_type`、`origin`，以及可用时的 `driver_type`、`sqlstate`、`constraint`、`missing`、`hint`。字段同时进入可读消息和 LogRecord，方便按组件、阶段及错误码检索。项目外抛错且没有项目栈帧时 `origin=unavailable`；安全堆栈仍保留文件、行号和函数。

数据库只输出校验后的 SQLSTATE 与受限 ASCII 对象名；标准 PostgreSQL 缺列/缺表错误包含迁移提示。不打印 SQL、参数、原始驱动消息、异常链、源码或局部变量。`DevFormatter` 对外部 `exc_info` 也使用相同的安全堆栈，避免输出终端时回显源码或异常消息。

`context` 只接受 `task`、`task_id`、`message_id`、`target`、`source`、`request_id`、`retry_in_s`、`method`、`route`、`src`、`limit` 的标量值；其他字段和嵌套对象丢弃，不允许覆盖诊断字段。允许字段的值仍须来自可信元数据，不能将凭据或业务正文冒充标识符。

## 各异常出口

- HTTP 技术异常、数据库/Redis 异常与 5xx 应用异常共用安全诊断，响应状态、包络及 trace_id 保持原映射。4xx 业务/参数校验日志保持原规则。
- ECS Event、ECS Result、WMS 回调接收失败仍使用原协议的 503；后台快照写入失败不改变已提交 ACK。
- 审计旁路失败仍不影响业务成功；限流连接/超时仍 fail-open，脚本错误仍暴露。
- Sender、逐条快照维护与领取隔离错误附带阶段及可用的消息标识，不改变锁、事务、租约、投递或未知交付处理。
- 单条 STATIC/SNAPSHOT 投递的原吞获异常记录固定消息 `reliable delivery failed`，`component=reliable`，上下文仅 message_id/target。阶段为 prepare、build_request、acquire_client（取得/释放）、before_send、send、classify_response、apply_outcome；仍生成原状态和证据。PoolUnavailable 不升格，取消继续传播，store.finish 失败仍由原 Sender 收口；不扩宽捕获范围，不叠加降噪。
- Celery 通过 `task_failure` / `task_retry` 信号记录原始异常的安全诊断。预期异常仍为 INFO，非预期失败为 ERROR，重试为 INFO。框架默认含参数/原始异常的重复记录被过滤；worker 其他异常记录转换为安全诊断。任务结果、重试、ACK/requeue、result backend 和事件协议均不改写；日志脱敏不代表结果后端或事件数据也已脱敏。

## 降噪与恢复

通用模块无全局故障计数器。Sender 的计数、60 秒摘要和完整扫描成功后的恢复判定仍属于各 Sender 实例；摘要保存首次安全诊断，不保留异常对象。满并发跳过领取不算恢复，取消不算失败，恢复不代表交付或设备执行成功。

维护和 Celery 独立任务不因其他任务成功而报恢复，也不套用 Sender 的错误合并规则；不同消息的异常逐条保留。新增循环任务若需降噪，须由其自身明确故障分组、成功条件及生命周期，不能用一个全局成功事件清除所有故障。

## 本地验证

SQLite/伪 Redis 可验证 HTTP 响应与安全输出；并发、事务及迁移使用显式一次性 PostgreSQL/Redis。真实 Celery worker 回归要求 `TEST_REDIS_URL` 指向一次性实例，通过独立队列和 solo worker 验证失败传播及实际重试，不连接开发 broker。

日志中的缺列/缺表提示只是修复路标，不自动迁移数据库。Sender 仍须先停用，再按版本切换流程升级数据库。
