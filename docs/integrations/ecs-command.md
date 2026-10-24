# ECS 命令与结果闭环

本能力提供事务内命令登记、冻结投递、持久结果与授权查询。模板没有正式设备路由或附录，不会仅因配置目标就启用真实设备发送。

## 消费项目装配

显式创建 `EcsCommands(outbox, target, routes, annexes)`。`routes` 实现 `DeviceRouteProvider`：创建时解析生效快照，发送门禁中锁原授权记录并返回 ALLOW/REVOKED/UNKNOWN；A→B 不改写旧命令的 A，也不自动撤销 A。缺省 UnconfiguredRoutes 拒绝解析。

`annexes` 使用 `(annex_id, annex_version)` 映射到 `DeviceAnnex(params_model, result_model, error_model, timeout_min_ms, timeout_max_ms)`，必须是已确认的参数、结果、错误及 timeout 合同；测试附录仅在 tests 中，生产没有自动发现或回退。

运行时将 `EcsLifecycle(service)` 放进冻结协议 `ecs-command` 的生命周期映射，将 `EcsCommandAdapter()` 放进适配器映射，并注入 `DynamicClients`。使用 `reliable_lifespan(..., lifecycles=..., client_provider=...)` 或应用上的 `reliable_lifecycles`、`reliable_client_provider`、`reliable_adapters` 装配。应用还须设置 `app.state.ecs_commands=service`；lifespan 会将其 outbox 关联到当前发送器。

SNAPSHOT 目标仅声明逻辑协议及 HTTP 资源参数，`base_url` 省略/为 null；每条消息冻结 origin。当前快照仅接受已实现的 HOLD，快照 RETRY 明确拒绝；STATIC 保持原 RETRY 行为、三参数和 base_url 规则。ECS 路径同时缺省路由、附录及发送装配，结果入口在未装配时返回 503，不会 ACK 掉无法处理的结果。

正式发送前必须关闭设计稿 C1/C2/C3/C5，确认 params/source、错误矩阵、跨重启去重及保留期、窗口与路由授权。回调来源限制必须落实。本文不提供真实地址或开箱即用的设备命令。

## 事务内创建与读取

调用 `await service.create(session, owner=CommandOwner(domain), call_key=..., device_code=..., task_type=..., timeout_ms=..., params=...)`，由调用者提交。owner 来自受信任业务装配，不能从外部请求任意选择；没有生产 HTTP 发命令接口。

同业务调用键、同原输入返回旧命令；不同输入冲突。创建先查旧命令，再解析路由并生成一次 UUIDv7、时间和原始请求。回滚不留下命令/出站消息。技术重试保持 command_code、时间、字节和冻结 origin，不按设备历史失败封锁新独立意图。

`get_command(session, owner=..., command_code=...)` 和 `get_by_call(session, owner=..., call_key=...)` 返回只读 DTO，区分 command status、delivery status、实际 ACK、已应用结果、暂停原因和对账标记。查询不返回可靠层 ORM。消费方在自己的事务用 `(consumer, command_code)` 唯一记录和业务更新保证只推进一次；不能凭 ACK/DONE 推断物理完成，也不自动撤销已应用业务。

## 发送与结果

POST `/api/v1/device/command` 使用白皮书字段。仅合法 200/ACK 为已接纳；合法 503、有效 429 和可证明 NOT_SENT 可按原身份退避。UNKNOWN、409 身份冲突或租约中断进入 HELD，不自动重放；HELD 不支持普通 resume，不由 TTL 或终态保留清理。快照公共 hold/settle 必须注入原冻结协议的生命周期；ECS 裸 settle 在没有可应用的持久结果时被拒绝，不能凭基础设施 API 伪造接纳。STATIC 的空配置 settle 保持原行为。ECS Retry-After 不截短，超本地等待范围暂停；WMS 保留原截短与重试规则。

门禁提交当前尝试依据后才发 HTTP；首次或后续尝试仍可能交付时，Result 可先于 ACK 应用。历次均明确未接纳且没有新在途尝试的结果只保存冲突。提前 Result 封锁并撤销租约；封锁不由迟到结果解除。迟到响应只保存证据与必要的对账标记，不降级已应用结果。

POST `/api/v1/callback/result` 严格校验核心包络及冻结附录，忽略顶层诊断扩展；成功提交后返回 200/ACK。未知命令/设备不匹配 404，提前/矛盾 409，非法输入 400，超限 413，事务或原附录不可用 503。结果证据、收据、命令投影及 settle 同事务。长期证据在短期收据/可靠消息清理后仍提供原响应，不重新创建发送义务。Event 接口保持原语义。

本项目内的通用能力隔离参考接入与端到端验收见[隔离设计](../superpowers/specs/2026-10-25-ecs-first-consumer-isolation-design.md)与[实施计划](../superpowers/plans/2026-10-25-ecs-first-consumer-isolation-plan.md)。参考目录、记录及消费只存在于 tests；通过不代表供应商、生产路由或现场验收，也不要求先指定外部业务项目。

## 发布与验证边界

迁移 `a025a20261025` 扩展可靠状态/快照并创建命令、发送依据和结果证据表。旧 STATIC 数据保持可读。存在快照、HELD 或 ECS 证据时拒绝破坏性 downgrade。停用所有旧 Sender/维护进程后，执行 `uv run alembic upgrade head`，再启用新版本；不支持旧二进制回退后解释新消息。本地 `--reload` 仅重载代码，不会自动迁移数据库。若 Sender 日志出现 SQLSTATE `42703`（缺列）或 `42P01`（缺表），先用 `uv run alembic current` 对照 `uv run alembic heads` 检查迁移状态，按上述停机顺序升级，勿通过捕获后忽略异常继续发送。

本地快测使用 SQLite；并发、唯一键竞争、锁序、迁移与回滚必须使用显式一次性 PostgreSQL/Redis。测试与模拟对端不证明供应商去重、真实物理动作或业务现场验收。Status、具体 Event、结果纠正、人工重发与交互控制台不在 P1。


## 后台日志排障

可靠发送器日志用 `phase` 区分快照维护（`maintain_snapshots`）、过期处理（`expire_pending`）、租约回收（`recover_expired`）、领取（`claim`）、派发（`dispatch`）及独立投递（`delivery`）；关闭资源时记录对应清理阶段。数据库异常保留 SQLSTATE、驱动异常类型及迁移提示，调用栈仅保留文件、行号和函数，不输出原始异常消息、SQL、参数或源码。

扫描首次失败输出完整诊断。同阶段、异常类型、驱动类型、SQLSTATE、缺失对象、约束名和抛出位置均相同的连续失败，在每 60 秒后的下一次失败时汇总总次数与本窗口抑制次数，不重复堆栈。每条汇总仍保留首次诊断的 SQLSTATE、驱动类型、项目内调用位置和修复提示；标准 PostgreSQL 缺列/缺表错误还给出受限格式的 `missing` 对象名，不回显任意驱动消息。错误变化时先用原诊断结算被抑制次数，再立即输出新诊断。完整扫描再次成功后记录一次 `scan recovered`，包含失败总次数、尚未汇总的抑制次数和持续时间；满并发而跳过领取的扫描不作为恢复证据。计数仅保存在当前 Sender 内存中，重启后重新开始。独立投递任务的异常逐条记录，避免合并不同消息的问题。

日志降噪不改变轮询间隔、重试、事务或未知交付处理。`scan recovered` 只表示扫描恢复，不证明消息已交付、设备已执行或业务已完成。
