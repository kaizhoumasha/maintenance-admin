# SMT 手工分拣、入库业务子包设计

设计基线：2026-10-31；交付状态核对：2026-11-01。**smt_manual宿主未实施、未注册、未部署**；共享sorting_task ST1/ST2已随[PR #44](https://github.com/kaizhoumasha/maintenance-admin/pull/44)合入develop（380aeba），见[已合入状态与验收](../plans/2026-10-31-sorting-task-implementation.md#sorting-task-merged)。本稿及关联 API 已同步人工两方向与 smt_auto 统一业务划分；本轮只改文档，不实施代码、迁移、配置、提交或真实请求。文档确认不代表 WMS/ECS/RCS 对端签认、联调或现场验收。

实施顺序与验收见 [SMT 手工实施计划](../plans/2026-10-31-smt-manual-implementation.md)；任务合同/可信分派按 [sorting_task 共享计划](../plans/2026-10-31-sorting-task-implementation.md)独立交付一次，手工与自动宿主复用。共享ST1/ST2已完成并合入；手工M1～M6尚未执行，不因状态同步获得后续宿主实施授权。

## 1. 目标与依据

在一个 SMT 手工业务子包中承接手工分拣出库和手工入库，共用任务生命周期与箱级人工作业流程。工作线选择一个业务，任务通过 `task_type=MANUAL`、不可变的 `business_type=inbound/outbound` 表达方向。

- 手工入库：人工在 SCAN2 将料盘放入料箱；具体箱格、逐盘扫码、数量、库存及作业明细由 WMS/PDA 管理。
- 手工出库：人工在 SCAN2 从料箱拣料，或按计划在直接取料架作业；WMS/PDA 管理人工明细。
- 两种方向均由 WES 请求 WMS 箱级准入：WORK_REQUIRED 等箱级完成、NO_WORK 直接释放、WAIT 到期再问。
- 业务决定释放，经 BIN_Roller 登记唯一原 SCAN2 放行命令。人工任务完成不等待该命令 Result；原释放、公共回流及其他独立义务继续履约。

任务部分以[自动 API 3 节](../../integrations/smt-auto-api.md#tasks)为依据，引用其公共信封、六类任务操作、队列、准备、连续计划、取消、完成确认及原责任规则；本稿只定义 MANUAL 两种方向的资源与生产完成差异。任务额外字段、工作线准入和终态保留分别沿统一 API 引用的公共章节，不从旧人工 operation 推导新任务合同。

其他依据：

- [SMT 手工线 API 设计](../../integrations/manual-picking-api.md)：当前 sorting.task.*、manual_bin.*、两方向资源与完成条件，以及出库直接取料/可选转运架；原行为依据另见下述 wes_backend 插件。
- [BIN_Roller 设计](2026-10-30-bin-roller-capability-design.md)、[B1～B7 实施计划](../plans/2026-10-30-bin-roller-implementation.md)：四点事件、供箱、公共回流和统一五层架流。
- [公共回流 C01～C07/R01](../../integrations/workline-bin-return-api.md)、[业务包规范](../../integrations/business-packages.md)、[注册规则](../../integrations/registration.md)。
- `wes_backend/workline_plugins/manual-picking`：实际 SCAN2 入口、准入结果、完工应用、直接取料及任务完成行为的参考。旧插件只覆盖人工出库；不据其存在宣称本系统人工入库已实现，也不移植其旧扫码门槛、锁、容量窗口或完整运行时。

BIN_Roller 接入以已合并 PR #42（`e265ad7`）的组件行为为基线；公共回流 [C07](../../integrations/workline-bin-return-api.md#return-rescan)已同步：真正新 SCAN3 到位独立建轮，不等待旧轮闭合或人工移出。已有轮次内的 SCAN4 失败重扫与新轮分开，SMT 不重建轮次门槛。组件本地实现和隔离验证不代表正式生产接入完成。

## 2. 已选择的方案与范围

| 讨论方案 | 取舍 | 本次选择 |
| --- | --- | --- |
| 入库、出库各建一个人工业务包 | 重复任务、准入、完工和接缝，增加跨方向维护 | 不采用；一个业务包，以方向约束资源 |
| 一个业务包，内部组合共享代码能力 | 保留各业务状态与推进，复用合同和公共物理流程 | 采用：SMT 手工业务 + sorting_task + BIN_Roller |
| 首个业务先独占统一任务注册，后续再提取 | 初期声明少，其他业务接入时需迁移注册来源 | 不采用；现在规划 sorting_task 共享组件 |
| 通用任务状态机或工作流引擎 | 会把任务状态、推进和业务裁决搬入共享层 | 不采用；sorting_task 仅合同与分派 |

本设计采用业务码及目录名 `smt_manual`，显示名称“SMT 手工分拣与入库”；这是本稿的命名选择。入库、出库不分别注册业务、工厂、Celery 任务或 progression。

不包含自动机械臂逐盘取放、自动上架单层来源架、WES 人工逐盘账、库存/箱架绑定维护、PDA 页面、人工移出维护端点、跨任务/跨普通-drain 架周期接管、早到完工自动补绑、历史数据迁移或旧 operation 兼容入口。不得增加手工锁、预占、通道租约、实时容量账或第二个 Sender。

## 3. 主体与资源归属

| 主体 | 唯一责任 |
| --- | --- |
| smt_manual | 任务表与队列、prepare/计划/撤销/完成确认、人工作业状态、供箱需求与接续事实、SCAN2 准入/完工/释放决定、直接取料架与可选转运架的业务流转、唯一 scan_pending |
| sorting_task | 统一任务严格合同、六类 operation 的单份注册、入站归属解析和薄分派；无任务表、状态迁移、队列领取、计时推进或独立调度 |
| bin_roller | SCAN1～4 唯一事件入口、原本点经过、方向命令/SCAN2 原释放、五层供箱、FIFO、回架、drain 和普通/纯回收架统一 CTU 生命周期 |
| 既有平台 | receiver、原收据/幂等、可信绑定、公开结果读口、原业务 after_commit 通知、Outbox/Sender、统一业务任务壳与 Beat |
| WMS/PDA | 任务与资源意图、人工作业明细、箱格和库存、箱级准入/最终处置、整单完成裁决 |
| ECS/RCS/PLC | 原命令/搬运的实际执行、物理交接/互锁及权威结果 |

四点角色及公共五层架/入口/出口资源只由 BIN_Roller 声明，SMT 不重复声明。出库自有 `RETURN_RACK`、`TRANSFER_RACK` 槽位由 SMT 声明为可选资源；只在计划实际使用时要求相应绑定及合法性。RETURN_RACK 是 PDA 直接取料资源，不是滚筒退箱目标。首版省略 target_rack 时，人工通过 WMS 管理转运架，WES 不以其绑定或状态限制来源、扫码或任务完成。

具体 KT 线、设备码、地址、实际位置、架型与模板绑定属于部署配置；不从旧库复制运行配置，不按设备前缀推断业务，不把声明或配置当作到位事实。

## 4. 任务合同与人工计划

### 4.1 六类操作

| operation | 方向/端点 | 本稿承接 |
| --- | --- | --- |
| sorting.task.issued@v1 | WMS → WES events | MANUAL，business_type 必填 inbound/outbound；任务号、模式、方向及工作线发布后不可变 |
| sorting.task.queue_changed@v1 | WMS → WES events | 独立连续 queue_revision；仅 QUEUED 可更新，领取进入 PREPARING 与原 prepare 意图同事务冻结 |
| sorting.task.prepare@v1 | WES → WMS decisions | 仅 task_id/workline_code；同任务只成功准备一次，未知保持原身份 |
| sorting.task.plan_delta@v1 | WMS → WES events | 连续 plan_revision，按 §4.2 的人工资源形态受理并交付原供箱需求 |
| sorting.task.cancel@v1 | WMS → WES events | TASK 与 PLAN_MEMBERS 分开；精确撤销，不擦除原义务 |
| sorting.task.completion_confirm@v1 | WES → WMS decisions | task_id/last_applied_plan_revision；按 §7 人工条件发起并复核原结果 |

events 为 `POST /api/v1/wms/events`；decisions 为 `POST /api/v1/wes/decisions`。信封、类型范围、响应字段和完整错误规则直接引用统一自动 API 的第3章任务接口及第9章协议规则，不复制第二份完整任务报文规范。

本线符合时间及启动条件的 QUEUED 任务按非唯一 dispatch_sequence、原 issued.timestamp、task_id 排序；入库出库在同线共用 PREPARING/EXECUTING 一个执行位，不抢占；不同工作线独立。queue_revision 与 plan_revision 分开保存。

prepare 未定的早到计划只留待定接收信息并返回 503，不能反推准备成功；原准备确认后由 WMS 用原消息重试，应用提交后才成功 ACK。缺版不执行；queue_changed 的确定拒绝稳定重放与 plan_delta 的明确可重验原因分别处理，不能用同一个通用重放分支代替。

### 4.2 计划资源联合

| 字段 | MANUAL/inbound | MANUAL/outbound |
| --- | --- | --- |
| task_id、plan_revision | 必填；从 1 连续应用 | 相同 |
| added_bin_source_racks | 支持，提供接料箱；rack_id 与非空有序 rack_face[] | 支持，提供待拣料箱；同一结构 |
| added_direct_picks | 不允许 | 支持；source_locator 为 RACK_SLOT，精确 rack_id/rack_face/slot_id |
| target_rack | 不允许 | 首版可选；指定 rack_id 和单值 rack_face，后续不补充或更换 |
| added_source_racks、added_return_racks、逐盘/机械臂参数 | 不允许 | 不允许；不得混入自动模式字段 |

入库每版至少新增一种非空供箱架面需求。供箱只表示提供接料箱，WES 不增加空箱校验。人工料盘从何处取得、放入哪个箱格不进入 WES 计划。

出库首版至少包含有效 target_rack 或一种非空来源；仅直接取料也是有效计划。后续每版至少新增一种非空来源。无变化字段省略，不发 null/空数组；原清单、数组顺序和原动作不被新版本覆盖。同一供箱面可在后续版本形成新成员，使用原 task/plan_revision/rack/face 引用，不按架面吞并历史服务。

task_type/business_type 从原任务读取，不在 plan_delta 增加模式字段。sorting_task 负责保留并校验合法计划分支，SMT 核对本任务所允许的分支和成员，不用宽松字典吞掉跨模式业务字段。自动业务的来源联合及完成前置仍沿统一 API 第3章，不被本人工结构覆盖。

### 4.3 精确撤销

TASK 只含 task_id/cancel_scope，仅 QUEUED/PREPARING 且尚未应用首版计划可用；不用于执行中急停。PLAN_MEMBERS 不以父任务阶段替代成员判断，不改变父任务状态或 plan_revision。

| 人工方向 | 允许选择器 |
| --- | --- |
| inbound | bin_source_racks[]：原 plan_revision、rack_id、非空 rack_face[] |
| outbound | 上述供箱选择器；另支持 direct_pick_sources[]：原 plan_revision、rack_id、单值 rack_face；可选非空 slot_ids[]，省略表示该原版本本面的全部直接取料成员 |

合法选择器未命中、已取消或当前不可取消的成员跳过，全部跳过仍成功；不取消未来版本。原在制段继续按实际到位与原上下文请求 WMS，原分配/动作/报告/回流独立履约。供箱追加/撤销通过同一 receiver 事务交组件，不通过取消伪造供箱成功、取尽或物理退出。

## 5. 人工箱级接口

### 5.1 统一命名与范围

本设计采用新名称 `manual_bin.work_admission_decide@v1`、`manual_bin.work_completed@v1`，同时服务 MANUAL/inbound 和 MANUAL/outbound。两者归 smt_manual，sorting_task 不承接人工准入或完工。新名称与对端的签认、注册及部署属于后续交付，不因本稿产生真实可用接口。

不批量改名直接取料架 operation；出库专属的 `outbound.manual_rack.direct_pick_completed@v1`、`outbound.return_rack.task_completed@v1`、`outbound.return_rack.arrival_report@v1` 沿人工 API 的出库专属合同，入库不能调用这些分支。

### 5.2 manual_bin.work_admission_decide@v1

WES → WMS，`POST /api/v1/wes/decisions`。data 必填 task_id、归一化实际 bin_code、首次 SCAN2 scanned_at；不发送工作线、business_type、箱格、逐盘参数或准入前序引用。方向来自原任务。scanned_at 为原设备正毫秒时间，不晚于本次请求 timestamp；重复扫码、重启和 WAIT 不刷新它。

| HTTP/code | data 分支 | 行为 |
| --- | --- | --- |
| 200/DECIDED | result=WORK_REQUIRED，task_id 等于原请求 | 保存原准入及本次作业关联，等待箱级完工 |
| 200/DECIDED | result=NO_WORK | 保存正常直通决定，同事务经组件保存唯一原释放；不等待完工 |
| 200/DECIDED | result=WAIT、retry_after_ms | 保存固定到期点，到期以新 operation_id/请求时间求值；原 task/bin/scanned_at 不变 |

retry_after_ms 沿已核对的 manual_bin_admission_wire：JSON 整数，范围 1～60000 毫秒，不接受字符串、布尔值或 null；不能用设备命令超时替代。未知响应、503 或非法分支不放行，技术交付只恢复原身份、timestamp 和冻结正文；不能以未知响应新开业务求值。同步决定重放首次完整结果，不改成 DUPLICATE。

合法直达 SCAN2 不要求先有 SCAN1 或原投箱成功回调。无有效执行上下文、不可读/错误朝向或本次业务关联不成立时，保留原扫码和业务状态，不请求 WMS、不自动放行。组件不在委托前套人工准入门槛。上游成员取消不抹去有效本地在制上下文；实际准入由原 WMS 决定处理。

### 5.3 manual_bin.work_completed@v1

WMS → WES，`POST /api/v1/wms/events`。data 必填 task_id、实际 bin_code、result=NORMAL/NG、正毫秒 completed_at；completed_at 不晚于信封 timestamp，不作为经过选择器。不增加外部 arrival_id/visit_id、准入 operation_id、business_type、数量或逐盘明细。

1. receiver 先可靠保存原通知及待应用依据，提交后返回 202/RECEIVED；ACK 不表示已匹配、已放行或已物理完成。
2. 应用先处理原消息/已消费身份，再在原任务所属工作线唯一匹配本箱有效经过，核对 task_id、原 SCAN2 关联及 WORK_REQUIRED。
3. NORMAL/NG 都是本次箱级最终业务处置；匹配后同事务保存首次完工依据、释放决定，调用组件 release_scan2 并保存原 command_code。
4. 原消息重报或同一原经过的同结果重复不覆盖首次依据、不重复释放。无匹配、歧义、结果冲突、NO_WORK 已结清后又到的完工，保存待核对，不猜测“最新等待”。
5. 早到完工不因后来准入成功而自动补绑；不新增自动补绑、完工应用报告或通用恢复端点。

NG 只属于本次人工作业，不成为箱码永久属性；NORMAL/NG 都按原 SCAN2 MOVE_FORWARD 释放，SCAN3 只按本点事实分流，不读取上游人工 NG。

非任务族人工接口的类型、额外字段、有效内容幂等和错误边界沿人工参考 wire 适配；WES 出站 RequestPayload 拒绝额外字段，入站按各 operation 已定义的策略处理，不把直接取料通知的严格规则批量套给全部通知。新命名不能重算历史收据摘要或改写旧请求。

## 6. 出库自有架流

直接取料来源精确到 SLOT，人工明细由 WMS/PDA 管理。SMT 按原搬运确认架、工作位和面向后提交 arrival_report；报告 ACK 不制造到位。原报告未收妥保留义务并参与整单完成条件，但不作为所有换面/离场的统一门禁。

面级 direct_pick_completed 只闭合同任务、原版本、原架面的直接取料成员；整架 task_completed 只覆盖同任务同架 `plan_revision <= through_plan_revision`，等连续计划到截止版本后应用。两者不替代物理换面/离场，也不结束滚筒作业或授权转运架离场。

首版指定 target_rack 时由本业务承担转运架准备；未指定时不由 WES 管理其呼入、旋转、移出。转运架离场需匹配本任务 COMPLETED、原进场成功及当前在位事实，再申请共用 departure 决定；不等待独立来源架离场终态。

RETURN/TRANSFER 自有生产架的进场、必要换面与离场仍归 SMT，经既有 Transport 能力执行；`rack.departure_decide@v1` 复用 BIN_Roller 的唯一严格声明，不在 SMT 重注册同名 operation，也不要求组件替生产业务编排自有架。五层供箱/回箱及 CTU01/02/03 只归 BIN_Roller。

## 7. 人工任务完成与时序

<a id="smt-supply-completion"></a>
### 7.1 本地完成条件

| 条件 | 依据 |
| --- | --- |
| prepare 已确认，计划连续且无待处理计划 | 原准备响应、已应用版本及待定/缺版接收事实；尚无首版的特殊发起条件见统一 API 第3章 |
| 本任务供箱需求及其直接义务结清 | 原计划成员的有效/撤销事实 + get_supply_progress 返回的精确 SourceFaceRef/feed_complete/pending；按下表分别判断，撤销不伪造成功，空查询/缺关联不证明完成 |
| 已受理 SCAN2 人工作业结清 | NO_WORK 已应用，或 WORK_REQUIRED 已应用合法箱级完成；无未决准入/WAIT、未应用决定或待核对完工 |
| 释放决定和原命令关联已可靠保存 | 每个需要释放的原 arrival 都已同事务登记唯一原放行；不等待其 Result |
| 出库直接取料成员结清 | 对应版本面级/整架截止版本权威完成或精确撤销；原直接物理义务仍按所属流程收口 |
| 出库无供箱来源时，已指定的转运架准备完成 | 沿旧人工 ready_to_confirm 基线：任务未声明供箱来源且指定 target_rack 时，原进场成功及本线 TRANSFER_RACK 工作位/面向的可靠到位必须成立；进场未知/失败不补认到位。未指定目标架不增加此门槛，也不扩大为等待该架离场 |
| 必报事实已交付 | 本任务实际存在的到位报告等获得原 RECORDED/DUPLICATE；不新增人工逐盘 movement_report |
| 原完成确认仍适用 | 发起与消费响应时都复核当前连续版本、本地条件和原请求关联 |

本任务供箱需求与已受理生产作业是两组独立关联；不把每个 SCAN2 箱反查绑定到原供箱架、面或投箱成员。来源当前耗尽仅触发检查，不能自行宣告整单完成。

供箱成员逐项匹配原 task/plan_revision/rack/face，沿组件已实现的进度语义判断：

| 原成员与进度 | 本任务供箱条件 |
| --- | --- |
| 有效需求或已开始后撤销的需求，feed_complete=true 且 pending=false | 该原供箱义务已按实际进度结清；不扩大为本架回箱/离场全部完成 |
| 未开始即撤销，匹配进度 feed_complete=false、pending=false | 结合本业务可靠保存的原撤销事实，结清该需求；不要求 feed_complete=true，不补造投箱成功 |
| 原进度 pending=true，包括已开始后撤销但原供箱仍未结清 | 保留原请求、清单、段/搬运及失败待处置；不能因撤销跳过，待原供箱义务按实际进度收口 |
| feed_complete=false、pending=false，但本业务没有匹配的原撤销依据；或预期关联缺失/歧义 | 不视为完成，保留原依据待核对；不套用别的任务/版本或用空查询放行 |

SupplyProgress 不携带 withdrawn，撤销判断来自本业务原成员事实；pending 只表达该原供箱进度，不是整线所有公共义务。已开始需求在尚未形成 feed 时仍可 pending=true；宿主不通过“尚无投箱清单”推导未开始，也不直接读取组件内部表补判。

completion_confirm 的 COMPLETED、PLAN_REVISION_STALE、BUSINESS_IN_PROGRESS 三分支、原身份恢复、版本 0 条件与任务封口沿统一自动 API 的第3章任务接口。版本 0 没有默认等待时长，未显式配置就继续 PREPARING。WMS 返回 COMPLETED 且当前条件仍匹配才落任务终态；不以公共 FIFO 清空、全部箱回架、全部架离场或 SCAN2 放行 Result 为统一前置。

### 7.2 正常尾箱：完成确认先于放行 Result

正常人工流程在本任务作业结清后发起完成确认。最后一个箱的合法完工/NO_WORK、释放决定、唯一 MOVE_FORWARD 及原关联已经提交，其他本地条件成立，即可请求 WMS 确认。

WMS 完成确认响应与该 MOVE_FORWARD 的 Result 独立到达。COMPLETED 先到不构成任务矛盾，不等待或猜测 SCAN2 Result；原释放继续按原命令履约。Result 先到也不代替 WMS 整单确认。这里不依赖“多数情况下谁先到”的概率，不新增时延等待或宽限期。任务完成不宣称箱已经离开、到达 SCAN3 或物理回架。

### 7.3 完成确认未决期间另有新到箱

本项处理“另有真实新 SCAN2 到箱”的情况，不能与 §7.2 的原尾箱 Result 迟到混为一谈，也不以原 Result 迟到推导有新作业。按本次用户明确选择，保留以下规则：

| 事实/结果 | SMT 行为 |
| --- | --- |
| 原完成确认未决，新 SCAN2 到箱 | 组件照存事实并成功委托；SMT 保存原 arrival 和原任务关联，持久记录暂缓原因，暂不发起该新箱首次人工准入；不按无任务直通 |
| 此时又有合法连续新计划 | 原受理/应用并交付供箱变化；不覆盖已有成员，不因确认未决停公共供箱/架流 |
| 原有准入、WAIT、人工作业、完工、释放或公共义务推进 | 继续沿各自原身份；仅暂缓新箱首次准入，不冻结整线 |
| 原确认未知/503，或重复唤醒/重启 | 恢复同一原确认和暂存作业，不另发确认、不重复委托或刷新扫码时间 |
| 已消费有效 STALE/BUSINESS_IN_PROGRESS | 原最终决定已明确为非终局；再核对连续计划、任务/箱及既有等待条件后推进暂存箱准入，不借用完成确认身份 |
| 旧 COMPLETED 到达，但已有新计划/暂存新作业 | 保留原响应及新事实待 WMS 核对，不应用终态、不新开该箱准入、不直接释放、不换号绕过原终局 |

STALE 还须连续补齐原响应要求的版本；BUSINESS_IN_PROGRESS 的下一次整单确认遵守原 retry_after_ms，不能把任意 HTTP 返回当成解除暂缓。对本次新箱的处理不增加外部字段、协调锁或独立恢复器。

### 7.4 终态与旧义务

任务完成/合法取消/切换后停止本任务新的需求和作业授权，旧 task/成员/arrival/请求/命令/Transport/报告不迁移给新任务。原 pending 不能只筛 EXECUTING 或当前任务；原释放、公共回流、独立架离场继续。历史终态与新事实矛盾时不重开、不倒填成功、不自动换身份。

## 8. 目录与装配职责

下表中的宿主目录为后续目标，已交付的sorting_task/BIN_Roller组件继续复用。按真实职责组织，不要求为了凑目录给简单逻辑再加层；模块继续增长时才按已有模式细分。

| 目标文件/目录 | 责任 |
| --- | --- |
| app/businesses/smt_manual/definition.py | 不可变纯声明：业务码/名称、SMT 自有可选槽位与入口；公共角色从组件有效定义取得 |
| 同包 composition.py | 显式入口/模型；COMPONENTS 引用 sorting_task.COMPONENT 和 bin_roller.COMPONENT；COMMANDS=()；唯一 PROGRESSION=scan_pending；无状态工厂 |
| 同包 contracts.py | 人工严格合同和 MANUAL 计划/撤销分支约束，复用统一任务声明，不再次注册 |
| 同包 models.py、repository.py | 本业务任务/成员、原人工经过、准入/完工/释放决定、直接取料及转运架过程、等待/消费依据；不复制组件表或框架结果库 |
| 同包 task_flow.py | 任务状态、队列领取、prepare、计划应用/撤销、完成确认和终态原义务 |
| 同包 manual_bin_flow.py | 两方向共用准入/WAIT/完工/原释放决定；确认未决期间新箱首次准入暂缓 |
| 同包 manual_rack_flow.py | 出库直接取料、原到位报告、可选转运架及各自原 Transport/离场决定 |
| 同包 roller_ports.py | read_current_task/read_successor/accept_scan2；只适配本业务真相源 |
| 同包 service.py | 业务受理方法、固定组件映射、生产与组件待办轮换、唯一独立 advance 事务入口 |
| 同包 callback/、request/ | 人工及出库专属薄入口/严格模型，一个 operation 一个规范化文件 |
| app/businesses/_components/sorting_task/ | composition.py、contracts.py、ports.py、service.py、callback/、request/；只做统一合同与可信分派，无 models.py/迁移/任务状态机 |
| app/businesses/_components/bin_roller/ | 沿 B1～B7 原目标文件，不由 SMT 再建扫码、FIFO、五层架或方向命令实现 |
| tests/businesses/smt_manual/ | 入/出库公开行为、计划/完成差异、真实事务与生产接缝测试；共享 fixture 放 conftest.py |

每个目录按现有规范配置 __init__.py。SMT 工厂分别构造本业务服务、任务薄适配及一份 BinRoller，映射精确为 `components['sorting_task']`、`components['bin_roller']`；两个组件互不嵌套，不共享活动 session。任务适配只引用业务公开受理/归属读口，不递归构造服务或建立任务执行器。

## 9. sorting_task 单份合同与分派

### 9.1 声明边界

组件 key 为 sorting_task。四个入站 callback 与两个出站 request 全局只注册一次；无设备角色、位置、命令或持久化模型，组件无业务码、独立 progression 和计时待办。smt_manual 与 [smt_auto](2026-10-31-smt-auto-business-design.md) 两宿主显式组合它；AUTO 的 inbound/outbound 是同一 smt_auto 内部流程，不另设消费者业务。不注册尚未交付的占位消费者。

| 注册位置（相对 sorting_task） | operation |
| --- | --- |
| callback/sorting_task_issued_v1.py | sorting.task.issued@v1 |
| callback/sorting_task_queue_changed_v1.py | sorting.task.queue_changed@v1 |
| callback/sorting_task_plan_delta_v1.py | sorting.task.plan_delta@v1 |
| callback/sorting_task_cancel_v1.py | sorting.task.cancel@v1 |
| request/sorting_task_prepare_v1.py | sorting.task.prepare@v1 |
| request/sorting_task_completion_confirm_v1.py | sorting.task.completion_confirm@v1 |

统一任务模型须在首个正式宿主接入前完整交付MANUAL/AUTO四分支并保留各模式合法字段，按原任务的task_type/business_type选择严格分支，不能先用某一模式的忽略额外字段模型解析而丢掉另一模式字段。plan/cancel不增加外部模式字段。若现有注册器的单模型接缝不足，实施时仅补必要的显式合同解析/重验适配，复用原receiver/收据，不注册三个同名operation或新增通用宽松转发接口。

业务层的当前状态、版本、资源和完成判断仍在 SMT 原受理/推进事务中完成。共享组件的 service 只做“确定原归属 → bind_callback_business → require_component → 委托本业务”，不替业务创建任务、递增版本或消费完成响应。

### 9.2 原归属的可信解析

首次 issued 根据 workline_code 的受信配置确定业务，再核对任务模式/方向和合同消费者关系；已有 task_id 的重报先沿原收据及原任务归属处理，不能因当前工作线改配而转给别的业务。

queue_changed/plan_delta/cancel 的 wire 只有原任务引用，不能靠当前工作线或 payload 中另加的 business_code 路由。可信装配提供窄的只读原任务归属读口；本稿选择从显式消费者各自的任务真相源及保留终态摘要读取原业务/模式/方向，不增加共享任务表或把任务状态移入组件。原 task_id 在这些消费者范围内必须能唯一解析；这也是 WMS 任务身份的对接要求。若引用缺失/歧义或归属与原消息矛盾，沿任务引用/状态/幂等合同拒绝或保留原身份重试，不向多个业务执行处理、不按任务号前缀猜测。不能把暂时未提交的 issued 当成可新建任务的权限。

“查到原任务归属”与“当前有权新增作业”分别校验；停用、终态、历史重放及明细清理仍按统一 API 的任务章节处理。该归属读口和分支适配是本设计的待实施接缝，不能宣称当前框架已提供完整任务分派。

出站 prepare/completion_confirm 由本业务调用注入的 WmsRequests.send，并消费原 get_result；共享组件只提供唯一注册模型。business_key 保持 business:smt_manual，组件 key 不成为 owner。

### 9.3 公共重验、冻结合同与阶段验收

原plan重验的串行裁决归[共享计划1.1](../plans/2026-10-31-sorting-task-implementation.md#sorting-task-recheck)：receiver锁定并刷新原收据后才委托宿主，成功收妥不可被较晚失败覆盖；宿主原事务保存成员/版本/意图，不反向锁公共收据。公共锁序/5秒等待及刷新机制由ST1验，手工M1只补真实入口的原子性证明。

发布后遵守[共享计划1.4](../plans/2026-10-31-sorting-task-implementation.md#sorting-task-v1-freeze)：@v1有效字段、规范化和未知字段策略冻结，后到宿主不收紧或重解析历史输入；新语义经独立设计采用显式新版本，本轮不新增版本框架。历史成功/确定拒绝/待定计划均按原合同恢复。

验收按[共享计划1.3](../plans/2026-10-31-sorting-task-implementation.md#sorting-task-ownership)：ST1以隔离operation验证扩展/旧默认，ST2完成四个真实callback后验证errors、仅plan的recheck及可选guard登记，再回归ST1；正式catalog仍等待完整真实宿主。共享ST1/ST2已随PR #44合入develop，修复后专项24 passed、全仓4654 passed/52 skipped及CI证据见[共享交付§11](../plans/2026-10-31-sorting-task-implementation.md#sorting-task-merged)；真实宿主原子性及正式接入仍待实施。

## 10. SMT 自有接口注册

| 位置（相对 smt_manual） | 方向与职责 |
| --- | --- |
| request/manual_bin_work_admission_decide_v1.py | 新统一人工准入模型，decisions；两方向共用 |
| callback/manual_bin_work_completed_v1.py | 新统一箱级完工薄入口；原任务归属及本次匹配由本业务负责 |
| callback/outbound_manual_rack_direct_pick_completed_v1.py | 出库原版本架面直接取料完成 |
| callback/outbound_return_rack_task_completed_v1.py | 出库整架截止版本完成 |
| request/outbound_return_rack_arrival_report_v1.py | 出库原架到位事实报告，facts |

人工完工及直接取料通知先确定原任务归属，绑定 smt_manual 并核对消费者及方向；不能按当前最新任务或箱码选择业务。TASK 共享入口不在本目录重复注册。

四点 SCAN_COMPLETED、方向命令、供箱/回架/drain 和共用 departure 的原模型来自 BIN_Roller；SMT 自有 COMMANDS 为空，不声明机械臂 PICK_AND_PUT。需要共用 departure 的生产架流使用其唯一严格模型与原能力，动作状态仍在本业务。

所有固定模块进入各自 ENTRY_MODULES，模型进入各自 MODEL_MODULES，API/worker 共用显式 catalog；只有完整真实包与所需组件完成后才登记，不用空模型/空工厂补齐。共享合同全局加载一次、使用关系逐业务编译，另一业务 import 不授予本业务权限；真正撞键应启动失败。

## 11. BIN_Roller 接入与推进

| 本地接缝 | SMT 责任 |
| --- | --- |
| read_current_task(session, work_line_id) | 返回真实当前执行上下文；排队/本地已结束任务不当有效任务，成员撤销不抹去在制段 |
| read_successor(session, work_line_id) | 按原队列/prepare 返回 UNKNOWN/CHECKING/AVAILABLE/NONE 及证据；查空不直接推导 NONE；准备/领取仍在 task_flow |
| apply_supply_change(session, SupplyChange) | 计划受理原事务交付追加/撤销，change_id 引用原受理消息，SourceFaceRef 精确到原版本架面 |
| accept_scan2(ctx, Scan2Arrival) | 原事务接收组件已保存的 arrival，保存本业务任务关联/状态及合法初始准入意图；暂缓或普通不准入也保留事实 |
| release_scan2(session, arrival_id) | 只在原业务决定成立时调用，保存唯一原 command_code；只传原 arrival，不按箱码找最新经过 |
| get_supply_progress(session, work_line_id, task_id) | 按 §7.1 结合原成员撤销事实核对进度；未开始撤销允许 false/false，已开始/未决继续，缺引用不补认成功；不等待全局 FIFO/架流 |
| get_scan2_release_result(session, arrival_id) | 仅有需要时读原命令公开结果；不加入人工整单完成统一条件 |

组件 SCAN2 成功委托、原事实、业务状态及初始意图在 receiver 同一事务保存；不 commit、不换隔离、不另开写事务、不发 HTTP。业务普通不准入不回滚扫码；存储失败必须回滚并由原事件重报，不能吞掉错误返回成功。

组件依本线本点事件流确定原经过，同一 arrival 只成功委托一次；准入 WAIT 和完工属于 manual_bin_flow 待办，组件不保存第二套人工作业状态。释放决定归 SMT，原命令/结果消费归组件，均保留原业务归属。SCAN3 从实际本点到位独立判断，不能等待人工完成、上游任务或 SCAN2 Result。

同箱符合本点分隔规则的新 SCAN3 到位独立建立物理轮次，原旧轮是否结束/人工接管不作为前置；旧命令、分配与 Transport 仍按原身份履约，不自动 VOID 或改挂新轮。SCAN4 只关联当前有效 SCAN3 的双向原关联，必须有本轮自己的 SCAN4 匹配 SUCCESS 才具备回架资格；SCAN3 不分隔 SCAN4 本点经过，旧成功不替新轮授予资格。SMT 复用组件已合并行为，不自行查询旧轮或增加人工移出门禁，完整规则见公共 C07。

唯一 scan_pending 轮换本业务任务/人工/生产架待办及组件 SCAN、SCAN2_RELEASE、FEED、RETURN_BATCH、RACK_CYCLE、DRAIN；各分支固定本轮上界、按游标有界分页。等待、单条无变化或失败仍推进遍历；每轮结束丢弃游标，下轮重扫，不新增持久协调表或组件调度器。

组件 advance 使用宿主传入的独立单条事务。仅会进入 BIN_Roller 架流/正常求架检查的原宿主单条 advance，在首 SQL 前使用 SERIALIZABLE，整笔含外层 commit 做 40001 有界重试；不拆 DRAIN 专用事务或新增隔离选择器。receiver、短扫描和已有无关纯生产不一律升级，其他独立多查询结果消费沿既有 REPEATABLE READ。只读公开 WMS/ECS/Transport 结果，消费身份/版本、进度和下一 Outbox 意图原子提交。

新输入按受信解析业务通知；WMS 用原 business_key，ECS/Transport 用原 owner_domain，after_commit 只在外层提交后唤醒 smt_manual，5 秒 Beat 调同一入口兜底。不从组件名/当前任务重建旧 owner、不广播、不回填历史 None。UNKNOWN/HELD/超时不重做物理动作；重试耗尽留原待办。新 ECS 动作使用公共 PROTOCOL_RETRY_WINDOW，历史身份、字节、路由、附录和期限保持。

Transport 提交接纳与实际结果应用是两条已实现的原业务唤醒来源：原 submit 的有效 `202/RECEIVED` 或 `200/DUPLICATE` 保存为 DONE 时，主应用现有 on_delivered 事务回调按原 operation_id 与 business_key=transport_task_id 定位 TransportTask.owner_domain，经 after_commit 唤醒原业务；SMT 复用此装配，不另查平台内部表或创建通知器。失效租约、重复完成、失败/未知接纳、无已知 owner 或回滚不误通知；通知失败不撤销已保存接纳，持久待办由同入口 Beat 恢复。

组件在原 RACK_CYCLE 推进中读取公开提交接纳，原 CTU03 取得明确接纳即可释放 WES 架周期，不等待最终 outcome/位置；接纳不代表物理离场或运输完成。其他搬运与回架仍按各自原权威结果/终位条件收口，不能套用 CTU03 周期释放规则。

## 12. 验收要求与交付依赖

以下是后续实现必须证明的行为，**本轮未执行这些测试**：

| 范围 | 验收重点 |
| --- | --- |
| 单份任务注册 | SMT 与其他真实业务共用六类任务名称，合同仅一份；模式分支不丢字段，错误资源联合拒绝，其他业务不获额外消费者权限 |
| 原归属 | 首次 issued 按配置分派，后续按原任务；改配/迟到/归属缺失或歧义不串业务；终态摘要不重开任务 |
| 两方向计划 | 入库仅供箱；出库支持 BIN/直接取料/首版可选目标；连续版本、早到重试、重复和精确撤销遵守引用合同 |
| 供箱撤销进度 | 未开始撤销的匹配 false/false 结合原撤销事实结清需求；无撤销依据不补认，已开始/原 pending 继续；不要求撤销项投箱成功，不扩为等待全局架流 |
| 人工闭环 | 两方向 WORK_REQUIRED/NORMAL/NG、NO_WORK、WAIT；原扫码时间固定、技术未知原身份恢复、非法扫码留证、合法直达 SCAN2 |
| 完工关联 | 先可靠保存；唯一原经过/任务/WORK_REQUIRED；重复不放行两次，早到不自动补绑，冲突/歧义不猜最新 |
| 人工任务完成 | 本任务条件满足后可在 SCAN2 Result 未到时确认；Result 先到也不替代 WMS COMPLETED；公共义务不随任务关闭消失 |
| 确认时序 | 原尾箱迟到 Result 与另有新到箱分开；新箱暂存/原关联/暂缓首次准入，原作业继续；非终局恢复、旧终局矛盾留证 |
| 组件与事务 | 原收据/事实/业务/意图同事务回滚；组件单委托/单释放、公开进度、原 owner；即时与 Beat 竞争、重启/通知丢失恢复原身份 |
| 提交接纳唤醒 | 原 RECEIVED/202、DUPLICATE/200 保存后仅通知原 owner；回滚/失效租约/重复完成/失败未知不误通知；通知丢失仍可从原 pending 恢复 CTU03 周期释放，不伪造物理完成 |
| 公共轮次接入 | 真正新 SCAN3 在旧轮未结时可独立建轮，旧命令/分配/Transport 保留；SCAN3 不切分 SCAN4，旧结果不授予新轮资格；已有轮次 SCAN4 失败重扫保持首次 FIFO 顺序 |
| 出库生产架 | 原到位报告、面级/截止版本完成、可选转运架、精确撤销及自身离场；无供箱来源但指定转运架时，到位前不发起整单确认；RETURN_RACK 不接滚筒退箱 |
| 两线隔离 | 同一业务两线使用不同绑定并行；任务/arrival/原命令/供箱/FIFO 不串线，公共物理顺序沿组件合同 |

业务测试模型用独立 MetaData，显式创建/finally 清理，不污染生产 Base 或迁移。正式表及组件表各自列 MODEL_MODULES，迁移串到实施时唯一 head；独立数据库验证升级，不复制 wes_backend 历史生产表。

实施依赖为 sorting_task 共享严格合同/归属分派、SMT 完整业务、BIN_Roller B1～B7、既有公开结果/原业务通知和实际资源绑定；缺任一项不能把完整组合标为已交付。当前仓库已有 BIN_Roller 组件本地实现、共享声明及隔离验证，但无 SMT 正式生产业务与 BIN_Roller 正式消费方，组件状态迁移及整体验收仍属 B7；本稿不将组件或框架能力等同于完整生产业务。

自动上架/拣料现统一为 smt_auto，[业务设计](2026-10-31-smt-auto-business-design.md)、统一API和[总实施计划](../plans/2026-10-31-smt-auto-implementation.md)均按共同规则与方向差异维护。自动计划共同步骤各一份，I1～I4/O1～O4保留专属生产，验收编号统一追踪；sorting.task.* 六类声明唯一归 sorting_task，两宿主显式消费，不在方向包重复注册，也不把自动/人工任务状态推进搬进组件。统一 API 的第3章仍定义方向计划与完成差异。共享归属和错误/重验接缝只交付一次，ST1/ST2已随PR #44合入develop（380aeba）；完整SMT宿主和正式接入仍未交付。

新 manual_bin.* 名称、任务号在共享消费者范围的唯一归属要求、首批计划等待配置、供应商合同及现场绑定须在正式集成前逐项落实；这些属于交付依赖，不能用本地测试或文档状态代替对端签认。准入 WAIT 的 1～60000 毫秒范围已按旧 wire 固定，本稿不将其列为新的业务未决。

## 13. 留给实施者的选择

本稿已固定业务/组件边界、任务引用、人工资源联合、箱级语义、原释放与任务完成关系，以及确认期间新到箱规则。实施者可按现有风格确定表名/索引、私有方法和文件的适度拆分、分页大小和有界事务重试常量，但不能改变 wire、归属、唯一原意图和业务完成条件。

共享ST1/ST2复用PR #44已合入交付；手工M1～M6仍需后续实施授权。本轮只同步设计/API/计划交付状态，不创建产品代码、模型、迁移、测试、配置或占位 catalog，不提交、不推送、不部署、不进行真实发送。
