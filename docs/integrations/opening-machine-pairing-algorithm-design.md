# 开箱机上下盖配对调度算法设计

版本：WES 开发评审稿 0.21

日期：2026-11-05  
适用范围：同一产品、同一任务，上下盖等量需求，每层 2 片，单取料位与单余箱缓存

本文描述 WES 内部的开工确认、选箱、额度、结果结算、补取和整单完成通知。WMS 决策和 ECS 回报由外部系统提供，在 WES 开发环境用模拟报文验证。设备职责、WES 交付范围及 C01-C12 对接依赖见概要设计。 [概要设计](opening-machine-overview-design.md)

## 1. 算法目标与输入

WES 继续处理当前箱，直至取空或本侧达标。需要新箱时，优先选择成功供料量较少的一侧。选源同时遵守取料额度、工位占位和单缓存限制。

选源输入包括工单、WMS 确认的箱资料、当前来源占位、已结算数量和原指令状态。Buffer 的备板及 Magazine 周转在设备内处理。

当前容量示例为上盖箱最多 22 片、下盖箱最多 16 片，每箱实际数量可以更少。纸箱外码经 ECS、WES 转交 WMS，由 WMS 解析有效外码并返回数量。开箱回库的纸箱按核实的实物余量重新出具外码，再次上线时使用更新后的数量。设备内余箱从缓存恢复时，沿用原箱号和已结算的余量。

配对按已成功放到载板的实物片数计算。实际到线顺序由 Buffer、搬运和下游共同协调。

| 评审状态 | 内容 |
|---|---|
| 已确认 | 到箱确认时下发当前箱额度并预判下一箱，原额度结清后按两侧实际成功量复核。整层取料额度、同层 GET/PUT 配对、Buffer 处理整层 NG、整单完成通知、同产品余箱复用及换产品人工清线 |
| 本稿采用的调度约束 | 一次只执行一条来源流程，一个未结清取料额度。设备按同一层结清后推进，层内取料与备板并行 |
| WES 对接依赖 | 额度 Result 统一入账方案及字段、工位释放回报、整单通知报文和原指令恢复约定。ARM01 提前备箱条件待 ECS 联调 |

## 2. 状态和数量单位

业务数量以片计，伪代码内部统一用层。转换在输入和输出边界进行，`1 层 = 2 片`。

| 状态 | 内容 |
|---|---|
| task | 任务身份、产品、原需求、向上取偶的执行目标 |
| target[side] | 各侧目标层数，例如 2501 套对应 1251 层 |
| good[side] | 已结算成功层数 |
| drawn[side] | 已结算实际取出层数 |
| ng[side] | 已结算、已确认投料 NG 层数 |
| active | 取料位纸箱，包含箱号、产品、盖型和剩余层数；无箱为 None |
| cache | 唯一缓存箱，结构与 active 相同；无箱为 None |
| carton records | 用入线记录 ID 保存原箱、实物余量、可取余量及待恢复或待处置原因。箱离位后仍可按原额度找到该记录 |
| start_operation_id / start_binding_confirmed | 原开工通知身份，以及 WMS 对当前工单保存执行绑定的确认 |
| pending | 原供箱、校验、取料、离位或缓存流程尚未结清。供箱流程包含缺箱等待补给，阻止新来源动作 |
| next_carton_plan | 随额度动作保存的下一箱候选盖型和来源。到箱时按预计成功量生成，结算时按实际成功量更新。双侧达标时为 None |
| carton_consumption | 按工单与来源箱归集已结清的取出、成功和 NG 层数，以及启用逐箱上报时的原报告身份 |
| supply_complete_reported / completion_notice | 两侧供料汇总是否已登记，以及 WMS 整单完成通知与原转发身份 |

WES 保留当前额度直到结清。按本稿拟定的入账方式，GET/PUT 用于逐层核对，最终额度 Result 统一更新业务数量。

已结清状态满足：

```text
0 <= good[side] <= target[side]
drawn[side] = good[side] + ng[side]
remaining_draw_allowance[side] = target[side] + ng[side] - drawn[side]
                             = target[side] - good[side]
```

物理动作进行中还包含未结清层，此时完整物理账为 `实际取出 = 成功 + 已确认 NG + 未结清`。额度授权数不等于实际取出，更不等于成功量。

## 3. 来源选择顺序

WES 取得本线执行资格并核对留箱或清线后，在同一事务登记开工及原通知。收到 WMS 对当前工单、工作线和原通知的绑定保存确认后，才允许供箱、恢复缓存箱或取料。未确认时沿原通知恢复，历史确认归原任务。 [开工通知](opening-machine-api.md#task-started)

1. 原指令、当前层或扫码决策未结清，等待原处理，保持来源不变。
2. 当前箱已空，等待 ARM03 移走空纸箱及设备确认取料位释放。清箱后的送箱放行由设备联动完成。
3. 两侧成功量均达标，且额度、在途层和相关来源动作均已结清后，WES 向 WMS 汇总供料结果。已确认的余箱可人工清线，或留在原位供同产品下一单复用。
4. 当前箱仍有料且本侧未完成，继续当前箱。投料 NG 引起的不足也优先从当前箱补取。
5. 当前箱仍有料但本侧完成，另一侧未完成且缓存为空时，将本箱移入缓存。
6. 取料位空、缓存箱属于本任务且其侧尚未完成时，先恢复缓存箱。
7. 无需恢复缓存时，在未完成两侧中选 `good` 较小者；相同则先上盖。ARM01 只按该选择取一箱，不提前批量开箱。

缓存中已有本侧已完成的余箱时，可以继续处理另一侧。另一侧中途取空箱就移走空箱、继续新箱；另一侧最终完成时允许将其余箱留在取料位。此时两侧都不再需要取料，属于待清场，不属于供料死锁。

### 3.1 单缓存位尾数保护

默认算法不会中途缓存未完成侧的箱子。恢复、人工干预或后续优化策略可能带来这类状态，仍必须保护：

> 缓存中有本任务尚需取料的箱子时，取料位一旦可用，就先恢复该箱。不要为了追平配对差值先取另一侧新箱。

例如需求 4 套，缓存有上盖余箱，上盖只成功 2 片。若新开一个 16 片下盖箱，只取 4 片后下盖达标，取料位还剩 12 片。该箱无法进入已占用的缓存，上盖缓存箱也回不来，形成来源死锁。

此前关闭保护的推演已经出现该反例，因此本稿不启用中途缓存追平策略。若将来引入该策略，另一侧本次处理必须能保证取空，而不是只保证它暂时落后。数量未知时可用经确认的单箱最大量做保守判断；最大量不可靠时不能绕过先恢复规则。

### 3.2 跨任务复用

同产品续单时，核对原位留箱的原箱号、盖型、位置和已结算余量，将这些箱绑定到新任务。新任务从零累计成功量和 NG。换产品时，操作员扫描外箱码并在 WMS 的 PC 或 PDA 页面确认移箱。WMS 停用旧码后向 WES 回报。登记开工通知前，WES 核对工作线和前后任务，按原工位及入线时保存的 SCAN1 外码唯一匹配全部待清占位。确认后关闭原来源记录，历史耗用继续引用原箱。WMS 在独立退料时实点并签发新码，余箱重新上线时走 SCAN1 校验并使用有效新码数量。 [人工清线确认](opening-machine-api.md#line-clearance-confirmed)

两侧仍需供料且取料位和缓存位均占用时，在登记开工通知前检查 `dual_carton_blocks(s)`。例如满箱开始的 6 套工单结束后，ARM03 留下 10 片下盖，缓存留下 16 片上盖。下一单仍为同产品 6 套时，取完 6 片下盖仍有 4 片占位，须先腾位。WMS 组织人工移出其中一箱，WES 按当前交接、工作线、原工位及扫描外箱码核对实际移出事实，只释放匹配的占位一次，然后重新判断开工条件。确认后仍须等待本单 WMS 开工绑定。此处是同产品单箱腾位，回报承载待 WMS 联调；换产品仍按完整清线处理。取料过程中发现同类双占位也按相同方式等待腾位并重新选源。

## 4. Python 伪代码：WES 选源与额度

下列 `State`、`Carton` 由上节的状态组成。跨任务场景先按 3.2 完成交接，再调用选源。`pending` 包括已持久化但尚未发送的命令，避免两个并发调度同时选源。ARM03 到箱后，在同一推进事务登记当前箱额度和下一箱预判，提交后发送额度。预计量只参与预判，实际取箱仍受结果结算和占位约束。 [API 对接规则](opening-machine-api.md#next-carton-planning)

```python
UPPER, LOWER = "UPPER", "LOWER"
SIDES = (UPPER, LOWER)


def target_layers(requested_sets):
    if type(requested_sets) is not int or requested_sets < 0:
        raise ValueError("需求必须是非负整数")
    return (requested_sets + 1) // 2


def need(s, side):
    return s.target[side] - s.good[side]


def quota_layers(s, box):
    # 调用时原额度已结清，drawn/good/ng 都是本侧累计层数。
    side = box.side
    allowance = s.target[side] + s.ng[side] - s.drawn[side]
    assert allowance == need(s, side)
    return min(box.remaining, box.usable_remaining, allowance)


def dual_carton_blocks(s):
    box = s.active
    return (
        box is not None
        and s.cache is not None
        and box.remaining > min(box.usable_remaining, need(s, box.side))
        and any(need(s, side) > 0 for side in SIDES if side != box.side)
    )


def next_carton_candidate(s, good):
    unfinished = [side for side in SIDES if good[side] < s.target[side]]
    if not unfinished:
        return None
    if s.cache is not None and s.cache.side in unfinished and not s.cache.hold_reason:
        return ("RESTORE_AND_CONFIRM", s.cache.id)
    side = min(unfinished, key=lambda side: good[side])  # 平局先上盖。
    return ("FETCH_ONE_CARTON", side)


def register_current_quota(s, box, layers):
    quota = register_quota_intent(s, box, layers)
    projected_good = dict(s.good)
    projected_good[box.side] += layers
    quota.next_carton_plan = next_carton_candidate(s, projected_good)


def on_carton_arrived(s, arrival):
    with locked_task_transaction(s.task_id):
        save_original_carton_arrival(s, arrival)
        advance_source(s)


def on_carton_departed(s, departure):
    with locked_task_transaction(s.task_id):
        if apply_matched_departure(s, departure):
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

    if s.active is not None and s.active.remaining == 0:
        return ("WAIT_EMPTY_CARTON_REMOVAL", s.active.id)

    if all(need(s, side) == 0 for side in SIDES):
        if supply_is_complete(s):
            return ("SUPPLY_DONE",)
        return ("WAIT_ORIGINAL",)

    if s.active is not None:
        box = s.active
        if box.hold_reason:
            return (box.hold_reason, box.id)
        if dual_carton_blocks(s):
            return ("WAIT_DUAL_CARTON_CLEARANCE",)
        if need(s, box.side) > 0:
            return ("RUN_QUOTA", box.id, quota_layers(s, box))
        if s.cache is None:
            return ("PARK_AND_CONFIRM", box.id)
        # 正常策略不会制造此状态；已有状态需保持并核对，不能强行挤入缓存。
        return ("WAIT_DUAL_CARTON_CLEARANCE",)

    if s.cache is not None and need(s, s.cache.side) > 0 and s.cache.hold_reason:
        return (s.cache.hold_reason, s.cache.id)
    candidate = next_carton_candidate(s, s.good)
    if candidate[0] == "FETCH_ONE_CARTON" and not source_path_is_available(s):
        return ("WAIT_SOURCE_PATH",)
    return candidate
```

到箱事实按原供箱关联保存。`apply_saved_eligible_arrival` 核对当前工单、箱号、盖型和原供箱动作，只在前一额度已结清、原箱已离位且占位匹配时应用一次，绑定 `active` 并关闭本次供箱等待。无已匹配的在途供箱时，过早或旧到箱事实仍归原动作。`apply_matched_departure` 按工作线、原工位及入线箱身份核对实际离位，只释放当时匹配的占位一次，保留原箱记录及未结清额度。重复旧箱离位回报不会释放新箱。

`apply_matching_source_resolution` 按原异常及箱身份应用一次有效证据。ECS 确认 ARM03 已恢复且原箱仍在位时解除取料恢复等待。WMS 明确批准多料可用量时更新 `usable_remaining`，以上限和已核实实物余量中的较小值为准；确认退料时保持待离位，实际移出后释放占位。多料差异报告的收妥回执只更新消息交付状态。同产品双余箱按 §3.2 应用 WMS 腾位回报，开工前重新检查交接条件。`apply_source_decision` 在同一事务登记可执行动作，`RUN_QUOTA` 调用 `register_current_quota`，等待分支保留待推进依据。

动作的状态变化由物理结果驱动：

| 动作 | 发出前 | 成功结果到达后 |
|---|---|---|
| FETCH_ONE_CARTON | 从 WMS 工单取得产品，上盖选 A、下盖选 B，固定相应箱型及原命令身份，设置 pending | ARM01 到位后扫码，WES 校验通过后下发本箱开箱指令。ARM03 新箱到位回报的盖型及关联身份核对通过后绑定 active。工位无可用箱则等待补给 |
| RUN_QUOTA | 固定箱号、最大取层数及箱余量快照，设置 pending，并按本轮预计成功量保存下一箱候选 | 核对额度结算，更新成功/NG/实际取出与箱余量，清除原 pending，按实际数量与占位重新选源 |
| WAIT_EMPTY_CARTON_REMOVAL | 已知箱空，ARM03 自行移走纸箱 | 确认原箱实际离位后释放匹配占位。额度 Result 可先到或后到，按原额度关联的箱记录入账 |
| PARK_AND_CONFIRM | 缓存为空且当前层已结清，保留两个位置的预留 | 原 MOVE_BACKWARD 完成并核对箱号后，cache=active，active=None |
| RESTORE_AND_CONFIRM | 取料位可用、缓存箱身份明确 | 原 MOVE_FORWARD 完成后，active=cache，cache=None |

原动作结果未知时，WES 保留原箱体位置和待结清状态。ECS 在断线恢复或重启后按原 `command_code` 重发已保存的最终 Result，WES 对原指令结算一次，再更新箱体位置或取料额度。若设备尚未确认执行结果，先核对实际动作和占位，再按原指令结清。

WES 在内部关联 SCAN1 的 `scan_ref` 和发给 WMS 的原校验 `operation_id`。WMS 按工单号查期望产品，并用外码解析实际产品、盖型和数量。取箱来源由 WES 从原 ARM01 指令读取。校验响应回显原 `operation_id`，匹配当前待开箱纸箱及有效扫码后，WES 才登记开箱指令，并保存其与原扫码和校验决定的关联。

开箱机自行联动清空箱及送箱。ARM01 提前备箱须与 ECS 确认独立上游等待位及可用回报、待用箱扣留与放行、与空箱离位和缓存恢复的互锁，以及计划变化后的箱体去向。确认前按串行流程取箱。已有未结清的 ARM01 指令沿原身份核对，选源等待原动作。

### 4.1 缺箱后的补给恢复

ARM01 明确缺箱且未搬箱后，WES 按 WMS 方案在相应工位复用公共 RackDispatch。公共作业只推进新栈板 ENTER，搬运模板为 `f0`。WES 保存原缺箱动作与该入场作业的关联，门禁和现场互锁由 AGV/MCS 与 ECS 处理。C/D 空栈板处理走独立流程。 [新栈板入场](opening-machine-api.md#replenishment-handoff)

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

设备按 WES 下发的最大取层数限制实际 GET。每取出一层占用一层额度，达到额度后停止下一次取料并保留箱内余料。额度内既有成功层，也可能有 `PUT_COMPLETED` 报告 `PUT_FAILED` 的整层 NG。额外补取由 WES 在结算后另行授权。本节采用原额度 Result 统一入账的方案，逐层事件作为该额度的明细。

下列逻辑字段可先用于本地模拟，正式报文按开发契约对齐。`validate_original` 核对任务、产品、盖型、箱号、原 `command_code`、授权额度和下发时的箱余量。原结果重复到达时沿用已处理状态。同身份内容冲突时暂停并核对。

`box_empty_confirmed`、`remainder_verified` 和 `failed_attempt_confirmed_zero` 表示已核对的设备事实。离箱数量不明或只离箱 1 片时保留原额度及核对状态。以下布尔值用于表达判定，实际承载按 ECS 联调确认。`validate_original` 还校验已收逐层事实和原箱离位事实之间的矛盾，例如旧箱已确认空箱离位却报告箱内仍有料。

每层 `GET_COMPLETED` 与终局 `PUT_COMPLETED` 用原取料额度指令号和额度内层序号配对。ECS 重发时保持 `device_code`、`event_type`、事件体 `timestamp` 和 `data` 不变，WES 通用接收器据此识别重复事件。按本稿拟定的额度结算方案，逐层事件作为明细，WES 以原额度指令 Result 统一入账，重复事件和重复 Result 都只对应同一份补取资格。

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
            or result.pick_count_unknown_or_partial
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
        box.usable_remaining = min(box.remaining, box.usable_remaining - result.drawn)

        if delta != 0:
            register_discrepancy_once(original, box, expected, box.remaining)
        if excess or (box.remaining > 0 and box.usable_remaining == 0):
            box.hold_reason = "WAIT_WMS_EXCESS_DISPOSITION"
        if result.stop_reason == "PICK_FAILED":
            box.hold_reason = "WAIT_ECS_PICK_RECOVERY"

        assert s.drawn[side] == s.good[side] + s.ng[side]
        mark_original_result_applied(original.id, result)
        release_original_pending(s, original)
        original.next_carton_plan = next_carton_candidate(s, s.good)
        advance_source(s)
```

断言用于表达合同前提；正式实现应拒绝无效报文并保留核对状态，不能依赖可关闭的 Python assert 处理外部输入。Result=FAILED 并不一定没有物理动作，部分实际成功和 NG 也必须按相同规则结算。

| 反例 | 入账和恢复 |
| --- | --- |
| 外码 22，实物 20，10 层均成功 | 预期箱余 2 片、实物 0，结算取出和成功各 20 片，报告短少 2 片。原额度结清且空箱实际离位后恢复供箱，库存差异报告独立恢复。 [提前箱空](opening-machine-api.md#carton-early-empty) |
| 外码 22，实物 24，11 层均成功 | 预期箱余 0、核实余 2 片，结算取出和成功各 22 片，报告多料 2 片。WMS 批准且仍有缺口时才可再取最多 1 层；退料时待实际移出再选源。两侧已达标时可按交接规则留箱。 [多料处置](opening-machine-api.md#carton-excess) |
| 前 4 层成功，第 5 层取料失败且确认 0 片离箱 | 取出及成功各 8 片，NG 为 0，箱余 14 片。保持原箱，等 ECS 恢复证据后重算额度。失败尝试无逐层 GET/PUT。 [取料失败](opening-machine-api.md#quota-pick-failed) |
| 离位、额度 Result 与下一箱到位乱序 | 离位只释放匹配占位，Result 更新原箱记录；下一箱到位事实可先存。前一额度结清、原箱实际离位且下一箱的原供箱关联及占位都匹配后，才下发下一箱额度。 |

若设备已返回终局 Result，但仍有物料未结清，WES 保留原 Result 并挂起后续额度，通过另行关联的核对证据完成结算。不能覆盖原终局记录，也不能将同一身份改写成另一份成功 Result。该恢复证据合同需 ECS/WES 双方补齐。

例如目标还差 3 层，WES 授权最多 GET 3 层，结果 2 层成功、1 层确认投料 NG。此时累计成功还差 1 层，下一额度最多再取 1 层。若补取再 NG，则结算该次 NG 后再发下一额度。每次补取都有原始依据，重复回报不会增加第二份额度。

例如目标各 100 片，已成功上盖 0 片、下盖 20 片。22 片上盖箱到位时登记 11 层额度，预计上盖成功 22 片，预判下盖。若实际取出 11 层，其中 2 层投料 NG，结算后上盖实际成功 18 片。两处没有可用余箱时，空箱离位且上料路径可用后改选上盖新箱。

### 5.1 外部设备回报序列

本段伪代码说明外部设备产生回报的时序，供 WES 构造本地模拟输入。ARM03 取料到皮带与 Buffer 备板并行，物料和匹配载板均到位后由 ARM04 投料。SCAN2/3 的条码用于追溯。Buffer 处理整层投料 NG 后，ECS 在 `PUT_COMPLETED` 中报告 `pick_and_put_result=PUT_FAILED`，以该事件结清本层。成功分支拟使用 PDF 枚举 `PUT_FINISHED`。

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
        report_get_completed(command, layer_seq=layer_seq)
        move_layer_to_arm04_via_belt(layer)
        board = wait_for_matching_board_at_arm04(board_request)

        while True:
            outcome = arm04_put_same_layer(layer, board)
            if outcome in {"PUT_FINISHED", "PUT_FAILED"}:
                # PUT_FAILED 由 Buffer 完成整层 NG 处置后产生。
                report_put_completed(command, layer_seq=layer_seq, pick_and_put_result=outcome)
                results.append((layer_seq, outcome))
                break
            wait_or_recover_same_layer_inside_equipment(layer, board)

    return closed_quota_result(command, actual_gets, results)
```

内部等待持续到设备恢复或人工处置。设备按同一层的唯一终局结果推进下一次 GET。取料可在原额度内恢复时由设备自行恢复。`close_or_hold_original_pick` 表示无法继续时按原指令回报已确认的提前箱空、零片离箱失败，或部分离箱及数量不明的核对事实。仅确认整层出箱后才产生该层 GET 和对应 PUT。WES 根据已结清的投料 NG 计算补取资格。

## 6. Python 伪代码：逐箱耗用、供料汇总与整单完成

两侧成功量分别达到执行目标，且额度、在途层和相关来源动作均已结清后，WES 向 WMS 发送工单级汇总。余箱位置和数量由 WES 保存。若 WMS 确认按箱扣账，在单箱或工单结算时检查该箱是否已无后续取料并已结清，再登记逐箱耗用意图。WMS 的整单完成通知由 WES 转告 ECS，设备自行收尾。

```python
def supply_is_complete(s):
    if s.active is not None and s.active.remaining == 0:
        return False
    return all(
        s.good[side] == s.target[side]
        and not has_unsettled_quota_or_layer(s, side)
        and not has_unsettled_source_action(s, side)
        for side in SIDES
    )


def maybe_report_supply_complete(s):
    if supply_is_complete(s) and not s.supply_complete_reported:
        persist_supply_summary_intent(s)
        s.supply_complete_reported = True


def maybe_report_carton_consumption(s, carton):
    if not carton_posting_agreed():
        return
    if has_unsettled_carton_quota_or_layer(s.task, carton):
        return
    if carton.remaining > 0 and not supply_is_complete(s):
        return
    usage = settled_usage_for(s.task, carton)
    if usage.drawn > 0:
        persist_carton_consumption_once(s.task, carton, usage)


def on_wms_task_completed(s, notice):
    validate_notice_matches_task(s, notice)
    if not supply_is_complete(s):
        retain_notice_for_reconciliation(s, notice)
        return
    persist_completion_forward_once(s, notice)
```

逐箱上报仅统计原工单的已结清额度。缓存箱仍可能在本单续取，移动时继续累计。工单供料结清时，复核有耗用记录的各来源箱，并为尚未登记的余箱耗用保存原报告。按工单和来源箱去重，各报告与对应业务状态同事务保存，发送时将层数换算为片数，重发沿原身份。箱级报告和工单汇总独立恢复。WMS 若要求先扣账，由 WMS 在发送完成通知前判断。

完成通知和转发意图关联保存。设备收到通知后自行补空板、送出 Magazine。来源余箱清场和任务切换按 C10 处理。 [逐箱耗用报文](opening-machine-api.md#carton-consumption-report) · [整单完成通知](opening-machine-api.md#task-completed)

以下计算只用于硬件评审和仿真核对，不进入 WES 选源算法：

```python
def expected_padding_positions(successful_layers, magazine_layers):
    return (-successful_layers) % magazine_layers
```

假设任务开始时对应成品 Magazine 为空，成功物料连续跨箱装载。2500 片是 1250 层，上盖容量 13 层，尾料有 2 层真实物料，需补 11 个层位。下盖容量 17 层，尾料有 9 层真实物料，需补 8 个层位。空板实际块数按 Buffer 的层位映射确定。

## 7. 为什么不会因为补取而超量

新额度最多为 `min(箱剩余层数, target - good)`。原额度开始后，WES 不再发新额度；设备最多执行该额度的 GET。每次 GET 最多成功 1 层，所以本轮即使全成功，也不会使累计成功超过目标。

NG 消耗来源数量但不增加成功量。额度结清后，已确认 NG 使剩余取料预算恢复相同层数，补取从新额度开始。未确认失败、重复结果和超时均不恢复预算。

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

开发验证以单条来源流程和一个未结清额度为前提。用回报模拟空箱离位、NG、结果缺失、原指令重发及整单通知。验证结果以 WES 的指令、数量和状态变化为准。

### 9.1 卡停修订后的推演

用本文选源和额度结算伪代码配合模拟的对端回报复测。两单各 6 套的双余箱场景在有效腾位和本单开工绑定确认后恢复。22/20 短少、22/24 多料的允许和退料、零片离箱取料失败和恢复均按原身份入账。枚举空箱离位、旧额度 Result、新箱到位的 6 种到达顺序并重放，确认只结算旧箱一次，三个事实匹配后只登记一份新额度。缺证据、错身份或重复回报未提前触发推进。乱序到箱用于验证接收逻辑，实际供箱仍按串行执行。

固定随机种子 `20261112`，覆盖 20 种需求量（1 至 2501 套）、完整层随机不满箱和每层 0.5% 投料 NG。标准场景 2,000 组，混合注入箱量差异、取料失败、乱序及重复回报 2,000 组。模拟提供匹配的 WMS 处置、ECS 恢复及离位证据后，4,000 组均达到两侧目标，成功量无超量。设备实际确认方式、等待时长和 ARM01 提前备箱仍待联调。
