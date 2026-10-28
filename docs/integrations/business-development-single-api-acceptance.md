# 业务开发单一 API 验收记录

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
