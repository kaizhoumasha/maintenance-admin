"""向 WMS 发请求(本项目 → WMS)的 decisions 和 facts 两个端点,以及 operation 注册。

这个包是通用机制,目前没有任何具体 operation。每个 operation 先对照 WMS 合同
(wes_backend/docs/integration/wes-wms-interface-development-specification.md)逐个确认,
再单独添加。方向相反的机制是 `wms_callback`。搬运任务提交走同一个 WMS 地址,但属于
Transport 业务,在 `wms_transport`。

## 两个端点

| 端点 | 路径 | 协议名 | WMS 的成功响应 |
|---|---|---|---|
| facts | /api/v1/wes/facts | wms-facts | 200 RECORDED,重发 200 DUPLICATE |
| decisions | /api/v1/wes/decisions | wms-decisions | 200 DECIDED 或 202 PREPARE_ACCEPTED |

启用方式:在 `RELIABLE_TARGETS` 里为每个端点配一个 `protocol` 等于上表协议名的目标,
每个协议恰好一个。没配置就不会发送,`send` 会报错并说明缺哪个。适配器已在 `app/main.py` 注册。

## 它负责什么

- `operation.py`:operation 注册表。`@request_operation(名称, Endpoint.FACTS 或 DECISIONS)`
  登记,同名重复登记会报错。`RequestPayload` 禁止多余字段,与 WMS 拒绝未知字段的合同一致。
  JSON Schema 示例按严格 JSON 模式验证,不合法时在导入期报错。
- `service.py`:`WmsRequests.send(session, MyFact(...), operation_id=...)`。在调用方的事务里
  校验 UUIDv7 和 Payload,冻结信封(operation_id、operation、timestamp、data),再交给
  可靠层 `Outbox` 入队。不提交,不发网。
- `adapter.py`:`WmsEndpointAdapter` 按端点发 POST,并按合同 §4.4 的公共响应联合分类。
  - 成功码算已送达。
  - 合法的 503(空 data),以及只有 `retry_after_ms`(1 到 60000)的 429,算未送达,按原身份重试。
  - 合法的 409、422 是对端终局;公共错误 data 为闭集,`field_path` 仅 `INVALID_DATA` 可选。
  - 信封为四字段闭集;facts 和 `PREPARE_ACCEPTED` 的成功 data 必须为空,`DECIDED` 业务 data 不解释。
  - 其余一律算未知,继续重试。
  - 不属于本端点的消息会被拒绝。
- 合同 §4 的响应前置检查在 `app/integrations/wms_envelope.py`,Transport 适配器共用它。
- `app.integrations.discovery`:第一次读注册表时(`send`,或发送器构造请求时)，
  按 catalog 的显式声明导入 `request` 入口，让装饰器完成注册。

发送、租约、退避、TTL 和终态清理都在 `app/reliable`,这个包不重复实现。

## 怎么新增一个 operation

在 `app/businesses/<业务>/request/` 目录里加一个文件,一个 operation 一个文件。
在所属 composition.ENTRY_MODULES 列出入口包，并由 catalog.entries() 列出完整业务的 composition。
文件名是注册名把 `.` 和 `@` 换成 `_`,导入期检查两者一致。例如下面这个 operation 写在
`app/businesses/<业务>/request/<域>_<名称>_v1.py`:

    @request_operation("域.名称@v1", Endpoint.FACTS)
    class MyFact(RequestPayload):
        task_id: str

可选装饰器参数 title=、ttl= 声明标题及有效期；注册项直接保存模型与元数据。
完整接入步骤与注册清单排障见 docs/integrations/registration.md。

只有一两个很小的 operation 时,也可以用单个 `request.py`，
两种形式都须在所属 composition.ENTRY_MODULES 中显式列出。
调用方保存自己的业务记录和 `operation_id`(小写 canonical UUIDv7),在同一个事务里调用 `send`。

## 上线前要配置

facts 默认不设 TTL(事实不能丢),目标默认也不设 `max_not_received_attempts`。WMS 长期不可用或
一直返回无法解析的内容时,消息会按退避(上限默认 300 秒)无限重试,不会进入终态,只有
`alert_after_attempts` 的告警日志。上线前要在 `RELIABLE_TARGETS` 的对应目标上按现场约定设置
`max_not_received_attempts` 或 `fixed_retry_interval_s`,把告警接入通知渠道,并确认 WMS 的幂等记录
不过期(合同 REQ-COM-25),否则无 TTL 的重试没有安全前提。

## 本地联调

`ENVIRONMENT=local` 时联调控制台提供 operation 目录、触发入口、状态查询和能认识这两个端点的
Mock WMS,演示 operation 在 `dev.py`。用法和配置见 README 的「联调控制台」。

## 使用时要知道的限制

- 同一 `operation_id` 再次调用 `send` 时,Payload 相同返回原消息引用(时间戳不参与比较),
  Payload 不同抛 `DuplicateMessage`。所以重入口可以放心重复调用,但仍应复用已保存的身份。
- 默认不设 TTL,因为 facts 是已发生的事实,丢弃等于丢数据。Transport 提交的 1 小时 TTL
  是它自己的决定,不适用于这里。
- 使用 `get_result(session, message_id=..., operation=..., operation_id=...)` 读取原消息的
  投递状态和已验证终局响应;不会按当前目标改选,不返回重试响应或原始字节。
  DONE不表示业务完成,DECIDED的data仍由具体业务严格解析。缺失／清理不默认成功或重发。
  统一快照、消费恢复与保留边界见 docs/integrations/outbound-results.md。
- 分类器抛异常时消息会进入 `FAILED(LOCAL_ERROR)`,而对端可能已经处理。这是可靠层现有的
  策略,不是这个包新增的。
"""

from .adapter import ADAPTERS, DECISIONS_PROTOCOL, FACTS_PROTOCOL
from .operation import (
    Endpoint,
    InvalidRequestPayload,
    RequestPayload,
    UnknownOperation,
    request_operation,
)
from .results import WmsRequestResult
from .service import WmsRequests, wms_requests

__all__ = [
    "WmsRequestResult",
    "ADAPTERS",
    "DECISIONS_PROTOCOL",
    "FACTS_PROTOCOL",
    "Endpoint",
    "InvalidRequestPayload",
    "RequestPayload",
    "UnknownOperation",
    "WmsRequests",
    "wms_requests",
    "request_operation",
]
