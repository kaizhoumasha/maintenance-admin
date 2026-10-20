# SPEC-019B 可靠交互层实施与验证

基线：`develop/f5b4c46bcd3ab483adf6a541642658c3cd8f56db`。实施分支：`feature/spec-019-reliable`。
生效合同：[SPEC-019B](../specs/2026-10-20-spec-019b-reliable-interaction.md)、[HttpClient](../specs/2026-10-20-spec-019a-http-client.md)。
实施阶段仅本地修改及可销毁环境验证；外部评审优化后，用户已授权全量验证通过再提交、创建 PR 到 develop、CI 通过后合并并核验合并 CI。不部署，不改用户 `.env`、版本或依赖。

## 原有八项任务

| 任务 | 实现与永久行为测试 |
|---|---|
| 1. HttpClient 公开校验 | `validation.py`、公开导出，与 `send` 共用构建；HTTP 校验一致性、同步无客户端/无事件循环、隔离导入测试 |
| 2. 合同/配置/退避/测试工具 | 冻结 draft/message/verdict、两个适配器端口、配置优先级、注入时间/随机/HTTP/故障；`test_config.py` |
| 3. 模型/迁移/事务入队 | `ReliableMessage`、唯一身份、调用者事务、存续行先比较字节、仅新消息做校验；`test_enqueue.py` 与 PG 并发首次入队/回滚/清理竞争 |
| 4. 租约与独立到期 | 短事务 `SKIP LOCKED`、条件回写、未知租约恢复、独立有限 TTL 扫描、UTC-aware 边界；`test_leases.py`、`test_postgres.py` |
| 5. 单次投递 | `send_one` 每次使用当前配置，冻结正文，最终发网前检查 TTL；完整 HTTP 才分类，持久退避、不设次数上限、日志与尝试证据；`test_delivery.py` |
| 6. 发送器/lifespan | 空闲槽位领取、双实例/专用 PG 池、空目标仍扫描、故障后续扫、一个整体停机预算；`test_sender.py`、`test_runtime.py`、两阶段 PG 骤停子进程 |
| 7. 保留期清理 | 仅终态且 `updated_at < cutoff`，Celery beat 每日上海时间 03:15；`test_retention.py`、真实 PG 清理任务 |
| 8. 整体验收/文档 | 全量 PG/Redis、HTTP/分层、升降级守卫、单 head、模型/迁移索引一致、代表性 EXPLAIN、Ruff 与文档同步 |

### 保持的评审决定

- 空目标也用受预算限制的专用池持续扫描；不创建 HTTP 客户端。外部优化后，只领取本进程能服务的目标；未注册适配器不装配客户端，未服务目标的未过期消息保留 PENDING，继续参与 TTL 扫描与租约回收。
- 每次后台检查先扫描 TTL 到期的 `PENDING` 并标为 `EXPIRED`，再回收租约到期的 `SENDING`；回收时未过 TTL 的转回 `PENDING` 并退避，已过 TTL 的转为 `EXPIRED`。两项默认各最多处理 100 行，批量独立于发送领取，即使发送槽位已满也执行。TTL 扫描不依赖重试时间；到期本身不增加尝试次数、不撤销已发请求，租约回收也不证明对端未执行。
- 后续调度修复：发送完成后唤醒补位，领取批量较小时继续填充空槽；保留空闲轮询、扫描异常退避与整体停机预算。后台检查在正常运行时执行、不需要停机，后台检查间隔默认 1 秒；发送器启动首轮、轮询计时器到点、扫描异常等待该间隔后重试时执行。发送完成唤醒或连续补位时，只有距上次后台检查已满该间隔才执行，否则仅领取发送；直接调用 `Sender.tick()` 默认仍执行后台检查。不新增环境变量或另一套重试调度。
- 同身份存续行先比较冻结字节，同字节返回已有引用、异字节冲突；只对真正新入队做 HTTP 校验。插入竞争不破坏外层事务，清理后重新入队按新消息校验。
- 可靠层停机共用一个 monotonic deadline（默认 15 秒），各阶段只取剩余预算的一部分，为后续清理留机会。它不是完整进程/API 池/Redis 的退出 SLA。只释放已确认停止且仍属当前租约的任务；未确认取消/回写留待租约恢复，不强行解锁。
- 数据库行进入冻结消息/Claim 时统一 UTC-aware；只有本层已知 UTC 写入的 SQLite naive 读值可补 UTC，外部未知 naive 时钟拒绝。
- 永久真实 PG 子进程测试分别在领取已提交、脚本发送已接受但结果未存储后直接退出；新进程重建事件循环/客户端，用同身份和原字节恢复。脚本证据不等于真实对端去重或物理完成。
- 模型与迁移均有普通 `(status, expires_at)` 索引。EXPLAIN 保存为测试报告属性；不固定 planner 节点或毫秒阈值，不作吞吐承诺。

## 配置与使用

环境配置完整默认值见 `.env.example`。JSON 形状为：

```json
{"defaults":{"retry_base_s":1,"retry_cap_s":300},"targets":{"peer":{"base_url":"https://example.test","protocol":"my-protocol","max_retry_after_s":1200}}}
```

全局 `RELIABLE_MAX_RETRY_AFTER_S=3600`；JSON defaults 覆盖环境默认值，target 再覆盖 defaults。最终退避为 `max(local_jitter, min(peer_hint_ms / 1000, max_retry_after_s))`，默认本地 cap 为 300 秒。提示超过采纳上限才截断告警，TTL 不变。

业务域须自行提供同步、无副作用的 `ProtocolAdapter.build_request` / `classify_response`，在应用启动前注册：

```python
app.state.reliable_adapters = {"my-protocol": adapter}
```

`build_request` 返回 `HttpRequest`，正文必须等于冻结字节，并携带对端可去重的稳定身份；`classify_response` 只接收完整响应，返回 `Verdict`。`DELIVERED` 的 response 必须保留实际响应原字节；无合法协议证据的 429/502/503、身份不匹配由适配器归 UNKNOWN，不能按状态码猜已接收/未接收。

启用目标时 lifespan 产出 `app.state.outbox`；空配置为 None，但后台检查仍运行。调用者用自己的 session/事务：

```python
from app.reliable import MessageDraft

async with session.begin():
    reference = await app.state.outbox.enqueue(
        session,
        MessageDraft(
            target="peer", operation="opaque", operation_id=stable_id, request_raw=frozen_bytes
        ),
    )
```

`reference` 是入队时的引用/状态，不是异步业务结果。Celery/脚本可直接用同样的配置和注册表构造 `Outbox`，不创建 HTTP 客户端。核心没有业务/协议实现、结果消费/查询接口、同步 call、事件表或人工恢复接口；已有 Celery 发送任务未迁移。

迁移 `a019b20261021` 接在 `c9f2b7a41d08` 后；仅新增消息表及索引。降级对非空消息表拒绝执行，PG 用独占表锁保护检查。不得通过删除消息绕过守卫。

## 离线验证记录

- 外部优化后全量 PG/Redis 回归：**590 passed，0 skipped，0 warnings，24.46 秒**；此前 HTTP 单独回归 154 passed。Ruff check / format --check / diff --check 通过；Alembic 单 head `a019b20261021`，`alembic check` 无待生成变更。
- PostgreSQL 17、Redis 8 使用本轮新建的专用临时容器/内存卷；原有开发服务及数据未动。测试消息各自隔离、按所属身份清理，不运行生产种子。
- 永久测试覆盖并发领取、fencing、外层事务回滚、冲突与清理竞争、独立 TTL、双 API lifespan、连接池隔离、网络等待不占 DB、发送后保存失败、两种跨进程骤停、保留期微秒边界与安全降级。
- 全量命令：显式设置独立测试库的 `TEST_DATABASE_URL` / `TEST_REDIS_URL` 后运行 `uv run pytest -ra`；另运行 `uv run ruff check .`、`uv run ruff format --check .`、`git diff --check`、`alembic heads` 与 `alembic check`。不把未配置 PG 时的 skip 当验收。
- 本地 JUnit 证据保留在 `.superpowers/sdd/2026-10-21-reliable-interaction-plan/verification.xml`（未跟踪），用 `-o junit_family=xunit1` 保存 EXPLAIN 属性。代表性 2000 条数据中 3 条到期，有限扫描依次处理 2/1/0 条；本轮 planner 使用 `reliable_message_expiry_idx` 的直接 status/expires_at 范围条件，不将这个节点固定为回归断言。

### 独立审查与裁定

一名全新上下文审查者只读检查整个未暂存工作区（包含未跟踪文件），未发现 Critical/Important，提出两项 Minor。执行者将默认 HTTP 请求头丢失重新定为兼容性问题：两个公开发送回归先失败、恢复原 HTTPX 默认头与调用者覆盖后通过，随后全量 588 项通过；未增加 HTTP 重试、网络行为或校验副本。

外部优化后，DONE/PEER_TERMINAL 的终态原文仅保存在 `response_raw`，`last_response_raw` 清空以避免重复存储；其余尝试可在 `last_response_raw` 保存实际取得的有界诊断响应，分类器异常不伪造终态响应。文档按当前存储规则同步，不把旧“包含终态”措辞当作重复存储要求。

实施阶段的其余执行裁定：按用户要求在当前 checkout 的 feature 分支实施，不另建 worktree；当时不执行计划中的提交步骤，保留未跟踪执行账本；标准 review-package 因没有新提交拒绝空提交区间，因此改为 tracked diff + 全部新文件的只读审查。迁移接在实际 develop head，而非更早的审计迁移之后。停机按单一剩余预算分配各阶段，实际优雅等待会小于总计 15 秒，换取取消确认与资源清理的机会。

审查保留的范围裁定：真实对端去重/业务或物理完成仍需接入验收（否则可能重复执行）；安全/脱敏/限流/熔断/公平调度/结果与人工恢复保持后移（提前启用有数据暴露和重试放大风险）；配置迁移前必须排空（否则新去重库可能再执行）；可靠层预算不是进程退出 SLA；无生产容量/存储增长承诺。对应边界未扩张。

## 真实目标启用前

所有参与进程所在主机须先配置并监控时钟同步；应用使用 UTC 墙钟而非数据库统一授时，实例间偏差与调度、回写延迟合计须远小于默认 5 秒租约余量。UTC-aware 不等于主机时钟同步；不满足此前提可能提前回收仍在发送的租约，围栏不能防止重复投递。

必须另验对端同身份同内容重放/不重复执行、异内容冲突；有限去重窗口要求业务 TTL 不超过窗口。变更目标地址到新去重库或改变协议前先排空存量消息，首版不冻结配置、不提供程序化迁移保护。

终态保留默认 90 天，从进入终态的 `updated_at` 开始且不再刷新；清理后同身份成为新投递，没有 tombstone 或跨清理本地幂等。原文明文存储，安全/脱敏/访问控制、出口限流/熔断/公平调度、业务域接入及 SPEC §12 全部仍后移；执行预算不是出口限流。协议投递不代表业务或物理完成。本轮不启用任何真实目标。
