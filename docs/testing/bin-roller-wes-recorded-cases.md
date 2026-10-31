# BIN_Roller：WES 现场 BAD / GOOD 记录的当前组件验证

本次将 `wes_backend` 的真实记录取样，经过明确的协议适配，在当前 `maintenance-admin` 的公开 receiver、BIN_Roller、WMS/Transport、ECS Result 入口执行。**这是当前共享组件的场景适配验证，不是原始现场全量 trace replay，也不是人工拣选业务、真实 worker 或设备现场验收。** 原仓库只读；未部署、未修改数据库配置或现场数据。初轮为验证；后续 SCAN3 专项按实际 RED 修复了当前 BIN_Roller 轮次绑定、当前 FIFO 和旧候选决定消费，范围见下文。

## 可重复执行的交付

- `tests/businesses/bin_roller/test_recorded_wes_cases.py`：首轮案例；`test_recorded_scan3_reentry.py`：SCAN3 专项案例。两者复用已有 `conftest.py` 的隔离数据库与公开入口。
- `tests/businesses/bin_roller/fixtures/wes_recorded_cases.json`：无需访问原 checkout 即可运行。保留原文件 SHA-256、原任务/operation/Transport/command/Evidence 身份、真实箱号/槽位/90 与 270 面、24 个原始扫码及其时间。只摘录相关字段，不复制服务器地址、鉴权信息、完整日志。
- `conftest.py` 仅将测试生产接口的 `scanned_at` 从 `Integer` 改为 `BigInteger`：真实毫秒时间戳在 PostgreSQL 上超过 32 位范围。正式组件本来使用足够宽的时间字段；这不是生产漏洞修复。

数据源均相对于只读 `wes_backend` 根目录：

| 来源 | 本次使用与边界 |
| --- | --- |
| `tests/mock/BAD_CASES.md` | 全部一级案例标题逐条评估，见下表；没有把旧验收规则无条件搬进当前组件。 |
| `tests/mock/GOOD_CASES.md`、`good_cases/KT16-A950FC259A78C43DB8D6D82FC110C8B03/{README.md,evidence.json}` | 2026-09-25 真实六箱 GOOD，保留成功路径和异常分支的区别。原 archive 自身尚无 replay runner。 |
| `workline_plugins/manual-picking/tests/fixtures/kt16_a087_a000002668_return_before_rotate.json` | A000002668 → 510004/270/510004B2F2C101，原回架只到 ACCEPTED，随后错误旋转至 90 面。 |
| `workline_plugins/manual-picking/tests/fixtures/kt16_a087_a000002121_cancelled_source_scan2.json` | 原 A000002121、510002/90/510002A3F1C101、原 batch 和成功投料，取消后 SCAN2 原始码 A000002121-C。 |
| `tests/mock/acceptance_cases/KT16-A86FE0A0825564E29B57D2FBB692B9A08/README.md` | A000002701-C 乱序到达、冻结命令和任务结束后返料独立等边界；原档仍有两箱缺返料结果，不能称 GOOD。没有读取其整份大快照。 |
| `tests/mock/data/kt16_manual_picking_good_case.json` | 另一历史任务，27 条已确认决定、5 条补齐 inbound、issued/plan 信封重建；仅核实来源说明，不把它与 A950 六箱混用，不声称 32 条决定重放通过。 |

## 原记录与当前测试之间的适配

1. **身份**：原身份只作 provenance。当前公开服务生成新的 task action、operation、Transport、command 和 receipt 身份。同一测试内重复 receiver、Result、advance、重建组件都使用原本生成的运行时身份。GOOD 保留 `原 inbound operation → 当前 operation`、`原 command → 当前 command` 映射并校验一一对应；返料按原 `bin_code + rack/face/slot` 关联当前 batch，绝不向现场发送原身份。旧 schema 的 Evidence/Confirmation/Return 主键没有伪装成当前表主键。
2. **扫码信封**：保留原 raw code、原毫秒 timestamp；设备 `STATION_SCAN9…12` 对应当前隔离绑定 SCAN1…4。旧事件附带 `is_debug`、`data.event_id/location`，当前严格信封不接受这些字段，因此不发送它们；仍留原事件 ID 供核查。ECS Result 采用当前已编译方向命令的契约，旧 `MOVE_FINISHED/actual_qty` 扩展不是当前方向命令的结果合同，不假装原 wire 字节一致。
3. **真实时间与派发期限**：保存历史扫码时间，不将它用作新 outbox 的过期期限。当前服务生成本轮期限；重复消费断言 command code、`expires_at`、冻结 `request_raw` 不变。测试只在本地调用 `before_send`/Result，没有 HTTP 设备派发。
4. **GOOD 顺序**：原 plan 第一项是 510001/90，实际先到为 510017/270，两者分别保留。测试按实际来源到位顺序逐个供应，适配当前顺序宿主；未实现或验证旧“同时三架入场”的窗口调度。跨箱 SCAN1/2 与 SCAN3/4 分段注入，用于保持原机会式返料目的架及 FIFO；保留每条原时间，但不声称执行顺序逐毫秒复刻。
5. **WMS 与生产接口**：WMS 决策借用现有测试端口写入原 outbox 的本地响应，再由当前公开 reader/advance 消费。GOOD 使用原六条 READY 入箱、六条 APPLIED READY 返料和两条 drain 决定；离场 READY 用当前 `rack.departure_decide@v1` 契约表达 WH01，而旧名称是 `outbound.rack.departure_decide@v1`。SCAN2 是持久化 WAIT、显式 `release_scan2` 的测试生产接口；未实现 WORK_REQUIRED、PDA、人工作业完成、批次资格或取消成员的准入规则。
6. **结果与放置证据**：不补造 SOURCE_PICKED。回架通过当前公开 Transport callback 注入 `TARGET_PLACED` 和匹配的 applied SUCCEEDED；仅成功结果而缺放置事实仍阻塞。原 BAD A000002668 从未捕获成功，测试追加的成功是**受控解除故障刺激**。当前门槛比旧档仅 TARGET_PLACED 即离场更强，不能用本测试认定旧时间线完全符合当前合同。
7. **额外故障注入**：BAD 架循环使用合成 SCAN3/4 时间与 `A000002668-B`（该原 fixture 未保存 raw scan，后缀不是现场事实）；`RACK_FACE_DONE`、DRAIN 下一面测试的 NO_BATCH、组合 GOOD A000002456-B 尾箱也是显式测试刺激。封闭历史测试使用独立合成旧循环，不将归档中 RECONCILING 改成成功。GOOD CTU03 先按有效接纳释放循环，再单独注入档内 WHE0304 等真实终点的迟到最终结果；不存在用 ZONE 假装最终物理点位。

8. **SCAN3 专项刺激**：新增 portable fixture 保留 REENTRY/CANCELLED 的 source_ref、旧 Return/Transport/Evidence/旧 MOVE_LEFT 身份。源档没有新访问的完整 raw envelope，专项 `-B` 后缀、运行时间、当前 WMS 决定和 ECS/Transport 结果均为显式合成刺激；旧 `OPERATOR_CONFIRMED_CANCELLED` 只保留原值，当前公开 callback 使用 `MANUAL_ABORTED`。另一箱 A000002701 来自 GOOD，但组合先后顺序不是原逐毫秒 trace。真正新点到访用同点另一箱或本点原 applied Result 分隔；SCAN3 本身不分隔 SCAN4。

## BAD 案例覆盖矩阵

下文“通过”只指对应当前组件切片的自动检查；“部分”仍有原案件范围未覆盖。函数名省略共同前缀 `test_recorded_`；注明“专项”的函数位于 `test_recorded_scan3_reentry.py`。

| 原 BAD 标题 | 使用的具体事实 / 当前测试 | 结论与剩余边界 |
| --- | --- | --- |
| A000002121：来源取消后已入线箱 SCAN2 被拒绝 | 原 A000002121-C、原任务/batch/投料来源在 fixture；`scan2_delegates_only_its_own_arrival` 对本 arrival 只准入一次、只释放一次、不改另一箱。 | **部分 / PRODUCTION_ONLY**：共享接口隔离通过；“已取消但确已入线”“仅 ACCEPTED 不准入”“原 batch 匹配”由生产业务判断，当前测试端口不实现它，不能称原事故已修复。 |
| A087…：回架未完成却旋转同架 | 原 A000002668 → 510004/270/B2F2；`unfinished_return_blocks_original_rack` 参数 ORDINARY/DRAIN × CTU02/CTU03。接纳、无终态、仅成功缺放置均不推进；原放置+成功后允许，重复结果不重复动作、原 manifest/身份不变。 | **通过，含合同差异**：4 组合各 SQLite/PG。DRAIN 旋转用合成尾箱/NO_BATCH；追加成功不是现场结果。原已经错误执行的旋转不做自动逆转。 |
| A86FE…：全过程恢复 | `scan2_delegates_only_its_own_arrival`、`scan_replay_keeps_frozen_original_command`、GOOD 末箱 drain。 | **部分**：乱序按本箱接口、冻结命令与返料独立切片通过；不重建原任务全流程、旧窗口清理、人工删库或缺失记录。原 A000002213/A000000702 未闭环仍保留。 |
| 旧退箱异常阻塞货架下一轮 | 原新任务 A842…、revision 8、510002/270、A000000341、510002B3F1C101；`new_cycle_ignores_closed_cycle_replayed_acceptance`。 | **部分**：当前公开入口建立独立已结束循环，新任务原循环继续，旧接纳重放不改新循环。两条旧 RECONCILING Confirmation 只留 provenance，当前无该旧模型，因此不声称复现旧 SQL 查询修复。 |
| SCAN4：完成回调缺失 | 原 A000002456-B 与原 Return/Evidence；组合 GOOD 的 A000002701-B；`missing_scan4_result_keeps_fifo_until_original_success`。 | **EXPECTED_RULE_DIFFERENCE**：当前正常 FIFO 不绕过未完成队首，后箱成功也不能入选；原队首迟到/重复 SUCCESS 后按原顺序可选。未发送结果时不伪成功。本次未模拟时钟超时或物理已动。 |
| SCAN3-REENTRY：旧 Return 阻塞 | 原 A000002456、旧 Return 353416576205376、扫码 Evidence 353461418492480；专项 `reentry_preserves_old_facts_and_requires_current_fifo_success`、`scan3_reentry_rolls_back_and_does_not_separate_scan4`、`new_scan3_supersedes_old_unassociated_scan4_queue_shadow`、`old_allocation_releases_unchanged_members_in_fifo`。 | **通过当前共享切片 / EXPECTED_RULE_DIFFERENCE**：新到访独立轮次/方向，旧 SCAN3/4 SUCCESS/FAILED 前后到达、重复、重建和 receipt 清理均不借给新轮次。A旧、B、A新按当前 B→A FIFO；B 未成功不绕过。旧未派发 READY 只消费旧候选，FACE_END/PURE_RETURN 仍按当前 B→新 A 继续；SEGMENT 继续余段。旧无关联 SCAN4 被后续有效 SCAN3 的物理事实排除出当前队列，原记录不改。未自动 VOID/removed，未模拟旧时钟超时恢复。 |
| CTU03：放箱未完成提前离场 | 原问题为 510027、A000000341；共享门槛由 `unfinished_return_blocks_original_rack` 的 source/drain CTU03 参数以及 GOOD drain 返料覆盖。 | **部分**：同类依赖门槛通过；没有将此案未保存的目标 slot 猜造为另一槽，也没重放该案完整 payload。当前需要原 applied success + TARGET_PLACED，不能照抄旧“确定终态即解除”。 |
| SCAN3-CANCELLED：交接后取消重新投入 | 原 A000000541、旧 Return 353416162218560、Transport transport-aa406a2c-f066-4a7d-979a-fc882a779dc8、扫码 Evidence 353477169455680；专项 `reentry_retains_native_obligation_without_moving_new_round`。 | **通过当前共享切片 / 部分原案**：公开创建原批次/Native Transport 后，以当前 MANUAL_ABORTED callback 注入取消；真实新扫码可创建独立方向/轮次，须自己的 SCAN4 SUCCESS。旧执行/冻结成员/身份保留，旧迟到成功只完成旧轮次。原已完成 MOVE_LEFT 仅留 provenance，不重放成反向命令；取消失败的原 Native 义务仍保留，未实现上游恢复或自动解除架服务。 |
| SCAN1-TARGET-IN-FLIGHT | 使用真实 A000002165-B；`scan1_needs_no_target_or_inbound_gate` 在无架动作、无入站 Transport 时保存扫码并生成 MOVE_FORWARD。 | **通过当前扫码边界 / 部分旧案**：现规则承认扫码物理事实，不增加旧 batch 或目标 F01 门禁；没有造 F01 执行器，不覆盖旧人工生产资格检查。 |
| DEPARTURE-WAKE：两轮扫描延迟 | 取真实 A000002456/510024/90 的 inbound；`departure_native_ack_wakes_and_releases_without_result` 用真实 MessageStore + Transport adapter classify/finish，提交后恰一条原 owner 通知，再公开 advance 释放原循环。 | **通过进程内通知/接纳边界**：无需 Beat 或最终 Result；没有启动真实 worker/HTTP，也没有声称现场 250ms SLA。离场请求首次 HTTP/READY 的完整 worker 调度仍由平台集成验收负责。 |
| CANCELLED-FACE-DONE | 原 AE361…、510002/270、rev1、原取消/CTU01/空清单 Evidence；`cancelled_arrival_consumes_face_done_once` 在 CTU01 创建后取消，真实到达仍消费一个空清单，重建/重复推进不重复申请并可离场。 | **通过当前共享切片 / EXPECTED_RULE_DIFFERENCE**：旧“取消后 READY 一律禁止”不采用；用户当前规则要求原已开始的分段继续，相关既有取消测试联合运行。 |
| SCAN3 独立重投入补充验收 | 原附加验收说明；专项 `independent_scan3_requires_only_own_scan4_success`。 | **通过当前共享边界**：无旧 Passage/投箱/当前有效生产任务，或同箱异线历史均可接收；异线结果、自己的 SCAN3 SUCCESS 均不能替代本轮 SCAN4 SUCCESS。未伪造生产业务历史/PositionProjection。 |

## GOOD A950 六箱与 G01–G15

`good_six_bin_shared_flow` 在两个数据库执行：六次 feed segment + 六次 return batch；8 次 CTU01 + 8 次 CTU03；24 个方向命令、一箱一次 SCAN2 admission 与 release；六个返料轮次完成，八个架循环释放。以上是测试中实际产生的计数，不把原总计 30 个 Transport（另有 2 个生产 F01）改写为新组件全链通过。原档没有 CTU02；CTU02 只在 BAD 受控测试覆盖。

| G | 原验收意图 | 本次结果 |
| --- | --- | --- |
| G01 | issued/prepare/plan_delta | **PRODUCTION_ONLY**：保留原 plan；当前仅调用公开 supply change，不声称生产任务入口实现。 |
| G02 | 来源架并发到位 / 真实首到 510017 | **部分**：按实际首到顺序运行；原 plan 顺序保留，未测旧并发窗口。 |
| G03 | 六次入站 BIN_MOVE | **通过共享切片**：精确保留原箱、source locator 与 CNV0301，当前 Transport callback/advance 消费成功。 |
| G04 | SCAN1 前进且无目标门禁 | **通过共享边界**：六次实际扫描 + 独立无 target/inbound 测试；F01 不在组件内。 |
| G05 | SCAN2 与 PDA | **部分 / PRODUCTION_ONLY**：WAIT 前无 command，显式许可只发一次；PDA、WORK_REQUIRED、NORMAL 回调未实现。 |
| G06 | SCAN3/4 轮次/FIFO/成功 READY | **通过共享切片**：原 raw 后缀、时间，独立 ECS Result 后进入可返集合。 |
| G07 | 机会式返料 | **通过共享切片**：A2231→510001/90、A2165→510002/270、A0628→510024/90，原槽不改。 |
| G08 | 两次独立 drain | **通过共享切片**：510035/270 两箱、510001/270 末箱；原箱与槽位，未增加 PickingTask 占用条件。 |
| G09 | 510035 同槽冲突重分配 | **NOT_COVERED / 职责不同**：fixture 保留 RECONCILING B2F1 与后续 APPLIED B2F2；GOOD 测试只消费原 APPLIED 决定。没有引入跨批 slot ledger、自动换 operation 或 WMS 库存修复，不能称本分支通过。 |
| G10 | 离场事件优先 | **部分**：原 owner 的 native submit acceptance 提交后通知通过；实际 worker 和原毫秒延迟未验收。 |
| G11 | 本轮最后一箱放置依赖 | **通过当前更强规则**：两个 drain 在当前匹配 applied success + TARGET_PLACED 后推进；原仅 TARGET_PLACED 更早到达的时序不作完全兼容承诺。 |
| G12 | 业务完成不截断末箱退料 | **部分**：生产 test port 改 current_task=None、重建组件，末箱正常 drain/return/depart；未执行真正 completion_confirm/F01。 |
| G13 | 无 SOURCE_PICKED 仍可继续 | **通过共享切片**：所有新测试均不注入该事件，也未补造取走时刻。 |
| G14 | 重复/漏唤醒/重启 | **部分**：重复 scan/Result/advance、组件重建、冻结身份/期限通过；没有杀 worker 或真实 Broker 丢通知演练，既有 recovery 测试联合运行。 |
| G15 | 总体完成计数 | **部分**：仅上述 28 个共享 Transport、24 个方向命令、6 个 return；未覆盖生产 Passage、2 个 F01、原取消 RECONCILING，也非 30 Transport 全链原 trace。 |

## 验证记录

验证环境为专门新建的本机 PostgreSQL、Redis；通过显式 `TEST_DATABASE_URL` / `TEST_REDIS_URL` 选择，未读取或覆盖生产 `.env`。本次执行者串行独占 pytest，不向 WMS/ECS/设备发网络请求。

```sh
rtk proxy sh -c '. .superpowers/sdd/2026-10-31-wes-recorded-case-validation/test-environment.sh; rtk uv run pytest tests/businesses/bin_roller/test_recorded_wes_cases.py -q -o addopts=""'
rtk proxy sh -c '. .superpowers/sdd/2026-10-31-wes-recorded-case-validation/test-environment.sh; rtk uv run pytest tests/businesses/bin_roller -q -o addopts=""'
rtk uv run ruff check tests/businesses/bin_roller/conftest.py tests/businesses/bin_roller/test_recorded_wes_cases.py
rtk uv run ruff format --check tests/businesses/bin_roller/conftest.py tests/businesses/bin_roller/test_recorded_wes_cases.py
```

2026-10-31 最终验证：

- 证据精确化后的新增案例文件：**30 passed / 0 skipped，8.90s**（SQLite 与专用 PostgreSQL 各 15 项）；此前 8.99s 的日志保留。
- 主代理随后完整目录检查：**464 passed / 6 skipped，74.13s**，见本任务 `component-final.log`。这轮位于本次 manifest/原 visit/ACK 刺激精确化之前；精确化后只重跑上面的 30 项，不能称 464 是修改后的全目录新证据。更早 462 passed / 6 skipped、65.83s 的记录也保留。6 个跳过是 SQLite 不适用的 PostgreSQL 并发/隔离/提交失败变体，实际 PostgreSQL 已执行，不是缺配置。
- Ruff check 与 format --check：**通过**，仅检查两个相关 Python 文件。
- 8 个原数据源 SHA-256 再核验：**全部未变**。
- 对照任务开始前快照，既有 `conftest.py` 只改一处 import 及 `scanned_at` 测试列类型；新增两文件与本报告，其余已有修改保留。

本次遇到并修正的都是测试适配问题：测试生产表 32 位时间溢出；旧 helper 用 ZONE 作为 SUCCEEDED 最终点位被正式合同正确拒绝；DRAIN 已空时按当前规则直接离场，不能期待无意义旋转。后两项已通过原真实点位和明确合成尾箱纠正，未修改当前组件以迎合旧档。


### 复核后的证据精确化

- 每次 GOOD 返料在 callback 前，独立比较当前冻结 manifest 与原 WMS moves，再比较实际 Native submit 的 container_id 与完整 rack/face/slot 目标。位置/终态 callback 从原 WMS moves 构造，避免运行时请求与结果共同漂移仍自洽通过。
- CTU03 使用原 binding step（source/drain）、rack、原 departure 请求 current_face 唯一选择，保存 8 个原 Transport 到 8 个运行时 Transport 的一一对应。510001/90/source 明确映射 `transport-1838ce1c-6c40-44fc-940d-d45886eee220`；510001/270/drain 映射 `transport-e2f1e84c-0c66-43ea-8e46-e254d8de32f8`。两者终点虽同为 WHE0306，原身份不混用。
- 修正 BAD 测试先前把 WMS decision `operation_id` 误传给接纳 helper 的刺激错误。旧 30 项通过不能单独证明 native return 已接纳；本次改用 `transport_operation_id`，由公开 Transport.get_result 断言 VALID/RECEIVED 且 outcome=None，再公开 WmsRequests.get_result 确认原 DECIDED/READY 内容未改，随后检查仍阻止 CTU02/CTU03。生产实现没有因此修改。


## 2026-10-31 SCAN3 专项后续修复

新增公开回归先复现新 SCAN3 不生成方向/复用旧 round（SQLite/PG 共 14 个失败），修复集中在 BIN_Roller：从最新有效 SCAN3 的原始关联推导当前物理轮次，旧履约继续保存；当前 FIFO 只接受本轮 SCAN4 的匹配 SUCCESS。旧未派发 WMS READY 被新物理访问取代时，消费原请求，释放该旧分配中仍当前的成员，沿原机会和原 batch.id 继续正常 FIFO。已有 Native Transport 的原履约和未解决失败保持原身份，不自动重派。

原 `test_ambiguous_scan4_round_keeps_fact_and_no_command` 的 oracle 人工插入一个没有 SCAN3 的孤立旧 round，要求它挡住当前真实到访；这与当前明确的物理事实优先规则冲突，因此改为孤立旧史不挡唯一可证明的当前 SCAN4，并另加最新真实 SCAN3 关联缺失/冲突的拒绝用例。最新关联缺失不得回退旧轮次；当前关系修复也不能给原未关联 SCAN4 补绑定。没有为历史数据增加全局兜底门禁。

专项显式测试原 SCAN3/4 SUCCESS/FAILED 在新访问前后、原 Transport 取消/迟到成功/迟到失败、重复回调、组件重建和 receipt 清理；原 command/Transport/WMS 冻结请求与身份不改。源码回归及全部组件验证记录见 `.superpowers/sdd/2026-10-31-scan3-reentry/task-1-report.md`。全仓库回归由控制会话另行执行，不能将当前自动检查称作真实设备验收。


### SCAN3 补齐后的最终全仓库验证（2026-10-31）

在本轮 SCAN3 独立重入、取消后真实新进站、旧轮次迟到隔离及 I1 旧尾部释放修复之后，控制会话以专用 PostgreSQL/Redis 执行 `rtk uv run pytest -q -rs`：**4363 passed / 42 skipped，433.26 秒**，退出码 0。42 个跳过均为 SQLite 不适用的 PostgreSQL 并发、隔离或提交行为变体，对应 PostgreSQL 参数已执行。Ruff check、format --check（554 个 Python 文件）、diff check 和唯一迁移 head `f0b220261031` 均通过。此结果取代本轮早期 504/106 项局部验证作为当前代码的全仓库证据；不改写上轮记录的历史时间顺序。

代码审查及最终跨流程复核均通过，无遗留 Critical/Important。原 wes_backend 八个案例源文件 SHA-256 均未变，当前 HEAD 与空暂存区保持。仅本轮明确的 9 个代码/测试/文档文件修改，其余既有未提交内容保留。专用临时数据库/Redis 已清理，源码及审计快照保留。未提交、部署、访问联调系统或执行真实设备动作；旧 Native 失败/取消义务未被自动解决。
