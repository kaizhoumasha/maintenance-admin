# 公共货架调度实体与推进服务设计

**2026-11-03 实施状态。** 手工与 BIN_Roller 已在本地实施公共调度、原提交接纳及两类宿主接入，原公共调度迁移为 sharedrackdispatch；后续工作位容量迁移追加 positioncapacity head。验证范围为软件与可丢弃数据库，供应商验收、真实生产数据迁移和现场执行仍待验证。


**工作位容量实施增量（2026-11-03，基线 caca3c0）。** 配置归 `WorkLinePosition`；原 `RackJob.admission_order` 仅在本线、本任务、本点决定候选接纳顺序，公共调度在 ENTER 登记 Transport 前统一校验当前任务授权、同点额度及同物理架责任。已登记 ENTER 持续占额，普通排队候选不成为队头的直接交出障碍；只有原 DEPART 有效接纳或可靠 NOT_ARRIVED 依据释放。不同任务、其他 owner 的同点外部责任仍隔离，未提交候选不伪造现场责任。无任务 ROLLER_DRAIN 保留同点单架串行与原 FIFO，不随 capacity 扩成排队池。原任务的结果/离场可继续收尾，失去当前任务授权后不得新增供箱授权或转到新服务面。参见[容量设计](2026-11-03-workline-position-capacity-design.md)和[软件验收](../../testing/smt-manual.md#position-capacity)。

历史状态：2026-11-02，**用户已选择架构与离场接纳完成语义；本文为设计草案，尚未实施、迁移或部署**。本轮仅更新文档，不修改应用代码、不操作数据库、不提交。当前 develop/HEAD 为 064b5c8，现有实现行为仍以源码为准。

## 1. 已确认目标与方案

手工、SMT 自动、BIN_Roller、粗分及后续涉及货架搬运的业务，共用一张货架调度实体表和一个公共推进服务。各业务保留自己的生产条件判断，通过明确接口提交需求、关联原调度作业和提供业务前置依据。公共服务统一进场、换面、离场求值、搬运动作、结果消费、并发去重及恢复。

用户确认并补充：**货架以CTU03或F01离开当前工作线货架工作位，目的地可以是库区，也可以是生产线工作位。两者均属DEPART，WMS/RCS成功接收指令即视为原工作线WES调度完成；后续回调仅补记最终落地位置或失败，允许失败，不回退完成、不阻塞后继、不自动重搬。**

离场由**结束当前工作线货架工作位服务并调走原架的业务动作**判定，不由目的地是否ZONE判定。公共服务沿原RackJob的已确认工作位/原归属、离场业务前置和READY去向登记DEPART，并冻结原意图。限定条件为 `DEPART + RACK_MOVE + (CTU03|F01)`；合法去向包括 `ZONE` 和 `RACK_POSITION`。转运架从本线TRANSFER_RACK直接送生产线工作位属于F01 DEPART，不能因目标RACK_POSITION而改判进场或要求等待最终回调。

DEPART不能仅凭F01/CTU03模板、地址名称或任意RACK_MOVE推断；ENTER及ROTATE保留各自完成规则。离场的明确RACK_POSITION目标必须与原已确认工作位不同，同位换面仍走ROTATE，不能改称DEPART提前完成。目标沿原WMS决定原样冻结，不按生产线编码前缀识别、不新增生产线位置映射/接收业务。以后增加模板或去向类型须显式扩充合同。

此决定覆盖旧文档中“F01/RETURN/TRANSFER/粗分旧架离场无论目标类型均须实际搬离成功”的目标规则。旧文档内已合入代码、旧测试和现场记录仍是其各日期的证据，不因新设计改写为已交付。

| 方案 | 取舍 |
| --- | --- |
| 仅合并存储，各业务继续实现架调度 | 不采用；只能减少表名，仍重复实现动作、WAIT、结果和恢复 |
| **公共调度实体 + 动作明细 + 公共推进服务，业务保留生产条件** | 用户已选择；统一搬运机制，保留真实业务差异 |
| 将所有生产、供回箱和整单流程合成通用工作流 | 不采用；不属于本次货架调度范围 |

### 1.1 第一性原则：点位服务需求、原物理责任、调度交接分别判断

用户补充的统一原则是：**工作点位没有其他待服务任务，就应释放该点位，并为其原货架安排离场。** 点位指本次货架服务的原工作点位，不是整条工作线；其他点位还有任务，不成为留下本架的理由。业务父任务仍为EXECUTING，也不自动构成本点位仍有服务需求。

| 要回答的问题 | 唯一依据 | 对调度的作用 |
| --- | --- | --- |
| 本点位还需要这次货架过程继续服务吗？ | 原业务完整、当前可见的计划/成员、原有效授权及在制上下文 | 有可承接需求则继续服务；无需求则触发原架离场，不等整单终态 |
| 原架/原动作现在能安全交出吗？ | 原身份下可靠位置、原进场/换面结果、直接取放/交接责任及现场互锁 | 决定能否登记离场；未知不被“无需求”抹去 |
| 原工作线调度何时释放完成？ | 原CTU03/F01 DEPART的有效成功接纳 | 完成原调度、释放对应额度/业务占用并安排合法后继；最后回调仅备查 |
| 原业务义务是否全部完成？ | 原成员、报告、释放、完成确认等各自的合同和收妥依据 | 独立决定成员/箱/面/整单及归档；不由RackJob完成代替 |

“无需求”是已核对的业务事实，不是空查询：必须覆盖本点位、本原架过程的全部有效消费者，包括跨版本覆盖、其他应服务面、尚未消费的许可及仍可能产生本架取放许可的请求。原业务需明确给出CONTINUE/NONE/UNKNOWN及原依据；这三个值是当次评估结果，不新增需求表、调度器或跨业务任意回调引擎。可空task_id、父任务终态、pending=false或查不到一条新任务，均不能单独推出NONE。

该评估只能来自显式装配的原宿主，在调用方事务内读取原事实并提供可核对的记录/版本依据；公共服务校验原owner、job及点位关联。不是外部请求可直接声明的NONE字段，也不接受调用方裸布尔值绕过未闭合动作。内部类型按 §11.2 及实施计划固定，不新增WMS wire字段。

只有原架/原过程能合法承接的本点位需求才构成CONTINUE。另一架的候选、另一点位的任务、尚未获准转入本过程的新任务，不应无限占住旧架；也不能为复用在位架改写原owner/task或把新任务悄悄接进已开始离场的过程。其他工作线/业务对货架的需求仍由WMS安排，公共服务不直接遍历各业务私表推断全场资源归属。

NONE成立但仍有原在途取放/进场或位置未知时，保存待释放意图并保留原动作恢复，等直接条件成立再沿唯一原链离场。未开始离场决定根之前，新有效覆盖可在同一锁定事务重查后改变评估；已登记departure首轮后，原链不因新任务重开或改向，后来需求关联后继新作业，避免WAIT期间反复撤销/重建搬运。

原已完成物理动作的到位/移动报告只要求原事实继续可靠交付，不要求货架继续留在本点位。只有仍需本架参与执行、或尚不能确认交出的直接责任才阻离场。原箱/面/整单仍可因报告未收妥保持未闭合，不能因为允许退架就把报告或成员标成完成；本条覆盖旧文档将全部报告ACK作为统一架位驻留门槛的目标规则。

## 2. 当前源码与职责边界

| 现有来源 | 已有能力 | 本设计如何处理 |
| --- | --- | --- |
| `app/models/transport.py` | 一次具体搬运、冻结请求、提交及已应用回调 | 保留为原协议任务，不用新调度表替换，不复制 Sender/Outbox |
| `app/integrations/wms_transport/rack_scenarios.py` | 无状态 RackCycle 句柄、模板映射及 cycle_id 分组，不保证持久步骤先后 | 复用其场景知识；公共服务补齐持久调度，仍调用 TransportService |
| `smt_manual_rack/rack_action` | 手工生产架过程和搬运动作 | 调度状态迁入公共表，成员、人工通知、到位报告等生产事实仍归手工 |
| `bin_roller_rack_cycle/rack_action` | 普通/drain 架周期、CTU01/02/03、额度释放 | 调度状态迁入公共表；供箱、面服务、回箱、FIFO、drain 编排仍归组件 |
| AUTO 两方向 rack_visit 及 rack_action 草案 | 未来来源架/目标架调度 | 改为引用公共作业，不再新增这三张宿主调度表 |
| 粗分 RackPreparation/Action 草案 | 一个补换架方案、旧新架编排和分配代次 | 保留跨旧/新架方案和代次；其搬架过程引用公共作业，搬架请求和消费不另存第二份调度真相 |

公共服务只解释货架调度。是否取尽、全撤回、面结束、原盘/报告是否结清、是否需要补换架，由原业务在同一事务核对并提供原依据。已有单份 `rack.departure_decide@v1` 合同转由公共调度显式消费，不复制注册。

WMS 仍负责库存、货架分配、库区解析和搬运接纳后的安排；ECS/RCS 仍负责实际运动、排队及安全互锁。WES 调度完成不等于物理位置已确认。

## 3. 公共持久化模型

采用一张实体表 `rack_dispatch_job` 和一张一对多动作表 `rack_dispatch_action`。名称按 §11.1 固定。不是一架一行的货架主数据，也不把完整动作历史放进实体大 JSON。

### 3.1 RackJob：一次货架调度过程

| 字段组 | 必须保存的事实 |
| --- | --- |
| 身份/归属 | 本地 id、原 owner_domain、work_line_id/workline_code、rack_id、原业务出处及类型；task_id 允许无值，不强迫公共 drain 等场景伪造任务 |
| 原过程 | 原用途/架型、原 caller、原 transport cycle_id、关联前一过程（如有）、原业务/组件引用；创建后归属和出处冻结 |
| 资源 | 原工作位/配置依据、必要面向、最初进场方案；不能拿后来的配置重写原动作 |
| 推进 | 当前阶段/动作、待核对原因、扫描到期点；待释放意图及当次原点位需求/直接依赖依据，均与本次job关联；业务生产终局不塞入统一调度状态 |
| 终结 | closed_at、closure_reason及原依据；新规则的COMPLETED仅为原离场成功接纳（历史已闭合的独立例外见§11.1），另以WITHDRAWN/NOT_ARRIVED区分未使用或已确认未进入本点位；不把这些终结伪造为物理离场成功 |

本地出处唯一键须含原 owner、业务出处类型/身份及必要轮次，重复同一需求取得原作业。`rack_id` 不设全局唯一；同一架不同进场过程各自保留。不能按当前同码架重绑旧动作或重用旧到位依据。

业务可将多个成员/盘关联同一原过程；关系和生产事实由各自业务保留。不要求这些业务共用任务表，也不建立可清理业务明细到公共表的级联删除。

### 3.2 RackAction：一次原求值或搬运

| 字段组 | 必须保存的事实 |
| --- | --- |
| 归属 | 原 job_id、动作种类 ENTER/ROTATE/DEPARTURE_DECIDE/DEPART、稳定业务出处、原前驱/轮次 |
| 冻结意图 | 原 source/target、rack/face、template、caller、业务前置依据；原请求登记后不改写 |
| WMS 求值 | departure 原 operation/operation_id/message_id、请求/决定及消费时间、WAIT 固定期限/触发依据 |
| Transport | 原 client_request_id/transport_task_id/submit operation_id/cycle_id；只保存引用和恢复必要快照 |
| 完成规则 | 新搬运 `completion_rule=APPLIED_RESULT/ACCEPTED_DEPARTURE`，迁移已闭合历史另见§11.1，登记原搬运时由原业务动作/模板和合法冻结去向确定，之后不可切换 |
| 接纳依据 | 独立的 acceptance_basis/accepted_at，包括经校验的原提交响应；不从 Transport.outcome 的成功/失败推定 |
| 已消费结果 | 原 outcome_revision、结果身份及必要快照，供要求已应用结果的动作判定 |
| 备查关联 | 保留原Transport引用；最终结果/终位由原Transport保存，经公开只读投影关联展示，不要求事件驱动复制到本表；失败不得覆盖acceptance_basis |

一次作业至多一个 ENTER、一个 DEPART 原搬运和一条离场决定根；ROTATE 可多次，但同一次服务需求只产生一次动作。WAIT 后继必须关联原已消费 WAIT，前驱后继唯一；READY 后只恢复原 DEPART，不另问去向。通信未知保持原身份，不产生业务新轮次。

`DEPARTURE_DECIDE` 保存一次求值，搬运 READY 后另外登记 DEPART；二者均在同一动作表，不能用一条记录反复覆盖多轮请求。无可执行目的地时不先造 Transport。

### 3.3 无需求、撤回与非成功进场的完整收口

在办阶段的具体枚举可随实施细化，以下终结和推进语义必须固定；原FAILED/CANCELLED不被改成成功，也不因业务取消假定已停止或已经离开。 尤其原ENTER若冻结允许失败后继续执行的结果更正policy，FAILED即使带明确位置也不证明执行责任已结束；原任务仍可到目标时，保留pending/原点位保护并等待合法更正，不因NONE另起搬运或放入冲突新架。NOT_ARRIVED和安全清架都必须排除这种原继续执行责任。

| 原事实和本点位需求 | 公共作业处理 | 不允许的捷径 |
| --- | --- | --- |
| 从未登记ENTER意图/Transport，也没有本作业取得本点位物理责任的依据；需求已NONE | WITHDRAWN，保留原出处和撤回/未使用依据，停止本作业pending，不发DEPART | 不能为未到位候选补发离场或清除别人的点位投影；不算释放过一份CTU01额度 |
| ENTER已登记，在途/未知，但后续需求已NONE | 留待释放意图，恢复原ENTER；可靠结果到来后按实际位置继续，无生产需求则不为开启生产再问首盘 | 不删原ENTER、不换号重建、不主动新增取消命令，不把无需求当已腾空 |
| ENTER成功可靠在本点位，需求NONE，直接责任可交出 | 登记唯一DEPARTURE_DECIDE，READY后原CTU03/F01 DEPART；有效接纳即COMPLETED | 不等父任务终态或独立报告收妥，不制造取尽/面终局 |
| ENTER的已应用FAILED/CANCELLED仍可靠指出原架在本点位，需求NONE，且原ENTER无可继续执行/更正责任、无更高位置冲突/未决核对或直接执行责任 | 不补认ENTER生产成功；以原结果位置及业务无需求/交出依据进入同一安全离场链 | 不能只因ENTER非SUCCESS永久留架；也不能用未知位置或普通失败码猜在位 |
| ENTER原执行责任已可靠终结、无仍可继续执行/更正或未决核对责任，可靠终位不在本点位，且原检查确认无其他受管点位/角色冲突 | NOT_ARRIVED，记录原取消/失败和实际位置，只结束本作业的原预留/待办，无需DEPART | 不把原生产需求/目标到位判为满足，不清除另一作业/点位责任 |
| 可靠位置落在另一受管点位、存在更高冲突事实/未知，或业务评估UNKNOWN | 保留原阶段和待核对原因；无新生产/搬运许可，沿原事实恢复 | 不静默解除其他角色保护、不为完成模型补造进离场，不把UNKNOWN当NONE |
| 还有本点位可由原过程继续承接的有效任务/成员 | 保持原作业服务，按合法需求服务本面或换到下一面 | 不能只因某一成员完成/撤回就整架离场 |
| DEPART已被有效接纳，随后有新需求或晚到原结果 | 原COMPLETED保持；新需求按其原出处创建/关联后继作业，结果仅备查 | 不回占额度、不重开旧作业或重新发一次旧离场 |

COMPLETED/WITHDRAWN/NOT_ARRIVED是不同的终结原因。已终结作业不因后来有新需求复活；新合法出处取得自己的新作业，不继承旧取消服务或旧进场成功。迁移保留历史原状态和引用；历史WITHDRAWN中若仍有关联有效服务，应先核对原关系，不能用“新需求出现”覆盖旧结论。

未发ENTER的drain候选在提前排空时按WITHDRAWN/未使用收尾，本批可依原drain合同完成；已经登记ENTER的候选仍持有原责任，不能因FIFO变空跳过其到位/离场。手工原ENTRY_CANCELLED及本点位取消后安全退架分别映射上表，不把原手工取消补偿机械套为所有ENTER成功。新统一无需求退架要求适用于各真实消费者，但各自生产资格仍不以取消/失败补认。

## 4. 离场：成功接纳即完成

### 4.1 成功接纳的可执行定义

沿现有 Transport 原协议，WES 以自己可靠保存的 WMS 提交响应为依据：

- 原任务/client_request_id/caller/owner、原冻结 kind/template/target 和原 submit 身份一致。
- 原提交响应可用且校验有效，回显原 transport_task_id。
- 精确组合为 `HTTP 202 / RECEIVED` 或 `HTTP 200 / DUPLICATE`；后者是原成功接纳的技术重放。
- 原业务动作确为DEPART、Transport为RACK_MOVE、模板为CTU03/F01，沿原工作位及合法READY去向登记；目标可为ZONE或生产线等其他RACK_POSITION，不能与原明确工作位相同。

不增加 WES 直连 RCS 的第二发送路径。RCS 接纳由现有 WMS 接口表达，不能要求 WES 等第二份 ACK。

`rack.departure_decide.READY` 只授予去向，命令已创建、Outbox.DONE、任意 HTTP200、回调接收 HTTP202 或设备在线均不等于上述成功接纳。特别是现有 `settle_submitted` 可因收到回调把提交消息置 DONE，该状态不能充当 ACK。

### 4.2 接纳事务与后继

首次取得上述依据后，在原宿主的一次推进事务内同时：

1. 固定 acceptance_basis、accepted_at 及调度完成时间。
2. 完成该 DEPART 和 RackJob，停止把最终回调等待列为调度 pending。
3. 关闭 WES 对原架过程的调度占用/请求额度；其他业务生产或报告义务仍按原合同单独判断。
4. 满足其他既有业务前置时，登记后继新架请求、推进粗分补架或其他原工作，不再等旧架最终落地。

这是原工作位的业务交接，不写入“货架已物理离开”或虚构终位/离场物理时间。新架进场可登记，但只有其自己的可靠到位结果才获得生产资格，实际运动顺序由 WMS/RCS/ECS 保证。

原盘/入箱命令尚未闭合、取料授权尚未收口等发起离场前置仍有效；接纳即完成不能跳过这些条件。NONE触发离场不要求把业务成员/面/整单提前标成完成；那些业务仍可等待独立报告收妥。原任务整单条件也不会因一个架调度完成全部自动满足。

### 4.3 回调仅备查

| 成功接纳后的回调情况 | 处理 |
| --- | --- |
| SUCCEEDED，有可信终位 | 原Transport按框架规则保存最终落地位置及原版本，公共动作按引用关联展示；调度仍已完成 |
| FAILED、位置未知或 CANCELLED | 原Transport按原框架规则保存实际结果并供关联备查，接受失败，不重开调度、不恢复额度/占用、不自动重搬 |
| 最终终位仍为原工作位 | 保存这一历史落地事实，仍不撤销原接纳完成、不阻断已合法后继；不能把这条旧结果改作新过程的当前位置 |
| 长期没有回调 | 不阻塞任务、后继或离场调度完成；没有最终落地记录按缺失显示，不猜位置 |
| 重复、迟到、错任务/错架、版本冲突 | 沿原接收器与版本规则，不越过关联校验，不修改其他架/新过程，不制造第二动作 |

“接受失败”不把 Transport 的 FAILED 改成 SUCCEEDED，不放开框架终态覆盖，也不新增回调状态。调度状态和 Transport 实际结果可以分别为 COMPLETED、FAILED。

接纳后的回调可以沿原框架通知唤醒原 owner，但其业务效果仅为幂等补记；不能因这次唤醒重开调度或撤回后继。备查不新增用户告警/自动修复流程；既有原始协议诊断保留。

### 4.4 接纳尚未明确、回调先到及公开读口

未取得合法成功接纳，提交拒绝、未知、缺失或无法关联时，不应用本节的接纳完成规则；恢复/核对原身份，不因失败可接受就当作已接纳，不自动创建替代任务。未可靠取得 ACK 的回调仅作为原结果保存，不反推一个成功接纳响应。

当前 `get_result` 同时投影提交与应用结果，结果整体 INVALID 或回调先到导致 settle，不能让已可靠保存的原 ACK 依据丢失。实施须提供**独立校验的公开提交接纳读口/子投影**：先核对原任务/提交/frozen/身份，再给出原有效 ACK，不依赖当前 outcome/pending 是否有效。公共调度不直接查 ReliableMessage/TransportTask。

成功ACK必须有可恢复的持久来源，公共动作一旦消费就保存必要快照。**可靠发送返回的真实成功ACK即使晚于回调settle，也必须能保存，明确纳入本次实施范围。** 当前settle清除lease、原finish仅接受SENDING及匹配lease，会丢弃这种迟到ACK；仅复用当前finish不足以交付新规则。

目标在原TransportTask增加独立SQL NULL可空JSON `submission_acceptance`，保存第一次合法成功ACK的原消息ID/目标/submit身份、原请求摘要、attempt身份、HTTP/code/回显task_id/原ACK时间及保存时间和原响应必要内容。普通按当前lease完成的成功响应及因settle失去lease后的迟到成功响应，都可通过**注入的证据写入端口**核对原claim/message/请求字节与任务冻结身份后填入该字段；可靠层不得导入业务/Transport模型。具体端口命名留实施，但保留成功证据是必交行为，不把是否丢弃留给实施者选择。

该证据观察只接受真实HTTP返回且原协议严格验证为成功接纳的响应，保持原任务及首次依据不可变；相同响应幂等，矛盾响应保留核对而不覆盖原成功。它与发送状态回写分开：不放开原finish的lease围栏，不复活DONE，不改TTL/重试/attempt计数，不把失败/未知结果变接纳，也不再发送已settle的请求。首次新增可信证据按原owner在提交后唤醒，重复不唤醒；这是新的接纳证据通知，不冒充失效lease的普通on_delivered回写。

公开接纳投影优先验证这一原任务内独立证据，历史已有有效submit响应也可验证读取；不能从查空自动回填。历史任务该字段初始NULL，且不能凭回调/当前DONE合成旧ACK。ACK实际丢失且仅有回调、或旧消息已清理而尚无充分依据时，保持核对，不用FAILED/口头说明补造。上述增量只保存证据，不授权重发已settle的运输请求。

### 4.5 接纳证据与发送状态的事务边界及框架锁序

采用明确的先后顺序：**HTTP原成功响应完成严格校验 → 独立事务保存Transport接纳证据并提交 → 原ReliableStore.finish按既有lease围栏处理发送状态。** 证据端口在进入finish的消息写事务之前调用，不能挂进已持有ReliableMessage行锁的on_delivered再写TransportTask。

证据事务只锁原TransportTask；核对原claim/message/冻结请求所需的不可变身份可只读，不取得ReliableMessage、WorkLine、RackJob或宿主业务行锁。证据提交后才通知原owner，宿主在另一个事务消费。现有回调仍按TransportTask→ReliableMessage完成settle；原finish只锁消息并保留通用WMS结果通知，Transport接纳唤醒由首次独立证据提交负责且仅一次，禁止从其中反向锁写TransportTask。因此框架两条路径不会形成“发送持消息等任务、回调持任务等消息”的环。

普通成功ACK和因settle丢失lease的迟到ACK都先走同一证据端口。端口对序列化/死锁类可重试存储失败，以原HTTP响应及原身份最多3次新事务重试；提交结果未知先重读原证据核对，不重新发HTTP。数据库无法持久保存时不发布成功证据或声称调度完成，保留原失败/核对事实；原finish若仍可按lease保存响应则是既有可读来源，若已settle且没有可恢复ACK则仍待核对，不补造。

证据已提交但finish尚未完成或进程停止时，前者仍是真实接纳依据，finish/原消息恢复继续遵守原技术幂等和lease规则；证据写入不自行将消息复活、回退或启动第二次发送。由此明确区分“真实ACK证据持久化”和“该次发送状态能否回写”，不修改callback事务为等待原owner消费。

## 5. 其他动作及结果更正的范围

| 动作 | 调度完成依据 |
| --- | --- |
| ENTER，任意模板/目标 | 原已应用成功，业务所需实际工作位/面向及核对条件满足 |
| ROTATE/换面 | 原已应用成功、原架/工作位/目标面匹配 |
| DEPART，CTU03/F01，目标ZONE或不同的RACK_POSITION（含生产线工作位） | 第4节有效成功接纳；最终落地只备查，不等生产线到位回调 |
| 其他未明确采用接纳完成规则的动作/模板 | 原已应用成功及必要条件满足；不能仅按去向或RACK_MOVE类型套DEPART规则 |
| BIN_MOVE/BIN_EXCHANGE、ECS 命令 | 不属此次货架调度完成规则，继续各自原合同 |

拟议 `F01_RESULT_CORRECTION_V1` 与本节完成规则是两件事。离场按接纳完成，不再依赖失败→成功更正，不为此动作启用更正 policy；普通回调保持框架原严格终态，已经记录 FAILED 后不因允许备查失败而接受任意高版本 SUCCESS。

粗分新架进场等确需物理结果且在更正合同范围内的动作，仍可按独立更正设计冻结新任务policy；该能力仍未实施。不能把更正能力当全部共享调度的前置，也不反向修改原任务已冻结 policy。

## 6. 公共服务、装配和业务适配

目标使用 `app.businesses._components.rack_dispatch` 静态组件，持有上述公共模型和唯一 departure 请求声明；不是新业务 code。每个真实消费宿主显式声明该组件并组装一个绑定原 owner 的 RackDispatch 服务，components 映射提供真实服务对象，不用占位对象。BIN_Roller 不嵌套声明组件，由宿主同层声明并注入同一实例。

公共门面提供原需求登记/原作业关联、进场/换面/离场请求、只读进度以及有界 pending/advance。精确签名见实施计划 Task 2，但所有入口使用调用方事务；只读接口不写状态或取得新许可。无独立 Sender、Celery 任务、Beat、报警表或通用节点引擎。

departure 声明从 BIN_Roller 原 request 模块迁至公共组件时，原模型/operation/规范化和历史 owner/business_key/消息字节保持；全部真实消费者在同次变更中更新显式 contract_consumers 和组件映射。业务不因 import 获得发送权限。

| 业务 | 留在原业务中的判断/编排 | 委托公共调度 |
| --- | --- | --- |
| smt_manual | 任务、原成员/精确撤回、人工完成、到位报告独立待办和整单资格；提供原点位需求/直接交出依据 | RETURN/TRANSFER的原架作业、全部搬运动作及无需求退架 |
| smt_auto | 计划、授权/盘/报告、来源取尽或全撤回、箱/面终局及归档 | 两方向来源/目标调度；盘/成员持久引用原 RackJob/RackAction |
| BIN_Roller | 扫码、供箱/面服务、回流/FIFO、drain 批次和本线额度配置 | 普通/drain 架的 CTU01/02/03；按共享原作业计算额度，不保留另一份周期状态 |
| coarse_sorting | 单盘作业、分配代次、旧新架方案及已创建入箱的直接前序 | 原旧架/新架的各自 RackJob；旧架接纳完成后可登记新架进场，新架仍等到位 |
| 后续真实业务 | 自己的需求、前置和业务完成条件 | 同一公共门面，显式装配，不新增私有架调度表或手写搬运恢复链 |

原业务 after_commit 唤醒、同一 scan_pending 及5秒Beat继续使用。公共服务pending按原owner/工作线分页，任务终态或本线当前方向不能隐藏旧责任；离场完成后不为等备查回调无限扫描。框架回调负责保存原结果和原通知；这里不要求owner事件驱动复制结果到已完成公共动作，也不扫描已完成job补记。

粗分无待服务单盘且无仍可能授权本架的分配/取放责任时，也由原宿主评估NONE并释放自己的原在位架，不要求先收到REPLACE_REQUIRED或已有incoming_rack。该空闲退架只走原公共RackJob的离场链，不伪造RackPreparation、新架分配或容量结论；后来新盘仍须由WMS明确分配/补架。BIN_Roller同样按原供箱/面服务/回箱/drain上下文判断，不把通道瞬时为空当作原在途责任已消失。

### 6.1 调度待办与业务报告待办分开扫描

公共RackJob完成后pending=false，不再等待最终落地回调；原到位/移动报告仍是原业务的直接义务，由其自己的原请求表发现和消费。手工明确保留本地RACK_REPORT候选类型：以原ManualRequest.operation_id为record_id，查询kind=RACK_ARRIVAL_REPORT且state尚未CONSUMED的原请求，限定原owner/task/workline，不以公共job未完成、当前任务或当前配置仍有效为筛选前置。

RACK_REPORT参与同一个宿主scan_pending的只读RR上界/游标和公平轮换，每条在宿主独立事务内按原请求/原message_id读取WmsRequests结果，严格核对身份及RECORDED/DUPLICATE再消费；未知/冲突保留原请求，不换号、不补报到位、不要求重新搬架。它是具名业务待办，不新增Celery/Beat、公共扫描器、通用请求引擎或业务状态副本。

原请求的rack_work_id/action_id迁移为稳定的公共job/action关联或确定的原ID映射，原task/workline、实际到位时间、原请求快照及message_id均保留。原动作因离场已完成、工作线被禁用/删除/改绑，均不阻这条历史报告消费；要读取公共原依据时经只读门面，不直接查询公共组件私表。其他业务若有同类独立报告，同样从自身原请求/盘记录派生候选，不依赖共享调度pending。

未收妥报告继续参与原成员/箱/面/整单闭合和归档判断；“本点位NONE可退架”不消费报告或伪造业务完成。公共调度完成、原到位报告已创建、报告已收妥、父任务已完成，分别保存各自事实。

当前无独立结果ID通知；本设计确定将备查原结果仅保存在原Transport，以公开只读查询按原动作引用关联展示，避免新增已完成job扫描/回调反查业务表。具体为：公共动作保存原Transport引用，备查展示直接读原框架；没有公开结果则显示缺失。不得承诺原框架清理后仍有未曾保存的无限档案；需要长期保留的终位/失败快照，随框架保留策略另行明确，不以可靠消息90天清理误删仍有义务的调度依据。

## 7. 相容准备、前序与并发

同一 owner/工作线/原目标架过程，多个盘要求相同且尚未完成的目标准备时，关联同一原 RackAction，不以各盘不同决定ID各建一次 ROTATE/ENTER。已经有矛盾准备时保留各自授权和原需求，等待/核对，不覆盖原目标、不并发派发冲突动作。各盘保存实际依赖的原 job/action，而不是完成后按同码最新架猜配。

点位需求评估、直接交出检查与首次departure根登记处于同一锁定事务，真实读取原任务/所有关联成员和原许可/未决取放集合，并把本轮NONE及原依据冻结到该根。没有新表记录一份可独立改写的“当前任务计数”；原业务必须证明评估覆盖完整，尚未装配或读取失败返回UNKNOWN，不按零处理。接纳完成及释放投影只作用该原job；后到同位新任务/新架不被旧NONE依据或旧回调清除。

粗分一次补换架仍是旧架和新架两个公共作业；新架动作前序引用旧架的明确调度完成依据。旧架CTU03/F01 DEPART无论去库区或不同的具体工作位，均以原成功ACK作为调度完成依据。不是把两个架号塞进一条 RackJob，也不凭一个 job 完成认定整个 Preparation READY。

所有可能登记相互冲突的新放料/取料、目标准备和架动作的完整推进入口，应采用同一局部 SERIALIZABLE 及相互依赖的真实读集合；包含 outbound 自有目标架与盘流程，不能只提升公共搬架侧。receiver 保持原事务入口时，需通过共同原行加锁/写入变化检测证明交接，不能只凭标签宣称并发安全。

总体锁序按 WorkLine（多线时ID顺序）→ Devices（ID顺序）→ 相关公共 RackJob（ID顺序）→ 宿主业务主体（固定类型/ID顺序）→ RackAction。锁前仅定位ID；原行和直接依赖锁后重读。接入时须检查现有宿主/组件已持有的锁，必要时同次调整调用顺序，不在持有业务子行后回头补取前面的锁。不得为刷新快照更新工作线配置版本。

原决定消费、共享关联、动作登记、Transport/Outbox引用和下一意图同事务。40001含commit最多3次新会话整笔重试；耗尽保留原待办。提交未知重读原出处和外部身份，不能换号。公共服务不持锁等待网络或物理执行。

接纳完成允许安排后继，不撤销发起离场前的业务/安全条件；不新增物理占位许可或全局rack_id锁，不用旧落地回调把新过程的位置投影回退。

## 8. 迁移、历史与保留

本设计新增公共模型/装配，并迁移现有手工、BIN_Roller 的调度事实；不是继续执行AUTO“只增14张自有表且不动组件”的旧范围。AUTO未来改用公共引用，其任务/生产事实仍独立；粗分相应减少自有搬运动作重复状态。新总表数以实际实体依赖为准，不用旧14/7/10数字作为目标。

迁移只在单一Alembic链上进行；已有生产义务、Feed/面服务、drain、成员及请求引用必须完整映射。保留原 owner/caller/operation/Transport/client_request_id/cycle_id/冻结字节/期限/结果版本和原具名ID；公共内部主键可映射，须提供确定、可验证的原记录关联。检测到原ID碰撞或引用歧义时先拒绝迁移，不能临时猜测或重新下发。

原TransportTask的submission_acceptance增列随同一迁移链交付，历史为SQL NULL，不重算原frozen/input_digest。公开接纳投影及发送证据端口须同版部署；降级若会删除尚在使用的接纳证据，必须先拒绝，不能清空字段换取旧代码可运行。

保留已闭合历史结论及其真实依据，不把过去按旧合同等待/失败的记录伪装成过去已经完成；未决离场可在升级后依据原已可靠保存的合法ACK按新规则首次消费，记录实际升级后消费时间，不改原Transport。已完成历史记录不被重开；未决转运架去生产线工作位等原DEPART，也可依据原真实ACK首次按新规则完成，不改原请求或伪造过去的消费时间。

新架调度公共记录的保留独立于各业务任务清理。本轮不新增公共RackJob/RackAction自动清理器，避免调度完成后仍被原业务报告引用的依据先行删除。AUTO inbound仍可按原直接业务义务收口后7天清理生产明细；公共调度未决时保留恢复必要身份/快照，不用task级级联删除。成功接纳的离场没有待最终回调的直接调度义务，缺回调或回调FAILED不阻止该任务按原其他条件归档；未收妥业务报告仍阻其所属业务清理，不能伪装成备查回调绕过。

已有 `recovery_only` 的必要语义转由公共作业的独立保留承担，不要求保留另一份AUTO私有visit/action。旧TaskOwnerRef和归档guard继续指原业务摘要；新备查结果不重开task、不刷新7天窗口。

有损降级必须在删除公共数据或恢复旧约束前拒绝；双写两套调度真相不作为长期兼容方案。上线需全部API/worker完成同版装配后再启用新消费者，不混用旧私有调度与新公共推进派发同一原动作。

## 9. 验收要求与当前边界

| 场景 | 必须观察到的结果 |
| --- | --- |
| 手工/自动/BIN_Roller/粗分及新增真实消费者 | 同一公共实体/服务，原owner独立、原归属不转移，无私有第二条搬运链 |
| CTU03/F01 DEPART→ZONE或不同RACK_POSITION，202 RECEIVED/200 DUPLICATE | 一次完成原动作/作业、释放原额度/业务占用、原子推进合法后继，无最终Result也完成 |
| TRANSFER_RACK→生产线工作位（RACK_POSITION），F01 DEPART | 有效ACK完成原工作线调度并释放原作业；目标明确且不同，不等待生产线到位回调、不变成ENTER；接收侧真实到位资格不由该ACK授予 |
| F01 ENTER到原工作线/同位ROTATE，或伪装DEPART到原同一工作位 | 前两者仍等各自已应用成功/实际面向；同位伪装离场不授接纳完成，不新增指令绕过 |
| 上述动作接纳后FAILED/UNKNOWN/CANCELLED/缺回调/原地终位 | 仅留原实际结果备查，不重开、不反占额度、不自动重搬、不回退新架 |
| 相同模板但ENTER/ROTATE/箱或ECS动作 | 不能套接纳完成；保持各自原结果/真实到位要求 |
| 无有效ACK、422/409拒绝、通信未知、回调settle后的DONE | 不伪造接纳；原身份核对，不新建替代任务 |
| ACK/回调先后、迟到ACK、结果INVALID、原消息清理 | 真实成功ACK即使晚于settle仍保存原Transport独立接纳证据，不放开旧lease状态围栏/不重发；独立读口可消费，已保存依据不因结果失败或清理丢失 |
| 粗分旧架接纳、新架到位仍未决 | 可登记唯一新架进场；Preparation不提前READY，各盘不提前PUT |
| 两盘相容/矛盾ROTATE或REPLACE | 相容只一份实际准备，矛盾不并发派发，各盘固定原关联 |
| 放盘登记与架离场/换面交错 | 真实相关集合/锁序/局部隔离下不漏直接前序；40001新会话整笔恢复 |
| 本点位无其他可承接任务，但整线/父任务仍执行或另一点位有任务 | 本架在直接交出条件满足后唯一离场，不等整单终态、不被无关任务占住；不提前关闭原成员/报告 |
| 本点位还有其他有效版本/面/授权，或评估UNKNOWN | CONTINUE/UNKNOWN阻止错误的无需求离场，不以一条任务取消、空查询或读取失败宣告NONE |
| 粗分原架无待服务盘/潜在分配责任，也无补换架方案 | 安全交出后走原公共离场，不等待REPLACE_REQUIRED、不伪造新架/Preparation；后来需求由WMS重新明确分配 |
| 未登记ENTER的供箱撤回/drain未使用候选 | WITHDRAWN停止本作业待办，不发DEPART、不占/反复释放一份CTU01额度，不清别人点位 |
| 已登记ENTER后无需求；原成功/失败/取消结果仍可靠在本点位 | 先收原位置/直接责任，再按统一无需求规则离场；不重建ENTER、不补认生产成功或伪造首盘 |
| 原ENTER按冻结policy可在FAILED后继续执行，当前终位明确但尚未更正 | 保留原ENTER待办/点位保护，不因NONE终结NOT_ARRIVED或发冲突离场/新架；只消费原合法更正，不能新建ENTER |
| 原ENTER取消在其他非冲突位置/另一受管点位/位置未知 | 分别NOT_ARRIVED结束原预留或保留冲突/未知保护，原需求不被伪造完成，不随意搬他人点位的架 |
| 新ACK证据与回调settle/finish并发，证据写入失败或提交未知 | 独立证据事务在消息写锁之前，不出现Task/Message反向锁环；失败不发布虚假完成，原lease围栏和原HTTP身份保持 |
| RackJob已完成但手工到位报告仍REQUESTED，后来RECORDED/DUPLICATE | 同宿主RACK_REPORT候选继续消费并解除原报告义务，原job不重开/重搬；通知丢失由同一Beat恢复 |
| 报告尚未收妥而调度已完成，父任务尝试完成/归档 | 原报告仍阻所属业务完成/清理；不借点位释放或公共job完成抹掉报告 |
| 外层回滚、提交未知、通知丢失、任务终态/切方向 | 无半份动作或完成；原业务pending和5秒Beat恢复，外部身份不变 |
| 迁移手工/BIN_Roller填数及公共读口 | 原具名ID/关系/期限/历史结果全部保留；不新增外发，不因迁移重放 |
| inbound清理与独立调度/备查 | 直接义务不丢失；已完成离场的缺/失败回调不阻清理、不重开摘要 |

这些是未来测试，未执行。实施按§11决议及配套计划验证；不以现有基线测试通过证明新调度、迁移或接纳语义已实现。真实WMS/RCS接纳后排队/安全交接和现场执行须另记证据，不能从原工作线WES调度完成推断已到库区或生产线工作位。

## 10. 独立设计检查

前轮独立实施者审读提出两项衔接问题：回调先到使原finish丢弃迟到真实ACK，以及备查结果复制与直接公开查询两种责任冲突。已分别在§4.4明确独立接纳证据持久化且不放开lease状态围栏，在§3.2/4.3/6统一为原Transport保存、按原引用公开只读展示。该结论仅是当轮设计检查，没有产品或现场测试证据。

2026-11-02用户进一步纠正离场范围：去向并不限库区，转运架可从本工作线货架位直接送到生产线工作位。已将定义和接纳完成规则改为业务DEPART及原工作位上下文，覆盖ZONE/RACK_POSITION；补充直送生产线、ENTER/ROTATE及同位伪装离场的验收，原ACK证据和只读备查设计保持。该轮修订仍为文档，未实施。

本轮从“点位无其他可承接任务就应释放并下达离场”重新推导，将点位需求、直接执行责任、离场接纳、业务履约分开。此前评审的三项缺口分别落实为：§4.5明确独立接纳证据先提交、禁止Message→Task反向锁；§3.3补WITHDRAWN/NOT_ARRIVED及已知在位非成功ENTER的安全离场；§6.1以原请求派生独立RACK_REPORT待办，不让公共调度完成隐藏原报告。旧整单终态/独立报告ACK驻留门槛同步修订，但原业务完成和归档条件仍保留。修订后仍须按§9验证实现与并发；当前只有设计和文档检查。

本轮独立复核另发现非成功ENTER收口与结果更正的交叉边界：明确FAILED位置不能代替原执行责任终结。§3.3及验收已补充，允许原任务继续执行/更正或仍有未决核对时保留原保护；不能提前NOT_ARRIVED、下发冲突离场或许可后继新架。

修订后的独立实施者窄段复核未发现继续阻碍实施规划的重要问题；这是设计可规划性的结论，未验证新实现、数据库并发或现场行为。文档结构检查覆盖本分支19份改动文档：192个JSON示例可解析且本轮未改报文，785处本地链接无新增失效，10处旧验收附件缺失引用保持原历史边界。本轮只改文档，未运行产品测试、迁移或对外请求。

## 11. 2026-11-03 实施定稿

以下约束覆盖前文拟定名称及在办阶段的开放选项。当前实施范围是手工和 BIN_Roller。AUTO 与粗分后续生产阶段只使用此合同编写设计和计划。

- 公共组件为 `app.businesses._components.rack_dispatch`，唯一调度真相为 `rack_dispatch_job` 和 `rack_dispatch_action`。
- DEPART + RACK_MOVE + (CTU03|F01)，目标为 ZONE 或不同的 RACK_POSITION，精确接纳组合为 HTTP 202 / RECEIVED 或 HTTP 200 / DUPLICATE。
- ENTER/ROTATE 的生产资格必须来自原动作已应用成功及匹配的实际位置/面向。
- WorkLine（ID顺序）→ Devices（ID顺序）→ RackJob（ID顺序）→ 宿主主体（固定类型/主键顺序）→ RackAction（ID顺序）。
- PostgreSQL 的 SERIALIZABLE 或 REPEATABLE READ 在第一条 SQL 前设置，40001 含 commit 最多 3 次新会话整笔重试。
- 原 owner/caller/cycle_id/client_request_id/transport_task_id/operation_id/message_id/请求字节/期限不变。原 after_commit、同一 scan_pending 和 5 秒 Beat 恢复。
- 不新增 Sender、Outbox、Celery、Beat、需求表、全局架锁、物理占位许可、清理器、告警或对外 API。

### 11.1 原架、动作和历史结论

作业原出处按 (owner_domain,work_line_id,source_kind,source_ref,rack_id) 唯一，五项非空；source_ref 编入原轮次。来源类型是 MANUAL_RETURN、MANUAL_TRANSFER、ROLLER_ORDINARY、ROLLER_DRAIN。普通手工 RETURN 使用触发该轮的首个成员 id，TRANSFER 使用原目标计划出处（首轮任务标识），普通 roller 使用 demand_id，drain 使用原决定 id，同一 drain 中按原架区别。重复已闭合出处只返回历史；新增真实授权使用新出处。保存独立 original_cycle_id 和不可变 caller，允许无 task 的 drain。早期无资源候选保留未知，派发前以原宿主批准的完整资源一次冻结。使用字符串 job/action 本地主键；不以 rack_id 或当前配置作历史身份。

Job.state = ACTIVE / RELEASING / COMPLETED / WITHDRAWN / NOT_ARRIVED。ACTIVE 可含 release_requested、review_reason；首次合法 decide 根使状态 RELEASING，禁止再接生产，直至终结。终态 pending=false。正常 COMPLETED 仅限 closure_reason=DEPARTURE_ACK，并保存严格 ACK 和消费时间；迁移独占例外 closure_reason=LEGACY_CLOSED，completion_rule=LEGACY_RECORDED_CLOSURE，保存原表、原 id、原状态、原 release_basis、原 outcome_revision/时间等实际存在的依据。该历史分支既适用于手工 DEPARTED 也适用于 roller RELEASED，包括仅以 SUCCEEDED 闭合、缺原 ACK 的情况。移植已闭合行不重新计算或要求 ACK；无原时间保留 NULL，独立记录迁移出处，绝不将迁移时间当原完成时间。新建动作和历史未决动作禁止选择旧规则。历史歧义或缺少关联拒绝迁移；历史已确认结论与无 ACK 分开保留。

Action.kind = ENTER / ROTATE / DEPARTURE_DECIDE / DEPART。phase = REGISTERED / WAIT / READY / APPLIED / ACCEPTED / REVIEW / LEGACY_CLOSED，原 FAILED/CANCELLED 等 outcome 及 revision 独立于 phase。decide 的 completion_rule 为空，ENTER/ROTATE=APPLIED_RESULT，DEPART=ACCEPTED_DEPARTURE（上述历史特例除外）。WAIT 仅 decide 且 due_at_ms 非空；READY 仅 decide 且保存严格决定；ACCEPTED 仅 DEPART 且原证据非空；APPLIED 仅 ENTER/ROTATE 且有结果版本，不据此单独授予生产。动作到 job 的 FK RESTRICT，前驱为同一 job 的动作（组合 FK）；唯一 (job_id,kind,source_ref)。ENTER/DEPART 分别按 job 单例；以 WHERE kind='DEPARTURE_DECIDE' AND previous_action_id IS NULL 对 job_id 建唯一索引，非空 WAIT 前驱上以 (job_id,previous_action_id) 部分唯一，补齐 SQL NULL 不去重的漏洞。后继仅在前驱是已消费 WAIT 后登记。DEPART 引用原 READY。对非空 transport_task_id/client_request_id/submit_operation_id 及非空 (operation,decision_operation_id) 各设唯一。pending 查询索引 (owner_domain,work_line_id,pending,id)；另按 (owner_domain,work_line_id,source_kind,pending,id) 扫宿主类别。SQLite/PostgreSQL 同一谓词。任务没有级联删除权。

同一 job 锁内按 kind、原点位、规范化目标和目标面核对在办准备动作。相容 ENTER/ROTATE 返回同一 action_id 供宿主关联，不相容保留原授权待办。已完成的原 source_ref 重查只返回原动作；不同轮次后来回同面仍可登记新 ROTATE。原前驱身份和冻结请求一经登记不改写。读取原可纠正/继续执行责任、未决结果、更高原观察及其他受管点位的冲突。原 ENTER **或 ROTATE** 非成功、需求 NONE、已可靠终结且在本点位并可交出时，只登记一次安全离场；仍保存原 FAILED/CANCELLED，不授生产、不重转。ROTATE 在外部位置不使用未到场终局；位置/责任不确定时保留保护。NOT_ARRIVED 只适用于已终结、可靠不在场且无其他受管点冲突的 ENTER。

### 11.2 宿主原依据及事务入口

原宿主提供两个独立的冻结值 PointDemand、DirectHandoff，同一 PointRef(owner_domain,work_line_id,job_id,rack_id,point)；每份列明覆盖的原集合、已读记录的稳定键及版本（无修订号时保存决定该责任的状态字段/身份快照），连同当前 Task.plan_revision/received_work 等集合边界。空集合必须有已读范围和所属锚点。PointDemand.state=CONTINUE/NONE/UNKNOWN。DirectHandoff.state=CLEAR/BLOCKED/UNKNOWN。NONE + CLEAR 仍须共享服务自行检查原动作/位置/前驱。主键/版本/关联不完整或不同点位是 UNKNOWN；两项仅从宿主在同事务查询得到。根上保存当时两份依据及实际工作位，不接受外部 payload 自证。WAIT 到期或原合同允许的已验证依据变化只登记唯一后继，READY 不改向。原报告收妥独立于 CLEAR。

| 宿主 | 点位需求 | 直接交出及报告边界 |
| --- | --- | --- |
| 手工 RETURN | 同任务所有有效版本及可绑定本架的未绑定 DIRECT 成员、精确撤回、各目标面、未应用/缺版本通知 | STARTED 人工执行、直接许可、原 ENTER/ROTATE、位置冲突保护；完成通知按 face/revision 或 through_revision 覆盖。已物理完成的到位报告另归 RACK_REPORT |
| 手工 TRANSFER | 原 target_rack、所有关联直接及箱成员、待生效计划/初次计划等待、许可/箱分配评估是否仍可产生本架作业 | 核对原箱/面在制、已准人工取放和相关完成通知；不能把整单 EXECUTING 或独立报告待收妥当作占点理由。源数据不能区分交接时返回 UNKNOWN |
| 普通及 drain | 原 FaceWork 的跨版本有效面、可承接未绑定面、尚可产生许可的供箱/回流/opportunity/batch、原 drain 决定/候选/未用架、原有效 FIFO/后继 | 原 FeedSegment、ReturnBatch、已发取放或回流执行、在途扫描轮次及位置责任须闭合；消费收妥独立于当前通道是否短时为空。新需求不得重开 WITHDRAWN |
| 后续 AUTO / 粗分 | 两方向原计划、授权/盘/箱/面、潜在目标准备；粗分原分配代次、待分配盘与补换架 | 核对原在途取放/命令/位置；独立报告仍阻原业务归档，粗分可在无补架方案时安全退旧架。宿主未实现时不装配占位消费者 |

手工唯一 advance 的 TASK、MANUAL_RACK、SCAN、FEED、RETURN_BATCH、RACK_CYCLE、DRAIN 使用 SERIALIZABLE；MANUAL_BIN 可消费入站许可，MANUAL_COMPLETION 可登记放行并解除原箱责任，因此两者也使用 SERIALIZABLE。SCAN2_RELEASE（既有命令结果）、新增 RACK_REPORT（只收妥原请求）为 REPEATABLE READ。候选 sweep 为只读 REPEATABLE READ。无需修改其他业务或 receiver 的隔离。锁前只定位 id/范围；TASK 分解 TaskRepository.original，roller 分解 original_record(for_update=True)，先锁线/设备，再锁关联作业，再依次锁 task → input → member → manual_bin → manual_request → roller scan_arrival → face_work → feed_segment → opportunity → return_batch → drain decision（同类按完整主键），最后动作。不能从已经持有 task/face 的 helper 补锁既有 job。新 job 插入在原许可边界下，同一事务返回；遇到锁集合变化，撤销整笔，重新定位。

receiver 对同一原任务的计划、撤回和人工完成写原 Task.received_work，包含 operation + 原 id，幂等。若 receiver 变更可影响无任务 drain/已关联 roller 作业的现场事实，在同一事务按上述顺序锁相关 job 并递增其 facts_revision（仅新事实），再写原扫描/生产事实；重复 receipt 不增。需要定位/重读关联，不能在写 ScanArrival 后再补锁。尚无原 job 时，派发方须锁并实际重读相同原生产事实/原任务或 drain 锚点；并发创建与无归属新事实不能因两个事务各读空集合而通过。对不能以共同原行建立顺序的直接许可，只在原宿主 SERIALIZABLE advance 中登记，receiver 保留原 receipt/待办。禁止增加点位主数据或用更新 WorkLine 配置伪造事务冲突。

### 11.3 迁移边界和切换

所有本地原 rack_work_id/cycle_id 及搬运动作 id 原值作为公共 id，校验跨表碰撞；手工 departure request 使用原 operation_id、roller DEPARTURE decision 使用原 id 作为新的决定动作主键。手工 previous_operation_id 和 roller WAIT 后继 source_ref 映射到其原决定；roller 首轮 source_ref 属原业务出处，不能误连其他 job 的 WAIT。动作到决定的原 source_ref 原样保留。手工成员及报告的 rack_work_id/action_id、FaceWork.cycle_id、position_fact 中 source_kind=RACK 的 source_id 保持值并改读公共作业/动作。drain decision 留原表，由候选 job 的 source_ref 回连 drain 原 id；unused_racks、FIFO/FeedSegment/ReturnOpportunity/ReturnBatch 及其逐层原引用保留。detail 的幂等/历史查询不再通过旧私有表。资源缺失的未使用候选不补造 caller/点位；留原快照及待核对，生产/发送前再按原合同校验。

迁移事务先拒绝碰撞、孤儿、错 owner/工作线、WAIT 分叉/环、身份或原有证据互相矛盾，再填数校验逐项计数、关系与原冻结数据，最后移除调度私表、两个表中的 departure 专有行及其约束。缺历史时间允许为空；无迁移补发或网络查询。升级前停所有旧写方（API/receiver、worker、Beat、Sender），排空原事务，备份并在可丢弃副本验算，单事务迁移、同一制品全体切换后开放接单/推进。任何使用新语义/证据而不能无损还原的降级在首个破坏操作前拒绝；可还原的空数据结构降级在测试中验证。
