# Transport 回调外部审阅记录

日期:2026-10-23。host=codex,provider=claude-code,运行时报告模型 `glm-5.3`。输入为完整第四版设计、`origin/develop` merge base `8a90e2b` 起的工作区生产源码差异,以及未改动的可靠存储与 Transport wire/type 源码;不包含测试载荷。审阅输入冻结后只增加验证记录,生产源码未变。

结构审阅传输状态为 `completed`,退出码 0,session `2b86fb39-9d09-4442-967f-9220cc0c5e61`;`outside-review-result.ts structured` 校验通过,没有 P1,结构 gate 为 PASS。报告中 PostgreSQL“待验证”的措辞来自输入时点,当前实际验收已为完整套件 **1526 passed、5 skipped**,详见[设计 §19](../specs/2026-10-22-transport-callback-design.md#19-解除环境限制后的续验2026-10-23)。

主审核对:源码确实尚无卡住任务候选索引;但外部“ORDER BY 迫使主键扫描”的执行计划判断没有本轮 EXPLAIN/现场规模证据。将 P3 作为容量观察项保留,不把长期性能预测升级为已发生故障,本轮不据此增加迁移。任务表规模增长后,应先测量扫描查询计划与耗时,再决定是否增加候选 partial index。

独立的外部对抗调用以 540000ms 超时结束,没有审阅正文,不计作完成。返回数据如下:

```tool-output
{"status":"unavailable","provider":"claude-code","result":"","error":{"code":"timeout","message":"Claude Code timed out after 540000ms."},"exit_code":null,"stderr":"[claude-code:unrecognized_model] {\"model\":\"glm-5.3\",\"query_source\":\"sdk\"}"}
```

原生独立对抗复核已完成且无剩余确定源代码问题。外部结构审阅的完成不能填补外部对抗超时的覆盖缺口。

## 外部结构审阅原文

````tool-output
# Transport 回调接收代码评审报告

评审范围:`feature/transport-callback-receiver` 工作区源码对比 `origin/develop`(merge base `8a90e2b`),覆盖通用接收、幂等、事务回滚、并发、结果应用、settle、迁移与卡住告警;已按指示不重开认证 NONE、90 天收据保留、冻结缺失不匹配仍结算等已确认范围决定。

## 已修复两项的复核结论

**卡住任务扫描(修复正确)**:`app/tasks/transport_tasks.py:60-70` 采用 `TransportTask.id > last_id ... order_by(id).limit(limit)` 键集分批,`last_id = tasks[-1].id`(:94)正确推进;批内逐条告警后取下一批,空批终止。201 条任务两轮覆盖全部的回归测试已由维护者报告通过。无残留缺陷。

**诊断告警(修复正确)**:`app/integrations/wms_callback/receiver.py:31-44` 的 `_log_diagnostics`/`_drop_diagnostics` 沿用 Outbox 已验证的 `after_commit`/`after_rollback` 分层模式:`_handle` 中(:159-176)诊断先随收据同事务持久化,再按当前事务登记;嵌套提交提升到父事务(`transaction.nested` 分支),回滚丢弃,仅根提交输出 `callback diagnostic` 告警。`Rejected` 显式回滚保存点不影响已登记诊断(登记在保存点块之后)。逻辑与 Outbox 既有通过测试的模式一致,无残留缺陷。

## 主要路径复核(无缺陷)

- **通用幂等**(`receiver.py:receive`):原子占位 `ON CONFLICT DO NOTHING` + 败者重读;RAW/EFFECTIVE 双摘要、原指纹优先重放、规范化器不可用时 `503` 不降级为未知 operation、`409` 经 `echo_keys` 回显首次响应 data——均与设计 §5 一致。
- **结果应用**(`callback/apply.py`):行锁下版本登记、`UNKNOWN` 可收敛、`pending_outcome` 失效/完成与首次应用共用同一函数、判定表逐格核对(含 `RACK`/`ZONE` 目标放行、`CANCELLED` 不做目标校验)与设计 §6.2 相符;JSON 列全部整体赋值。
- **settle**(`settle.py`、`Outbox.settle`):先 `FOR UPDATE` 锁消息行再按身份 settle;`lease_id=None` 使迟到发送结果按租约被 `finish` 忽略;与 `expire_pending` 的并发由状态过滤双向互斥。锁顺序「收据 → 任务行 → 消息行」保持。
- **迁移**:`a020`→`a023` 单链,与模型 `create_all` 终态一致(含 `diagnostic`、两个保留期索引、`cycle_id` 索引);回填逻辑与 `_register` 写入的 `frozen` 口径一致(均无 `transport_task_id`)。

## 发现

**[P3] confidence 8/10** — `app/tasks/transport_tasks.py:55-70`:卡住扫描在无卡住任务时每 10 分钟对 `transport_task` 做全表主键遍历。

```python
select(TransportTask)
    .where(
        TransportTask.id > last_id,
        TransportTask.created_at < cutoff,
        TransportTask.outcome_status.is_(None)
        | (TransportTask.outcome_status == "UNKNOWN"),
    )
    .order_by(TransportTask.id)
    .limit(limit)
```

影响:`(outcome_status IS NULL OR ='UNKNOWN')` 无索引支持,`ORDER BY id` 迫使按主键顺序逐行过滤;循环以空批为终止条件,意味着每次 beat 运行都要走到表尾才能判定「无卡住任务」。`transport_task` 无保留期清理、永久增长,扫描成本随表规模线性上升;当前规模可忽略,长期运行后成为每 10 分钟一次的固定全表 I/O。键集分批只解决了「遮蔽」问题,未解决「空批代价」。

建议修复(PostgreSQL partial index,SQLite 测试不受影响):

```sql
CREATE INDEX transport_task_stuck_idx
    ON transport_task (id)
    WHERE outcome_status IS NULL OR outcome_status = 'UNKNOWN';
```

各批变为对候选行的索引范围扫描,表再大也与卡住任务数成正比。

其余检查项(信封校验顺序、`422` 不建收据路径、`RateLimit(None)` 豁免、快照限速与失败旁路、`SUBMIT_TTL`、收据清理分批删除、`cycle_id` 重放以首次为准、`Resulted`/`PositionChanged` DTO 枚举与互斥规则)均未发现缺陷。PostgreSQL 并发验收与测试载荷未在本次上下文中,按 §18 口径仍属待维护者验证项,不计入发现。

建议：合入前为卡住扫描补一个 `outcome_status IS NULL OR 'UNKNOWN'` 的部分索引(或等效候选索引),因为该 beat 任务每 10 分钟以主键序遍历整个持续增长的 `transport_task` 表,规模上升后成为固定的全表 I/O 开销。
````
