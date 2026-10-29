# 粗分机 SCAN_COMPLETED 可靠受理实施计划

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

本轮可交付：按已冻结基线实现的业务受理逻辑及隔离测试。正式 catalog、生产模型加载和迁移随完整粗分业务交付，不能为了测试本事件提前启用。当前合同冻结不调整“完整业务才可登记”的仓库约束，也不授权真实设备上报。

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
| `app/businesses/coarse_sorting/ecs_event/scan_completed.py` | 正式接入时的薄入口；其导入受 catalog 所有权约束，本阶段不能绕过注册检查直接启用 |
| `app/businesses/coarse_sorting/definition.py`、`composition.py`、`models.py`、各级 `__init__.py` | 完整业务正式接入阶段才落地；本计划描述目标接缝，不创建空工厂/占位包填充清单 |
| `tests/businesses/test_coarse_sorting_scan.py` | 经真实接收器/HTTP 和具体服务测试可观察受理行为 |
| `tests/businesses/conftest.py`，必要时 `tests/helpers.py` | 任务专用独立 MetaData、受信任测试装配、临时表与精确清理；复用既有隔离装配模式 |
| `docs/integrations/coarse-sorting-api-draft.md` | 精确标注受理验证范围及未启用边界，不能把整篇状态改成已实现 |

不修改公共 controller、receiver、身份摘要、通用诊断或 Sender；不新增管理页面、查询 API、权限码、配置项或依赖。只有测试证明既有机制缺陷时，才另列最小框架修复。

## Task 1：固化合同基线与受理样例

**Files:** 本文、`docs/integrations/coarse-sorting-api-draft.md`。

**Interfaces:** 输入用户冻结决定和 §4.1 基线；输出按基线编写的正常、NG、重报、新事件、无效数据及缺失箱码样例。

- [x] 记录用户授权：按当前文档冻结合同，后续调整再优化；已将字段、角色、身份及已知限制写入 §4.1，不再等待对端确认后才实现。
- [ ] 固定正常、设备检测 NG、原样重报、同码新事件、无效数据、缺失箱码各一份合同样例；有效事件为 200/ACK，无效字段为 400；完全相同事实按重复处理。
- [x] 明确本阶段停止于持久待办、正式 catalog 不启用；对端核对义务不等同本阶段实现阻塞。

**验收：** 按冻结字段表写出测试样例即可进入 Task 2，无需再获取合同确认；真实接收与正式登记仍不在本轮范围。

## Task 2：实现最小受理服务并固定事务行为

**Files:** `contracts.py`、`service.py`、`tests/businesses/test_coarse_sorting_scan.py`、任务所需测试夹具。路径均位于上表范围。

**Interfaces:**

- `ScanCompletedData(CallbackPayload)`：采用 §4.1 严格字段合同及 `extra="ignore"`；箱码省略时保留缺失语义，不补默认箱码；提供时严格为 string。
- `CoarseSortingService(scan_table: Table)`：仅持有自身表定义，不持有活动 session 或作业状态。
- `async accept_scan(self, ctx: EcsEventContext, payload: ScanCompletedData) -> None`：只用 `ctx.session`；业务不匹配 note 后返回；存储异常抛出。
- 测试薄入口使用既有装饰器和受信任测试 composition，将调用转交服务；不放宽生产所有权校验。

- [ ] 先写会失败的公开行为测试：合法事件提交后为 200/ACK，数据库仅有一个扫码记录且 pending；扫码内容、原时间和原绑定一致；ReliableMessage/ECS 命令数量不增加。
- [ ] 加入 NG 事件和字段校验测试：合法 NG、整数/小数检测值及缺失箱码同样只受理；缺必填字段、错误类型（含 bool 冒充数字）、非有限数、未知 result 返回 400 且无业务记录。原条码含逗号等字符保持不变；scan_time 与 timestamp 不同仍合法。
- [ ] 验证额外字段从业务 Payload 忽略，但仍参与公共完整 data 的身份摘要；服务直接使用 ctx.digest，不从已过滤模型重新计算身份。
- [ ] 运行新增测试，确认因受理逻辑缺失而失败；再实现最小 Payload 与服务，使测试通过。
- [ ] 注入扫码写入后的存储故障，经真实 HTTP 接收入口断言返回 503、收据/扫码/待办一起回滚；随后原样重报成功。
- [ ] 验证错工作线业务、配置不完整采用既有诊断并 ACK，不生成扫码待办；未知设备/未匹配角色沿既有未注册路径处理。

**验收：** ACK 对合法已处理事件证明原事务已保存扫码与待办；ACK 本身不证明所有事件都已进入粗分业务。无任何外部动作。

## Task 3：验证重传、并发、重启与长期去重

**Files:** 同一测试文件与任务夹具；仅在失败定位到受理实现时修改 `service.py`。

**Interfaces:** 沿用 Task 2；测试从公共 HTTP/receiver 驱动，断言自身业务行、原收据关联及持久待办，不读取私有服务状态。

- [ ] 原样重传、顶层无关字段变化：同一原记录、同一 execution 引用，pending 不重复；进程/服务重建后结果相同。
- [ ] 删除任务拥有的公共收据模拟清理，再重报原事件：公共接收器可形成新收据，业务仍只保留原记录，不重置原归属或待办。
- [ ] 两线相同条码独立受理；同码有效新经过按 Task 1 规则创建新记录。设备改绑后重报旧事件仍引用旧受理；新事件只按新绑定核对。
- [ ] 用独立 PostgreSQL 连接并发投递相同事件，断言仅一个业务受理；首次事务回滚后等待方可完成受理。数据库唯一键兜底，不用内存锁去重。
- [ ] 并发配置更新验证工作线 → 设备 → 业务行的锁序与重读；已确定诊断不能转成后台待办。
- [ ] 先按未注册/未绑定路径形成 ACK，再安装测试处理器并重报：沿用原收据，不自动补业务行。将此限制写入交付说明。

**验收：** 公共收据清理、重复请求、重启和配置变化均不能重复建单或迁移原业务；真实 PostgreSQL 并发用例通过。

## Task 4：回归与阶段交付

**Files:** 新增受理测试及草案状态说明；不创建正式迁移或修改 catalog。

- [ ] 执行 `rtk uv run --frozen --no-sync ruff check .` 和 `rtk uv run --frozen --no-sync ruff format --check .`。
- [ ] 执行 `rtk uv run --frozen --no-sync pytest -q -rs tests/businesses/test_coarse_sorting_scan.py tests/integrations/ecs_callback tests/controllers/test_ecs_callback.py tests/businesses/test_workline_business_flow.py tests/businesses/test_catalog.py tests/businesses/test_contract_assembly.py`。
- [ ] PostgreSQL 用例必须显式配置任务拥有的可丢弃 `TEST_DATABASE_URL`；其他所选测试如需要 Redis，同样显式配置可丢弃 `TEST_REDIS_URL`。跳过不能记为通过，不使用现有业务数据库做故障注入或清理。
- [ ] 用全新进程分别执行 `rtk uv run --frozen --no-sync python -m scripts.check_integration_contracts --environment production` 和 `--environment local`；确认原声明仍通过，粗分半成品未进入正式目录。
- [ ] 交付文件变更、测试实测结果、冻结基线引用和已知限制；说明待办停止在等待验证阶段。完整业务接入时再补正式定义、装配、模型导出/加载、唯一 Alembic head 迁移及正式注册验收。

**阶段完成标准：** 冻结扫码基线的受理逻辑及隔离验证通过，扫码事实与待办可靠保留；不宣称完整粗分机业务可用或真实 ECS 现场验收通过。

## 后续正式启用时的顺序（本轮不执行）

完整粗分业务获准后，先完成正式模型与迁移、业务清单与全部资源配置，再把处理器部署至全部接收实例并验证角色装配，最后让 ECS 开启上报。既有未注册 ACK 的事件没有自动补业务能力，应单独核对，不删除旧收据强行重放。

## 计划自检

已对照扫码事件、受理事务、持久幂等和正式 catalog 约束检查覆盖。Review Focus 五类风险分别进入 Task 2/3；WMS 验证、ECS 命令、独立 RR 推进和后台扫描不属于本轮，因此不预建相关状态机、接口或调度器。本文仅是实施计划，尚未执行功能测试或修改应用代码。
