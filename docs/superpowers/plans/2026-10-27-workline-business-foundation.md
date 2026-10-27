# 工作线业务公共接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> 用户现已授权按SDD连续实施Tasks 1–5及Task 6已具备条件的公共接缝验证；Task 0已完成。真实manual-picking／粗分业务不在范围；不连接真实WMS／ECS，不提交、推送或部署，保留原暂存内容。

**Goal:** 补齐出站结果消费读契约和最小查询接口，并为已明确合同的业务子包提供集中声明、显式清单、简单工厂、工作线业务选择及现有入口接线。

**Architecture:** 前置Task 0确定出站结果消费契约与最小查询接口，禁止业务直读平台表。保留设计确认的 definitions／catalog／runtime 和业务内部 definition／composition／contracts／command_contracts／services／models 划分。业务承担编排和状态，平台继续承担协议、收据、可靠投递和执行证据；不建设插件框架。ECS调试参数清理和纯配置可独立推进，业务消费平台能力及真实流程以Task 0为前提，用测试侧业务验证；真实业务接入须另有确认合同及自己的实施计划。

**Tech Stack:** Python 3.13+、FastAPI、Pydantic、SQLAlchemy、Alembic、pytest／pytest-asyncio／HTTPX；沿用现有 PostgreSQL、Redis、Celery 与 Outbox，不增加依赖。

**Spec:** [业务子包与工作线对接设计](../specs/2026-10-27-workline-business-packages-design.md)，尤其 §6–11、§14–17；目录选择 D1/A，以及用户决定“可以清理 is_debug 参数，由 ECS 自行决定 debug 状态”。

## Global Constraints

- 基线：`5e3383061532bd123d57e48c2451b4ac35d65959`；执行前重新核对 develop 与相关接口，不能把本计划视为后续代码的自动适配。
- 业务定义只使用标准库，不导入 ORM、Pydantic、运行服务或网络；角色／位置 key 使用现有大写格式、最长100字符。
- 不使用业务基类、通用 execute、动态用户输入 import、在线插件、Saga 引擎或第二个 Sender。
- 新工作线必须明确选择已部署业务；旧行保留 null；业务只能首次指定，首期不切换或清空已选业务。
- 管理 API 仍要求管理员及相应 asset:workline:*；业务选项复用 asset:workline:read，不新增权限体系。
- 调试状态属于 ECS；WES 清理 is_debug；历史身份不重算、不合并、不删除，升级不回放旧事件。
- Task 0先于业务结果消费接线和实际编排；业务不得直读ReliableMessage／TransportTask或解析response_raw。CLOSED不是成功或执行许可；NOT_FOUND／UNKNOWN不新身份重发。
- 受理事务只保存原收据、受理状态及持久待推进依据（无需结果消费的初始意图按业务合同）；需要统一快照的结果推进由独立业务入口创建新session，在首条SQL前配置REPEATABLE READ。业务状态、消费进度、下一步Outbox意图在该推进事务内原子提交；网络事务外；UNKNOWN／HELD不自动重发或猜测完成。
- 业务锁序为工作线→设备→业务执行；既有命令／消息的框架锁不能嵌入相反顺序的业务推进。
- 只使用 fake／loopback 及显式独立测试库；保留现有数据、配置、stash 和无关改动；不得真实发送或迁移现有业务库。
- 每任务完成测试与复核即可标记完成；只有用户明确要求时才提交／推送。开发验收与供应商／现场验收分别记录。

## Review Focus

1. 历史省略／false／true 调试字段的收据：清理后重报仍ACK且不进入业务，双历史收据也不产生第三执行；Task 1。
2. WorkLineRead继承WorkLineCreate：新建必填不应让旧null数据无法读取；Task 4。
3. 固定入口冷启动：缺入口／内部ImportError不能被吞掉；全局正式先于local，local作用域首次发现真实正式request仍为正式注册，退出后恢复作用域；Task 3。
4. 声明／实际合同交叉校验及晚装配：未声明命令或事件角色、初始入口不匹配、附录冲突必须在资源构造前失败；runtime仍在lifespan构造，旧context兼容；Task 5。
5. 出站结果与前序：DONE非执行完成，pending不可消费；缺任务／清理／错范围不能冒充全闭合；Task 0。空／占位清单和真实业务启用门槛继续由Task 6验证。

## 范围、交付与顺序

本计划覆盖出站结果消费接口和业务公共接入，不承诺实现manual-picking或粗分完整流程。Task 0必须先确定契约，并在Task 5结果能力接线和真实业务消费之前完成最小实现及隔离验收；Task 1可独立交付，Tasks 2–4不必等待其运行结果。Task 6验证两条接入链和真实业务启用条件，不用假业务填空。

```text
Task 0 读模型／身份／闭合矩阵 → 最小查询接口及隔离验收 ─┐
                                                     ↓
Task 1 调试字段清理（独立）                    Task 5 业务能力接线
Task 2 声明清单 → Task 3 正式发现 → Task 4 工作线配置 ─┘
                                                     ↓
                         真实业务编排（独立业务计划）→ Task 6

Task 0不串行阻塞Tasks 1–4；其代码不得依赖app.businesses。
manual与coarse各自以Task 0为前置，不要求两业务同时完成。
本轮实施Tasks 1–5及Task 6公共接缝；真实业务和Task 6真实业务启用门槛另行推进。
```

Task 4和5共享控制器、装配及测试夹具，默认顺序执行。纯声明工作不必等待所有字段清理测试结束；但共享receiver／registry／控制器改动必须串行整合。公共接缝通过与具体业务落地通过，是两个不同交付结论。

## 文件与接口地图

表中“新增”均是未来产品文件，当前不创建。测试业务只在 tests 下构造，生产清单不登记演示项。

| 文件／区域 | 职责 | 任务 |
| --- | --- | --- |
| app/integrations/wms_request/results.py、wms_transport/results.py及predecessors.py；原两个service与导出 | 只读结果模型、原身份读取、前序查询及闭合矩阵；不改reliable | 0 |
| app/integrations/ecs_callback/{envelope,registry,receiver,__init__}.py、app/controllers/ecs_callback.py | 删除正式调试参数，兼容旧身份查重；原ACK／事务不变 | 1 |
| app/businesses/{__init__,definitions,catalog}.py | 纯数据声明、受信模块清单、声明查询及显式模型加载 | 2–3 |
| app/integrations/discovery.py、三类discovery、registration.py、app/core/module_tree.py | 统一分期、受信正式作用域，区分必需与可选模块 | 3 |
| app/businesses/catalog.py | 声明与实际注册的交叉校验，返回已校验命令／附录 | 5 |
| migrations/env.py、tests/conftest.py、tests/architecture/test_layering.py | 全部业务模型进metadata，边界检查覆盖新根 | 3 |
| app/models/workline.py、app/schemas/workline.py、app/services/workline_service.py | business_code与绑定校验、版本、过滤及审计 | 4 |
| app/controllers/{worklines,businesses}.py、app/schemas/business.py | 工作线API与只读业务选项 | 4 |
| app/businesses/runtime.py、app/main.py | 能力对象、简单工厂及唯一通信运行时装配 | 5 |
| ECS／WMS context、receiver、controller | 可选运行时引用传递；不导入具体业务 | 5 |
| docs/integrations/、AGENTS.md、tests/businesses/ | 新业务接入说明、层次约定、公共接缝测试 | 随负责任务更新 |

---

### Task 0: 出站结果消费契约与最小查询接口

**Files:**
- Create: app/integrations/wms_request/results.py；app/integrations/wms_transport/results.py、predecessors.py；docs/integrations/outbound-results.md。
- Modify: app/integrations/wms_request/{service,__init__}.py；app/integrations/wms_transport/{service,__init__}.py；app/integrations/wms_envelope.py复用终局响应校验／原消息投影；在原集成说明中链接读契约。
- Test: tests/integrations/wms_request/test_results.py；tests/integrations/wms_transport/test_results.py、test_predecessors.py；tests/integrations/test_result_consumption.py（独立测试侧动作表及事务入口，夹具放tests/helpers.py，不依赖Task 6或业务运行时）。
- 首版不改表结构、发送器或reliable清理；如现有消息关联不唯一或已清理，按下述不可判定合同返回，不按当前target猜测、不为了读接口改写历史。

**Interfaces:**
- WmsRequests.get_result(session: AsyncSession, *, message_id: int, operation: str, operation_id: str) -> WmsRequestResult。
- TransportService.get_result(session: AsyncSession, *, transport_task_id: str, client_request_id: str, caller: TransportCaller) -> TransportTaskResult。
- TransportService.list_unclosed_predecessors(session: AsyncSession, *, caller: TransportCaller, cycle_id: str, predecessor_ids: tuple[str, ...], rack_id: str | None = None, container_id: str | None = None) -> PredecessorQueryResult。
- DTO字段、availability／response_state及CLOSED／OPEN／UNDETERMINED逐项采用SPEC §10.2–10.6；不把平台ORM、response_raw或pending内容作为业务接口。明确前序最多100个，重复／超限／缺资源／同时填两种资源拒绝；空元组不查全历史。
- 多次查询合并为同一次前序判断时，由独立业务推进事务入口创建新session并在首条SQL前配置REPEATABLE READ，同一session／事务完成固定完整身份范围内所有批次、跨轮逐个get_result及混合读取；READ COMMITTED事务不能汇总全闭合。读方法不升级隔离、不接管事务；失败／重启／新事务不能复用部分读取，按SPEC §10.6重读全组。单次有界查询仍用单SQL，不改变全局默认隔离。受理处理器继续使用原ctx.session保存受理与待推进依据，不在其内升级隔离、提交／回滚或另开推进写事务；新session由可信装配的factory在独立入口创建，结果应用／接收事务不直接调用推进。

- [x] **Step 1: 写WMS原身份与可信响应测试并确认失败。** `test_request_result_uses_original_message_identity`验证目标配置改名／删除／另设同operation后仍读取原message_id，错operation／operation_id为IDENTITY_MISMATCH，无行NOT_FOUND，不冒充PENDING。`test_delivery_terminal_is_not_business_completion`参数化RECORDED、DUPLICATE、PREPARE_ACCEPTED、DECIDED、对端拒绝、LOCAL_ERROR、EXPIRED；只暴露协议可信终局响应，503／429／未知last响应不当决定，已DONE缺响应为UNAVAILABLE，非法终局响应为INVALID。`rtk proxy uv run pytest -q tests/integrations/wms_request/test_results.py`；产品接口不存在应失败，不以环境故障代替红灯。
- [x] **Step 2: 写Transport结果与闭合矩阵测试。** `test_transport_result_uses_applied_outcome_only`验证回调settle的DONE但无已应用终态仍等待，pending不作结果；`test_transport_closure_matrix`逐行覆盖SPEC §10.4，含明确位置的成功／失败／取消、逐箱位置未知、UNKNOWN更高版本收敛、缺TARGET_PLACED、提交明确409／422、超时／HELD／LOCAL_ERROR／EXPIRED、缺冻结事实、消息清理及多目标关联。既有已应用终态优先，拒绝不能盖过相矛盾的UNKNOWN／pending执行事实，或同任务有效SOURCE_PICKED／TARGET_PLACED里程碑。补test_refusal_and_applied_position_milestones_require_reconciliation，覆盖409／422、两种有效里程碑、POSITION_UNKNOWN诊断对照以及后续有效结果解除冲突。结果版本、到达面、逐箱结果必须保留。新增`test_historical_member_facts_do_not_override_newer_unknown`：先记录TARGET_PLACED，整批UNKNOWN的v1中相关成员位置明确，v2将该成员改为UNKNOWN；保留历史member_facts，但get_result及前序成员投影均为UNDETERMINED，不根据旧里程碑覆盖更新UNKNOWN、绕过pending或判当前占用；不修改原回调前置条件。
- [x] **Step 3: 写前序范围与完整性测试。** `test_predecessors_use_explicit_ids_and_original_scope`验证同架不同面在明确前序内仍参与，未被列为前序的旧轮、其他线、其他架、后续与独立任务不自动进入；明确ID错caller／cycle／资源或不存在时返回UNDETERMINED项且complete=False，不能过滤为零未闭合。`test_predecessor_query_never_silently_truncates`验证0、1、100、101条、重复ID、无cycle及资源二选一；items含CLOSED项及其证据引用，OPEN／UNDETERMINED保留在派生unresolved，所有项均已确定分类为CLOSED或OPEN时complete=True；只有全部CLOSED才同时complete=True且unresolved为空；混合批次仅无关成员UNKNOWN时不阻塞所查资源已明确的成员，保留整批task_closure且不把pending成员算成功，整批仍UNKNOWN时高版本改变相关成员事实应重新判定，不能用旧局部闭合永久放行；分批不能以第一批为空放行；完整身份范围预先固定，所有批次与逐个／跨轮读取在同一REPEATABLE READ快照汇总，禁止READ COMMITTED或跨事务片段拼接。`rtk proxy uv run pytest -q tests/integrations/wms_transport/test_results.py tests/integrations/wms_transport/test_predecessors.py`确认新接口及分支尚未实现。
- [x] **Step 4: 最小实现原服务读方法和DTO。** 仅在对应集成层查询并解析数据库投影列，用no_autoflush防隐式写入，不FOR UPDATE、不commit／rollback／flush、不访问HTTP或当前目标解析。Transport关联task.operation_id＋固定submit operation＋business_key，原消息唯一才可用，多条AMBIGUOUS、零条UNAVAILABLE；先应用闭合矩阵的权威结果优先级。任务与提交消息、每个有界前序批次使用单SQL语句快照，避免不同时间事实拼接；独立DTO副本不受session旧ORM缓存影响。由独立推进事务入口在首条SQL前配置REPEATABLE READ并负责本组session生命周期与事务；outbound-results文档给出复用现有引擎／连接池的独立session用法、整事务失败重试及受理／推进边界，不修改全局factory或将切换逻辑塞进受理处理器；已执行SQL的READ COMMITTED事务不能中途升级或继续汇总，须在调用方拥有的边界重开并全组重读。读方法不自行调整隔离、不commit／rollback；member_facts仅保留历史，不参与覆盖较新已应用UNKNOWN的判定。临时DB读取错误抛出，不能吞成NOT_FOUND或全部闭合；沿用已有纯信封／结果DTO校验规则，不调用回调应用器改状态，不新增可靠层查询API或查询触发写入的缓存。
- [x] **Step 5: 验证只读、保留与消费恢复。** `test_result_reads_never_write_lock_or_send`设置flush／HTTP／Sender／框架写锁哨兵；读取后DB不变，调用方待写状态不被flush，返回副本修改不影响存储。`test_transport_reads_use_one_statement_snapshot`在独立PostgreSQL交叉运行回调与读取，不能拼成错误CLOSED；同session先持有旧ORM对象再读仍得当前事务可见SQL事实。新增`test_predecessor_group_uses_one_transaction_snapshot`：独立PostgreSQL用独立读写连接及确定性屏障，通过真实Transport回调接收／应用路径提交合法版本，不直接改outcome列。A／B均为含相关资源R1与无关资源R2成员的料箱任务；R2成员始终UNKNOWN，使两任务整体始终UNKNOWN。A v1的R1成员已知终位，B v1的R1成员UNKNOWN；读者建立快照并读A后，依次提交A v2（R1变UNKNOWN）、B v2（R1变已知终位）。成功回架的成员先经合法位置回调满足TARGET_PLACED；A保留旧里程碑仍不得盖过v2 UNKNOWN。101条用99个确定闭合前序＋A置首批、B置次批；跨轮逐个get_result及混合读另参数化原范围。断言首个快照A／B精确为v1／v1、相关closure为CLOSED／UNDETERMINED；新事务为v2／v2、相关closure为UNDETERMINED／CLOSED；批量项的task_closure及逐个get_result的整任务closure均UNDETERMINED、outcome均UNKNOWN；逐个get_result仅以公开已应用outcome在测试侧按相同成员规则汇总相关闭合，不把整任务closure改写成CLOSED，也不从member_facts推导闭合；完整requested_ids／items无缺失重复，closure_basis引用本次准确revision，聚合不得全闭合。另以A v3合法恢复R1已知、R2仍UNKNOWN，验证新快照A v3／B v2相关项全CLOSED，complete=True且unresolved为空，同时整批仍UNKNOWN；不能仅以“未全闭合”这一弱断言验收。`test_predecessor_group_restarts_after_read_committed_or_failure`验证已用READ COMMITTED事务不能中途升级并汇总，任一批失败／事务重开／重启后旧片段不复用；需要新事实时全组换新事务，不能声称同一快照重读已看到外部更新。`test_retention_never_becomes_success_or_resend`验证可靠消息清理后WMS不可用、Transport已应用终态仍闭合、未保存的拒绝证据不可伪造。本任务独立的tests/integrations/test_result_consumption.py中，`test_result_consumption_commit_and_recovery`用测试侧动作行保存原身份／版本和必要结果，证明消费前崩溃可重读、消费标记和下一意图同事务、提交后重启／重复唤醒不重复动作；不建立平台消费表；不同业务动作可各自消费同一结果版本，成员投影引用包含资源／member_ids，不能全局去重吞掉另一动作。扩展消费恢复用例：旧member_facts与新UNKNOWN并存时不登记下一意图；独立推进事务入口在真实并发更新／锁竞争下遇到序列化失败时，回滚关闭失败session，以新session全组重读（断言实际已应用版本）并沿用原执行／动作身份；失败尝试没有已提交消费／意图，成功后仅一条意图，不另发外部动作。补test_failure_after_reading_restarts_the_entire_predecessor_group：先读取两个整体UNKNOWN任务的已闭合成员投影v1，保存整组消费版本及暂存下一意图，再由独立guard行并发更新触发真实40001；回滚后核对动作／版本标记、TransportTask及ReliableMessage均无失败尝试残留。新session重读两个前序均为v2，分别覆盖成员变UNKNOWN时保留待办且不生成意图，以及仍闭合时仅提交一条意图、重复唤醒不重复。验证有界尝试耗尽后仍保留可恢复待办／受控失败依据，后续唤醒按业务规则恢复；读方法无重试，Outbox仍仅负责已提交意图投递。
- [x] **Step 6: 验收接口并确定可供业务接线。** 运行`rtk proxy uv run pytest -q tests/integrations/wms_request tests/integrations/wms_transport tests/integrations/test_result_consumption.py tests/reliable/test_retention.py`，独立PostgreSQL跑上述快照／原子消费并发，记录所有跳过。检查业务示例只调用公开读方法，包导出／文档与SPEC字段一致；WMS STATIC、Transport提交／原回调应用、重试、TTL和清理行为保持。Task 0接口完成后才允许Task 5结果能力接线及manual／coarse编排实施，不能用直读表的假实现通过验收。

### Task 1: 清理 ECS is_debug，保留历史查重

**Files:**
- Modify: app/integrations/ecs_callback/envelope.py、registry.py、receiver.py、__init__.py；app/controllers/ecs_callback.py。
- Test: tests/integrations/ecs_callback/test_envelope.py、test_registry.py、test_receiver.py、test_reliability.py；tests/controllers/test_ecs_callback.py；测试共用逻辑放tests/helpers.py。
- Docs: docs/superpowers/specs/2026-10-24-ecs-event-callback-design.md的现行合同说明；保留历史决策轨迹，不伪称旧版本本来没有该字段。

**Interfaces:**
- 保留 `event_identity(envelope: EcsEventEnvelope) -> str` 与 `receive_ecs_event(session, raw, *, resolve_binding=None)` 的现有调用方式。
- 新信封与EcsEventContext不再有is_debug。新摘要由device_code、event_type、timestamp、data（缺省空对象）生成；其他顶层字段继续忽略。
- 复用现有find_receipt／insert_receipt，不增加另一张收据表或重算原行。

- [x] **Step 1: 写行为回归。** `test_legacy_receipt_replay_never_invokes_handler` 对原省略／false／true各建立旧算法真实收据，再发送原消息及只改额外调试字段的消息，均断言200 ACK、handler零次、旧行未改写。`test_two_legacy_receipts_do_not_create_a_third_execution` 建立两条旧记录，断言无新业务动作。`test_extra_fields_do_not_split_event_identity` 断言同一核心事件的不同额外字段只受理一次；不写“属性不存在”这类临时迁移形状测试。
- [x] **Step 2: 运行失败用例。** `rtk proxy uv run pytest -q tests/integrations/ecs_callback/test_receiver.py tests/integrations/ecs_callback/test_envelope.py`；确认失败指向旧字段仍影响身份，不把环境故障当作预期失败。
- [x] **Step 3: 最小实现。** 删除字段、上下文传参和OpenAPI项；先按新摘要及同一规范化事件的两个旧摘要查重，再按新摘要唯一插入。旧摘要中的false含原省略字段；不接受旧摘要作为新消息主身份。命中旧行沿用其原ACK与原收据引用；历史双命中按created_at、id稳定选择诊断引用，不合并历史或再次执行。保持先查重后Payload校验及异常回滚。
- [x] **Step 4: 补并发与HTTP回归。** `test_same_core_event_concurrently_executes_once` 在独立PostgreSQL验证不同额外字段下同一核心事件只执行一次；原未注册收据升级后也不自动执行新handler；不合法核心信封仍400，临时错误仍503且无残留。删除旧测试中的is_debug构造依赖，更新有意义的诊断期望。
- [x] **Step 5: 验证交付。** `rtk proxy uv run pytest -q tests/integrations/ecs_callback tests/controllers/test_ecs_callback.py`；明确记录PostgreSQL跳过数。检查app和相关文档剩余is_debug仅用于兼容说明／旧摘要，不在正式上下文、OpenAPI或业务条件中。发布要求所有接收实例统一算法后恢复接收，不能新旧版本混接。

### Task 2: 纯声明与显式清单

**Files:**
- Create: app/businesses/__init__.py、definitions.py、catalog.py。
- Create tests: tests/businesses/test_definitions.py、test_catalog.py；共享fixture在tests/helpers.py。

**Interfaces:**
- definitions.py严格沿用设计四种`frozen=True, slots=True, kw_only=True`对象：DeviceRole、PositionSlot、InitialEntry、BusinessDefinition及其字段；不另增插件注册类。
- catalog.py：`entries() -> tuple[ModuleType, ...]`；`definitions() -> tuple[BusinessDefinition, ...]`；`get_definition(code: str) -> BusinessDefinition`（未知编码抛LookupError）。返回清单不调用业务factory。
- 每个实际composition模块提供DEFINITION、ENTRY_MODULES、COMMAND_BINDINGS、ANNEXES和build_service；没有设备命令时用空集合，不造占位合同。生产entries仅列实际存在且可装配的业务，初期可为空，不虚构本文两个样例已经可用。

- [x] **Step 1: 写定义与查询测试。** `test_definition_rejects_invalid_or_duplicate_keys` 断言非法大写key、重复key、非bool required、非tuple集合、未知入口角色失败；`test_definition_import_has_no_runtime_side_effects` 在独立解释器拦截ORM／集成／网络导入，导入纯定义仍成功；`test_listing_does_not_call_factories` 将factory设为一调用就失败，列选项仍成功。两个测试业务的同名事件可使用不同角色，不允许重复业务code。
- [x] **Step 2: 运行并确认缺少目标模块导致失败。** `rtk proxy uv run pytest -q tests/businesses/test_definitions.py tests/businesses/test_catalog.py`。
- [x] **Step 3: 实现声明校验与简单查询。** key使用现有`^[A-Z][A-Z0-9_]{0,99}$`合同；纯定义对WMS入口只做字符串／引用校验，合法operation语法在catalog装配时复用现有is_operation_name，避免复制operation正则或让纯定义导入框架。code为稳定小写下划线名称，显示名称非空；ENTRY_MODULES是代码固定路径，清单按显式import返回模块。重复声明启动失败，不做磁盘插件扫描。
- [x] **Step 4: 验证。** 上述测试通过；独立导入每份definition不加载composition、contracts、ORM或服务。此阶段不创建真实manual／coarse空壳，不登记不完整资源清单，不导入app.main。

### Task 3: 扩展发现、来源检查及模型加载

**Files:**
- Create: app/integrations/discovery.py（协调候选路径与正式／local加载，不创建注册表）；tests/integrations/test_registration_bootstrap.py。
- Modify: app/integrations/{ecs_callback,wms_callback,wms_request}/discovery.py；app/integrations/registration.py；app/core/module_tree.py；app/businesses/catalog.py；app/main.py；app/controllers/{ecs_callback,wms_events,dev}.py；migrations/env.py；tests/conftest.py；tests/architecture/test_layering.py。
- Test: tests/integrations/test_registration_discovery.py、test_registration.py、test_registration_bootstrap.py；tests/businesses/test_model_loading.py。

**Interfaces:**
- `import_module_tree(name: str, *, required: bool = False) -> None`，`import_registration_module(name: str, *, local_only: bool = False, required: bool = False) -> None`；旧调用默认行为不变。
- catalog新增 `entry_modules(kind: Literal["ecs_event", "callback", "request"], *, environment: str) -> tuple[str, ...]`；只返回固定候选路径，不在取列表时执行导入。协调器合并内置／业务候选并分期；kind表示注册器类别，正式／local按现有DEV_MODULES及入口布局区分，不从payload或数据库取import路径。
- app/integrations/discovery.py：`load_formal_modules(kind: Literal["ecs_event", "callback", "request"] | None = None) -> None`；`load_local_modules(*, environment: str, kind: Literal["ecs_event", "callback", "request"] | None = None) -> None`。完整应用使用全量正式加载；独立WMS请求发现仅使用kind="request"，不顺带加载其他类型或main。
- registration.py新增 `formal_registration()` 受信作用域供正式协调器使用，ContextVar显式置False并在finally还原；通用import_registration_module(local_only=False)保留继承外层作用域的现有含义，不能借此放宽dev普通导入链。
- 明确模型路径用composition可选`MODEL_MODULES: tuple[str, ...] = ()`登记；`load_business_models() -> None`只导入这些固定模块及轻量包，不调用factory。该字段是显式模型清单，不是新插件生命周期。

- [x] **Step 1: 写发现回归。** 保留`test_required_business_entry_missing_fails`、`test_internal_import_error_is_not_skipped`、`test_business_source_keeps_filename_and_local_guards`、`test_standalone_request_lookup_loads_business_contract`；新增`test_all_formal_kinds_load_before_any_local_entry`、`test_cold_request_discovery_inside_local_scope_stays_formal`、`test_formal_scope_restores_on_success_and_error`、`test_dev_plain_import_of_unloaded_formal_entry_still_fails`、`test_reentrant_discovery_does_not_return_partial_catalog`。冷启动测试在隔离子进程中创建真实尚未导入的正式request模块，不能用“无request可发现”代替；清空相关模块、注册表和成功缓存，分别在不同全新子进程验证operations()和lookup()首次注册该请求，不能先调用operations再把已缓存的lookup当作冷启动通过；断言两条路径均返回正式请求项且source.local_only=False，退出后新dev注册仍为True。故意让正式模块导入失败时原异常上抛、作用域还原，不返回半注册目录。全局顺序测试跨两个根和三类注册器，首个dev导入前所有正式项已完成；nonlocal不导入任何dev入口。
- [x] **Step 2: 运行并定位失败。** `rtk proxy uv run pytest -q tests/integrations/test_registration_discovery.py tests/integrations/test_registration.py tests/integrations/test_registration_bootstrap.py tests/businesses/test_model_loading.py`；当前平台的False仍继承外层local作用域，新增冷启动用例应暴露该路径，不能靠提前import把它变成已缓存测试。
- [x] **Step 3: 扩展现有机制。** 先合并三类内置／业务候选，再经受信formal_registration加载正式项；不执行旧的“内置formal/dev混合循环→业务formal”顺序。main在任何include_*加载local项前完成全量正式加载；Task 5补上此处与local阶段之间的业务交叉校验。各include函数改用协调后的阶段，不再私自抢先加载dev；dev控制台也先经过正式阶段。WMS request惰性发现仅委托正式request阶段，可在local作用域冷启动，不导入main、其他入口或factory。协调器用候选函数而不反向调用会再次触发自身的load函数；正式入口导入期不得查询尚未完整的注册目录。发现复入明确失败，不返回部分目录；只缓存完整成功，导入错误原样上抛。装饰器仍检查新根来源／文件名，必需业务路径以required=True导入，原可选空路径继续可跳过。
- [x] **Step 4: 加载模型和执行层次约束。** Alembic读取Base.metadata前和测试建立schema前调用load_business_models；已登记模型不随工作线是否选择业务而变化。`test_model_loading_is_independent_of_configured_worklines` 断言在没有工作线、没有Sender时也能登记全部受管模型。依赖检查覆盖业务入口→服务→模型和纯definition；只给明确的发现／应用装配／模型加载路径例外，不对integrations整体放开反向业务依赖。
- [x] **Step 5: 验证。** 跑上述用例及`rtk proxy uv run pytest -q tests/architecture/test_layering.py tests/integrations/ecs_callback/test_discovery.py tests/integrations/wms_callback/test_discovery.py`；分别用全新子进程验证local／非local全量启动、local作用域首次请求目录查询、重复发现与失败路径。断言正式来源标记正确且作用域恢复，原dev普通导入保护仍有效；模型加载独立且不触发入口、网络、DB连接或factory。分层检查只对明确发现协调路径开放清单读取，不让generic机制导入具体业务包。

### Task 4: 工作线业务选择、绑定和管理选项

**Files:**
- Modify: app/models/workline.py；app/schemas/workline.py；app/services/workline_service.py；app/controllers/worklines.py；app/main.py。
- Create: app/controllers/businesses.py；app/schemas/business.py；migrations/versions/2026-10-27_workline_business.py（执行时按当日和真实head确定修订链，不预造revision ID）。
- Test: tests/controllers/test_workline_business.py；tests/services/test_workline_business.py；tests/migrations/test_workline_business.py；相关现有工作线创建fixture／示例／审计断言。

**Interfaces:**
- WorkLine新增nullable字符串business_code，不设猜测默认值。WorkLineCreate新增必填编码；WorkLineUpdate可省略；WorkLineRead必须显式允许null，不能直接继承Create的新非空约束而漏覆写。
- `create_workline(session, data, *, actor, definition_lookup)`、`update_workline(session, workline_id, data, *, actor, definition_lookup)`；lookup由应用传入Task 2的get_definition，禁止service直接import具体业务。
- 现有`validate_bindings(session, workline_id, bindings, *, definition=None)`保留旧null线原规则；有业务时增加声明key检查。设备归属、重复编码和锁继续用原实现。
- `GET /api/v1/businesses`返回只读BusinessRead：code、display_name、device_roles、position_slots、initial_entries；每个角色／位置仅key、display_name、required。管理员＋asset:workline:read，不暴露factory、模型和源码路径。

- [x] **Step 1: 写管理合同测试。** `test_legacy_null_line_remains_readable`、`test_new_line_requires_known_business`、`test_business_can_only_be_selected_once`、`test_business_update_omission_preserves_selection`、`test_workline_business_is_audited_and_filterable`。断言新建缺失／null／未知code失败；旧null首次选择成功；已选后同值更新允许、换值或显式清空409；普通编辑省略该字段不清空。service未知code映射既有INVALID_REQUEST，不新增错误枚举。
- [x] **Step 2: 写绑定及权限测试并确认失败。** `test_partial_bindings_can_save_but_wrong_keys_cannot`、`test_cross_line_or_duplicate_device_is_rejected`、`test_business_options_require_admin_and_workline_read`。缺必需角色允许暂存；已填未知角色／位置key拒绝，当前字符串位置不被误判成现场可用。接口仍验证原version；选择业务和绑定同事务校验，版本与审计一次更新。
- [x] **Step 3: 实现迁移、schema和CRUD。** 先给现有数据增加null；不回填猜测用途。业务编码省略处理按现有`model_fields_set`模式，不能漏项清空。读取／列表过滤／snapshot审计同步支持business_code；创建／更新控制器显式传lookup，并更新全部调用方、fixture和示例，不保留绕过校验的默认创建路径。
- [x] **Step 4: 实现只读选项与配置执行校验。** `business_configuration_issue(session, binding, business_code, *, definition_lookup) -> str | None`放在workline_service.py，按已锁定工作线重新核对实际business_code及当前声明。错业务返回受控WORKLINE_BUSINESS_MISMATCH，缺必需绑定返回WORKLINE_CONFIGURATION_INCOMPLETE；临时DB／绑定竞态抛出，不诊断ACK。此函数不负责业务许可、物理状态或发送命令；具体动作仍经原角色解析和发送门禁。新增选项路由按项目响应／权限约定挂载。
- [x] **Step 5: 验证数据与管理边界。** `rtk proxy uv run pytest -q tests/controllers/test_workline_business.py tests/services/test_workline_business.py tests/migrations/test_workline_business.py tests/controllers/test_device_management_flow.py`；迁移在临时SQLite及显式独立PostgreSQL验证旧行、单一head和有业务选择时不能静默丢数据降级。没有真实业务包前只可通过测试注入清单验收，不发布新建必选版本。

### Task 5: 业务运行时、上下文和唯一应用装配

**Files:**
- Create: app/businesses/runtime.py；tests/businesses/test_runtime.py、test_entry_context.py。
- Modify: app/businesses/catalog.py；app/main.py；app/controllers/{ecs_callback,wms_events}.py；app/integrations/{ecs_callback,wms_callback}/{registry,receiver}.py；tests/helpers.py；对应控制器和context测试。
- Create test: tests/businesses/test_contract_assembly.py。

**Interfaces:**
- 结果能力接线的前置依赖：Task 0契约及最小读接口隔离验收已完成；交叉校验／上下文等不消费结果的步骤可在Task 3后独立推进，Task 5整体完成仍须包括结果能力接线。能力对象引用带有get_result／list_unclosed_predecessors的原服务，不创建另一套结果仓储或直接读表。
- runtime.py提供简单Capabilities数据对象（ecs: EcsCommands | None、wms: WmsRequests、transport: TransportService）和BusinessRuntime（definitions: Mapping[str, BusinessDefinition]、services: Mapping[str, object]）；服务映射保存无作业状态对象，不保存session／某线进度。各factory仍为`build_service(capabilities)`，允许不同业务返回不同服务类型。
- catalog.py新增 `validate_business_contracts() -> tuple[tuple[CommandBinding, ...], dict[tuple[str, str], DeviceAnnex]]`，仅在全部正式入口加载成功后调用。按SPEC §6.5核对每业务命令角色／附录、实际正式事件角色、初始入口完整键及正式来源、全局命令重复键、附录冲突；返回已检查的命令集合和合并附录，不构造设备路由、factory、Sender或网络资源。
- `build_runtime(*, outbox, ecs_commands) -> BusinessRuntime`：使用前置校验完成的同一catalog，用现有WmsRequests(outbox)、TransportService(outbox)和唯一ECS服务注入；不自行创建Sender、引擎、Redis或另一个事件循环客户端。空清单返回空runtime；非空清单缺少所需通信能力时启动失败。不能把交叉校验延迟到该函数取得运行中Outbox后才执行。
- ECS／WMS context新增可选`runtime: object | None`；平台层不反向导入具体业务。WMS的字段用`field(default=None, kw_only=True)`加在尾部，保留原位置参数和notes默认值；不靠继承context构造子类。
- receiver添加可选runtime关键字参数；WMS内部_handle也传同一引用。include_ecs_callback／include_wms_events可接受可选runtime，用于独立应用显式装配；普通应用控制器请求时从可信app.state.business_runtime取值。省略参数不得清空已装配引用。
- `require_runtime(ctx) -> BusinessRuntime`在业务runtime模块验证引用；测试使用真实Runtime容器装fake业务服务。业务入口以ctx.session和原触发身份调用受理服务，仅保存受理状态及持久待推进依据；不在已执行SQL的ctx.session中建立结果快照或另开推进写事务。提交后由本业务独立事务入口以原execution_id推进，创建／关闭新session、首条SQL前配置隔离及整事务重试均属于该入口；应用／worker可信装配注入所需factory，不新增通用execute、不在runtime保存活动session。实际入口和持久待办由后续各业务计划实现，Task 0／6仅在测试侧验证。

- [x] **Step 1: 写装配测试。** 保留`test_factories_share_platform_capabilities_without_job_state`、`test_context_receives_runtime_created_during_lifespan`、`test_callback_context_old_positional_calls_still_work`。新增test_contract_assembly.py：`test_command_role_must_belong_to_its_business`、`test_registered_event_role_must_belong_to_its_business`、`test_command_requires_its_declared_annex`、`test_initial_entry_requires_own_formal_registration`、`test_duplicate_command_keys_fail_assembly`、`test_conflicting_annex_keys_fail_before_resource_creation`、`test_identical_annex_contract_can_be_shared`、`test_invalid_contract_never_constructs_routes_sender_or_factory`。断言未声明角色失败、命令在本业务ANNEXES漏声明时即使别的业务或历史集合有同名附录也失败；初始入口缺注册、错ECS角色、仅有同名request而无callback、来源在另一业务／dev时失败；同键附录任一模型类或超时不同均失败，模型类与超时完全相同可复用；仅留历史附录不新增命令权限。将DeviceRoutes、Sender和factory构造设为一调用就失败的哨兵，验证错误先于资源构造且不进入local阶段；纯定义查询不执行入口或这些校验。正常路径服务复用同一outbox／ECS实例，两线携带各自事务和binding。
- [x] **Step 2: 运行并确认失败。** `rtk proxy uv run pytest -q tests/businesses/test_contract_assembly.py tests/businesses/test_runtime.py tests/businesses/test_entry_context.py`；未声明角色用例不能仅测RoleRoutes抛错，现有RoleRoutes无业务定义输入，必须经新的应用装配校验入口验证。
- [x] **Step 3: 实现交叉校验及可选依赖传递。** 实现validate_business_contracts，以原注册项RegistrationSource.module的业务包完整前缀核对归属，排除平台内置和local项；初始入口不能借另一业务／dev注册满足。先检查每个命令引用在本业务ANNEXES中存在，再将附录逐项合并；共享合同在各业务显式引用。同键的params_model／result_model／error_model分别必须为同一类且两个timeout相同，否则失败，禁止先字典覆盖再检查。Generic context只透传runtime，不在receiver按business_code分发。保留原context构造方式，独立业务应用及其测试先显式完成同一全量加载／交叉校验／local序列，再挂载入口，不依赖app.main。
- [x] **Step 4: 实现一次通信装配。** main在include_wms_events／include_ecs_callback／include_dev_routers之前完成`load_formal_modules() → validate_business_contracts() → load_local_modules(environment=...)`；任何失败不构造DeviceRoutes／Sender／factory、不对外服务。local仅在此后经configure_local_ecs组合正式／dev，非local经configure_ecs接入已校验的DeviceRoutes及附录；沿原ecs_runtime_config和reliable_lifespan创建唯一通信运行时。Outbox可用后才调用factory，将runtime传入入口，在lifespan yield前核对服务完整；不在模块加载时要求runtime已存在。空清单保留非业务WMS／local调试原行为。没有实际业务消费者前不改Celery或新增Beat扫描器；未来业务worker复用上述正式加载与校验，独立装配资源。
- [x] **Step 5: 验证协议及事务。** `test_ecs_business_diagnostic_acks_without_execution`、`test_transient_failure_rolls_back_receipt_and_business`、`test_wms_business_keeps_rejected_conflict_contract`用测试侧service断言稳定配置问题诊断ACK无动作；临时错误503且同事务回滚；WMS Rejected的savepoint回滚与Conflict保持原合同。新增`test_acceptance_never_starts_result_advancement`，从真实ECS／WMS接收路径断言处理器仅登记受理／待办，不切隔离、不自行commit／rollback、不在受理内开第二个推进写事务；原收据与受理／待办同事务，稳定拒绝不产生待办，临时错误无残留。跑现有ECS角色合同、WMS回调和注册测试；UNKNOWN／HELD、冻结路由、历史附录和WMS STATIC保持。

### Task 6: 公共接缝整合验收和真实业务接入门槛

**Files:**
- Create: tests/businesses/test_workline_business_flow.py、test_result_consumption.py；docs/integrations/business-packages.md。
- Modify: AGENTS.md对应目录／发现／业务职责约定；既有注册与设备管理文档的新增业务说明；本计划进度。

**Interfaces:**
- 不新增生产接口或演示业务；复用Task 0及Tasks 1–5，测试业务只经原服务公开读接口消费结果；此处为整合回归，不能反向成为Task 0完成的前置。测试fixture构造两份业务及两条线，仍使用既有协议注册和真实事务入口，外部能力只fake／loopback。

- [x] **Step 1: 写完整接缝用例。** `test_same_business_two_lines_never_cross_routes`、`test_business_mismatch_never_creates_intent`、`test_two_businesses_same_event_name_use_own_role_payload`、`test_callback_restart_preserves_original_receipt`；从管理API配置，到单据／设备事件受理提交，再经独立推进入口检查推进事务及Outbox意图；无需消费结果的初始意图仍按其业务合同检查。增加`test_app_checks_all_business_contracts_before_any_local_or_runtime_assembly`，用事件顺序及构造哨兵证明“全量正式→交叉校验→local→资源→factory→对外受理”，同时验证错误包使整体启动失败，非local不出现dev注册。用测试表保存进度，不向生产代码加入通用业务执行表或流程引擎。
- [x] **Step 2: 证明事务与锁。** 增加test_business_runtime_consumption_recovery，扩展Task 0独立消费／崩溃／清理验收到真实业务运行时接线，禁止从业务测试绕过平台读ReliableMessage／TransportTask列。 新增`test_accepted_execution_advances_in_its_own_snapshot_transaction`：经真实ECS／WMS接收入口提交原收据、受理状态及待办，之后用测试业务独立推进入口创建新session，在任意业务定位／锁定SQL前配置REPEATABLE READ；通过数据库实际隔离级别及连接生命周期断言，而不只检查mock参数。推进故障不撤销已提交受理／原ACK；丢提交后通知由持久待办恢复，重复触发／唤醒仍仅一条下一意图，结果接收提交后才能推进。复用Task 0合法回调版本场景，断言精确revision／成员closure／完整ID；推进序列化失败新session重读，失败尝试无消费／意图残留，重试耗尽有可恢复依据。上述事务入口夹具复用tests/helpers.py，不导入其他测试文件，也不加入生产通用worker。 在显式独立PostgreSQL交叉运行接收、配置更新、命令发送门禁和重复消费，分别断言“收据＋受理状态＋持久待办”在受理事务内原子、“推进状态＋消费标记＋下一意图”在独立推进事务内原子，不要求两阶段共用事务；阶段间靠持久待办恢复，同身份仅一次、无串线和锁超时。SQLite通过不能替代此项；外部只loopback，测试不得连接真实供应商。
- [x] **Step 3: 完整本地检查。** `rtk proxy uv run ruff check .`、`rtk proxy uv run ruff format --check .`、`rtk proxy uv run pytest -ra`；分别记录通过／跳过／环境失败，不把跳过写为通过。再次检查Alembic仅一head，不在应用启动create_all；生产清单不得含tests或dev占位业务。
- [x] **Step 4: 完成开发者说明。** 按definition→严格合同→composition→子包service/model→薄入口→catalog→模型加载→两线测试的顺序给一个最小写法；占位名称明确不可注册发送。文档区分“配置保存成功／可执行／物理完成”，以及消息身份／本次业务经过身份，不复制整套WES插件SDK。
- [ ] **Step 5: 核对实际交付范围。** 至少一份合同和资源完整的真实业务包可用，才能把新建必选business_code、正式清单和完整应用接线作为可用功能合入发布。不用空业务／虚构合同补齐；若尚无真实包，仅报告“公共接缝已在隔离测试验证，整合交付未完成”；Task 6保持未完成，不能声称本计划已经整体交付。已配置真实业务不能仅凭factory能构造就宣布完整业务闭环通过。

## 另行编写的业务实施计划

| 子计划 | 需要先明确的输入 | 必须覆盖的业务范围 | 当前结论 |
| --- | --- | --- | --- |
| manual-picking | WES目标合同版本、移植范围、真实角色与位置／附录、任务与计划身份 | issued/prepare/plan，货架／供箱／经过／回程协作状态，退箱／换面／离场直接依赖和本轮隔离 | 设计有流程示例，尚无可执行移植任务 |
| coarse-sorting | 本次经过身份、验证／GRN绑定／分配／报告operation及字段，NG／失败／等待闭合 | 扫码→验证→OK/NG→原FORWARD成功→分配→机械臂→原结果报告 | 不能从示例名称推导正式协议 |
| WMS／Transport结果消费 | 本计划Task 0与SPEC §10.2–10.6已经明确最小契约 | Task 0先实现读模型／查询／闭合矩阵；业务只补自身响应Payload、消费进度和下一步规则 | 明确前置，不再等业务开始后定接口；不放入reliable |

Task 0已有最小平台合同，不再作为“首个业务出现后再定”的未决项；各业务仍需确认自身Payload和完成规则。上述是具体业务计划的必要输入，不要求现场验证来阻止已授权的软件开发验收；也不擅自定义供应商字段。没有业务状态模型前不设计通用恢复扫描器。后续各业务必须自己实现持久待办、消费进度和独立推进事务入口，明确session factory装配、首条SQL前的隔离配置、整事务有界重试及耗尽后的恢复／受控失败策略；受理处理器不得承担已提交后的推进重试。提交后通知可丢失／重复，不能接管Outbox重试。

## 计划自检与执行状态

- [x] 设计追踪：§10.2–10.6对应Task 0；§6–7对应Task 2；§8对应Task 4；§9.3对应Task 1；其余§9与§14对应Tasks 3/5；§10–13完整编排明确留在后续具体业务计划，未冒充当前公共计划已覆盖实现。
- [x] 五项Review Focus均有负责任务及明确断言；交叉校验由Task 5负责，协调加载与local冷启动由Task 3负责，Task 6验证整体顺序；未来接口与调用方已在各任务列出。
- [x] 公共机制／本地开发入口／正式业务／供应商合同边界分开；没有新增插件生态、基类或第二套可靠投递。
- [x] 既有路径和函数已对照当前源码；新增路径为计划项，不声称现有。
- [x] Task 0已完成；Tasks 1–5已实施并逐项评审通过；Task 6公共接缝已实施，真实业务启用步骤仍未满足。Task 4新增nullable business_code迁移。
- [ ] 真实业务合同未齐备，Task 6整体启用条件尚不满足；本计划不授权提交、推送、合并或部署。


## 实施前评审修订复核（2026-10-27）

| 原评审项 | 负责步骤 | 复核点 |
| --- | --- | --- |
| P1 交叉校验 | Task 5 Steps 1–4；Task 6 Step 1 | 有明确校验函数、返回值和失败时点；角色／初始入口／附录与真实注册关联，错误先于DeviceRoutes／Sender／factory |
| P2 冷启动加载 | Task 3 Steps 1–5；Task 5 Step 4；Task 6 Step 1 | 正式候选统一收集，受信作用域及还原，真实未缓存request测试，dev普通导入保护保留，全局顺序有集成断言 |

本轮已对照 SPEC §6.5、§14.1–14.2、§16 及现有角色／注册／发现／main接口复核，未发现新的阻断性文档问题。未修改产品或测试代码，新增测试均尚未执行；旧22项既有测试结果不作为新增能力通过证据。Task 6的真实业务接入门槛保持未满足，不因本次文档复核而自动开启实施、提交或部署。


## Task 0修订复核（2026-10-27）

- [x] SPEC §10.2–10.6与本计划使用相同三个读模型、三个方法、三态闭合、原身份和保留边界；不把last_response_raw、pending或消息DONE当作业务完成。
- [x] 依赖为Task 0→Task 5结果能力／两业务编排→Task 6；Tasks 1–4及Task 5的非结果消费步骤可独立。Task 0的独立消费恢复测试不依赖Task 6，避免验收环。
- [x] 前序查询保存明确原任务身份，只按当前指定范围；items含完整判定及证据，unresolved由其派生。成员投影不误伤无关架／箱，也不伪造整批终态。
- [x] 只读SQL／事务快照、动作消费唯一性、消息清理后未知、历史身份不改写均有负责任务与断言；业务实现不得绕过公开读接口。
- [ ] 尚未开发任何Task 0模块、迁移或新增测试；文档复核未代替接口行为、PostgreSQL并发或具体业务验收。原暂存区不变，本轮只改两份工作区文档。


## 统一快照与历史里程碑修订复核（2026-10-27）

- [x] SPEC §10.5–10.6与Task 0 Interfaces／Steps 3–6一致：同一次判断的分批、逐个及混合读取共用首条SQL前配置的REPEATABLE READ事务；固定完整身份范围，不跨事务拼接，读方法不接管调用方事务。
- [x] SPEC §10.2、§10.5、§16与Task 0 Steps 2、5一致：member_facts仅为历史里程碑；较新已应用UNKNOWN保持不可判定，不推导占用、不登记下一意图，保留原回调前置条件。
- [x] PostgreSQL验收覆盖101条分批、跨轮逐个、混合查询、并发高版本、READ COMMITTED误用、失败后全组重读、新事务看到更新及序列化失败恢复；沿用Task 0独立测试，不反向依赖Task 6。
- [x] 定向复核未发现新的阻断性文档问题；原身份、三态闭合、保留边界及Task 0前置依赖不变。旧复核记录是历史，本节对应最新修订。
- [ ] 未开发产品或测试代码、未运行新增数据库并发用例；本轮只有文档修订与静态复核，不表示运行能力已通过验收。


## 事务建立入口与合法并发验收修订复核（2026-10-27）

- [x] P1：SPEC §9.4、§10.1、§10.6与本计划Global Constraints、Task 0、Task 5／6使用相同边界：原ctx.session受理并提交待办；独立业务入口创建新session、建立统一快照并负责commit／rollback及整事务重试；服务／读方法不接管事务。
- [x] 受理失败沿原协议回滚；受理提交后推进失败不撤销ACK、不靠重放受理恢复。Task 0验证独立事务与故障恢复，Task 5验证受理不越界，Task 6贯通真实入口与持久待办；具体业务实现及有界重试策略仍由其后续计划落实，不新增平台调度器。
- [x] P2：Task 0并发用例使用整体始终UNKNOWN的合法成员投影变化，经真实回调产生A／B的v1→v2→正向v3；断言完整ID、准确revision、成员closure、task_closure与依据引用，覆盖101条分批、跨轮逐个及混用，并保留全闭合正向对照。
- [x] 定向复核未发现新的阻断性文档问题；Task 0独立于app.businesses／Task 6，原收据协议、历史member_facts、UNKNOWN规则和Outbox投递重试不变。
- [ ] 新增入口与测试尚未实现或运行；本轮仅修订SPEC／PLAN，不改变暂存区，不授权实施、提交、推送或部署。


## Task 0实施与完成复核（2026-10-27）

本轮授权仅Task 0。已提供WmsRequests.get_result、TransportService.get_result及list_unclosed_predecessors，导出三个读模型及前序项；未实现人工分拣／粗分、业务运行时或生产推进入口，未改发送器、原回调应用、reliable或表结构。共享终局响应投影放在既有wms_envelope.py，复用原适配器分类，不新建可靠层查询API。正式说明见docs/integrations/outbound-results.md，AGENTS／原集成说明及两项已实现TODO同步更新。

| 验收 | 实际结果 |
| --- | --- |
| RED→GREEN | WMS初始12例因缺get_result失败；Transport／前序初始21例因缺接口失败，修复后通过；BIN_EXCHANGE冻结合同另有失败复现及修复 |
| 新增契约与恢复验收 | 48项本地读模型／闭合／前序用例通过；7项独立PostgreSQL快照／消费恢复用例通过 |
| Task 0相关回归 | 独立PostgreSQL：768 passed、4 skipped；跳过不计作通过 |
| 全库测试 | 未配置外部测试库：1966 passed、700 skipped；独立PostgreSQL专项证据另列，不把全库跳过解释为并发验证 |
| 静态检查 | 全库ruff check通过；app／tests／新增读契约及本计划format检查通过；git diff --check通过 |
| 全库格式限制 | ruff format --check .仍报告用户已暂存SPEC的三处既有Python示例排版差异；已对暂存原文重现，未更改该SPEC或暂存区 |
| 完成复核 | fresh-context只读最终复核未发现高置信度产品正确性问题；PLAN complete措辞按SPEC澄清为分类完整（允许OPEN），全闭合仍需unresolved为空，并补OPEN断言 |

PostgreSQL使用本机127.0.0.1:25439的独立postgres:17临时容器与tmpfs数据，未连接真实WMS／ECS。快照用真实合法回调建立整体仍UNKNOWN的成员投影v1→v2→v3；分批／逐个／混合读取断言准确版本、成员闭合、整任务闭合及完整ID。消费侧测试仅有测试表与事务入口，验证序列化失败新session全组重读、提交前回滚、提交后恢复、多消费者及重试耗尽仍可恢复；不据此声称生产业务已接入。

本轮未提交、推送、合并或部署；原已暂存SPEC／PLAN的index内容保持不变，实施与文档更新仅在工作区。Task 0在上述格式检查限制下完成；Task 5结果接线、Task 6真实业务门槛及其他业务计划仍需后续授权，不自动开始。


## Task 0评审修复与再次复核（2026-10-27）

本轮仅修复本次评审的P1／P2，不实施Tasks 1–6或真实业务。原始已暂存实现／文档保持在index，本轮修复只写工作区。

- P1：有效409／422拒绝、无已应用结果时，同任务冻结成员的SOURCE_PICKED=true或与冻结目标相符的TARGET_PLACED使闭合返回UNDETERMINED／INVALID_EVIDENCE；原拒绝与历史里程碑保留。原POSITION_UNKNOWN只记诊断，不误认为已存执行证据；后续有效已应用结果仍优先，解除冲突不清历史。新回归先复现4个错误CLOSED，再修至6个拒绝／里程碑／诊断对照组合通过，同时覆盖货架、料箱前序与已应用结果收敛。
- P2：新增两个独立PostgreSQL参数场景，实际读取两个前序的v1并保存全组消费版本、暂存Transport任务及Outbox行后，再由并发guard更新强制触发真实40001。失败事务退出后核对动作／版本、任务和消息均已回滚；新session完整重读两个前序均为v2。更新UNKNOWN时不产生意图，仍闭合时仅提交一条意图，重复唤醒不重复；原仅在读取前冲突的测试继续保留，覆盖不同阶段。
- 验证：相关模块独立PostgreSQL回归776 passed／4 skipped；全库本地回归1972 passed／702 skipped。全库ruff check、本轮代码／读契约／PLAN格式检查与git diff --check通过。既有SPEC三处Python示例排版差异未扩大处理，不能称全库format已通过。
- 再次复核：fresh-context只读审查未发现高置信度产品正确性问题或必须补充的测试空洞；父代理再核对有效事实识别、已应用结果优先、失败时点、准确版本及动作／Outbox回滚断言，P1／P2关闭。测试侧guard和消费版本字段只存在于独立测试表，不是产品迁移或通用推进机制。

验证仅使用内存／本机临时PostgreSQL与进程内回调，不连接真实WMS／ECS。未提交、推送、合并或部署；未自动开始后续任务。


## Task 0临时测试表生命周期修复与复核（2026-10-27）

本轮仅修复测试夹具，不改产品代码、产品表定义或迁移。action_factory委托同一测试上下文：进入时只删除并按当前定义重建outbound_result_test_action，测试体结束后在finally中仅删除该表。通过yield保持依赖引擎在清理完成前可用，不使用Base.metadata或全schema清理，也不修改其他测试表／产品表。

- 回归先以原create_all行为复现3项失败：旧表缺consumed_versions，以及正常／异常退出后动作表仍存在；修复后，SQLite与PostgreSQL均验证旧结构／旧记录被清除、新字段可读写、连续两次进入从空表开始、正常与抛错后均无动作表残留，且同库无关哨兵表和数据保持。
- 同一本机独立PostgreSQL库连续两轮运行完整test_result_consumption.py，每轮15 passed。第二轮前通过独立连接预置旧表和legacy-run旧记录，实际action_factory恢复正常，运行后动作表／哨兵表残留数为0；除测试表外的列结构指纹前后一致。
- 全库本地回归1975 passed／705 skipped；全库ruff check、本次测试文件format和git diff --check通过，跳过不计作通过。不为本次夹具修复重复运行其他模块的PostgreSQL验收。
- 完成复核覆盖固定测试表作用域、创建／清理事务、正常／异常退出、依赖引擎释放顺序及连续运行隔离，未发现新的阻断问题。旧版测试表属于可丢弃测试资源，不通过产品迁移升级。

测试使用独立临时postgres:17容器及内存SQLite，无真实WMS／ECS连接。未实施后续任务，未提交或推送；原暂存区内容保持，本轮工作区仅有测试文件与本记录的修改。


## Tasks 1–5及Task 6公共接缝实施（2026-10-28）

Tasks 1–5均已按SDD完成实施、SPEC符合性和代码质量评审；Task 3分层导入遗漏、Task 6测试业务原归属／异常清理／并发快照／待办恢复及重复前序边界均有失败复现、修复和独立复审。Task 6 Steps 1–4公共接缝通过；Step 5真实业务条件未满足，Task 6整体及可发布业务交付保持未完成。生产清单为空，未开发manual-picking或coarse-sorting。开发指南见docs/integrations/business-packages.md。

阶段证据：修复前自然全仓2848 passed／29 skipped／1既有Alembic警告；最新修复后业务目录137 passed。最后整体复核及最终树全量结果将在下方补录。全库Ruff check通过；format仅原已暂存SPEC三处既有Python围栏格式差异未改。迁移仅在独立SQLite／临时PostgreSQL验证，单head为a6d464b20ab7；未迁移现有应用库。

所有新增改动保持未暂存；原暂存内容保留，无commit／push／真实WMS或ECS连接。SDD证据位于忽略目录.superpowers/sdd/2026-10-27-workline-business-foundation，因未提交而保留，不删除不可从Git恢复的评审记录。

### 最终复核与最终树验证

- 整体复核确认I1真实composition元数据导入连带ORM／engine、I2错误ENTRY_MODULES被静默忽略；已集中修复并经独立增量复审关闭。公共ECS声明导入及类型身份保留，所有入口先验证布局／归属再过滤，错误不缓存成功。
- 最终自然全仓：2913 passed、29 skipped、2 warnings，196.19s；显式临时PostgreSQL／Redis。跳过为SQLite不能验证的PG锁／Sender变体和1项未配置独立设备迁移库；对应PG变体已运行。两条warning均为既有Alembic path_separator弃用提示。
- 全库ruff check与git diff --check通过；full format非绿，仅原已暂存SPEC三处Python围栏排版差异，425文件已符合格式。原index／SPEC内容均核对未变。
- 单一Alembic head及临时库revision均为a6d464b20ab7；actions与business_seam_test_execution临时表不存在。未迁移现有应用库。
- 仍记录两个非阻断P3测试维护项：模拟未来迁移夹具仍固定前驱，下一真实迁移出现时需调整；竞争测试首holder等待及最终取消gather未覆盖所有严格有界清理路径。实际三会话锁等待证据已验证，不能扩大为所有失败路径或所有调度均有界。按SDD最终单波修复上限保留这两项，不影响当前已验证产品边界。
- Tasks 1–5完成；Task 6公共接缝完成，Step 5真实业务门槛仍未满足，整合／发布未完成。生产清单为空，无manual-picking／coarse-sorting产品业务，无真实WMS／ECS连接，无提交／推送。完整证据见.superpowers/sdd/2026-10-27-workline-business-foundation/{final-review.md,final-fix-report.md,final-rereview.md,final-pytest.log}。

### 剩余P3后续修复与复核

用户追加授权后，先前保留的两项P3现已关闭：模拟未来迁移按复制图实际head顺次追加，覆盖连续0／1／2条；竞争任务从首任务创建前纳入所有权保护，初始等待、观察异常及调用方取消均进入释放／排空，结果等待和取消清理分别以asyncio.wait设定独立期限，仅对已完成任务收集结果。延迟响应取消时明确限时报错，不声称可强杀协程。

回归先复现双head、取消排空无界等待及结果等待无法进入finally的失败；最终业务目录＋本迁移文件199passed、0skipped、6既有Alembic path_separator警告。全库Ruff及两文件格式／diff检查通过。独立复核p3-followup-rereview.md无剩余可操作问题；前文P3保留是历史状态，不再是当前未决项。

此轮只改两份测试文件和进度／验收记录，未改产品代码、产品表或迁移。没有重复全仓测试；前述2913结果为上一轮全仓历史证据。原暂存内容不变，无真实业务、真实WMS／ECS、提交或推送；Task6真实业务Step5仍未满足。新增证据保存在.superpowers/sdd/2026-10-27-workline-business-foundation/p3-followup-{report,review,rereview}.md和p3-followup-green.log/xml。
