# SMT 自动上架、拣料业务子包设计

设计基线：2026-10-31已确认统一业务划分；交付状态核对：2026-11-01。**smt_auto宿主未实施、未注册、未部署**；共享ST1/ST2已随[PR #44](https://github.com/kaizhoumasha/maintenance-admin/pull/44)合入develop（380aeba）。本轮仅同步文档，不执行迁移、不提交、不发送真实请求。文档确认不代表 WMS/ECS/RCS 合同签认、联调或现场验收。

## 1. 目标与已选择方案

统一业务码及目录名为 `smt_auto`，显示名称“SMT 自动上架与拣料”。工作线绑定一个业务；`sorting.task.issued@v1` 的 `task_type=AUTO`、不可变 `business_type=inbound|outbound` 决定本次任务使用的生产流程。

| 方案 | 取舍与结论 |
| --- | --- |
| automatic_putaway、automatic_picking 各建业务包 | 会重复任务、队列、业务归属与组件装配；不采用 |
| smt_auto 内保留 inbound/outbound 两条流程 | 统一任务与恢复主体，保留方向合同、参数、异常和完成条件；已选择 |
| 合成通用逐盘状态机或工作流引擎 | 会混淆机械臂分工、NG 报告与来源终局；不采用 |

统一 [smt_auto API](../../integrations/smt-auto-api.md)按接口和共同规则维护一套合同：六类任务操作、授权/等待/关联、ECS公共信封、BIN_Roller接缝及协议规则各定义一次；[inbound流程](../../integrations/smt-auto-api.md#inbound-flow)与[outbound流程](../../integrations/smt-auto-api.md#outbound-flow)的资源、逐盘字段、设备参数、报告范围及完成前置用严格联合、差异表和专属子节表达。操作名称和报文字段不因包归属变更而改名。人工两方向仍归 [smt_manual](2026-10-31-smt-manual-business-design.md)，不纳入自动业务。

本设计固定统一边界；[总实施计划](../plans/2026-10-31-smt-auto-implementation.md)以S1/S2/S5/S6/S7维护唯一共同任务、接缝、释放/确认、装配恢复和综合验收；[S3的I1～I4](../plans/2026-10-31-smt-auto-implementation.md#inbound-steps)与[S4的O1～O4](../plans/2026-10-31-smt-auto-implementation.md#outbound-steps)保留方向专属生产及完成资格。验收追踪和交付依赖各一份，不再保留两份完整方向计划。产品代码、迁移、配置和历史执行迁移均不在本轮范围。

## 2. 主体、组件与资源

| 主体 | 唯一责任 |
| --- | --- |
| smt_auto | 一份任务/输入记录、队列、版本及供箱成员；prepare/取消/完成确认；两个方向的生产状态和原执行引用；一个服务、工厂及 scan_pending |
| inbound | 单层来源架、SCAN2 接料箱、逐盘授权/取放/成功报告、NG/空取/续取、上架完成条件；是业务内部代码，无业务码/独立 progression |
| outbound | BIN/RackSlot 来源、RETURN/TRANSFER 生产架、逐盘取放及正常/NG 报告、面级终局、拣料完成条件；无业务码/独立 progression |
| sorting_task | 六类任务 operation 的单份严格合同、原归属解析和薄分派；无任务表、状态机、队列领取、独立 owner 或调度器 |
| bin_roller | 四点扫码唯一入口、SCAN2 原事务委托及原放行、公共供箱/回流/FIFO/drain、五层架流及共享 departure 合同 |
| 既有平台 | receiver/收据/幂等、Outbox、公开结果读口、可信装配、原业务 after_commit 通知和 5 秒 Beat |

`COMPONENTS=(sorting_task.COMPONENT, bin_roller.COMPONENT)`；工厂各构造一份对象，`components` 映射精确为这两个 key，不嵌套组件，不给方向分别创建 BinRoller。两个组件都是本业务内部能力，工作线不单独选择它们。

四点及公共五层架资源只由 BIN_Roller 声明。两臂和生产位置由 smt_auto 统一声明；按方向实际使用核对绑定。inbound 沿单层架 STATION_A/B、扫码台、SCAN2/NG；outbound 沿 BIN/RETURN_RACK/TRANSFER_RACK、扫码台及其 NG 设备附录。仅因包支持两个方向，不能要求每条线无条件具备另一方向全部专属资源；缺少本次流程必需配置时拒绝启动该流程，不猜默认值或增加工作线类型字段。实际支持两个方向必须有相应绑定及对端合同证据。

同角色同设备 task_type 的 PICK_AND_PUT 合同由宿主 `ecs_command.py` 单份声明，精确保留两条流程合法分支、数值及 Result 约束，由 catalog 编译，不各注册一个冲突模型、不用宽松字典抹平参数差异。供应商尚未确认的分支保持未交付。

## 3. 统一任务生命周期与可信分派

### 3.1 首次 issued 和原归属

首次 issued 根据 `workline_code` 的可信配置确定宿主业务，再核对消费者关系、启用状态、`task_type=AUTO`、方向及本线相应资源。支持时 inbound/outbound 均可创建 QUEUED 任务；合法 inbound 不因该线当前在做拣料而被固定拒绝，也不进入 outbound 生产链。不支持的组合沿原工作线准入错误拒绝。

task_id、task_type、business_type、workline_code 与原 issued.timestamp 首次受理后冻结，不能换 operation_id 改向/改线。已有任务重报先沿原收据、原任务及保留终态摘要解析；摘要保留TaskOwnerRef所需的原模式/方向/工作线ID及code，不随业务明细清理，当前工作线改配不能改变原 owner。WMS负责task_id在sorting_task显式消费者范围唯一；读口拒绝可见缺失/歧义，不承诺跨宿主任务表的全局数据库唯一性，不广播或按任务号前缀猜测。

queue_changed、plan_delta、cancel 沿 task_id 读取原业务/模式/方向；prepare 与 completion_confirm 保持各 API 规定的最小字段，不额外增加 business_code 或方向字段。统一注册仍遵循 [sorting_task 单份合同与可信归属](2026-10-31-smt-manual-business-design.md#9-sorting_task-单份合同与分派)；该组件与归属接缝的ST1/ST2已随PR #44合入develop，专项24 passed及全仓4654 passed/52 skipped、最新复审和CI证据见[共享交付§11](../plans/2026-10-31-sorting-task-implementation.md#sorting-task-merged)；完整SMT宿主任务执行仍待实施。

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

原plan重验按[共享计划1.1](../plans/2026-10-31-sorting-task-implementation.md#sorting-task-recheck)由receiver锁定并刷新原收据后串行裁决，成功收妥不可降级；宿主随后沿WorkLine→Devices按ID→原业务动作保存成员/版本/意图，advance不反向锁收据。公共锁后刷新/5秒等待/异常恢复由ST1交付，S1验证自动真实入口的原子性，不另做收据保护。

首个正式宿主接入前完整交付MANUAL/AUTO四分支；发布后遵守[共享计划1.4](../plans/2026-10-31-sorting-task-implementation.md#sorting-task-v1-freeze)，冻结@v1有效字段、规范化及未知字段策略。自动方向晚接入不收紧同一@v1或重解析旧输入，新语义须独立设计显式新版本，既定guard只接入原方向政策。本轮不新增版本框架或新版本operation。

ST1以隔离operation验证公共扩展/旧默认及并发兼容；ST2创建四个真实callback后才验errors、仅plan的recheck和可选guard登记，再回归ST1。总计划S1消费[同一交付分工](../plans/2026-10-31-sorting-task-implementation.md#sorting-task-ownership)，不重复公共测试或提前注册产品占位消费者；共享交付已完成，不代表自动宿主、运行恢复或现场验收完成。

## 4. 生产分派与原执行关联

首次 SCAN2 委托先在 receiver 原事务保存组件本点事实，宿主将本次作业绑定当时合法任务及冻结方向，再交对应流程。无任务、不可读箱、完成确认未决等分支沿各 API；完成确认未决的新到位保存原关联并暂缓首盘，不能误作无任务直通。

重复委托及恢复先查原 arrival/作业，不读当前任务改绑。RackSlot/单层架过程、盘、求值、命令、报告均沿所属原任务的 business_type 选择处理；旧 inbound 未决与新 outbound 任务可以分别被同一个 pending 发现，不能按当前方向过滤掉旧责任，也不能把旧授权用于新任务。真实同箱新 arrival 沿组件原经过事实独立受理，不按箱码合并。

WMS 绑定业务实例写 `business_key=business:smt_auto`，Transport 写 `owner_domain=smt_auto`，新 ECS 意图使用 `CommandOwner("smt_auto")`。call_key 包含原动作的稳定业务出处，两个方向的同名本地主键不发生碰撞；按总计划固定 kind 分别找到原记录。组件 key 或 inbound/outbound 均不成为 owner。

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
| 同包 inbound/contracts.py、models.py、repository.py、source_flow.py、material_flow.py、archive.py、callback/、request/ | 上架专属状态、严格业务合同、单层架/逐盘/归档；不重复任务表、工厂或统一任务 operation |
| 同包 outbound/contracts.py、models.py、repository.py、rack_flow.py、material_flow.py、request/ | 拣料专属状态、严格合同、生产架/逐盘/面级收口；不新建任务表、owner 或归档期限 |
| app/businesses/_components/sorting_task/ | 共用合同与可信薄分派，和人工设计同一来源，全局仅注册一次 |
| tests/businesses/smt_auto/、inbound/、outbound/ | 共同生命周期/归属/切换，方向行为及原完成矩阵；独立 fixture 不互相 import 测试文件 |
| migrations/versions/2026-10-31_smt_auto_state.py、tests/migrations/test_smt_auto_state.py | 一次业务状态迁移交付；实施日期/head 按真实基线调整，组件迁移独立串接 |

业务自有每个 operation 仍用规范化文件名；六类 sorting.task.* 不在 inbound/outbound 再注册。根 composition 显式列方向入口/模型模块及统一命令，真实完整业务才进入 catalog。当前 catalog 仅 coarse_sorting；不能为展示新业务增加空工厂、空模型或测试消费者。

任务/计划真相源归根模型；方向表以原 task 引用关联，不再各建一份生命周期。迁移只建必要业务表/约束，保持一个 Alembic head；不改 BIN_Roller 表、不改已发布历史迁移、不执行本轮数据库升级。

## 7. 推进、事务与验收

唯一 SmtAutoService.scan_pending 公平分页轮换任务、inbound/outbound 生产及组件六类原待办。组件仍由宿主传入事务；after_commit 只通知 smt_auto，5 秒 Beat 调用同一入口，不新增方向 Celery 任务、Sender、锁/预占/租约或 WAIT 调度器。

消费公开原结果、已消费版本与下一意图同事务；独立推进沿 WorkLine → Devices 按 ID → 原业务记录加锁。纯生产沿既有 REPEATABLE READ，实际进入 BIN_Roller 架流覆盖范围的原 advance 在首 SQL 前设 SERIALIZABLE；新会话有界整事务重试覆盖 commit，提交未知重读原身份，不凭暂时查空换号。receiver/候选短扫描不整体升级隔离。

总计划验收必须覆盖：同线混合方向唯一领取；两线独立；共享 operation/组件只注册一次；同角色命令严格联合；方向计划不串支；inbound 旧结果晚于 outbound 新任务仍归原执行；共享 pending 不漏旧义务；两方向NG/WAIT/完成与保留差异；工作线停用/恢复及按方向核对资源；公开结果FOUND/VALID、精确HTTP/code及响应联合；通知丢失、回滚、40001、重启及原身份防重。

统一API验收表的72项编号在[总计划唯一追踪表](../plans/2026-10-31-smt-auto-implementation.md#acceptance-matrix)定位：共同项按原方向参数化，I/O任务覆盖专属差异，S5验证释放/确认交错，S6验证组合恢复，S7统一综合回归及证据；[总计划G01～G04](../plans/2026-10-31-smt-auto-implementation.md#integration-acceptance)补原结果唤醒/两线、非eager worker/Beat、公共真实接缝与首批等待配置。非eager worker只连独占测试DB/Redis及对端替身，独立记软件恢复证据；真实WMS/ECS/RCS联调与现场物理另记证，ACK/启动/本地通过不代表物理完成。首个 BIN_Roller 正式宿主顺序沿 B7，统一自动包不自行改变人工宿主交付安排。
