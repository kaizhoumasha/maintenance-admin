# 阶段二：WMS/Transport 原业务归属与即时唤醒 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans；2026-10-30 采用 executing-plans；阶段一复审提交 90861b7 后实施，经追加授权复审后已提交 a34b28d。Steps use checkbox (`- [ ]`) syntax for tracking。本阶段实现与隔离验收完成，已提交；不部署，不进入阶段三。

**Goal:** 同一共享 operation/Transport 被多个业务使用时，结果只唤醒创建它的原业务，且通知发生在有效事实提交之后。

**Architecture:** WMS 请求复用 business_key 保存本地原归属，可靠结果透传原关联；TransportTask 保存 nullable owner_domain，在统一结果应用接缝 after_commit。业务统一 scan_pending 与 5 秒 Beat 不变。

**Tech Stack:** 原 WmsRequests/Outbox/MessageStore/Transport、SQLAlchemy/Alembic、Celery、pytest；仅 Transport 原归属需本阶段新增 schema。

**参考：** [业务组合与原归属指南](../../integrations/business-packages.md)、[阶段一计划](2026-10-30-business-components-phase1.md)、[阶段二验收](../../testing/business-components-phase2.md)。

## Global Constraints

- 依赖阶段一 P1.1～P1.4 验收；只绑定实际业务，不创建组件 owner 或新调度器。
- 原 operation_id/client_request_id、冻结字节、input_digest、附录及发送策略不变；旧 None 不回填。
- reliable 模块只透传已有 opaque 关联，不解析 business 标记或导入 catalog/业务表；无第二 Sender/重试队列。
- after_commit 的原保存点/回滚、ignore_result=True、retry=False、零发布重试和 1 秒预算保持；Transport ACK 不触发回架完成。

## Review Focus

- 并发命中返回胜者的原 business_key，不能返回调用者新元数据——P2.1。
- 共享源无归属不可广播，业务专属旧 None 保留原来源通知——P2.2。
- 同 ID 换 owner 冲突，不能因历史 null 被自动接管——P2.3。
- Transport 结果先到暂存，位置补齐后才真正应用并通知——P2.4。
- 租约失效、回滚、broker 失败及重启不改原事实或重复外部意图——P2.2/P2.4。

---

## Task P2.1：WMS 原消息关联保存与重放核对

**范围/Files：** 修改 app/reliable/types.py、outbox.py、app/integrations/wms_request/service.py；测试 tests/reliable/test_enqueue.py、test_postgres.py、tests/integrations/wms_request/test_service.py、test_concurrency.py（新增，使用既有 conftest）。不新增归属表/列。

**依赖/Interfaces：** P1.1 catalog.contract_consumers；EnqueueResult(message_id,status,business_key:str|None=None) 增末尾可选只读字段。WmsRequests(outbox=None, *, clock=原默认, owner_domain:str|None=None, contract_consumers:Callable|None=None)；send/get_result 调用签名保持。

- [x] **Step 1：写失败测试。** test_enqueue_returns_original_business_key 在新建、同 ID 不同字节但 adapter 判等、并发胜者三路径返回原值，其他 protocol 原 business_key 不变。test_shared_request_owner_is_frozen 让 A/B 发同一共享 operation：不同 operation_id 各自归属，相同 ID 无论串行/并发均拒绝改业务，原行/请求字节保持。test_unbound_legacy_request_keeps_null 断言默认实例原 None 不回填。
- [x] **Step 2：运行 RED。** `rtk proxy uv run pytest tests/reliable/test_enqueue.py tests/integrations/wms_request/test_service.py tests/integrations/wms_request/test_concurrency.py -q`；PostgreSQL 并发用显式独立 TEST_DATABASE_URL，不能仅以 SQLite 证明唯一约束。
- [x] **Step 3：实现 opaque 返回值。** Outbox 新建/命中均返回持久 business_key，不改变其通用判重策略。WmsRequests 通过运行时注入的 contract_consumers 端口校验有 owner 的合同消费者并写 business:<业务码>（通用层不导入 catalog），比较 enqueue 返回值，不同抛 InvalidRequestPayload 的明确归属错误并回滚当前事务；不修改原消息。无 owner 的原业务专属/local 声明调用保留 None；新正式共享合同的 owner=None 发送明确拒绝，调试它也须使用受信业务装配，不能从 payload 任意传 owner。补 test_shared_formal_request_requires_bound_business，防止 global wms_requests 绕过原归属保存。
- [x] **Step 4：同专项 GREEN。** 加上 `rtk proxy uv run pytest tests/integrations/wms_request/test_adapter.py tests/integrations/wms_request/test_results.py tests/reliable/test_postgres.py -q`；确认 endpoint/TTL、adapter.same_request、请求时间戳和历史冻结内容未变化。

**完成门槛：** 元数据与请求同事务保存，所有重放看到原关联；尚不把此项当即时通知验收。

## Task P2.2：WMS 结果端口与业务运行时装配

**范围/Files：** 修改 app/reliable/store.py、app/reliable_runtime.py、app/business_wakeups.py、app/businesses/runtime.py、app/main.py、app/tasks/business_tasks.py；适配其测试注入端口，测试 tests/reliable/test_result_notifications.py、test_runtime.py、tests/businesses/test_runtime.py、tests/tasks/test_business_tasks.py；新增 tests/businesses/test_component_wms_wakeups.py。

**依赖/Interfaces：** P2.1；MessageStore/reliable_lifespan 的 on_result:Callable[[str,str|None],None]|None，两参数是原 operation/business_key。wms_result_committed(operation:str,business_key:str|None=None)->None。runtime 给每个业务构造 owner_domain=业务码 的 WmsRequests，仍共享原 Outbox，不改变业务工厂签名。

- [x] **Step 1：写失败测试。** test_shared_wms_result_wakes_only_original_business 通过 Outbox→实际 MessageStore 有效终态→通知→business.advance_pending→原业务组件验证，不只 mock enqueue。test_legacy_exclusive_result_uses_original_source 对专属旧 None 正确回归；test_unowned_shared_result_is_not_guessed 对共享 None/非法标记留安全诊断，不广播。test_lost_lease_and_rollback_do_not_wake/ test_publish_failure_keeps_result 覆盖失效租约、保存点、close 与 broker 异常。
- [x] **Step 2：运行 RED。** `rtk proxy uv run pytest tests/reliable/test_result_notifications.py tests/reliable/test_runtime.py tests/businesses/test_component_wms_wakeups.py tests/businesses/test_runtime.py tests/tasks/test_business_tasks.py tests/test_transactions.py -q`。
- [x] **Step 3：实现结果通知。** 在原有效非快照终态保存后透传原关联（finish、pending 过期及过期租约恢复/释放均覆盖，不改变原状态策略），可靠层不解析。平台解 business: 固定标记并核对实际业务/消费者；旧专属源码可按 RegistrationSource，旧共享缺归属不猜。用 P1.4 notify_business_committed 调同一发布器，运行时实例按业务绑定；不存在业务 progression 时诊断、保留原归属，不改投其他业务。
- [x] **Step 4：GREEN 并验证重建。** 原 pending 在通知丢失或任务重建后由同一 Beat 扫描，原 operation_id/消息数量不增加；测试中多个内部组件均属于同一个被唤醒业务。更新 outbound-results/业务指南的本地归属说明，不增加 wire 字段。

**完成门槛：** 共享 WMS operation 可精准即时通知原业务；原专属/local/default 行为有回归。

## Task P2.3：Transport 原业务字段与四方法一致性

**范围/Files：** 修改 app/models/transport.py、app/integrations/wms_transport/service.py、results.py、app/businesses/runtime.py；新增 migrations/versions/2026-10-30_transport_business_owner.py；测试 tests/integrations/wms_transport/test_service.py、test_rotate_and_bins.py、test_concurrency.py、test_results.py、test_predecessors.py；新增 tests/migrations/test_transport_business_owner.py。

**依赖/Interfaces：** 阶段一有效业务与 P2.2 每业务 Capabilities 装配；TransportService(outbox=None, *, target=None, clock=原默认, uuid7=None, owner_domain:str|None=None)。TransportTask.owner_domain nullable Text，结果只读投影 owner_domain:str|None=None。TransportCaller、四方法及 get_result/list_unclosed_predecessors 业务签名保持。

- [x] **Step 1：写失败测试。** test_all_transport_intents_keep_original_owner 覆盖 move_rack/rotate_rack/move_bins/exchange_bins；test_concurrent_transport_replay_cannot_change_owner 包含 None/非空和不同业务。test_legacy_null_and_frozen_digest_are_preserved 断言迁移后旧值为空、原 input_digest/request_raw/cycle/client_request_id 不变；前序公开读口不因字段增加丢失物理依赖。
- [x] **Step 2：运行 RED。** `rtk proxy uv run pytest tests/integrations/wms_transport/test_service.py tests/integrations/wms_transport/test_rotate_and_bins.py tests/integrations/wms_transport/test_concurrency.py tests/integrations/wms_transport/test_results.py tests/integrations/wms_transport/test_predecessors.py tests/migrations/test_transport_business_owner.py -q`。
- [x] **Step 3：实现 schema 与原子保存。** 迁移 down_revision 取实施时的唯一 head，不臆造 revision。_register 的新行、原命中及并发胜者都核对 owner，保持 wire/input_digest 不含该字段；runtime 为每业务绑定实例，local 默认 None。不把 owner 当权限，不导入业务到 Transport 库。
- [x] **Step 4：GREEN；串行迁移验收。** 在独立 PostgreSQL 做旧行升级、单 head、alembic check、空库升级，回退只在可丢弃库验证且不用于丢失新增 owner 的现场操作。迁移反射不能与会增删测试业务表的 pytest 并行。

**完成门槛：** 全部 Transport 意图有稳定原业务归属，新列不改变协议/身份；尚未新增唤醒。

## Task P2.4：Transport 实际应用通知与阶段故障验收

**范围/Files：** 修改 app/integrations/wms_callback/registry.py、receiver.py、app/controllers/wms_events.py、app/integrations/wms_transport/callback/base.py、app/business_wakeups.py、app/main.py；复用 callback/apply.py 原应用规则，只有必要返回值适配才修改。测试 tests/integrations/wms_transport/test_callback.py、test_app_wiring.py、tests/controllers/test_wms_events.py、tests/test_transactions.py；新增 tests/businesses/test_component_transport_wakeups.py。

**依赖/Interfaces：** P2.3 原 owner、P1.4 notify_business_committed；CallbackContext/receiver.receive 透传 on_owner_committed:Callable[[str],None]|None（扩展现有 keyword-only 参数），默认 None。统一 apply_callback 比较原 applied_outcome_revision，通知体只有业务码。

- [x] **Step 1：写失败测试。** test_transport_result_wakes_after_actual_application 用实际 resulted 回调，先缺 TARGET_PLACED 不通知，位置补齐后只通知一次；反向顺序同样通过。参数化 SUCCEEDED/FAILED/CANCELLED/UNKNOWN 及有效更高版本；重复/冲突/仅 ACK/位置事实/未知任务/frozen 缺失不冒充应用。test_transport_commit_failure_and_lost_notification 保证保存点/回滚/close 不通知，broker 失败仍保存结果与原 ACK，Beat 重建后恢复。
- [x] **Step 2：运行 RED。** `rtk proxy uv run pytest tests/businesses/test_component_transport_wakeups.py tests/integrations/wms_transport/test_callback.py tests/integrations/wms_transport/test_app_wiring.py tests/controllers/test_wms_events.py tests/test_transactions.py -q`。
- [x] **Step 3：统一接缝实现。** 结果和位置 handler 继续原 apply_callback，只在应用版本实际前进、原 owner 非空时同 session after_commit。Transport 不导入业务服务；平台 Transport 注册源自身不决定业务、也不额外广播。主应用既有 business_committed 端口由 WMS controller 透传为 on_owner_committed；业务 runtime/worker 继续按原业务绑定 Transport，无新增通知器或调度器；实际 main lifespan HTTP 用例覆盖装配。
- [x] **Step 4：专项及全链路 GREEN。** 测试注册 receiver→原持久结果→after_commit→真实共享任务体→业务 scan_pending→内部组件；保留无归属旧记录行为。测试真实 Celery/Kombu 内存 broker 及 Beat 注册/未注册任务两条 ignore_result=True 发布路径，不访问现场。
- [x] **Step 5：阶段出口验证。** `rtk proxy uv run pytest tests/reliable tests/integrations/wms_request tests/integrations/wms_transport tests/businesses tests/tasks/test_business_tasks.py tests/test_transactions.py -q`；通过后执行总计划静态检查与一次全量测试，串行 schema 检查。记录资源、失败/skip 与实际结果；实施时未提交，追加复审后已单独提交 a34b28d。

**阶段出口：** WMS/Transport 原业务归属、提交后即时通知及 5 秒同入口兜底通过隔离验收；没有新增生产业务，阶段三才接入回流。

## 实施与验收记录（2026-10-30，阶段二完成）

P2.1 205 passed；P2.2 55 passed，原粗分唤醒独立回归 33 passed / 1 SQLite 条件跳过；
P2.3 含专用 PostgreSQL/SQLite 迁移 170 passed；P2.4 专项 327 passed / 5 条件跳过。
综合回归补齐旧测试业务对共享请求的显式组件声明；有效本地 EXPIRED 的原关联通知有 6 项 RED→GREEN，
专项 47 passed。综合 1685 passed / 11 条件跳过；最终全量 **3788 passed / 39 skipped（299.81 秒）**。专库迁移/反射无漂移、唯一 head f0b120261030，独立终审无缺陷，详情见[验收记录](../../testing/business-components-phase2.md)。

实现裁决：通用 WMS 使用注入消费者查询端口；Transport 复用主应用 business_committed；新迁移保留 nullable 旧值，
非空原归属阻止降级删除。SQLite 验证本迁移往返，历史 audit 全链降级的 PostgreSQL LOCK 限制不在本次改动范围；
完整空库 base/head 与反射检查使用专用可丢弃 PostgreSQL，和业务测试串行执行。

阶段二完成状态：全部步骤已验证，已按追加授权复审并提交 a34b28d，不部署；原外部工作树及暂存内容保留，提交基线为阶段一 90861b7。
最终全量后确认独立组件测试表已清理，并删除本次两个测试容器；未触碰运行环境或现场数据库。

## 按计划验收复核与当前进度（2026-10-30）

结论：**P2.1～P2.4 全部通过隔离验收，代码已提交 a34b28d**；本轮复核无未满足的阶段出口或新增代码缺陷。

| 任务 | 复核的完成门槛 | 具体行为测试索引 |
|---|---|---|
| P2.1 | 新建/重放/并发返回原关联，消费者校验，换业务或接管旧 None 拒绝 | tests/integrations/wms_request/test_service.py、test_concurrency.py；tests/reliable/test_enqueue.py、test_postgres.py |
| P2.2 | 逐业务装配、共享结果只唤醒原业务；旧专属 None 兼容，共享无归属诊断；所有有效非快照终态提交后通知 | tests/businesses/test_component_wms_wakeups.py；tests/reliable/test_result_notifications.py（含 EXPIRED 三路径、失效租约、提交/发布失败） |
| P2.3 | Transport 四方法与唯一约束胜者核对原 owner；原协议/身份保持、历史 None 不回填、非空归属阻止降级删除 | tests/integrations/wms_transport/test_service.py、test_concurrency.py；tests/migrations/test_transport_business_owner.py |
| P2.4 | 结果实际应用版本前进才唤醒；结果/位置乱序、事务失败、丢通知恢复及实际 HTTP/内存 broker/任务体装配 | tests/businesses/test_component_transport_wakeups.py；tests/businesses/test_coarse_sorting_application.py |

计划原列 main.py / business_tasks.py 的范围经核对复用了既有装配和任务，无需为满足文件清单而修改；实际新增端口由 WMS 控制器透传原 business_committed。通用模块只透传原关联，不导入业务目录。

提交前最终联合全量：**3791 passed / 36 skipped（322.09 秒，exit 0）**；此前三项独立迁移库回归已补齐。36 项仅是 SQLite 不适用的 PG 分支，其 PG 形态已执行。迁移 upgrade/check/唯一 head f0b120261030/downgrade 已在独立 PG 串行通过；运行环境未应用迁移。

本次核对源码与上述测试版本一致，沿用已完成验收证据；前次进度复核未重复全量；本次 ship 另行验证并提交相关文档。详细范围及剩余环境边界见[阶段二验收记录](../../testing/business-components-phase2.md)。阶段三、生产业务注册、部署和现场验收保持未完成。
