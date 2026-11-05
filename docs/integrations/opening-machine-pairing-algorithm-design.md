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
| 已确认 | 整层取料额度、同层 GET/PUT 配对、Buffer 处理整层 NG、整单完成通知、同产品余箱复用及换产品人工清线 |
| 本稿采用的调度约束 | 一次只执行一条来源流程，一个未结清取料额度。设备按同一层结清后推进，层内取料与备板并行 |
| WES 对接依赖 | 额度 Result 统一入账方案及字段、工位释放回报、整单通知报文和原指令恢复约定 |

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
| start_operation_id / start_binding_confirmed | 原开工通知身份，以及 WMS 对当前工单保存执行绑定的确认 |
| pending | 原供箱、校验、取料、离位或缓存流程尚未结清。供箱流程包含缺箱等待补给，阻止新来源动作 |
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
2. 两侧成功量均达标，且额度和在途层结清后，WES 向 WMS 汇总供料结果。余箱可人工清线，或留在原位供同产品下一单复用。
3. 当前箱已空，等待 ARM03 移走空纸箱及设备确认取料位释放。清箱后的送箱放行由设备联动完成。
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

同产品续单时，核对留箱的原箱号、盖型、位置和已结算余量，将这些箱绑定到新任务。新任务从零累计成功量和 NG。换产品时，选源前必须取得人工清线完成的确认。

若取料位与缓存位都留箱，且取料位箱的余量大于本侧尚需量，另一侧又尚未完成，先等待人工腾位。例如取料位有 6 片下盖，缓存位有 4 片上盖，下一单要 2 套。若先取 2 片下盖，下盖箱仍有 4 片占着取料位，上盖缓存箱就无法回到取料位。选源时先拦住这一状态，再由人工腾出其中一处。

## 4. Python 伪代码：WES 选源与额度

下列 `State`、`Carton` 由上节的状态组成。跨任务场景先按 3.2 完成交接，再调用选源。`pending` 包括已持久化但尚未发送的命令，避免两个并发调度同时选源。每次状态推进应在同一事务中锁定任务，保存状态与下一条命令意图，提交后发送。

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
    return min(box.remaining, allowance)


def choose_source(s):
    if not s.start_binding_confirmed:
        return ("WAIT_WMS_START_BINDING",)

    if s.pending:
        return ("WAIT_ORIGINAL",)

    if all(need(s, side) == 0 for side in SIDES):
        return ("SUPPLY_DONE",)  # 仍可有余箱，不代表清场。

    if s.active is not None:
        box = s.active
        if box.remaining == 0:
            return ("WAIT_EMPTY_CARTON_REMOVAL", box.id)
        if need(s, box.side) > 0:
            other_needs_supply = any(need(s, side) > 0 for side in SIDES if side != box.side)
            if s.cache is not None and box.remaining > need(s, box.side) and other_needs_supply:
                return ("HOLD_FOR_SOURCE_RECONCILIATION",)
            return ("RUN_QUOTA", box.id, quota_layers(s, box))
        if s.cache is None:
            return ("PARK_AND_CONFIRM", box.id)
        # 正常策略不会制造此状态；已有状态需保持并核对，不能强行挤入缓存。
        return ("HOLD_FOR_SOURCE_RECONCILIATION",)

    if s.cache is not None and need(s, s.cache.side) > 0:
        return ("RESTORE_AND_CONFIRM", s.cache.id)

    unfinished = [side for side in SIDES if need(s, side) > 0]
    side = min(unfinished, key=lambda side: (s.good[side], SIDES.index(side)))
    return ("FETCH_ONE_CARTON", side)
```

动作的状态变化由物理结果驱动：

| 动作 | 发出前 | 成功结果到达后 |
|---|---|---|
| FETCH_ONE_CARTON | 从 WMS 工单取得产品，上盖选 A、下盖选 B，固定相应箱型及原命令身份，设置 pending | ARM01 到位后扫码，WES 校验通过后下发本箱开箱指令。ARM03 新箱到位回报的盖型及关联身份核对通过后绑定 active。工位无可用箱则等待补给 |
| RUN_QUOTA | 固定箱号、最大取层数及箱余量快照，设置 pending | 核对额度结算，更新成功/NG/实际取出与箱余量，清除原 pending |
| WAIT_EMPTY_CARTON_REMOVAL | 当前箱 remaining=0，WES 保留占位并等待。ARM03 自行移走空箱 | 设备确认箱体离位及取料位释放后设置 active=None |
| PARK_AND_CONFIRM | 缓存为空且当前层已结清，保留两个位置的预留 | 原 MOVE_BACKWARD 完成并核对箱号后，cache=active，active=None |
| RESTORE_AND_CONFIRM | 取料位可用、缓存箱身份明确 | 原 MOVE_FORWARD 完成后，active=cache，cache=None |

原动作结果未知时，WES 保留原箱体位置和待结清状态。ECS 在断线恢复或重启后按原 `command_code` 重发已保存的最终 Result，WES 对原指令结算一次，再更新箱体位置或取料额度。若设备尚未确认执行结果，先核对实际动作和占位，再按原指令结清。

开箱机自行联动清空箱及送箱。WES 以 ECS 转报的 ARM03 新箱到位事实绑定 `active`，核对到箱盖型与原供箱计划一致。建议同时核对箱号或唯一供箱身份。前一箱额度尚未结清时可先保存到箱回报，待结算后再授权新箱取料。

### 4.1 缺箱后的补给恢复

ARM01 缺箱后，WES 保留原供箱意图和待结清状态。WMS 按执行绑定和缺料工位发起 AGV 搬运，补给轮次随原搬运任务交接给 ECS。ECS 确认可取箱后回报轮次及原缺箱取箱指令。该外部链路在 WES 开发环境通过模拟回报验证。 [补给交接](opening-machine-api.md#replenishment-handoff)

```python
def resume_after_replenishment(s, waiting, ready):
    if not s.start_binding_confirmed or waiting is None:
        return "WAIT"
    if not matches_current_supply(s, waiting, ready):
        return "WAIT"
    if not waiting.original_result_confirms_no_move:
        return "WAIT"
    if replenishment_already_consumed(s, ready.replenishment_ref):
        return "DUPLICATE"

    # 同一事务消费补给轮次，并登记一次带新身份的取箱意图。
    consume_round_and_register_fetch(s, waiting, ready.replenishment_ref)
    return "FETCH_REGISTERED"
```

`matches_current_supply` 核对工作线、当前工单、产品、盖型、工位及原缺箱指令，并确认回报对应当前补给轮次且工位可取箱。可取箱回报先于原 Result 时先保存，待原结果明确后用同一条件复核。同轮次已消费时复用原处理结果。

## 5. Python 伪代码：额度结算与补取

设备按 WES 下发的最大取层数限制实际 GET。每取出一层占用一层额度，达到额度后停止下一次取料并保留箱内余料。额度内既有成功层，也可能有 `PUT_COMPLETED` 报告 `PUT_FAILED` 的整层 NG。额外补取由 WES 在结算后另行授权。本节采用原额度 Result 统一入账的方案，逐层事件作为该额度的明细。

下列逻辑字段可先用于本地模拟，正式报文按开发契约对齐。`validate_original` 核对任务、产品、盖型、箱号、原 `command_code`、授权额度和下发时的箱余量。原结果重复到达时沿用已处理状态。同身份内容冲突时暂停并核对。

每层 `GET_COMPLETED` 与终局 `PUT_COMPLETED` 用原取料额度指令号和额度内层序号配对。ECS 重发时保持 `device_code`、`event_type`、事件体 `timestamp` 和 `data` 不变，WES 通用接收器据此识别重复事件。按本稿拟定的额度结算方案，逐层事件作为明细，WES 以原额度指令 Result 统一入账，重复事件和重复 Result 都只对应同一份补取资格。

```python
def settle_quota(s, original, result):
    # 伪代码原语：事务锁、原身份去重、校验、入账及 pending 释放必须原子完成。
    with locked_task_transaction(s.task_id):
        if original_result_already_applied(original.id, result):
            return  # 同身份异内容由该原语报告冲突。

        validate_original(s, original, result)
        require_nonnegative_integer_layers(result)
        assert result.drawn <= original.max_get_layers

        if result.unsettled != 0:
            retain_pending_and_request_original_settlement(original, result)
            return

        assert result.drawn == result.good + result.confirmed_ng
        assert result.box_remaining == original.box_remaining - result.drawn

        side = original.side
        assert s.good[side] + result.good <= s.target[side]
        s.drawn[side] += result.drawn
        s.good[side] += result.good
        s.ng[side] += result.confirmed_ng
        s.active.remaining = result.box_remaining

        assert s.drawn[side] == s.good[side] + s.ng[side]
        mark_original_result_applied(original.id, result)
        release_original_pending(s, original)
        # 下一次 quota_layers 自动得到 NG 补取后的剩余额度。
```

断言用于表达合同前提；正式实现应拒绝无效报文并保留核对状态，不能依赖可关闭的 Python assert 处理外部输入。Result=FAILED 并不一定没有物理动作，部分实际成功和 NG 也必须按相同规则结算。

若设备已返回终局 Result，但仍有物料未结清，WES 保留原 Result 并挂起后续额度，通过另行关联的核对证据完成结算。不能覆盖原终局记录，也不能将同一身份改写成另一份成功 Result。该恢复证据合同需 ECS/WES 双方补齐。

例如目标还差 3 层，WES 授权最多 GET 3 层，结果 2 层成功、1 层确认投料 NG。此时累计成功还差 1 层，下一额度最多再取 1 层。若补取再 NG，则结算该次 NG 后再发下一额度。每次补取都有原始依据，重复回报不会增加第二份额度。

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

内部等待持续到设备恢复或人工处置。设备按同一层的唯一终局结果推进下一次 GET。WES 根据已结清的投料 NG 计算补取资格。

## 6. Python 伪代码：整单完成通知

两侧成功量分别达到执行目标，且额度、在途层和相关来源动作均已结清后，WES 向 WMS 汇总供料结果。已经确认位置和数量的余箱随汇总保留。WMS 下发整单完成通知，本稿按 WES 转告 ECS、设备自行收尾的路径设计。

```python
def supply_is_complete(s):
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


def on_wms_task_completed(s, notice):
    validate_notice_matches_task(s, notice)
    if not supply_is_complete(s):
        retain_notice_for_reconciliation(s, notice)
        return
    persist_completion_forward_once(s, notice)
```

供料汇总状态及通知转发意图各自在同一事务保存，重发沿用原身份。设备收到整单完成通知后，自行补空板、送出 Magazine，并管理设备内部收尾。来源余箱清场和任务切换按 C10 处理。

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
