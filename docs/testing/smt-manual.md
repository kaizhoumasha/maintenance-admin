# SMT 手工正式组合软件验收

**2026-11-03 实施状态。** 手工 MANUAL_RACK、MANUAL_BIN、MANUAL_COMPLETION 与组件相关推进在首条 SQL 前采用 SERIALIZABLE。独立 RACK_REPORT 与 SCAN2_RELEASE 保留 REPEATABLE READ。

<a id="current-delivery"></a>
## 2026-11-02 提交交付与最终全仓验证

当前源码已在 `feature/smt-manual-m1` 提交并推送：实现 `e8e0782`、合同/设计/验收文档 `9d61af6`、交付元数据 `bc999b6`。本节记录这批提交的最终软件门槛；下方各日期/阶段的“未提交”、旧表数、旧 head 和测试数字保留当时快照含义，不作为当前交付状态。PR、合并、部署及运行库升级仍须分别记录。

| 最终门槛 | 结果与可复查入口 |
| --- | --- |
| 全仓测试 | **5206 passed / 47 conditional skipped / 0 failed，981.79 秒，exit 0**；在上述已提交内容上执行，运行前后 673 个源码/测试/迁移/配置路径无漂移 |
| 迁移 | 全仓内 198 项迁移用例全部通过；本分支六份迁移沿唯一 head `fiveracklimit`，当前 SMT 7 表、BIN_Roller 10 表；`tests/migrations/test_smt_manual_persistence.py`、`test_bin_roller_persistence.py`、`test_five_rack_request_limit.py` 等保留数据与拒绝有损降级断言 |
| 实际进程 | 全仓内 `tests/tasks/test_smt_manual_worker.py` 两例通过，覆盖原 owner 通知、真实 worker/Beat、WAIT/完工/公共待办及丢通知补位；peer 为本机模拟服务 |
| 静态检查 | 全仓 Ruff check、647 文件格式检查及 diff check 通过 |
| 跳过与警告 | 47 项均为 SQLite 不具备 PostgreSQL 锁/隔离/连接语义的条件变体，没有因未配置集成环境而跳过；不计作通过。两条既有 `record_property` 与 xunit2 导出格式警告未造成断言失败 |
| 独立审查 | 本轮原生主审、API/数据审查及对抗复核已闭合；Claude CLI 未安装，外部跨模型覆盖为 UNAVAILABLE，不记作已通过 |

原始 `full-tests.log`、`full-tests.xml`、`full-tests-summary.json` 及逐文件一致性记录保存在本机外部目录 `/Users/kaizhou/.codex/tmp/ship-smt-manual-zgg3oxwk/`，不随仓库分发。下方 `.superpowers` 路径同样是本地历史证据；本节使最终结论可随源码交付，但不代替原始日志。

当前正式源码 [catalog](../../app/businesses/catalog.py) 同时登记 `coarse_sorting` 与完整 `smt_manual`；[手工 composition](../../app/businesses/smt_manual/composition.py) 组合同源 sorting_task/BIN_Roller，API 与 worker 使用同一声明和 `scan_pending`。这证明正式装配源码已就绪，不表示运行环境已部署该版本。接口、配置与迁移用法以[人工 API](../integrations/manual-picking-api.md)、[业务包指南](../integrations/business-packages.md)及本页下方分项说明为准。

同任务同箱再次作业按真实 **A → 其他箱实际 WORK_REQUIRED/完成 → A** 验证，见 `tests/businesses/smt_manual/test_completion_notifications.py::test_original_completion_retry_stays_with_first_work_after_another_bin`：原完工重试保持原 operation_id/有效内容和首次消费依据，同号改内容冲突，第二次作业使用自己的新完成。NORMAL/NG 是最终结果。先前 A1 的“换新身份、改相反最终结果、沿用旧完成时间”情形已撤回为无效正常重试场景；没有据此修改 WMS/ECS/API 代码，也不将其列作已修缺陷或接受风险。

本轮覆盖审计为最多 30 个行为组加 6 条流程的范围映射，映射 100% 不是实测行覆盖率。N1 仍为非阻断补充：新模型已选 service/feed 的专门降级往返样本未单列，已有填数双向迁移与运行时验证不等于该专门样本。未部署、未迁移旧 wes_backend 数据；供应商 1.1、Status 10 秒及现场物理验收不在本次软件通过结论内。

<a id="rack-cancellation-fixes"></a>
## 2026-11-02 取消与离场修复：新专项通过

**初轮修复专项为 88 passed，随后顺序控制回归 53 passed、加强重复推进断言后四例 4 passed / 22 deselected，均 exit 0；最终独立评审另发现的 mixed 取消与目的点冲突缺口已经补修，补修专项 83 passed in 40.25s，exit 0。** 这些是不同运行范围，不相加为独立场景总数。已修复下文历史 ZONE 同源终位错误释放，取消终位/成员撤销与 STARTED 原完成路径已补回归。修复前较广门禁为 1811 passed / 11 conditional skipped（255.64s，含 worker/Beat），不能验证后续补修；冻结最终快照门禁结果见下文 final-focused3；本节不以旧全仓 5145/47 替代本轮新证据。

工厂专项包括 KT16/KT18 × RETURN/TRANSFER × ZONE 同源/异位共 **8 个真实正式 profile 场景**：KT16 OUT55/OUT65、KT18 OUT56/OUT66。同源终位保持 POSITION_CONFLICT/占位；异位成功才可沿原公开闭合事实释放。其他取消回归覆盖目标外安全释放与受管点冲突、目标内 null 朝向且 RETURN 全撤销补偿、有效/STARTED 或 TRANSFER 的 RECOVERY_REQUIRED、取消同架不隐式重入及不同架独立授权继续。正常 TRANSFER 的任务 COMPLETED/原成功进场门禁保持。

证据目录 **C** 为 [本轮 Task1 报告](../../.superpowers/sdd/2026-11-02-smt-manual-rack-cancellation-fixes/task-1-report.md)，顺序控制的独立复核见 [fix1 review](../../.superpowers/sdd/2026-11-02-smt-manual-rack-cancellation-fixes/task-1-fix1-review.md)；实施范围见[修复计划](../superpowers/plans/2026-11-02-smt-manual-rack-cancellation-fixes.md)。C 为本地保留证据，不表示已提交交付。真实命令模板：

```text
rtk uv run python .superpowers/sdd/2026-11-02-smt-manual-rack-cancellation-fixes/run_checks.py LABEL -m pytest <测试文件/筛选> -q
```

| LABEL | 实际范围 | 结果 |
| --- | --- | --- |
| task-1-final-focused | rack_cancellation、factory_profile、transfer_rack、rack_reports、direct_pick、cancellation、task_completion | 88 passed in 40.95s |
| task-1-round1-controls | rack_cancellation、cancellation、direct_pick、rack_reports、transfer_rack | 53 passed in 14.18s |
| task-1-round1-pending-repeat | test_rack_cancellation.py，`-k 'all_member_cancellation or partial_member_cancellation'` | 4 passed, 22 deselected in 1.68s |

C 保存对应 `.log/.exit` 与完整测试文件命令。顺序控制覆盖成员撤销前/后原 ENTER SUCCEEDED，原 ENTER pending 期间重复推进不重建，WAIT 截止前不重问、到期重问、READY 后仍唯一 DEPART；到位报告未交付时不能 completion_confirm，交付后恰好一次。部分撤销保留剩余普通 PENDING 的原面作业，不提前离场。Task1 四文件 Ruff check/format check 通过。

`CANCELLED` Transport 不是成员撤销或 TASK 取消；角色释放、pending、任务完成、报告与公共义务独立。ENTRY_CANCELLED 保留原 pending，不提供恢复授权/API；当前存在后续持续扫描成本。到位取消补偿不伪造成功或朝向，无真实报告则不新增；已存在原报告仍须交付。细则见[自有架设计](../superpowers/specs/2026-10-31-smt-manual-business-design.md#6-出库自有架流)及[人工 API 离场规则](../integrations/manual-picking-api.md#manual-departure)。本轮不改五层 CTU03 接纳释放规则，不新增表/公开接口/通用框架。

最终增量另补两处保护：混合 BIN+target 计划沿原普通完成资格（包括 target 尚无 Result 的情形），但原 TRANSFER ENTER 的公开已应用 `CANCELLED` 必须否决 completion_confirm，即使架推进尚未消费该结果也不例外；位置观测不能代替 applied outcome。新 SMT-owned ENTER 还检查原业务 rack/action 中未释放的 `POSITION_CONFLICT` 的确切 RACK_POSITION/location，避免向同线其他角色或其他线已知冲突目的点再次进场；只阻断命中的点，无关点仍可推进。保护仅依据当前事务可见的已提交业务投影，不证明全局物理互斥，也不改变共享 BIN_Roller 目的点调度；并发未相互可见的事实不因此获得全局串行保证。后续 sweep 仍读取原公开结果并重算冲突，投影无变化时不重复写 rack/action；只读轮询成本仍保留。

最终补修证据见 [final-fix-report](../../.superpowers/sdd/2026-11-02-smt-manual-rack-cancellation-fixes/final-fix-report.md)，修复前发现见 [final-review-report](../../.superpowers/sdd/2026-11-02-smt-manual-rack-cancellation-fixes/final-review-report.md)。`final-fix-focused` 使用同一 wrapper，运行 rack_cancellation、factory_profile、supply_completion、task_completion 四文件；83 项与先前测试范围重叠，不相加。五文件 Ruff check/format check 通过；最终冻结快照门禁结果已补录如下。

最终冻结快照专项门禁 `final-focused3` 已通过：**1822 passed / 11 skipped in 296.54s，exit 0**（wrapper 300.83s），覆盖 SMT、BIN_Roller、sorting_task、WMS Transport 及真实 worker/Beat 两例；不是全仓或现场验收。11 项为 SQLite-only 事务/锁分支，其 PostgreSQL 对应覆盖通过。最终源码增量六文件 Ruff check、format check 及 diff check 通过。证据为 [final-focused3.log](../../.superpowers/sdd/2026-11-02-smt-manual-rack-cancellation-fixes/final-focused3.log)、[退出码](../../.superpowers/sdd/2026-11-02-smt-manual-rack-cancellation-fixes/final-focused3.exit)及 [XML](../../.superpowers/sdd/2026-11-02-smt-manual-rack-cancellation-fixes/final-focused3.xml)。

中间 `final-focused2` 的 **1 failed / 1821 passed / 11 skipped** 保留为失败历史：未改的 BIN_Roller 容量测试把最后创建的 demand 误当作当前 waiter，同毫秒 UUID 排序使该前置不成立；仅在测试本地排序三个 demand ID 修正前置，原 50 条断言与参数均保持。补验四例及关联 104 项通过，不与最终门禁或先前专项相加。修复独立复核见 [final-fix-review-report](../../.superpowers/sdd/2026-11-02-smt-manual-rack-cancellation-fixes/final-fix-review-report.md)，测试前置修正复核见 [capacity-verification-review](../../.superpowers/sdd/2026-11-02-smt-manual-rack-cancellation-fixes/capacity-verification-review.md)。

测试仅用隔离 PostgreSQL/Redis 与 loopback peer；未部署、未作供应商/现场验证，未扩展供应商 1.1、Status10秒或未消费源元数据的兼容结论。


## 历史：2026-11-02 退料/转运架进出场补验未通过（修复前）

新增工厂配置24场景为 **20 passed / 4 failed**，原相关回归另 **42 passed**，共62 passed / 4 failed / 0 skipped。正常进场、退料架A→B换面、转运架等待真实任务COMPLETED、WAIT/READY、ACK不算离场、UNKNOWN保留原身份等均通过。上一次位置对应PASS与五层CTU03接纳释放不能代表这两类架完整离场PASS。

**发现1个ZONE离场收口缺口：** KT16 RETURN/TRANSFER的OUT55/OUT65、KT18 RETURN/TRANSFER的OUT56/OUT66，若DEPART目的地是ZONE，而SUCCEEDED回报的终位仍是原工作位，当前四组合都落DEPARTED、pending=false，错误解除本角色占位。明确RACK_POSITION目的地的同样回报则被正确拦成POSITION_CONFLICT。该时点源码未修；本节保留修复前失败证据，当前实现和新专项结果以上方最新记录为准。

本次W：`.superpowers/sdd/2026-11-02-smt-manual-rack-entry-exit-acceptance/`。`acceptance-report.md`解释职责、证据、根因和建议；`failure-evidence.json`保存四例冻结源/目的地及原Result；`case-manifest.json`保存24例与624项零清理计数。业务代码、永久测试、配置、原暂存内容和HEAD保持，所有操作为独占库与loopback，未提交/部署。



## 上一次：2026-11-02 配置再次验收

本次依据同一工厂快照重新运行当前实际代码：**127 passed / 0 failed / 0 skipped**（正式配置/额度/worker119项＋两线入库8项）。21配置场景的546项清理计数全0，独占库当前7+10表、head fiveracklimit、17表零行。源配置已覆盖业务路径PASS；合同1.1、Status10秒和目标未消费元数据仍未闭合，不能由通过数替代全量兼容结论。详细字段判定与新证据见[KT16/KT18再次验收](smt-manual-kt16-kt18.md#current-factory-profile)及本次R目录的 `acceptance-report.md`。

本次仅验收、未改产品/永久测试/配置，未提交/部署；没有重复全仓。下面模型收敛阶段的5145/47及失败历史保持原证据范围，不标为本次新跑。


<a id="current-persistence-capacity"></a>
## 2026-11-02 当前收敛与请求额度增量

本轮模型收敛、额度修复、分项及整体复核已完成。最终冻结源码全仓 **5145 passed / 47 skipped，965.32 秒，exit 0**；全仓 Ruff 通过，645 个文件格式检查通过。原 12 项工厂规则与新增 1 项停启线场景，共 13 项，均在最终全仓通过。当前工作未提交、未部署、未操作运行库。下面的 4933/52、11 PASS/1 FAIL 及本轮中间快照保留各自历史范围。

- 当前 SMT 7 张、BIN_Roller 10 张实际业务表（仅两包，不含平台/coarse_sorting），`sorting_task` 无表、只提供合同和分派。顺序 revision：`0177313880f9 → smtmanualcompact → binrollercompact → fiveracklimit`，唯一 head 为 fiveracklimit。
- 合表保留每条原输入、原计划唯一基线、多次请求/WAIT 截止、具名 demand/service/feed/round/release ID、多段搬运、多回架机会及历史投影。六种组件 pending 引用和原公开 ports 不变；原 operation/wire/owner/冻结消息与 Transport/ECS 身份不变。
- 管理 API 新增可省略的 `five_rack_request_limit: int | null`：严格正整数、创建省略 null、更新省略保留/显式 null 清除、原 version 更新保护不变，见[请求及响应示例](../integrations/manual-picking-api.md#manual-request-limit)。原 CTU01 登记占额度，原周期 RELEASED 退出，未知/失败不伪造释放；CTU03 有效 submit 接纳只释放一次，不证明物理完成。
- 开始前 WITHDRAWN 面服务保留 demand/service/cycle/source_ref，有效同架兄弟可继续原周期。兄弟登记 CTU01 和周期 RELEASED 后该撤销服务仍 WITHDRAWN，公开进度 false/false，不列 pending FEED。降级在任何 DDL 前拒绝非空额度或 WITHDRAWN 服务，保全数据和 revision。
- 管理端停线后，已选架沿原 cycle/service 等待重新启用。原 CTU03 接纳照常消费。drain 只在本线 FIFO 排空、原回架批次均已收口时，把尚无原动作或面作业的候选记入原决定的 `unused_racks`。已选的周期和面服务以 WITHDRAWN 收口，保留各自原 ID 和空的 release_basis。若先通过非空额度检查，降级遇到这类 WITHDRAWN 服务会以原 service_id 拒绝，两个检查均在 DDL 前完成。
- SMT 迁移在升降级建表前核对箱完工前序最终可达原通知，绑定的到箱须同任务、线、箱码。已消费的确认结论须与原回复一致。PREPARE 与 task 上的原 operation/message、主体及接纳事实须一一对应。原 REVIEW、结果冲突通知以及待消费的已保存回复均可保留。
- FIFO 按首次有效 SCAN4 到位的 received_at/id 取连续合格前缀；最新有效 SCAN3 判断当前物理轮次（包括尚未绑定回流根的新事实），不以 SCAN3 时间重排 FIFO、不借旧轮结果给新轮资格。

本次证据目录 **P**：`.superpowers/sdd/2026-11-02-smt-persistence-and-capacity-implementation/`。永久测试在 `tests/businesses/smt_manual/test_factory_profile.py`，直接使用全局 `pg_sessions`：未设置 TEST_DATABASE_URL 才跳过，设置后连接或迁移失败就失败。`task4-factory-final2.log/.exit`、`task4-case-manifest.json` 保存全部 12 例和各自清理结果，17 张业务表及本轮平台资源均清零；`task4-store.log` 再次只读核对 7/10、fiveracklimit 与 17 表零行。配置、loopback、实际原请求/响应、冻结 Transport、Result 和原身份都保存在逐例 evidence 中，详细映射见[当前 KT16/KT18 专项](smt-manual-kt16-kt18.md#current-factory-profile)。

| 本轮分项证据 | 原始结果/范围 |
| --- | --- |
| SMT 收敛修复后定向组合 | `task1-fix1-final.log`：296 passed / 0 skipped；有数据升降级、SQL NULL/JSON null、原计划/完工/当前请求指针和冻结平台身份 |
| BIN_Roller 收敛修复后定向组合 | `task2-fix1-final.log`：142 passed / 0 skipped；48 类坏历史双方向拒绝、数据/schema/revision 保全、SCAN2 原释放 |
| 额度及撤销修复后定向组合 | `task3-fix1-final-related.log`：253 passed / 0 skipped，86.17 秒；API/DB 严格整数、版本/SQLite 主键高水位、两种撤销顺序和兄弟最终释放 |
| 无新计划/真实进程补位 | `task3-profile-green2.log` 保留配置专项容量探针；`task3-worker-green.log` 及 `task3-final-worker-route-restore.log` 覆盖真实 worker/Redis owner 通知先补至4，故意丢通知由 Beat 补至5；无需第二 Sender 或手动唤醒 |
| 永久工厂配置 | `task4-factory-final2.log`：12 passed / 0 skipped；初始3/3，首次 KT16 接纳后4/3，满额追加 revision2 两来源仍4/3，第二个不同原 CTU03 接纳后5/3，两个 CTU03 均无最终 outcome |
| 最终整体审查修复 | `final-fix-related.log`：320 passed / 0 skipped，274.00 秒；I1～I5、M1 经 `final-fix-review.md` 逐项关闭 |
| 测试前置稳定化 | `final-verification-forced-green.log`：双库 12 passed；`final-verification-capacity.log`：104 passed / 0 skipped；仅固定两个场景的合法需求 ID 顺序，50 个业务断言不变 |
| 当前最终全仓/整体复核 | `final-full4-pytest.log/.exit`：**5145 passed / 47 skipped，965.32 秒，exit 0**；唯一整体审查、修复差异复核及测试准备补充复核均闭合，无未解决 Critical/Important |

最终控制器证据：`final-full4-factory-manifest.json` 及 `final-full4-factory/` 保存最终 13 份配置 evidence 和 13 份 cleanup，338 项计数全为 0；`final-store-after.log` / `final-store-check.json` 只读确认 fiveracklimit、7+10 张业务表且全为 0 行。`final-preservation.json` 核对 563 个源码/测试/配置文件无漂移、原暂存条目与 cached 补丁不变、HEAD 仍为 `05bcc750215790c91c43e94d1aae1f127fb12549`。外部 Finder 元数据变化单列保留，没有覆盖恢复。最终静态输出为 `final4-ruff.log` / `final4-format.log`。

验证过程保留：首轮 `final-full-pytest.log` 为 3 failed / 5093 passed / 47 skipped / 24 errors，源于控制器误把多个独占迁移 URL 指向同库；新建六个隔离迁移库后 `final-migrations-isolated.log` 全部 180 passed，`final-full2-pytest.log` 为修复前 5120 passed / 47 skipped。整体审查随后发现的 5 项 Important 与 M1 经同一修复波关闭。`final-full3-pytest.log` 为 1 failed / 5144 passed / 47 skipped；唯一失败来自测试假设同毫秒 UUIDv7 生成顺序等于 ID 排序。确定性逆序复现后只稳定两处测试前置，最终由 full4 取得上述通过结果。没有删除失败用例、新增 skip/xfail 或改变产品排序来通过测试。

N1 继续列为非阻断覆盖补充：新写已选 service/feed 的专门降级往返样本尚未单列，旧填数双向迁移与当前运行时已有验证。N2 的 fixture 前默认目标 WARNING 与故意注入的通知失败分别按原日志解释。D1 已精确写明先检查非空额度，再以原 service_id 拒绝 WITHDRAWN，均在任何 DDL 前执行。完整执行裁定及代价保留于本计划 P 的 `rulings-for-delivery.md`。

真实供应商 `third_party_integration@1.1`、Status 10 秒有效期和未消费位置/设备元数据仍为 UNVERIFIED/PARTIAL；loopback 的 ACK/submit 接纳、worker 成功或软件 PASS 都不代表这些合同或物理结果通过。

## 历史软件快照：2026-11-01 M1～M6

核对日期：2026-11-01（America/Los_Angeles）。工作区 `feature/smt-manual-m1`。**SMT 手工 M1～M6 本地实现、分项独立复核、整体审查修复及最终软件验收完成：4933 passed / 52 skipped，556.14秒，退出0；当前最终证据见第8节。** 共享 sorting_task/BIN_Roller 复用已合入源码；手工增量未创建提交、未推送或部署，未执行运行库迁移、真实供应商/设备请求或现场验收。收尾发现的暂存内容已保留并备份，未重置暂存区；最终状态文档更新保留为工作区差异。

2026-11-02（北京时间09:29用户快照）新增 [KT16/KT18配置专项验收](smt-manual-kt16-kt18.md)：最终11 passed / 1 failed / 0 skipped，业务结论FAIL。两线MANUAL、准入/完工、公共回流及CTU03接纳释放已验证；每线5个独立来源架均产生CTU01，不满足队列容量3，单次接纳后恰好补位也未满足。产品未修改；原4933/52仅保持原软件验收范围，不代替本次配置结论。

历史阶段证据保持原日期与范围：[M1](smt-manual-m1.md)、[M2](smt-manual-m2.md)。本页是M3～M6及正式组合的当前记录，不将早期“尚未实施”段落当作当前组合状态。

## 1. 交付与证据归属

| 阶段 | 当前软件产物及验证入口 | 边界 |
| --- | --- | --- |
| ST1/ST2 | 共享 `sorting_task` 六类注册、MANUAL/AUTO四分支、原归属分派及仅plan重验；[共享计划交付记录](../superpowers/plans/2026-10-31-sorting-task-implementation.md#sorting-task-merged) | 已合入共享源码；未注册占位AUTO宿主，不以共享层保存任务状态 |
| M1 | `tests/businesses/smt_manual/test_tasks.py`、`test_plans.py`、`test_task_routing.py`、`test_cancellation.py` | 两方向共队列/执行位、原版本成员/撤销、receiver事务原子性；[M1历史验收](smt-manual-m1.md) |
| M2 | `test_scan2.py`、`test_admission.py`、`test_waits.py`、`test_completion_notifications.py` | 原arrival准入、WAIT、完工关联及唯一原释放；[M2历史验收](smt-manual-m2.md) |
| M3 | `test_direct_pick.py`、`test_rack_reports.py`、`test_transfer_rack.py` | 出库原成员/截止版本、原到位报告与可选转运架自身离场；原Transport身份保留 |
| M4 | `test_task_completion.py`、`test_supply_completion.py`、`test_confirmation_races.py`、`test_first_plan_config.py` | 原prepare本地可靠保存时间、任务确认、新箱暂缓、终局/非终局证据；释放决定已可靠保存即可确认，不等SCAN2 Result |
| M5 | `test_assembly.py`、`test_pending.py`、`test_transactions.py`、`test_recovery.py`；`tests/migrations/test_bin_roller_state.py`、`test_smt_manual_state.py` | 正式catalog/默认表、十类待办轮换、原锁序、单条隔离及含commit的40001最多3次全新会话整事务重试 |
| M6 | `test_owner_wakeups.py`、`test_two_worklines.py`、`test_roller_boundaries.py`、`tests/tasks/test_smt_manual_worker.py` | 原owner唤醒、两线、合并的C07边界，以及真实broker/worker/Beat恢复；不增加业务Celery任务或第二推进路径 |

阶段实现、评审与原始日志保存在本工作区忽略目录 `.superpowers/sdd/2026-10-31-smt-manual-implementation/`，下文简称W；忽略目录不是已提交交付。M3/M4/M5分别见W内 `task-M3-report.md`、`task-M4-report.md`、`task-M5-report.md`及对应复核；M6见 `task-M6-report.md`。

## 2. 正式装配、schema与共享源码

正式catalog包含 `coarse_sorting` 和 `smt_manual`。手工宿主唯一身份是 `smt_manual`，WMS关联 `business:smt_manual`，组件命令使用 `CommandOwner("smt_manual")`；方向和组件不成为运行主体。`composition.py`的 `COMMANDS=()`、`PROGRESSION="scan_pending"`，组件映射精确为 `sorting_task` 和 `bin_roller`。

- `sorting.task.issued/queue_changed/plan_delta/cancel/prepare/completion_confirm@v1`沿共享组件注册一次。宿主仅注册两个方向共用的 `manual_bin.work_admission_decide@v1`、`manual_bin.work_completed@v1`，以及三个出库生产架接口。
- 四点 `SCAN_COMPLETED`、MOVE_FORWARD/MOVE_RIGHT/MOVE_LEFT及请求模型使用现有 `app/businesses/_components/bin_roller/` 单份声明；附录来自其 `ecs_command.py`：`bin_roller.move_forward`、`bin_roller.move_right`、`bin_roller.move_left`，版本均为 `1`。共用离场严格模型为原 `DepartureRequest`，不增同名注册。
- API与worker均消费实际catalog和同一个无状态 `SmtManualService`。M6夹具直接调用原 `business_tasks._runtime` 装配资源，使用实际DeviceRoutes、EcsCommands、Outbox、Transport、receiver和默认迁移表；没有替代宿主或测试调度器。
- 迁移链为 `a39a1988a81a → ac980eb32c52 → 0177313880f9`；最后一项是唯一head。前两项分别为既有WorkLine启停及组件状态，最后为宿主状态；本轮不迁移wes_backend历史数据。
- `enterprise_backend`中15张组件表、13张宿主表在独占测试库完成迁移并在回归后保持完整。`m6-schema-cleanup.log`再次核对head、28张表及零正式业务行；本轮拥有的工作线/设备/路由/ECS命令/可靠消息也均为零。未创建、清空或升级运行库。

组件相关原单条TASK/SCAN/FEED/RETURN_BATCH/RACK_CYCLE/DRAIN advance在首SQL前使用SERIALIZABLE；当时纯人工及SCAN2_RELEASE保留REPEATABLE READ（当前范围见文首），receiver和短扫描未整体升级。扫候选采用本轮只读REPEATABLE READ快照、固定上界和轮换分页，各记录仍用独立新事务。limit是页大小，等待/失败不阻断后续页；该规则由M5保留测试及本轮回归共同证明。

## 3. 两线及公共轮次

`test_two_lines_keep_tasks_arrivals_and_returns_separate`在每次运行创建不同前缀的两条实际工作线。线1执行inbound、线2执行outbound，各线另排一个相反方向任务；四点设备和FIVE_RACK/INLET/OUTLET/RETURN_RACK/TRANSFER_RACK位置全部分开。两方向在各自线内共享执行位，排队任务始终QUEUED。

| 关联 | 实际断言 |
| --- | --- |
| 原任务及同箱arrival | 两线都扫描 `A000000001-C`，形成不同原arrival并保留各自task_id/work_line_id；释放命令各自唯一 |
| 供箱与位置 | 两线精确原成员分别为SOURCE-0/A、SOURCE-1/A；CTU01的原目标分别使用各线FIVE_RACK |
| 撤销及旧义务 | 只撤销线1成员，线2不变；已开始的线1进度保持pending，原action/Transport未删除或替换 |
| FIFO与回架批次 | 线1SCAN4 SUCCESS只使线1候选可用；线2取得本线SUCCESS后才可用。各批次只冻结本线round_id、caller_workline_id；线1旧Result重报不改变线2候选 |
| 迟到原结果 | 原任务取消并切换后，原ECS/Transport通知仍按原owner；不按当前任务、同箱码或当前绑定重建归属 |

`test_manual_host_preserves_new_scan3_round_and_old_obligations`通过正式手工receiver/runtime验证：旧回架分配已有原Transport而未终结时，真正新的本点SCAN3独立建轮；旧命令、分配members、WMS请求、client_request_id、Transport及冻结正文保留。旧SCAN4 SUCCESS重报不能赋予新轮资格，必须由新轮自己的匹配SCAN4 SUCCESS进入候选。

`test_scan4_retry_keeps_original_fifo_under_manual_host`证明本轮SCAN4失败后的合法重扫保留首次FIFO顺序和原首个SCAN4引用。其他C07情形沿已合并组件的 `test_recorded_scan3_reentry.py`、`test_rounds.py`、`test_rescan.py`等执行本轮受影响回归；宿主没有复制轮次算法或增加人工移出门禁。回架分配测试设置已存在READY架服务这一前置，扫码、WMS分配、Transport和原结果消费使用真实公开路径，不作为物理到位证据。

## 4. 唤醒与真实进程证据

`test_owner_wakeups.py`覆盖有效RECEIVED/202、DUPLICATE/200落DONE后仅通知原owner；失效lease、重复finish、FAILED、UNKNOWN、无owner不通知。保存点释放不提前通知，外层回滚不通知。WMS决定保存、ECS实际应用、Transport有效outcome_revision应用与submit接纳分别验证，原任务切换不改变旧责任。

M6发现并修正唯一产品缺口：`receive_ecs_result`原先在409未应用证据落库后也会通知。现在只在 `applied` 为真时注册原after_commit；收据、拒绝、reconciliation、hold和ACK语义保留。`m6-owner-red.log`记录1 failed/5 passed，失败正是409后误通知；最终专项及受影响回归验证修复。

真实进程夹具只读取显式TEST_DATABASE_URL/TEST_REDIS_URL。子进程DATABASE_URL明确指向本轮独占PostgreSQL，broker明确指向本轮Redis；WMS三个协议地址都为同一本机模拟peer，实际DeviceRoute也全部核对为该loopback地址。ECS通道由原runtime管理，不在RELIABLE_TARGETS另造ECS目标。

worker使用原 `business.advance_pending`、关闭eager、唯一队列和节点；Beat只保留原catalog声明的手工5秒条目，使用原PersistentScheduler。父进程的唯一原Sender只向本机peer发送；测试不直接调用宿主service/advance来代替worker证据。原任务壳由既有 `tests/tasks/test_business_tasks.py`继续回归。

验收过程包括两线prepare提交唤醒、真实人工WORK_REQUIRED与WAIT混合待办、可靠完工通知独立匹配、保存原释放命令后的整单COMPLETED，以及公共RACK_CYCLE。原SCAN2 Result未到，整单仍可完成。停止并重建worker后，原准入/释放/Transport身份保留；重复唤醒没有第二次完工应用或确认请求。

周期兜底单独建立严格前置：到期WAIT在停用线仍保持一个原准入；恢复worker并处理完全部即时通知，确认 `received=4/succeeded=4/queued=0` 后，才引入漏通知的第二个CTU03接纳及重新启用线。此时原周期仍ACTIVE。Beat日志实际记录5秒后发布原任务，随后同一宿主消费持久待办；WAIT只有一次合法新求值，仍保留原task/arrival/bin/scanned_at，CTU03只释放原WES周期，没有最终outcome或物理位置完成。

保留的代表证据（`m6-worker-beat-verified.log`及其三个子进程日志）：

| 事实 | 原身份/证据 |
| --- | --- |
| worker停止/重建 | PID 87410 → 87442；fixture finally只停止所拥有进程，退出后核对无遗留 |
| 原WAIT arrival | `01a199ac-7ee3-7a14-8b77-bad518ae1924` |
| 原求值 → 合法到期新求值 | `01a199ac-7eef-7e04-a495-de9e33b8b3c0` → `01a199ac-9e10-7654-9a0d-bdb0d7b863a3` |
| 原完工释放命令 | `01a199ac-7fd2-7f28-ae97-d37a0000f4f1`，重建后仍为原smt_manual命令 |
| 重建保留的Transport | `transport-01a199ac-8109-7115-aac7-d05ca3bb0426`，冻结正文不变、outcome_revision为空 |
| 仅Beat恢复的接纳义务 | `transport-01a199ac-88b7-7efc-85d0-4f893dccf506`，接纳后漏通知，原周期在tick后RELEASED |
| 实际Beat发布 | 16:33:04启动，16:33:09出现 `Sending due task advance-business-smt_manual (business.advance_pending)` |

上述三个子日志已复制至W的 `m6-worker-beat-verified-worker-0.log`、`m6-worker-beat-verified-worker-1.log`、`m6-worker-beat-verified-beat-2.log`，不依赖临时目录长期保留。早期worker通过记录尚未排除即时队列竞争，**不计作Beat验收**；名称相近的 `m6-beat-proven.log`也是补断言前的中间运行，以本节明确列出的日志为准。

## 5. 当前验证结果与环境分层

| 检查 | 本次结果 | 证据 |
| --- | --- | --- |
| M6正式边界与worker专项 | 13 passed / 0 skipped；其中进程兜底收紧后单独再验1 passed / 0 skipped | `m6-final-focused-pass.log`；收紧后的最终worker为 `m6-worker-beat-verified.log`，10.94秒 |
| M6修复前历史受影响回归 | **1465 passed / 27 skipped，250.84秒，退出0**；包含该快照worker、M1～M6、BIN_Roller、ECS、submit通知、各原owner唤醒、原任务壳和after_commit事务 | `m6-final-affected.log`；对应worker/Beat日志复制为 `m6-final-affected-{worker-0,worker-1,beat-2}.log`；不是第6节修复后的验证 |
| M6修复前历史静态检查 | `ruff check .`通过；`ruff format --check .`为630 files already formatted；`git diff --check`通过 | M6修复前源码快照；本轮最终完整静态检查见第8节 |
| 迁移及清理 | 唯一head `0177313880f9`，28张正式表保留、正式行与本轮拥有的资源行均为零；无拥有的worker/Beat遗留 | `m6-alembic-heads.log`、`m6-schema-cleanup.log`及fixture退出核对 |
| 最终全仓/整体复核 | **4933 passed / 52 skipped，556.14秒，退出0**；整体及两次明确增量的复核均闭合，无未解决阻断项 | `m36-final-full2.log`及第8节；首轮失败原样保留，不计作通过 |

27项跳过来自既有SQLite行锁、唯一键仲裁、独立连接、SERIALIZABLE/延迟commit等不支持的变体，未当作通过；对应配置的PostgreSQL测试实际运行。M6新增四个文件没有skip。测试预期注入的通知ConnectionError在after_commit日志中留证，原事务仍提交；该日志不是运行失败。所有pytest运行串行，无其他套件与真实worker夹具重叠，活动运行期间未修改产品或测试源码。

| 环境/交付层级 | 状态 |
| --- | --- |
| 本地严格合同/实际flow/组件 | 已执行软件验证 |
| 独占PostgreSQL/Redis、默认迁移表 | 已执行；与迁移专用库及运行库区分，回归后保留schema、清理本轮数据 |
| 真实broker/worker/单Beat与本机WMS/ECS模拟peer | 已执行；ACK、submit接纳、最终应用与周期释放分开断言 |
| 共享ST1/ST2合并 | 沿PR #44已有证据 |
| M1～M6源码提交、PR、合并、部署、运行库升级 | 未执行 |
| 真实WMS/ECS/RCS合同签认、正式地址/资源绑定联调 | 未验；本机模拟不能替代对端签认 |
| 现场搬运/离场/物理完成与供应商验收 | 未验；CTU03 RECEIVED/DUPLICATE不代表物理离场或运输完成 |

## 6. 整体审查最终修复波

2026-11-01按W的 `whole-final-review.md` 一次处理 I1 Important 与三个 Minor。修复仍未提交；完整实现/TDD/自检报告为W的 `whole-fix-report.md`，不提前代替控制任务的一次定点复审、最终全仓或完整静态 gate。M1/M2历史验收数字及上节M6修复前1465/27保留各自原快照含义。

- **I1：** 完成资格和 arrivals 语义基准只排除 `state=HOLD` 且从未发起准入的原留证。NOREAD/错面后合法新经过独立完成 WORK_REQUIRED，原任务可以确认；旧 HOLD 的整条人工作业记录保持、没有准入和释放，只有合法经过取得原唯一释放。DEFERRED 无请求、真实准入未决、WAIT、WORK_REQUIRED、未应用或 REVIEW 完工继续沿原规则阻断；供箱公开进度与原新事实/确认竞态复核不变。
- **Minor M1：** 补强原真实worker重启测试，按重启前 arrival_id 回查当前 manual_bin 及唯一 scan2_release，两者都仍指向保存的原 command_code；保留原idle前置、实际Beat到期发布和finally进程清理。
- **Minor M3：** mock精确匹配现有 `rack.departure_decide@v1` / `workline.return_buffer.drain_rack_decide@v1`；清尾WAIT还补齐原合同必填的 `reason_code=NO_DRAIN_RACK_AVAILABLE`。参数化验证经正式注册请求模型、原outbox/Sender、本机peer、公开WMS结果读口及原DECISION得到严格合法WAIT，没有重复合同或另建调度路径。
- **Minor M2：** 保留已知非阻断测试输出限制。代表验收主日志开头三条缺目标警告来自pytest进程导入dev console、fixture尚未安装本轮本机targets的阶段，并非子worker配置或实际发送失败。原fixture在发送前断言三个WMS协议都指向同一 `127.0.0.1` origin，并检查全部拥有的DeviceRoute；子进程明确携带同一targets和独占PG/Redis。本轮三个子日志没有缺目标警告。未改生产默认targets，未全局压制或删除历史警告；故意注入的after_commit ConnectionError继续作为恢复证据保留。

| 本轮验证 | 结果 | 保留日志（W内） |
| --- | --- | --- |
| I1功能RED | **2 failed**：NOREAD与错面均在合法新作业结清后仍有0条原确认请求 | `whole-fix-i1-red.log` |
| M3功能RED | **2 failed**：两个正式operation得到空决定；只改拼写后 **1 passed / 1 failed**，清尾WAIT明确缺reason_code | `whole-fix-m3-red.log`、`whole-fix-m3-reason-red.log` |
| 完成/供箱/确认竞态/SCAN2/准入/WAIT/完工及正式组件边界 | **138 passed / 0 skipped，37.66秒，退出0** | `whole-fix-affected-green.log`；八个原测试文件，完整命令见报告 |
| 补强后的原真实worker/Beat | **1 passed / 0 skipped，10.69秒，退出0** | `whole-fix-worker-green.log`；三个子日志为 `whole-fix-worker-green-{worker-0,worker-1,beat-2}.log` |
| 本轮六个Python修改文件 | Ruff检查通过、格式检查6 files already formatted | 本轮定点检查；不代表全仓静态gate |
| 定点复审、最终全仓及完整静态检查 | 已完成，见第8节 | 本节138+1为当时定点证据，未代替后续真实全仓 |

本轮worker PID为18850→18869，退出后按PID核对均不存活。启动Beat前即时任务已达到 `received=6/succeeded=6/queued=0`；Beat在17:06:05实际发布原 `advance-business-smt_manual`。重启前原arrival `01a199ca-a84d-7ceb-a7fc-b07f8d8728d5` 的人工作业和唯一释放仍指向命令 `01a199ca-a955-701d-86e2-b37606cf133b`，该释放仍pending。原WAIT仅到期形成一次合法新求值，两个原CTU03义务的接纳恢复不等于物理完成。所有pytest串行执行，活动测试期间未修改源码或测试。

## 7. 首次最终全仓失败与验证修复

第6节修复波和其定点复核结束后，控制任务执行的首次最终全仓得到 **16 failed / 4858 passed / 52 skipped / 59 errors，551.08秒**，原日志为W的 `m36-final-full.log`，75个失败/错误块清单为 `m36-final-full-failures.json`。该次运行前后549个源码/配置输入hash一致，不存在源码漂移或重叠pytest；**该结果不是通过验收**，不以此前138项或M6历史1465项通过替代。

根据W的 `verification-diagnosis.md`，控制任务批准10文件最小修复；实现与完整命令/输出见 `verification-fix-report.md`。原I1完成边界、worker原身份和严格WAIT修复均保留。

| 全仓失败组 | 根因与本次处理 |
| --- | --- |
| 63个catalog ownership块 | 临时catalog的公共业务夹具复制全部生产callbacks，另一个lifespan测试漏隔离callbacks；只修测试注册表隔离，保留平台Transport回调和生产所有权guard |
| 1个metadata-only冷进程失败 | BIN_Roller扫码入口、三个manual回调及共享sorting_task服务在导入期加载runtime helpers；只移到原handler或_resolve执行边界，不降低冷进程guard |
| 1个显式runtime装配失败 | 旧测试未传共享输入resolver；只补catalog.input_target，保留生产必需resolver规则 |
| 8个开发请求400 | 新正式operation未声明可选example，旧测试把None作为data期待成功；保留全部参数，None验证400且查无入队，有效示例保留200/PENDING。合法shared payload的无绑定/非consumer拒绝仍验证通过，未增加默认owner或目标 |
| 2个缺平台入口误报duplicate SCAN4 | manual/sorting夹具直接pop模块，但还原原注册表，造成module-cache不一致；恢复进入前存在的同一module对象，只移除夹具首次引入项。缓存request模型沿已有方式重新登记，避免reload改变原类身份；生产loader/重复注册拒绝不变 |

| 修复后检查 | 实际结果 | W内日志 |
| --- | --- | --- |
| 精确定点组合：原75块拥有文件及执行态manual/sorting回调、完整导入/来源负向guard | **451 passed / 1 skipped，63.92秒，退出0**；共452项 | `verification-fix-affected-green.log` |
| 原污染顺序：NOREAD回归后立即检查两个缺平台入口参数 | **3 passed / 0 skipped，0.71秒，退出0** | `verification-fix-order-green.log` |
| 10个Python修改文件 | Ruff检查通过；格式检查10 files already formatted | 本轮定点静态检查，不代表全仓静态gate |
| 修复后最终全仓、完整静态检查及此增量复核 | 已由控制任务完成，见第8节 | 本节451+3为修复者的定点结果；控制任务另跑最终全仓 |

唯一跳过为既有 `sorting_task/test_dispatch.py::test_business_wakeup_waits_for_outer_commit` 的SQLite无外层事实SAVEPOINT变体，原因为 `fresh caller root SAVEPOINT has legacy SQLite early-commit semantics`；对应PostgreSQL例在定点集合实际运行。未增加skip或删减原operation参数。

另保留一条不作为通过证据的收集失败：最初交错目录参数触发manual_sessions不在fixture闭包的1个collection error，未执行测试；源码不变，将同一文件集合按目录相邻排列后收集452项并完成上述451/1结果。原失败、收集核对及最终运行日志均保留。第6节已说明的导入期缺目标警告继续保持非阻断输出限制，没有修改生产targets或全局压制；本轮未访问真实供应商/设备或运行库。

相关合同：[人工线API](../integrations/manual-picking-api.md)、[业务设计](../superpowers/specs/2026-10-31-smt-manual-business-design.md)、[实施计划](../superpowers/plans/2026-10-31-smt-manual-implementation.md)、[BIN_Roller公共API](../integrations/workline-bin-return-api.md)、[业务包装配](../integrations/business-packages.md)。

## 8. 最终软件验收与收尾

最终全仓在上述验证修复及明确增量复核后的冻结源码上运行，`m36-final-full2.log`记录 **4933 passed / 52 skipped in 556.14s，退出0**（runner 563.02秒）。本次覆盖原全部参数，未靠删除用例、跳过失败、放宽注册/归属/重复检查或更改完成合同过关。首轮 `m36-final-full.log` 的16 failed/59 errors和中间收集失败保留为非通过证据；原因及修复沿第7节复核，不另起一轮开放式整体审计。

| 最终门槛 | 实际结果与证据 |
| --- | --- |
| 完整测试 | 4933 passed / 52 skipped，556.14秒，退出0；`m36-final-full2.log` |
| 源码一致性 | app/tests/migrations/scripts、配置、运行器与独占测试配置共558项，运行前后逐文件SHA-256完全一致；`m36-final-full2-before.json` / `m36-final-full2-after.json`，汇总 `ad5ff0b4843932a5ef8bca7b339a0a6c8ccd2d3297ff54ab1ea2b709c3737ffc`；其后仅更新状态文档/账本 |
| 跳过清单 | 52项与M2最终基线按路径、原因和数量完全相同；`m36-final-skip-comparison.json`。包含既有数据库差异变体及5项未配置旧迁移专用库，不算通过；本次新增业务及组件/宿主迁移的已配置PostgreSQL分支实际执行 |
| 完整静态 | `ruff check .`、`ruff format --check .`、`git diff --check`均退出0；格式检查记录631 files already formatted |
| 独立复核 | M3～M6任务门槛、整体审查后的I1等四项处置、最终验证修复五组接缝均有独立报告；`whole-fix-rereview.md`、`verification-fix-rereview.md`均无新增阻断或范围外发现 |
| 实际schema | 唯一head `0177313880f9`；原链 `a39a1988a81a → ac980eb32c52 → 0177313880f9`。可丢弃库内15张组件表、13张宿主表完整、业务行数0；WorkLine/Device/DeviceRoute/EcsCommand/ReliableMessage剩余行均0，`m36-final-resource-check.json` |
| 最终真实进程证据 | 最后全仓再次执行同一非eager worker/Beat测试；三个原子进程日志保存在 `m36-final-full2-worker-0.log`、`m36-final-full2-worker-1.log`、`m36-final-full2-beat-2.log`，含实际原任务due emission；收尾无该轮worker/Beat进程，`m36-final-process-check.json` |
| Git与既有工作 | HEAD仍为 `05bcc750215790c91c43e94d1aae1f127fb12549`，分支未切换；`.serena/project.yml`与M3起点逐字节一致。收尾观察到89项暂存内容，未查明触发来源；保留原暂存区并在W备份index和staged patch，未新增提交/推送/合并，详见 `m36-final-git-state.json` |

三条pytest导入期dev-console缺target告警仍按第6节限定为已知非阻断输出；没有改生产默认target或全局压制。实际worker fixture与子进程的loopback配置、原身份、实际Beat发布和独立履约均按已有测试验证。发送方仍须保持技术重试的原operation_id/timestamp/正文；本系统不能从合法的新身份/改写正文判断其外部真实意图。

运行库未升级；将来运行本版本前需按已验证的迁移链升级，当前记录不授权部署。供应商签认、真实WMS/ECS/RCS联调及现场物理验收保持未执行；SCAN2原释放、原Transport和公共回流独立履约，任务COMPLETED或CTU03提交接纳不证明物理完成。测试容器清理结果在下段收尾记录登记，工作区、未提交源码和忽略目录内全部证据保留。

收尾结果：仅停止并自动移除本轮拥有的 `codex-smt-manual-m36-pg` / `codex-smt-manual-m36-redis` 两个专用容器，移除后按精确名称检查无残留；没有清理其他服务、主目录、工作区、源文件或证据。真实worker/Beat在容器停止前已确认退出。分支与完整未提交工作继续保留。

## 共享货架调度切换

1. 关闭新业务入口，停止所有旧版 API/receiver、worker、Beat 和 Sender。等待已启动事务结束，逐笔核对在途原 HTTP、回调、冻结请求与接纳身份，保留尚未核清的证据。
2. 对静止数据库做一致备份，在可丢弃副本从 `racksubmitack` 演练升至 `sharedrackdispatch`，核对原作业、动作、报告、候选、消息数与 WAIT 截止时间。无法证明前驱或归属时保留源库和原证据，定位原始记录后重新验算。
3. 运行单链升级，在同一事务内回填、核对并移除旧调度表。确认 head、核对计数、原请求字节及外发数。将全部进程切至同一制品，确认配置、版本和唯一 Beat 后恢复接收、Sender 与推进。
4. 升级失败时事务回滚。若已按新语义写入数据或已有受保护接纳证据，降级会在首个破坏操作前拒绝。保持停写，恢复切换前一致备份，并逐笔对账切换窗口内的外部请求和回调后再开放。

这些步骤用于部署窗口。当前分支仅在可丢弃数据库验证，真实数据及设备行为须另行验收。
