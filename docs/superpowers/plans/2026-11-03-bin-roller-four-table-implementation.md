# BIN_Roller 四表重构实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking. 不默认派发子代理、提交、推送或部署。

**Goal:** 按四表设计实现当前调度、最小公共 EVENT/停止发送能力和完整验收；旧表数据不迁移，只做无旧在制责任的 schema 切换。

**Architecture:** 四表保存当前业务检查点；公共层保存原事件、消息、命令和搬运。业务登记与任务终态使用同一原锁序，关闭事务停止匹配的调度消息；Sender 只校验消息/租约，不回查业务状态。独立 LINE 保留自己的事实和动作，绝不接管已终止 TASK 的执行权。

**Tech Stack:** 现有 Python 3.13+、FastAPI、SQLAlchemy、Alembic、PostgreSQL、Celery、pytest/pytest-asyncio、Ruff；不新增依赖。

**Spec:** [四表实体与状态机设计](../specs/2026-11-03-bin-roller-four-table-design.md)，含本轮再审查后的 §8～9、§11～12 与字段精简清单。

日期：2026-11-03。状态：**本地软件实施与验收完成**。以 `caca3c0` 为基线的独立工作树中完成修改；旧数据不迁移。未提交、推送或部署。实际检查与待验边界统一记录在 [软件验收记录](../../testing/2026-11-03-bin-roller-four-table-acceptance.md)。

## Global Constraints

- WES 不建立永久业务历史；原 TASK 终态后不恢复旧调度，后续业务安排归 WMS。
- SCAN1/SCAN2 同线同箱、原任务一致、原方向向前、唯一合格前驱才关联；歧义独立保存，不新增协议字段或上游 Result 门槛。
- 四表固定为 bin_passage/bin_return/face_work/workline_state；不新增 scope 表、历史表、别名表、第二 Sender 或重试器。
- **旧表数据不迁移**：无原 ID 映射、收据恢复、scope 回填、旧 FIFO/WAIT 复制、旧步骤重建或兼容字段。
- 切换前无旧 BIN 执行/发送/宿主活跃引用；旧私表可有可弃历史，公共表及其他业务不清空。发现旧责任则拒绝切换，不自动终止或假造完成。
- TASK 面中的调度意图归 TASK，DRAIN/独立扫码归 LINE；转存引用不改变动作归属。事实报告/ACK/证据交付不批量贴上可终止 TASK 标签。
- 范围关闭与新增意图共用 WorkLine/原任务锁，所有入口锁后重验；Sender 不通过 lock_scope/scope_status 回调读取业务。
- 两类发送均以原状态/租约及持久派发起点核对。ABANDONED 仅表示停止后续投递，不能召回已获得派发权的请求。
- receiver 原事务不换隔离；进入架流/供回箱集合判定的独立 advance 首 SQL 前 SERIALIZABLE，纯消费沿原 RR。40001 包含 commit，最多三次新 session 整事务重试。
- 锁序保持 WorkLine → Devices ID → RackJobs ID → 宿主 → 四表固定序/主键 → RackActions ID；框架原生命周期锁在对应资源锁后，不反向锁业务。
- 四表只保存设计列；关闭统一由 closed_at/close_reason 表达，在制 CHECK/步骤索引排除关闭行，不补回重复状态。
- 新运行登记后原外部身份/字节/target/期限冻结；无论 TASK 是否关闭，不能篡改原物理结果。
- 所有 SQL revision 追加执行时真实唯一 head，历史 revision 不改。当前 sharedrackdispatch 仅是源码基线提示。
- 保留已有 dirty/staged/untracked；执行时按仓库规则隔离 feature 分支。所有复选框完成默认仅表示工作区验证，不自动 commit/push。

## Review Focus

1. 新 SCAN1/等价 SCAN2 与唯一前驱匹配并发：集合锁和本点去重必须阻止误关联/重复委托；Task 5。
2. 关闭与新增意图/claim/gate 竞争：业务锁封住新登记，消息锁封住未取得派发权的尝试；Task 3/7。
3. 部分 READY 与成员部分失败：完成集合仅为获准前缀，尾部不被错误等待，部分退出不等于整批成功；Task 6。
4. TASK 停止后把引用留在 LINE：只能进入 WAIT_FACT 解释新事实，不能重新发送或创建旧步骤后继；事实报告不能误停；Task 7/9。
5. 不搬旧数据被误实现为共享清库或新版本继续跑旧队列：切换前拒绝旧活跃责任、保全其他业务，数据为空的新模型才允许 schema 降级；Task 8。

## 1. 任务顺序与完整切换

| Task | 交付物 | 依赖 |
| --- | --- | --- |
| 0 | 基线/合同及无旧责任切换前提 | 无 |
| 1 | 最小公共元数据/停止类型与 schema A | 0 |
| 2 | 三列 EVENT 原记录与无状态读口 | 1 |
| 3 | 普通/snapshot 消息门禁与 stop_scope | 1 |
| 4 | 四表模型/约束与原引用仓储 | 1 |
| 5 | 横表匹配、本点接缝和 FIFO | 2、4 |
| 6 | 面作业/排空/可服务架 | 3、4、5 |
| 7 | 动作归属、宿主终态、WAIT_FACT 与 pending | 3、5、6 |
| 8 | 不搬数据的 schema B 与共享数据保全 | 2～7 |
| 9 | 本批活跃引用保护与有限清理 | 2、3、7、8 |
| 10 | 最终回归和切换文档 | 0～9 |

同一隔离分支完成全部切换后再交付；Task 4～8 中间状态不得独立部署。允许公共增量 A 单独评审，但业务切换 B 必须包含四表、宿主和清理适配。没有新旧双运行/模式开关，不承诺中间快照兼容旧 runtime。

## 2. 文件与最小接口

| 范围 | 修改/新建路径 | 边界 |
| --- | --- | --- |
| EVENT | `app/models/callback.py`、`app/integrations/callback_receipts.py`、`app/integrations/ecs_callback/receiver.py`、`registry.py`、`__init__.py`；新建 `app/integrations/ecs_callback/results.py` | 三个 EVENT JSON 列、原应用关联、纯读函数；无全仓 Capabilities.events 改造 |
| 可靠消息 | `app/models/reliable.py`、`app/reliable/types.py`、`__init__.py`、`outbox.py`、`store.py`、`delivery.py`、`lifecycle.py` | 可终止消息标签、ABANDONED、派发起点；不导入业务 |
| 原协议门面 | `app/integrations/wms_request/service.py`、`results.py`；`app/integrations/wms_transport/service.py`、`results.py`；`app/integrations/ecs_command/service.py`、`contracts.py`、`lifecycle.py` | 显式透传可终止调度范围、原结果投影；不改变 wire/TTL，不给所有 facts/report 加范围 |
| 业务 | `app/businesses/_components/bin_roller/` 的 models/repository/ports/service/scan_flow/feed_flow/return_flow/rack_flow/composition | 现有模块内实现四表，不新建实体/执行器框架 |
| 架调度 | `app/businesses/_components/rack_dispatch/models.py`、`contracts.py`、`service.py` | 原范围/CLOSED_BY_SCOPE、可服务架投影 |
| 宿主/装配 | `app/businesses/smt_manual/` 的 roller_ports/manual_bin_flow/task_flow/repository/service/manual_rack_flow；`app/businesses/runtime.py`、`app/main.py`、`app/tasks/business_tasks.py` | 原事务/锁序、任务关闭、统一 pending；没有 ScopeStatus 授权回调 |
| 保留/清理 | 新建 `app/businesses/_components/bin_roller/retention.py`；修改 `app/tasks/callback_tasks.py`、`reliable_tasks.py`、`app/reliable/store.py` | 固定本批保护集合和同事务删除；不是通用多资源回调分派器 |
| schema | 新建 `migrations/versions/2026-11-03_bin_roller_runtime_foundation.py`（A）、`2026-11-03_bin_roller_four_tables.py`（B） | 公共增量及旧结构替换，无数据搬运 |

固定新增接口（DTO 为 frozen 值对象，非新表；其余现有参数保持）：

```python
# app/reliable/types.py
ScopeRef(owner_domain: str, kind: Literal["TASK", "LINE"], scope_id: str)
ScopeStopItem(message_id: int, previous_status: MessageStatus,
              dispatch: Literal["NEVER_DISPATCHED", "MAY_HAVE_DISPATCHED", "TERMINAL"])
# MessageDraft/OutgoingMessage.scope: ScopeRef | None = None

# app/integrations/ecs_callback/results.py，无状态模块函数
EventRecord(event_id: int, digest: str, received_at: datetime, envelope: dict,
            context: dict, application_ref: EventApplicationRef | None)
EventReadResult(availability: Literal["FOUND", "NOT_FOUND", "UNAVAILABLE", "INVALID"],
                record: EventRecord | None)
EventApplicationRef(subject_kind: str, subject_id: str, canonical_event_id: int,
                    predecessor_event_id: int | None, separator_command_code: str | None)
get_event(session: AsyncSession, *, event_id: int) -> EventReadResult
list_point_events(session: AsyncSession, *, owner_domain: str, work_line_id: int,
    point: str, before_event_id: int | None = None, limit: int = 100) -> tuple[EventRecord, ...]
# ctx.bind_application(reference: EventApplicationRef) -> None，不增加 scope 参数

# app/reliable/outbox.py
Outbox.stop_scope(session: AsyncSession, scope: ScopeRef, *, reason: str)
    -> tuple[ScopeStopItem, ...]

# bin_roller/ports.py
Production.resolve_scan2_task(session: AsyncSession, *, work_line_id: int) -> CurrentTask
Production.accept_scan2(ctx: EcsEventContext, arrival: Scan2Arrival) -> str
# 关闭由宿主原终态事务调用；不添加 Production.read_scope_status 或 runtime.lock_scope
BinRoller.close_scope(session: AsyncSession, scope: ScopeRef, *, reason: str) -> None
BinRoller.consume_return_fact(session: AsyncSession, *, return_id: str) -> None
BinRoller.cleanup_closed(session: AsyncSession, *, cutoff: datetime, limit: int = 500) -> int

# rack_dispatch/service.py，owner 已绑定实例
RackDispatch.register(session, origin: JobOrigin, *, scope: ScopeRef | None = None) -> str
RackDispatch.close_scope(session: AsyncSession, scope: ScopeRef, *, reason: str) -> None
RackDispatch.get_point_preparations(session: AsyncSession, *, work_line_id: int,
    point: str) -> tuple[RackProgress, ...]
```

WmsRequests.send、EcsCommands.create、TransportService 的各登记方法增加可选 `scope` 关键字；它只标签可终止调度意图，重复身份 scope 不同冲突。FACTS 及独立报告沿原可靠义务，不由 TASK 关闭批量停发；对本次正式到位报告调用者显式验证 scope=None，不在可靠层硬编码业务 operation。EventRecord.received_at 是既有原 created_at 投影，不是新数据库时间列。

保留查询放在固定 bin_roller/retention.py：`protected_event_ids(session, *, candidate_ids: tuple[int, ...]) -> frozenset[int]`、`protected_message_ids(...) -> frozenset[int]`，以及关闭行清理。框架清理只接收 `protected_ids` 本批集合，不回调任意业务方法。调度/清理装配选择本批相关 WorkLine 后按同一锁序锁定，收集引用并在同事务确认/删除候选；不使用事务外快照做最终删除。

## Task 0：基线与无旧责任切换前提

**Files:** Read `AGENTS.md`、设计 §14、当前 head、`docs/integrations/workline-bin-return-api.md`、`outbound-results.md`；只记录计划执行证据。
**Interfaces:** 产出真实单 head、隔离工作树、测试资源和切换前提。

- [x] **Step 1 — 核对并记录。** 分支/dirty/工作树与唯一 head；执行时隔离 feature 分支，保留无关工作。
- [ ] **Step 2 — 外部启用合同待验证（不作为本地软件通过）。** FIFO 首次到位排序与 API 文字对齐；有限重报窗口/超窗处理及 task_id 不复用仍待供应商/WMS 确认。无新增协议字段；本次仅做可丢弃测试，不正式启用新模型。
- [x] **Step 3 — 明确旧数据处置范围。** 只丢旧 BIN 私表历史；无旧待发/可能已接纳动作和宿主活跃引用才切换。发现旧责任不能“按新任务终态规则猜掉”，由另行授权运维处理。
- [x] **Step 4 — 核对测试资源。** 仅显式可丢弃 PostgreSQL/Redis/迁移库；未配置 skip，配置失联 fail。此次不访问/迁移运行库。

**Check:** `rtk git status --short --branch`；`rtk uv run alembic heads`。预期一个真实 head；本步骤只读。Task 8 的 upgrade/downgrade 仅针对显式可丢弃测试库。

## Task 1：最小公共元数据和 schema A

**Files:** Modify `app/reliable/types.py`、`__init__.py`、`app/models/callback.py`、`reliable.py`、`app/businesses/_components/rack_dispatch/models.py`、`contracts.py`；Create A、`tests/migrations/test_roller_runtime_foundation.py`；Modify `tests/reliable/test_enqueue.py`、`tests/businesses/rack_dispatch/test_constraints.py`。
**Interfaces:** ScopeRef/ScopeStopItem、消息范围标签、三个 EVENT 列、RackJob 关闭语义；无业务状态授权桥接。

- [x] **Step 1 — 写失败用例并运行。** 相同身份/范围幂等、换范围冲突；半空范围拒绝；非 EVENT 可空新列；CLOSED_BY_SCOPE 不伪造 DEPART 接纳。
- [x] **Step 2 — 加公共 schema。** MESSAGE 的 scope_owner/kind/id 和 nullable dispatch_started_at；ABANDONED 排期/终态 CHECK；EVENT 仅 envelope/context/application_ref；RackJob 原范围/关闭约束。旧公共行不回填，不改原字节或结果。
- [x] **Step 3 — 精简类型。** scope 只是不变标签；无 ScopeStatus、scope 授权调度器、通用 resource_kind 接口或 Capabilities.events。
- [x] **Step 4 — 验证 A。** 空库/有其他业务公共数据增量升级；新语义无法由旧 schema 表达时降级前拒绝；此时不启动业务四表 runtime。

**Run:** `rtk uv run pytest tests/reliable/test_enqueue.py tests/businesses/rack_dispatch/test_constraints.py tests/migrations/test_roller_runtime_foundation.py -q`。
**Expected:** 最小元数据约束通过，共享公共记录保全。

## Task 2：公共 EVENT 三列与纯读函数

**Files:** Modify `app/integrations/callback_receipts.py`、`app/integrations/ecs_callback/receiver.py`、`registry.py`、`__init__.py`、`app/controllers/dev_ecs_event.py`；Create `app/integrations/ecs_callback/results.py`、`tests/integrations/ecs_callback/test_results.py`；Modify `tests/integrations/ecs_callback/test_receiver.py`、`test_reliability.py`、`tests/controllers/test_ecs_callback.py`、`test_dev_ecs_event.py`。
**Interfaces:** §2 的 get_event/list_point_events、EventRecord/application_ref；不要求所有 Capabilities 构造者变化。

- [x] **Step 1 — 写并运行失败测试。** 原事件重报在新 resolver 之前命中原 ACK；handler 失败时事件/业务/outbox 一起回滚；读口无 autoflush/提交/网，副本不污染原记录。
- [x] **Step 2 — 在 receiver 同事务保存。** 完整有界规范事件、最小原路由/版本和业务关联；不复制全配置、消费状态、scope 列；原 digest/256 KiB/ACK/注册不变。
- [x] **Step 3 — 实现公开纯读。** 原 ID 精确读取；本点 canonical 首次受理顺序分页，不被技术重报刷新、不因单页去重丢事实。旧收据内容为空返回明确 UNAVAILABLE，不能恢复/拼接历史。
- [x] **Step 4 — 接入调试并复核。** 保留未注册/invalid/oversized 与 snapshot 原策略；不新增未知事件 replay。业务只调用公开读函数。

**Run:** `rtk uv run pytest tests/integrations/ecs_callback tests/controllers/test_ecs_callback.py tests/controllers/test_dev_ecs_event.py tests/migrations/test_ecs_event_receipt.py -q`。
**Expected:** 原证据/幂等闭合，无全仓事件能力装配改造。

## Task 3：消息门禁和 stop_scope，不回查业务

**Files:** Modify `app/reliable/outbox.py`、`store.py`、`delivery.py`、`lifecycle.py`、`app/integrations/ecs_command/lifecycle.py`；Create `tests/reliable/test_scope_stop.py`；Modify `tests/reliable/test_delivery.py`、`test_leases.py`、`test_postgres.py`、`test_retention.py`、`tests/integrations/ecs_command/test_snapshot.py`。
**Interfaces:** stop_scope、普通/snapshot 消息状态/租约门禁；不存在 lock_scope/session 授权回调。

- [x] **Step 1 — 写失败用例并运行。** 两类消息都覆盖 claim→stop→gate（零次 HTTP）、gate→stop（最多原次，不重试）、关闭回滚→gate；晚到 finish/recover/maintain/resume/settle 不能复活 ABANDONED。
- [x] **Step 2 — 冻结标签。** Draft/OutgoingMessage/Claim 传 scope；business_key 不变。Sender 仅核对消息原身份、状态/租约，scope 只用于 stop 精确选择。
- [x] **Step 3 — 实现持久派发起点。** gate 在框架事务中确认原消息/租约，仅对带范围标签的消息设置首个 dispatch_started_at，提交后允许原尝试；未标签普通/snapshot 消息保留原保守语义，不扩大新增派发证据及降级限制；无数据库事务/连接跨 HTTP。last_attempt_at/attempts 不能冒充真正派发证据。
- [x] **Step 4 — 实现 stop。** PENDING/SENDING/STALLED/HELD 的精确标签匹配行停止；snapshot 沿原 lifecycle 锁序，保留既有冻结路由/角色/代次授权门禁及真实证据。ABANDONED 单独处理，不能走默认状态映射把实际 DISPATCHING/WAITING_RESULT 重置 QUEUED；原 Result/核对事实不改。空派发起点且无矛盾原证据才 NEVER_DISPATCHED；TERMINAL 不等于物理完成。
- [x] **Step 5 — PostgreSQL 交错。** 原 message gate 与 stop 争消息锁，业务不在 Sender 内被查询/锁定；旧租约不能越过 stop，新事务不为旧任务重新入队（后者由 Task 7 业务锁验证）。

**Run:** `rtk uv run pytest tests/reliable/test_scope_stop.py tests/reliable/test_delivery.py tests/reliable/test_leases.py tests/reliable/test_postgres.py tests/integrations/ecs_command/test_snapshot.py -q`。
**Expected:** 两条真实发送路径闭合，非 scope 和原 ECS HOLD/UNKNOWN 行为不被放宽。

## Task 4：四表与原引用仓储

**Files:** Modify `app/businesses/_components/bin_roller/` 的 models/repository/ports/service/composition、`tests/businesses/bin_roller/conftest.py`、`tests/helpers.py` 的对应装配；Create `tests/businesses/bin_roller/test_model_constraints.py`。
**Interfaces:** 四表键 bin_passage/bin_return/face_work/workline_state，列以设计 §3～7/15 为唯一清单。

- [x] **Step 1 — 写并运行约束失败测试。** 关闭行保留阶段且清引用合法；未关闭 MOVING 缺 Transport、跨 owner/线引用、当前步骤冲突拒绝；WAIT_FACT 无旧选择但有原事实引用合法；同箱关闭旧轮不占当前根唯一键，新轮可登记。
- [x] **Step 2 — 定义四表。** 无 origin_service_id、FaceWork.source_ref、drain_source_ref、旧 ID 映射或别名；scope_kind、终态、分段范围等按设计推导。
- [x] **Step 3 — 实现状态/身份仓储。** 初次登记生成新 ID，重复当前输入用原唯一依据；旧状态不能伪造新外部身份。WAIT_FACT 与 MOVING 区分的是执行权，不是重复的错误状态。
- [x] **Step 4 — 适配 fixture。** 独立 MetaData/显式清理，测试不互相导入；允许更新物理表键，不放宽业务断言，不建八表影子。

**Run:** `rtk uv run pytest tests/businesses/bin_roller/test_model_constraints.py tests/businesses/bin_roller/test_identity.py tests/businesses/bin_roller/test_supply.py -q`。
**Expected:** 四表及在制约束可表达设计；仅对应当前分支测试，不单独部署中间态。

## Task 5：唯一前驱、SCAN2 与回流 FIFO

**Files:** Modify `app/businesses/_components/bin_roller/scan_flow.py`、`repository.py`、`ports.py`、`service.py`；`app/businesses/smt_manual/roller_ports.py`、`manual_bin_flow.py`；`app/integrations/ecs_command/contracts.py`、`service.py`；Create `tests/businesses/bin_roller/test_passage_matching.py`；Modify 同目录 test_scans/test_scan2_port/test_scan2_release/test_identity/test_rounds/test_rescan/test_fifo/test_recorded_scan3_reentry。
**Interfaces:** Production.resolve_scan2_task/accept_scan2；命令原 task_type/device_code 投影；一个最终 passage_id。

- [x] **Step 1 — 写失败用例。** 唯一同线同箱/原任务/向前命令前驱才合并；零/多候选、任务不同、旁路、不可读独立；不等 SCAN1 Result，先 SCAN2 后 SCAN1 不反向补绑。
- [x] **Step 2 — 实现本点优先去重。** 原 receiver 事务按 WorkLine→Devices→宿主→Passage 锁集合，先解析原任务，再重验唯一候选，最后以最终 passage_id 委托；不新增 wire 字段/临时换号。
- [x] **Step 3 — 验证并发。** 等价 SCAN2 只委托一次，新 SCAN1 不能穿过候选集合锁；技术失败整笔回滚。使用独立 PG 会话和屏障，不用 sleep 充当证明。
- [x] **Step 4 — 实现回流本点状态。** SCAN3 无任务独立；SCAN4 不等 SCAN3 Result；孤立 SCAN4 阻挡 FIFO；C07 真失败重扫保留首次序，新轮不继承旧命令。每次原应用关联同事务保存。
- [x] **Step 5 — 验证点位锚点。** 本点最新已消费 canonical 经过仍是后续判等依据，不能因它“已消费”提前清掉；重复/同码重扫/A→B→A 不误判。

**Run:** `rtk uv run pytest tests/businesses/bin_roller/test_passage_matching.py tests/businesses/bin_roller/test_scans.py tests/businesses/bin_roller/test_scan2_port.py tests/businesses/bin_roller/test_scan2_release.py tests/businesses/bin_roller/test_fifo.py tests/businesses/bin_roller/test_rounds.py tests/businesses/bin_roller/test_rescan.py tests/businesses/bin_roller/test_recorded_scan3_reentry.py tests/integrations/ecs_command/test_query.py -q`。
**Expected:** 匹配和独立点位规则同时成立，无新增设备协议或恢复旧数据分支。

## Task 6：FaceWork、排空和公共架位

**Files:** Modify `app/businesses/_components/bin_roller/feed_flow.py`、`return_flow.py`、`rack_flow.py`、`repository.py`、`service.py`；`app/businesses/_components/rack_dispatch/service.py`、`contracts.py`；Modify `tests/businesses/bin_roller/test_feed.py`、`test_return_batch.py`、`test_interleave.py`、`test_drain.py`、`test_rack_lifecycle.py`、`test_facts.py`、`test_concurrent_progression.py`、`tests/businesses/rack_dispatch/test_dispatch.py`。
**Interfaces:** 当前步骤/窗口/供箱进度、公开 get_point_preparations；新原需求来源用 FaceWork.id，DRAIN 来源用 drain_operation_id。

- [x] **Step 1 — 写失败行为用例。** 4+1、段间至多一批、面末多批、NO_BATCH 不重开；未知原步骤不替换；已完成供箱不等于面完成。
- [x] **Step 2 — 实现串行检查点。** 原清单+不提前移动的 feed_cursor 确定供箱区间；原 Transport 确定成员；成功消费/开窗/后继同事务。无分段历史、重复 manifest 或 REVIEW 枚举。
- [x] **Step 3 — 实现严格获准前缀。** return_members 最多四个原 round；READY 有序获准成员和目标槽严格匹配；只等该执行集合，尾部恢复资格。成员已知退出不伪造整批成功；旧 step 不能推进新批次。
- [x] **Step 4 — 实现排空上下文。** 原未知/WAIT 恢复，retry_at 不刷新；READY 游标与精确 source/rack 查询恢复作业，接纳离场/推进候选同事务；提前排空不建剩余架，不要求新旧来源兼容列。
- [x] **Step 5 — 替代位置副本。** 原 ACTIVE、current preparation、严格位置/面/版本及无冲突才授权；多匹配报冲突。旧 CTU02/03/INLET 迟到不覆盖后到事实，不取最新 job、不建第五张关系表。

**Run:** `rtk uv run pytest tests/businesses/bin_roller/test_feed.py tests/businesses/bin_roller/test_return_batch.py tests/businesses/bin_roller/test_interleave.py tests/businesses/bin_roller/test_drain.py tests/businesses/bin_roller/test_rack_lifecycle.py tests/businesses/bin_roller/test_facts.py tests/businesses/bin_roller/test_concurrent_progression.py tests/businesses/rack_dispatch/test_dispatch.py -q`。
**Expected:** 正常流程和负向规则通过，独立责任不因合表消失。

## Task 7：业务终态、动作归属与 WAIT_FACT

**Files:** Modify WmsRequests/EcsCommands/TransportService 原登记/结果门面、BIN_Roller/rack_dispatch 服务、`app/businesses/smt_manual/` 的 task_flow/repository/service/roller_ports/manual_rack_flow；按既有 `app/businesses/runtime.py`、`app/main.py`、`app/tasks/business_tasks.py` 更新实际调用，不添加 scope 授权服务；Create `tests/businesses/bin_roller/test_scope_close.py`；Modify test_recovery/test_cancellation/test_application/test_production_integration、`tests/tasks/test_smt_manual_worker.py`、`test_business_tasks.py`。
**Interfaces:** stop_scope、BinRoller.close_scope/consume_return_fact、RackDispatch.close_scope；没有 read_scope_status/lock_scope 桥接。

- [x] **Step 1 — 固定归属测试。** SUPPLY 窗口中 feed/return 调度意图为 TASK，DRAIN 为 LINE；独立 SCAN3/4 不随无关 TASK 关闭；已提交 facts/report 和结果 ACK 不被批量停掉。
- [x] **Step 2 — 接入原终态事务。** 真正写 COMPLETED/CANCELLED 时先持原 WorkLine/任务锁，stop 该范围消息、关闭业务后继及原 RackJob 同事务提交。所有创建 TASK 意图入口锁后重验；成员撤回不误关整 TASK。
- [x] **Step 3 — 验证登记竞争。** 新消息先提交则终态可见并停止；终态先提交则登记方不得新增。关闭回滚不留下半闭合/ABANDONED；发送与业务锁序无环。不得依赖 Sender 查询宿主兜底。
- [x] **Step 4 — 实现 TASK 选中 LINE 箱的收尾。** 未登记 Transport 或明确 NEVER_DISPATCHED 时解除选择，按原资格恢复；可能已发时进入 WAIT_FACT，清旧 FaceWork 选择但保留原结果关联，消息仍是原 TASK 且不重试。
- [x] **Step 5 — 实现事实消费，禁止旧动作恢复。** WAIT_FACT 分支仅用匹配原结果/新可靠物理事实更新 FIFO，不调用 send/create/move/prepare/release。沿原 business 通知和既有 pending/Beat 可消费已到事实，不新建对账/专门轮询任务，不创建旧 TASK 后继；未到事实不因此重新投递。确定在源且资格仍有效才 READY；原最终事实明确且已被真实新轮替代时，以 SUPERSEDED 关闭旧检查点并保留原关联；确定退出则关闭，位置未知保持 WAIT_FACT。
- [x] **Step 6 — 同步宿主/worker。** PASSAGE/RETURN/FACE/WORKLINE/RACK_JOB 分页，事实消费与动作推进在同入口分支明确区分；RR/SERIALIZABLE/三次整事务恢复保留；独立 LINE 自己的有效 DRAIN 动作可正常恢复。显式停止 LINE 也不能删除未知物理队首；新范围仍看见原 WAIT_FACT，不因重置运行 ID 误判空通道。

**Run:** `rtk uv run pytest tests/businesses/bin_roller/test_scope_close.py tests/businesses/bin_roller/test_recovery.py tests/businesses/bin_roller/test_cancellation.py tests/businesses/bin_roller/test_application.py tests/businesses/bin_roller/test_production_integration.py tests/businesses/smt_manual tests/tasks/test_smt_manual_worker.py tests/tasks/test_business_tasks.py -q`。
**Expected:** 旧 TASK 无执行权；LINE 不被误删也不继承旧执行权；事实可靠交付未被吞掉。

## Task 8：无数据搬迁的 schema B

**Files:** Create B、`tests/migrations/test_bin_roller_four_tables.py`；复用 `tests/migrations/conftest.py`，同步 `tests/migrations/test_project_schema.py`。
**Interfaces:** A→B 连续追加真实 head。迁移自有冻结表定义；不导入当前业务服务、调用 Sender 或写回旧记录。

- [x] **Step 1 — 写 DDL 测试并确认失败。** 空库直接 head；旧结构带可弃历史可替换；新四表为空；旧表不残留；关闭行/唯一索引符合 metadata。
- [x] **Step 2 — 写共享数据保全用例。** 旧库预置其他 owner/非 ROLLER RackJob、收据、WMS 消息、ECS/Transport 证据；A/B 后原行数、原字段/正文哈希不变。旧公共 NULL 新列保持 NULL，不回填。
- [x] **Step 3 — 写旧责任拒绝用例。** 由旧私表原 ID 定位仍待发/可能已接纳的 BIN 命令/消息/作业，或宿主活跃引用时，首个破坏性 DDL 前失败；B 执行前原 schema/数据/head 保持（已完成的 A 可保留，不要求撤回整个 A→B 链）。未决清除归另行授权运维，不在迁移里 stop/删除共享行。
- [x] **Step 4 — 实现 schema 替换。** 无 COPY/INSERT SELECT/循环映射、旧 receipt 恢复、原 scope/FIFO/WAIT 搬运。处理 face_work 同名替换、依赖删除顺序，不用 CASCADE 隐式丢其他业务；正式 runtime 无双路径。
- [x] **Step 5 — 验证 DDL 失败与降级。** PG 事务失败回滚，SQLite batch 路径明确验证；空新结构可重建空旧表，但不承诺恢复已丢弃历史。新四表非空/公共新语义不可表达时，任何破坏性 downgrade 前拒绝。

**Run:** `rtk uv run pytest tests/migrations/test_roller_runtime_foundation.py tests/migrations/test_bin_roller_four_tables.py tests/migrations/test_project_schema.py -q`。
**Expected:** 单 head、四张新空表、无旧行转换、其他业务原数据保全、旧责任阻止切换；无任何外部发送。

## Task 9：固定活跃引用集合与有限清理

**Files:** Create `app/businesses/_components/bin_roller/retention.py`、`tests/businesses/bin_roller/test_retention.py`；Modify `app/tasks/callback_tasks.py`、`reliable_tasks.py`、`app/reliable/store.py` 和原业务清理装配；Modify `tests/tasks/test_callback_tasks.py`、`tests/reliable/test_retention.py`。
**Interfaces:** §2 的 protected_event_ids/protected_message_ids；框架只收本批保护集合，组件 cleanup_closed；无 ScopeStatus/多资源回调框架。

- [x] **Step 1 — 写失败保留/删除用例。** 长任务原供箱决定、canonical/分隔/本点锚点超过保留期仍被引用时保留；关闭无引用可删；WAIT_FACT 保留最小原身份，不把旧 TASK 一直留成活跃。
- [x] **Step 2 — 实现固定查询与同事务协调。** 按候选涉及原 WorkLine 的锁序读取完整活跃引用，再在同 session 删除本批公共记录；不能拿事务外旧集合或只取第一页作最终保护。框架不得在已持消息锁后回调并锁业务。
- [x] **Step 3 — 处理关闭行依赖。** 当前 FaceWork.return_members 仍引用的成员行不早删；原报告可靠交付不被关闭误删；先消除依赖/公共引用，再删有限任务摘要，不产生永久 UNKNOWN 钉住。
- [x] **Step 4 — 验证闭合历史可丢。** 清理所有允许删除的闭合记录后，新任务和新独立回流可运行；无永久墓碑/旧源 ID 兼容层。范围结束只解除旧调度，不能按保存期把未知物理队首当 EMPTY。
- [x] **Step 5 — 验证有限重放与竞争。** 保留窗内原事件不重执行；清理与新关联创建并发不出现悬空引用。超窗规则按已确认合同，不自行新增 wire 或 timestamp 拒绝门槛。

**Run:** `rtk uv run pytest tests/businesses/bin_roller/test_retention.py tests/tasks/test_callback_tasks.py tests/reliable/test_retention.py tests/businesses/bin_roller/test_scope_close.py -q`。
**Expected:** 活跃新证据不早删、已关闭历史可删、没有框架回调业务或清理竞争漏洞。

## Task 10：最终回归与切换清单

**Files:** 同步 `docs/integrations/ecs-event-debugging.md`、`outbound-results.md`、`workline-bin-return-api.md`、`manual-picking-api.md`、`business-packages.md`；原设计/SMT AUTO 接口说明；`docs/testing/smt-manual.md`、`smt-manual-kt16-kt18.md`、`bin-roller-wes-recorded-cases.md` 的实际相关验收记录。
**Interfaces:** 产出真实软件/DDL/并发证据与待授权运维清单，不表示部署授权。

- [x] **Step 1 — 精确核对消费者。** `rtk rg` 查旧表/work kind、兼容字段、scope 授权回调及私表访问残留，区分历史文档；核对 fixture/helper、catalog、worker、宿主、模型导出。
- [x] **Step 2 — 补跨层验收。** 正式宿主 TASK 终态停止发送、LINE 不接管、facts/report 继续；两个真实发送路径；KT16/KT18/录制案例不以 mock ACK 代替物理结果。
- [x] **Step 3 — 跑一次最终矩阵。** 下方命令及 PG 交错/DDL 实测；复用未被后续变更失效的专项结果，不反复全量。skip/缺失环境不当已验收。
- [x] **Step 4 — 更新交付文档。** 明确无旧数据迁移、基础层边界、TASK/LINE/事实交付、有限窗口及软件/现场限制；保留旧快照证据。
- [x] **Step 5 — 写供用户评审的切换步骤。** 确认旧 BIN 责任已结束、停止旧写入、备份/对应代码版本、执行 A→B、新四表空/公共数据保全、启动同版并接收新任务；不恢复旧队列。实际迁移/重置/部署需另行授权。

**Run:** 执行 §4 命令与已配置 PG 专项。
**Expected:** 软件检查通过、DDL/并发有真实证据、单 head；未满足外部合同/现场门槛如实列出，不实际切换。

## 4. 验收矩阵与最终命令

| 范围 | 必须有的证据 |
| --- | --- |
| EVENT | 原路由/技术幂等/原应用关联同事务，旧收据不回填，snapshot 失败不影响受理 |
| 停止发送 | 普通/snapshot、业务登记与关闭、claim/gate/stop/late finish；Sender 不查业务 |
| TASK/LINE | 原动作归属不转移、WAIT_FACT 事实消费绝不发意图、无关 LINE DRAIN 正常、事实报告不误停 |
| 横表/FIFO | 唯一前驱和候选并发、孤立 SCAN4、C07、旧结果不作用新轮、未知队首不越过 |
| 供回箱 | 4+1、获准前缀/部分失败、NO_BATCH、多架多面、晚到位置/结果 |
| schema | 空库/旧结构可弃历史、共享数据保全、旧活跃责任拒绝、DDL 原子性、空结构降级/新数据降级拒绝 |
| 清理 | 长任务/本点锚点、WAIT_FACT 最小引用、删除竞争、报告义务、无闭合历史依赖 |
| 装配/回归 | 正式宿主、catalog、worker、KT16/KT18、完整 Ruff/pytest；无其他业务语义外溢 |

验证命令（仅对工作区及显式可丢弃测试资源执行）：

```bash
rtk uv run ruff check .
rtk uv run ruff format --check .
rtk uv run pytest tests/reliable tests/integrations/ecs_callback tests/integrations/ecs_command tests/integrations/wms_request tests/integrations/wms_transport tests/businesses/bin_roller tests/businesses/rack_dispatch tests/businesses/smt_manual tests/tasks/test_business_tasks.py tests/tasks/test_smt_manual_worker.py tests/tasks/test_callback_tasks.py -q
rtk uv run pytest tests/migrations/test_roller_runtime_foundation.py tests/migrations/test_bin_roller_four_tables.py tests/migrations/test_project_schema.py -q
rtk uv run pytest
rtk uv run alembic heads
rtk git diff --check
```

使用现有显式可丢弃测试连接/fixture；未配置 skip，配置失联 fail，PG 会话交错/DDL 没实际运行不标通过。SQLite 不证明并发；CI/软件结果不证明 WMS/ECS/RCS/现场完成。

## 5. 设计覆盖与本轮审查记录

| 设计 | Task |
| --- | --- |
| §1～3 四表/归属 | 0、1、4、7 |
| §4 前向横表 | 2、5 |
| §5 回流/FIFO/WAIT_FACT | 5、6、7 |
| §6～7 面作业/排空/架位 | 4、6 |
| §8 最小 EVENT | 1、2 |
| §9 终态/发送/清理 | 3、7、9 |
| §10 事务/pending | 3、5、7 |
| §11～12 功能替代/schema 切换 | 8 |
| §13～15 验收/精简 | 0、4～10 |

本轮自审已修正：基础层 scope 授权与全仓事件能力装配过度；TASK 引用转存 LINE 后继续恢复旧动作的边界漏洞；旧数据无需迁移但仍设计兼容 ID/回填/重建的冗余；遗漏的事实报告、点位锚点、清理竞争、共享数据保全和无旧队列切换测试。

完成定义：四表/基础增量/schema 一致，旧 TASK 无新投递，LINE 事实不被误删或转成旧执行权，必要 PG/DDL/回归有真实证据。实际执行状态以下方实施收口与验收记录为准；外部启用合同不计为软件验收通过，未执行运行库切换、提交、推送或部署。


## 实施收口

基线 `caca3c0`、工作分支 `feature/bin-roller-four-tables`，用户要求不提交/推送/部署。本地代码、A/B 脚本、固定清理、宿主/fixture、文档与相关软件检查均已完成；上述复选框只表示本地交付。外部启用合同保留未勾选；没有执行运行库切换。

全仓初轮 5463 passed / 118 skipped / 1 旧断言失败；该断言修正后 worker/任务套件 13 passed。审查修复后的三项业务全套 1097 passed / 3 SQLite 并发分支 skipped，新增/受影响专项均通过。完整证据、审查四项修复与待执行切换步骤见[验收记录](../../testing/2026-11-03-bin-roller-four-table-acceptance.md)。


实现复审修订收口：已替代 WAIT_FACT 的明确来源终位以 SUPERSEDED 关闭，未知继续等待；两类 Sender 只对带范围标签的消息写新增派发起点。新增 13 类旧公共/宿主责任独立拒绝迁移用例及未标签投递后基础迁移降级用例。最终全仓串行验证 **5517 passed、118 skipped，零失败（987.79s）**，包含本轮新增 40 项回归；未增加迁移 revision 或执行旧数据搬迁，未提交、推送或部署。详见软件验收记录。
