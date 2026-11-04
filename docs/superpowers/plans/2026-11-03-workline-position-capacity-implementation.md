# 工作位进场请求额度 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 用户最初授权本地实施及软件验收，后续已授权 SHIP 提交、推送和创建 PR；合并与部署未执行。

**Execution:** T1～T7 的本地实现已完成；历史软件验收快照为 5,557 passed / 47 SQLite 条件 skipped，2026-11-04 SHIP 复验为 5,558 passed / 47 SQLite 条件 skipped / 0 失败，其中 272 个迁移用例全部通过。复核发现 T1/T5/T6/T7 的五组直接验收仍缺覆盖，不能据此宣称下列验收矩阵全部完成；具体缺口和后续 review 修复见 [验收记录](../../testing/smt-manual.md#position-capacity)。以下复选项保留原实施检查步骤；结果以对应快照和日志为准。实现 `ca33056` 与文档 `048a04f` 已提交并推送，尚未合并、部署或操作运行库。

**Goal:** 将进场请求额度统一配置到工作位，让当前单个任务的五层架、取料架在额度内真实提前下发、由对端就近排队；转运架统一支持额度配置，保留每任务唯一目标架。

**Architecture:** 用最小 `WorkLinePosition` 配置表保存上限，复用 RackJob/RackAction 的原进场、离场接纳与闭合事实推导占额，不新增运行计数账。公共 RackDispatch 执行任务与工作位接纳，宿主负责原授权和候选顺序；生产始终核对原 ENTER/ROTATE 的精确架、点、面和实际结果。

**Tech Stack:** 既有 Python 3.13+、FastAPI、Pydantic、SQLAlchemy Async、Alembic、PostgreSQL/SQLite、pytest、Celery/Redis；不加依赖。

**Spec:** [修订后的工作位容量设计](../specs/2026-11-03-workline-position-capacity-design.md)。本文为已确认的修订实施依据；本地实施与软件验收结果另见 `docs/testing/smt-manual.md#position-capacity`，现场验收独立。

**Baseline:** `caca3c0bef16d6a9eff31ff7628fea0e378a05d3`，PR #52 合并代码。规划时工作区 HEAD 是 `d9a9da514f9614a972af914157f228e5b1ada85b`；核对两者 `app/`、`migrations/` 无差异，测试依据直接读取 `caca3c0`。不切换或合并当前有未提交内容的工作区。正式实施应从包含该合并的 develop 创建独立 `feature/*` 工作分支；若基线后移，先重核本文落点与迁移 head。

**Review:** 已再次直接读取 caca3c0 的宿主、公共调度、roller 与 drain 源码复核。下列修订记录实施前复核结论；后续实现与验收证据另列。

## Global Constraints

- 所有容量的运行作用域限定在单个任务内。同一分拣工作线保持至多一个 PREPARING/EXECUTING 任务；不同任务不共享余额、排队顺序或进场授权，也不混合建立现场队列。
- 配置键为 `(work_line_id, position_code)`；运行计数和接纳顺序键为 `(work_line_id, task_id, position_code)`。同任务同点跨角色别名共用一份额度。
- 保持现有任务准备时机，不在当前任务生产时提前 prepare 后续任务。转运架先统一支持额度配置，不扩展任务预备机制。
- 当前合同每任务只有一个 target_rack，capacity 即使设为 3，实际也至多使用 1；其余名额闲置，不从后继任务补齐。
- 原任务已结束后，尚未下发的候选停止新接纳，已下发义务继续按原身份结清。旧任务同点未交出的责任单独阻止新任务 ENTER，不能用新任务空计数绕过。
- “已下发”以原 Transport 已事务性登记并提交为边界，不以网络是否发送判断。已提交 outbox 但 Sender 尚未送出的请求仍占额、保留原身份；任务结束不能把它当本地空候选撤销。纯候选和有严格 LOCAL_ONLY 依据但尚未绑定 Transport 的动作才适用未提交候选规则。
- 无任务 drain 不取得任务接纳顺序，不借用当前任务 ID 或额度，保留独立原决定、单架串行、FIFO 和交出规则。
- `capacity` 严格正整数 1～2147483647，或显式 null 表示不限额；新架位省略 capacity 默认 1，缺配置行阻止新 ENTER。不限额仍受任务、同架和唯一生产点门禁约束。
- ENTER 与原 Transport 同事务登记即占 1；到位、换面不释放。DEPART 严格原接纳或可证明的 NOT_ARRIVED 才释放；未知、超时、冲突继续保留原责任。
- 继续 WorkLine → Devices → RackJobs → 宿主主体 → RackActions 锁序；SERIALIZABLE 整笔事务，40001 包括 commit 最多三次新会话重试。结果只经公共读口使用，不直接读取 ReliableMessage/TransportTask。
- 沿原 outbox、Sender、`scan_pending`、提交后通知与每 5 秒 Beat；不新增调度器、队列表、占用表、任务状态、WMS/ECS operation 或物理排队点。
- T1～T7 是同一功能的分段实现/评审单元，必须整体集成并完成最终门禁，不能把中间的配置/调用端不一致版本单独上线。提交与发布须另有用户指令。

## Review Focus

1. 已绑定 job 但没有 ENTER 的满额候选，释放或上调后仍应由原 pending 找回；不能被 unbound/target_job_id 筛选漏掉。测试归 T3。
2. 队尾 pending 不能阻止队头交出；两条真正到位事实也不能同时取得生产许可。测试归 T4。
3. 任务结束、旧尾部结果和新任务开始交错时，不得给结束任务补位、给后继预叫或改写旧责任归属。测试归 T2/T5。
4. 跨角色别名、无任务 drain、其他 owner 的同点原责任不能被各算一份额度或按 task_id 过滤隐藏。测试归 T1/T2/T5。
5. 配置变更与最后一个名额竞争、整笔回滚/40001、重启及漏通知都必须保持原身份、稳定顺序与单次释放。测试归 T6/T7。

## 改动范围与明确边界

| 层面 | 新增/修改落点 | 具体责任 |
| --- | --- | --- |
| 位置声明 | `app/businesses/definitions.py`、`_components/bin_roller/composition.py`、`smt_manual/definition.py`、`coarse_sorting/definition.py` | 显式声明 RACK_POSITION/HANDOFF_POSITION；沿 catalog 合并校验类型一致性 |
| 声明展示 | `app/schemas/business.py`、`app/controllers/businesses.py` | 位置项展示 position_type；设备项不增加无意义位置类型 |
| 配置模型/API | 新 `app/models/workline_position.py`；`app/models/{__init__,workline}.py`、`app/schemas/workline.py`、`app/services/workline_service.py` | 聚合 positions、批量读取、版本与审计、在途保护；去掉旧专用额度字段 |
| 公共调度 | `app/businesses/_components/rack_dispatch/{models,contracts,service}.py` | task 门禁、admission_order、统一占额、同架/旧责任隔离、严格释放、唯一到位持有者 |
| 宿主装配与锁集合 | `app/businesses/smt_manual/{service,repository,roller_ports,task_flow}.py` | 注入当前任务检查，按原授权顺序形成候选；收齐接纳涉及的同线 job/主体/action；保留原 prepare/执行位约束 |
| 三类架接入 | `app/businesses/_components/bin_roller/{rack_flow,service}.py`、`app/businesses/smt_manual/manual_rack_flow.py` | 两条普通供架 ENTER 路径、LOCAL_ONLY、直接取料与目标架候选重扫 |
| 生产/交出 | `manual_rack_flow.py`、核对 `manual_bin_flow.py`，以及 `bin_roller/{rack_flow,return_flow}.py` | 正常排队不算冲突；准确到位才生产；无任务 drain 不参与任务多架额度 |
| 数据库 | 新 `migrations/versions/2026-11-03_workline_position_capacity.py` | 唯一 head 后接一条迁移：配置搬迁、顺序回填、索引与旧列移除、预检/无损降级 |
| 测试基础 | `tests/helpers.py`、manual/roller conftest、rack_dispatch 存储夹具、worker 夹具 | 按新模型建表、配置/清理，复用 caca3c0 工具；不另起一套工厂模拟器 |
| 合同说明 | 工作线管理、manual-picking、workline-bin-return、业务包声明、共享调度设计、SMT 软件验收说明 | 更新当前合同；已有历史验收结果不改写成新功能通过 |

保留现有 `/api/v1/work-lines` 路径、admin 与 `asset:workline:*` 权限，不新增工作位端点；controller 只有响应/校验需要时作薄调整。WMS/ECS 载荷、模板、身份、结果接口与通用发送框架不改。管理客户端需改用 positions；本仓库未定位到实际工作线管理 UI，后续实施须盘点真实调用端，未完成前不得宣称可无缝上线。相邻 BIN_Roller 四表草案不并入本计划。

## caca3c0 复核后必须落实的边界

| 原计划遗漏/含混处 | 基线代码事实与反例 | 本次修订 |
| --- | --- | --- |
| “原 pending 重扫”未明确如何补位 | `smt_manual/service.py::_advance` 只有 TASK 调 reconcile_task；MANUAL_RACK 直接进 advance_rack，后者无 actions 时返回 WAITING_FOR_ENTRY。满额后已经绑定 job 的直接取料/目标架不能只靠扫到 job 就补位 | T3 明确 MANUAL_RACK 先重验当前任务并执行任务内接纳，再推进原 job；改 reconcile_task 的 unbound 和 target_job_id 短路 |
| 未提交候选与外部责任混为 pending | 公共 `pending_ids` 包含所有 pending job，包括没有 ENTER 的候选；一律用 pending 阻止新任务，会把已结束任务的空候选变成永久尾部 | T2/T5 分开计数、候选队头、外部责任门禁；可证明未提交的候选退出，已提交 outbox 继续占额 |
| 缺少跨 owner 和 NULL 的具体查询边界 | `RackDispatch.jobs` 的 `_jobs` 固定当前 owner，且传 task_id=None 表示“不加过滤”，不是 `IS NULL`；照搬此读口会漏掉其他 owner，或把无任务 drain 与全部任务混在一起 | 公共接纳内部对共享业务表显式查询责任；null drain 显式 IS NULL。repository 的锁集合同步扩展，不能只改计数 SQL |
| 当前任务检查容易误用于旧责任收口 | `read_current_task.is_valid` 包含 prepare_accepted、启用及业务/编码匹配；A 终态或停线后为 false。若在 advance 总入口统一 return，A 的原结果、离场 ACK 和安全退出都会停住 | 门禁只限制新授权/首次外部登记；旧已提交动作与原离场收口继续。不为旧任务生成新的生产授权 |
| drain 并非天然脱离旧额度检查 | `rack_flow.first_registration/advance_cycle` 对普通与 drain 都调用 entry_available；`advance_drain` 的 pending 检查仅限本 drain_id。删旧计数后两个不同 READY drain 决定可各发一架 | T2/T5 明确 taskful 与 taskless 分支；drain 不用任务数值额度，但同点所有 drain 原决定合计最多一个有效进场责任 |
| 闭合后的旧点位事实仍可能误挡新流程 | `evaluate_roller_point` 根据 position_fact 的 is_current/source_id 判断冲突；只让公共计数忽略已闭 job，仍可能被旧架最后一次点位投影挡住 | T4 将投影来源关联回原 job/action 闭合依据；有效交出后的旧投影只保留历史含义，新/无归属未决事实仍阻断 |

### 三种事实必须分开处理

| 原 job 状态 | 占任务额度 | 是否作为旧任务/无任务同点外部责任阻断 | 结束/恢复动作 |
| --- | --- | --- | --- |
| 纯候选，或严格 LOCAL_ONLY 且无 Transport/位置/其他已分配执行责任 | 否 | 否；不因为 pending 就占点。原授权未知可阻断本任务候选顺序，但不能冒充已到场责任 | 当前任务内保留补位；确定撤销/任务终态后用原事实安全关闭，保留原关联 |
| 已提交的原 ENTER/Transport，含 outbox 尚未网络发送、已接纳、在途、到位、换面或结果未知 | 是，始终计入原 task_id；drain 不进入任务额度 | 是；即使原父任务已终态。其他任务按自身空计数也不能绕过 | 继续原发送、结果、回流/交出义务；不改绑到新任务、不重新申请 ENTER |
| 已按原 DEPARTURE_ACK/可靠 NOT_ARRIVED 等严格依据闭合 | 否 | 否；普通最终回调尚未到达不恢复旧责任 | 原 Transport 保留备查；迟到冲突的新证据仍按原安全规则处理，不把旧缓存当新占点事实 |

分类不是只看 actions 是否为空：出现未证明的动作、位置或已分配执行责任时不能判为纯候选，也不能因 task_id 缺失切入 drain。公共任务计数、候选队头选择与原责任隔离是三种查询，不能用同一个 `pending=True` 列表代替。

## 实施顺序

依赖：T1 配置与迁移 → T2 公共接纳 → T3 三类架接入 → T4 唯一生产点 → T5 任务/drain 边界 → T6 并发 → T7 恢复与总验收。每项先补行为测试、确认红灯，再实施并运行相应专项；不写仅验证内部字段形状或迁移过渡代码存在的测试。

### T1：工作位配置、资源类型与保真迁移

**Files:** 上表位置声明/展示、配置模型/API、数据库；`app/businesses/catalog.py::_effective/effective_definition` 沿现有完整声明比较覆盖新增类型，不另造注册机制。测试使用 `tests/businesses/test_{definitions,components}.py`、`tests/{services,controllers}/test_workline_business.py`，新建 `tests/migrations/test_workline_position_capacity.py`。

**Interfaces:**
- 新 `PositionSlot.position_type: Literal["RACK_POSITION", "HANDOFF_POSITION"]` 为必填关键字；正式声明及直接构造测试全部补齐，不按 slot_key 前缀推断。现有 dataclass 不可变/仅标准库约束保留。
- `WorkLinePosition(id, work_line_id, position_code, capacity)`；唯一 `(work_line_id, position_code)`，数据库检查严格合法整数/null。运行历史不以该配置行作级联外键。
- `WorkLinePositionWrite(position_code: BindingCode, capacity: StrictPositiveInt | None = 1)`；`WorkLinePositionRead` 增加 id。WorkLineCreate/Update/Read 聚合 `positions`，更新以 `model_fields_set` 区分省略与完整替换。
- `read_workline/list_worklines/create_workline/update_workline/delete_workline` 保持现有调用签名；positions 采用显式聚合/批量查询，不依赖异步隐式 lazy load；审计 snapshot 包含完整配置。

- [ ] 先写配置行为测试：同点别名只有一行；创建默认 1；显式 null；缺配置不等于不限额；省略 positions 保留、提交集合完整替换；非法值/重复点/交接口类型混用拒绝；version 冲突、在途解绑/删除、工作线删除保护；列表与详情一致，审计和回滚一致。数值参数至少含 `0/-1/True/1.5/"3"/2147483648`。
- [ ] 执行 `rtk uv run pytest tests/businesses/test_definitions.py tests/businesses/test_components.py tests/services/test_workline_business.py tests/controllers/test_workline_business.py -q`；确认新增用例因能力缺失失败，随后完成最小声明、模型、导出和聚合服务。
- [ ] 新建迁移前先解析唯一 head。`caca3c0` head 已核对为 `sharedrackdispatch`，新增 revision 使用独立 ID，down_revision 指向该 head；执行时若 head 变化则顺延，不编辑历史迁移。
- [ ] 在任何 DDL/数据写入前预检：同码异类、跨线同物理架位重复、非空旧限额无唯一 FIVE_RACK、冻结点归属或任务来源矛盾、无法证明原提交先后顺序。输出安全标识与原因，整体拒绝且不发网络请求。
- [ ] 创建表并搬迁：FIVE 复制旧值含 null，其他架位默认 1；同码别名去重，有 FIVE 原值时优先该唯一值；由冻结原事实可证明的旧责任点保留配置。删除旧列/旧检查约束，管理 API 不保留第二个可写来源。
- [ ] RackJob 增加 nullable BigInteger `admission_order`；约束为 null 或正数，task_id 为 null 时顺序必须 null；任务点顺序唯一索引及候选扫描索引。回填仅用同任务同点可证明的原登记/提交顺序；并列无法消歧拒绝，不能按 UUID 大小造顺序。无任务 drain 保持 null。
- [ ] 迁移测试覆盖 SQLite 与独立 PostgreSQL；原 job/action/Transport/operation、冻结 body、接纳/闭合依据逐项保真，SQLite 身份高水位不回退。降级先检查非 FIVE >1/null、多架待办、新顺序与新闭合依据能否无损表达；有损时 DDL 前拒绝。
- [ ] 执行 `rtk uv run pytest tests/migrations/test_workline_position_capacity.py tests/migrations/test_project_schema.py -q`；新增测试通过，唯一 head、失败前后结构/数据不变，方可进入 T2。迁移必须一次交付完整配置与运行字段，不通过中途部署兼容层衔接。

### T2：公共任务门禁、顺序与额度生命周期

**Files:** `rack_dispatch/{models,contracts,service}.py`、`smt_manual/{service,repository,roller_ports}.py`；新 `tests/businesses/rack_dispatch/test_capacity.py`，扩展 `test_dispatch.py`、`test_constraints.py` 和该目录 `__init__.py` 的测试存储工具。

**Interfaces:**
- `TaskAdmission` 仅为结构化只读 Protocol：`async allows_task_entry(session: AsyncSession, *, work_line_id: int, task_id: str) -> bool`。由 RollerPorts 实现，复用原 `read_current_task` 和宿主事实；true 仅证明同线唯一进行中且准备已接纳，不替代候选自身的原计划/成员授权与完整输入覆盖检查。RackDispatch 不导入 smt_manual 表，也不导入 bin_roller 的 CurrentTask。
- `RackDispatch(owner_domain, capabilities, *, task_admission: TaskAdmission | None = None)`；taskful 首次 ENTER 缺端口一律保留候选、不登记 Transport；读取历史与既有动作恢复不依赖新授权。正式宿主和需新进场的测试夹具都显式注入，不能提供“默认允许”。
- `register(session, origin: JobOrigin) -> str`、`prepare(session, *, job_id, kind, source_ref, face) -> str | None` 保持调用签名；`RackProgress` 增加 admission_order。公共内部 `_entry_available(session: AsyncSession, job: RackJob) -> bool` 同时用于首次 ENTER 与 LOCAL_ONLY 首次绑定，不能另留绕行分支。

- [ ] 先补 `test_only_current_task_can_register_new_entry`、`test_capacity_counts_bound_entries_across_source_kinds`、`test_local_only_binding_obeys_the_same_budget`：5 个合法候选、capacity=3，前三个有原 Transport，后两个没有；别名不同 source_kind 不能各发 3 个；缺配置/非当前任务/停线不可新接纳。
- [ ] 执行 `rtk uv run pytest tests/businesses/rack_dispatch/test_capacity.py -q` 确认红灯；在持有 WorkLine 及原锁集合后重验当前任务和任务终态，完整授权首次登记时一次分配同任务同点 MAX+1，重试沿用。计数只含已绑定外部进场责任的有效 job，一架多面/重试只占一次。
- [ ] 另查所有 owner 的同点旧任务/无任务未交出外部责任及同物理架责任；按上表区分纯候选与已提交责任，不能用 pending 一律挡住后继，也不能被 owner/source_kind/task_id 过滤隐藏。公共组件内部直接查询 RackJob/RackAction 的责任事实，不借当前 owner 的 jobs() 假装全量查询；排除当前 job 时要用原 ID，NULL 归属显式 IS NULL。保持已有跨线冲突规则，不实现跨线共享队列。
- [ ] 修改 `TaskRepository.lock_dispatch_line`：现有调用只锁当前 owner 的 MANUAL/ROLLER pending 集合，不足以覆盖新增责任检查。先在 WorkLine/Devices 之后定位接纳所需同线 job，按 ID 锁定，再锁本宿主可识别的原 task/主体，最后锁所需 RackActions，并复核集合。其他 owner 仅经公共 RackJob/Action 读事实与加锁，不导入或改写其宿主表；证据不足则阻断。现有 lock_jobs/lock_actions 也受 owner 过滤，不能不改锁查询就声称已锁全部责任。不能 prepare 后补取更早层级的锁，不猜配当前任务。
- [ ] 按稳定 admission_order 从“尚未绑定 ENTER Transport 的本任务候选”选队头，已接纳/闭合 job 不再占候选队头；不跳过授权 UNKNOWN 的队头。明确 WITHDRAWN/失效候选不阻挡其后合法候选。已有已绑定 ENTER 优先返回原动作，上限降低或停线不改变冻结发送/结果身份；已有 LOCAL_ONLY 尚未绑定 Transport 的 ENTER 必须按新门禁接纳。
- [ ] 接纳先判来源归属：MANUAL_RETURN/MANUAL_TRANSFER/ROLLER_ORDINARY 必须有可验证 task_id 且匹配唯一当前任务；task_id=None 只允许经原宿主证明的 ROLLER_DRAIN，该 drain 的 task_id 也必须为空。缺任务的普通作业和被错误挂入任务的 drain 都不能新接纳。公共 taskless 分支仍查缺配置、停线、同架、其他任务同点已提交责任及所有 drain 决定的同点单架责任；capacity=3/null 不放宽该串行规则。drain 原 READY/FIFO/未使用架授权由原 roller 路径在同事务重验，不在公共组件导入 roller 决定表。
- [ ] 补生命周期测试：原到位/ROTATE 不释放；原 DEPART `202/RECEIVED` 或 `200/DUPLICATE` 各释放一次；拒绝/未知 DEPART 不释放；重复 ACK 和迟到失败不重开；新空位只补同任务候选，不为原拒绝需求换号重试。
- [ ] 增加确定提交拒绝收口：只使用公开 Transport 投影严格匹配 owner、job/action、原请求与冻结内容，且 `closure=CLOSED/closure_reason=SUBMIT_REJECTION`、无不确定发送/位置里程碑/待处理结果/未闭前驱等执行责任；保存原依据为 NOT_ARRIVED。409/422 文本或单独 CLOSED 不足以释放，不满足证明的类别继续占额，不扩改通用框架。
- [ ] 执行 `rtk uv run pytest tests/businesses/rack_dispatch -q`；通过同架跨类防重、可靠 NOT_ARRIVED、未证明拒绝保留、原身份复用及所有现有安全回归。

### T3：按原任务授权形成候选，三类架统一补位

**Files:** `bin_roller/rack_flow.py`、必要时 `bin_roller/service.py`、`smt_manual/{task_flow,manual_rack_flow}.py`；`tests/businesses/bin_roller/test_request_capacity.py`、manual 的 `test_{direct_pick,transfer_rack,plans,pending}.py`，新 `tests/businesses/smt_manual/test_position_capacity.py`。

**Interfaces:** 保持 `ManualRackFlow.reconcile_task(session, *, task_id)` 与原 `register_job` 调用形式；满额时先绑定 job、action 留空，接纳后填回原关联。宿主 `SmtManualTaskFlow._register_task_rack_candidates(session: AsyncSession, *, task_id: str) -> None` 仅按原计划登记候选与关联、不发外部请求：在 plan_delta 完整保存后同事务调用，当前任务的 TASK 重扫也调用，以覆盖升级前未物化的合法需求；幂等复用原 job，终态不再物化新候选。TASK 和 MANUAL_RACK/FEED/RACK_CYCLE 重扫使用同一原候选，不新增唤醒入口。

- [ ] 先补五层架/取料架各 5 个真实授权、capacity=3 的宿主测试：登记并经 Sender 送到 loopback 的 ENTER 恰好 3 个；余下 2 个保留本地 job/成员，但没有 Transport/client_request/submit operation 身份。只数 job 或本地 Action 不算“已下发”。
- [ ] 执行上述新增用例确认红灯；移除普通供架 `entry_available` 的旧 WorkLine 专用计数，两条 ENTER 路径及 LOCAL_ONLY 都调用 T2 公共接纳；不得删除原供架/取消/FIFO 前置条件。
- [ ] 在原任务计划应用后、首次新 ENTER 前，按所有已应用 plan_revision 与现有全局 member_order 物化完整合法候选，再提交公共接纳。BIN 和 DIRECT 使用既有成员序号；同版 target_rack 没有 member_order，固定排在该版成员之前。该唯一补充次序只解决同任务同点别名混合候选，不跨任务或承诺 RCS 实际顺序；重复扫、倒序分页和 FEED/MANUAL_RACK 先后不得改变它。不得由 UUID、轮询 source_kind 或只先物化当前分页决定顺序。
- [ ] 改取料架为按不同 rack_id 形成各自 job，ACTIVE 同架同点同任务后续面复用；RELEASING 不吸收新授权。同架多个 DIRECT 成员只占一次，原成员、面和动作关联不串用。
- [ ] 目标计划先保存 `target_job_id`，接纳后保存 `target_action_id`；DIRECT 先保存 `rack_work_id`，接纳后保存对应面的 `preparation_action_id`。已有 job 但 action 空的候选必须纳入重扫，空 action 不授予生产许可。
- [ ] 明确手工补位调用链：`SmtManualService._advance` 在已有锁集合下处理 MANUAL_RACK 时，若原 job 属于当前有效任务，先调用该任务的 `reconcile_task`（复用完整原候选顺序），再对原 rack_work_id 执行 `advance_rack`；终态/非当前任务跳过新接纳，但仍执行原 advance_rack 收口。`reconcile_task` 必须重新发现已绑定 job/action 空的 DIRECT 和 target，不能保留旧 `if job or target_job_id: continue`。不得在 advance_rack 锁 action 后再补取任务前置锁。
- [ ] 明确五层架补位调用链：FEED 的 `select_supply_cycle` 对已绑定 cycle 继续返回原 ID，RACK_CYCLE 的 `advance_cycle` 对原已绑定 face/service 重进公共接纳；移除仍按 UUID 选首面的行为，按任务原成员顺序选面。taskful 首次外部绑定时再次核对当前任务、未撤销成员、pending plan/cancel 覆盖，不能只依赖 select_supply_cycle（它在已有 cycle_id 时提前返回）。LOCAL_ONLY ENTER 同样执行这些检查。
- [ ] 保留原 keyset pending 分页，不把游标改成 admission_order。该顺序只决定接纳资格：反向 ID 的候选即使先被扫描也不能插队；一个页轮结束后，已扫描候选可能在下一轮 Beat 才补位。因此验收通过完整 pending 扫描及必要的后续周期证明最终补位，不要求单次释放调用同步补齐，也不加私有补位任务。
- [ ] 补 `test_bound_candidate_is_admitted_after_release_without_new_plan`、上调补位、同版别名混合顺序、相同毫秒多 ID、撤销首候选保留合法后续面用例；均核对原身份，不修改 plan_delta 触发补位。
- [ ] TRANSFER 宿主只用本任务首版唯一 target_rack：capacity=3 仍只有一架；公共调度可用独立合法 job 测额度，但不能伪造同任务多个目标架或多个进行中任务作为宿主验收。
- [ ] 执行 `rtk uv run pytest tests/businesses/bin_roller/test_request_capacity.py tests/businesses/smt_manual/test_position_capacity.py tests/businesses/smt_manual/test_direct_pick.py tests/businesses/smt_manual/test_transfer_rack.py tests/businesses/smt_manual/test_plans.py tests/businesses/smt_manual/test_pending.py -q`；新旧身份、顺序及取消行为均通过。

### T4：队列不阻塞交出，生产仍只认唯一到位架

**Files:** `rack_dispatch/service.py`、`smt_manual/{manual_rack_flow,manual_bin_flow}.py`、`bin_roller/{rack_flow,return_flow}.py`；扩展 direct_pick/transfer_rack、BIN_Roller 的 `test_{rack_lifecycle,return_batch,production_integration}.py` 和 T3 新测试。

**Interfaces:** 复用现有 `_applied`、`has_position_conflict`、`evaluate_manual_point(...) -> tuple[PointDemand, DirectHandoff]`、target_permission/can_use_face 等生产读口；不新增物理缓存或生产授权接口。

- [ ] 先写三 ENTER 已被接纳、仅第一架精确到位的用例：只有该架/面能取投箱、直接取料或作为目标；ACK、错误架/点/面、排队记录不授予许可。再写两个未交出架被报告同点到位时全部相关新生产被阻断。
- [ ] 改 `evaluate_manual_point` 的 others 判断：本任务同点合法候选和正常在途队尾不制造 DirectHandoff UNKNOWN；同架未交出责任、实际位置冲突、未决箱盘操作、无归属与关联错误继续阻断。coverage 仍包含影响决定的原事实，不能简单删除审计覆盖。
- [ ] 公共原结果检查排除尚未交出责任的其他真正到位持有者；DEPART 已有效接纳的旧架不因历史 actual_position 保留而永久占住该点，迟到原结果也不能重新取得生产许可。
- [ ] 同时修改 `evaluate_roller_point` 对旧 `position_fact` 的解释：用 source_id 关联原 RackAction/Job 和严格交出依据，已交出原架的最后位置记录不能单独造成下一候选 handoff BLOCKED。只忽略已证明属于该闭合原责任的历史记录，保留原投影和证据；未知来源、更新未消费位置事实、其他未交出架仍阻断。不能删除整点位置事实或把离场接纳写成物理离场成功。
- [ ] 补队头无需求正常离场且队尾仍 pending 的闭环；补队尾 UNKNOWN、队头自身未知、真实同点冲突的对照用例，证明只放开合法排队。回箱机会/换面仍选择原到位服务面，不把队列尾部当可用架。
- [ ] 执行 `rtk uv run pytest tests/businesses/smt_manual/test_position_capacity.py tests/businesses/smt_manual/test_direct_pick.py tests/businesses/smt_manual/test_transfer_rack.py tests/businesses/smt_manual/test_admission.py tests/businesses/bin_roller/test_rack_lifecycle.py tests/businesses/bin_roller/test_return_batch.py tests/businesses/bin_roller/test_production_integration.py -q`，正常闭环与反例均通过。

### T5：单任务、旧尾部和无任务 drain 的完整边界

**Files:** `smt_manual/{task_flow,roller_ports,service}.py`、`bin_roller/{rack_flow,return_flow}.py` 只修改确有缺口处；测试 manual 的 `test_{tasks,task_completion,cancellation,two_worklines,roller_boundaries}.py`，roller 的 `test_{drain,fifo,cancellation}.py` 及 T3 新测试。

**Interfaces:** 原 task 执行位部分唯一索引、prepare_next、read_current_task、read_successor 不扩状态/合同；旧结果按原 job.task_id 消费，任务终态不等于其架已交出。

- [ ] 先补 A 进行中、B QUEUED，三个架位都有余额的测试：B 不 prepare、不建 ENTER；A 释放一个名额也只推进 A。数据库层第二个 PREPARING/EXECUTING 仍失败，两个不同工作线分别推进不互相阻塞。
- [ ] A 完成后，A 未提交候选不可因旧通知/Beat 进场。对无外部/位置/已分配箱盘责任的候选，以原终态/确定撤销授权构造覆盖完整的 NONE/CLEAR，复用 RackDispatch.advance 的 WITHDRAWN 收口，保留 job、成员/计划关联和已分配顺序；严格 LOCAL_ONLY 先保留原动作撤销依据。`evaluate_manual_point` 对“未提交目标候选且 target_action_id 为空”必须走候选退出判断，不能因缺 ENTER 永久返回 UNKNOWN。已提交 outbox 但未网络发送的 ENTER 不得按此撤销。
- [ ] A 已提交责任继续按原身份消费结果、原已登记箱盘义务、报告及离场 ACK；不要在 `_advance`、`advance_rack/advance_cycle` 总入口加“不是当前任务就返回”。新 ENTER、旧候选新物化、补位和新的生产授权则必须重验 A 仍是唯一有效任务；不得把 B 的当前任务状态代入 A 的需求/交出评估。B 按原时机成为唯一进行中任务后，同点 A 旧外部责任未交出仍不接纳；纯旧候选不冒充外部责任，也不把 A 恢复为进行中。
- [ ] A 原 DEPART 有效接纳后 B 才可使用该点；验证 B 的顺序从自身作用域建立，A 的迟到结果仅更新原责任，不挪用候选、改变 B 已有身份、重开 A 或预叫再后任务。
- [ ] 无任务 drain 的 task_id/admission_order 保持 null：capacity=3/null 仍只提前下发一个 drain 架，且该单架限制覆盖同点所有原 drain 决定，不只 `advance_drain` 当前 source_ref。DRAIN 和 RACK_CYCLE、LOCAL_ONLY 首绑三条推进情况都要证明不能绕过；不分配 task 顺序、不借当前任务额度、不增 taskless 多架池。
- [ ] 保留 `check_drain` 的原语义：新建 drain 决定须 successor=NONE 和可用 FIFO；已存在 READY 原决定的推进继续按其原身份、候选/FIFO/资源和责任保护重验，不能把新建条件误加到所有旧 drain 收口前。原 `summarize_drain/withdraw_unused` 只收回可证明未使用且无执行责任的候选；回箱 batch 已获 READY 并冻结成员、或 BIN_MOVE 未决的责任不可因 task_id=None、当前任务变化或容量上调被清掉。drain 决定的 READY 架清单本身不等于每架已占点，仍可按原规则收口 unused 架。任务的纯本地候选与未提交 drain 候选不能仅凭 pending 互相挡死，谁实际接纳后按原责任隔离；不新增跨任务/跨 drain 全局队列优先级。
- [ ] 改写旧 `test_ordinary_and_drain_original_requests_use_one_budget` 为“独立作用域 + 同点责任隔离”的长期行为用例，保留其 FIFO/原身份/UNKNOWN 断言；其他 owner 的旧测试不能继续断言可绕过同点未交出责任。
- [ ] 执行 `rtk uv run pytest tests/businesses/smt_manual/test_position_capacity.py tests/businesses/smt_manual/test_tasks.py tests/businesses/smt_manual/test_task_completion.py tests/businesses/smt_manual/test_two_worklines.py tests/businesses/smt_manual/test_roller_boundaries.py tests/businesses/bin_roller/test_drain.py tests/businesses/bin_roller/test_fifo.py tests/businesses/bin_roller/test_cancellation.py -q`，不靠放松索引或伪造未来计划通过。

### T6：PostgreSQL 真实并发与配置竞争

**Files:** 宿主现有加锁/重试路径（包括 `smt_manual/repository.py::lock_dispatch_line/lock_dispatch_subjects`）与 workline_service，仅修复已复现缺口；新 `tests/businesses/rack_dispatch/test_capacity_concurrency.py`，扩展 `tests/businesses/smt_manual/test_transactions.py`、`tests/services/test_binding_concurrency.py`。

**Interfaces:** 不在 RackDispatch 内开启/提交事务或另加重试；宿主收齐锁集合后调用 T2。配置更新遵守原 WorkLine/version/审计事务。

- [ ] 用 PostgreSQL 两个独立连接与 barrier 控制：capacity=1 两候选抢最后名额，仅一个原 Transport 提交；另一个重扫保留顺序。用同点跨角色再跑一次，不能靠 Python 串行调用冒充并发。
- [ ] 交错测试释放/接纳、3 降 1、1 升 3、停线/重启用、位置变更/解绑；已登记三条不取消，降额后数量降到严格小于 1 才接纳，上调无需新计划；在途重绑/删除拒绝，事务审计与原配置一致。
- [ ] 在 Transport 登记后、commit 前注入回滚/40001：新会话整笔重试，回滚的顺序/外部意图不泄漏，没有超发或双重释放；三次耗尽保留原 pending，随后正常重扫恢复。重试未提交的内部随机 ID 可重新产生，但从未发送且无第二份已提交原意图。
- [ ] 检查锁序与锁集合变动重试，验证同线串行、不同线并行；跨线同物理点重复配置在管理/迁移预检拒绝，不把 WorkLine 锁当全局物理点锁。
- [ ] 补两个独立事务分别接纳 taskful 与 taskless、两个不同 READY drain 决定的竞争：同点不可形成混合队列或两个 drain 进场。另测其他 owner 同点原责任的登记/闭合与本任务接纳交错；公共查询可见该责任、锁集合变化可重试，而本宿主不更新其他 owner 的原记录。
- [ ] 在显式隔离 `TEST_DATABASE_URL` 配置下执行 `rtk uv run pytest tests/businesses/rack_dispatch/test_capacity_concurrency.py tests/businesses/smt_manual/test_transactions.py tests/services/test_binding_concurrency.py -q`；相关 PostgreSQL 用例须 0 skipped。SQLite 通过不替代此门禁。

### T7：复用合并后的测试工具，恢复与总验收

**Files:** `tests/helpers.py`、manual/roller conftest、`tests/businesses/smt_manual/{factory_profile,test_factory_profile,test_two_worklines}.py`、`tests/tasks/test_smt_manual_worker.py`、T1/T2 存储夹具及相关文档。

**Interfaces / baseline reuse:** `caca3c0` 的 `formal_manual_fixture(factory, monkeypatch, *, line_count=2, source_profile=None, evidence_path=None)` 是唯一正式 loopback 装配；factory_profile.py 保留薄包装。数据库复用用现有 `postgres_tables(url, family, metadata)`、`state_migration`/`sqlite_migration_start`、`run_alembic`，不把旧版复制代码重新放回测试文件。

- [ ] 所有使用独立 MetaData 的存储增加 WorkLinePosition 及依赖；所有正式配置夹具显式创建架位额度，finally 先清本轮配置再删 WorkLine。formal_manual_fixture 的 source_profile、证据与 cleanup 计数同步覆盖新配置表；只能清理本轮拥有资源，禁止清空正式表。
- [ ] 全量定位旧专用字段读写，修改 `tests/helpers.py`、manual/roller conftest、test_request_capacity/test_factory_profile/test_two_worklines、worker 配置和服务/controller 当前合同测试。历史 `test_five_rack_request_limit.py` 仍用于旧 revision 往返，不为清除搜索命中删除历史事实；迁移旧列读取与历史文档也属合法保留。
- [ ] 正式 loopback 覆盖五层架 3、取料架 3 和转运架 3但仅1个原目标：通过真实 receiver → 宿主 → outbox → Sender → loopback ACK/Result，分别保存实际 submit 次数、原身份、到位面、交出依据与任务边界证据。
- [ ] 非 eager 真实 worker + 唯一队列 + 一套本轮 Beat：接纳后丢通知仍靠原周期补位；重复通知不多发；上调后无新 plan_delta 也恢复；worker 重建恢复顺序；后继任务不得被前任务余额预叫。用已绑定 job/action 空的取料架和转运架各测一次，覆盖原 MANUAL_RACK 分支，不只复测普通供架。直接手调 advance/reconcile_task 或 eager 测试不代替真实进程门禁。
- [ ] 更新现行合同说明：`docs/integrations/{device-management,workline-business-api,manual-picking-api,workline-bin-return-api,business-packages}.md`、共享调度设计及 `docs/testing/smt-manual.md`；`smt-auto-api.md` 仅同步涉及旧配置字段的段落，不扩 SMT auto 业务实现。不覆盖既有未提交草稿，需逐段合并。
- [ ] 执行下列最终检查，记录实际数量、跳过原因、commit SHA、资源清理证据；所有关键 PG/worker/迁移类别必须实际运行，未配置而 skipped 记为未验收。基线旧测试通过数不填作本功能结果。

```bash
rtk uv run ruff check .
rtk uv run ruff format --check .
rtk uv run alembic heads
rtk uv run pytest tests/businesses/rack_dispatch tests/businesses/bin_roller tests/businesses/smt_manual tests/controllers/test_workline_business.py tests/services/test_workline_business.py tests/services/test_binding_concurrency.py tests/tasks/test_smt_manual_worker.py tests/migrations/test_workline_position_capacity.py tests/migrations/test_shared_rack_dispatch.py tests/migrations/test_project_schema.py -q
rtk uv run pytest -q
```

最后两项复用同一套隔离测试配置，迁移变体另用名称含 migration 的独立测试库；实际环境变量沿现有夹具要求逐项配置，不读取运行库。全仓回归在专项通过后执行一次，只有新失败或新修改才重复。CI 保留现有 migrations/workflows/remaining 三组和总 test 门禁，新测试按目录自然归组，不扩建 CI 或增加非标准 pytest 选项/并行插件要求。

## 验收矩阵

表中“发送”均须有 loopback 收到原 submit 的证据；有本地 job 不等于发送，有 ACK 不等于到位。

| ID | 输入/交错场景 | 必须观察到的结果 | 归属 |
| --- | --- | --- | --- |
| C01 | 创建点、省略/null/上限/非法值 | 省略=1，null不限额；非法值拒绝；接口与 DB 一致 | T1 |
| C02 | 同点别名/交接口混用/多线同点 | 同任务只一份额度；混用及跨线独立重复额度被拒绝 | T1/T2 |
| C03 | 省略 positions/完整替换/空集/version | 原值保留或原子替换；绑定/责任存在不清空；旧版409 | T1 |
| C04 | 缺行、在途改码/解绑/删除、历史已闭合 | 缺行不新发；在途409；合法删除保留历史身份/冻结点 | T1/T2 |
| Q01 | 五层架5授权/容量3 | 3真实发送、2原候选，重复扫不重发 | T3/T7 |
| Q02 | 取料架5授权/容量3 | 多个不同架真实提前发送；成员/面不串用 | T3/T7 |
| Q03 | 转运架容量3/首版一目标 | 只发送该目标1架，不能从下一任务补齐 | T3/T5/T7 |
| Q04 | 绑定job、无action、释放/上调 | 经TASK/MANUAL_RACK原pending补位，无新计划，保留job/order | T3/T7 |
| Q05 | 同点混合角色、倒序分页/通知 | 任务原revision/member序保留，target同版先于成员，不被轮询插队 | T2/T3 |
| Q06 | 队头授权UNKNOWN/明确撤销 | UNKNOWN暂停新接纳；明确失效后同任务合法候选可继续 | T2/T3 |
| Q07 | 重试、多成员、多面/RELEASING新授权 | ACTIVE同架只一ENTER/一名额；RELEASING不吸收新需求 | T2/T3 |
| Q08 | 跨类别/owner同一物理架 | 未交出原责任前无第二条有效进场链 | T2/T5 |
| Q09 | 反向job ID、满额后已接纳队头/已闭合历史 | 资格按未提交候选admission_order；历史/已接纳项不堵候选队头，必要的下一周期可补位 | T2/T3 |
| Q10 | 已绑定cycle后原成员撤销或有待应用plan/cancel | 不因select_supply_cycle提前返回而绕过新ENTER授权复验 | T2/T3 |
| Q11 | 取料/转运已绑定job且action空，只投递MANUAL_RACK | 可重新接纳；不能永远WAITING_FOR_ENTRY；无需新plan或手调reconcile | T3/T7 |
| Q12 | 普通source_kind缺task_id、drain误挂task_id，或旧任务ID与当前不符 | 不误入另一接纳分支、不借当前任务资格；无新Transport | T2/T5 |
| P01 | 3接纳、1真实到位、错架/点/面结果 | 只有精确原架面生产，其他无许可 | T4 |
| P02 | 同点两架真到位、前架未交出 | 冲突阻止相关生产，不挑最新结果放行 | T4 |
| P03 | 队尾正常pending、队头已无需求 | 队头可按原事实离场，队尾不制造死锁 | T4 |
| P04 | 队头自身未知/未决箱/无归属/冲突 | 安全阻断仍在，不能以“排队”豁免 | T4 |
| L01 | ENTER到位/换面完成 | 一直占1，不补位 | T2 |
| L02 | 满额、配置未变，仅一条原DEPART获202 RECEIVED/200 DUPLICATE | 恰好释放一次，后续原pending周期最多多接纳一个同任务候选；不要求释放调用同步发送 | T2/T3 |
| L03 | DEPART拒绝、错误ACK组合、超时 | 不释放、不换号重建离场链 | T2 |
| L04 | 已接纳DEPART后迟到失败/位置事实 | 原Transport可备查，不恢复额度、不重开生产或重搬 | T2/T4 |
| L05 | 可靠未到场/严格无执行责任提交拒绝 | 保存原依据释放；没有伪造到位/离场成功 | T2 |
| L06 | 仅HTTP拒绝、delivery uncertain、缺结果、受管点冲突 | 保留责任与额度，无自动新身份 | T2 |
| L07 | 原ENTER outbox已提交但Sender未发送，此时任务终态/停线 | 仍占原任务额度、阻断同点新任务，原意图继续；不得当纯候选撤销 | T2/T5 |
| S01 | A进行中、B排队、各点有余量 | 同线至多1个PREPARING/EXECUTING；B无prepare/ENTER | T5 |
| S02 | A终态、A未提交候选收到旧通知 | 不再新ENTER；A已提交责任仍按原身份收口 | T2/T5 |
| S03 | B当前计数0、同点A旧尾部未交出 | B仍不接纳；不把A重新变成进行中 | T2/T5 |
| S04 | A交出后B启动，再来A迟到事实 | 只更新原归属；无余额转移、候选改绑、任务重开 | T5 |
| S05 | 无任务drain、多架决定、容量3/null | task/order为null，仍单架串行；不借任务额度，FIFO保留 | T5 |
| S06 | drain与task同点/不同线任务并行 | 同点原责任隔离；不同线不互相占额度 | T5/T6 |
| S07 | A终态只余未提交候选，B已有自己的合法授权 | A候选安全退出且保留关联，不因pending让B永久等旧尾部 | T2/T5 |
| S08 | A终态/B当前，A收到原到位结果或DEPART ACK | 旧事实继续消费、交出可收口；不为A新增生产或替B复用许可 | T4/T5 |
| S09 | 其他owner同点原责任；task_id=None查询 | 全部未交出外部责任可见，NULL严格分流；不改写其他owner、不计成另一份额度 | T2/T5 |
| S10 | 两个不同READY drain决定，capacity=3/null，DRAIN/RACK_CYCLE/LOCAL_ONLY | 同点合计最多1个drain有效进场，原决定与FIFO不变 | T2/T5/T6 |
| S11 | task本地候选与未提交drain候选并存；回箱最后一箱/UNKNOWN | 不因互为pending挡死；已冻结回箱分配/BIN_MOVE未决责任保留，只有可证明unused候选退出 | T5 |
| U01 | 3降1、仍有3条有效请求 | 原3条保留，计数严格小于1后才补 | T1/T6 |
| U02 | 1升3、无新计划；停线再恢复 | 只补原当前任务候选，原发送/结果仍收口 | T3/T6/T7 |
| X01 | PG双连接抢最后名额/释放与接纳交错 | 不超发、不漏释放、无双重补位 | T6 |
| X02 | 注册后回滚/commit 40001/三次耗尽 | 无未提交外发；整笔新会话重试；pending可恢复 | T6 |
| X03 | 配置更新与接纳、锁集合变化 | version/配置/审计一致，锁序保留、无死锁绕过 | T6 |
| X04 | PG同点task/drain或两个drain决定同时抢入场；其他owner责任闭合交错 | 不混合建队列、不多发drain；锁集合与全量责任查询一致 | T6 |
| R01 | 重复/漏通知、真实worker/Beat、重启 | 原身份/顺序恢复，单任务约束不变 | T7 |
| R02 | DEPART已接纳但旧position_fact仍is_current，或出现更新未消费位置事实 | 已证明交出的旧投影不永久阻塞下一架；新/不明事实仍安全阻断 | T4/T7 |
| M01 | 旧FIVE null/正数、别名、其他架位 | 保真搬迁、别名不被默认1覆盖，其他初值1 | T1 |
| M02 | 原在途/任务来源/顺序不明 | 可证明者保真回填；不明者原子拒绝，不造UUID顺序/当前task | T1 |
| M03 | 有损downgrade/SQLite高水位/双库 | DDL前拒绝；既有身份保留；SQLite与PG均证明 | T1 |
| M04 | 新配置表清理、工厂profile/worker退出 | 只清本轮资源，无残留进程/队列/配置，原数据不变 | T7 |

## 交付与验收分界

- 用户后续已授权依本计划在独立工作树实施并运行软件验收，基线保持 caca3c0；不提交、推送或部署。实际完成状态和测试证据以 `docs/testing/smt-manual.md` 的工作位容量章节为准。
- 软件验收完成条件：T1～T7 全部完成，以上矩阵有对应测试证据；实际 PostgreSQL 并发/迁移和非 eager worker/Beat 未跳过；管理调用端完成适配，API/worker 同版、迁移可证明无损。
- 对端/现场验收另外记录：WMS/RCS 是否确实接受同点多条请求并就近排队、唯一物理作业点的实际入场/到位语义和互锁。loopback 接纳与历史模拟不能代替这些事实；现场未验收则保持该项未验收，不把软件通过写成部署成功。
- 自检映射：设计第4/8节→T1；第5/6.1节→T2；第6.2/6.4节→T3/T5；第6.3节→T4；第7节→T5/T6/T7；第9/10节→范围表与验收矩阵。未扩任务预备、跨任务额度或无任务多架排队。
- 本次复核新增 Q09～Q12、L07、S07～S11、X04、R02 共 12 个反例，验收矩阵合计 50 项。以上为实施后的必测项；最初复核仅做源码对照，后续授权实施的软件证据另列，不以规划结论替代实测。
