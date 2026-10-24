# ECS 命令与结果闭环实施计划及 develop 核对

> **执行状态：** P1 实现、后台错误日志优化及两轮 simplify 已完成隔离验证。用户已授权将最终改动与本计划进度一起暂存并提交；推送、本机数据库迁移和真实设备发送仍未授权。下列进度以实际文件和测试为准，不把隔离机制验收等同于供应商或现场验收。

**Goal:** 在业务事务内登记冻结 ECS 命令，通过唯一可靠 Sender 投递，持久接收关联结果，并向授权消费方公开可区分接纳与物理结果的查询。

**Architecture:** 保留单次 HTTP 客户端和一个可靠 Sender；可靠层仅增加快照、HOLD/HELD 与注入的事务生命周期能力。ECS 层拥有命令、尝试依据、结果证据、路由授权和业务投影；网络 IO 位于数据库事务之外。

**Tech Stack:** Python 3.13+、FastAPI、SQLAlchemy async、Alembic、现有 HTTPX 封装、pytest/pytest-asyncio；不新增依赖或重试框架。

**Spec:** ../specs/2026-10-25-ecs-command-result-design.md（工作区版本，不仅是暂存版本）。

**Source SHA-256:** `464fb5732293103451a7b1ded7e3474992969c5b69341f73a88dd4ee6185b8a3`。

**Status:** P1_IMPLEMENTED_ISOLATION_VERIFIED。实现分支 `feature/ecs-command-result-p1` 基于下述 develop 基线；S1/S2、评审修复及后台日志优化均已实现。最近完整隔离回归为 **2023 passed, 11 skipped**；提交已获授权，未推送、未迁移本机数据库、未启用真实设备发送。

## Global Constraints

- 基线是 develop @ `9f912d6cccf36d821e05b6424b65fd5ea5d229ae`；源稿 §1.2 的 c65c014/注册暂存快照已过时。当前函数式注册已合并，旧 CallbackOperation/RequestOperation 不能作为新依赖。
- 命令 `POST /api/v1/device/command`；结果 `POST /api/v1/callback/result`；Event 原语义不动；Status 仅诊断并延后。
- 请求闭集 `device_code, command_code, task_type, priority, timeout, timestamp, params`；priority 固定严格整数 1；核心整数排除 bool；单消息 256 KiB；无 source 双协议兼容。
- `command_code` 全局唯一；业务调用键独立；时间、地址、协议、策略、附录和请求字节首次冻结。ACK、可靠 DONE、设备 SUCCESS、消费方业务完成各自独立。
- UNKNOWN/中断不自动重放；HELD 不领取、不普通 resume、不 TTL/终态清理；不以历史失败阻塞同设备的新独立有效意图。
- 所有双表/多表状态变更在一个数据库事务；锁顺序为命令 → 原授权（门禁才需要）→ 消息 → 结果收据。不持锁发 HTTP、获取/关闭客户端。
- 不新增库存逻辑、WorkLine、取消、纠正版本、回放、结果总线、人工重发接口、真实设备附录或 ECS 交互控制台。
- 本轮提交仅包含 ECS P1、后台日志优化、simplify 及相关设计/计划/接入文档、测试和 TODOS 风险说明；不重排其他待办。其他改动及 stash 保留；推送、部署、本机迁移和真实设备发送仍需单独授权。

## 已确认并同步修正的两项

### S1 / P1：首次在途结果准入（已修正）

修订前源稿 §5.3 准入行及总则要求“至少一次未知尝试或合法 ACK”，同时 A9/§5.1 要求首次 Result 可先于发送响应到达并闭环。时序：首次门禁提交 → HTTP 已被 ECS 接纳 → Result 到达 → ACK/classify/finish 尚未落库。此时没有历史 UNKNOWN 或已保存 ACK；若只检查这两项会拒绝合法结果；若只查 dispatch_started_at，又重开 R5 已修复的“历次均明确未接纳仍应用 Result”漏洞。

**确认规则：** 结果可应用依据是“存在门禁已提交、尚未被明确未发送/未接纳结论排除的当前尝试，或历史交付未知，或合法 ACK”。仅已领取、门禁未提交不算；历史 dispatch_started_at 单独不算。明确拒绝、身份冲突、提前结果等封锁优先级高于此依据。

用一次发送尝试的持久依据表达该区别，不从 QUEUED/DISPATCHING 状态名反推。当前尝试即使实际上尚未来得及发网，也只能表示“可能”，不伪造字节已离开证明。结果先应用后迟到 NOT_SENT/拒绝保持原结果，保存矛盾并标记 needs_reconciliation；与源稿按提交顺序保留先应用结论的原则一致。

新增成对验收：首次 Result 抢在 HTTP 返回前得到 ACK；明确 NOT_SENT/503/429 落库后的退避期同样 Result 得到持久 409。再覆盖第二次门禁已提交、但上次明确未接纳时的 Result，不能沿用上次结论拒绝当前在途尝试。

### S2 / P2：维护 deadline 与首次锁前超时（已修正）

修订前源稿 §5.2 已有 NOWAIT + 整轮预算，但实现合同还需覆盖表锁、数据库连接获取和端口内其他等待。当前 `Sender.tick` 先 await 两类维护再领取；只在循环轮次之间看时钟，无法中断某条 await。当前 insert_receipt 在插入收据前才设 5s lock_timeout；新 Result 如果先锁命令，必须提前设置自己的事务超时，不能指望之后的收据函数保护之前的锁。

**确认规则：** 快照恢复与到期共享一个从候选扫描前启动的单调时钟整轮 deadline，预算复用 `ReliableConfig.poll_interval_s`（现默认 1s），不新增环境参数。扫描、连接获取、端口调用、SQL、提交和取消/回滚均纳入，并预留清理时间；单条执行上限不超过剩余预算与现有 `db_pool_timeout_s` 的较小值。首次业务锁前设置事务局部 `lock_timeout`/`statement_timeout`，不超过该条剩余预算；不足以设置正数或启动并清理下一条时退出，不能用 0（禁用超时）代替到期。预算耗尽不再开始候选，有界清理后让出领取，失败推进游标。端口须响应取消、不在后台继续持锁；回滚/连接状态不明时隔离告警、不复用。Result/正常门禁/finish 在首次命令锁前设置既有 5s 锁/语句上限，连接获取仍受现有连接池上限约束；维护覆盖为更短的剩余预算。旧 WMS 不变，数据库整体不可用只报告故障，不声称健康消息仍可发送。

PostgreSQL 官方明确 NOWAIT/SKIP LOCKED 只约束行锁，所需表锁仍正常等待；lock_timeout 覆盖表、索引和行锁，statement_timeout 覆盖整条语句。来源见本文末尾。

## 当前实现可以复用与不能照搬的接缝

| 当前文件/入口 | 已有能力 | ECS 必须增加/隔离的行为 |
|---|---|---|
| app/http_client/{client,types,validation}.py | 单次请求、NOT_SENT/UNKNOWN、完整响应、总超时、大小/编码限制 | origin-only 规范化另作小函数；当前 base_url 允许路径前缀，不能直接视为 ECS origin 授权；客户端不导入 ECS/DB |
| app/reliable/types.py、config.py | 不透明正文、静态 target、Verdict、可选同身份比较器 | 有兼容默认值的快照、地址模式、HOLD 判定；ECS 不实现 ComparableRequests |
| app/reliable/outbox.py:57/128 | 唯一键竞争、冻结字节、事务内入队 | 重入还比完整规范化快照；SNAPSHOT 不填假 base_url |
| app/reliable/delivery.py:15/82 | 完整响应分类；UNKNOWN 排期；本地异常 FAILED/LOCAL_ERROR | 仅快照 HOLD 路径按执行阶段保守暂停；不全局改 WMS 兜底 |
| app/reliable/store.py:127 | finish 用状态+token 围栏；失效时直接丢弃回写 | 在命令锁后复核消息；响应晚到需 ECS 旁路保存；无效 token 不做正常投影 |
| app/reliable/store.py:225/330 | 批量锁消息后恢复/到期，消息单表更新 | 不在这些持有消息锁的事务内调用 ECS；快照分流为候选扫描+逐条按序锁+原子投影 |
| app/reliable/outbox.py:178 | 空配置也可 settle；失效租约不写 ACK | 保留 Transport 语义；新增 HELD 可 settle，ECS 只在领域准入通过后调用 |
| app/reliable_runtime.py、sender.py | 静态客户端、共享并发、空 targets 仍维护、统一停机 | 按地址有界借用/关闭；快照无配置候选也能进入维护；缺装配 fail closed |
| app/integrations/ecs_callback/{envelope,receiver}.py | 严格 JSON、先查重、脱敏 issues、事件收据 | 复用小工具，不把 Result 注册为 Event；EXTRA_FORBIDDEN 只增新映射，不改变现有事件路径格式 |
| app/integrations/callback_receipts.py | 插入/查询/首响应、并发唯一键、锁超时 | 只是短期收据；不能替代长期结果去重，命令锁必须先于它 |

## Review Focus

1. 首次/重试 HTTP 仍在途与历次明确未接纳必须可区分：Task 2/5/6 的成对竞态断言。
2. 租约在客户端获取、命令锁等待和门禁提交后耗尽时不迟发：Task 3/5 的 fake clock + PostgreSQL 对抗时序。
3. 收据和可靠 DONE 已清理后仍去重、仍接收首次合法结果、不重建发送义务：Task 2/6/7。
4. 未配置 target、缺端口或坏候选不能把 ECS 误恢复发送、不能永久占住共享 Sender：Task 4/5/8。
5. 原附录不可用和越权查询不被当成供应商错误或跨域成功：Task 2/6/7。

## 模块与数据布局（已实现）

| 文件 | 唯一职责 |
|---|---|
| app/integrations/ecs_command/contracts.py | ECS Command/Result 包络、命令状态、摘要规范化、只读 DTO |
| app/integrations/ecs_command/routing.py | DeviceRouteProvider、冻结路由与 origin 规范化；缺省拒绝提供者 |
| app/integrations/ecs_command/protocol.py | 只构造 Command 请求和按完整响应分类；Retry-After 解析 |
| app/integrations/ecs_command/service.py | 命令业务调用幂等创建与授权查询；调用者事务所有权 |
| app/integrations/ecs_command/lifecycle.py | 门禁、发送投影、维护、迟到证据；统一领域锁顺序 |
| app/integrations/ecs_command/result.py | Result 收据/证据/领域状态/settle 同事务 |
| app/models/ecs_command.py | EcsCommand、EcsSendEvidence、EcsResultEvidence 三个模型及约束 |
| app/reliable/client_provider.py | 仅协议无关借用端口；动态实现放 app/integrations/ecs_command/clients.py 并由 runtime 注入 |
| app/reliable/lifecycle.py | 仅协议无关事务端口；不导入 ECS 模型 |
| app/controllers/ecs_result.py | 256 KiB 入口、外层事务、提交后 ACK、失败 503 与脱敏 |

不为每个动作建工厂/策略类，不增加第二套发现机制。设备附录使用 `(annex_id, version)` 显式映射到参数/结果模型；只在测试装配测试附录。三张领域表的理由：命令保存应用状态，结果表保存长期首响应与冲突，发送证据保存当前在途依据及失效租约迟到响应；不能只依赖将被清理的 reliable_message.last_*。

核心约束：EcsCommand.command_code 全局唯一；业务调用键 `(owner_domain, call_key_digest)` 唯一且保留完整 call_key 并在摘要相同后复核；EcsSendEvidence `(command_code, lease_id)` 唯一；EcsResultEvidence `(command_code, digest)` 唯一且重报比较完整规范化核心；applied_result_id 只设置一次并归属本命令。长期证据不依赖可靠行的级联外键，不自动清理。

## Task 1：冻结协议模型与路由端口（A3/A5/A12）

- [x] `contracts.py`、`routing.py`、`protocol.py` 实现严格包络、origin 规范化、缺省拒绝路由端口及 ECS 响应分类；测试附录不进入生产装配。
- [x] 非法/非有限 ACK 保持未知交付；Retry-After 持久化且不截短；SNAPSHOT 仅接受已实现的 HOLD，STATIC/WMS 保持原规则。
- [x] `test_protocol.py` 覆盖响应矩阵、非法 ACK、Retry-After、origin 和未实现策略；timeout/附录约束由 `test_service.py` 验证。原计划的 contracts/routing 独立测试已合入这些文件。

## Task 2：持久身份与事务内创建（A1/A2/A10/A13/A14/A16/A18）

- [x] `EcsCommand`、`EcsSendEvidence`、`EcsResultEvidence` 与可靠消息在同一条增量迁移中落地；长期证据不依赖短期消息/收据清理。
- [x] 按 owner 与完整业务调用键幂等创建，冻结命令身份、原输入、字节、地址、附录和共同截止时间；保存点不破坏调用者事务。
- [x] 门禁提交保存本次可能交付依据；明确未接纳结论仅排除相应尝试，实际 ACK 独立记录。
- [x] `test_service.py`、`test_concurrency.py`、`test_result.py`、`test_flow.py` 验证重入、回滚、并发、持久证据与结果准入；原计划的 evidence 测试合入结果/流程测试。

## Task 3：快照入队、HOLD 判定与暂停 API（A6/A7/A16/A18/A26）

- [x] 可靠类型、配置、Outbox、模型新增 SNAPSHOT/HOLD/HELD；原 STATIC 构造和默认行为保持兼容，SNAPSHOT 不填写假 base_url。
- [x] 同身份重入比较完整快照与字节；HELD 不自动领取、resume、到期清理；公共快照 hold/settle 必须注入原冻结协议的事务生命周期。
- [x] `test_snapshot.py`、`test_protocol.py`、`test_flow.py` 及现有可靠层配置、重试、settle、预算和保留期测试通过。

## Task 4：事务生命周期与维护分流（A7/A12/A15/A19/A21/A23/A24）

- [x] `app/reliable/lifecycle.py` 提供协议无关事务端口与快照维护，`ecs_command/lifecycle.py` 负责领域投影；两者分层，不引入第二套重试调度。
- [x] 按命令 → 原授权（门禁）→ 消息 → 收据顺序加锁，先复核租约再投影；维护共享整轮 deadline，失败回滚、推进游标并隔离坏候选。
- [x] PostgreSQL 行为覆盖创建/结果并发、表锁有界等待、授权锁顺序；投影失败与混合坏快照不能破坏健康 STATIC 消息。证据位于 `test_concurrency.py`、`test_flow.py` 及现有可靠层用例。
- [x] 评审发现的坏候选阻断共享领取、公共 hold/settle 投影分裂、门禁锁顺序问题均已修复，并保留先失败后通过的回归。

## Task 5：动态客户端与受门禁保护的发送（A3–A9/A15/A20–A24/A26）

- [x] `ClientProvider` 端口与 ECS `DynamicClients` 显式注入，默认上限 64，复用 origin 并将创建/借用/关闭计入容量；无真实路由或附录默认值。
- [x] 获取客户端后再走事务门禁，提交本次依据后才发送；门禁后租约到期不续租迟发，不持数据库锁发 HTTP。
- [x] UNKNOWN、身份冲突及租约中断保持 HELD，不自动重放；迟到响应保留证据，不降级已应用结果。
- [x] `test_clients.py`、`test_flow.py` 覆盖容量、并发借用/取消、关闭失败、完整投递转换、统一 Sender 和旧 STATIC 路径。

## Task 6：独立结果入口与长期重放（A8–A13/A15/A22/A25）

- [x] `POST /api/v1/callback/result` 独立接收，256 KiB 上限、严格核心包络、冻结附录校验；结果证据、收据、投影和 settle 同事务，成功提交后 ACK。
- [x] 按 S1 区分当前可能交付与历次明确未接纳，提前结果持久冲突并撤销租约；封锁不被迟到结果解除。
- [x] 首次响应长期保存；短期消息/收据清理后重报不重建发送义务。原附录不可用返回本地 503，未知命令不形成永久负缓存。
- [x] `test_result.py`、`test_flow.py`、`test_concurrency.py`、`tests/controllers/test_ecs_result.py` 覆盖在途竞态、冲突、提交失败、重放、非法输入和未装配入口；Event 原语义回归通过。

## Task 7：授权查询与消费侧恰好一次推进（A2/A13/A14/A25）

- [x] owner 限定的命令/调用键查询返回只读 DTO，分别表达命令状态、投递状态、实际 ACK、已应用结果和对账原因，不暴露可靠层 ORM。
- [x] `test_service.py` 验证跨 owner 不可见；`test_query.py` 用测试消费模型演示业务事务内唯一消费记录，并通过 PostgreSQL 并发只推进一次验收。
- [x] 消费只依据已应用结果；ACK/DONE 不代表设备 SUCCESS，不自动撤销已完成业务；未新增生产消费表或结果总线。

## Task 8：迁移、回归与安全装配门槛（A1–A26 汇总）

- [x] 单一 Alembic head `a025a20261025` 接 `a024a20261024`；`test_ecs_command_result.py` 在 SQLite/一次性 PostgreSQL 验证迁移往返、旧 STATIC 数据保留与破坏性 downgrade 拒绝。
- [x] 默认不装配正式 ECS 路由、附录或客户端；未装配结果入口返回 503，不能 ACK 掉未处理结果。
- [x] README、AGENTS、`.env.example`、接入指南、设计稿及 TODOS 中的 ECS 伪造结果风险说明与实现同步；保持原待办格式。
- [x] A1–A26 的隔离机制证据覆盖身份/事务、协议矩阵、HOLD、锁/租约、保留期、查询/消费和 STATIC/WMS 兼容，分布于上述 ECS 测试及现有 WMS request、Transport、Event 和可靠层测试；未把供应商或现场门槛标为完成。
- [x] 完整隔离回归 **2023 passed, 11 skipped**；Ruff lint/format 与 `git diff --check` 通过。
- [ ] 本机/部署库升级及版本切换：本轮明确不执行。仍须先停用所有旧 Sender/维护进程，再迁移并启新版本；旧二进制不能读取新快照/HOLD。
- [ ] 供应商合同、真实路由授权、回调来源限制及现场设备验收：保持下文 C1–C7 门槛，不启用真实发送。
- [x] 用户已单独授权本轮最终改动重新暂存并提交；推送/合并/部署不在本轮授权范围。

## 后台错误日志优化与 simplify

- [x] 扫描日志区分 `maintain_snapshots`、`expire_pending`、`recover_expired`、`claim`、`dispatch`；独立投递和关闭资源错误记录对应阶段。
- [x] 首次失败输出脱敏诊断；同类连续扫描错误在 60 秒后的下一次失败汇总；错误变化立即诊断，恢复只在完整扫描成功后记录。满并发跳过领取不报恢复，取消不算故障。
- [x] SQLSTATE/驱动类型/迁移提示及栈位置可排障，不输出原始异常消息、SQL、参数或源码；独立投递异常逐条保留。
- [x] ECS simplify 合并 SQLite 保存点前的事务准备与重复对账状态更新，简化等价状态分支；日志 simplify 合并结构化字段与冗余空值保护。协议、事务、幂等、重试及未知交付语义保持不变。
- [x] `tests/reliable/test_sender_logging.py` 新增 13 项回归，连同 `test_sender.py` 的脱敏/轮询回归通过；simplify 前后相关回归均为 25 passed, 17 skipped。

## 验收映射与交付顺序

| 阶段 | 覆盖矩阵 | 可独立核验的成果 |
|---|---|---|
| Task 1 | A3/A5/A12 | wire 与路由纯边界；不开放发送 |
| Task 2 | A1/A2/A10/A13/A14/A16/A18 | 持久身份与调用者事务；不开放发送 |
| Task 3 | A6/A7/A16/A18/A26 | 通用快照/HOLD，旧 STATIC 回归仍绿 |
| Task 4 | A7/A12/A15/A19/A21/A23/A24 | 持久维护、锁序、失败隔离 |
| Task 5 | A3–A9/A15/A20–A24/A26 | 隔离 ECS 发网闭环，不连接真实设备 |
| Task 6 | A8–A13/A15/A22/A25 | Result HTTP、长期幂等与竞态闭环 |
| Task 7 | A2/A13/A14/A25 | 授权查询与测试消费方幂等推进 |
| Task 8 | A1–A26 | 全量兼容、迁移与发布约束；P1 机制验收 |

Task 1–7 是分层验证点，当前已完成统一 P1 隔离机制验收；不能将任一层单独视为可投产能力。Result 未装配时不 ACK；真实开放仍须完成供应商、路由授权、来源限制及现场门槛。不为拆阶段新增运行时开关。

## 供应商与现场门槛（保留原 C1–C7，不代答）

- C1 params/source 与实际附录、C2 非 200 code/Retry-After、C3 跨重启去重和保留期：真实发送前关闭；隔离机制测试可先开展。
- C4 无纠正权威时人工核对，不做自动终态改写；C5 消费方路由生效/撤销端口为真实接入前置条件。
- C6 64 客户端上限只为本地容量提案，现场按实际资源核定；C7 具体 Event 接入另行处理历史 ACK 与可区分性。
- Result 的伪造终态风险沿用源稿已登记的网关来源限制与认证待办；不默认为增加代码即可解决。真实开放回调前须证实该入口的来源限制已生效。

## 本轮验证与限制

- 设计稿内容 SHA-256 与上文一致；未修改 ECS/WMS wire 合同。Task 1–7 的实现与隔离验收完成，Task 8 的本机迁移及现场步骤明确保留未完成。
- 最新完整验证：显式 `TEST_DATABASE_URL` / `TEST_REDIS_URL` 指向独立临时 PostgreSQL/Redis，执行 `uv run pytest -q -rs`，结果 **2023 passed, 11 skipped in 103.32s**。11 个跳过均为 PostgreSQL 专用用例的 SQLite 参数，对应 PostgreSQL 参数通过。
- `uv run ruff check .`、`uv run ruff format --check .`、`git diff --check` 通过。临时测试容器及其匿名卷已清理，未操作现有开发存储。
- PostgreSQL 专项证据包括：创建/结果竞争、消费唯一键、表锁 deadline、授权锁顺序、事务失败回滚与迁移往返；HTTP 仅使用隔离模拟对端，不代表供应商去重或物理完成。
- 原仅文档评审阶段的 75 passed / 29 skipped 不再作为当前验收证据；未执行的供应商请求、真实设备、现场/生产回调验证仍然未验收。
- 本机开发库尚未执行 `a025a20261025`；此前只读复现的缺列故障须按接入指南的停机迁移顺序另行处理。日志优化只改善排障和降噪，不消除数据库版本差异。
- 本轮仅准备并完成已授权的本地提交；不推送、不部署、不迁移本机数据库、不启用真实设备发送，保留其他改动及 stash。

## 参考依据

- 源设计稿 §3–§9、当前 develop 源文件与现有测试，是本实施计划的主要依据。
- [PostgreSQL 17 SELECT locking clause](https://www.postgresql.org/docs/17/sql-select.html#SQL-FOR-UPDATE-SHARE)：NOWAIT/SKIP LOCKED 的行锁边界及表锁例外。
- [PostgreSQL 17 statement/lock timeout](https://www.postgresql.org/docs/17/runtime-config-client.html)：事务内局部 SQL 超时的适用范围。
- [RFC 9110 §10.2.3 Retry-After](https://www.rfc-editor.org/rfc/rfc9110.html#name-retry-after)：非负整数秒/HTTP-date；本设计额外采用失效/歧义提示暂停的保守合同。
