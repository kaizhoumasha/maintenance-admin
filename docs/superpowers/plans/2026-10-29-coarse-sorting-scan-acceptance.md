# 粗分机 SCAN_COMPLETED 可靠受理实施计划

> 当前增量已进入[验证与首个 MOVE_FORWARD / Result 阶段](../specs/2026-10-29-coarse-sorting-move-forward-design.md)，操作见 [ECS Event 调试](../../integrations/ecs-event-debugging.md)。下文全部为首阶段历史设计与验收，保留当时范围和结果；现行合同以上述增量为准。

> 当前范围修订：2026-10-29 用户确认将扫码受理登记为同一套正式装配，撤销此前空生产 catalog 及专用 local 隔离要求；移除 ECS 无法提供的箱码清单。下述旧阶段决定与验收结果保留为历史，当前实现与验证以 [调试计划 Task 7](2026-10-29-dev-ecs-events.md#task-7唯一正式粗分扫码装配当前修订) 为准。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 可靠保存粗分机扫码事件、原设备/工作线归属及持久待办；重复投递和重启不重复受理。

**Architecture:** 复用现有 ECS Event 接收器、BindingResolver、BusinessRuntime 和原接收事务。业务薄入口转交具体受理服务，以公共事件摘要建立业务持久唯一键；扫码记录本身承载待办，不新建队列、Sender 或后台调度。

**Tech Stack:** 项目现有 Python 3.13+、FastAPI、Pydantic、SQLAlchemy、PostgreSQL、pytest/HTTPX；不新增依赖。

**Spec:** [粗分机草案 §4.1 扫码受理基线 v1、§6–7](../../integrations/coarse-sorting-api-draft.md)、[业务包约束](../../integrations/business-packages.md)、[原子受理配方](../../integrations/business-transaction-recipes.md)。2026-10-29 用户决定按当前文档冻结本阶段合同，后续调整再优化；这是内部实现依据，不表示对端或现场已验收。

## Global Constraints

- 用户已明确本轮仅做扫码事件可靠受理；不调用 WMS，不创建任何 ECS 命令，不实现验证、分配、搬运或完成报告。
- 入口保持 `POST /api/v1/callback/event`，事件名 `SCAN_COMPLETED`；提交成功后沿用 `{"code":200,"message":"ACK"}`。
- 业务只写 `ctx.session`，不自行 commit/rollback、改隔离级别、访问外部网络或另开写事务。
- 当前正式 `catalog.entries()` 为空；只有完整真实业务获准后才能加入。本阶段不登记半成品业务，不开放管理员选择粗分机，不启用真实设备上报。
- 本阶段按 §4.1 冻结基线开展受理实现和隔离验证，不再等待对端确认；正式注册仍遵守完整业务交付约束。
- 使用 `feature/*` 分支；未经用户指示不提交、推送、部署或请求真实 WMS/ECS。
- 测试业务表使用独立 MetaData，显式创建并 finally 清理，不进入生产 Base 或迁移。

## 方案与交付边界

推荐沿既有接收器增加粗分业务受理逻辑。只保存公共收据无法提供独立的业务去重和待办；另造接收接口会复制身份、校验和事务机制，两者均不采用。

本轮可交付：按已冻结基线实现的真实事件薄入口、业务受理逻辑及隔离测试。正式 catalog、生产模型加载和迁移随完整粗分业务交付，不能为了测试本事件提前启用。当前合同冻结不调整“完整业务才可登记”的仓库约束，也不授权真实设备上报。

### 已冻结的本阶段合同

| 项目 | 冻结决定 | 后续调整边界 |
| --- | --- | --- |
| P07：重报身份 | 沿用完整核心字段规范化摘要；同条码不同事件事实独立受理，完全相同事实视为重复 | 不补造身份，不重算历史收据或业务键；身份变化需显式变更合同 |
| P08：扫码字段 | 以 §4.1 类型表为唯一字段依据：原条码、原扫码时间、有限数值检测值及 OK/NG 必填；箱码可省略，提供时保存原值 | 不增加未规定的业务限额、单位换算或精度要求；字段变化需更新模型及合同测试 |
| P11：绑定与来源 | 业务 code 为 `coarse_sorting`，入料角色为 `INPUT_ARM`；仅使用可信绑定，A/B 不硬编码 | 正式端点和来源配置随完整业务启用落实，不阻塞隔离实现 |
| 受理许可 | 复用工作线业务/配置校验，online/IDLE 不构成许可；正式完整声明随完整业务落地 | 隔离夹具声明其测试范围，不将测试声明用于正式 catalog |
| 业务记录保留 | 扫码记录及摘要不跟随公共收据 90 天清理；本轮不增加业务清理作业 | 后续清理必须保留重报所需去重凭证，不级联丢失待办 |

同毫秒同设备同内容的两次真实扫码，在当前协议下无法区分，本基线按重复处理。这是已知能力边界，不再作为本阶段实现的阻塞项；必要的协议扩展后续另行评估。

## 受理行为

1. 既有接收器先校验信封、查公共收据、解析并锁定工作线与设备，再按角色选择 Payload；不改公共身份算法或查重顺序。
2. 薄入口调用 `CoarseSortingService.accept_scan(ctx, payload)`。服务先按 `ctx.digest` 查业务记录，已有记录即返回，不修改原归属、原数据或待办。
3. 新事件核对 `ctx.binding`、工作线业务及配置许可。确定的业务不匹配通过 `ctx.note(reason_code)` 记录并 ACK，不创建粗分业务行；不改成 503 重试毒消息。
4. 合法新事件在当前事务插入一条扫码受理记录，`pending=True`、`wait_reason="AWAITING_VALIDATION_STAGE"`。这是下一阶段接入前的明确停点；本阶段没有消费者或自动外发。
5. 公共收据、扫码记录及待办一起提交后 ACK。临时存储异常向外抛出，由既有接收器/控制器整体回滚并返回 503，原消息可重报。

### 最小持久数据

一张 `coarse_sorting_scan` 表即可，不拆执行表和待办表：

- `id`：首次受理生成 UUIDv7，作为本次受理/后续业务经过引用；以后重复使用。
- `event_digest`：64 位公共事件摘要，数据库唯一约束，是跨公共收据清理的业务幂等键。
- `receipt_id`：首次原收据逻辑引用；公共收据删除不得级联删除扫码记录或去重依据。
- 原设备/工作线 ID、设备 code、工作线 code、角色、设备/工作线版本和 `binding.revision`：从受信任绑定与已锁定工作线保存快照，不从请求指定工作线。
- 原事件 `timestamp`、冻结基线内的扫码 Payload 字段、接收时间：原条码不解析、不修剪、不改写；`scan_time` 不替换为接收时间；不换算检测数值单位，不给缺失箱码补造值。
- `pending`、`wait_reason`、创建时间：记录本阶段停点。`result=OK` 或 `NG` 均仅是设备检测事实，不产生 WMS 业务结论。

业务服务不导入生产 ORM 类，以注入的 SQLAlchemy Table 访问自身表，沿用仓库现有隔离业务测试的方式。未来正式 composition 注入真实表；本轮测试注入独立 MetaData 表，避免让隔离表进入生产模型体系。

## Review Focus

1. 公共收据清理后的原事件重报：业务唯一键仍保留原扫码和待办，不新建执行。
2. 两条工作线同码扫码、设备改绑后的旧事件：按原事件身份隔离，旧受理不随设备迁线。
3. 并发重复与首次事务失败：只能提交一个业务记录；首次回滚后重报可正常接管。
4. 未绑定、错业务、配置缺失和已被未注册路径 ACK 的历史事件：诊断不能冒充业务受理，不自动回放。
5. `result=NG`、非有限数、字段类型错误和缺失箱码：按冻结字段表校验，不擅自进行业务放行或物理动作。

## 文件范围

| 文件 | 责任与时机 |
| --- | --- |
| `app/businesses/coarse_sorting/contracts.py` | 按 §4.1 冻结基线定义 `ScanCompletedData(CallbackPayload)`；不新增 WMS/命令模型 |
| `app/businesses/coarse_sorting/service.py` | 原事务受理、持久去重、归属核对和扫码待办 |
| `app/businesses/coarse_sorting/ecs_event/scan_completed.py` 及业务包、入口包的 `__init__.py` | 本阶段实现真实薄入口；包初始化无注册副作用。隔离测试通过受信任测试 composition 加载该入口并校验 catalog 归属，不启用生产 catalog |
| `app/businesses/coarse_sorting/definition.py`、`composition.py`、`models.py` | 完整业务正式接入阶段才落地；本计划描述目标接缝，不创建空工厂/占位包填充清单 |
| `tests/businesses/test_coarse_sorting_scan.py` | 经真实接收器/HTTP 和具体服务测试可观察受理行为 |
| `tests/businesses/conftest.py`，必要时 `tests/helpers.py` | 任务专用独立 MetaData、受信任测试装配、临时表与精确清理；复用既有隔离装配模式 |
| `docs/integrations/coarse-sorting-api-draft.md` | 精确标注受理验证范围及未启用边界，不能把整篇状态改成已实现 |

不修改公共 controller、receiver、身份摘要、通用诊断或 Sender；不新增管理页面、查询 API、权限码、配置项或依赖。只有测试证明既有机制缺陷时，才另列最小框架修复。

## Task 1：固化合同基线与受理样例

**Files:** 本文、`docs/integrations/coarse-sorting-api-draft.md`。

**Interfaces:** 输入用户冻结决定和 §4.1 基线；输出按基线编写的正常、NG、重报、新事件、无效数据及缺失箱码样例。

- [x] 记录用户授权：按当前文档冻结合同，后续调整再优化；已将字段、角色、身份及已知限制写入 §4.1，不再等待对端确认后才实现。
- [x] 固定正常、设备检测 NG、原样重报、同码新事件、无效数据、缺失箱码各一份合同样例；有效事件为 200/ACK，无效字段为 400；完全相同事实按重复处理。
- [x] 明确本阶段停止于持久待办、正式 catalog 不启用；对端核对义务不等同本阶段实现阻塞。

**验收：** 按冻结字段表写出测试样例即可进入 Task 2，无需再获取合同确认；真实设备上报与生产正式登记仍不在本轮范围。

## Task 2：实现最小受理服务并固定事务行为

**Files:** `contracts.py`、`service.py`、`tests/businesses/test_coarse_sorting_scan.py`、任务所需测试夹具。路径均位于上表范围。

**Interfaces:**

- `ScanCompletedData(CallbackPayload)`：采用 §4.1 严格字段合同及 `extra="ignore"`；箱码省略时保留缺失语义，不补默认箱码；提供时严格为 string。
- `CoarseSortingService(scan_table: Table)`：仅持有自身表定义，不持有活动 session 或作业状态。
- `async accept_scan(self, ctx: EcsEventContext, payload: ScanCompletedData) -> None`：只用 `ctx.session`；业务不匹配 note 后返回；存储异常抛出。
- 真实薄入口使用既有 `@ecs_event("SCAN_COMPLETED", device_role="INPUT_ARM", data=ScanCompletedData)`，转交具体服务。测试 composition 显式声明真实入口模块，经正式发现、合同校验与运行时装配后通过 HTTP 驱动；不编写替代入口或放宽所有权校验。

- [x] 先写会失败的公开行为测试：合法事件提交后为 200/ACK，数据库仅有一个扫码记录且 pending；扫码内容、原时间和原绑定一致；ReliableMessage/ECS 命令数量不增加。
- [x] 加入 NG 事件和字段校验测试：合法 NG、整数/小数检测值及缺失箱码同样只受理；缺必填字段、错误类型（含 bool 冒充数字）、非有限数、未知 result 返回 400 且无业务记录。原条码含逗号等字符保持不变；scan_time 与 timestamp 不同仍合法。
- [x] 验证额外字段从业务 Payload 忽略，但仍参与公共完整 data 的身份摘要；服务直接使用 ctx.digest，不从已过滤模型重新计算身份。
- [x] 运行新增测试，确认因受理逻辑缺失而失败；再实现最小 Payload 与服务，使测试通过。
- [x] 注入扫码写入后的存储故障，经真实 HTTP 接收入口断言返回 503、收据/扫码/待办一起回滚；随后原样重报成功。
- [x] 验证错工作线业务、配置不完整采用既有诊断并 ACK，不生成扫码待办；未知设备/未匹配角色沿既有未注册路径处理。

**验收：** ACK 对合法已处理事件证明原事务已保存扫码与待办；ACK 本身不证明所有事件都已进入粗分业务。无任何外部动作。

## Task 3：验证重传、并发、重启与长期去重

**Files:** 同一测试文件与任务夹具；仅在失败定位到受理实现时修改 `service.py`。

**Interfaces:** 沿用 Task 2；测试从公共 HTTP/receiver 驱动，断言自身业务行、原收据关联及持久待办，不读取私有服务状态。

- [x] 原样重传、顶层无关字段变化：同一原记录、同一 execution 引用，pending 不重复；进程/服务重建后结果相同。
- [x] 删除任务拥有的公共收据模拟清理，再重报原事件：公共接收器可形成新收据，业务仍只保留原记录，不重置原归属或待办。
- [x] 两线相同条码独立受理；同码有效新经过按 Task 1 规则创建新记录。设备改绑后重报旧事件仍引用旧受理；新事件只按新绑定核对。
- [x] 用独立 PostgreSQL 连接并发投递相同事件，断言仅一个业务受理；首次事务回滚后等待方可完成受理。数据库唯一键兜底，不用内存锁去重。
- [x] 并发配置更新验证工作线 → 设备 → 业务行的锁序与重读；已确定诊断不能转成后台待办。
- [x] 先按未注册/未绑定路径形成 ACK，再加载真实处理器并重报：沿用原收据，不自动补业务行。将此限制写入交付说明。

**验收：** 公共收据清理、重复请求、重启和配置变化均不能重复建单或迁移原业务；真实 PostgreSQL 并发用例通过。

## Task 4：回归与阶段交付

**Files:** 新增受理测试及草案状态说明；不创建正式迁移或修改 catalog。

- [x] 执行 `rtk uv run --frozen --no-sync ruff check .` 和 `rtk uv run --frozen --no-sync ruff format --check .`。
- [x] 执行 `rtk uv run --frozen --no-sync pytest -q -rs tests/businesses/test_coarse_sorting_scan.py tests/integrations/ecs_callback tests/controllers/test_ecs_callback.py tests/businesses/test_workline_business_flow.py tests/businesses/test_catalog.py tests/businesses/test_contract_assembly.py`。
- [x] PostgreSQL 用例必须显式配置任务拥有的可丢弃 `TEST_DATABASE_URL`；其他所选测试如需要 Redis，同样显式配置可丢弃 `TEST_REDIS_URL`。跳过不能记为通过，不使用现有业务数据库做故障注入或清理。
- [x] 用全新进程分别执行 `rtk uv run --frozen --no-sync python -m scripts.check_integration_contracts --environment production` 和 `--environment local`；确认原声明仍通过，粗分半成品未进入正式目录。
- [x] 交付文件变更、测试实测结果、冻结基线引用和已知限制；说明待办停止在等待验证阶段。完整业务接入时再补正式定义、装配、模型导出/加载、唯一 Alembic head 迁移及正式注册验收。

**阶段完成标准：** 冻结扫码基线的受理逻辑及隔离验证通过，扫码事实与待办可靠保留；不宣称完整粗分机业务可用或真实 ECS 现场验收通过。

## Task 5：补齐真实入口并迁移受理验收

**Files:** `app/businesses/coarse_sorting/ecs_event/scan_completed.py`、必要的业务/入口包初始化、现有受理测试与夹具、本文及粗分草案。

**Interfaces:** 沿用冻结 Payload、公开事件装饰器、正式发现与 catalog 合同校验；真实入口转交具体服务，业务状态仍由原接收事务保存。

**范围更正：** 用户指出 `ecs_event` 缺失。真实入口属于本阶段交付，不能由测试替代入口证明完成；生产 catalog、模型/迁移及外部业务推进仍随完整业务启用，不改变 MOCK 和内部验证口径。

- [x] 补齐 `ecs_event/scan_completed.py`，以 `INPUT_ARM` 角色和冻结 Payload 注册，只转交具体服务；包初始化不自动注册。
- [x] 测试 composition 声明真实入口，经正式发现、合同校验和运行时装配驱动原 HTTP 受理用例，移除测试自写入口；保留原事务故障、去重、并发与配置变更覆盖。
- [x] 新增全新进程负向回归：catalog 校验完成后，首次导入未列业务的真实入口仍被归属校验拒绝，不能静默注册。
- [x] 重跑相关与全量回归、Ruff、production/local 合同检查，并以新范围复评；保留原暂存状态，不提交推送或部署。

## 后续正式启用时的顺序（本轮不执行）

完整粗分业务获准后，先完成正式模型与迁移、业务清单与全部资源配置，再把处理器部署至全部接收实例并验证角色装配，最后让 ECS 开启上报。既有未注册 ACK 的事件没有自动补业务能力，应单独核对，不删除旧收据强行重放。

## 计划自检

已对照扫码事件、受理事务、持久幂等和正式 catalog 约束检查覆盖。Review Focus 五类风险分别进入 Task 2/3；WMS 验证、ECS 命令、独立 RR 推进和后台扫描不属于本轮，因此不预建相关状态机、接口或调度器。本轮已实现严格扫码 Payload 和表注入的具体受理服务，并经原 HTTP 接收器验证；真实入口已实现但未在生产 catalog 启用；生产模型/迁移仍待完整业务交付。实测记录见下节。


## 2026-10-29 本轮实施与内部验证记录

- 已实现 `app/businesses/coarse_sorting/contracts.py` 的 `ScanCompletedData` 与 `service.py` 的 `CoarseSortingService.accept_scan`；只保存事实和 `AWAITING_VALIDATION_STAGE` 待办，无 WMS/ECS 出站动作。
- 真实 `ecs_event/scan_completed.py` 使用原装饰器，入口包与业务包初始化不自动导入或注册处理器。隔离测试以测试 composition 声明该真实模块，经发现、catalog 所有权/合同校验与运行时工厂装配，注入可信绑定和独立 MetaData 表；不再使用测试替代入口。未创建生产 definition/composition/models 或迁移，未将半成品加入正式 catalog。
- 表中的 `received_at` 同时记录该受理行的创建接收时间，不增加重复的创建时间列；原事件 timestamp 与 scan_time 分别保存。
- TDD 红灯：实现前，两种数据库下 46 项失败、4 项通过，证明缺少业务持久化、严格字段校验和事务行为；初始仅保存公共未注册收据，不能视为业务受理。
- 受理服务初次验证（当时使用测试替代入口）：65 通过、3 跳过。3 项跳过均为 SQLite 不具备行锁语义的并发变体；对应 PostgreSQL 并发重报、首次回滚接管、配置更新等待均已执行通过。
- 所有 PostgreSQL/Redis 验证使用本轮创建的可丢弃存储，显式注入 TEST_DATABASE_URL / TEST_REDIS_URL，仅发布回环端口；未使用现有业务库或 Redis。
- 基线全量回归：2509 通过、841 跳过，跳过来自当时未配置独立存储；这不作为 PostgreSQL 验证通过的证据。
- 受理服务初次相关回归：244 通过、6 个 SQLite 并发变体跳过；初次全量回归：3385 通过、33 跳过（221.22 秒）。全量跳过为 SQLite 不适用的并发变体及未配置专用迁移库的既有用例，未记作通过。
- 全仓 Ruff 检查与格式检查通过；production/local 合同检查均通过，businesses 均为空。初次独立评审在正式入口延期的原范围内未发现 Critical / Important / Minor 问题，不覆盖真实入口补齐后的范围；未运行真实 ECS/WMS、设备物理动作或现场验收。

### 真实入口补齐后的内部验证

- 原问题是测试自写了替代入口，未覆盖真实 `ecs_event/scan_completed.py` 的交付。现已补齐真实入口；原 HTTP 用例统一通过真实模块的正式发现、catalog 合同校验和运行时工厂装配执行，服务事务观察仅用于检查事务边界和控制并发，不替代受理实现。
- 新增首次导入负向回归先失败于真实入口模块缺失，补齐后通过：显式设置空测试 catalog 并完成校验后，首次导入真实入口仍须被归属校验拒绝；注册表保持原值。该用例不要求生产 catalog 永久为空。
- 相关回归：245 通过、6 个 SQLite 行锁变体跳过；全量回归：3386 通过、33 跳过（218.03 秒）。对应 PostgreSQL 并发用例均执行；未配置专用迁移库的两个既有用例仍跳过。负向回归在明确测试 catalog 设置后另行重跑通过。
- Ruff 检查与格式检查通过（446 个文件）；全新进程 production/local 合同检查通过，正式 businesses 仍为空。文档机械检查：35 个 JSON 样例、12 个本地文件链接通过；这不代表目录内锚点、对端合同或现场行为已验收。
- 补齐后的九个文件经独立复评，未发现 Critical / Important / Minor 问题；评审核对源码和本轮测试日志，未独立重跑测试。结论仅覆盖真实入口及内部受理阶段。
- 本次仅补齐入口实现与验收链路；生产 definition/composition、模型/迁移、catalog 启用及 WMS/ECS 外发仍未实施。此前阶段原暂存内容保持不变，未提交、推送或部署；本批提交范围见下节。

### 此前阶段实施取舍（历史记录）

- 沿用户既有工作流继续在当前功能分支的主目录实施；代价是主目录保留未提交实现，不另建工作树。
- 此前阶段仅提交获准的五份文档；当时新增实现、测试与阶段实测更新保持未提交。
- 本轮临时 PostgreSQL/Redis 使用任务自有容器和桥接网络，仅发布回环端口；原 internal-only 网络无法分配发布端口，调整后完成验证并清理，现有业务存储不参与测试。
- 工作线编码通过既有 read_workline 公开查询取得，避免业务服务直接导入生产 ORM；代价是受理时附带该查询的设备计数读取。
- 独立评审确认：正式装配和启用、WMS/ECS 外发及物理推进、其他接口提案不纳入本阶段；完全相同事实的实物重入仍按重复处理。对应成本是本阶段待办不会自动推进，且不能辨识这类相同实物重入；后续须按完整业务及合同变更分别交付。

### 当前提交范围与后续工作

2026-10-29 用户授权先评审、优化并提交当前待提交内容。本批包含粗分扫码 Payload、真实 ECS Event 入口、受理服务、隔离测试及对应草案和计划；评审发现的历史提交状态已明确标注。后续通用 `dev/ecs/events` 和粗分本地手工验收装配属于下一阶段，按新设计和实施计划继续，保持未提交、不推送、不部署。当前提交仍不代表生产 catalog 启用、真实 WMS/ECS 对接或现场验收。


### 主应用 local 调试入口增量（历史，已撤销）

此前扫码受理与正式薄入口的验收记录保留。现有主应用在 `ENVIRONMENT=local` 时直接提供 `INPUT_ARM / SCAN_COMPLETED` 下拉，local 薄适配器复用原具体服务；它使用私有注册集合、SQLite 和两线绑定，不修改生产 catalog 或公共回调处理器集合。原独立验证进程及令牌用法已迁移，当前操作见 [ECS Event 调试](../../integrations/ecs-event-debugging.md)，实施与实测结果见 [调试入口计划 Task 4–6](2026-10-29-dev-ecs-events.md#主应用-local-集成增量)。仍只覆盖 MOCK/内部扫码受理，待办不进入验证或设备执行阶段。
