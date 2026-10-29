# ECS Event 调试与粗分扫码正式装配实施计划

> 当前业务范围见[验证与首个 MOVE_FORWARD / Result 增量](../specs/2026-10-29-coarse-sorting-move-forward-design.md)和 [ECS Event 操作指南](../../integrations/ecs-event-debugging.md)。下文全部为当时阶段设计及验收记录，其中“当前”指该历史时点；现行合同以上述增量为准。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**当前授权：** 2026-10-29 用户确认撤销空生产 catalog 约束，使用同一套正式扫码装配；MOCK 由测试/部署的数据库、设备绑定和对端配置决定。当前实施为 Task 7，Task 1–6 均为已被替代方案的历史记录。后续用户已授权修正文档并复评、验证通过后本地提交；不推送、不部署。

**Goal:** 通过既有 Swagger/公共回调受理同一正式粗分事件，在配置数据库内保存与查询事实、去重及待办。

**Architecture:** catalog 显式登记粗分 definition/composition、唯一正式 ECS Event 入口与业务模型；API/worker 共用装配。业务表沿现有 Base/Alembic 管理；调试投递复用原接收事务，摘要查询复用只读投影注入。不增加私有 local 业务处理器、注册集合、上下文、设备前缀或 SQLite 资源。

**Tech Stack:** 既有 FastAPI/Pydantic/SQLAlchemy/Alembic/pytest/HTTPX；不增加依赖。

**Spec:** [当前设计与历史决定](../specs/2026-10-29-dev-ecs-events-design.md)

## 当前约束与评审重点

- 继续在当前功能分支实施，保留已有修改、暂存内容和本地数据；复评、验证通过后仅提交已评审改动，不推送或部署。
- 正式扫码处理器不按环境分叉；控制台沿用项目既有展示策略。启动不建表、不造测试设备，部署执行迁移并配置资源，测试夹具只操作可丢弃存储。
- 删除无业务意义的箱码清单；保留原始事件身份、收据优先查重、事务回滚和未知事件语义。
- 正式业务模型与迁移一致，独立去重及待办不随收据清理消失；查询只读且不重新触发事件。
- metadata-only 发现、catalog 来源归属和运行时完整性保护继续有效；测试夹具显式隔离自己的 catalog/注册项。
- 不新增业务执行框架、后台推进或 WMS/ECS 出站；只验收 MOCK/内部扫码受理，不宣称后续业务或物理完成。

## 当前文件范围

- `app/businesses/coarse_sorting/`：合同、definition/composition/models、唯一事件入口、受理服务与只读投影。
- `app/businesses/catalog.py` 和扫码表迁移：正式登记、模型加载与单一迁移链。
- 既有通用事件调试 controller/schema/service、共享 HTTP 接收函数与 main 注入；不引入额外资源。
- 受理、主应用、架构、运行时、注册目录/检查脚本回归及必要夹具迁移。
- 设计、计划、指南、草案、README/AGENTS 的当前状态同步；历史验收保留并标明适用范围。

## 此前 Task 1–6（历史，方案已替代）

## Task 1：通用事件下拉、真实投递与摘要查询

**Interfaces:** `receive_event_request(request, background_tasks) -> EcsResponse`；`ecs_event_router(*, read_business=None)`；`event_state(factory, digest, read_business=None) -> DevEcsEventState`。业务投影回调接收 `(session, digest)`，返回可 JSON 化 dict 或 None，禁止写入/管理事务。

- [x] 先写 HTTP 回归：登录、实际注册事件的 Examples、同名不同角色及空注册表；POST 与公共接口摘要一致、原数据不丢失、重复和诊断一致；结果 status_code 对无效字段为 400、大小超限为 413、故障回滚为 503（调试尝试本身 HTTP 200）；GET 非法摘要 400、未知 404、已有收据和可选业务投影一致且只读。
- [x] 运行 `rtk uv run --frozen --no-sync pytest -q tests/controllers/test_dev_ecs_event.py`，确认因缺少调试路由失败。
- [x] 实现共享处理、内部诊断 DTO、调试接口与独立查询事务；不增加接收逻辑或新的事件注册来源。
- [x] 执行新增文件及原 ECS 回调/业务回归，通过后更新记录。

## Task 2：粗分独立本地装配与重启验收（历史，已由 Task 4–6 迁移）

**Interfaces:** 共享 `coarse_sorting_scan_table(name)` 和 `coarse_sorting_composition(table)` 测试辅助；独立 `tests.manual.coarse_sorting:create_app` 工厂，配置仅 `DEV_ECS_DATA_DIR` 和 `DEV_ECS_TOKEN`。

- [x] 先写独立进程回归：非 local/缺少配置/非空未标记目录拒绝启动；登录要求；Swagger 真实 SCAN_COMPLETED 下拉；投递后查询 pending/原数据、重复不新增、改 timestamp 新受理、重启后记录不变、收据清理后业务仍可查；不挂载或构建出站运行时。
- [x] 运行新测试，确认缺少工厂导致公开启动/HTTP 断言失败。
- [x] 提取重复测试装配，增加标记目录与独立 SQLite、验证令牌鉴权、两线绑定、真实 catalog 校验/服务和查询投影；为 Payload 增加样例。
- [x] 运行独立进程测试及原 SQLite/PostgreSQL 受理测试；观察结果后更新草案、使用文档及计划。

## Task 3：整体回归与独立复评

- [x] Ruff 检查与格式检查，全新进程 production/local 合同检查，确认生产 businesses 仍为空。
- [x] 执行全量 pytest；PostgreSQL 与 Redis 使用任务自有可丢弃存储，明确跳过项，独立验证应用用任务自有目录。
- [x] 独立复评本轮全部未提交实现；修复重要问题并用负向回归验证。
- [x] 记录 Swagger 使用步骤、实测结果与未启用边界，清理任务自有运行资源，确认 HEAD 仍为第一阶段提交、第二阶段无新增暂存/提交/推送/部署。

## 验收记录

相关回归已实测 263 通过、7 跳过（SQLite 不适用的快照/行锁变体；对应 PostgreSQL 用例执行）。独立应用通过真实 HTTP 验证样例、授权、受理、两线隔离、重报、重启、收据清理后业务查询及零出站记录。全仓 Ruff 与格式检查通过（456 个文件）；全量与独立复评待最后记录。

### 此前独立应用验证（历史）

- 首次全量发现 `test_openapi_error_responses_follow_route_rules` 不通过：dev 投递自定义 4xx DTO 与统一 ErrorResponse 规范冲突。已按既有调试接口方式更正为 HTTP 200 的观察结果，接收器原状态保留在 status_code；原规则测试未修改。六项新协议结果断言先失败后通过，修复专项 172 通过、4 跳过。
- 最终全量：3417 通过、34 跳过，221.38 秒。32 个跳过为 SQLite 不适用的并发/快照变体，对应 PostgreSQL 用例已运行；另两项为未配置专用迁移库的既有用例。
- Ruff 检查与格式检查通过（456 个文件）；全量运行中仅换行修正一处响应说明，模块 AST 保持一致。production/local 新进程合同检查通过，正式 businesses 均为空。文档检查 37 个 JSON 样例、22 个本地文件链接通过。
- 实际 Swagger 已完成授权、查看 INPUT_ARM / SCAN_COMPLETED 下拉、投递和摘要查询；应用重启后原业务 ID 与 pending 状态不变。截图和完整页面记录保留在本计划的忽略工作区。
- 第二阶段实现尚未提交或暂存，HEAD 仍为 a101ea7；最终独立复评与资源清理均已完成，详见下文。

### 最终复评与收尾

- 独立评审逐一核对 19 个未提交文件及关联框架，文件哈希与评审清单一致；未发现 Critical / Important / Minor 问题。评审未自行重跑测试，核对的是本轮实测日志。
- 缺少 Payload 样例的注册事件另经独立 Swagger Schema 断言验证：保留角色/事件下拉与需填写 data 的提示，不补造业务值。
- 临时浏览器页、仅本轮启动的验证进程、两个带任务标记的 PostgreSQL/Redis 容器和自有网络已关闭/清理。专用 SQLite、截图和日志作为内部证据保留，不删除其他本地文件。
- 第一阶段提交仍为 a101ea7；第二阶段无新增提交，暂存区为空，未推送或部署。生产 catalog、main 装配和迁移保持原样。
- 完整业务登记、后续验证/推进和正式模型迁移仍未启用；SQLite 页面验收不代替 PostgreSQL 锁验证，真实 WMS/ECS、设备动作及现场验收未执行。

## 主应用 local 集成增量（历史，已撤销）

此前 Task 1–3 与独立应用的验收记录保留为历史证据。本轮用户明确要求在现有 Swagger 使用，不再保留第二种启动方式；下面任务取代独立应用作为当前交付方式。

## Task 4：隔离声明与粗分资源

**Files:** ECS registry/receiver、coarse_sorting/dev.py 和 dev_ecs_event.py、测试辅助与隔离装配测试。

**Interfaces:** `collect_local_events(modules)` 收集不可变 local 注册映射；`events(additional=...)` 仅对显示合并；`ReceiverContext` 携带独立会话工厂、解析器、运行时和映射；`LocalCoarseSorting.context_for(raw)`/`event_state(digest)`/`lifespan()`。

- [x] 先写公开启动/元数据与隔离回归，确认主应用下拉缺少 SCAN_COMPLETED；覆盖正式注册表/生产 catalog 不变、local_only 来源、禁止未列正式导入、同名冲突及目录保护。
- [x] 以 ContextVar 收集局部声明，不修改全局注册表和 catalog；local 薄入口复用原 Payload/具体服务，表使用独立 MetaData。
- [x] 迁移专用目录、两线种子与资源生命周期，默认目录稳定隔离，可选 DEV_ECS_DATA_DIR；不覆盖已有绑定或引入发送器。

## Task 5：主应用生命周期与开发入口路由

**Files:** main.py、controllers/dev.py、dev_ecs_event.py、ecs_callback.py、dev_registry.py、schemas/dev_ecs_event.py、core/config.py。

- [x] 先验证 local 主应用出现粗分下拉并沿用原登录；生产不加载相关模块或目录。以失败回归锁定当前缺口。
- [x] raw 只读取一次，接收函数接受可信上下文选择器；保留大小限制、收据先查重、异常回滚、日志和快照。正式回调不传隔离选择器。
- [x] local 设备前缀分流到私有 SQLite，其他事件维持原上下文；POST/GET 返回 scope；查询隔离未命中才查询主应用，错误不能被吞掉。
- [x] 以主应用正常启动执行 HTTP 回归，验证主数据库无模拟绑定/扫码收据、隔离库无出站表、重传与重启不重复受理，收据清理后业务可查。

## Task 6：迁移文档与最终验证

- [x] 迁移并移除 tests.manual 独立启动与 DEV_ECS_TOKEN 用法，保留其有效测试；同步设计、指南、业务草案和 AGENTS。
- [x] 执行 Ruff、正式/local 合同检查与全量 pytest，任务自有 PostgreSQL/Redis/SQLite 验证，不使用现有服务数据。
- [x] 实际主应用 Swagger 验证下拉、投递、scope 与摘要查询；最终独立复评，保留证据并清理任务自有进程和存储。
- [x] 核对 HEAD、原暂存差异、stash 与其他本地文件；无提交、推送或部署。


### 主应用 local 集成验收记录（历史，已撤销）

- 开工负向验证已复现：普通主应用只有 EXAMPLE_SCAN，下拉缺少 SCAN_COMPLETED。接入私有声明后通过；生产 catalog、正式注册表、模型导出和迁移未变。
- 专项验证：声明/存储回归 49 通过；调试/正常主应用回归 35 通过、1 个 SQLite 快照变体跳过；最终主应用、目录与资源释放回归 31 通过。
- 最终全量：3424 通过、34 跳过，228.71 秒。32 个跳过为 SQLite 不适用的并发/快照/独立连接变体，对应 PostgreSQL 用例已执行；2 个既有迁移用例因未配置专用迁移数据库跳过。
- 全量曾发现旧目录测试假设只有全局注册、资源释放测试固定两个引擎，已迁移为实际声明及全部资源释放规则；重复运行的主应用测试曾触发回环登录限流，已修正为严格使用配置的可丢弃 TEST_REDIS_URL 并仅清理该测试登录限流 key。最终全量未跳过这些回归，未降低产品限流或关闭隔离装配。
- Ruff 检查和格式检查通过（456 文件）；production/local 全新进程合同检查通过，正式 businesses 均为空；37 个 JSON 样例、27 个本地文件链接检查通过。
- 实际正常 app.main Swagger 完成原用户登录/授权、INPUT_ARM / SCAN_COMPLETED 下拉、投递与摘要查询：scope=local_isolated、接收器 200/ACK、原扫码 pending=true / AWAITING_VALIDATION_STAGE。重启与收据清理持久性由正常主应用进程回归覆盖；截图和脱敏页面记录保留在本计划忽略工作区。
- 全新上下文独立复评核对完整前置提交与 28 个当前文件，哈希一致，无 Critical / Important / Minor；最终记录与资源清理由实施者核验。
- 本轮浏览器、验证主应用进程和自有 PostgreSQL/Redis 已关闭清理，专用 SQLite 与日志/截图作为内部证据保留；不重启或修改现有用户服务。当前功能分支及 HEAD=a101ea7 未变，原暂存内容逐项匹配开工文件，stash 保持原样，本轮新增实现未暂存，无提交、推送或部署。
- 仅完成 MOCK/内部扫码受理；完整业务目录登记、后续验证/推进、生产业务模型迁移及真实 WMS/ECS/设备/现场验收均未启用或宣称完成。


## Task 7：唯一正式粗分扫码装配（当前修订）

- [x] 先写公开合同和正常主应用装配回归，观察失败；扫码样例只提供 ECS 事实。
- [x] 添加正式 definition/composition/models 和单一迁移，将唯一扫码入口登记 catalog；复用原服务。
- [x] 删除私有 local 处理器、注册集合、ReceiverContext、前缀分流、SQLite 生命周期、专用配置和 scope DTO；通用调试使用正式注册及配置库。
- [x] 迁移测试到正式入口/模型及配置数据库，测试数据仅由夹具或部署准备；更新当前指南、草案、AGENTS，保留历史验收记录并标注适用范围。
- [x] MOCK/内部专项及全量回归、Ruff、合同与迁移一致性、独立复评；核验原 index/stash/HEAD，清理任务自有资源，不提交推送部署。


### 唯一正式装配验收记录（当前）

- 已删除箱码清单字段、校验和样例；唯一正式 SCAN_COMPLETED 处理器由所属 definition/composition 登记 catalog，API/worker 共享模型及装配。当前声明只覆盖 INPUT_ARM 扫码受理，COMMANDS 为空。
- 已移除专用 local 处理器、私有注册集合与接收上下文、设备前缀分流、额外 SQLite/标记目录/配置及 scope DTO；部署使用的数据库、绑定和 MOCK 对端由既有配置决定。代码不自动生成测试资产。
- 新增正式 CoarseSortingScan 和迁移 c5ca20261029（父 a6d464b20ab7）。可丢弃 PostgreSQL 上 Alembic check 无模型差异；空表降级/升级通过，含已受理扫码时拒绝破坏性降级且原记录保留。未对现有用户数据库执行迁移。
- 负向起点 3 失败：Payload 仍暴露额外字段、local/production 主应用均无正式粗分目录项。收敛后专项 146 通过、4 个 SQLite 变体跳过；架构/运行时/正式受理回归 28 通过，保留 metadata-only、归属与运行时完整性保护。
- 最终全量 3416 通过、34 跳过，223.76 秒。32 个 SQLite 不适用并发/快照/独立连接变体的 PostgreSQL 对应项已执行；2 个既有迁移用例因未配置独立专用迁移库跳过。旧 local 资源测试及箱码专用用例随撤销方案删除，原业务去重、回滚、配置与并发回归保留。
- 普通 app.main 实进程分别以 local、production 配置验证同一 PostgreSQL 数据库和非 LOCAL 设备码：正式入口受理、调试查询、清理收据后重启保持原扫码 ID、新事件独立受理，无 ReliableMessage/ECS Command。production/local 冷合同检查均报告 coarse_sorting、命令合同为 0。
- Ruff 检查/格式检查通过（458 文件），72 个 JSON 样例和154 个本地文件链接检查通过。独立复评无 Critical/Important；两项 Minor（README 旧状态、计划历史边界）已修复并复核关闭，最终无剩余项。
- 上述实现验收结束时，分支及 HEAD=a101ea7、原 index 内容和 stash 保持原样，当时未暂存/提交/推送/部署。正常主应用验证进程与任务自有 PostgreSQL/Redis 已停止/移除，日志和开工备份保留；其他本地服务、文件与旧验证数据未清理或迁移。
- MOCK/内部验收只证明扫码事实、独立去重与 pending=AWAITING_VALIDATION_STAGE；正式登记不代表后续 WMS 验证、ECS 执行、完整粗分流程或真实设备/现场验收完成。

### 文档复评与提交前核验（2026-10-29）

- 处理器说明改为通过公开业务 API 登记外发意图；旧 local 设计的各章节归入“历史、已撤销”层级，历史正文及原验收数据保留。
- 本轮没有执行逻辑变化；模块去除文档字符串后的 AST 与复评前一致，其余实现和测试文件内容不变。
- 新一轮架构/声明合同回归 67 通过；local/production 冷合同检查均包含 coarse_sorting / INPUT_ARM / SCAN_COMPLETED，命令合同为 0。Ruff 检查与格式检查通过（458 文件），72 个 JSON 样例和 154 个本地文件链接目标检查通过。
- 两处文档问题复评关闭，无新增阻塞项；前述全量及隔离主应用验收记录保留。本节记录提交前事实，按后续授权提交已评审内容，不推送、不部署。
