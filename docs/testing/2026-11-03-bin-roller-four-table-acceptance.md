# BIN_Roller 四表软件验收

基线：`caca3c0bef16d6a9eff31ff7628fea0e378a05d3`。工作分支：`feature/bin-roller-four-tables`；独立工作树实现。源码已按后续 SHIP 授权提交并推送；未部署或操作运行库。旧八表数据不迁移。

状态：**本地软件实施完成，计划中的五组直接验收仍为部分覆盖**，详见下方待验组合。实现复审三项修订后的历史全仓串行回归为 5517 passed、118 skipped、零失败，包含当轮新增 40 项回归；后续 SHIP 复验另列，不覆盖历史记录。历史首轮旧终态断言失败及其修复记录保留在下表。本文件只记录本地软件验证，不证明 ECS/WMS/RCS/现场物理完成。

## 交付范围

- 四个当前检查点：bin_passage、bin_return、face_work、workline_state。原 EVENT 复用公共 callback_receipt 三个 JSON 列，原命令和搬运复用公共结果；删除私有扫描收据/位置/段/机会/批次/决定历史。
- SCAN1/2 唯一前向前驱关联；本点消重先于匹配，不等待 SCAN1 Result。歧义独立，不新增 wire。父 TASK 关闭后，新 WMS 任务可开始独立经过，原 digest/旧 Passage 不改挂。
- FaceWork 只保存当前步骤及 feed_cursor；部分 READY 使用批准前缀，尾部保持 FIFO。SCAN4 原失败合法重扫保留首次 receiver 到位顺序。
- SUPPLY 动作归 TASK，DRAIN/独立回流归 LINE；事实报告不贴可终止标签。TASK 终态同事务停发并关闭自身检查点/原 RackJob，独立回流按派发证据和原实际终位恢复资格或 WAIT_FACT。WAIT_FACT 只消费事实。
- 两类 Sender 门禁、首个 dispatch_started_at、ABANDONED；无业务状态回调、第二 Sender/重试器、额外历史/别名/scope 表。
- 清理复用原有限保存期和维护入口；当前引用、在制架使用的关闭面及未完成步骤使用的关闭回流成员受保护。

## 迁移与切换

唯一新 head 为 rollerfour：sharedrackdispatch → positioncapacity（工作位容量）→ rollerfoundation（公共增量 A）→ rollerfour（私表替换 B）。历史 revision 不修改。

A 不回填旧公共行。B 在任何旧私表删除前检查旧待办、可能发送/原搬运、活跃架作业和宿主引用；发现责任则拒绝。允许丢弃无在制责任的旧私表历史，公共及其他业务原字节/身份保留。B 新表非空拒绝降级；A 有新范围/ABANDONED/派发/事件语义时拒绝降级，均在破坏性 DDL 前预检。

旧在制运行恢复不在本次支持范围。历史迁移测试保留结构/身份/字节和有损降级断言，旧八表 runtime 续跑断言改为新四表切换拒绝；新四表行为在当前端口/正式宿主用例验收。

## 软件证据

所有 PostgreSQL/Redis 验证仅使用任务自建可丢弃实例；迁移测试与业务回归分库，最终完整测试串行执行。SQLAlchemy 模型与全新迁移 schema 比对：`alembic check` 返回 No new upgrade operations detected。

实施阶段历史软件证据（数量存在重叠，不相加）：

| 检查 | 结果及边界 |
| --- | --- |
| 复审修订后全仓串行回归（历史） | **5517 passed、118 skipped，零失败，987.79s**；包含本轮新增 40 项回归，配置可丢弃 PostgreSQL/Redis，完整执行至 worker/Beat 和维护任务 |
| 全仓串行回归（原实施首轮） | 5463 passed、118 skipped、1 failed，耗时 997.53s。唯一失败为 worker 用例仍期待已完成 TASK 的 SCAN2 release 保持 pending；按新边界改为关闭后，相关 task/worker/Beat 套件 13 passed。没有将初轮报为全绿或将 skip 计为通过 |
| 审查修复后 BIN_Roller + smt_manual + rack_dispatch 全套 | 1097 passed、3 skipped，SQLite 与可丢弃 PostgreSQL；3 个跳过为 SQLite 无法证明的并发/隔离分支，PostgreSQL 对应路径已执行 |
| 两类发送门禁 / scope-stop | 44 passed，含 PostgreSQL stop 持锁时 gate 等待、提交/回滚两种结果 |
| 历史架迁移 + 新 A/B | 73 passed；五类旧私表待办分别拒绝切换、共享数据保全、降级前拒绝等 |
| 恢复的旧 MANUAL 历史 runtime 迁移用例 | 6 passed；原绑定搬运消费、保护位置、后续成员不接入旧 fallback |
| 审查修复后事务隔离与 scope 关闭 | 31 passed（SQLite/PostgreSQL） |
| 回流生命周期/保留相关组合 | 66 passed（SQLite/PostgreSQL） |
| 最终清理专项（含真实交错） | 19 passed；清理持 WorkLine 锁时新 SCAN2 等待，提交后沿原 SCAN1 唯一关联，原事件不悬空 |
| Ruff / 格式 / diff | 全仓 check、690 文件 format --check、git diff --check 通过 |
| 结构 | 唯一 head rollerfour；全新 schema 的 alembic check：No new upgrade operations detected |
| 历史收集快照 | 5595 tests collected 为较早快照，不能作为最终总数；随后历史完整回归为 5517 + 118 = 5635 项 |

全仓回归期间完成审查收敛，随后完整复验受影响业务及 worker/新增迁移与清理用例；未为格式或无关模块反复全量执行。所有列出的测试连接均为本任务可丢弃数据库/Redis；真实本地 Celery worker/Beat 测试使用测试 HTTP 对端。

独立审查及同一审查席复核已完成，四项问题全部处理：

1. LINE 停止独立关闭原 WorklineState，清除 line_run_id；未知物理事实仍保留，新物理输入才开启新运行。
2. PASSAGE/RETURN/FACE 的宿主隔离选择恢复为 SERIALIZABLE，所有待办类型有隔离约束覆盖。
3. 被新 SCAN3 替代的旧已解决轮次在解除分配选择后以 SUPERSEDED 关闭，不脱离 pending 后永久存活；READY/NO_BATCH 和清理回归均验证。生命周期相关 66 passed。
4. 旧共享 MANUAL 搬运的原结果消费、保护位置和历史 fallback 不绑定后续成员覆盖已恢复。夹具先隔离无关旧 BIN 样本，再 A→B，保留 MANUAL/native 身份和正文；恢复用例 6 passed。

四项审查问题均已复核关闭，无遗留 Critical/Important 审查项。

## SHIP 复验与待验组合（2026-11-04）

已推送 `7169d80` 对应的运行时代码完成独立迁移库与业务库复验：迁移套件 **301 passed、0 skipped、0 failed（901.28s）**；其余套件 **5292 passed、44 skipped、0 failed（817.32s）**，合计 **5593 passed、44 skipped，共 5637 项**。44 项为 SQLite 不能证明的条件分支，不计为通过。本轮新增的两项清理保存期校验覆盖 0/-1 在访问存储前拒绝。Ruff、690 文件格式、差异检查及唯一 `rollerfour` head 通过；本次文档同步不再次全量运行测试。

计划逐项审计仍有五组直接验证未完整覆盖，现有通过结果不能代替这些组合；这表示验证缺口，未据此确认运行时缺陷：

- snapshot 门禁与 stop 的独立 PostgreSQL 会话竞争（含其额外生命周期锁）；普通消息竞争已有证据。
- 跨 owner/工作线的选面外键、当前步骤冲突、关闭行清引用等完整数据库约束组合。
- 新合格 SCAN1 与 SCAN2 选择前驱的候选集合竞争；现有等价 SCAN2 竞争不等同此项。
- 业务新意图登记与 TASK 终态关闭的双事务竞争，以及整个关闭事务注入回滚后的原子性。
- 成功切换时包含其他 owner 的 RackJob、收据、ECS/Result、Transport 等完整混合公共数据保全，以及破坏性 DDL 之后注入 PostgreSQL 失败的回滚证明。

## 与工作线容量分支的集成前提

2026-11-04 已在本分支合并容量来源 `48e6a180` 的代码并完成冲突移植，组合链为 `sharedrackdispatch → positioncapacity → rollerfoundation → rollerfour`。容量预检先读取旧 BIN 关联，B 之后不再运行容量迁移。原容量用例已移植至 SUPPLY 的 scope_id/id/rack_job_id；不能恢复旧列或兼容私表。

用户已明确选择“停投递，保留未决原架责任”。CLOSED_BY_SCOPE 禁止一切新动作，但未决原架继续 pending，保护同点/同架及引用。纯候选、严格从未派发或原严格交出接纳才解除；ABANDONED 不证明未发送。停止原因保留，解除事实嵌套保存为 responsibility_release。

组合专项：停止责任 SQLite/PostgreSQL **56 passed**（含关闭/新入位真实锁等待、提交/回滚），宿主容量与生命周期 **153 passed**，清理 **21 passed**（未决原架保留、严格交出后可清理）。这些专项结果有重叠，不相加；整合后的完整测试和最终评审将在同节补记。此前两分支独立全绿不能代替组合结论。

整合后全仓初验：迁移 309 passed、9 failed、0 skipped；其余 5406 passed、1 failed、44 skipped。失败均为两处测试样例适配：旧迁移用例重复插入已存在工作线；双线用例在旧供架未交出时要求第二架进同点。产品/迁移/helper 源码未再修改；更新既有配置，并以原 Transport/严格接纳先交出旧架后，完整受影响文件复验关闭失败（迁移文件 56 passed，双线文件 2 passed）。不将初验命令冒称 exit 0；最终按每项最新结果去重覆盖，迁移 318、其余 5407 passed/44 skipped，合计 5725 passed/44 skipped。完整提交树另由 PR CI 重验。

独立整体审查无 Critical/Minor，发现 1 项 Important：停止后的真实宿主传 UNKNOWN，阻止严格原 ENTER 拒绝事实收口。公共入口回归先失败再通过，保留原冻结身份、无接纳/结果/位置/前驱冲突等全部证据检查；受影响停止组 84 passed。历史恢复还以先失败后通过的回归修复重复核对清空原 preparation_action_id，容量/恢复 23 passed。Ruff、702 文件格式、差异检查和唯一 rollerfour head 通过。所有检查使用独占可丢弃 PG/Redis/loopback，未操作运行库。


## 未验证边界

未部署；未调用现场接口、未执行物理搬运。供应商有限重报窗口、超窗处置和 WMS task_id 不复用仍为正式启用前合同条件；本地构造回执/设备 HTTP 测试不替代这些现场合同与验收。


## 供评审的正式切换顺序（本次不执行）

1. 核对当前版本、唯一 migration head、原任务身份合同及有限重报/超窗规则；确认 WMS 不复用 task_id。外部合同未确认时保持未启用。
2. 停止旧版本 BIN 写入并核对旧私表待办、宿主活跃引用、未明确结束的消息/命令/搬运/架责任。预检拒绝时停止切换，不由脚本自动关单、撤销或冒充成功。
3. 按运行环境规则备份，确保代码与 A/B schema 配套。执行已评审的 A→B；B 只删除无在制责任的旧 BIN 私表历史并建立空四表，共享公共数据及其他业务保全。
4. 核对新四表为空、旧私表已替换、公共身份/冻结正文保全、单 head 和模型结构一致，再启动同版 API/worker/既有维护入口。
5. 仅接收新运行/新 WMS 任务，执行独立的供应商与现场验收。回退前运行降级预检；新运行数据非空或存在公共新语义时不做有损降级。不存在把旧八表数据装入四表或续跑旧队列的步骤。

以上只是可评审的切换说明，不是本次操作记录，也不构成部署授权。


## 实现复审后的修订

本轮按复审的三项建议收敛，产品代码只修改回流事实消费与两类发送门禁中的范围证据写入；不增加模型字段、迁移 revision 或发送/恢复流程。

1. 已被真实新轮替代的旧 WAIT_FACT，在原匹配结果明确终位仍为来源时，以 SUPERSEDED 关闭检查点，保留原 Transport/client/caller 和已消费版本；不标搬运成功。位置未知仍保留 WAIT_FACT。覆盖最终事实在 TASK 关闭前已被消费和关闭后才到达两种顺序，确认不创建新意图并退出动作待办。
2. 可靠消息新增 dispatch_started_at 仅由带范围标签的消息写入；未标签普通/snapshot 投递继续原状态/租约门禁和保守判断，原 EcsCommand 派发记录及 EcsSendEvidence 保留。另验证未标签普通消息投递后 A 可降级，原请求/响应字节及 DONE 状态保持。
3. 新增 13 类独立旧责任切换拒绝场景：活跃 RackJob、含 BIN 的活跃宿主任务、SCAN2 接纳、三类 ECS 未结责任、三类 Transport 未决结果，以及四种可发送/待处理消息状态。前置私表无待办，每类必须命中自身公共/宿主表保护，并验证原记录、冻结字节和 Alembic 版本未变。旧表数据仍不迁移。

首次专项：普通/snapshot 及已替代回流修复后 8 passed；扩展未知终位对照后回流专项 8 passed；独立责任拒绝专项 26 passed（SQLite/PostgreSQL）。补齐后的最终全仓串行回归：**5517 passed、118 skipped，零失败，987.79s**，本轮新增 40 项回归全部执行通过，包括未标签消息正常投递后 A 降级的 SQLite/PostgreSQL 两条端到端路径。完整日志：`/tmp/roller-refinement-full.log`。Ruff、690 文件格式检查、差异检查通过。
