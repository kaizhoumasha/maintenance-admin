# 开箱机上下盖配对调度算法设计

本文描述单一产品 MVP 的选源、额度、结算及工单交接。开箱机工作线只受理约定产品的工单，支持该产品连续下单。每层 2 片，取料位与余箱缓存各容一箱。 [API 草案](opening-machine-api.md) · [概要设计](opening-machine-overview-design.md)

## 1. 算法目标与输入

WES 继续处理当前箱，直至取空或本侧达标。需要新箱时，优先选择成功供料量较少的一侧。选源同时遵守取料额度、工位占位和单缓存限制。

选源输入包括工单、WMS 确认的箱资料、当前来源占位、已结算数量和原指令状态。Buffer 的备板及 Magazine 周转在设备内处理。

当前正常容量示例为上盖箱 22 片、下盖箱 16 片，每箱实际数量可以更少。纸箱外码经 ECS、WES 转交 WMS，由 WMS 解析有效外码并返回标示数量。设备内余箱从缓存恢复时，沿用原箱号和已结算的余量。核实后的多料同样留箱，箱量差异按原报告由 WMS 独立调整库存。

配对按已成功放到载板的实物片数计算。实际到线顺序由 Buffer、搬运和下游共同协调。

| 项目 | MVP 约定 |
| --- | --- |
| 选源 | 串行供箱，一份未结清额度。到箱只登记本箱额度，结算后按实际成功量和占位选源。业务侧的盖型 `cover_type` 为 `UPPER` 或 `LOWER` |
| 逐层执行 | 设备维持单层在途，同层取料和备板可并行。当前层物理结清后继续，GET/PUT 由 ECS 上报 WES 供追溯 |
| 结算与交接 | 每条额度一份最终 Result，按原指令号入账一次。同箱补取使用新额度。逐箱耗用留在 WES，供料结果只向 WMS 报工单汇总和箱量差异。ECS 确认可接下一单后交接 |

## 2. 状态和数量单位

业务数量以片计，伪代码内部统一用层。转换在输入和输出边界进行，`1 层 = 2 片`。

| 状态 | 内容 |
|---|---|
| task | 原工单、产品、领取时生效的版本、原需求套数及向上取偶的执行目标 |
| target[side] | 各侧目标层数，例如 2501 套对应 1251 层 |
| good[side] | 已结算成功层数 |
| drawn[side] | 已结算实际取出层数 |
| ng[side] | 已结算、已确认投料 NG 层数 |
| active | 取料位纸箱，包含箱号、产品、盖型和剩余层数；无箱为 None |
| cache | 唯一缓存箱，结构与 active 相同；无箱为 None |
| carton records | 用入线记录 ID 保存原箱、实物余量、可取余量及待恢复原因。箱离位后仍可按原额度找到该记录 |
| start_operation_id / start_binding_confirmed | 原开工通知身份，以及 WMS 对当前工单保存执行绑定的确认 |
| pending | 原供箱、校验、取料、离位或缓存流程尚未结清。供箱流程包含缺箱等待补给，阻止新来源动作 |
| carton_consumption | 按工单与原入线箱归集已结清的取出、成功和 NG 层数 |
| supply_complete_reported / completion_notice | 两侧供料汇总是否已登记，以及 WMS 整单完成通知与原转发身份 |

WES 保留当前额度直到结清。最终 Result 统一更新业务数量，GET/PUT 用于原额度和层序追溯。

已结清状态满足：

```text
0 <= good[side] <= target[side]
drawn[side] = good[side] + ng[side]
remaining_draw_allowance[side] = target[side] + ng[side] - drawn[side]
                             = target[side] - good[side]
```

物理动作进行中还包含未结清层，此时完整物理账为 `实际取出 = 成功 + 已确认 NG + 未结清`。额度授权数不等于实际取出，更不等于成功量。

## 3. 来源选择顺序

WES 在领取前核对续单余量下限和原箱状态，取得本线执行资格后，在同一事务登记开工及原通知。WMS 核对工单版本和取偶后的执行目标并确认原绑定已保存后，才允许供箱、恢复缓存箱或取料。交付未知沿原通知恢复，明确冲突保留待核对，历史确认归原任务。 [开工通知](opening-machine-api.md#task-started)

1. 原指令或扫码决策未结清，等待原处理，保持来源不变。
2. 当前箱已空时，设备自主清箱，并随原额度最终 Result 确认实际离位。若 ARM02 已有按原授权开好的箱，ECS/PLC 在取料位释放后自动送箱。
3. 两侧成功量均达标，且额度、在途层和相关来源动作均已结清后，WES 向 WMS 汇总供料结果。已确认可用的余箱可留在原位，供该产品下一单复用。末箱取空时，消费确认实际离位的原额度最终 Result 后再汇总。核实后的多料记为可用余料，差异独立上报。
4. 当前箱仍有料且本侧未完成，继续当前箱。投料 NG 引起的不足也优先从当前箱补取。
5. 当前箱仍有料但本侧完成，另一侧未完成且缓存为空时，将本箱移入缓存。
6. 取料位空、缓存箱属于本任务且其侧尚未完成时，先恢复缓存箱。
7. 无需恢复缓存时，在未完成两侧中选 `good` 较小者；相同则先上盖。ARM01 只按该选择取一箱，不提前批量开箱。

缓存中已有本侧已完成的余箱时，可以继续处理另一侧。另一侧中途取空箱就移走空箱、继续新箱；另一侧最终完成时允许将其余箱留在取料位。此时两侧都无需取料，可在完成交接后将余箱留给该产品的下一单。

续单时两个位置均有箱，仍按上面的顺序处理。只有本侧达标后取料位仍有余料、缓存已占用且另一侧还需供料，取料和缓存移动都无法继续时，才进入通用实物异常流程。

### 3.1 单缓存位尾数保护

默认算法不会中途缓存未完成侧的箱子。设备恢复或后续优化策略可能带来这类状态，仍必须保护：

> 缓存中有本任务尚需取料的箱子时，取料位一旦可用，就先恢复该箱。不要为了追平配对差值先取另一侧新箱。

例如需求 4 套，缓存有上盖余箱，上盖只成功 2 片。若新开一个 16 片下盖箱，只取 4 片后下盖达标，取料位还剩 12 片。该箱无法进入已占用的缓存，上盖缓存箱也回不来，形成来源死锁。

此前关闭保护的推演已经出现该反例，因此本稿不启用中途缓存追平策略。若将来引入该策略，另一侧本次处理必须能保证取空，而不是只保证它暂时落后。数量未知时可用经确认的单箱最大量做保守判断；最大量不可靠时不能绕过先恢复规则。

### 3.2 跨任务复用

MVP 只受理工作线约定产品的工单。该产品续单时，核对原箱号、盖型、位置和已结算可用余量，新任务从零累计成功量和 NG。核实后的多料计入该位置的可用余量。

领取前按盖型分别合计取料位和缓存位的已确认可用余量。有效需求套数至少达到两侧余量片数中的较大值。例如下盖余 10 片、上盖余 16 片时，下一单至少 16 套。较小的队首工单继续保持未选中，等待 WMS 改量或撤销。 [续单余量下限](opening-machine-api.md#continuation-quantity)

## 4. Python 伪代码：WES 选源与额度

下列 `State`、`Carton` 由上节的状态组成。跨任务场景先按 3.2 完成交接，再调用选源。`pending` 包括已持久化但尚未发送的命令，避免两个并发调度同时选源。ARM03 到箱后，在同一推进事务只登记本箱额度，提交后发送。最终 Result 入账且占位条件满足后再选源。 [API 对接规则](opening-machine-api.md#next-carton-planning)

```python
UPPER, LOWER = "UPPER", "LOWER"
SIDES = (UPPER, LOWER)


def target_layers(requested_sets):
    if type(requested_sets) is not int or requested_sets <= 0:
        raise ValueError("需求必须是正整数")
    return (requested_sets + 1) // 2


def need(s, side):
    return s.target[side] - s.good[side]


def quota_layers(s, box):
    # 调用时原额度已结清，drawn/good/ng 都是本侧累计层数。
    side = box.side
    allowance = s.target[side] + s.ng[side] - s.drawn[side]
    assert allowance == need(s, side)
    return min(box.remaining, box.usable_remaining, allowance)


def on_carton_arrived(s, arrival):
    with locked_task_transaction(s.task_id):
        save_original_carton_arrival(s, arrival)
        advance_source(s)


def on_source_resolution(s, evidence):
    with locked_task_transaction(s.task_id):
        if apply_matching_source_resolution(s, evidence):
            advance_source(s)


def advance_source(s):
    apply_saved_eligible_arrival(s)
    apply_source_decision(s, choose_source(s))


def choose_source(s):
    if not s.start_binding_confirmed:
        return ("WAIT_WMS_START_BINDING",)

    if s.pending:
        return ("WAIT_ORIGINAL",)

    if all(need(s, side) == 0 for side in SIDES):
        return ("SUPPLY_DONE",) if supply_is_complete(s) else ("WAIT_ORIGINAL",)

    if s.active is not None:
        box = s.active
        if box.hold_reason:
            return (box.hold_reason, box.id)
        if need(s, box.side) > 0:
            return ("RUN_QUOTA", box.id, quota_layers(s, box))
        if s.cache is None:
            return ("PARK_AND_CONFIRM", box.id)
        # 已结算事实确认当前箱无法取料或移入缓存，另一侧仍需供料。
        return ("HOLD_PHYSICAL_EXCEPTION",)

    if s.cache is not None and need(s, s.cache.side) > 0 and s.cache.hold_reason:
        return (s.cache.hold_reason, s.cache.id)
    if s.cache is not None and need(s, s.cache.side) > 0:
        return ("RESTORE_AND_CONFIRM", s.cache.id)
    if not source_path_is_available(s):
        return ("WAIT_SOURCE_PATH",)
    unfinished = [side for side in SIDES if need(s, side) > 0]
    side = min(unfinished, key=lambda side: s.good[side])  # 平局先上盖。
    return ("FETCH_ONE_CARTON", side)
```

ARM03 `BIN_ARRIVED.data.source_command_code` 等于原 ARM01 取箱指令的顶层 `command_code`，由 ARM02 的 `OPEN_CARTON.params.source_command_code` 随箱传递。`apply_saved_eligible_arrival` 核对该引用、取料位 `STATION_CCD`、当前工单及开箱授权上的箱号和盖型。前一额度已结清、原占位已释放且新箱到位匹配后，只绑定一次 `active` 并关闭本次供箱等待。到箱先送达时按原动作保存。取空时按原额度最终 Result 结算，并同事务释放对应的空箱占位。 [到位报文](opening-machine-api.md#ecs-carton-ready-example)

`apply_matching_source_resolution` 按原异常及箱身份应用一次有效证据。ECS 确认 ARM03 已恢复且原箱仍在位时解除取料恢复等待。多料在应用原额度最终 Result 时计入 `usable_remaining`，差异报告的收妥回执只更新消息交付状态。`apply_source_decision` 在同一事务登记可执行动作，`RUN_QUOTA` 只登记本箱额度。`HOLD_PHYSICAL_EXCEPTION` 调用 [通用实物异常流程](opening-machine-api.md#quota-result)，保留原占位，核清且取料路径恢复后重新选源。

动作的状态变化由物理结果驱动：

| 动作 | 发出前 | 成功结果到达后 |
|---|---|---|
| FETCH_ONE_CARTON | 从 WMS 工单取得产品。`UPPER` 选 A，`LOWER` 选 B。冻结原命令及盖型，按 [ARM01 协议](opening-machine-api.md#fetch-carton) 组装下发，设置 pending | ARM01 到位后扫码，WES 校验通过后下发本箱开箱指令。ARM03 新箱到位回报的盖型及关联身份核对通过后绑定 active。工位无可用箱则等待补给 |
| RUN_QUOTA | 固定箱号、最大取层数及箱余量快照，设置 pending | 核对额度结算，更新成功/NG/实际取出与箱余量，取空时按原结果释放原箱占位，清除原 pending，按实际数量与占位重新选源 |
| PARK_AND_CONFIRM | 缓存为空且当前层已结清，保留两个位置的预留 | 原 MOVE_BACKWARD 完成并核对箱号后，cache=active，active=None |
| RESTORE_AND_CONFIRM | 取料位可用、缓存箱身份明确 | 原 MOVE_FORWARD 完成后，active=cache，cache=None |

设备命令 ACK 后，WES 保留原占位和待结清状态，持续等待原 Result。ECS 在断线恢复或重启后按原 `command_code` 重发已保存的最终 Result，WES 对原指令结算一次，再更新箱体位置或取料额度。若设备尚未确认执行结果，先核对实际动作和占位，再按原指令结清。

WES 在内部关联 SCAN1 的 `scan_ref` 和发给 WMS 的原校验 `operation_id`。WMS 按工单号查期望产品，并用外码解析实际产品、盖型和数量。取箱来源由 WES 从原 ARM01 指令读取。校验响应回显原 `operation_id`，匹配当前待开箱纸箱及有效扫码后，WES 才登记开箱指令，并保存其与原扫码和校验决定的关联。

ARM03 按设备互锁清走空箱。取料位空出时，ARM02 如有按原授权开好的箱，设备自动送箱。ARM01 仍串行供箱，WES 匹配原额度最终 Result 和新箱到位再授权取料。

### 4.1 缺箱后的补给恢复

ARM01 明确缺箱且未搬箱后，WES 主动向 WMS 求方案。ECS 取空事件先由 WES 接收并转报 WMS，保留原开工绑定。WMS 核对已收妥的取空事实及设备侧实际清位证据，证据不足时返回 WAIT 和重试时间。WES 核对证据归属的工位和原板或入场轮次，经公共 RackDispatch 释放匹配的旧作业并只登记一次新 ENTER。公共 Transport 源点传原栈板号，WMS 定位库区。AGV/MCS 与 ECS 确认可取箱后，WMS 回原 f0 成功。C/D、E/F 工位事件由 WES 转报 WMS，独立于配对。 [补板报文与跨单证据](opening-machine-api.md#replenishment-handoff)

```python
def resume_after_replenishment(s, waiting, enter_result):
    if not s.start_binding_confirmed or waiting is None:
        return "WAIT"
    if waiting.fetch_already_registered:
        return "DUPLICATE"
    if not waiting.original_result_confirms_no_move:
        return "WAIT"
    if not matches_applied_enter_and_source_ready(s, waiting, enter_result):
        return "WAIT"
    consume_wait_and_register_fetch(s, waiting)
    return "FETCH_REGISTERED"
```

`matches_applied_enter_and_source_ready` 核对当前工单及工位的原公共作业、已应用的 ENTER 结果、`transport_task_id`、栈板及目标点位，并要求现场可取箱条件成立。提交接纳只确认搬运已接纳，恢复取箱仍需原入场结果。消费原等待和登记一条新取箱在同一事务完成。

## 5. Python 伪代码：额度结算与补取

设备按 WES 下发的最大取层数限制实际 GET。每取出一层占用一层额度，达到额度后停止下一次取料并保留箱内余料。额度内既有成功层，也可能有 `PUT_COMPLETED` 报告 `PUT_FAILED` 的整层 NG。每条 `PICK_LAYER_QUOTA` 对应一份最终 Result，WES 按原 `command_code` 入账一次，同箱补取在结算后使用新 `command_code`。逐层事件按各自的额度与层序追溯。 [同箱补取示例](opening-machine-api.md#ecs-quota-supplement-example)

下列逻辑字段可先用于本地模拟，正式报文按开发契约对齐。`validate_original` 核对任务、产品、盖型、箱号、原 `command_code`、授权额度和下发时的箱余量。原结果重复到达时沿用已处理状态。同身份内容冲突时暂停并核对。

`box_empty_confirmed`、`remainder_verified` 和 `failed_attempt_confirmed_zero` 表示已核对的设备事实。数量未知时设备侧核实，WES 保留原额度及箱位。取空时原最终 Result 以 `carton_departed` 确认原箱实际离位。数量与离位字段不符时保留原动作和占位待核对。单独出现的 GET/PUT 冲突留作追溯，正常结算以合法最终 Result 为准。

```python
def settle_quota(s, original, result):
    # 伪代码原语：事务锁、原身份去重、校验、入账及 pending 释放必须原子完成。
    with locked_task_transaction(s.task_id):
        if original_result_already_applied(original.id, result):
            return  # 同身份异内容由该原语报告冲突。

        validate_original(s, original, result)
        if (
            result.unsettled != 0
            or result.box_remaining is None
            or result.pick_count_unknown
            or type(result.carton_departed) is not bool
            or result.carton_departed != (result.box_remaining == 0)
            or (result.stop_reason == "PICK_FAILED" and not result.failed_attempt_confirmed_zero)
        ):
            retain_pending_and_request_original_settlement(original, result)
            return

        require_nonnegative_integer_layers(result)
        assert result.drawn <= original.max_get_layers
        assert result.drawn == result.good + result.confirmed_ng
        expected = original.box_remaining - result.drawn
        delta = result.box_remaining - expected
        shortage = (
            delta < 0
            and result.stop_reason == "BOX_EMPTY"
            and result.box_empty_confirmed
            and result.box_remaining == 0
        )
        excess = (
            delta > 0
            and result.stop_reason == "QUOTA_REACHED"
            and result.drawn == original.max_get_layers
            and expected == 0
            and result.remainder_verified
        )
        if delta != 0 and not (shortage or excess):
            retain_pending_and_request_original_settlement(original, result)
            return

        box = get_original_carton(s, original.carton_record_id)
        side = original.side
        assert s.good[side] + result.good <= s.target[side]
        s.drawn[side] += result.drawn
        s.good[side] += result.good
        s.ng[side] += result.confirmed_ng
        box.remaining = result.box_remaining
        if excess:
            box.usable_remaining = box.remaining
        else:
            box.usable_remaining = min(box.remaining, box.usable_remaining - result.drawn)

        if delta != 0:
            register_discrepancy_once(original, box, expected, box.remaining)
        if result.stop_reason == "PICK_FAILED":
            box.hold_reason = "WAIT_ECS_PICK_RECOVERY"

        assert s.drawn[side] == s.good[side] + s.ng[side]
        if result.carton_departed:
            release_matching_carton_occupancy(s, box)
        mark_original_result_applied(original.id, result)
        release_original_pending(s, original)
        advance_source(s)
```

断言用于表达合同前提；正式实现应拒绝无效报文并保留核对状态，不能依赖可关闭的 Python assert 处理外部输入。Result=FAILED 并不一定没有物理动作，部分实际成功和 NG 也必须按相同规则结算。

| 反例 | 入账和恢复 |
| --- | --- |
| 外码 22，实物 20，10 层均成功 | 预期箱余 2 片、实物 0，结算取出和成功各 20 片，报告短少 2 片。原最终 Result 同时结清额度并确认空箱已离位，随后恢复供箱。差异报告独立交付。 [提前箱空](opening-machine-api.md#carton-early-empty) |
| 外码 22，实物 24，11 层均成功 | 预期箱余 0、核实余 2 片，结算取出和成功各 22 片，报告多料 2 片。核实余料计为可用，本侧仍有缺口且原箱在位时可再授权 1 层。本侧已达标时留箱，计入下一单余量下限。 [多料留箱](opening-machine-api.md#carton-excess) |
| 前 4 层成功，第 5 层取料失败且确认 0 片离箱 | 取出及成功各 8 片，NG 为 0，箱余 14 片。保持原箱，等 ECS 恢复证据后重算额度。失败尝试无逐层 GET/PUT。 [取料失败](opening-machine-api.md#quota-pick-failed) |
| 原额度 Result 与新箱到位乱序 | 到位可以先保存。匹配的原最终 Result 一次结算并释放原空箱占位，匹配新箱到位和开箱授权后才发新额度。重复原结果只归原箱。 |

ECS 在已取各层实际结清、数量核实后发唯一最终 Result。取空时还须等实际离位，并随原结果回报。同命令重发原结果。若收到不符合终局约定的报文，保留原状态待核对。原结果失实时，WES 根据设备侧实物证据和具名确认调整执行账。设备确认可安全续作且 WES 核清原动作后恢复调度。 [结算与异常处置](opening-machine-api.md#quota-result)

例如目标还差 3 层，WES 授权最多 GET 3 层，结果 2 层成功、1 层确认投料 NG。此时累计成功还差 1 层，下一额度最多再取 1 层。若补取再 NG，则结算该次 NG 后再发下一额度。每次补取都有原始依据，重复回报不会增加第二份额度。

例如两侧目标各 100 片，已成功上盖 0 片、下盖 20 片。22 片上盖箱到位时只发 11 层额度。最终 Result 确认其中 2 层 NG，上盖实际成功 18 片。当同一结果确认空箱离位、两处无可用余箱且上料路径可用时，再选上盖新箱。

### 5.1 外部设备回报序列

本段伪代码说明外部设备产生回报的时序，供 WES 构造本地模拟输入。ARM03 取料到皮带与 Buffer 备板并行，物料和匹配载板均到位后由 ARM04 投料。SCAN2/3 的条码用于追溯。Buffer 处理整层投料 NG 后，ECS 在 `PUT_COMPLETED` 中报告 `pick_and_put_result=PUT_FAILED`，设备按实际终局结清本层。GET/PUT 可异步重报，回执不阻塞逐层执行。成功分支拟使用 PDF 枚举 `PUT_FINISHED`。

```python
def execute_authorized_quota(command):
    # 仅用于首次执行该额度，恢复时沿用原执行记录及层序号。
    actual_gets = 0
    results = []

    while actual_gets < command.max_get_layers and carton_has_material():
        # 请求启动后 Buffer 独立备板，ARM03 同时取料。
        board_request = start_buffer_board_request(command.product, command.side)
        layer = arm03_get_one_layer(command.carton_id, pieces=2)
        if not confirmed_full_layer(layer):
            return close_or_hold_original_pick(command, actual_gets, results, layer)
        actual_gets += 1
        layer_seq = actual_gets
        record_get_for_delivery(command, layer_seq=layer_seq)
        move_layer_to_arm04_via_belt(layer)
        board = wait_for_matching_board_at_arm04(board_request)

        while True:
            outcome = arm04_put_same_layer(layer, board)
            if outcome in {"PUT_FINISHED", "PUT_FAILED"}:
                # PUT_FAILED 由 Buffer 完成整层 NG 处置后产生。
                record_put_for_delivery(command, layer_seq=layer_seq, pick_and_put_result=outcome)
                results.append((layer_seq, outcome))
                break
            wait_or_recover_same_layer_inside_equipment(layer, board)

    return finalize_verified_quota(command, actual_gets, results)
```

设备按上一层实际成功或整层 NG 已处理的终局推进下一次 GET。每次取出为完整一层，取料失败的尝试未取出物料。`close_or_hold_original_pick` 表示设备可在原额度内恢复则继续，无法继续则核实早空、取料失败及实际数量，结清所有已取层后再回原最终 Result。数量不明期间保持原额度。逐层记录保留原事件身份。`finalize_verified_quota` 核实实际数量，箱空时等 ARM03 自行清箱并确认实际离位，再回原额度最终 Result。

## 6. 工单汇总与整单完成

```python
def supply_is_complete(s):
    return all(s.good[side] == s.target[side] for side in SIDES) and supply_actions_settled(s)


def maybe_report_supply_complete(s):
    if supply_is_complete(s):
        persist_supply_summary_once(s)


def on_wms_task_completed(s, notice):
    if (
        supply_is_complete(s)
        and matches_effective_summary(s, notice)
        and summary_sent_or_possibly_delivered(s)
    ):
        persist_completion_forward_once(s, notice)
    else:
        retain_notice_for_reconciliation(s, notice)
```

收口检查包括本单来源动作结清。末箱取空时，原额度最终 Result 须确认实际离位。可用余箱保留原占位，核实后的多料计入可用余量，差异独立上报。逐箱耗用按原工单和原箱保存在 WES。WMS 完成通知引用原有效汇总，匹配且本地已结清即可登记一次转发。收到 ECS 确认可接下一单的原成功 Result 才交接。 [整单完成](opening-machine-api.md#task-completed)

以下计算只用于硬件评审和仿真核对，不进入 WES 选源算法：

```python
def expected_padding_positions(successful_layers, magazine_layers):
    return (-successful_layers) % magazine_layers
```

假设任务开始时对应成品 Magazine 为空，成功物料连续跨箱装载。2500 片是 1250 层，上盖容量 13 层，尾料有 2 层真实物料，需补 11 个层位。下盖容量 17 层，尾料有 9 层真实物料，需补 8 个层位。空板实际块数按 Buffer 的层位映射确定。

## 7. 为什么不会因为补取而超量

新额度最多为 `min(箱剩余层数, target - good)`。原额度开始后，WES 不再发新额度；设备最多执行该额度的 GET。每次 GET 最多成功 1 层，所以本轮即使全成功，也不会使累计成功超过目标。

NG 消耗来源数量但不增加成功量。额度结清后，以实际成功缺口重算取料额度，补取从新额度开始。未确认失败、重复结果和超时均不恢复预算。

这一结论依赖硬件严格执行最大 GET 次数，并保证整层事实互斥。若设备在后台自行无限补取，或把同一层同时计为成功和 NG，该约束失效。

## 8. 配对效果及其条件

在两侧初始成功量相同、每次只选较少侧的新箱、当前箱取完才换侧、单层串行确认、无外部插单的前提下，每次新箱带来的成功增量不超过该箱实际数量。投料 NG 只会减小该箱成功增量，不会放大领先量。

在这些条件及当前箱容量下，上盖成功供料最多领先 22 片。下盖至少落后 2 片才会被选中，单箱最多增加 16 片，因此最多领先 14 片。该数值用于评估配对效果。缓存恢复、续单留箱和容量变化时按实际条件重新评估。

某侧长时间等待或连续发生投料 NG 时，保持当前箱会延长另一侧的等待。开发验证以选源和数量推进为准。

### 8.1 已有随机推演证据

主推演包含 9472 组：5250 组固定组合、3000 组随机数量、1206 组小数量扫描及 16 组大数量组合，其中 5480 组为奇数原需求。覆盖随机整层不满箱、满箱、极小箱、上下盖容量极端组合、不同 NG 模式及有限内部等待。

历史记录累计 21831623 次取层，有限恢复场景中未出现成功量超目标或来源死锁。观察到最大上盖领先 22 片、下盖领先 14 片。模型以抽象时间表示设备等待。 [仿真记录](attachments/opening-machine/simulation-results.json)

288 组相同来料及 NG 序列的策略比较如下。平均差值是在每次成功层后采样的绝对片数差，再对样本取平均；不是按物理秒数加权。

| 策略 | 平均成功供料差 | 每组平均缓存移动次数 | 判断 |
|---|---:|---:|---|
| 当前箱处理完再选较少侧 | 4.63 片 | 0.64 | 本稿采用 |
| 中途缓存当前箱追平 | 4.33 片 | 189.18 | 差值改善约 0.30 片，缓存动作明显增加，不作为默认策略 |
| 上盖全部处理后再下盖 | 1250.50 片 | 0.68 | 不满足穿插供料目的 |

### 8.2 2500 套正常流程样本

种子 88，随机完整层不满箱，每次整层投料尝试的 NG 概率为 0.5%，补取同样适用；下盖内部等待较长。设备内部等板及可恢复同层等待不计为 NG。概率输入不要求单次样本的 NG 比例恰好为 0.5%。开始顺序如下：

| 次序 | 箱号 | 盖型 | 箱内量 | 成功量 | NG | 累计上盖/下盖 |
|---|---|---|---:|---:|---:|---|
| 1 | U001 | 上盖 | 10 | 10 | 0 | 10 / 0 |
| 2 | L001 | 下盖 | 16 | 16 | 0 | 10 / 16 |
| 3 | U002 | 上盖 | 10 | 10 | 0 | 20 / 16 |
| 4 | L002 | 下盖 | 6 | 6 | 0 | 20 / 22 |
| 5 | U003 | 上盖 | 12 | 12 | 0 | 32 / 22 |
| 6 | L003 | 下盖 | 4 | 4 | 0 | 32 / 26 |
| 7 | L004 | 下盖 | 8 | 8 | 0 | 32 / 34 |
| 8 | U004 | 上盖 | 22 | 22 | 0 | 54 / 34 |

它并非严格一箱上盖、一箱下盖轮换。下盖连续两箱，是因为第一箱之后仍落后。该样本最终上盖 207 箱、下盖 282 箱，成功量各 2500 片，实际取出分别为 2510 片、2518 片，确认 NG 分别为 5 层 10 片、9 层 18 片。共取层 2514 次，其中成功 2500 层、NG 14 层，实际样本 NG 比例约 0.557%。最大领先分别为 22 片、14 片，平均差 5.7088 片。

第 488 箱 L282 原有 16 片，取出 12 片全部成功，下盖达标，余 4 片入缓存。第 489 箱 U207 原有 8 片，取出 6 片全部成功，上盖达标，余 2 片留在取料位。两箱余量保留，不再为了清箱超量投料。

完整 [489 次供箱记录](attachments/opening-machine/2500-ng005-carton-sequence.txt)和 [2514 次逐层记录](attachments/opening-machine/2500-ng005-layer-results.txt)包含出料及确认 NG 标志。补取只依据已确认的原层 NG，不依据内部等待；样本未发生成功超量或来源死锁。汇总及概率口径见[0.5% 样本结果](attachments/opening-machine/sample-ng005-results.json)。标志只表示真实物料投料成功，不是 Magazine 离场标志。

### 8.3 投料 NG 异常与压力验证

补取场景采用定点注入：仅在上盖、下盖各自最后一层各注入一次整层投料 NG，确认后分别补取 1 层。结果两侧实际取出各 2502 片、确认 NG 各 2 片、成功各 2500 片，额度结算 489 次。成功量没有超过目标，来源仍可继续，最终余量为上盖缓存 2 片、下盖取料位 4 片。结果见[正常与定点异常样本](attachments/opening-machine/normal-and-exception-samples.json)。

无 NG 样本保留作对照。原 25% NG 样本仅作为补取逻辑的压力验证保留，详见[压力供箱记录](attachments/opening-machine/2500-carton-sequence.txt)及[压力逐层记录](attachments/opening-machine/2500-layer-results.txt)。它不代表正常生产 NG 率，也不用于估算正常箱耗用。本稿主例采用约定的 0.5% 仿真输入，现场实际 NG 率仍需质量统计确认。

## 9. 必须保留的反例与验证边界

| 条件 | 结果及要求 |
|---|---|
| 不保护未完成侧缓存箱，另一侧提前留下尾箱 | 已找到来源死锁反例，见 3.1；不得启用无保护的中途切换 |
| Buffer 永久等待 | 不会错误增加成功或补取量，但任务无法完成；等待不等于来源算法死锁 |
| 每层持续投料 NG | 来源持续消耗，成功量保持不变，任务保持未达标 |
| NG 尚未确认就补取，原层后来又成功 | 可能超量；必须等原层终局且防止物料重新回流 |
| 只按 GET 数量判断完成 | 会少供；完成必须按成功投料量判断 |
| 成功量达标后仍继续把当前箱清空 | 会超量；必须停在目标并保留来源余箱 |
| 用实际 Magazine 离场差硬卡来源 | Buffer 内部状态不可见，可能引入额外循环等待；本稿没有此控制 |
| 将 4 个空板输入 Magazine 当作 4 个成品缓存 | 资源模型错误；现有推演不覆盖 Buffer 内部流转死锁 |

开发验证以单条来源流程和一个未结清额度为前提。模拟带实际离位的原额度最终 Result、新箱到位先送达、NG、原结果缺失和重报，以及整单通知。验证结果以 WES 的指令、数量和状态变化为准。

### 9.1 卡停修订后的推演

以下既有样本用于核对数量和调度，覆盖 22/20 短少、22/24 多料、零片离箱取料失败，以及旧箱结算与新箱到位的乱序及重放。多料留箱和续单余量下限按 [API 验收用例](opening-machine-api.md#102-验收用例) 验证。

固定随机种子 `20261112`，覆盖 20 种需求量（1 至 2501 套）、完整层随机不满箱和每层 0.5% 投料 NG。标准场景 2,000 组，混合注入箱量差异、取料失败、乱序及重复回报 2,000 组。既有模拟提供各等待所需的匹配证据后，4,000 组均达到两侧目标，成功量无超量。设备确认方式及实际等待时长待联调。
