# ECS / WMS 对接记录统一 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 将零散的 ECS / WMS 交互归集为可追查原身份、原字节、发送、回调、裁决和业务引用的统一记录，减少实体与重复原文，并保持原幂等、执行及恢复合同。

**Architecture:** 沿现有 models / integrations / services / controllers / schemas 分层，将原命令和收据归入 IntegrationExchange，将发送 / Result / 快照归入 IntegrationRecord，用 IntegrationReference 做明确关联。ReliableMessage 保留调度，TransportTask 保留业务事实；通过原事务和注入记录端口迁移正文，受控维护窗口最终对账后统一启用新权威，验证通过后独立收缩旧表。

**Tech Stack:** 项目已锁定的 Python 3.13+、FastAPI、SQLAlchemy、Alembic、PostgreSQL、Celery、pytest / pytest-asyncio / HTTPX；SQLite 保留既有兼容测试。复用标准库与现有依赖，不新增运行服务或依赖包。

**Spec:** [已确认设计及工程评审](/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/docs/superpowers/specs/2026-11-06-integration-history-unification-design.md)，实际决定 D1 / B、D2～D10 / A，D11 / C 已确认进入本实施计划。

**Workspace:** /Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin，feature/integration-history-unification，核对基线 1338e255d401e53683c2ea575ab13784b6b8e8c3。本文件是计划，代码、迁移、下述新测试均尚未实施。

## Global Constraints

- 三个共享模型集中于 app/models/integration_history.py；共享历史写入与原文读取放在 app/integrations/history.py。
- 普通历史摘要和查询索引默认保留 90 天；普通时间范围检索单次最大跨度为 90 天；每页默认 20 条、最多 100 条。
- 原文按来源期限保留。诊断为每来源每进程每分钟 120 条、48 KiB 预览、两秒预算、默认 7 天，均不限制核心受理。
- 原收据资格与可靠终态沿原 90 天配置和原时钟；ECS 长期恢复证据无新增统一自动到期；未决、核对、消费、前序和锚点继续按原保护。
- 新内部主键统一 UUIDv7，原 receipt_id、command_code、operation_id、lease_id、digest、业务主键和原算法保持。数值事件 ID 覆盖历史和未来新事件。
- 数据库时间统一 UTC timestamptz，API 使用带 Z 的 ISO 8601；正文统一 BYTEA，NULL 与确认空字节分开；旧 JSON 回复重编码不得冒充 wire 原字节。
- 纯重复观察尽力保存，失败不改变原重放回复；新受理、新裁决、重检变化、业务状态及原 outbox 意图仍保证权威证据原子提交。
- 原命令根 → Outbox 及各业务 WorkLine / Device / RackJob / 主体 / RackAction 锁序保持。清空正文和删除行使用同一保护准入，先原业务锁下重读，再锁根与证据。
- app/reliable 只通过注入端口访问历史；无业务 / 协议实现、第二 Sender、额外重试调度器或隐式网络。
- 原公开 create / send / get_result / get_event 及其 DTO、原回复与无副作用读取合同保持；内部 Outbox / MessageStore 装配增加记录端口，所有首方装配一起迁移。
- 保持一个 Alembic head，只新增迁移，不改旧迁移；业务测试模型使用独立 MetaData，不加入生产 Base。
- D6 采用受控维护窗口。准备阶段旧侧权威；所有旧写入者和写事务退出、最终对账通过后才启动新权威；原在途事实与期限不重置。
- 当前只在该独立工作区实施；保留已有暂存内容，不自动提交、推送、合并或生产部署。任务末检查差异，提交仅在用户明确要求后执行。

## Review Focus

1. 最高旧事件行已删但序列仍更大：迁移后新 event_id 不复用，原 canonical / predecessor 和倒序查询保持；测试归 T1 / T4 / T10。
2. 收据历史仍存在而受理资格已到期：同外部身份合法新建实例，原 ECS 调用身份与长期重放不释放；测试归 T3 / T4 / T8。
3. Result 先于 ACK，之后同租约迟到拒绝：保留原采用 Result 与核对义务；旧 OPEN 不越过同次 NOT_RECEIVED，新租约独立判断；测试归 T2 / T3。
4. 相同 trace、条码、设备或接近时间对应多个生命周期：旧快照不拼接为受理事实，关联仅来自可信原引用；测试归 T4 / T6 / T7。
5. 保存点或外层 commit 失败、或 ORM 中已有未提交旧值：权威事实一起回滚，纯诊断不污染原事务，结果读取无 flush / 锁 / 补录 / 发送；测试归 T2 / T4 / T5。

---

## 交付顺序与运行启用

固定 T1 → T2 → T3 → T4 → T5 → T6 → T7 → T8 → T9 → T10。每项包含自己的测试闭环和差异检查；这些是同一改造的顺序检查点，中间版本不作为生产可用兼容版本交付。共享模型与引用合同集中变化，本计划不启动并行写入工作区。

T1 的扩展迁移只增加存储与可空引用；其后有独立 authority 结构准备版本，完成最终补录后才执行。T2～T9 在隔离环境完整适配。运行发布时先扩展、保护来源、补录和最终对账，再完成结构准备并统一启动新读写，最后满足 T10 准入后执行收缩。不得按代码任务编号在准备阶段提前启用只写新表的应用。

具体采用三个连续 revision：integrationhistoryexpand → integrationhistoryauthority → integrationhistorycontract。原因是旧 ReliableMessage.request_raw 的 NOT NULL 会阻止仅写引用的新行；authority 版本在稳定来源对账后只解除该旧列约束、收紧已补齐的运行引用并校验原数值映射，不删除旧字节。运行启用使用 schema head 为 authority 的制品 A；独立收缩使用后续制品 B，不把 contract 提前包含在制品 A 的 upgrade head 中。三步是 D6 / 原独立收缩准入的实现机制，不增加在线混合版本协议。

## 文件结构与责任

下列路径是任务的文件图，任务中的 F 编号引用此表。标为新增的文件当前不存在。

| ID | 文件或文件组（均相对本工作区的确定位置） | 责任 |
| --- | --- | --- |
| F01 | /Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/models/integration_history.py（新增） | 三个 ORM 模型、类型及原生约束 / 索引 / 事件数值序列 |
| F02 | /Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/integrations/history.py（新增） | 原会话内记录、正文引用、原文批量读取与中立投递端口实现 |
| F03 | /Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/services/integration_history_service.py（新增） | 正式查询、复合游标、受控原结果投影 |
| F04 | /Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/controllers/integration_history.py（新增）；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/schemas/integration_history.py（新增） | 四个管理 GET、参数 / 返回格式、权限与正文响应 |
| F05 | /Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/scripts/backfill_integration_history.py（新增） | 有界补录、可恢复状态文件与最终对账报告，不启动 Sender |
| F06 | /Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/migrations/versions/2026-11-06_integration_history_expand.py（新增）；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/migrations/versions/2026-11-06_integration_history_authority.py（新增）；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/migrations/versions/2026-11-06_integration_history_contract.py（T10 新增，不进入制品 A） | 扩展 / 权威切换前结构准备 / 受保护收缩，连续单 head |
| F07 | /Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/models/__init__.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/models/reliable.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/models/transport.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/models/ecs_command.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/models/callback.py | 新导出、运行引用、最终退出旧五个实体 |
| F08 | /Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/reliable/lifecycle.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/reliable/store.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/reliable/outbox.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/reliable/delivery.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/reliable/types.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/reliable/__init__.py | 注入端口、冻结 bytes 装配、门禁 / finish / 迟到与异常边界 |
| F09 | /Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/reliable_runtime.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/main.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/tasks/business_tasks.py | API / Sender / worker 的统一记录端口装配 |
| F10 | /Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/integrations/ecs_command/service.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/integrations/ecs_command/lifecycle.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/integrations/ecs_command/result.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/integrations/ecs_command/results.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/integrations/ecs_command/dev.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/integrations/ecs_command/runtime.py | 原命令创建、冻结校验、发送结论、Result 与设备 / 调试读取 |
| F11 | /Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/integrations/callback_receipts.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/integrations/ecs_callback/receiver.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/integrations/ecs_callback/results.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/integrations/wms_callback/receiver.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/integrations/wms_callback/snapshots.py | 收据数值兼容视图、原 guard / recheck、诊断 |
| F12 | /Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/controllers/ecs_callback.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/controllers/ecs_result.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/controllers/wms_events.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/controllers/dev_ecs_event.py | 本次接收身份、编码回复、核心 / 诊断边界与调试外层区别 |
| F13 | /Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/integrations/wms_envelope.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/integrations/wms_request/results.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/integrations/wms_request/service.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/integrations/wms_transport/results.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/integrations/wms_transport/acceptance.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/integrations/wms_transport/service.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/integrations/wms_transport/settle.py | 正文引用后的原终局 / 接纳 / 业务结果读取及 lazy Outbox |
| F14 | /Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/services/dev_submit_service.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/services/dev_callback_service.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/services/dev_ecs_event_service.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/services/dev_wms_request_service.py | 调试读口显式批量装配，不留删除字段的访问 |
| F15 | /Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/tasks/callback_tasks.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/tasks/reliable_tasks.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/tasks/celery_app.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/core/config.py | 原清理任务承接、普通历史窗口配置和原计划入口 |
| F16 | /Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/businesses/_components/bin_roller/retention.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/businesses/coarse_sorting/retention.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/businesses/smt_auto/retention.py | 原业务保护与首批消费 / 关联接入 |
| F17 | /Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/services/audit_service.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/scripts/seed_admin.py | 正文读取旁路审计及两个权限种子；普通 GET 中间件合同保持 |
| F18 | /Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/scripts/release_bundle.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/docs/integrations/integration-history.md（新增）；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/docs/testing/integration-history.md（新增） | 原发布检查复用、切换清单、对账及软件验收记录 |
| F19 | /Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/tests/conftest.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/tests/helpers.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/tests/reliable/conftest.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/tests/reliable/crash_worker.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/tests/integrations/ecs_command/conftest.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/tests/businesses/smt_auto/conftest.py | 记录端口、SQLite / PG schema、副本夹具、原进程重启与清理顺序 |
| F20 | /Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/tests/integrations/test_integration_history.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/tests/controllers/test_integration_history.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/tests/services/test_integration_history.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/tests/migrations/test_integration_history.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/tests/scripts/test_backfill_integration_history.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/tests/tasks/test_integration_history_retention.py（均新增） | 新共享行为、查询、迁移、补录与保留回归 |
| F21 | /Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/businesses/coarse_sorting/service.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/integrations/wms_transport/callback/base.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/tests/businesses/test_coarse_sorting_forward.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/tests/businesses/test_coarse_sorting_wakeups.py；/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/tests/businesses/test_coarse_sorting_direct_put.py | 原消费事务与 Transport 采用版本的明确历史，保持原业务推进 |

继续扩展原 ECS / WMS / Transport / reliable / 权限 / 审计 / 业务保留测试，不将整个旧套件替换为新表存在性断言。F19 的共享 helpers 只容纳多处实际复用的输入与准备，不从一个测试文件导入另一个测试文件。

## 跨任务接口与存储合同

### 1. 数据模型

- IntegrationExchange.id 是 UUIDv7。增加 role，取 ECS_COMMAND / RECEIPT / OUTGOING / DIAGNOSTIC，区分运行根职责，不是新的通用状态机。
- common 字段沿 spec 4.1；正式 ECS / WMS 装配提供 peer_system。来源确实未知用 NULL，不能读载荷中的自报系统来分类。
- ECS_COMMAND 保留原 command_code、owner_domain、call_key、call_key_digest、input_raw、route、delivery_snapshot、target、expires_at、status、delivery_status、dispatch_started_at、hold_reason、blocked、needs_reconciliation、applied_result_id。request_raw / ack_raw 改为 request_record_id / ack_record_id，同时新增独立冻结 request_sha256。
- RECEIPT 保留 receipt_id: int、operation / operation_id、raw_digest / effective_digest / compare_mode、resp_timestamp、event_envelope / event_context / application_ref / diagnostic、current_response_record_id，并用 receipt_active: bool 表达仍占受理资格。历史留存与资格分开。
- 命令 command_code 唯一，命令角色 owner_domain / call_key_digest 唯一；活动收据按 operation / operation_id 部分唯一，谓词只用 role / receipt_active，不用 now()。保留 RAW / EFFECTIVE CHECK。角色字段不得塞入诊断 details。
- IntegrationRecord 沿 spec 4.2，映射数据库 metadata 列的 ORM 属性名为 details，避免 SQLAlchemy 保留名。正文允许 body_record_id 指向一份规范保存的正文，禁止引用链；source / semantic / attempt 摘要不混用。Record.capture_origin 区分本条 LIVE / BACKFILL；根的 capture_origin 只表示实例初次来源，不能据其判定后来的记录仍是补录。
- RESULT_EVIDENCE 保留 semantic_digest、effective_payload、response_record_id、applied，按原命令 exchange / semantic_digest 等价唯一。
- ECS 发送使用 SEND_ATTEMPT_OPENED 与内部 ECS_ATTEMPT_CONCLUSION；HTTP 原观察使用 HTTP_RESPONSE_OBSERVED，租约处理使用 SEND_ATTEMPT_FINISHED。ECS_ATTEMPT_CONCLUSION 有 ecs_attempt_outcome 和 attempt_revision，引用对应观察，自己不复制响应正文。新增枚举值只表达原网络 / 发送判断，不改变原分类。
- IntegrationReference 沿 spec 4.3。有 source_record_id 与无 source_record_id 分别建立部分唯一键，避免 NULL 绕过补录防重；命中摘要后比较完整 namespace / value / role。
- 运行者持有的正文 / 裁决引用使用 ON DELETE RESTRICT。根与记录的构造 / 清理次序必须避免循环 FK 阻断；允许历史子记录到根的 FK 延后检查，但不能降低运行引用的删除保护。新根的临时组装在同一事务完成，已提交事实字段保持追加式。
- ReliableMessage 新增 exchange_id、request_record_id、terminal_response_record_id、last_response_record_id。命令 / ACK / 最近 / 终局可共指同一正文，但选择关系不合并。TransportTask 新增 submission_exchange_id，Outbox 清理后仍指原提交实例；接纳依据中仅重复请求 / 回复改为引用，保留原证明字段和摘要、原规范化业务事实。

### 2. 数值事件身份

F01 声明 PostgreSQL 原生 integration_receipt_id_seq，receipt_id 用原 Integer 语义且全局唯一，由收据写入显式取 nextval；非收据不分配。新序列在最终稳定对账中接续原 callback_receipt 的实际序列高水位、已迁移数值 ID 和持久引用，保留原整数，不用 MAX(现存行)+1 或时间 / UUID 推算。序列允许间隙，不要求连续。

SQLite 没有 PostgreSQL 序列；仅为既有 SQLite 路径提供一个未映射为 ORM 实体的 _integration_receipt_sequence 原生 INTEGER PRIMARY KEY AUTOINCREMENT 分配表。插入取号、删除已取行仍沿同一事务，保留原生已提交高水位，不直接修改 sqlite_sequence。创建逻辑通过 SQLite 专用 DDL 同时用于 metadata.create_all 和扩展迁移，不进入 PostgreSQL Base 表清单，不增加生产业务实体。迁移前 SQLite 未记录的已删除历史高水位不能补造；生产接续证明必须用真实 PostgreSQL 序列。

本轮仅做过 SQLite 内存原生行为探针：删除已提交的 500 后，后续两次已提交分配得到 501 / 502；不是新模型验证。依据 [SQLite AUTOINCREMENT](https://www.sqlite.org/autoinc.html)，回滚号允许复用，已提交号不复用且不保证无间隙。测试只断言合同，不断言 next_id 恰好加一。

### 3. 共享写入与可靠端口

F02 使用值对象 RecordDraft（按 spec 4.2 具名字段，keyword-only）和 StoredBody(record_id: UUID, body_state: str, raw_body: bytes | None, body_sha256: str | None, stored_bytes: int | None)。不新增历史管理服务类。

~~~python
async def write_record(session: AsyncSession, *, draft: RecordDraft) -> UUID: ...
async def read_bodies(
    session: AsyncSession, *, record_ids: tuple[UUID, ...]
) -> dict[UUID, StoredBody]: ...
def encode_reply(body: dict | None) -> bytes: ...
async def record_consumption(
    session: AsyncSession,
    *,
    owner_domain: str,
    action_id: str,
    source: SourceRef,
    consumed_at: datetime,
) -> UUID: ...
~~~

SourceRef 是 kind（ECS_COMMAND / WMS_MESSAGE / TRANSPORT_TASK）、identity: str、revision: str | int | None 的只读值对象，不是 ORM 实体。ECS 使用原 command_code / Result digest，WMS 使用原 message_id 并在同一读取快照解析实际终局响应，Transport 使用原 task_id / outcome_revision。它只解析原引用，不负责判定业务能否消费。

write_record 不开 / 提交事务，不发送；同 exchange / event_key 的重试复用原记录并比较完整事实，不同内容报错。自动 UUID / recorded_at 不属于重复比较的输入，重试沿首次事实，不拿新处理时钟覆盖。read_bodies 在 no_autoflush 内用一次定点联接解析最多一层正文引用；不锁、不写、不补录。encode_reply 使用与现有 JSONResponse 相同的 UTF-8 / ensure_ascii=False / allow_nan=False / 紧凑分隔符，None 返回确认空字节。record_consumption 在动作原事务中以 owner / action / 实际来源版本防重，不读取其他业务表或授予执行权；同一结果被两个动作消费则保留两条。

F08 在 lifecycle.py 声明协议无关 DeliveryRecords；F02 模块本身实现协议，不新增具体 recorder 类。

~~~python
async def register(
    session, *, message: OutgoingMessage, protocol: str, exchange_id: UUID | None = None
) -> tuple[UUID, UUID]: ...
async def requests(session, *, record_ids: tuple[UUID, ...]) -> dict[UUID, bytes]: ...
async def opened(session, *, claim: Claim, at: datetime) -> UUID: ...
async def finished(
    session, *, claim: Claim, outcome: AttemptOutcome, at: datetime, late: bool
) -> UUID | None: ...
~~~

register 返回 exchange_id / request_record_id，ECS 提供既有命令 exchange，普通出站由它登记原实例。协议分类在受信装配决定，可靠层不解析业务。Outbox / MessageStore 增加 keyword-only 必填 records: DeliveryRecords，所有 API / worker / lazy services / 原重启程序与夹具显式传入 F02；不保留无记录静默降级模式。

ECS 已提供 message.request_record_id 时，register 核对原引用、独立冻结摘要及原 bytes 后复用，不能重新生成一条时间略有差异的请求事实。HTTP_RESPONSE_OBSERVED 不从协议未知的时间字段推导 occurred_at，使用首次 recorded_at 表示本地观察；observer 和 finished 重用同一次原网络事实。finish / 迟到的处理时间和有效性分别记录，不参与生成另一份相同 HTTP 正文。

MessageDraft / OutgoingMessage 仅以 keyword-only 可选字段携带 exchange_id / request_record_id；原调用传入 bytes、适配器取得 request_raw 的合同保持。Claim 在原 claim 事务内批量装配请求；不通过 ORM 属性触发异步 I/O。AttemptOutcome 增加 keyword-only observation_record_id: UUID | None，finish 写好通用观察后把引用交给原 lifecycle，避免 ECS 再保存一份响应。

Transport submission_observer 当前先于 Outbox.finish，在独立的原任务事务内保存接纳。它在该事务写 HTTP_RESPONSE_OBSERVED 的唯一正文及接纳引用；后续 finished 按原 exchange / lease 的固定 HTTP 观察键复用完整相同的网络事实，再追加 SEND_ATTEMPT_FINISHED 并引用正文。网络观察不预先写 lease 是否有效，后者在原 finish / 迟到 / ECS 结论中表达；同一响应不重复存字节，也不把 observer 移到 finish 之后。observer 事务回滚不留接纳假记录，原 observer 异常 / 通知处理保持；普通未经过 observer 的响应由 finished 首次登记。

F08 定义 EvidencePersistenceError(Exception) 和 EvidenceIntegrityError(EvidencePersistenceError)，固定安全消息。包括端口调用及原事务 commit 的新增持久化失败在原可靠分类边界之外传播；它们不能变成 ValueError / LOCAL_ERROR / PRE_GATE_VALIDATION 或对端拒绝。明确冻结不匹配沿原门禁拒绝发送，不能扩大发送资格。

### 4. 收据兼容与本次接收

F11 的 find_receipt 保持 operation / operation_id / for_update 调用形式，返回 ReceiptView；它的 id: int 仍是 receipt_id，另有 exchange_id: UUID，其他属性沿原收据。不得返回 .id 为 UUID 的 ORM 对象冒充旧收据。get_event / list_point_events 仍用数值 receipt_id 过滤、排序和分页。

insert_receipt 的数值返回合同保持，接入新根、原请求 / 当前回复引用。原接收器的所有直接 UPDATE 改为按 receipt_id 定位活动收据根；guard / recheck / RAW-EFFECTIVE / 保存点的位置不迁到通用 writer。

CallbackResponse 增加 keyword-only prepared_body: bytes | None、pure_replay: bool=False、exchange_id: UUID | None、attempt_id: str | None；EcsResponse 的既有位置参数和 body 字典保持。prepared_body 在原协议路径完成最终回复时编码一次并保存，HTTP 使用同一字节返回。pure_replay 必须由拥有原检查的接收分支明确设置，不能由 outcome 文本推断。新接收 attempt_id 在入口生成，trace_id 单独保存；本地调试外层 DTO 和内层协议回复分开标注。

### 5. 查询与清理

F03 提供：

~~~python
async def list_exchanges(
    session: AsyncSession, params: ExchangeListParams
) -> CursorPage[ExchangeSummary]: ...
async def get_exchange(session: AsyncSession, exchange_id: UUID) -> ExchangeDetail: ...
async def list_records(
    session: AsyncSession, exchange_id: UUID, params: RecordListParams
) -> CursorPage[RecordSummary]: ...
async def get_body(session: AsyncSession, record_id: UUID) -> StoredBody: ...
~~~

控制器在鉴权之后另开历史读取会话，首 SQL 前设置只读 REPEATABLE READ；服务不取得事务所有权。两个列表使用原筛选白名单与 limit+1，时间范围是外层 AND 条件，不可用 OR filter 绕过。普通时间自定义必须同时提供 from_at / until_at，0 < 差值 <= 90 天；未给时取最近 90 天。精确原身份且未给时间时覆盖合法留存期；同外部身份的多个原实例分别返回。

复合游标上限 256，紧凑 JSON 字段为 v、at、id、q，以及可选 u。q 是端点 / 原筛选 / 明确时间参数的规范摘要；默认时间窗口的上界 u 在首个游标中固定，之后从游标恢复，避免翻页时 now 漂移。at / u 固定 UTC 微秒 Z，id 为标准 UUID；严格验证并重编码核对。不要套用 64 字符整数游标，也不添加签名密钥或服务器游标表。

正文 GET 返回 application/octet-stream，头为 X-Integration-Body-State / X-Integration-Body-Sha256 / X-Integration-Stored-Bytes，不把未受信正文当 HTML 渲染。不存在记录返回原 NOT_FOUND；有摘要但正文已 PURGED / UNAVAILABLE 也返回 NOT_FOUND，detail 为固定 body_state，记录页仍说明原因，不能变 JSON null。成功读取在 F17 新增下列旁路留痕，沿原失败策略，只收标识，不收 bytes；普通 GET 中间件行为不改。

~~~python
async def record_payload_read(
    factory: async_sessionmaker[AsyncSession],
    *,
    user_id: int,
    record_id: UUID,
    trace_id: str,
    ip: str | None,
    user_agent: str | None,
) -> None: ...
~~~

F15 复用原任务名。新增配置 INTEGRATION_HISTORY_RETENTION_DAYS=90，仅用于无额外运行保护的普通摘要；诊断与收据继续读原配置。原 cleanup_terminal 的 TERMINAL 包括 DONE / FAILED / EXPIRED / ABANDONED，HELD / 未决不因普通窗口删除。每次清空或删行先调用原业务保护入口并按原锁序重读。

## 验证命令约定

所有命令在 Workspace 执行；所列测试名是待编写的验收合同，不是已存在或已通过结果。任务开始先验证对应断言失败，再实施并运行同一组。使用工作区 uv 环境；无环境时按原锁文件 uv sync，不复制其他工作区 .env。

通用快速命令为 rtk proxy uv run pytest -q <下列测试路径>。PostgreSQL 专项必须先提供专用、可销毁的 INTEGRATION_HISTORY_TEST_DATABASE_URL；需要真实 Redis 的现有测试另提供 INTEGRATION_HISTORY_TEST_REDIS_URL，不打印 URL：

~~~sh
rtk proxy python3 -c 'import os; assert os.environ.get("INTEGRATION_HISTORY_TEST_DATABASE_URL"), "需要显式隔离 PostgreSQL 配置"'
rtk proxy env TEST_DATABASE_URL="$INTEGRATION_HISTORY_TEST_DATABASE_URL" TEST_REDIS_URL="$INTEGRATION_HISTORY_TEST_REDIS_URL" uv run pytest -q tests/migrations/test_integration_history.py
~~~

未配置时旧夹具跳过不计通过；配置但连接失败应失败。生产数据库不能用于这些命令。

### Task 1 / T1: 建立模型与扩展迁移

**Files:** F01、F06 的 expand / authority、F07 导出及可空运行引用、F19 schema 夹具、F20 migrations 测试。当前 down_revision 为 coarserackstage3；开始前重新核对 head，若基线已改变则接到实际单 head，不创建 merge head。

**Interfaces:** Produces 三个模型、四个 role、RecordKind、原生 receipt 序列及全局格式；后续任务使用上述字段。旧模型此时保留，只用于尚未迁完的路径及历史测试，尚不启用新权威。

- [ ] **Step 1: 写失败的迁移合同。** 在 tests/migrations/test_integration_history.py 增加 test_receipt_sequence_survives_deleted_highwater、test_role_uniqueness_keeps_command_and_receipt_lifetimes_separate、test_expand_preserves_original_rows_and_foreign_schema。用独立库造原收据、原序列更高但最高行已删、原命令和外部系统表；断言新库承接旧值，而非简单比较表清单。

~~~python
assert new_event_id > original_allocated_highwater
assert migrated_event_id == original_event_id
assert foreign_payload_after == foreign_payload_before
~~~

- [ ] **Step 2: 运行同文件，确认在缺少新迁移 / 约束处失败。** Run: rtk proxy uv run pytest -q tests/migrations/test_integration_history.py。不得把模块导入错误当作最终红灯定位，先让旧库种子有效。
- [ ] **Step 3: 在 F01 / expand 实现上述模型、角色约束、索引与方言序列。** 扩展只加新表和可空引用；schema 使用原 SCHEMA / naming convention。运行证据 FK 为 RESTRICT，正文引用唯一指向规范正文。保留旧 PG 序列，最终高水位接续由 T7 在停写后执行。authority 接在 expand 后：旧运行行未补齐引用、身份 / 原文不符则拒绝；通过后只解除旧 request_raw 非空并收紧已迁移引用，使新代码无需填充重复原文。无 LIVE 写入前可按检查回退，有 LIVE 事实后不自动交回旧程序。
- [ ] **Step 4: 适配 F19。** metadata.create_all 与制品 A 的 authority head 产生同等约束，旧 request_raw 在 ORM 映射中同步允许 NULL；SQLite 分配表只在 SQLite 创建。fixture 清理先原业务 / 运行引用，再 records / references / exchange；历史迁移测试使用该版本的独立 Table 种子，不向业务 Base 加测试模型。migrations/env.py 的 PROJECT_TABLES 是旧 public 版本 a023 的搬迁清单，不能随新表追加。T1 先验证迁移自身，原运行写入到新必需引用的全量兼容由 T2～T5 完成后一起验证，中间检查点不冒充整分支已通过。
- [ ] **Step 5: 运行 SQLite 及显式 PG 的同一合同，加 tests/migrations/test_project_schema.py。** Expected: 配置的后端全部 PASS，迁移单 head，原数据和外表保留。记录未配置跳过，不宣称 PG 通过。
- [ ] **Step 6: 检查差异。** rtk git diff --check；审阅迁移没有删旧数据、改旧版本或默认切换运行路径。未经用户要求不提交。

### Task 2 / T2: 接入原事务中的记录端口

**Files:** F02、F08、F09，以及 F13 两个 lazy Outbox 属性、F19 所有 Outbox / MessageStore 构造；F20 integration 测试和原 reliable 测试。

**Interfaces:** Consumes T1 模型；Produces write_record / read_bodies / DeliveryRecords 及 typed persistence errors。register / opened / finished 的会话必须是原 outbox / gate / finish 会话，不引入事务或 Sender。

- [ ] **Step 1: 写 test_core_record_and_outbox_rollback_together、test_duplicate_fact_requires_equal_content、test_open_commit_failure_prevents_http_for_both_modes、test_finish_storage_failure_keeps_original_recovery。** 使用 callback_store 测共享 writer；STATIC / SNAPSHOT 失败放在各原 delivery 测试，复用其真实 rig。

~~~python
assert http_calls == []
assert committed_business_changes == []
assert replacement_operation_ids == []
~~~

- [ ] **Step 2: 运行新共享 tests/integrations/test_integration_history.py 和 tests/reliable/test_leases.py、tests/integrations/ecs_command/test_snapshot.py，先确认新增断言失败。**
- [ ] **Step 3: 实现 F02 和 F08 的新端口。** 同 event_key 全值比较，使用原保存点防重；新增持久化异常连同事务出口 commit 异常越过原网络分类。STATIC 原门禁取得当前消息行后登记 OPEN；SNAPSHOT 在原授权 / lifecycle 检查通过且期限复核后登记 OPEN；提交成功后才允许网络。
- [ ] **Step 4: 在原 finish / apply 内写通用观察。** observation_record_id 经 replace(outcome, ...) 交给 lifecycle；失效租约只保存已观察的原尝试，不能改新 lease 的状态或调度。无 HttpResult 不补造 HTTP 状态或正文。保留原 after_commit 通知和 observer 次序。
- [ ] **Step 5: 完成所有首方装配。** 查找 app 与 tests 内每个 Outbox( / MessageStore(，更新 API lifespan、worker、WmsRequests.outbox、TransportService.outbox、ECS Result 内 settle 装配及 tests/reliable/crash_worker.py。原 MessageDraft / adapter 仍看到冻结 bytes；批量 claim 不逐条隐式取正文。
- [ ] **Step 6: 运行上述测试及 tests/reliable、tests/integrations/wms_request/test_service.py、tests/integrations/wms_transport/test_service.py。** Expected: 原租约 / 原输入规则 PASS；新失败用例证明无发送、无假对端拒绝。检查 fixture 没有无记录 fallback。

### Task 3 / T3: 迁移 ECS 根、发送结论与 Result

**Files:** F10，F11 的 ECS 结果读取适配，F12 的 ecs_result，原 tests/integrations/ecs_command 下 service / flow / snapshot / result / concurrency / restart / query / dev / device_directory。

**Interfaces:** Consumes T2 端口；Produces 保持原签名的 EcsCommands.create / get_command / get_by_call、原 Result 接收，以及 get_attempt_conclusions(session, *, exchange_id: UUID) -> dict[str, str]。后者留在 ECS lifecycle 模块，只读原租约的有效结论。

- [ ] **Step 1: 扩展原测试，增加 test_finished_nonacceptance_replaces_open_of_same_lease、test_result_replay_survives_unified_history_cleanup、test_frozen_request_reference_keeps_independent_integrity。** 覆盖另外租约仍 OPEN、Result-before-ACK、迟到拒绝、当前 annex 缺失、原 Outbox 已清理。

~~~python
assert command_code_after == command_code_before
assert replay.status_code == original.status_code
assert replay.body == original.body
assert adopted_status == "SUCCEEDED"
assert needs_reconciliation is True
~~~

- [ ] **Step 2: 运行 tests/integrations/ecs_command/test_flow.py、test_result.py、test_service.py、test_snapshot.py，确认新存储断言先失败。**
- [ ] **Step 3: 将 service / lifecycle / result 的查询与锁指向 role=ECS_COMMAND 的 exchange。** 原 owner / call_key_digest 查询后比较完整 key 与 input_raw，先找到原调用再解析当前路由；并发唯一赢家仍在原保存点处理。冻结 byte 只保存一次，保留独立 request_sha256、原 route / snapshot 和原期限核对。
- [ ] **Step 4: 实现发送有效结论。** OPEN 由 T2 写入。EcsLifecycle.record 将原 ACK / REJECTED / NOT_RECEIVED / UNKNOWN 映射写为 ECS_ATTEMPT_CONCLUSION，parent 指通用观察；在原命令锁下为该 lease 取最大 attempt_revision+1，使用等价唯一键防事务重试。每 lease 有结论则最后修订取代 OPEN；无结论但已登记 OPEN 才保持原可能派发。不得按时间 / UUID 或全时间线 any(OPEN) 决定。
- [ ] **Step 5: Result 按命令 exchange / semantic_digest 读取 RESULT_EVIDENCE，先原 core 相等与原回复重放，再解析当前可用 annex。** 新采用 / 拒绝、原 hold / settle、响应记录及通知同事务；额外接收观察不改裁决。ack_record_id 只来自真实 ACK，Result settle 的 DONE 不产生 ACK。
- [ ] **Step 6: 适配 results.py、dev.py、设备关联与批量消息引用读取。** 不为旧 EcsCommand 保留未过滤 ORM 别名，不把 exchange UUID 当 command_code。保留所有原公开 CommandView 字段。
- [ ] **Step 7: 运行整个 tests/integrations/ecs_command 及原端口 / 租约测试。** Expected: 两后端适用用例 PASS，PG 并发需实际配置；无新 command identity、无 unknown 自动重发。检查差异，不提交。


### Task 4 / T4: 合并收据与诊断快照

**Files:** F11、F12、F14 的 callback / ECS Event 读口，F19 callback / SMT 夹具，F20 integration 测试；扩展原 ECS Event / WMS callback / controller 测试。

**Interfaces:** Consumes T1 receipt_id 与 T2 writer；Produces ReceiptView 和带 keyword-only 证据上下文的 CallbackResponse。保留 find_receipt / insert_receipt 的原输入及数值返回合同，原接收器仍负责事务。

- [ ] **Step 1: 写 test_receipt_history_does_not_keep_expired_acceptance_slot、test_pure_replay_observation_failure_preserves_protocol_reply、test_recheck_new_facts_require_atomic_evidence、test_legacy_preview_does_not_invent_receipt_association。** 扩展原事件公开读口测试，检查旧 / 新数值事件顺序和 canonical / predecessor。

~~~python
assert second_event_id != first_event_id
assert replay.body == protocol_expected_replay_body
assert handler_calls == 1
assert current_reply_bytes == http_response.content
~~~

- [ ] **Step 2: 运行 tests/integrations/ecs_callback、tests/integrations/wms_callback/test_task_contract_extensions.py、tests/controllers/test_wms_events.py、tests/controllers/test_ecs_callback.py，确认新合同先失败。**
- [ ] **Step 3: 在 callback_receipts.py 实现角色限定的登记与 ReceiptView。** 活动收据唯一冲突仍等待原事务赢家，赢家回滚后可接管；.id 保持 int，查询或 UPDATE 使用 receipt_id，exchange.id 仅用于关联。新根请求 / 回复的临时组装必须在原事务完成，提交后不改旧事实记录。
- [ ] **Step 4: 沿原 receiver 分支标记 pure_replay。** ECS 先原身份 / legacy aliases，WMS 先原 guard / 比较 / recheck。recheck None 保留原保存点回滚，handler Duplicate / Conflict 中可能的新写入不丢；guard Unavailable 仍不留下稳定失败收据。新裁决原子保存，原 WMS RECEIVED 重报仍变 DUPLICATE / 200，不能一律重放首次 wire。
- [ ] **Step 5: 在协议返回点编码一次，HTTP 复用 prepared_body。** 纯重放原回复及所有必要检查成功后，独立诊断事务失败不影响回复，不能吞 SQL 错误后复用 aborted 会话。记录本次 attempt 与原根，不把 trace 当 attempt，不把调试外层响应和协议回复混记。
- [ ] **Step 6: snapshots.py 写统一诊断，不再增加另一份核心预览。** 保留 allow_snapshot 的来源 / 每进程配额，额度耗尽不把正文挂到后台；保持 48 KiB、两秒、空回复与截断。旧 source=NULL 连同 WMS 默认来源解释保留；无法证明关联的旧快照有独立诊断根。流读取失败的空预览不标为完整空请求。
- [ ] **Step 7: 重跑上述原测试与 tests/tasks/test_callback_tasks.py、tests/controllers/test_dev_ecs_event.py。** Expected: 字节 / 原回复 / 原事件排序 / 原 guard 行为 PASS，新增 PG 失败事务与并发测试实际执行；无 replay 触发新业务效果。

### Task 5 / T5: 收敛正文副本并适配原公开读口

**Files:** F07 的引用字段、F08 的 bytes 装配、F10 的原 DTO、F13、F14、F21，以及原 WMS Request / Transport / dev / acceptance / coarse 消费测试。旧正文列暂留到 T10，仅作为尚未收缩的来源，不做运行读取 fallback。

**Interfaces:** Consumes T2 read_bodies；Produces 原 OutgoingMessage、CommandView、WmsRequestResult、TransportTaskResult、AcceptanceBasis 的原字段与签名。F13.read_rows 在一条显式语句中使用各正文记录 alias；不改变调用方的事务所有权。

- [ ] **Step 1: 扩展 test_result_reader_never_writes_locks_flushes_or_sends、test_reads_do_not_flush_or_reuse_old_orm_values、test_missing_terminal_response_and_retention_never_mean_success；增加 test_acceptance_body_references_preserve_original_basis、test_acceptance_observer_survives_later_finish_failure_without_duplicate_body、test_consumption_history_commits_with_action_once_per_source、test_consumption_and_send_gate_keep_original_lock_order。** 测试读取时 ORM 内已有未提交修改、Outbox DONE 无 HTTP 回复、仅最近响应存在、接纳依据有独立 saved_at / lease；observer 成功后 finish 失败仍有原接纳依据；粗分消费与历史同回滚，两个动作消费同源各留事实，PG 验证新增 FK 隐式锁不破坏原门禁锁序。

~~~python
assert result.response_state != "VALID"  # 仅有最近响应，不能当终局
assert emitted_sql_count == 1  # 原 Transport read_rows 合同
assert writes_or_flushes_or_locks == []
assert restored_acceptance_digest == original_acceptance_digest
~~~

- [ ] **Step 2: 运行 tests/integrations/wms_request/test_results.py、tests/integrations/wms_transport/test_results.py、test_rack_lifecycle_contract.py，确认新增断言先失败。**
- [ ] **Step 3: 改写 WMS RESPONSE_COLUMNS / stored_response 的装配来源。** 终局正文、最近正文和 HTTP / 协议分类分别取原引用及原状态。保持 no_autoflush 和原消息全身份校验；记录缺失 / 已到期不回读普通档案冒充运行 FOUND。
- [ ] **Step 4: 改写 Transport 接纳依据的请求 / 回复存储。** 用两个明确记录引用替代原文字符串副本，保留版本化的已存依据格式和原 evidence_sha256 校验。读取时用原字节重建同一个 AcceptanceBasis 再比较完整摘要；保持 observer 在原提交完成之前保存接纳的事务次序。Outbox 清理后，任务仍保护其独立接纳依据。
- [ ] **Step 5: 保留 TransportTask.frozen、input_digest、member_positions、applied_outcome / revision、pending_outcome、outcome_digests、position_observation。** F21 的 CoarseSortingService._consume 在原去重判断和原动作 UPDATE 同一事务调用 record_consumption：来源取 action.message_id 或原 command_code / digest；原 evidence_history 与 consumed_at 保留。Transport apply_callback 在实际采用版本前后比较处追加 RESULT_APPLIED，分别说明原结果来源与本次触发回调；位置事件补齐 pending 时不能把它冒充原结果。可证明的原关联落入 IntegrationReference；旧 pending 的原回调来源不可证明时仅引用原 task / revision 规范化事实并声明 LEGACY / 未采集。其他业务只投影已有持久消费，不猜测成功或从后续命令反推。
- [ ] **Step 6: 显式装配原 dev_submit_service.message_out、WMS 状态查询、ECS 命令查询与回调调试。** 批量取本页正文后调用纯格式函数，不给 ORM 增加异步属性、旧列 fallback 或查询时自动补录。
- [ ] **Step 7: 重跑原 WMS Request / Transport 结果、接纳、前序、位置回调、dev 控制器及 F21 粗分消费 / wakeup 测试。** Expected: 原归一化面、已应用 / pending、拒绝与未知、实际 ACK 及无副作用读取全部 PASS；原消费、历史、后续意图和通知同回滚，最后原字符 / bytes 副本只剩待收缩来源。

### Task 6 / T6: 实现正式统一查询及正文权限

**Files:** F03、F04、F09 的 router 挂载、F17，F20 controller / service 测试；复用现有 filtering.py / query.py 的白名单与页数合同，不改原整数 paginate_cursor。

**Interfaces:** Consumes T1 模型、T5 原结果投影；Produces list_exchanges / get_exchange / list_records / get_body 和四个 spec 路由。ExchangeSummary / RecordSummary 无 raw_body、effective_payload、凭据或完整路由配置。ExchangeDetail 包含原来源引用、delivery / protocol / application 分列及 coverage，不维护统一成功状态。

- [ ] **Step 1: 写 test_history_search_keeps_original_instances_distinct、test_time_uuid_cursor_keeps_equal_time_rows、test_query_range_and_default_anchor、test_payload_permission_and_metadata_only_audit。** 用相同外部身份不同生命周期、多 reference、一致时间多 UUID、默认窗口翻页期间时钟变化、90 天边界、撤权和空结果数据集。

~~~python
assert len(ids) == len(set(ids)) == expected_matching_count
assert set(summary) & {"raw_body", "effective_payload", "route"} == set()
assert body_response.status_code == 403  # 只有摘要权限
assert audit_detail["record_id"] == str(record_id)
assert "body" not in audit_detail
~~~

- [ ] **Step 2: 运行 tests/controllers/test_integration_history.py、tests/services/test_integration_history.py，先确认路由 / 参数 / 权限等新增行为未实现而失败。**
- [ ] **Step 3: 实现四个查询函数及 schema。** 时间范围外层 AND 过滤，reference 用 EXISTS 或先去重候选，再 keyset / limit+1；本页状态按明确引用批量装配。精确身份比较命中后核对原值，同编号多个实例分别返回，不按最新活动实例覆盖。
- [ ] **Step 4: 实现 256 字符规范复合游标。** q 绑定端点与请求筛选，u 固定默认窗口上界；后页不重新用 now 生成范围。校验未知键、非法类型 / 时区 / UUID / 编码、其他时间线或筛选的旧游标。完整最大示例含 u 为 255 字符，本轮仅做过编码探针；不得扩大上限来掩盖格式变更。
- [ ] **Step 5: F04 使用 new_router，挂载四个 GET。** 摘要权限 integration:history:read，正文权限 integration:payload:read，与 INITIAL_PERMISSIONS 和文档一起更新；鉴权会话与历史只读 RR 会话分开。正文缺失 / PURGED 返回已有 NOT_FOUND 加固定 body_state，不回显 SQL 或完整目标。
- [ ] **Step 6: F17 新增 record_payload_read 旁路写 AuditLog。** 仅记录 actor / record UUID / 路由 / trace，resource_id 保持 NULL，UUID 放 detail。异常按原安全诊断策略处理，不改变已准备回复；从入口就不把正文传给审计，不修改所有 GET 的审计行为。
- [ ] **Step 7: 运行新查询测试及 tests/core/test_permission_code.py、tests/controllers/test_audit.py、tests/services/test_pagination.py、tests/services/test_filtering.py。** Expected: 原 seed / revoke / OpenAPI / 普通 GET 合同 PASS；新只读快照、字节状态与正文审计通过 PG 专项。

### Task 7 / T7: 实现补录与受控权威交接

**Files:** F05、F06 的 authority 完整准入、F18；F20 scripts 测试。使用确定的旧来源列清单，脚本不导入 app.main、Sender 或业务推进装配。

**Interfaces:** Produces 下列入口与安全 JSON 报告。状态文件只保存 source / cursor / 计数 / 摘要 / 版本，不放正文、连接串或凭据；批次 commit 后原子替换，崩溃后可重跑已提交批次。

~~~python
async def backfill_batch(
    session: AsyncSession, *, source: str, after: int | str | None, limit: int
) -> BatchResult: ...
async def reconcile(
    session: AsyncSession,
    *,
    phase: Literal["precopy", "final"],
    writer_evidence: WriterEvidence | None,
) -> ReconciliationReport: ...
def main(argv: list[str] | None = None) -> int: ...
~~~

BatchResult 为 source / next_cursor / copied / reused / conflicts / done；ReconciliationReport 为 source_counts / identity_mismatches / byte_mismatches / missing_refs / writer_status / sequence_highwater / eligible。WriterEvidence 明确列出已核对的 API、内嵌 Sender、worker / Beat、后台、one-off、人工入口和 DB 写事务，区分操作者确认与脚本可见检查，未证明者保持 UNKNOWN。

- [ ] **Step 1: 写 test_backfill_resume_is_idempotent_without_sending、test_final_reconcile_rereads_changed_old_rows、test_remaining_writer_refuses_authority_switch、test_legacy_bytes_and_unknown_links_keep_their_limits。** 造已预复制后更新的原行、相同 trace 多快照、非法 base64、JSON-only 旧回复、原 command / outbox 字节不一致，以及 commit 后但写 checkpoint 前退出。

~~~python
assert external_calls == []
assert second_run.created_fact_count == 0
assert final_report.eligible is False  # 残留写入者或字节不一致
assert legacy_body_state == "LEGACY_REENCODED"
assert guessed_business_links == []
~~~

- [ ] **Step 2: 运行 tests/scripts/test_backfill_integration_history.py；先确认断点 / 最终变化 / writer 准入新增断言失败。**
- [ ] **Step 3: 实现有界来源映射与 CLI。** 子命令 copy / verify，参数 --source、--batch-size（默认 500）、--state、--phase、--writer-evidence。PG 用单一协调者 advisory lock，SQLite 每批 BEGIN IMMEDIATE；不等待未知写入者自动退出。批次使用原 PK 游标，source_kind / source_key / source_version 记录观察来源，已存在映射先比较后复用，不以 UUIDv5 替代新 UUIDv7。
- [ ] **Step 4: 映射原事实并保护原时间。** 来源清单固定为 ecs_command / callback_receipt / reliable_message / ecs_send_evidence / ecs_result_evidence / callback_snapshot，加 transport_task 原规范化事实及 coarse_sorting_action 已有 evidence_history。command / receipt / message 用原身份或受保护 exchange_id 固定实例；snapshot 以原源行键固定独立诊断根。旧 WMS 只补可用最近 / 终局，不补造每次 HTTP；旧发送只补已保存当前结论，未知完成时刻为 NULL。旧消费仅依据 action / 原已保存版本 / consumed_at，无法证明来源则明确未关联。base64 仅解码原预览，旧 JSON 标记重编码；解码失败或字节冲突进入安全异常清单，保留来源并阻止相应收缩。
- [ ] **Step 5: 实现最终稳定对账与序列接续。** 最终阶段重扫所有可变来源，包括 PK 游标之前已复制的行，在原锁序下 guarded update 绑定运行引用。核对全身份、原 bytes / 长度 / SHA、采用 / 版本、integer / JSON 引用和 source 防重。迁移用重编码摘要不是业务语义版本。final 在旧写事务未退出或库存清单未确认时不返回 eligible=True。
- [ ] **Step 6: 在 authority 迁移重复数据库可证明的准入。** 运行引用、原输入 / 回复完整性和数值映射不符则拒绝。仅解除旧 ReliableMessage.request_raw NOT NULL，并收紧已补齐引用；不删除旧表或旧字节。迁移代码保留该版本的检查，不导入未来可变服务实现。LIVE 记录出现后，不允许 downgrade 直接把权威交给旧程序。
- [ ] **Step 7: 在 F18 写制品 A 的明确运行清单。** 默认全部在受控窗口内搬迁；可选预补录须先停用并排空原清理写入和人工删除入口，保持旧 API / 原核心权威，最终仍完整停写核对。顺序为：留存原镜像 / 配置和备份 → 确认全部旧 writer 停止 → 用新镜像只升级到 expand → copy → final verify / 原序列接续 → 正常 migrate 升到制品 A 的 authority head → 一次启动新 API / worker / 单 Beat → 原身份恢复与检查。中途异常保留来源，不把旧表存在当成可直接回退。
- [ ] **Step 8: 运行脚本 / migration / release bundle 测试及隔离库完整演练。** Expected: 中断恢复、防重、最终可变行、源保护及未知 writer 的拒绝路径 PASS。release_bundle.check 仍只证明选定 Compose 制品 / 服务；独立 writer 清单与数据库稳定检查另列证据，不伪称自动发现全部未知实例。

### Task 8 / T8: 适配保留、清空与受理槽释放

**Files:** F15、F16、F20 retention 测试，原 BIN / coarse / AUTO / callback / reliable 保留测试。

**Interfaces:** 保持 callback.cleanup_snapshots / callback.cleanup_receipts / reliable.cleanup_terminal 名称和现有函数参数。F15 新增 run_history_cleanup(factory, days=90, *, now=None, batch=500) -> int；保护查询继续走原业务入口，不新增业务保留注册器。run_history_cleanup 管普通元数据，原快照任务管诊断正文，原资格 / 可靠清理沿原合同。

- [ ] **Step 1: 写 test_body_purge_and_new_reference_share_original_lock、test_expired_receipt_slot_reopens_without_releasing_command_identity、test_source_deadlines_are_not_reset_by_backfill、test_protected_first_batch_does_not_starve_later_rows。** PG 覆盖清理先锁与业务先锁两种交错，包含整数 / JSON canonical / predecessor / 当前锚点及原消费责任。

~~~python
assert protected_body_is_readable is True
assert new_receipt_id != expired_receipt_id
assert replayed_command_code == original_command_code
assert eligible_later_row_removed is True
~~~

- [ ] **Step 2: 运行 tests/tasks/test_integration_history_retention.py 和原 tests/businesses/bin_roller/test_four_table_retention.py，确认新模型上的交错断言先失败。**
- [ ] **Step 3: 候选轻读后沿原业务保护入口锁主体、重读，再锁可清理根 / 记录。** 不能先锁 history 再反向等 WorkLine。正文 PURGED 和删行同资格；共享正文合并全部仍需它的引用责任，不能只看外键，也不能因主体终态推定可删。
- [ ] **Step 4: 释放收据资格与普通摘要分开执行。** 到期且原保护允许才 receipt_active=False；命令角色无资格释放。原资格时间、诊断 received_at、可靠终态 updated_at 保持；recorded_at / 重报 / 补录不续期。ECS 原命令 / Result / 有效发送依据沿原长期保护，不受 7 / 90 天统一清理。
- [ ] **Step 5: 原清理入口适配统一正文及迁移来源。** 预补录期间尚未验证的来源不删；验证后旧诊断来源可按原期限清理，不能通过保留旧表扩大正式正文可用性承诺。来源删除与普通历史摘要到期都记录可核对结果，未知保护保留并推进批次。沿现有 Beat 入口承接 run_history_cleanup，不另建清理任务调度系统。
- [ ] **Step 6: 运行新 retention、原 callback / reliable / BIN / coarse / AUTO 专项，PG 交错实际执行。** Expected: 原保护 / 原锁序 / 恰好期限 / 过期后新身份行为全部 PASS，未配置跳过单列。

### Task 9 / T9: 验证查询与核心读取成本

**Files:** F03、F02、F20 service 测试、F18 testing 文档；只有实际计划证明缺失时才补所需索引。

**Interfaces:** 不增加新服务或基准框架；用现有 pytest 和隔离 PG 保存 EXPLAIN (ANALYZE, BUFFERS) 与原 / 新核心事务比较。产出制品 A（head=authority）的软件验收记录和不可变构建输入，尚不包含 T10 contract 迁移。

- [ ] **Step 1: 写 test_history_page_count_is_independent_of_reference_fanout、test_summary_queries_do_not_fetch_body、test_result_lookup_does_not_scan_diagnostic_timeline。** 数据包含 90 天选择性差异、同时间大量 UUID、单根多 reference / 大量诊断、较长原租约历史。断言长期行为和查询数量，不快照整段 SQL 字符串。

~~~python
assert page_item_ids == expected_distinct_ids
assert query_count_for_100 <= query_count_for_20 + fixed_batch_delta
assert fetched_payload_bytes_for_summary == 0
~~~

- [ ] **Step 2: 在显式隔离 PG 运行 tests/services/test_integration_history.py 与 tests/controllers/test_integration_history.py，先暴露 fanout / N+1 / 大字段加载问题。**
- [ ] **Step 3: 按已声明索引修正访问。** 原身份 / reference 有界索引、时间 / UUID keyset、Result semantic_digest 和每 lease 有效结论定向读取。列表显式选列，Body 仅在授权取正文时装配；不加通用 JSON GIN、Redis 缓存、全局成功投影或正文覆盖索引。
- [ ] **Step 4: 保存测量。** 记录数据行数 / 各 kind 比例、物理正文量、查询数、扫描 / 返回行、排序 / 临时文件、buffers、核心 gate / finish / Result 事务时长及锁等待，补录 / 清理批次时长。容量包含旧新并存、受保护存量、索引、WAL / 备份，不承诺未测压缩率。
- [ ] **Step 5: 跑制品 A 的完整软件验证。** 原创建 → 原 gate → 本地受控 HTTP → Result / 回调 → 业务消费 → 进程重启 → 受保护清理一条贯通；PG 实际执行，原 UNKNOWN / HELD 不自动换号重发。阈值以实际部署同类环境及原基线记录，不用空表结果判定达标。
- [ ] **Step 6: 完成制品 A 交付记录。** 标明 schema head=integrationhistoryauthority、现场状态和旧 writer 核对仍需真实运行证据；代码测试及演练不等于现场完成。检查差异并停在独立 T10 准入前，不自动在生产构建或发布。

### Task 10 / T10: 独立收缩并完成整体验证

**Files:** F06 的 contract、F07 退出旧实体 / 原文列、F19 / 原 migration 测试种子、F18；全部相关回归。这是制品 B，必须与 T9 制品 A 的启用边界分开，不能在同一默认 upgrade head 提前执行。

**Interfaces:** Consumes 已验证的统一读写与不可变来源映射；Produces 五实体的最终 schema 和全新 / 历史升级路径。所有业务公开 DTO 与接口不变；不提供自动旧权威反向迁移。

- [ ] **Step 1: 写 test_contract_refuses_unmigrated_or_inconsistent_evidence、test_contract_keeps_numeric_event_reads_and_long_result_replay、test_unsafe_downgrade_preserves_new_authoritative_facts。** 升级到 expand，造旧数据，copy / final verify / authority，再用公开入口创建 LIVE 新事实后尝试降级 / 收缩异常路径。

~~~python
assert unsafe_operation_succeeded is False
assert original_event_id_after == original_event_id_before
assert late_original_result_reply == saved_original_result_reply
assert new_live_fact_still_readable is True
~~~

- [ ] **Step 2: 运行 tests/migrations/test_integration_history.py 与 tests/migrations/test_project_schema.py，确认新的收缩保护断言先失败。**
- [ ] **Step 3: 在 contract 版本冻结准入检查。** 所有需要保留的来源已映射、原身份 / byte / 版本一致、未知来源异常已处理，且运行读写 / 清理全部新侧。生产执行前按 D6 再核对所有制品 A / 更早实例已排空；数据库检查不能冒充进程证明。原表仍有新读者、未迁移依据或回退会丢 LIVE 事实时拒绝。
- [ ] **Step 4: 退出五个旧实体及重复物理字段。** 删除 ecs_command / ecs_send_evidence / ecs_result_evidence / callback_receipt / callback_snapshot 旧表与 ORM 定义 / 导出，移除 reliable_message.request_raw / response_raw / last_response_raw，以及已改引用的接纳原文副本。保留 Transport 的原规范化业务事实。处理旧序列所有权，不删除统一 receipt 序列；删除前检查原整数 / JSON / FK 使用者，未知引用阻止收缩。
- [ ] **Step 5: 清理本次产生的孤立导入与夹具。** 历史迁移测试用独立旧版本 Table 种子，继续保留升级 / 降级行为回归；不留指向新模型但未加 role 的旧 ORM 名称别名。只删除本次变更使其无用的代码，不顺带改资产、RBAC 或审计模型。
- [ ] **Step 6: 在最终 schema 上完整回归。** 依次执行以下命令；只有实际配置的 PG / Redis 专项完成才记录通过，已知跳过注明原因：

~~~sh
rtk proxy uv run ruff check .
rtk proxy uv run ruff format --check .
rtk proxy uv run alembic heads
rtk proxy env TEST_DATABASE_URL="$INTEGRATION_HISTORY_TEST_DATABASE_URL" TEST_REDIS_URL="$INTEGRATION_HISTORY_TEST_REDIS_URL" uv run pytest -q tests/migrations
rtk proxy env TEST_DATABASE_URL="$INTEGRATION_HISTORY_TEST_DATABASE_URL" TEST_REDIS_URL="$INTEGRATION_HISTORY_TEST_REDIS_URL" uv run pytest -q
rtk git diff --check
~~~

Expected: Ruff 通过，恰好一个 integrationhistorycontract head，迁移 / 全量测试通过，必要 PG 用例无未解释跳过。重跑旧基线不能代替这次最终 schema 检查。

- [ ] **Step 7: 留存制品 B 与独立运行步骤。** 取得 T9 软件 / 实际启用证据和本次审批范围内的运行授权后，停写、检查原在途事实、执行 contract、启动与验证。切换后故障优先前向修复或使用同新存储合同的兼容版本；不直接启动只读旧表的镜像。记录代码 / CI / 演练 / 实际数据库 / 现场证据各自状态。
- [ ] **Step 8: 完成审查与交付。** 审查整个分支的事务 / 数据迁移 / 权限 / 字节与原身份合同，汇总通过 / 跳过 / 风险；用户明确要求后才提交或推送。按用户选择的执行方式完成逐项或最终独立代码审查，不能拿前一轮不可用的 Outside Voice 当通过。

## 决定与验收覆盖映射

| 已确认要求 | 承接任务 / 必需证明 |
| --- | --- |
| D1 / B 分层和少实体 | 文件图、T1 / T10；7 个对接实体最终 5 个 |
| D2 / A Result 归集、原 core / 重放 | T3 / T5 / T10，清理 / annex 缺失后仍原裁决 |
| D3 / A 收据根、数值 ID、资格释放 | T1 / T4 / T8 / T10，旧序列高水位与新实例 |
| D4 / A 命令根、原身份 / 冻结 / 恢复 | T2 / T3 / T5，原锁序与独立完整性 |
| D5 / A 纯重复尽力保存、核心原子 | T2 / T4，commit / 保存点 / PG aborted 会话 |
| D6 / A 维护窗口与稳定最终对账 | 三 revision / 两制品、T7 / T9 / T10，原在途不重置 |
| D7 / A 90 天摘要、来源原文 / 原保护 | T8，7 / 90 天与长期 ECS 分开 |
| D8 / A 发送证据归集及有效结论 | T2 / T3 / T9，同 lease 替代、新 lease、迟到 |
| D9 / A 快照归集、原预算与不猜关联 | T4 / T7，预览 / 空 / 未采集 / 内外层回复 |
| D10 / A 单次查询 90 天 | T6 / T9，参数、默认游标锚点、代表性 PG 计划 |
| D11 / C 共同理解及实施计划 | 本文件；保持原工程取值，不自动执行生产操作 |

## 实施前后证据与停止条件

- 本轮已完成计划的来源 / 调用 / 接口核对，仅用 SQLite 原生分配及游标编码探针确认实现机制，没有编写产品代码、新验收套件或迁移。
- 原设计记录的 82 passed / 49 skipped、6 passed / 6 skipped、20 passed / 19 skipped 是有重叠的旧行为基线，不相加，也不作为最终 schema 通过。
- 用户未要求提交 / 推送；原暂存、其他工作区和现场服务保持。
- 执行前核对实际 head / dirty；同一文件出现用户后续改动时保留，调整任务差异范围，不覆盖暂存。
- 缺少新任务的必需 PG 配置只暂停相应依赖，不能以 SQLite 替代并发 / schema 证明；不阻止尚不依赖该证明的文档或纯格式工作。
- 发现与已确认执行 / 幂等 / 恢复合同矛盾时停止相关实现并带具体失败证据返回设计，不自行删减功能、延长期限或放开重放。
- 数据源缺失、无法证明所有旧 writer 退出、最终对账不符、独立收缩条件不足时拒绝相应运行切换；保留原事实，不把“脚本完成”写成“现场完成”。

## 计划自审记录

- 已逐项覆盖 D1～D11、F-A1～A7、F-C1 / C2、F-T1～T8、F-P1～P3；没有未分配的必需行为。
- 所有新接口在跨任务合同或所属任务定义；后续任务使用同名参数 / 返回类型，不引入隐藏的事务、ORM 异步属性或第二发送器。
- 五项 Review Focus 分别落到具体任务和测试断言；新增持久化 / 并发 / 数据清理风险不靠模型存在性测试代替。
- 文件名为拟新增，命令为计划执行，不宣称本轮已创建迁移或通过新测试。历史迁移所有权清单及原公开接口的特殊行为已核对来源。
- 本计划完成后请用户审阅，并选择执行方式；当前尚未开始 T1。
