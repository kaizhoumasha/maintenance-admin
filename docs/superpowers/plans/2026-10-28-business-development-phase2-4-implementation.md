# 业务开发简化阶段 2–4 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。各任务顺序实施，每个任务独立规格/质量评审，最后做全分支综合评审。保留工作区及索引，不提交或推送。

**Goal:** 实现 ECS 显式声明/编译、WMS 模型调用和声明感知 ECS 模型调用；保持旧接口、原消息和历史合同兼容，并核对真实业务阶段的进入条件。

**Architecture:** 新声明只转换为现有 CommandBinding/DeviceAnnex；WMS 两种调用共用现有注册表和入队路径；ECS 两种调用共用原创建、去重、冻结及 Outbox 路径。声明角色校验仅在新建时由可信路由完成，已有调用优先恢复原命令。

**Tech Stack:** 现有 Python、Pydantic、SQLAlchemy、pytest；真实 PostgreSQL/Redis 仅用任务专属可丢弃存储，不新增依赖。

**Spec:** [开发简化设计](../specs/2026-10-27-business-development-simplification-design.md) §2.2、§6、§8、阶段 2–6、A1–A10。该设计的示例不是供应商确认合同。

## Global Constraints

- 在主目录 `feature/worker-contract-bootstrap` 连续实施；本代理不修改两份原有已暂存接口文档、整份索引及其并行更新，不改写此前 worker 修复。
- 不提交、推送、合并、部署或发送真实 WMS/ECS 请求；代码审查按每任务工作树快照差异进行，不为了生成 Git 范围而提交。
- 正式 `catalog.entries()` 保持为空；示例只在隔离测试/文档，不进入生产 Base/迁移。无新表、配置、依赖、发现树、全局业务注册表、通用 execute、业务 worker、事务 runner 或第二 Sender。
- HTTP/wire、operation/task_type、owner/call_key、operation_id、JSON 规范化、原 bytes/origin/路由字段、UNKNOWN/HELD、结果语义和保留规则不变。
- 新 ECS 命令沿用 `PROTOCOL_RETRY_WINDOW=300 秒`；已有命令保留原期限。手工 CommandBinding/DeviceAnnex、共享模型和宽松旧合同继续可用，不收紧原模型/附录。
- 正式入口完整加载 → 合同校验 → local 入口 → 资源/工厂顺序不变；声明/编译不访问配置、数据库或网络，不执行工厂/处理器。
- 子代理遵循 TDD，报告真实 RED/GREEN 和聚焦回归；控制器最后运行一次完整独立存储套件、Ruff、文档和差异检查。

## Review Focus

- model_construct、赋值后变异、嵌套模型/容器不能绕过严格验证；别名/默认值/null/合法序列化保持。
- 精确类型而非 isinstance/继承定位声明；重复或失败声明不能留下半份元数据/输出，诊断保留原类来源和修复方向。
- 两角色共享同动作/附录时，新模型调用仍核对实际角色；旧形态和历史 Result 不受新增约束影响。
- 原调用在改址/撤销/换合同后仍优先返回原行，不调用新建路由；普通路由缺声明能力必须拒绝新建，不降级。
- local 只允许显式正式角色委托，通用 v3 dev 不授予新式发送权限；API/worker/独立 CLI 目录一致，持久格式和冻结字段不增加声明元数据。

## Task 1: ECS 显式声明与纯编译（阶段 2）

**Files:** 新增 `app/integrations/ecs_command/declaration.py`、`tests/integrations/ecs_command/test_declaration.py`；修改该包 `__init__.py`；在 `app/businesses/catalog.py` 补齐附录冲突双方来源诊断；按需要扩展既有 catalog/CLI/来源/架构测试，不新增发现树或注册器。

**Interfaces:**
- 导出 `ecs_command(task_type, *, device_role, annex_id, annex_version, result, error, timeout_min_ms, timeout_max_ms)`，装饰 BaseModel 参数类并返回原类。
- 导出 `compile_commands(*command_models) -> (tuple[CommandBinding, ...], dict[(annex_id, annex_version), DeviceAnnex])`，每次仅从显式参数纯转换。
- 内部共享 `CommandDeclaration` 为 frozen dataclass，包含 `model`（精确参数类）、`binding`（source 为原类）、`annex`（原模型对象）；`command_declaration(model)` 从精确类自身获取元数据并校验，不继承声明。供 Task 3 使用，不必从包级导出内部类型/函数。

- [x] 先写并运行公开装饰/编译入口测试：原类/模型身份和来源、严格字段及嵌套模型、非法 role/task/附录/timeout、严格性或 extra 不合格（含 CallbackPayload）、重复装饰/绑定/附录冲突、local 来源不得编入正式清单、未声明子类；失败不能污染后续合法编译。编译与全局校验的冲突诊断均保留双方来源和修复方向，不改变原冲突判定或异常类型。
- [x] 最小实现声明和编译。复用 registration_source/CommandBinding/DeviceAnnex/merge_annexes；检查嵌套严格性及显式放宽字段，不能为迁声明修改原模型。命令文件推荐按动作命名但不强制，不要求迁移 contracts.py。
- [x] 以隔离业务 composition 证明编译产物可进入现有 catalog/command_contract 目录，原 class/symbol/local_only 来源正确；独立检查不初始化设施，失败发生在资源/工厂前。保留手工/共享旧合同回归。
- [x] 运行 declaration、role/contracts、catalog、架构及 CLI 对应聚焦回归和 Ruff，报告测试证据与变更文件。暂不修改 create。

## Task 2: WMS 模型发送与可复用重验（阶段 3）

**Files:** 修改 `app/integrations/wms_request/service.py`、按需要修改 `operation.py`；新增 `app/integrations/model_payload.py`（仅承接 WMS/ECS 共同的模型重验/规范数据提取）；扩展 WMS `test_service.py` 及必要的来源用例，不新增平行注册表。

**Interfaces:**
- 保留 `WmsRequests.send(session, operation, data, *, operation_id)` 的位置/关键字调用；增加 `send(session, payload, *, operation_id)`。混用/缺失形态 TypeError；精确类型无注册 UnknownOperation，多注册 InvalidRequestPayload。
- 内部共享 `validated_model_payload(payload: BaseModel) -> dict`：在不修改类配置、不信任 model_validate(instance) 的前提下，重验实际字段/嵌套数据，再按原模型 JSON 序列化。供 Task 3 复用，不从公共包导出新业务 API。

- [x] 先写 RED：新旧形态两种先后顺序同身份得到同 message_id/原 bytes/TTL/target，别名/default/null/嵌套及合法序列化等价；异输入仍 DuplicateMessage，回滚无残留。
- [x] 覆盖精确类型、未注册子类、共享模型多 operation 歧义、混用/缺参，以及 model_construct、后续赋值、嵌套模型/列表非法值；失败无入队。注册来源/local 作用域沿现有规则，不凭类名/可写字符串属性授予权限。
- [x] 实现明确参数分派和现有注册表反查，共用原校验/冻结/入队路径；重验时保留模型真实字段，避免先调用序列化器掩盖非法值。别名含 validation_alias/嵌套须可重验，不能改变旧字符串路径。
- [x] 字段级 `Field(frozen=True)` 的 Pydantic dataclass 若无法在不丢失 init=False 实值及原校验器的前提下重验，新模型形态须在入队前明确拒绝并提示沿用旧调用；类级 frozen=True 继续支持。不得放宽验证或重写原合同。此为显式模型调用资格限制，不宣称所有合法旧模型已获等价迁移证明。
- [x] 标准 dataclass 重验保留父模型的实际编译合同（含继承严格性和字段显式放宽）。能确定直接字段或唯一有效 schema 时复用原 schema；同一运行时 dataclass 对应多个无法唯一定位的泛型合同等歧义形态须在序列化前拒绝并提示旧调用，不猜测分支、不退回独立宽松 adapter，也不建立通用 schema 路由框架。
- [x] 运行 WMS 整目录及注册/结果读取/独立 CLI 回归、Ruff。静态 target 改名/改址的既有限制不修复；存量动作仍读原 message_id，不迁成重新 send。

## Task 3: ECS 模型创建与声明角色门禁（阶段 4）

**Files:** 修改 `app/integrations/ecs_command/service.py`、`roles.py`、`dev.py`；扩展对应 service/role_contract_guards/dev/restart/result 测试。复用 Task 1/2 内部接口，不改 RouteSnapshot 字段或旧路由端口必选方法。

**Interfaces:**
- 保留旧 `create(session, *, owner, call_key, device_code, task_type, timeout_ms, params)`；增加 `create(session, *, owner, call_key, device_code, command, timeout_ms)`。互斥分派，混用 TypeError，未声明/非法模型 ValueError。
- `RoleRoutes.resolve_declared(session, device_code, declaration) -> RouteSnapshot`；同一次绑定解析核对 device/role/task_type/活动 annex id/version/params/result/error 模型及超时合同一致。
- `LocalEcsRoutes.resolve_declared` 仅显式委托正式路由；普通 provider/通用 dev 缺此能力拒绝新建，不回退 resolve。

- [x] 先写 RED：新旧形态按原规范输入同 owner/call_key 返回同命令；输入漂移、非法 timeout、变异模型及回滚无残留；兼容原位置 session 与关键字字段。
- [x] 原调用查询/比较先于当前路由解析；原行存在时不调用 resolve/resolve_declared，不重算期限。模型重验后共用原 input_raw、CommandEnvelope、入队路径；不增加 wire/冻结元数据。
- [x] 写角色门禁公开 create 用例：同动作同附录的不同角色不可串用，未启用/错误模型/不支持声明能力均拒绝且无新命令；已撤销配置不妨碍读取同输入原调用。
- [x] 验证 local 正式角色委托和一个 Sender；共享/宽松旧合同仍可旧 create，V1 命令在 V2/current 配置变更/重启后按历史附录接收迟到 Result，原 bytes/origin/身份/expires_at 不变，历史附录不授予新发送权限。
- [x] 运行 ECS 整目录、业务合同装配、来源/架构及独立 CLI 聚焦回归和 Ruff，报告 RED/GREEN。非技术故障不得转为业务成功或自动重发。

## Task 4: 现行文档、兼容证据与阶段 5–6 进入条件

**Files:** 更新 `docs/integrations/business-development-quickstart.md`、`ecs-command.md`、`registration.md`、`business-packages.md`、`workline-business-api.md` 及本计划中实际受影响内容；可新增一份阶段 2–4 验收/兼容说明。不得修改两份原有已暂存人工/粗分接口文档。

**Interfaces:** 消费 Task 1–3 已交付 API 和真实测试证据，不新增产品代码/正式业务/生成器。

- [x] 将现行指南的“不可用”列表逐项更新为实际能力，给出最短模型调用/显式编译示例，保留旧调用、共享/宽松合同不迁移、原身份恢复与新建角色门禁边界。设计稿可保留历史语境，但须有清晰实现状态指针，不能称整份设计已完成。
- [x] 对 A1–A6/A10 关联可执行证据；A7–A9 继续引用隔离业务基线，不能升级为真实业务完成。API/worker、CLI 及目录契约不改变权限或加载范围。
- [x] 读取人工/粗分参考及 catalog 当前源码，列出阶段 5 真实切片的具体未决合同/资源/恢复条件及阶段 6 的真实开发者指标缺口；条件不齐不猜测协议或填生产目录，记录不可进入的事实和责任边界。
- [x] 检查修改文档实际本地链接、示例语法及发布文档依赖闭包；报告未执行的真实供应商、现场、部署回退演练和开发者上手指标。

## 最终验收

控制器独立审查全分支新增内容（包含此前 worker 修复，排除用户两份原文档），运行任务专属 PostgreSQL/Redis 与两个迁移库的完整 pytest、Ruff、格式和差异检查；只在代码/测试改变后重跑相应验证。对本代理未写入的原文档/索引保存开始及最新快照；并行工作更新时保留其当前内容，不用开始快照覆盖。最终记录任务评审、综合评审、实际结果和阶段 5–6 阻断，不提交/推送。


## 本轮执行记录

- 当前主目录分支仍为 `feature/worker-contract-bootstrap`；全部实现均未暂存、未提交或推送。
- Task 1–4 已逐任务实施并完成独立规格/质量评审。Task 1 的嵌套 TypedDict/dataclass 配置绕过、Task 2 的继承严格性丢失均经 RED→GREEN 和限定修复复审关闭。
- 控制器修复前完整锁定依赖套件显式使用任务专属 PostgreSQL/Redis 及两个独立迁移库：**3256 collected，3228 passed、28 skipped，198.07 秒，退出 0**。28 项仅为 SQLite 无法表达的 PostgreSQL 并发/行锁/独立连接分支，对应 PostgreSQL 用例已执行；不累计各专项重叠数量。
- Ruff、442 文件格式及差异检查通过；Task 4 文档检查为 129 个本地链接/锚点、19 段 Python 语法、56 文件发布闭包。
- 新模型形态的字段级 frozen dataclass 和无法唯一定位的泛型 dataclass 上下文按记录明确拒绝，继续保留旧调用；未宣称所有旧模型已获等价迁移证明。ECS 路由/服务合同一致性只约束新模型动作，不额外收紧旧装配。
- 原有两份接口文档及 `.gitignore` 在本轮期间出现并行修改，文档暂存内容亦由并行工作更新；本代理及子代理均未编辑、暂存或回退这些内容。开始快照及较新快照保留，最终以当前用户内容为准，不用旧快照覆盖。
- 全分支综合评审发现的别名交叉值交换、嵌套 RootModel 内部异常，以及 catalog 重复绑定修复提示，已在单轮统一修复中经 RED→GREEN 关闭；限定复审确认三项均已解决，无新遗留。
- 统一修复后最终全仓复验：**3271 collected，3243 passed、28 skipped，200.77 秒，退出 0**；所有跳过为已有 SQLite 条件分支，对应 PostgreSQL 变体已执行。最终 Ruff、443 文件格式（含文档代码块）及差异检查通过；文档格式修改前后 Python AST 相同。
- 阶段 5–6 缺真实合同/接缝及开发者实测，未注册真实业务、未连接真实 WMS/ECS/RCS/设备、未部署或做现场验收。
