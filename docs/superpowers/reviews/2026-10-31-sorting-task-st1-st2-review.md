# sorting_task ST1/ST2 实现工程复核与 WES 案例验收

评审对象：复核时`feature/sorting-task-shared`尚未提交的ST1/ST2实现，基线`19082e3`；依据[共享计划](../plans/2026-10-31-sorting-task-implementation.md)。本轮授权包含复核、案例验收及实际缺陷修复，不包含宿主功能扩展、提交、部署或真实发送。

## 结论

在当前共享层范围内，计划符合性、原收据重验/稳定重放、可信原归属及事务回滚边界均通过复核。新增发现：Critical 0、Important 0、Minor 0；没有可复现产品缺陷，故本轮未改产品源码。新增维护性验收测试及最小历史案例样本。

评审按并发 receiver 和多业务消费者的既有运行方式检查；执行了 PostgreSQL 功能并发测试，未作吞吐/负载评估。原有 SQLite 调用方先于原生BEGIN建立根SAVEPOINT的限制保留，未将其视作本轮新缺陷或偷偷取消跳过。

## 四项重点

| 重点 | 核对与执行结果 |
| --- | --- |
| 计划符合性 | 四callback、两decisions单份声明；MANUAL/AUTO四分支字段和在场性保留；只issued为初始入口；组件无任务表、命令、独立推进/调度或正式占位消费者 |
| 原收据重验与稳定重放 | receiver锁定并刷新原收据后构造只读快照；成功不再重验/降级；确定拒绝沿原响应时间；后到任务不会使原引用拒绝取得新资格；身份漂移先于guard/宿主；重验返回None回滚本次写入并保留原响应 |
| 可信原归属 | 仅查编译消费者；已有任务先于当前工作线；错误来源、歧义和读取故障停止分派；另一消费者读口故障也不能借历史成功绕过；原成功收据保持 |
| 回滚边界 | 普通异常传播并回滚；无重验入口的Unavailable只撤销本次receiver写入，调用方原事实保留；合格原计划重验的业务写入、收据及提交后通知随外层事务一起提交或回滚 |

独立评审重新读取代码、调用方、计划/API及测试，未沿用前轮结论代替检查。独立新增三组公开入口探针，SQLite/PostgreSQL各一遍：重验删除待定行后返回None、删除后抛异常、另一消费者归属读取异常；6项通过。共享及旧默认回归合计443 passed、6 skipped。

## WES 数据来源及转换

只读使用`wes_backend/tests/mock/GOOD_CASES.md`、`BAD_CASES.md`及其原始材料：

- GOOD：`good_cases/KT16-A950FC259A78C43DB8D6D82FC110C8B03/evidence.json`，取已归档issued、prepare响应、plan_delta和completion响应。保存六来源面`510001/90、510002/270、510017/270、510024/90、510027/90、510034/90`及目标`610017/90`。
- BAD：`workline_plugins/manual-picking/tests/fixtures/kt16_a087_a000002121_cancelled_source_scan2.json`，取明确的`510002/90/revision 1`选择器；来源取消不能抹去已入线事实的完整业务断言留给宿主。
- 本仓样本：[kt16_task_cases.json](../../../tests/businesses/sorting_task/fixtures/kt16_task_cases.json)，保存来源路径、任务与材料标识、源文件SHA-256；本轮对两个原文件重新计算摘要，均与样本一致。

新测试生成新task_id、operation_id和时间，operation明确转换为`sorting.task.*@v1`，issued显式补`business_type=outbound`，隔离工作线用KT16配置。取消请求由原selector按当前合同重建，不伪装成保留旧operation/旧wire的直接回放。资源数组及顺序不裁剪、不重排；不增加兼容入口。不向现场发送历史身份或冻结请求。旧operation名仍明确拒绝。

GOOD归档声明尚未接入可执行Mock replay runner；旧`data/kt16_manual_picking_good_case.json`是另一任务的重建fixture，本轮没有把它当成同一六箱现场归档。两份wes_backend原始文件均未改动。

## 案例验收矩阵

测试入口：[test_recorded_cases.py](../../../tests/businesses/sorting_task/test_recorded_cases.py)。真实运行本仓receiver、合同、catalog/runtime、共享适配及数据库；仅宿主受理/重验为隔离测试端口，不声称真实宿主状态已实现。

| 来源/规则 | 本轮执行 | 结论与边界 |
| --- | --- | --- |
| GOOD G01任务合同 | issued→原六面计划→相同身份重投；DB正文逐项核对，通知不重复 | PASS（共享输入/保真/重放）；不证明真实成员应用或不重复搬运 |
| GOOD G01/G12响应 | 原PREPARE_ACCEPTED、COMPLETED响应按精确HTTP/code解析；错误组合拒绝 | PASS（响应合同）；不证明宿主完成资格或物理完成 |
| GOOD G14事务部分 | 原消息在宿主写入后抛异常，整次撤销后沿同一raw恢复；原计划重验外层回滚恢复原503/正文且无通知 | PASS（原身份/事务/提交后通知）；worker重启、漏唤醒和Beat补扫不在本切片 |
| BAD来源取消的共享入口 | 精确selector在工作线改配后仍写入原宿主；重复仅沿原响应；Unavailable保留调用方事实且不冻结本次收据/输入 | PASS（选择器/归属/回滚）；无真实成员状态，不证明来源取消资格或后续SCAN2准入 |
| 身份故障注入 | 同一原计划改变timestamp或六面数组顺序 | PASS：409/IDEMPOTENCY_CONFLICT，原收据及原正文可继续重放 |
| 旧协议隔离 | 原归档operation使用本次新身份投递 | PASS：UNSUPPORTED_OPERATION，无业务受理或通知 |
| GOOD G02～G11、G13、G15及G12/G14完整业务部分 | 进场、扫码、人工作业、Transport/Command、退箱、离场依赖及真实worker | 未执行：不属于ST1/ST2，需真实宿主/组件正式接入 |
| BAD回架未完成旋转、旧退箱异常跨轮阻塞、SCAN4漏完成、SCAN3重投入/已取消恢复、目标架在途、CTU03提前离场、DEPARTURE-WAKE、CANCELLED-FACE-DONE、A86恢复全过程 | 逐项核对所属状态/结果/worker职责 | 未执行：共享层无对应状态机，留给宿主/组件/运行链路；不把入口接受算作这些案例通过 |

维护性历史案例测试：17 passed（8组数据库行为各SQLite/PostgreSQL，另1组纯响应合同）。未把真实六箱闭环、取消后的已入线箱处理、SupplyChange、SCAN/Return/Rack义务或真实对端联调标为通过。

## 验证记录

| 检查 | 结果 |
| --- | --- |
| 历史案例`uv run pytest tests/businesses/sorting_task/test_recorded_cases.py -q -rs` | 17 passed，1.37秒 |
| 独立共享/旧默认回归与额外公开入口探针 | 443 passed，6 skipped |
| 最终全仓`uv run pytest -q -rs` | **4634 passed，52 skipped，443.65秒，退出0** |
| Ruff / format / diff | 全部通过；578个Python文件格式符合 |
| 最终测试输入一致性 | 全套前后466份app/tests文件（含样本）摘要一致 |
| 本轮范围核对 | 产品源码及既有测试均未改变；新增测试、样本和本报告，并同步计划记录 |

52项跳过为46项SQLite不支持的PostgreSQL并发/锁语义镜像、1项已记录的SQLite调用方新根SAVEPOINT基线限制、5项需额外独占数据库的既有迁移用例。PostgreSQL对应并发路径已运行，不以缺少本任务数据库配置的跳过充当通过。测试使用本任务专用PostgreSQL16与Redis7；评审者与主执行使用不同数据库，不访问应用存储。

复核完成时未提交、未推送；独占测试容器已清理，工作树及本地证据保留。用户随后单独授权整理提交、推送及创建合入develop的PR，范围见[共享计划§9](../plans/2026-10-31-sorting-task-implementation.md#sorting-task-pr-delivery)；不包含合并、部署或真实发送。

执行前后摘要及完整日志位于本工作树`.superpowers/sdd/2026-10-31-sorting-task-rereview/`：`recorded-cases.log`、`independent-review.md`及`full-suite.log`。独立评审曾因忽略目录缺少导入路径而出现一次收集错误，补`PYTHONPATH=.`并显式加载既有fixture插件后探针6项通过；该次不是产品RED，也未作为产品修复依据。

本轮未修改产品源码。后续真实宿主的准入、成员、版本、重验资格和完成状态仍必须在M1/S1及正式组合阶段单独验证。跨宿主task_id全局唯一性仍由WMS保证，不因归属扫描通过而宣称数据库已提供全局约束。
