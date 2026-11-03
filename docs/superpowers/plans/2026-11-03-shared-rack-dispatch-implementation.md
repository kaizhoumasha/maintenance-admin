# 公共货架调度 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 手工和 BIN_Roller 共用原货架调度作业和动作，安全交出后按原 CTU03/F01 离场接纳释放，保留生产、报告和历史证据。

**Architecture:** 现有 Transport 持久保存原提交接纳。一个由真实宿主装配的 rack_dispatch 组件拥有持久调度；业务保留点位需求、直接责任和业务完成资格。迁移对历史原链一次性切换。

**Tech Stack:** Python 3.13+、SQLAlchemy async、Alembic、PostgreSQL、SQLite 单测、现有可靠发送和 Celery。

**Spec:** [共享货架调度设计，含 §11 决议](../specs/2026-11-02-shared-rack-dispatch-design.md)

## Global Constraints

- 公共组件为 `app.businesses._components.rack_dispatch`，唯一调度真相为 `rack_dispatch_job` 和 `rack_dispatch_action`。
- DEPART + RACK_MOVE + (CTU03|F01)，目标为 ZONE 或不同的 RACK_POSITION，精确接纳组合为 HTTP 202 / RECEIVED 或 HTTP 200 / DUPLICATE。
- ENTER/ROTATE 的生产资格必须来自原动作已应用成功及匹配的实际位置/面向。
- WorkLine（ID顺序）→ Devices（ID顺序）→ RackJob（ID顺序）→ 宿主主体（固定类型/主键顺序）→ RackAction（ID顺序）。
- PostgreSQL 的 SERIALIZABLE 或 REPEATABLE READ 在第一条 SQL 前设置，40001 含 commit 最多 3 次新会话整笔重试。
- 原 owner/caller/cycle_id/client_request_id/transport_task_id/operation_id/message_id/请求字节/期限不变。原 after_commit、同一 scan_pending 和 5 秒 Beat 恢复。
- 不新增 Sender、Outbox、Celery、Beat、需求表、全局架锁、物理占位许可、清理器、告警或对外 API。

任务按 1→5 串行执行，各任务红灯→最小实现→绿灯→独立评审；实现、测试、审阅使用隔离工作树，本次规划不提交。实施任务先作本地任务提交并生成 BASE..HEAD 评审包，修订另作本地提交；评审通过后才进入下一任务，不推送、不部署中间版本。数据库测试只使用显式指定的可丢弃实例。未配置导致的 SKIPPED 不算并发或迁移验收通过。

## Review Focus

1. 回调 settle、lease 丢失、Message 清理或 outcome 损坏时仍可读取真实 ACK；只有原回调时等待核对（Task 1）。
2. 非成功 ROTATE 的原架留在本点位，结果可继续执行和已可靠终结的两例必须给出不同决定（Task 2）。
3. WAIT 分叉、两个盘同面准备、撤回后产生新面或 drain FIFO 瞬空时保持原身份和正确额度（Task 2/3）。
4. 手工报告晚于离场、先读旧快照再收到新许可、原配置被删或另一架占同点时，只改变原业务事实（Task 4）。
5. 原 CTU03 仅凭 SUCCEEDED 已 RELEASED，迁移仍保留历史结论及空 ACK；旧动作/请求引用不丢失（Task 5）。

## 文件与提交边界

Task 1 只改 Transport/可靠端口及第一条迁移。Task 2 新建组件、模型和门面，通过独立夹具验证；正式声明移动和 API/worker 真实装配同在 Task 3。Task 3 移除 roller 的运行时私有调度，保留业务/位置事实。Task 4 移除手工私有调度、接入独立报告待办并完成两宿主的锁序。Task 5 完成第二条迁移、数据库实测和文档。旧部署只可一次升级到全部任务完成的版本。下述类型均为内部 Python 对象；session 表示当前调用方的 AsyncSession，写操作不 commit。新文件用绝对包语义描述，路径相对仓库根。

### Task 1: 保存和独立读取原 Transport 提交接纳

**Files:** 修改 `app/models/transport.py`、`app/integrations/wms_transport/{results.py,service.py,__init__.py}`、`app/reliable/{delivery.py,sender.py}`、`app/reliable_runtime.py`、`app/main.py`、`app/business_wakeups.py`；新增 `app/integrations/wms_transport/acceptance.py`、`migrations/versions/2026-11-03_transport_submission_acceptance.py`。测试 `tests/integrations/wms_transport/{test_delivery.py,test_results.py,test_submit_notifications.py,test_concurrency.py}`、`tests/migrations/test_transport_task.py`。

**Interfaces**
- Consumes: Claim/AttemptOutcome、TransportSubmitAdapter.classify_response、results.read_rows/validate_frozen、stored_response、当前 business_committed 发布与 after_commit 机制。保持 TransportService.get_result 和 MessageStore.finish 原行为。
- Produces: `TransportService.get_submission_acceptance(session: AsyncSession, *, transport_task_id: str, client_request_id: str, caller: TransportCaller) -> SubmissionAcceptance`；不可变投影的 state 为 ACCEPTED / MISSING / INVALID / IDENTITY_MISMATCH，只有 ACCEPTED 有 basis。basis 含 owner_domain、caller、cycle_id、kind、冻结 rack/source/target/template、原 submit 身份及时间、原请求摘要与成功响应。业务须再比对原 action。
- Produces: 可选注入 `observe_delivered: Callable[[Claim, AttemptOutcome], Awaitable[None]] | None`，通过 reliable_lifespan → Sender → send_one 透传，Transport 观察器绑定目标/协议、session_factory 和原 owner 通知。记录 transport.task.submit@v1 的完整真实成功响应。其他 target/协议、静态非 Transport 和动态 ECS 维持既有 finish。新 revision `racksubmitack` → down_revision `fiveracklimit`。

- [x] 增加 `test_late_ack_survives_settle_and_cleanup`，真实 Sender 测试 client 在返回前暂停，原回调提交 settle 后放行 202/RECEIVED 和 200/DUPLICATE；断言 `calls == 1`、原 lease 不复活、删除对应测试 Message 后读口仍 ACCEPTED。测试原消息仍在的历史有效响应，填坏 outcome 或 pending 不影响 ACK；拒绝/错 echo/重复键/错 owner/caller/篡改请求/仅 DONE 回调不产生可用接纳。
- [x] 红灯：`rtk uv run pytest -q tests/integrations/wms_transport/test_delivery.py tests/integrations/wms_transport/test_results.py -k 'ack or acceptance'`。确认新行为失败。
- [x] 添加 SQL NULL 可空 JSON(none_as_null=True)，历史不回填、不改 input_digest。证据自包含原消息 id/target/business_key、submit 身份、原请求 SHA-256 与可独立复验的原请求/响应必要内容、attempt_no/lease 身份、HTTP、回显 task_id、原 ACK 时间及保存时间。重用严格解析/冻结核对；当 task 内证据非空但无效时返回 INVALID，不能以另一个来源掩盖。NULL 时才核对唯一历史 submit；没有响应为 MISSING，冲突为 INVALID。读口不执行写入或自动修复。
- [x] HTTP 严格成功后、调用 finish 前的独立新事务仅锁 Task。验证原 claim、原目标、请求原字节、frozen、真实响应和回显；首次保存并 after_commit 通知原 owner。重复成功（包括同一原请求的两个成功码）保留首次证据，矛盾不覆盖。生产 reliable_lifespan 移除原 on_delivered=transport_submit_delivered 接纳通知装配，改由首次证据事务提交后唤醒一次。保留通用 on_result=wms_result_committed、非 Transport 行为及原回调新事实通知。若证据存储失败但 finish 保存了历史可验证响应，同一 Beat 可恢复；无需第二条成功通知。调整原 Transport 通知测试经真实 reliable_lifespan 验证普通/迟到 ACK 各仅一次，重复/rollback 零次，WMS decisions/facts 原通知照常。存储 40001/40P01 最多三次独立新事务，commit 未知先重读已保存证据；恢复失败记录安全诊断，仍让原 finish 以原 outcome 和原 lease 工作。不能把存储异常改成发送失败或再次 HTTP。
- [x] 增加并发/故障测试：持 Task 的回调与 finish 交错无锁环；证据失败且 settle 已完成则 MISSING、finish 存入有效响应则可读；证据已提交而 finish 崩溃仍可读本人原依据。首写通知等待 commit，重复/rollback 无证据通知。降级有非空证据在 drop 前拒绝。
- [x] 绿灯：`rtk uv run pytest -q tests/integrations/wms_transport tests/reliable tests/migrations/test_transport_task.py`，并在独立 PostgreSQL 上确认并发用例实际运行（用既有已配置测试环境，不读取/打印凭据），`rtk uv run alembic heads` 仅 racksubmitack。

### Task 2: 共享作业、动作及点位交接状态机

**Files:** 新建 `app/businesses/_components/rack_dispatch/{__init__.py,models.py,contracts.py,service.py,composition.py}`。测试新增 `tests/businesses/rack_dispatch/{__init__.py,test_dispatch.py,test_constraints.py}`，复用现有 business/Transport/WMS 测试资源。此步尚不把新组件加入正式宿主。

**Interfaces**
- Consumes: Task 1 读口；WmsRequests.send/get_result、TransportService.move_rack/rotate_rack/get_result/list_unclosed_predecessors、现有 DepartureRequest/DECISION（Task 3 搬移其唯一定义）。完整读普通 Transport 结果只经公开端口。
- Produces: frozen dataclasses `PointRef(owner_domain:str,work_line_id:int,job_id:str,rack_id:str,point:str)`、`FactRef(kind:str,key:tuple[str,...],version:tuple[str,...])`、`Coverage(collection:str,scope:tuple[str,...],anchor:FactRef,rows:tuple[FactRef,...])`、`PointDemand(point:PointRef,state:Literal["CONTINUE","NONE","UNKNOWN"],coverage:tuple[Coverage,...])`、`DirectHandoff(point:PointRef,state:Literal["CLEAR","BLOCKED","UNKNOWN"],coverage:tuple[Coverage,...])`。key 是原完整主键，version 为修订或有序原事实字段、操作号和状态值，禁止以当前时间/一枚 bool 充当版本。
- Produces: `JobOrigin(source_kind:str,source_ref:str,work_line_id:int,rack_id:str,task_id:str|None,workline_code:str|None,point:str|None,initial_face:str|None,caller:TransportCaller|None,cycle_id:str,resources:dict|None)`；`RackDispatch(owner_domain:str,capabilities:Capabilities)`。异步方法 `register(session, origin:JobOrigin)->str`（job_id；新作业使用 origin.cycle_id 作 id 并另存不可变 original_cycle_id；重复原出处返回原 id/原 cycle，不以重试中新生的候选 UUID 重建或覆盖），`prepare(session, *, job_id:str, kind:Literal["ENTER","ROTATE"],source_ref:str,face:str|None)->str|None`（action_id，冲突时 None），`advance(session, *, job_id:str, demand:PointDemand, handoff:DirectHandoff, now_ms:int)->RackProgress`。作业未取得可表达的 point/资源时不构造虚假 PointRef；对预候选提供 `withdraw_unused(session, *, job_id:str, source:Coverage)->bool`，须核对原宿主、原候选出处、覆盖范围及无 ENTER/直接责任。
- Produces: `progress(session, *, job_id:str)->RackProgress|None`、`jobs(session, *, work_line_id:int, source_kinds:tuple[str,...], task_id:str|None=None, pending:bool|None=None)->tuple[RackProgress,...]`（task_id=None 表示不按任务过滤）、`pending_ids(session, *,source_kinds:tuple[str,...],after_id:str|None,upper_id:str|None,limit:int)->tuple[str,...]`、`pending_upper_id(session, *,source_kinds:tuple[str,...])->str|None`、`lock_jobs(session, *,job_ids:tuple[str,...])->None`。仅绑定 owner；宿主轮换扫描所属类别并按原工作线推进。若需跨线保留历史位置冲突，提供只读 `has_position_conflict(session, *,point:str)->bool`，只暴露原冲突是否保护点位，不出租其他 owner 的 job。原点位 release/清除只能匹配本 job 的动作。
- Produces: `RackProgress` 含 id、origin、state、pending、release_requested、facts_revision、closure_reason/basis 和 ordered actions；只读动作含 id、job_id/kind/source_ref、phase、原前驱、caller、冻结 target/face、client_request_id、transport_task_id、submit_operation_id、原 outcome/position/各版本、acceptance_basis、原决定/到期。独立公开 `final_result(session, *,job_id:str,action_id:str)->TransportTaskResult|None` 用原 action 引用读备查；读口不获取新许可。

- [x] 新增 `test_safety_departure_after_non_success` 对 ENTER/ROTATE × FAILED/CANCELLED × 已终结/可继续/更正/位置冲突参数化；断言只有已终结、同原工作位、有 NONE+CLEAR、无未解决前序的一行 `count(DEPARTURE_DECIDE)==1`，原失败结果仍在，所有例子均不得增加 ENTER/ROTATE。加入 `test_closed_departure_ignores_late_result`（两个模板×两种目的地×两种成功 ACK、无回调/失败/仍在原地），以及同位假 DEPART、错身份/缺 ACK、其他模板/动作不准提前完成。
- [x] 红灯：`rtk uv run pytest -q tests/businesses/rack_dispatch`。先让必要接口及夹具可加载，确认失败来自缺少业务状态转换/副作用，而不是 import、fixture 或数据库连接错误。
- [x] 依 §11.1 实现两表、状态、CHECK、FK、部分唯一索引和出处重放；SQLite 与 PostgreSQL 均实测双根（NULL）、双 WAIT 后继、重复 ENTER/DEPART、错 job 前驱/归属、重复外部身份不能插入。明确 WAIT 前驱原决定、READY→DEPART、合法 kind/rule/phase 和终态/闭合依据组合。记录 release_requested/依据、最后实际位置和 consumed revisions；历史例外只接受迁移建行，所有运行时入口均禁止。
- [x] 参数化来源模板：手工 ENTER/ROTATE/DEPART 使用 F01/CK04/F01，roller 分别使用 CTU01/CTU02/CTU03，保持 move/rotate 原参数映射，原 caller/资源/身份不重新分配。新相容生产准备锁 job 后关联原 action，允许回同面新轮次。不得拿原 ACK 做 ENTER/ROTATE 生产许可。宿主保有点位/额度/生产资格检查，公共服务校验原动作范围、原点位、相容性和结果/前驱。
- [x] 实施 §3.3、§4、§11.1–2 的需求/安全状态表。先收原结果，未知时无许可。WITHDRAWN 仅未取得物理责任且无 ENTER；NOT_ARRIVED 仅可证明已终结的外部非冲突 ENTER。对进场先登记后撤回保留释放意图。冻结根的同次 NONE/CLEAR 与所有原依据，后到新授权只能另起合法后继。READY/WAIT 只接受原消息 `200 DECIDED` 及严格 DECISION schema（1..60000ms），消费一次固定 due_at_ms；manual 可因原合同的实质 basis 变化提前唤醒，roller 仅到期。READY 去向冻结，跨轮/重复/乱序不二次询问。有效 DEPART ACK 原子完成共享调度并可同事务推进后继，不扫终态作业等待回调。
- [x] 绿灯：`rtk uv run pytest -q tests/businesses/rack_dispatch tests/integrations/wms_transport/test_rack_lifecycle_contract.py`。用并发事务验证两个相容请求同一 action、冲突两个请求只有原准备、到期 WAIT 一条后继，包含事务回滚和重复扫描。

### Task 3: 迁移 BIN_Roller，原子移动 departure 注册与宿主装配

**Files:** 移动 `app/businesses/_components/bin_roller/request/rack_departure_decide_v1.py` 至 `app/businesses/_components/rack_dispatch/request/rack_departure_decide_v1.py`，新包加 `__init__.py`。修改两组件 `composition.py`、公共 `service.py`、`app/businesses/_components/bin_roller/{service.py,repository.py,rack_flow.py,return_flow.py,feed_flow.py,scan_flow.py,models.py}`、`app/businesses/smt_manual/{composition.py,service.py,manual_rack_flow.py,repository.py}` 及受影响的 `tests/businesses/{conftest.py,bin_roller/conftest.py,smt_manual/conftest.py}`、catalog/装配测试。只更新确实改变的 fixture 和导入。

**Interfaces**
- Consumes: Task 2 的 RackDispatch 实例、register/prepare/advance/withdraw_unused、jobs/progress、pending 两读口；JobOrigin.source_kind=ROLLER_ORDINARY / ROLLER_DRAIN。
- Produces: `BinRoller(context:CommandOwner,capabilities:Capabilities,production:Production,*,rack_dispatch:RackDispatch)`；在 `rack_flow.py` 定义 `async def evaluate_roller_point(roller:BinRoller,session:AsyncSession,job:RackProgress)->tuple[PointDemand,DirectHandoff]`。生产接口 SupplyProgress、SourceFaceRef、RollerWorkRef("RACK_CYCLE", record_id=原 cycle id) 及已交付其余公共行为保留；其内部由公共 job 恢复。给宿主注入唯一真实实例并用于手工适配，API/worker 使用同一 build_service。
- Produces: 宿主调用的 `RackDispatch.note_facts_changed(session, *, job_ids:tuple[str,...])->None` 仅在原行先锁且该新 receiver 事实确实改变覆盖时递增 facts_revision；重复事实不写。尚无 job 时见 §11.2 的原锚点规则。RACK_CYCLE/FEED/DRAIN 等 dispatch 之前使用共用锁前定位，不保留原任务先锁再查 job 的入口。

- [ ] 以 `tests/businesses/bin_roller/test_rack_lifecycle.py`、`test_drain.py`、`test_request_capacity.py` 增加 ARRIVED 后跨版本同架还有一面、只撤回一面、有 FeedSegment/ReturnBatch 在途、FIFO 暂空及旧 drain 未进场/已派 ENTER 两组。断言证实无本点位需求且安全时 `departures == 1`，接纳前后额度只变一次；晚失败/缺回调不反占、不改 FaceWork 完成和新位置投影，进入和换面从未因 ACK 就 READY。
- [ ] 红灯：`rtk uv run pytest -q tests/businesses/bin_roller/test_rack_lifecycle.py tests/businesses/bin_roller/test_drain.py tests/businesses/bin_roller/test_request_capacity.py`。新断言复现原行为差异。
- [ ] 以普通文件搬移（保持工作树未暂存）迁移原 DepartureRequest/DECISION 整个模块，不改任何 wire 规则或字节。删 BIN_ROLLER.entry_modules 中该项，加到 RACK_DISPATCH.entry_modules；smt_manual 同层 COMPONENTS 加公共组件/真实映射。同步修改 manual_rack_flow 和 roller 旧导入及全部真实 fixture/catalog 消费者。契约消费者由 catalog 编译，不创建第二份 decorator、兼容声明或私有 Sender。组件不自注册为业务；已存独占旧来源仍按原 business_key / 旧来源原归属恢复，检查旧 source module 路由的实际依赖。
- [ ] 原供箱/面选择、可承接新需求、机会/FIFO、回箱/manifest、drain 顺序和请求上限留在 roller；依 §11.2 完整调用 evaluate_roller_point。五层架额度按原 owner/line、存在 CTU01 的未终结共享 job（普通和 drain）核对；生成 ENTER 和额度检查同一事务。结束投影仅清当次 job 的原动作来源；位置历史及扫码箱事实仍在 roller。服务/箱/报告的闭合按自己的原结果逐一消费，不再在 job 释放时无条件批量标记完成。历史 WITHDRAWN 不因同 rack 同 task 有新增需求而复活。
- [ ] 删除 RollerRepository、Service、return_flow、rack_flow 的私有周期/动作/DEPARTURE 查询、发送和状态写入；FaceWork.cycle_id 保留公共 id。原 test_facts/test_identity/test_recovery 等通过实际生产入口和公开读口替换旧结构/夹具断言，保留各项验收。单独 DRAIN 决定/Input/unused_racks 保留，模型将原 decision 限为 DRAIN，去除仅 departure 适用字段，Task 5 再处理数据库。把 roller 和 TASK 的既有推进改到 §11.2 顺序；原手工在 Task 4 前仅使用新注册位置，隔离工作树不部署。
- [ ] PostgreSQL 交错旧扫描与最后一面释放、入口额度仅一份、晚 position 来自旧 action；断言原接纳释放后新架可登记、旧架结果不可覆盖新架。运行 `rtk uv run pytest -q tests/businesses/bin_roller tests/businesses/test_components.py tests/businesses/test_component_wms_wakeups.py tests/businesses/test_contract_assembly.py tests/businesses/smt_manual/test_assembly.py`；测试必须验证声明只出现一次、operation/business_key/旧字节不变、重复原 id 返回原事实。

### Task 4: 迁移手工生产架和报告，落实隔离与锁序

**Files:** `app/businesses/smt_manual/{manual_rack_flow.py,manual_bin_flow.py,task_flow.py,service.py,roller_ports.py,repository.py,models.py}`；与 Task 3 的实际共享事务入口对齐。测试 `tests/businesses/smt_manual/{test_direct_pick.py,test_transfer_rack.py,test_rack_cancellation.py,test_rack_reports.py,test_task_completion.py,test_confirmation_races.py,test_transactions.py,test_pending.py,test_owner_wakeups.py,conftest.py}`。

**Interfaces**
- Consumes: 同一 RackDispatch；source_kind 为 MANUAL_RETURN 或 MANUAL_TRANSFER；真实宿主用 `async def evaluate_manual_point(self,session:AsyncSession,job:RackProgress)->tuple[PointDemand,DirectHandoff]`（ManualRackFlow）。用原任务调度依赖、许可和生产事实填充 §11.2 的覆盖清单，使用公共读口而非业务自行读取公共表。
- Produces: 保留 `SmtManualService.advance(kind:str,record_id:str)->str` 和 `scan_pending(*,limit:int=100)->int`，MANUAL_RACK 的 record_id=job_id；新增 RACK_REPORT 的 record_id=原 ManualRequest.operation_id，`ManualRackFlow.advance_report(session:AsyncSession,*,operation_id:str)->str`。消费返回 CONSUMED，未知/冲突保留原请求，不存在为 NOT_FOUND。内部原报告唯一键和原工位到达时间保持。
- Produces: `TaskRepository.original` 对 rack 相关入口拆为无锁定位、lock_line、rack_dispatch.lock_jobs、task(lock=True)，既有只处理独立任务/报告的入口仍可使用合适的子集；调用者先锁后传已锁定上下文，子服务不能回头重新取得前级锁。实现不是靠切换 isolation 字符串本身证明安全。

- [ ] 增加 `test_idle_transfer_departs_before_parent_and_report` 和 RETURN 等价案例；原整单 EXECUTING、其他点位有任务、报告 REQUESTED，本原架可交出时唯一 DEPART；同面/其他面仍有原许可则继续服务。报告后到时 `report.state == "CONSUMED"`、原 job 不复活、未收妥时确认/清理仍被阻止。测试初次计划等待、跨版本、精确撤回、未决许可、其他任务和同码后继、不可关联输入。
- [ ] 红灯：`rtk uv run pytest -q tests/businesses/smt_manual/test_rack_reports.py tests/businesses/smt_manual/test_transfer_rack.py tests/businesses/smt_manual/test_rack_cancellation.py`。确认旧父任务/报告门槛或旧报告扫描遗漏。
- [ ] reconcile_task 以原 task/member/目标确定出处，相容时固定原 job/action；首次登记根后的后来需求不得搭入旧过程。对同 task 同架后续合法批次允许新的出处（TRANSFER 需不同原目标授权，不能用重复 task 唤醒冒充）。保持配置检查与原历史义务恢复分立。以前由私表核对的 _rack_completion_ready、has_report_obligations 及 action 消费改经公共进度/原生产报告；用实际到位及面向授予生产。按原业务需要创建到位报告，保持原 action 身份和时间，技术重放不重复生成。删除私表模型、TABLES 项及 RACK_DEPARTURE 的私有保存/读取，保留成员和原 RACK_ARRIVAL_REPORT 关联。
- [ ] 将 RACK_REPORT 纳入同宿主公平轮换、稳定 upper_id 与 cursor，以确切 operation + operation_id、原 task/line、job/action/message 校验身份；仅原有效 RECORDED/DUPLICATE 使请求收妥。原任务终态、方向切换、工作线已删除/禁用、job 已经 COMPLETED 都可继续处理。对于已经有真实到位而尚缺应有报告的情况保留原业务义务，不能把缺行解释成完成。禁止通过报告待办另行搬运。
- [ ] 实施并记录唯一入口矩阵：SERIALIZABLE = TASK、MANUAL_RACK、MANUAL_BIN、MANUAL_COMPLETION、SCAN、FEED、RETURN_BATCH、RACK_CYCLE、DRAIN；REPEATABLE READ = SCAN2_RELEASE、RACK_REPORT；scan_pending 候选只读 RR，receiver 保持原事务。按 §11.2 的完整锁序合并所有嵌套 helper 的锁前定位；锁后重读资格，最多 3 次新事务处理含 commit 的 40001，耗尽由既有 pending+Beat 恢复，提交未知查原身份。receiver 通过 Task.received_work 或原 job.facts_revision/生产原行改变检测覆盖；检查接收方是否在已有 resolver/设备锁下调用，在取得低级锁后不得再取高级锁。
- [ ] PostgreSQL 双连接 barrier 测试：A 评估 NONE，B 收到新 plan/撤回/完成/箱许可，反向顺序再跑；允许 B 先登记原事实使 A 重新评估，或 A 先冻结根使 B 只能用后继，不能同时给旧过程发许可和离场。包含 commit 抛 40001、三次耗尽、外层 rollback/通知丢失与缺线历史报告。绿灯：`rtk uv run pytest -q tests/businesses/smt_manual tests/businesses/bin_roller/test_concurrent_progression.py`，记录实际 PostgreSQL 测试数量，确认其他业务隔离未变。

### Task 5: 确定性迁移、原子切换及全分支验收

**Files:** 新增 `migrations/versions/2026-11-03_shared_rack_dispatch.py`、`tests/migrations/test_shared_rack_dispatch.py`；更新 `tests/migrations/test_project_schema.py` 及受模型清单影响的 `tests/businesses/test_model_loading.py`。更新 `AGENTS.md`、`docs/integrations/{business-packages.md,outbound-results.md,wms-transport.md}`、`docs/testing/smt-manual.md`、`docs/superpowers/specs/{2026-11-02-shared-rack-dispatch-design.md,2026-11-02-smt-auto-data-model-design.md,2026-11-02-coarse-sorting-data-model-design.md}`、`docs/superpowers/plans/{2026-10-31-smt-auto-implementation.md,2026-10-29-coarse-sorting-scan-acceptance.md}` 的共享服务用法和验证边界。同步核心设计的当前软件实施/验证状态，保留原日期的文档/现场证据边界；只在与本次合同相冲突处更新历史文档。

**Interfaces**
- Consumes: `racksubmitack` revision、§11.1–3、Task 2 模型及两套真实消费者。revision=`sharedrackdispatch`，down_revision=`racksubmitack`，唯一 head。公共动作 ordinal/current_action_id/position_action_id 依原搬运动作与决定前驱链、原状态/显式引用重建，不按跨表 UUID 最大值或迁移写入时间推测。只迁移数据库，不调用应用 Sender、API、scan_pending 或从最新配置猜配旧记录。
- Produces: `upgrade()->None` 在一个事务中预检、建表、填数、核对、替换原引用/约束和删除旧调度状态；`downgrade()->None` 可逆空库还原，含不可还原业务数据/接纳证据时在破坏操作前拒绝。备份恢复为有损场景的回退路径。

- [ ] 由旧 revision 建含数据样本：原手工 ACTIVE/ENTRY_CANCELLED/DEPARTED、roller ACTIVE/WITHDRAWN/RELEASED、仅 SUCCEEDED 的已释放、有 ACK 的未决、已过期/未到期 WAIT 和 READY、同码异作业、无 task drain、存量报告、已经清理 Message 的旧动作。断言升级后新外发数为 0、原 Transport/可靠消息字节和全部原 ID/期限/已消费 revision 相等。另造跨表 id 碰撞、孤儿、WAIT 环/分叉和 owner 错配样本，迁移须原子失败、源表与记录完整。
- [ ] 红灯：`rtk uv run pytest -q tests/migrations/test_shared_rack_dispatch.py` 在独立 PostgreSQL 失败于缺少新 revision/映射。
- [ ] 按 §11.3 保持原 work/cycle/action id。手工来源用绑定首个 DIRECT 原成员（按 plan_revision/member_order/id），TRANSFER 用原目标授权；不能可靠取得时用显式历史出处 `legacy/manual/<work.id>` 保持可定位、禁止将后来相同架需求自动并入。历史首个成员不一定等于后来剩余成员，不从当前状态倒推原授权。roller 来源为原 source_ref。原从未登记 ENTER 的未用候选保留空资源，已发动作只能取历史原 caller/配置/Transport，绝不能从当前 WorkLine 补填。逐行核对原 action/client_request/transport/submit/cycle/target，允许原有“已登记未绑定 transport”保持原 client_request_id 恢复，不伪造 id。
- [ ] 手工动作 ENTER/ROTATE/DEPART 原种类不变，roller CTU01→ENTER、CTU02→ROTATE、CTU03→DEPART；保存原 source_ref、原状态/版本/位置/观察和来源。旧 RACK_DEPARTURE 和 DEPARTURE 决定移入同 job 的 DEPARTURE_DECIDE，分别按 previous_operation_id 和 predecessor source_ref 连 WAIT，保持快照、response、message_id、已消费状态、due_at_ms（roller 原 due_at 已是毫秒），READY 引用原 DEPART。保留可能仅有业务状态而无原 ACK 的闭合依据原样；对 DEPARTED/RELEASED 选择 LEGACY_RECORDED_CLOSURE / LEGACY_CLOSED，不填新 ACK，closed_at 只取实际存在值；未闭合 DEPART 统一新规则，真正消费时才写本次时间。ENTRY_CANCELLED 若可证明外部无冲突/无继续责任才 NOT_ARRIVED，否则保留原取消和 pending 待核；已有 WITHDRAWN 保持历史并校验无活跃持有者，不能重开。
- [ ] 对每个成员、原报告、FaceWork 的 job/action，position_fact 的 source_kind=RACK、每个 drain/candidate/unused_racks、供回箱 feed/service/opportunity/batch 逐项比对原归属、原主键和对应数量，保留其余来源类型不动。任务/业务清理无级联；确认 public 门面能读原历史行。只在所有断言通过后移除四张旧调度表和手工/roller 的 departure 行/专有列及旧约束，保留仍由报告或 drain 使用的列。检查原 constraint/index 与 ORM 一致。历史文件和不可变旧迁移仍可包含旧表名；所有运行时查询及测试读取改走新源。
- [ ] 在可丢弃实例演练空库升级、带数据升级、空库降级、含新证据的拒绝降级（首个 drop 前拒绝），输出核对计数。写出停旧 API/receiver、worker、Beat、Sender，等事务/在途原 HTTP 可核对、备份、单链升级、同版全体进程确认、再开入口的顺序和失败回退；不执行线上步骤。AUTO/coarse 只记录以后宿主需实现的来源/交接和真实进场资格，保留未实现的生产/更正/归档状态。
- [ ] 绿灯：`rtk uv run pytest -q tests/migrations/test_shared_rack_dispatch.py tests/migrations/test_project_schema.py`，`rtk uv run alembic heads` 仅 sharedrackdispatch。随后 `rtk uv run ruff check .`、`rtk uv run ruff format --check .`、`rtk uv run pytest -q`、`rtk git diff --check`；核实 PostgreSQL 迁移/竞态与原协议回归确实执行。清点 `rtk rg -n 'smt_manual_rack(_action)?|bin_roller_rack_(cycle|action)|rack_departure_decide_v1' app` 的每个命中，预期仅新 declaration/真实引用，不再读写私有表。测试报告区分软件验证、未进行的供应商接纳/现场执行、真实生产数据迁移。
