# KT16 / KT18 用户配置专项验收

<a id="current-factory-profile"></a>
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

**本次新跑127 passed / 0 failed / 0 skipped：已覆盖业务路径PASS，源配置全量兼容仍未闭合。** 永久工厂13项＋额度/架周期104项＋真实worker2项为119 passed（65.64秒），两线入库NO_WORK/NORMAL/NG/WAIT补充8项为8 passed（5.85秒），两组exit0。两线按启用MANUAL、源运行模式AUTO、version31/7装配，实际业务任务覆盖inbound和outbound；源输入经独立逐字段核对，未修改产品或永久测试。

本次证据目录 **R**：`.superpowers/sdd/2026-11-02-smt-manual-kt-lines-reacceptance/`。完整字段判定及范围见R的 `acceptance-report.md`，原始运行见 `formal-acceptance.log/.exit`、`inbound-acceptance.log/.exit`；`case-manifest.json`保存21份配置evidence及cleanup，546项清理计数均0。`audit-store.json`只读确认当前head fiveracklimit、SMT7/BIN_Roller10、17表零行；`preservation.json`确认报告更新前671个原文件、暂存补丁和HEAD一致。

| 本次验收项 | 新证据判定 |
|---|---|
| 两线启用、MANUAL、版本、12设备/4SCAN角色/5位置及两线箱码隔离 | PASS；源AUTO不是自动任务类型，旧ID仅审计映射 |
| INLET/OUTLET与RETURN/TRANSFER完整架号和目标 | PASS；沿当前HANDOFF_POSITION/RACK_POSITION合同，未增扫码工作点或上游链 |
| 额度3及单个补位 | PASS；累计CTU01 3/3→4/3，满额追加计划仍4/3，第二个不同CTU03接纳后5/3；10个Transport/10次loopback submit，CTU03均无最终outcome |
| 两线入库、出库准入/WAIT/箱级完工/唯一原释放 | PASS；入库新增8例，不等待SCAN2最终Result |
| 停启线、真实owner唤醒和Beat恢复 | PASS；worker2例与直接宿主推进证据分开保留 |
| 指令timeout30秒 | PASS；21配置例24条实际命令timeout均30000 |
| third_party_integration@1.1、Status10秒 | UNVERIFIED/未消费源配置映射；当前编译annex版本1不等同供应商1.1 |
| 设备enable/deleted/upstream及工作点其余容量/priority/rack_type/logic/external等元数据 | PARTIAL；目标无全部同名字段，位置代码和现有用途已验，未声称支持全部配置策略 |

源私网地址只作审计，所有实际请求在冻结前配置为loopback，未请求工厂端点。本次没有重复全仓；下文5145/47及历史失败保留上轮范围。新跑结果不代替供应商、Status或未消费字段的验证。没有提交或部署。

## 上一轮：收敛及额度修复后的永久专项

**本轮软件验收 PASS：原 12 项配置规则与新增停启线 1 项，共 13 项场景在最终全仓通过；最终全仓 5145 passed / 47 skipped，965.32 秒，exit 0。** 下文首次永久专项 12/0、原 11 PASS/1 FAIL、5/5 → 7/5、13 次提交及旧 4933/52 保留各自历史时点。当前增量未提交、未部署，未操作运行库或源私网端点。

当前证据目录 **P** 为 `.superpowers/sdd/2026-11-02-smt-persistence-and-capacity-implementation/`。永久资产为 `tests/businesses/smt_manual/test_factory_profile.py`、`factory_profile.py`、`factory_source_profile.json`，由原 `tests/conftest.py::pg_sessions` 和本包 `conftest.py::profile_flow` 组装，不依赖忽略目录或顶层 pytest_plugins。独立运行只需 TEST_DATABASE_URL 指向可丢弃 PostgreSQL；未设置时跳过，已设置但连接/迁移失败则失败。

以下命令及输出保留首次永久专项的 12 项快照；当前代码增加了停启线场景，共 13 项。

```text
rtk uv run python .superpowers/sdd/2026-11-02-smt-persistence-and-capacity-implementation/run_checks.py task4-factory-final2 python -m pytest tests/businesses/smt_manual/test_factory_profile.py -vv --tb=short --basetemp .superpowers/sdd/2026-11-02-smt-persistence-and-capacity-implementation/task4-final2-pytest
```

原始输出 `task4-factory-final2.log/.exit`；`task4-case-manifest.json` 指向全部12例逐项 evidence/cleanup。每例清理 17 张业务表与平台资源均为0，`task4-store.log/.exit` 再次通过只读 RR 核对真实表数7/10、head=fiveracklimit、17表0行。测试只连接本轮独占 PostgreSQL/Redis 与意图冻结前装配的127.0.0.1 loopback，源私网地址仅为审计输入；没有改已冻结路由或请求。

| 当前验收项 | 数量/结果 | 本轮真实范围 |
| --- | --- | --- |
| 源配置、相同箱各归其线/任务 | 1 PASS | KT16/KT18均 enabled、MANUAL/outbound，源版本31/7；各6设备、4扫码角色、5位置、无上游/扫码位置。创建即显式保存额度3，不靠管理更新伪称版本不变 |
| WAIT→WORK_REQUIRED→NORMAL/NG原释放 | 4 PASS | 两线×2结果；截止前不重问，到期新operation但原arrival不变；重复完工/唤醒只一条原释放，不等SCAN2最终Result |
| 独立SCAN3/SCAN4/FIFO/OUTLET | 2 PASS | 无任务/SCAN2亦受理；第二箱先成功不能跳队头，第一箱自身SUCCESS后才得连续前缀；实际BIN_MOVE源为本线CNV0302/CNV0402 |
| CTU01/INLET及不同RETURN/TRANSFER架 | 2 PASS | 来源到KT16/18，投箱目标CNV0301/0401；完整冻结三元组 `(rack_id,target.kind,target.location_code)` 精确对应RETURN→OUT55/56、TRANSFER→OUT65/66，不能互换目标，两架ID不同 |
| CTU03接纳且无最终结果 | 2 PASS | 真实CTU01到位、空面/离场决定、原Sender和Transport；有效202/RECEIVED仅释放原周期一次，outcome_revision/applied_outcome仍空，重复并发保留原CTU03 |
| 额度、追加计划和重复并发补位 | 1 PASS | 每线初始5需求但只登记/提交3，默认页100、无限制合法peer；首次KT16原CTU03接纳补至4/3，满额时plan_revision2增第6/7来源仍4/3且原ID不变；第二个不同原CTU03接纳才5/3，两个均无最终outcome。最终10条Transport/10次submit（8 CTU01+2 CTU03） |

本永久容量用例在追加 revision2 前也检查既有等待项无需新计划即补一位。更完整的无新计划与真实 worker/Beat 证据属于 Task3：`task3-profile-green2.log` 的容量探针及 `task3-worker-green.log`/`task3-final-worker-route-restore.log`，分别证明 owner 通知与故意漏通知后的 Beat 兜底；本轮未另开 worker 重复验证。不能把永久用例的直接 service 推进写成真实 worker 证明。

当前仅SMT/BIN_Roller为7+10表，sorting_task无表；单链 `0177313880f9 → smtmanualcompact → binrollercompact → fiveracklimit`。配置 API 的 nullable int、省略/null、version语义和请求响应见[管理配置](../integrations/manual-picking-api.md#manual-request-limit)；降级拒绝非空额度及开始前WITHDRAWN服务。该撤销服务在有效兄弟继续并 RELEASED 后仍保持 WITHDRAWN、false/false、不列pending FEED；不伪造COMPLETED。

最终复核在上述 12 项永久配置场景外增加停启线 1 项。管理服务把 KT16 额度设为 1，在原架占用且第二架已选时停用 KT16。原 CTU03 接纳和 KT18 的叫架继续，KT16 重启用后沿原 cycle/service 恰好补一次。公共 drain 的未使用状态及降级约束见[当前收敛验收](smt-manual.md)。

FIFO 按首次有效 SCAN4 received_at/id 排序；最新有效 SCAN3 仅确定当前物理轮次，包括尚未绑定新根的情况，不能把二者混成同一排序依据。源 AUTO 是运行元数据；源设备1.1合同、Status10秒和未消费的位置容量/优先级等仍为 UNVERIFIED/PARTIAL，后文源字段审计的这些限制继续有效。

首轮迁移后的永久测试为6 PASS/6 FAIL，均是旧 admission/supply_demand 物理查表键未适配（`task4-factory-verified.log`），随后直接改成 request.kind/face_work/scan_arrival具名列，未建旧表代理或改产品规则。首次 runner 未进虚拟环境导致缺pytest，仅为运行环境错误（`task4-factory.log`）。没有把这两类夹具问题记为产品容量失败。原启动三条本地dev console缺target警告与Task3故意丢通知的ERROR分属不同来源，均保留原日志。

最终修复及验证：`final-fix-related.log` 320 passed / 0 skipped 覆盖迁移、额度、排空及全部 13 个工厂配置场景。`final-full4-pytest.log/.exit` 为 **5145 passed / 47 skipped，965.32 秒，exit 0**；`final-full4-factory-manifest.json` 和 `final-full4-factory/` 保存最终 13 份 evidence/cleanup，338 项清理计数均为 0。KT16/KT18 累计 CTU01 仍为 3/3 → 4/3，满额追加 revision2 两来源不增加，第二个不同原 CTU03 接纳后才 5/3；最终 10 个 Transport、10 次 submit，两个 CTU03 均无最终 outcome。停线期间旧 CTU03 继续消费、KT18 照常推进，KT16 未启动候选等待重新启用后沿原 cycle/service 补一次。整体复核发现、同一波修复与测试前置稳定化的完整历史见[软件验收记录](smt-manual.md#current-persistence-capacity)，不把修复前的中间全仓结果当作最终结论。

## 历史专项：原 11 PASS / 1 FAIL（未修额度）

源时点：**北京时间2026-11-02 09:29**。验收对象是工作区 `feature/smt-manual-m1` 的当前实际源码，HEAD `05bcc750215790c91c43e94d1aae1f127fb12549`。本轮仅新增验收harness和报告，产品、catalog、模型、guard、迁移及原测试均保持原样。

**业务验收结论：FAIL。** 最终专项为 **11 passed / 1 failed / 0 skipped，12.94秒，退出1**。两个实际工作线均可按MANUAL出库处理，准入/人工完工/公共回流和CTU03提交接纳释放原周期得到通过证据；每线CTU01队列容量3不满足：首次各5个独立来源架均产生并提交原Transport。仅KT16释放一个周期后再增加两个来源架，CTU01累计为KT16=7、KT18=5，未体现容量3和恰好一个补位的限制。任务已忠实执行，测试没有将失败转为xfail/skip，也未实现新限额。

本轮工作目录简称 **W**：`.superpowers/sdd/2026-11-02-smt-manual-kt-lines-acceptance/`。完整输入为W的 `source-profile.json`；执行报告为 `AT1-report.md`，最终原始日志为 `at1-final-verified.log`。W是保留于本工作区的忽略目录，不表示已提交交付。

## 1. 输入映射及真实消费范围

源快照由用户提供；没有查询或迁移旧wes_integration库。源大整数ID只用作审计引用。本项目采用自己的自动主键，每个用例的实际映射与loopback origin都保存于 `<test_name>.evidence.json` 的 `mapping`，汇总见W的 `case-manifest.json`、`evidence-summary.json`。

| 源配置 | 本项目实际映射/消费 | 判定 |
| --- | --- | --- |
| KT16，源ID `348950323769920`，version31，enabled=true | `WorkLine.line_code=KT16`、`version=31`、`is_enabled=True`；最终上下文用例local_id=57，容量用例local_id=79 | PASS；本地主键不是旧库数据迁移 |
| KT18，源ID `361883088622144`，version7，enabled=true | `WorkLine.line_code=KT18`、`version=7`、`is_enabled=True`；最终上下文用例local_id=58，容量用例local_id=80 | PASS；未沿用旧停用状态 |
| business_type=MANUAL，plugin=`manual-picking@0.1.0`，runtime_mode=AUTO | 当前正式身份 `business_code=smt_manual`；通过实际receiver发出 `task_type=MANUAL`、`business_type=outbound`，两线原prepare payload实际携带KT16/KT18 | PASS（任务模式）；plugin/runtime_mode是源字段，目标没有同名消费入口，未伪造目录身份或AUTO任务 |
| 每线6设备，INLET/OUTLET无device contract | 每线6个实际 `Device.work_line_id` 关联、6个新 `DeviceRoute`；只有SCAN1～4进入 `device_bindings`，正式catalog按这四个角色授予命令合同。INLET/OUTLET不产生命令 | PASS（关联与四角色）；目标没有逐设备 `has_contract` 字段 |
| 设备enabled=true、deleted=false、upstream_device_id=null | 建立现存Device配置；目标没有这些同名列或上游链。实际四点流程无需补充上游关系 | PARTIAL（源元数据无对应存储）；无上游链流程PASS |
| 源INLET/OUTLET为STATION | `position_bindings`保存实际编码；实际BIN_MOVE的源/目标按业务合同生成 `HANDOFF_POSITION` | PASS，见第2节 |
| 源FIVE/RETURN/TRANSFER为RACK_POSITION | 保存实际编码；CTU01及人工生产架原请求使用 `RACK_POSITION` | PASS，见第2节 |
| FIVE_RACK.capacity=3 | 没有新增position表、容量字段或测试计数器；以每线5个独立rack的真实注册/submit行为验收 | **FAIL，见第4节** |
| 其余position.capacity=1、priority=100、rack_type、enabled、logic_location_code、external_location_code=null、device_code | 目标WorkLine仅存角色→编码；rack用途/Transport种类由原业务代码选择。没有位置实体来逐项消费这些源字段；logic code恰好等于实际code不能当作另一个字段已实现 | PARTIAL/源元数据；位置单容量、优先级策略和external映射未验，不宣称支持 |
| command_timeout_ms=30000 | 检查实际保存的 `EcsCommand.request_raw.timeout=30000`；SCAN1/SCAN2命令和人工释放均覆盖 | PASS；ECS技术重试有效期另为300秒，不与30秒命令timeout混淆 |
| device_contract=`third_party_integration@1.1` | 当前实际route保存 `annex_id=bin_roller.move_forward` 等、`annex_version=1`，由当前catalog编译合同 | UNVERIFIED（源合同1.1没有同名目标字段）；本地mock不证明供应商1.1兼容 |
| status_max_age_ms=10000 | 当前工作线/设备/路由/命令模型和本轮业务入口不消费该字段；设备Status为独立查询 | UNVERIFIED；未构造假Status有效期或以在线状态替代业务证据 |

源地址 `http://10.24.209.26:8080`仅保存为审计值。最终上下文用例实际origin为 `http://127.0.0.1:53571`，最终容量用例为 `http://127.0.0.1:53658`；其余10例各自origin见 `case-manifest.json`。所有配置均在首次意图创建前完成；未改写已冻结消息/route来重定向。三个WMS协议目标和全部DeviceRoute在创建意图前及发送前核对loopback，动态ECS客户端使用原 `simulator_client_factory([origin])` 精确允许列表。没有访问源私网地址。

## 2. 设备与位置逐项覆盖

| 工作线 | 6个实际关联设备；括号内为唯一命令角色 | 5个实际位置 |
| --- | --- | --- |
| KT16 | STATION_INPUT3、STATION_OUTPUT3、STATION_SCAN9(SCAN1)、STATION_SCAN10(SCAN2)、STATION_SCAN11(SCAN3)、STATION_SCAN12(SCAN4) | FIVE_RACK=KT16；INLET=CNV0301；OUTLET=CNV0302；RETURN_RACK=OUT55；TRANSFER_RACK=OUT65 |
| KT18 | STATION_INPUT4、STATION_OUTPUT4、STATION_SCAN13(SCAN1)、STATION_SCAN14(SCAN2)、STATION_SCAN15(SCAN3)、STATION_SCAN16(SCAN4) | FIVE_RACK=KT18；INLET=CNV0401；OUTLET=CNV0402；RETURN_RACK=OUT56；TRANSFER_RACK=OUT66 |

两个 `test_ctu01_inlet_and_distinct_return_transfer_positions` 用例通过实际issued、plan_delta和host pending产生三类生产Transport：来源架CTU01到KT16/KT18；不同的RETURN架到OUT55/OUT56；不同的TRANSFER架到OUT65/OUT66。实际CTU01 Result经原receiver应用后，WMS分配一个箱，原FEED产生的BIN_MOVE目标精确为CNV0301/CNV0401，种类为HANDOFF_POSITION。RETURN和TRANSFER始终使用不同rack_id；本轮没有同架双角色或比较其wire不等的场景。

两个公共回架用例的原Transport.moves.source精确为CNV0302/CNV0402、HANDOFF_POSITION。所有位置证据来自实际 `TransportTask.frozen` 与原请求；不存在SCAN2位置，也未补扫码位置或上游链。

## 3. 分项结果

| 验收规则 | 结果 | 实际证据与范围 |
| --- | --- | --- |
| 两线enabled、版本31/7、MANUAL/outbound、真实catalog与两组件 | PASS | `test_source_context_and_same_bin_route_to_own_scan2`：原 `business_tasks._runtime` 创建唯一SmtManualService；Task及实际prepare payload包含各自工作线 |
| 相同箱码经STATION_SCAN10/14分别归属本线/本任务 | PASS | 两条不同arrival、task、command；实际命令device_code和保存request_raw分别对应这两台设备；原NO_WORK各释放一次 |
| 四扫码绑定、六设备关联、五位置、无SCAN2位置/上游要求 | PASS（可表示部分） | 所有配置来自源快照；SCAN1/2实际命令、独立SCAN3/4回流分别覆盖四角色；源额外元数据边界见第1节 |
| WAIT未到期不重问；到期新求值保留原arrival | PASS | 两线×NORMAL/NG共4例：200ms WAIT在截止前保持一个admission，到期才产生第二个operation_id，仍指向原arrival |
| WORK_REQUIRED+NORMAL/NG、原唯一释放、无需SCAN2最终Result | PASS | 每例真实 `manual_bin.work_completed@v1` receiver及host pending；重复通知/唤醒仍只有一条原scan2_release、同一command_code；ECS applied_result_id为空 |
| SCAN3独立于任务和SCAN2 | PASS | 两线均无任务、无SCAN2也受理两个实际SCAN3/SCAN4 passage |
| SCAN4自己的SUCCESS及连续FIFO | PASS | 只给第二箱SCAN4 SUCCESS，候选仍空；第一箱自己的SUCCESS后，候选按第一箱→第二箱顺序。未给SCAN3 Result也未借其他线Result赋予资格 |
| OUTLET及回架分配/Transport | PASS（含明确前置） | READY回架face用原free helper明确建立为测试前置；之后原WMS分配、RETURN_BATCH advance和Transport真实执行，冻结source为本线OUTLET。未将前置称作自动到架证明 |
| RETURN/TRANSFER生产位置分离、INLET交接位 | PASS | 两线各一个用例；不同RETURN/TRANSFER rack_id经真实plan建立原生产请求；INLET原BIN_MOVE见第2节 |
| CTU03有效submit接纳释放原周期、不等最终Result | PASS | 两个独立用例均由真实CTU01 Result→WMS RACK_FACE_DONE→原departure READY→原CTU03走完。RECEIVED/202保存后周期RELEASED，release_basis.outcome_revision为空，CTU03 applied_outcome为空 |
| 重复/并发pending保留原CTU01、CTU03身份 | PASS（幂等） | 容量用例两轮重复加一轮并发pending保留10个原CTU01 action_id；独立CTU03用例重复并发后仍一条原CTU03 |
| 每线CTU01队列容量3，第四请求等待CTU03接纳 | **FAIL** | 每线5个独立来源架第一次扫描即均注册/submit；在任何CTU03前已超过3 |
| 一次CTU03接纳恰好释放一个额度并补位，重复不超额 | **FAIL** | 容量前置已经不满足。完整后续trace中KT16一个周期释放后，新增两个独立需求均提交，累计7；不能将其称作按3额度补位通过 |
| 两线额度独立 | PARTIAL | task/caller/target归属分离，单动KT16后KT18请求数保持5；由于两线都未满足上限3，不能声称额度隔离已通过 |
| 新真实worker/Beat、Status10s、供应商合同与物理验收 | UNVERIFIED | 本轮不重跑worker，不以既有4933条或本地mock替代本配置新证据 |

## 4. 容量失败的原始事实

测试peer对所有提交无限制返回合法 `202/RECEIVED`。测试没有自设三额度、没有limit=3分页、没有通过客户端并发数冒充预算。实际host.scan_pending采用默认页大小100，每个advance仍由原产品独立事务运行。

1. 两线分别通过实际issued/prepare及plan_delta发布 `AT1-KT16-QUEUE-1..5`、`AT1-KT18-QUEUE-1..5`，共10个不同来源架。
2. 第一次host pending及原Sender后，CTU01为KT16=5、KT18=5，全部存在原Transport和已接纳的可靠submit。两轮重复及一轮并发pending没有新增原CTU01身份，但也没有把第四、第五个拦住。
3. 只推进KT16第一架：真实CTU01到位Result、RACK_FACE_DONE、departure READY产生CTU03；原submit `RECEIVED/202`接纳使该周期RELEASED，CTU03无最终outcome。
4. KT16 plan_revision2增加第6、7两个不同来源架，再执行并发/重复pending及原Sender。最终CTU01累计KT16=7、KT18=5；整体为12个CTU01+1个CTU03，**13条原Transport、13次实际submit**。没有重建原请求身份。

| 代表原请求 | Transport ID |
| --- | --- |
| KT16第4架（在首个CTU03前已提交） | `transport-01a19a28-f255-74e3-870d-41dacf5e07a0` |
| KT18第4架（在首个CTU03前已提交） | `transport-01a19a28-f2ca-7fc2-a478-4d036aa334cb` |
| KT16第1架CTU03接纳释放 | `transport-01a19a28-f81d-734b-a07a-02ffb8132326` |
| KT16释放后新增第6架 | `transport-01a19a28-fb89-71f0-baa8-cb72dd4a6acd` |
| KT16释放后新增第7架 | `transport-01a19a28-fbae-7686-86f3-bcc7fad50773` |

完整原action/client_request_id/operation_id/Transport/frozen/request_raw/response_raw和接纳依据均保存在W的 `test_capacity_three_distinct_racks_repeated_wakeups_and_replenishment.evidence.json`；摘要为 `evidence-summary.json`。失败断言同时比较 `(初始, 单次接纳后)` 实测 `(5/5, 7/5)` 与应有 `(3/3, 4/3)`。该断言在保存完整后续trace后真实失败，未以首次失败短路丢失接纳证据。

静态追踪只用于解释实测：`rack_flow.select_supply_cycle`按每个不同rack新建周期，`advance_cycle`在没有原CTU01时调用 `rack_action`，当前路径未检查每线请求预算；WorkLine.position_bindings仅存编码。`SmtManualService.scan_pending(limit=100)`的limit控制分页。CTU03的 `apply_rack_result` 确实在原有效submit接纳时释放周期。**容量结论来自上述实际行为，不仅来自缺字段搜索。** 本轮未修复该能力缺口。

## 5. 复现与运行记录

最终完整专项命令（使用W内专用资源配置，不输出配置内容）：

```text
rtk uv run python .superpowers/sdd/2026-11-02-smt-manual-kt-lines-acceptance/run_acceptance.py at1-final-verified -p tests.conftest .superpowers/sdd/2026-11-02-smt-manual-kt-lines-acceptance/test_kt_lines_acceptance.py -vv --tb=short
```

| 日志 | 真实结果 | 解释 |
| --- | --- | --- |
| `at1-smoke.log` | plugin导入错误，未执行用例 | runner尚未把仓库根目录加入PYTHONPATH；仅修W runner |
| `at1-smoke2.log` | 1 failed / 11 deselected | harness误将300秒技术重试有效期当作30秒命令timeout；改为检查保存request_raw.timeout=30000，保留两种时间含义 |
| `at1-first.log` | 9 passed / 3 failed，11.90秒 | CTU03帮助步骤漏推进已有FEED，只推进RACK_CYCLE；属于harness路径不完整，原始证据保留于 `first-run-evidence/` |
| `at1-cycle.log` | 2 passed / 1 failed / 9 deselected，5.97秒 | 改用原host.scan_pending覆盖FEED等kind，两线CTU03通过；容量实际失败 |
| `at1-final.log` | 11 passed / 1 failed，13.14秒 | 完整12项，另有fixture已导入的pytest重写提示；仅调整harness导入顺序 |
| **`at1-final-verified.log`** | **11 passed / 1 failed / 0 skipped，12.94秒，退出1** | 同一最终harness完整12项；唯一失败为真实容量/补位规则 |

所有pytest串行，活动运行期间未编辑产品或harness。W三份Python文件的Ruff检查及格式检查通过；`git diff --check`通过。日志开头保留原dev console在测试fixture安装targets之前的三条缺目标告警；实际意图/发送阶段三个协议均已绑定本轮loopback，没有修改生产默认targets或压制告警。

本轮不重复全仓；既有 `4933 passed / 52 skipped` 保留为原软件快照验收事实，不能代替本次配置专项。

## 6. 保全、清理与未验边界

W的 `product-before.json` / `product-after.json`及 `preservation-check.json`核对**557个原产品/测试/迁移/脚本/配置文件hash全部一致**，HEAD未变，原index与 `start-index.backup`逐字节一致。未stage、commit、push、merge或deploy；未修改原测试/helper，也未新增position表或容量实现。

测试仅使用本轮独占PostgreSQL `127.0.0.1:58162/smt_kt_accept_test` 和loopback peer；配置预留的Redis为 `127.0.0.1:58163`，本轮未启动worker/Beat或向Redis写业务队列。`pg_sessions`通过既有Alembic入口确认已迁移head；没有运行库操作。`schema-after.json`再次确认head `0177313880f9`、28张正式业务表保留、全部业务行和拥有的设备/路由/命令/消息/Transport/收据/evidence行均为0。12个 `<test_name>.cleanup.json`全部为0。fixture关闭唯一Sender与其拥有的loopback peer。控制任务核对12份清理记录全部为0后，仅停止并移除本轮 `codex-smt-kt-accept-pg`、`codex-smt-kt-accept-redis`；按该唯一名称前缀复查无残留。其他资源未动，W内原始证据保留。

未验：真实供应商1.1合同签认、Status10秒有效期、源位置优先级/外部编码/其余位置容量、生产部署及设备物理搬运。CTU03的RELEASED仅为本系统周期释放；不表示物理离场或运输最终成功。INLET/OUTLET和生产架证据是本地实际软件请求与模拟回调，不代表现场验收。

## 7. 独立复核结论

本轮单一验收增量的独立复核：**验收执行及证据Approved，实际业务验收仍为FAIL**。复核逐项关联全部13次submit的原action、Transport、ReliableMessage、peer请求和接纳身份，未发现阻断证据可信度的Critical/Important问题；完整报告保留于W的 `AT1-review.md`。

两项Minor保留：最终日志三条fixture安装目标前的初始化告警已披露；RETURN/TRANSFER目标的自动断言只检查目标集合，尚未逐rack比较角色目标。本轮两线详细原始Transport已人工核对角色与目标正确，故位置PASS有实际证据；后续维护harness可收紧为完整rack/target映射断言。本轮不为此修改产品或重复专项运行。
