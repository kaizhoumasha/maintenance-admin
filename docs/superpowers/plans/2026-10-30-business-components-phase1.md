# 阶段一：业务多能力组合、唯一注册与分派 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans；2026-10-30 按 executing-plans 原位串行实施，经追加授权复审后已提交 90861b7；当前按 ship 准备阶段一、二联合 PR，不部署，原共享目录及其外部修改保留。Steps use checkbox (`- [ ]`) syntax for tracking。

**Goal:** 业务可组合多个内部能力，共享合同唯一注册，新输入准确分派到本业务唯一处理目标。

**Architecture:** catalog 汇总 COMPONENTS 并编译业务资源、合同消费者和分派元数据；BusinessRuntime 仍只有业务。共享模块只导入一次，组件由原业务工厂创建并由原业务 scan_pending 调用。

**Tech Stack:** 现有 Python/FastAPI/SQLAlchemy/Celery/pytest；无数据库迁移或新依赖。

**Spec:** [组件框架合同](../../integrations/business-packages.md)、[当前 BIN_Roller 边界](../specs/2026-10-30-bin-roller-capability-design.md)、[总计划](2026-10-30-business-components-bin-roller.md)。

**参考：** [业务组合与原归属指南](../../integrations/business-packages.md)、[阶段一计划](2026-10-30-business-components-phase1.md)、[阶段二验收](../../testing/business-components-phase2.md)。

## Global Constraints

- 本阶段不新增正式 bin_return/SMT 业务、组件 owner、组件任务或占位合同。
- 保持 build_service(capabilities)、PROGRESSION="scan_pending" 和 business.advance_pending(business_code)；根业务清单仍显式。
- 同业务每输入键唯一目标；真正第二次注册同键仍失败；不可把“相同名称忽略”当去重。

## Review Focus

- _components 路径不能绕过 registration_filename、formal/local 和 late registration——P1.1/P1.2 测试。
- 同一共享命令不能让未声明它的业务或他线业务执行——P1.2 的公共 create 测试。
- 部分组件配置缺项不误挡合法 SCAN3 本点，原业务资源校验不放宽——P1.3 测试。
- 同事件重报/收据清理不因业务分派改动改变原身份——P1.4 测试。
- 两业务/多组件仍只有业务级服务和 Beat、待办分页有界公平——P1.3/P1.4 测试。

---

## 本阶段共享接口

在 app/businesses/components.py 定义不可变 ComponentDefinition：key:str、device_roles/position_slots/initial_entries 沿原定义类型的 tuple、entry_modules/model_modules:tuple[str,...]、commands:tuple[type,...]、required_methods:tuple[str,...]、requires_progression:bool=False；无业务码、DEFINITION 或组件 PROGRESSION。描述类只依赖标准库与原 definitions，Pydantic/ORM 类仅作为 composition 传入对象。

ContractKey 使用 (kind, name, device_role_or_none)：kind 为 ecs_event/wms_callback/wms_request/ecs_command；事件 name=event_type，命令 name=task_type。InputTarget(component_key:str|None, source:RegistrationSource) 中 None 表示本业务原入口。catalog.effective_definition(business_code)->BusinessDefinition、catalog.contract_consumers(key)->frozenset[str]、catalog.input_target(business_code,key)->InputTarget|None；这些读取仅使用已校验不可变元数据，不查询业务表。

## Task P1.1：组件声明、有效资源与真实来源

**范围/Files：** 新增 app/businesses/components.py；修改 app/businesses/catalog.py、app/integrations/registration.py、app/integrations/discovery.py；测试修改 tests/businesses/test_catalog.py、test_definitions.py、test_model_loading.py、tests/integrations/test_registration.py、test_registration_discovery.py、test_registration_bootstrap.py。共用测试 fixture 放 tests/businesses/conftest.py，不导入其他 test 文件。

**依赖/Interfaces：** 输入原 BusinessDefinition/composition；输出本阶段共享接口及 COMPONENTS 可选 tuple，validated_entries()/progressions() 仍只有业务。

- [x] **Step 1：增加失败行为测试。** test_shared_component_is_loaded_once_for_two_businesses 断言同源码只注册/加载一次、两个业务都有有效资源；test_component_conflicts_fail_before_runtime 断言重复 key、嵌套组件、同资源异声明、未知模块和同业务同键双认领全部失败；test_shared_source_keeps_registration_guards 覆盖文件名不符、local 首次误注册和启动后迟注册。
- [x] **Step 2：运行 RED。** `rtk proxy uv run pytest tests/businesses/test_catalog.py tests/businesses/test_definitions.py tests/businesses/test_model_loading.py tests/integrations/test_registration.py tests/integrations/test_registration_discovery.py tests/integrations/test_registration_bootstrap.py -q`；新行为须因现有机制缺失失败，不能是 fixture 导入错误。
- [x] **Step 3：实现声明及编译。** 合并同键相同资源，路径只接受业务自有或显式 _components.<key> 下模块；模块列表去重但装饰器原重复声明错误保留。已校验快照包含 COMPONENTS 元数据，重新校验失败使运行时不可构建；definitions/get_definition 返回业务有效定义。
- [x] **Step 4：重跑同一专项 GREEN。** 验收包括未引用组件不加载、无组件粗分定义/模型加载不变、测试组件未进入 catalog.entries()。更新 docs/integrations/business-packages.md 的目标/当前状态区分，不改历史记录。

**完成门槛：** 纯声明可编译、来源可追溯、无组件运行主体；不依赖新数据库表。

## Task P1.2：共享命令编译与新动作授权

**范围/Files：** 修改 app/businesses/catalog.py、app/integrations/ecs_command/service.py、runtime.py、app/services/device_routes.py、app/main.py、app/tasks/business_tasks.py；测试 tests/businesses/test_contract_assembly.py、tests/integrations/ecs_command/test_model_create.py、test_role_contract_guards.py、test_result.py。

**依赖/Interfaces：** 消费 P1.1 ContractKey/contract_consumers；EcsCommands 增可选 authorize_new(session, owner:CommandOwner, device_code:str, command_model:type)->Awaitable[None] 注入端口，默认 None。app.services.device_routes.authorize_new_command 实现该签名，在原绑定锁内检查本业务合同使用关系和实际设备所属线业务；configure_ecs 和 worker _runtime 透传 authorize_new；通用命令库不导入 catalog/业务表。create/get_command 的业务调用形式不变。

- [x] **Step 1：写失败测试。** test_shared_command_compiles_once_with_business_consumers 断言两个业务共用同一源模型只编译一份；不同源模型/附录/超时同键失败。test_new_command_requires_original_business_and_line 拒绝未声明业务和他线 owner；test_existing_command_replay_skips_new_authorization 原动作配置变化后仍取得原命令，旧 Result 按原附录消费。
- [x] **Step 2：运行 RED。** `rtk proxy uv run pytest tests/businesses/test_contract_assembly.py tests/integrations/ecs_command/test_model_create.py tests/integrations/ecs_command/test_role_contract_guards.py tests/integrations/ecs_command/test_result.py -q`。
- [x] **Step 3：最小实现。** 以源模型和完整严格合同去重，保存每业务角色交集。authorize_new 仅放在 _enqueue 已查原身份后的新 resolve_route 路径，不能在旧重放前执行；API/worker 正式装配透传端口，debug/local 原路径保持边界。
- [x] **Step 4：同专项 GREEN。** 再断言 source、CommandOwner、冻结 command_code/参数/地址/附录及 retry window 不因共享而改变；同键同模型去重不容许第二个真实装饰声明。

**完成门槛：** 共享的是命令合同，owner 仍为原业务；原动作重放和迟到结果不受新动作授权误伤。

## Task P1.3：业务工厂组合、资源子集及统一扫描

**范围/Files：** 修改 app/businesses/runtime.py、app/controllers/businesses.py、app/services/workline_service.py、app/main.py；按需适配 app/tasks/business_tasks.py，不新增任务；测试 tests/businesses/test_runtime.py、test_progression.py、tests/controllers/test_workline_business.py、tests/services/test_workline_business.py、tests/tasks/test_business_tasks.py。

**依赖/Interfaces：** P1.1/P1.2 有效定义和命令消费者。服务的 components:Mapping[str,object] 与声明 key 一致；build_service(capabilities) 不变；新增 require_component(ctx,key:str)->object 校验可信上下文及本业务映射。business_configuration_issue 原签名增加 required_device_roles:tuple[str,...]|None=None、required_position_slots:tuple[str,...]|None=None，默认 None 保持原业务完整校验，固定子集只能来自受信调用方。

- [x] **Step 1：写失败测试。** test_business_composes_two_components_under_one_progression 断言两内部对象、一个业务服务/任务/Beat，scan_pending 两分支都有机会且有固定上界；缺对象/必要方法/业务异步入口启动失败。test_point_resources_do_not_require_upstream_roles 验证只缺 SCAN1/2 时 SCAN3 可执行，而 SCAN3 缺失、他线绑定、未知子集和业务不匹配拒绝。
- [x] **Step 2：运行 RED。** `rtk proxy uv run pytest tests/businesses/test_runtime.py tests/businesses/test_progression.py tests/controllers/test_workline_business.py tests/services/test_workline_business.py tests/tasks/test_business_tasks.py -q`。
- [x] **Step 3：实现工厂/资源适配。** 工厂原位 Python 组合，目录/工作线只看到业务；编译元数据校验 components 和方法，保留原工厂无参 progression 校验。业务扫描分别持久分页，不构造长事务或组件调度器；配置保存使用有效定义，执行固定必要子集。
- [x] **Step 4：专项 GREEN。** 验证 KT16/KT18/KT20 自有绑定和无组件业务原用法、配置缺项仍能保留原结果；只扩展资源行为，不切换/清空工作线业务。

**完成门槛：** 多组件统一业务推进可运行；本点资源校验满足 C02，尚无生产 bin_return。

## Task P1.4：新输入唯一分派与阶段装配验收

**范围/Files：** 修改 app/integrations/ecs_callback/binding.py、registry.py、receiver.py、app/services/device_bindings.py、app/business_wakeups.py、app/controllers/ecs_callback.py、app/main.py；WMS 回调上下文/receiver 按本任务受限接口扩展。测试 tests/integrations/ecs_callback/test_receiver.py、test_roles.py、tests/businesses/test_entry_context.py、test_entry_transactions.py、test_workline_business_flow.py、tests/controllers/test_ecs_callback.py、tests/test_transactions.py；新增 tests/businesses/test_component_dispatch.py。

**依赖/Interfaces：** P1.1 input_target、P1.3 require_component；DeviceBinding 增 business_code:str|None=None 快照且不改变原 revision 摘要；EcsEventContext 增受信 business_code/component_key。新 notify_business_committed(business_code:str)->None 复用 enqueue_business；原 event_committed(source) 保留给业务专属来源，接收器共享路径使用解析后的业务码。共享 WMS callback 可由可信单次 bind_callback_business(ctx,business_code) 保存原关联并核对消费者；payload 不能直接指定 owner。

- [x] **Step 1：写失败测试。** test_one_event_dispatches_only_to_its_workline_business 通过真实 receiver 给两个业务/三条线交错输入同键，断言只一份原事实/待办及本业务通知；未声明业务留诊断不执行。test_original_receipt_is_replayed_before_new_dispatch 验证重报沿原响应和摘要。test_shared_callback_requires_unique_original_business 验证可信原关联一次绑定、重复/矛盾/缺失归属不能广播或覆盖。
- [x] **Step 2：运行 RED。** `rtk proxy uv run pytest tests/businesses/test_component_dispatch.py tests/integrations/ecs_callback tests/businesses/test_entry_context.py tests/businesses/test_entry_transactions.py tests/controllers/test_ecs_callback.py tests/test_transactions.py -q`。
- [x] **Step 3：实现 receiver 接缝。** 原查重顺序、事件摘要、ACK/诊断规则保持，绑定锁内取得业务；验证消费者/唯一目标后共享适配器只调用原业务内对象。共享 callback 仅支持受信原关联解析，无该合同不启用；通知由通用接收端口 after_commit 登记，组件不显式发布。分派解析由应用注入 resolve_input_target=catalog.input_target，通用 receiver 不导入 catalog，沿用原分层约束。
- [x] **Step 4：专项及阶段 GREEN。** 重跑上列用例，加 `rtk proxy uv run pytest tests/businesses tests/integrations/test_registration.py tests/integrations/test_registration_discovery.py tests/integrations/test_registration_bootstrap.py tests/controllers/test_workline_business.py tests/tasks/test_business_tasks.py -q`；确认 formal/local/debug、回滚/close、发布失败仍 ACK、重复通知及原粗分回归。
- [x] **Step 5：阶段收尾。** 按总计划串行静态检查和全量 `rtk proxy uv run pytest -q`；记录隔离 PostgreSQL/Redis 的实际运行或未验收项。同步注册/业务指南，生产 catalog 不增加测试组件/业务。实施时按原授权未提交，追加复审后已单独提交 90861b7。

**阶段出口：** 只有业务级运行主体；组件合同唯一定义、消费者与分派明确，可交付阶段二接口。共享 WMS 出站结果精准即时通知不属于本阶段出口，已由阶段二 P2.1/P2.2 交付。

## 实施与验收记录（2026-10-30，阶段一完成）

阶段一代码已实现，全部新业务/组件样例仅在测试夹具中；生产 catalog 仍只有 coarse_sorting。
无新依赖、迁移、生产 _components 包、业务码或任务。原受理身份、冻结意图、原 Result、Outbox 和 Beat 保持。

实现调整：用单个冻结 catalog 快照原子发布校验元数据；按每个业务显式入口模块编译消费者，不能仅匹配命名空间。
ECS 通用接收器通过应用注入的 resolve_input_target 端口解析目标，避免反向依赖业务层。
旧 non-async progression 校验用例在修改声明后先重新校验 catalog，再验证服务方法，符合新元数据冻结约束。

最终隔离 PostgreSQL/Redis 全量：**3697 passed、39 skipped，292.22 秒，exit 0**。Ruff check、501 文件格式检查、git diff --check 通过；Alembic 唯一 head 为 ea8b20261029。详细范围、跳过说明及证据见[阶段一验收记录](../../testing/business-components-phase1.md)。

独立只读审查发现 dev 命令来源和本地入口分类两项问题，均先复现再修正；对应专项 73 passed、13 条件跳过，最终全量包括两项修复的 PostgreSQL 形态。实际 HTTP→原业务通知→真实任务体→内部对象链路通过，通知发布边界在测试中截获，不冒充独立 broker/worker 进程验收。

原有暂存文档保持；实施期间共享工作区由其他操作推进 HEAD a7f607d→bac02a6（PR #40，重启测试和验收文档），
并有其他文档并行更新；本轮未执行暂存或提交，不回退这些外部变化。发现共享工作区被切回 develop 后，按仓库规则在同一 HEAD 创建 feature/business-components-phase1，全部文件与暂存条目保留，逻辑暂存快照核对一致。

追加复审记录：用户随后授权阶段一复审优化并单独提交。独立审查无 Important/Critical，删除两处 catalog 冗余分支；
复测 3697 passed / 39 skipped（298.72 秒），提交 90861b784917190047e0dddeae335ea22bcd571e，仅包含阶段一代码、测试及独立验收记录。
原 7 份暂存文档未带入提交且 blob 逐项一致；当时阶段二按未提交、不部署推进，随后已另获复审提交授权并提交 a34b28d。

## 按计划验收复核与当前进度（2026-10-30）

结论：**P1.1～P1.4 全部通过隔离验收，代码已提交 90861b7**。当前分支已接续阶段二提交 a34b28d。

| 任务 | 复核的完成门槛 | 具体行为测试索引 |
|---|---|---|
| P1.1 | 不可变声明、有效资源/真实来源、模块唯一加载，冲突或迟注册拒绝 | test_components.py：shared_component_resources_and_modules_are_merged_once、invalid_component_composition_fails_before_runtime、shared_source_still_enforces_filename_and_formal_scope |
| P1.2 | 共享命令只编译一次；新动作核对消费者/实际线，原身份重放绕过新授权 | test_components.py：shared_command_compiles_once_for_the_same_role；test_model_create.py：new_authorization_is_enforced_but_original_replay_keeps_identity；test_workline_business.py：new_command_requires_declared_contract_and_own_workline |
| P1.3 | 工厂组合多内部对象但只有业务服务/推进；资源子集不放宽业务/绑定门禁 | test_components.py：business_composes_multiple_internal_components；test_workline_business.py：operation_resource_subset_preserves_business_and_binding_guards |
| P1.4 | 按工作线唯一分派、原收据/业务去重、可信 WMS 回调绑定及 HTTP→真实任务体链路 | test_component_dispatch.py：one_event_dispatches_only_to_its_workline_business、original_receipt_precedes_dispatch_and_business_dedupe_survives_cleanup、shared_callback_requires_unique_original_business、http_input_wakes_business_and_real_task_body_advances_its_component |

上述测试函数均以 test_ 为前缀，文件位于 tests/businesses、tests/services 或 tests/integrations/ecs_command。
P1.3 的有界公平覆盖是业务扫描分别给各内部能力固定预算的隔离样例；平台不另造组件调度器，未来消费业务仍须验收其自身扫描预算。

阶段一提交前全量 3697 passed / 39 skipped；后续阶段二提交前联合全量 3791 passed / 36 skipped，补齐原来未配置的三套既有迁移回归库。阶段一无新增迁移；当前项目唯一 head 已随阶段二变为 f0b120261030，不能继续把 ea8b20261029 写成当前 head。

前次进度复核已核对提交、源码未漂移、逐步 RED/GREEN/全量日志及测试实现；本次 ship 另行验证并提交相关文档。部署/现场、生产 bin_return/SMT 接入仍未验收。
