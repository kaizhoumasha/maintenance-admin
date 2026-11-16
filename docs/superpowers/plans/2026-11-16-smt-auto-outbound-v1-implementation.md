# SMT AUTO 出库 v1 实施计划

**目标**　让 `material.decide@v1` 只决定本盘去向，`work_plan@v1` 按原箱或架面到位发放下一份取盘授权，并统一用 `NO_WORK` 确认该次取料终局。

**设计依据**　[SMT AUTO 出库 v1 接口与时序设计](../../integrations/smt-auto-outbound-v1-design.md)。工程评审按 `develop` `3a07b11` 定位入口。实施前从当时最新 `develop` 起工作分支并复核迁移 head。

实施顺序为**贯通来源续问 → 调整平台调度与来源收口 → 演练历史清理、双端统一切换**。切换后仅运行新版出库 v1 语义，旧任务在切换前收口，已结清的出库历史可以清理。

## 工程评审结论

| 优先级 | 发现 | 本计划的处理 |
| --- | --- | --- |
| P1 | 同名 v1 的旧实例会把省略 `next_source_action` 的新版 `ACCEPT` 当作取料终局。清理扫码收据或经过数据后，旧 SCAN2 重放还可能被解释为新到位。 | 两端在停接新任务的窗口统一切换。演练按出库归属清理和旧事件隔离，确认老实例退出及现场责任已结清后再恢复。 |
| P1 | WES 的未决唯一性只约束本地。WMS 在旧 `READY` 尚未消耗时若接受另一个请求 ID，可能重复授权或错误返回 `NO_WORK`。WES 当前 RackSlot 校验主要排除显式关闭的 SLOT。 | 在 WMS 以原到位串行消耗和新发授权。WES 拦下原架面已成功取离 SLOT 的重复 `READY`，保留合法空取 `RETRY`。 |
| P1 | B 取盘已排队后，A 的放盘可能失败或未清台。只验证 ACK 和 `READY` 的先后覆盖不了这一段。 | 固定原放盘指令引用。实机注入先 ACK、B 排队、A 未清台，验收 PLC 拦截和原指令恢复。 |
| P1 | 当前目标架更换先被任务范围内的未决轮次挡住。范围放宽过大又可能放走仍有直接物理责任的架。 | 把 `REPLACE` 的例外限于当前盘所需的更换和其同来源正常续问，保留其他占用。覆盖未决、`WAIT`、未取盘 `READY` 和实际物理责任未闭合的场景。 |
| P2 | 每次 `ACCEPT` 增加一次 `work_plan`，尾盘也包含在内。只测 HTTP 响应耗时会漏掉发送排队及 WES 消费延迟。 | 比较切换前后的完整节拍，并分别记录发送排队、WMS 求值及回执到 WES 推进的时间。 |

已把评审修正写入设计和下述步骤。WMS 原到位授权的事务行为与 ECS/PLC 失败时的物理交接仍需联调取证。

## 1. WES 改动地图

| 文件与入口 | 本次责任 |
| --- | --- |
| [`models.py`](../../../app/businesses/smt_auto/models.py) 与 Alembic 迁移 | 持久化正常续问待办及已登记的轮次类别，移除出库盘模型的旧 `NEXT` 字段和约束。 |
| [`outbound_contracts.py`](../../../app/businesses/smt_auto/outbound_contracts.py)、[`outbound.py`](../../../app/businesses/smt_auto/outbound.py) 的 `advance`、`_final_material`、`_grant`、`_work_plan_pick_ready`、`_continue_exception` | `ACCEPT` 只应用本盘去向，并按来源登记正常续问。后续按 `work_plan.READY` 独立授权，覆盖异常续问和原盘恢复入口。 |
| [`work_plans.py`](../../../app/businesses/smt_auto/work_plans.py) 的 `start_bin`、`consume_bin`、`start_return_face`、`consume_return_face` | 复用原持久化求值链，增加正常续问的登记、等待、消费及终局。保留 RackSlot 原首轮授权更正入口。 |
| [`outbound.py`](../../../app/businesses/smt_auto/outbound.py) 的 `_pick_priority`、`_platform_allows_pick` 和 `advance` 中的扫码推进 | 由同次 SCAN2 到位和原设备放盘指令识别重叠窗口，三个跨来源切换沿平台可用交接。 |
| [`outbound.py`](../../../app/businesses/smt_auto/outbound.py) 的 `_subject_closed`、`_subject_no_work`、`target_demand`，以及 [`task_flow.py`](../../../app/businesses/smt_auto/task_flow.py)、[`rack_ports.py`](../../../app/businesses/smt_auto/rack_ports.py)、[`outbound_racks.py`](../../../app/businesses/smt_auto/outbound_racks.py) | 分别处理取料终局、物理交接和报告收口。把正常尾问 `NO_WORK` 接入提前放箱，保留来源架换面门槛，并让本盘目标架 `REPLACE` 可在后继求值未决时推进。 |
| [`service.py`](../../../app/businesses/smt_auto/service.py)、[`business_wakeups.py`](../../../app/business_wakeups.py) | 核对事务提交后，WMS 答复、ECS ACK/Result、扫码和到位能唤醒同一业务推进入口。Beat 兜底同一待办，验证掉通知仍可恢复。 |
| [对接合同](../../integrations/smt-auto-api.md)、[验收记录](../../testing/smt-auto.md) | 实现合入时更新出库接口、终局、放行和验收场景中的旧后继语义。记录软件与实机各自的验收结果。 |

四个 WMS 请求模型、操作名、设备指令与回调报文沿用现有合同。入库、手工线、报告身份、空取和 NG 的授权边界沿各自流程处理。

## 2. 实施步骤

### 步骤一　收敛出库合同与数据模型

- [ ] **先覆盖唯一的新语义。** 带本盘目标的 `ACCEPT` 应用本盘决定并登记来源续问。回包带 `next_source_action` 时显式判合同冲突，覆盖现有 `extra="ignore"` 可能吞掉该字段的路径。其余扩展字段沿现行解析规则处理。
- [ ] **移除出库旧后继路径。** 删除 `_next`、`_grant` 的 `NEXT` 分支和旧终局推断。随轮次、平台门槛和 `REPLACE` 的替代判断一起收敛 `AutoOutboundPlate` 的 `grant_parent_plate_id`、`next_source_action` 及对应外键、唯一性和 CHECK。正常后继用 `READY + grant_round_id`，空取仍用 `RETRY`。入库的同名字段继续服务入库。任务接收无需增加开关或模式字段。
- [ ] **用增量迁移承载新增状态。** 在 `AutoBinWork` 和 `AutoOutboundSourceMember` 保存正常续问待办，在 `AutoOutboundDecisionRound` 标记正常续问类别。先在独立库演练第 3 节的按归属清理；结构迁移校验已无仍依赖旧 `NEXT` 的出库盘，然后移除对应结构。清理操作留在切换步骤中执行。核对只有一个 Alembic head。
- [ ] **验证。** 运行 `test_outbound_contracts.py`、出库回归和入库回归。在独立 PostgreSQL 验证保留入库、手工及共享数据时仍能完成出库结构升级。

### 步骤二　贯通本盘决定与来源级 `work_plan`

- [ ] **先覆盖正常续问与恢复。** 参数化 BIN/RackSlot。`ACCEPT` 一次提交持久化去向和来源待办，目标架未就绪时仍有续问。模拟回滚、提交后进程退出、重复唤醒、有效 `WAIT`、HTTP 超时和 `503`。验证 WMS 请求正文保持原来源和到位时间，每次求值最多一笔未决，`READY` 最多生成一个取盘。
- [ ] **消费 `ACCEPT` 后登记来源待办。** 在 `Outbound.advance` 首次应用最终决定的分支保存本盘目标，并在同事务为原来源标记正常续问。`_final_material` 和 `AutoRackPorts` 的目标授权读者共用同一校验规则。`REJECT`、`WAIT`、扫码更正和已下发放盘继续按原准入恢复。
- [ ] **在既有来源推进回收待办。** 允许 `start_bin`、`start_return_face` 在原任务、到位、配置、取消或撤回门槛满足时消费正常续问待办。可在 `ACCEPT` 的同一推进事务立即登记；若原来源暂不可执行，留下可扫描的持久化待办。原来源已终局或停止后，迟到事实仅收口原义务。清除待办、登记原 `WORK_PLAN` 轮次和 outbox 同事务。放盘依据本盘目标推进，读取或等待 WMS 下一盘决定不作为放盘条件。
- [ ] **复用原轮次及公开结果。** 同一到位沿现有 `stage_key`、`round_no`、`previous_round_id` 和未决唯一性约束。正常续问 `WAIT` 后的新轮次继承该类别，`retry_after_ms` 到期仅换信封身份。`READY` 产生关联本轮 `grant_round_id` 的独立取盘授权。调整 `_work_plan_pick_ready`，以经原来源准入的正常轮次作为授权根，继续严格核对其后续 `WAIT` 链。`_continue_exception` 以同来源未决轮次和未执行授权防重，保留原 NG 报告和空取收口门槛。RackSlot 核对原 `preparation_action_id` 及 `arrived_at`，拦下原架面已成功取离 SLOT 的重复 `READY`，保留原首轮尺寸更正路径和合法空取 `RETRY`。BIN 同 Cell 的后继按自己的完整参数执行。
- [ ] **保持现有事务和唤醒。** 在 `AutoS1Transactions` 的生产推进入口使用现有 `SERIALIZABLE` 和锁顺序。`40001` 含提交阶段，最多三次新会话整事务重试。原结果读取通过 `WmsRequests.get_result` 和 ECS 公共读者。检查 `BIN`、`OUTBOUND_FACE` 待办覆盖暂不可执行的续问、到期续问、放行和架面收口。先复用提交后的通用通知与 Beat，实测 `work_plan` 响应到放行或取盘的延迟，若有排队再定位现有推进入口。
- [ ] **验证。** 在 `test_outbound.py`、`test_outbound_contracts.py` 覆盖两类来源的正常、异常、重复 SLOT 和迟到结果。在 PostgreSQL 并发运行定向 `ACCEPT` 和周期推进，核对最终只有一条来源续问及一份有效授权。在 worker 测试覆盖提交后唤醒和丢通知恢复。

### 步骤三　恢复同箱双臂重叠，实现终局与物理交接

- [ ] **把重叠条件改成设备证据。** `_pick_priority` 从取盘授权的 `work_plan` 轮次识别正常同箱续取，保留已就绪候选的优先顺序。`_platform_allows_pick` 只在候选属同一次 SCAN2、有效正常续取且唯一未释放的平台占用对应原正常放盘时，允许凭已提交 ECS ACK 登记一次取盘。登记取盘时在设备执行快照保存该放盘的原 `call_key`、`command_code` 和设备身份，以便重启后核对。下一盘扫码可先保存，`material.decide` 等关联的原放盘成功结果。该引用用于平台交接，后继物料授权仍来自 `work_plan`。B 已排队而 A 未清台时，PLC 仍拦截 B 的平台放入，WES 保留两条原指令状态。其余来源走平台正常可用门槛。
- [ ] **给三种收口分别接入 `NO_WORK`。** `_subject_no_work` 严格核对原操作、消息、来源到位和已应用决定，`_subject_closed` 用该证据判取料终局。正常箱尾 `NO_WORK` 且本箱所有来源取盘与异常已结清时，`TaskFlow.start_release` 立即登记原 SCAN2 唯一放行，当前盘放置及正常报告继续独立推进。首问和异常续问 `NO_WORK` 沿原放行条件。架面在 `NO_WORK` 及直接物理责任可交出后可换面或退架，报告进一步参与架面与任务闭合。核对 `TaskFlow.original_inprocess` 和 [`RollerPorts.accept_scan2`](../../../app/businesses/smt_auto/roller_ports.py)，同码再次到位也需符合原放行证据与新到位准入。切换两条链路时，新来源的到位、首问和就绪可以先完成，平台接续只等待自身设备条件和平台可用。
- [ ] **解除当前目标架换架的循环等待。** 现有 `target_demand` 会因未决 `WORK_PLAN` 返回 `CONTINUE`，旧 `REPLACE` 特例又沿 `grant_parent_plate_id` 识别后继。按已验证的当前 `REPLACE`、原到位的当前正常续问轮次及目标架直接物理责任判断。仅豁免这次同来源后继的未决、`WAIT` 或尚未开始取盘的授权。旧架已有明确工作、未结束放盘或未知物理责任继续占用，其他来源也留下独立判据。核对 `rack_ports.py` 中覆盖度、目标授权和释放状态的调用路径，以及 `outbound_racks.py` 的同架换面先决条件。
- [ ] **验证。** 在 `test_outbound.py` 和现有 TaskFlow 相关测试中执行下表全部场景。用真实服务入口消费 WMS 决定和 ECS 事实，断言原命令的数量、次序和身份。覆盖迟到的旧 ACK/Result、同码新到位以及没有通知时的周期恢复。

## 3. WMS 配合和启用条件

| WMS 需要提供 | 通过条件 |
| --- | --- |
| 同一 `@v1` 统一切换 | 按共同窗口停止下发和接纳新自动出库任务，旧在制收口后两端切换。恢复接单后的四条操作统一执行新版合同。 |
| 提交本盘去向及来源进度 | 两条 `decide` 的 `ACCEPT` 仅返回本盘目标和必要目标准备。模拟下一盘计算变慢，本盘决定仍可先提交并返回。紧接的 `work_plan` 能看到同次到位的已提交进度。 |
| 按原到位管理 `READY / WAIT / NO_WORK` | 同次到位串行核对授权状态。本盘 `ACCEPT` 要消费唯一有效授权并更新已取出进度。`READY` 与单次授权原子保存，`NO_WORK` 只封闭原箱或原架面，原请求重放返回原结果。另一个请求 ID 在原授权未消耗时只能等待或明确冲突。同轮已取离的 SLOT 或已关闭的 Cell、SLOT 不再次授权。 |
| 同一 Cell 取栈顶及同源同码 | 能安全判断下一层时，在前盘移库报告入账前返回该层完整参数。否则返回 `WAIT`。同源同码的下一份本盘去向，在前盘报告未入账时按现有规则等待，原报告去重和库存入账规则继续生效。 |

### 切换与清理演练

- [ ] **暂停接单并核对旧在制。** 冻结 WMS 下发和 WES 接纳的 SMT AUTO 出库新任务。逐工作线核对旧任务、未消费授权、可靠发件箱、ECS 指令、货架搬运和库存报告。用原结果或现场对账结清真实占位、未确认报告及未知交付，再暂停会写入本次清理范围的进程。
- [ ] **按归属预览并清理。** 先用出库 `task_id` 和原拥有者生成清单及预计行数。沿 `sorting_task`、AUTO 子表、滚筒作业、RackDispatch、WMS/ECS/Transport 往来和收据引用核对依赖。在独立库演练删除顺序与引用完整性。共享表仅处理明确归属且已结清的数据，旧扫码收据的去重依据保留至重放窗口结束，或用已验证的旧事件隔离替代。向任一方重放旧 SCAN2、原决定、命令回报和计划，确认它们不会产生新作业。
- [ ] **两端一起恢复。** 部署同语义的 WMS 与 WES，核对旧实例已退出。空线验证首问、正常尾问 `NO_WORK`、末盘放行与旧回报隔离，再重新接单。恢复前可按共同切换快照成对回退，恢复后先暂停新任务并收口或对账已产生的新版物理事实，优先修复当前版本。不得用清理操作替代已发生的物理或库存事实核对。

## 4. 联调验收矩阵

| 场景 | 输入顺序与故障注入 | 通过条件与应留证据 |
| --- | --- | --- |
| **料箱 → 退料架面** | 架面真实到位且首问 `READY`，料箱末盘 `ACCEPT` 后尾问超时或 `503`，末盘报告暂缓。 | 末盘放置成功后，已就绪架面取得平台并取盘。原箱继续占 SCAN2，尾问恢复后才按 `NO_WORK` 放行。保存架面到位和首问、末盘 PUT 结果、架面 PICK 登记与执行时间。 |
| **退料架面 → 料箱** | 架面末盘放置期间，料箱真实到达 SCAN2 并获首盘 `READY`。架面尾问分别返回 `WAIT` 和超时。 | 末盘放置成功、平台可用后，料箱接续。原架在 `NO_WORK` 之前保留，之后按直接物理交接条件换面或退架，新面到位后才发首问。保存首盘取盘、架面 `NO_WORK` 和原换面动作及结果。 |
| **料箱 A → 料箱 B** | A 的 `NO_WORK` 在其 PUT 成功前生效。分别延迟 A 报告确认和 A 放行后的 B 真到位。另测 A 尾问比 PUT 慢，退料架面已可用。 | A 放行后，B 真到位才登记首问。B 的 `READY` 与平台可用均满足才取盘。A 报告未收妥可继续 B。A 的放行 ACK、旧回调及同码旧到位均不触发 B 首问。A 尾问慢时 B 留在上游，退料架面仍可接用空闲平台。 |
| **BIN 同箱双臂重叠** | 对调后继 `READY` 与当前盘正常 PUT ACK 的到达顺序，注入重复 ACK、调度并发、重启。覆盖 PUT 成功先于 ACK、以及 ACK 后 B 已排队但 A 未能清台。 | 重叠路径在两项齐备后只登记一次同箱取盘。已经取得原成功结果时走平台已可用路径。A 未清台时 PLC 拦截 B 的平台放入，A、B 原指令保留且不生成替代指令。B 可先扫码，下一份 `decide` 只在原 PUT 成功后发出。同工位新箱没有提前取盘资格。 |
| **BIN 末盘提前放箱** | A 原取盘成功和扫码均已应用，正常续问 `NO_WORK` 到达，A 的 PUT 尚未完成。分别注入 `WAIT`、缺失响应、未结清来源异常，以及重复 `NO_WORK` 唤醒。 | 只有有效正常尾问 `NO_WORK` 且来源侧已结清时，原 SCAN2 只登记一次 `MOVE_FORWARD`，早于 A 的 PUT 成功和报告确认。区分放行登记、ECS 接纳和实际离位。首问与异常 `NO_WORK` 继续满足原放行门槛。 |
| **RackSlot 同面续取与目标架更换** | 同面下一 `READY` 先于当前 PUT 完成，另返回一次已成功取离的 SLOT。对本盘 `REPLACE` 分别注入续问超时、`WAIT` 和 `READY` 未执行，并插入旧目标架仍有在途放盘的负例。 | 同面有效授权等机械臂及平台可用再取，重复 SLOT 无取盘指令。目标架责任可交出时，本盘的换架与放置可以推进，物理责任未明时保持原占用。 |
| **统一切换后重放** | 带历史出库和其他业务数据演练清理。在新任务首问前后重放旧 SCAN2、旧回报与 WMS 原消息。 | 旧消息无新取盘、放盘、放行或退架，其他业务数据和作业继续保持，真实的新到位可以按新版首问。 |

### 验证顺序和现场记录

1. **软件回归。** 复用 `test_outbound.py` 的双臂、SCAN2、退料架面和 `REPLACE` 用例及现有夹具，新增上表缺口。实施后运行以下命令。并发、迁移及 worker 用例使用明确配置的独立 PostgreSQL 和 Redis，确认相关用例实际运行。 [仓库测试环境说明](../../testing/test-lanes.md)

   ```shell
   rtk uv run pytest tests/businesses/smt_auto tests/businesses/test_component_wms_wakeups.py tests/tasks/test_smt_auto_worker.py
   rtk uv run alembic heads
   rtk uv run ruff check app/businesses/smt_auto tests/businesses/smt_auto
   rtk uv run ruff format --check app/businesses/smt_auto tests/businesses/smt_auto
   ```

2. **WMS 联调。** 验证提交可见性、原 `READY` 响应丢失后同 ID 重放、另一 ID 并发求值、同源进度和库存报告去重。记录每笔的任务、来源到位时间、`operation_id`、响应及生效时点。先确认 WMS 能否在报告入账前安全授权同 Cell 下一层，再判断可利用的重叠窗口。
3. **ECS/PLC 实机。** 在同一工作线逐项执行矩阵。保存原取盘和放盘指令号及 ACK/Result、扫码和到位事实、SCAN2 放行和实际离位。和现有链路对照三种切换的「平台可用 → 下一来源开始取盘」、同箱「`READY` 和放盘 ACK 齐备 → 下一取盘登记」，以及「末盘 `ACCEPT` 生效 → `NO_WORK` 生效 → 放行登记 → B 到位」。在持续出库负载下分别记录发送排队、WMS 求值、WES 收到响应后的推进时间及物理动作耗时，验证正常响应由提交后唤醒推进，漏通知由 Beat 恢复。目标节拍由联调双方根据基线确定。
