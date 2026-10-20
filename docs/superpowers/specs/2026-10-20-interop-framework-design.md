# SPEC-019 非业务交互基础能力:分层与边界

状态:**v5(结构重组)** · 分支:`feature/spec-019-interop-framework` · 承接:无

> 本文只说明分层、边界与能力状态;HttpClient 与可靠交互层各有独立文档,互不引用。迁移的条款保持 REQ 编号不变,v4 全文见 git 历史。

## 1. 分层

```text
业务域 handler / 类型化 client                          业务域接入,各自独立
        │
协议适配器   各业务域对接时自带(WMS/Transport…)
        │  ProtocolAdapter 端口
可靠交互核心  app/reliable   首版:事务发件箱 · 持久化重试 · 租约 · 崩溃恢复 · 尝试记录(出站优先)
        │ DI
HttpClient   app/http_client 一次 HTTP 请求(已实现)
```

| 层 | 内容 | 状态 |
|---|---|---|
| HttpClient | 单次异步 HTTP、超时、连接池、大小限制、交付事实分类 | **已实现并复审通过**(首期交付);出口限流后移 |
| 可靠交互层 | 协议无关的出站可靠投递(首版);同步调用、去重窗口校验、调试 API 已设计但后移 | 已实现并完成离线 PostgreSQL 验证；未启用真实目标、未部署 |

具体协议(信封、operation 格式、`code` 词表等)**不在基础能力内**:待有具体对接合同后,再由对应业务域设计协议适配器。

## 2. 能力状态矩阵

图例:✅ 已实现(有代码与测试)· 📐 设计未实施 · ⏸ 已后移或移出范围。首版/可后移仅用于 📐 项。

| 层 | 能力 | 状态 | 代码 / 测试位置 | 首版 / 可后移 |
|---|---|---|---|---|
| HttpClient | 单次发送、原字节、不重试不重定向(REQ-HTTP-01/02) | ✅ | `app/http_client/client.py`;`tests/http_client/test_request_validation.py`、`test_result_mapping.py`、`test_network_behavior.py` | — |
| HttpClient | 阶段与总超时、连接池、关闭与取消(REQ-HTTP-03/04/10) | ✅ | 同上;`test_network_behavior.py` | — |
| HttpClient | 请求/响应大小限制、identity 编码(REQ-HTTP-05/06) | ✅ | `test_response_body.py`、`test_request_validation.py` | — |
| HttpClient | 交付事实分类(REQ-HTTP-09) | ✅ | `test_result_mapping.py` | — |
| HttpClient | 公开请求校验函数 `validate_request(config, request)`(REQ-HTTP-13),仅依赖配置,与 `send` 复用规则 | ✅ | `validation.py`、公开校验一致性与无客户端测试 | — |
| HttpClient | 导入隔离与脱敏日志(REQ-HTTP-11) | ✅ | `test_import_isolation.py`、`test_logging.py`、`tests/architecture/test_layering.py`；包含禁止反向依赖 `app.reliable` | — |
| HttpClient | 出口并发/速率限流(REQ-HTTP-07) | ⏸ | — | — |
| HttpClient | 常驻事件循环与应用装配(REQ-HTTP-08/12) | ✅ | `reliable_runtime.py`；空目标仍扫描、零客户端；双 API/专用池测试 | — |
| 可靠交互层 | 事务发件箱、持久化退避重试、过期 | ✅ | `app/reliable`；`test_enqueue.py`、`test_delivery.py`、`test_leases.py` | 首版 |
| 可靠交互层 | 租约领取、崩溃恢复、条件回写 | ✅ | `store.py`；真实 PG 并发、两阶段子进程骤停测试 | 首版 |
| 可靠交互层 | 消息行尝试记录与结构化状态日志、终态消息保留期清理、执行预算、发送器 lifespan 载体 | ✅ | `test_retention.py`、`test_sender.py`、`test_runtime.py`、迁移及 Celery 清理测试 | 首版 |
| 可靠交互层 | 同步调用 `call`、去重窗口校验 | 📐 设计保留 | — | 后移 |
| 可靠交互层 | 调试查询 API、证据导出、状态事件表与折叠 | 📐 设计保留 | — | 后移 |
| 可靠交互层 | `shadow`/`step` 审批模式、运行期暂停开关 | ⏸ | 已移出范围 | — |
| 业务域接入 | WMS / Transport 等 | ⏸ | 另立范围 | — |

## 3. 边界

- **依赖方向**:`app/reliable → app/http_client`,逆向导入一律禁止;`app/http_client` 不得导入 `app/reliable`、业务域、`app.core`、数据库、Redis、Celery。
- **协议适配不在通用能力里**:核心只存取字节,并通过 ProtocolAdapter 端口询问"请求怎么构造、响应如何分类"。具体协议由各业务域在有对接合同后自带适配器,不增加通用映射 DSL。
- **业务能力独立**:业务域拥有自己的 payload、身份关联与业务/物理结果解释;已有可靠任务可直接注入 HttpClient,不强制经过可靠交互层,但**不得叠加第二个调度器**。
- **安全后移**:鉴权、签名、来源识别、脱敏另立范围;HTTPS 始终做标准证书校验。
- **出口限流后移**,建议在可靠交互层发送器上线前补齐。
- 不删除或修改 wes_backend、maintenance-agent 或本项目已有业务代码。

## 4. 开放问题与代码侧待办

| # | 问题 | 当前结论 |
|---|---|---|
| Q10 | 不具备去重能力的存量对端如何处理 | **开放**:现有设计要求对端必须去重;业务域接入如需接入不可改造对端,另定目标级策略 |

- **实施记录**：分层/导入测试已改为禁止 `app.reliable` 反向依赖；公开校验、可靠首版与验证记录见 `docs/superpowers/plans/2026-10-21-reliable-interaction-plan.md`。安全、限流与业务域接入仍未实施。

## 5. 评审记录(精简)

逐条历史记录见 git 历史。以下只保留仍然有效的决策:

| 决策 | 内容 |
|---|---|
| 安全后移 | 鉴权、来源识别、脱敏、原文保存策略另立范围;HTTPS 始终做标准证书校验 |
| 基础与业务分离 | 基础层不知道任何业务域;业务域各自拥有 payload、handler、表和权限码;不选定首个业务域 |
| HttpClient 先行 | 先交付只做单次 HTTP 的纯客户端;幂等、持久化重试在可靠交互层;出口限流后移 |
| 出站优先 | 可靠层只做出站;入站和 completion 不在可靠层,由业务域自己的 router/service 承担;同步调用、去重窗口校验、调试 API、事件表均后移(条文保留) |
| 无人工恢复入口 | 不提供人工裁决、重发、改状态入口,也没有 `DEAD` 状态;重试不设终止次数,只告警;出路只有重试续送和 `expires_at` |
| 对端必须按身份去重 | 不设对账协议;送达未知时一律用原身份重发;`build_request` 必须把稳定标识带在请求里 |
| 发送器载体 | FastAPI lifespan 内的后台任务,配执行预算(按空闲槽位领取)与优雅停机 |
| 范围收缩 | 标准协议提案(信封与 operation)已删除;`shadow`/`step` 与暂停开关移出范围;适配器首版仅 `build_request`/`classify_response` 两个端口,`Verdict` 合并为四个取值 |
| 必要性审计 | 删除目标快照、状态事件表,以及 `app_id`、`direction`、`maybe_delivered` 三列;保留 `business_key`;出站唯一键为 `(target, operation, operation_id)` |
| 首版新增 | 重复入队幂等、入队校验(基于 `HttpClientConfig` 的纯函数,不依赖客户端实例)、本地错误隔离、终态消息 90 天保留期、对端等待提示设采纳上限(默认 1 小时) |
| target 配置 | 保持放在环境变量 `RELIABLE_TARGETS`(JSON)中 |
