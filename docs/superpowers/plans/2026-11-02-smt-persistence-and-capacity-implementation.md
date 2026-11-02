# SMT / BIN_Roller 模型收敛与供架额度修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. User has authorized execution in this order; do not pause between tasks. No commit, deployment or live operation.

**Goal:** 先将 SMT/BIN_Roller 的 28 张业务表收敛为 7+10 张并保留已有数据，再实现可配置的每线 CTU01 请求额度及 CTU03 接纳后的单个补位，重新验收 KT16/KT18。

**Architecture:** 原业务函数直接推进聚合状态，本包输入/请求表保留必要明细；不增加状态机框架。两个组件分两次连续迁移完成收敛，以便每步独立验证及回滚；随后追加工作线额度配置迁移。BIN_Roller 在原 CTU01 注册事务中按原周期状态判断额度，原 CTU03 接纳只释放原周期。

**Tech Stack:** Python、SQLAlchemy、Alembic、PostgreSQL/SQLite、pytest、现有 Reliable/Transport/ECS 公共接口。

**Spec:** `docs/superpowers/specs/2026-11-02-smt-bin-roller-persistence-design.md`；用户新指令覆盖该文档“容量另行实施”的范围限制：本计划 Task 1/2 完成收敛后，Task 3 修复容量，Task 4 重新验收。输入配置与旧失败见 `docs/testing/smt-manual-kt16-kt18.md`。

## Global Constraints

- SMT 7 张、BIN_Roller 10 张活动业务表（仅计这两包，不含coarse_sorting及平台表）。sorting_task 无持久化表，职责仍为合同和分派；SMT 生产与 BIN_Roller 公共义务各自推进。
- 保持现有业务 WMS/ECS operation、wire、版本、owner、公开 ports、六类 RollerWorkRef 和原身份。允许内部 TABLES/repository/fixture 适配，不保留一套旧物理表或通用 SQL 表代理来假装完成收敛。
- 合并一对一状态，保留多条技术消息、多输入、多次请求、多段搬运、多回流机会、原计划唯一基线、当前投影历史。不得覆盖旧 operation/message/arrival/round/demand/service/feed/command/Transport ID、WAIT 截止或完成依据。
- 追加连续 Alembic revision，起点0177313880f9，保留旧迁移，不改写已合并 migration。每步升级/降级验证数据，单 head；迁移无新外部意图。Task1后22张、Task2后17张业务表。
- 保留当前按责任分类的隔离：TASK/SCAN/FEED/RETURN_BATCH/RACK_CYCLE/DRAIN为SERIALIZABLE；纯人工及SCAN2_RELEASE为RR。锁序、receiver重验保护、事务后唤醒、原消息恢复保持。
- 额度配置为 WorkLine.five_rack_request_limit: int | None，新增可省略的管理API字段；None兼容已有未配置线的行为，正整数为每线预算，KT16/KT18验收显式配置3。不能硬编码线号、把position_bindings值改为对象、固定默认3或新增额度/占位表。
- 每线预算在CTU01原动作登记时占用；原周期RELEASED才退出。CTU01未知/失败不伪造释放；CTU03有效submit接纳或原成功结果沿现有规则释放一次，不等待最终结果补位、不以重复接纳多释放。
- 原冻结资源/路由和请求保持。任何实际测试I/O只连本轮独占库/缓存及意图冻结前装配的loopback模拟端；用户源私网地址只作审计输入。真实供应商1.1/Status10s未验项不能伪称已修复。
- 保留本工作树所有既有工作、暂存区和配置，禁止git add/reset/clean/commit/push/merge/deploy。子agent不派子agent，单实现者串行修改、pytest串行。全仓最终一次，在稳定代码上运行。

## Review Focus

1. 同版本多条plan_delta、早到/重复/冲突完工：合表后仍只有正确原版本基线和原主体关联（Task1）。
2. 同码新旧回流轮、多次SCAN4及独立SCAN2释放：不得借旧Result或终态任务关闭公共义务（Task2）。
3. 具名旧ID与新物理主键排序不同：六类pending的上界/游标不能漏原工作（Task1/2）。
4. 迁移时WAIT/未决/CTU03已接纳无最终Result、多面/多段：升级降级都保留原数据与身份，无新发送（Task1/2）。
5. 每线5个需求、两线并发、重复接纳/唤醒、撤销/失败：原请求预算3、一次释放只补1，模拟peer无限制，不以分页3伪造额度（Task3/4）。

## Task 1: SMT 收敛到7表，数据迁移与行为保持

**Files:** Modify `app/businesses/smt_manual/{models,repository,task_flow,manual_bin_flow,manual_rack_flow,roller_ports,service}.py`、必要composition；Create `migrations/versions/2026-11-02_smt_manual_persistence.py`；Modify `tests/businesses/smt_manual/`、对应 `tests/helpers.py` 装配、`tests/migrations/test_smt_manual_state.py`；Create `tests/migrations/test_smt_manual_persistence.py`。

**Interfaces:**
- Consumes 当前 sorting_task/BIN_Roller 公共接口；当前28表schema head0177313880f9。
- Produces 设计第4节7个正式模型和当前等价行为；`SmtManualService`/`RollerPorts`调用合同不变；单新revision `smtmanualcompact` 接0177313880f9，物理业务表22张，后续Task2接此revision。
- 目标为 task/input/task_member/bin/rack/rack_action/request；input以(operation,operation_id)唯一，is_plan_record部分唯一；request固定五类、原尝试/当前主体指针分离。明确列与CHECK约束，不开发字段可配置的通用引擎/表代理。

- [x] 先运行原SMT行为测试作局部基线；新增有数据迁移失败用例，升级新head前/后逐项核对计划唯一原记录、早到架完工、箱REVIEW、重复通知、WAIT/完成确认依据及message/command/Transport引用；观察目标表缺失/新revision缺失的预期RED。
- [x] 按设计第4节迁移13→7。TaskInput+TaskPlan同消息只合成一条input，其他同版消息保留；保留原来源状态区分，输入和请求原快照不从当前值伪造。具名业务函数直接读写新模型，删除被替代的旧模型和旧表使用，更新原测试装配。
- [x] 追加升级/降级实现，原13表数据双向可逆，无新external intent；对孤儿/冲突失败回滚。兼容空库及SQLite/PostgreSQL，不改旧revision。历史migration测试按指定revision验证原模型，当前head按新模型验证，不能减弱行为/约束断言。
- [x] 跑SMT行为、sorting_task路由/接收重验、workline装配与SMT迁移测试；记录精确命令、RED/GREEN及skip原因。重启/重复/并发保持原任务和确认basis；保留当前6种组件ref。
- [x] 自审本任务增量，报告原/新行与ID对应、全部未决责任、当前局限，交由任务review。不得提交或运行全仓、不得改BIN_Roller模型或容量行为。

## Task 2: BIN_Roller 收敛到10表，保持供箱/回流原语义

**Files:** Modify `app/businesses/_components/bin_roller/{models,repository,service,scan_flow,feed_flow,rack_flow,return_flow}.py`；Create `migrations/versions/2026-11-02_bin_roller_persistence.py`；Modify `tests/businesses/bin_roller/`、SMT/共享fixture的组件装配、`tests/helpers.py`必要部分、`tests/migrations/test_bin_roller_state.py`；Create `tests/migrations/test_bin_roller_persistence.py`。

**Interfaces:**
- Consumes Task1公开宿主/7表和revision smtmanualcompact。
- Produces 设计第5节10个正式模型、原BIN_Roller公共ports和六种RollerWorkRef不变；新revision `binrollercompact` 接smtmanualcompact，总活动业务表17张。Task3沿此模型统计原周期额度。
- scan_arrival含具名release与round身份及独立状态轴；face_work以(purpose,id)区分SUPPLY/DRAIN，保留demand_id/service_id/feed_id；decision以(kind,id)保留drain/departure身份，(kind,operation_id)唯一而非跨两种operation强加全局唯一；service_id/feed_id等原同一表的主键唯一性保留。

- [x] 新增有数据迁移及恢复RED：同经过多fact、多个SCAN4、旧新round、原释放pending、未选周期/撤销后仍有责任的需求、多面A→B→A、多段/回流机会、WAIT决策及CTU03接纳无最终结果。确认新schema/revision尚不存在。
- [x] 按设计第5/6节实现15→10，保留原具名ID与查找/引用；原round ID仅在SCAN3根行上唯一，多个SCAN4经过仍可关联同一round，不能对所有arrival的round关联列建全局唯一约束；普通需求在实际开始前不能伪造service/feed；feed_complete/pending由明确业务状态投影。原六种pending ref使用各自原ID取上界/游标；没有旧表影子或泛化代理层。
- [x] 追加可逆迁移binrollercompact，从原revision正向构造15表schema基准；区分SQL NULL/显式JSON null，保留原数据、顺序、作用域、所有冻结意图与原结果引用。合并前验证基数，异常不删行/不任选；down后可回原15表，单head。
- [x] 跑完整BIN_Roller行为、SMT跨边界/供箱/回流/事务/pending/recovery、migration测试；原SCAN2独立释放和SCAN4资格/FIFO负向断言保留；FIFO继续按首次SCAN4到位的received_at/id排序，补SCAN3与SCAN4到位顺序相反的反例；当前物理成员仍取最近已受理有效SCAN3（即使新经过尚未绑定round），不能只在已有round根中选最新而让旧轮继续取得资格；此步不改变CTU01额度，旧专项容量仍应失败。
- [x] 自审并报告新旧身份/恢复/迁移结果，交任务review；不提交、不跑全仓、不实现Task3预算。

## Task 3: 每线CTU01预算与CTU03接纳补位

**Files:** Modify `app/models/workline.py`、`app/schemas/workline.py`、`app/services/workline_service.py`、`app/businesses/_components/bin_roller/{rack_flow,service}.py`及必要资源读取；Create `migrations/versions/2026-11-02_five_rack_request_limit.py`、`tests/businesses/bin_roller/test_request_capacity.py`；Modify 工作线service/controller/schema tests及两线集成回归、`tests/tasks/test_smt_manual_worker.py`（若需验证自动接纳唤醒）。

**Interfaces:**
- Consumes Task2原rack_cycle/rack_action及CTU03有效submit释放事实，原workline锁、公开Transport结果读口及唤醒。
- Produces WorkLine.five_rack_request_limit 可空正整数（SQL Integer范围1..2147483647），在WorkLineEditable/Create/Update/Read及现有CRUD/snapshot中同步；创建省略为None、更新省略保留旧值、显式null清除配置，维持version校验/递增和审计；新revision `fiveracklimit` 接binrollercompact，无新表。
- 原 `advance_cycle(roller, session, cycle_id)`/`rack_action(...)` 签名保持。注册原CTU01前查询同owner/line尚未RELEASED且已有原CTU01的周期数，原周期已有动作直接复用、不重复占用；新准入读取当前工作线额度，不从旧cycle.resources冻结快照取过期额度或重写原资源；预算已满只保留待办，不创建Transport。采用现有线锁/Serializable事务保证原子性，不维护第二份计数器。
- 普通和drain都请求FIVE_RACK，均计入预算。预算变更只影响下一次新准入，不取消/改写已冻结动作。配置None兼容旧行为；降低到低于已占用值时等待自然释放，不强行撤回。
- 复查修订：尚无任何 rack_action、feed 或 release_basis 的原候选，已撤销服务记为 service_state=WITHDRAWN；保留原周期、需求、服务身份及关联，有效兄弟面可继续原周期。该服务不得因兄弟面后来登记 CTU01 而追溯为已开始。仅扩展现有状态/CHECK，不新增表或字段。fiveracklimit 降级若仍有非空额度或该新服务状态，应在任何 DDL 前拒绝并保留原数据；不伪造 PENDING/COMPLETED 来强行回退。

- [x] 先写/执行真实服务RED：每线发布5个不同rack，显式额度3，无限接纳peer；原scan_pending默认页100，首次CTU01应3/3，重复/并发不增。管理API拒绝0/负数/bool/字符串/超SQLInteger范围值且正确保存3；旧客户端更新省略新字段不得清掉已配置3。
- [x] 增加可空工作线字段和顺序迁移；原schema、CRUD、版本更新和资源读取同步。额度为实际配置，不固定KT16/18或mock端限流；不改原binding字典形状或业务wire。
- [x] 最小实现CTU01登记前预算判断；同一事务保留原周期/动作绑定与等待状态。原CTU03有效接纳后RELEASED不需新增动作/计数表；pending scanner/原唤醒可补一个旧等待需求，不能只让新plan才触发。
- [x] GREEN证明仅KT16一个原CTU03接纳、没有最终Result，累计CTU01变4/3；重复接纳/并发scan不再增加，下一次不同CTU03接纳才5/3。已有动作重试不占第二份；CTU01未知/失败、CTU03拒绝/未知不释放；未开始撤销不占额度、已开始撤销继续原责任（特别覆盖因额度已满而选了候选周期但尚无CTU01的撤销，不得随后偷偷提交）；同架仍有效的其他架面及其他架继续推进，不因一个未开始需求撤销而卡住；不同线/owner隔离，普通/drain合计不超额。
- [x] 验证原Transport提交接纳owner唤醒与Beat兜底沿原入口，不引入手工唤醒、反向查ID或新scheduler。跑额度/工作线API/事务/worker/迁移相关测试，自审后任务review。不提交，不提前声称源供应商合同/Status字段通过。

## Task 4: KT16/KT18重验、全仓验证及文档收口

**Files:** Create `tests/businesses/smt_manual/test_factory_profile.py` 及必要专用fixture；Modify 两份业务设计/原实施计划、人工API及公共回流相关说明、`docs/testing/smt-manual.md`、`docs/testing/smt-manual-kt16-kt18.md`、本计划。旧专项原始证据只读保留，最终新证据属于本计划工作目录。

**Interfaces:**
- Consumes Task1～3的17表、顺序migration及新配置字段，当前正式catalog/runtime/原receivers/Sender。
- Produces可复现12项工厂配置验收（原11PASS规则＋容量修复，完整rack/位置一一映射断言），历史FAIL与本次结果分开，真实软件验证与供应商/部署/物理边界分开。

- [x] 从用户原冻结快照复用KT16/KT18：两线enabled/MANUAL/outbound，version31/7，每线6关联设备/4SCAN合同/5位置，无上游/扫码位置；源AUTO是运行模式。显式工作线额度3；源IP只作审计，新路由/loopback必须在意图前装配。
- [x] 保留原12项验收规则/负向断言及实际I/O，更新物理表查询；新增每次CTU03接纳自动补位重复/并发断言，不用mock/分页模拟预算。RETURN/TRANSFER比较完整rack_id和目标，保留不同实体架。
- [x] 串行运行专项和两包集成/迁移/路由worker相关测试，真实Result/接纳/超时身份和清理结果写入W；确认17表及单head，不访问现场。未消费源字段/供应商合同保持未验，不改成通过。
- [x] controller在源稳定且无其他pytest运行时执行全仓pytest、Ruff检查/格式检查、diff检查，并记录原始输出/退出码；各任务不重复全仓。本任务实现者只负责专项和文档，完整最终gate由controller执行。
- [x] 文档同步实际完成/未完成/表结构/管理配置示例和迁移限制，保留旧历史FAIL及4933/52旧快照，不覆盖旧证据。最终任务review＋一次全增量review，按发现修订并复核。

Task4 首次永久专项证据（2026-11-02）：永久 `test_factory_profile.py` 12 passed / 0 skipped，16.62秒，见本计划W的 `task4-factory-final2.log/.exit`；逐例 evidence/cleanup 与全部零行见 `task4-case-manifest.json`、`task4-store.log`。初始3/3，KT16首次接纳后旧等待项无需新计划补至4/3，满额追加revision2两个来源仍4/3，第二个不同原CTU03接纳后5/3；两个CTU03均无最终outcome。RETURN/TRANSFER使用不同实体架并精确断言完整rack/目标三元组。前序两包/迁移/路由/真实worker定向回归沿Task1～3原日志保留，不重复大组；Task3真实worker owner通知与故意漏通知后Beat补位证据单列，不与本轮直接service推进混称。此为最终整体修复前的专项快照；后续修复、最终全仓及文档收口均已完成，见下述最终验证记录。原11PASS/1FAIL、4933/52只作历史记录，无提交/部署。

## 最终验证记录

- 最终全仓 `final-full4-pytest.log/.exit`：**5145 passed / 47 skipped，965.32 秒，exit 0**。全仓 Ruff 通过，645 个文件格式通过；原 47 个条件跳过的逐项原因保存在 `-ra` 原始日志中。
- 唯一整体审查提出的 I1～I5 与 M1 已在同一波修复；`final-fix-review.md` 逐项 ADDRESSED。最终相关组 320 passed / 0 skipped；随后仅稳定两处 UUID 造数前置，双库 12 项及完整 104 项通过，50 个业务断言不变。
- 永久工厂配置为原 12 项加停启线 1 项，共 13 项；最终 evidence/cleanup 见 `final-full4-factory-manifest.json`，338 项清理计数均为 0。两线累计 CTU01 3/3 → 4/3 → 5/3，满额增加计划不超发，CTU03 补位不等待最终 outcome。
- 从零迁移本轮新主专库验证当前未合并 CHECK，最终只读确认单 head fiveracklimit、7+10 表、17 表零行。563 个源码/测试/配置文件无漂移，原暂存条目、cached 补丁及 HEAD 不变；用户工作区和证据保留，不提交、不部署。
- 首轮独占迁移库误共用、修复前快照及 full3 的测试前置随机排序失败均保留原日志；完整过程和原延期 N1/N2、供应商未验边界见[软件验收记录](../../testing/smt-manual.md#current-persistence-capacity)。

## 验收出口

Task1/2通过后才进入Task3；Task3通过后才跑最终专项。成功须同时具备17表及可逆数据迁移、接口/原身份与业务行为回归、KT16/KT18预算3和单次补位实测、最终全仓及静态检查。无新增通用状态机/额度实体表。专用容器仅本轮创建/清理，现有其他容器不动；原暂存条目与 cached 补丁内容保持一致；Git stat 缓存与外部 Finder 元数据变化单列核对，不回写恢复。最后只交付未提交工作区和完整证据，不提交、不部署。
