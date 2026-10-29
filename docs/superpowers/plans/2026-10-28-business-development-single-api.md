# 业务开发唯一用法实施计划

> Task 1–4、原分支、交付授权及首轮 catalog 归属修复均保留当时记录；首轮修复已合入 develop。
> “校验后首次导入”增量已完成，并通过 [PR #35](https://github.com/kaizhoumasha/maintenance-admin/pull/35) 合入 develop；
> 原两份业务合同文档及 `.gitignore` 修改已通过 [PR #36](https://github.com/kaizhoumasha/maintenance-admin/pull/36) 合入 develop。
> 下文分支、文件保护要求及交付授权均保留当时记录；当前能力与验证边界见[单一 API 验收记录](../../integrations/business-development-single-api-acceptance.md)，实际交付以 Git 记录为准。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development。任务顺序实施、逐项评审；提交范围遵循全局约束及末尾交付授权。

**Goal:** 公开模型调用、命令声明和业务目录各保留唯一形式，迁移所有内部/调试/测试/文档调用并移除旧分支。

**Architecture:** 复用原接收器、Outbox/Sender、结果读取和事务配方。业务只在 app.businesses 的显式 catalog；命令模型由 catalog 编译。动态调试由可信平台适配器解析后进入同一私有核心，不再调用旧公开签名。

**Tech Stack:** 现有 Python/Pydantic/SQLAlchemy/pytest，loopback MOCK 与任务专属 PostgreSQL/Redis，锁定依赖。

**Spec:** [唯一用法设计](../specs/2026-10-28-business-development-single-api-design.md)。用户已明确要求更新后直接继续实施，沿用当前功能分支和子代理方式。

## Global Constraints

- 留在 `/Users/kaizhou/codeDev/maintenance-admin` 的 `feature/worker-contract-bootstrap`；用户现已授权提交并推送本轮单一 API、MOCK 与文档清理变更，排除两份原有业务合同文档及 .gitignore 修改；不合并或部署。
- 在当前未提交工作上叠加迁移；原 .gitignore、人工/粗分文档的工作区及索引不变，其他已有改动不得丢失。
- 正式 catalog 为空；不新增生产业务、模型/迁移、配置或依赖，不连接真实对端。
- 原身份/字节/路由/期限/历史 Result、UNKNOWN/HELD、权限、事务/重试语义不变；无第二 Sender 或通用 execute/worker。
- 受理原事务；推进新 session/RR 首 SQL 前设置，WorkLine→Device→业务动作；原消费依据及下一意图原子；40001 有界整事务重试。
- 所有业务调用均迁到精确模型；活动源码/指南不保留 legacy 回退、签名别名或双业务目录；已替代的历史材料保留原文并移出仓库归档。
- 模拟表独立 MetaData，fixtures/finally 清理；测试只校验长期行为，不新增旧参数拒绝等过渡测试。

## Review Focus

- 调试 JSON 必须通过真实模型验证和原授权，不能为去掉旧签名绕过校验；Task 1/2 HTTP 与 malformed 模型测试覆盖。
- 多角色共享保留同模型/附录身份与 extra 语义，错误角色仍拒绝；Task 2 声明、创建与 Result 测试覆盖。
- 旧业务目录不能通过 import/来源伪造绕过清单；平台固定入口不误拒绝，Task 3 冷进程来源/顺序/错误测试覆盖。
- 重启/撤销后原命令不重新解析当前路由，Task 2 原身份/迟到 Result 测试及 MOCK 回归覆盖。
- 全部现行指南/示例与真实接口一致，不能残留“失败时用旧方法”；Task 4 语法/链接、残留扫描与端到端命令覆盖。

## Task 1: WMS 唯一模型发送与调用方迁移

**Files:** `app/integrations/wms_request/service.py`、`app/services/dev_wms_request_service.py`、WMS request 包 docstring、`app/integrations/model_payload.py` 的旧回退提示；WMS request/controllers/service 测试及其他确实调用 WmsRequests.send 的夹具/测试，包括 tests/helpers.py。ECS 旧调用留到 Task 2。

**Interfaces:** `WmsRequests.send(self, session: AsyncSession, payload: BaseModel, *, operation_id: str) -> EnqueueResult`；只精确模型反查，调用方预先实例化原注册模型。`validated_model_payload` 的安全验证保留，失败提示使用明确模型而非旧 API。

- [x] 编写/调整动态调试入口和模型入队的行为测试，先验证真实缺口；去掉只证明旧签名兼容的测试。
- [x] 删除 operation/data 参数、_MISSING 与旧分派；迁移全部 WMS 调用，保留原异常类别/原冻结身份与结果引用。
- [x] 调试 service 把已校验 payload 直接传入；实际字段非法/歧义/未注册不得入队；别名/默认值/序列化、回滚、target/TTL 行为保留。
- [x] 运行 WMS request、dev WMS controller/service、受影响的业务/结果消费测试和 Ruff；报告残留扫描与所有实际改动。

## Task 2: 唯一 ECS 声明、模型创建与装配

**Files:** `app/integrations/ecs_command/{declaration,service,roles,dev,__init__}.py`、`app/businesses/{catalog,runtime}.py`、`app/services/device_routes.py`；相关 ECS/业务/catalog/CLI/HTTP 测试、tests/helpers.py、tests/businesses/conftest.py 及其他实际调用方。

**Interfaces:** 装饰器唯一角色参数为 `device_roles: tuple[str,...]`；CommandDeclaration 保存 `bindings` tuple，无 `.binding` 兼容属性；compile_commands 展开多角色，返回原平台内部表示。composition 只暴露 `COMMANDS`，catalog 编译。EcsCommands.create 仅 command 模型参数。正式路由唯一声明感知解析方法，不保留字符串动作回退；Sender authorize_frozen 行为保持。

- [x] 先用多角色同模型/附录、extra 策略及 JSON 调试的公开行为测试验证缺口；严格类型仍要求，沿原 extra/RootModel 合同，不保留“不能声明就手写”路线。
- [x] 实现多角色声明/编译；catalog 仅消费 COMMANDS，并按本业务 DEFINITION 角色交集激活绑定；每模型至少匹配本业务一角色，全部声明角色须存在于显式 catalog 定义中。保留跨业务角色/附录冲突、来源及初始入口检查，测试同模型/附录跨业务共享与未知角色拒绝，不能隐式授予未列 COMMANDS 的业务命令。移除公共包的 CommandBinding/DeviceAnnex 导出。底层类型保留为平台表示，框架负向/历史解析测试可内部使用，业务样例必须使用声明。
- [x] 删除 create 的 task_type/params/_MISSING/兼容错误改写及路由分派。SDK与可信调试 adapter 共用一个私有入队核心；不得只是把旧公开重载改名为私有旧接口，核心无 legacy 模式，两个入口分别完成其类型/授权解析。
- [x] 迁移调试入口与全部调用方/测试，移除只证明手工/旧调用兼容的测试；保留原冻结 bytes/期限、idempotence、历史 Result、shared extra、权限/路由撤销与单 Sender 的长期行为。
- [x] 更新本轮前已存在的 MOCK 测试支持，在其当前修复上叠加，保留并发受理回归。运行 ECS 全目录、业务/catalog/CLI/dev controller 受影响测试、Ruff 与残留扫描。

## Task 3: 业务目录与显式发现收敛

**Files:** 三类 `app/integrations/*/discovery.py`、`app/integrations/discovery.py`、`registration.py`、相关包 docstring；`tests/integrations/test_registration*.py`、架构/业务装配与 CLI/目录测试。必要的活动 loader 调用方一起迁移。

**Interfaces:** 统一加载器仅合并固定平台入口与 catalog.entry_modules；业务只用 app.businesses。正式平台 callback=`app.integrations.wms_transport.callback`，local 现有 ECS/WMS demo 与 Transport dev 模块固定列出。无 app.integrations 任意子包扫描/业务来源兼容，按显式模块包导入树仍允许。

- [x] 先写业务 catalog 冷进程发现、固定平台入口和 local 隔离/缺失模块测试，验证目录收敛缺口。
- [x] 移除 pkgutil integration-root 候选生成，集中平台固定清单；去掉不再使用的包装/发现兼容路径，迁移其真实调用方，保持 API/worker/CLI 先正式/校验后 local/资源。
- [x] 来源/文件名检查支持真实 app.businesses 业务与固定平台入口；任意 app.integrations.foo 不能绕过 import 注册；不依赖自动扫描、不允许 local 修补正式缺项。
- [x] 迁移原双来源测试为唯一来源和平台边界行为；保留文件/包、角色、冲突诊断和冷启动无配置/无资源基线。运行注册/架构/catalog/CLI/目录相关套件。

## Task 4: 现行指南、工程约束与验收

**Files:** AGENTS.md、业务快速开始/业务包/注册/ECS/角色/事务与请求指南、相关包 docstring/README 和脚本示例；新建 `docs/integrations/business-development-single-api-acceptance.md`。旧设计/计划/阶段2–6记录原样移出仓库归档，仓库内引用转向现行文档；不改两份原暂存业务合同文档。

**Interfaces:** 所有活动示例只用 model send/create、device_roles、COMMANDS、app.businesses；旧协议字段 task_type/params 仍可出现在 HTTP/wire 参考，不误删。历史验收数字不冒充本轮新结果。

- [x] 全量检索活动指南及 docstring 的旧调用/手工声明/双目录/回退措辞，逐项更新；AGENTS.md 仅更新本次架构相关规则。
- [x] 写唯一接入路线、动态调试边界、支持模型约束、MOCK 验证矩阵与不兼容变化；已被完整替代的设计/计划/旧验收保留原记录并移出仓库归档。
- [x] 验证本地链接/锚点、Python AST、shell 语法和实际相关命令；报告活动源码/示例残留扫描，区分必要的内部 wire 表示。

## 最终验证

- [x] 逐任务规格/质量评审，最后综合审查及单次修复复审。
- [x] 控制器显式配置任务专属 PostgreSQL/Redis/两个独立迁移库运行全仓 pytest、Ruff、格式、diff 检查；完整运行前协调无其他 DB 用例并发。
- [x] 两种 MOCK 入口及并发/回滚/重启/未知、完整前序/40001 基线无丢失；冷进程 API/worker/CLI 和正式目录保持正确。
- [x] 审计本轮资源与完整暂存补丁，清理自建容器；执行记录/验收写实际结果；实施阶段未提交推送，后续按下述交付授权执行。

## 实施结果（2026-10-28）

四项任务均完成规格与质量评审；综合审查后的单次修复及复审完成，无遗留发现。
全仓最终复验 3264 passed、28 skipped（3292 collected，210.64 秒），Ruff、446 文件格式检查及 diff 检查通过。
两种既有 MOCK 共 34 项全部保留通过；公开接口/声明/目录旧形式扫描为零，三处旧发现模块删除。
全新 production/local CLI 的正式业务及命令均为空；两个任务专用容器及匿名卷已清理。
实施验收完成时，原保护文件及完整暂存补丁不变，留在当前功能分支，未提交推送。
完整范围、跳过原因、修复与资源审计见[本轮验收记录](../../integrations/business-development-single-api-acceptance.md)。

## 交付授权（2026-10-28）

用户已授权提交并推送本轮单一 API 收敛、两类 MOCK 验证和文档清理，目标为当前功能分支。
两份原有业务合同文档继续保留原暂存修改，不进入本次提交；原 .gitignore 修改也保留在本地。
九份过期文档的仓库删除与现行引用更新纳入提交，仓库外归档原文和校验清单继续本地保留。
实际提交与远端状态以 Git 记录为准；本次不合并或部署。

## catalog 归属校验修复（2026-10-28）

用户已批准修复验收发现的归属校验缺口；当前增量授权覆盖本节，提交/推送按本节之后的新授权执行。
复用最终合同交叉校验，在三类实际注册项与 catalog 正式 ENTRY_MODULES 之间核对模块/包归属，
不在装饰器引入第二个注册表、动态扫描或新的发送路径。

- [x] 在现有冷进程测试中复现：未列业务、相似前缀、同业务未声明入口经依赖导入均失败；已列业务合法引用保持。
- [x] 补齐共享正式合同校验，覆盖 ECS Event、WMS callback/request；CLI 失败无部分目录，失败 catalog 不可用于装配。
- [x] 同步现行设计和注册指南，明确间接 import 也受显式清单约束。
- [x] 完成 API/worker/CLI、固定平台/local、两类 MOCK 和全仓回归，清理自建资源并核验原暂存补丁。
- [x] 按归档的 `2026-10-27-business-development-simplification-design.md` 第 10 节 A1–A10 复验，
  其中旧 API/手工回退/双业务来源要求按后续唯一用法设计替代，保留原身份、事务、恢复和完成边界。

最终实测结果及评审结论追加到[单一 API 验收记录](../../integrations/business-development-single-api-acceptance.md)，
不恢复已归档的过期设计文件，不连接真实 WMS/ECS/设备；实施验收结束时未提交、推送或部署。

## catalog 修复交付授权（2026-10-28）

用户随后授权提交并推送本轮 catalog 归属修复、负向回归和文档更新到当前功能分支。
原 `.gitignore`、人工/粗分两份文档的修改和暂存状态继续保留，不纳入提交；不合并或部署。

## 校验后首次导入增量（2026-10-28）

首轮 catalog 修复合入 develop 后，评审发现工厂/业务方法首次导入可发生在最终校验之后，
用户当时授权从 develop 新建 `feature/catalog-late-registration-guard` 修复。
该增量已完成，并通过 [PR #35](https://github.com/kaizhoumasha/maintenance-admin/pull/35) 合入 develop；具体任务和实际验收见
[增量实施计划](2026-10-28-catalog-late-registration.md)。实施验收阶段保留原三项修改及暂存状态，未提交推送或部署；这些保护要求和阶段授权仅记录当时范围。
原两份业务合同文档及 `.gitignore` 修改随后通过 [PR #36](https://github.com/kaizhoumasha/maintenance-admin/pull/36) 合入 develop。
当前能力与验证边界见[单一 API 验收记录](../../integrations/business-development-single-api-acceptance.md)，继续区分 MOCK/内部验证与真实对端、设备和现场验收。
