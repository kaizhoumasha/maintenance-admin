# catalog 校验后首次导入归属防护实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development。按任务执行并复核；用户要求不提交推送。

**Goal:** 业务工厂或业务方法首次导入清单外正式入口时立即拒绝注册，不能绕过先前已完成的 catalog 校验。

**Architecture:** 复用共享 registration 的三类实际注册路径，经既有 discovery 桥接访问 catalog 归属判定，在已完成正式校验的阶段、registry 写入前检查。registration 不直接依赖业务层。首次启动保留完整最终合同校验，避免读取未初始化 composition；不为每次发送增加全量编译。

**Tech Stack:** 现有 Python/Pydantic/pytest，冷进程真实临时模块、MOCK 与任务专属 PostgreSQL/Redis，锁定依赖。

**Spec:** [唯一用法设计·校验后首次导入增量](../specs/2026-10-28-business-development-single-api-design.md#校验后首次导入的归属边界2026-10-28-增量)。

## Global Constraints

- 在主目录的 `feature/catalog-late-registration-guard` 工作，基线 `develop@dcc684c`；不提交、不推送、不合并、不部署。
- 原 `.gitignore`、`docs/integrations/manual-picking-api.md`、`docs/integrations/coarse-sorting-api-draft.md` 的文件内容、索引及完整暂存补丁不变；只具名操作本轮文件。
- 正式 catalog 仍为空；不新增生产业务、Base表/迁移、依赖、配置、第二注册表/Sender、通用业务 worker 或旧API兼容分支。
- 保持原模型声明、平台/local/非app夹具、合法已列业务共享，保持原身份/冻结字节/期限/路由/Result/UNKNOWN与HELD及事务恢复边界。
- 使用MOCK与内部验证，不连接真实WMS/ECS/设备；独立测试表/临时模块和任务专属存储最终清理，原存储不操作。

## Review Focus

- 首次启动时组合模块可能正在导入声明模型：不得在元数据未完整时递归检查catalog；Task1保留冷启动/声明/CLI回归。
- 工厂之后的业务方法也能首次导入：Task1须分别覆盖两个公开运行时入口时点，不能只在build_runtime末尾补查。
- 三类注册及模块/点分包边界共用一条规则：Task1覆盖ECS Event、WMS callback/request，未列/同业务未声明/相似名称与合法已列共享。
- 注册失败不得污染注册表或占用operation/event键：Task1验证拒绝后目录无非法项，合法后续声明不被误判重复。
- 正式先于local、固定平台与隔离非app保持，命令模型不误套ENTRY_MODULES：Task1复用既有正向边界及命令声明用例。

### Task 1: 修复共享注册边界并完成回归和文档

**Files:** 修改 `app/businesses/catalog.py`、`app/integrations/registration.py`、`app/integrations/discovery.py`；回归放入现有 `tests/integrations/test_registration_bootstrap.py`，必要时仅调整受影响的真实隔离夹具，并验证既有 `tests/architecture/test_layering.py`。更新 `docs/integrations/registration.md`、`docs/integrations/business-development-single-api-acceptance.md`，回填本计划结果。现行设计增量由控制器先写，不改保护文件。

**Interfaces:** 不变更公开SDK/HTTP/声明签名。catalog提供共享精确模块/点分包归属检查供最终合同校验和校验后的三类实际注册共用；接在三类函数/operation共同文件校验路径，由既有 discovery 桥接转交 catalog，不给分层规则添加例外，不能误用于ECS命令模型来源校验。未验证阶段保留既有最终检查；已验证目录的新正式注册在写入registry前核验。

- [x] 先写真实临时模块+新进程回归：kind=`ecs_event/callback/request`，时点=`factory/method`，来源至少包含未列业务、同业务未声明、相似业务名，及另一完整已列业务合法入口对照。通过公开build_runtime/业务方法触发首次导入；非法来源抛明确错误且目录没有该项，方法不能到达下一意图/捕获Outbox；合法共享仍能装配和使用。
- [x] 在旧实现运行新增用例，确认因非法注册被接受而失败，保存RED命令/输出；语法/夹具错误不算RED。新增一个拒绝不占键、合法后续注册可继续使用的行为用例。
- [x] 在共享registration与catalog实现最小检查，复用归属规则；不增加发送检查/新loader/新状态调度，不用工厂结束补查替代注册时防护。保持初始化顺序与既有最终校验失败关闭。
- [x] 运行新增GREEN及注册/bootstrap/发现、catalog/装配/模型加载/CLI、ECS声明/模型创建和WMS发送定向套件；无DB参数的阶段明确跳过PG变体。
- [x] 同步注册指南与验收说明，明确新时序、拒绝前无注册副作用、当前唯一用法与MOCK边界；历史测试数字保留原时序，不冒充本轮结果。
- [x] 控制器执行独立PG/Redis和两个迁移库的完整pytest、两类MOCK、Ruff/格式、链接/残留及保护状态核验；保存实际结果并回填文档。实施者不启动DB套件或修改父任务测试存储。
- [x] 清理任务资源及临时模块，确认原三项文件及暂存补丁不变。本轮不提交、推送、部署。
- [x] 最终全分支独立只读复审通过，无未解决发现；逐任务修复波已复审通过。

## 实施与验收结果

Task 1 实施和自审完成，未提交、暂存或推送。公开运行时冷进程矩阵旧实现为 18 failed / 6 passed；
相似模块名前缀与失败复验后导入旧实现另有 2 failed。修复后上述 26 项全部通过。
定向套件最终 453 collected、412 passed、41 skipped（29.35 秒，退出 0）；
`TEST_DATABASE_URL`、`TEST_REDIS_URL` 及两项迁移库变量明确移除，跳过为 PostgreSQL 变体。
相关四份 Python 文件 Ruff/格式、diff 检查通过；详细 RED/GREEN 日志与实施报告保留在
`.superpowers/sdd/2026-10-28-catalog-late-registration/`。

成功阶段保护使用一个布尔标记，与运行时验证缓存分开；失败复验不重新开放启动导入，
仍由同一归属函数处理最终全量检查和后续实际注册。首次启动组合模块和命令声明不提前读取目录。
此阶段后的控制器完整 PostgreSQL/Redis/迁移/MOCK、复审及清理/保护核验结果按时序补记在下文，不以定向套件或历史结果替代。
用户已授权本增量设计/计划同步并继续修复；本计划不重新授权历史提交、推送或合并。

修复轮次 1：控制器首轮全仓 1 failed、3321 passed、28 skipped（218.29 秒），
唯一失败为既有分层规则禁止 registration 直接依赖业务 catalog。
架构套件先复现 1 failed、27 passed；依赖改经既有 discovery 桥接，同一归属函数/成功阶段标记保持，
未改架构测试或增加例外。修复后架构与全部原定向范围联合：481 collected、440 passed、41 skipped
（30.34 秒，退出 0）；五份 Python 文件 Ruff/格式、diff 检查通过。
首轮全仓结果不能作为修复后全仓通过证据；控制器精确修复波复审确认分层问题已解决，无新增问题。

最终存储复验：控制器再次在任务专属 loopback PostgreSQL/Redis 和两个独立迁移库运行锁定依赖全仓 pytest，
3350 collected、3322 passed、28 skipped（pytest 215.47 秒，外层 218.7 秒，退出 0）。
28 项为既有 SQLite 条件分支，对应 PostgreSQL 变体已运行；设备/单据两种 MOCK 9+25 共 34 项通过，包含在全仓结果内。
运行前后源码校验值一致。全仓 Ruff/格式通过（438 files already formatted），52 份文档 316 个本地链接/图片目标通过，
app/scripts/tests AST 旧 API 残留为零，production/local CLI 的业务/命令为空、注册项 2/6。

清理前 18 张非版本用户表的业务/义务数据为零、MOCK 表不存在；管理种子和测试审计行保留。
Redis 35 个已知测试键、业务键为零。仅移除本任务两个专属容器及匿名卷，独立迁移库随之清除，原开发存储未操作。
原三项文件内容/索引及完整暂存补丁保持，HEAD 仍为 dcc684c；本轮新增工作未暂存、未提交/推送/合并/部署。
最终全分支独立只读复审通过，无未解决发现；源码与通过的完整验证一致。本增量按 MOCK/内部口径完成，真实对端、设备和现场物理验收仍在范围外。

## 本轮交付授权（2026-10-28）

用户随后授权提交并推送本轮 catalog 校验后首次导入防护、负向回归和文档更新到 `feature/catalog-late-registration-guard`。
原 `.gitignore`、人工/粗分两份文档的文件内容及暂存状态继续保留，不纳入本次提交；本轮不合并、不部署。
上述“未提交/推送”描述保留实施验收结束时的事实，本次交付的提交及远端状态以 Git 记录为准。
