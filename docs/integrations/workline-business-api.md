# 工作线业务 API 交互索引

本文面向业务开发人员及 WMS/ECS 对接人员，先读业务流程，再查对应消息。它整理接口需求，**不登记新 operation、不表示真实业务已接入**。

| 文档 | 用途 | 合同状态 |
| --- | --- | --- |
| [人工分拣](manual-picking-api.md) | 单据驱动，货架供料、人工处理、退箱、换面及离场 | 参考旧系统流程；共用离场合同等调整待确认，业务未接入 |
| [粗分机](coarse-sorting-api-draft.md) | 设备驱动，验证绑定、分流、分配储位、机械臂搬运、反馈 | 已实现扫码、验证绑定、MOVE_FORWARD 及原 Result 消费，成功后等待箱格分配；后续阶段未实现，对端合同及设备附录待确认 |
| [自动线](automatic-picking-wms-api.md) | BIN / RackSlot 逐盘取料、决定、放置及来源架面推进 | 本系统对接草案；简化 wire、盘次关联及设备边界待确认，业务未接入 |
| [业务开发唯一用法设计](../superpowers/specs/2026-10-28-business-development-single-api-design.md) | 唯一公开模型 API、命令声明、业务目录及迁移边界 | 已实现；MOCK 与内部验证结果见当前验收说明 |

业务使用模型 send/create、`@ecs_command(..., device_roles=(...))` 及 composition `COMMANDS`，catalog 内部编译；业务目录唯一为 `app/businesses/<business>/`。
最短调用见[注册指南](registration.md)与[ECS 命令](ecs-command.md)，模型约束、不兼容变化及可执行证据见
[验收说明](business-development-single-api-acceptance.md)。这些能力不登记新的真实 operation 或业务。

## 1. 核对基线和“已存在”的含义

- 本系统：`a1eccce7bf35fafb82ca408a07ff834d31324e36`（PR #28 合入 develop 后）。已有业务声明、工作线绑定、入口装配、通信与结果读取接缝；**该基线的正式业务清单为空**。当前增量已登记粗分扫码、验证及首个 MOVE_FORWARD / Result 阶段，其他业务仍按各自草案实施。
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
  "operation": "outbound.picking_task.prepare@v1",
  "timestamp": 1791991270077,
  "data": {"task_id": "PICK-001", "workline_code": "LINE-01"}
}
```

响应信封为 `operation_id/code/timestamp/data`，回显原请求 operation_id；不增加 `operation` 字段。operation_id 为小写、带标准连字符的 UUIDv7，timestamp 为 UTC Unix 毫秒整数，各 operation 可以进一步收紧范围。通知自身有新的 operation_id，关联任务用 `task_id/transport_task_id`，不复用原请求 operation_id。

技术重试必须复用原身份、时间戳和冻结正文。同 ID 内容漂移为冲突。业务 WAIT 已被有效解析并可靠保存后，才可能按约定时间/新事实发起**新的业务求值**；未知响应不是换 ID 重做的理由。

不同协议的 HTTP/code 联合不同：Transport submit 不定义 `429/BUSY`；通用 WMS request 支持的组合不自动成为每个业务 operation 的合同。人工参考合同亦不定义 BUSY，不能从通用能力推导对端接受它。

### 货架离场决定的拟议共用合同

人工、粗分和自动线草案统一使用 `rack.departure_decide@v1`，通过 `POST /api/v1/wes/decisions` 请求。`data` 仅含必填的 `workline_code`、`rack_id`、`current_location`、`current_face`；`workline_code` 为原业务保存的非空工作线编码，须与货架及工作点位上下文一致；当前位置为已确认的 `RACK_POSITION`，面向不适用或无法确认时显式为 `null`。粗分架位不涉及面向，使用 `current_face=null`；人工及自动线仍按各自业务要求核对面向。任务和计划关联由各业务内部保存，不加入此共用请求。完整 READY / WAIT 示例见[粗分离场决定](coarse-sorting-api-draft.md#wms-rack-departure)。

这是本系统**拟议的新共用合同，尚待 WMS/WES 确认，未注册、未实施**。旧系统来源基线的离场 operation 带 `outbound` 前缀，且要求 `task_id` 字段存在（可为 null）；不能把本文的改名和字段裁剪当作旧 DTO 已支持的原样复用。三份草案仅保留上述目标用法，不增加兼容入口；对端确认名称、请求/响应、幂等及错误规则后，才能交付本系统的正式 DTO 和业务入口。这里的历史来源说明不改变第 1 节所列核对基线。

### ECS 消息信封

| 消息 | 公共字段 | 业务字段与验证 |
| --- | --- | --- |
| Event | `device_code, event_type, timestamp, data` | data 按角色/Event 模型验证；没有必须由调用方生成的 event_id；内容派生收据身份不替代业务经过身份 |
| Command | `device_code, command_code, task_type, priority, timeout, timestamp, params` | 当前 priority 固定为 1，timeout 为毫秒；参数及超时范围按冻结的声明合同，不从示例猜测 |
| Result | `command_code, device_code, result, finish_time, data, error_detail` | result 仅 `SUCCESS/FAILED`；FAILED 必须有 `{code,msg}` 错误对象，SUCCESS 不得携带错误；data/error 按原附录验证 |

例如下列是人工线参考 Event，设备码只用于展示配置，不应写进业务逻辑：

```json
{
  "device_code": "LINE01-SCAN2",
  "event_type": "SCAN_COMPLETED",
  "timestamp": 1791991270077,
  "data": {"bin_code": "A000002097-C"}
}
```

**不使用 is_debug 选择业务逻辑，debug 状态由 ECS 自行决定。** 该新系统约定与旧人工代码中仍存在的过滤规则是迁移差异。真实设备的每个命令参数、结果 data、错误枚举和超时界限仍要形成已确认附录，不能将旧实现中的 `params={}` 直接当成完整供应商合同。

Result 不是另一个业务 Event，也不为每个业务增加一个 Result URL。通用接收器保存、验证与结算原命令；业务通过 `EcsCommands.get_command/get_by_call` 读取关联结果。

## 3. 四件不同的事

| 证据 | 能说明什么 | 不能说明什么 |
| --- | --- | --- |
| 入站 ACK | 原收据及对应受理依据已可靠保存 | 业务已应用、设备已移动 |
| WMS `PREPARE_ACCEPTED` / `RECORDED` | 准备请求被受理 / 事实报告被收妥 | 已有计划 / 搬运完成 |
| WMS 有效 `DECIDED` | 原请求得到了某个业务决定 | ECS/RCS 已执行该决定 |
| ECS 命令 ACK | 设备侧已接纳该命令 | 原命令物理执行成功 |
| ECS Result / 已应用 Transport 结果 | 原动作的确定结果，按合同解释 | 当前业务仍有执行许可；其他动作或整单必然完成 |

因此不能把 HTTP 200、消息 DONE、命令已创建、历史 TARGET_PLACED、业务任务 COMPLETED 互相替代。

## 4. WORKLINE 如何关联业务

新工作线选的是业务 code（例如拟议 `manual_picking`、`coarse_sorting`），不是插件包路径、operation 字符串或事件脚本。业务声明列出角色、位置槽和初始入口；WORKLINE 保存业务选择及实际设备/位置绑定。

- 单据驱动：`workline_code → 当前工作线 → 已选业务 → 该业务正式 WMS 入口`。需校验单据和当前业务匹配，不因 operation 相同跨线处理。
- 设备驱动：`device_code → 当前绑定设备/工作线/角色 → 已选业务的角色 Event 入口`。同名 SCAN_COMPLETED 可以在两个业务中拥有不同 data 模型。
- Event 后续到位、WMS 完成通知、Result 都能唤醒已有执行，但不因此变成“允许新建业务执行”的初始入口。
- 工作线可以保存尚未配完整的资源；保存成功不授予执行权限。未完成正式合同及资源的业务不加入 catalog。

Python 集中声明描述“这个业务需要什么”；静态 catalog 与简单工厂负责“装配哪个业务”；业务子包负责“何时做下一步”。详细现行规则见[业务接入指南](business-packages.md)。

## 5. 结果读取和事务规则

统一复用[出站结果契约](outbound-results.md)，业务不读取 ReliableMessage、TransportTask 的列或 response_raw。

1. 受理使用接收器的 `ctx.session`，同事务保存收据、业务受理和持久待办。处理器不提交、不换隔离级别，不在事务内调用网络。
2. 需要组合前序事实时，由**该业务的独立推进入口**新建 session，在第一条 SQL 前建立 REPEATABLE READ；核对当前工作线许可和完整前序。提交后通知可丢/可重，持久待办仍须可恢复。
3. WMS 按原 message_id + operation + operation_id 读取，严格解析终局响应 data 并核对原请求。Transport 按原 transport_task_id + client_request_id + caller 读取。ECS 保存原 owner/call_key/command_code，不按“设备最近一条”猜测。
4. 明确当前直接前序集合和 caller/cycle/资源范围；分批或跨轮逐个读取仍处在同一快照。闭合需要 complete 且 unresolved 为空；CLOSED 仍需另查成功与业务许可。
5. member_facts 是历史里程碑，不能覆盖更新已应用 UNKNOWN；不以某个旧 TARGET_PLACED 永久移除仍可变化的相关成员。
6. 推进状态、消费原身份/准确版本/成员范围、下一 Outbox 意图同事务提交。40001 重试由具体业务独立入口负责：关闭旧 session，新 session 重读整组及许可；耗尽保留待办。Outbox 只负责已提交意图的投递。

需要异步推进的业务在原 composition 声明 `PROGRESSION = "scan_pending"`，交付自己的待办发现和独立推进方法。平台共享任务壳负责提交后业务通知与每 5 秒 Beat 调度，两者调用同一入口；业务仍需验证丢通知、重复通知和重建后的原事实恢复。共享壳不接管业务事务或提供完整人工/粗分后续流程，见[按业务唤醒设计](../superpowers/specs/2026-10-29-business-pending-wakeups-design.md)。

## 6. 开发前的交付检查

每个业务至少给出：流程及结束条件、完整 API 字段及联合分支、原身份/业务经过身份、角色附录、工作线资源表、异常等待/核对路径、受理与推进边界、孤立环境测试清单。接口草案必须由 WMS/ECS 责任方确认后，才转为正式注册。

人工的旧系统流程参考见其专页；人工、粗分和自动线的未决合同各自在文末列出，共用离场调整以本文草案说明为准。公共协议细节以[注册](registration.md)、[ECS](ecs-command.md)、[Transport](wms-transport.md)及[出站结果](outbound-results.md)为准。
