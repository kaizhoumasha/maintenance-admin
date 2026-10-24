# enterprise_admin ECS 通用能力隔离参考接入与端到端验收实施计划

> **For agentic workers:** 代码实施获批后使用 superpowers:executing-plans，或用户明确选择时使用 superpowers:subagent-driven-development，逐任务执行。Task 0–5 已在本项目测试范围完成，simplify 后专项验证通过；前轮命名暂存/本地提交已完成；本轮 SHIP、推送与创建 develop PR 已获授权，不合并，不执行本机迁移或真实发送。

**Goal:** 在 enterprise_admin 当前仓库内，复用 P1 现有测试，仅补隔离参考接入和端到端验收缺口。

**Architecture:** 测试目录/附录、最小参考记录及消费仅存在于 tests；复用 EcsCommands、唯一 Sender 和 Result 入口。参考登记与命令/outbox 同事务，消费唯一记录与参考状态写同事务；HTTP 位于事务之外，不建立生产业务模块或第二个重试器。

**Tech Stack:** 当前 Python/FastAPI/SQLAlchemy/pytest/HTTPX；SQLite、一次性 PostgreSQL/Redis、回环 HTTP。无新增依赖、应用模型、Alembic 迁移或生产入口需求。

**Spec:** [通用能力隔离参考验收设计](../specs/2026-10-25-ecs-first-consumer-isolation-design.md)。沿用旧文件名保留链接，不再按文件名理解为外部项目接入。

**Status:** REFERENCE_ISOLATION_VERIFIED。Task 0–5 与 simplify 已完成；范围仅本仓库测试参考接入，生产 app/协议/迁移未改，真实接入保持 NOT_READY。本轮提交包含四份文档范围修订与五份测试文件的最终改动，不依赖 wes_backend 运行代码。

**Baseline:** P1 develop @ `ed9005ff0c12e6b090cfcf6cb19ffa3df4a44eea`；文档提交 `796aa6c`。P1 合并 CI 2054 passed、11 skipped，Ruff/格式通过；不能当作本轮新增参考验收结果。

## Global Constraints

- 当前实施和 simplify 已完成，前轮最终改动已提交；本轮 SHIP 允许推送当前 feature 分支并创建 develop PR，不合并、不迁移本机数据库、不启用真实发送；保留其他改动和 stash。
- 后续实现遵守本仓库 feature 分支规则；不直接在 develop 实现。代码开工和真实启用分别授权，不因计划写完自动执行。
- 参考值复用 D1/MOVE、可信 owner=test、(test,1) 附录、qty:int、timeout 1–60000 ms 和五分钟窗口；全部为测试合同，不能导入生产默认装配或关闭 C1/C2/C3。
- 先找等价行为测试，再补缺口。已有覆盖直接复用；不要求所有测试先失败，不为重构/夹具接线重复建测试。
- 只通过公开 EcsCommands 读取结果/登记命令；参考业务表可由测试访问，但不私锁 ECS ORM 推进参考状态。
- 原身份/字节/origin/附录冻结、命令→原授权→消息→收据锁序、UNKNOWN/HELD、WMS STATIC 及日志脱敏/独立降噪/恢复均不变。
- 仅隔离临时存储/队列和回环模拟器；无本机 .env 的源码副本/工作树中显式配置。测试会迁移并清理数据，禁止指向现有开发/生产存储。
- 不加 Status、具体 Event、设备取消协议、自动纠正/超时、强制重发、HELD 解封、生产页面或领域业务模块。底座缺陷先复现并列明最小修复范围，不以测试夹具绕过真实接口。

## 文件绑定与已有覆盖

以下为真实文件；“仅补缺口”不意味着每份文件都要修改。共享夹具放 conftest，不从测试文件互相导入。

| 绑定 | 文件/公开接缝 | 现有证据与允许的增量 |
|---|---|---|
| F1 目录授权/附录 | `tests/integrations/ecs_command/conftest.py`；`app/integrations/ecs_command/routing.py`、`contracts.py` 只复用 | Routes/Payload/ecs 已有；仅在夹具补有效配置选择、原授权锁和参考记录，临时表在专属测试库建立/清理，不新增生产目录或迁移 |
| F2 登记 | `tests/integrations/ecs_command/test_service.py`；复用 `EcsCommands.create(session, owner=..., call_key=..., device_code=..., task_type=..., timeout_ms=..., params=...)` | 重入/外层回滚/owner/timeout 已有；缺口是参考记录与命令/outbox 一起提交回滚 |
| F3 消费 | `tests/integrations/ecs_command/test_query.py`、共享 conftest；复用 `get_command(session, owner=..., command_code=...)` / `get_by_call(session, owner=..., call_key=...)` | 唯一记录并发示例已有；补参考状态写、失败回滚、旧映射及业务变更竞争，不导出 app 模型/消费框架 |
| F4 发送/HTTP/并发 | `tests/integrations/ecs_command/test_flow.py`、`test_concurrency.py`、共享 conftest | 原路由撤销、UNKNOWN、结果竞争和 HTTP mock 已有；补真实回环+参考消费及目录/消费缺口；复用 EcsLifecycle/EcsCommandAdapter/DynamicClients/Sender |
| F5 兼容与说明 | `tests/reliable/`、`tests/integrations/wms_request/`、`tests/integrations/wms_transport/`、`tests/core/test_global_diagnostics.py`、`tests/tasks/test_diagnostics.py`、`tests/http_client/test_logging.py`、`tests/migrations/test_ecs_command_result.py`；必要时 `docs/integrations/ecs-command.md` | 既有回归复用；本设计/计划记录覆盖与证据，不增加生产触发/查询页 |

## Review Focus

1. 测试参考记录与命令/outbox 若两次提交，会掩盖原子性缺陷——Task 2。
2. A 撤销但 B 有效时不得封锁新意图；旧调用仍返回原身份且旧命令不得改投——Task 1。
3. 消费唯一键不能代替参考许可保护；取消/换绑/版本变化与消费竞争必须有两种顺序证据——Task 3。
4. ACK/DONE、早到/迟到结果、缺原附录和重启不能制造参考完成或换号重发——Task 3/4。
5. 根夹具导入 app 会读取设置；存储迁移/清理和回环测试不得触达本机配置或现场——Task 0/4/5。

## Task 0：已有覆盖、缺口与隔离预检

**Files:** 上述测试/夹具、`tests/conftest.py`、`.github/workflows/ci.yml`、本设计/计划；只读盘点，不检查或绑定外部业务项目。

- [x] 将设计 V1–V12 对应到实际测试函数，记录“已覆盖/需扩展/缺失”；现有等价覆盖只复用，未运行证据不得填通过。
- [x] 核实参考合同与当前夹具一致，按设计 I6 固定参考状态及竞争规则，不推断正式设备/业务规则。
- [x] 准备无本机 .env 的隔离源码副本/工作树；显式设置安全测试 DATABASE_URL、REDIS_URL、JWT_SECRET_KEY、RELIABLE_TARGETS={}、TEST_DATABASE_URL、TEST_REDIS_URL，以及需要时的 Celery 专属测试队列。清除继承的现场/业务连接配置，不复制本机 .env。
- [x] 一次性资源使用本次独立身份；实际 DB/Redis/模拟器连接须与本次资源清单一致，网络仅允许这些资源及回环模拟器。缺失、不可信目标或配置冲突时拒绝运行；迁移往返测试独占测试库，不与其他测试并发。
- [x] 记录代码基线、运行配置来源（脱敏）、验证命令、资源所有权和清理清单。取得文档确认及代码授权后才补测试；启动测试前 I7 预检必须通过。

**验收结果：** 已完成覆盖盘点及隔离预检；测试源码副本无 .env，PG/Redis/runner 在本次内部网络、无发布端口，目标白名单正向通过，缺失/冲突配置拒绝；不再存在指定外部项目/真实设备的前置条件。

## Task 1：测试目录与原授权边界（P2 路由条件）

**Files:** F1/F4，优先扩展现有路由测试。**Interfaces:** 测试提供者实现 `resolve(session, device_code, task_type) -> RouteSnapshot`、`authorize_frozen(session, route) -> RouteAuthorization`，不更换生产接口。

- [x] 复用 `test_revoked_old_origin_does_not_block_new_independent_command`、授权锁序及参数附录测试；先确认哪些有效/待生效配置路径缺失。
- [x] 对缺口补行为：待生效 B 不替代有效 A；没有唯一有效且获准配置才拒绝新建；A 已撤销但 B 有效且获准时新独立调用冻结 B，旧调用核对原输入返回原身份/字节/A，撤销先提交的旧门禁不发送、不改投 B；UNKNOWN 授权 fail closed。
- [x] 在共享测试夹具补最小有效配置/授权记录与锁；不创建生产设备目录。撤销/门禁两个顺序已在 PostgreSQL 验证；移除有效配置过滤或参考业务行锁的负向对照均能使对应回归失败，随后恢复。
- [x] 在安全隔离 ECS 专项中验证下列定向入口对应的全部用例（实际命令见执行结果）：`uv run pytest tests/integrations/ecs_command/test_service.py tests/integrations/ecs_command/test_flow.py tests/integrations/ecs_command/test_concurrency.py -q -rs`。

**验收：** V3/V4/V10；已有 P1 不被重写，两个 origin 均为模拟目标，原版本附录保持可解析。

## Task 2：参考调用方事务登记

**Files:** F1/F2。**Interfaces:** 同一 session 内保存临时参考对象/意图/映射，再调用原 `EcsCommands.create`；事务由测试调用方提交，不发 HTTP。

- [x] 复用重入、外层回滚及并发创建测试，仅为参考记录与命令/outbox 的联合原子性补断言/用例。
- [x] 参考记录保留原对象、意图、绑定、版本、command_code；同意图稳定调用键，同输入重入不改原身份，不同输入冲突；新独立有效意图不被旧失败/HELD 封锁，不能换键逃避原未知。
- [x] 注入参考记录写失败、enqueue 失败与外层回滚，断言没有半条可发送记录；失败重试不拆开两次提交。临时表/调用辅助只在共享测试夹具中存在。
- [x] 在 ECS 专项中验证下列定向入口对应的全部用例（实际命令见执行结果）：`uv run pytest tests/integrations/ecs_command/test_service.py tests/integrations/ecs_command/test_concurrency.py -q -rs`，PostgreSQL 参数验证并发唯一身份。

**验收：** V1/V2/V4；不新增正式业务表或应用迁移。

## Task 3：参考结果消费与业务变化竞争（P2 消费条件）

**Files:** F1/F3。**Interfaces:** 仅经公开查询获取 CommandView；对临时参考业务行加锁后复核原对象/绑定/版本、READY 和结果冲突标记，消费唯一记录与状态写同事务。辅助函数仅用于测试，不提供生产消费服务。

- [x] 扩展 `test_example_consumer_advances_once_in_its_own_transaction`，复用其唯一键机制；只有应用结果可推进 READY→SUCCEEDED/FAILED，ACK/DONE 无结果不推进，跨 owner 不可见。
- [x] 补重复/并发一次效果、业务写失败回滚消费记录、旧映射/版本或当前许可不符不推进；重试读取原结果，不创建新命令或设备请求。
- [x] 对取消、换绑、版本变化分别验证两种顺序：变更先持有参考业务锁并提交，消费重新读取后无业务效果/完成标记；消费先持锁提交终态，后续变更不能覆盖。变更入口共用同一业务行锁；若采用等价版本条件更新，零行更新也必须回滚本次消费标记并复核当前状态。
- [x] 使用受控同步点/锁等待观察覆盖并发窗口，不用 sleep 或纯串行调用冒充竞争。不得要求取消在消费已持有业务锁时先提交；乐观机制则覆盖旧读值后的条件更新失败。
- [x] 已知 ECS 结果矛盾不推进；读取后迟到结果冲突只保留证据/核对，不撤销已提交参考状态。参考业务并发保护不扩成与全部 ECS 回调严格串行化，不私锁底座 ORM。
- [x] 在 ECS 专项中验证下列定向入口对应的全部用例（实际命令见执行结果）：`uv run pytest tests/integrations/ecs_command/test_query.py -q -rs`；PostgreSQL 必须验证两种顺序及事务失败，SQLite 通过不替代锁语义。

**验收：** V8/V9；参考状态与消费记录原子、防重和许可保护分别有证据，原结果保留。参考规则不冒充实际库存/任务流程。

## Task 4：真实回环 HTTP 与参考消费闭环

**Files:** F1/F4。**Interfaces:** 复用原 EcsCommands、EcsLifecycle、EcsCommandAdapter、DynamicClients 及唯一 Sender；Result 的 `app.state.ecs_commands`、`callback_session_factory` 显式指向同一隔离数据库，默认主应用不启用。

- [x] 复用 `test_sender_http_and_result_http_complete_before_ack` 等 ASGI/MockTransport 快测，不为已覆盖矩阵另建平行实现。
- [x] 补同一次闭环：参考事务创建 → 原 Sender → 绑定回环随机端口的模拟 ECS → 回环 Result HTTP → owner DTO → 参考消费提交。SUCCESS/FAILED 均验证，不只断言 HTTP200；模拟命令端与结果端均不得暴露到非回环接口。
- [x] 将早到/矛盾结果、缺附录503恢复、响应丢失重报、长期重放、503/429/NOT_SENT 重提和 UNKNOWN/租约中断 HELD 对应到已有回归；只有未覆盖的网络/参考消费交互才增补，断言原身份/字节、次数、证据及三类状态。
- [x] 非隔离存储、非本次模拟目标或缺失配置拒绝启动；不使用 `uvicorn app.main:app` 默认加载开发环境，不同时启动第二 Sender。测试夹具负责有界停机并只清理本次资源。
- [x] 在 ECS 专项中验证下列定向入口对应的全部用例（实际命令见执行结果）：`uv run pytest tests/integrations/ecs_command/test_flow.py tests/integrations/ecs_command/test_query.py -q -rs`，保存网络连接及清理证据。

**验收：** V5/V6/V7/V12；真实回环网络接缝通过，仍不证明现场设备或供应商行为。

## Task 5：兼容回归、证据和真实门槛复核

**Files:** F5 与本设计/计划；仅在必要时补接入指南的测试说明。

- [x] 先定向运行增量相关测试，再分环境运行全套用例，覆盖计划的 `uv run pytest -q -rs` 入口（实际命令及分开运行的2项见执行结果）；TEST_DATABASE_URL/TEST_REDIS_URL 均显式指向本次一次性存储。保留 WMS、日志、可靠层及迁移既有回归；不要把配置缺失跳过的必验项算通过，配置后连接失败必须失败。
- [x] 执行 `uv run ruff check .`、`uv run ruff format --check .`、`git diff --check`，不自动修复无关文件。
- [x] V1–V12 逐项链接实际测试函数及本轮执行证据，区分复用/新增；记录基线、命令、退出码、通过/跳过数和资源清理。P1 历史通过数不替代本轮结果。
- [x] 复核仅测试/说明增量、默认真实发送仍未启用；保存现场供应商/物理验收/prefork 等未验证范围。只清理本次资源，不动其他容器、卷、改动或 stash。
- [x] 前轮最终改动已按授权命名暂存并本地提交；本轮用户已授权 SHIP、推送和创建 develop PR，不合并、不迁移本机或部署库、不启用真实设备发送，保留其他改动和 stash。

**验收：** 仅得出“enterprise_admin ECS 通用能力隔离参考验收通过”。真实启用保持 NOT_READY，直到设计 §7 各适用门槛有证据并获单独授权。

## 真实接入交接表（当前均未执行）

| 项目 | 接受人及证据要求 | 本阶段结论 |
|---|---|---|
| C1/C2/C3 | 协议负责人+供应商：版本/原文/响应矩阵/重启去重及保留期 | 测试合同不代替供应商证据 |
| C5 | 实际接入项目负责人：实际目录/权限/生效撤销/锁竞争证据 | 隔离适配通过不代替现场目录验收 |
| 来源限制 | 网关/安全负责人：Result开放前来源限制实际生效 | UUID、mock回调与HTTP200都不能证明认证 |
| 迁移与回退 | 环境负责人：备份、停所有旧Sender/维护、迁移、新版本及保留证据的回退演练 | 只允许一次性环境演练，本机/部署库未获授权 |
| C6与现场 | 容量/现场/业务负责人：资源上限、限定设备动作、实际物理效果及业务核对 | 不以日志恢复、ACK或CI代替验收 |
| C4/C7 | 另行批准结果纠正协议或具体Event接入时处理 | 不提前扩范围或伪标完成 |

## 本次文档自查

- 已移除指定外部业务项目、真实设备/动作与正式业务规则的隔离开工门禁；I1–I6 已绑定本仓库参考合同，I7 是执行前安全检查，I8 保留真实门槛。
- 两个 P2 分别对应 Task 1/V4 和 Task 3/V9；已有测试优先复用，增量只验证实际缺口，不修改 P1 协议、事务、幂等或未知交付规则。
- 本轮只扩展五份测试文件，没有修改生产 app、模型、配置或迁移；临时参考表不进入应用元数据。范围修订和验证记录随本轮文档提交，不将隔离验收冒充真实接入。

## 执行进度与验证结果（2026-10-25）

| 任务 | 完成结果与源码证据 |
|---|---|
| Task 0 / V1–V12 | 已逐项盘点已有覆盖，仅补实际缺口；专属一次性 PG/Redis、内部网络与回环模拟器，未读取本机 .env 或连接业务存储 |
| Task 1 / V3/V4 | `test_pending_route_keeps_active_route_and_revocation_only_fences_old_call`、`test_no_single_authorized_route_or_action_has_no_side_effects`、`test_persistent_route_revocation_serializes_with_send_gate`、`test_missing_original_authorization_does_not_allow_dispatch` |
| Task 2 / V1/V2 | `test_reference_registration_and_outbox_rollback_together`、`test_concurrent_reference_registration_preserves_original_mapping`，复用已有身份/冻结字节/外层回滚用例 |
| Task 3 / V8/V9 | `test_example_consumer_advances_once_in_its_own_transaction`、`test_consumer_write_failure_rolls_back_marker_and_can_retry`、`test_result_does_not_override_owner_mapping_permission_or_conflict`、`test_late_result_conflict_does_not_undo_consumed_business`、`test_consumption_and_business_change_follow_lock_order` |
| Task 4 / V5–V7/V12 | `test_loopback_command_result_and_reference_consumption` 参数化 SUCCESS/FAILED 与 before_ack/after_ack；原响应矩阵、重启 HELD、缺附录及清理重放继续复用 |
| Task 5 / V10/V11 | 复用协议/客户端、WMS、可靠层、诊断、迁移既有回归；全套用例分隔离运行环境完成，结果见下表 |

独立评审发现参考 `register` 对新意图缺少当前许可检查，已补 READY/D1 守卫；6 个失败用例先复现，修复后通过。新增终态用实际 SUCCESS/FAILED 消费形成；取消/换绑/已消费终态下新键不产生义务，旧键仍返回原身份/映射，有效新版本独立意图允许登记。对应 `test_new_reference_intent_requires_current_permission_but_replay_keeps_identity`、`test_new_authorized_reference_intent_uses_current_version`。此修复只作用于测试参考入口，不改变生产 P1。

| 阶段 | 实际执行及结果 |
|---|---|
| 实施完成后的 ECS 专项 | `python -m pytest tests/integrations/ecs_command/ -q -rs`：196 passed、12 skipped in 9.71s |
| simplify 前完整回归 | `python -m pytest -q -rs --deselect=tests/scripts/test_release_bundle.py::test_site_override_mounts_external_configuration_read_only`：2112 passed、19 skipped、2 deselected in 125.13s |
| 上述单独运行的挂载用例 | 无 .env 的同一临时源码副本，宿主测试 Python 执行 `python -m pytest tests/scripts/test_release_bundle.py -k test_site_override_mounts_external_configuration_read_only -q`：2 passed、33 deselected；使用独立 Compose 项目、不启动业务 API/DB，结束后清理。仅这些用例需要 Docker daemon，未向测试容器暴露宿主 socket |
| 完整回归合计 | 2114 passed、19 skipped；完整回归中分开的2项均已有通过证据，未将 deselected 算作通过 |
| simplify 前/后 | 同一 ECS 专项分别为196 passed、12 skipped in 10.31s / 10.16s；JUnit 208个用例身份、结果及各测试断言数量一致。simplify 后未重跑全仓，不能把前一阶段完整回归冒充 simplify 后结果 |
| 静态检查 | `python -m ruff check .`、`python -m ruff format --check .`通过，331 files already formatted；`git diff --check`通过 |

以上测试使用锁定依赖的隔离 Python，对应计划中的 uv run pytest/ruff 入口。19项全量跳过（专项12项）均为SQLite不适用的PostgreSQL锁/并发参数，对应PostgreSQL参数通过。simplify 仅涉及 conftest、test_query、test_flow：查询分步读取、共用结果报文构造、合并重复导入、删除重复同步信号，净减45行；两个P2、当前许可、事务、幂等和未知交付覆盖完整保留。

临时运行日志及JUnit用于当轮核验，不作为仓库链接依赖；本节保存可复核命令与测试定位，重跑须创建新的专属资源并再次预检。两轮专属容器、匿名卷、内部网络与测试镜像均已清理，文档/暂存区/其他改动及stash在实施、simplify阶段未被覆盖。没有本机数据库迁移、部署、真实发送或供应商/现场验收；真实门槛表仍全部未关闭。

## SHIP 补充验收（2026-10-25）

本节为 simplify 和前轮本地提交之后的新证据，不改写上节历史阶段结果。当前 feature 分支已按本轮授权推送，目标为创建合入 develop 的 PR，尚未合并或部署。

- 外部结构化评审提出 P3 补测缺口，已在既有 `test_pending_route_keeps_active_route_and_revocation_only_fences_old_call` 参数化 `revoked=False/True`：A 失效但未撤销时旧命令仍获准，显式撤销时旧命令被围栏；新独立意图在两种情况下均冻结有效 B，旧身份与字节不变。定向 `python -m pytest tests/integrations/ecs_command/test_service.py -k pending_route -q` 为4 passed；隔离副本中临时把失效等同撤销的负向对照恰好检出 SQLite/PostgreSQL 两项失败，恢复后完整回归通过。未改变生产授权或协议。
- 本次最终测试源码副本与当前测试内容逐字节一致，不复制本机 .env；专属内部 PostgreSQL/Redis、无发布端口，未接入真实设备。完整回归 `python -m pytest -q -rs --deselect=tests/scripts/test_release_bundle.py::test_site_override_mounts_external_configuration_read_only`：2114 passed、19 skipped、2 deselected in129.74s，退出码0。
- 同一无 .env 源码副本另在宿主执行 `python -m pytest tests/scripts/test_release_bundle.py -k test_site_override_mounts_external_configuration_read_only -q`：2 passed、33 deselected in0.84s，退出码0。仅挂载用例使用独立 Compose 项目，不启动业务 API/DB，不向测试容器暴露宿主 socket。两路合计 **2116 passed、19 skipped**；19项均为SQLite不适用的PostgreSQL参数，对应PostgreSQL参数通过，没有将 deselected 视为通过。
- `python -m ruff check .`、`python -m ruff format --check .` 通过，331 files already formatted；推送前测试与 Ruff 证据均为 FRESH。最终原生工程/覆盖/计划/对抗复核无遗留问题；外部结构化评审的 P3 已修复并经原生复核，不能将修复前外部报告当作修复后的独立认证；外部对抗审查540秒超时，保留为缺失覆盖。
- 本轮专属容器、匿名卷、内部网络和测试镜像标签已按资源归属清理；其他容器、其他改动与 stash 保留。Task 0–5 仍为29项全部完成；现有8项 TODO 和真实接入门槛均未冒充完成，不迁移本机数据库、不启用真实发送。
