# 粗分原身份等待与恢复回归

日期：2026-11-24。依据[粗分 API 合同](../integrations/coarse-sorting-api-draft.md)和[恢复计划](../superpowers/plans/2026-11-24-coarse-sorting-ecs-recovery.md)。用户要求放料结果和报告状态独立：收到原失败先记录，WMS 收妥只完成报告，不能卡住后续物料；不新增 ECS 更正，执行结果只有成功和失败。

## 行为与现有测试入口

| 行为 | 回归入口 |
| --- | --- |
| 原 FAILED 消费后、WMS 发送前 put_result=FAILED/report_status=PENDING；收妥后 FAILED/COMPLETED，放料结果不变且 placed_at_ms 为空；成功同样独立记录，重试不重建放盘，后续物料继续 | [direct_put](../../tests/businesses/test_coarse_sorting_direct_put.py) |
| 输送及 NG 的故障、未知和不完整事实保留等待；原严格成功沿已有路径推进，真实 NG 完成仍可正常结束 | [forward](../../tests/businesses/test_coarse_sorting_forward.py)、[ng](../../tests/businesses/test_coarse_sorting_ng.py) |
| 未决放盘不能提前交出原架；原明确成功或未放料失败可解除执行占用，报告重试不永久卡架；F01 离场接纳与新架严格到位保持各自边界 | [depart_only](../../tests/businesses/test_coarse_sorting_depart_only.py)、[rack_preparation](../../tests/businesses/test_coarse_sorting_rack_preparation.py) |
| 接收事务提交后唤醒；通知失败、丢失和服务重建后，真实 business pending 入口继续消费原事实；重复和并发通知不造第二套意图 | [wakeups](../../tests/businesses/test_coarse_sorting_wakeups.py) |
| API/worker 使用相同正式 catalog；真实 API 与独立进程 worker 通过原 Sender 验证正常、NG 和失败报告闭环，重启保留原身份 | [application](../../tests/businesses/test_coarse_sorting_application.py)、[integration](../../tests/businesses/test_coarse_sorting_integration.py) |
| 普通 Result 同内容重放原响应，不同内容冲突；消费不越过 owner/权限/原附录边界，不回退已提交副作用 | [ECS result](../../tests/integrations/ecs_command/test_result.py)、[query](../../tests/integrations/ecs_command/test_query.py)、[concurrency](../../tests/integrations/ecs_command/test_concurrency.py)、[restart](../../tests/integrations/ecs_command/test_restart.py) |
| 实际清理后原验证、分配、报告及架责任仍可读取；runtime authority 的过期租约回复保留可审计原文，但不覆盖当前采用结果；无保留责任的正常终态仍可清理 | direct_put、rack_preparation 及 [HTTP recovery](../../tests/reliable/test_http_exchange_recovery.py)、[reliable tasks](../../tests/tasks/test_reliable_tasks.py)、[callback tasks](../../tests/tasks/test_callback_tasks.py)、[history retention](../../tests/tasks/test_integration_history_retention.py) |
| 历史七字段成功报告按原身份和正文闭环；目标不符或原 PUT 失败不能按该格式闭环；旧历史库种子使用对应旧表结构 | direct_put 及 [backfill](../../tests/scripts/test_backfill_integration_history.py) |
| 已记录原放料事实但尚未建报告时，运行记录清理后仍能用严格保存凭证恢复报告；仅缺一个原身份或凭证摘要不符继续核对；新架准备不重纳入已闭环失败历史，已冻结前序仍核验 | direct_put、rack_preparation |
| 两状态字段升级、可确认的旧成功回填、旧任务/动作身份保留；失败不写放料时间，报告收妥与原执行分别校验，已有失败事实时拒绝丢失该事实的降级 | [coarse migration](../../tests/businesses/test_coarse_sorting_migration.py) |

原报告取得有效 `RECORDED / DUPLICATE` 后，该盘业务终局。ECS 保证终局后不再上报不同执行结果；WES 保持普通不同 Result 的 409 拒绝，不接受结果更正或重新打开已闭环物料。不能以“已收妥报告后再次接受不同结果并重开责任”为前提要求新增恢复或离架阻断逻辑；报告未收妥和原责任未闭合时的严格核验保持。已有集成回归验证关闭后不同 Result 返回 409，原报告关联、`FAILED / COMPLETED` 及退出待办状态不变。

`FAILED` 已应用后再直接上报不同 `SUCCESS` 仍是 409，不属于可消费恢复事实。未收到 Result 时等待原首次合法结果；收到合法明确的放料失败后走失败闭环，不能无限等待成功。矛盾信息需要校验，不增加 UNKNOWN 执行结果，也不能用“持续 pending”证明设备已自主恢复。

## 隔离验证方式

用 `uv run pytest` 执行表中需要验证的现有文件。显式设置 `TEST_DATABASE_URL` 和 `TEST_REDIS_URL` 指向本轮专用一次性资源；运行迁移用例时额外设置对应的独立迁移库。测试数据库必须支持仓库所需扩展及建临时数据库权限，不能使用现场库。SQLite 与 PostgreSQL 的后端专属跳过分别统计，未配置数据库造成的跳过不能计作 PostgreSQL 通过。

保存具体命令、源码哈希、数据库/Redis 版本、JUnit 和进程退出码。后续代码或被覆盖依赖改变时，原记录仅作历史证据；复审核对原始结果，不重复运行状态测试。失败终态的增量迁移需验证旧记录保留、终态约束及降级门禁。

未发布迁移 `coarse_put_failure_v1` 现用于增加两个独立状态字段并调整约束，须在启用新版本前升级数据库；本轮仅用一次性数据库验证。已记为 `put_result=FAILED` 或待报告记录缺少原报告动作时拒绝降级。尚未消费的新格式原 Result、待办与原身份可在结构降级后保留，旧版本能否继续消费须另行验证。迁移不自动重新启用旧 inactive 记录；历史重放测试须完整保留新列、原值和当前约束/索引，不能只恢复检查约束。

## 已取得的软件证据与界限

- 此前 T1 的失败持续等待已被用户修正为明确失败闭环，历史全套结果保留在计划中，不作为最新语义的通过证明。
- T4a 历史定向门禁：152 passed、10 skipped、0 failed，涵盖八个 runtime WMS 原始回复保留场景及 PostgreSQL 投影锁竞争；最新失败终态适配后又纳入下述最终覆盖。
- 分离前历史实现按同一 20 文件/选择器范围分成两个通道完成：651 passed、36 skipped、0 failed/error，687 个唯一用例无遗漏/额外/重复。它覆盖实际 `0 / PUT_FAILED / 1005`、后续物料、货架、历史前序及保留规则；本次两个状态字段和约束已变化，不能把旧结果当作新模型的通过证明。最新分离验证见下项。
- 此前分离模型及兼容修复：独立两道共 **697 个唯一用例，661 passed、36 skipped、0 failed/error**，无遗漏/额外/重复；跳过仅为 SQLite 不适用的 PostgreSQL 专属场景。50 项冻结哈希及迁移 CLI 7 项哈希一致，20 个修改 Python 文件 Ruff/格式通过，三项 Important 全部关闭。原始证据为 `separate-fix1-final-manifest.json`、`separate-fix1-reconciliation.json`、`separate-fix1-review.md` 及分道 log/xml。
- 两个会提交历史迁移的旧测试共用 `tests.migration_helpers.replay_coarse_sorting_history`，恢复入场时的检查约束和缺失索引，原业务断言保留；直接放料 3 项 → NG 5 项 → 实际报文及此前 4 项失败在同进程按序复验，13 passed。
- 一道首次在临时库创建期间被原历史维护门禁拒绝；单独复验通过，监测观察到后台遥测事务但未捕获原始拒绝瞬间。该专用实例按仓库 CI 配置关闭遥测后完整重跑通过，生产门禁和测试断言未改。旧失败日志保留，未计入通过。
- 发版复核复用了与 `dc6d1ed` 运行输入一致的本地证据。四道全仓测试合计 8083 个唯一用例，5168 通过、2915 跳过，未配置存储的用例仍计为跳过。另有配置一次性存储的定向 PostgreSQL 137 项、SQLite 43 项通过，隔离库的 `alembic upgrade head`、`alembic current` 和 `alembic check` 通过。恢复后的三项 QA 探针通过，涵盖实际 API/独立 worker 的原失败报告闭环和闭环后不同 Result 返回 409。
- 历史定向结果见[恢复计划实施记录](../superpowers/plans/2026-11-24-coarse-sorting-ecs-recovery.md)。工作区未提交时，原始日志、哈希和审查记录保留于该计划的 `.superpowers/sdd/2026-11-24-coarse-sorting-ecs-recovery/`，新分离模型的用例核对文件为 `separate-fix1-reconciliation.json`（原 `failure-final-reconciliation.json` 仅为分离前历史证据）；该结果是本地定向证据，不是全仓 CI 或现场验收。

本地回归不证明现场 ECS 恢复、物理完成、WMS 当前预约有效或存量已修复。旧失败报告的原出站意图未被改写，仍须核实其送达与预约释放影响。2026-11-02 的[现场配置核对](2026-11-02-coarse-sorting-design-configuration-check.md)保留当时只读观察，不作为本轮设备状态证明。
