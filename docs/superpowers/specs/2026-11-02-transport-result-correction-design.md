# Transport 原任务结果更正设计

> 2026-11-02 新目标决定：[公共货架调度设计](2026-11-02-shared-rack-dispatch-design.md)统一手工、自动、BIN_Roller、粗分及后续业务的货架调度实体/推进服务，生产条件仍归原业务。原动作是离开当前工作线货架工作位的DEPART/RACK_MOVE、模板为CTU03或F01时，有效提交接纳即完成调度；目标可为库区ZONE或不同的生产线等RACK_POSITION工作位，不按是否库区判定离场；最终回调仅保留终位/失败备查，不回退、不阻塞后继、不自动重搬。本点位无其他可由原过程承接的有效需求时，直接执行责任可安全交出就安排原架离场，不等父任务终态或独立报告ACK；未知不作无需求，原业务完成/报告义务仍独立保留。本文中与此冲突的目标规则及私有架调度表/装配规划由新设计覆盖；已合入实现和旧验收保留原时点含义，新公共能力尚未实施，当前代码不会因文档更新自动改变。

状态：2026-11-02，**设计草案，尚未实施、未获对端签认**。用户要求先细化规则、保留原失败记录，暂不改代码。本文件只定义新增合同、持久依据和状态转换，不修改接收器、ORM、迁移、测试或部署。

## 1. 目标、边界与取舍

粗分补架/换架中仍须物理结果的原新架进场明确失败后，WES保留原任务等待后续结果，不自动重发。新公共规则下，原工作位CTU03/F01 DEPART取得有效接纳即调度完成，含去库区及不同RACK_POSITION（例如生产线工作位），其失败回调仅备查，不依赖本文更正。**同一任务的同一货架最终到达原目标**后，由 WMS 通过原 `transport.task.resulted@v1` 回调更正结果；WES 保存原失败依据，并在合法更正应用后恢复原准备作业。

WES 不区分手工完成还是自动完成。此前讨论的人工处理属于 WMS/现场的执行安排，不进入 WES 的字段、状态、合同命名或成功判定；WES 只核对原任务身份、结果版本、原目标和原方向。

采用**原任务显式更正 + 原失败快照保留**。不是创建新的搬运任务，也不将失败历史改写为“从未失败”。当前有效结果可从 FAILED 更新为 SUCCEEDED，失败事实仍独立可读。

比较过的方案：

| 方案 | 决定 |
| --- | --- |
| 所有更高版本直接覆盖任何终态 | 不采用；会放开成功/取消回退，以及其他业务既有终态规则 |
| 另建搬运任务或 WES 手动改成功 | 不采用；不符合已确认的原任务回调流程，也丢失原义务关联 |
| **任务登记时冻结窄合同，原回调显式引用原失败版本** | 采用；沿原任务、版本去重、事务和公开读口推进，额外证据有界 |

按用户本轮确认，粗分货架动作只有 **F01 入场（携带 target_face）和 F01 退场**，均使用 RACK_MOVE；不设计独立换面、BIN 搬运或其他模板。新增更正能力只覆盖事先选择本合同的 `kind=RACK_MOVE / rcs_template_id=F01` 的**已应用终态 FAILED → 原目标 SUCCEEDED**。`SUCCEEDED / CANCELLED` 不可被更正；`RACK_ROTATE / BIN_MOVE / BIN_EXCHANGE`、其他模板和 ECS Result 不在范围内。位置未知归一为 UNKNOWN 的结果仍走既有未确定结果收敛，不伪装成本例的终态 FAILED。

入场 target_face 由 WMS 在原 incoming_rack 方案中返回，与 rack_id、原工作位一起冻结；更正不改读配置或重新申请方向。退场沿原 departure 目的地，不补造方向或增加换面过程。

依据：[粗分实体/状态机设计](2026-11-02-coarse-sorting-data-model-design.md)、[当前 Transport 合同](../../integrations/wms-transport.md)、[回调应用代码](../../../app/integrations/wms_transport/callback/apply.py)、[公开结果读口](../../integrations/outbound-results.md)。文中新增字段、DTO 与接口参数都是目标设计，不是现有 API。

## 2. 原身份与任务级合同

以下身份及内容不变：原 `transport_task_id`、`client_request_id`、submit `operation_id`、caller、owner_domain、cycle_id、原 frozen 来源/目标/架号、提交字节、路由和已保存发送期限。`transport_task_id` 是如 `transport-<UUIDv7>` 的不透明 Text，不能按 String(36) 或裸 UUID 校验/截断。

任务登记时新增固定的本地 `result_policy`：

| 值 | 语义 |
| --- | --- |
| `STRICT_TERMINAL` | 默认及所有既有任务；维持当前终态不可改写 |
| `F01_RESULT_CORRECTION_V1` | 本设计的结果更正合同；仅新建 RACK_MOVE/F01 可选择，支持原任务冻结的入场 target_face，不限制实际完成方式 |

目标接缝为 `TransportService.move_rack(..., result_policy=...)`，默认 STRICT_TERMINAL。粗分新架进场正式业务装配显式使用F01_RESULT_CORRECTION_V1；原F01 DEPART不按库区/生产线等去向分支，均按接纳完成且保持STRICT_TERMINAL，不为其启用更正；不是环境配置、HTTP 用户开关或前端权限。其他消费者保持默认，不按 `owner_domain == "coarse_sorting"` 在底座硬编码业务分支。

同一 client_request_id 的本地重入，必须同时核对原 input_digest、owner 和 result_policy；修改 policy 判幂等冲突，不能原地升级已登记任务。policy 不进入 submit wire、不重算原 frozen/input_digest，也不修改历史字节。既有任务迁移后统一为 STRICT_TERMINAL；本设计不授权把历史失败任务批量改成允许更正。

本地 policy 不构成对端能力协商。WMS 对原任务继续执行及结果更正消息的约定，须随此合同签认和对应业务上线落实；WES 不能仅凭 WMS 返回普通提交 ACK 推断它支持更正，也不靠修改 submit 载荷临时传递这个本地字段。

与已合入SMT手工/BIN_Roller的复用边界：它们及未来smt_auto都保留既有严格终态，不能仅因同用RACK_MOVE/F01而自动采用本policy。公共新目标统一CTU03/F01 DEPART有效submit接纳完成，库区或不同工作位去向均可，直接RETURN/TRANSFER也适用；进场/换面及原框架FAILED/CANCELLED仍按各自规则；本更正增量不扩充它们的成功或释放资格。后续底座实施须回归这些既有消费者及默认调用，不要求其改表/显式补参或重新登记原任务，也不作为AUTO生产实施前置。

## 3. WMS 回调合同

继续使用 `POST /api/v1/wms/events` 和 `transport.task.resulted@v1`。更正是**同一任务的一条新结果消息**，因此使用新的回调 operation_id、新 timestamp、更高 outcome_revision；技术重报保持这条更正消息的原 ID、时间及完整正文。绝不能复用原失败回调的 operation_id 改正文。

在 Resulted.data 增加可选 `correction` 对象：

| 字段 | 类型与约束 |
| --- | --- |
| `from_revision` | 必填，严格整数 `1..2**63-1`，小于本消息 outcome_revision，不接受 bool/小数/字符串 |

correction 对象只包含 from_revision，严格禁止冗余字段，不增加完成方式、操作员或人工标记。非空 correction 只能与任务 `kind=RACK_MOVE`、`status=SUCCEEDED` 一起出现，任务是否为 F01 通过原 frozen 核对，不让回调另行指定模板。SUCCEEDED 仍要求明确的 final_position，不允许 failure_code、position_unknown 或 results[]。入场原任务带 target_face 时，更正结果必须显式携带相同的 arrival_face；不能在更正路径用期望方向补造实际到位方向。普通回调不提供 correction；null 按未提供处理，见第 7 节摘要规则。

例：原任务失败在版本2，完成结果在版本3更正。示例是新增目标合同，原任务必须已选择允许更正的 policy。

```json
{
  "operation_id": "019f1f00-0003-7000-8000-000000000003",
  "operation": "transport.task.resulted@v1",
  "timestamp": 1793635203000,
  "data": {
    "transport_task_id": "transport-019f1f00-0001-7000-8000-000000000001",
    "kind": "RACK_MOVE",
    "outcome_revision": 3,
    "rack_id": "RACK-NEW-01",
    "status": "SUCCEEDED",
    "final_position": {
      "kind": "RACK_POSITION",
      "location_code": "CLN01"
    },
    "arrival_face": "90",
    "correction": {
      "from_revision": 2
    }
  }
}
```

本例原 F01 入场任务已冻结 target_face="90"；90 仅为字段示例，不构成现场方向配置。

该消息表示 WMS 已确认原任务按原目标完成。继续沿现有可信来源接收；完成事实由 WMS 负责，WES 不要求说明执行方式，不新增人工“改成功”接口。

## 4. 允许应用的全部条件

与公共调度衔接：冻结本更正policy的ENTER在原FAILED后仍可能沿原任务继续到位，故即使带明确点位外位置也不能直接终结为NOT_ARRIVED/释放原点位保护，亦不能据NONE并发发起离场；保留原ENTER待办直到合法更正或合同规定的执行责任可靠终结。DEPART已有效接纳的完成不受此限制回退，最终失败仍仅备查。

持有原 TransportTask 行锁，在同一接收事务中依次校验：

1. 任务存在，原 frozen 完整且当前 policy 为 F01_RESULT_CORRECTION_V1；kind 为 RACK_MOVE、原 rcs_template_id 为 F01。
2. 当前已应用结果是**位置明确的终态 FAILED**；其版本等于 correction.from_revision，且独立失败快照与当前结果及版本摘要一致。不能引用仅收到但未应用的失败，也不能引用另一任务的版本。
3. 新 outcome_revision 严格高于当前已应用版本；不是只要“更高版本”就允许覆盖。SUCCEEDED/CANCELLED、已完成一次更正或源版本不符时均不允许。
4. 原任务、kind、rack_id 一致。新结果必须是严格 SUCCEEDED，且确实是原任务的完成：冻结 target.kind=RACK_POSITION 时，final_position 必须精确匹配原目标。原任务有 target_face 时，arrival_face 必须非空并与原值精确相等，不换方向、不转数字、不以配置当前值替代。原任务不要求方向时，不从其他任务借方向增加约束。
5. 原 target.kind=ZONE/RACK 时保留既有 WMS/RCS 解析责任，WMS 必须给出明确实际 RACK_POSITION；WES 不新增区域归属主数据。此结果不自动证明原架物理离开；按新公共设计，原CTU03/F01 DEPART已有效接纳后只记录终位备查，不以此拦后继。本文更正用于仍需原应用成功的F01进场。
6. 同一任务+版本的规范化 data 摘要未冲突；失败快照不能被修改，更正元数据必须完整保存。

更正的显式 arrival_face 检查必须在 `_normalized` 补面向或保存候选之前进行；只读取本条更正实际提供的方向，不先用 frozen.target_face 补齐再判相等。通过这一检查后，成功方向与原目标一致，既有成功归一过程不会再改变该值，也不会让保存结果和版本摘要因自动补面向产生分歧。

允许一次终态纠正，完成后重新回到严格成功终态：不得再用新版本更正、回退、改目标或更换货架。源版本未知/未到的处理按第 8 节暂存，不能省略第2项。

不带 correction 的高版本 SUCCESS 在终态 FAILED 后仍是 TERMINAL_CONFLICT；只有显式更正路径可以成为例外。不同任务、普通 submit 拒绝、CANCELLED 和历史 STRICT_TERMINAL 任务不能借这个字段变成成功。普通成功回调既有的面向缺省归一规则不在本轮改动范围；更正路径的显式 arrival_face 要求只适用于本新增合同。

## 5. 失败与更正依据的最小持久化

沿用原 TransportTask，不增加新的任务/动作实体或一张通用事件账本。新增两个目标字段：

| 字段 | 类型 | 规则 |
| --- | --- | --- |
| `result_policy` | String(32)，非空 CHECK | 默认 STRICT_TERMINAL，创建后不可改 |
| `result_correction` | JSON，SQL NULL 可空 | 普通任务/未出现终态失败时为空；允许合同下保存下述有界证据对象 |

result_correction 的固定结构：

| 成员 | 保存内容 |
| --- | --- |
| `failure` | 原已应用失败的完整规范化 data、outcome_revision、原版本 data_digest、回调 operation_id、receipt_id、回调 timestamp 和 WES applied_at_ms |
| `completion` | 初始 null；更正成功后保存 from_revision、新 outcome_revision、新版本 data_digest、更正回调 operation_id/receipt_id/timestamp 和 WES applied_at_ms |

允许 policy 的任务**首次应用终态 FAILED 时就保存 failure**，不等待业务消费或后续更正。failure 写入后不可改；最多一份，completion 最多填一次。当前成功完整结果仍在 applied_outcome，completion 是与原失败连接的元数据，不复制整份成功响应。

更正事务必须同时保存 completion、applied_outcome/status/revision、outcome_digests 和收据诊断，再原子提交。失败当前投影更新为成功，不删除 failure、原版本摘要或原任务。不满足快照一致性时记录核对原因并保持原失败，不能临时从已清理收据猜出一个失败快照。

收据90天清理、可靠消息清理不删除 result_correction；它与原 TransportTask 同寿命保留。本轮不新增 Transport 任务清理机制、不宣称脱离原任务仍有无限档案。JSON 更新整体赋新值，不原地修改嵌套对象。

## 6. 判定矩阵与外部响应

沿用原公共回调响应：首次202 RECEIVED表示已持久接收，不能单独当作已应用成功。原已受理消息按同回调身份及相同正文重报时，沿既有接收器返回200 DUPLICATE，保留原收据时间和data；收据本身保存的原202响应不被改写。原已拒绝/冲突消息沿既有收据重放相应拒绝/冲突响应，不统一变成200。同任务同版本同规范化data但使用不同回调身份也为200 DUPLICATE，不同data为409 CONFLICT。合法 DTO 但与任务事实矛盾仍以首次202+收据诊断处理，不为本例扩展全局错误协议。

| 原有效结果 / 输入 | 行为 |
| --- | --- |
| 允许 policy，FAILED r，合法结果更正 c>r 且 from=r | 保存原失败及更正依据，应用 SUCCEEDED c，不判断执行方式 |
| FAILED r，不带 correction 的 SUCCESS | 原失败不变，TERMINAL_CONFLICT |
| STRICT_TERMINAL 任务携带 correction | 不应用、不暂存，CORRECTION_NOT_ALLOWED |
| 其他 kind、correction 错结构或与非 SUCCESS 混用 | DTO 422 REJECTED，任务不变 |
| 任务不存在或 frozen 缺失 | 沿原 UNKNOWN_TASK/FROZEN_MISSING 诊断，不建任务、不补原 frozen |
| 源版本等于当前版本但当前是 UNKNOWN/非 FAILED | 不应用、不暂存，CORRECTION_BASE_MISMATCH |
| from 小于当前已应用版本，或当前已 SUCCEEDED/CANCELLED | 不应用、不暂存，CORRECTION_BASE_MISMATCH/TERMINAL_CONFLICT |
| 源版本尚未到且任务未形成终态 | 按第8节暂存，原有效投影不变 |
| 版本相同且已注册相同摘要 | 原200 DUPLICATE；不二次写快照/应用/唤醒 |
| 版本相同但摘要不同，包括改 from 或改目标 | 原409 CONFLICT，原依据不变 |
| 低于当前已应用版本的普通迟到结果 | 原投影不回退，原失败快照不改 |
| 更正成功后又报另一高版本结果 | 保持成功终态，不发生第二次更正 |

CORRECTION_* 为目标诊断码，仅作固定 allowlist 标识；不回显 SQL、凭据或原异常。语义不合法的版本仍沿现有 register_revision 记录其摘要，不能修正文体后复用已占版本；后续合法新消息须使用新 operation_id 和未占用的更高结果版本。

无效更正不更新 position_observation，不借异常位置观察路径授权恢复或新增位置唤醒。普通回调原有位置观察逻辑保持，不能把这项窄更正扩展成终态任意改位置。

## 7. 身份、摘要与历史重放

当前 Resulted 通过 model_dump 生成规范化 data，默认字段也进入版本摘要。新增字段时必须**仅对新增 correction 字段**定义：缺失或 null 都从规范化结果删除；其他既有字段、默认值、排序与归一规则不变。接收解析、register_revision、保存结果和公开读口必须使用同一规范化结果。

因此：普通历史结果的 data 摘要不变，outcome_digests 不重算、不回填；有非空 correction 时该对象完整参与回调 effective_digest 和任务版本摘要。禁止全局改为 exclude_none/exclude_unset，避免影响既有字段。

原已受理失败回调重报仍按第6节返回200 DUPLICATE，不转成更正，也不修改其 operation_id/body。此前已按未知附加字段接收的报文不能因为升级后识别 correction 就自动重放业务；原样技术重报沿既有收据去重响应，不重新应用。WMS 要发送正式更正，必须使用新回调身份和新结果版本。

## 8. 更正与失败乱序到达

复用现有 `pending_outcome`，仅对允许 policy 的 RACK_MOVE 增加一种严格、可辨识的暂存内容；BIN 等原暂存语义保持不变。不增加后台应用器或另一结果队列。

### 8.1 暂存条件

显式更正的任务/policy/原货架/原目标/新版本/DTO 均已合法，唯独尚无被引用的已应用失败：当前无结果，或当前版本低于 from_revision 且仍非终态。此时保存：

- revision：候选更正版本；data：规范化更正 data。
- origin：原更正回调 operation_id、receipt_id、timestamp 和 received_at_ms，供未来应用时保留原来源，不能使用后到失败回调的身份冒充。

暂存不设置 SUCCEEDED，也不伪造 FAILED；原消息重报按第6节返回去重响应，不重新应用候选。公开读口只返回 has_pending_outcome/pending_revision，不暴露暂存正文或授予业务成功。

每个原任务只保留一个待应用更正候选；第一条合法候选占位。同版本同 data 沿原去重，另一个版本的更正候选记 CORRECTION_PENDING_CONFLICT，不替换已有候选、不积累队列。被记诊断的消息不会在候选消失后自动回放；需要 WMS 基于明确事实以新消息/未占版本重新发送。

### 8.2 后到原失败

在原回调任务行锁内：

1. 原失败按既有版本/身份/位置规则合法应用，保存不可变 failure 快照。
2. **先重核暂存候选，再决定是否清理它**，不能沿用“任何终态都直接删除 pending”的旧分支。
3. 候选 from_revision 恰等于刚应用的失败版本，且其所有校验仍合法，则在同一事务应用候选成功、保存 completion 的原候选来源，清空 pending。
4. 即使业务从未在一次独立事务中看到 FAILED，也能通过 result_correction 读到完整原失败；失败消息收据仍保留自己的原202响应。

候选的版本摘要已经在首次接收时登记，重新核对只复用结果应用校验，不再次走 register_revision 的消息判重入口；否则会把候选自己判为 Duplicate 而永不应用。不新建收据、不换候选消息身份，completion 仍使用 origin 保存的原来源。

候选重新核对采用以下完整规则，不为结果更正放开 FAILED→FAILED：

| 当前已应用事实 | 候选处理 |
| --- | --- |
| 无结果，或非终态且 revision < from | 继续等待被引用的原失败 |
| 终态 FAILED 且 revision = from，失败快照/目标等全部合法 | 应用一次更正并清空候选 |
| 终态 FAILED 且 revision != from，包括 revision < from | 候选失效：原失败已锁定，不能再应用候选声称的另一个 FAILED |
| 当前 revision >= from，但不是所引用的终态 FAILED，例如 UNKNOWN revision = from | 候选失效：原版本已被其他事实占用，不可能再补成所引用失败 |
| SUCCEEDED 或 CANCELLED | 候选失效，不覆盖终态 |

失效时清空 pending，保留已登记版本摘要，并在触发核对的原回调收据诊断中保存 CORRECTION_BASE_MISMATCH、候选原消息/结果版本及实际基准版本；不修改候选原收据响应、不改指向新的失败。WMS 如需针对实际已应用失败继续更正，须发新 operation_id 和未占用的更高结果版本，显式引用实际失败版本。

若原失败版本自身冲突/被拒绝、尚未形成足以判定上述失效的有效已应用事实，候选不授予成功，保留原等待/核对，不制造缺失事实。已有明确失效条件时不能无限占住暂存槽。

暂存期间的普通结果仍按既有合法版本规则处理：合法普通成功可先成为严格终态并使候选失效；不为一个未证实的更正阻塞更高有效事实。此时不生成 completion，也不将未应用的被引用失败伪装成已应用历史。WMS 若要保留本例失败→成功更正链，应可靠投递原失败，或让候选等待与之相符的失败；不能只发送一条自述失败历史的成功。

## 9. 公开读口、通知与业务恢复

TransportTaskResult 目标增加只读 `result_policy` 和经校验的 `result_correction` 副本。任务/原 client_request_id/caller 仍先核对；IDENTITY_MISMATCH 不暴露另一任务的失败或完成依据。

读口须校验：failure 为该任务/货架的规范化终态 FAILED，与原版本摘要一致；completion 的 from/new revision/digest 与 failure、当前已应用成功及 policy 一致。任务存在更正 data 但缺失有效连接依据，返回 INVALID/INVALID_EVIDENCE，不降级解释为普通成功。严格 policy 下出现 result_correction 同样视为 INVALID。

合法持久组合进一步固定：

| policy / 当前投影 | result_correction 约束 |
| --- | --- |
| STRICT_TERMINAL | 必须为NULL；不能有带 correction 的已应用或暂存结果 |
| F01_RESULT_CORRECTION_V1，从未应用过终态失败 | 必须为NULL；普通结果仍按原合同成立，不能虚构失败链 |
| F01_RESULT_CORRECTION_V1，当前FAILED | failure 必填且精确匹配当前失败，completion=NULL |
| F01_RESULT_CORRECTION_V1，已应用成功更正 | failure/completion 必填，当前SUCCESS及其from/new版本、摘要/原方向均相符，pending候选已清空 |

未知 policy、非F01任务采用该policy、已FAILED却缺失failure、仅有failure却当前已变为其他终态、completion不匹配当前成功，均返回 INVALID。合法前提已满足却仍遗留可立即应用的候选也不是业务成功凭证，应待核对，读口本身不补应用或修复行。

- 更正前：公开 outcome 仍是 FAILED，closure=CLOSED/APPLIED_RESULT **不等于成功**，result_correction 仅有 failure。
- 有暂存但基准未到：既有 outcome 保持；无 outcome 时为 OPEN/AWAITING_CORRECTION_BASE，UNKNOWN 时仍为 UNDETERMINED；has_pending_outcome=true，不能据候选认为已完成。
- 更正后：公开 outcome 为 SUCCEEDED 新版本，closure_basis 使用该新版本，同时可读原 failure 和 completion 关联。
- predecessor 读取按最新有效已应用版本和原明确范围求值；不把历史失败固定为永久失败，也不因历史位置片段覆盖当前结果。前序 CLOSED 仍不能独自授予粗分成功。

当前 `apply_callback` 已在已应用版本前进后通过 after_commit 唤醒原 owner_domain，可直接沿用。失败后到并同事务应用更正时只需原业务一次唤醒；重复、仅诊断或仅暂存不新增成功通知。发布失败不改变已提交结果，由原业务待办/Beat 恢复。

粗分 Preparation 保留原 MOVING_OUT/MOVING_IN 和当前 MOVE_RACK_* 动作。其完整流程独立推进事务按实体设计采用 SERIALIZABLE，通过原 `get_result(task_id, client_request_id, caller)` 读取新成功，核对原任务/架号/F01模板/原result_policy/目标和 pending：新架需到达原目标工作位且arrival_face匹配原target_face；已有效接纳的原F01 DEPART旧架不走本更正恢复，去库区或不同工作位相同，后到失败不阻后继。消费新版本与进入下一阶段/登记下一正常动作原子提交；下一动作许可不足时仅保存原依据并保留原阶段，不能先前进再补空指针。

其中F01模板与policy的兼容性由公开读口对原frozen验证，业务比对自己的resource_snapshot和DTO.result_policy/kind；不要求业务读取未公开的框架表或另造一个原frozen查询API。

Action.evidence_history 追加原 failure（如尚未保存）和本次 completion/新成功依据，原失败项不覆盖。即使失败和更正在两次业务推进之间都已完成，也能保留两项事实。不会生成新 Transport task_id、client_request_id 或新的架位准备作业来“恢复”。

## 10. 事务、迁移与原能力边界

沿用原接收事务与 TransportTask 行锁。收据、版本摘要、失败快照、暂存更正、当前应用结果和 after_commit 注册一起提交；异常整体回滚。更正接收器不调用粗分服务、不获取业务工作线锁、不读取粗分表，业务在提交后按自身实体设计的隔离要求独立推进，避免反向锁序；本接收增量不改变 receiver 或全局隔离级别。

原 settle_submitted 仍只结束原提交消息，不发送新任务。结果更正不改技术重试/TTL/任务来源，也不改变原任务的 owner/caller/cycle 和其他未采用新合同的业务。

后续实施需要新增两列及唯一 Alembic 链迁移；历史行 policy=STRICT_TERMINAL、result_correction=NULL，不合成历史失败。新合同只适用于可信代码新登记的任务；只要存在采用新 policy 的任务，破坏性降级移除 policy/更正依据必须拒绝。不得把更正对象塞入 frozen，破坏原提交核对。

整个生命周期只保存一份终态失败及一次成功更正，不扩展通用审计事件表，不新增人工接口、任务取消/重建流程、配置开关或 Celery 任务。公开 read_rows/project、predecessor 投影、pending 校验及 Resulted 规范化必须同步支持该合同，不能只改 apply_result 就宣称完成。

## 11. 设计验收清单（未编写或执行测试）

| 场景 | 预期 |
| --- | --- |
| 允许 policy 的 FAILED → 合法原目标成功更正 | 当前成功、原失败不可变、原任务与 submit 身份不变；手工/自动方式不影响判定 |
| 未选择 policy、历史任务、取消/成功、BIN/换面或 ECS | 维持原严格终态与原范围，不获得更正能力 |
| 原失败重复、更正原消息重复、跨消息同版本重复 | 原响应/业务去重稳定，证据不重复追加、通知不重复 |
| 同版本改目标、改 from 或改状态 | 409冲突，不覆盖原依据 |
| 不带标记、更正源版本不符、错架/错目标 | 不应用，不使粗分恢复，不另建任务 |
| F01入场更正缺少/错 arrival_face，或回调尝试改原方向 | 不应用；不从目标方向或当前配置补造成功证据 |
| 普通成功回调原面向缺省、非F01模板试图采用新policy | 前者既有规则保持；后者登记即拒绝，不扩大更正范围 |
| 新增 correction 缺失/null 与所有普通历史样例 | 原规范化 data 和摘要不变；新增非空对象完整参与摘要 |
| 更正先到、原失败后到 | 暂存原候选来源，后到失败触发同事务应用；原失败仍可读 |
| 被引用失败不是已应用事实、其他版本先形成终态 | 不伪造失败链，候选无权覆盖当前终态 |
| 两个不同更正候选并发 | 原任务行锁下至多一个占位；不覆盖候选，不增加队列 |
| 持久化/提交失败、并发回调与重复通知 | 原子回滚，原身份重报可继续；业务消费不生成替代搬运 |
| 业务未消费失败即读到更正成功、公共收据清理 | 公共 DTO 仍有失败快照，动作历史可补齐原失败依据 |
| 公开读口中更正连接损坏或 caller不符 | INVALID或IDENTITY_MISMATCH，不暴露错误成功 |
| 原DEPART已有效接纳，回调终位仍在原工作位 | 原结果备查，不重开调度或阻后继；进场仍必须原实际目标匹配，不能借DEPART规则补认到位 |

本轮只进行文档来源、字段/版本/状态一致性和独立可实施性审阅。新增 wire 字段须由 WMS 对端签认并随对应版本实施联调；本设计不声称更正已生效或现场已完成。

## 12. 启用顺序与设计检查

后续实施先完成迁移及全部 API 接收实例、worker 的同版合同/读口部署，再允许新业务登记允许更正的任务，最后让 WMS 发送带 correction 的消息。不能混用忽略 correction 的旧接收器和新接收器；旧版本可能将它当普通附加字段，返回202而不保存更正链。

升级前已按旧规则受理的报文保持原收据和版本摘要，不删除、不重算、不自动补应用。对应任务若没有在创建时采用本合同，也不能通过新回调原地启用更正；必须单独核对既有事实，不能把更正合同签认当成历史任务升级授权。

独立实施者审阅提出并已补齐暂存失效规则：低于 from 的另一个终态 FAILED、等于 from 的 UNKNOWN 都使原候选不再可能成立，应清理而不是等待一个不合法的终态覆盖。其余已确认的字段、来源、摘要、公开读口和事务边界按本文执行。本检查只针对文档，不表示功能测试或现场验收通过。

本轮 F01/方向增量复核已同步清除接口验收表中的“入场省略 target_face”旧约定；更正显式方向校验位于归一/暂存之前。现有其他模板和普通结果的规则不随本增量改变。

完成方式边界已按用户纠正同步：移除人工专用合同值、存储名及 correction.kind，只保留原失败版本关联；手工/自动完成不参与解析、状态或成功判定。失败后等待的是原任务有效结果，WES 不据此自动重发。
