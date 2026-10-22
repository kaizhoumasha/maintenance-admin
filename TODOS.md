# TODOS

## 联调待确认

- [ ] 与 WMS 确认回调接收来源认证方案；当前合同认证为 `NONE`，暂不实施。详见[回调接收设计 §1.2、§9](docs/superpowers/specs/2026-10-22-transport-callback-design.md)。
- [ ] 联调前与 WMS 确认 `member_position_changed` 里程碑的先后规则；当前采用本方约定，尚未经对端确认。详见[回调接收设计 §6.3、§9](docs/superpowers/specs/2026-10-22-transport-callback-design.md)。

## 后续开发

- [ ] 制定回调收据与证据的清理策略；本阶段不清理，依赖货架结果应用上线后的容量数据及 WMS 对重发窗口的承诺。详见[回调接收设计 §7](docs/superpowers/specs/2026-10-22-transport-callback-design.md)。
- [ ] 联调控制台增加「投递样例回调并查看结果」场景，仅限 `ENVIRONMENT=local`；依赖货架结果应用切片完成。详见[回调接收设计 §1.2、§10](docs/superpowers/specs/2026-10-22-transport-callback-design.md)。
- [ ] 定义 `transport_task` 的「闭合」语义，并按货架/料箱提供「未闭合前序任务」的查询；依赖第 3 步完成和第一个读取任务结果的消费方（滚动窗口、业务重试、换面）。wes_backend 现场事故（`tests/mock/BAD_CASES.md`：回架未完成就旋转同架、放箱未完成就离场、旧退箱异常响应冻结下一轮、目标架在途使扫码永久停止）表明，同一货架上前序任务是否闭合直接决定后续动作会多挡还是少挡：`SUCCEEDED/FAILED/CANCELLED`（位置已确定）为闭合；无结果、暂存的待完成结果、`UNKNOWN` 是否算未闭合由第一个消费方定，但异常状态不能被永久算作未闭合，也不能伪造成功。查询按「整架」而不是当前面，其他货架、旧轮次、其他工作线不阻塞。搬运层保持不预查、不阻塞，同一货架上是否等前序任务闭合由调用方按场景决定；被拒（`409/422`）的任务算闭合，结果不明（超时/无响应）不算未下发。第 3 步不新增关联表：`transport_task.frozen` 保存货架、料箱、位置的完整事实，需要时由 `frozen` 回填索引或关联表。详见[回调接收设计 §4.2、§6](docs/superpowers/specs/2026-10-22-transport-callback-design.md)。
- [ ] 卡住任务的可见性与告警：出站已接纳却迟迟没有最终结果，或料箱成功结果暂存后等不到位置事件，目前没有任何超时或告警，会静默地一直不完成。至少在超过阈值后输出结构化告警日志（任务号、已等待时间、缺什么）；阈值与处置按现场 SOP 定，不用伪造成功掩盖。回调一直不来的分支单独记录，迟到的回调到达后应自动恢复且重复到达不重复（参考 wes_backend `BAD_CASES.md` 的「完成回调缺失」）。依赖第 3 步完成。详见[回调接收设计 §6](docs/superpowers/specs/2026-10-22-transport-callback-design.md)。
