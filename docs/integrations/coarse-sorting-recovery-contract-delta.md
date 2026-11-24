# 粗分恢复合同差异与存量处置前置

日期：2026-11-24。对应[恢复计划 T0 / T5](../superpowers/plans/2026-11-24-coarse-sorting-ecs-recovery.md)。

**依据：用户明确指定 [coarse-sorting-api-draft.md](coarse-sorting-api-draft.md) 为唯一现有合同，没有额外约定。** 本文是该合同与代码的索引及存量调查说明，不新增协议或独立签认门槛。按既有合同继续本地实施；软件验证与现场动作验收分别留证，不能把本地模拟轨迹当作实际 ECS 恢复。

用户进一步选择“保留现合同，仅完成现有恢复与回归”。因此本轮不实施计划 T2/T3 的 ECS 更正扩展，普通 Result 冲突规则保持；下文的未定义项作为未来范围记录，不作为本轮额外审批或待签认事项。

**最新修正优先：放料结果和报告状态分别记录。** 原 ECS 结果先可靠保存并由业务消费到 put_result=SUCCESS/FAILED，报告发送前 report_status=PENDING；WMS 收妥只使报告 COMPLETED，不决定或改写放料结果。原命令匹配、无冲突的明确未放料失败走原失败报告，后续物料继续；执行 Result 不增加 UNKNOWN。未收到回报是待回报，矛盾信息继续校验，不能凭时间生成失败。

## 已存在的 WES 合同

| 命令 | WES 冻结附录 | 当前参数与完成事实 | 证据边界 |
| --- | --- | --- | --- |
| MOVE_FORWARD | coarse_sorting.move_forward / 2 | barcode；业务推进要求原 SUCCESS 和 actual_qty=1 | 模型允许 FAILED 空 data；ACK/在线/动作开始不能证明完成 |
| MOVE_TO_NG | coarse_sorting.move_to_ng / 1 | barcode、reason_code；原 SUCCESS、actual_qty=1、原 NG 完成位置 | 已应用 FAILED 仍保留原责任；普通不同 SUCCESS 不是合法更正 |
| PUT_TO_BIN | coarse_sorting.put_to_bin / 1 | barcode、bin_location、bin_cell_location、bin_type、reel_totalthickness；严格成功须匹配原完成位置与数量 | 匹配的明确 FAILED、actual_qty=0 且 pick_and_put_result=PUT_FAILED（兼容原空 data），无冲突时报告失败；原报告收妥后失败闭环，不重做原放料 |

来源：[粗分声明](../../app/businesses/coarse_sorting/ecs_command.py)、[业务消费](../../app/businesses/coarse_sorting/service.py)。附录版本是 WES 注册版本，不能代替 ECS 程序版本。完整起止位置和高度语义差异继续以[粗分 API §5](coarse-sorting-api-draft.md#5-ecs-命令与结果示例)为准。

共享命令信封包含原 command_code、device_code、task_type、priority、timeout、timestamp 和 params；Result 包含 command_code、device_code、result、finish_time、data 与条件性的 error_detail。FAILED 必须带错误，SUCCESS 不允许带错误。来源：[信封](../../app/integrations/ecs_command/contracts.py)。

普通 Result 首次应用后，同内容重放原响应；新的不同内容按原冲突规则处理。它没有已冻结的粗分更正入口。来源：[接收器](../../app/integrations/ecs_command/result.py)。本文不新增路径、注册项或私有 data 开关。

## T0 既有合同与实现差异表

| 范围 | 当前 WES 行为 | 现有合同依据 / 实施边界 | 责任边界 | 当前状态 |
| --- | --- | --- | --- | --- |
| 原命令跨故障恢复 | 保存原 command/call/目标，重复扫描不另造动作 | §6 保留原身份；P09 列明持久接纳、去重、原结果重报和跨重启保证。WES 不重复已完成的物理动作 | ECS 执行及恢复，WES 持久待办 | 本地身份保护已实现；实际设备恢复不是软件测试结论 |
| FAILED 的执行责任 | WMS 收妥前已有 put_result=FAILED、report_status=PENDING；收妥后仅报告 COMPLETED，失败 placed_at_ms 为空 | §3.3 及最新分离规则：成功/失败由 ECS 给出，WMS 只确认报告，不换号重做 | ECS 原执行事实，WES 分别记录并报告，WMS 处理预约 | 两状态独立记录已通过本地定向验证（661 passed / 36 skipped）；此前内部 PUT_FAILED 口径为历史，未部署或现场验收 |
| 已应用失败后的成功 | 普通不同 Result 仍冲突；Transport 更正不能套用到 ECS | §6 明确“本轮暂不扩展 ECS 更正规则”；P09 没有给出新的入口、版本字段及更正响应 | ECS / WES | 计划 T2/T3 是待定义的协议扩展，不能当成现有 wire 实施 |
| 原目标预约有效性 | 保存原分配，明确失败经原报告收妥后由 WMS 按约释放；不能把旧目标直接用于重做 | §3.2、§3.3：WMS 管理分配和预留，WES 不自行更换或重复使用原目标 | WMS；物理互锁归 ECS/RCS | 后续物料走独立绑定和分配；现场调查需核实实际预约 |
| 失败报告后的成功报告 | 原失败已构成终局；不改发成功、重做或换 operation_id 覆盖 | §3.3、P10：原 workline_code + binding_id 的同事实幂等、不同事实冲突；不新增更正报文 | WMS / WES | 普通不同 SUCCESS 仍 409，不让旧失败影响后续独立物料 |
| 原货架动作和物理交接 | F01 DEPART 接纳与新架严格到位分别判断 | §6、B3/B4：原在途动作收口，原任务与方向严格匹配；未知不冒充实际移走或到位 | RCS/Transport、WMS、ECS 按原职责 | 复用原软件合同；现场实际交接仍需证据 |

现场或存量调查材料逐条关联实际程序版本、工作线与设备、原命令身份、原请求/ACK/Result、故障及重启时间、实际完成事实。保留原身份与原文的可审计证据，分享材料时移除凭据。它们是实际状态证据，不是本地实施必须另行取得的合同或签名。

## 按现有合同继续实施的边界

1. 实现和验证原身份等待、原 SUCCESS/FAILED 及时记录及原报告恢复；WMS 只完成反馈义务，不能把其收妥时刻当作得知放料失败的时刻。后续物料正常推进，不以缺少额外签认作为停止理由。
2. 保持普通 Result 的原响应重放及不同结果冲突规则，不增加未校验的 data 开关，不凭在线/空闲或超时解除责任。
3. 明确失败解除原执行占用，报告未收妥只重试原报告；已释放或缺证据的预约不直接复用，不自主更换目标或声称合法覆盖。
4. 若继续实施计划 T2/T3 的版本更正，须先在同一合同文档明确新增入口、字段和冲突规则；§6/P09 目前没有定义这些内容，不能把实施计划的设想当作既有报文。

这里区分现有成功/失败闭环和未来扩展，不新增审批流程。T1/T4 的本地修正继续推进；T2/T3 的协议扩展按用户选择不实施。此前 D3 的更正设计仅保留为未来方案，不作为本轮功能。

## T5 只读清单与分类

**本节定义清单和处置顺序，不是已生成的现场清单。** 本轮没有选定运行环境，没有查询现场库或发起对端请求。后续必须先确认目标环境、程序/数据库版本及只读访问范围；冻结可复核的清单后，业务修复另按用户授权执行。

| 清单字段 | 可信来源 / 注意事项 |
| --- | --- |
| 原物料、工作线、业务状态、pending、当前动作与分配动作 | [CoarseSortingJob / Action](../../app/businesses/coarse_sorting/models.py) 的原关联；pending=false 不等于物料真实完成 |
| command_code、device_code、call_key、原参数和目标 | 原 CoarseSortingAction.call_key / request_data / resource_snapshot 与业务分配关联；ECS 公开读口核对原命令/设备、类型、状态及已应用结果，不返回 call_key、参数或目标。不能拿当前配置替换历史 |
| 已应用结果与未决冲突 | [EcsCommands 公共读取](../../app/integrations/ecs_command/service.py)；缺证据、UNKNOWN、冲突和 FAILED 分别记录，不能补造成功 |
| binding_id、原 allocation、原报告 operation/message、业务请求数据与送达状态 | 原业务动作和 request_data；[WmsRequests.get_result](../../app/integrations/wms_request/service.py)只核对原身份、投递及响应，不返回请求正文。PENDING/SENDING/UNKNOWN/已 ACK 均纳入清单，ACK 不证明责任解除 |
| WMS 当前预约、过期/释放、原目标授权 | WMS 的明确证据；旧 ALLOCATED、旧失败报告或新 operation_id 均不能替代 |
| ECS 是否仍可能执行、已完成阶段 | ECS 原任务及物理证据；Status 在线/空闲或超时不替代执行责任结论 |
| 货架原任务、点位、朝向及未解责任 | 原 RackDispatch 与公开 Transport 事实；离场接纳不冒充实际移走，新架 ACK 不冒充到位 |
| 原始证据可用性 | 精确请求/响应字节由[现有受审计历史原文入口](../../app/controllers/integration_history.py)读取；目标端点和路径由[现有 diagnostics 元数据](../../app/services/http_exchange_diagnostics.py)的 peer_endpoint / endpoint_path 核对。业务 request_data 不是 wire，公开结果 DTO 不返回全部原文。正文不可用就记录未知，摘要不等于原文仍可取回 |

每条记录按以下证据分类，缺一项就保留相应未知：

- 未释放且原授权仍有效：保留原命令/目标，按已冻结的原任务恢复合同继续，不新造物理动作。
- 预约已释放或未知：列出目标争用风险；先取得 WMS 原目标授权及 ECS/RCS 安全交接证据，不能自主改派其他箱格。
- 实际已完成但未正确报告：核对原完成事实及 WMS 幂等规则，只处理原报告责任，不再执行物理动作。
- 失败报告已落账：按既有合同核对原报告及真实完成事实；没有覆盖规则就记录合同缺口，不改原正文或假设同键/换键成功可覆盖。
- 冲突或证据缺失：持续保留原责任；不能因超过保留期、报告已 ACK 或 pending=false 自动清理或补造完成。

## 受控修复方案的交付要求

执行前须有逐条原身份清单、授权依据、可恢复备份及摘要、原目标当前授权、对端恢复责任和幂等检查。备份只能证明可取回，不能用恢复备份抹去之后已发生的物理动作或已提交事实。

方案逐条写明允许变更的业务记录、严格前置条件、原命令/报告身份、执行后公开读取核验及失败处置；不提供通用批量启用或直接改结果指针的快捷操作。执行结果不明时先重读原身份和已提交证据，不以查空当作回滚证明，更不能换号重发。

任何安全交接或证据仍缺失时，该记录继续列为跨系统合同缺口；不宣称可直接恢复，不将正常扫描作为修复器。T4 保护证据、T5 形成清单、T0 冻结合同、授权后的存量操作分别留证，不能相互替代完成状态。
