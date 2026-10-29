# 业务开发单一 API 验收记录

> 下文为空 catalog 阶段的历史验收。2026-10-29 用户另行批准将粗分扫码受理登记正式 catalog，加入业务表/迁移；当前状态见 [业务包指南](business-packages.md)和[扫码调试指南](ecs-event-debugging.md)。本轮仍未部署或连接真实对端。

2026-10-28。本轮把业务调用、命令声明与业务目录收敛为唯一接入路线；当前规则见
[快速开始](business-development-quickstart.md)、[业务包](business-packages.md)、[注册](registration.md)和[ECS 命令](ecs-command.md)。
本轮[设计](../superpowers/specs/2026-10-28-business-development-single-api-design.md)与[实施计划](../superpowers/plans/2026-10-28-business-development-single-api.md)保留接入取舍和任务验收范围。
正式 `catalog.entries()` 仍为空。未新增生产业务、表、迁移、配置或依赖，未部署或连接真实 WMS/ECS/设备。实施验收时未提交，后续提交与推送按末尾交付授权执行。
本文保留各任务的专项记录，并在末尾列出全部改动完成后的全仓复验、综合评审和资源清理结果。验收口径为 MOCK 与内部验证。

## 唯一接入及不兼容边界

| 范围 | 当前形式 | 明确边界 |
| --- | --- | --- |
| WMS 发送 | `WmsRequests.send(session, payload, *, operation_id)` | payload 为精确已注册模型；同类须唯一匹配 operation，拒绝未声明子类/伪造类/歧义；业务不再传 operation/data 字符串字典 |
| ECS 创建 | `EcsCommands.create(session, *, owner, call_key, device_code, timeout_ms, command)` | command 为精确声明模型；参数不再使用 task_type/params；owner 来自可信业务装配 |
| ECS 声明 | `@ecs_command(..., device_roles=(ROLE, ...))` | 非空唯一 tuple；一个模型可共享多个角色，事件 `@ecs_event` 仍用单数 device_role |
| composition | `DEFINITION`、`ENTRY_MODULES`、`MODEL_MODULES`、`COMMANDS=(Model, ...)`、无状态工厂 | catalog 内部编译，不在业务手写绑定/附录或编译清单；每模型须覆盖本业务一个角色，所有角色须存在于 catalog definitions，仅激活本业务角色交集；未列模型不获授权 |
| 正式路由 | `resolve_declared(session, device_code, declaration)` | 依据当前绑定及声明检查原模型/角色/任务/合同；不保留字符串动作路由分支 |
| 业务来源 | `app.businesses.<business>.<module>` + 所属显式 ENTRY_MODULES/catalog | 不扫描 integrations 根、不支持第二业务目录；普通 import 也检查来源；固定平台入口树由共享 manifest 拥有 |
| 动态调试 | local HTTP 的 operation/data、task_type/params | 受信任适配器先校验原模型或 ProtocolObject，再进入同一规范化核心；不是第二业务 SDK 或第二 Sender |

包根不再导出内部 CommandBinding/DeviceAnnex，业务不直接构造编译产物。
`PLATFORM_FORMAL_MODULES` / `PLATFORM_LOCAL_MODULES` 位于 `app.integrations.registration`；
唯一共享入口是 `app.integrations.discovery.load_formal_modules/load_local_modules`，三类旧 discovery 包装模块已删除。
正式平台入口仅 `wms_transport.callback` 的两个回调；local 保留 ECS/WMS 示例、WMS 请求示例和六个 Transport 场景。
模块缺失、内部 import 错误、冲突或 local 补正式缺项都必须阻止启动，全部正式声明和合同先于 local/资源/工厂。

参数、结果、错误和嵌套合同使用受支持的严格类型；原 extra 策略（forbid/ignore/allow）保留，支持严格对象 RootModel。
实际字段在序列化前重验，保留模型别名、默认值、校验/序列化器而不修改实例。
字段级 frozen dataclass、无法解析或无法唯一定位的同运行时类泛型上下文拒绝发送，应使用明确嵌套模型表达合同。
新字节遵循模型 JSON 和 extra 策略；被 ignore 丢弃的字段不隐藏、不恢复，不承诺与任意旧 dict 输入等价。

这是一项业务代码接入不兼容变更。存量冻结记录、身份、字节、路由、期限和 Result 合同保留；
同身份同规范输入重入先返回原记录，再考虑当前配置，不能改原路由或期限。
无法表达原输入时按原引用读取并核对，不换 owner/call_key/operation_id 或目标重做。
可信平台历史附录仅验证原 Result，不授权新命令；UNKNOWN/HELD 不自动重放、改投或解封。

## 持久化与事务保持项

受理仍使用接收器原事务，收据、业务状态和持久待办一起提交。
具体业务推进使用新 session，首条 SQL 前设置 REPEATABLE READ，按 WorkLine → Device → 业务动作顺序加锁。
完整前序和混合结果读取共享快照，进度、消费原身份/版本/成员及下一 Outbox 意图原子提交。
40001 在新 session 中有界重试整个事务，耗尽保留原待办；没有通用 execute/worker 或第二 Sender。
Mock 表使用独立 MetaData、显式创建和 finally 清理，不进入生产 Base/迁移。
ACK、投递 DONE、Status online/IDLE、历史成员里程碑不替代当前许可或业务/物理完成。

## 验证矩阵

| 行为 | 现有可执行证据 | 本轮必须保持 |
| --- | --- | --- |
| 精确 WMS 模型与 JSON | [WMS service](../../tests/integrations/wms_request/test_service.py)、[动态请求](../../tests/integrations/wms_request/test_dev_service.py) | 别名重叠、默认值/null、嵌套/root/dataclass、实际字段重验、序列化器、歧义和无入队失败 |
| ECS 多角色与合同 | [declaration](../../tests/integrations/ecs_command/test_declaration.py)、[catalog](../../tests/businesses/test_catalog.py)、[assembly](../../tests/businesses/test_contract_assembly.py) | 多角色同原模型、extra 策略、严格嵌套合同、角色交集、未列模型不授权、冲突与非法来源在资源前失败 |
| 冻结记录/历史 Result | [service](../../tests/integrations/ecs_command/test_service.py)、[model create](../../tests/integrations/ecs_command/test_model_create.py)、[Result](../../tests/integrations/ecs_command/test_result.py)、[restart](../../tests/integrations/ecs_command/test_restart.py) | 原身份/字节/路由/期限、同键异输入冲突、回滚、并发赢家、历史附录、UNKNOWN/HELD 与迟到结果 |
| 唯一目录/冷进程 | [source guards](../../tests/integrations/test_registration_discovery.py)、[bootstrap](../../tests/integrations/test_registration_bootstrap.py)、[CLI](../../tests/scripts/test_check_integration_contracts.py) | 普通 import 来源保护、固定平台清单、缺项/内部错失败、API/worker/CLI 正式先于 local、六场景保留 |
| 动态调试/唯一 Sender | [dev](../../tests/integrations/ecs_command/test_dev.py)、[dev registry](../../tests/controllers/test_dev_registry.py) | 既有权限、地址限制、v3 原协议、历史 Result 与正式/调试共享核心及 Sender |
| 完整前序与恢复 | [业务消费](../../tests/businesses/test_result_consumption.py)、[前序](../../tests/integrations/wms_transport/test_predecessors.py) | 101 成员/100 分批同快照、更新 UNKNOWN 优先、原版本/成员依据、40001 整事务新 session 重读及耗尽留待办 |
| 单据 MOCK 入口 | [document cycle](../../tests/businesses/test_mock_document_cycle.py) | 原受理事务、并发单据冲突、显式许可、原 Result/事实回报、回滚/丢返回/丢通知、双线隔离、独立 RR 重试 |
| 设备 MOCK 入口 | [device cycle](../../tests/businesses/test_mock_device_cycle.py) | 跨入口原周期、Result 先于 ACK、错误线/角色拒绝、冻结目标/配置门禁、双线结果隔离、矛盾结果不能完成 |

## 已执行专项结果

以下为任务交接时的实际范围，后续代码可能覆盖其部分文件；不把重叠套件相加，也不作为最终全仓结果。

| 任务 | 运行范围与实际结果 | 限制 |
| --- | --- | --- |
| Task 1 WMS | WMS request + dev controller：203 passed；隔离业务消费/两线/两种 MOCK：73 passed；相关 Ruff/格式及 diff 检查通过 | ECS 关联提示随后由 Task 2 收敛；不是全仓 |
| Task 2 ECS | ECS/business/device-route/CLI/registry 联合：844 passed, 20 skipped；独立 bootstrap：26 passed；两种 MOCK 共 34 cases 保留且包含在通过范围；相关 Ruff/格式及 diff 检查通过 | 跳过为既有后端条件，显式 PG/Redis 已配置；Task 3 随后更改发现，不能当最终结果 |
| Task 3 来源/加载 | 最终 registration/bootstrap：59 passed；最终三 registry/CLI：334 passed, 3 skipped；相关 Ruff/格式及 diff 检查通过 | 3 skipped 为 SQLite ECS 并发变体，对应 PG 已运行；此前 490 passed 是较早扩展范围，不与最终结果累加 |
| Task 4 文档 | 38 份当前/历史文档的 355 个链接目标/锚点通过；23 个 Python、15 个 shell 示例语法通过；app AST、全部 scripts/*.sh、7 个 docstring 文件 Ruff/格式及 diff 通过；冷 production/local CLI 均退出 0，正式 catalog/命令为空、注册项分别 2/6 | README 两个既有 seed 命令用抽象占位符，语法检查时仅临时替换占位符；未运行 DB 套件或真实对端 |

上述 DB 专项使用任务专属 PostgreSQL/Redis，不连接真实对端。Task 3 结束后只读审计：除 alembic_version 外公开测试表零行、Redis DB0 零键；这是当时清理状态，不替代最终资源审计。

## 最终综合验收

2026-10-28，在 `feature/worker-contract-bootstrap` 当前工作区完成。
所有源码修复完成后，使用锁定依赖和四项显式配置运行全仓：
`TEST_DATABASE_URL`、`TEST_REDIS_URL`、`DEVICE_MIGRATION_DATABASE_URL`、`BINDING_MIGRATION_DATABASE_URL`。
前两项为任务专属 loopback PostgreSQL/Redis，两个迁移库相互独立；未连接默认开发库或真实对端。
实际命令为 `uv run --frozen --no-sync pytest -ra --tb=short`。

- 全仓收集 3292 项，**3264 passed、28 skipped，210.64 秒，退出 0**。
  28 项为既有 SQLite 参数分支不具备 PostgreSQL 并发、行锁或独立连接语义的跳过；对应 PostgreSQL 分支已在本次显式配置范围内运行，跳过不计作通过。
  原有单据 MOCK 25 项、设备 MOCK 9 项全部保留并通过；全仓同时覆盖并发受理、回滚、丢返回/通知、重启、UNKNOWN/HELD、完整前序和 40001 重试等现有行为。
- 逐任务规格/质量评审完成，综合审查发现的两个问题均修复并经独立复审关闭，无遗留发现。
  调试 ECS 原始 NaN/无穷值曾在模型序列化时转成 null；现在先复用严格 JSON 校验，再进入原模型及唯一入队核心。
  原有四个 HTTP 用例先复现失败、修复后全部通过，仍断言 400 且无命令/Outbox；随后相关完整套件 820 passed、20 skipped，再完成上述全仓复验。
  两个 Markdown Python 示例仅修正格式，AST 与周边正文保持一致。
- 全仓 Ruff 检查、格式检查（446 files already formatted）和 `git diff --check` 通过。
  源码 AST/接口扫描覆盖 app/tests/scripts 共 366 个 Python 文件，检查 42 处 WMS send 和 49 处 create 调用候选：
  旧公开签名、旧 composition 属性、公开底层绑定/附录导出均为零；三处旧 discovery 模块已删除，正式路由不再保留字符串动作解析。
- API/worker/CLI 冷启动与加载顺序由全仓回归覆盖；另外从临时空目录启动两个全新 CLI 进程，production/local 均退出 0，
  businesses 与 command_contracts 为空，三类注册项分别为 2/6。local 的六个注册项与六个 Transport 场景是不同目录，不混作同一计数。
  当前设计、计划及本验收的本地链接目标/锚点及 Python 示例经最终复核通过；Task 4 的更广文档检查记录保留在上表。
- 最终资源审计遍历所有非系统 schema，包括实际 `enterprise_backend`，检查除 alembic_version 外 18 张表。
  命令、消息、收据、结果、Transport、设备/工作线及其他业务数据均为零，独立 MOCK 表不存在；仅管理种子及测试审计行保留。
  Redis 最终剩余 4 个测试结果键和 2 个诊断测试队列绑定，无业务键；不把测试缓存误报为自然零键。
  随后停止并删除本任务两个专用容器及匿名卷，两个独立迁移库一并清除；复核容器已不存在，原有开发容器未操作。
- 原 `.gitignore`、两份人工/粗分合同文档的工作区与索引哈希，以及完整暂存补丁，均与本轮入口快照一致。
  上述验证完成时，既有未提交实现和文档在当前内容上迁移；仍停留原功能分支、HEAD 未改变，未暂存、提交、推送、合并或部署。

2026-10-28，已将被完整替代的业务基础/简化设计、旧阶段计划、worker 启动专项计划及阶段 2–6 旧验收共 9 份文档移出仓库归档。
归档位于仓库同级 `maintenance-admin-archives/` 的当日目录，按原路径原样保留当前工作区内容、历史数字和限制，并附索引及 SHA-256 清单；未覆盖未提交修改。
现行能力与验证以本文和唯一用法设计/实施计划为准。真实业务、供应商合同、真实 WMS/ECS/设备、部署和现场物理验收不在本轮结果内。

## 交付授权（2026-10-28）

用户已授权将本轮单一 API 收敛、MOCK 验证及文档清理提交并推送当前功能分支。
两份原有业务合同文档的暂存修改和 .gitignore 修改不纳入提交；仓库外归档仅在本地保留。
实际提交与远端状态以 Git 记录为准，本授权不改变上述验证范围及真实对端/部署边界。

## 交付前复验（2026-10-28）

以下补充交付前修复后的结果；上文 3264 passed 等数字保留为当时记录，不与本次结果累加。

- 修复 ECS 已校验模型的 JSON 输出被再次按 Python 字段类型校验而拒绝的问题；实际字段仍先重验，再按原模型 JSON 规则冻结，保留别名、日期/tuple 和改变输出类型的序列化器。
- 公开 `create` 入口新增三种合同形状，分别运行 SQLite/PostgreSQL，共 6 个变体先复现失败、修复后通过；同键重入继续返回原命令和原字节，不新增命令或 Outbox。
- 修复后全仓收集 **3298 项，3270 passed、28 skipped，213.51 秒，退出 0**。四项独立数据库/Redis 配置均显式提供；28 项仍为既有 SQLite 条件跳过，对应 PostgreSQL 分支已运行。原单据/设备两种 MOCK 共 34 项保留并通过。
- 逐任务计划审计 24/24 完成；代表性行为族核对 30/30，此数不是代码行覆盖率。综合及独立对抗复审无遗留发现；外部 Claude Code 未安装，未获得该外部评审覆盖。
- Docker 应用镜像构建、全仓 Ruff 检查与格式检查（437 files already formatted）及 diff 检查通过；仅为内部构建和验证，未部署。
- 本次专属存储清理前检查 18 张非版本表：业务/义务零行、MOCK 表不存在；Redis 35 个键均为已知测试命名空间，无业务键。随后专属容器、匿名卷及独立迁移库一并移除，原开发容器未操作。

正式业务 catalog 仍为空；真实合同、WMS/ECS/设备及现场物理验收不在以上结果内。

## catalog 归属修复与原简化设计复验（2026-10-28）

本轮从 `develop@fb01bb2` 新建 `feature/catalog-registration-ownership`，修复只读评审发现的正式注册归属缺口。
共享合同校验现在反查 ECS Event、WMS callback/request 的实际注册来源；`app.businesses.*` 正式项必须属于
当前 catalog 显式声明的正式 ENTRY_MODULES 模块或包树。依赖 import 不能启用未列业务、相似名称业务，
也不能启用同业务未声明入口；已列业务之间的合法引用保留。固定平台、local 和隔离非 app 测试边界沿用原规则。
原角色/初始入口诊断先执行，全部检查成功后才保存已验证 catalog；API/worker 在 local/资源/工厂前失败，
CLI 失败不输出部分成功目录。未新增注册表、发现树、发送器、业务表、迁移、配置或依赖。

负向回归位于 `tests/integrations/test_registration_bootstrap.py`，以真实临时模块和新进程验证三类注册，
分别经过 catalog 和 CLI：旧实现 **18 failed、6 passed**；修复后 **24 passed**。
负向覆盖未列业务、相似业务名前缀、同业务未声明入口，正向覆盖另一个已列业务的合法入口引用。
原合法 WMS 初始回调夹具补齐其 ENTRY_MODULES 声明；启动/catalog/CLI 扩展套件 **124 passed**。

本轮读取了仓库同级归档中 `2026-10-28-obsolete-business-docs-rllqnzwp` 目录内
`2026-10-27-business-development-simplification-design.md` 原文。该文已经标记为历史设计，未恢复到仓库。
按其第 10 节 A1–A10 的安全目标与当前唯一用法设计交叉验收：

| 原标准 | 当前验收口径与证据 | 结论 |
| --- | --- | --- |
| A1 合同/来源 | 原类身份、严格嵌套字段、非法声明/重复键、未声明子类；declaration/model_create/WMS service 回归 | 通过，extra 策略按原模型保留 |
| A2 发现/隔离 | 本轮归属负向回归；API/worker 冷启动、正式先于 local、固定平台与 CLI 目录回归 | 通过，原 P2 缺口关闭 |
| A3 WMS 等价 | 模型唯一入口；精确类型、别名/null/序列化、同 target 原身份/字节/TTL、冲突和回滚回归 | 按现行模型 API 通过 |
| A4 ECS 等价 | 多角色共享原模型、合法 extra、同调用原命令/字节/期限、非法超时与回滚回归 | 按现行 device_roles/COMMANDS 通过 |
| A5 角色门禁 | 同动作/同附录不同角色、未启用或错误合同、声明路由能力和 local 唯一 Sender 回归 | 通过 |
| A6 历史恢复 | 原 Result 合同、重启/换绑/撤销、迟到/冲突/先于 ACK、历史附录不授权新发送回归 | 通过 |
| A7 原子受理/消费 | 独立 PostgreSQL 上原收据/受理/待办、消费和下一意图原子、失败回滚与重复重入 | 通过 |
| A8 快照/恢复 | 完整前序、更新 UNKNOWN、统一 RR 快照、40001 新 session 整事务重试和耗尽留待办 | 通过 |
| A9 完成边界 | ACK/投递终态/历史里程碑不独立完成；拒绝、无效响应、失败、超时和未知保持原身份 | 通过 |
| A10 内部体验 | 单据/设备两类 MOCK 共34项、生产 catalog 空、公开结果读取和现有唯一 Sender | 按现行唯一用法通过 |

原文 A3/A4/A10 及第 8 节中的旧签名继续可用、单数命令角色、手工绑定回退和双业务来源，
已由后续明确批准的单一 API 设计替代，不作为本轮通过项，也不为满足历史文字恢复旧用法。
wire、原身份/冻结记录、结果合同、UNKNOWN/HELD 和事务恢复要求继续验收。

最终使用锁定依赖、专属 loopback PostgreSQL/Redis 及两个相互独立的迁移数据库运行全仓：
**3322 collected，3294 passed、28 skipped，214.26 秒，退出 0**。
28 项为 SQLite 下不适用的 PostgreSQL 行锁/并发/独立连接分支；对应 PostgreSQL 分支已运行。
全仓后仅整理新增测试格式，并重新运行同24项归属回归；源码行为未改变。
全仓 Ruff/格式、文档引用及旧 API 残留检查通过；production/local CLI 正式业务和命令仍为空。
独立只读复审无未解决发现。清理前检查全部非系统 schema，业务义务零行、独立 MOCK 表不存在，
随后清理任务专属存储与匿名卷；原开发存储未操作。

原 `.gitignore`、人工/粗分两份文档的文件内容、索引及完整暂存补丁保持不变；上述实施验收结束时，本轮改动全部未暂存，未提交、推送、合并或部署。
用户随后授权提交并推送本节修复、回归和文档更新，原三项修改不纳入提交；实际提交及远端状态以 Git 记录为准。
未运行真实对端、现场/物理验收、混合版本部署回退或真人上手测量。

## catalog 校验后首次导入防护（2026-10-28 增量）

本轮从 `develop@dcc684c` 新建 `feature/catalog-late-registration-guard`，补齐正式校验之后的注册边界。
工厂和业务方法首次 import 三类正式入口时，实际装饰器在写入注册表前复用最终合同校验的
精确模块/点分包归属规则；未列业务、相似业务名前缀、同业务未声明入口立即失败。
拒绝的项不进入目录、不占用键，业务无法继续到下一意图或 WMS 入队；合法已列业务共享仍可用。
初始组合模块导入不读取尚未完整的 composition，ECS 命令模型仍只通过 `COMMANDS` 编译。
成功校验后的注册保护与运行时有效缓存分开：再次校验失败使运行时装配失效，不能重新开放初始注册阶段。
固定平台、local、隔离非 app 夹具及原冻结记录/身份/Result/事务边界保持。

新增冷进程回归通过真实临时业务 composition、三类装饰器和公开 `build_runtime`/业务方法验证：
三类 × 工厂/方法 × 四种来源共 24 项；其中 18 个非法来源在旧实现均因注册被接受而失败，
6 个合法跨业务对照通过。相似模块名前缀及失败复验之后的首次导入另有 2 项，旧实现均复现失败。
修复后上述 **26 项全部通过**。每个非法来源还验证目录无该项、没有下一意图，
并在已声明包下创建新的合法子模块，用同一键再次完成真实注册和调用，验证拒绝没有占键副作用。
合法共享的正常对照沿用启动时的完整发现；新增合法子模块只用于保持恢复注册的首次导入状态。

注册/bootstrap/发现、catalog/装配/运行时/模型加载/CLI、ECS 声明与模型创建、WMS 发送定向套件
**453 collected，412 passed、41 skipped，29.35 秒，退出 0**。
四项测试存储变量全部显式移除，41 项为需要独立 PostgreSQL 的 ECS 模型创建变体，不计作通过。
受影响的发现单元夹具显式恢复初始发现阶段，避免继承 `app.main` 的启动校验状态；
新防护本身始终在独立冷进程中验证。相关四份 Python 文件 Ruff/格式及 diff 检查通过。

以上为实施者首阶段定向验证，随后控制器执行独立存储全仓验证并发现下述分层问题。
以下按发生顺序保留失败、修复和最终复验结果，不能引用上文历史全仓数字替代本轮结果。
所有外部交互均为内部替身/既有 MOCK；未连接真实 WMS/ECS/设备，未提交、推送、合并或部署。

控制器首轮全仓验证为 **1 failed、3321 passed、28 skipped，218.29 秒**，唯一失败是现有分层检查：
共享 registration 直接依赖业务 catalog。修复后改经既有 discovery 桥接访问同一归属函数，
未改分层规则或架构测试，保护时序、首次校验阶段和失败复验后的持续保护均保持。
架构套件先复现 **1 failed、27 passed**；修复后与上述定向范围联合验证为
**481 collected，440 passed、41 skipped，30.34 秒，退出 0**，五份 Python 文件 Ruff/格式与 diff 检查通过。
这些是修复后的定向结果；第二轮独立存储全仓复验和清理结果补记如下。

控制器随后使用锁定依赖、任务专属 loopback PostgreSQL/Redis 及两个相互独立的迁移库，
再次运行 `uv run --frozen --no-sync pytest -ra --tb=short`：
**3350 collected，3322 passed、28 skipped，215.47 秒，退出 0**（外层执行耗时 218.7 秒）。
28 项为既有 SQLite 参数分支不具备 PostgreSQL 行锁、并发仲裁或独立连接语义而跳过，
对应 PostgreSQL 分支已运行；跳过不计作通过。设备 MOCK 9 项、单据 MOCK 25 项共 **34 项全部通过**，
已包含在本次全仓结果中，不再累加。全仓运行前后的源码校验值一致，之后仅补充本轮验收和计划文档。

全仓 Ruff 与格式检查通过（438 files already formatted）；52 份文档的 316 个本地链接/图片目标无问题，
app/scripts/tests AST 的旧公开 API、单数命令角色及旧 composition 赋值残留为零。
production/local CLI 的正式业务和命令合同仍为空，注册项分别为 2/6；CLI 结果不代表真实对端或工厂执行验收。
任务修复波只读复审已确认分层问题解决，无新增问题；最终全分支独立只读复审通过，无未解决发现。
复审核对三类实际注册、命令声明隔离、冷启动/失败复验时序及真实冷进程回归，源码仍与上述通过的全仓校验值一致。

清理前审计全部非系统 schema 的 18 张非版本表：业务和义务数据为零，独立 MOCK 表不存在；
管理种子及测试审计行保留。Redis 的 35 个键均属已知测试命名空间（29 个限流、4 个 Celery 测试结果、
2 个诊断测试队列绑定），业务键为零。随后只清除本任务两个专用容器及其匿名卷，独立迁移库一并清除，
原开发存储未操作。原三项文件的内容、索引及完整暂存补丁与入口快照一致，HEAD 仍为 `dcc684c`。
本轮实现和文档保持未暂存，未提交、推送、合并或部署；真实 WMS/ECS/设备和现场物理验收不在结果内。

用户随后授权提交并推送本节防护、负向回归和文档更新，原三项修改及暂存状态继续保留、不纳入提交。
上文未提交/推送为实施验收结束时的记录；本轮交付的提交及远端状态以 Git 为准，不合并、不部署。
