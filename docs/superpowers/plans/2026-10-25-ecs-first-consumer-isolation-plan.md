# 首个消费项目 ECS 单设备、单动作隔离接入实施计划

> **For agentic workers:** 代码实施获批后，使用 superpowers:executing-plans，或用户明确选择时使用 superpowers:subagent-driven-development，逐任务执行。当前仅授权文档，所有步骤均未执行；不得因计划完成自动开始实施。

**Goal:** 在首个明确的消费项目内，隔离验证一个设备、一种动作从业务登记到结果消费的完整闭环。

**Architecture:** 消费项目复用已合并的 ECS P1；目录授权、附录、业务映射与消费归消费项目，Sender/协议/长期证据归底座。业务登记与 outbox 同事务，消费记录与业务推进同事务；HTTP 位于事务之外，不另建重试调度器。

**Tech Stack:** 沿用消费项目现有 Python/FastAPI/SQLAlchemy/pytest/HTTPX 与 P1；一次性 PostgreSQL/Redis。若项目不具备同事务接入条件，先重审架构，不默认新增依赖、服务或桥接层。

**Spec:** [首个消费项目隔离接入设计](../specs/2026-10-25-ecs-first-consumer-isolation-design.md)。

**Status:** DOCUMENTED_INPUTS_PENDING。方案方向已选定；本文待审阅，I1–I8 均 OPEN。真实项目和业务尚未指定，因此这是带显式输入门禁的计划，不是可直接执行的编码清单。

**Baseline:** maintenance-admin develop @ `ed9005ff0c12e6b090cfcf6cb19ffa3df4a44eea`；PR #22 合并 CI 2054 passed、11 skipped，Ruff/格式通过。此证据只覆盖 P1，不覆盖本文尚未实现的消费接入。

## Global Constraints

- 当前只改文档，不代码、不提交、不推送、不迁移本机数据库、不启用真实设备发送；保留其他文件、暂存区和 stash。
- 代码实施前先满足设计 §2 的输入条件并取得独立授权；绑定到明确项目后从其当前已验收 develop 建 feature 分支，不直接在 develop 写实现。
- 单设备、单动作；对照模拟 origin 可有 A/B。测试附录与窗口明确标为隔离合同，不进入主应用默认装配，不声称关闭供应商 C1/C2/C3。
- I1 必须证明业务登记、命令、outbox 可使用同一 AsyncSession/数据库事务。业务消费必须有持久唯一身份并与业务写同事务。
- 复用原 EcsCommands、DeviceRouteProvider、DeviceAnnex、EcsLifecycle、EcsCommandAdapter、DynamicClients 和唯一 Sender；不直接操作底座 ORM推进业务。
- 原身份/字节/origin/附录冻结、命令→原授权→消息→收据锁序、HELD/未知交付、WMS STATIC、日志脱敏及独立降噪/恢复均不变。
- 不加入 Status、具体 Event、自动结果超时、自动纠正、取消、强制重发、HELD 解封接口或生产控制台；阶段性只读查询复用现有服务和权限。
- 本阶段只运行隔离存储、独立队列与回环模拟 ECS；不读取本机 .env 选择运行目标，不复用开发/生产数据库、Redis 或设备地址。

## 文件绑定表：Task 0 完成前禁止创建这些实现文件

下表的 F 编号是责任位置，不是目录名或新包建议。由 I1/I4/I6 确定真实路径；已存在的等价文件/模型优先复用。Task 0 须把路径、符号、测试命令绑定为可读的实际值，替换后才能审阅开工；不得以 `consumer`、D1、MOVE 等占位符建立生产接入。

| 绑定 | 内容 | 变更策略 |
|---|---|---|
| F1 | 消费项目目录/生效配置的模型、repository、授权实现文件及并发测试文件 | 复用目录；必要授权记录/约束须先确认锁语义，不复制设备主数据 |
| F2 | 一个动作的参数/结果/错误附录文件及合同测试文件 | 按实际附录版本定义，测试合同与正式合同分离 |
| F3 | 现有业务 service、业务映射/消费记录模型及其行为测试文件 | 复用现有幂等记录；缺少时仅加最小持久约束；迁移只在一次性库验收 |
| F4 | 消费项目实际应用装配文件，以及专用隔离测试装配/模拟器和共享夹具文件 | 实验进程显式装配；生产入口保持不默认启用 |
| F5 | 现有查询/权限 service及测试、场景运行说明和证据位置 | 无 HTTP 产品需求则仅保留 service查询；不为本阶段引入页面 |
| T | 上述行为测试集合的具体文件列表 | 根据消费项目 AGENTS/测试约定填写实际 `uv run pytest ...` 命令，不从其他测试文件导入夹具 |

maintenance-admin 的实现依赖为 `app/integrations/ecs_command/{service,routing,contracts,lifecycle,protocol,clients}.py`、`app/reliable_runtime.py` 和 `app/controllers/ecs_result.py`；默认只复用，不修改。底座回归入口为 `tests/integrations/ecs_command/`、`tests/reliable/`、WMS request/Transport 和日志相关现有测试。

## Review Focus

1. 消费项目不与底座共用事务时，禁止用两次提交冒充原子登记——Task 0/2。
2. 设备改址、撤销和绑定变化时，旧命令/结果不能变成新一轮业务许可——Task 1/2/3。
3. 消费唯一记录插入成功但业务更新失败时必须一起回滚——Task 3。
4. 回调先到、附录缺失、迟到冲突和重启不能触发换号重发或假成功——Task 2/3/4。
5. 错误环境配置或多目标混跑不能访问开发库、现场设备或破坏 WMS 原策略——Task 4/5。

## Task 0：绑定输入、文件和验收合同（当前阻塞项）

**Files:** 本设计和本文；I1 确认后只读检查目标项目的 AGENTS、业务/目录/事务/权限及测试入口，未授权前不修改目标项目。

**Interfaces:** 输入为设计 I1–I8；输出为已确认输入清单、F1–F5/T 的实际路径/符号/命令、业务状态迁移表及隔离/正式合同区分。

- [ ] 指定消费项目及基线，核对 P1 引入方式和同事务可行性；不满足则记录阻断原因并停止代码阶段。
- [ ] 由业务负责人确认设备/动作、owner、调用键、原业务对象映射、SUCCESS/FAILED 处理及消费触发点；明确消费与取消/换绑/版本变化的判定点、两种竞争顺序的状态迁移，以及既有业务行锁/版本条件更新或等价机制。绑定所有相关业务写入口和统一锁序；不能由代理推断，不能仅凭消费唯一键关闭 I6。
- [ ] 由目录/协议负责人确认实际目录及撤销串行化、版本化附录和窗口；正式合同缺失时明确哪些仅是测试合同，不勾供应商门槛。
- [ ] 由环境负责人确认一次性存储/队列、回环地址和清理归属；列明拒绝真实目标的方法及不会加载本机 .env 的运行配置。
- [ ] 填完 F1–F5/T，核对目标项目现有模型与迁移；让任务拥有实际路径、精确接口和可运行测试命令。未完成的输入逐项记录负责人/所缺证据。
- [ ] 对输入绑定后的设计/计划取得用户确认及代码授权，再创建实现分支；本次不执行该动作。

**验收：** I1/I2/I4/I6/I7 关闭，I3/I5 有明确隔离版本，文件表和测试命令无符号路径；I8 保留真实门槛，不假称已完成。不能满足时状态保持 INPUTS_PENDING，不进入 Task 1。

## Task 1：目录授权与单动作附录

**Files:** F1/F2 及其合同/并发测试；仅在确需持久约束时列出具体消费项目迁移，不改底座表。

**Interfaces:** 实现 `resolve(session, device_code, task_type) -> RouteSnapshot`、`authorize_frozen(session, route) -> RouteAuthorization`；输出版本化 `DeviceAnnex`，供 Task 2/4 显式注入。

- [ ] 先补失败行为测试：`test_missing_or_ambiguous_route_refuses_command`、`test_effective_route_and_original_authorization_are_distinct`、`test_frozen_annex_validates_payload_and_timeout`。拒绝分支断言无命令/消息副作用；无法确认授权按 UNKNOWN 处理。补 `test_pending_route_does_not_replace_effective_route` 与 `test_no_authorized_effective_route_refuses_command`：待生效 B 不替代有效 A，只有本次新建无法取得唯一、有效且允许该动作的配置才拒绝，不能把“存在待生效配置”或“历史 A 撤销”一律判为不可创建。补 `test_revoked_old_route_allows_new_intent_on_effective_route`：A 已撤销但 B 已生效且获准，新独立调用冻结 B；旧调用核对原输入后返回原身份/字节/A，旧命令在撤销先提交的门禁中不发送、不改投 B。
- [ ] 用 T 中对应测试命令确认失败来自缺少适配行为，而非配置错误/连接失败。
- [ ] 在真实绑定文件中实现最小目录适配与附录；不复制目录或选择新的生产地址，不解析未确认参数。
- [ ] 在一次性 PostgreSQL 验证撤销/门禁竞争、A→B、新旧附录版本；旧命令不改址、保留原版本解析；SQLite 快测同时通过。

**验收：** 设计 V3/V4/V10；产物只在隔离装配中生效，正式合同未确认时不导入生产入口。

## Task 2：业务事务登记与稳定映射

**Files:** F3 的原业务 service、业务映射及测试；必要唯一约束/迁移明确后才实施。

**Interfaces:** 调用 `EcsCommands.create(session, owner=..., call_key=..., device_code=..., task_type=..., timeout_ms=..., params=...)`；会话由原业务调用者提交。外层业务入口签名在 Task 0 按既有模块确定，不新增通用命令 HTTP API。

- [ ] 写 `test_business_command_and_outbox_commit_or_rollback_together`、`test_repeated_intent_preserves_command_and_bytes`、`test_new_authorized_intent_is_not_blocked_by_old_failed_command`；断言重复仅一个身份，不同输入冲突，不因旧未知换号重做同一意图。
- [ ] 定向运行确认失败，再复用业务事务保存稳定映射并调用 P1；调用入口本身不提交、不发 HTTP。
- [ ] 注入业务写失败、入队失败和外层回滚；并发同调用键在 PostgreSQL 只产生一个命令/outbox，原业务上下文与 command_code 可追溯。

**验收：** V1/V2/V4；未提交义务不可被发送，原业务对象/轮次不会被当前设备绑定覆盖。

## Task 3：结果读取、业务幂等消费与只读核对

**Files:** F3 的消费 service/持久记录及测试，F5 的既有查询/权限入口与操作说明。

**Interfaces:** 只调用 `get_command(session, owner=..., command_code=...)` 或 `get_by_call(session, owner=..., call_key=...)` 获得 CommandView；消费入口和返回类型由 Task 0 的现有业务接口绑定。消费不在 Result 回调内执行，不引入订阅总线或第二调度器。

- [ ] 写 `test_duplicate_consumption_updates_business_once`、`test_consumer_record_rolls_back_with_business_failure`、`test_ack_or_done_without_result_does_not_advance_business`，先确认失败后补最小实现。
- [ ] 对 SUCCESS/FAILED 分别使用 I6 的业务迁移；保留消费唯一身份并在同一事务更新原业务。按 I6 绑定的业务行锁、版本条件更新或等价机制，将原对象/映射/版本/许可校验与业务更新原子关联，并与取消、换绑、版本变化入口使用统一并发规则；条件失败回滚本次消费完成标记与业务写。只操作消费项目业务模型，不私锁 ECS ORM。复用现有记录，没有等价机制时才新增最小模型/迁移。
- [ ] 写 `test_other_owner_and_stale_business_mapping_do_not_advance`、`test_conflicted_result_requires_reconciliation`、`test_late_conflict_does_not_undo_committed_business`。判断时已有冲突/封锁、对象映射或当前许可不符即不推进；读取后新的 ECS 结果冲突按迟到证据处理，不承诺与一切迟到回调严格串行化；业务取消/换绑/版本变化仍须遵守 I6 的并发保护。
- [ ] 用 PostgreSQL 验证并发消费只有一次业务效果、失败可重试且不生成设备请求；回放原结果不创建新命令。
- [ ] 补 `test_business_change_before_consumption_prevents_stale_update` 与 `test_consumption_before_business_change_preserves_committed_transition`，分别参数化取消、换绑和版本变化。用可控同步点覆盖消费已读到旧许可后发生竞争的窗口，验证两种取得推进权的顺序：变更先提交则消费无业务效果、无消费完成标记，保留原结果/原因；消费先提交则后续变更遵守 I6 迁移、不覆盖既定事实。验证版本条件失败回滚消费标记，重试重新读取当前业务状态、不换号重发；不得仅用串行调用或 sleep 代替竞争验收。
- [ ] 复用查询/权限展示 DTO 与业务映射、HELD 原因及对账标记；文档列核对责任人和证据查看权限，不新增解封/重发写接口，不把普通日志当作原始证据仓。

**验收：** V8/V9；业务更新与消费记录不会分裂；消费唯一键防重与当前业务许可的并发保护分别有证据，取消/换绑/版本变化的两种竞争顺序均通过。需要与 ECS 迟到回调跨组件严格串行化或外部副作用的新要求先回到设计，不用私读 ORM 绕过接口。

## Task 4：隔离装配与 HTTP 全闭环

**Files:** F4 的专用装配/夹具/模拟器与集成测试；F1–F3 已绑定产物。

**Interfaces:** 将 Task 1 的 routes/annexes 注入 EcsCommands，将 EcsLifecycle/EcsCommandAdapter/DynamicClients 注入现有 reliable_lifespan；Result 服务绑定同一隔离会话工厂。复用 Task 2 登记和 Task 3 消费入口。

- [ ] 写 `test_business_to_sender_result_and_consumer_close_loop`；先失败，再仅在测试/专用实验进程显式装配；模拟端按原 wire 返回响应并通过 Result HTTP 回报。
- [ ] 运行回环 HTTP 全链路，覆盖正常 ACK/SUCCESS、FAILED、Result 先于 ACK、响应丢失后重报、早到/矛盾结果、缺原附录503后恢复重试及短期清理后的长期重放。
- [ ] 通过原 Sender 覆盖503/429/NOT_SENT原身份重提、Retry-After下界、UNKNOWN/租约中断重启HELD；断言请求次数、原始字节、证据、投递/命令/业务状态，不能只断言分类器或 HTTP200。
- [ ] 验证配置缺失、不可信目标或非隔离资源时实验不能启动；检查实际连接仅为本次存储/独立队列和回环模拟器。禁止运行 `uvicorn app.main:app` 等默认加载开发配置的命令代替隔离装配。

**验收：** V5/V6/V7/V12；过程没有现场设备请求，模板默认主应用未启用真实发送。

## Task 5：兼容回归、交付证据与门槛复核

**Files:** T 对应测试及本设计/计划、消费项目接入说明；底座测试只复用，若修缺陷另开清晰修复任务。

- [ ] 对消费项目执行 Task 0 固定的快测及一次性 PostgreSQL/Redis 集成命令；配置缺失可以明确跳过专项，配置后连接失败必须失败，不能将跳过标成通过。
- [ ] 对底座执行 `uv run pytest -q -rs`，显式 TEST_DATABASE_URL/TEST_REDIS_URL 指向本次一次性存储；覆盖 `tests/integrations/ecs_command/`、`tests/reliable/`、WMS request/Transport 及日志回归。该命令属未来隔离验证，不读取/迁移本机数据库。
- [ ] 执行目标项目文档化 lint/format 命令；本仓库为 `uv run ruff check .`、`uv run ruff format --check .` 和 `git diff --check`，不使用自动修复脚本扩大差异。
- [ ] 对 V1–V12 逐项链接实际行为测试与执行证据；保存基线、命令、退出码、通过/跳过数，列出未验证供应商项、真实网络/设备/prefork 及外部评审限制。
- [ ] 复核源代码/装配差异及 C1–C7、来源限制和部署门槛；只清理本次标签/目录/队列下资源，不删其他容器、卷、分支或 stash。
- [ ] 待用户另行授权后才进行命名暂存/提交/SHIP；本计划不自动执行提交、推送、迁移或真实设备步骤。

**验收：** V10/V11/V12 与全矩阵证据齐全；结论只能是“该场景隔离接入通过”，真实启用仍为 NOT_READY，直到设计 §7 的适用门槛逐项有证据且取得单独授权。

## 真实接入交接表（当前均未执行）

| 项目 | 接受人及证据要求 | 本阶段结论 |
|---|---|---|
| C1/C2/C3 | 协议负责人+供应商：版本/原文/响应矩阵/重启去重及保留期 | 测试合同不代替供应商证据 |
| C5 | 消费项目负责人：实际目录/权限/生效撤销/锁竞争证据 | 隔离适配通过不代替现场目录验收 |
| 来源限制 | 网关/安全负责人：Result开放前来源限制实际生效 | UUID、mock回调与HTTP200都不能证明认证 |
| 迁移与回退 | 环境负责人：备份、停所有旧Sender/维护、迁移、新版本及保留证据的回退演练 | 只允许一次性环境演练，本机/部署库未获授权 |
| C6与现场 | 容量/现场/业务负责人：资源上限、限定设备动作、实际物理效果及业务核对 | 不以日志恢复、ACK或CI代替验收 |
| C4/C7 | 另行批准结果纠正协议或具体Event接入时处理 | 不提前扩范围或伪标完成 |

## 本次文档自查

- 设计 §2 的输入与任务0一一对应；§3 的责任归属映射 F1–F5；§4/§6 不变量与 V1–V12 分别由任务1–5验证。
- 复用现有端口签名；没有把未知项目路径、业务规则、供应商合同或测试通过数写成确定事实。
- 当前未创建实现文件/测试/模型/迁移，未执行任何代码步骤；文档审阅和输入绑定完成后仍需单独代码授权。
