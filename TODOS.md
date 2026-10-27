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
