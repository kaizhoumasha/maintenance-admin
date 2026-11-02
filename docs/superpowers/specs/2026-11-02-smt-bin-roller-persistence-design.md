# SMT / BIN_Roller 持久化模型收敛设计

状态核对：2026-11-02。**Task1～3 已完成本地实现及分项独立复审；17 张模型、连续数据迁移及每线请求额度已实现。永久 KT16/KT18 专项 12 passed / 0 skipped，最终全仓及整体复核仍待控制器执行。** 当前增量未提交、未部署、未操作运行库。依据见[实施计划](../plans/2026-11-02-smt-persistence-and-capacity-implementation.md)与[专项记录](../../testing/smt-manual-kt16-kt18.md#current-factory-profile)。

## 1. 目标与范围

用户要求：SMT 与 BIN_Roller 一起重新收敛持久化模型，保留现有接口及业务边界，不增加通用状态机框架。

收敛前代码包含 SMT 13 张表、BIN_Roller 15 张表，共 28 张。当前已按下文映射收敛为 **SMT 7 张、BIN_Roller 10 张，共 17 张**（只计这两包，不含平台及 coarse_sorting）。这个数来自下文逐表映射，不作为继续压表的指标；多条输入、多次请求、多次搬运动作不能被“只留当前状态”覆盖。

本方案采用现有 Python 服务函数、SQLAlchemy、事务、公开结果读口和 after_commit 唤醒。状态直接属于业务模型；不新增状态机运行时、通用节点/边/实例表、统一调度器或第二套消息发送机制。

Task1/2 只处理持久化结构及查询，收敛后原容量探针仍真实失败（5/5 → 7/5），证据保留。后续授权的 Task3 增加 `WorkLine.five_rack_request_limit: int | None`：省略/None 保留未配置线行为，严格正整数 1～2147483647 为本线原 CTU01 请求预算。新 CTU01 登记时占用，原周期 RELEASED 后退出；原动作恢复不重新占用，CTU01 未知/失败不释放，CTU03 有效 submit 接纳或原成功结果只释放一次。无新增额度表、计数器、位置占位或 Sender。管理 API 的省略、null 和版本语义见[人工 API 配置](../../integrations/manual-picking-api.md#manual-request-limit)。

## 2. 路线与取舍

用户已确认共同收敛及保留边界；下列分组已在当前工作区实现，软件验证与生产交付分开记录。

| 路线 | 结果与代价 | 本方案选择 |
| --- | --- | --- |
| 只合并一对一附属表 | 改动较少，但输入、请求仍随 operation 增加多张步骤表，查询分散 | 不选为最终目标 |
| 按业务归属保留聚合、统一本包输入/请求明细 | 当前状态易查，保留乱序、WAIT 与原身份历史；需要明确区分记录种类与约束 | **采用：7 + 10 张** |
| 所有状态与动作进入一个跨业务工作流表/引擎 | 需要类型驱动的统一推进，混淆生产与公共义务，也削弱关系约束 | 不采用，违背本次边界 |

17 张均是具体业务数据表；本包请求明细只是业务消费凭据，不是可注册/可配置的执行框架。不同 operation 仍由现有具名函数验证和推进。

## 3. 归属及接口保持

| 归属 | 保留的责任 |
| --- | --- |
| `sorting_task` | 六类统一任务合同、唯一注册、可信原任务归属解析及薄分派；仍无表、队列或状态推进 |
| `smt_manual` | MANUAL 入/出库任务、计划/取消/完成确认；SCAN2 人工准入及完工；直接取料与转运架生产责任 |
| `bin_roller` | 四点扫码事实、原 SCAN2 释放、供箱及普通/drain 架周期、回流轮次/FIFO、公共回架义务 |
| 既有平台 | Callback/Event receipts、ReliableMessage/outbox、ECS 命令/Result/evidence、Transport 原任务及 applied outcome |

保持全部现有 WMS/ECS operation 名、版本、信封、字段、分支、ACK/重验规则及 owner 路由。人工入/出库继续共用生命周期；`task_type=MANUAL` 与 `business_type=inbound/outbound` 不改。原始请求 payload、路由、有效期、operation_id、client_request_id、command_code、Transport ID 不因迁移重建。

BIN_Roller 的 `apply_supply_change`、`get_supply_progress`、SCAN2 接收/释放/结果读取、回流候选、`get_pending_upper_id`、`list_pending`、`advance` 保持调用合同。`RollerWorkRef` 仍为 SCAN、SCAN2_RELEASE、FEED、RETURN_BATCH、RACK_CYCLE、DRAIN 六种；不要求消费业务认识新物理表。当前模型 `TABLES` 的内部键、repository 查询及测试 fixture 可以调整，不作为公共 API 保留一套旧表影子。

SMT 不直接查询组件私表；组件不直接查询 SMT 私表。SCAN2 DTO 的设备、箱码、扫描时间在 SMT 中保留为原生产上下文快照，通过 `arrival_id` 关联；不为了“消重”变成跨归属读表。平台结果仍只由原公开读口取得，业务只保存引用、消费版本及必要裁决依据。

## 4. SMT：13 → 7

### 4.1 目标表及逐表映射

| 目标表 | 原表 | 保留的事实 |
| --- | --- | --- |
| `smt_manual_task` | task | 任务唯一身份、线别、方向、队列版本/顺序、当前状态、已应用计划版本、准备/确认指针及 received_work 基础 |
| `smt_manual_input` | task_input、task_plan、completion、rack_completion | 原入站 operation/operation_id、不可变有效 payload、应用状态、重验原因、计划版本、完工匹配/冲突和原关联 |
| `smt_manual_task_member` | task_member | 每个原版本的供箱架面、直接取料 slot 成员；完成/取消及原架作业关联 |
| `smt_manual_bin` | bin | 原 arrival 及任务关联、人工准入/完工当前状态、当前请求及释放命令引用 |
| `smt_manual_rack` | rack | 一个生产架角色履约，RETURN 与 TRANSFER 分别推进；原资源快照及当前状态 |
| `smt_manual_rack_action` | rack_action | 每次 ENTRY/TURN/DEPART 等原动作、Transport 身份、消费版本和当前位置依据 |
| `smt_manual_request` | task_confirmation、admission、rack_report、rack_departure；原 task 中的 prepare 请求依据 | 本包每次出站 WMS 请求的身份、主体、原依据、消费决定和到期时间 |

保留 task/member/bin/rack/rack_action 的现有主键。`input` 以 `(operation, operation_id)` 唯一，不能仅按 task_id 或箱码去重。`request` 以 `(operation, operation_id)` 唯一，额外保存原业务表记录 ID（若原来与 operation_id 不同），不引入新的外部执行编号。

### 4.2 输入记录

通用列仅用于本包输入的存储和查询：operation、operation_id、receipt_id（旧数据允许原本不存在）、task_id、work_line_id、timestamp、payload、state、reason。按需要有可空的 plan_revision、rack_id、rack_face、bin_code、arrival_id、completed_at、completion_result、original_operation_id。主体和查询条件用列，原完整 payload 用 JSON，不把所有字段藏入 JSON。

输入状态按明确语义映射：

- 原任务 input 的 applied=true → APPLIED；三种原可重验原因 PREPARE_PENDING/MISSING_REVISION/WORKLINE_DISABLED → DEFERRED；其余原 applied=false → REJECTED，并保留原 reason。
- 箱级完成原 PENDING/APPLIED/DUPLICATE/REVIEW 原样映射；REVIEW 不进入自动重试，也不因迁表重新匹配当前同码箱。
- 架级完成原 applied=false → PENDING，applied=true → APPLIED。超前计划版本仍等待原版本，不闭合新任务成员。

不同操作的合法状态由各具名业务函数限制；没有统一的“给状态就自动跑节点”入口。

**计划版本有唯一原记录。** `task_plan` 当前承担版本内容基线，不能简单删除并把所有 APPLIED plan_delta 都当原计划。为 input 增加 `is_plan_record` 标记，仅迁入原 task_plan 指定的 operation_id；建立 `(task_id, plan_revision)` 的部分唯一索引（仅 is_plan_record=true）。同版本同内容的其他消息可为 APPLIED/DUPLICATE，但不抢占原计划基线。相同版本异内容仍冲突；缺版/prepare 未决的原消息保留并按原 receipt 重验。

### 4.3 请求尝试记录

固定支持 PREPARE、COMPLETION_CONFIRM、BIN_ADMISSION、RACK_ARRIVAL_REPORT、RACK_DEPARTURE 五类，operation 字符串仍为既有合同名称。共同列为 operation/operation_id、message_id、task_id、work_line_id、关联主体列、request_snapshot、basis、response、state、decision_code、saved_at_ms、due_at_ms、previous_operation_id；到位报告另关联原 action_id。request_snapshot/basis 按操作可空，迁移只复制旧业务表实际持有的依据；没有原请求快照的 prepare/admission 不从当前状态重建假原报文，权威冻结字节仍留在原平台消息中。

PREPARE 继续由 task 保存 prepare_accepted 及时间这些任务事实；请求表保存原请求/消费记录。它不复制 ReliableMessage 的投递重试/租约状态，不负责再次发送。只通过原 WmsRequests.send/get_result 创建/读取。

状态区分 REQUESTED、WAITING、CONSUMED、CONFLICT；原 COMPLETED/BUSINESS_IN_PROGRESS/PLAN_REVISION_STALE 等业务分支保存在 decision_code 及原 response，具名函数按原规则处理。当前指针放在 task/bin/rack，旧尝试不覆盖。到期重询创建合法新 operation_id，并链接 previous_operation_id；UNKNOWN/未决消息继续原请求，不借重构制造重发。

每个 arrival 的当前准入、每个 task 的当前完成确认、每个 rack 的当前离场请求分别单独查询，不把一张表等同为同一状态机。报告与离场的独立未决义务也分别存在，不能由 rack 一个 DONE 状态抹掉。

### 4.4 状态与完成条件

task 保留 QUEUED/PREPARING/EXECUTING/CANCELLED/COMPLETED；bin 保留 DEFERRED/ADMISSION_PENDING/WAIT/WORK_REQUIRED/RELEASED 及原拒绝/复核状态。rack/action 的现有业务分支不改。

完成确认未决时新 SCAN2 到箱仍保存 arrival 和原 task 关联，暂缓首次准入；原人工工作、SCAN2 释放及公共义务继续。人工任务仍不等待 SCAN2 MoveForward Result；完成依据继续包含原计划成员、供箱进度、架生产义务、待应用输入和原 received_work。任何新事实必须使过期确认依据失效，不能仅凭 task.state 完成任务。

## 5. BIN_Roller：15 → 10

### 5.1 目标表及逐表映射

| 目标表 | 原表 | 保留的事实 |
| --- | --- | --- |
| `bin_roller_scan_fact` | scan_fact | 每条技术扫码消息、digest/receipt、原绑定快照及到位关联 |
| `bin_roller_scan_arrival` | scan_arrival、scan2_release、return_round | 每次本点业务经过；一对一 SCAN2 原释放；以 SCAN3 经过为根的回流轮次状态 |
| `bin_roller_position_fact` | position_fact | 原扫码/Transport 依据及本线当前投影，保留历史事实和当前唯一约束 |
| `bin_roller_rack_cycle` | rack_cycle | 普通/drain 原架周期、资源快照及 RELEASED 接纳依据 |
| `bin_roller_rack_action` | rack_action | 多次 CTU01/02/03 动作和原 Transport/消费依据 |
| `bin_roller_face_work` | supply_demand、face_service、feed | 普通需求及其唯一面服务/供箱过程；drain 面服务；撤销、开始、feed_complete 和当前进度 |
| `bin_roller_feed_segment` | feed_segment | 每段至多四箱、顺序、原成员及独立 Transport 结果消费 |
| `bin_roller_return_opportunity` | return_opportunity | 每次段尾/面尾/纯回架机会及结束依据 |
| `bin_roller_return_batch` | return_batch | 每次原 FIFO 候选/WMS 分配/回架 Transport 及原成员 |
| `bin_roller_decision` | drain、departure | 固定 DRAIN/DEPARTURE 两类原请求与决定、WAIT 到期和前序关联 |

所有组件记录继续带 owner_domain/work_line_id。相同 rack/bin/operation 不能跨宿主或跨线查找和更新。

### 5.2 扫码与回流根记录

scan_fact 与 scan_arrival 保持多对一：多条技术消息可以属于同一业务经过。不能以一行当前状态覆盖所有消息，也不能用技术 digest 判定新的物理轮次。

SCAN2 释放当前为每 arrival 至多一条，合入 arrival 的 release_record_id、release_call_key、release_command_code、release_state、release_consumed_result_id。委托受理与释放是两个独立状态轴：delegation_state 表示原生产委托，release_state 表示原命令责任。委托完成不得关闭未决释放；task 完成也不得关闭它。原 release_record_id 保留供 SCAN2_RELEASE pending 引用，返回的命令及读口不变。

return_round 当前每个原 SCAN3 arrival 唯一，合入该 SCAN3 根行的 round_id、return_state、removed、first_scan4_arrival_id。SCAN4 的各次经过仍各保留一行并关联原 round_id；不是把多次 SCAN4 缩成一个命令。所有判资格查询仍检查本轮各点原命令的有效结果，SCAN4 SUCCESS 不借 SCAN3 或旧轮次结果取得。

迁移保存原 round_id，不以 SCAN3 arrival_id 替换对外候选/原批次中的 round_id。回流 FIFO 继续按首次 SCAN4 到位的 received_at/id 排序（冻结 C01），SCAN3 根用于核定当前物理轮次而不改排队顺序；对候选保留“先看到所有未完成/未移出轮次，再截连续合格前缀”的规则。新轮次与旧历史均保留，不能凭当前相同箱码改绑原 SCAN4。

### 5.3 面作业聚合

普通 SupplyDemand 通过 `service_id` 绑定一个精确 `(task_id, plan_revision, rack_id, rack_face)` 的面服务，Feed 又由 service_id 唯一。因此三者合为 face_work，包含：

- 身份/归属：内部 id、owner_domain/work_line_id、purpose=SUPPLY/DRAIN、task_id、plan_revision、change_id、rack_id、rack_face、cycle_id、source_ref。
- 原公开及持久引用：demand_id、service_id、feed_id 三个具名唯一列。普通行从原 demand 开始；未选择周期/未开始 feed 时后两个为空，按原开始时点分配，不能预填成已开始。
- 状态轴：demand_state（有效/撤销）、service_state（等待/就绪/完成/开始前撤销）、feed_state（未开始/请求中/分段执行/已完成/待核对）、到期/待办依据。feed_complete 是 SUPPLY 的 feed_state=COMPLETED 投影，不再维护第二个可独立改写的布尔列；DRAIN 投影为 false。
- 原供箱请求 operation_id/message_id、caller、target、manifest 及必要消费依据。drain 不进行供箱，这些列为空，不伪造“已投料”。

内部主键为 `(purpose, id)`：SUPPLY 的 id 使用原 demand ID，DRAIN 使用原 service ID，允许这两个旧表曾出现相同字符串。保留三种具名旧 ID 的查找入口和原调用返回值；不增加通用别名实体或改写历史 source_ref。迁移验证它们各自作用域内唯一，跨字段碰巧相同的字符串按引用种类解析，不能做无类型 `id in (...)` 查找。

保留原供箱进度合同：有效需求须 feed_complete=true/pending=false；未开始即撤销是 false/false；已开始再撤销仍履行原 CTU/已冻结 feed 责任。SUPPLY 开始判断继续以原 CTU01 登记或原 feed 已建立为依据，不能用 demand_state 或已分配 service_id 代替。唯一例外是明确的开始前撤销服务 `service_state=WITHDRAWN`：只在全周期无 rack_action、无 feed、无 release_basis 时记录，保留 demand/service/cycle 身份及原 source_ref；有效同架兄弟面继续原周期。兄弟后来开始及周期最终 RELEASED 都不把该服务追溯为 started 或改为 COMPLETED，其公开进度保持 false/false，且不再进入 pending FEED。

DRAIN 已选候选的未使用事实归原决定的 `unused_racks`。本线 FIFO 排空、原回架批次全部收口且候选尚无已冻结义务时，保留其 cycle/service/source_ref，以既有 WITHDRAWN 状态结束周期与面服务待办，release_basis 为空。原 CTU01 一经登记即沿原周期履约。供箱与 drain 的 WITHDRAWN 服务都受同一个降级保护。见[提前排空规则](../../integrations/workline-bin-return-api.md#return-drain-empty)。

`pending` 作为接口投影由这些具体状态及仍存在的原义务计算。数据库中不再同时维护可独立改写的多个 pending 布尔值；需要索引的状态轴保留普通列。不能把“当前没有新工作”误投影为已开始责任结清。

### 5.4 决策明细与必须保留的子项

decision 固定支持 DRAIN 和 DEPARTURE。保留原 record id、operation_id/message_id、request/decision、due_at、source_ref/input_kind/input_ref、input_snapshot、unused_racks、原 cycle_id。DRAIN 的旧 ID 仍被原 drain cycle.source_ref 引用；DEPARTURE 的旧 ID 仍被原 CTU03 source_ref 引用。主键采用 `(kind, id)`，operation_id 按 `(kind, operation_id)` 保持唯一，不假定两张旧表的记录 ID 或 operation_id 跨表绝不相同；原两个不同 operation 合同可合法复用相同 operation_id。请求前序按 kind 关联，不能跨种类继承。

CTU02 仍按原面服务转换标识；A→B→A 的第二次 A 保留新动作。feed_segment 仍一面多段；return_opportunity 仍一面可有多次段尾机会；return_batch 仍一个机会可有多次分配。因此这四类动作/明细不能压入一个“当前面状态”或覆写 JSON 数组。

CTU03 有效 submit 接纳后，原 rack_cycle 进入 RELEASED，保存原接纳依据并退出原在位业务关联；最终物理 Result 仍归原 Transport。只更新原关联，不清后来到位事实。整线公共 pending、任务完成和 rack_cycle RELEASED 仍为不同判断。

## 6. 查询、约束与推进

常用查询以列及索引直接完成，不必重新遍历全部消息或解析 JSON：

| 查询 | 主体/过滤条件 |
| --- | --- |
| 本线当前任务及其阻塞原因 | task.work_line_id/status + 原 confirmation 请求指针/state/decision_code |
| 本任务待应用通知/计划 | input.task_id/operation/state；计划基线用 is_plan_record |
| 本线 SCAN2 等人工作业 | bin.work_line_id/state + 当前 admission 请求的 due_at_ms |
| 本线未履约原释放 | arrival.owner_domain/work_line_id/release_state |
| 本任务原供箱进度 | face_work.owner_domain/work_line_id/task_id/demand_state/service_state/feed_state |
| 本线回流 FIFO | 首次 SCAN4 到位的 received_at/id，关联 SCAN3 根的 round_id/return_state/removed，再检查原点命令资格 |
| 本架待消费动作/原离场请求 | rack_action.cycle_id/status + decision.kind/cycle_id/state |

所有旧唯一身份约束迁到目标表；增加约束前先核对旧数据。不把 nullable 组合唯一当成“所有方言均自动保证”的约束：demand/service/feed ID 及按 kind 的主体约束采用显式部分唯一索引；非适用字段用 CHECK 限制。

同线唯一 PREPARING/EXECUTING 任务、原计划版本唯一、扫码 digest 唯一、业务经过 business_key 唯一、每周期一次 CTU01/CTU03、每次段/转换的原身份，以及 position_fact 当前对象/位置唯一均保留。请求当前指针必须与其 task/arrival/rack 主体一致；更新前做原身份核对，不允许跨主体指针。

仍由宿主 `scan_pending` 公平分页及原 advance 入口推进，各类独立读取固定本轮上界。物理合表后 `SCAN2_RELEASE` 按 release_record_id 取上界/游标，`FEED` 按原 demand_id，`DRAIN` 按 kind/id；不能按 arrival/face_work 的另一 ID 取上界而漏掉旧 ID 排序不同的工作。同一物理行的不同责任可分别成为 pending，不因一次推进抹掉另一个责任。

推进保留当前按责任分类的事务隔离：TASK、SCAN、FEED、RETURN_BATCH、RACK_CYCLE、DRAIN 使用新的 SERIALIZABLE 事务，纯人工作业及 SCAN2_RELEASE 使用新的 Repeatable Read 事务。继续采用 WorkLine → Devices 按 ID → 业务记录的原锁顺序；输入 receipt 的重验保护仍归 receiver，不由业务倒序加锁。业务事实、消费依据和下一 outbox 意图原子提交，提交后再唤醒；序列化失败按原有界整事务重试，Beat 沿原身份恢复。没有新 scheduler 或新 retry loop。

## 7. 有数据迁移

当前三个连续 revision 为 `0177313880f9 → smtmanualcompact → binrollercompact → fiveracklimit`，最后是唯一 head。前两步分别将 28 → 22 → 17 张业务表，第三步增加可空额度及原 FaceWork 的开始前撤销状态 CHECK，不新增表或状态字段。

按**保留既有数据**设计，不假定当前功能未部署或所有旧表为空。保留现有 Alembic 历史，在 `0177313880f9` 后先追加 SMT、再追加 BIN_Roller 两个顺序 revision（分别独立验证），随后为容量配置追加 revision；不重写已合并 BIN_Roller 迁移，不删旧 revision，不产生第二个 head。离线维护窗口中停止 API/worker/Beat 写入后迁移，切换新代码；不设计双写或长期兼容层。

迁移顺序：

1. 验证原身份、作用域、计划原记录、demand/service/feed 对应关系、SCAN3/round 唯一关系及前序引用。发现冲突或孤儿时失败并回滚，给出安全的记录 ID，不能删除或任意挑选一行。
2. 建立/扩展目标表及索引，逐条复制输入与请求记录，按上述具名 ID 合并一对一对象。旧 payload、response、basis、时间及关联原样保留。
3. 逐类核对旧→新可逆映射：输入/尝试条数、原 ID 集合、计划版本原内容、每个未决义务、所有 command/Transport/message 引用以及当前投影一致。原 Request/Result/evidence/receipt 不修改，不创建任何外部意图。
4. 通过验证后移除被替代旧表、完成 schema 切换；新 metadata 与新迁移 head 必须一致。
5. 用原公开读口和 advance 恢复 pending；未决/UNKNOWN 继续原身份，不重新发 issued/命令/Transport。保留原 due_at，不能把所有 WAIT 改成已到期。

迁移不读取/改写业务结果来推测物理完成。只有原记录本来保存的裁决和公开结果消费语义能产生新进度。

downgrade 也按具名旧 ID 拆回原表；新增数据在新模型中继续保留恢复原表所需的旧身份字段和状态。input 的 is_plan_record 重建 task_plan；请求 kind 重建四类原步骤表；face_work 重建已实际建立的 demand/service/feed；SCAN2 release 和 SCAN3 root 重建原附属表。为保证可逆，迁移及新写入不得丢弃原模型可表示的字段。回滚须切回对应旧代码，不支持新旧代码同时写。

审查补充：数据库 SQL NULL 与显式 JSON null 分别验证，不能用都解码为 Python None 的快照证明无损。历史 schema 由原 revision 正向建库取得规范，不能仅用本次 downgrade 自证。旧架离场查询按 operation_id 最大值选当前请求；本包新离场尝试须保留该排序兼容性，仍使用现有 UUIDv7 生成入口。若已有当前指针无法由旧版排序表达，降级在任何 DDL 前明确失败并保留全部原数据，错误只给安全架 ID；不改写原 ID、不删历史来强行降级。

`fiveracklimit` 降级在任何 DDL 前拒绝非空 `five_rack_request_limit` 或任一 WITHDRAWN 面服务，保留数据、schema 和 revision；不能清空撤销历史或把它伪造成 PENDING/COMPLETED 强行回退。仅额度可通过正常管理接口显式 null 清除；新撤销状态仍须兼容代码/迁移处理。SQLite 保留 AUTOINCREMENT 及删除后的主键高水位；数据库保留正整数范围约束，并在 SQLite 检查整数值，API 严格拒绝布尔、小数、字符串、零、负值和越界额度。

## 8. 实施影响范围与验收

本节保留实施边界和验收条件；各阶段实际结果见[正式实施计划](../plans/2026-11-02-smt-persistence-and-capacity-implementation.md)。当前永久专项已通过，最终全仓/整体复核待执行；不把旧 4933/52 或旧 11 PASS/1 FAIL 改写成本次结果。

| 范围 | 要同步的调用者/文件 | 验收 |
| --- | --- | --- |
| 模型/迁移 | 两包 models.py、composition.MODEL_MODULES、追加 migration、migration tests | 17 张活动业务表、单 head；空库和有数据升级/降级；metadata 一致 |
| SMT 状态与查询 | repository.py、task_flow.py、manual_bin_flow.py、manual_rack_flow.py、roller_ports.py、service.py | 入/出库同合同；计划重验/唯一基线、取消、乱序完工、WAIT、完成确认冲突恢复 |
| BIN_Roller 状态与查询 | repository.py、scan_flow.py、feed_flow.py、rack_flow.py、return_flow.py、service.py | 六种原 pending ref、原 ID 及公共接口保持；多段/多机会/循环转面/FIFO/接纳释放 |
| 测试装配 | 两包 conftest、tests/helpers.py 的对应装配、独立 MetaData、迁移清理 helper | 物理表查询可更新，业务身份/行为负向断言不得删弱；无生产表污染 |
| 平台交叉入口 | catalog/runtime 装配、sorting_task 委派、结果/接纳 wakeup、worker tests | 原 owner 唤醒和原事务边界不改；不为合表改平台授权或注册规则 |
| 文档 | 两份业务设计/实施计划的当前状态、人工线 API、公共回流 API、统一验收记录 | 公共 wire 不改；明确已实施/未实施、模型迁移与验收证据 |

必须覆盖的有数据检查：

- 同版本多条同内容 plan_delta 与异内容冲突并存，只保留一个正确原计划基线。
- 完成通知早于准入、同码不同 task/line、多次重复/结果冲突、REVIEW 不重绑；架完成早于计划。
- 准入 WAIT、离场 WAIT、完成确认未决及旧 COMPLETED 响应分别迁移，重启保持原截止与依据。
- SCAN2 委托已完成但释放命令未完成；任务已完成而原释放/公共回流仍 pending。
- 一个业务经过多条扫码 fact；一轮多次 SCAN4；同码新轮与旧轮并存，原 round ID/原各点结果不变。
- demand 尚未选周期、已选未 feed、已开始后撤销、未开始撤销；同架多个原计划版本/面及 A→B→A。
- 多 feed 段、多回流机会、同机会多 return_batch，冻结成员、顺序及原 Transport 保持。
- CTU03 仅 submit 接纳无最终 Result 的周期，以及晚到最终结果/更晚新架在位投影。
- 至少两线、两个消费 owner 的隔离；pending 分页跨原 ID 排序、重复/并发、事务回滚和恢复。
- 原非终态消息、命令及 Transport 数量/ID/冻结字节在迁移前后相同；迁移本身无新发送。

先运行相关行为、迁移、注册/worker 及 KT16/KT18 专项，再跑完整软件检查。KT 专项保留源快照和原发送方式，并收紧生产架测试为完整 `(rack_id, target.kind, target.location_code)` 比较。容量用例在纯结构收敛后预计仍失败，必须如实报告；如果行为改变，先解释是否越出本次范围，不能通过放宽断言取得全绿。

## 9. 依据与待确认事项

当前实现依据：

- `app/businesses/smt_manual/models.py`（13 表）；`task_flow.accept_task_plan_delta`/`advance_task`、`manual_bin_flow`、`manual_rack_flow`、`repository`、`roller_ports`。
- `app/businesses/_components/bin_roller/models.py`（15 表）；`repository.accept_scan_fact`/`bind_scan_round`/`supply_progress`、`feed_flow`、`rack_flow`、`return_flow`、`service.pending_query`。
- [原 SMT 业务设计](2026-10-31-smt-manual-business-design.md)、[原 BIN_Roller 能力设计](2026-10-30-bin-roller-capability-design.md)、[人工线 API](../../integrations/manual-picking-api.md)、[KT16/KT18 实际代码专项](../../testing/smt-manual-kt16-kt18.md)。

本设计作出的建议：7+10 表分组；具名旧身份列；本包输入/请求明细；追加有数据可逆迁移；容量功能修复不混入结构收敛。用户确认本设计后再进入实施计划及代码阶段。本次设计不授权清空旧数据、运行库迁移、提交或部署。
