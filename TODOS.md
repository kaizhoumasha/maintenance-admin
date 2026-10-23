# TODOS

## 联调待确认

- [ ] 联调前与 ECS 供应商确认事件重报的最大窗口和去重保留期：ECS 事件回调复用 `callback_receipt`（按 `RELIABLE_RETENTION_DAYS`=90 天清理），收据清理后同一事件再来会被当作新事件，处理器必须自带持久幂等键（见 [ECS 事件回调设计 §5.5](docs/superpowers/specs/2026-10-24-ecs-event-callback-design.md)）。同时确认两点：未注册事件类型回 `200 ACK` 不会被其重报逻辑误判；白皮书 §4.2 对 `ESTOP_PRESSED` 返回 `400` 的条款是否保留（设计评审决定暂不内置）。

- [ ] 联调前与 WMS 确认收据清理：我方已按 `RELIABLE_RETENTION_DAYS`（90 天）每日清理 `callback_receipt`，需 WMS 确认同一消息身份最长重发时间不超过 1 小时（暂定，远小于该期限），并同意把清理周期写入合同（合同要求先改文档）。详见[回调接收设计 §7、§9](docs/superpowers/specs/2026-10-22-transport-callback-design.md)。

## 后续开发

- [ ] ECS 未注册事件的回放能力：未注册事件类型按 `200 ACK` 接收并把完整事件（≤32 KiB）存入 `callback_receipt.diagnostic`，滚动发布窗口内被老实例占位的事件新实例只返回重复，业务效果补不上。需要一个对 `UNREGISTERED_EVENT_TYPE` 诊断行重新应用已注册处理器的入口，处理器须自带幂等键；超过 32 KiB 的事件只存了摘要，无法回放。依赖第一个具体事件处理器出现。当前以「先部署处理器、再在 ECS 侧启用事件类型」的发布顺序规避，见 [ECS 事件回调设计 §6](docs/superpowers/specs/2026-10-24-ecs-event-callback-design.md)。
- [ ] 定义 `transport_task` 的「闭合」语义，并按货架/料箱提供「未闭合前序任务」的查询（货架一轮生命周期已有本地分组键 `cycle_id`，按它聚合各步骤即可，见回调接收设计决定 15）；依赖第 3 步完成和第一个读取任务结果的消费方（滚动窗口、业务重试、换面）。wes_backend 现场事故（`tests/mock/BAD_CASES.md`：回架未完成就旋转同架、放箱未完成就离场、旧退箱异常响应冻结下一轮、目标架在途使扫码永久停止）表明，同一货架上前序任务是否闭合直接决定后续动作会多挡还是少挡：`SUCCEEDED/FAILED/CANCELLED`（位置已确定）为闭合；无结果、暂存的待完成结果、`UNKNOWN` 是否算未闭合由第一个消费方定，但异常状态不能被永久算作未闭合，也不能伪造成功。查询按「整架」而不是当前面，其他货架、旧轮次、其他工作线不阻塞。搬运层保持不预查、不阻塞，同一货架上是否等前序任务闭合由调用方按场景决定；被拒（`409/422`）的任务算闭合，结果不明（超时/无响应）不算未下发。第 3 步不新增关联表：`transport_task.frozen` 保存货架、料箱、位置的完整事实，需要时由 `frozen` 回填索引或关联表。详见[回调接收设计 §4.2、§6](docs/superpowers/specs/2026-10-22-transport-callback-design.md)。
- [ ] 卡住任务告警已落地（beat 每 10 分钟扫描，默认 120 分钟，输出结构化日志，见回调接收设计决定 16）；联调时按现场 SOP 校准 `TRANSPORT_STUCK_ALERT_MINUTES`，并把日志接入通知渠道、确定责任人。迟到的回调到达后任务自动恢复、重复到达不重复（参考 wes_backend `BAD_CASES.md` 的「完成回调缺失」）。
- [ ] 出站时效：Transport 提交消息已统一带 1 小时 `SUBMIT_TTL`（暂定），到期未送达转 `EXPIRED`，不再重发；联调前与 WMS 确认其同一消息身份最长重发时间同样不超过 1 小时，并确认对端按 `operation_id` 去重（结果未知时原身份重发的安全前提）。`STALLED` 预算（`max_not_received_attempts`）默认不设，是否启用待定。依据见[回调接收设计 决定 10、13](docs/superpowers/specs/2026-10-22-transport-callback-design.md)。
- [ ] 调用方读取任务结果的出口：目前只能直接读 `transport_task`（`outcome_status`、`applied_outcome`、`cycle_id`）判断取消/失败/成功后是否重试，没有结果发布或按任务/按生命周期的查询接口；依赖第一个调用方（工作线插件）出现后再定形态。取消结果的处理见[回调接收设计 §6.5](docs/superpowers/specs/2026-10-22-transport-callback-design.md)。

## 低优先级（联调完成后再实施）

- [ ] 回调来源认证：与 WMS 确认认证方案；在认证落地前回调入口只靠网关限制来源，评审确认的已知风险（已接受）：无认证下可无限创建收据行、快照落盘上限偏大（每进程约 11GB/天）、持有 transport_task_id 者可伪造首个终态或终止提交重试、回调与主 API 共用连接池；认证落地时一并评估收据准入上限与独立连接池。当前合同认证为 `NONE`，联调完成前不处理，落地后需复核快照脱敏与暴露面限制（伪造回调可终止提交消息重试，见回调接收设计 §6.4）。详见[回调接收设计 §1.2、§9](docs/superpowers/specs/2026-10-22-transport-callback-design.md)。
