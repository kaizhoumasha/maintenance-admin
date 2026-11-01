# sorting_task 共享任务合同与分派实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development only after separate implementation authorization. Steps use checkbox (`- [ ]`) syntax for tracking。计划编制时仅审文档；后续用户已单独授权实施ST1/ST2。当前共享组件已在隔离分支实现并验证，见[实施交付](#sorting-task-delivery)；真实宿主、正式catalog、迁移、部署及真实发送仍未交付；后续用户已另行授权整理提交并创建合入develop的PR，见[交付范围](#sorting-task-pr-delivery)。

**Goal:** 为 smt_manual 和 smt_auto 交付一份 sorting.task.* 严格合同及可信原业务分派，任务状态和推进继续归各宿主。

**Architecture:** `app/businesses/_components/sorting_task/` 只声明四个入站、两个出站 operation 及无状态薄适配。复用既有 receiver、收据、catalog、bind_callback_business、WmsRequests 和原业务通知；组件无任务表、工厂注册、设备、命令、progression 或调度器。宿主通过固定本地读口提供原任务归属及业务受理方法。

**Tech Stack:** Python 3.13+、现有 Pydantic/SQLAlchemy/FastAPI/pytest，独立 PostgreSQL；不新增依赖。

**Spec:** [SMT 手工设计 §9](../specs/2026-10-31-smt-manual-business-design.md#9-sorting_task-单份合同与分派)、[SMT 自动设计](../specs/2026-10-31-smt-auto-business-design.md)、[人工 API](../../integrations/manual-picking-api.md)、[自动 API 3 任务节](../../integrations/smt-auto-api.md#tasks)。宿主实施分别见[手工计划](2026-10-31-smt-manual-implementation.md)和[自动总计划](2026-10-31-smt-auto-implementation.md)。

## Global Constraints

- 唯一注册源为 sorting_task；四个 callback 为 issued、queue_changed、plan_delta、cancel，两个 decisions request 为 prepare、completion_confirm，均使用 sorting.task.*@v1。
- 共享 wire 不新增 business_code、任务方向选择器、外部经过号或兼容 operation；issued 的 task_type/business_type/workline_code 冻结，其余按原任务解析。
- 不建共享任务表、跨业务任务状态机、任务身份缓存或第二个归属真相源。原 task_id 必须在显式消费者范围唯一解析；缺失/歧义不广播或猜测。
- 合法各模式字段须在初始解析中保留，再按原模式/方向校验；不先按某一模式 extra=ignore 而静默丢掉另一模式已定义字段。cancel.data 严格，其他未知字段/已定义禁用字段分别沿各 API。
- 原技术身份/有效内容/响应时间保留，队列确定拒绝稳定重放与计划可重验规则分开。只有准备未定、缺前序版本、工作线停用的原未成功计划允许原消息重验；条件解除不自动应用。
- 原收据重验由receiver锁定并刷新原行后串行裁决，成功收妥不可降级；宿主不反向锁入站收据。首次正式接入前完整交付MANUAL/AUTO四分支，发布后冻结@v1有效字段及规范化语义，细则见[1.1](#sorting-task-recheck)和[1.4](#sorting-task-v1-freeze)。
- 原 receiver 事务保存原接收/业务事实/意图；纯异常回滚。正式输入提交后只唤醒原业务，组件不执行队列、WAIT 或完成推进。
- 新显式错误合同、业务 Unavailable、recheck 及可选归档检查不改变旧登记默认行为；不得将自动入库的 7 天清理政策套给手工或自动出库。
- 仅完整真实宿主就绪后进入正式 catalog；共享隔离消费者不注册为产品业务，不等待两宿主全部完成才交付首个真实消费者。

## Review Focus

- 初始解析丢掉另一模式合法字段、跨方向字段被默默接受，或后到宿主收紧@v1导致旧消息摘要变化——ST1 严格联合及冻结兼容矩阵。
- 原计划首次503/缺版409后无法重验，并发重复应用或旧失败覆盖成功——ST1重验、锁后刷新及成功不降级矩阵。
- 当前工作线改配、重复 issued、仅有 task_id 的后续通知被转给新业务——ST2 原归属矩阵。
- 两消费者同时声称同一 task_id，或原归属查询失败时继续成功受理——ST2 歧义/故障矩阵。
- 新通知错误/guard优先级污染旧接口，或ST1验收依赖尚未创建的正式入口——ST1隔离扩展/旧默认回归，ST2真实登记/原子分派。

---

## 1. 文件责任与固定本地接口

以下为待创建/修改目标，文件存在与否以实施时基线核对；不复制已由另一宿主交付的同源实现。

| 文件 | 责任 |
| --- | --- |
| app/businesses/_components/sorting_task/composition.py | ComponentDefinition：key=sorting_task、固定入口、model_modules=()/commands=()；initial_entries仅issued，requires_progression=True要求宿主推进；组件无独立progression，required_methods沿下文 |
| 同包 contracts.py | Issued、QueueChanged、PlanDelta、Cancel 的无损初始模型、按原模式/方向选择的严格资源分支及任务响应合同；仅校验，不读业务表 |
| 同包 ports.py | TaskOwnerRef 与宿主受理/归属/可选检查协议；无持久化模型 |
| 同包 service.py | SortingTask 无状态适配、可信只读归属解析和入站薄分派 |
| 同包 callback/sorting_task_{issued,queue_changed,plan_delta,cancel}_v1.py | 四份严格注册/薄入口；plan 显式登记 recheck，其他入口不借重验改判 |
| 同包 request/sorting_task_prepare_v1.py、sorting_task_completion_confirm_v1.py | Prepare、CompletionConfirm 两个 RequestPayload；只注册出站模型 |
| app/integrations/wms_callback/operation.py、registry.py、receiver.py、__init__.py | 仅必要的可选错误合同、Unavailable、原消息 recheck 和只读 archive_guard 接缝及公共导出/原包说明同步；不写 sorting 业务条件 |
| app/integrations/callback_receipts.py | ST1仅给既有收据读口增加可选锁定/刷新读取，默认行为不变；复用既有LOCK_TIMEOUT=5秒，无新列/调度器 |
| tests/businesses/sorting_task/conftest.py | 独立 MetaData、两份隔离宿主适配、固定 catalog/runtime 测试装配及 finally 清理 |
| tests/businesses/sorting_task/test_contracts.py、test_dispatch.py、test_registration.py | 严格联合、唯一来源、原归属及原子委托 |
| tests/integrations/wms_callback/test_task_contract_extensions.py | 新接缝及旧登记默认回归，不从其他测试文件导入 |

文件名花括号表示固定展开，每个目录含 __init__.py。六类模型及请求字段沿人工与自动两方向 API，本文不再发明另一份任务 wire。

本地接口在 ST2 固定，宿主按相同签名实现；这是代码接缝，不新增 HTTP：

| 接口 | 约定 |
| --- | --- |
| TaskOwnerRef | 不可变 task_id:str、business_code:str、task_type:MANUAL/AUTO、business_type:inbound/outbound、work_line_id:int、workline_code:str；来自原任务/保留终态摘要；清理后仍保留所列原模式/方向/工作线字段，business_code由原宿主固定提供，不用当前WORKLINE补值 |
| 宿主 async read_task_owner(session, *, task_id:str) -> TaskOwnerRef\|None | 同调用方 session 只读原真相源；None 仅表示该宿主不拥有，数据库故障不能吞成 None |
| 宿主 async accept_task_issued/accept_task_queue_changed/accept_task_plan_delta/accept_task_cancel(ctx, payload) -> Outcome | 在原 receiver 事务保存该宿主事实；具体状态/版本/资源/错误均由宿主决定 |
| 宿主 async recheck_task_plan(ctx, payload, original_response:Mapping) -> Outcome\|None | 只按原业务输入/阻碍及当前完整条件判断；None 复用历史响应，不转成 Accepted |
| SortingTask(business_code:str, production) | 无状态适配；映射宿主上述方法，不构造宿主，不保存 session |
| SortingTask async read_task_owner(session, *, task_id:str) -> TaskOwnerRef\|None | 薄委托原宿主读口，供固定消费者范围归属解析 |
| SortingTask async accept(ctx, payload) -> Outcome | ctx.operation 只允许四个已声明任务通知，委托对应宿主方法；无通用 execute/运行时插件注册 |
| SortingTask async recheck(ctx, payload, original_response:Mapping) -> Outcome\|None | 原 plan 的同事务薄委托；不由共享层推断能否重验 |
| SortingTask async archive_guard(ctx, payload) -> CallbackResponse\|None | 可选业务检查的薄委托；宿主无同签名 archive_guard 时返回 None，不能用当前方向或到期时间猜测已清理 |

组件 required_methods 只检查 read_task_owner、accept、recheck；无可扫描工作、无组件工厂注册。任务 callback 在 bind 前通过 catalog 已编译的消费者集合及 ctx.runtime 中对应组件读取原归属，唯一后 bind_callback_business，再 require_component；不是向每个业务执行任务处理。首次未存在 issued 按受信工作线选宿主；已存在任务先沿原归属，不按当前配置夺取历史。固定查询键为 catalog.contract_consumers(("wms_callback", ctx.operation, None))，只读这些消费者的组件；六个 entry_modules 才形成合同消费者/输入目标，initial_entries 仅标识 issued，不能拿它当后续三回调的路由全集。WMS 须保证 task_id 在这些消费者范围唯一；组件能发现已可见的歧义，但不以扫描读口宣称已提供跨两套业务表的全局唯一约束，也不新增全局锁或身份表。

<a id="sorting-task-recheck"></a>
### 1.1 公共接缝边界

callback_operation 仅增加三个可选参数：errors:CallbackErrors|None=None、recheck=None、archive_guard=None；Registered 保存同名元数据。CallbackErrors 是 operation.py 内的不可变三字段合同 invalid_data/invalid_envelope/identity_conflict，新接口固定为 INVALID_DATA/INVALID_ENVELOPE/IDEMPOTENCY_CONFLICT；None 完整保留旧路径，包括原身份冲突的 data/回显。业务 Unavailable 是 registry.py 内新增的 Outcome，返回与 raise 语义一致；固定 data={}，不能被当成 Rejected 回滚已允许的待定信息。现有 controller/receiver 入口不复制。recheck 的 exact signature 为 `async recheck(ctx, payload, original_response:Mapping) -> Outcome | None`；原响应只读，成功历史消息不送入 recheck。显式业务 Unavailable 输出 503/UNAVAILABLE/{}；仅已绑定可信原宿主且显式登记recheck的入口，允许待定输入/原503收据同事务保存，不能应用成员/版本。没有recheck或尚无可靠原归属的Unavailable，须在本次receiver保存点回滚新收据及处理写入，再返回503；不回滚调用方原有外层写入，不留下永远重放的503、不发送成功通知。下次原身份重试重新受理；系统异常仍沿原异常路径整笔回滚。receiver按这些显式元数据判断，不硬编码任务operation名称。

新错误合同明确将 DTO 失败、可识别新 operation 的非法信封及身份内容漂移分别映射 INVALID_DATA、INVALID_ENVELOPE、IDEMPOTENCY_CONFLICT；Conflict 原有引用/状态/版本理由仍由宿主给出。未知 operation、无合法身份/JSON 的400、超限413及旧登记默认不变，不为 field_path 扩公共结构。

自动入库设计已有可选只读 archive_guard 要求，扩展代码同属本共享交付，避免自动计划再做一次 receiver 改造。固定为 `async archive_guard(ctx, payload) -> CallbackResponse | None`，只返回既有 CallbackResponse 的拒绝或 None；业务负责从原摘要提供固定拒绝时间。优先级为信封/DTO及原身份比较 → 显式 guard → 历史重放/合格原 plan 重验/首次受理；不改旧成功收据。共享薄适配仅在原宿主明确提供 guard 时委托；smt_manual 不启用归档拒绝/7天清理，自动政策及摘要查询仍由自动方向实现。该扩展不可当作手工完成或共享归档引擎。

原收据可重验路径由receiver串行裁决：仅显式登记recheck且命中已有收据时，沿既有收据读口在同一接收事务对该原行FOR UPDATE，并复用callback_receipts.LOCK_TIMEOUT=5秒；原行锁持有到外层事务结束。锁后必须刷新ORM已加载状态并重新读取原摘要/响应，再按原身份比较→guard→历史重放/合格recheck判定；已成功收妥不得再次调用recheck或降级。业务是否属于三类可重验原因仍归宿主，不能凭HTTP/code授权。

既有内部读口的待实施签名固定为 `async find_receipt(session:AsyncSession, operation:str, operation_id:str, *, for_update:bool=False) -> CallbackReceipt | None`。只有本接缝显式传for_update=True时加行锁并强制刷新已加载对象；默认False保留既有调用行为。receiver必须在取得该锁后才构造传给recheck的只读original_response，不能复用锁前缓存的响应或资格判断。

固定写入锁序为原CallbackReceipt→WorkLine→实际涉及的Devices（ID顺序）→原业务动作；归属/guard只读查询不先取得宿主写锁，宿主advance不反向锁入站收据。成员/版本/意图和收据更新仍同事务。锁等待/数据库异常沿既有回滚/503路径恢复原身份，不当作可提交待定信息的业务Unavailable；无新资源锁、收据列或重试器。未登记旧接口仍走原路径。

<a id="sorting-task-validation"></a>
### 1.2 无模式字段的两阶段校验与响应合同

Registered.parse 发生在业务分派前且没有 session；因此 plan/cancel 不能在这里读取原任务，也不能直接让 Pydantic 按联合顺序选择某一模式再忽略其余字段。ST1 的初始模型显式识别全部已定义资源/选择器，严格检查公共类型/必填/空值及cancel_scope结构，保留所有已定义字段、嵌套slot_ids、数组顺序和字段在场性；真正未知字段按各API的层级策略处理：cancel.data严格拒绝未知字段，其他明确可忽略层级才忽略。任务模型自身的规范化输出不得为省略项补 null/空数组，也不得丢掉跨分支已知字段；旧登记的默认 parse 不改。

对已有任务，ST2 确定唯一 TaskOwnerRef 后以原 task_type/business_type 调用 contracts.py 的 `validate_task_branch(payload, *, task_type:str, business_type:str) -> None`，校验方向资源、首版限制和已知禁用字段；失败 raise Rejected("INVALID_DATA")，业务受理方法尚未执行。首次不存在的issued先核验受信工作线与payload模式/方向，不要求尚不存在的TaskOwnerRef；任务创建仍归宿主。共享层只校验冻结合同的字段/类型/模式方向及首版/后续报文形态；实际连续版本、原首版目标不被改写、工作线准入、成员和任务阶段由宿主校验。validate_task_branch只读初始payload，不改已冻结有效内容/摘要、数组顺序或字段在场性，不另生成一份裁剪后的任务正文。recheck 使用同一路由/分支校验，不能在无原归属时重新猜方向。归档查询失败返回503，不回退历史成功。

两个出站模块继续只注册 Prepare/CompletionConfirm；其响应有效 data 的严格模型/适配器也由共享源码提供，宿主用公开 WmsRequestResult 核对原 message_id/operation/operation_id、availability=FOUND、response_state=VALID 及精确 HTTP/code 后再解析。PrepareAccepted 仅对应202/PREPARE_ACCEPTED/data={}；CompletionDecision 对应200/DECIDED的三个分支。允许真正未知响应字段但拒绝已知分支冲突/null；STALE 大于原请求版本的关系在宿主消费时验证，不以本地最新版本替代，不把出站 RequestPayload 的 extra=forbid 批量套到响应。

<a id="sorting-task-ownership"></a>
### 1.3 共享合同与宿主行为的唯一交付归属

| 交付方 | 唯一负责 | 消费方验证边界 |
| --- | --- | --- |
| ST1 | 六类任务无损模型、validate_task_branch、响应合同及必要receiver/原收据重验保护 | 合同/规范化/未知字段/公共默认回归只在共享测试维护；自动宿主不重建DTO、解析器或receiver |
| ST2 | 原消费者/归属解析、按原TaskOwnerRef调用ST1校验、bind及单份注册/薄委托 | 四个正式callback的errors/recheck/guard登记断言归ST2；ST1只验隔离注册项，不能先验未创建的正式入口 |
| smt_auto S1（其他宿主沿各自计划） | 任务/输入/成员、队列领取、prepare消费、连续版本、工作线准入、精确取消及原plan重验资格 | 通过真实共享入口验证数据落入正确方向且拒绝无业务写入，不重复ST1字段排列组合测试 |
| smt_auto S5 | 用ST1响应合同消费原完成决定、核对原版本/资格并保存共同终态 | 解析成功不授予完成；STALE比较原请求版本，新增义务/原释放由宿主复核 |
| smt_auto I/O | 逐盘/生产架、授权/报告和方向完成资格；仅I4持有入库归档政策/摘要 | guard算法在方向业务，公共只调用；出库/人工不采用入库7天清理，公共原结果不受阻断 |

共享源码和公共回归回到ST1/ST2；宿主只补真实接入、状态/事务断言及自身worker证据，见[自动总计划补充验收](2026-10-31-smt-auto-implementation.md#integration-acceptance)。

<a id="sorting-task-v1-freeze"></a>
### 1.4 首次发布与冻结兼容

ST1在首个正式宿主接入前完整交付已约定的MANUAL/AUTO四分支；不以等待另一宿主生产流程为由省略合法字段。首次发布后，@v1有效字段、规范化/未知字段策略、在场性及已定义联合冻结；不能把原先未知字段直接定义成同一@v1的新有效字段，或用新模型重算旧有效摘要。后续新语义使用显式新合同版本，经独立设计后交付；本轮不新增版本框架或注册新版本。

历史成功/确定拒绝按原合同响应，旧待定计划也保持原有效正文/含义。ST1验证仅未知备注变化、原可选字段省略、相同旧输入在拟议新模型下摘要不同等边界：旧消息必须按已冻结版本比较，不能因原始Body不完全相同而误用新定义。候选新模型只作隔离对比，不替换或注册新的@v1模型，不为本轮创建新版本operation。后到宿主只消费同源冻结模型，不因方向接入重解析历史输入。guard仍只是既定方向政策的只读委托，不允许借后到宿主修改已发布公共规则。

<a id="sorting-task-st1"></a>
## Task ST1：严格任务合同及最小 receiver 适配

**Files:** 创建共享 contracts、两份 request 注册模块及目录；修改上述wms_callback四文件及callback_receipts.py的必要接缝/窄锁读口与公共导出；测试 test_contracts.py 和 test_task_contract_extensions.py。四个 callback 由 ST2 在可用分派接缝上完成，ST1 使用隔离接收合同验证公共扩展，不注册未完成的产品入口；正式 catalog 留首个真实宿主阶段。

**Interfaces:** 产出 Issued/QueueChanged/PlanDelta/Cancel、Prepare/CompletionConfirm、PrepareAccepted/CompletionDecision 的唯一合同、validate_task_branch签名和上述可选公共接缝。入站归属与宿主委托由 ST2 完成；本任务不能注册无处理能力的产品入口。

- [x] **步骤1：写合同 RED。** test_task_payloads_preserve_mode_fields 验 MANUAL 两方向、AUTO 两方向已定义资源在规范化后完整保留；test_plan_and_cancel_reject_cross_mode_fields 验手工入库禁直接取料/目标、自动两方向来源形态不串用、cancel 严格；test_auto_plan_delta_uses_original_direction断言原AUTO/inbound仅保留added_bin_source_racks/added_source_racks，原AUTO/outbound完整保留added_bin_source_racks/首版target_rack/added_return_racks；入库混入target_rack或added_return_racks、出库混入added_source_racks均422/INVALID_DATA，不能extra=ignore吞字段，也不靠字段猜方向。ST1直接以原模式/方向参数验证严格分支，ST2再验证真实原任务路由后调用同一校验；不在parse阶段查任务或猜方向。首版/后续可省略规则、rack_face数组/单值与rack_faces数组分别核验，正常受理ACK仍202/RECEIVED/data={}；类型/枚举/null/空数组/重复面/选择器及正整数边界按对应 API。prepare 只带 task_id/workline_code，completion_confirm 只带 task_id/last_applied_plan_revision，响应三分支互斥。test_task_normalization_preserves_absence_and_known_fields 验仅允许忽略层级的真正未知字段不入有效摘要，cancel.data未知字段仍422；新增/改变已知资源、嵌套slot_ids、timestamp或数组顺序会参与身份比较；省略不被补成null，AUTO出库取消含slot_ids、单层选择器含rack_face/cell_id不能被裁剪后扩大范围。test_task_response_contracts_ignore_only_unknown_fields 验响应额外备注可忽略，已知分支冲突/null及200/PREPARE_ACCEPTED或202/DECIDED错误组合均不作有效业务决定；宿主在公开结果非FOUND/VALID时不调用解析器补默认成功。
- [x] **步骤2：写接收 RED。** test_plan_recheck_requires_original_retry 覆盖准备未定503、缺版409、停用后原身份重试；条件解除不自动应用，业务返回None保持原响应，成功历史不重验、并发只应用一次；首次请求时间、原收据身份/创建时间保留，成功后的DUPLICATE沿成功收妥响应时间，迟到未成功响应不能覆盖成功收据。test_unavailable_keeps_only_pending_input 验业务待定信息提交、成员/意图未应用，与存储异常整笔回滚区分。test_unavailable_without_recheck_does_not_freeze_rejection验非重验入口因暂不可用而显式503时无本次已提交收据/业务写入，条件可判定后同身份可202、再重报DUPLICATE；已明确不存在的任务仍REFERENCE_CONFLICT，不泛化为503；保存点回滚不丢调用方已有外层事实，旧登记默认不变。
- [x] **步骤3：写默认及优先级 RED。** test_new_task_error_contract_keeps_legacy_defaults通过隔离注册项验证新错误口径、原回显及旧登记默认；四个真实callback元数据由ST2验，不作为ST1 GREEN前置；test_identity_conflict_precedes_opt_in_archive_guard 验内容漂移先冲突、已清理原任务固定时间拒绝、旧收据不改；test_manual_consumer_does_not_use_auto_archive_policy 验手工未提供guard不触发拒绝/清理。
- [x] **步骤4：补并发/兼容RED。** test_recheck_serializes_original_receipt、test_recheck_success_never_regresses用独立PostgreSQL受控交错验两请求竞争同一已提交的待定收据；预加载旧503/409的事务在取得锁后刷新到另一事务已提交的成功，不再调用宿主recheck、不覆盖成功；成员/版本/意图只一次，原身份/创建时间不变。test_recheck_lock_timeout_preserves_original_receipt验5秒锁等待失败回滚/503、外层回滚及原身份恢复，失败不通知、成功只外层提交通知。test_frozen_v1_effective_content_survives_unknown_notes验证1.4的旧成功/确定拒绝/待定计划：仅允许层级的未知备注变化仍等价，可选字段省略不补值；候选新模型把原未知字段纳入摘要时也不能替换冻结@v1。上述测试只用隔离注册项，不依赖ST2正式callback。
- [x] **步骤5：观察 RED。** `rtk uv run pytest tests/businesses/sorting_task/test_contracts.py tests/integrations/wms_callback/test_task_contract_extensions.py -q`。应因明确合同/行为断言失败；导入/fixture/数据库未配置不是目标 RED。
- [x] **步骤6：最小实现并 GREEN。** 原 receiver/有效摘要/收据路径内完成显式扩展及1.1锁定读口；未登记旧接口不走新回调，重验资格不从 HTTP/code 推断。重跑步骤5，并运行 `rtk uv run pytest tests/integrations/wms_callback tests/controllers/test_wms_events.py tests/businesses/test_component_dispatch.py -q`；独立PostgreSQL覆盖原并发重验/外层回滚，未配置而跳过不得标记并发GREEN。无新公共收据列、重试器或生产假注册。

<a id="sorting-task-st2"></a>
## Task ST2：可信原归属、单份注册与宿主接缝

**Files:** 创建/完成共享 composition、ports、service 和四个 callback，登记 plan 的 recheck 与四个任务通知的可选 archive_guard 薄函数；测试 test_dispatch.py、test_registration.py 与共享 conftest；必要时仅适配现有 runtime/callback 装配，保留原消费者与来源校验。

**Interfaces:** 消费 ST1 合同/receiver 接缝，产出第1节 TaskOwnerRef/SortingTask 与固定宿主协议，供手工 M1 和[自动S1共同生命周期](2026-10-31-smt-auto-implementation.md#common-tasks)实现；无任务状态存储。组件声明/装配可以在独立测试宿主下完整验证。

- [x] **步骤1：写归属 RED。** test_new_issued_uses_trusted_workline、test_existing_task_ignores_workline_rebinding 验首次受信业务/模式准入和重报原归属；test_reference_notifications_use_original_task 验 queue/plan/cancel 的最小 wire、不从箱码/任务号前缀/当前任务推断；test_ambiguous_or_unavailable_owner_never_dispatches 验零/多宿主归属、查询失败、消费者不匹配均不广播、不成功写错业务。test_task_branch_validation_follows_original_owner 验同形供箱计划仍依原模式/方向、嵌套slot_ids不丢失、跨模式字段在任何宿主业务写入前422/INVALID_DATA；三个后续callback不带新选择器也能从编译输入目标绑定。test_archive_guard_routes_before_successful_replay 验四通知重放均定位原宿主，可选guard先于成功重放，MANUAL/AUTO-outbound不采用inbound清理规则。
- [x] **步骤2：写注册/原子 RED。** test_two_consumers_share_one_task_registration 验六类operation只一份来源，两测试宿主分别受理且非消费者无权限；test_business_change_and_callback_receipt_commit_together 验委托前绑定正确、拒绝/存储故障/保存点外回滚不留半个任务或通知；test_reference_resolution_survives_receipt_retention验原任务/完整摘要在明细及公共收据清理后仍按冻结字段路由，当前工作线改配不夺取归属；缺关联不重建；重复原成功只沿历史结果。test_task_callbacks_register_shared_extensions_once在四个正式callback完成后验证唯一来源/errors，只有plan登记recheck，四通知按原宿主提供可选guard；再重跑ST1回归，不把这些正式登记断言放回ST1。
- [x] **步骤3：观察 RED。** `rtk uv run pytest tests/businesses/sorting_task/test_dispatch.py tests/businesses/sorting_task/test_registration.py -q`，两宿主表使用独立 MetaData；不存在实际自动/人工产品包不是注册占位消费者的理由。
- [x] **步骤4：实现并 GREEN。** 按编译消费者做只读原归属定位、bind_callback_business 和唯一组件委托；宿主继续拥有完整状态判断。重跑步骤3与ST1聚焦回归；确认组件无模型/迁移/scan_pending、代码不读框架消息结果表、不改原 owner。正式发布范围仅在手工 M5 或先到的真实宿主装配时增加该消费者。

## 2. 顺序、依赖及验收口径

ST1 → ST2 → 首个完整宿主正式接入。ST1仅靠隔离operation即可完成合同/receiver验收；ST2创建四个真实callback后核对errors、仅plan的recheck及可选guard登记，再回归ST1。ST1/ST2不等待真实SMT任务完成；两份独立测试宿主只证明合同/分派边界，不证明正式业务已交付。手工M1在ST2类型/协议固定后实现自己的任务真相源；自动S1复用同一交付，不能重新创建共享文件或receiver扩展。

后续调整按1.4冻结兼容规则处理：既定guard只补宿主政策接入，新字段/新语义不能原地改已发布@v1；不在方向包注册同名任务。不要求手工先实施自动逐盘流程，也不因共享合同支持 AUTO 就把未交付自动包列进 catalog。

阶段验收分为：ST1 严格合同/公共默认保持；ST2 两消费者隔离分派/原子受理；正式宿主阶段真实业务表/配置/catalog/通知；生产启用另需对端与运行环境验收。后两层不由前两层自动取得。

## 3. 自查与本轮交付

设计任务族/无损规范化/响应/错误/重验→ST1；原归属/组件唯一来源→ST2；任务状态、执行位、WAIT、完成→宿主计划；BIN_Roller 内部流程→已合并组件/B7正式接入。Review Focus 五项均有对应命名验收。类型签名沿本计划第1节，不在宿主计划再定义另一组。

计划编制及§4～§6为历史文档评审记录。随后用户明确授权实施ST1/ST2；当前勾选与证据以[§7](#sorting-task-delivery)为准。宿主任务、迁移及正式启用仍待各自阶段；整理提交及PR交付范围见[§9](#sorting-task-pr-delivery)。

## 4. 工程评审记录（2026-10-31）

> 修订前评审快照：下文原行号、ISSUES OPEN/NOT CLEARED及待采纳列表保留当时结论。本轮已将R1～R3纳入上文实施步骤，处置见§5；文档修订不代表代码、并发或外部复核已通过。

评审对象：本文第1～122行的修订计划；代码基线 `19082e3`，工作区为 `feature/smt-manual-design-alignment`，包含尚未提交的配套设计。以下是计划评审发现，不表示未来实现已经发生故障。仅追加评审记录，原任务、范围和未勾选状态保持不变；建议尚未采纳，不构成实施授权。

### 范围与复用判断

保留现有组件边界：六类合同单份注册；任务真相源、版本、队列和推进归宿主。不建议新增任务引擎、全局身份表、身份缓存、组件工厂或独立调度器。两个消费者当前均为拟议宿主，不能据此宣称已验证真实双业务复用或代码行数收益。

现有复用点已核对：`ComponentDefinition`、catalog 的 `contract_consumers` / `input_target`、`bind_callback_business` / `require_component`、receiver 收据、`WmsRequests.get_result` 和 `after_commit`。固定 operation 文件拆分符合注册规范，合并这些文件不能减少注册责任；本轮不提出目录结构调整。按明确文件清单，涉及14个产品文件（含公共导出文件，不含新增目录的初始化文件）和5个测试文件；这是共享接收路径的变更，需要保留旧登记与共享分派回归。

### Architecture：R1 [P1] 原收据重验需要明确串行化边界（confidence: 8/10）

计划依据：第93行要求“并发只应用一次”及“迟到未成功响应不能覆盖成功收据”；第71行给出同事务 recheck 接缝，但未说明已有收据从检查到更新之间的并发保护。

现有代码依据：`app/integrations/callback_receipts.py:30` 的 `find_receipt` 是普通 SELECT；`app/integrations/wms_callback/receiver.py:252` 读取后原本只重放历史，`receiver.py:137` 的写回仅以 `CallbackReceipt.id` 为条件。现有并发测试证明首次插入竞争只执行一次处理器，没有证明已提交的待定收据可安全重验。

风险场景：A、B 同时读到旧503/409，各自判断重验；A在条件尚未满足时形成未成功结果，B在条件满足后应用并提交成功，A随后写回旧结果。宿主成员或版本的唯一约束即使阻止了重复应用，也不能独自阻止公共收据倒退。此为新增路径的设计缺口，未在本轮构造或运行未来实现。

建议：ST1明确由receiver对可重验的原收据串行裁决，例如同事务锁定并重新读取原收据，再决定历史重放或调用recheck；成功后不得降级。写清与宿主锁的顺序和锁等待失败的原503恢复路径；不需要新公共列或第二个重试器。若选条件更新方案，也须覆盖业务副作用，不能只保护最终响应字段。

验证建议：独立PostgreSQL中用受控交错分别覆盖两个重验并发、旧失败晚于成功返回、锁等待失败及外层回滚；同时断言成员/版本/意图仅一次、最终收据成功、原身份与创建时间不变、成功提交才通知原业务。第93行已有行为要求，需补机制及可控交错，不增加新的业务政策。

### Code Quality / Compatibility：R2 [P2] 后续收紧同一合同须保护历史有效内容（confidence: 8/10）

计划依据：第114行允许“自动方向后续需要更严格字段/guard时，在同一已注册源码补相应真实合同与回归”；第79行要求新任务规范化保留全部已定义字段，但未限定首个宿主上线后的合同演进方式。配套API `docs/integrations/smt-auto-api.md:4067` 明确历史消息不因后续字段定义变化重算有效内容。

现有代码依据：`operation.py:54` 的 `Registered.parse` 使用当前模型的规范化结果；`receiver.py:204` / `receiver.py:210` 用当前模型计算本次有效摘要，再于 `receiver.py:260` 与旧摘要比较。`app/models/callback.py:35` 起只保存raw/effective摘要和响应，没有原解析合同版本。保持旧摘要列不变并不能保证本次比较仍使用原合同。

已执行的有界验证：仅用现有 `CallbackPayload` 与 `canonical_digest`，在内存中比较旧模型和新增字段模型。首次输入含当时被忽略的future_field；重试只改变另一个始终未知的note。旧模型下有效摘要相同，升级模型下有效摘要不同。原始摘要完全相同的快捷重放不受此例影响，但不能覆盖所有有效内容等价的重试。

建议：最小方案是在首个正式宿主接入前完整交付本文承诺的MANUAL/AUTO四分支，并明确冻结 `@v1` 的有效字段和规范化语义；后续新语义采用显式合同版本。若必须原版本演进，应先设计可恢复原解析合同的依据，而非直接替换当前模型；本轮不建议为假设需求新增通用版本框架。

验证建议：增加跨模型版本的历史重放用例，覆盖仅未知备注变化、原来未知字段后来被定义、可选字段省略以及旧待定计划重验；旧成功/确定拒绝保留原响应，旧待定输入不按新模型静默改变含义。

### Tests / Sequencing：R3 [P2] ST1 的正式callback断言依赖ST2产物（confidence: 9/10）

计划依据：第88行明确“四个callback由ST2……完成”，第94行ST1步骤3却要求“四个共享callback均显式选择CallbackErrors”，第96行要求该组测试在ST1 GREEN；第112行又固定ST1 → ST2。

影响：若断言真正检查四个正式注册项，ST1无法独立GREEN；若用隔离注册项替代，只验证公共扩展，不能证明四个正式入口均选择了新错误合同。后者会把尚未交付的产品接入报告成已验证。

建议：ST1保留隔离operation的新错误合同与旧默认回归；把“四个真实callback的errors/recheck/archive_guard登记及响应口径”断言移入ST2的 `test_registration.py` / `test_dispatch.py`。ST2再重跑ST1测试，维持现有顺序，不提前创建产品占位入口。

### 测试路径图与覆盖判断

标记：EXISTING=已有源码/测试，PLANNED=本文已要求但尚未执行，GAP=建议补充。以下为行为路径图，不把拟议测试计作现有覆盖率。

```text
WMS /api/v1/wms/events
  receive [EXISTING]
  +-- 身份/JSON/Body上限/旧登记错误与回显 [EXISTING]
  +-- Registered.parse -> 无损初始模型 [PLANNED ST1]
  |     +-- 类型/null/在场性/未知字段/已知禁用字段 [PLANNED ST1]
  |     +-- 模型演进后恢复旧有效内容 [GAP R2]
  +-- 原身份比较
  |     +-- 内容漂移 -> 409，不调用guard [PLANNED ST1]
  |     +-- 等价 -> 原归属解析 -> bind -> 分支校验 [PLANNED ST2]
  +-- 可选archive_guard
  |     +-- 已清理 -> 固定409；查询故障 -> 503 [PLANNED ST1/ST2]
  |     +-- MANUAL/AUTO-outbound -> 无inbound清理政策 [PLANNED ST2]
  +-- 历史成功/确定拒绝 -> 原响应，无业务重做 [PLANNED]
  +-- 合格待定plan -> 原消息recheck [PLANNED]
  |     +-- None/仍受阻 -> 历史响应或业务503 [PLANNED ST1]
  |     +-- 条件具备 -> 原子应用 -> 成功收据 [PLANNED ST1/宿主]
  |     +-- 并发串行化/成功不降级 [机制GAP R1；行为已PLANNED]
  +-- 首次受理 -> 原业务事实+收据+合法意图 [PLANNED ST2/宿主]
        +-- 无可信归属/无recheck的Unavailable -> receiver保存点回滚 [PLANNED ST1]
        +-- Rejected/存储异常/外层回滚 -> 不留半个业务与通知 [PLANNED]
        +-- 外层提交 -> after_commit仅唤醒原业务 [EXISTING接缝/PLANNED接入]

宿主Prepare/CompletionConfirm
  WmsRequests.get_result [EXISTING]
    -> 核对原身份/availability/response_state/HTTP/code [PLANNED]
    -> 共享响应联合/STALE原请求版本关系 [PLANNED ST1/宿主]
    -> 宿主原事务消费和推进，组件不拥有状态 [PLANNED宿主]
```

现有证据包括 `tests/integrations/wms_callback/test_operation.py` 的规范化/严格类型/结果转换，`tests/controllers/test_wms_events.py` 的首次身份并发、缺handler、拒绝回滚、提交通知，以及 `tests/businesses/test_component_dispatch.py` 的原业务绑定、共享来源校验和提交后推进。它们不能替代新recheck和guard的验收。实施回归建议同时保留上述共享分派测试；纯合同用单元测试，锁/保存点/外层提交/真实HTTP到原业务通知用集成测试。无需新增测试框架，无LLM/eval范围。

### Performance与运行边界

未发现另一个需要扩张范围的性能阻塞。按显式消费者逐个查task_id是O(C)，当前设计C=2；宿主应沿task_id唯一索引读取原表/摘要，不能遍历任务历史。无证据支持引入缓存或批量查询框架。R1锁保护限于原消息，锁等待沿已有预算失败并恢复；正式宿主仍需验证工作线与任务写入竞争。本轮未做吞吐、压测或锁等待实测。

### 依赖、处置和结论

现有交付依赖保留：ST1 → ST2 → 首个完整真实宿主；手工M1/自动S1消费ST2接口，正式catalog仅随真实宿主就绪启用。ST1内部纯合同与receiver实现可分别推进后汇合；ST2依赖其公共接口，不能用尚未注册产品包填补验收。无需等待两个宿主全部完成，也不把自动逐盘实现纳入共享交付。

R1～R3均为待采纳建议，尚未修改原实施步骤或增加已批准任务。工程评审结论：**ISSUES OPEN，建议补齐后再冻结实施计划**。架构方向可保留；R1影响新接收路径的正确性，R2影响历史消息恢复，R3影响阶段验收可信度。

验证范围：阅读当前计划、两份宿主计划、相关设计/API、catalog/runtime/receiver/事务和现有测试；执行一次无数据库的解析摘要对比。未运行pytest、RED/GREEN、迁移、真实worker或对端调用。外部独立复核不可用：Claude Code解析探针返回 `CODEX_MODE: not_installed`，未启动外部进程；当前工具不提供技能指定的Plan/TaskOutput/TaskStop回退组合，没有外部复核覆盖。

数据库语义参考：[PostgreSQL Read Committed与ON CONFLICT](https://www.postgresql.org/docs/16/transaction-iso.html)、[SQLAlchemy保存点事务](https://docs.sqlalchemy.org/en/20/orm/session_transaction.html)。这些只支持机制分析，不构成本站并发验证。

### 修订前评审状态（历史快照）

| Review | Trigger | Why | Runs | Status | Findings |
| --- | --- | --- | --- | --- | --- |
| Eng Review | plan-eng-review；用户指定本文 | 架构、兼容、测试、性能 | 1 | ISSUES OPEN (PLAN) | 3项：1 P1、2 P2；1项关键并发机制缺口 |
| Outside Review | Claude Code可用性检查 | 独立复核 | 0 completed | unavailable | CLI未找到；无外部复核覆盖 |
| CEO Review | 未执行 | 本轮未改变产品范围 | 0 | not run | 不据此给出通过结论 |
| Design Review | 不适用 | 无UI变更 | 0 | not applicable | 无 |
| DX Review | 未执行 | 本轮为后端计划评审 | 0 | not run | 无 |
| Implementation Re-review | 用户本轮要求 | 四项工程边界及WES历史案例共享切片 | 1独立复核+全仓 | CLEAR（ST1/ST2范围） | 无新增缺陷；17案例验收，4634 passed/52 skipped；真实宿主及物理未验收 |

**OUTSIDE COVERAGE:** host=codex；provider=claude-code；phase=plan-review；outside_status=unavailable。

**VERDICT:** NOT CLEARED；3项建议待处理，未批准实施；eng review required。

**UNRESOLVED DECISIONS:**
- R1：原收据重验的串行化、锁顺序和成功不降级机制尚待纳入ST1。
- R2：首个宿主上线后的合同演进与历史有效内容恢复方案尚待明确。
- R3：四个正式callback登记断言应由ST1移至ST2，待采纳。


<a id="sorting-task-review-disposition"></a>
## 5. 本轮一致性修订对照

用户已明确要求“按这三项评审建议修订……本轮只改文档，不实施代码”。R1～R3均已采纳到计划，以下为当前处置；§4保留修订前证据与结论，不把它当作修订后仍待选择的建议。

| 原发现 | 已纳入计划的位置 | 实施时必须取得的证据 |
| --- | --- | --- |
| R1 原收据重验并发 | [1.1窄行锁/刷新/锁序](#sorting-task-recheck)、ST1步骤4 | 独立PostgreSQL受控交错、5秒锁等待/外层回滚、成功不降级及唯一业务意图 |
| R2 历史有效内容 | [1.4首次发布/冻结规则](#sorting-task-v1-freeze)、ST1步骤4 | 四分支发布前完整；旧版本未知备注/省略/待定计划按原规范比较，后续语义不原地替换 |
| R3 ST1依赖未创建callback | [1.3唯一归属](#sorting-task-ownership)、ST1步骤3/ST2步骤2 | ST1隔离扩展可独立GREEN，ST2四个真实登记及原子委托后再回归ST1 |

上述处置仅使计划具备明确实施/验收归属；未运行产品测试、数据库并发、worker或外部复核，不把历史评审报告改写为已清除或授权实施。

宿主同步：[手工M1](2026-10-31-smt-manual-implementation.md#smt-manual-m1)、[自动S1](2026-10-31-smt-auto-implementation.md#common-tasks)消费同一收据保护/冻结合同，并验证自己的成员、版本及意图原子性；两份业务设计及API引用本计划1.1/1.3/1.4，不各自重写公共机制。本节为文档修订时的状态快照；后续ST1/ST2实施见[§7](#sorting-task-delivery)，宿主任务仍未实施。

<a id="sorting-task-second-review"></a>
## 6. 修订后工程复核（2026-10-31，仅文档）

复核范围为R1～R3的计划闭合及共享计划、手工/自动宿主计划、两份业务设计和两份API的对应引用。保留前轮已采纳的结构、合同与交付范围，不新增实现任务或政策。结论：**三项均在计划层面闭合，本轮未发现新的计划阻塞或跨文档矛盾。** 这是文档工程复核通过，不是实现、并发测试、对端签认或现场验收通过。

### 三项闭合证据

| 原发现 | 机制与责任 | 计划中的验证与宿主落点 | 本轮结论 |
| --- | --- | --- | --- |
| R1 收据重验并发 | [1.1](#sorting-task-recheck)明确FOR UPDATE、锁后刷新、锁后构造original_response、持锁至外层事务结束、5秒等待失败和固定锁序；宿主advance不反向锁收据，公共默认不变 | ST1步骤4的受控交错/成功不降级/超时/外层回滚及步骤6公共回归；[手工M1](2026-10-31-smt-manual-implementation.md#smt-manual-m1)步骤2b、[自动S1](2026-10-31-smt-auto-implementation.md#common-tasks)步骤2a补真实成员/版本/SupplyChange/意图原子性 | CLOSED AT PLAN LEVEL；confidence 9/10 |
| R2 历史有效内容 | [1.4](#sorting-task-v1-freeze)明确首个宿主前四分支齐备，发布后冻结@v1字段/规范化/未知字段/在场性，新语义另行设计显式版本；候选新模型不替换原登记，不新增版本框架 | ST1步骤4覆盖未知备注、可选字段省略、旧成功/确定拒绝/待定输入；两宿主约束均要求消费原@v1，不再重解析历史输入；[API字段策略](../../integrations/smt-auto-api.md#task-field-policy)保持一致 | CLOSED AT PLAN LEVEL；confidence 9/10 |
| R3 ST1反向依赖ST2 | [1.3](#sorting-task-ownership)、ST1步骤3和第2节均明确ST1用隔离operation独立验收；四个真实callback由ST2创建并验errors、仅plan的recheck及可选guard，然后回归ST1 | ST2步骤2的test_task_callbacks_register_shared_extensions_once；两宿主计划与设计均引用相同分工，M1/S1只补宿主接入，不提前承担登记断言 | CLOSED AT PLAN LEVEL；confidence 9/10 |

### 架构、质量、测试与性能复核

- 架构：无新增问题。共享层仍无任务状态/调度器/全局身份表；收据保护归receiver，业务资格与原子写入归宿主。只读归属解析不授予业务推进权限，归档政策仅沿原AUTO/inbound委托。
- 合同与组织：无新增问题。有效摘要与原响应的历史恢复规则、旧登记默认、业务Unavailable与数据库异常路径分别清楚；R2采用冻结既有版本的最小方案，不要求先建通用版本系统。
- 测试计划：无新增缺口。关键交错、锁后刷新、成功不降级、5秒锁等待、外层回滚、未知字段兼容及正式登记均有任务和命名断言；宿主集成证明不替代ST1/ST2公共验收，公共机制也不替代真实宿主原子性。所有测试均为待实施要求，本轮未执行。
- 性能：无新增计划阻塞。锁范围为原收据，等待预算固定；消费者查询仍限显式宿主，未引入身份缓存或新调度器。吞吐、锁竞争与查询性能须在实现阶段验证，本轮不作运行性能结论。

```text
原消息再到
  -> ST1 按冻结@v1解析/比较
  -> 可重验登记命中原收据：锁定并刷新（5秒等待）
     +-- 数据库失败 -> 原异常回滚/503，不提交业务待定
     +-- 内容漂移 -> 原身份冲突，不执行guard/业务
     +-- 原归属/方向 -> ST2绑定与严格分支/可选guard
         +-- 历史成功/确定拒绝 -> 原合同重放，不重做
         +-- 合格原待定 -> 宿主判断 -> 原事务事实/版本/意图+收据
             +-- 外层回滚 -> 无业务副作用/通知
             +-- 外层提交 -> 成功不可降级，仅唤醒原业务
验收：ST1隔离公共机制 -> ST2真实登记/分派 -> M1/S1真实宿主原子性
```

图示只汇总本轮复核路径，不改变1.1的优先级：需先有DTO/身份比较才执行guard；无recheck登记的旧接口继续原路径，普通入站不因本图被要求加重验行锁。首次输入、无可信归属的Unavailable和独立归档拒绝仍沿既有计划约束。

### 宿主、设计与API一致性

| 文档 | 核对位置与结果 |
| --- | --- |
| [手工宿主计划](2026-10-31-smt-manual-implementation.md) | Global Constraints、阶段顺序、recheck协议、M1步骤2b和依赖表：锁序/冻结/登记归属一致 |
| [自动宿主计划](2026-10-31-smt-auto-implementation.md) | Global Constraints、第1～2节、S1步骤2a及补充验收：公共机制单份交付，宿主只补真实原子性，一致 |
| [手工设计§9.3](../specs/2026-10-31-smt-manual-business-design.md#93-公共重验冻结合同与阶段验收) | 指向共享1.1/1.3/1.4，无第二套收据保护或合同版本规则 |
| [自动设计§3.4](../specs/2026-10-31-smt-auto-business-design.md#34-公共重验冻结合同与阶段验收) | 同一锁序、四分支发布前置及ST1/ST2分工；无提前注册要求 |
| [人工API](../../integrations/manual-picking-api.md) | 开头共享交付说明引用三条同源规则，不新增wire字段，不宣称已注册/已验收 |
| [自动API](../../integrations/smt-auto-api.md#tasks) | 第3节、3.5.3、9.1和9.2.2分别对应交付/成功响应时间/异常恢复/冻结兼容，与共享及宿主一致 |

文档检查：对本轮开始时7份文档之间的409处本地引用逐一核对文件和锚点，均有效；全部实施复选框未勾选。前轮§4的问题、旧行号和旧结论是标明的历史快照；当前处置以§5及本节为准，不把历史快照算作重新打开的三项问题。

本轮新增问题0项、未决设计选择0项；实施顺序仍为ST1→ST2→首个完整宿主，手工M1/自动S1不重复公共实现，不要求两宿主同时交付。本轮不新增TODO或实现任务，没有新的持久规则需要记忆。外部复核本轮探针仍返回CODEX_MODE: not_installed，未启动外部进程；指定的Plan/TaskOutput/TaskStop回退组合不可用，不计独立外部覆盖。

<a id="sorting-task-delivery"></a>
## 7. ST1/ST2 实施交付（隔离分支）

当前范围：`feature/sorting-task-shared`，基于`develop@19082e3`；ST1及ST2全部步骤完成；本节记录初次实现验收时的状态，后续提交及PR交付范围见[§9](#sorting-task-pr-delivery)。原设计工作区的文档和暂存区保持原样。本节更新共享交付状态，不将后续宿主步骤勾选为完成。

- ST1：MANUAL/AUTO四分支无损合同、严格原模式校验、两份decisions请求及响应解析器；可选错误合同、Unavailable、原收据重验锁/刷新、成功不降级和只读guard。未新增依赖或收据列。
- ST2：固定ComponentDefinition、TaskOwnerRef/宿主协议、四份callback、可信消费者内原归属解析与薄委托。只有plan登记recheck；组件无任务表、命令、progression或调度器，未加入正式catalog。
- 原确定拒绝保留原响应及时间：后续出现兼容或不兼容任务不授予原消息新的受理/重验资格；可信归属检查及实际宿主guard仍执行。历史成功/待定缺失原归属、可见歧义和查询故障不借此绕过。

实际接缝补充：响应合同提供纯`parse(status_code, code, data)`类方法，宿主仍先核对原公开结果身份及FOUND/VALID。`CallbackContext.original_response`是新增可选关键字字段，提供身份比较及必要锁后刷新的深度只读原响应；guard/recheck共用该快照，不新增数据库列。原guard返回契约仍为拒绝CallbackResponse或None，普通异常传播。

### 验证与评审证据

| 检查 | 最终结果 | 证明范围 |
| --- | --- | --- |
| 共享组件/receiver聚焦回归 | 483 passed，6 skipped，22.82秒 | 合同、两隔离宿主、登记、重放、重验、事务及旧默认 |
| 全仓`uv run pytest -q -rs` | 4617 passed，52 skipped，441.15秒，退出0 | 最终修复后全树；独占PostgreSQL 16/Redis，非产品存储 |
| Ruff / format / diff | 全部通过；577文件格式已符合 | 未用格式化改写无关文件 |
| 源码/原工作区核对 | 全套前后463份app/tests Python文件摘要一致；原暂存区摘要未变，原产品代码无差异 | 测试结果对应最终代码，保留用户原工作 |
| ST1/ST2独立任务评审 | 规格及质量通过 | ST1-M1在ST2补齐；ST2-M1在最终修复闭合 |
| 最终整体评审及一次定向复审 | FR-I1历史拒绝改判、FR-M1夹具部分初始化清理均已修复；无新增问题 | 全部发现已闭合，不代替真实宿主验收 |

52项跳过分为：46项SQLite不支持的PostgreSQL并发/锁语义镜像（对应PostgreSQL路径已运行）、1项原有SQLite调用方在原生BEGIN前创建根SAVEPOINT的提前提交限制、5项需要额外独占迁移数据库的既有迁移用例。未将缺失本任务PostgreSQL/Redis配置的跳过计作通过；本轮无迁移变更。

SQLite限制已对基线HEAD复现：receiver无法先于调用方已执行的SQL补原生事务。新增receiver自身保存点路径已作窄BEGIN保护；SQLite先有真实外层写入和PostgreSQL新调用方保存点的回滚路径均通过。不改全局引擎来掩盖该既有边界。

本地RED/GREEN、任务评审、最终修复/复审及完整日志保留在本工作树`.superpowers/sdd/2026-10-31-sorting-task-implementation/`，便于未提交期间复核。专用测试容器用后移除，证据和工作树保留。

### 后续边界

下一阶段由手工M1/自动S1等真实宿主实现原任务/终态摘要、不可变属性、准入、连续版本、成员、精确重验资格和业务事务；宿主继续核对出站原结果及完成资格。WMS负责跨宿主task_id唯一性，组件只拒绝已可见歧义。首个完整宿主就绪后才做正式catalog接入及相应分层验收；未实施实际SMT宿主、生产表迁移、部署、真实WMS/ECS/RCS调用或现场验收。

<a id="sorting-task-rereview"></a>
## 8. ST1/ST2 实现再评审与 WES 案例验收

用户随后要求重新工程评审，并用wes_backend的BAD/GOOD CASE验收，实际缺陷直接修复。本轮复核没有发现可复现产品缺陷（Critical/Important/Minor均0），产品源码未改；新增历史案例测试与带来源摘要的最小样本。详细证据及逐项范围见[工程复核报告](../reviews/2026-10-31-sorting-task-st1-st2-review.md)。§7保留初次交付结果，最新复核以本节为准。

- 历史案例验收17 passed：六来源面及目标保真、原响应稳定重放、身份漂移冲突、工作线改配后原宿主、精确取消选择器、Unavailable保留外层事实、异常沿原身份恢复、原计划重验外层回滚、严格响应及旧operation拒绝。
- 独立复核443 passed/6 skipped；最终全仓4634 passed/52 skipped（443.65秒，退出0），Ruff/578文件格式/diff检查通过，466份最终app/tests输入摘要一致。跳过分类及PostgreSQL执行范围见报告。
- WES六箱GOOD归档未接入可执行完整回放器；BAD来源取消样本本轮只验共享选择器/分派/事务。真实成员取消、已入线箱SCAN2、SupplyChange、SCAN3/SCAN4、Return/Rack、worker恢复及物理闭环未执行，不能以这17项替代M1/S1及正式组合验收。
- 未修改wes_backend或原设计工作区，未提交、推送、部署或真实发送。专用测试资源用后清理；工作树及本地完整证据保留。

<a id="sorting-task-pr-delivery"></a>
## 9. 提交及 PR 交付范围

用户在实现及再评审完成后，单独授权整理提交、推送并创建合入`develop`的PR。本次交付包含ST1/ST2实现、公共receiver/收据必要扩展、共享/历史案例验收测试、相关宿主计划/设计/API及工程评审报告；不包含尚未实现的真实SMT宿主或正式启用。

§7～§8及评审报告保留各阶段的测试结果与未验收边界，阶段记录中的“未提交/未推送”指当时状态。提交和PR身份以Git历史及PR为准。本次未修改产品/测试源码来整理提交，沿用最终4634 passed/52 skipped对应的验收快照；另核对Ruff、格式、暂存差异及文件清单。临时SDD执行目录/原始本地日志不进入提交，维护性评审结论和验收矩阵随文档交付。

本授权不包含合并、部署、生产迁移、真实WMS/ECS/RCS请求或现场操作。真实宿主、正式catalog、worker/Beat运行恢复及完整WES BAD/GOOD CASE业务闭环仍按后续计划分别验收。

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
| --- | --- | --- | --- | --- | --- |
| Eng Review | 本轮plan-eng-review；用户指定修订后文档 | 三项闭合及跨文档一致性 | 2累计 | CLEAR (PLAN，限本轮复核范围) | R1/R2/R3均计划闭合；新增问题0，未决0 |
| Document Revision | 前轮用户明确采纳三项建议 | 文档约束及宿主引用 | 1 | DOCUMENTS REVISED；历史复核通过 | 旧评审快照保留；当前实施见§7 |
| Implementation Review | 用户后续授权ST1/ST2 | 任务规格/质量及最终整体修复 | 2任务评审+1整体+1定向复审 | CLEAR（共享范围） | 全部发现闭合；4617 passed，52 skipped；宿主/生产验收未完成 |
| Outside Review | 本轮Claude Code可用性检查 | 独立复核 | 0 completed | unavailable | CLI未找到；不计外部覆盖 |
| Implementation Re-review | 用户本轮要求 | 四项工程边界及WES历史案例共享切片 | 1独立复核+全仓 | CLEAR（ST1/ST2范围） | 无新增缺陷；17案例验收，4634 passed/52 skipped；真实宿主及物理未验收 |

**OUTSIDE COVERAGE:** host=codex；provider=claude-code；phase=plan-review；outside_status=unavailable。

**VERDICT:** 历史文档工程复核通过，三项问题及对应引用在计划层面闭合。用户随后单独授权的ST1/ST2实现、测试及独立评审已完成，当前最新复核以§8为准；正式SMT宿主、生产启用及现场验收仍未完成。

NO UNRESOLVED DECISIONS
