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
- `service.py`:`WmsRequests.send(session, 名称, 数据, operation_id=...)`。在调用方的事务里
  校验 UUIDv7 和 Payload,冻结信封(operation_id、operation、timestamp、data),再交给
  可靠层 `Outbox` 入队。不提交,不发网。
- `adapter.py`:`WmsEndpointAdapter` 按端点发 POST,并按合同 §4.4 的公共响应联合分类。
  - 成功码算已送达。
  - 503,以及带 `retry_after_ms`(1 到 60000)的 429,算未送达,按原身份重试。
  - 合法的 409、422 是对端终局。
  - 其余一律算未知,继续重试。
  - 不属于本端点的消息会被拒绝。
- `discovery.py`:第一次 `send` 时导入各业务包的 `request.py`,让装饰器完成注册。

发送、租约、退避、TTL 和终态清理都在 `app/reliable`,这个包不重复实现。

## 怎么新增一个 operation

在 `app/integrations/<业务>/request.py` 里:

    @request_operation("域.名称@v1", Endpoint.FACTS)
    class MyFact(RequestOperation):
        ttl = None                              # 可选;默认不过期
        class Payload(RequestPayload):
            task_id: str

调用方保存自己的业务记录和 `operation_id`(小写 canonical UUIDv7),在同一个事务里调用 `send`。

## 使用时要知道的限制

- 同一 `operation_id` 再次入队时,内容有任何变化(包括时间戳)都会抛 `DuplicateMessage`。
  重入口先读自己的业务记录,复用已有身份,不要重新生成。
- 默认不设 TTL,因为 facts 是已发生的事实,丢弃等于丢数据。Transport 提交的 1 小时 TTL
  是它自己的决定,不适用于这里。
- 业务现在读不到 WMS 的响应。可靠层只把响应原文存在消息行里,没有公开的读取接口,
  这和 Transport 的结果消费是同一个缺口。需要当场使用决定结果的 decisions operation,
  在确认它时再决定调用方式(同步直调,或补读取接口),不要绕过可靠层去读表。
- 失败反馈同理:消息进入 `FAILED` 或 `EXPIRED` 后,业务侧目前只能靠告警日志和运维排查发现。
- 分类器抛异常时消息会进入 `FAILED(LOCAL_ERROR)`,而对端可能已经处理。这是可靠层现有的
  策略,不是这个包新增的。
"""

from .adapter import ADAPTERS, DECISIONS_PROTOCOL, FACTS_PROTOCOL
from .operation import (
    Endpoint,
    InvalidRequestPayload,
    RequestOperation,
    RequestPayload,
    UnknownOperation,
    request_operation,
)
from .service import WmsRequests

__all__ = [
    "ADAPTERS",
    "DECISIONS_PROTOCOL",
    "FACTS_PROTOCOL",
    "Endpoint",
    "InvalidRequestPayload",
    "RequestOperation",
    "RequestPayload",
    "UnknownOperation",
    "WmsRequests",
    "request_operation",
]
