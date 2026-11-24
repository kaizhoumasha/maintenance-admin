# 粗分 ECS 自主恢复与物料持续推进实施计划

**状态：本轮放料结果/报告状态分离已实施并完成本地定向验证（661 passed / 36 skipped）。** 原明确失败在报告发送前记录，WMS 收妥只完成报告，后续物料继续；T2/T3 更正扩展仍排除，T0 现场材料和 T5 存量调查未执行。本文件不授权部署、现场动作或存量业务数据修改。

**2026-11-24 本轮范围决定：用户选择“保留现合同，仅完成现有恢复与回归”，随后明确放料失败应闭环并允许后续物料继续。** 本轮交付修正后的 T1、T4a 及 T4b 接收/提交/通知丢失/重启/Beat 回归；不新增 ECS 更正入口或结果版本字段。放料结果/报告状态两字段及其增量约束迁移属于本轮实现，不改变 ECS wire。下方 T2/T3 及依赖它们的更正测试保留为未实施的未来方案，不再作为本轮待批准事项。T0 现场材料和 T5 存量调查不是本地软件测试结果，当前未选定现场环境、不执行现场修复。

**用户最新原则：放料结果与报告状态分别记录。** WES 先可靠接收原 ECS SUCCESS/FAILED；消费明确失败后、WMS 报告发出前记录 `put_result=FAILED`，报告义务为 `report_status=PENDING`。WMS 收妥仅把报告改为 COMPLETED，不决定或改写放料结果。作业 AWAITING_REPORT/COMPLETED 只表示流程阶段，不能把 COMPLETED 理解成物理放料成功；后续物料沿各自身份正常推进。部分放料信息或冲突继续校验，不增加 UNKNOWN 执行结果。

## 1. 编制基线与修正前已核实事实

计划编制基线为 develop 合并 PR #143 后的 `2278764f36286c1a01fe5253df21e197140c4614`。实施前重新检查代码、数据库和对端版本，不把本节作为运行环境证明。

下表保留编制基线的修正前行为；后续本地 T1 的变更和软件验证见文末实施记录，不能把旧行为表视为修正后的运行状态。

| 事实 | 源码或文档 | 影响 |
| --- | --- | --- |
| 普通 ECS Result 首次应用后，同内容返回原响应；不同内容 409，并标记待核对 | `app/integrations/ecs_command/result.py` | 目前不能用普通 Result 将已应用 FAILED 改成 SUCCESS |
| 公开结果读取返回原命令及已应用结果，没有独立业务结果版本消费合同 | `app/integrations/ecs_command/results.py`、`service.py` | 更正必须同时设计公开读取与业务消费，不能只改接收器 |
| PUT_TO_BIN 的 FAILED 且空 data 被转换为 CONFIRMED_NOT_PLACED，并发送失败执行报告 | `app/businesses/coarse_sorting/service.py::_advance_put` | 空结果不证明原动作不会恢复，也不单独证明未放盘 |
| 失败报告被接收后设 AWAITING_PUT、pending=false | 同文件 `_advance_report`；`tests/businesses/test_coarse_sorting_direct_put.py::test_confirmed_empty_put_failure_reports_to_wms` | 这是已有测试锁定的旧行为，需先用新行为回归替换 |
| 文档规定 WMS 收妥失败报告后释放箱格预约 | `docs/integrations/coarse-sorting-api-draft.md` §3.3 | 之后直接恢复原目标可能与新的箱格占用冲突 |
| 粗分物料、准备作业及公共货架待办已有分页 scan_pending 与提交后唤醒 | `service.py::scan_pending`、`composition.py` | 复用既有恢复入口，不新增调度器 |
| 粗分尾段只在显式 local 集成装配中启用 | `integration.py`、`app/core/config.py` | 本计划不扩大正式环境启用范围 |

V1.5 项目附录关于失联、位置不明保留原任务及查询原任务的内容主要面向 X-Ray。它是可参考的项目协议原则，不是粗分 ECS 程序已实现同等行为的证据。Transport 的 outcome_revision 与更正机制也不能直接当作 ECS 已有能力。

## 2. 不变量与修改边界

1. 超时、离线、尚无 Result、合同冲突、配置变化不得凭空生成执行结论。原明确 FAILED 是已存在的放料事实，必须在 WMS 收妥前独立记录；报告回执只结束反馈义务，不因等待 SUCCESS 或报告重试卡住后续物料。
2. 恢复沿用原 command_code、device_code、call_key、参数、目标、路由和业务关联。WES 不因故障、重启或超时另造物理命令，不以 Status 的在线/空闲替代完成事实。
3. 仍可能执行或结果未知时保留原 WMS 预约和货架责任；明确未放料且原执行结束的失败解除对应放料占用，WMS 收妥失败报告后按原合同处理预约。失败终局后不允许 ECS 继续原动作或由 WES 重做。
4. 原成功与原明确未放料失败分别创建一次匹配的执行报告；正常放料的数量、位置和目标校验不降低，失败同样核对原命令、分配、错误及无未决冲突。未知不冒充失败。
5. 已终结的物理动作不重做；原报告送达失败只恢复原报告。正常、NG 和明确放料失败均使用各自真实终局，不能把失败写成已放料成功。
6. 未解除责任的原证据不得随报告收妥、命令终态或普通保留期被清理。
7. 其他业务继续保留普通 Result 的一次性终态规则。更正不撤销已经提交的业务副作用，不允许 SUCCESS 回退或换目标。

本次不重写 RackDispatch，不增加私有 Sender、定时任务、人工继续按钮或 WES 硬件恢复动作；不调整 F01 离场接纳完成规则，不把接纳改称物理离场；不同时做全历史扫描优化。

## 3. 执行阶段与交付物

### T0：按既有粗分合同核对恢复边界

**用户补充：没有额外约定，唯一现有合同是 [coarse-sorting-api-draft.md](../../integrations/coarse-sorting-api-draft.md)。** 本地实施不另设独立签认或补充协议门槛；以下实际版本、日志和预约状态材料属于现场/存量核实，不能用缺少这些材料阻止已定义的本地修正。该合同 §6 明确暂不扩展 ECS 更正规则，P09 没有定义新的更正入口或版本报文；T2/T3 的相关提案不能当作既有 wire 直接实施。

- [ ] 收集粗分三类命令 MOVE_FORWARD、MOVE_TO_NG、PUT_TO_BIN 的实际合同版本、原始请求/回报和重启恢复日志。核实原命令与设备内部任务的持久映射、恢复去重及回报责任。
- [ ] 确认可恢复故障不提前生成最终 FAILED；ECS 持久保留原任务，恢复原未完成阶段，实际完成后以原身份回报。断电、ACK 丢失、掉线、动作预算耗尽均不得凭时间编造终态。
- [ ] 明确 FAILED 的具体语义：动作已确定结束，还是仍承担自主恢复义务；若已经回报 FAILED 后仍可能继续执行，必须先冻结 T2 的显式恢复/更正合同。
- [ ] 与 WMS 冻结预约有效性：恢复中不因故障、消息超时、WES 重启或旧失败报告自动释放；核实既有租约/到期策略，不假定预约永久有效。
- [ ] 明确已失败后恢复成功对应的 WMS 报告幂等与冲突规则。同一 binding_id 已有失败报告时，不能假设换 operation_id 发成功报告就能合法覆盖。
- [x] 交付合同差异表：[既有合同与实现差异、存量调查前置](../../integrations/coarse-sorting-recovery-contract-delta.md)。按现有行为、合同条目、责任及未定义项逐项记录；实际现场材料未取得就如实注明，不将本文升级为额外协议或对端验收。

**证据边界：** 本地按已有合同继续修正及隔离回归，不等待额外签名。整条恢复链完成仍要有原任务恢复、真实完成及目标预约的实际证据；未定义的新更正 wire 须先明确在同一合同文档中，不猜测实现。软件通过不等于现场恢复通过。

### T1：分离放料结果与报告状态

本节替换此前用内部 PUT_FAILED 同时表达放料失败与报告收妥的状态设计；此前结果见文末历史记录，不撤销原证据。

- [x] 原匹配 SUCCESS/FAILED 独立写入 put_result，原反馈义务写入 report_status=PENDING，均早于 WMS 报告发送/收妥；实际 `0 / PUT_FAILED / 1005` 及原空 data 失败继续支持。
- [x] WMS RECORDED/DUPLICATE 只把 report_status 改为 COMPLETED 并结束反馈待办；put_result 保持原值。成功、失败都可使作业流程 COMPLETED；失败 placed_at_ms 始终为空。
- [x] 报告失败只恢复原报告，物理严格成功或明确失败均按原证据解除对应执行占用；报告重试、重启和轮次切换不永久卡架或重做原物料。
- [x] 同工作线后续物料独立完成；公开投影分别展示执行结果和报告状态，尚未消费原 Result 的空值不定义成 UNKNOWN 执行结果。
- [x] 在未发布增量迁移内加入两字段和兼容回填，保留旧动作及 inactive 责任；两处历史迁移测试完整保留新列/数据/约束，降级不丢已确认失败。

**完成条件：** 原明确放料失败能够可靠报告并闭环，后续物料和有证据的货架调度继续；不改普通 Result 的 409，不实现失败后成功更正，不以失败终态代替物理成功。

### T2：实现显式原命令结果更正，保留普通 Result 保护

**本轮排除：** 按用户最新选择，保留粗分 API §6 的现有合同，不实施以下协议扩展。普通不同 Result 保持 409；本节是未来方案，不表示待办记录可以通过普通成功回报覆盖原失败。

- [ ] 若在既有粗分 API 文档中明确新增版本更正合同，再选择与现有注册架构一致的唯一入口；当前 §6/P09 未定义此扩展。不得在普通 Result 中按 owner 自动吞掉所有 409，也不得由未校验的 data 字段开启更正。
- [ ] 合同至少关联原 command_code、原 device_code、前一已应用结果摘要、单调结果版本及新真实完成事实；定义首版、递增、重复、乱序、竞争与缺前序的响应。不使用接收时间推断顺序。
- [ ] 首版限定粗分三类命令的已应用 FAILED → 经明确恢复得到的 SUCCESS；新事实仍通过原冻结附录校验，目标、数量和位置不符拒绝。不同结果争用同一版本或前序时不得覆盖赢家。
- [ ] 保存追加证据，保留旧失败、原响应、冻结请求；原失败重报仍返回它原来的响应，不能把旧失败重新应用到当前成功结果。
- [ ] 原命令行锁内原子更新当前有效结果关联、版本和允许解除的核对原因；不能顺带清除完整性破坏、非法身份或其他未解决冲突。更正不重新打开出站发送，不产生第二次设备执行。
- [ ] 按 D3=A，在共享 ECS 层复用原命令的持久结果及发送结论判定本次更正解决的证据，不新增独立核对项模型。不得仅凭 hold_reason 或一次成功清空 needs_reconciliation；其余冲突或证据不足时保持待核对。覆盖迟到拒绝、更正及其他矛盾证据的交错与回滚。
- [ ] 更正持久化提交后按原 owner 唤醒；增加公开只读结果版本/关联信息供粗分消费。不得让业务直接读取 ECS ORM、IntegrationRecord 或 ReliableMessage。
- [ ] 实施前盘点原 Result 入口、公开 DTO 的所有调用方、当前权威存储与受支持兼容路径。若新增持久字段，生成可审查的增量 Alembic 迁移；不修改已发布迁移，不借用 Transport 的修订记录存 ECS 更正。

**完成条件：** 只有显式合法更正改变当前有效事实；其他业务、普通矛盾 Result、旧严格附录和原响应重放的保护全部保留。既有合同没有定义的 wire 不猜测实现；这不是要求另取额外协议签名。

### T3：让物料和货架恰好消费一次恢复事实

**本轮排除更正相关扩展：** T2 未实施，不增加更正版本消费。现有原命令首次合法成功、重复回报、通知丢失及重启恢复仍由 T4b 验证，不能把原 FAILED 后持续等待称为更正成功。

- [ ] 三类动作都经公开读接口核对原命令及当前有效完成事实，在业务事务中保存消费关联并登记唯一后继意图。
- [ ] MOVE_FORWARD 恢复成功后只登记一次合法分配；MOVE_TO_NG 恢复成功后只完成一次 NG；PUT_TO_BIN 恢复成功后只产生一次合法成功报告，不再放一次。
- [ ] 重复更正、普通成功重报、多个 worker 并发及原失败迟到不重复推进、不回退业务。
- [ ] 原放盘仍在恢复或预约未有效确认时，准备作业/空闲退架继续保留占用；原严格完成后解除对应占用。新架严格到位与原目标朝向的验证保持。
- [ ] 复用现有 SERIALIZABLE 整事务重试与公共锁序；覆盖提交时 40001、新会话重试、回滚和提交未知后的原身份重读。

**完成条件：** 故障前后同一物料只保留一套原执行身份；完成事实、消费关联和后继意图原子提交，不能因更正形成重复放盘或重复报告。

### T4：证据保留与软件回归

本轮续接先处理 T4a：现有原验证/分配/失败报告、旧未完成但 pending=false 的责任及现有发送尝试证据的真实清理保护。更正链仍受 T2 的实际合同定义限制；T4a 不改变消息采用、发令、业务终局或现场数据。

- [x] T4a：原验证、分配和报告按未完成业务责任保留；旧 pending=false 不被清理或自动激活。真实完成的无保留责任记录仍可正常清理。
- [x] T4a：runtime authority 下保留原 WMS 迟到回复和延迟投影；不将过期租约回复变成已采用结果，不改变当前租约或业务状态。SQLite/PostgreSQL 的在途、有 owner 保留、正常完成和延迟投影场景及 PostgreSQL 并发投影清理验证通过。

- [x] 审计粗分 `retention.py`、现有公共 ECS/Transport 证据保留和清理调用方，保留仍被未结物料、架准备或公共货架责任引用的原请求/结果；更正链本轮不适用。
- [x] 用真实清理入口验证未收妥报告、待核对责任及 pending 货架所需证据仍可读取；真正成功或明确失败闭环、无其他责任时可正常清理。
- [x] 在一次性 PostgreSQL 和独立 Redis 验证 receiver → 结果提交 → 唤醒/Beat → 业务推进、重建服务和真实独立进程全过程；SQLite 不替代 PostgreSQL 并发和迁移证据。
- [x] 在既有文件覆盖普通重复、不同 Result 冲突、冻结附录/owner、正常/NG 成功及新明确失败闭环；公开普通 ECS 消费与并发测试保持保护，不新增跨业务矩阵或生产测试接缝。
- [x] 更新粗分 API 的成功/失败报告和预约处理、ECS 两种结果边界及[恢复回归说明](../../testing/coarse-sorting-recovery.md)，保留带日期的历史验收记录。
- [ ] 原完整方案中的更正版本/竞争、D3 混合冲突及专用跨业务更正回归：按用户本轮选择排除，仅保留未来方案。

### T5：存量调查与受控修复方案

- [ ] 先生成只读清单：原物料、command_code、binding_id、原目标、已应用结果、失败报告送达情况、WMS 预约、ECS 是否仍可能执行、货架责任及可用证据。
- [ ] 分类处理：未释放且授权有效；预约已释放/未知；实际已完成但未报告；结果矛盾或证据缺失。缺证据记录保持待办，不自动补造成功。
- [ ] 已释放预约必须由 WMS 明确恢复原目标授权，并与 ECS 恢复执行形成不会争用目标的交接顺序；不能让 WES 自主换箱格。无法取得原目标授权的记录列为跨系统合同缺口，不伪称可直接恢复。
- [ ] 失败报告已落账的记录须按 T0 冻结的 WMS 恢复合同处理；不直接改原报告正文或假设同键成功能够覆盖。
- [ ] 产出具体修复清单、前置条件、备份、幂等检查、执行后核验和失败处置。代码迁移不得批量开启存量物理执行；业务数据修复另按用户授权执行。

## 4. 文件与验证映射

| 范围 | 预计文件 | 必须验证的可观察行为 |
| --- | --- | --- |
| 粗分持续待办与正常推进 | `app/businesses/coarse_sorting/service.py`；`tests/businesses/test_coarse_sorting_direct_put.py`、`test_coarse_sorting_forward.py`、`test_coarse_sorting_ng.py` | 技术异常不结束业务，恢复完成只推进一次 |
| 货架占用与恢复 | 同 service；`test_coarse_sorting_rack_preparation.py`、`test_coarse_sorting_depart_only.py` | 未决放盘不退架；严格完成才解除占用；接纳不冒充到位 |
| ECS 新合同与公开读取 | `app/integrations/ecs_command/`、实际注册入口及必要模型；`test_result.py`、`test_query.py`、`test_concurrency.py`、`test_restart.py` | 显式更正、原失败重放、乱序/并发、非粗分隔离 |
| 扫描与真实装配 | `test_coarse_sorting_wakeups.py`、`test_coarse_sorting_application.py`、`test_coarse_sorting_integration.py` | 重启与通知丢失后仍由同一 pending 入口恢复 |
| 保留与迁移 | `coarse_sorting/retention.py`、实际清理入口、必要增量迁移；相关 retention/migration 回归 | 未解责任证据不丢；单一 head；空库与存量副本升级 |
| 文档 | `docs/integrations/coarse-sorting-api-draft.md`、`docs/integrations/ecs-command.md`、相应测试说明 | 明确区分项目合同、对端实现、软件测试与现场验收 |

先运行改动直接涉及的完整测试文件及 Ruff，再覆盖共享 ECS/货架/保留消费者回归，最终使用仓库十 lane CI。数据库/Redis 测试必须显式指向一次性资源；未配置而跳过不能计为通过。迁移完成后在隔离副本执行 `alembic upgrade head`、`alembic current` 与 `alembic check`；不在本计划阶段升级 enterprise_backend。

## 5. 最终验收矩阵

| 场景 | 预期 |
| --- | --- |
| 放料明确 FAILED、报告重试及同线后续物料 | 原失败报告收妥后失败闭环；报告重试不重做原动作、不永久卡架，后续物料能完成 |
| 尚未收到三类命令 Result、掉线或超时 | 等待原 SUCCESS/FAILED，不用超时生成失败，不更换物理命令；后继动作遵守各自原合同 |
| 原 FAILED 后显式更正成功（本轮排除） | 保留为 T2/T3 未来方案，本轮不提供入口；普通不同 SUCCESS 仍冲突，不要求失败等待成功 |
| 普通 FAILED 后直接发送不同 SUCCESS | 仍按现有冲突规则处理；不借恢复原则放宽入口 |
| 错设备、错命令、错目标、错数量或错朝向 | 不放行后继，不结束物料，不解除无关冲突 |
| ECS 重启、WES 重启、通知丢失、回调重报与乱序 | 原身份和证据保留，扫描可恢复，无重复执行和记账 |
| 放盘恢复期间 WMS 预约过期/释放 | 不自行放行；需有效授权及对端安全交接，列出合同缺口 |
| 旧架提交已接纳但未物理移走，新架在途或失败 | 不冒充新架就绪，等待新架自己的严格到位事实 |
| 并发扫描/更正/清理及事务冲突 | 无丢待办、重复后继、提前退架或清理未解责任证据 |
| 其他业务既有 FAILED/普通 Result 冲突 | 保持原有合同，不因粗分新增能力改变 |

## 6. 交付与回退边界

- 本计划之后先完成软件修正及隔离验证，再按用户要求 SHIP；合并不等于部署，部署/启线另行授权。
- 对端联调必须记录实际命令、设备、故障时间、原任务恢复、更正及预约证据。软件 CI 不证明 ECS 自主恢复或现场运动完成。
- 如持久化新更正证据，先冻结旧版本代码的读取兼容性与降级限制；不能在已有恢复事实或后继业务后靠删证据/回滚指针回退。回退只限制新能力入口，保留在途责任，并使用确认可兼容的构建。
- 清理本次一次性验证库、临时脚本和任务专用配置；保留审计记录及用户要求保留的备份，不清理共享运行库/队列。

**完成标准：** 明确放料失败和成功均可按各自原报告闭环，后续物料继续；缺少回报不能由 WES 编造终局。WES 实现、现场执行、预约安全及存量处置分别有证据，无法闭合的外部依赖明确列出，不以无限 pending 冒充恢复成功。

## Engineering review scope record — 2026-11-24

Target: 本文件《粗分 ECS 自主恢复与物料持续推进实施计划》。本节记录审查过程，不表示工程审查完成或实施获准。

- Prerequisite answer: D1 = B，用户回复“B”；跳过 office-hours，直接审查现有计划。
- Feature answers: 未提出功能裁剪，T0–T5 及原有不变量保持。
- Structure: D2 = A，用户回复“A”；保留 Original arrangement。
- Accepted scope: 第 99–104 行明确列出的 16 个文件（2 个业务源码、12 个测试、2 个文档），以及合同冻结后确定的共享 ECS 源码、注册入口、必要模型/迁移和保留测试。新增类或服务数量尚未确定；不为缩减文件数合并职责边界。
- Pending remedies: 此选择未批准任何具体工程修正；后续发现分别审查。
- Scope Challenge result: scope accepted as-is；本次审查进展及最终结果见文末报告。
## Scope Challenge evidence

Scope Challenge: No issues found requiring a feature cut. Dispositions: D2=A preserves T0–T5 and original arrangement; no remedy approval inferred. Scope accepted as-is.

What already exists [Layer 1]: service.advance owns fresh-session SERIALIZABLE retries (app/businesses/coarse_sorting/service.py:148); scan_pending owns bounded pages (same file:2138); _consume stores original evidence/digest and deduplicates (same file:619); result receiver owns command lock, replay and original annex validation (app/integrations/ecs_command/result.py:65); lifecycle stores original send conclusions (app/integrations/ecs_command/lifecycle.py:295). No new scheduler or private sender is needed.

Premise receipts (source review only, no live-environment certification):

| Premise | Receipt | Verdict |
| --- | --- | --- |
| Ordinary contradictory results retain conflict | app/integrations/ecs_command/result.py:208 | verified |
| Empty FAILED creates failure report and ACK stops pending | app/businesses/coarse_sorting/service.py:1255; app/businesses/coarse_sorting/service.py:1344 | verified |
| Existing regression asserts pending=false | tests/businesses/test_coarse_sorting_direct_put.py:348 | verified |
| Tail requires explicit local setting | app/core/config.py:81 | verified |
| ECS restart retains task mapping; FAILED recovery meaning | T0, this plan:39–41 | unverified: two peer premises |
| WMS reservation lifetime; failed-to-success report semantics | T0, this plan:42–43 | unverified: two peer premises |

unverified=4. No live peer inspection performed. Fetched origin/develop and current HEAD both resolve to 2278764; no newer matching implementation appeared in the requested-day path log. Existing 16 explicit files are a deduplicated count of rows 99–104, not a total implementation estimate. New classes/services remain unknown.

REALITY: premise-table pass source and peer premises separated; unverified=4 docs/superpowers/plans/2026-11-24-coarse-sorting-ecs-recovery.md:39
REALITY: already-done pass fetched base matches compilation baseline; no newer matching implementation app/businesses/coarse_sorting/service.py:1215
REALITY: surface-check pass alembic upgrade/current/check help and existing callback registry inspected app/controllers/ecs_result.py:27
REALITY: binding-decisions pass D1=B and D2=A recorded; recovery invariant retained docs/superpowers/plans/2026-11-24-coarse-sorting-ecs-recovery.md:5
REALITY: numbers pass explicit file count derived from file mapping; further counts unknown docs/superpowers/plans/2026-11-24-coarse-sorting-ecs-recovery.md:99
REALITY: deferred-asks pass no linked issue; explicit exclusions retained docs/superpowers/plans/2026-11-24-coarse-sorting-ecs-recovery.md:33
REALITY: pr-count pass no split proposed; default single PR is a planning assumption docs/superpowers/plans/2026-11-24-coarse-sorting-ecs-recovery.md:124
REALITY: prior-art pass no vendored dependency or library replacement proposed; reuse authored result receiver and lifecycle evidence app/integrations/ecs_command/result.py:65
REALITY: transaction-semantics pass existing whole-transaction retry verified; future PostgreSQL proof still required app/businesses/coarse_sorting/service.py:148
REALITY: contract-assumptions finding four peer premises await T0 confirmation docs/superpowers/plans/2026-11-24-coarse-sorting-ecs-recovery.md:39
REALITY: dependency-fallback pass local pending correction can proceed; real recovery cannot enable before contract closure docs/superpowers/plans/2026-11-24-coarse-sorting-ecs-recovery.md:46
REALITY: obligation-tiers pass contract and software proof required; live repair and deployment separately authorized docs/superpowers/plans/2026-11-24-coarse-sorting-ecs-recovery.md:93

Research readiness: Aside unavailable; used host web search against primary documentation. PostgreSQL requires retrying the complete transaction, including decision logic: https://www.postgresql.org/docs/18/mvcc-serialization-failure-handling.html . This supports retaining the existing transaction boundary, not adding a second retry layer. No runtime probe or benchmark performed.

## Architecture review — original decision checkpoint

Finding A1 [INFORMATIONAL] (confidence: 9/10): this plan:64 requires “不能顺带清除完整性破坏、非法身份或其他未解决冲突”, but the mechanism for identifying unresolved evidence is not selected. This is a planning decision, not a claim that an unimplemented correction is already broken.

Runtime receipts:
- app/models/integration_history.py:179–182: hold_reason, blocked, needs_reconciliation and applied_result_id are current scalar summaries.
- app/integrations/ecs_command/result.py:208–210: “if command.applied_result_id:” sets “command.needs_reconciliation = True” on a new contradictory result.
- app/integrations/ecs_command/lifecycle.py:365–366: “if late and command.applied_result_id and kind in (\"REJECTED\", \"NOT_RECEIVED\"):” also sets that flag without assigning hold_reason. The runtime-authority branch does the same at lines 328–329.
- Existing result evidence and ECS_ATTEMPT_CONCLUSION records retain the underlying evidence (result.py:262; lifecycle.py:347).

Two shapes: A keeps conflict knowledge inside the shared ECS layer and derives the unresolved projection from original evidence; B introduces a separate persisted reconciliation-item model and updates every writer. Both expose the same read-only safety summary to business consumers. Recommendation A has the smaller public surface and one source of facts. B has split-ownership/hand-sync risk unless every writer and retention path is migrated. Exact helpers/signatures remain unimplemented and conditional on T0; this review does not invent wire fields.

## Decision ledger

### R1: identify reconciliation evidence resolved by a legal correction

Finding: A1, INFORMATIONAL, confidence 9/10, plan:64; reviewer: native GPT.
Plan baseline: original proposal requires preserving every unrelated unresolved conflict; no mechanism previously approved. D2=A approved file arrangement only.
Runtime evidence: one needs_reconciliation boolean can be set by multiple independent evidence paths; hold_reason does not enumerate them.

| Choice | Current | A | B | Approval |
| --- | --- | --- | --- | --- |
| Conflict mechanism | unspecified | derive from original persisted result/send evidence under command lock | new persistent per-reason/source/resolution model and summary projection | pending |
| Unrelated unresolved conflicts | must retain | retain, insufficient evidence waits | retain, unresolved items wait | original proposal; part of this choice |
| Tests required for chosen mechanism | original concurrency and conflict invariants | mixed conflicts, late rejection/correction interleavings and missing evidence | same assertions plus writer/projection consistency | necessary proof of chosen behavior |
| Peer wire and capability | T0 pending | pending, unchanged | pending, unchanged | none |
| Other businesses / ordinary Result | existing rules | unchanged | unchanged | original proposal |
| File arrangement | D2=A | existing modules plus necessary already-planned models/migrations | existing modules plus necessary already-planned models/migrations | user D2=A |
| Product implementation / deployment / data repair | not authorized here | not authorized here | not authorized here | user requested review only |

Question D3:
D3 — 更正成功后，如何保留仍未解决的冲突？
请回复 A 或 B。<gstack-qid:plan-eng-review-reconciliation-evidence>
Project/branch/task: feature/coarse-sorting-ecs-recovery-plan，审查 T2 的核对责任解除方式。
ELI10: 一条命令可能同时存在失败结果、矛盾回报和迟到的发送拒绝。恢复成功只解决其中一项，不能把其他疑点一起擦掉。现有 needs_reconciliation 只有“是/否”，hold_reason 也未记录全部来源。
Stakes if we pick wrong: 尚有矛盾证据时，物料可能继续分配、上报成功或释放货架占用。
Recommendation: A because 原结果和发送结论已经持久化，优先复用它们，避免新增第二套冲突状态。
Note: options differ in kind, not coverage — no completeness score.
A) 从原证据判定 (recommended)
Completeness: N/A。由共享 ECS 层在原命令锁内，按原命令关联读取结果冲突及发送结论，识别本次更正解决的具体证据；只有其余冲突均已依法解决时才清除汇总标记。它复用现有证据并保持一个事实来源，但需要覆盖迟到拒绝与更正交错的回归，证据不足时继续等待。预计 human: ~0.5–1 天 / CC: ~30–60 分钟，仅为额外设计、实现及回归的估算。
B) 独立保存核对项
Completeness: N/A。新增可逐项记录原因、来源证据及解除关系的持久模型，原布尔字段作为汇总投影；更正只解除明确关联的核对项。它使未解事项可直接查询，也便于审计，但增加模型、迁移以及全部置位路径的同步责任。预计 human: ~1–2 天 / CC: ~1–2 小时，仅为额外设计、实现及回归的估算。
Net: 两项都保留现有安全不变量；选择复用原证据计算，或增加一套必须持续同步的核对项模型。T0 对端合同、wire 设计、其他业务语义和部署授权保持原值。

Header: 冲突证据
Options:
A) 从原证据判定 (recommended)
Exact description: Completeness: N/A。由共享 ECS 层在原命令锁内，按原命令关联读取结果冲突及发送结论，识别本次更正解决的具体证据；只有其余冲突均已依法解决时才清除汇总标记。它复用现有证据并保持一个事实来源，但需要覆盖迟到拒绝与更正交错的回归，证据不足时继续等待。预计 human: ~0.5–1 天 / CC: ~30–60 分钟，仅为额外设计、实现及回归的估算。
B) 独立保存核对项
Exact description: Completeness: N/A。新增可逐项记录原因、来源证据及解除关系的持久模型，原布尔字段作为汇总投影；更正只解除明确关联的核对项。它使未解事项可直接查询，也便于审计，但增加模型、迁移以及全部置位路径的同步责任。预计 human: ~1–2 天 / CC: ~1–2 小时，仅为额外设计、实现及回归的估算。

State: approved
Actual answer: D3=A，用户回复“A”。
Accepted scope: 由共享 ECS 层在原命令锁内，按原命令关联读取持久结果冲突及发送结论，识别本次更正解决的具体证据；其余冲突未解或证据不足时保持核对标记；不新增独立核对项模型。包含混合冲突、迟到拒绝/更正交错、缺证据与回滚的必要回归；T0 wire、其他业务及部署授权不变。
History: 原方案未指定机制；D3 选择 A，原待决状态保留于本题问题及比较表。

Review progress: Scope Challenge and Sections 1–4 reviewed; D3=A and D4=A resolved. Outside Voice is unavailable and explicitly receives no clean credit. Final results and remaining execution prerequisites are recorded below. No implementation changed.
### Architecture review disposition

A1 / R1 accepted by D3=A; required proof travels with that answer. No other architecture issues found at this stage. The shared ECS layer owns correction knowledge; businesses receive the public effective fact/version and consume it in their own transaction. Correction never authorizes a send.

Two shapes for the new public correction boundary (conceptual only; no wire/signature commitment before T0):

    Shape A — chosen by D3=A
    shared ECS receive_correction(...) -> original evidence + effective projection
    shared ECS get_command(...)       -> public effective fact/version
    coarse advance(...)               -> consume + unique next intent
    bodies: not implemented

    Shape B — not selected
    reconciliation-item model owns source/resolution rows
    result receiver + sender + correction each update that model
    get_command reads its summary; business consumes original fact
    bodies: not implemented

Depth: A hides evidence and resolution policy behind the existing shared boundary; B creates a second write responsibility. Red flags screened: no new shallow/pass-through/temporal service, no business reads of transport internals, no duplicate scheduler or sender. Split ownership and hand-synced summaries are avoided by D3=A. Importable internals remain internal to shared integrations, not a shortcut for business consumers. State storage for any new independently constrained result version must remain explicit, as already required by T2.

### Code Quality review

No issues found. Dispositions: no extraction proposed. Verified callers have different business contracts: coarse service.py:1227 (strict original PUT), xray/service.py:346 (stage facts), smt_auto/outbound.py:638 (plate PUT), _components/bin_roller/scan_flow.py:35 (scan release). Reuse the existing public ECS readers; similar-looking advancement is not sufficient grounds for a shared business helper. Implementation savings from extraction: not claimed; added/removed lines: none proposed. Existing service diagrams must be updated if changed; no existing diagram was established to be incorrect in this bounded review.

### Test review — regression analysis and decision

Authoritative framework: AGENTS.md Testing Guidelines, pytest/pytest-asyncio/HTTPX with explicit disposable PostgreSQL and Redis for integration. Read existing concrete source and tests before drawing the following map. It covers behavior groups in this plan, not statement/branch coverage of all transitive libraries.

    CODE PATHS / existing or proposed                      USER FLOWS
    SCAN -> WMS validation -> original MOVE
      [*** TESTED] original identity/quantity                accept scan; duplicate; rebuild
        test_coarse_sorting_forward.py:12, :48
      [*** TESTED] rollback + missed wakeup                  retry/restart after broker loss
        test_coarse_sorting_wakeups.py:88, :114, :149
    receive_ecs_result -> command lock -> original annex
      [*** TESTED] replay/conflict + annex                   invalid/wrong/contradictory callback
        test_result.py:340; result.py:98, :208
    get_command -> business transaction
      [*** TESTED] consume once / rollback / mapping         concurrent worker; stale permission
        test_query.py:8, :35, :78
    rack and normal/NG completion
      [*** TESTED] strict arrival + normal/NG                wait for original rack; no ACK-as-arrival
        test_rack_preparation.py:45; test_ng.py:138
      [*** TESTED] actual cleanup retains original facts     recover original report/rack decision
        test_rack_preparation.py:637
    proposed ECS correction (not implemented)
      [GAP G1] validate version/identity/predecessor          [->E2E] legal recovery or rejection
        invalid -> retain; duplicate -> original response
        missing/older/competing -> frozen T0 response
      [GAP G2] append evidence + effective fact atomically    [->E2E] restart/unknown commit/40001
        rollback -> neither; committed -> owner wake
      [GAP G3] D3 mixed reconciliation evidence              late rejection before/after correction
        unrelated/missing evidence -> still blocked
      [GAP G4] three business consumers                      [->E2E] same material, one successor
        FORWARD -> one allocation; NG -> one completion
        PUT -> one report, no second physical command
      [GAP G5] empty FAILED pending/report/rack invariants    failure cannot release reservation
        replacement identity/report forbidden; old T5 separate
      [GAP G6] correction-chain retention + migration        [->E2E] clean while waiting; upgrade copy
        unresolved -> readable; eligible closed -> cleanup
      [GAP G7] cross-business changed DTO / permission       existing X-Ray/SMT/BIN_Roller flows
        non-coarse correction denied; ordinary rules unchanged

Legend: *** behavior + edge/error; GAP proposed behavior without existing coverage.
[->E2E] means software integration, not physical-device acceptance.
Coverage audit: 6/13 behavior groups have existing strong tests (46%, source inspection only); 7 groups require new/extended assertions. Quality: 6 strong groups, no smoke-only group counted. Proposed code is not tested by these existing groups. No LLM/prompt changes; eval not applicable.

[CRITICAL] (confidence: 9/10) plan:66 and plan:120 — shared ECS reader/receiver changes require explicit preservation assertions for existing consumers. Existing evidence is test_query.py:78 (“test_result_does_not_override_owner_mapping_permission_or_conflict”) and the public callers cited in Code Quality; these do not test the proposed coarse-only permission/version extension. This is regression risk, not an observed regression. G7 is the dedicated decision R2 below. G1–G6 already appear as proposed T1–T4 proof; D3 adds required mixed-conflict proof.

No UI forms, double-click controls or navigation are introduced. Equivalent interactions are duplicate callbacks, concurrent scans, stale bindings, lost ACK, receiver restart, 503/retry and dropped wakeup. Public failures retain the original state/identity; no completion or physical recovery claim follows from a software ACK.

Tests made obsolete: the old empty-FAILED terminal/report expectation in test_coarse_sorting_direct_put.py:348; replace its assertions with the newly selected intended behavior, not delete coverage. No other retirement proposed.

### R2: existing-behavior regression contract

Finding: Test G7, CRITICAL regression requirement, confidence 9/10; plan:66 and plan:120; reviewer native GPT.
Plan baseline: ordinary one-shot Result and other-business contracts preserved; coarse empty FAILED reporting intentionally changes. Exact cross-consumer assertions have not previously been approved.
Runtime evidence: public CommandView is shared; actual callers and existing behavior tests cited above. New version/correction behavior is not implemented.

| Choice | Current | A | B | Approval |
| --- | --- | --- | --- | --- |
| Regression organization | files named, exact cross-consumer assertions incomplete | extend existing public-boundary tests/parametrization | new cross-business public-boundary matrix plus keep existing tests | pending |
| Ordinary Result/annex/owner behavior | preserve | original response replay; conflict; annex and owner isolation | identical | original proposal, selected here |
| Other-business correction permission | no new permission | X-Ray/SMT AUTO/BIN_Roller cannot use coarse correction | identical | original proposal, selected here |
| Deliberate behavior difference | empty coarse FAILED reports/releases and pending=false today | no failure release report; stays pending with original references | identical | original proposal, selected here |
| Required new proof | T1–T4 + D3 | G1–G6 plus actual public consumers; PostgreSQL concurrency/migration | identical | D3=A for mixed conflicts; other original proof selected here |
| Product testing seam | none requested | none; existing production boundaries | none; existing production boundaries | fixed |
| Scope/peer wire/deployment/data | D2=A, T0 pending; review only | unchanged | unchanged | D2=A |

Question D4:
D4 — 如何固定共享 ECS 改动的回归合同？
请回复 A 或 B。<gstack-qid:plan-eng-review-recovery-regression>
Project/branch/task: feature/coarse-sorting-ecs-recovery-plan，审查 T1–T4 对现有消费者的影响。
ELI10: 新能力只允许粗分通过显式合同更正原 FAILED；普通 Result 和其他业务仍按原规则处理。共享读取接口一旦改错，其他业务也可能提前推进，所以既要验证恢复成功，也要验证原有保护没有松动。
Stakes if we pick wrong: X-Ray、SMT AUTO 或 BIN_Roller 可能误消费更正、忽略冲突，或重做已经完成的动作。
Recommendation: A because 现有测试已覆盖真实业务边界，扩展它们可以保留保护并减少重复测试及新文件。
Note: options differ in kind, not coverage — no completeness score.
A) 扩展现有回归 (recommended)
在现有 ECS 及各业务测试中补充或参数化断言：普通重复返回原响应、不同内容仍冲突、冻结附录及 owner 隔离、其他业务不获更正许可、正常/NG 完成不重做；唯一有意改变的旧行为是粗分空 data FAILED 不再生成失败释放报告或退出待办。同时覆盖新更正的版本/乱序/并发、原失败重放、D3 混合冲突、原货架责任和证据保留，并在一次性 PostgreSQL 验证事务及迁移；复用现有夹具，不增加生产测试接缝。优点是断言贴近已有消费者且不重复搭建基础设施，代价是多个测试目录需要协调。预计 human: ~1–2 天 / CC: ~1–2 小时，均为完整增量回归的估算。
B) 新建跨业务矩阵
保留并运行现有测试，另建跨业务回归矩阵，覆盖与 A 完全相同的行为、不变量、故意变化和 PostgreSQL/迁移证明；调用现有公开业务入口及共享夹具，不增加生产测试接缝。优点是可集中查看新能力的隔离结果，也方便统一比较各业务，代价是增加测试文件及跨业务夹具维护，需避免重复断言。预计 human: ~1.5–3 天 / CC: ~1.5–3 小时，均为完整增量回归的估算。
Net: 两项保护相同的合同，只选择将回归放入现有测试还是新增集中矩阵；不增加功能、对端协议、部署或存量数据修改授权。

Header: 回归组织
Options:
A) 扩展现有回归 (recommended)
在现有 ECS 及各业务测试中补充或参数化断言：普通重复返回原响应、不同内容仍冲突、冻结附录及 owner 隔离、其他业务不获更正许可、正常/NG 完成不重做；唯一有意改变的旧行为是粗分空 data FAILED 不再生成失败释放报告或退出待办。同时覆盖新更正的版本/乱序/并发、原失败重放、D3 混合冲突、原货架责任和证据保留，并在一次性 PostgreSQL 验证事务及迁移；复用现有夹具，不增加生产测试接缝。优点是断言贴近已有消费者且不重复搭建基础设施，代价是多个测试目录需要协调。预计 human: ~1–2 天 / CC: ~1–2 小时，均为完整增量回归的估算。
B) 新建跨业务矩阵
保留并运行现有测试，另建跨业务回归矩阵，覆盖与 A 完全相同的行为、不变量、故意变化和 PostgreSQL/迁移证明；调用现有公开业务入口及共享夹具，不增加生产测试接缝。优点是可集中查看新能力的隔离结果，也方便统一比较各业务，代价是增加测试文件及跨业务夹具维护，需避免重复断言。预计 human: ~1.5–3 天 / CC: ~1.5–3 小时，均为完整增量回归的估算。

State: approved
Actual answer: D4=A，用户回复“A”。
Accepted scope: 扩展现有共享 ECS 及 X-Ray、SMT AUTO、BIN_Roller 公开业务入口回归；保留原响应重放、普通冲突、冻结附录/owner 隔离、其他业务无更正许可、正常/NG 不重做。唯一故意变化为粗分空 data FAILED 不发失败释放报告且持续原待办。包含 G1–G6、D3 混合冲突、货架责任、真实 PostgreSQL 并发及迁移/保留证明；复用夹具、不增加生产接缝、不新建跨业务矩阵。此为计划批准，不授权本轮实现、部署或数据修复。
History: 原有提案未确定跨消费者回归组织；D4=A 固定现有文件扩展方式及其完整比较表 A 列的合同。

Approval readiness: PASS — R1 cites actual D3=A; R2 cites actual D4=A; D1=B and D2=A are routing/structure only, not remedy approvals. No other accepted remedy, new policy or TODO is inferred. T0 peer premises remain external execution prerequisites, not missing review answers.
### Test review disposition and artifact

Dispositions: G1–G7 accepted as required proof by D4=A; G3 also covered by D3=A. These are seven unimplemented behavior groups, not seven demonstrated production bugs. Cross-consumer regression remains CRITICAL to implementation acceptance. No proposed test has been written or run.

Approved test artifact: /Users/kaizhou/.gstack/projects/kaizhoumasha-maintenance-admin/kaizhou-feature-coarse-sorting-ecs-recovery-plan-eng-review-test-plan-20261124-060308.md. It specifies actual public boundaries, existing files to extend, assertions and one value card for each edge/critical group. T0 defines precise wire responses before receiver test cases are implemented. No new framework, production seam, eval or benchmark is needed.

### Performance review

No issues found requiring an additional remedy. Dispositions: no performance scope added. Existing get_command uses one scoped join for effective fact and delivery projection (app/integrations/ecs_command/results.py:32); scan_pending pages IDs and finishes its captured upper bound (service.py:2138); get_attempt_conclusions scopes evidence to an original exchange and reduces attempt revisions (lifecycle.py:30). Existing integration_record exchange/event and exchange/attempt indexes cover these original associations (app/models/integration_history.py:199, :237). This does not prove the future correction query plan or bounded latency.

Scale: original command count, per-command attempt/evidence count, requests/s and correction latency are unknown. D3=A must not become a global history scan; existing fact readers and original-exchange associations remain the reuse boundary. There is no measured performance improvement, new cache, batch scheduler or benchmark claim. Correctness tests should not assert arbitrary query counts or timing. T2's finalized schema and queries still need normal implementation review; no broad scan optimization is included.

### Outside Voice

Running the outside voice automatically (standard step). Disable: `gstack-config set codex_reviews disabled`.

Preflight: codex_reviews is enabled (stored unset; effective enabled); resolver reports CODEX_MODE:not_installed. No Claude Code process was started. The bounded challenge prompt was constructed from this selected working plan and actual D1–D4 decisions, with the required filesystem boundary and read-only instructions. It was not sent to a provider.

Outside voice unavailable: Claude Code command could not be resolved; Codex has no read-only native Plan subagent. Fix: install or repair Claude Code and authenticate with `claude auth login` if required. Continuing to planning decisions and Approval readiness. (OUTSIDE_VOICE_UNAVAILABLE)

outside_provider: claude-code
outside_status: unavailable
source: none
No external findings, model identity, agreement or clean-review credit exists. No replacement general-purpose agent was launched.

### Final TODO choices and approval readiness

TODOS.md was reviewed for existing callback authentication, peer contract and retention obligations. No new TODO was proposed: T0 already owns peer confirmation, T4 owns retention, and this plan explicitly excludes unrelated callback authentication and maintenance mechanisms. Existing TODO dispositions are not changed.

Approval readiness: PASS — accepted remedies are R1/D3=A and R2/D4=A with complete records above. D1=B and D2=A are not used as remedy approvals. No unanswered review choice remains. Four T0 peer premises are explicit implementation/activation prerequisites; they are not silently defaulted.

## NOT in scope

- Deployment, line activation and live physical execution: original user authorization boundary remains; review does not approve them.
- Live business-data repair: T5 produces a reviewable inventory and repair procedure; execution remains separately authorized.
- Private senders, new schedulers, manual continuation buttons and WES hardware recovery: original shared progression/reliable layers suffice.
- Global history optimization, new cache or independent reconciliation-item model: no need established; D3=A chooses original evidence.
- Changing F01 departure acceptance or declaring ACK to be physical arrival/departure: unchanged contract.
- Callback authentication or unrelated existing TODO work: existing independent ownership and scheduling remain.

## What already exists

- Reuse app/businesses/coarse_sorting/service.py:148 whole-transaction retry and :2138 paginated progression.
- Reuse shared original result receiver, frozen annexes, send evidence and public readers in app/integrations/ecs_command/; implement correction at this owning boundary after T0.
- Reuse _consume and existing original action identities, RackDispatch, after_commit and existing retention references.
- Reuse pytest/HTTPX and public business integration fixtures; D4=A extends existing tests rather than building a new matrix.
- No extraction approved. Verified consumers require distinct business decisions, so no speculative helper or line-savings claim is made.

## Reviewed flow and failure modes

    existing ordinary Result ----------> original replay / conflict (unchanged)
    proposed explicit correction
      |
      +-- T0 not frozen ----------------> no wire implementation / no real activation
      +-- invalid owner/annex/predecessor/version -> retain evidence + approved error
      +-- original command lock
          +-- append correction evidence + effective projection
          +-- unresolved/missing other evidence -> keep reconciliation obligation
          +-- rollback / 40001 ------------> fresh transaction / original identity
          +-- commit ----------------------> owner wake; lost wake -> same Beat entry
                                               |
    business SERIALIZABLE transaction <---------+
      +-- pending original action + public fact/version
      +-- strict fact/authorization absent -> keep pending, preserve rack/target
      +-- valid fact -> consume + one successor / genuine completion atomically
      +-- normal/NG complete -> no repeated physical intent
    cleanup -> retain unresolved original chain; eligible closed -> normal retention

| Failure mode | Handling / observable outcome | Required proof |
| --- | --- | --- |
| Ordinary contradictory callback | existing 409 / preserved fact and reconciliation | existing tests + G7 |
| Correction malformed, reordered or competing | frozen T0 error/replay; no unsafe overwrite | G1 |
| Late send rejection remains after correction | evidence-derived unresolved summary; material waits | G3 / D3=A |
| Commit fails or publication is lost | rollback/whole-transaction retry or original pending/Beat | G2 |
| Two workers consume one fact | original locks/identity and atomic successor; no duplicate intent | G4 |
| Empty FAILED PUT / unknown WMS authorization | pending and target/rack responsibility retained; no release report | G5 |
| Cleanup races with unresolved recovery | original facts remain publicly readable | G6 |
| Shared reader changes another business | no coarse correction permission; original public consumer behavior | G7 |
| Peer does not support original-task recovery or reservation safety | T0 remains blocked; no real recovery activation or completion claim | peer evidence; mocked software cannot prove it |

Failure modes: 0 additional critical gaps with both no test plan and no error handling. There are seven required software-proof groups and four unverified external premises; zero does not mean software/field acceptance.

## Worktree parallelization strategy

Sequential implementation, no parallelization opportunity.

| Step | Modules touched | Depends on |
| --- | --- | --- |
| T0 contract freeze | peer contracts and documentation | — |
| Shared effective-result and D3 evidence handling | app/integrations/ecs_command, owning models/migrations, tests/integrations/ecs_command | T0 for wire; existing evidence analysis can proceed |
| Coarse consumers and retained obligations | app/businesses/coarse_sorting, tests/businesses | shared result/version contract; T1 local persistent-pending fix can proceed independently of wire |
| End-to-end compatibility, retention and migration acceptance | shared integration/retention modules and their existing tests | owning changes |
| T5 repair procedure and real acceptance | documentation / read-only inventory | T0 and actual original evidence; live repair separately authorized |

One implementation lane; no worktrees are created by this review. External T0 collection and local T1 regression analysis can run alongside one another, but the two review-derived tasks touch shared integration tests and are sequenced. No navigation choice may make every T1 edit wait for T0: only wire and real activation retain that prerequisite.

## Implementation Tasks

Synthesized from this review's findings. Each task derives from a specific finding above. Run with Claude Code or Codex; checkbox as you ship. Estimates are planning ranges, not measured delivery promises; shared-boundary reasoning has less AI compression than mechanical test edits.

- [ ] **T1 (P1, human: ~0.5–1 day / CC: ~30–60 min)** — shared ECS — Derive unresolved reconciliation obligations from original evidence.
  - Surfaced by: Architecture A1 / R1, approved D3=A.
  - Files: app/integrations/ecs_command/result.py, lifecycle.py and the T0-selected correction owner; necessary existing contracts/models/migrations; tests/integrations/ecs_command/test_flow.py, test_result.py, test_concurrency.py.
  - Verify: public receiver/read tests cover mixed conflicts, late rejection before/after correction, missing evidence, rollback, and original failed replay; run full touched files plus PostgreSQL cases. No independent reason model or new sender.
- [ ] **T2 (P1, human: ~1–2 days / CC: ~1–2 h)** — existing regression suites — Extend public-boundary proof for G1–G7 and preserve shared consumers.
  - Surfaced by: Test review G1–G7 / R2, approved D4=A; G3 also D3=A.
  - Files: existing tests/integrations/ecs_command/, tests/businesses/test_coarse_sorting_*.py, tests/businesses/test_xray.py, tests/businesses/smt_auto/, tests/businesses/bin_roller/ and relevant existing retention/migration modules.
  - Verify: direct affected complete pytest files, Ruff, disposable PostgreSQL/Redis software integration and migration checks, then ten-lane CI. Replace old empty-failure expectations; no runtime or physical acceptance claim from mocks.

_No new tasks from Code Quality._
_No new tasks from Performance._
These tasks supplement the original T0–T5 implementation roadmap; they do not reauthorize its deferred operational actions. Prior task lists: none.

## Unresolved decisions that may bite you later

None from this review. External prerequisites remain: ECS original-task persistence, FAILED/recovery semantics, WMS reservation lifetime, and failed-to-success report semantics. T0 must settle them with actual evidence before dependent wire/activation work.

## Suppressed findings

None. No speculative failure is promoted to a source-confirmed bug.

## Engineering review completion summary (before implementation)

- Step 0: Scope Challenge — scope accepted as-is (D2=A).
- Reality rows: 12/12 applicable rows emitted; PLAN_REALITY verdict=complete; 4 unverified peer premises.
- Architecture Review: 1 issue found, plan remedy accepted D3=A.
- Code Quality Review: 0 issues found.
- Test Review: diagram produced; 7 proposed behavior groups lack current implementation coverage; required proof accepted D4=A.
- Performance Review: 0 additional issues found; runtime scale and future correction latency unmeasured.
- NOT in scope: written.
- What already exists: written.
- TODOS.md updates: 0 items proposed; existing TODOs unchanged.
- Failure modes: 0 additional critical silent/unhandled gaps; proposed software proof still pending implementation.
- Unresolved decisions: 0 in this review; T0 external prerequisites remain.
- Outside voice: claude-code unavailable; resolver found no command; no read-only native fallback; no outside clean credit.
- Parallelization: 1 lane, 0 parallel / 2 sequential review-derived tasks; original T1 local work need not await T0 wire freeze.
- Lake Score: N/A (0 answers scored for Completeness; all choices differ in kind).
- Verification performed: source/test inspection, fetched-base check, CLI surface checks, decision read-backs and plan-reality validation. No pytest, migration execution, benchmark, deploy or device action.
- Persistence: selected plan, approved test artifact and task JSONL saved; required native review log is written only after final report read-back.
- Native review status: issues_open under the skill's counting rule (1 architecture issue + 7 proof gaps = 8), even though all review choices are resolved. This is a mapped implementation plan, not a passing code review.
- Operational review: no new durable learning beyond existing project rules; no memory update requested or made.

## T1 本地实施记录 — 2026-11-24

本节晚于上方工程计划审查，记录本次未提交工作区的软件实现，不表示其余阶段完成或分支已合并、部署。

- `_advance_put` 删除空 FAILED 推导确认未放料及发送失败报告的分支；失败事实仍按原动作消费，原 command/call、allocation、binding 及 pending 保留。
- `_advance_report` 对旧失败报告保持当前动作及待办，不因未送达或已 ACK 回退到 PUT、关闭业务或创建新意图。已经 pending=false 的存量不自动重新启用；旧报告的原 outbox 状态与正文未改写，仍可能送达并释放预约，T5 必须调查。
- MOVE_FORWARD、MOVE_TO_NG 的既有失败等待增加重建/重扫及 pending 断言；放盘失败增加货架责任回归。正常成功报告、NG 严格成功及 F01 离场接纳规则保持。
- 代码审计：粗分服务仅 NG_COMPLETED 和 COMPLETED 两个真实完成路径仍设置 pending=false；未增加模型、迁移、配置、注册入口、Sender 或调度器。
- RED：定向回归 6 failed / 4 passed；GREEN：相同选择 10 passed，均含 SQLite 与独立 PostgreSQL。
- 五个受影响完整文件：344 passed / 12 skipped，240.10 秒；无未配置 PostgreSQL 导致的跳过。
- 仓库十 lane：8,015 个收集用例全部进入结果且无跨道重复，7,853 passed / 162 skipped / 0 failed，10/10 进程退出码为 0。162 个跳过中，81 个依赖其他未配置的独立迁移库，其他为后端或既有状态条件，均未算作通过。
- 运行库、history/Transport/SMT AUTO 迁移库及 Redis 按道独立；仅任务专用 PostgreSQL 使用 CI 风格测试参数，不证明崩溃刷盘耐久性。未执行远端 CI 或现场验收。
- 全仓 Ruff check、Ruff format --check（934 个文件）及 git diff --check 通过。
- 独立只读代码审查无 Critical/Important 代码问题；审查发现的流程表失败反馈文案已按用户影响修正。未委托实现，也未再次委托复审。
- 验证过程：单进程全量在 523 passed 后为改用十 lane 主动中断，不计全量通过；首次分片因一次性 PostgreSQL 重启后随机映射端口变化产生连接错误，停止并动态读取端口重跑，失败首轮不计通过。最终统计仅来自完整成功的十道。
- 证据：本计划的任务专用 `.superpowers/sdd/2026-11-24-coarse-sorting-ecs-recovery/` 内保留 progress、RED/GREEN/受影响日志、十道 JUnit、collection 及 verification-summary.json；失败首轮日志独立保留。
- 下一阶段仍受 T0 限制：不猜测更正 wire，不声称 ECS 自主恢复/WMS 预约闭环，不执行真实恢复、业务数据修复或部署。代码未提交、未推送。

## T4a 续接实施记录 — 2026-11-24

本节记录当前未提交工作区的局部完成，不替代 T2/T3 的合同定义或 T5 现场调查。

- 粗分 `retention.py` 按 pending 或未完成状态保护原 WMS action/message，涵盖验证、分配和失败报告；不重新启用旧 inactive 业务。完成后正常释放的对照仍通过。
- 复审发现共享 HTTP 发送尝试清理绕过业务保留：过期租约收到的原 WMS 回复虽不能结算当前消息，却可能是唯一可核对事实。`http_exchanges.py` 复用原 message/exchange/request 身份、消息生命周期和现有通用 owner hold，同一条件保护真实回复与过期投影；不增加业务专用清理模型、不采用迟到结果或改写原报告。
- 修复前 SQLite/PostgreSQL 均复现三类原始回复不可读取；修复后冻结快照定向验证 **152 passed / 10 skipped / 0 failed，99.05 秒，退出码 0**。10 个跳过均为 SQLite 不适用的 PostgreSQL 专属场景，对应 PostgreSQL 变体已运行。八个 runtime 回复保留用例、PostgreSQL 投影锁竞争、原货架证据保留及现有 runtime authority 迁移/重放兼容性均通过。8 个修改代码/测试文件的 Ruff 检查与格式检查、diff 检查通过。
- 证据：`.superpowers/sdd/2026-11-24-coarse-sorting-ecs-recovery/T4a-fix1-green-manifest.json`、`T4a-fix1-focused.log/xml`、`T4a-controller-evidence.md`；覆盖文件 SHA256 在定向门禁后保持一致。此前 T1 全套结果不作为本轮共享依赖变更后的全套证明，本轮未重复全套测试。
- [合同差异与存量调查前置](../../integrations/coarse-sorting-recovery-contract-delta.md) 已按用户指定的唯一既有合同整理，不要求额外签认；明确业务字段、公开结果和审计原文的不同来源。它是调查说明，未生成现场存量清单。§6/P09 未定义 ECS 更正报文，普通不同 Result 仍维持冲突保护。
- 本轮没有提交、推送、部署、对现场系统发请求或修改存量数据；旧失败报告原 outbox 仍未改写，后续实际送达及预约状态须按 T5 核实。
- T4a 复审：Spec PASS / Quality PASS；共享清理证据丢失和两项文档来源/签认问题均关闭，没有新增 Critical/Important。复审未重复状态测试，核对门禁哈希与结果；本轮专用 PostgreSQL/Redis 容器及匿名卷已移除并验证不存在。T4a 状态为 **IMPLEMENTED - FOCUSED VERIFIED**，不表示全计划、合并或现场验收完成。

## 明确放料失败闭环修正 — 2026-11-24

本节晚于上方 T1/T4a 历史实施记录，按用户最新口径修正本轮目标。用户明确：放料失败也是物料闭环，不得卡住后续物料；执行结果只有成功和失败。本轮不新增 ECS 结果更正协议。

- 用户实际回报形状：`result=FAILED`、`data={"actual_qty":0,"pick_and_put_result":"PUT_FAILED"}`、错误 `1005 / Cancelled by operator`。现有 PUT 通用位置结果模型因数量限制为 1 返回 400；先在 SQLite 和 PostgreSQL 公开回调复现，再修正为 PUT 专用解析。原附录身份、SUCCESS 数量/位置和 NG 规则保持，兼容既有空 data 失败形状。
- 明确原失败按原绑定、原分配、原错误及 finish_time 生成一次失败报告；WMS RECORDED/DUPLICATE 后保存独立 `PUT_FAILED`、`pending=false` 和业务闭环时间，`placed_at_ms` 为空。报告失败只重试原意图，不重做放料。
- 原命令的严格失败证据可解除对应放料执行占用；货架准备与空闲退架复用同一原事实核验。覆盖报告重试时后续物料继续、失败闭环后新物料继续，以及真实进程重建后旧失败闭环和后续正常物料完成。
- 新增增量迁移 `coarse_put_failure_v1`（前序 `ecs_request_projection_v1`），只调整四项状态约束。旧记录不批量转换或激活；失败终态需原动作/分配关联及闭环时间、禁止放料时间，存在失败证据拒绝降级。
- 独立 PostgreSQL 17.10 数据库的 upgrade head/current/check 全部通过，唯一 head 正确，无新增迁移操作；SQLite/PostgreSQL 的新状态约束及保护性降级用例通过。该命令记录另含既有 SMT 循环外键警告，不因此声称所有历史外键都已重新验证。
- 首轮综合定向门禁：683 项中 644 passed / 35 skipped / 4 failed，502.05 秒。四个失败根因是历史迁移测试提交旧约束并丢失新索引，污染后续共用测试结构。后续排查补齐直接放料和 NG 两个会提交历史迁移的调用方，统一恢复当前约束/索引；同进程按直接放料 3 项 → NG 5 项 → 实际失败报文 → 原 4 项失败的顺序复验，13 passed。
- 复审发现的失败报告跨 MOCK 轮次及历史 ID 参数上限均已修复。既有失败报告可跨轮次消费原 ACK；数据库筛选相关 PUT/分配动作，固定前序以单个 JSON 参数传入，保留缺失/冲突检查和动作升序锁。PostgreSQL 32,768 条历史记录的真实前序等待/明确失败后推进回归通过。
- 最终保持同一 20 文件/选择器范围，以仓库现有两道机制在独立 PostgreSQL/Redis 及迁移库运行：一道 346 passed / 18 skipped，另道 305 passed / 18 skipped，均退出码 0。合计 **687 个唯一用例，651 passed / 36 skipped / 0 failed/error**；逐项核对原 683 用例加 4 个新回归，missing/extra/duplicate 均为 0。36 个跳过均为 SQLite 不适用的 PostgreSQL 场景，无未配置存储跳过。
- 一道首次建临时库时被既有历史维护门禁拒绝；单独复验通过，监测到 Telemetry Reporter 事务但未捕获原拒绝瞬间，不宣称已唯一归因。按仓库 CI 文档将该任务专用 PostgreSQL 实例遥测设为 off 后，完整重跑该道通过；生产门禁和断言未改，失败尝试未计为通过。
- 最终快照 48 项哈希与迁移 CLI 6 项哈希保持一致；16 个修改 Python 文件的 Ruff/格式及差异检查通过。服务/解析/货架与终态/迁移/保留两个范围的 Spec/Quality 均 PASS，3 项 Important 全部关闭，无新增问题。状态 **IMPLEMENTED - FOCUSED VERIFIED**，不等同于全仓 CI、合并、部署或现场验收。
- 原始日志、哈希及审查记录位于本计划 `.superpowers/sdd/2026-11-24-coarse-sorting-ecs-recovery/`；最终 `failure-final-reconciliation.json`、`failure-final-green-manifest.json`、`failure-final-lane1-ci-manifest.json`、`failure-final-fix1-review.md` 及分道 log/xml 均保留。4 个任务专用测试容器和匿名卷已清理并验证不存在；未提交、推送、部署或修改现场数据。

## 放料结果与报告状态分离：本轮实施记录

- 新增 `put_result=SUCCESS/FAILED` 与 `report_status=PENDING/COMPLETED` 两字段。接收器持久保存原 ECS Result；业务事务消费原事实后先保存执行结论并登记报告义务，Sender 再发送。失败报告尚未收妥为 `FAILED/PENDING`，收妥后为 `FAILED/COMPLETED`，失败不写 `placed_at_ms`。作业 `COMPLETED` 仅表示反馈流程完成，不代表放料成功。
- 成功与明确未放料失败均沿原证据解除对应执行占用；原报告重试、WES 重建和轮次切换不重做物料、不永久卡架。后续物料在首件报告仍待收妥时可独立完成。普通不同 Result 的 409 与严格目标/数量/原身份校验保留。
- 未发布、未提交的 `coarse_put_failure_v1` 迁移增加两字段和约束，回填可确认的旧成功状态，保留原身份及 inactive 责任。存在失败事实或缺原报告动作的待报告记录时拒绝丢失证据的降级；已发布迁移未改。
- 复审发现历史七字段成功报告与新十字段比较会卡住。SQLite 与 PostgreSQL 均先复现，再限定兼容原严格成功、全部七字段及目标/时间匹配的旧报告，保留原报文和 operation_id；失败或错误目标不放行。两处测试种子分别适配新终态字段和固定旧版表结构，原清理/回填断言保留。三项 Important 均关闭，规格与质量复核通过。
- 根代理定向 PostgreSQL：原历史迁移顺序及两状态场景 18 passed；修复后历史报告/保留/回填 6 passed。最终冻结同一 50 项快照，以独立 PostgreSQL/Redis 与迁移库运行既有 20 文件/选择器范围及一个旧回填选择器：一道 306 passed / 25 skipped，另一道 355 passed / 11 skipped；合计 **697 个唯一用例，661 passed / 36 skipped / 0 failed/error**。missing/extra/duplicate 均为 0，36 个跳过均为 SQLite 不适用的 PostgreSQL 专属场景，无未配置存储跳过。
- 最终代码/依赖/文档 50 项哈希与迁移 CLI 7 项哈希一致；迁移 `upgrade head`、`current`、`check` 通过，无待生成升级操作。20 个修改 Python 文件的 Ruff、格式及差异检查通过。失败尝试日志保留，未计作通过。证据见本计划工作区 `separate-fix1-final-manifest.json`、`separate-fix1-reconciliation.json`、`separate-fix1-review.md` 和分道 log/xml；本地状态为 **IMPLEMENTED - FOCUSED VERIFIED**。
- 本轮仅使用四个任务专用一次性测试容器。未提交、推送、部署或修改现场/存量业务数据；软件回归不代替现场物理验收。

## Ship 审查补充修复记录

发版前审查补齐三个边界：已记录放料事实但因目标证据不足尚未建报告时，运行记录清理后仍可按原严格保存凭证恢复；拒绝 `SUCCESS` 与 `PUT_FAILED` 相矛盾的回报，并拦截历史同类证据的成功报告/货架放行；新架准备不再默认纳入已经 COMPLETED 的历史失败，但既有冻结前序仍全部核验。先在 SQLite 和 PostgreSQL 复现，再作最小修复；修复后 31 项定向 SQLite、涉及的直接放料/架准备两文件 94 项 PostgreSQL 通过。此前 697 项与迁移 CLI 记录保留为前一快照证据，最终 ship 的全仓检查、两轮审查及十道 CI 以 PR 的当前提交收据为准，不把旧快照冒充当前全套结果。

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | NOT RUN | No significant product expansion proposed |
| Outside Review | Claude Code resolver / `codex-plan-review` | Independent 2nd opinion | 1 | unavailable | host=codex; provider=claude-code; phase=plan-review; no command resolved, no external review completed |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | ISSUES OPEN | 8 items: 1 architecture issue and 7 planned proof gaps; 0 additional silent/unhandled critical gaps; all review decisions resolved; host=codex; phase=plan-review |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | NOT RUN / NOT APPLICABLE | No UI change |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | NOT RUN / NOT APPLICABLE | No new developer-facing product flow |

**OUTSIDE COVERAGE:** claude-code / plan-review / unavailable; resolver found no command, Codex has no read-only native fallback. No independent findings, agreement or clean-review credit.

**VERDICT:** The original native engineering plan review recorded approved amendments (D3=A, D4=A) and remains ISSUES OPEN for the complete recovery scope. Subsequent local T1 implementation and its isolated software verification are complete as recorded above; remaining correction implementation/proof and four external T0 premises are not cleared. Eng review required before shipping the complete recovery scope. This does not authorize deployment, physical execution or live data repair.

NO UNRESOLVED DECISIONS
