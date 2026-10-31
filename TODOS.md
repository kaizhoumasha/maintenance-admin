# TODOS

> 优先级与工作量用于整理现有待办；不改变合同确认、联调排期或真实业务启用门槛。

## ECS 回调

### 确认事件重报窗口与未注册事件 ACK

**What:** 在启用具体事件前确认去重与供应商重报约定。

**Why:** 保证收据清理与 ECS 重报行为一致。

**Context:** 联调前与 ECS 供应商确认事件重报的最大窗口和去重保留期：ECS 事件回调复用 `callback_receipt`（按 `RELIABLE_RETENTION_DAYS`=90 天清理），收据清理后同一事件再来会被当作新事件，处理器必须自带持久幂等键（见 [ECS 事件回调设计 §5.5](docs/superpowers/specs/2026-10-24-ecs-event-callback-design.md)）。同时确认两点：未注册事件类型回 `200 ACK` 不会被其重报逻辑误判；白皮书 §4.2 对 `ESTOP_PRESSED` 返回 `400` 的条款是否保留（设计评审决定暂不内置）。

**Effort:** S
**Priority:** P1

### 补充未注册事件回放入口

**What:** 在具体处理器出现后提供受控回放能力。

**Why:** 补齐滚动发布期间被旧实例接收但未应用的业务效果。

**Context:** ECS 未注册事件的回放能力：未注册事件类型按 `200 ACK` 接收并把完整事件（≤32 KiB）存入 `callback_receipt.diagnostic`，滚动发布窗口内被老实例占位的事件新实例只返回重复，业务效果补不上。需要一个对 `UNREGISTERED_EVENT_TYPE` 诊断行重新应用已注册处理器的入口，处理器须自带幂等键；超过 32 KiB 的事件只存了摘要，无法回放。依赖第一个具体事件处理器出现。当前以「先部署处理器、再在 ECS 侧启用事件类型」的发布顺序规避，见 [ECS 事件回调设计 §6](docs/superpowers/specs/2026-10-24-ecs-event-callback-design.md)。

**Effort:** M
**Priority:** P2

## WMS 回调

### 确认收据保留和消息重发期限

**What:** 确认收据清理期限并写入 WMS 合同。

**Why:** 避免收据清理后重报被重新受理。

**Context:** 联调前与 WMS 确认收据清理：我方已按 `RELIABLE_RETENTION_DAYS`（90 天）每日清理 `callback_receipt`，需 WMS 确认同一消息身份最长重发时间不超过 1 小时（暂定，远小于该期限），并同意把清理周期写入合同（合同要求先改文档）。详见[回调接收设计 §7、§9](docs/superpowers/specs/2026-10-22-transport-callback-design.md)。

**Effort:** S
**Priority:** P1

## Transport 合同

### 确认出站时效与原身份去重约定

**What:** 确认提交 TTL、原身份重发和去重合同。

**Why:** 保证到期和结果未知时的处理符合对端约定。

**Context:** 出站时效：Transport 提交消息已统一带 1 小时 `SUBMIT_TTL`（暂定），到期未送达转 `EXPIRED`，不再重发；联调前与 WMS 确认其同一消息身份最长重发时间同样不超过 1 小时，并确认对端按 `operation_id` 去重（结果未知时原身份重发的安全前提）。`STALLED` 预算（`max_not_received_attempts`）默认不设，是否启用待定。依据见[回调接收设计 决定 10、13](docs/superpowers/specs/2026-10-22-transport-callback-design.md)。

**Effort:** S
**Priority:** P1

## Transport 运维

### 校准卡住任务告警并接入通知渠道

**What:** 联调时校准告警阈值、现场 SOP 和责任人。

**Why:** 让已实现的结构化日志进入可执行的运维流程。

**Context:** 卡住任务告警已落地（beat 每 10 分钟扫描，默认 120 分钟，输出结构化日志，见回调接收设计决定 16）；联调时按现场 SOP 校准 `TRANSPORT_STUCK_ALERT_MINUTES`，并把日志接入通知渠道、确定责任人。迟到的回调到达后任务自动恢复、重复到达不重复（参考 wes_backend `BAD_CASES.md` 的「完成回调缺失」）。

**Effort:** M
**Priority:** P2

## 公共料箱回流基础能力

### 业务子包组合接入与即时唤醒

**What:** 将 BIN_Roller 实现为业务内唯一滚筒组件，统一四点、供箱/回流及普通/drain 五层架生命周期；生产业务提供任务/供箱/接续和 SCAN2 作业接缝；工作线只关联业务。状态、命令、WMS 请求和 Transport 归原业务，由业务统一 scan_pending 与提交后即时唤醒；5 秒 Beat 仅兜底。

**Why:** SMT 手工上下料等业务可在 KT16、KT18、后续 KT20 复用相同逻辑，同时保持本业务/本线事实隔离，避免把代码能力扩成第二个运行主体或重复注册相同合同。

**Context:** [公共回流 C01～C07、R01](docs/integrations/workline-bin-return-api.md#return-pending)已确认；C06 按最新认知修订为业务内代码复用、原业务归属与统一推进。[实现设计稿](docs/superpowers/specs/2026-10-30-bin-roller-capability-design.md)与[三阶段实施计划](docs/superpowers/plans/2026-10-30-business-components-bin-roller.md)按阶段交付：阶段一组合/分派已复审提交 90861b7；阶段二 WMS/Transport 原归属与即时唤醒已通过隔离验收并复审提交 a34b28d，见[记录](docs/testing/business-components-phase2.md)，未部署；阶段三 BIN_Roller 的 B1～B5 本地实现与分项验证已完成，B6 已完成 FIFO/维护边界核对；B7 共享声明及隔离测试装配已完成，正式生产接入、组件迁移及整体验收未完成。工作范围：

阶段一 P1.1～P1.4、阶段二 P2.1～P2.4 已逐项复核并随 [PR #41](https://github.com/kaizhoumasha/maintenance-admin/pull/41) 合入 develop（f7a9628）；PR 头 ebb7c31 刷新隔离全量 3806 passed / 36 skipped，309.27 秒；合并后 [develop CI](https://github.com/kaizhoumasha/maintenance-admin/actions/runs/41129070543) 已通过，未部署。下列框架/通知项已交付，BIN_Roller 状态及统一架流已在 B1～B5 分项验证，正式生产接入仍属 B7；维护端点独立交付，不关闭本 TODO。

- 业务 composition 显式 COMPONENTS 多项组合，原 build_service 内普通 Python 组合创建处理对象；管理端、runtime.services/progressions、Celery/Beat 均只有业务子包，不新增组件业务码、owner 或任务。
- 共享事件/operation/命令合同只注册/编译一次，目录记录真实源码和业务使用关系；受信新事件按工作线业务进入本业务内唯一组件，原结果按持久业务归属消费。同业务多个组件认领同一输入键失败，不广播试执行。
- 共享模型/表结构行按原业务、工作线、轮次隔离；命令 owner 为原业务，call_key 含内部组件命名空间。业务 scan_pending 公平覆盖工作段及全部内部能力，不因父任务完成停止回流。
- 配置展示合并后的业务资源，运行动作按必要子集校验；SCAN3 不增加 SCAN1/2、上游任务或 WMS 作业决定门禁。新线仍显式绑定自己的设备/位置，不复制其他线。
- BIN_Roller 内部一个架流入口编排普通/纯回收架 CTU01/02/03，四点唯一受理且 SCAN2 委托生产业务、原放行由组件执行；架面按原 Transport 物理结果更新；旧在位记录不能阻挡新到位。SCAN1 直接证明本点到位，不等投箱回调/成员匹配；迟到上游只补原履约，不回退已确定下游事实。删除组件手工锁/持久占位/reserve-release，通道只记录原步骤，不增租约/容量账；不把锁搬到宿主。SCAN4 本次关联合法即登记自身放行、不等 SCAN3 Result，回架仍等自身成功；旧事实重报不提升为当前，同对象跨点位旧关联退历史，旧周期不预占新架面。完整 CTU01→逐面投回箱→CTU02→CTU03 只有一套组件内部待办，flow_kind 只表示原服务目的而非组件分派；首次面不重复旋转、换面不重新叫架，段间/面末机会区分，排空不替代原供架/离场收尾。
- 新增[BIN_Roller 设计](docs/superpowers/specs/2026-10-30-bin-roller-capability-design.md)与[统一实施计划 B1～B7](docs/superpowers/plans/2026-10-30-bin-roller-implementation.md)，原双组件四份设计/计划索引已删除并迁移引用；SCAN2 委托只成功提交一次、受理项不等待生产完成，准入/WAIT/完工及释放决定唯一归生产业务，组件只负责唯一放行命令与原结果，恢复不重新准入，迟到旧完成只关联原经过/原命令，不扩过期派发状态机；统一 rack_flow 唯一实现 CTU/面转换，service 消费原结果后同事务登记下一步骤/首意图，不以 work 种类或 Beat 隔开本地交接；同任务同面新服务先收口旧原义务再复用到位，不重复架动作；首次 drain 从未承接 FIFO 发现，DRAIN 只管原批次/周期引用；return_batch 非空 1～4 箱合法 FIFO 前缀即请求，不等凑满；U01 物理/RCS 互斥、U02 本线本点连续事件/技术幂等、U03 取消后在制段按实际到位继续请求 WMS 及 ECS/物理 Result 推进已确认；不新增跨 Transport 排序器、额外离开证明或过期 SCAN2 派发状态机。U04 已由用户明确参考 wes_backend manual-picking 的已联调验证代码，按其空参数/Result/绑定适配本项目；CTU03 原 submit 明确接纳即 WES 周期释放，不等最终位置/Result，普通/drain 一致，平台原运输独立保留。U06 为实现验收，按真实经过/段/回架批次/架动作及实际业务输入/原意图的必要唯一约束验证重复、即时/Beat 竞争、外层回滚、提交结果不明和重启；队首只作输入快照、原请求不因换头改号；无 ACTIVE 不等于无历史，已完历史不等于当前义务；U06 删除不足以覆盖跨决策窗口的待决请求唯一补丁；仅进入架流/正常求架的原宿主单条独立 advance 整笔 SERIALIZABLE、首 SQL 前设置，覆盖判定/登记/消费；复用原 session_factory/含外层 commit 的 40001 新会话整事务重试及 Outbox，提交未知沿原 work 完整 SERIALIZABLE 推进，查空不证明回滚/不直接授权首登，按原业务判定/唯一性/依赖及成功提交校验；不升 receiver/短扫描/已有无关纯生产/其他业务，不改共享引擎/池默认或新增隔离选择器；WES 只保本地事务/原身份，上下游负责库存决策/物理互锁，不加协调状态、锁或兜底调度；设计已收口为可编码基线，软件验收随实现执行真实 PostgreSQL 交错测试，不再拆连续小尺度设计审查；删除本线所有未收口 drain 的排他索引，经过按原分隔依据识别，新增到位不受旧状态阻挡、A→B→A 不合并、原 Result 不重复分隔；删除通用父后继登记及本点前驱条件更新，不写 FIFO 求架协调引用，不扩平台接口；删除固定 FIRST/查链尾/来源协调链；供架恢复原待决/WAIT 求值及 READY 执行，仅按真实业务输入登记单笔原意图，不设批次号/轮次计数器；已提交经过直接恢复，回滚重报沿原 receiver/U02，不增“经过待核对”状态或正常等待前置；收据清理保留原摘要到经过映射，已选批次/命令恢复原引用，不把同事务误当逻辑唯一性，不新增锁/调度器。U05 人工准入/完工/WAIT/重复与早到处理、U08 同任务跨版本续面已有实现基线，转本项目适配；用户已明确首版不做跨任务/跨普通-drain 目的留场复用和早到完工自动补绑，不留启用分支。U01～U08 无首版待确认业务规则；正常/失败终位沿原公开结果，B1/B7 仅补身份正确且位置明确的目标/面向不符架位置/原 owner 通知，不复制正常/pending 数据；B6 只核对正常 FIFO 与独立人工移出 TODO 的边界，不新增冻结批次联动或 Sender/ECS 改造，待办按本轮上界/游标分页，严格事件/命令/请求声明随 B2～B5 定义，B7 正式接入只依赖首个另行交付的完整生产包，不要求四类业务齐备，本系统验收另行落实，未扩展料箱回架判据。B6 本轮仅文档，复用 B4/B5 已有验证，不重跑产品测试；B7 及现场验收未完成，不关闭 TODO。
- 共享 WMS operation 的原请求归属复用本地 business_key=business:<业务码>，EnqueueResult 返回原关联用于重放核对，MessageStore 注入端口传出原消息关联；可靠模块不解析业务，不新增请求归属表或重试机制。
- TransportService 由运行时绑定原业务，保存 nullable owner_domain；原 submit 有效接纳保存 DONE 后，平台同事务以原 operation_id/task_id 精确读取 owner，经 after_commit 通知原业务；失败/未知/重复/失效租约/回滚不误通知，发布失败沿原 pending/Beat 恢复。统一回调在新 applied_outcome_revision 实际应用后经 after_commit 唤醒该业务，位置补齐 pending 结果同样覆盖。新事件、WMS/ECS 结果及内部事实也沿原业务通知，不按最新任务猜测、不广播组件或业务。
- 实施前同步 WMS 的 C03/C05 目标合同：NO_BATCH 仅 result，drain 多架供给且无固定成员/容量账；保留原冻结身份。[R01](docs/integrations/workline-bin-return-api.md#return-drain-empty)提前真实排空后已到位架正常申请离场、不逐一换空面，未到位架沿原动作收口、不自动撤销。
- 验证至少两业务共享一份合同、同业务多内部能力、按线分派、原归属与重放冲突、原事务/公开读口、提交后通知、回滚/保存点、WMS 与 Transport 乱序、丢/重通知及重启恢复。正常回流按 return_batch FIFO 执行，人工实际移出由下述独立维护 TODO 剔除对应队列记录，不作为正常流程启用前置；不以 ACK 代替物理完成。

**Effort:** L
**Priority:** P1

### 公共回流设备附录与事件重报约定

**What:** 以用户确认已在联调服务器验证的 wes_backend manual-picking 为基线，提取公共 SCAN1～4 的 Event/方向命令/Result/绑定并适配本系统。源码映射见 [BIN_Roller U04](docs/superpowers/specs/2026-10-30-bin-roller-capability-design.md#94-u04沿已验证-manual-picking-提取合同不重新设计)；本项是本系统实现/验收待办，不再笼统要求供应商重新确认已验证的 params={} 或 Result 信封。本轮只改文档，不调用设备。

**Why:** 源项目已验证不等于本系统已装配；需要保持原 wire/动作含义、冻结身份和工作线绑定，同时采用本会话已确认的分流/经过规则，不能搬入旧锁/容量窗口/上游准入门槛。

**Context:** 提取与本系统验收清单：

- SCAN_COMPLETED/data.bin_code，人工 SCAN2 -C、其他扫码点 -B，方向 MOVE_FORWARD/MOVE_RIGHT/MOVE_LEFT 及 params={}；工作线设备/位置仍显式绑定，不复制现场编码。
- Result 沿 command_code/device_code/result/finish_time/data/error_detail；按原命令核对，SUCCESS/FAILED 与 code/msg 沿现有合同，不新增扫码流程未要求的 actual_qty 必填字段。新动作沿本系统 PROTOCOL_RETRY_WINDOW，历史冻结期限不改。
- 技术原身份幂等保留；同一工作线/同一点位的新消息以本点相关事件流划分连续重报/新进站，排除其他线/点位、ACK/心跳及重复 Result；不新增外部 visit_id 或物理排序器。
- 失败后的新尝试仍按 C07，保持原失败和首次 FIFO，不把新经过直接当重试授权；旧轮结束/人工移交仍分别保存。源码已有处理作为提取基线，用户确认的新业务边界优先。
- 本系统专项验证原事件重报、本点事件分隔、扫码先到、原命令 Result、重启及收据清理后持久消费；参考项目联调已验证按用户说明记录，本轮未重新访问服务器。

**Effort:** M
**Priority:** P1

## 公共料箱回流维护

### 人工移出与责任接管维护端点

**What:** 提供受控维护端点，确认料箱已实际人工搬离并接管，将对应工作线、本次回流记录从活动 FIFO 剔除，保留原历史和审计。小概率例外独立保留在 TODO，当前不新增路由或代码，不作为 BIN_Roller 正常回流启用前置。

**Why:** KT16、KT18 为不能超车的单通道；人工搬离需要以明确事实解除该箱的 FIFO 队头阻挡，避免后箱永久等待或误清同箱新轮次。

**Context:** [公共回流 C01](docs/integrations/workline-bin-return-api.md#return-fifo)按本线物理 FIFO 连续合格前缀执行。端点按明确的本次回流身份确认实际移出，保存专用权限、幂等、操作者/时间/原因及接管记录，并使该记录退出活动队列；系统再按剩余队首正常推进。保留原命令、分配及 Transport 记录，不伪造已回架或库存成功；迟到旧结果不重新入队，同箱以后合法投入按新轮次处理。B4 已识别本次 ReturnRound.removed 持久事实，并以原 round_id 隔离迟到结果；B6 仅核对这些既有行为，不实现本维护端点，不将其交付设为 B7 前置。不在当前 BIN_Roller 计划展开冻结多箱联动、共用消息暂停或平台派发改造。

**Effort:** M
**Priority:** P2

## 回调来源认证

### 联合确认并实施回调来源认证

**What:** 按原排期在联调完成后确认并实施认证方案。

**Why:** 收敛既有回调来源信任与资源准入风险。

**Context:** 回调来源认证：与 WMS 确认认证方案；在认证落地前回调入口只靠网关限制来源，评审确认的已知风险（已接受）：无认证下可无限创建收据行、快照落盘上限偏大（每进程约 11GB/天）、持有 transport_task_id 者可伪造首个终态或终止提交重试、回调与主 API 共用连接池；认证落地时一并评估收据准入上限与独立连接池。当前合同认证为 `NONE`，联调完成前不处理，落地后需复核快照脱敏与暴露面限制（伪造回调可终止提交消息重试，见回调接收设计 §6.4）。详见[回调接收设计 §1.2、§9](docs/superpowers/specs/2026-10-22-transport-callback-design.md)。ECS 事件回调(`/api/v1/callback/event`)有同样的暴露面,且每行占用更大(未注册事件最多约 32 KiB,保留 90 天)。ECS 结果回调(`/api/v1/callback/result`,2026-10-25 设计)影响更高:持有效 `command_code`+`device_code` 的伪造结果可把命令推成 SUCCEEDED 并驱动消费业务推进,认证排期时应与事件面合并评估(见 [ECS 命令与结果闭环设计 §9](docs/superpowers/specs/2026-10-25-ecs-command-result-design.md))。

**Effort:** L
**Priority:** P3

## Completed

### Transport 闭合矩阵与明确前序查询

**What:** 提供按原范围读取的闭合矩阵和前序查询。

**Why:** 将当前闭合事实与历史里程碑、动作许可分开。

**Context:** Transport闭合矩阵及按原caller／cycle／明确前序ID的货架／料箱查询已由Task 0实现；成员投影与整任务闭合分开，不自动扫描旧轮／独立历史。读取不授权新动作，UNKNOWN不伪造成功或换身份重发。见[出站结果契约](docs/integrations/outbound-results.md)；实际业务的并发门禁与后续规则仍由具体业务计划落实。

**Effort:** M
**Priority:** P1
**Completed:** v0.2.0（Unreleased，2026-10-27；本分支实现，未发布）

### 出站结果正式只读接口

**What:** 提供 WMS 和 Transport 的正式结果读接口。

**Why:** 业务通过公开契约消费结果，保持推进事务原子性。

**Context:** WmsRequests.get_result、TransportService.get_result／list_unclosed_predecessors已提供正式只读出口，业务不再直接读可靠消息／任务表。统一快照及消费进度由调用方独立事务负责，不新增结果总线或全局消费位，见[出站结果契约](docs/integrations/outbound-results.md)。

**Effort:** M
**Priority:** P1
**Completed:** v0.2.0（Unreleased，2026-10-27；本分支实现，未发布）
