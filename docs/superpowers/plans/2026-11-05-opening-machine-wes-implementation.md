# 开箱机 WES 内部开发实施计划

> 实施时按任务顺序逐项验证。可使用 `superpowers:executing-plans`，或在明确授权分派后使用 `superpowers:subagent-driven-development`。复选框用于跟踪实施进度。

**目标** 在 WES 内完成开箱机工单、选源、设备指令、整层入账、补取和整单通知，并用模拟报文验证状态推进。

**架构** 拟新增 `app/businesses/opening_machine` 业务包。任务拥有业务状态，纸箱拥有已确认占位，原动作和逐层事实拥有各自身份。复用 WES 的回调注册、可靠发送、公共结果读取与提交后唤醒。

**技术基线** Python、FastAPI、SQLAlchemy、Alembic、Celery、pytest。开发验证使用 WES 本地应用及模拟对端。

**设计依据** [概要设计 0.21](../../integrations/opening-machine-overview-design.md) · [配对算法](../../integrations/opening-machine-pairing-algorithm-design.md) · [API 对接稿](../../integrations/opening-machine-api.md)

## 全局约束

- 交付范围为 WES。WMS 下发工单、确认开工绑定、校验箱码、提供新栈板补给方案及 Transport 结果。WES 复用粗分机所用的公共 RackDispatch 和 Transport 提交搬运。ECS 提供原取箱 Result 和设备事件，AGV/MCS 与 ECS 执行现场搬运及门禁。外部交互以模拟报文验证。
- 当前工单的 WMS 开工绑定确认是新箱供箱、缓存箱恢复和余箱取料的共同前置条件。指令 ACK 表示接纳，设备动作按匹配的原结果推进。
- 每层 2 片，任务奇数套数向上取偶。取料位、余料缓存各一箱。每线同一时点最多一个执行任务、一个未结清取料额度。选源及双余箱保护沿 0.21。
- ARM03 到箱且可取料时，在同一次推进登记本箱额度及下一箱预判。原额度结清后按两侧实际成功量和占位复核计划。ARM01 提前备箱待 ECS 联调，当前按串行路径登记新取箱。 [下一箱计划](../../integrations/opening-machine-api.md#next-carton-planning)
- 测试中的 `OPEN_CARTON`、`PICK_LAYER_QUOTA`、`TASK_COMPLETED`、到箱事件名沿 API 草案。正式注册使用确认后的命令附录及报文字段。
- C07 本地采用原额度汇总 Result 唯一入账。逐层明细用于关联、冲突识别及追溯。C09 本地用技术结果确认设备接收整单通知，设备内部补空板和出料由设备跟踪。两处均是待对齐的接入方案。

## 评审重点

| 易错输入 | 预期 | 覆盖任务 |
|---|---|---|
| 新单在等开工确认，旧单 `RECORDED` 迟到 | 旧响应只归原通知，新单继续等待 | 2 |
| 搬运已接纳但新栈板未确认到位 | 仍等待原 ENTER 已应用成功及可取箱条件 | 3 |
| 原 ENTER 重发或回报错任务、错栈板、错工位 | 匹配原等待只登记一条新取箱，其他回报保持原归属 | 3 |
| ARM03 到箱重发，或额度实际成功量改变选侧 | 当前额度和候选计划只登记一次；原 Result 结清后按实际成功量复核。相关占位和路径允许后才下发下一箱 | 3、4 |
| PUT 先到、同层冲突或额度 Result 重发 | 稳定配对；冲突暂停；成功、NG、余量只入账一次 | 4 |
| 同产品双余箱续单，或换产品时仍缺清线确认 | 双位有锁死风险时待腾位；换产品等待确认 | 1、2 |

## 1 任务状态机与推进门槛

状态名为本计划拟定的 WES 内部枚举。动作等待记录在原动作行，新栈板补入引用公共入场作业，`RUNNING` 的工单可处于待箱码、待开箱、待取料位、额度执行、待补给或待余箱移动。冲突保留原阶段及原因，由推进函数检查 `hold_reason`。

| 工单状态 | 进入条件 | 后继 |
|---|---|---|
| `QUEUED` | 原下发通知与工单同事务保存 | 本线取得唯一执行资格且交接允许后，登记开工通知，进入 `WAIT_START_BINDING`。换产品待清线、双余箱待腾位时保留原任务 |
| `WAIT_START_BINDING` | 开工通知已登记 | 原 `task.started` 的有效 `RECORDED` 或 `DUPLICATE` 证明当前绑定已保存，进入 `RUNNING`。未知响应保持原通知 |
| `RUNNING` | 当前工单已确认开工绑定 | 按下表推进；双侧目标已达且额度、在途层和相关来源动作均已结清，冻结供料汇总并进入 `WAIT_COMPLETION_NOTICE` |
| `WAIT_COMPLETION_NOTICE` | 双侧供料结清、汇总意图已保存 | 应用匹配的 WMS 整单通知，登记一次转发，进入 `WAIT_COMPLETION_FORWARD`。早到的原通知保存后按结清条件复核 |
| `WAIT_COMPLETION_FORWARD` | 原转发意图已保存 | 按 C09 对齐的技术结清条件进入 `COMPLETED`，释放本线执行资格。余箱位置及余量供交接继续使用 |
| `COMPLETED` | 原通知转发技术义务结清 | 重复回报和历史结果按原归属处理。WMS 允许后补的箱级报告继续由原发送意图恢复 |

| `RUNNING` 子流程 | 权威输入与下一步 |
|---|---|
| 选源与取箱 | 先处理可复用的当前箱或缓存箱。需新箱时按成功量选侧。ARM01 原成功 Result 更新上料占位，明确缺箱且未搬箱则保留原意图等待补给 |
| 开箱与到位 | `SCAN1` 匹配原取箱，WMS 校验通过且箱号、产品、盖型及数量匹配后登记开箱指令。核对 ARM03 实际到箱、原供箱关联和已放行记录。取料条件齐备后，同次登记当前额度和下一箱候选；预计量只用于选源。开箱 Result 字段按 C03 对齐 |
| 补给恢复 | 原取箱明确未搬箱后，按 WMS 方案关联当前工位的公共入场作业。核对原 ENTER 已应用成功、栈板和点位匹配且工位可取箱后，原子关闭原等待并登记一次新取箱 |
| 额度与 NG | 额度为箱剩余层数和本侧成功量缺口的较小值。原 Result 的实际取出、成功、NG、未结清和箱余量按冻结额度校验。未结清层或矛盾事实保持待核对。入账后按实际成功量和占位复核候选，决定续取、补取、恢复余箱、取新箱或结束。已有 ARM01 原指令待结清时先核对其纸箱去向 |
| 数量差异与取料恢复 | 确认提前箱空后按实际取出结算并独立报告短少，原箱离位后可供下一箱。额度用完且核实多料后结算本轮取出并报告差异，多料箱等 WMS 的继续取用或退料决定。确认零片离箱的取料失败先结清已完成层，等匹配的 ECS 恢复事实再授权；离箱数量不明或部分离箱进入原动作核对 |
| 清箱和缓存 | 实际离位只释放原箱匹配占位，额度仍按原箱记录结算。新箱到位可先保存，原额度结清、旧箱离位和新箱匹配到位后才发新额度。余箱移动按原 Result 更新位置。发现双余箱无法让位时，收到 WMS 匹配的实际移箱事实后重新选源 |

## 2 数据模型和事务

以下四张表为拟定的业务模型，表名及迁移按实施分支确认。新栈板补入复用公共 RackJob、RackAction 和 Transport；框架已有的收据、出站消息、命令和结果证据表继续作为协议记录。

| 模型 | 持久化内容 | 唯一性及归属 |
|---|---|---|
| `OpeningTask` / `opening_task` | WMS `task_id`、工作线、产品、工单版本、原套数、执行层数、双侧取出/成功/NG 计数、阶段、阻塞原因、原开工和汇总动作引用、原整单通知引用 | `task_id` 唯一。同一工作线只有一个占用执行资格的任务。用部分唯一约束保护执行阶段。原输入与任务重复下发比较，异内容保留冲突 |
| `OpeningCarton` / `opening_carton` | 原取箱动作、外部箱号、校验通过的 SCAN1 外箱条码原文、产品/盖型/料号、所在工作线及位置、入线数量、实物余量及可取余量、待恢复或处置原因、当前任务归属 | 原取箱动作唯一。取料位和缓存位置各有一个有效占位。箱号为业务标识，重新入线另建来源记录。跨任务复用更新当前归属，历史消耗归原额度动作 |
| `OpeningAction` / `opening_action` | 工单、动作种类、唯一业务 `action_key`、原箱及阶段、WMS 消息引用或 ECS 原命令引用、授权层数和下发时余量快照、下一箱候选盖型和来源、结算摘要、处理标志。原缺箱动作保存公共入场作业及恢复取箱动作引用 | `(task, action_key)` 唯一；外部引用在各自类型内唯一。候选随额度动作保存，结算时更新。每工单最多一个未结清来源动作，串行额度独立受同一执行资格保护。额度与原动作结算后供汇总使用 |
| `OpeningLayer` / `opening_layer` | 原额度动作、层序、GET/PUT 各自原事件引用及规范化事实、终局及冲突状态 | `(quota_action, layer_seq)` 唯一，同一层分别保存 GET 和 PUT。迟到明细只核对原额度；相反终局留证并暂停尚待执行的推进 |

产品切换使用当前线已持久化的前单产品及占位记录。清线确认匹配工作线及前后工单，再逐箱以原工位和 SCAN1 条码原文匹配待清占位。WES 由占位记录取得箱标识和已结算余量，关闭原余箱来源记录后进入开工流程。同产品双余箱续单若无法让位，WMS 组织单箱实际移出，按本次交接及原工位、外箱码仅释放该箱占位，重新检查开工资格和 WMS 绑定。WMS 独立办理退料实点及签发新码。工单汇总只冻结双侧取出、成功和 NG 等工单数据，余箱占位和余量沿纸箱记录保存。若启用逐箱耗用，用原额度结算按工单和来源箱归集，在 `OpeningAction` 中按工单及来源箱登记一次独立报告。来源箱关联键按 WMS 库存合同确认。后续续单不改写原报告。

回调在接收事务内保存原事实与待推进记录。`scan_pending` 使用 `Capabilities.session_factory` 开独立事务，先设 `REPEATABLE READ`，再按 `WorkLine → Devices（按 ID）→ 业务动作` 加锁并复核当前资格。原结果仅从公开 reader 读取。消费身份、状态、计数和下一动作意图同事务提交。`40001` 使用新事务有界重试，耗尽保留待办。由现有提交后通知及 5 秒 Beat 调用同一业务推进入口。

消费有效到箱事实时，同一事务绑定占位、登记额度发送意图并保存下一箱候选，提交后发送额度。额度动作固定引用 `OpeningCarton` 入线记录 ID，离位保留该记录。消费原额度 Result 时，同一事务按原箱更新实物余量、可取余量、待恢复或处置状态及实际数量，差异报告按原额度与箱登记一次。复核候选后按占位及来源路径条件登记可执行的后续动作。到箱、离位和 Result 各自保存事实及消费身份，任一事实到达后都重评原等待；重复旧回报不释放新占位。

## 3 接口注册与文件归属

正式包使用 `definition.py` 声明业务代码 `opening_machine`、设备角色和工位槽，`composition.py` 声明 `ENTRY_MODULES`、`MODEL_MODULES`、`COMMANDS` 和 `PROGRESSION = "scan_pending"`。工单下发是初始入口。使用开箱机专属逻辑角色区分 `ARM01`、`ARM02`、`ARM03`、`STATION_OUTPUT01`、SCAN1 至 SCAN3、A/B 和 CCD，实际编码通过工作线绑定。这样同名事件或动作可以按角色区分。

| 方向 | 拟注册合同 | 归属 |
|---|---|---|
| WMS → WES `/api/v1/wms/events` | `opening_machine.task.issued@v1`、`opening_machine.task.completed@v1` | `callback/opening_machine_task_issued_v1.py`、`callback/opening_machine_task_completed_v1.py` |
| WMS → WES `/api/v1/wms/events` | `opening_machine.line.clearance_confirmed@v1`（拟议） | `callback/opening_machine_line_clearance_confirmed_v1.py`。WES 开发范围为回调接收、占位校验和开工门控，本地模拟 WMS 清线回报。PC 或 PDA 页面及独立退料由 WMS 提供 |
| WES → WMS `/api/v1/wes/decisions` | `opening_machine.carton.validate@v1` | `request/opening_machine_carton_validate_v1.py`。本地按同步 `DECIDED` 验证 |
| WES → WMS `/api/v1/wes/facts` | `opening_machine.task.started@v1`、`opening_machine.task.supply_report@v1` | `request/opening_machine_task_started_v1.py`、`request/opening_machine_task_supply_report_v1.py` |
| WES → WMS `/api/v1/wes/facts` | `opening_machine.carton.consumption_report@v1` | `request/opening_machine_carton_consumption_report_v1.py`，WMS 确认按箱扣账后正式注册 |
| WES → WMS `/api/v1/wes/facts` | `opening_machine.carton.quantity_discrepancy_report@v1`（拟议） | `request/opening_machine_carton_quantity_discrepancy_report_v1.py`，按 C02 确认后正式注册。多料处置决定和同产品腾位回报的承载另待 WMS 联调 |
| WES → ECS `/api/v1/device/command` | `PICK_AND_PUT`、`OPEN_CARTON`、`PICK_LAYER_QUOTA`、`MOVE_BACKWARD`、`MOVE_FORWARD`、`TASK_COMPLETED` | `ecs_command.py` 的严格命令模型，经 `composition.COMMANDS` 编译。新增命令及完成通知承载待对齐 |
| ECS → WES `/api/v1/callback/event` | `SCAN_COMPLETED`、`CARTON_AT_PICK_STATION`、`GET_COMPLETED`、`PUT_COMPLETED` | `ecs_event/` 按事件名小写分文件。SCAN1 校验、SCAN2/3 追溯分别按角色声明。空箱释放按已对齐的原 Result 或回报处理 |
| ECS → WES `/api/v1/callback/result` | 原 `command_code` 及设备身份 | 复用通用结果接收与公开读取。按原动作消费，ECS 重发沿原身份 |

搬运提交及回调沿 `transport.task.submit@v1` 和 `transport.task.resulted@v1`。业务装配引用公共 RackDispatch 组件并复用其注册和结果读口。以开箱机归属装配 A/B 点位、新栈板 ENTER 作业和 `f0` 模板，沿开箱机的原缺箱与 WMS 方案提供业务依据。C/D 空栈板由独立流程处理。粗分的箱格分配合同及在位证明仍由粗分业务使用。

上述业务文件路径均位于 `app/businesses/opening_machine/`。业务服务 `service.py` 提供 `build_service(capabilities)` 对应的状态推进服务，建议按 `source.py` 管理供箱、补给与缓存，`settlement.py` 管理额度及计数。`models.py` 声明业务表。WMS 接收 `PALLETS_EMPTY` 并提供方案。WES 提交原 Transport 并消费原结果，门禁及现场互锁由外部系统执行。

本地测试通过隔离业务目录组装正式形状的合同、模拟对端和测试表，避免将草案注册进部署目录。正式接入由 `catalog.entries()` 一次装配业务，API、worker 和迁移共同校验。若箱码决策最终选择异步，先确认其回调名和原请求关联，再登记对应入口。 [业务包接入](../../integrations/business-packages.md) · [接口注册](../../integrations/registration.md)

## 4 实施顺序

### 任务 1：状态和持久化骨架

**文件** `definition.py`、`composition.py`、`models.py`、`service.py`，`tests/businesses/test_opening_machine_state.py`。迁移在正式装配前基于届时唯一 Alembic head 编写。

- [ ] 先写工单重复下发、不同内容冲突、同线两个工单争夺执行资格、换产品无清线事实及双余箱占位测试。测试表使用独立 `MetaData`。
- [ ] 串行模拟同产品两个 6 套工单。首单结束后 ARM03 下盖余 10 片、缓存上盖余 16 片。第二单开工前识别无法让位，注入错箱、错位和重发的腾位回报。只有匹配的实际移出确认释放一处，随后仍等 WMS 绑定确认。
- [ ] 实现四张业务表的唯一约束、状态变更和工作线执行资格。验收交接先于开工，余箱记录能跨同产品工单保存。
- [ ] 用 `rtk uv run pytest tests/businesses/test_opening_machine_state.py -q` 检查同线仅一个任务获准，重复下发只形成一个工单。

### 任务 2：WMS 工单、开工通知和门控

**文件** 工单 callback、started request、`service.py`，`tests/businesses/test_opening_machine_start.py`。

- [ ] 通过真实 WMS 回调入口注入同一工单，核对首次受理和重发只创建一份开工通知。
- [ ] 按原通知读取并校验开工结果。注入超时、旧单成功回报、同身份 `RECORDED` 或 `DUPLICATE`。前两种情况下 ARM01、新额度和缓存恢复命令计数均为 0，匹配确认后才有首次授权。
- [ ] 运行 `rtk uv run pytest tests/businesses/test_opening_machine_start.py -q`。模拟发送重试保留 `operation_id`、原时间和正文。

### 任务 3：取箱、开箱与补给恢复

**文件** `source.py`、`ecs_command.py`、箱码 request、SCAN1/到箱事件处理及公共 RackDispatch 组件装配，`tests/businesses/test_opening_machine_source.py`。

- [ ] 从原取箱 Result 到 SCAN1 决策、开箱、ARM03 到位串通一箱。模拟 WMS 按 `task_id` 查期望产品，WES 按原 ARM01 指令核对来源工位。通过原校验 `operation_id` 在 WES 内部关联 `scan_ref` 和原开箱指令。同箱同码重扫后，旧决定迟到也不能放行。错产品/盖型和未知占位保持原等待。移动余箱后才更新缓存和取料位。
- [ ] 注入重复到箱、错箱或错原供箱关联，以及原额度尚未结清时先到的到箱回报。只应用匹配且具备取料条件的到位事实，供任务 4 同次登记额度及预判。
- [ ] 模拟 548 在执行、558 在排队，分别注入新栈板补入和等待方案。同一工位复用原 ENTER 作业，冻结 `f0` 模板，超时和重发沿原 Transport 身份。覆盖 C/D 空栈板处理与 A/B 入场各自推进。
- [ ] 模拟搬运 ACK、旧任务或错工位结果及正确 ENTER。原缺箱明确未搬箱、原 ENTER 已应用成功且工位可取箱后，在同一事务关闭等待并登记一条新取箱。重复回调和 Result 不新增取箱。
- [ ] 运行 `rtk uv run pytest tests/businesses/test_opening_machine_source.py -q`，检查模拟入口和出站请求身份可串联。

### 任务 4：额度、下一箱预判与复核、NG 补取

**文件** `source.py`、`settlement.py`、额度命令模型、GET/PUT 和 SCAN2/3 事件处理，`tests/businesses/test_opening_machine_quota.py`。

- [ ] 先按 5 套工单、上盖箱 10 片建立用例。执行目标各 6 片，第一轮上盖额度为 3 层。模拟第 2 层 `PUT_FAILED` 先于 GET 到达并重发，事件只配对，等待额度结清。
- [ ] 用原额度 Result 回报取 3 层、成功 2 层、NG 1 层、未结清 0、箱余 4 片。WES 应记录取 6 片、成功 4 片、NG 2 片，只登记 1 层补取。原 Result 和失败事件重发后计数及意图不变。
- [ ] 按下节 100 片案例注入到箱、重复到箱、原额度 Result 和空箱离位。到箱同次登记一份额度及候选，未结算时只有计划。结算后实际成功量改变选侧，先更新候选，等离位且路径可用后只登记一条修正后的取箱指令。覆盖两侧预计均达标时无新箱候选，以及有可用缓存箱时优先恢复。
- [ ] 按下节覆盖 22/20 短少、22/24 多料与处置、取料失败后恢复。分别注入有效、迟到及重复结果。单独验证多料报告收妥后仍等待业务决定，明确退料后等实际离位，ECS 恢复未确认时保持 0 条新取料指令。
- [ ] 枚举空箱离位、原额度 Result、新箱到位乱序到达的 6 种排列，并重发旧箱离位及 Result。核对原箱仅结算一次，旧回报只作用于原箱，三个事实均匹配后才下发一份新箱额度。
- [ ] 覆盖超额度、余量不守恒、相反 PUT 终局及未结清层。冲突保留证据，待结清前下一额度数为 0。SCAN2/3 缺码及迟到仅影响追溯。
- [ ] 运行 `rtk uv run pytest tests/businesses/test_opening_machine_quota.py -q`，逐项核对余额与外发命令。

### 任务 5：整单收口、恢复和 WES 组合验证

**文件** 工单汇总及拟议逐箱耗用 request、整单完成 callback 及转发模型、`service.py`，`tests/businesses/test_opening_machine_flow.py`。

- [ ] 双侧结清后冻结一份供料汇总，发送执行目标和结算数量，模拟 WMS 按 `task_id` 查原需求。提前通知只保存，当前工单可收口后生成原转发。重复通知不新增转发。用模拟技术结果确认转发阶段结束，保留余箱事实供续单。
- [ ] 模拟启用逐箱耗用。同一箱多轮取料、入缓存和复用后只登记一份本单耗用，实际取出量包含整层 NG。模拟发送超时和重发，箱级报告沿原身份恢复，工单汇总保持独立。WMS 若允许扣账后补，收到原完成通知仍可正常转发，未结清箱级报告继续恢复。
- [ ] 按正常接收路径走完一单，包含开工等待、缺箱补给、重复回报、整层 NG 补取和整单通知。模拟原命令 Result 丢失后按同一 `command_code` 恢复。验证状态、原身份和数量均只推进一次。
- [ ] 验证提交后唤醒失败仍由 Beat 扫到持久待办，重复触发只消费一次原结果。使用一次性 PostgreSQL 验证同线领取、补给消费并发、原 Result 结算竞争及 `40001` 回滚重试。SQLite 用例用于状态和报文回归。
- [ ] 运行 `rtk uv run pytest tests/businesses/test_opening_machine_*.py -q`，再对变更路径运行 Ruff 和 `git diff --check`。记录本地用例、一次性数据库检查及仍待签认的接口项。

## 5 模拟报文与验收断言

测试夹具从 0.21 API 报文取完整外壳和字段。以 `OPEN-001 / 548`、`OPEN-LINE-01`、需求 5 套串联，另排一单 558。WMS 模拟器经出站适配器返回原请求响应，ECS 回报走通用回调。WMS 模拟器通过既有 Transport 回调提供原搬运结果，ECS 模拟原缺箱 Result。

| 场景 | 报文和注入顺序 | 可观察断言 |
|---|---|---|
| 开工等待 | 下发工单后，开工通知首次超时，再注入旧 `operation_id` 的 `RECORDED`，最后返回本次原通知的 `RECORDED` 或等价 `DUPLICATE`。[开工报文](../../integrations/opening-machine-api.md#task-started) | 有效确认前是 `WAIT_START_BINDING`，供箱、余箱恢复和取料额度均为 0。确认后进入 `RUNNING`，只登记一次首动作 |
| 缺箱补架 | A 工位缺箱后，模拟 WMS 提供 548 上盖栈板方案和公共 Transport 接纳、原 ENTER 成功结果，以及独立的 C/D 空栈板处理结果。[缺箱 Result](../../integrations/opening-machine-api.md#carton-unavailable) · [新栈板入场](../../integrations/opening-machine-api.md#replenishment-handoff) · [原搬运结果](../../integrations/opening-machine-api.md#source-available) | 搬运接纳、C/D 空栈板处理结果、旧任务、错栈板或错工位结果均无法恢复。匹配原 ENTER 到位及可取条件后只生成一条新取箱 |
| 重复回报 | 原样重放上一步的 Transport Result、ARM01 Result，以及额度内同层 GET、PUT 和原额度 Result。另送同层相反 PUT 终局测试冲突 | 原回报重发后新取箱、额度入账和补取意图各仍只有 1 份。同层终局冲突保留证据并暂停下一动作 |
| 到箱预判与结算复核 | 两侧目标各 100 片，已成功上盖 0 片、下盖 20 片。22 片上盖箱到位并重发；原额度实际取完 11 层，9 层成功、2 层 NG。再确认空箱离位，两处无可用余箱。[下一箱计划](../../integrations/opening-machine-api.md#next-carton-planning) | 到箱时只下发一次 11 层额度，预计上盖 22 片并预判下盖。原 Result 结清后按实际上盖 18 片、下盖 20 片改为上盖。离位和路径可用确认后才下发一次 ARM01 上盖取箱 |
| 整层 NG 补取 | 上盖箱 `U001` 扫码确认 10 片，先发 3 层额度。第 2 层 `PUT_FAILED` 先于同层 GET 到达，随后提交 `drawn_layers=3`、`success_layers=2`、`ng_layers=1`、`unsettled_layers=0`、`remaining_pcs=4` 的原 Result。[逐层事件](../../integrations/opening-machine-api.md#layer-events) · [额度 Result](../../integrations/opening-machine-api.md#quota-result) | Result 结清前补取数为 0。结清后该侧累计取出 6 片、成功 4 片、NG 2 片。只补取 1 层，成功后该侧达到执行目标 6 片，箱余 2 片 |
| 短少 22/20 | 原额度 11 层，ECS 确认箱空、10 层全成功、余量 0。分别先注入离位或先注入 Result。[提前箱空](../../integrations/opening-machine-api.md#carton-early-empty) | 取出和成功各 20 片、NG 0，短少 2 片只登记一次。两个事实齐备后恢复供箱，差异报告独立发送 |
| 多料 22/24 | 原额度 11 层均成功，核实余 2 片；依次模拟差异收妥和明确批准，另测退料决定。[多料处置](../../integrations/opening-machine-api.md#carton-excess) | 先结算取出和成功各 22 片，库存差异 +2 片。有效批准且有缺口时授权最多 1 层；退料只在实际离位后推进 |
| 取料失败后恢复 | 前 4 层成功，第 5 层取料失败且确认 0 片离箱。注入同身份重发、过时或错箱恢复，最后注入匹配的恢复事实。[取料失败](../../integrations/opening-machine-api.md#quota-pick-failed) | 取出和成功各 8 片、NG 0、箱余 14 片。匹配恢复前新额度为 0；恢复后按原箱和缺口登记一次。部分离箱或数量未知时保持待核对 |
| 工单汇总与逐箱耗用 | 启用逐箱扣账模拟。上例 `U001` 两轮取出 8 片，逐箱发送超时后重发，再由下一单取走剩余 2 片。[逐箱耗用](../../integrations/opening-machine-api.md#carton-consumption-report) | 原工单只报一次 8 片，续单只报 2 片。工单级汇总独立发送，扣账是否先于整单通知由 WMS 模拟对应规则 |
| 换型清线与独立退料 | 548 清线换 558，回报原工位和 PDA 扫描条码。模拟少报、错码、错位、重复条目和确认重发。[人工清线确认](../../integrations/opening-machine-api.md#line-clearance-confirmed) | 仅全部待清占位均唯一匹配时才释放一次。558 等开工绑定确认后供料，退料实点可随后处理。原箱码不放行，新码供原产品后续上线 |

### 正式装配条件

ARM01 提前备箱待 ECS 联调确认。确认独立上游等待位及可用状态回报、待用箱扣留和放行条件、与空箱离位及缓存恢复的互锁，以及计划变化后箱体留置或退出路径。当前 WES 实施及本地验收按到箱预判、结算复核和串行取箱执行。

WES 内部开发完成以本地链路和持久化检查为准。正式业务目录启用前核对 C01 开工、栈板方案、`f0` 模板校验接入及 ENTER 可取箱条件、C02 箱码决策、C03/C04 开箱与到箱释放、C05 至 C07 额度及入账、C09 转发技术结清、C10 清线确认来源和 C12 设备绑定及回报合同。正式登记 SCAN2/3 追溯回报前，还需核对 C11 条码与物料及层的关联、读码异常的记录方式。随后在实施分支加入 `catalog.entries()`，让 API、worker 与 Alembic 加载同一份正式声明，核对唯一 head 和公共入口回归。发布、真实设备联调和现场验收分别安排。
