# SMT 手工分拣、入库业务子包实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development only after separate implementation authorization. Steps use checkbox (`- [ ]`) syntax for tracking。本计划M1～M6仍未实施，依赖的共享ST1/ST2已随PR #44合入develop；本轮仅同步交付状态，不写产品代码、迁移、配置或测试，不执行产品测试/运行库操作，不提交、不推送、不部署或真实发送。

**Goal:** 交付一个 smt_manual 正式业务，支持手工入库/出库共用任务生命周期、箱级人工准入/完工、出库直接取料及可选转运架，并复用已合并 BIN_Roller 完成公共箱架流程。

**Architecture:** 任务/计划/人工作业/生产架状态归 `app/businesses/smt_manual/`，只有一份服务、工厂、owner 和 scan_pending。宿主组合同源 sorting_task 和一份 BinRoller；前者只合同/分派，后者只四点/公共箱架履约。任务完成不等待 SCAN2 放行 Result，原释放/公共回流及独立责任继续；不复制已交付组件内部流程。

**Tech Stack:** Python 3.13+，既有 FastAPI/Pydantic/SQLAlchemy/Alembic/Celery/pytest，独立 PostgreSQL/Redis；不新增依赖。

**Spec:** [SMT 手工设计](../specs/2026-10-31-smt-manual-business-design.md)、[人工线 API](../../integrations/manual-picking-api.md)、[共享 sorting_task 计划](2026-10-31-sorting-task-implementation.md)、[统一自动API任务接口](../../integrations/smt-auto-api.md#tasks)、[BIN_Roller 设计](../specs/2026-10-30-bin-roller-capability-design.md)及[B1～B7 计划](2026-10-30-bin-roller-implementation.md)。

## Global Constraints

- code/owner_domain 固定 smt_manual，WMS business_key 为 business:smt_manual，组件使用 CommandOwner("smt_manual")；方向和组件不是独立运行主体。
- issued 固定 task_type=MANUAL、business_type=inbound|outbound 必填不可变；同线两个方向共用队列和一个 PREPARING/EXECUTING 执行位。按非唯一 dispatch_sequence、原 issued.timestamp、task_id 排序，工作线独立。
- sorting.task.* 六类operation只在共享组件注册；手工箱级接口固定 manual_bin.work_admission_decide@v1/manual_bin.work_completed@v1，出库直接取料接口保留 outbound 名称。历史冻结身份/正文/owner/路由/期限不改，无兼容双入口。
- 入库计划仅供箱架面；出库可供箱/直接取料/首版可选转运架。入库不接受target_rack/直接取料，两个方向不接自动来源/逐盘参数。字段/模式方向校验消费共享合同，实际版本、成员和任务状态归宿主，不建共享任务执行器。
- 四点及 MOVE_FORWARD/MOVE_RIGHT/MOVE_LEFT 由已合并 bin_roller.COMPONENT 单份声明；宿主 COMMANDS=()，不建 ECS 扫码入口/方向命令/机械臂/PDA/库存/箱架绑定账。
- 接收事实、业务状态及合法意图使用 receiver 原事务；独立结果消费用公开读口，进度/消费身份/下一 Outbox 原子。组件方法只用调用方 session，不提交或发 HTTP。独立 advance 沿既有锁序 WorkLine → Device按ID升序 → 原业务动作；只读定位可先查，持锁后重验原归属和状态，不新增业务锁或占位。
- 原plan重验消费[共享1.1收据保护](2026-10-31-sorting-task-implementation.md#sorting-task-recheck)：receiver先锁定/刷新原收据，再按WorkLine→Devices按ID→原业务动作写入，锁持有到外层结束；宿主advance不反向锁收据。公共并发机制归ST1，M1验证真实成员/版本/意图原子性。首次正式接入前四分支齐备，后到宿主遵守[1.4冻结兼容](2026-10-31-sorting-task-implementation.md#sorting-task-v1-freeze)，不收紧已发布@v1或重解析旧输入。
- 沿 BIN_Roller 专项范围在首 SQL 前设置相关单条 advance 的 SERIALIZABLE，40001 最多3次新会话整事务重试含 commit；已有无关纯生产 REPEATABLE READ，receiver/短扫描/其他业务不整体升级。不新增隔离选择器、手工锁、预占、租约、调度表或第二 Sender。
- WAIT 固定原到期点，retry_after_ms 为严格整数1～60000；到期一次合法新求值保留原 task/arrival/bin/scanned_at。未知/503只恢复原请求，不用 WmsRequests.send 的不存在 not_before 参数或另建延迟队列。
- 已完成作业可靠保存原释放决定/命令即可参与人工整单确认，不等 SCAN2 Result/FIFO清空/全部回架/全部架离场。新到箱确认暂缓、供箱撤销进度和合并后的C07沿设计，不新增全线门槛。
- 不自动补绑早到完工，不迁移 wes_backend 数据或复制旧锁/容量窗口；不新增手工7天归档政策。原任务终态/当前任务切换不停止旧 pending 或改挂原责任。

## Review Focus

- 领取/队列更新并发、两方向同线同优先级：原 prepare 只登记一次且冻结及时——M1。
- 连续同箱重报、同箱真正新经过、旧完工迟到及早到完工：按原 arrival 唯一应用，不重复释放、不自动补绑——M2。
- 撤销供箱返回 false/false，或已开始但尚无feed：前者须原撤销证据，后者不能被误判无义务——M1/M4。
- 原尾箱释放Result晚到与确认期间另有新箱：前者不阻塞完成，后者暂缓首次准入，原作业继续——M4。
- 当前任务结束/方向切换、CTU03仅接纳、通知丢失与提交未知：原owner/身份/待办继续，旧结果不清新轮/新架——M5/M6。

---

## 1. 已有基线、交付边界与执行顺序

| 内容 | 当前基线 | 本计划责任 |
| --- | --- | --- |
| 组件组合/唯一输入/原业务WMS与Transport通知 | 框架已合并 | 复用，不新建运行时或通知器 |
| BIN_Roller四点、供箱/回流/FIFO/drain/统一架流、原结果消费 | PR #42/e265ad7 已合并，本地实现和隔离验证已交付 | 只消费公开接口和原声明；缺陷另以具体证据处理，不重做B1～B5 |
| Transport提交接纳与应用结果通知、异常明确架位置 | 已有公开读口和原owner通知 | M5/M6 验真实宿主链路，ACK/接纳不冒充物理完成 |
| sorting_task | [PR #44](https://github.com/kaizhoumasha/maintenance-admin/pull/44)已合入develop（380aeba），共享软件验收通过，尚无正式宿主 | [最新交付及4654/52验收](2026-10-31-sorting-task-implementation.md#sorting-task-merged)；专项24 passed；手工M1及正式装配仍待实施 |
| smt_manual任务/准入/完工/出库自有架/完成 | 未实现 | M1→M2→M3→M4 |
| BIN_Roller组件状态表迁移、正式生产消费者/catalog、整体验收 | B7未完成 | M5与B7对接首个完整生产宿主，M6分层验收；不以测试消费者代替 |

固定顺序：**ST1（完成）→ ST2（完成）→ M1 → M2 → M3 → M4 → M5 → M6**。共享合同/类型及原归属接缝已单份合入；手工M1和自动S1直接复用同一源码及验收证据，各自验证真实宿主状态与原子性，不重复实施ST1/ST2。SMT不等待自动两方向逐盘业务交付。

共享阶段按[唯一交付归属](2026-10-31-sorting-task-implementation.md#sorting-task-ownership)验收：ST1仅验证隔离operation及旧默认行为，ST2创建四个真实callback后验证errors、仅plan的recheck和可选guard登记，再回归ST1；M1不提前承担这些正式登记断言。

M1先交付真实任务读口及生产适配的任务部分，M2补accept_scan2；测试fixture在各阶段将真实flow/端口与已合并BinRoller装配，按已交付模块使用独立测试catalog和收发能力，不等待M5正式工厂，也不以固定PERMIT或假任务替代真实状态。M1～M4由这些装配验证，不向默认正式catalog注册半成品。M5 需要完整手工业务和组件状态迁移一同就绪，才增加真实生产消费者。M6之前不能宣称完整生产接入/真实worker/现场已验收。

## 2. 文件责任与本地接口

| 待创建/修改文件 | 责任 |
| --- | --- |
| app/businesses/smt_manual/models.py、repository.py | 原任务/输入/成员/摘要，人工arrival作业、原准入/完工通知/释放引用，直接取料与转运架过程、消费依据和持久等待；不复制组件或框架结果表 |
| 同包 task_flow.py | 队列/prepare/计划/取消、完成条件及原完成响应、任务终态；无组件或方向独立任务 |
| 同包 contracts.py | 人工箱级与出库专属严格合同；任务模型使用共享source，资源规则不重新注册 |
| 同包 manual_bin_flow.py | 两方向同一准入/WAIT/箱级完成/原释放逻辑 |
| 同包 manual_rack_flow.py | 出库RETURN/TRANSFER原Transport、到位报告、按版本完成与离场 |
| 同包 roller_ports.py | M1交付真实read_current_task/read_successor，M2补accept_scan2；M5只完成正式注入，不拖到末期才提供前序依赖 |
| 同包 definition.py、composition.py、service.py及__init__.py | 有效资源、两组件精确映射、无状态工厂、唯一扫描/推进及正式装配 |
| 同包 request/manual_bin_work_admission_decide_v1.py | 两方向人工准入的唯一出站模型，decisions |
| 同包 callback/manual_bin_work_completed_v1.py | 箱级完工可靠受理薄入口 |
| 同包 callback/outbound_manual_rack_direct_pick_completed_v1.py、outbound_return_rack_task_completed_v1.py | 出库架面完成、整架截止版本完成薄入口 |
| 同包 request/outbound_return_rack_arrival_report_v1.py | 出库原到位报告，facts |
| app/businesses/catalog.py | 完整真实smt_manual的显式消费项；不加入sorting_task/bin_roller独立业务 |
| migrations/versions/2026-10-31_bin_roller_state.py、2026-10-31_smt_manual_state.py | M5/B7一次交付组件和宿主必要表，实施日期/文件名按当时基线调整，版本由Alembic生成；不手写固定revision或复制另一宿主已交付的组件迁移 |
| app/core/config.py、.env.example | M4增加可选SMT_MANUAL_FIRST_PLAN_WAIT_MS并显式注入宿主；默认None保持等待，只修改配置示例、不覆盖现有.env |
| tests/businesses/smt_manual/conftest.py、test_*.py | 本计划各任务公开行为及两线/两方向接缝；独立MetaData与finally清理，测试文件不互相导入 |
| tests/migrations/test_bin_roller_state.py、test_smt_manual_state.py | 正式表加载、迁移升级/约束/既有数据保留和单head |
| tests/tasks/test_smt_manual_worker.py、docs/testing/smt-manual.md | 非eager真实worker/同一Beat任务验收与分层证据；不新增产品Celery任务 |

以下固定公共服务接口；生产flow的私有拆分/表名由实施者按现有风格确定，不建通用work表。共享 TaskOwnerRef/任务受理与recheck方法精确沿[ST2接口](2026-10-31-sorting-task-implementation.md#sorting-task-st2)。

| 接口 | 产出阶段与语义 |
| --- | --- |
| SmtManualService(capabilities, *, first_plan_wait_ms:int\|None=None) | M5正式无状态装配，build_service读取并注入M4配置；M1/M2先提供真实flow/生产端口供隔离装配，不导入全局session factory |
| async read_task_owner(session, *, task_id:str) -> TaskOwnerRef\|None | M1；只读本业务原任务/终态摘要，返回MANUAL/原方向和原workline |
| async accept_task_issued/accept_task_queue_changed/accept_task_plan_delta/accept_task_cancel(ctx, payload) -> Outcome | M1；receiver原事务，状态/版本/成员及SupplyChange原子 |
| async recheck_task_plan(ctx, payload, original_response:Mapping) -> Outcome\|None | M1；receiver锁后刷新原响应再委托，仅凭原未应用原因/当前完整条件裁决；不锁或改公共收据，不自动应用/不伪造成功 |
| async accept_scan2(ctx, arrival:Scan2Arrival) -> None | M2；原arrival关联、合法初始准入或普通不准入/暂缓事实；不另生成经过 |
| async accept_manual_completed(ctx, payload) -> Outcome | M2；只可靠保存通知和待应用依据，随后独立匹配/应用 |
| async accept_direct_pick_completed(ctx, payload) / accept_return_rack_completed(ctx, payload) -> Outcome | M3；出库原成员版本/截止范围完成 |
| async read_current_task(session, *, work_line_id:int) -> CurrentTask | M1；原在办执行上下文，不因成员撤销抹掉在制段；不把排队或已结束任务当有效任务 |
| async read_successor(session, *, work_line_id:int) -> SuccessorContext | M1；按原队列/prepare实际判定提供UNKNOWN/CHECKING/AVAILABLE/NONE及证据，查询不领取/不发HTTP，查空不直接授予NONE |
| async scan_pending(*, limit:int=100) -> int | M5；可无参调用，返回本轮检查数，不作完成证明 |
| async advance(kind:str, record_id:str) -> str | M5；一个原记录的新会话整事务推进，返回持久业务状态 |

M3的转运架离场消费M1任务记录中的原COMPLETED事实；M3隔离测试可设置这一权威输入来验证离场条件，M4负责形成该事实并联合回归M3，不反向要求M3先实现整单确认。

kind 仅本地：TASK、MANUAL_BIN、MANUAL_COMPLETION、MANUAL_RACK，以及组件原SCAN/SCAN2_RELEASE/FEED/RETURN_BATCH/RACK_CYCLE/DRAIN。前四者分别定位原任务、生产arrival作业、原完工通知、原生产架执行；后六者构造原 RollerWorkRef 交组件。不建组件种类副本或跨任务通用调度状态。

本计划消费的组件接口以已合并 ports.py/service.py 为准，不重新定义 DTO：

| 原组件方法 | 约束 |
| --- | --- |
| BinRoller(CommandOwner("smt_manual"), capabilities, production) | production实现原Production三个方法；M1/M2先有真实端口，M5将其接入正式工厂；默认TABLES用于产品，隔离测试可注入独立tables |
| apply_supply_change(session, change:SupplyChange) -> None | 原receiver事务；change_id关联原受理消息，added/withdrawn均为精确SourceFaceRef元组 |
| get_supply_progress(session, *, work_line_id:int, task_id:str) -> tuple[SupplyProgress,...] | 不返回withdrawn，撤销依据只从宿主原成员取得 |
| release_scan2(session, *, arrival_id:str) -> str | 只传原arrival，返回唯一command_code，不自行构造释放命令 |
| get_scan2_release_result(session, *, arrival_id:str) -> CommandView\|None | 可选原结果读口，不加入人工整单确认统一前置 |
| get_pending_upper_id(session, *, kind:str) -> str\|None；list_pending(session, *, kind:str, after_id:str\|None, upper_id:str, limit:int) -> tuple[RollerWorkRef,...] | M5唯一扫描的六类组件候选；limit为分页大小，不是整轮截断 |
| advance(session, work:RollerWorkRef) -> str | 宿主传入完整独立单条事务，原组件kind/record_id原样传递，不递归其他待办 |

共享Departure模型的实际名称是 `DepartureRequest`，来自 `app.businesses._components.bin_roller.request.rack_departure_decide_v1`；响应沿该模块DECISION适配器，READY/WAIT不新注册。

<a id="smt-manual-m1"></a>
## Task M1：一份人工任务生命周期、计划及供箱需求

**Files:** 创建models/repository/task_flow/contracts的任务部分、roller_ports中的两项任务读口与上述任务方法；共享conftest从本阶段提供真实任务flow+端口的隔离装配；测试 test_tasks.py、test_plans.py、test_cancellation.py、test_task_routing.py。共享callback/request与公共receiver扩展只消费ST1/ST2，不重写。

**Interfaces:** 消费ST1/ST2原归属与模型、已合并SupplyChange/SourceFaceRef/apply_supply_change；产出宿主任务真相源、原prepare/计划输入及成员、可靠供箱变化、read_current_task/read_successor，供M2～M4的真实组件装配读取。单线在办约束与版本属于本业务表。

- [ ] **步骤1：写任务RED。** test_manual_directions_share_one_execution_slot：混合inbound/outbound同线同优先级，按原时间/task_id领取一个原prepare；queue_changed与领取并发按提交先后裁决。test_task_identity_and_direction_never_change：模式/方向/工作线冻结、重复task_id不重开；缺失/非法business_type拒绝，合法AUTO不归本宿主处理。test_not_before_and_successor_read_original_queue 验not_before未到不领取、原issued时间不刷新；读口不领取/造意图，CHECKING/AVAILABLE/NONE须有实际准备/队列依据，查询故障为UNKNOWN/错误而非无后继。
- [ ] **步骤2：写计划/撤销RED。** test_manual_plan_resources_follow_original_direction：入库仅供箱、出库三类资源与首版限制、直接取料-only有效、后续非空增量、跨方向已定义字段拒绝；test_pending_plan_requires_original_retry：准备早到/缺版/停用、成功重放/同版新身份/确定冲突分开；test_supply_change_and_member_cancel_commit_together：精确原版本追加/撤销与组件同事务，任务取消只QUEUED/PREPARING未应用首版，不删除在制义务。test_queue_rejection_is_stable_but_plan_recheck_is_limited 验queue无实际变化/同版新身份/领取后更新的409及首次时间稳定，成功重放即使任务已准备仍DUPLICATE；plan当前同版新身份只比完整data，旧版新身份/同版异内容拒绝，顺序变化不等价。test_exact_member_cancellation_skips_only_requested_members 验未知/已取消/不可取消成员跳过且全跳仍成功、slot_ids省略覆盖本版本本面、指定仅精确SLOT、父任务终态仍可撤销保留成员，不改变plan_revision或原动作。
- [ ] **步骤2a：写停用RED。** test_disabled_workline_preserves_original_obligations 验首次缺线REFERENCE_CONFLICT、停用/不匹配STATE_CONFLICT；在办时停用暂停领取/新prepare/新设备作业、新plan不应用，取消/原结果/既发报告/历史确认继续。恢复启用仅允许WMS原plan重试，不自动应用，不解除HELD/UNKNOWN、不改原owner/绑定；M5正式链路复验。
- [ ] **步骤2b：写共享接入RED。** test_plan_recheck_keeps_manual_changes_atomic通过真实共享入口验证原待定计划并发重报后成员/版本/SupplyChange及下一意图只一次，成功收据不降级；外层回滚不留业务或通知。使用ST1锁后刷新/5秒超时及冻结兼容验收证据，不在宿主重写收据保护或字段排列组合；恢复读取原@v1有效输入，后到消费者不改变其含义。
- [ ] **步骤3：观察RED。** `rtk uv run pytest tests/businesses/smt_manual/test_tasks.py tests/businesses/smt_manual/test_plans.py tests/businesses/smt_manual/test_cancellation.py tests/businesses/smt_manual/test_task_routing.py -q`；领取竞争及回滚使用独立PostgreSQL，不能以缺库跳过当通过。
- [ ] **步骤4：最小实现并GREEN。** 保存原输入、不可变属性、原prepare身份、未应用原因及连续版本，原接收事实与业务/组件需求一起提交；准备结果只从公开get_result消费，不能由计划反推；回滚不留下执行位/供箱需求。重跑步骤3和共享ST1/ST2宿主接缝回归。先不进入生产catalog，不引入自动入库归档政策。

<a id="smt-manual-m2"></a>
## Task M2：两方向共用人工准入、WAIT和箱级完工

**Files:** 创建manual_bin_flow、准入request、完工callback；补models/repository/contracts及accept_scan2/accept_manual_completed；测试 test_scan2.py、test_admission.py、test_completion_notifications.py、test_waits.py。

**Interfaces:** 消费M1原任务、组件Scan2Arrival及release_scan2；产出原arrival人工作业、原准入/等待/完工消费依据、业务释放决定和原command_code。不注册SCAN2事件，不直发方向命令。

- [ ] **步骤1：写准入RED。** test_both_manual_directions_use_bin_admission 验raw=A000000001-C而请求bin_code=A000000001，data仅原task_id/bin_code/scanned_at三个字段；WORK_REQUIRED核对task后等待、NO_WORK唯一释放、WAIT保存固定到期点。test_invalid_scan_or_missing_context_keeps_arrival 验不可读/朝向错/原上下文不成立留证不请求不放行；合法直达SCAN2不要求SCAN1/投箱回调或原供箱成员匹配。
- [ ] **步骤2：写WAIT/身份RED。** test_wait_expiry_creates_one_original_evaluation 验严格1～60000、到期前不求值、重复扫码/重启不刷新首次时间；并发即时/Beat到期仅一条新求值，旧WAIT不覆盖后续终局。test_uncertain_admission_keeps_original_identity 验未知/503只恢复原请求；不是每次唤醒重新uuid/send。有效WAIT的固定到期点取首次可靠保存该决定的本地时间+retry_after_ms，原响应/到期依据一同持久化。test_invalid_admission_response_never_releases 验WORK_REQUIRED错task、已知分支字段混发/null、错误HTTP/code、NOT_FOUND/UNAVAILABLE/IDENTITY_MISMATCH均不授权，真正未知备注沿响应策略。
- [ ] **步骤3：写完工RED。** test_completed_receipt_precedes_application 验先可靠保存再独立应用；test_completion_binds_only_unique_original_work_required 验原消费判重后唯一经过、task匹配、NORMAL/NG均原释放；歧义/冲突/NO_WORK后新完工不覆盖；test_early_completion_is_never_autobound 验早到后到准入也不自动释放。test_new_arrival_does_not_inherit_old_release 验同码新经过及迟到旧完成/Result不释放新箱。test_manual_callback_error_and_field_policy 验有效内容重复、身份漂移、类型/null/时间越界及旧接口默认不变；
- [ ] **步骤4：观察RED。** `rtk uv run pytest tests/businesses/smt_manual/test_scan2.py tests/businesses/smt_manual/test_admission.py tests/businesses/smt_manual/test_completion_notifications.py tests/businesses/smt_manual/test_waits.py -q`；独立PostgreSQL验证准入/完工/原释放与外层回滚以及到期竞争。
- [ ] **步骤5：实现并GREEN。** 原arrival为唯一生产经过关联，保留raw/bin/scanned_at、原task、每次原请求和已消费决定；WAIT计时/通知应用归本业务pending。完成通知不以completed_at/最新等待猜匹配；成功消费、首次完工、释放决定和组件原命令同事务。完工薄入口先按原任务绑定smt_manual，不按当前任务/箱码猜owner；显式使用ST1的CallbackErrors及必要Unavailable，沿人工API保留真正未知字段，completed_at严格为正且不晚于信封timestamp。本接口不启用plan的recheck/archive清理策略。重跑步骤4与现有组件test_scan2_port/test_scan2_release，不能用组件测试端口的PERMIT/WAIT冒充人工业务闭环。

<a id="smt-manual-m3"></a>
## Task M3：出库直接取料与可选转运架

**Files:** 创建manual_rack_flow、两份出库callback/arrival_report request；补models/repository及公开受理方法；测试 test_direct_pick.py、test_transfer_rack.py、test_rack_reports.py。

**Interfaces:** 消费M1原SLOT/架面版本与首版target、既有TransportService的move_rack/rotate_rack/get_result/list_unclosed_predecessors和组件唯一DepartureRequest模型；产出原到位报告、精确成员完成、原生产架流转/未决引用。五层供箱与回箱不进入本模块。

- [ ] **步骤1：写RED。** test_direct_pick_requires_original_rack_arrival 验原rack/工作位/面向可靠到位才报告，ACK不制造事实；test_face_and_through_revision_completion_do_not_cover_new_members 验面级精确版本、整架截止版本连续补齐后只闭对应范围，父任务终态不代替成员判断；明确验证整架task_completed在原任务未可靠保存时503原身份重试，无成功ACK/无无归属成员更新；按ST1非recheck的Unavailable路径回滚本次收据，原任务保存后同一通知可202，再重报DUPLICATE，不能永久缓存早到503。test_rack_callback_contracts_remain_operation_specific 验direct_pick_completed的data严格拒绝未知字段且timestamp为正；整架通知和其他通知沿各自未知字段策略，completed_at均为正且不晚于信封；两薄入口绑定原任务和outbound方向，使用ST1错误/Unavailable支持，不批量采用plan重验。
- [ ] **步骤2：写资源/门槛RED。** test_optional_transfer_rack_does_not_gate_unrelated_work：未指定不要求绑定/不编排，首版指定才要求；test_return_rack_never_receives_roller_bins：RETURN_RACK不作回箱目标；test_arrival_report_obligation_is_separate_from_rack_motion：原报告未收妥参与整单必报，不把ACK设成所有合法换面/离场的统一门禁。入库使用直接取料通知/资源拒绝且无动作。test_departure_wait_and_unknown_keep_original_context 验四字段current_face不可知显式null、WAIT固定到期点/新事实合法新求值、未知恢复原身份、READY冻结去向只建原Transport，ACK不等于离场成功。test_wrong_target_observation_never_authorizes_production 验公开position_observation只补原实际位置，目标/面向不符不报告到位、不续执行，迟到旧结果不清新架。
- [ ] **步骤3：观察RED。** `rtk uv run pytest tests/businesses/smt_manual/test_direct_pick.py tests/businesses/smt_manual/test_transfer_rack.py tests/businesses/smt_manual/test_rack_reports.py -q`。
- [ ] **步骤4：实现并GREEN。** 原Transport/caller/cycle及定位引用保持；使用共享DepartureRequest/DECISION与原capabilities，不重注册；转运架需本任务COMPLETED、原进场成功和当前在位后求离场，不等独立来源架全部离场。重跑步骤3及受影响Transport公开结果/通知回归；未知不重授/改址/换号，不复制组件CTU状态机。

<a id="smt-manual-m4"></a>
## Task M4：人工整单完成、撤销进度与新到箱时序

**Files:** 完成task_flow/manual_bin_flow/models/repository的完成条件、原确认与暂缓恢复；app/core/config.py/.env.example交付可选首批等待配置与test_first_plan_config.py；测试 test_task_completion.py、test_supply_completion.py、test_confirmation_races.py。

**Interfaces:** 消费M1～M3原任务/成员/作业/报告及get_supply_progress；使用共享CompletionConfirm/CompletionDecision合同和原WmsRequests；配置SMT_MANUAL_FIRST_PLAN_WAIT_MS为可选正整数毫秒，默认None，构造参数注入（无新增wire/数据库列）。产出当前版本确认、有效终态或等待、暂存箱可恢复依据，不以SCAN2 Result作人工统一门槛。

- [ ] **步骤1：写供箱RED。** test_unstarted_withdrawal_closes_demand_without_feed_success 验原撤销事实+匹配false/false可结清，不伪造成功；test_started_withdrawal_keeps_original_pending 验已开始尚无feed也不据查空结束；test_completed_started_withdrawal_uses_actual_progress 验已撤销原需求true/false按实际收口；test_missing_progress_or_withdrawal_evidence_never_implies_completion 验缺关联/无撤销依据false/false不完成，不直查组件表或要求全局架流结束。
- [ ] **步骤2：写人工完成RED。** test_manual_completion_does_not_wait_for_scan2_result 验最后一箱完工/NO_WORK及唯一原释放提交后可确认，COMPLETED先于放行Result仍合法；公共FIFO/回架/drain/独立离场不加门槛；反向用例验放行Result先到也不能替代WMS COMPLETED。test_target_only_outbound_requires_reliable_transfer_arrival 验无供箱来源但指定目标架的可靠到位前置；未指定不增条件。必报未知/原准入WAIT/待核对完工不漏计。
- [ ] **步骤3：写确认竞态RED。** test_new_arrival_waits_for_original_confirmation_resolution 验确认未决新箱事实/原任务关联保存且仅暂缓首次准入，原WAIT/完工/释放/公共流程继续；test_stale_or_in_progress_resumes_saved_arrival 验有效非终局及当前条件成立后才启动准入，原scanned_at不变；test_old_completed_never_discards_new_plan_or_arrival 验矛盾留证、不关单/直放/换号。并发只一条未决确认，未知原身份；revision=0只有显式首批等待期/无待定计划才可发，未配置继续PREPARING。test_first_plan_deadline_is_explicit_and_durable 验正整数配置/None、布尔值/零/负值非法，期限从原PREPARE_ACCEPTED首次可靠保存起算；重启/重报不刷新，存在早到/缺版/其他本段义务不确认0。test_confirmation_branches_keep_original_retry_rules 验STALE严格大于原请求版且无混合字段，本地已补齐不重复等版；BUSINESS_IN_PROGRESS在无新事实且未到期时不再确认，有效新事实或到期才按合同新身份确认；畸形分支/错误HTTP/未知只留原阶段。
- [ ] **步骤4：观察RED。** `rtk uv run pytest tests/businesses/smt_manual/test_task_completion.py tests/businesses/smt_manual/test_supply_completion.py tests/businesses/smt_manual/test_confirmation_races.py tests/businesses/smt_manual/test_first_plan_config.py -q`；独立PostgreSQL验新计划/到箱与COMPLETED应用的提交顺序。
- [ ] **步骤5：实现并GREEN。** 发起与消费都重验当前版本/原直接义务；有效非终局按合同等待/补版后重新确认，原尾箱Result迟到不制造新增工作。完成/取消/切换只停止新需求，原pending不只筛当前任务/EXECUTING；保留终态任务号/原owner，重报不复活。重跑步骤4并回归M1/M2，不新增全线清场、七天清理或补绑恢复器。

<a id="smt-manual-m5"></a>
## Task M5：正式装配、唯一推进与B7状态迁移

**Files:** 创建/完成definition/composition/service/roller_ports及目录，修改catalog；交付第2节组件/宿主迁移、tests/migrations/test_bin_roller_state.py/test_smt_manual_state.py；测试 test_assembly.py、test_pending.py、test_transactions.py、test_recovery.py。

**Interfaces:** 消费ST1/ST2和M1～M4全部真实业务方法，完成第2节服务接口与components精确映射；承接B7首宿主装配/组件表迁移，不将公共内部实现重新列为任务。生产源码使用组件默认正式表，tables注入仅限隔离测试。

- [ ] **步骤1：写装配RED。** test_catalog_loads_real_manual_and_shared_inputs_once 验API/worker同一显式清单，组件资源/模型唯一、宿主COMMANDS为空、共享六任务/四点唯一目标、原business:smt_manual与Transport/ECS owner不变；可选RETURN/TRANSFER仅计划使用时要求。sorting_task无模型/任务；没有真实自动业务时不注册其占位包。
- [ ] **步骤2：写推进/事务RED。** test_pending_pages_cover_production_and_all_roller_kinds 验固定本轮上界、各类轮换、跨页/等待/单条失败继续、后来新项留下一轮；test_terminal_task_keeps_original_pending 验原释放/回流/生产架在任务结束仍扫描。test_serialization_retries_whole_advance_in_new_session 验相关架流入口首SQL前SERIALIZABLE、commit40001整笔最多3次、耗尽保留待办、同连接回到receiver/无关业务隔离；提交未知重读原记录，不用暂时查空换身份。test_unassigned_scan4_reaches_drain_through_original_scan_pending 验尚无DRAIN/RACK_CYCLE记录的合格SCAN4也能从原SCAN发现求架，不等先造协调记录。
- [ ] **步骤3：观察RED。** `rtk uv run pytest tests/businesses/smt_manual/test_assembly.py tests/businesses/smt_manual/test_pending.py tests/businesses/smt_manual/test_transactions.py tests/businesses/smt_manual/test_recovery.py -q`。使用真实receiver/runtime与组件公开方法；独立PostgreSQL覆盖事务/并发，不能仅以方法返回数断言物理完成。
- [ ] **步骤4：最小正式装配。** 纯声明、固定ENTRY/MODEL模块，两组件各一个实例，components键精确为sorting_task/bin_roller，唯一scan_pending/advance；工厂注入原capabilities/session_factory，read方法不领取任务。复用M1/M2真实任务读口及SCAN2端口，并按原锁序重验。组件架流相关的原单条统一advance整笔按专项隔离，不查询后再升隔离或拆DRAIN执行器；无关纯生产仍沿既有RR。真实宿主和必要表齐全才列catalog，不修改共享任务壳以查询业务表。用公开get_pending_upper_id/list_pending遍历六类组件待办；
- [ ] **步骤5：迁移RED→实现→GREEN。** `rtk uv run pytest tests/migrations/test_bin_roller_state.py tests/migrations/test_smt_manual_state.py -q`。测试当前基线head（本次为f0b220261031，执行时重新确认）→组件状态→宿主状态，干净逻辑库与已有平台数据保留、必要业务唯一约束和唯一head；组件迁移若已由其他正式宿主交付则只引用其revision，不再建一次。迁移由Alembic生成真实revision，MODEL_MODULES和数据库实际表一致，不在启动create_all。数据库操作仅限后续授权的可丢弃测试库，不运行用户运行库迁移。
- [ ] **步骤6：GREEN。** 重跑步骤3/5和 `rtk uv run pytest tests/businesses/smt_manual tests/businesses/sorting_task -q`，以及受影响catalog/runtime/WMS回调/Transport/ECS/业务任务回归。阶段结论只记录正式模型/装配和独立库行为，不把它当真实worker或现场证据。

<a id="smt-manual-m6"></a>
## Task M6：真实任务链、两线恢复及交付证据

**Files:** tests/tasks/test_smt_manual_worker.py、tests/businesses/smt_manual/test_owner_wakeups.py/test_two_worklines.py/test_roller_boundaries.py，必要共享fixture，docs/testing/smt-manual.md；沿既有tests/tasks/test_business_tasks.py验证原任务壳，无新产品Celery任务。

**Interfaces:** 消费M5正式catalog/唯一服务与现有business.advance_pending、after_commit和5秒Beat。只验证/记录真实组合；修复若涉及生产代码，则回到对应任务补回归，不在本阶段引入第二任务路径。

- [ ] **步骤1：写唤醒/两线RED并运行。** test_transport_acceptance_wakes_original_manual_owner 验有效RECEIVED/202与DUPLICATE/200落DONE、仅外层提交通知，失效租约/重复完成/失败未知/无owner/回滚不误通知；test_ctu03_acceptance_releases_cycle_without_final_outcome 验原RACK_CYCLE恢复不等最终位置。test_two_lines_keep_tasks_arrivals_and_returns_separate 验设备/位置/同箱码/两方向交错不串线，旧结果不改挂新arrival或清新架。运行 `rtk uv run pytest tests/businesses/smt_manual/test_owner_wakeups.py tests/businesses/smt_manual/test_two_worklines.py -q`，优先复用既有通知实现。test_manual_host_preserves_new_scan3_round_and_old_obligations与test_scan4_retry_keeps_original_fifo_under_manual_host通过正式手工receiver/runtime验新SCAN3不等旧轮/上游Result、旧命令/分配/Transport保留、本轮SCAN4 SUCCESS资格及失败重扫原FIFO；使用已有组件公开行为，不复制算法；同一命令补入 `tests/businesses/smt_manual/test_roller_boundaries.py`。test_wms_ecs_and_transport_result_wake_original_manual_owner 验原WMS决定保存、ECS实际应用及Transport有效outcome_revision推进后才唤醒原业务，回滚/无新应用不误通知；与submit接纳分开，旧终态任务的原义务仍由同一宿主推进。
- [ ] **步骤2：真实worker验收。** `rtk uv run pytest tests/tasks/test_smt_manual_worker.py -q`。使用显式TEST_DATABASE_URL/TEST_REDIS_URL的独占测试资源，关闭eager，由真实broker/worker执行原business.advance_pending任务；一个Beat调用同一已声明入口，原输入/结果提交唤醒及通知失败后的周期兜底都能推进持久待办。test_manual_worker_advances_wait_completion_and_roller_pending用真实人工WAIT到期/WORK_REQUIRED完工/完成确认响应与公共pending混合队列验证有界轮换，重复唤醒无重复请求，停用/恢复按M1保持原门禁；不能只测CTU03就把B7人工闭环勾选。进程停止/重建不换请求/命令/Transport身份。无配置跳过须记为未验，不用eager/直接调用service充当worker证据；不触及现场或发送真实设备请求。
- [ ] **步骤3：最终软件检查。** 在最终代码快照运行本计划专项、受影响框架/组件/通知/任务测试和一次适当全量 `rtk uv run pytest -q`；`rtk uv run ruff check .`、`rtk uv run ruff format --check .`、`rtk git diff --check`，可丢弃库内核对 `rtk uv run alembic heads` 一个head。无后续影响变更不重复完整测试/审计，缺项保留未完成。
- [ ] **步骤4：登记分层证据。** docs/testing/smt-manual.md逐项记录ST/M任务、schema/head、共享源码/附录、两线绑定、请求/命令原关联和各验证环境；本地/独立DB/真实worker/WMS/ECS/RCS/现场分别列状态。公共C07复用合并的新SCAN3独立建轮规则，旧履约保留/本轮SCAN4资格不串，不为宿主重新造一套组件测试。真实供应商签认/联调/现场物理验收未做则明确未验。

## 3. 依赖与验收映射

| 依赖/门槛 | 责任与不满足时的结论 |
| --- | --- |
| ST1/ST2单份合同/错误/重验/原归属接缝 | PR #44/380aeba已合入，专项24及全仓4654/52通过；[共享分工](2026-10-31-sorting-task-implementation.md#sorting-task-ownership)独立交付：ST1隔离验收及收据保护/冻结合同，ST2真实登记/分派；M1消费并验宿主原子性，自动S1复用，不要求自动生产先交付 |
| BIN_Roller已合并组件及公开进度/结果/通知 | M1～M4测试装配可用；不做B1～B5第二实现，不把测试生产口替代人工逻辑 |
| B7组件状态迁移/首个真实消费者 | M5与B7共同闭合一次；无表/无真实宿主不标正式装配完成，后续自动宿主不重复建组件表 |
| manual_bin新名称、共享task_id原归属及各严格wire | 正式WMS集成前落实；本方测试不代表对端已接受，未知字段和错误策略按每operation保留 |
| 实际RETURN/TRANSFER/FIVE_RACK/INLET/OUTLET和四点绑定、模板/面向 | 只用可丢弃测试配置做软件验收，不复制旧库或推断在线；现场配置与设备合同另验 |
| 首批计划等待期限 | M4交付SMT_MANUAL_FIRST_PLAN_WAIT_MS，M5正式工厂注入；双方明确配置才允许从原准备首次可靠保存时间计时确认0，None继续PREPARING |
| 非eagerworker及Beat | M6独立证据，不用API健康/单测通过替代；真实发送/部署另需授权 |

覆盖自查：设计§1～4/§9→ST1/ST2/M1；§5→M2；§6→M3；§7→M4；§8/§10/§11→M5；§12→M5/M6。API六类任务、新人工接口、三类出库专属接口、供箱撤销/完工关联/确认竞态均有对应命名验收，C07/Transport接纳使用合并基线。

共享ST1/ST2交付与最新评审/CI结果见[共享计划§11](2026-10-31-sorting-task-implementation.md#sorting-task-merged)。本计划M1～M6保持未勾选，真实宿主、正式catalog/迁移、SMT运行恢复及WES完整物理闭环仍未验收。本轮仅同步文档，未执行新的RED/GREEN、产品测试、数据库操作、部署或真实请求；后续宿主实施另需授权。
