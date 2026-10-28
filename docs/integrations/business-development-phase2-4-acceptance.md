# 业务开发阶段 2–4：能力、兼容证据与后续进入条件

日期：2026-10-28。本文记录当前工作区实现及可执行证据，不表示已提交、合并、部署或真实业务验收。
[源设计](../superpowers/specs/2026-10-27-business-development-simplification-design.md)的历史文本继续保留；
现行用法以[快速开始](business-development-quickstart.md)、[注册](registration.md)、
[业务包](business-packages.md)和[ECS 命令](ecs-command.md)为准。

## 已交付能力及资格

| 能力 | 当前边界 |
| --- | --- |
| ECS 声明/编译 | 包导出 `ecs_command`、`compile_commands`；装饰器保留原精确类，编译只处理显式清单，输出原 `CommandBinding/DeviceAnnex` 类型；无全局命令注册表/自动发现树 |
| WMS 模型调用 | `await wms.send(session, Payload(...), operation_id=saved_id)`；精确注册类型须唯一对应 operation，拒绝未注册子类及歧义；无 `payload=` 参数，旧字符串/字典位置与关键字调用保留 |
| ECS 模型调用 | `await ecs.create(session, owner=owner, call_key=saved_key, device_code=device_code, timeout_ms=timeout_ms, command=Payload(...))`；与旧 task_type/params 互斥，共用原命令/Outbox/事务 |
| 新动作角色门禁 | `RoleRoutes.resolve_declared` 一次绑定解析核对设备、精确角色、动作、活动附录键及原模型/范围；服务独立检查附录；local 组合显式委托正式角色，普通/通用调试路由无能力时拒绝新建 |
| 已有动作 | 按原 owner/call_key 和原输入优先返回原命令，不先要求当前路由能力；输入漂移仍拒绝。WMS 存量动作保存原 message_id/operation/operation_id 读取，不以当前 target 重调发送 |
| 加载/检查 | API/worker 共用显式 catalog，正式入口及合同先于 local/资源/工厂；沿用无配置 CLI 和原 local HTTP 目录的范围/权限；正式 catalog 仍为空 |

模型路径先按原合同上下文重验实际字段，再沿原模型 JSON 序列化，不修改原实例来消除非法值。
以下是**可选模型调用的资格限制**，不是强制旧合同升级：

- Pydantic dataclass 含字段级 `Field(frozen=True)` 时明确拒绝模型调用；类级 `@dataclass(frozen=True)` 可用。
- 泛型 dataclass 的原上下文须可唯一定位；直接字段可区分的 `Box[int]` / `Box[str]` 可用，同运行时类
  在容器内有多个无法唯一选择的 specialization，或原上下文无法解析时，明确拒绝并提示旧调用。
- WMS 继续原 operation/data，ECS 继续原 task_type/params；不改写 schema、不修改原类校验配置、不绕过验证。
- ECS 声明要求单参数类单角色/动作、原 params/result/error 及嵌套合同严格且禁止扩展字段、错误继承
  `ErrorDetail`、超时明确。共享多绑定和 `extra="ignore"` 原合同保持手工声明/旧 create。
- 声明资格不等于 wire 等价。别名、默认值、自定义校验器/序列化器须逐合同验证；新 JSON 形状须仍能通过
  未改动的旧 params 合同，且与旧调用原输入一致。不能靠改旧规范化、复制模型或人为增版制造等价。

未增加表/迁移、RouteSnapshot 字段或 wire 字段；新命令继续 300 秒投递窗口，历史期限不重算。
历史附录只保留原 Result 解析，不授予新发送权限。UNKNOWN/HELD、收据、Evidence、原结果与消费依据保持。
没有 `@ecs_result`、通用业务 worker/execute、第二 Sender 或脚手架生成器。

## A1–A10 与可执行证据

下表定位公开行为测试，测试函数名可交给 pytest `-k`。A7–A9 仅为既有隔离业务基线，不能升级为人工/粗分
真实业务完成；即便使用真实 PostgreSQL，也只证明本方隔离软件行为。

| 标准 | 可执行位置与行为 | 当前结论 |
| --- | --- | --- |
| A1 合同/来源 | [test_declaration.py](../../tests/integrations/ecs_command/test_declaration.py)：`test_declaration_preserves_original_models_and_source`、`test_compile_conflicts_name_both_original_sources_and_do_not_accumulate`、`test_undeclared_subclass_does_not_inherit_a_declaration`；同文件嵌套严格性/local 来源测试 | 声明/编译机制有证据，保留模型身份与来源，不证明真实合同已确认 |
| A2 发现/隔离 | [bootstrap](../../tests/integrations/test_registration_bootstrap.py)（含 `test_invalid_formal_business_prevents_worker_initialization`）、[discovery](../../tests/integrations/test_registration_discovery.py)、[CLI](../../tests/scripts/test_check_integration_contracts.py)、[HTTP 目录](../../tests/controllers/test_dev_registry.py)；正式先于 local、缺项/冲突失败、原来源与编译合同单项展示 | 沿用清单、顺序、目录范围与权限；检查成功不证明任意未加载源码或运行资源 |
| A3 WMS 等价 | [test_service.py](../../tests/integrations/wms_request/test_service.py)：`test_model_and_string_sends_share_the_original_frozen_message`、`test_model_send_requires_one_exact_registered_type`、`test_model_send_revalidates_actual_fields_before_serializing`、`test_model_send_rollback_leaves_no_message`；同文件 dataclass/别名/默认值/null/嵌套序列化测试 | 固定 target/相容配置及合资格上下文下等价；字段 frozen/歧义泛型明确保留旧调用，不声称任意 dataclass 可迁 |
| A4 ECS 等价 | [test_model_create.py](../../tests/integrations/ecs_command/test_model_create.py)：`test_model_and_legacy_calls_share_original_bytes_and_deadline`、`test_model_creation_rolls_back_with_caller_transaction`、`test_shared_manual_bindings_keep_legacy_creation`；[test_result.py](../../tests/integrations/ecs_command/test_result.py)：`test_shared_legacy_models_keep_original_result_contract` | 原输入相同时原命令/字节/路由/期限一致；旧共享/宽松合同继续有效，原 params JSON 接受性仍须逐合同验证 |
| A5 角色门禁 | 同上 `test_model_creation_requires_exact_active_role_and_contract`、`test_model_creation_requires_explicit_declared_route_capability`、`test_local_model_and_debug_commands_use_one_sender` | 同动作同附录仍核对精确角色，缺能力不降级；拒绝无新义务 |
| A6 历史恢复 | 同上 `test_restart_keeps_original_result_contract_after_current_route_changes`、`test_existing_model_call_bypasses_current_declared_capability`；[contract assembly](../../tests/businesses/test_contract_assembly.py)：`test_history_annex_does_not_grant_new_role_command_permission` | V1 原结果在活动 V2/改址/换绑/撤销后仍按原合同处理；重复/冲突/先于 ACK 与原冻结内容有隔离证据 |
| A7 原子受理/消费 | [test_result_consumption.py](../../tests/businesses/test_result_consumption.py)：`test_acceptance_receipt_pending_and_state_roll_back_together`、`test_business_runtime_consumption_recovery`、`test_result_receive_must_commit_before_independent_advance` | 隔离业务的收据/受理/待办和消费/下一意图原子基线；没有真实业务验收 |
| A8 快照/恢复 | 同上 `test_runtime_snapshot_keeps_full_101_member_versions`、`test_serialization_failure_reopens_and_rereads_complete_business_group`、`test_serialization_after_intent_rolls_back_and_rechecks_all_results`；[predecessors](../../tests/integrations/wms_transport/test_predecessors.py) 的新 UNKNOWN 不被历史成员覆盖 | 独立 PostgreSQL 场景覆盖完整版本/成员、分批快照、40001 新 session 重读及耗尽待办；不是现场恢复结论 |
| A9 完成边界 | [WMS results](../../tests/integrations/wms_request/test_results.py)：`test_delivery_terminal_is_not_business_completion`；[Transport results](../../tests/integrations/wms_transport/test_results.py)：`test_transport_result_uses_applied_outcome_only`、`test_transport_closure_matrix`；消费测试 `test_wms_consumption_survives_cleanup_only_with_saved_business_evidence` | 隔离基线区分 ACK/DONE/RECORDED/PREPARE_ACCEPTED/CLOSED 与业务结果；缺证据不授予成功或重发 |
| A10 回退/上手 | A3/A4/A6 的新旧调用、原输入/冻结内容、V1 历史 Result 和旧调用恢复；[两线隔离业务](../../tests/businesses/test_workline_business_flow.py) | 具备平台兼容证据；未做候选发布版本的实际部署回退、真实业务开发者上手或另一触发业务验收 |

本轮任务报告的覆盖运行分别记录：Task 2 相关套件 339 通过；Task 3 配置任务专属可丢弃 PostgreSQL/Redis 的
ECS 目录 573 通过、20 条既有条件跳过，相关套件 156 通过，新模型创建专项 88 通过。各运行存在重叠，不能相加。
控制器修复前全仓运行 `uv run --frozen --no-sync pytest -ra`，显式配置任务拥有的
`TEST_DATABASE_URL` / `TEST_REDIS_URL` 及独立的 `DEVICE_MIGRATION_DATABASE_URL` /
`BINDING_MIGRATION_DATABASE_URL`：3256 条收集，3228 通过、28 跳过，198.07 秒，退出 0。
28 条均为仅 PostgreSQL 支持的并发/行锁/独立 Sender 语义对应 SQLite 变体，PostgreSQL 版本已执行；
声明、模型创建、重验和事务恢复相关用例无此类缺失。全仓 Ruff check 与 format check 通过（442 个文件）。
以上是本地控制器隔离验证，不是 CI、发布或现场验收；跳过不计为通过。
综合审查及原暂存补丁/文档保护由控制器汇总。最终修复已关闭 I1、I2 和 M1：实际字段按字段名重验时关闭别名查找，
WMS 嵌套 RootModel 递归重验原 root 合同并保留原序列化，catalog 全局重复绑定诊断补齐单角色/任务只声明一次的修复方向。
公开 WMS/ECS 用例覆盖重叠/互指别名的新旧同身份引用及原字节、实例保持；WMS RootModel 用例覆盖原 wire、
构造/赋值非法值、原校验器、缺失 root 与循环在入队前拒绝。前述全仓运行是修复前的真实记录，不与后续结果累加。

控制器在统一修复后以相同锁定依赖命令、任务专属 PostgreSQL/Redis 及两个独立迁移库重新执行全仓：
**3271 条收集，3243 通过、28 跳过，200.77 秒，退出 0**。28 项仍仅为 SQLite 条件分支，
对应 PostgreSQL 变体已执行；新增别名交叉、RootModel 和诊断回归均执行。最终全分支限定复审确认
I1/I2/M1 全部关闭，无新增 Critical/Important/Minor；此结论覆盖本地源代码和隔离测试，不代表合并、CI、部署或现场验收。
最终 Ruff 检查和全仓格式检查通过（443 个受检文件，含文档代码块）；文档格式调整前后 Python AST 一致。

## 兼容迁移与回退

迁移前保存原 operation/目标配置/TTL、角色/附录/模型/超时及消费方清单，先做逐调用等价证明，再迁合资格新动作。
旧动作继续按保存的原引用读取/恢复，不能为语法迁移改变 operation_id、owner/call_key、目标、附录、截止时间或状态。
同 WMS target 去重不提供跨 target/地址快照恢复；配置变化前须另核对存量义务。

没有持久格式变化，不表示任意旧版本都可回退。候选版本必须同时兼容业务代码导入、API/worker 的活动绑定、
仍可能收到 Result 的历史附录及业务消费模型；新语法调用方不能先于支持它的平台发布。回退时一起选择相容平台和
业务代码，保留原消息、证据、未决义务和期限。候选版本恢复演练及部署回退尚未执行。

## 阶段 5：当前不可进入真实切片

[catalog 源码](../../app/businesses/catalog.py)的 `entries()` 仍返回 `()`。隔离夹具不是生产包；
未选定且获准的完整真实业务，不补占位业务、生产模型、handler 或正式注册。
阶段 5 按合同成熟度选最小闭环，不预定人工或粗分先后；业务及 WMS/ECS/RCS 责任方须先关闭该切片相关阻断项。
下表整理本轮评审发现的合同缺口；两份业务参考的工作区补充另行保留，不包含在本轮实现提交中，
不能将这些待确认条件视为对端已确认合同。

| 候选/责任方 | 仍需确定的具体条件 |
| --- | --- |
| 人工：WES/WMS | [人工参考](manual-picking-api.md) P01：SCAN1 可信事实受理/应用及冲突处理口，精确匹配唯一冻结入站成员、原目标和事件时间，全成员齐备才确认整笔，不制造 WMS outcome_revision；P02：Return 本地交接的可信 ACCEPTED 接纳/公开结果投影，本地交接与物理取走分开，未知/冲突保留原义务和直接前序。平台读接口未代替这些业务应用合同 |
| 人工：WES/ECS/WMS/RCS | P03：正式角色、架型/工作位/位置绑定及配置校验，各命令/版本 params/result/error/timeout 附录；P04：CTU01/02/03、F01、CK04 等模板/现场支持、目标点位和面向。不以旧实现空 params 或设备 Status 代替确认 |
| 粗分：WMS，P01–P02/P10/P12 | [粗分草案](coarse-sorting-api-draft.md)：三项专用 operation 正式名称/字段/限额/原因码/HTTP-code/时间鉴权；GRN/行唯一关联、重放及 NG 副作用；完成报告去重/冲突及 RECORDED/DUPLICATE 边界；分配联合、工作点位/当前架/新架和 binding_id 定位 |
| 粗分：ECS/WMS，P03–P08 | 真实箱码与箱位/箱型一致；检测 NG 与原因透传、NG 是否另报及闭合；无新架/不能绑定响应、并发补换架稳定方案及高度有效性；barcode business_key 长度/字符及命令实物对应；重传与同码真实重入 passage 身份、原 timestamp/scan_time/正文稳定性；三种动作严格参数/结果/错误/超时、SUCCESS/PUT_FINISHED 完成范围和尺寸单位精度 |
| 粗分：ECS/WES/WMS/RCS，P09/P11/P13–P15 | 命令持久接纳、同号去重/冲突、跨重启及结果保留/重报，FAILED/未知后的核对纠正；正式角色/A-B 单元绑定、来源认证与转发端点；旧架可交出及未结入箱/报告义务；Transport 模板、真实点位/面向、门禁互锁和已应用结果与 WMS 在位更新衔接；失败/取消后重新决定、物理位置依据和并发去重，UNKNOWN 保留原身份 |

合同齐备后，业务实施计划还须交付真实模型/迁移、薄受理、独立 RR 推进、具体持久待办发现与恢复、资源和许可门禁，
覆盖两线、重复/乱序、丢唤醒/崩溃、消费与下一意图原子及失败/未知路径。该业务独立 PostgreSQL 证据、
本方闭环、真实供应商/设备/现场验收分别记录；平台测试、HTTP ACK、窗口释放均不替代物理事实。

## 阶段 6：开发体验仍待实测

首个真实切片尚未交付，另一种单据/设备触发方式也未验收。没有真实开发者完成时间、阅读内核文件数、跨层修改数、
求助次数或误注册/错误消费位置记录，不承诺时间、代码量或比例收益。
后续实际开发须记录这些指标，并验证无需修改控制器/Sender/可靠层、无需直读框架表且原身份/状态可定位。
只有两个真实触发场景的证据显示稳定重复后，再判断是否提取工具；本轮没有自动授权新抽象或新业务。

未执行真实 WMS/ECS/RCS 请求、设备物理动作、供应商持久性/断电验收、部署或部署回退演练；阶段 5–6 保持阻断。
