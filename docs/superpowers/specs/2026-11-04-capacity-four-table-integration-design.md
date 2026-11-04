# 工作位容量与 BIN_Roller 四表整合设计

日期：2026-11-04。依据用户“继续”推进，并按其明确选择：**停投递，保留未决原架责任**。该选择覆盖原 TASK 标签的调度；独立 LINE、事实报告及未标签历史行为保持既有合同。本设计中的实现细节为本轮工程取舍，不冒充用户另行确认。

## 目标与范围

将 PR #53 的 positions 配置、任务内接纳顺序/容量/候选恢复移植到 PR #54 的四表模型，保持两个独立分支已验行为，修正其终态合同冲突。继续使用 feature/bin-roller-four-tables；基线 d319d991，容量来源 48e6a180，develop caca3c0。主工作区既有 14 项文档改动不动。版本保持 0.2.0，不部署或运行实库迁移。

## 方案与取舍

1. **采用：原 RackJob 保留未决事实责任。** CLOSED_BY_SCOPE 立即禁止调度；其 pending 仅在原架责任尚未被可靠证明解除时保留，使既有容量门禁、锁集合、pending/Beat 和引用清理继续看见原责任。停止状态的推进只消费原事实，不产生任何外部意图。无需新表、责任账或第二 Sender。
2. 不采用单纯 pending=False 后扩大所有历史查询：会使清理/锁/生产检查各自重复推断，清理后还可能将已释放历史重新判成未知。
3. 不采用恢复旧任务发送：与用户本轮选择相反。

保留 pending 的工程风险是既有读取者可能把它误当执行权；因此停止状态必须在所有动作分支之前只读消费原结果，且面生产仍要求 ACTIVE 和原严格到位。必须补公共调度、宿主、retention 和真实 Sender 回归。

## 终态与责任

- 原 TASK 终态事务继续经 Outbox.stop_scope 停止精确标签匹配的未结束消息，不重写原身份/冻结正文/既有物理结果。
- RackJob.state=CLOSED_BY_SCOPE 表示不再有调度权；closure_basis 保留停止原因和原范围。pending=True 仅表示仍需解释原在途/在位事实，不允许 prepare、离场决定、新 ENTER/ROTATE/DEPART 或重发旧消息。
- 没有原物理 Transport 的纯候选可结束责任；已登记的 ENTER 仅在原公开投影证明未派发、无不确定/接纳/结果/位置/前驱冲突时释放。缺消息、ABANDONED、超时、UNKNOWN 本身均不足以证明未发。
- 已获派发权、接纳或在位时继续保护同点/同架和原证据。只有严格原 DEPART 提交接纳或既有等价严格无执行责任依据可收口；不新增物理迁移/人工接管接口。缺少可证明的解除事实时保持 pending，不能为了进新任务自动发旧架离场。
- 原晚到事实经现有 RACK_CYCLE/pending 消费重验，结果只归原动作；消费本身不产生外部意图，不能恢复旧 TASK。
- pending 原架的原消息/动作引用继续由现有 retention 保护；责任可靠解除后保存依据，防止已消费事实清理后重新变成未知。

## 容量与四表接口

- positions、三类架的共享接纳、admission_order、LOCAL_ONLY、旧任务/其他 owner/同物理架保护沿 PR #53；无任务 drain 仍单架。
- 候选排序沿原 plan_revision/member_order，首版 target 同版先于成员；把旧 face_work.task_id/demand_id/cycle_id 读取改为 SUPPLY scope_id/id/rack_job_id，不能恢复旧列或另建兼容表。
- read_supply_order、当前任务门禁、已绑定无 action 的补位和队尾不挡队头均保留；四表的原前驱、WAIT_FACT、FIFO、stop-scope 与有限清理不回退。
- 锁序仍 WorkLine → Devices → 全量相关 RackJobs → 宿主/四表主体 → RackActions；首 SQL SERIALIZABLE、40001 三次新会话整事务重试保持。

## schema 与原历史

最终唯一链 sharedrackdispatch → positioncapacity → rollerfoundation → rollerfour。positioncapacity 先读旧 BIN 关系；只调整尚未合入的 rollerfoundation.down_revision，并同步其 RackJob closure 约束以允许停止调度但仍 pending 的事实责任。模型与迁移约束一致；不改历史已合入 revision，不搬旧 BIN 数据。SQLite/PG 均证明空库 head、旧共享数据保全、拒绝旧责任、降级保护和单 head。

## 验证与边界

新增永久组合用例：候选/排队未派发/gate 后/已接纳/已到位/原 DEPART 接纳六阶段终态；原消息不再发、新任务同点/同架不漏保护；独立 LINE/事实报告保留；晚到事实不重开动作；关闭回滚及 PG 最后名额/关闭竞争；清理保护 pending 原架证据。复用容量多候选排序/五层与取料架补位、真实 worker/Beat，并执行完整不重叠迁移+其余 pytest、Ruff/format/diff/metadata 对齐检查。

两份旧计划已公开的直接验收缺口不因整合测试自动记完成，除非本轮有逐项直接证据。#54 保持 Draft 直到整合验证和评审完成；正式并入顺序仍 #53 → #54。CI、软件与 loopback 均不证明现场物理完成。

## Builder check 后固定的工程合同

- CLOSED_BY_SCOPE 保留主停止状态和原 closure_basis 的 reason/scope；严格解除在同一 basis 下保存 `responsibility_release`（kind、action_id、原 acceptance_basis、解除时刻），并设 pending=False。对已严格 DEPART 交出的点位排除读取同时识别正常 DEPARTURE_ACK 与该嵌套原依据，不能因保留停止原因而永久挡住新架。无物理登记/严格未派发用独立 proof kind，不冒充 DEPARTURE_ACK。
- `advance_cycle` 必须在 evaluate_roller_point/宿主需求 UNKNOWN 之前进入停止态原事实消费；`RackDispatch.advance` 也在任何动作分支前处理停止态，`prepare` 不可借原动作返回绕过停调度。现有严格 DEPART 接纳验证可抽出只消费/校验的部分复用，不放宽原匹配条件。
- builder check 确认原 Transport 接纳独立保存，ABANDONED 不天然使其不可读；不需要新增协议或 Sender。
