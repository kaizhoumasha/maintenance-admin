# SMT 手工 M1 软件验收

核对日期：2026-11-01（America/Los_Angeles）。基线为 `develop@05bcc75`，实施分支为 `feature/smt-manual-m1`。本轮只交付 M1 任务基础，未提交；任务规格/质量复核、整体评审修复的定向复核与最终全量均已通过，M1软件验收完成。M2～M6、正式 catalog、人工执行、组件/宿主状态迁移、worker 和现场验收不在本轮完成范围。

当前交付状态请以[SMT 手工统一验收](smt-manual.md)为准；本页保留本阶段当时的数字、范围及未验项，后续 M3～M6 已按新授权继续实施。

本页保留M1阶段的历史验收数字与范围；后续M2箱级准入/WAIT/完工/原释放已完成软件验收，当前证据见[M2验收](smt-manual-m2.md)。M3～M6及正式生产交付仍未完成。

## 1. 已实现范围

- `SmtManualTaskFlow`/`TaskRepository` 保存 MANUAL 入库、出库共用的任务、原输入、队列/计划版本、成员及原准备关联；`RollerPorts` 提供真实当前任务和接续读口。
- 复用已合并的 sorting_task 六操作/严格合同/原归属与重验接缝；不重复注册任务，也不创建组件或方向独立业务。
- 同线共用 PREPARING/EXECUTING 执行位；按 dispatch_sequence、原 issued.timestamp、task_id 排序，遵守 not_before。领取与原 prepare Outbox 同事务；通过 WmsRequests.get_result 消费原结果。
- 原消息早到/缺版/停用仅留允许的待定信息；条件变化不自动应用。原消息重验时成员、版本、SupplyChange、下一意图及收据原子提交，确定拒绝保留首次响应。
- 入库只追加供箱架面，出库支持供箱、精确 SLOT 直接取料及首版可选目标架；连续计划、当前同版完整 data 判重、数组顺序及旧版/新身份冲突各自处理。
- TASK 取消限 QUEUED/PREPARING 且无首版；成员撤销精确到原版本/架/面/可选 SLOT，跳过不匹配/已取消/不可取消项，不取消未来版本，不删除在制义务。追加及撤销使用真实 BinRoller.apply_supply_change。

## 2. 必要适配与迁移

源码核对发现 WorkLine 原本没有启停字段，无法真实验证已批准的停用/恢复规则。本轮增加 `WorkLine.is_enabled`，兼容默认 true；既有管理接口使用严格布尔值，更新省略该字段时保留原值，沿用原版本和审计。M1 据此限制首次任务/领取、新 prepare 和新计划；取消、原结果与历史身份继续按原规则处理。该状态不是设备急停或物理停止证明；后续执行路径由 M2～M6正式接入。

新增平台迁移 `a39a1988a81a`（文件 `2026-11-01_workline_enabled.py`），父版本 `f0b220261031`。新构建运行前需先升级该字段；本轮只在专用可丢弃库验证，未操作运行库。BIN_Roller 及手工业务状态表迁移仍留 M5，只交付一次。迁移测试覆盖原数据保留/default true、显式 false 及防止丢失停用状态的降级拒绝。

真实入口 RED 还暴露了共享 first-issued 的适配错误：已配置工作线指向非任务消费者时原来返回503。现按 API 工作线准入合同返回409/STATE_CONFLICT；真实消费者/装配暂不可用仍503，不广播或回退到人工业务，不改原归属和历史收据。

接续查空只返回UNKNOWN；CHECKING/AVAILABLE须有真实队列/准备依据。M1没有权威“无接续”决定，不伪造NONE；后续M4形成可靠完成事实后再提供相应依据。

## 3. 测试与证据

所有运行均使用本工作区专用 PostgreSQL 16/Redis 7，没有连接 WMS/ECS/RCS 或用户运行库。业务/组件测试表采用独立 MetaData，显式创建并 finally 清理；共享入口使用真实 catalog 编译消费者、receiver、Outbox、公开结果读口和组件供箱持久化。

| 检查 | 结果 | 范围 |
| --- | --- | --- |
| 实施前依赖基线 | 854 passed / 11 skipped | sorting_task、BIN_Roller、WMS callback、组件分派 |
| 受影响回归 | 931 passed / 12 skipped，123.08秒 | M1、共享组件/receiver、工作线管理、平台迁移与原绑定 |
| 修复前全量 pytest | 4700 passed / 52 skipped，428.71秒，退出0 | 完整 app/tests/迁移输入冻结期间运行 |
| 断言补强后 M1 专项 | 26 passed / 0 skipped，4.96秒，退出0 | 全量后仅补强两份测试的断言及两项测试，产品/迁移输入不变 |
| 并发评审修复回归 | 147 passed / 1 skipped，12.12秒 | M1及共享dispatch；跨线同task_id竞发RED为202/IntegrityError，GREEN为202/409且冲突稳定重放 |
| 原输入完整身份修复回归 | 247 passed / 5 skipped，23.03秒，退出0 | 真实receiver同任务/跨任务、SQLite/PostgreSQL四组合均通过，原计划可恢复且供箱撤销不串身份 |
| 修复后最终全量 | 4708 passed / 52 skipped，446.17秒，退出0 | 所有评审修复后最终代码快照；无失败、无警告 |
| 静态检查 | Ruff check、593文件format check、diff check通过 | 最终修复后由控制会话执行 |
| Alembic | 一个head：a39a1988a81a | 只验证迁移源码和独立测试库，不代表运行库已升级 |

保留功能 RED：停用时当前任务被误标有效、新身份计划错误重放终态版本、业务不匹配错误503，以及待定转确定冲突后响应时间漂移。初次缺模块/fixture错误不计功能RED。详细命令、日志、输入摘要和实现自查保存在本任务的忽略目录 `.superpowers/sdd/2026-10-31-smt-manual-implementation/`，未随提交交付。

全量运行前后506份输入摘要一致；其后只补强 `test_tasks.py`、`test_cancellation.py` 并重跑最终专项。初次全量与当时补强断言后的产品/迁移/配置摘要保持一致：`a8058fc5285e92884082262458767692d5b7fc11ee324868601724b7e79ee603`。随后独立评审发现并修复了跨线首次任务身份竞争和跨operation复用UUID时的原输入覆盖；该旧摘要和4700结果不是修复后最终代码的全量证明。最终4708结果对应506份输入摘要 `693146b795f2bbb6ca18026038162c63bfdfc6c9d7bb5e970d2fa051e97773e1`，运行前后完全一致；另核对4份既有fixture/迁移模板也未变化。

52项跳过是既有SQLite不具备的并发/行锁/独立连接等语义、原根SAVEPOINT限制以及需要额外专用库的既有破坏性迁移项；不计为通过。本轮主测试库/Redis已配置，M1的PostgreSQL竞争与原子性用例实际运行。最终全量与修复专项无警告。新工作线迁移的SQLite/PostgreSQL升级回归已实际运行，不与跳过的旧迁移项混淆。

## 4. 验收对应

| 业务合同 | 证据位置（tests/ 下） |
| --- | --- |
| 原任务/方向/工作线冻结、稳定排序、not_before与唯一执行位 | businesses/smt_manual/test_tasks.py |
| 原prepare身份、领取/队列竞争及外层回滚 | businesses/smt_manual/test_tasks.py |
| 方向资源、连续版本、当前同版判重与原计划重验 | businesses/smt_manual/test_plans.py |
| 停用/恢复、确定拒绝不再重开、终态及原结果 | businesses/smt_manual/test_plans.py |
| TASK/精确架面与SLOT撤销、原供箱需求/回滚 | businesses/smt_manual/test_cancellation.py |
| 相同UUID的不同operation独立留证、待定原计划恢复与真实供箱撤销 | businesses/smt_manual/test_plans.py |
| 真实共享原归属、改配/重放及工作线拒绝 | businesses/smt_manual/test_task_routing.py；businesses/sorting_task/test_dispatch.py |
| 严格启停CRUD/省略字段/审计及兼容升级 | services/test_workline_business.py；migrations/test_workline_enabled.py |

## 5. 评审与未验收范围

任务规格/质量初审发现一项P2：同一task_id跨线并发首次发布时败方抛IntegrityError。实现已用任务主键的原子冲突处理修正，保存明确STATE_CONFLICT且不改赢家；真实PostgreSQL回归覆盖跨线及同线竞争、原输入/prepare和冲突重放。定向复核已确认P2修复且无新增阻塞项；任务规格/质量复核通过。整体评审另发现一项P2：TaskInput仅用operation_id主键，合法的不同operation复用UUID会覆盖待定计划证据；已改为(operation, operation_id)复合主键并同步读写/重验，四组合真实入口回归通过。P3状态摘要已同步。整体评审的单轮修复经独立定向复核全部关闭，无新增或搁置问题；最终全量4708/52通过后，M1软件交付门槛闭合。

正式API/worker的catalog仍只登记原coarse_sorting；本轮M1使用隔离真实装配，不是已启用的SMT生产业务。SCAN2准入/完工、出库架流、整单确认、M5组件/业务状态迁移与唯一推进、M6真实worker/两线恢复，以及WMS/ECS/RCS和现场物理闭环均未验收。ACK或已登记意图不能替代这些证据。

## 6. 本轮执行裁定

以下保留控制账本中的裁定及可能代价：

| 裁定 | 理由与判断错误时的代价 |
| --- | --- |
| 只实施M1 | 用户列出的任务/队列/计划/取消/供箱对应M1；误判会造成后续生产范围扩张 |
| 建立隔离worktree | 用户选择的SDD要求隔离，保留主目录；代价是一份需保留的独立检出 |
| 不暂存、不提交、不推送 | 沿用用户/仓库未授权提交的边界；须保留未提交diff与证据，不能仅靠Git历史恢复 |
| 为M1标题手工提取任务brief | 技能脚本仅支持数字标题，M1提取失败；用明确锚点逐字提取，风险是边界误选，已与原任务核对 |
| 增加最小WorkLine启停前置 | 平台缺少真实状态；错误默认/省略更新会误停或误恢复旧线，因此新增兼容迁移与回归 |
| 接续缺证据为UNKNOWN | 没有无接续接口，完成事实属于M4；代价是取得权威依据前不授予新drain求值 |
| 修正非消费者工作线首发为409 | 真实RED证明503与合同不符；代价是一处共享分支变化，已保留临时不可用503及历史重放回归 |

本轮专用PostgreSQL/Redis容器在最终验证后清理；隔离工作区、未提交源码、报告及忽略目录内账本/原始日志保留，未清理主目录或其他运行资源。M2后续交付见[M2验收](smt-manual-m2.md)，后续按[实施计划M3](../superpowers/plans/2026-10-31-smt-manual-implementation.md#smt-manual-m3)另行实施，不重做已合并的共享组件。
