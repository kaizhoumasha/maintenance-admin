# SMT 自动上架与拣料统一实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans only after separate implementation authorization. Steps use checkbox (`- [ ]`) syntax for tracking。本计划S1～S7及I/O方向任务仍未实施；共享ST1/ST2已随PR #44合入develop。本轮仅同步文档，不实施代码、不运行产品测试或迁移、不提交、不部署或真实发送；宿主实施与验收步骤保持未勾选。

**Goal:** 交付一个 `smt_auto` 业务子包，共用任务、队列、原归属和恢复入口，按冻结的 `business_type` 运行 inbound/outbound，分别满足资源、逐盘、异常及完成合同。

**Architecture:** 根目录持有共同任务状态、`SmtAutoService`、公共接缝和一份严格命令声明；`inbound/`、`outbound/` 持有自己的生产过程。共同交付只安排一次，方向任务只补生产行为及资格判断，不另建任务框架、服务工厂、调度器或通用逐盘状态机。

**Tech Stack:** Python 3.13+，既有 FastAPI/Pydantic/SQLAlchemy/Alembic/Celery/pytest、独立 PostgreSQL；不新增依赖。

**Spec:** [统一业务设计](../specs/2026-10-31-smt-auto-business-design.md)、[统一自动API](../../integrations/smt-auto-api.md)、[sorting_task共享计划ST1～ST2](2026-10-31-sorting-task-implementation.md)、[BIN_Roller计划B1～B7](2026-10-30-bin-roller-implementation.md)。本文只维护实施位置、步骤和证据归属；字段及响应规则以统一API为准。

## 阅读与实施导航

- [S1：共同任务、队列、计划与取消](#common-tasks)
- [S2：公共生产接缝与单份机械臂合同](#common-ports)
- [S3：inbound专属I1～I4](#inbound-steps)
- [S4：outbound专属O1～O4](#outbound-steps)
- [S5：共同释放与完成确认](#common-completion)
- [S6：唯一装配、迁移、待办与恢复](#common-runtime)
- [S7：合同回归与分层验收](#common-acceptance)
- [统一验收追踪](#acceptance-matrix) · [D1～D8交付依赖](#delivery-dependencies) · [原任务迁移位置](#previous-task-map)

## Global Constraints

- 业务码/owner_domain=`smt_auto`，WMS business_key=`business:smt_auto`，新ECS owner=`CommandOwner("smt_auto")`；方向和组件均不是独立业务。
- issued固定task_type=AUTO，必填且冻结business_type=inbound|outbound、workline_code及原下发时间；其他任务操作不增加模式/方向字段。已有task/arrival/动作按原归属恢复，不随工作线改配或当前任务改向。按本次方向实际使用核对资源，不要求仅做一方向的线具备另一方向全部专属绑定。
- 同线共用任务表、队列和PREPARING/EXECUTING部分唯一约束；符合not_before/启动条件的QUEUED按dispatch_sequence、原issued.timestamp、task_id排序，优先序不唯一，不抢占；不同线独立。不建执行位表/忙碌位。
- 六类sorting.task.*只由sorting_task注册一次；四点由BIN_Roller唯一受理。共享错误、Unavailable、recheck和可选只读guard消费ST1/ST2，不在自动包再次扩展receiver。旧登记默认不变，field_path首版可省略。
- 原plan重验按[共享1.1](2026-10-31-sorting-task-implementation.md#sorting-task-recheck)由receiver先锁定/刷新原收据，宿主随后按WorkLine→Devices按ID→原业务动作写入，成功收妥不可降级，advance不反向锁收据。首次正式接入前完整交付四分支，后到宿主按[1.4](2026-10-31-sorting-task-implementation.md#sorting-task-v1-freeze)消费冻结@v1，不原地增补有效字段或重解析旧待定输入。
- 一份bin_roller、一套接缝和scan_pending；组件及方向无独立业务码、Sender、Celery、WAIT调度器、手工锁/租约/资源预占/协调版本。WMS负责库存、绑定、容量/占用、业务唯一性和最终决定；ECS/PLC负责运动、互锁、HOLD及实物恢复。
- receiver使用原调用方事务；独立推进按WorkLine→Devices（ID顺序）→原业务动作，消费版本/下一意图原子保存，只读公开结果。纯生产REPEATABLE READ；实际进入BIN_Roller架流/正常求架检查的原advance按其覆盖表首SQL前SERIALIZABLE，不扩到receiver/候选短扫描/其他业务。40001覆盖commit，最多3次新会话整事务重试；提交不明不当回滚。
- 新ECS命令使用PROTOCOL_RETRY_WINDOW=300秒；历史operation/身份/timestamp/正文/owner/路由/期限/附录/证据不改，不回填历史null，不搬另一项目数据或运行配置。
- 正常成功必须有已应用的原Result及严格成功字段；ACK、返回命令号、online/IDLE、空查询、消息过期不证明物理完成。任务终态、直接收口、inbound归档和公共排空分别判断。
- 实施时按每任务RED→观察失败→最小实现→GREEN；导入/fixture失败不是业务RED。测试表独立MetaData、显式创建/finally清理，PostgreSQL用可丢弃库；未配置跳过不算通过，配置后连接失败必须失败。测试文件不互相import。
- 仅完整真实宿主进入正式catalog；隔离测试可用同形端口，但不得加入产品占位类、假合同或测试业务。迁移执行、提交、部署、真实发送另需明确授权。

## Review Focus

- 同线混合方向领取/queue_changed竞争和原任务归属漂移——S1的队列及归属测试。
- 计划初始解析丢字段由ST1/ST2验；自动宿主的完整成员落地、早到/缺版原消息只应用一次由S1验。
- 同角色命令及本盘/后继参数串用，扫码时间错绑——S2合同、I2/O2参数测试及S7原关联回归。
- NG、空取、WAIT和明细保留差异被共用机制覆盖——I3/I4、O3及S6政策隔离测试。
- 新计划/新到位与旧COMPLETED交错、释放仅ACK、终态后原责任遗失——S5完成测试和S6恢复测试。

<a id="scope-and-files"></a>
## 1. 当前基线、文件与固定接口

2026-11-01交付状态核对：smt_auto、smt_manual正式生产包尚无实现；sorting_task的ST1/ST2已随[PR #44](https://github.com/kaizhoumasha/maintenance-admin/pull/44)合入develop（380aeba），专项24 passed、修复后全仓4654 passed/52 skipped，合并前两项及合并后develop CI均成功，见[共享合入记录](2026-10-31-sorting-task-implementation.md#sorting-task-merged)；S1等宿主任务仍待实施。BIN_Roller已有组件本地实现、共享声明与隔离验证，正式消费方/组件状态迁移仍属B7。本轮不以文档或隔离验证声称生产交付。

现有catalog/runtime/Capabilities、receiver收据及raw/effective digest、WmsRequests、EcsCommands、TransportService公开结果、after_commit和5秒Beat均复用。ST1/ST2的共享任务合同/分派及必要receiver接缝已合入，S1只消费；B7交付公共模型/组件迁移与正式接入。wes_backend只作已确认合同/设备参考，不移植其运行时、旧锁/容量窗口、入线成员门槛、失败报告或跨点等待。

下表是唯一文件责任表。共享ST1/ST2路径已存在，其余宿主路径为后续实施位置，各目录含__init__.py；实际存在性与迁移日期/head在实施时再核对。

| 文件/目录 | 唯一责任与交付任务 |
| --- | --- |
| app/businesses/smt_auto/models.py、repository.py、task_flow.py | S1共同任务/输入/供箱成员及原归属；S5共同完成确认引用/消费，不另建任务表 |
| 同包roller_ports.py、ecs_command.py | S2统一SCAN2委托/供箱适配及精确命令联合；S5补原释放，方向任务只提供自己的参数/事实 |
| 同包definition.py、composition.py、service.py | S6一份声明、工厂、SmtAutoService和唯一progression；不按方向另建 |
| 同包inbound/contracts.py、models.py、repository.py、source_flow.py、material_flow.py、archive.py | I1单层架、I2正常生产、I3异常、I4完成资格/7天明细与摘要；contracts只声明专属逐盘/取尽合同，不重建sorting.task模型 |
| 同包inbound/callback/putaway_source_rack_exhausted_v1.py | I1初始空架薄入口，沿原任务归属；归档检查消费I4，不注册第二任务入口 |
| 同包inbound/request/putaway_{work_plan,material_decide,material_movement_report,source_empty_decide}_v1.py | I2/I3四个专属严格请求，按规范文件名分别注册 |
| 同包outbound/contracts.py、models.py、repository.py、rack_flow.py、material_flow.py | O1生产架、O2两条正常链、O3异常、O4面终局及方向资格；不重建共享任务DTO/分支校验器 |
| 同包outbound/request/outbound_{bin_work_plan,return_rack_work_plan,bin_material_decide,rackslot_material_decide,material_movement_report,source_empty_decide}_v1.py | O2/O3六个专属严格请求；任务prepare/completion_confirm不在方向内重建 |
| app/businesses/_components/sorting_task/及wms_callback必要扩展 | ST1/ST2单份交付，S1只消费；实际适配缺陷回共享计划修复/回归 |
| BIN_Roller四点、供回箱/FIFO/drain、五层架、departure及组件迁移 | B1～B7单份交付；自动包只消费公开门面，不复制公共状态或测试工程 |
| tests/businesses/smt_auto/及conftest.py | S1/S2/S5/S6/S7共同行为、隔离端口装配和共享fixture；方向测试仅在inbound/、outbound/ |
| app/businesses/catalog.py；migrations/versions/2026-10-31_smt_auto_state.py；tests/migrations/test_smt_auto_state.py | S6一次完整业务注册、根及两方向必要表/约束；组件迁移独立串接 |
| app/core/config.py、.env.example | S5声明可选SMT_AUTO_FIRST_PLAN_WAIT_MS，默认None；S6工厂注入，G04验证配置/期限恢复，不覆盖现有.env |
| tests/tasks/test_smt_auto_worker.py | S7独占测试DB/Redis、关闭eager的真实worker/Beat恢复证据，不连接现场设备 |
| docs/testing/smt-auto.md | S7一份分层证据，内含共同项、inbound、outbound及外部交付状态，不另建两份综合验收模板 |

### 1.1 固定服务和外部门面

| 接口 | 输入输出与调用边界 |
| --- | --- |
| SmtAutoService(capabilities, *, first_plan_wait_ms:int\|None=None) | 构造时注入可选正整数毫秒；只存配置不持有session，原期限沿任务依据，不新增wire字段或期限调度表 |
| composition.build_service(capabilities) | S6构造一个无状态SmtAutoService；components精确含sorting_task、bin_roller，production指向本服务，工厂从get_settings读取并注入S5配置，不查数据库 |
| SmtAutoService.read_current_task(session, *, work_line_id:int) -> CurrentTask | 原共同任务/执行位提供上下文，不抹去已取消但在制的原关联 |
| SmtAutoService.read_successor(session, *, work_line_id:int) -> SuccessorContext | 原队列/prepare证据形成UNKNOWN/CHECKING/AVAILABLE/NONE，不凭空查询报NONE |
| SmtAutoService.accept_scan2(ctx, arrival:Scan2Arrival) -> None | 原receiver事务按arrival先查历史，首次绑定原任务/方向并委托生产；重复不读当前任务改绑 |
| SmtAutoService.scan_pending(*, limit:int=100) -> int | 唯一PROGRESSION，可无参调用；返回检查数，按上界/游标公平轮换共同/方向/组件待办 |
| SmtAutoService.advance(kind:str, record_id:str) -> str | 新会话推进一条原记录，返回持久状态，不以返回数表示物理完成 |
| WmsRequests.send(session, payload, *, operation_id) / get_result(session, *, message_id, operation, operation_id) | 精确已注册RequestPayload；保存原三引用，仅消费公开终态结果，不直读ReliableMessage |
| EcsCommands.create(session, *, owner, call_key, device_code, timeout_ms, command) | 保存原owner/call_key/command_code；通过get_command(... owner, command_code)/get_by_call(... owner, call_key)读原已应用结果，不调用发送器 |
| TransportService.move_rack/rotate_rack/get_result | 沿原caller/client_request_id/transport_task_id及公开位置观察；精确签名沿现有service，不建搬架ECS指令或直读TransportTask |

kind固定为TASK、INBOUND_RACK/INBOUND_BIN/INBOUND_PLATE/INBOUND_ARCHIVE、OUTBOUND_RACK/OUTBOUND_BIN/OUTBOUND_PLATE，另有组件原SCAN/SCAN2_RELEASE/FEED/RETURN_BATCH/RACK_CYCLE/DRAIN。record_id为该类原主键，前者按原task方向复核，后六类组装原RollerWorkRef交组件。call_key稳定包含方向、原授权出处、动作类型，不增加wire身份或另建结果反查路由。

[任务合同/分派与宿主状态的唯一分工](2026-10-31-sorting-task-implementation.md#sorting-task-ownership)必须先落实。TaskOwnerRef、read_task_owner、accept_task_*、recheck_task_plan及只读guard签名消费[ST2固定协议](2026-10-31-sorting-task-implementation.md#1-文件责任与固定本地接口)。组件类型及apply_supply_change/get_supply_progress/release_scan2/get_scan2_release_result/分页advance直接消费[BIN_Roller固定接口](2026-10-30-bin-roller-implementation.md#文件责任与固定接口)，不在方向段重定义。

ST1以隔离operation独立验公共扩展/旧默认、原收据并发及冻结兼容；ST2完成四个真实callback后再验errors、仅plan的recheck和可选guard登记并回归ST1。S1只验证自动宿主真实接入和状态原子性，不把未创建的正式入口当作ST1前置，也不另维护公共机制测试副本。

<a id="execution-records"></a>
## 2. 共同记录、原子交接与方向差异

下列共同要求只有一份。实现仍在各自根/方向模型和流程，表格不是新增通用状态机、授权表、消息表或公共运行时；方向任务引用它并补自身字段。

| 记录 | 原事实、唯一性与保存要求 |
| --- | --- |
| 根任务/原输入 | task_id在本宿主表唯一，跨消费者唯一性由WMS保证；不可变issued/原时间、阶段、队列/计划版本、prepare/确认引用及首次可靠接收时间。输入(operation,operation_id)唯一，实际应用(task_id,plan_revision)唯一；早到/缺版/停用未应用消息不占版本，同版新消息确认指向原应用，不复制正文/成员 |
| 计划成员 | 五层task/revision/rack/face与原顺序、SupplyChange关联；单层task/revision/rack，不能用含NULL面字段的组合唯一键逃过防重；RackSlot同任务同架面一次且保留原成员版本。撤销/完成/取尽分别保存，不复制CTU/清单状态 |
| 生产架过程 | 原进场/换面/搬离Transport、结果消费依据、可靠位置/面向及到位时间；成员指向原过程，旧结果不覆盖新过程。实际位置存业务事实，不回写静态WORKLINE绑定，不形成占位许可 |
| BIN作业 | 组件arrival_id与工作线唯一，保存首次task（无任务可无）、方向、箱码/scanned_at、求值/异常/终局和原释放；不以bin_code或未收口作业吞掉同箱真实新arrival，不造经过号或强制匹配供箱成员 |
| 本地盘/原授权 | 关联原BIN作业或来源面/到位、完整授权/来源参数、原取盘及Result扫码、唯一最终目标/后继、原放盘或NG及报告。首盘/异常READY、父盘唯一后继或最终RETRY各消费一次，不按WAIT请求ID多物化一盘 |
| 原求值/消费 | 直接归属上述记录，保存原WMS身份/message_id、有效决定/WAIT时点、命令/Transport/报告及已消费版本；公共收据可清理，恢复所需原业务依据仍保留。WMS同源同码只约束未闭合授权，不在WES建永久业务唯一键 |

| 交接 | 同一原事务内的要求 |
| --- | --- |
| 四类任务通知/计划重验 | receiver收据、原输入/阻碍、任务/版本/成员及精确SupplyChange一起提交；重验由共享receiver先锁定/刷新原收据，原响应及资格不从锁前缓存取得，宿主不反向锁收据。未应用只留待定，不建成员/动作；成功不降级。新旧数组顺序不改，同身份比较含原timestamp；跨身份当前同版只比较完整已定义data；旧输入按冻结@v1恢复 |
| 领取/准备 | 重读本线在办任务、排序及not_before，PREPARING与唯一prepare意图同提交；准备响应以公开结果消费，不因计划到达推定成功或自动应用早到计划 |
| READY/后继/RETRY | 先读原记录/意图，原授权消费、一次本地盘及原命令意图同提交；失效授权保留，不换源/重授。合法目标与独立后继分别处理，后继失效不扣本盘，响应自身矛盾不删字段执行 |
| 取盘扫码/最终决定 | 正常链只接受原已应用SUCCESS、actual_qty严格整数1、PUT_FINISHED、完整barcode/scanned_at且无阻断核对义务；消费与唯一material.decide意图同提交。Result先于ACK仍沿原应用规则，不另等Event/SCAN命令 |
| 放盘成功/报告 | 原成功消费与唯一报告意图同提交；发生时间取原放盘Result.finish_time，来源/条码/实际去向来自原盘。不以重试时间、当前任务或后盘代替；报告失败/未知留原事实，不重做取放 |
| WAIT/异常续问 | 首次可靠保存有效WAIT的本地时间+retry_after_ms（整数1～60000）固定最早时点；到期/通知/Beat并发只登记一次新求值，重放/重启不延时。原NG/来源关闭事实与异常阶段/下一意图同提交，阶段内WAIT不二次消费触发事实 |

取料终局只有有效work_plan.NO_WORK或正常ACCEPT明确无后继；CLOSE、来源取尽/关闭、缺架、取消、查空、WAIT均不代替箱/面终局。终局后旧计时/异常/补架不重开；当前合法未决晚到WAIT仍消费，历史WAIT不回退。新的命令/续问仍检查原arrival/所选来源及设备直接条件，UNKNOWN/HELD不因online/IDLE、重启或补架解除。

有效后继不统一等待前盘PUT或报告ACK；同源同码等待由WMS裁决。原报告独立恢复：WMS先消息去重、再完整完成事实去重、最后唯一授权匹配，零/多候选或目标矛盾按REFERENCE_CONFLICT，不猜最新盘。该WMS事务/库存闭合由D2验，WES只保存原事实及交付依据。

<a id="production-differences"></a>
### 2.1 方向差异的唯一执行对照

| 边界 | inbound（I任务实施） | outbound（O任务实施） |
| --- | --- | --- |
| 计划/资源 | added_bin_source_racks+added_source_racks；每版至少一类非空新增 | added_bin_source_racks+首版target_rack+added_return_racks；首版可仅目标，后续至少一种来源且不改目标 |
| 正常路径/参数 | 单层架→ARM02→扫码台→ARM01→SCAN2；WMS给放盘前占用层数/深度，空格0/0，不加一/推算 | BIN→ARM01→扫码台→ARM02→转运架；RackSlot两段ARM02。BIN原总厚度沿既有厚度×层数；RackSlot层/列/厚度/直径取完整授权，单位毫米/寸 |
| WAIT到期data | work_plan/material.decide可带最新source_racks及本地原进场映射，其他原事实不变；empty_decide只换信封身份/时间 | 两类work_plan、两类material.decide及empty_decide的原data不变，只换新operation_id/timestamp |
| NG及报告 | 正常目标明确成功才报告，FAILED/NG均不报、不增待成功报告；原NG成功才满足对应异常续问触发 | 正常/NG成功且位置确定均必报；NG报告收妥才触发该异常续问，不能套入库不报告规则 |
| 权威空取 | 原FAILED/0/PICK_EMPTY，observed_at=原Result.finish_time；SOURCE_ABSENT仅闭原来源，显式source_rack_exhausted=true才整架取尽 | 明确无料载体由D4签认；普通FAILED/PICK_FAILED/0/超时不证明空取。SOURCE_DONE仅闭原Cell/SLOT，无移动不报，不猜PICK_EMPTY/错误码 |
| 异常续问 | NG成功或SOURCE_ABSENT合法收口后，用原arrival.scanned_at、最新合法source_racks；不要求无关正常盘报告先收妥 | NG报告收妥或SOURCE_DONE后，用原BIN scanned_at或面arrived_at；BIN CONTINUE可选原Cell、CLOSE排除它，RackSlot关闭原SLOT |
| RETRY | 原最终RETRY只授权一次新取盘过程/命令，不改旧FAILED；新命令成功接正常链，再次明确空取才形成新观察 | 相同一次授权规则，来源为原BIN_CELL或RackSlot；新命令未知不另求许可 |
| 完成资格/明细 | 单层需求终局+原接料生产及正常必报；仅本方向7天清理/摘要/只读guard | 原供箱+实际RackSlot面终局+BIN/面正常和NG必报；无RackSlot计划不补面条件，不采用入库清理/归档拒绝 |

所有异常阶段只在原箱/面未终局且无待消费授权/未决求值/在途取盘等冲突时建立；已有有效后继继续原授权，不叠加work_plan。完整字段、严格联合、共用成功条件和完成差异分别见[API逐盘合同](../../integrations/smt-auto-api.md#material-operations)、[共同规则](../../integrations/smt-auto-api.md#production-rules)及[完成映射](../../integrations/smt-auto-api.md#task-completion-conditions)。

<a id="common-tasks"></a>
## Task S1：共同任务、队列、计划与取消

**Files:** 根models.py/repository.py/task_flow.py及方向models/repository的成员/状态读取；共享任务DTO、validate_task_branch及响应模型只消费ST1，不在方向contracts重建。Test根test_tasks.py、test_task_routing.py、test_plans.py、test_callbacks.py。

**Interfaces:** 消费ST1/ST2模型/TaskOwnerRef/宿主协议，产出唯一任务真相源、prepare引用、连续成员与供箱变化，供S2和方向任务读取；完成确认引用留S5，归档政策留I4。

- [ ] **步骤1：写RED。** `test_mixed_direction_queue_has_one_active_task`覆盖同优先序/原时间/task_id排序、not_before、领取/queue_changed竞争及每线唯一prepare。`test_task_direction_is_immutable`从真实共享入口验证语法合法的AUTO两方向进入正确状态，合法但本线不支持的模式/方向STATE_CONFLICT，原task不改线改向；字段枚举/null等排列组合由ST1验。`test_task_reference_uses_original_owner`验改配/终态/收据清理后原归属和拒绝无业务写入；缺失/歧义机制沿ST2，不另写解析器。
- [ ] **步骤2：补计划/重验RED。** `test_workline_disable_preserves_original_obligations`按API9.5验证停用不领取/prepare/新建或发送作业命令，新plan不应用，合法取消/原结果和报告继续；启用后不自动应用plan或解除UNKNOWN/HELD。 `test_plan_delta_preserves_direction_resources`消费ST2已校验载荷，断言两方向全部合法资源落入各自成员/原顺序、供箱跨版同面与RackSlot同面一次；通过真实入口验证跨向字段被ST1/ST2拒绝且无宿主写入，不在本地重新解析。`test_precise_cancel_preserves_started_work`验原版本选择器、已在制保留/全部跳过和TASK阶段。`test_early_plan_retries_original_message`验原准备明确/前序补齐/恢复启用后仍须原消息再到，当前同版新身份只确认原应用、旧版/异内容冲突，未应用不占版本，单层NULL面不破唯一性；共享收据并发保护和错误协议由ST1验，宿主只验成员/版本/意图同事务。
- [ ] **步骤2a：补真实重验接入RED。** test_plan_recheck_keeps_auto_changes_atomic通过真实共享入口验证并发重报仅一份成员/版本/SupplyChange及下一意图，成功收据不降级，外层回滚不留业务或通知。共享锁后刷新/5秒超时/错误协议和冻结兼容由ST1验，S1不重写公共保护，按原@v1恢复输入。
- [ ] **步骤3：观察RED。** `rtk uv run pytest tests/businesses/smt_auto/test_tasks.py tests/businesses/smt_auto/test_task_routing.py tests/businesses/smt_auto/test_plans.py tests/businesses/smt_auto/test_callbacks.py -q`；领取/重验并发和外层回滚另用独立PostgreSQL观察原子性失败。
- [ ] **步骤4：最小实现。** 按第2节同事务保存原输入/具体阻碍/应用引用/成员及唯一prepare。TASK取消只沿QUEUED/PREPARING，PLAN_MEMBERS精确选择原版本，全部跳过仍成功，已在制责任继续；取消不伪造取尽/急停。只以原未应用业务记录和完整当前条件判断recheck，不另存漂移的can_retry标志；不自动激活早到计划。消费ST1显式Unavailable/新错误合同及只读guard接缝，不复制receiver实现、删除收据或按HTTP推断重验。
- [ ] **步骤5：GREEN。** 重跑步骤3，用ST1/ST2共享验收证据核对公共错误/重验/默认行为；仅消费且共享源码无变更时不重复编写或执行整套公共字段/receiver测试。验证宿主保存点外回滚、合法早到503只留原待定、prepare结果按原引用/FOUND/VALID及202/PREPARE_ACCEPTED消费，共享PrepareAccepted拒绝无效响应；解析/共享接缝缺陷回原计划修复再聚焦回归。只产出合法SupplyChange，不把隔离端口当实际投箱完成。

<a id="common-ports"></a>
## Task S2：公共生产接缝与单份机械臂合同

**Files:** 根roller_ports.py、ecs_command.py，根test_scan2.py、test_arm_contracts.py；方向参数/请求由I2/O2提供。服务最终装配归S6，不在此阶段加入catalog。

**Interfaces:** 固定read_current_task/read_successor/accept_scan2及供箱变化适配，消费原Scan2Arrival/SourceFaceRef，输出原任务/方向绑定和方向首盘待办；单份COMMANDS精确覆盖已签认合法分支。接缝可在隔离宿主测试，测试替身不进入产品。

- [ ] **步骤1：写接缝RED。** `test_scan2_admission_keeps_original_arrival`覆盖有效任务可读箱无SCAN1/投箱成员仍委托WMS、无任务且无原在制走原释放决定、不可读只留到位/异常；完成确认未决保存关联/暂缓首盘，不作无任务直通。同arrival重报不改绑，新arrival独立，取消在制实际到位沿原上下文首次合法求值。
- [ ] **步骤2：写合同/供箱RED。** 验证SourceFaceRef只由五层供箱展开、原顺序和跨版范围精确，重复应用/同版确认不二次交付，单层/RETURN/TRANSFER不进供箱；CurrentTask/SuccessorContext无证据不报NONE。验证一份同角色严格联合，字段互斥、整数/有限数/字符串映射和Result约束；缺参数拒绝，不以宽松dict或占位枚举容纳未交付分支；inbound只按API9.2.1约定类型/枚举传原值，不因共用模型新增尺寸上下限、精度/步进或占用关系校验。
- [ ] **步骤3：观察RED。** `rtk uv run pytest tests/businesses/smt_auto/test_scan2.py tests/businesses/smt_auto/test_arm_contracts.py -q`；通过公开门面观察委托/原意图和外层回滚，不依赖私有函数调用次数。
- [ ] **步骤4：最小实现。** receiver原session先保存组件事实，再保存业务原arrival/方向及合法待办；不另开会话/HTTP、注册SCAN2或发MOVE_FORWARD。单份ECS模型由catalog编译，设备/位置取WORKLINE实际绑定；未获D3/D4/D5的严格分支保持未交付，不猜设备码/NG位置。具体逐盘状态及参数提取仍在方向流程，不新增共用生产引擎。
- [ ] **步骤5：GREEN。** 重跑步骤3，接收外层回滚不留下业务事实/意图，原身份/首次时间不刷新；确认两组件合同各单份、类型直接复用公共ports。真实catalog/组件链及跨方向恢复留S6，原释放登记/结果门槛留S5。

<a id="inbound-steps"></a>
## Task S3：inbound专属生产I1～I4

以下文件/测试均位于inbound/，根文件仍按第1节唯一归属；共同原子交接和差异只引用第2节，任务/接缝/释放/装配不另写一轮。

<a id="inbound-racks"></a>
### I1：左右单层来源架、取尽与独立换架

**Files:** source_flow.py、models.py、repository.py、contracts.py及初始空架callback；Test test_source_racks.py、test_source_transport.py。

**Interfaces:** 消费S1单层成员，经原Transport/唯一departure模型形成可靠原进场/在位关联和最新source_racks，供I2/I3求值；取尽只来自WMS肯定决定或初始空架通知。

来源过程关联原进场transport_task_id，保存原任务/工作线、STATION_A/B和冻结目标位置、rack_id、结果消费依据、实际位置/当前关联、取尽及原departure/搬离引用。多版本成员可指同一过程，source_racks去重列一次；不用外部rack_visit_id、位置占位或新的物理排序字段。

| 特有场景 | 实现/验收要求 |
| --- | --- |
| 候选与同架跨版 | 原版本/数组顺序及A→B选位；原在途/在位过程复用进场，不双侧调入；只撤一版不影响其他有效覆盖 |
| 原架取尽/离场后再列 | 不重置旧取尽/未知、不撤旧departure；真实再到位用新Transport，版本递增不证明新物料/到位 |
| 新左架先到、旧左架结果未回 | 新可靠事实消费后更新当前关联，旧结果只处理原过程，不清新架/重提旧事实，不要求先收旧搬离成功 |
| 实际终位不符/姿态不符 | 保存公开位置事实，不补认动作成功；可选集合仍须原成功、实际工作位及直接条件均匹配 |
| 左右独立与原授权 | 新左架不重领合法右侧后继；原已取离盘的目标/报告继续。只检查所选架/安全交出直接依赖，物理交接归ECS/RCS，不加全线门禁 |

- [ ] **步骤1：写RED。** 验上表全部场景、初始空架通知早于进场结果不伪造到位、跨版单成员取消、旧进出场重报和同架新轮不接旧授权；来源变动不提前WAIT、不重开终局箱。
- [ ] **步骤2：观察RED。** `rtk uv run pytest tests/businesses/smt_auto/inbound/test_source_racks.py tests/businesses/smt_auto/inbound/test_source_transport.py -q`。
- [ ] **步骤3：最小实现。** 单层RACK_MOVE/F01，无动态target_face，departure.current_face=null；READY只登记原搬离，WAIT/未知独立履约。事实更新/旧责任/下一意图同事务，不套CTU03接纳即释放，不新增安全轮询或库位查询。
- [ ] **步骤4：GREEN。** 重跑步骤2，验证最新集合只影响合法新求值，不改历史source/原授权；缺departure或窄位置读口时只验隔离输入，真实接入记D6未完成，不补产品占位实现。

<a id="inbound-normal"></a>
### I2：接料箱正常逐盘链、来源上下文与占用参数

**Files:** material_flow.py、contracts/models/repository及前三个putaway请求模型；Test test_material_flow.py、test_arm_contracts.py、test_correlations.py。

**Interfaces:** 消费S2原arrival、I1最新来源及已应用WMS/ECS结果，产出本段原授权/正常报告/取料终局供S5读取；原取盘到最终目标/后继的事务边界沿第2节。

每次work_plan/material.decide冻结source_racks及其原进场本地映射；READY/后继先属于该冻结范围，再复核所选同次在位，不按响应到达时同码最新架补绑。已取出的本盘source保留历史，最新source_racks只约束下一盘选择；无关另一侧集合变化不作废合法授权。

- [ ] **步骤1：写RED。** source_racks=[]也首问work_plan；ARM02单层→扫码台、ARM01扫码台→SCAN2，来源/目标参数分别取本次授权/最终决定。目标放盘前层数/总厚度原值、空格0/0；不加一/乘算深度、不推bin_id/cell_id。bin_cell_location/reel_layer严格JSON整数，尺寸/深度有限数，ECS bin_type字符串；本盘参数不借后继。
- [ ] **步骤2：观察RED。** `rtk uv run pytest tests/businesses/smt_auto/inbound/test_material_flow.py tests/businesses/smt_auto/inbound/test_arm_contracts.py tests/businesses/smt_auto/inbound/test_correlations.py -q`。
- [ ] **步骤3：最小实现。** 使用原请求/进场映射选源，原READY/后继和本地盘各消费一次，扫码从原ARM02已应用Result取。正常放盘成功才建立原报告，FAILED/NG不报，前盘明确FAILED不阻断有效后盘；后继失效不扣本盘合法PUT，字段本身矛盾不执行。WMS同源同码资格不变成WES报告ACK/容量门槛。
- [ ] **步骤4：GREEN。** 重跑步骤2，观察完整首盘→本盘决定/合法后继→正常成功报告；验证新架不替旧授权、不同真实盘不按条码合并、三类时间不混用、后继流水及本箱终局保留末盘义务。释放和整单协议只由S5交付。

<a id="inbound-exceptions"></a>
### I3：入库WAIT、NG、PICK_EMPTY与一次异常续取

**Files:** material_flow.py、contracts/models/repository及putaway_source_empty_decide_v1.py；NG参数使用S2单份命令。Test test_waits.py、test_exceptions.py。

**Interfaces:** 消费I2原盘/箱/授权及原已应用异常事实，按2.1入库分支产出后续合法求值/取盘或来源终局；不新增异常接口/外部前序号/黑名单/失败报告。

- [ ] **步骤1：写RED。** 三类WAIT的共同计时由S7验证；本任务验证work_plan/material.decide到期可更新最新source_racks及本地映射，task/箱/本盘source/条码/原扫码不变；empty_decide保持原source/observed_at且无来源数组。验证NG无报告、原NG明确成功才一次续问，SOURCE_ABSENT只闭来源、显式true才整架取尽，正常报告未收妥不统一阻塞合法异常续问。
- [ ] **步骤2：观察RED。** `rtk uv run pytest tests/businesses/smt_auto/inbound/test_waits.py tests/businesses/smt_auto/inbound/test_exceptions.py -q`，到期/补架/Beat交错补独立PostgreSQL验证。
- [ ] **步骤3：最小实现。** 仅原FAILED/0/PICK_EMPTY触发empty_decide，observed_at取原finish_time；普通失败、扫码失败、数量0单独或原厂码不能替代它。RETRY关联原最终许可建立一次新过程/命令，不改旧FAILED或重消费READY；新成功用新扫码接正常链，新明确空取才新观察，UNKNOWN/HELD不另求许可。NG用已定STATION_NG→SCAN_NG附录及原臂，实际绑定不猜值。
- [ ] **步骤4：GREEN。** 重跑步骤2；验证重复NG/空取/最终决定只恢复原阶段，阶段内WAIT不再消费触发事实，有后继/终局时不叠加work_plan。CONTINUE/CLOSE后重新由WMS给完整参数；源失效保留原授权交人工，不换授，同码新箱/新架不接旧RETRY/取尽；旧WAIT不回退、当前未决晚到WAIT不误丢。

<a id="inbound-retention"></a>
### I4：入库完成资格、7天明细与最小终态摘要

**Files:** archive.py、models/repository及material_flow/source_flow完成依据；根task_flow只接S5方向委托。Test test_completion.py、test_retention.py。

**Interfaces:** 提供本任务单层需求终局、接料作业及正常必报的原依据给S5；archive消费根任务终态/直接义务收口事实，不再实现完成确认协议或receiver扩展。

- [ ] **步骤1：写RED。** `test_archive_summary_keeps_original_task_owner`验证原任务行摘要仍提供完整TaskOwnerRef，工作线改配/业务明细清理及公共收据按原策略清理后，旧通知经共享入口仍到原inbound guard、固定STATE_CONFLICT且不重建任务；原收据尚在时身份漂移仍先冲突，不承诺恢复已删除的逐消息内容。原单层需求未取消者有WMS取尽/终局，五层需求与已受理生产独立核对；不额外列公共在途/SCAN1旁路或等单层空架全部离场。终态与直接收口分开，未决超过7天仍保留；收口满期前/后及重复消息不刷新分别验证，公开结果缺失不假成功/换号。
- [ ] **步骤2：观察RED。** `rtk uv run pytest tests/businesses/smt_auto/inbound/test_completion.py tests/businesses/smt_auto/inbound/test_retention.py -q`；此阶段用明确的原终态/结果事实输入，S5再验共同确认与方向资格的实际组合。
- [ ] **步骤3：最小实现。** 原任务行保留task_id、冻结task_type/business_type及原work_line_id/workline_code，并保存最终状态/关闭/直接收口/清理完成时间和标记；原business_code由宿主固定提供，不从当前线配置补值；终态且直接义务收口后完整7天才有界清理闭合业务明细，持续留最小终态摘要。清理前同事务重验，未决继续保留；原交互/决定/参数/消费及公共恢复所需引用不能随父任务级联丢失。不新增任务壳/拒绝表/归档调度器，不改RELIABLE_RETENTION_DAYS/TTL或全局收据清理。
- [ ] **步骤4：GREEN。** 重跑步骤2及下列真实receiver归档断言；清理/原事实消费/接收并发用独立PostgreSQL证明，不以缓存计数或空查询作收口。

<a id="inbound-archive-guard"></a>
#### 入库只读归档准入与历史收据

公共archive_guard支持已由ST1交付，I4只从原task_id/冻结方向及持久摘要提供只读拒绝/None。在共享issued/queue_changed/plan_delta/cancel及putaway.source.rack_exhausted五通知显式委托；outbound、smt_manual和旧登记默认不启用，WES出站、Transport/ECS公共原结果不接入。

只有原任务已终态且业务明细实际清理才拒绝；仅终态、满7天未清理、仍有本任务直接未决而保留明细均不据此拒绝。清理后独立公共恢复引用仍保留，不撤销已清理标记或重建业务明细。

验收固定顺序：信封/DTO及原身份内容比较→显式只读guard→原收据重放/合格原plan重验/首次受理。身份漂移先IDEMPOTENCY_CONFLICT；命中归档为409/CONFLICT/STATE_CONFLICT，timestamp复用摘要持久清理完成时间，重复拒绝稳定，不改旧成功收据或逐消息建拒绝明细。查询失败503，不回退成功重放。

清理与标记同事务；检查只用接收事务可见的已提交事实。清理先提交后接收必须拒绝，检查先于清理可沿当时事实重放历史但不授新执行。必测旧成功收据仍在时拒绝优先、未清理历史正常重放、身份漂移优先、外层回滚/清理并发和公共原结果不受阻。

<a id="outbound-steps"></a>
## Task S4：outbound专属生产O1～O4

以下文件/测试均位于outbound/，其余共同文件沿第1节。BIN不回查原供箱架/面/成员/投箱批次；RackSlot保留自己的原成员/面/到位及储位，不能因二者都用RACK_SLOT而混用来源与目标。

<a id="outbound-racks"></a>
### O1：RETURN来源架、TRANSFER目标架与目标准备

**Files:** rack_flow.py、contracts/models/repository；Test test_racks.py、test_target_preparation.py、test_transports.py。

**Interfaces:** 消费S1来源顺序及首版target_rack、O2本盘最终ACCEPT，输出原来源面到位/arrived_at、目标就绪和搬运责任；不产生机械臂取盘或公共Feed。

- [ ] **步骤1：写RED。** `test_rack_roles_keep_original_templates`验RETURN/TRANSFER进出F01、换面CK04，五层供箱仍走公共CTU01～03；`test_source_face_requires_own_applied_arrival`验原成功及实际位置/面向匹配才启动本面。`test_old_transport_result_keeps_new_process`验旧进出场只归原过程。`test_accept_target_preparation_uses_current_plate`验target_rack只准备初始面，ROTATE/REPLACE取本盘target_locator，不重新求物料资格、不指挥来源架换面；终位不符只留事实。
- [ ] **步骤2：观察RED。** `rtk uv run pytest tests/businesses/smt_auto/outbound/test_racks.py tests/businesses/smt_auto/outbound/test_target_preparation.py tests/businesses/smt_auto/outbound/test_transports.py -q`。
- [ ] **步骤3：最小实现。** 来源按架数组再面数组顺序，同任务同架面只安排一次，不预列SLOT。按原决定/阶段稳定client_request_id保存Transport/结果消费；departure用公共唯一模型，WAIT/未知独立履约，READY冻结去向。面终局由O4供给，新面仍须自己真实到位，首盘受S5的共同确认门槛约束；无猜库位/改模板兜底或CTU03规则外推。
- [ ] **步骤4：GREEN。** 重跑步骤2及受影响原Transport场景/结果测试；三类资源不混用、原直接依赖不变全线清场门槛。实际CK04支持由D7验，缺公共位置/离场交付记D6未完成。

<a id="outbound-normal"></a>
### O2：BIN/RackSlot正常链、完整取盘参数与实际报告

**Files:** material_flow.py、contracts/models/repository及五个正常请求模型；Test test_normal_flow.py、test_arm_contracts.py、test_reports.py、test_correlations.py。

**Interfaces:** 消费S2的原BIN委托、O1来源面/目标准备及WMS/ECS公开结果，输出原盘/正常报告/终局给O3/O4；公共授权和原子交接沿第2节。

- [ ] **步骤1：写RED。** `test_bin_uses_source_arm_and_rackslot_uses_target_arm`验2.1两条路径及各自decide，不跳扫码台。`test_rackslot_parameters_are_pre_pick_inputs`验首盘2/3/15.3毫米/7寸、后继4/2/20毫米/13寸；四项任缺/null/类型错误不消费，不解析slot_id、不借BIN/旧盘/目标参数。`test_target_and_report_keep_rack_slot_contract`验目标六字段→ECS FIVE_LAYER_RACK原架面/层列→实际to_locator四字段，当前盘尺寸不被后继覆盖。
- [ ] **步骤2：观察RED。** `rtk uv run pytest tests/businesses/smt_auto/outbound/test_normal_flow.py tests/businesses/smt_auto/outbound/test_arm_contracts.py tests/businesses/smt_auto/outbound/test_reports.py tests/businesses/smt_auto/outbound/test_correlations.py -q`。
- [ ] **步骤3：最小实现。** BIN按原task/arrival/箱格求值；RackSlot按当前原架面/储位及真实到位调用return_rack.work_plan。来源坐标取原授权，目标坐标取本盘ACCEPT，尺寸沿本盘原取盘；RackSlot层列转设备字符串、尺寸保留数值/单位，BIN总厚度沿既有规则。正常movement_report保留实际RACK_SLOT目标，不增目标箱码/格位/plan_revision/参数补查。
- [ ] **步骤4：GREEN。** 重跑步骤2，覆盖本盘/后继冻结、BIN解绑后原架/面/成员变化不拒绝正常链和报告、RackSlot旧到位不串新面。来源/目标字段矛盾留原事实，不删字段或猜归属；WMS同码授权/报告事务由S7和D2验证，WES假服务不证明对端已实现。

<a id="outbound-exceptions"></a>
### O3：出库WAIT、NG必报、权威空取与一次续问

**Files:** material_flow.py、contracts/models/repository及outbound_source_empty_decide_v1.py；NG使用S2单份命令。Test test_waits.py、test_ng.py、test_empty.py、test_exception_continuation.py。

**Interfaces:** 消费O2原盘/最终REJECT/真实空取，输出NG原报告、一次RETRY或SOURCE_DONE、合法异常阶段；区别沿2.1，不复制入库PICK_EMPTY/NG不报告。

- [ ] **步骤1：写RED。** 五类WAIT的共同计时由S7验证；本任务验证到期新求值的原data全部不变；`test_ng_requires_original_success_and_report_before_requery`验NG位置明确成功才报告、收妥前不建该异常续问。`test_failure_is_not_authoritative_empty`验FAILED/PICK_FAILED/0/超时/IDLE不产生empty请求。BIN空取source_locator仅type/bin_code/cell_id，无原供箱架；RackSlot按原rack/face/slot及成员版本，不向普通decide/report加版本。
- [ ] **步骤2：观察RED。** `rtk uv run pytest tests/businesses/smt_auto/outbound/test_waits.py tests/businesses/smt_auto/outbound/test_ng.py tests/businesses/smt_auto/outbound/test_empty.py tests/businesses/smt_auto/outbound/test_exception_continuation.py -q`。D4/D5缺失时测试只用明确事实替身，产品解析/NG命令及实机路径仍未交付，不登记猜测枚举/位置。
- [ ] **步骤3：最小实现。** `test_retry_consumes_permission_once`验原RETRY仅一次新命令，新尝试真实空取才新empty；`test_source_done_closes_only_original_source`验只闭Cell/SLOT、无移动不报。NG收妥或SOURCE_DONE关联原异常阶段；BIN CONTINUE可再选原Cell、CLOSE排除它，RackSlot只CLOSE原SLOT。已有授权/未决/在途不并发续问，条件后来成立仍只消费一次；正常终局后不重开。
- [ ] **步骤4：GREEN。** 重跑步骤2并在独立PostgreSQL验到期/通知竞争及消费后外层回滚。按[NG案例](../../integrations/smt-auto-api.md#outbound-exception-example-1)、[RackSlot空取案例](../../integrations/smt-auto-api.md#outbound-exception-example-2)验新的READY用自己的完整参数，保留原scanned_at/arrived_at，不加原因/前序请求号/恢复字段；NG成功测试不冒充设备实际接受。

<a id="outbound-completion"></a>
### O4：面级终局与出库完成资格

**Files:** rack_flow.py、material_flow.py、contracts/repository；Test test_face_completion.py、test_completion.py。

**Interfaces:** 从原任务/成员、O1到位和O2/O3原盘/异常/报告提供BIN、RackSlot面级收口依据给S5；不创建另一条release/completion_confirm处理链。

- [ ] **步骤1：写RED。** `test_face_terminal_closes_only_after_all_plate_obligations`验NO_WORK/正常末盘无后继两种终局都须原面授权/异常/RETRY/取放/正常NG必报全部结清；CLOSE/SOURCE_DONE不等于面结束，无额外面尾work_plan。`test_completion_uses_own_supply_and_production_progress`验供箱需求和BIN生产两组独立关联，无RackSlot计划不补面条件；单箱结束而本任务供箱未决仍不能整单完成。
- [ ] **步骤2：观察RED。** `rtk uv run pytest tests/businesses/smt_auto/outbound/test_face_completion.py tests/businesses/smt_auto/outbound/test_completion.py -q`。
- [ ] **步骤3：最小实现。** 只按原面/到位及其真实义务给出终局依据，全部收口后允许O1换面/离场；新面重新真实到位，不复用前面授权。BIN段终局及必报范围按第2节，原供箱/公共成员变化不改其生产事实；不新增入库7天清理或清理后拒绝。
- [ ] **步骤4：GREEN。** 重跑步骤2；完整验证[末盘/WAIT/原释放/确认案例](../../integrations/smt-auto-api.md#outbound-exception-example-3)的方向资格部分，原释放及整单版本交错由S5统一测试。父任务终态后独立回流/离场继续，不反向放宽正常完成前置。

<a id="common-completion"></a>
## Task S5：共同原释放与完成确认协议

**Files:** 根roller_ports.py、task_flow.py、models/repository、app/core/config.py及.env.example；Test根test_release.py、test_completion.py；方向首盘入口接入同一确认门槛，I4/O4仅提供本段事实，prepare/completion_confirm请求复用sorting_task模型。

**Interfaces:** 消费I/O原终局/授权/异常/必报、原SupplyProgress及get_scan2_release_result，产出原arrival唯一释放及原任务唯一完成确认/终态。消费原WmsRequestResult时先核对message_id/operation/operation_id、availability=FOUND、response_state=VALID及200/DECIDED，再用ST1的CompletionDecision解析；不自建响应联合。完整前置唯一沿[API完成映射](../../integrations/smt-auto-api.md#task-completion-conditions)，不复制两套确认协议。

- [ ] **步骤1：写释放RED。** `test_bin_release_requires_terminal_and_all_original_obligations`按两方向参数化：NO_WORK/正常无后继且原授权/异常/WAIT/RETRY/取放/扫码/必报结清才保存释放决定；仅入库正常必报、出库正常/NG必报。释放决定、原arrival调用release_scan2及返回command_code同事务，重复取原命令。ACK/None/命令号/失败不证明成功，不另发MOVE_FORWARD，不加箱尾work_plan或等整单/公共回架。
- [ ] **步骤2：写确认RED。** 同任务最多一条原确认，登记时保存最高连续版本、原请求/等待引用；发起及消费均重查准备/连续计划/原需求/本段/原释放。`test_no_first_plan_requires_explicit_original_deadline`验原PREPARE_ACCEPTED首次可靠保存的显式期限、无配置继续等、有早到/缺版/义务不确认0；重放/重启不刷新期限。`test_task_response_contract_requires_valid_original_result`覆盖非FOUND/VALID、错误HTTP/code、分支冲突/null、STALE不大于原请求等无效输入不消费/关单/解除暂缓，真正未知备注可忽略；字段联合单测归ST1，宿主只验状态不误推进。
- [ ] **步骤3：写交错RED并运行。** `test_confirmation_preserves_held_arrivals_and_new_plans`验确认未决仍连续应用/交供箱，新生产到位存原关联但不首问/误直通；原授权/报告/公共义务继续。旧COMPLETED与新计划/义务矛盾留证交WMS、不换号绕过；STALE严格比较原请求版本，已补齐不重复等；BUSINESS_IN_PROGRESS允许相关新事实或到期，区别逐盘WAIT。运行 `rtk uv run pytest tests/businesses/smt_auto/test_release.py tests/businesses/smt_auto/test_completion.py -q`。
- [ ] **步骤4：最小实现。** 共同请求/结果消费在根task_flow，按原方向查询I4/O4资格；三互斥响应沿API严格联合。COMPLETED仍适用才同事务落终态/释放在办约束；有效非终局消费后才恢复暂存首盘或按条件发新确认，旧已消费响应不回退。未知/503恢复原请求，不新建轮询器/确认表；WMS封口计划的事务责任由D2落实。SMT_AUTO_FIRST_PLAN_WAIT_MS为可选正整数毫秒、默认None，由构造参数注入；缺失/无有效显式配置不允许确认0，不借设备超时。保存原prepare首次可靠时间及已形成期限所需配置依据，已登记期限不因重放/重启/配置变动刷新；不新增wire字段或配置/期限表，配置示例不覆盖用户.env。
- [ ] **步骤5：GREEN。** 重跑步骤3，独立PostgreSQL验并发确认和外层回滚；缺引用/空SupplyProgress/pending=false不补成功，不强制逐箱映射供箱成员，不等公共在途/旁路/FIFO/drain/全部架离场。正常完成前必报/公开原释放必须收口，任务终态后仍保留独立离场/回流，不借继续履约绕过本次完成门槛。

<a id="common-runtime"></a>
## Task S6：唯一正式装配、迁移、待办与恢复

**Files:** 根definition/composition/service及显式ENTRY_MODULES/MODEL_MODULES/COMMANDS；catalog.py、一份smt_auto_state迁移及迁移测试。Test根test_assembly.py、test_direction_handoff.py、test_pending.py、test_recovery.py、test_retention_boundaries.py。

**Interfaces:** 完成1.1固定服务接口、一个业务/工厂/两组件映射和一份业务状态迁移；consume S1～S5完整分支及D1～D7实际交付，B7组件迁移/首宿主安排沿原计划，不因自动包统一而改动。

- [ ] **步骤1：写装配/切换RED。** `test_first_plan_wait_configuration_is_injected_and_preserved`验证None继续PREPARING、有效T从原准备可靠时间起算、重启不刷新已登记期限。 `test_shared_inputs_and_commands_register_once`验两方向加隔离smt_manual消费者仍同源任务单份、四点唯一、一份严格命令联合、非消费者无权限；`test_direction_resources_are_checked_when_used`验证只做单方向的线无需另一方向专属绑定，本方向缺必需配置仍按原门禁阻止；`test_old_inbound_result_keeps_original_task_after_outbound_starts`验旧报告/释放/架结果仍归原任务，同码新arrival不串方向；`test_inbound_archive_policy_does_not_apply_to_outbound`验五通知仅原入库guard，出库不清理/拒绝，公共原Result仍可收口。
- [ ] **步骤2：写恢复RED并运行。** `test_waiting_direction_does_not_starve_other_pending`覆盖本轮上界/游标、等待/失败也翻页、单条异常不阻塞其他类、新工作下一轮，终态仍发现生产和组件六类原责任。`test_original_owner_survives_rollback_and_restart`覆盖三种原owner外层提交后通知、保存点后外层回滚不通知、丢通知Beat恢复、提交不明重读原身份、不凭暂时查空换号。`test_result_consumption_and_next_intent_commit_together`验消费/原意图/结果版本同提交、回滚后无半个授权；`test_exception_trigger_survives_rollback_without_duplicate`按入库NG/SOURCE_ABSENT、出库NG收妥/SOURCE_DONE参数化，触发消费与原阶段/下一意图同回滚，提交后的重复不二次续问；`test_roller_isolation_does_not_leak_to_receiver`在真实PostgreSQL验覆盖表首SQL前隔离、连接归还、原锁序、含commit的40001新会话重试最多3次及超限保留pending、两线独立。`test_applied_results_wake_original_auto_owner`区分WMS决定可靠保存、ECS实际应用、Transport提交有效接纳和applied outcome_revision推进，仅外层提交后唤醒原smt_auto，旧终态责任也恢复；旧租约/无新应用不误唤醒；共享结果缺原归属不猜测/广播或认领历史null，原独占注册来源的既有回退保持。`test_two_worklines_keep_original_scope`在test_direction_handoff.py验两线同箱码/不同方向交错、回滚和旧回调不串task/arrival。`test_auto_host_preserves_scan3_round_and_scan4_pending`通过真实host receiver/runtime验新SCAN3不等旧轮/上游Result、原义务保留、原SCAN4成功资格/失败重扫不改FIFO，以及未分配SCAN4能从原SCAN待办推进求架；只验证宿主接入既有公开行为，不复制组件算法。运行 `rtk uv run pytest tests/businesses/smt_auto/test_assembly.py tests/businesses/smt_auto/test_direction_handoff.py tests/businesses/smt_auto/test_pending.py tests/businesses/smt_auto/test_recovery.py tests/businesses/smt_auto/test_retention_boundaries.py -q`。
- [ ] **步骤3：最小装配。** 复用现有catalog._providers/模型加载/runtime，API/worker同一完整声明；COMPONENTS=(sorting_task.COMPONENT, bin_roller.COMPONENT)，PROGRESSION="scan_pending"，COMMANDS只来自根ecs_command。组件production指向宿主，工厂注入SMT_AUTO_FIRST_PLAN_WAIT_MS；业务只操作原公开门面；read方法用调用方session，组件推进用宿主事务；六类组件待办使用其get_pending_upper_id/list_pending公开分页，包含尚未生成DRAIN/RACK_CYCLE的SCAN4，不只扫已建协调记录。无第二通知路由/扫描器/事务runner。未交付合同/组件不以空工厂或local专用生产登记补缺。
- [ ] **步骤4：迁移及数据库验证。** 一份迁移仅根/两方向必要表/唯一约束/归档标记，MODEL_MODULES显式加载，公共模型只一次；组件表归B7，position_observation已由f0b220261031交付不重复添加。实施时接唯一最新head，独立库验证升级/既有数据保留及 `rtk uv run alembic heads` 一个head；运行 `rtk uv run pytest tests/migrations/test_smt_auto_state.py -q`。不重算历史、导入别项目任务或复制配置。
- [ ] **步骤5：GREEN与真实事务。** 重跑步骤2，在独立PostgreSQL按上述受控交错验证真实receiver→组件→生产→原释放/公开结果、原owner通知→同一pending及SupplyChange精确范围；保存点/锁序/隔离/40001/提交未知均须原始证据，Mock/SQLite不算实际组合通过。

<a id="common-acceptance"></a>
## Task S7：合同回归、统一矩阵与分层证据

**Files:** 根test_application.py、test_history_contracts.py、test_waits.py及tests/tasks/test_smt_auto_worker.py；方向既有测试仅补缺失差异；docs/testing/smt-auto.md。不再各跑一次综合回归或各建交付模板。

**Interfaces:** 消费S1～S6、下文统一矩阵和D1～D8；产出每项可追溯的实现/测试/实际联调状态，严格区分隔离、正式接入和物理验收。

- [ ] **步骤1：写综合RED。** `test_wait_deadline_survives_wakeup_and_restart`按入库三类/出库五类决定参数化共同计时：首次有效WAIT固定到期、补架/报告/新扫码/通知不提前或刷新，原未知/503恢复原身份，当前未决晚到WAIT消费、旧WAIT不回退。 `test_bin_ng_to_original_release_and_completion`、`test_rackslot_empty_to_other_slot_and_face_completion`及入库正常/NG/PICK_EMPTY流程在同一应用装配下验各自正常收口；`test_completion_conflict_preserves_new_work`验晚到计划/新首盘暂缓；`test_frozen_history_is_not_rebuilt_by_new_contract`验旧operation/身份/正文/期限/路由/附录及重复结果原义务。`test_original_result_drives_own_decision`、`test_report_recovery_never_repeats_put`、`test_wms_authorized_later_plate_does_not_wait_for_prior_ack`按实际方向参数化，覆盖Result先于ACK、缺码/矛盾不补造、三类时间和WMS同码裁决，不各建同形回归。
- [ ] **步骤2：观察RED并补实际缺口。** `rtk uv run pytest tests/businesses/smt_auto/test_application.py tests/businesses/smt_auto/test_history_contracts.py tests/businesses/smt_auto/test_waits.py -q`；只有新发现行为缺口才先RED再最小修复，已有行为直接保留GREEN，不为制造RED改既有结果。不以截断/改名样例伪造历史迁移。
- [ ] **步骤2a：编写并执行非eager真实worker验收。** `rtk uv run pytest tests/tasks/test_smt_auto_worker.py -q`使用显式TEST_DATABASE_URL/TEST_REDIS_URL的独占测试资源，关闭eager，以真实broker/worker执行原business.advance_pending及同一Beat入口，只连测试WMS/ECS/RCS替身。test_auto_worker_recovers_wait_completion_and_roller_pending覆盖WAIT到期、完成确认响应、两方向/组件混合待办、通知丢失/重复、进程停止重建后原请求/命令/Transport身份不变；无配置跳过记未验，不用直接service调用/eager代替。此证据不要求或证明真实设备发送，现场另按步骤4验。
- [ ] **步骤3：统一GREEN。** 运行步骤2、全部smt_auto及受影响共享组件/receiver/Transport/ECS/原owner/任务/粗分回归，再一次适当全量 `rtk uv run pytest -q`，以及 `rtk uv run ruff check .`、`rtk uv run ruff format --check .`、`rtk git diff --check`。未配置隔离库的跳过不算数据库验收；只在新失败/改动需要时重跑，不用自动修复脚本扩大差异。
- [ ] **步骤4：记录与外部验收。** 按矩阵编号、方向、实际命令/结果、隔离环境、合同/附录/绑定版本、原身份/Result证据逐项记录；共同项按适用方向参数化验证，方向未验不能借共同通过勾选。独立worker证据按步骤2a记录；D2～D8及授权具备后再验真实WMS/ECS/RCS/物理动作、NG/空取/重启/错误及关单交错。同码唯一授权/库存事务须WMS证据，空取/NG须设备证据；ACK、本地Mock、启动或勾计划不代替现场通过。

<a id="acceptance-matrix"></a>
## 3. 统一验收追踪：共同项与方向差异

编号对应[API §10.2唯一验收表](../../integrations/smt-auto-api.md#acceptance)，保留A01～A55、B01～B06全部61项，并显式纳入I01～I07入库差异及C01～C04共同接入边界，共72项。表中只定位任务/测试，不再抄写API预期结果；“共同”按原方向合同参数化，尤其不能跨用NG、WAIT、参数或归档政策。

测试路径：`根/`为tests/businesses/smt_auto/，`入/`为其inbound/，`出/`为其outbound/；`共享/`为tests/businesses/sorting_task/，`接收/`为tests/integrations/wms_callback/。每行对应行为用例或参数化分支，不为每行新增测试文件。测试定位以此表和各任务Files为准，不沿旧专项目录再建同名共同测试。

| 编号 | 范围 | 验证焦点 | 实施任务 | 验证文件 |
| --- | --- | --- | --- | --- |
| A01 | 共同（按方向合同） | 新增AUTO任务使用统一任务族 | [S1](#common-tasks) | 根/test_tasks.py |
| A02 | outbound | BIN 正常取放：SCAN2 滚筒线料箱 → 扫码平台 → 转运货架储位 | [O2](#outbound-normal) | 出/test_normal_flow.py |
| A03 | outbound | RackSlot 正常取放：退料货架 → 扫码平台 → 转运货架储位 | [O2](#outbound-normal) | 出/test_normal_flow.py |
| A04 | outbound | BIN/RackSlot 取得有效后继，或原取盘 Result 迟到/重复 | [S7](#common-acceptance) / [O2](#outbound-normal) | 根/test_application.py；出/test_normal_flow.py |
| A05 | outbound | 首版 target_rack、当前本盘 ACCEPT 和实际正常报告分别核对 | [O2](#outbound-normal) | 出/test_arm_contracts.py |
| A06 | outbound | 两类来源正常目标与现有合同一致，或误将来源箱参数用于目标 | [O2](#outbound-normal) | 出/test_arm_contracts.py |
| A07 | outbound | 对照原始供应商 PDF 核对正常目标路径及字段 | [O2](#outbound-normal) / [S7](#common-acceptance) | 出/test_arm_contracts.py；验收记录 |
| A08 | 共同（按方向合同） | issued字段缺失/null/枚举/类型错误 | [ST1](2026-10-31-sorting-task-implementation.md#sorting-task-st1) / [ST2](2026-10-31-sorting-task-implementation.md#sorting-task-st2) | 共享/test_contracts.py；共享/test_registration.py |
| A09 | 共同（按方向合同） | 合法AUTO/inbound在支持的smt_auto工作线进入上架，不进入拣料；或队列更新试图改变原方向 | [S1](#common-tasks) | 根/test_tasks.py |
| A10 | 共同（按方向合同） | 已冻结旧 outbound.picking_task.* 请求/旧 AUTO 正文尚未收妥，文档已切换新任务合同 | [S7](#common-acceptance) | 根/test_history_contracts.py |
| A11 | 共同（按方向合同） | 同线 QUEUED 任务优先序相同，或队列更新与领取任务并发 | [S1](#common-tasks) | 根/test_tasks.py |
| A12 | 共同（按方向合同） | queue_changed 已成功后任务进入 PREPARING，再重放原消息；或原确定拒绝后任务条件改变 | [S1](#common-tasks) | 根/test_callbacks.py |
| A13 | 共同（按方向合同） | prepare 响应丢失，首版计划先到，随后原准备响应可靠保存 | [S1](#common-tasks) | 根/test_callbacks.py |
| A14 | 共同（按方向合同） | 本地尚无首版计划且首批等待期限未配置，或存在早到/缺版待处理计划 | [S5](#common-completion) | 根/test_completion.py |
| A15 | 共同（按方向合同） | 完成确认尚未收妥时新计划到达，随后收到旧版本 COMPLETED | [S5](#common-completion) | 根/test_completion.py |
| A16 | outbound | 原 NG 已放置成功但报告未收妥；或只收到 SOURCE_DONE/REJECT.CLOSE，原箱/面没有取料终局 | [O3](#outbound-exceptions) / [O4](#outbound-completion) / [S5](#common-completion) | 出/test_ng.py；根/test_release.py |
| A17 | outbound | 本箱已有有效 NO_WORK，但其他正常/NG 成功报告尚未确认，或存在待消费授权/异常阶段/RETRY/未知取放 | [S5](#common-completion) | 根/test_release.py |
| A18 | outbound | 普通盘报告已收妥，但正常 ACCEPT 带有后继，或原异常续问仍 WAIT | [S5](#common-completion) | 根/test_release.py |
| A19 | outbound | NG/SOURCE_DONE 事实重复到达时，原箱/面已有正常 ACCEPT 无后继或 NO_WORK，或已有另一有效授权/取料请求 | [O3](#outbound-exceptions) | 出/test_exception_continuation.py |
| A20 | 共同（按方向合同） | NO_WORK 与全部报告/授权/物理义务已结清，释放方法返回原命令号且 ECS 已 ACK，但原结果仍未决/失败 | [S5](#common-completion) | 根/test_release.py |
| A21 | 共同（按方向合同） | 原释放公开成功结果已应用，任务自身供箱/方向来源/报告等前置及当前版本均符合 | [S5](#common-completion) | 根/test_completion.py |
| A22 | 共同（按方向合同） | completion_confirm 未决期间，新计划及新的生产对象实际到位先到；原设备和公共义务仍在推进 | [S5](#common-completion) | 根/test_completion.py |
| A23 | 共同（按方向合同） | 原完成确认未知/503后重启；或有效 STALE/BUSINESS_IN_PROGRESS 已消费 | [S5](#common-completion) | 根/test_completion.py |
| A24 | outbound | BIN NG 成功但原 NG 报告未收妥；报告收妥后 CONTINUE | [O3](#outbound-exceptions) | 出/test_ng.py |
| A25 | outbound | BIN CLOSE 或 RackSlot REJECT/CLOSE 后，新 work_plan 仍返回已关闭 Cell/SLOT | [O3](#outbound-exceptions) | 出/test_ng.py；出/test_exception_continuation.py |
| A26 | outbound | RackSlot NG 报告收妥，或原 SLOT 的 SOURCE_DONE 已应用后取得同面其他 SLOT 的 READY | [O3](#outbound-exceptions) | 出/test_exception_continuation.py |
| A27 | outbound | 普通 FAILED/PICK_FAILED/actual_qty=0、超时或位置未知 | [O3](#outbound-exceptions) | 出/test_empty.py |
| A28 | outbound | 料箱已与货架解绑，原取盘明确空取，且没有原供箱架号/面或其记录已变化 | [O3](#outbound-exceptions) | 出/test_empty.py |
| A29 | outbound | BIN 已在制，原供箱架换面/离场、原成员撤销或投箱批次归档 | [O2](#outbound-normal) / [O3](#outbound-exceptions) | 出/test_normal_flow.py；出/test_exception_continuation.py |
| A30 | outbound | BIN 正常/NG 报告恢复时，原供箱关联缺失或已变化，但实际目标是 TRANSFER 架或 NG 区域 | [O2](#outbound-normal) / [O3](#outbound-exceptions) | 出/test_reports.py；出/test_ng.py |
| A31 | outbound | BIN 作业与报告已结清、原释放已完成，且本任务供箱等其他独立需求亦有结束依据，但无法将某箱映射到原供箱成员 | [O4](#outbound-completion) / [S5](#common-completion) | 出/test_completion.py；根/test_completion.py |
| A32 | outbound | 单个 BIN 已完成，但本任务自己的供箱计划成员仍有未决责任 | [O4](#outbound-completion) / [S5](#common-completion) | 出/test_completion.py；根/test_completion.py |
| A33 | outbound | 原 empty_decide 返回 RETRY，决定被重复消费、通知丢失或登记新命令后重启 | [O3](#outbound-exceptions) | 出/test_empty.py |
| A34 | outbound | 原 SOURCE_DONE 重报，或其后同面仍有其他合法需求 | [O3](#outbound-exceptions) | 出/test_empty.py |
| A35 | outbound | NG 成功/报告确认或 SOURCE_DONE 消费时外层事务回滚 | [O3](#outbound-exceptions) / [S6](#common-runtime) | 出/test_exception_continuation.py；根/test_recovery.py |
| A36 | outbound | work_plan 已 NO_WORK，或正常 ACCEPT 已明确无后继后，再收到旧异常确认/补架计划 | [O3](#outbound-exceptions) | 出/test_exception_continuation.py |
| A37 | 共同（按方向合同） | 本地段已在制，上游取消后原箱/架才到位，且此前尚未登记该阶段的 WMS 请求 | [S1](#common-tasks) / [S2](#common-ports) / [S6](#common-runtime) | 根/test_plans.py；根/test_scan2.py；根/test_recovery.py |
| A38 | 共同（按方向合同） | 有效任务且可读箱缺 SCAN1/投箱成员；可读无任务无原在制；不可读箱 | [S2](#common-ports) | 根/test_scan2.py |
| A39 | 共同（按方向合同） | 所属work_plan、material.decide或empty_decide 返回 WAIT，期间有报告确认、新扫码/唤醒或重启 | [S7](#common-acceptance) / [I3](#inbound-exceptions) / [O3](#outbound-exceptions) | 根/test_waits.py；入/test_waits.py；出/test_waits.py |
| A40 | 共同（按方向合同） | 所属work_plan/material.decide 响应未知或 503，随后原结果可靠到达 | [S7](#common-acceptance) | 根/test_waits.py |
| A41 | outbound | 首版仅有目标架，后续版本分别或同时追加两类来源 | [S1](#common-tasks) | 根/test_plans.py |
| A42 | 共同（按方向合同） | 供箱同架同面跨版本追加，随后精确撤销其中一个版本 | [S1](#common-tasks) / [S6](#common-runtime) | 根/test_plans.py；根/test_assembly.py |
| A43 | outbound | RackSlot 整面撤销、重复撤销或携带 slot_ids | [S1](#common-tasks) | 根/test_plans.py |
| A44 | 共同（按方向合同） | WMS 在执行中因不同业务原因追加合法来源，原动作尚未收口 | [S1](#common-tasks) | 根/test_plans.py |
| A45 | outbound | 计划同时涉及料箱供箱、RackSlot 来源架和物料目标架准备 | [O1](#outbound-racks) / [S6](#common-runtime) | 出/test_racks.py；根/test_assembly.py |
| A46 | outbound | 三类货架进场/换面/离场及同角色恢复 | [O1](#outbound-racks) | 出/test_transports.py |
| A47 | outbound | RackSlot 首盘 READY 含来源层2/列3、厚度15.3毫米、直径7寸，slot_id=A-03；本盘目标为另一层/列 | [O2](#outbound-normal) | 出/test_arm_contracts.py |
| A48 | outbound | RackSlot 正常末盘 ACCEPT 明确无后继，但另一原盘仍有 NG/空取前序事实、WAIT/RETRY、未知动作或未确认报告 | [O4](#outbound-completion) | 出/test_face_completion.py |
| A49 | outbound | 后继 next_source_action 提供层4/列2、厚度20毫米、直径13寸，与当前盘参数不同 | [O2](#outbound-normal) | 出/test_arm_contracts.py |
| A50 | outbound | 首盘 READY 或后继对象中，四项参数任一缺失、null、类型错误；或完整参数仅存在于旧盘/BIN/目标对象 | [O2](#outbound-normal) | 出/test_arm_contracts.py |
| A51 | outbound | WMS 返回 NO_WORK/WAIT，或本盘最终 ACCEPT 明确省略 next_source_action | [O3](#outbound-exceptions) / [O4](#outbound-completion) | 出/test_waits.py；出/test_face_completion.py |
| A52 | 共同（按方向合同） | 首盘/后继授权响应丢失、原请求重放或命令登记后重启，WMS 后续另有新授权参数 | [S7](#common-acceptance) / [S6](#common-runtime) | 根/test_application.py；根/test_recovery.py |
| A53 | 共同（按方向合同） | 来源授权消费、命令登记后外层事务回滚，或提交后重复推进 | [S7](#common-acceptance) / [S6](#common-runtime) | 根/test_application.py；根/test_recovery.py |
| A54 | 共同（按方向合同） | 生产委托/原释放登记后外层回滚；提交后重复处理或通知丢失 | [S6](#common-runtime) | 根/test_recovery.py |
| A55 | 共同（按方向合同） | 正常任务已完成后原释放结果重复/迟到，或仍有独立公共回流/离场责任，随后新任务到来 | [S6](#common-runtime) | 根/test_pending.py；根/test_recovery.py |
| B01 | 共同（按方向合同） | A 盘报告未被 WMS 收妥，同源同码 B 盘；或 A 已入账但响应丢失 | [S7](#common-acceptance) | 根/test_application.py；D2/D8对端证据 |
| B02 | 共同（按方向合同） | 不同业务键的后继盘，前盘报告仍在恢复 | [S7](#common-acceptance) | 根/test_application.py |
| B03 | 共同（按方向合同） | A 原报告已收妥，后续盘已获授权，A 的延迟副本再到达 | [S7](#common-acceptance) | 根/test_application.py |
| B04 | 共同（按方向合同） | 新报告无匹配授权、多份匹配授权或目标冲突 | [S7](#common-acceptance) | 根/test_application.py；D2/D8对端证据 |
| B05 | 共同（按方向合同） | WMS 报告事务回滚，或已提交但响应丢失；WES 消费确认时崩溃 | [S7](#common-acceptance) / [S6](#common-runtime) | 根/test_application.py；根/test_recovery.py；D2/D8对端证据 |
| B06 | 共同（按方向合同） | 自动线请求货架离场 | [I1](#inbound-racks) / [O1](#outbound-racks) | 入/test_source_transport.py；出/test_transports.py |
| I01 | inbound | source_racks=[]及WMS累计等待 | [I2](#inbound-normal) / [I3](#inbound-exceptions) | 入/test_material_flow.py；入/test_waits.py |
| I02 | inbound | 左右独立换架及原授权延续 | [I1](#inbound-racks) / [I2](#inbound-normal) | 入/test_source_racks.py；入/test_material_flow.py |
| I03 | inbound | 单层取盘/SCAN2放盘与占用参数 | [I2](#inbound-normal) | 入/test_arm_contracts.py |
| I04 | inbound | 正常成功、NG和FAILED报告范围 | [I2](#inbound-normal) / [I3](#inbound-exceptions) | 入/test_material_flow.py；入/test_exceptions.py |
| I05 | inbound | 明确空取与一次RETRY | [I3](#inbound-exceptions) | 入/test_exceptions.py |
| I06 | inbound | 接料箱/原需求与整单完成 | [I4](#inbound-retention) / [S5](#common-completion) | 入/test_completion.py；根/test_completion.py |
| I07 | inbound | 7天保留、只读guard与方向隔离 | [I4](#inbound-retention) / [S6](#common-runtime) | 入/test_retention.py；根/test_retention_boundaries.py |
| C01 | 共同（按方向合同） | 工作线准入、停用/恢复及原责任延续 | [S1](#common-tasks) / [S6](#common-runtime) | 根/test_tasks.py；根/test_plans.py；根/test_recovery.py |
| C02 | 共同（按方向合同） | 原结果可用性、精确HTTP/code及共享响应联合 | [ST1](2026-10-31-sorting-task-implementation.md#sorting-task-st1) / [S1](#common-tasks) / [S5](#common-completion) | 共享/test_contracts.py；根/test_callbacks.py；根/test_completion.py |
| C03 | 共同（按方向合同） | 显式503留存、只读查询暂不可用及原身份恢复 | [ST1](2026-10-31-sorting-task-implementation.md#sorting-task-st1) / [ST2](2026-10-31-sorting-task-implementation.md#sorting-task-st2) / [I4](#inbound-retention) | 接收/test_task_contract_extensions.py；共享/test_dispatch.py；入/test_retention.py |
| C04 | 共同（按方向合同） | 只配置本方向资源及缺必需绑定 | [S1](#common-tasks) / [S2](#common-ports) / [S6](#common-runtime) | 根/test_tasks.py；根/test_assembly.py |

公共四点/FIFO/drain/投回箱/五层架/CTU03内部验收引用[公共验收](../../integrations/workline-bin-return-api.md#return-acceptance)和B1～B7。S2/S6只验宿主原需求、arrival、原释放、公开进度及真实组合，不复制公共测试工程。Review Focus五项分别由S1/S2/I/O/S5/S6命名用例覆盖；跨方向/两线、通知丢失及事务恢复证据并入S6/S7。

<a id="integration-acceptance"></a>
### 3.1 G01～G04：宿主运行接入补充验收

这些是实现/装配证据，不新增API字段或业务分支，也不复制共享/组件内部测试；与72项API追踪分别记证。

| 编号 | 遗漏边界及必须观察的行为 | 唯一任务 / 测试 |
| --- | --- | --- |
| G01 | WMS原决定/ECS已应用结果/Transport提交接纳与实际outcome更新只在外层提交后唤醒原宿主；回滚/旧租约不误通知，共享缺归属不猜测、原独占来源回退保持；两线同箱码及旧回调不串原关联 | S6；根/test_recovery.py、test_direction_handoff.py |
| G02 | 非eager真实broker/worker、同一Beat、通知丢失/重复及进程重建后能恢复WAIT/完成/生产和组件待办，保留所有原身份；只用独占测试资源/对端替身 | S7步骤2a；tests/tasks/test_smt_auto_worker.py |
| G03 | 六类公开pending含未形成DRAIN/RACK_CYCLE的SCAN4；宿主实际接入保持新SCAN3轮次和旧义务、原SCAN4资格/FIFO，不自建或遗漏公共分支 | S6；根/test_pending.py、test_recovery.py |
| G04 | 首批等待配置可选正整数/默认None，工厂实际注入；原prepare可靠时间和已登记期限不随重放/重启/配置变化刷新，无有效配置或待处理计划不得确认0 | S5/S6；根/test_completion.py、test_assembly.py |

ST1原收据锁/刷新/5秒等待、历史@v1冻结及ST2正式callback登记只按[共享计划](2026-10-31-sorting-task-implementation.md#sorting-task-ownership)验收；自动宿主消费其实际交付证据，不再设计并发收据机制或复制一套公共用例。

<a id="delivery-dependencies"></a>
## 4. D1～D8共同交付依赖与方向要求

责任按职责，不猜人员。编号沿既有计划，交付检查只维护本表；合同细节见[API共同交付表](../../integrations/smt-auto-api.md#delivery-dependencies)。规则已经确认，缺交付不重开业务决定。

| 编号 / 责任方 | 一次交付的产物与门槛 | inbound额外要求 | outbound额外要求 | 影响任务 |
| --- | --- | --- | --- | --- |
| D1 平台集成/smt_auto/共享任务实施方 | ST1/ST2唯一六操作/可信归属、新错误/业务503/recheck/可选guard，旧接口/历史收据兼容；早到原503→202→200真实受理 | I4五通知只读guard/固定拒绝时间，不能扩到公共结果 | 不启用入库清理/拒绝；严格资源联合不丢三类字段 | S1/S2/I4/S6 |
| D2 WMS接口及库存负责人 | 已签认任务/资源/逐盘/完整授权/报告/完成合同及版本；同键唯一授权、报告先去重/库存入账原子、计划封口和关单交错实测 | 单层来源/放盘前占用、空集合等待、NG不报及SOURCE_ABSENT | BIN裁剪空取DTO、RackSlot完整参数、NG收妥续问、SOURCE_DONE；同码WAIT/旧报不关后盘 | S1/I/O/S5；正式注册门槛与S7真实WMS证据，不阻塞隔离测试 |
| D3 ECS供应商/设备集成方 | 两臂严格附录、原command_code/device_code/Result、完整barcode/scanned_at、ACK/Result先后/重报；实际路径/单位/成功数量一致 | ARM02单层取、ARM01入BIN，目标占用原值 | BIN/RackSlot路径、ARM02正常FIVE_LAYER_RACK精确目标及本盘原尺寸 | S2/I2/O2/S6/S7 |
| D4 ECS供应商/现场设备负责人 | 原厂code/msg、扫码/取放失败、未知/重启补报和实物恢复说明；权威无料事实唯一关联原取盘及观察时间 | 已定FAILED/0/PICK_EMPTY及finish_time、一次RETRY；不得把普通失败泛化为空取 | 权威载体仍待签认，未交付不猜PICK_EMPTY/错误码；一次RETRY/SOURCE_DONE无报告 | I3/O3；对应产品解析/实机分支未交付即保持未完成 |
| D5 ECS/现场负责人，WMS给NG定义 | 原NG区域到实际设备/位置/完整参数、成功失败说明和实际证据；不直接把NG_ZONE当ECS location_type | 已定STATION_NG/SCAN_NG及原臂，NG不向WMS报告 | NG设备映射未交付不得猜ARM01/02/位置；成功且位置确定报告，收妥才合法续问 | S2/I3/O3/S6/S7 |
| D6 公共组件/平台集成方 | B1～B7组件/ports/单份departure、窄位置、供箱进度/释放原结果、原owner通知和组件迁移；真实receiver/供箱/释放/公平pending可追溯 | 原单层架离场与可靠位置消费，不复制公共架流 | RETURN/TRANSFER原直接架过程，不将其当Feed/drain | S2/I1/O1/S6；B7首宿主既有人工优先安排保持，不要求人工完整生产作为隔离测试前置 |
| D7 现场部署/设备集成/WMS/RCS | 实际工作线设备/位置/角色/模板/拓扑及超时、首批等待显式配置，无示例默认；30秒设备示例不代替业务期限 | STATION_A/B单层F01、两臂/SCAN2/NG实际绑定 | RETURN/TRANSFER的F01/CK04/F01，供箱CTU；真实面向/CK04对端支持，CTU03接纳规则不扩F01 | I1/O1/S6实配、S7真实搬运 |
| D8 smt_auto实施/测试及WMS/ECS/RCS/现场验收方 | 一份完整业务/目录/迁移、冻结历史兼容及统一矩阵证据；单head/保留既有数据；隔离、worker、真实对端、部署、物理分层记录 | I1～I4全部差异及归档/原公共恢复边界证据 | O1～O4全部差异、正常/NG/空取/面收口证据 | S6/S7，未交付/未验列示，不以ACK或单项Mock宣称可启用 |

依赖不齐仍可做不依赖它的隔离步骤，对应严格分支/正式接入/物理验收保持未完成；不加默认值、补参查询、假组件、启用开关、维护端点或自动重建动作消缺。完整smt_auto正式接入须满足两方向适用合同及共同装配；本计划不改变B7首宿主安排或顺带实施人工业务，B6独立维护TODO不加为正常接入门槛。

<a id="previous-task-map"></a>
## 5. 原任务归并位置、顺序与自查

本表仅供既有引用迁移，不保留另一套执行步骤；共同任务只有上文一份。

| 原计划位置 | 现唯一交付位置 |
| --- | --- |
| 原S1、上架任务1、拣料AP1的共同任务/receiver要求 | ST1/ST2已随PR #44交付共享能力；S1交根生命周期及实际版本/成员/状态校验，不重建共享字段校验 |
| 上架任务2、拣料AP2 | I1、O1各自的生产架差异；公共搬架/离场能力沿B计划 |
| 上架任务3、拣料AP3 | S2交公共接缝/命令合同；I2、O2交专属正常生产，S7交共同原关联回归 |
| 上架任务4、拣料AP4 | 第2节一份共同授权/WAIT约束；I3、O3交异常差异 |
| 上架任务5、拣料AP5 | S5交唯一释放/确认协议；I4、O4给方向资格，只有I4交入库保留/guard |
| 上架任务6、拣料AP6及原S4 | S6交唯一catalog/工厂/pending/事务/迁移/恢复 |
| 拣料AP7及两方向综合回归/交付记录 | S7、一张验收矩阵、一份D1～D8和docs/testing/smt-auto.md |

实施顺序：ST1（完成）→ST2（完成）→S1→S2→I1～I4→O1～O4→S5→S6→S7。I/O的完成资格可用原事实输入独立测试，S5再验真实共同协议组合；S6以实际交付依赖为正式装配门槛。共享文件按此顺序修改，不安排两个方向并行重建共同能力，不增加新的规划或通用引擎阶段。

设计§2/§6→S1/S2/S6声明/文件/注册；§3→S1共同任务/严格计划；§4→S2/I/O/S6原关联；§5→2.1/I/O/S5方向合同/完成/保留；§7→S6/S7事务/恢复/证据。API§3字段/响应合同→ST1/ST2，宿主任务/确认→S1/S5，§4～7→S2/I/O/S7，§8→S2/S5/S6，§9→S1/I4/S7，§10→统一矩阵及D1～D8。所有72项API编号及G01～G04运行接入均有任务/测试定位，原细节以API正文为准。

共享ST1/ST2已合入状态、P2闭合、24项专项和4654/52全仓及CI证据统一见[共享计划§11](2026-10-31-sorting-task-implementation.md#sorting-task-merged)。本计划S1～S7及I/O复选框仍为未来事项，真实宿主/正式catalog和迁移、SMT worker/Beat恢复、完整WES BAD/GOOD CASE及对端/现场未验收。本轮只同步文档和检查引用，不运行新的产品测试、迁移、服务或真实请求，不修改产品代码/暂存内容，不提交。
