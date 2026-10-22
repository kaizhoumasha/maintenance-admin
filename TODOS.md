# TODOS

## 联调待确认

- [ ] 联调前与 WMS 确认收据清理：我方已按 `RELIABLE_RETENTION_DAYS`（90 天）每日清理 `callback_receipt`，需 WMS 确认同一消息身份最长重发时间不超过 1 小时（暂定，远小于该期限），并同意把清理周期写入合同（合同要求先改文档）。详见[回调接收设计 §7、§9](docs/superpowers/specs/2026-10-22-transport-callback-design.md)。

## 后续开发

- [ ] 定义 `transport_task` 的「闭合」语义，并按货架/料箱提供「未闭合前序任务」的查询（货架一轮生命周期已有本地分组键 `cycle_id`，按它聚合各步骤即可，见回调接收设计决定 15）；依赖第 3 步完成和第一个读取任务结果的消费方（滚动窗口、业务重试、换面）。wes_backend 现场事故（`tests/mock/BAD_CASES.md`：回架未完成就旋转同架、放箱未完成就离场、旧退箱异常响应冻结下一轮、目标架在途使扫码永久停止）表明，同一货架上前序任务是否闭合直接决定后续动作会多挡还是少挡：`SUCCEEDED/FAILED/CANCELLED`（位置已确定）为闭合；无结果、暂存的待完成结果、`UNKNOWN` 是否算未闭合由第一个消费方定，但异常状态不能被永久算作未闭合，也不能伪造成功。查询按「整架」而不是当前面，其他货架、旧轮次、其他工作线不阻塞。搬运层保持不预查、不阻塞，同一货架上是否等前序任务闭合由调用方按场景决定；被拒（`409/422`）的任务算闭合，结果不明（超时/无响应）不算未下发。第 3 步不新增关联表：`transport_task.frozen` 保存货架、料箱、位置的完整事实，需要时由 `frozen` 回填索引或关联表。详见[回调接收设计 §4.2、§6](docs/superpowers/specs/2026-10-22-transport-callback-design.md)。
- [ ] 卡住任务的可见性与告警：出站已接纳却迟迟没有最终结果，或料箱成功结果暂存后等不到位置事件，目前没有任何超时或告警，会静默地一直不完成。至少在超过阈值后输出结构化告警日志（任务号、已等待时间、缺什么）；阈值与处置按现场 SOP 定，不用伪造成功掩盖。回调一直不来的分支单独记录，迟到的回调到达后应自动恢复且重复到达不重复（参考 wes_backend `BAD_CASES.md` 的「完成回调缺失」）。依赖第 3 步完成。详见[回调接收设计 §6](docs/superpowers/specs/2026-10-22-transport-callback-design.md)。
- [ ] 出站时效：Transport 提交消息已统一带 1 小时 `SUBMIT_TTL`（暂定），到期未送达转 `EXPIRED`，不再重发；联调前与 WMS 确认其同一消息身份最长重发时间同样不超过 1 小时，并确认对端按 `operation_id` 去重（结果未知时原身份重发的安全前提）。`STALLED` 预算（`max_not_received_attempts`）默认不设，是否启用待定。依据见[回调接收设计 决定 10、13](docs/superpowers/specs/2026-10-22-transport-callback-design.md)。
- [ ] 调用方读取任务结果的出口：目前只能直接读 `transport_task`（`outcome_status`、`applied_outcome`、`cycle_id`）判断取消/失败/成功后是否重试，没有结果发布或按任务/按生命周期的查询接口；依赖第一个调用方（工作线插件）出现后再定形态。取消结果的处理见[回调接收设计 §6.5](docs/superpowers/specs/2026-10-22-transport-callback-design.md)。

## 低优先级（联调完成后再实施）

- [ ] 回调来源认证：与 WMS 确认认证方案；当前合同认证为 `NONE`，联调完成前不处理，落地后需复核快照脱敏与暴露面限制（伪造回调可终止提交消息重试，见回调接收设计 §6.4）。详见[回调接收设计 §1.2、§9](docs/superpowers/specs/2026-10-22-transport-callback-design.md)。
