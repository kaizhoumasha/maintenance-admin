# SMT 自动上架、拣料业务子包设计

**当前工作区状态（2026-11-05）**　见[SMT AUTO 实施与验收](../../testing/smt-auto.md)。入库、出库正常流、共同原释放/完成确认及实际宿主已接入隔离装配，代码迁移 head 为 `smtautosources`。出库权威空取和 NG 设备映射仍待对端交付。下文任务记录中的状态与测试数字保留当时范围。

当前实现核对：2026-11-05，develop 基线 `98008ff`；原 AUTO S1 foundation 的7表及 `smtautofoundation` 是历史基线。本工作区 ORM 已有11张AUTO表及原成员/输入/首目标的 typed job/action 引用；增量迁移链为 `smtautofoundation` → `smtautorackfacts` → `smtautofirstplan` → `smtautosourcearrival` → `smtautofirstdeadline` → 唯一代码 head `smtautobarcode`；Task 10/11增补原首问/终局、原到位时间和期限typed字段，保留既有表/身份/数据，有证据时拒绝有损降级。迁移未应用，既有部署未改变；手工宿主5表、BIN_Roller 4表、RackDispatch 2表保持。**S1/S2整体未完成，AUTO未正式注册/装配或现场验收**；PR #49 表数及 `sharedrackdispatch` 仅为历史快照。

Task 8历史有界进展（2026-11-05，未提交）：在Task 7公共三类AUTO来源/固定模板基础上，真实持久化 `AutoRackPorts` 已提供原任务/工作线十一类集合的 FactRef/Coverage、PointDemand/DirectHandoff、原 DeviceBinding 快照与当前绑定/代际/启用归属核对，以及初始 ENTER 和原最终 BIN/RackSlot 目标 ROTATE 门禁。WORK_PLAN只认对应真实 operation、原subject/request及完整 HTTP 200/DECIDED/READY；最终决定按原盘主体分别核对 `outbound.bin.material.decide@v1` / `outbound.rackslot.material.decide@v1` 的原请求、完整最终 ACCEPT 和目标。RackSlot来源还须原准备动作 APPLIED/SUCCEEDED，原目标/实际/当前point与face精确一致；原pending/applied动作重放复用原身份且不新增意图，原引用/动作/outbox同事务回滚；旧PUT、报警、未知物理责任及非空 `physical_closure_basis` JSON不能据此授予CLEAR。聚焦组合验证 **339 passed / 118 skipped**，独立有界复审无未解决Important/Critical；该测试结果来自实施报告，非本次文档同步重跑。生产Alembic RackJob来源CHECK仍只允许原四来源，保留扩充归S6；不得据此创建真实AUTO job。具体AUTO ECS合同/公开严格物理Result读者、方向完整writer、NEXT/RETRY/取尽运行写入、完整锁集/40001事务装配、资源现场准入及catalog激活仍未实施；PostgreSQL/SSI、全仓库、浏览器及现场未验证。

Task 9历史有界进展（2026-11-05，未提交）：真实 AUTO `RollerPorts` 已提供当前/后继任务、原计划顺序供给面及 SCAN2 原到箱关联；当前 PREPARING/EXECUTING 保留原任务身份，有效性核对恰好一条原PREPARE关联、精确 `sorting.task.prepare@v1` operation及原已消费 PREPARE_ACCEPTED、启用及宿主/编码，revision 0不被排除。错误operation或多条PREPARE关联保留原无效任务，后继为 `CHECKING`，SCAN2冻结原方向并保持 `CONTEXT_NOT_READY`/nonpending。SCAN2冻结原arrival/line/device/bin/scanned_at/task/方向，重放先返原关联，不随取消或配置变化改绑。宿主注入实际 `BinRoller` 时，规范已应用计划及实际供给成员撤回以原输入change_id、精确版本成员引用，在同一调用方事务交付公共 `apply_supply_change`；重复、别名确认及已撤回不重复交付。未注入时没有供给交付，也不证明以后自动补送。聚焦适配器 **15 passed**，AUTO/BIN_Roller/RackDispatch/迁移有界组合 **668 passed / 384 skipped**；证据来自Task 9实施报告，独立有界复审PASS，本次文档同步未重跑。SQLite证据不证明PostgreSQL锁/SSI；未完成全S2验收。S5仍须权威原在制查询、无任务旁路门禁、原释放决定/命令/结果消费、方向首次WMS work_plan及原完成确认恢复；`NO_TASK`/pending只保留关联，不授权旁路或证明释放，历史COMPLETION请求仍保守保持 `CONFIRMATION_PENDING`，`CONTEXT_NOT_READY`不自动激活。S6实际组件注入、唯一正式runtime装配/catalog激活及完整receiver路径仍未实施；11张AUTO表及代码head不变，迁移未应用，无真实WMS/ECS、部署或现场验收。 以下Task 10/11状态取代本段当时的S5待实现/代码head及事务缺口描述；历史测试数字不变。

Task 12当前有界进展（2026-11-05，未提交）：正常 inbound I2 已贯通原 READY/NEXT 授权→原架侧取盘→实际原 SUCCESS/1/PUT_FINISHED 及完整 barcode/scanned_at→`putaway.material.decide@v1`→当前 ACCEPT 的原 BIN 侧放盘→实际原成功 Result→`putaway.material.movement_report@v1`→严格原公开报告收妥→TaskFlow 原 BIN 释放及实际 roller MOVE_FORWARD Result。无效 NEXT 不阻合法当前 PUT/报告，也不能伪装无后继或闭箱；有效 NEXT 创建独立下一盘。非空物理/报告闭合须实际注入可选 normal-inbound 证明读者，重读原公开命令及报告身份/状态/数据；裸 JSON、时间戳或 ACK 不足，未注入仍保守阻塞。正常正例保留独立活跃来源责任，task 仍为 EXECUTING，不证明来源离场或整单完成。

原来源取尽修正已按原已消费 MATERIAL_DECIDE 的公开严格 ACCEPT/source_rack_exhausted、原盘/任务/请求/目标及原 RackDispatch job/action/到位资源证明派生，立即排除后续 work_plan/material_decide 的 source_racks、新取盘准入及迟到原 READY 消费；重启/重放仍保留原身份，回滚不遗留消费事实，同码新 job 不继承旧到位取尽。当前已合法授权的 PUT/报告继续；取尽后的 PointDemand NONE 不等于 DirectHandoff CLEAR，不制造撤回、离场或释放证明。原始空目标占用 0/0、完整 Unicode 条码最多1000 code point 保持；新增双向 barcode 扩容迁移 `smtautobarcode` 接 `smtautofirstdeadline`，降级先核对两表并拒绝有损收窄，仍11表、迁移未应用。

`BIN_ARM`/`RACK_ARM` 为内部逻辑角色，正常两段共用严格命令声明；每线原身份/版本、来源工位映射及两臂 timeout 必须 typed 注入，不取示例30000默认值。1..2**63-1仅协议数值边界，不代表供应商/现场 timeout 签认；S6必须提供真实配置适配及正式组件/catalog/runtime装配。Task 12修正受影响快照 **224 passed**，最后补原资源/点位/工作线核对后的 inbound 独立快照 **26 passed**，数字不可相加或当最终组合；修正后最终组合门槛 **1297 passed / 650 skipped（102.53s）**，精确命令见Task 12实施报告；这是受影响组合证据，不代表全仓库或PostgreSQL验证，本次文档同步未重跑。原独立复审的重要取尽问题及修正见实施报告，尚不据此声称全 I2/S2 验收通过。NG、空取/重试、outbound实际执行、material WAIT重求值、迟到结果更正、来源交出/整单闭合、撤回never-started、生产RackJob来源CHECK扩充（仍原四类）、PostgreSQL迁移/锁/SSI及现场验收仍明确开放；无正式激活、真实发送、部署或现场完成。

Task 10/11历史有界进展（2026-11-05，未提交）：`WorkPlans` 已实现三方向首问（inbound BIN、outbound BIN、outbound RETURN）及严格 READY/WAIT/NO_WORK 原请求/公开结果消费。RETURN 的 `arrived_at_ms` 仅在原 ENTER/ROTATE 首次可靠 APPLIED/SUCCEEDED 且原任务、成员、绑定、point/face 核对通过时同事务写一次；迟到首问/恢复/WAIT沿用原时间，历史缺失不得用后来时间补造。WorkPlans/AutoRackPorts共用 WorkLine → Devices → RackJobs → 固定顺序AUTO主体 → RackActions 锁序；锁集漂移整事务重试。

Task 11 的共同 `TaskFlow` 已提供原线/BIN在制查询与原任务责任保持、无任务/矛盾到位暂缓、同事务原释放决定及实际 BinRoller `release_scan2` 命令引用；只认公开原命令严格 SUCCESS/SUCCEEDED 且无hold/reconciliation的释放结果。完成确认复用严格 CompletionConfirm/CompletionDecision，保留原请求身份、版本及恢复责任，COMPLETED重查当前依据，STALE须严格超过原请求版本；有效非终局按原到期/新依据恢复原held首问，CONTEXT_NOT_READY/ORIGINAL_INPROCESS不自动放行。可选首计划等待默认None，原prepare首次可靠接纳时冻结显式配置及期限（含时间0），重启/改配置不刷新。独立入口首SQL前SERIALIZABLE，RackLockSetChanged/40001含commit最多三次新会话整事务重试；不改变receiver的RR合同。两方向已通过原WAIT完整链→到期续问→NO_WORK→实际原释放SUCCESS→CONFIRMING→COMPLETED的隔离公开入口回归，核对每轮原关联、连续编号、前序链接、scan上下文、严格公开结果及原retry/due依据；孤儿、未决及损坏历史仍阻完成。

Task 10/11独立有界复审均PASS，最终受影响组合 **928 passed / 388 skipped** 包含到位时间、锁序、STALE/物理标记及WAIT链修正；这是实施报告证据，本次文档同步未重跑，也不代表全仓库验证。当时真实方向正常生产ECS/报告writer、非空物理责任闭合及撤回成员严格never-started证明仍缺失并保守阻塞（正常 inbound 局部能力以下述Task 12状态为准），不以JSON/ACK/空进度替代。S1/S2/S5整体验收仍未完成；S6正式catalog/runtime、组件/资源政策与生产RR装配、PostgreSQL锁/SSI及生产RackJob来源CHECK扩充仍为前置门槛（现仍仅原四来源）。无真实WMS/ECS请求、迁移应用、部署或现场验收。

当前共享边界：复用工作线 `positions[].capacity` 的共享、按本任务/点位准入：额度只约束当前任务在该点的申请，不预准备未来任务，不另设AUTO额度。容量不证明物理到位、交接或任务完成；roller原CTU01/公共job释放责任仍由组件维护。新工作须核对 WorkLine 的原业务、启用状态及编码/归属；停用、删除或改绑不能清除原结果、必报、已开始动作与原身份恢复责任，也不能授权新生产。KT16/KT18 的 `smt_manual` 配置不构成AUTO验收；AUTO只用隔离fixture及KT11/KT14目标配置，本轮不操作现场。

共享 `PrepareAccepted` 只在原请求公开结果 `FOUND/VALID` 且 HTTP `202/PREPARE_ACCEPTED` 时按其解析器消费，`data` 不要求 `result`、版本或重试字段，额外data字段按该模型忽略；这不放宽 `CompletionDecision` 的严格分支或其他协议。早到计划须WMS重试原消息，在原prepare可靠保存后重新核验；不得prepare一到便自动应用。AUTO宿主已有调用方事务内的持久队列领取、唯一原prepare登记/公开原结果消费及TaskHost原计划重验入口；Task 6已通过测试专用catalog/runtime、真实共享WMS receiver及公开WmsRequests/Outbox结果口验证早到503原收据/BLOCKED、原prepare接纳不自动应用、原重试202/重复200空data、两方向成员/版本/输入/收据原子回滚及原归属恢复。新增仅供当前S1 prepare领取/原结果消费的AutoS1Transactions独立入口，首SQL前SERIALIZABLE，40001含commit最多三次新会话整事务重试；耗尽保留已提交原身份/pending，不改receiver的RR合同。聚焦验证221 passed / 82 skipped；本次未配置TEST_DATABASE_URL，PostgreSQL用例跳过。设备/位置/方向完整资源声明与准入、正式catalog/runtime激活及PostgreSQL锁/SSI并发证据仍开放；SQLite及注入40001不证明真实SSI冲突或物理完成。

## 1. 目标与已选择方案

统一业务码及目录名为 `smt_auto`，显示名称“SMT 自动上架与拣料”。工作线绑定一个业务；`sorting.task.issued@v1` 的 `task_type=AUTO`、不可变 `business_type=inbound|outbound` 决定本次任务使用的生产流程。

| 方案 | 取舍与结论 |
| --- | --- |
| automatic_putaway、automatic_picking 各建业务包 | 会重复任务、队列、业务归属与组件装配；不采用 |
| smt_auto 内保留 inbound/outbound 两条流程 | 统一任务与恢复主体，保留方向合同、参数、异常和完成条件；已选择 |
| 合成通用逐盘状态机或工作流引擎 | 会混淆机械臂分工、NG 报告与来源终局；不采用 |

统一 [smt_auto API](../../integrations/smt-auto-api.md)按接口和共同规则维护一套合同：六类任务操作、授权/等待/关联、ECS公共信封、BIN_Roller接缝及协议规则各定义一次；[inbound流程](../../integrations/smt-auto-api.md#inbound-flow)与[outbound流程](../../integrations/smt-auto-api.md#outbound-flow)的资源、逐盘字段、设备参数、报告范围及完成前置用严格联合、差异表和专属子节表达。操作名称和报文字段不因包归属变更而改名。人工两方向仍归 [smt_manual](2026-10-31-smt-manual-business-design.md)，不纳入自动业务。

本设计固定统一边界；[总实施计划](../plans/2026-10-31-smt-auto-implementation.md)以S1/S2/S5/S6/S7维护唯一共同任务、接缝、释放/确认、装配恢复和综合验收；[S3的I1～I4](../plans/2026-10-31-smt-auto-implementation.md#inbound-steps)与[S4的O1～O4](../plans/2026-10-31-smt-auto-implementation.md#outbound-steps)保留方向专属生产及完成资格。验收追踪和交付依赖各一份，不再保留两份完整方向计划。产品代码、迁移、配置和历史执行迁移均不在本轮范围。

[数据模型设计草案（2026-11-02）](2026-11-02-smt-auto-data-model-design.md)细化共同任务/输入/到箱记录、方向来源/盘/决定轮次、原生产架动作、唯一约束、外键及保留规则；M01～M12定位未来持久化验收，不替代API合同或总计划。草案§10已收敛三项业务边界：资格沿已合入手工规则；单层全撤回后安全退架、可恢复报警由ECS原命令内恢复由用户本轮选择。对端能力及D3/D4仍须交付，Task 8已补方向plate/decision_round模型及未应用的增量迁移；具体方向生产writer及严格物理结果闭合仍待实施。

## 2. 主体、组件与资源

| 主体 | 唯一责任 |
| --- | --- |
| smt_auto | 一份任务/输入记录、队列、版本及供箱成员；prepare/取消/完成确认；两个方向的生产状态和原执行引用；一个服务、工厂及 scan_pending |
| inbound | 单层来源架、SCAN2 接料箱、逐盘授权/取放/成功报告、NG/空取/续取、上架完成条件；是业务内部代码，无业务码/独立 progression |
| outbound | BIN/RackSlot 来源、RETURN/TRANSFER 生产架、逐盘取放及正常/NG 报告、面级终局、拣料完成条件；无业务码/独立 progression |
| sorting_task | 六类任务 operation 的单份严格合同、原归属解析和薄分派；无任务表、状态机、队列领取、独立 owner 或调度器 |
| bin_roller | 四点唯一输入、SCAN2原委托/释放、供回箱/FIFO/drain及公共五层架生产事实；原架job/action交RackDispatch |
| rack_dispatch | owner范围的架job/action、ENTER/ROTATE原结果、DEPARTURE_DECIDE及严格原submit接纳完成；不拥有自动盘生产/报告/任务终局 |
| 既有平台 | receiver/收据/幂等、Outbox、公开结果读口、可信装配、原业务 after_commit 通知和 5 秒 Beat |

`COMPONENTS=(sorting_task.COMPONENT, bin_roller.COMPONENT, rack_dispatch.COMPONENT)`，components精确三个key；工厂创建一个RackDispatch("smt_auto", capabilities)，同一对象注入BinRoller和两个方向生产适配。组件不嵌套、不各建一份调度器或独立业务，三个公共声明的模型/operation全局单份。AUTO来源/模板尚须按总计划S2窄扩充，不能直接把新source_kind送入#49现有四来源CHECK。

四点及公共五层架资源只由 BIN_Roller 声明。两臂和生产位置由 smt_auto 统一声明；按方向实际使用核对绑定。inbound 沿单层架 STATION_A/B、扫码台、SCAN2/NG；outbound 沿 BIN/RETURN_RACK/TRANSFER_RACK、扫码台及其 NG 设备附录。仅因包支持两个方向，不能要求每条线无条件具备另一方向全部专属资源；缺少本次流程必需配置时拒绝启动该流程，不猜默认值或增加工作线类型字段。实际支持两个方向必须有相应绑定及对端合同证据。

2026-11-03 用户确认的[KT11/KT14设备、现场点位与ECS服务地址](../../integrations/smt-auto-api.md#auto-config)及[机械臂位置映射](../../integrations/smt-auto-api.md#auto-ecs-locations)唯一维护在API文档：料箱侧臂分别ARM01/ARM03、货架侧臂ARM02/ARM04；非机械臂设备共用`{ECS_SHARED_ORIGIN}`，两线机械臂分别用`{ECS_KT11_ARM_ORIGIN}`、`{ECS_KT14_ARM_ORIGIN}`。左A为转运/单层，右B为退料/单层；outbound从B退料架取、只向A转运架放，inbound从A/B单层架取。WORKLINE保存OUTxx/CNVxx现场码供WMS/RCS使用，ECS沿STATION_A/B、STATION_SCAN、STATION_WORK1、STATION_NG及对应类型，不将现场码直接作为location_id。KT11机械臂暂不可访问；本次只更新文档，不代表配置已落库或现场通过，outbound NG完整执行仍待交付。

复用sorting_task合同/分派、BIN_Roller生产ports、RackDispatch注册/准备/推进/原进度及平台机制。手工5张宿主表及私有repository只参考，不继承；AUTO最终目标11张生产/任务表，从既有7张foundation保留数据演进，不建两方向rack_visit或rack_action。公共departure仅rack_dispatch登记，AUTO不再做私有WAIT/Transport发送；原供箱ID及CTU额度沿组件，RACK_CYCLE用公共job ID，不能从旧私有cycle表定位。

公共register/prepare不是两阶段发送协议：register只建/定位原job，prepare新ENTER/ROTATE已同事务创建Transport提交意图。宿主在调用prepare前按原锁序核对当前授权/资源和拟议搬运的安全交出，关联同提交；authorized_original_id仅恢复已有LOCAL_ONLY原动作。身份重入核对原冻结origin及原动作用途，不能把旧返回ID当新的许可；准备引用可先保存，生产仍要求公共当前原动作严格成功和精确面就绪。

同角色同设备 task_type 的 PICK_AND_PUT 合同由宿主 `ecs_command.py` 单份声明，精确保留两条流程合法分支、数值及 Result 约束，由 catalog 编译，不各注册一个冲突模型、不用宽松字典抹平参数差异。供应商尚未确认的分支保持未交付。

## 3. 统一任务生命周期与可信分派

### 3.1 首次 issued 和原归属

首次 issued 根据 `workline_code` 的可信配置确定宿主业务，再核对消费者关系、启用状态、`task_type=AUTO`、方向及本线相应资源。支持时 inbound/outbound 均可创建 QUEUED 任务；合法 inbound 不因该线当前在做拣料而被固定拒绝，也不进入 outbound 生产链。不支持的组合沿原工作线准入错误拒绝。

task_id、task_type、business_type、workline_code 与原 issued.timestamp 首次受理后冻结，不能换 operation_id 改向/改线。已有任务重报先沿原收据、原任务及保留终态摘要解析；摘要保留TaskOwnerRef所需的原模式/方向/工作线ID及code，不随业务明细清理，当前工作线改配不能改变原 owner。WMS负责task_id在sorting_task显式消费者范围唯一；读口拒绝可见缺失/歧义，不承诺跨宿主任务表的全局数据库唯一性，不广播或按任务号前缀猜测。

queue_changed、plan_delta、cancel 沿 task_id 读取原业务/模式/方向；prepare 与 completion_confirm 保持各 API 规定的最小字段，不额外增加 business_code 或方向字段。统一注册仍遵循 [sorting_task 单份合同与可信归属](2026-10-31-smt-manual-business-design.md#9-sorting_task-单份合同与分派)；该组件与归属接缝的ST1/ST2已随PR #44合入develop，专项24 passed及全仓4654 passed/52 skipped、最新复审和CI证据见[共享交付§11](../plans/2026-10-31-sorting-task-implementation.md#sorting-task-merged)；完整smt_auto宿主任务执行仍待实施，手工已合入状态见本文基线。

### 3.2 一份队列与版本

同线两个方向共用任务表、排序和 PREPARING/EXECUTING 部分唯一约束，不建两份执行位。符合 not_before/启动条件的 QUEUED 按 dispatch_sequence、原 issued.timestamp、task_id 排序；优先序不唯一，不优先某个方向，不抢占在办任务。不同线独立。

领取、PREPARING 与唯一 prepare 意图同事务。queue_revision/plan_revision 分开且连续；早到 prepare 未定计划只留待定并返回 503，不能应用或启动物理动作。原消息重验、确定拒绝稳定重放、有效内容及错误规则沿统一API；平台接缝由sorting_task ST1/ST2交付一次，总计划S1只实现宿主资格/状态消费。

### 3.3 计划和取消按原任务选择严格分支

| 方向 | 计划/资源分支 | 原合同 |
| --- | --- | --- |
| inbound | added_source_racks 单层候选及 added_bin_source_racks 接料供箱；不接受 outbound target_rack/added_return_racks/人工直接取料字段 | 统一API 3.5/3.6 |
| outbound | added_bin_source_racks 供箱、added_return_racks 的 RackSlot 来源、首版必填 target_rack；保留来源顺序/去重与首版约束 | 统一API 3.5/3.6 |

计划资源只有两组严格形态：AUTO/inbound为五层料箱供箱架 added_bin_source_racks + 单层来源架 added_source_racks；AUTO/outbound为五层料箱供箱架 added_bin_source_racks + 转运目标架 target_rack + 退料来源架 added_return_racks。通知data按原任务方向选择，不要求每版同时包含全部资源；首版/后续约束沿各方向。两方向WES成功应用后的ACK仍统一202/RECEIVED、data={}，差异不放入ACK资源返回；具体联合和示例见[API公共计划表](../../integrations/smt-auto-api.md#task-plan)。

统一任务组件按[共享计划唯一分工](../plans/2026-10-31-sorting-task-implementation.md#sorting-task-ownership)先保留全部已定义字段，再由ST2按原TaskOwnerRef调用ST1严格分支校验；宿主只核对实际版本连续性、工作线准入、原目标/成员及任务阶段，不再声明另一套任务DTO或重解析裁剪。方向成员及取消选择器不互借，实际应用版本/成员/供箱变化/原收据同事务；取消不变成取尽、急停或公共排空。

### 3.4 公共重验、冻结合同与阶段验收

原plan重验按[共享计划1.1](../plans/2026-10-31-sorting-task-implementation.md#sorting-task-recheck)由receiver锁定并刷新原收据后串行裁决，成功收妥不可降级；宿主随后沿WorkLines→Devices→RackJobs→固定类型/PK宿主主体→RackActions保存成员/版本/意图，advance不反向锁收据。公共锁后刷新/5秒等待/异常恢复由ST1交付，S1验证自动真实入口的原子性，不另做收据保护。

首个正式宿主接入前完整交付MANUAL/AUTO四分支；发布后遵守[共享计划1.4](../plans/2026-10-31-sorting-task-implementation.md#sorting-task-v1-freeze)，冻结@v1有效字段、规范化及未知字段策略。自动方向晚接入不收紧同一@v1或重解析旧输入，新语义须独立设计显式新版本，既定guard只接入原方向政策。本轮不新增版本框架或新版本operation。

ST1以隔离operation验证公共扩展/旧默认及并发兼容；ST2创建四个真实callback后才验errors、仅plan的recheck和可选guard登记，再回归ST1。总计划S1消费[同一交付分工](../plans/2026-10-31-sorting-task-implementation.md#sorting-task-ownership)，不重复公共测试或提前注册产品占位消费者；共享交付已完成，不代表自动宿主、运行恢复或现场验收完成。

## 4. 生产分派与原执行关联

首次 SCAN2 委托先在 receiver 原事务保存组件本点事实，宿主将本次作业绑定当时合法任务及冻结方向，再交对应流程。无任务、不可读箱、完成确认未决等分支沿各 API；完成确认未决的新到位保存原关联并暂缓首盘，不能误作无任务直通。

CurrentTask直接沿手工资格结构：PREPARING/EXECUTING、原prepare可靠接纳、线启用及原宿主/编码匹配，不以首版已应用作附加条件。prepare未知时保留原task_id及CONTEXT_NOT_READY异常，不误直通，也不因prepare后来接纳自动回放异常到箱；完成确认暂缓则按原3.7.3条件恢复，两个阻碍不混用。

成员精确撤回与原授权/生产责任不变。inbound非空完整成员集合全撤回、原取尽及outbound点位剩余需求，由AUTO在原事务转为PointDemand(CONTINUE/NONE/UNKNOWN)和DirectHandoff(CLEAR/BLOCKED/UNKNOWN)，含精确PointRef及完整Coverage/FactRef。缺输入/未知不是无需求，模板/工作线名也不能当授权。公共服务统一唯一离场链，不由宿主再复制。

本点位已无可承接需求、原直接取放/未知/安全交出可清时安排原架离场，不等父任务终态或独立报告ACK。TRANSFER同样按真实原目标授权、待接料/等待决定及未处理输入判断，不能用“task未完成”永远阻退，也不能用一盘已完/一次查空提前NONE。ENTER/ROTATE生产资格必须原已应用成功、点位及面匹配；非成功原到位的安全退架/NOT_ARRIVED只沿公共规则，不补认生产成功。

等待本次ENTER/ROTATE的授权保留需求，自身不当成机械臂占位；已有旧面PUT/原命令报警或未知则保护旧架面，后盘不得换面/换架抢占。outbound RackSlot NG报告后仍应复用原面work_plan、原material.decide/empty WAIT仍可能产生同面取料时保留点位需求，不能当作纯报告退架；已取料终局则不续问。原点REPLACE在旧job严格接纳完成后才登记新job/ENTER，目标授权仍归原盘。细则只维护于[模型§5.4](2026-11-02-smt-auto-data-model-design.md#54-准备交接与异常续问的事实边界)。

DEPART/RACK_MOVE+CTU03/F01的严格匹配原submit接纳（202/RECEIVED或200/DUPLICATE及原frozen/owner/caller/cycle/各消息身份）即完成调度，ZONE或不同RACK_POSITION包括生产线工作位均适用。后续FAILED、原点位终位或缺回调只备查，不回退、不阻后继、不重搬；这不是ENTER/ROTATE、机械臂或SCAN2_RELEASE的完成条件。生产/报告/放箱/整单确认仍独立；RackJob完成后原盘或报告不得从pending漏掉。

可恢复扫码/取放故障先由硬件停机报警、人工处理，ECS保留原未结束命令并只恢复未完成部分，最后用原command_code回传严格最终结果；WES不新增HOLD/复位/重发或人工恢复接口。已应用FAILED仍不可改SUCCESS，受影响直接责任无证据不能关闭，正常/NG报告范围不变；设备互锁决定实际停机范围，不新增WES全线门禁。该设备能力属于D3/D4待签认交付，不表示本轮实现。

ECS发送最终FAILED后即不再属于可恢复中间报警，不利用WES尚未应用/ACK未到窗口另报成功；合法Result先于ACK仍按原证据应用。成功消费只关闭对应原动作阶段，取到扫码台不能提前记整盘物理收口；正常/NG实际放置、应报报告和原授权/终局责任分别核对，未知/HELD或最终失败不能借人工已处理说明放箱/关单/归档。

重复委托及恢复先查原 arrival/作业，不读当前任务改绑。RackSlot/单层架过程、盘、求值、命令、报告均沿所属原任务的 business_type 选择处理；旧 inbound 未决与新 outbound 任务可以分别被同一个 pending 发现，不能按当前方向过滤掉旧责任，也不能把旧授权用于新任务。真实同箱新 arrival 沿组件原经过事实独立受理，不按箱码合并。

WmsRequests 的 decisions/facts 绑定业务实例写 `business_key=business:smt_auto`；Transport 写 `owner_domain=smt_auto`，其原提交 business_key 仍为原 Transport task ID，新 ECS 意图使用 `CommandOwner("smt_auto")`。call_key 包含原动作的稳定业务出处，两个方向的同名本地主键不发生碰撞；按总计划固定 kind 分别找到原记录。组件 key 或 inbound/outbound 均不成为 owner。

这是新增业务的归属约定；其他项目及既有冻结请求仍保留原 operation/身份/正文/路由/期限/owner/证据，不回填历史 null，不批量改 owner 或复制旧库任务。

## 5. 两条流程各自的合同与完成条件

| 边界 | inbound 自动上架 | outbound 自动拣料 |
| --- | --- | --- |
| 正常路径 | 单层架 → ARM02 → 扫码台 → ARM01 → SCAN2 接料箱 | BIN → ARM01 → 扫码台 → ARM02 → 转运架；RackSlot 两段均 ARM02 |
| 业务接口 | putaway.work_plan/material.decide/material.movement_report/source.empty_decide，初始空架 rack_exhausted 通知 | outbound.bin/return_rack.work_plan、bin/rackslot.material.decide、material.movement_report、source.empty_decide |
| 参数 | 目标层数/总厚度为放盘前占用，WMS 原值传递；不从层数推算深度 | 保留 BIN 原参数规则、RackSlot 取盘前完整层/列/厚度/直径，目标及报告保持 RACK_SLOT |
| 正常/NG 报告 | 正常明确成功放盘才报告；FAILED 和 NG 不报告 | 正常及 NG 明确成功且位置确定均报告；NG 报告收妥才满足其异常续问前置 |
| 空取/续取 | 明确 FAILED/0/PICK_EMPTY → RETRY/WAIT/SOURCE_ABSENT；NG 成功或 SOURCE_ABSENT 合法收口后依原规则续问 | 明确无料载体仍待设备附录；普通 FAILED/PICK_FAILED/0 不证明空取；RETRY/WAIT/SOURCE_DONE；保留 Cell/SLOT 关闭及异常续问规则 |
| WAIT | work_plan/material.decide 到期可携最新 source_racks；empty_decide 保持原观察 | 五类决定到期新身份但原 data 不变；不能复制 inbound 的来源集合更新 |
| 本箱/面结束 | NO_WORK 或正常 ACCEPT 无后继只是取料终局；本箱原授权/取放/扫码/决定及正常必报收口后释放 | 箱/面终局分别判断；正常与 NG 必报、授权/异常/未知动作全部收口，CLOSE/SOURCE_DONE 不等于箱/面结束 |
| 整单完成 | 严格按统一API 3.7，含原需求/已受理生产及原释放收口 | 严格按统一API 3.7/5，含本任务供箱、实际 RackSlot 面、正常/NG 必报及公开已应用原释放成功 |
| 明细保留 | 任务终态且直接义务收口后完整 7 天，未决继续保留，清理后持续保留最小终态摘要 | 维持原合同，不引入 inbound 的 7 天清理或清理后拒绝规则 |

同一任务最多一条未决 completion_confirm，共用登记/原响应关联；发起条件及响应复核委托冻结方向。准备未知、缺版、版本 0 的显式首批等待期限、新计划/新到位与旧 COMPLETED 冲突均按对应合同判断。任一方向不能以公共 FIFO/drain/全部回架/独立离场未空作统一门槛，也不能用“终态后可继续义务”跳过其正常完成前置。

inbound 的 archive_guard 在共享任务入口按原任务方向显式委托；只对满足 inbound 原归档条件的记录生效。outbound、smt_manual、旧接口默认行为不变。公共原结果回调不接入任务归档门禁，仍能收口原物理事实。

## 6. 文件、注册与迁移归属

| 后续实施位置 | 责任 |
| --- | --- |
| app/businesses/smt_auto/definition.py、composition.py、service.py | 一个业务声明/工厂/SmtAutoService；组件映射、唯一 progression、原事务及方向分派 |
| 同包 models.py、repository.py、task_flow.py | 一份任务、输入、共同供箱成员、版本/队列/确认引用及原任务归属读口 |
| 同包 roller_ports.py、ecs_command.py | 一套组件生产接缝、原供箱/释放适配；单份严格机械臂合同 |
| 同包 inbound/contracts.py、models.py、repository.py、source_flow.py、material_flow.py、archive.py、callback/、request/ | 上架专属状态、严格业务合同、单层架事实适配/逐盘/归档；不重复任务表、工厂或统一任务 operation |
| 同包 outbound/contracts.py、models.py、repository.py、rack_flow.py、material_flow.py、request/ | 拣料专属状态、严格合同、公共架事实适配/逐盘/面级收口；不新建任务表、owner 或归档期限 |
| app/businesses/_components/rack_dispatch/ | 复用公共模型/离场/提交接纳；S2只扩AUTO三来源及模板/授权接缝，不另写公共调度器 |
| app/businesses/_components/sorting_task/ | 共用合同与可信薄分派，和人工设计同一来源，全局仅注册一次 |
| tests/businesses/smt_auto/、inbound/、outbound/ | 共同生命周期/归属/切换，方向行为及原完成矩阵；独立 fixture 不互相 import 测试文件 |
| migrations/versions/2026-11-04_smt_auto_task_foundation.py及后续增量迁移、tests/migrations/test_smt_auto_state.py | 一次业务状态迁移交付；实施日期/head 按真实基线调整，组件迁移独立串接 |

业务自有每个 operation 仍用规范化文件名；六类 sorting.task.* 不在 inbound/outbound 再注册。根 composition 显式列方向入口/模型模块及统一命令，真实完整业务才进入 catalog。当前 catalog 为 coarse_sorting、smt_manual，新增完整smt_auto时保留两者；不能为展示新业务增加空工厂、空模型或测试消费者。

任务/计划真相源归根模型；方向表以原 task 引用关联，不再各建一份生命周期。Task 8增量迁移已从历史7张AUTO foundation保留数据演进至11表；S6不重建这些表，继续补必要公共source_kind CHECK扩充，接实施时最新head（当前smtautofirstdeadline），不得在S6重建全套表；不重跑#49切换、不改旧迁移/历史原身份、不重复创建公共2表，本轮不升级数据库。

## 7. 推进、事务与验收

唯一 SmtAutoService.scan_pending 公平分页轮换任务、inbound/outbound 生产及组件六类原待办。组件仍由宿主传入事务；after_commit 只通知 smt_auto，5 秒 Beat 调用同一入口，不新增方向 Celery 任务、Sender、锁/预占/租约或 WAIT 调度器。

点位需求须包含原job仍可服务的未绑定新有效成员；release_requested只是可由完整CONTINUE撤销的暂定标记，进入RELEASING/原离场根已登记后才停止关联新成员，不因版本追加复活已取尽来源。

消费、事实变化和下一意图原子保存；推进锁序WorkLines→Devices→RackJobs→宿主主体（固定类型/PK）→RackActions，各自按ID排序。锁后复核本线相关原job集合，RackLockSetChanged或40001含commit在新会话整笔重试最多3次。TASK、两方向RACK/BIN/PLATE和roller架流均首SQL前SERIALIZABLE；独立方向REPORT、SCAN2_RELEASE及纯归档RR，候选单个只读RR快照，receiver和全局factory不整体升级。源集合/授权/结果等改变时在原事务note_facts_changed，不只发通知。

独立REPORT/RR只消费原报告，不启动新生产；原NG报告收妥后的续问由SSI生产事务消费触发并一次登记。报告已消费但续问尚未登记，以及原PUT成功尚未登记报告，均为可重启发现的原责任，不依赖当前任务/配置或一次通知。停用/删除/改绑不拦旧结果和必报，禁止新物理动作；详见[模型§7.1](2026-11-02-smt-auto-data-model-design.md#71-报告消费与后续生产的事务交接)。

总计划验收必须覆盖：同线混合方向唯一领取；两线独立；共享 operation/组件只注册一次；同角色命令严格联合；方向计划不串支；inbound 旧结果晚于 outbound 新任务仍归原执行；共享 pending 不漏旧义务；两方向NG/WAIT/完成与保留差异；工作线停用/恢复及按方向核对资源；公开结果FOUND/VALID、精确HTTP/code及响应联合；通知丢失、回滚、40001、重启及原身份防重。

统一API验收表的72项编号在[总计划唯一追踪表](../plans/2026-10-31-smt-auto-implementation.md#acceptance-matrix)定位：共同项按原方向参数化，I/O任务覆盖专属差异，S5验证释放/确认交错，S6验证组合恢复，S7统一综合回归及证据；[总计划G01～G04](../plans/2026-10-31-smt-auto-implementation.md#integration-acceptance)补原结果唤醒/两线、非eager worker/Beat、公共真实接缝与首批等待配置。非eager worker只连独占测试DB/Redis及对端替身，独立记软件恢复证据；真实WMS/ECS/RCS联调与现场物理另记证，ACK/启动/本地通过不代表物理完成。手工首宿主B7软件接入已合入，自动只补自己的组合证据；不重复组件迁移、手工生产实现或套用尚未实施的粗分Transport结果更正。

当前详细评审与源码缺口见[#49规划复审](../reviews/2026-11-03-smt-auto-pr49-plan-review.md)，局部S1宿主修正已有总计划所列聚焦验证，尚无完整S1通过或生产AUTO验收结论。
