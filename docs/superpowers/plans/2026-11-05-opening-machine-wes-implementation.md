# 开箱机 WES 内部开发实施计划

> 实施时按任务顺序逐项验证。可使用 `superpowers:executing-plans`，或在明确授权分派后使用 `superpowers:subagent-driven-development`。复选框用于跟踪实施进度。

**目标** 在 WES 内完成开箱机工单、选源、设备指令、整层入账、补取和整单通知，并用模拟报文验证状态推进。

**架构** 拟新增 `app/businesses/opening_machine` 业务包。任务拥有业务状态，纸箱拥有已确认占位，原动作和逐层事实拥有各自身份。复用 WES 的回调注册、可靠发送、公共结果读取与提交后唤醒。

**技术基线** Python、FastAPI、SQLAlchemy、Alembic、Celery、pytest。开发验证使用 WES 本地应用及模拟对端。

**设计依据** [概要设计 0.21](../../integrations/opening-machine-overview-design.md) · [配对算法](../../integrations/opening-machine-pairing-algorithm-design.md) · [API 对接稿](../../integrations/opening-machine-api.md)

## 全局约束

- 交付范围为 WES。WMS 下发工单、确认开工绑定、校验箱码、发起 AGV 补给及下发完成通知。ECS 提供原 Result 和设备事件，AGV/MCS 向 ECS 交接补给轮次。外部交互以模拟报文验证。
- 当前工单的 WMS 开工绑定确认是新箱供箱、缓存箱恢复和余箱取料的共同前置条件。指令 ACK 表示接纳，设备动作按匹配的原结果推进。
- 每层 2 片，任务奇数套数向上取偶。取料位、余料缓存各一箱。每线同一时点最多一个执行任务、一个未结清取料额度。选源及双余箱保护沿 0.21。
- 测试中的 `OPEN_CARTON`、`PICK_LAYER_QUOTA`、`TASK_COMPLETED`、到箱和可取箱事件名沿 API 草案。正式注册使用确认后的命令附录及报文字段。
- C07 本地采用原额度汇总 Result 唯一入账。逐层明细用于关联、冲突识别及追溯。C09 本地用技术结果确认设备接收整单通知，设备内部补空板和出料由设备跟踪。两处均是待对齐的接入方案。

## 评审重点

| 易错输入 | 预期 | 覆盖任务 |
|---|---|---|
| 新单在等开工确认，旧单 `RECORDED` 迟到 | 旧响应只归原通知，新单继续等待 | 2 |
| 可取箱先到，原取箱是否搬箱未知 | 保留到位事实，原结果明确未搬箱后才恢复 | 3 |
| 同一补给轮次多次回报或错产品、错工位 | 匹配轮次只登记一条新指令，其他回报保持原归属 | 3 |
| PUT 先到、同层冲突或额度 Result 重发 | 稳定配对；冲突暂停；成功、NG、余量只入账一次 | 4 |
| 同产品双余箱续单，或换产品时仍缺清线确认 | 双位有锁死风险时待腾位；换产品等待确认 | 1、2 |

## 1 任务状态机与推进门槛

状态名为本计划拟定的 WES 内部枚举。动作等待独立记录在动作行或补给等待行，`RUNNING` 的工单可处于待箱码、待开箱、待取料位、额度执行、待补给或待余箱移动。冲突保留原阶段及原因，由推进函数检查 `hold_reason`。

| 工单状态 | 进入条件 | 后继 |
|---|---|---|
| `QUEUED` | 原下发通知与工单同事务保存 | 本线取得唯一执行资格且交接允许后，登记开工通知，进入 `WAIT_START_BINDING`。换产品待清线、双余箱待腾位时保留原任务 |
| `WAIT_START_BINDING` | 开工通知已登记 | 原 `task.started` 的有效 `RECORDED` 或 `DUPLICATE` 证明当前绑定已保存，进入 `RUNNING`。未知响应保持原通知 |
| `RUNNING` | 当前工单已确认开工绑定 | 按下表推进；双侧目标已达且额度、在途层和相关来源动作均已结清，冻结供料汇总并进入 `WAIT_COMPLETION_NOTICE` |
| `WAIT_COMPLETION_NOTICE` | 双侧供料结清、汇总意图已保存 | 应用匹配的 WMS 整单通知，登记一次转发，进入 `WAIT_COMPLETION_FORWARD`。早到的原通知保存后按结清条件复核 |
| `WAIT_COMPLETION_FORWARD` | 原转发意图已保存 | 按 C09 对齐的技术结清条件进入 `COMPLETED`，释放本线执行资格。余箱位置及余量供交接继续使用 |
| `COMPLETED` | 原通知转发技术义务结清 | 重复回报和历史结果按原归属处理 |

| `RUNNING` 子流程 | 权威输入与下一步 |
|---|---|
| 选源与取箱 | 先处理可复用的当前箱或缓存箱。需新箱时按成功量选侧。ARM01 原成功 Result 更新上料占位，明确缺箱且未搬箱则保留原意图等待补给 |
| 开箱与到位 | `SCAN1` 匹配原取箱，WMS 校验通过且箱号、产品、盖型及数量匹配后登记开箱指令。核对 ARM03 实际到箱及原开箱指令关联后才授权额度。开箱 Result 字段按 C03 对齐 |
| 补给恢复 | ECS 可取箱可先于缺箱 Result 到达。核对工作线、工单、产品、盖型、工位、原取箱指令及补给轮次，原 Result 明确未搬箱后，原子消费轮次并登记一条新取箱 |
| 额度与 NG | 额度为箱剩余层数和本侧成功量缺口的较小值。原 Result 的实际取出、成功、NG、未结清和箱余量按冻结额度校验。未结清层或矛盾事实保持待核对。已结清失败层仅按剩余成功量缺口补取 |
| 清箱和缓存 | 取尽按设备确认离位后释放占位。余箱移动按原 Result 更新位置，核对单缓存和来源交接条件后继续 |

## 2 数据模型和事务

以下五张表为拟定的最小业务模型，表名及迁移按实施分支确认。框架已有的收据、出站消息、命令和结果证据表继续作为协议记录。

| 模型 | 持久化内容 | 唯一性及归属 |
|---|---|---|
| `OpeningTask` / `opening_task` | WMS `task_id`、工作线、产品、工单版本、原套数、执行层数、双侧取出/成功/NG 计数、阶段、阻塞原因、原开工和汇总动作引用、原整单通知引用 | `task_id` 唯一。同一工作线只有一个占用执行资格的任务。用部分唯一约束保护执行阶段。原输入与任务重复下发比较，异内容保留冲突 |
| `OpeningCarton` / `opening_carton` | 原取箱动作、外部箱号、产品/盖型/料号、所在工作线及位置、入线数量、结算余量、当前任务归属 | 原取箱动作唯一。取料位和缓存位置各有一个有效占位。箱号为业务标识，重新入线另建来源记录。跨任务复用更新当前归属，历史消耗归原额度动作 |
| `OpeningAction` / `opening_action` | 工单、动作种类、唯一业务 `action_key`、原箱及阶段、WMS 消息引用或 ECS 原命令引用、授权层数和下发时余量快照、结算摘要、处理标志 | `(task, action_key)` 唯一；外部引用在各自类型内唯一。每工单最多一个未结清来源动作，串行额度独立受同一执行资格保护。额度与原动作结算后供汇总使用 |
| `OpeningLayer` / `opening_layer` | 原额度动作、层序、GET/PUT 各自原事件引用及规范化事实、终局及冲突状态 | `(quota_action, layer_seq)` 唯一，同一层分别保存 GET 和 PUT。迟到明细只核对原额度；相反终局留证并暂停尚待执行的推进 |
| `OpeningRefill` / `opening_refill` | 原取箱动作、可取箱回报、原工单/工作线/产品/盖型/工位、`replenishment_ref`、消费后新取箱动作引用 | 原缺箱取箱动作及匹配轮次只允许一次恢复。对提前回报先保存候选事实；同一原指令出现不同轮次或归属冲突时留待核对。主动补给事件不创建取箱意图 |

产品切换使用当前线已持久化的前单产品及占位记录。清线确认必须有可信来源并匹配原线和目标工单，接口来源按 C10 对齐；本地用例注入已确认的交接事实。任务汇总在发送前冻结成功、NG、箱耗用及留箱明细，后续续单不改写原汇总。

回调在接收事务内保存原事实与待推进记录。`scan_pending` 使用 `Capabilities.session_factory` 开独立事务，先设 `REPEATABLE READ`，再按 `WorkLine → Devices（按 ID）→ 业务动作` 加锁并复核当前资格。原结果仅从公开 reader 读取。消费身份、状态、计数和下一动作意图同事务提交。`40001` 使用新事务有界重试，耗尽保留待办。由现有提交后通知及 5 秒 Beat 调用同一业务推进入口。

## 3 接口注册与文件归属

正式包使用 `definition.py` 声明业务代码 `opening_machine`、设备角色和工位槽，`composition.py` 声明 `ENTRY_MODULES`、`MODEL_MODULES`、`COMMANDS` 和 `PROGRESSION = "scan_pending"`。工单下发是初始入口。使用开箱机专属逻辑角色区分 `ARM01`、`ARM02`、`ARM03`、`STATION_OUTPUT01`、SCAN1 至 SCAN3、A/B 和 CCD，实际编码通过工作线绑定。这样同名事件或动作可以按角色区分。

| 方向 | 拟注册合同 | 归属 |
|---|---|---|
| WMS → WES `/api/v1/wms/events` | `opening_machine.task.issued@v1`、`opening_machine.task.completed@v1` | `callback/opening_machine_task_issued_v1.py`、`callback/opening_machine_task_completed_v1.py` |
| WES → WMS `/api/v1/wes/decisions` | `opening_machine.carton.validate@v1` | `request/opening_machine_carton_validate_v1.py`。本地按同步 `DECIDED` 验证 |
| WES → WMS `/api/v1/wes/facts` | `opening_machine.task.started@v1`、`opening_machine.task.supply_report@v1` | `request/opening_machine_task_started_v1.py`、`request/opening_machine_task_supply_report_v1.py` |
| WES → ECS `/api/v1/device/command` | `PICK_AND_PUT`、`OPEN_CARTON`、`PICK_LAYER_QUOTA`、`MOVE_BACKWARD`、`MOVE_FORWARD`、`TASK_COMPLETED` | `ecs_command.py` 的严格命令模型，经 `composition.COMMANDS` 编译。新增命令及完成通知承载待对齐 |
| ECS → WES `/api/v1/callback/event` | `SCAN_COMPLETED`、`CARTON_AT_PICK_STATION`、`SOURCE_CARTON_AVAILABLE`、`GET_COMPLETED`、`PUT_COMPLETED` | `ecs_event/` 按事件名小写分文件。SCAN1 校验、SCAN2/3 追溯分别按角色声明。空箱释放按已对齐的原 Result 或回报处理 |
| ECS → WES `/api/v1/callback/result` | 原 `command_code` 及设备身份 | 复用通用结果接收与公开读取。按原动作消费，ECS 重发沿原身份 |

上述文件路径均位于 `app/businesses/opening_machine/`。业务服务 `service.py` 提供 `build_service(capabilities)` 对应的状态推进服务，建议按 `source.py` 管理供箱、补给与缓存，`settlement.py` 管理额度及计数。`models.py` 声明业务表。WES 不接收发给 WMS 的 `PALLETS_EMPTY`，补给和门禁由外部链路提供。

本地测试通过隔离业务目录组装正式形状的合同、模拟对端和测试表，避免将草案注册进部署目录。正式接入由 `catalog.entries()` 一次装配业务，API、worker 和迁移共同校验。若箱码决策最终选择异步，先确认其回调名和原请求关联，再登记对应入口。 [业务包接入](../../integrations/business-packages.md) · [接口注册](../../integrations/registration.md)

## 4 实施顺序

### 任务 1：状态和持久化骨架

**文件** `definition.py`、`composition.py`、`models.py`、`service.py`，`tests/businesses/test_opening_machine_state.py`。迁移在正式装配前基于届时唯一 Alembic head 编写。

- [ ] 先写工单重复下发、不同内容冲突、同线两个工单争夺执行资格、换产品无清线事实及双余箱占位测试。测试表使用独立 `MetaData`。
- [ ] 实现五张表的唯一约束、状态变更和工作线执行资格。验收交接先于开工，余箱记录能跨同产品工单保存。
- [ ] 用 `rtk uv run pytest tests/businesses/test_opening_machine_state.py -q` 检查同线仅一个任务获准，重复下发只形成一个工单。

### 任务 2：WMS 工单、开工通知和门控

**文件** 工单 callback、started request、`service.py`，`tests/businesses/test_opening_machine_start.py`。

- [ ] 通过真实 WMS 回调入口注入同一工单，核对首次受理和重发只创建一份开工通知。
- [ ] 按原通知读取并校验开工结果。注入超时、旧单成功回报、同身份 `RECORDED` 或 `DUPLICATE`。前两种情况下 ARM01、新额度和缓存恢复命令计数均为 0，匹配确认后才有首次授权。
- [ ] 运行 `rtk uv run pytest tests/businesses/test_opening_machine_start.py -q`。模拟发送重试保留 `operation_id`、原时间和正文。

### 任务 3：取箱、开箱与补给恢复

**文件** `source.py`、`ecs_command.py`、箱码 request、SCAN1/到箱/可取箱事件处理，`tests/businesses/test_opening_machine_source.py`。

- [ ] 从原取箱 Result 到 SCAN1 决策、开箱、ARM03 到位串通一箱。错产品/盖型、旧扫码和未知占位保持原等待。移动余箱后才更新缓存和取料位。
- [ ] 增加 548 正在执行、558 等待时的补给用例。先到可取箱，原 ARM01 尚未明确未搬箱时新命令数为 0。错指令、错产品、错工位及旧轮次保持等待。
- [ ] 匹配原缺箱 Result 后，消费原补给轮次并登记一条新取箱，重复可取箱及重复 Result 后命令数仍为 1。主动补给只保留事实。
- [ ] 运行 `rtk uv run pytest tests/businesses/test_opening_machine_source.py -q`，检查模拟入口和出站请求身份可串联。

### 任务 4：额度、逐层配对和 NG 补取

**文件** `settlement.py`、额度命令模型、GET/PUT 和 SCAN2/3 事件处理，`tests/businesses/test_opening_machine_quota.py`。

- [ ] 先按 5 套工单、上盖箱 10 片建立用例。执行目标各 6 片，第一轮上盖额度为 3 层。模拟第 2 层 `PUT_FAILED` 先于 GET 到达并重发，事件只配对，等待额度结清。
- [ ] 用原额度 Result 回报取 3 层、成功 2 层、NG 1 层、未结清 0、箱余 4 片。WES 应记录取 6 片、成功 4 片、NG 2 片，只登记 1 层补取。原 Result 和失败事件重发后计数及意图不变。
- [ ] 覆盖超额度、余量不守恒、相反 PUT 终局及未结清层。冲突保留证据，待结清前下一额度数为 0。SCAN2/3 缺码及迟到仅影响追溯。
- [ ] 运行 `rtk uv run pytest tests/businesses/test_opening_machine_quota.py -q`，逐项核对余额与外发命令。

### 任务 5：整单收口、恢复和 WES 组合验证

**文件** 汇总 request、整单完成 callback 及转发模型、`service.py`，`tests/businesses/test_opening_machine_flow.py`。

- [ ] 双侧结清后冻结一份供料汇总。提前通知只保存，当前工单可收口后生成原转发。重复通知不新增转发。用模拟技术结果确认转发阶段结束，保留余箱事实供续单。
- [ ] 按正常接收路径走完一单，包含开工等待、缺箱补给、重复回报、整层 NG 补取和整单通知。模拟原命令 Result 丢失后按同一 `command_code` 恢复。验证状态、原身份和数量均只推进一次。
- [ ] 验证提交后唤醒失败仍由 Beat 扫到持久待办，重复触发只消费一次原结果。使用一次性 PostgreSQL 验证同线领取、补给消费并发、原 Result 结算竞争及 `40001` 回滚重试。SQLite 用例用于状态和报文回归。
- [ ] 运行 `rtk uv run pytest tests/businesses/test_opening_machine_*.py -q`，再对变更路径运行 Ruff 和 `git diff --check`。记录本地用例、一次性数据库检查及仍待签认的接口项。

## 5 模拟报文与验收断言

测试夹具从 0.21 API 报文取完整外壳和字段。以 `OPEN-001 / 548`、`OPEN-LINE-01`、需求 5 套串联，另排一单 558。WMS 模拟器经出站适配器返回原请求响应，ECS 回报走通用回调。AGV/MCS 交接只作为模拟可取箱报文的来源，WES 入口直接接收 ECS 回报。

| 场景 | 报文和注入顺序 | 可观察断言 |
|---|---|---|
| 开工等待 | 下发工单后，开工通知首次超时，再注入旧 `operation_id` 的 `RECORDED`，最后返回本次原通知的 `RECORDED` 或等价 `DUPLICATE`。[开工报文](../../integrations/opening-machine-api.md#task-started) | 有效确认前是 `WAIT_START_BINDING`，供箱、余箱恢复和取料额度均为 0。确认后进入 `RUNNING`，只登记一次首动作 |
| 缺箱补给 | A 工位取空后，模拟 WMS 绑定 548 上盖并发起 `AGV-REFILL-A-001`。由 ECS 先回报 `REFILL-A-001` 和原 `source_command_code`，再回报原取箱 `FAILED / NO_AVAILABLE_CARTON`，`actual_qty=0`。[缺箱 Result](../../integrations/opening-machine-api.md#carton-unavailable) · [AGV 交接](../../integrations/opening-machine-api.md#replenishment-handoff) · [可取箱事件](../../integrations/opening-machine-api.md#source-available) | 先到的可取箱只保存。确认原动作未搬箱后，消费轮次并生成 1 条新的 548 上盖取箱。将可取箱产品改成排队的 558 时恢复数为 0 |
| 重复回报 | 原样重放上一步的 ECS 可取箱、ARM01 Result，以及额度内同层 GET、PUT 和原额度 Result。另送同层相反 PUT 终局测试冲突 | 原回报重发后新取箱、额度入账和补取意图各仍只有 1 份。同层终局冲突保留证据并暂停下一动作 |
| 整层 NG 补取 | 上盖箱 `U001` 扫码确认 10 片，先发 3 层额度。第 2 层 `PUT_FAILED` 先于同层 GET 到达，随后提交 `drawn_layers=3`、`success_layers=2`、`ng_layers=1`、`unsettled_layers=0`、`remaining_pcs=4` 的原 Result。[逐层事件](../../integrations/opening-machine-api.md#layer-events) · [额度 Result](../../integrations/opening-machine-api.md#quota-result) | Result 结清前补取数为 0。结清后该侧累计取出 6 片、成功 4 片、NG 2 片。只补取 1 层，成功后该侧达到执行目标 6 片，箱余 2 片 |

### 正式装配条件

WES 内部开发完成以本地链路和持久化检查为准。正式业务目录启用前核对 C01 开工和补给关联、C02 箱码决策、C03/C04 开箱与到箱释放、C05 至 C07 额度及入账、C09 转发技术结清、C10 清线确认来源和 C12 设备绑定及回报合同。随后在实施分支加入 `catalog.entries()`，让 API、worker 与 Alembic 加载同一份正式声明，核对唯一 head 和公共入口回归。发布、真实设备联调和现场验收分别安排。
