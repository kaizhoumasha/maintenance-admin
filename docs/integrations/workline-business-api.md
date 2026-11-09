# 工作线业务 API 交互索引

> 2026-11-02 新目标决定：[公共货架调度设计](../superpowers/specs/2026-11-02-shared-rack-dispatch-design.md)统一手工、自动、BIN_Roller、粗分及后续业务的货架调度实体/推进服务，生产条件仍归原业务。原动作是离开当前工作线货架工作位的DEPART/RACK_MOVE、模板为CTU03或F01时，有效提交接纳即完成调度；目标可为库区ZONE或不同的生产线等RACK_POSITION工作位，不按是否库区判定离场；最终回调仅保留终位/失败备查，不回退、不阻塞后继、不自动重搬。本点位无其他可由原过程承接的有效需求时，直接执行责任可安全交出就安排原架离场，不等父任务终态或独立报告ACK；未知不作无需求，原业务完成/报告义务仍独立保留。本文中与此冲突的目标规则及私有架调度表/装配规划由新设计覆盖；已合入实现和旧验收保留原时点含义，新公共能力尚未实施，当前代码不会因文档更新自动改变。

本文面向业务开发人员及 WMS/ECS 对接人员，先读业务流程，再查对应消息。它整理接口需求，**不登记新 operation、不表示真实业务已接入**。

| 文档 | 用途 | 合同状态 |
| --- | --- | --- |
| [SMT 手工线](manual-picking-api.md) | 一个业务承接入库/出库；共用供箱、SCAN2 箱级作业和任务生命周期，直接取料/可选转运架仅出库 | 当前设计采用 sorting.task.* 与 manual_bin.*；共享 sorting_task ST1/ST2 已随 PR #44 合入 develop，SMT 手工宿主仍未实施，BIN_Roller 正式生产接入仍待完成 |
| [粗分机](coarse-sorting-api-draft.md) | 设备驱动，验证绑定、分流、分配储位、机械臂搬运、反馈 | 正式启用扫码、验证绑定、MOVE_FORWARD 及原 Result 消费，成功后等待箱格分配。直分入箱、NG、补换架和仅退架已完成项目内隔离实现，对端合同及设备附录由联调阶段确认 |
| [SMT 自动线](smt-auto-api.md) | 一个 smt_auto 业务：任务及共同规则统一定义，inbound/outbound以资源联合、专属逐盘合同与完成条件区分 | 唯一自动线 API 文档；共享 sorting_task ST1/ST2 已随 PR #44 合入 develop，smt_auto 宿主及消费方已登记正式运行目录，对端交付、设备附录、联调与现场验收另行记录 |
| [X-Ray 退料线](xray-api.md) | 逐盘清点、贴标、复核、五层架上架和通箱中转，复用 WMS Operation、Transport 及 ECS 公共能力 | WMS Operation 和 ECS Event／Command／Result 的本地开发合同已拟定。ECS 通用投递策略增量、业务实现及软件验证待完成 |
| [BIN_Roller](../superpowers/specs/2026-10-30-bin-roller-capability-design.md) | 四点、完整供箱/回流及架生命周期；生产业务提供上下文与 SCAN2 接缝 | B1～B5 本地实现及分项验证、B6 边界核对已完成；B7 共享声明/隔离测试装配已完成，正式生产宿主/catalog、组件状态迁移及现场验收未完成，不加锁/占位 |
| [公共料箱回流](workline-bin-return-api.md) | SCAN3 → SCAN4 → 工作线 FIFO → Return_Batch、回架及尾箱清退 | C01～C07 本方边界及组件本地实现已完成；正式生产宿主/catalog、组件状态迁移及供应商/现场验收仍属 B7 |
| [业务开发唯一用法设计](../superpowers/specs/2026-10-28-business-development-single-api-design.md) | 唯一公开模型 API、命令声明、业务目录及迁移边界 | 已实现；MOCK 与内部验证结果见当前验收说明 |

业务使用模型 send/create、`@ecs_command(..., device_roles=(...))` 及 composition `COMMANDS`，catalog 内部编译；业务目录唯一为 `app/businesses/<business>/`。
2026-11-03 用户确认的工作线配置分别维护于[手工KT16/KT18](manual-picking-api.md#manual-config)、[自动KT11/KT14](smt-auto-api.md#auto-config)及[自动线ECS位置映射](smt-auto-api.md#auto-ecs-locations)。四线非机械臂设备共用`{ECS_SHARED_ORIGIN}`，自动线两组机械臂另用各自服务；现场OUTxx/CNVxx用于WMS/RCS，机械臂使用独立内部位置常量。本次配置记录不表示已保存、部署或通过现场验收。
最短调用见[注册指南](registration.md)与[ECS 命令](ecs-command.md)，模型约束、不兼容变化及可执行证据见
[验收说明](business-development-single-api-acceptance.md)。这些能力不登记新的真实 operation 或业务。

## 1. 核对基线和“已存在”的含义

- 本系统历史基线为 `a1eccce7bf35fafb82ca408a07ff834d31324e36`（PR #28 合入 develop 后）。该时点已有业务声明、工作线绑定、入口装配、通信和结果读取接缝，正式业务清单为空。当前公共离场的注册与消费范围见[共用离场合同](#rack-departure-confirmed-wire)。
- wes_backend：人工业务相关文件核对到 `c45e3ca7be52ad6afc30b22e0b09406eefcb5714`。本地检查使用的 HEAD 为 `8b558912ccd04873556582b70fa62163d7f45cfe`，本次引用的人工子包与相关合同/wire 文件相对该 develop 基线无差异。
- “参考合同”表示旧系统中有出处，不能据此宣称本系统已注册，也不能替代对端版本、设备附录和现场确认。
- 旧文档标题中的“人工出库”不限制 `business_type`：新合同允许 MANUAL 任务区分 `inbound/outbound`，仍用同一套任务生命周期。以较新合同与 wire 为准，旧 README 的“仅出库”说明已落后。

## 2. 共用入口与职责

| 方向 | HTTP 入口 | 内容 | 业务开发负责什么 |
| --- | --- | --- | --- |
| WMS → WES | `POST /api/v1/wms/events` | 单据、计划、业务完成、Transport 位置/结果通知 | 严格业务 DTO、业务关联、受理与持久待办；共用接收器负责信封、收据和幂等 |
| WES → WMS | `POST /api/v1/wes/decisions` | 请求 WMS 决策 | 声明 operation、请求 DTO、解析原请求的决定并检查关联；调用 `WmsRequests.send` 入队 |
| WES → WMS | `POST /api/v1/wes/facts` | 报告事实 | 明确何时形成事实、报告身份及收妥后的业务条件；同样入队 |
| WES → WMS → RCS | 默认 `POST /api/v1/wes/transport-requests` | 共用 `transport.task.submit@v1` | 调用 Transport 能力，保存原任务/调用身份，读取结果；不另外实现 RCS sender |
| ECS → WES | `POST /api/v1/callback/event` | 设备事实，例如扫码到位 | 按角色声明 Event 的 data，并决定是否产生业务执行 |
| WES → ECS | `POST /api/v1/device/command` | 原命令、冻结路由/附录与参数 | 声明命令模型并列入 composition `COMMANDS`，用精确模型创建稳定动作；由原发送器发送 |
| ECS → WES | `POST /api/v1/callback/result` | 原命令执行结果 | 通用接收器按原冻结附录验证；业务读取原命令结果后推进 |

WMS 出站路径为合同默认路径；Transport 提交路径可按其既有合同配置。ECS 使用冻结的实际设备地址，WORKLINE 不保存另一份 HTTP 调用脚本。鉴权、来源校验与真实网络验收沿既有平台和对接要求，不在此创造新认证方案。

### WMS 消息信封

请求及通知使用同一外形；具体 `data` 见各业务文档：

```json
{
  "operation_id": "01a13b00-cabd-7390-890a-0c093e7c07a1",
  "operation": "sorting.task.prepare@v1",
  "timestamp": 1791991270077,
  "data": {"task_id": "PICK-001", "workline_code": "LINE-01"}
}
```

响应信封为 `operation_id/code/timestamp/data`，回显原请求 operation_id；不增加 `operation` 字段。operation_id 为小写、带标准连字符的 UUIDv7，timestamp 为 UTC Unix 毫秒整数，各 operation 可以进一步收紧范围。通知自身有新的 operation_id，关联任务用 `task_id/transport_task_id`，不复用原请求 operation_id。

技术重试必须复用原身份、时间戳和冻结正文。同 ID 内容漂移为冲突。业务 WAIT 已被有效解析并可靠保存后，才可能按约定时间/新事实发起**新的业务求值**；未知响应不是换 ID 重做的理由。

不同协议的 HTTP/code 联合不同：Transport submit 不定义 `429/BUSY`；通用 WMS request 支持的组合不自动成为每个业务 operation 的合同。当前人工线 API 也不定义 BUSY，不能从通用能力推导对端接受它。

<a id="legacy-wms-request-recovery"></a>
### 旧 Operation 的请求恢复边界

历史请求及已保存的响应保留原 Operation、operation_id、时间戳、冻结正文和关联。对尚未闭合的请求，当前发送端仅在原 Operation 仍注册且与原端点匹配时，才具备按原报文继续投递的条件，仍须遵守原投递期限和恢复规则。原 Operation 未注册时，发送适配器会在发出 HTTP 前拒绝该旧报文；该拒绝无法证明对端此前是否接纳。此类未结义务须按原身份核对已有响应及对端受理事实，禁止改写旧正文或仅为绕过注册校验而换号重发。

当前未注册的旧名包括 `outbound.bin.inbound_batch@v1`、`outbound.bin.return_batch@v1`、`outbound.manual_bin.work_admission_decide@v1` 和 `outbound.picking_task.prepare@v1`。现行 Operation 用于按现行合同登记的新业务意图。

<a id="货架离场决定的拟议共用合同"></a>
### 货架离场共用合同

<a id="rack-departure-confirmed-wire"></a>

RackDispatch 统一使用 `rack.departure_decide@v1`，通过 `POST /api/v1/wes/decisions` 请求。`data` 仅含必填的 `workline_code`、`rack_id`、`current_location`、`current_face`；`workline_code` 为原业务保存的非空工作线编码，须与货架及工作点位上下文一致；当前位置为已确认的 `RACK_POSITION`，面向不适用或无法确认时显式为 `null`。粗分架位不涉及面向，使用 `current_face=null`；人工及自动线仍按各自业务要求核对面向。任务和计划关联由各业务内部保存，不加入此共用请求。完整 READY / WAIT 示例见[粗分离场决定](coarse-sorting-api-draft.md#wms-rack-departure)。

当前正式合同由 RackDispatch 唯一注册，默认正式目录的消费者为 `smt_manual`、`smt_auto` 和 `xray`。BIN_Roller 经宿主复用，粗分尾段在启用本地隔离装配时复用。对端签认及现场验证另行确认。旧系统来源基线的离场 Operation 带 `outbound` 前缀且要求提供 `task_id`（可为 null）。现行四字段合同未注册旧 DTO 兼容入口。

#### 已确认的报文、重放与唯一搬离

本方 P11-01 已确认。WES → WMS，POST /api/v1/wes/decisions；四个 data 字段均必填，不增加 task_id/外部到位号。仅在本架满足所属业务交出条件时请求，当前位置/面向必须沿原可靠事实，单层架面向不适用为 null。完整请求：

```json
{
  "operation_id": "01a1887a-d46a-7da3-8e2c-81762c6ee940",
  "operation": "rack.departure_decide@v1",
  "timestamp": 1793378000000,
  "data": {
    "workline_code": "SORTING-LINE-01",
    "rack_id": "COARSE-RACK-001",
    "current_location": {
      "type": "RACK_POSITION",
      "location_code": "SORTING-LINE-01-LEFT"
    },
    "current_face": null
  }
}
```

HTTP 200 / DECIDED 的 READY：

```json
{
  "operation_id": "01a1887a-d46a-7da3-8e2c-81762c6ee940",
  "code": "DECIDED",
  "timestamp": 1793378000100,
  "data": {
    "result": "READY",
    "rack_destination": {
      "type": "RACK_POSITION",
      "location_code": "EMPTY-RACK-BUFFER-01"
    }
  }
}
```

READY 的 rack_destination 支持 ZONE/RACK_POSITION，沿 Transport 位置结构映射；保存原去向，不固定库区或根据其它决定猜测。WAIT 与 READY 为同一请求的互斥示例：

```json
{
  "operation_id": "01a1887a-d46a-7da3-8e2c-81762c6ee940",
  "code": "DECIDED",
  "timestamp": 1793378000100,
  "data": {
    "result": "WAIT",
    "retry_after_ms": 1000
  }
}
```

<a id="rack-departure-wait"></a>
`retry_after_ms` 为整数 1～60000。首次消费有效 WAIT 时保存固定到期点。同次在位最多一条未决离场求值。原作业仍满足离场条件时，到期可用新 `operation_id` 和时间戳续问。到期前仅 `MANUAL_*` 来源可提前续问，并且须满足以下任一条件。

- 上次求值已记录原位置动作及其已应用结果版本，本次核对发现位置动作 ID 或已应用结果版本变化。
- 上次和本次都能确认原任务状态，且状态发生变化。

其他来源只按原到期点续问。重复唤醒、相同旧事实或一般业务变化不构成提前续问依据。响应未知或 503 时只恢复原身份及完整正文，逐盘物料 WAIT 继续遵循所属 Operation 的时点规则。

| HTTP / code | data 与处理 |
| --- | --- |
| 400 / 无 | 空响应体；非法 JSON 或不能提取合法请求身份 |
| 413 / 无 | 空响应体；正文超过公共 256 KiB 上限 |
| 422 / REJECTED | reason_code=INVALID_ENVELOPE/UNSUPPORTED_OPERATION/INVALID_DATA，仅 INVALID_DATA 可带 field_path |
| 409 / CONFLICT | reason_code=IDEMPOTENCY_CONFLICT/REFERENCE_CONFLICT/STATE_CONFLICT |
| 503 / UNAVAILABLE | 固定 {}，沿原身份恢复，不形成离场许可 |

同号原内容已经处理后，再修改 timestamp/当前位置等已定义字段，HTTP 409：

```json
{
  "operation_id": "01a1887a-d46a-7da3-8e2c-81762c6ee940",
  "code": "CONFLICT",
  "timestamp": 1793378000300,
  "data": {
    "reason_code": "IDEMPOTENCY_CONFLICT"
  }
}
```

工作线/架/当前位置等引用不成立或不匹配用 REFERENCE_CONFLICT；引用成立但当前业务状态不允许离场（如仍有在途搬运）用 STATE_CONFLICT。422/409 不自动换号绕过，不解释成 WAIT；正常暂不能分配去向使用 WAIT。

同一 (operation,operation_id)、相同有效内容重放首次完整 READY/WAIT 及原响应时间，不返回 DUPLICATE、不重新分配目的地。有效 WAIT 后的新身份是重新求值，可以得到新结果；已有 READY 后不得另领去向。旧响应只处理原在位/离场记录，不能作为同码新架的许可。

READY 对应的唯一 RACK_MOVE 示例，WES → WMS，POST /api/v1/wes/transport-requests：

```json
{
  "operation_id": "01a1887a-d46a-7da3-8e2c-81762c6ee941",
  "operation": "transport.task.submit@v1",
  "timestamp": 1793378000200,
  "data": {
    "transport_task_id": "TRANSPORT-PA-LEFT-OUT-002",
    "kind": "RACK_MOVE",
    "rack_id": "COARSE-RACK-001",
    "rcs_template_id": "F01",
    "source": {
      "kind": "RACK_POSITION",
      "location_code": "SORTING-LINE-01-LEFT"
    },
    "target": {
      "kind": "RACK_POSITION",
      "location_code": "EMPTY-RACK-BUFFER-01"
    }
  }
}
```

F01 仅为该示例模板，模板选择仍沿业务架型/动作映射；本条不单独冻结其它架型模板。WES 内部将原架同次在位、本次离场意图、原 READY 和原 Transport 关联，保存消费及唯一出站意图，重复推进只取得原 transport_task_id/submit 身份，不增加 wire 关联字段。不以 rack_id 全局唯一代替本次在位关联。

Transport 提交未知继续原任务/原正文，不重新申请去向或换号搬架。READY 只授予原去向。使用 CTU03 或 F01 的原 DEPART/RACK_MOVE 离开当前工作线货架工作位时，原有效 `202 / RECEIVED` 或 `200 / DUPLICATE` 提交接纳即完成 WES 调度，可安排合法后继；最终回调仅备查，失败/未知/原地终位不重开或阻塞后继，不自动重搬。它不证明实际搬离，不生成物理终位。去生产线等其他RACK_POSITION工作位的DEPART同样按接纳完成；进场/换面仍须真实到位，不按目的地类型推断动作。旧回调只属于旧过程，不能搬走或清空新架；现场排队互锁归WMS/RCS/ECS。完整范围见[公共调度§4](../superpowers/specs/2026-11-02-shared-rack-dispatch-design.md#4-离场成功接纳即完成)。

<a id="sorting-task-contract"></a>
### 任务统一命名与参考合同的差异

工作线业务按执行模式统一划分：MANUAL/inbound、MANUAL/outbound 归 [smt_manual](../superpowers/specs/2026-10-31-smt-manual-business-design.md)，AUTO/inbound、AUTO/outbound 归 [smt_auto](../superpowers/specs/2026-10-31-smt-auto-business-design.md)。自动包交付见[总计划](../superpowers/plans/2026-10-31-smt-auto-implementation.md)：共同任务/接缝/释放确认/装配恢复/综合验收各维护一份，S3的I1～I4、S4的O1～O4只维护方向步骤与验收差异，原专项编号沿唯一追踪表定位；不分别注册 automatic_putaway/automatic_picking 业务。工作线只绑定宿主，任务通过不可变 business_type 选择内部流程；同线两个方向共用队列和一个在办执行位，各自逐盘/异常/完成条件保留。六类任务操作在 sorting_task 单份注册，组件只做严格合同与原归属薄分派，任务状态及推进归宿主。共享组件 ST1/ST2 已随 [PR #44](https://github.com/kaizhoumasha/maintenance-admin/pull/44)合入 develop（380aeba），文档状态已随 [PR #45](https://github.com/kaizhoumasha/maintenance-admin/pull/45)同步；专项 24 passed、修复后全仓 4654 passed/52 skipped 及最新交付边界统一见[共享计划 §11](../superpowers/plans/2026-10-31-sorting-task-implementation.md#sorting-task-merged)。完整消费宿主、正式 catalog/生产迁移及现场验收仍待交付，不把共享软件验收当成完整生产能力。

人工/自动共用任务族为 `sorting.task.*@v1`，issued 的 `task_type=MANUAL|AUTO` 均必填 `business_type=inbound|outbound`。SMT 手工采用 MANUAL 两方向；自动上架采用 AUTO/inbound，自动拣料采用 AUTO/outbound，发布后模式及业务类型不可变。**共享任务合同已随 PR #44 实现；本方文档确认不表示真实宿主、正式 catalog 消费方、对端交付或现场验收已完成**；旧 DTO 和历史冻结请求仍沿创建时合同。

| 历史参考 operation | 当前统一名称 | 需要保留的职责 |
| --- | --- | --- |
| `outbound.picking_task.issued@v1` | `sorting.task.issued@v1` | 原任务、执行模式、业务方向、工作线及初始队列 |
| `outbound.picking_task.queue_changed@v1` | `sorting.task.queue_changed@v1` | 准备前的连续队列版本，不改冻结任务属性 |
| `outbound.picking_task.prepare@v1` | `sorting.task.prepare@v1` | 最小请求仍为 task_id、workline_code |
| `outbound.picking_task.plan_delta@v1` | `sorting.task.plan_delta@v1` | 连续增量、模式专属来源与首版目标约束 |
| `outbound.picking_task.cancel@v1` | `sorting.task.cancel@v1` | TASK 与精确版本成员取消分别处理 |
| `outbound.picking_task.completion_confirm@v1` | `sorting.task.completion_confirm@v1` | 按连续计划版本确认业务任务，不等同物理收尾 |

[自动线统一任务接口](smt-auto-api.md#tasks)的新请求使用 sorting.task.*@v1；business_type 在 issued 显式必填，其他任务请求仍沿最小字段。[SMT 手工文档](manual-picking-api.md)的新任务也统一 sorting.task.*，人工箱级准入/完工采用 manual_bin.* 并共用于入库/出库；出库专属直接取料、自动逐盘和公共回流 operation 保留各自合同，不对全部 outbound 前缀作批量替换。同一任务名称不使 MANUAL/AUTO 或自动上架/拣料的计划字段相同。

历史冻结的 outbound.picking_task.*@v1 请求及不带 business_type 的旧 AUTO 请求仍沿创建时合同、原 operation/operation_id/timestamp、完整正文、路由和期限履约，不补字段、改名、换号或重算摘要；不以文档对齐迁移原执行或新建兼容入口。

队列排序本方已确认：dispatch_sequence 为非唯一正整数；本线符合启动条件的 QUEUED 任务按该值升序、原 issued.timestamp 升序选择，同值同时间用原 task_id 稳定兜底。原下发时间不被 queue_changed、接收或重试时间替代；每线 PREPARING/EXECUTING 共用一个执行位，不抢占在办任务，不使不同工作线串行。相对旧参考合同，这是取消序号唯一要求的目标差异，不能因其他任务已使用该值返回冲突；不表示旧数据库约束或调度实现已迁移。

自动上架的正式接口及业务约定见[《SMT 自动上架与拣料 API》](smt-auto-api.md)：任务、协议及共同推进规则统一定义，左右单层架、逐盘字段、两臂参数和报告/完成差异见其中的inbound专属分支。

自动上架只通过[BIN_Roller 接缝](smt-auto-api.md#roller-seams)交付原供箱需求/释放决定并读取原进度；[整单完成条件](smt-auto-api.md#task-completion-conditions)只覆盖原需求和已受理生产/释放，公共在途/SCAN1 旁路不增加逐箱前置。原义务在任务终态或切换后继续；文档发布不表示业务已注册、实现或通过现场验收。

<a id="common-bin-return"></a>
### BIN_Roller 的职责与 SCAN2 接缝

smt_manual/smt_auto 两宿主的四条方向流程，SCAN1 统一按实际扫码原文末尾 -B 和本线当前执行任务有效性分流：满足进入 SCAN2，否则旁路 SCAN3；不匹配投箱清单、不等投箱位置/最终结果。SCAN2 准入/作业及释放决定归各生产业务。BIN_Roller 唯一受理四点：SCAN2 先保存到位再同事务委托业务，由组件执行原释放命令；每个本点经过的 SCAN2 委托不因恢复重复，迟到旧完成只关联原命令；普通/纯回收架、投回箱及 CTU01/02/03 统一内部处理，无双组件交还或外置架面服务。新有效架到位替换旧在位数据，不等旧离场，物理/RCS 互斥直接作为约束，不建跨 Transport 排序器。技术幂等保留，本线本点连续相同业务扫码归原经过，有其他有效本点扫码/原 Result 才新进站；其他点位/线、ACK/心跳不切分。ECS/物理按明确 Result 推进，WES 不另加跨点等待；本地在制段取消后按实际到位继续请求 WMS。扫码直接证明本点到位，旧结果只补原履约、不回退下游事实。无组件手工锁、物理预占/释放或通道租约，平台原事务/幂等保持。

设计、接口与未决项唯一见[BIN_Roller 设计](../superpowers/specs/2026-10-30-bin-roller-capability-design.md)，实施只按[统一计划 B1～B7](../superpowers/plans/2026-10-30-bin-roller-implementation.md)，旧 E/P3 仅作索引。本方规则不表示供应商签认、代码注册或现场验收。

### 公共料箱回流的职责与接入边界

人工、自动拣料和后续自动上架业务各自负责 SCAN2 生产工作段及原释放决定，实际原放行与 SCAN3/4、FIFO、return_batch、回架/drain 都由同一业务内 BIN_Roller 执行。公共逻辑从实际 SCAN3 到位独立判断，不回查 SCAN1/2、WMS 作业决定、上游任务/计划或 Result；这里的独立是本点事实判断边界，状态和动作仍归原业务。原释放命令和 ACK 不证明下游接箱，父任务完成不清空 FIFO，原物理与报告义务继续。

完整消息、分支、结果门槛及集中冲突见[公共料箱回流文档](workline-bin-return-api.md)。[C01 已确认](workline-bin-return-api.md#return-fifo)按物理 FIFO 连续前缀处理，人工搬离并确认接管后本系统不再承担该箱本次回流责任，维护端点列入 TODO；[C02 已确认](workline-bin-return-api.md#return-scan3)为 SCAN3 按到位事实独立处理、不向前追溯；[C03 已确认](workline-bin-return-api.md#return-no-batch)每段投料成功后可回箱一次、收口后续投；无候选/NO_BATCH 则续投，后段成功是新的回箱机会；NO_BATCH 为当前面无法承接 FIFO 队首的确定决定且不带 retry_after_ms，原机会不轮询；[C04 已确认](workline-bin-return-api.md#return-handoff)为 SCAN4 SUCCESS 进入下游缓存，CNV0302/CNV0402 是统一交接入口，CTU/滚筒自动连续取箱，WES 不逐箱放行；[C05 已确认](workline-bin-return-api.md#return-drain-supply)为 drain 只申请可用架、到位后动态回架，本批原服务及已创建整架动作收口后仍有待回箱就继续申请直到排空，不绑定固定箱集合或建立容量预留账。[C06 的基础能力组合及即时唤醒](workline-bin-return-api.md#return-capability)已有组件本地实现及隔离验证，正式生产接入与整体验收仍属 B7；[C07 已按合并实现对齐](workline-bin-return-api.md#return-rescan)：已有轮次 SCAN4 明确 FAILED 后真实重扫可产生同轮新尝试，超时/未知不重发，保留首次 FIFO 顺序；符合本点分隔规则的真正新 SCAN3 独立建轮，不等旧轮闭合或人工移出，旧命令/分配/Transport 继续，新轮须有自己的 SCAN4 SUCCESS，SCAN3 不切分 SCAN4。设备附录与重报供应商确认见 [TODO](../../TODOS.md#公共回流设备附录与事件重报约定)。`RETURN_RACK` 是 PDA 直接取料资源，禁止作为滚筒退箱目标；`RETURN_BUFFER` 是每线回程语义，不是由本文新建的位置码。[KT16/KT18 配置](manual-picking-api.md#manual-config)来自用户提供及只读核对记录，不能当作设备在线或物理到位证据。

公共回流按已确认 C06 作为业务子包内部复用的代码能力，一个业务可组合多个内部能力；WORKLINE、运行时服务、owner、progression 及 Celery/Beat 都只归业务子包。KT20 选择 SMT 手工上下料业务后使用本线明确绑定。共享事件/operation/命令合同唯一注册，目录编译业务使用关系，新事件按受信工作线业务分派，结果按原命令/请求/Transport 的持久业务归属通知。Transport 实际应用结果或原提交接纳落库后经 after_commit 即时唤醒原业务，由其统一 scan_pending，5 秒 Beat 兜底；不设组件间任务或事件总线。[TODO](../../TODOS.md#业务子包组合接入与即时唤醒)与[实现设计稿](../superpowers/specs/2026-10-30-bin-roller-capability-design.md)已按此修订；B1～B5 本地实现、B6 边界核对及 B7 共享声明/隔离测试装配已完成，正式生产宿主/catalog、组件状态迁移及现场整体验收仍未完成。

[R01 已确认](workline-bin-return-api.md#return-drain-empty)：drain 提前真实排空后，已到位架无其他直接义务时正常申请离场，不为空回架逐一换面；未到位架沿原动作收口，不自动撤销。此规则不将候选暂为0当作排空，也不替代发起离场前置及其有效接纳；进场/换面仍须原物理结果，CTU03/F01 DEPART接纳后的落地回调仅备查。

### ECS 消息信封

| 消息 | 公共字段 | 业务字段与验证 |
| --- | --- | --- |
| Event | `device_code, event_type, timestamp, data` | data 按角色/Event 模型验证；没有必须由调用方生成的 event_id；内容派生收据身份不替代业务经过身份 |
| Command | `device_code, command_code, task_type, priority, timeout, timestamp, params` | 当前 priority 固定为 1，timeout 为毫秒；参数及超时范围按冻结的声明合同，不从示例猜测 |
| Result | `command_code, device_code, result, finish_time, data, error_detail` | result 仅 `SUCCESS/FAILED`；FAILED 必须有 `{code,msg}` 错误对象，SUCCESS 不得携带错误；data/error 按原附录验证 |

例如下列是人工线 Event 示例，设备码只用于展示配置，不应写进业务逻辑：

```json
{
  "device_code": "LINE01-SCAN2",
  "event_type": "SCAN_COMPLETED",
  "timestamp": 1791991270077,
  "data": {"bin_code": "A000002097-C"}
}
```

**不使用 is_debug 选择业务逻辑，debug 状态由 ECS 自行决定。** 该新系统约定与旧人工代码中仍存在的过滤规则是迁移差异。滚筒扫码采用用户确认已联调验证的 wes_backend manual-picking 参数/Result/原绑定基线，含 params={}，见 BIN_Roller U04；在本系统提取声明和绑定，不再将该空参数列为供应商未确认。该参考不自动覆盖机械臂动作，也不改本系统既定 debug 规则。

Result 不是另一个业务 Event，也不为每个业务增加一个 Result URL。通用接收器保存、验证与结算原命令；业务通过 `EcsCommands.get_command/get_by_call` 读取关联结果。

## 3. 四件不同的事

| 证据 | 能说明什么 | 不能说明什么 |
| --- | --- | --- |
| 入站 ACK | 原收据及对应受理依据已可靠保存 | 业务已应用、设备已移动 |
| WMS `PREPARE_ACCEPTED` / `RECORDED` | 准备请求被受理 / 事实报告被收妥 | 已有计划 / 搬运完成 |
| WMS 有效 `DECIDED` | 原请求得到了某个业务决定 | ECS/RCS 已执行该决定 |
| ECS 命令 ACK | 设备侧已接纳该命令 | 原命令物理执行成功 |
| 原CTU03/F01 DEPART到库区或不同RACK_POSITION的有效submit接纳 | WES原货架调度完成、业务释放及合法后继；最后回调仅备查，允许失败 | 不证明物理搬离/最终库位；目的地运输归WMS/RCS，进场/换面不套用 |
| ECS Result / 已应用 Transport 结果 | 原动作的确定结果，按合同解释 | 当前业务仍有执行许可；其他动作或整单必然完成 |

因此不能把 HTTP 200、消息 DONE、命令已创建、历史 TARGET_PLACED、业务任务 COMPLETED 互相替代。

## 4. WORKLINE 如何关联业务

新工作线选的是业务 code（例如待实施的 `smt_manual`、`smt_auto` 或已登记的 `coarse_sorting`），不是插件包路径、operation 字符串或事件脚本。业务声明列出角色、位置槽和初始入口；WORKLINE 保存业务选择及实际设备/位置绑定。

- 单据驱动：`workline_code → 当前工作线 → 已选业务 → 该业务正式 WMS 入口`。需校验单据和当前业务匹配，不因 operation 相同跨线处理。
- 设备驱动：`device_code → 当前绑定设备/工作线/角色 → 已选业务的角色 Event 入口`。同名 SCAN_COMPLETED 可以在两个业务中拥有不同 data 模型。
- Event 后续到位、WMS 完成通知、Result 都能唤醒已有执行，但不因此变成“允许新建业务执行”的初始入口。
- 工作线可以保存尚未配完整的资源；保存成功不授予执行权限。未完成正式合同及资源的业务不加入 catalog。

Python 集中声明描述“这个业务需要什么”；静态 catalog 与简单工厂负责“装配哪个业务”；业务子包负责“何时做下一步”。详细现行规则见[业务接入指南](business-packages.md)。

## 5. 结果读取和事务规则

统一复用[出站结果契约](outbound-results.md)，业务不读取 ReliableMessage、TransportTask 的列或 response_raw。

1. 受理使用接收器的 `ctx.session`，同事务保存收据、业务受理和持久待办。处理器不提交、不换隔离级别，不在事务内调用网络。
2. 需要组合前序事实时，由**该业务的独立推进入口**新建 session，在第一条 SQL 前建立 REPEATABLE READ；核对当前工作线许可和完整前序。BIN_Roller 的局部例外按[专项事务设计](../superpowers/specs/2026-10-30-bin-roller-capability-design.md#roller-u06-minimal-transaction)：会进入架流/正常求架检查的原宿主单条独立 advance 首 SQL 前用 SERIALIZABLE，同事务完成相关判定、登记和消费；receiver、短候选扫描、无关纯生产及其他业务保持原策略，不改全局默认或新增隔离选择器。提交后通知可丢/可重，持久待办仍须可恢复。
3. WMS 按原 message_id + operation + operation_id 读取，严格解析终局响应 data 并核对原请求。Transport 按原 transport_task_id + client_request_id + caller 读取。ECS 保存原 owner/call_key/command_code，不按“设备最近一条”猜测。
4. 明确当前直接前序集合和 caller/cycle/资源范围；分批或跨轮逐个读取仍处在同一快照。闭合需要 complete 且 unresolved 为空；CLOSED 仍需另查成功与业务许可；CTU03 原接纳已结束 WES 周期的，后续独立周期不把其最终未闭合 Transport 重新列为业务前序，不伪造平台 CLOSED。
5. member_facts 是历史里程碑，不能覆盖更新已应用 UNKNOWN；不以某个旧 TARGET_PLACED 永久移除仍可变化的相关成员。
6. 推进状态、消费原身份/准确版本/成员范围、下一 Outbox 意图同事务提交。40001 重试由具体业务独立入口负责，包含外层 commit：关闭旧 session，新 session 重读整组及许可；耗尽保留待办。BIN_Roller 相关提交连接异常不当作 40001/已回滚，按原 work 进入完整专项事务；已见原引用恢复原 ID，查空不证明回滚、不授权首登，不新增确认状态或等待。Outbox 只负责已提交意图的投递。

需要异步推进的业务在原 composition 声明 `PROGRESSION = "scan_pending"`，交付自己的待办发现和独立推进方法。平台共享任务壳负责提交后业务通知与每 5 秒 Beat 调度，两者调用同一入口；业务仍需验证丢通知、重复通知和重建后的原事实恢复。共享壳不接管业务事务或提供完整人工/粗分后续流程，见[按业务唤醒设计](../superpowers/specs/2026-10-29-business-pending-wakeups-design.md)。

## 6. 开发前的交付检查

每个业务至少给出：流程及结束条件、完整 API 字段及联合分支、原身份/业务经过身份、角色附录、工作线资源表、异常等待/核对路径、受理与推进边界、孤立环境测试清单。接口草案必须由 WMS/ECS 责任方确认后，才转为正式注册。

人工的旧系统流程参考见其专页；人工、粗分和自动线的业务未决项各自在文末列出，公共回流语义冲突只在[公共文档](workline-bin-return-api.md#return-pending)维护。统一任务命名见[任务合同索引](#sorting-task-contract)，共用离场以[正式合同](#rack-departure-confirmed-wire)为准。公共协议细节以[注册](registration.md)、[ECS](ecs-command.md)、[Transport](wms-transport.md)及[出站结果](outbound-results.md)为准。


## 工作位配置与单任务接纳

资源 PositionSlot 显式声明 RACK_POSITION 或 HANDOFF_POSITION，业务清单对位置返回 position_type。工作线 `positions` 集合保存位置码与 capacity，替代专用五层架限额；同任务同点跨角色共用上限，运行计数、候选顺序和授权不跨任务。同线 PREPARING/EXECUTING 唯一约束和原 prepare 时机保持。旧任务/无任务流程的同点外部责任单独隔离，纯本地候选不冒充外部责任，原事实收口不依赖当前任务门禁。详见[管理合同](manual-picking-api.md#manual-request-limit)。
