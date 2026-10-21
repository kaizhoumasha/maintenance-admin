# 货架搬运业务接入层设计

状态:**草案,待评审** · 依据:`wes_backend/docs/integration/manual-outbound-rack-transport.md`(2026-09-10 现场确认,下称「现场规则」)、`docs/superpowers/specs/2026-10-21-transport-outbound-design.md`(能力层,下称「能力层说明书」)

> 本层位于 `TransportService`(能力层)之上,工作线插件只调用本层。能力层不认识「进场/离场/五层货架」,本层不认识 HTTP、信封和重试。

## 1. 目标与边界

**目标**:把现场规则里确定的部分落成代码——货架角色决定每个场景用哪个 RCS 模板、源/目标位置的类型约束,并保证同一货架一次进出场生命周期的模板配对。

**非目标**:本层不认识任何上游业务单据,不判断为什么搬、何时搬、搬几次,入参都是调用方已决定好的事实(货架号、角色、目标位置、目标面)。留待后续阶段(见 §5)的有:目标点容量的滚动下发窗口、取消/失败后的业务重试、`departure_decide` 请求、换面时机判断、整架任务完成 API、任何需要入站结果(T3/T4)才能判断的状态。

## 2. 分层与位置

```text
工作线插件(只调用)
  │
货架搬运业务接入层   app/integrations/wms_transport/rack_scenarios.py   ← 本文
  │  场景 → TransportService.move_rack / rotate_rack
TransportService    能力层(已实现)
```

依赖只能向下;本层不读数据库、不发网络、不查可信状态(换面所需的可信位置/面由能力层的 `RackStateReader` 提供)。

## 3. 设计

> 本节的 `RackRole`、`enter_rack` / `rotate_rack` / `depart_rack`、`RackDestination`、`_move` 均为拟实现,**代码尚未编写**;已实现的只有能力层(`TransportService`)。

### 3.1 角色与模板表(唯一一份)

角色由调用方显式传入,**不得根据货架号前缀或位置编码猜测**(现场规则开头)。本层只定义自己的三种角色,不认识 `plan_delta`、`departure_decide` 等上游业务单据;由调用方的业务任务把业务字段映射成角色(现场规则中转运货架对应 `target_rack`、退料货架对应 `added_direct_picks`、五层货架对应 `bin_source_racks`,drain 货架也按五层货架处理)。

| 角色 `RackRole` | 进场 `enter` | 换面 `rotate` | 离场 `depart` |
|---|---|---|---|
| `TRANSFER` 转运货架 | `F01` | `CK04` | `F01` |
| `RETURN` 退料货架 | `F01` | `CK04` | `F01` |
| `FIVE_LAYER` 五层货架(含 drain 货架) | `CTU01` | `CTU02` | `CTU03` |

- 模板只在这一张表里出现一次;三个场景函数都查它,不各写一份判断。
- **配对由构造保证**:离场与进场取自同一行,所以 `CTU01 → CTU03`、`F01 → F01` 不需要存储进场用过哪个模板,也不会在离场时换用另一组。

### 3.2 三个场景

对外是一个薄门面,持有 `TransportService`;每个场景返回 `TransportHandle`,在调用方事务内登记,不提交。

| 场景 | 方法 | 源 | 目标 | 说明 |
|---|---|---|---|---|
| 进场 | `enter_rack(session, *, client_request_id, caller, role, rack_id, target, target_face=None)` | `Rack(rack_id)` | `RackPosition` 或 `Zone` | 模板取表中 `enter`;`target_face` 可选,原样传递 |
| 换面 | `rotate_rack(session, *, client_request_id, caller, role, rack_id, target_face)` | `Rack(rack_id)` | 当前实际所在的精确位置 | 位置由能力层从可信状态补出;模板取 `rotate`;不得根据计划目标推断当前位置 |
| 离场 | `depart_rack(session, *, client_request_id, caller, role, rack_id, target, target_face=None)` | `Rack(rack_id)` | `RackPosition` 或 `Zone` | 模板取表中 `depart`;目标由调用方原样传入(如 WMS 的离场决定),本层不改写、不二次选址 |

目标位置统一收敛为两类:`RackPosition`(精确地码)或 `Zone`(库区),类型别名 `RackDestination = RackPosition | Zone`。进场与离场的区别只在取表中的哪一列模板,二者共用同一个内部实现(`_move(action, ...)`),不各写一份校验与调用;`Rack`(货架引用)只作源位置,不作目标。

通用约束(来自现场规则与合同):`rack_id` 与 `RACK.location_code` 一致(能力层已校验);面是不透明非空字符串,`"90"`、`"270"` 原样传递,不做数值转换或去空格。

### 3.3 CK04

能力层按场景限定模板:`move_rack` 只接受 `CTU01`、`CTU03`、`F01`,`rotate_rack` 只接受 `CTU02`、`CK04`(`CK04` 仅换面可用)。默认模板:`move_rack` 为 `F01`;`rotate_rack` **不设默认、必须显式传**(评审 2026-10-22:五层货架要 `CTU02`,转运/退料货架要 `CK04`,一个默认值只对部分角色成立,漏传会悄悄发出错误模板;本层每次按角色表显式传入)。比合同 §3.2「不校验场景组合」更严,合同文本需同步。合同 0.4.0 §4.1 的文本仍只列 `CTU01/CTU02/CTU03/F01`,现场规则里也写明它「仍属独立合同」——**WMS 若不接受会以 `422 REJECTED` 返回**,按既有逻辑成为终态拒绝。联调前须与 WMS 确认,合同文本需同步。

### 3.4 错误

沿用能力层异常:未知角色、目标类型不符 → `InvalidTransportRequest`;可信状态缺失 → `RackStateUnavailable`。不新增异常类型。

## 4. 测试(只固化现场规则里的长期合同)

- 模板表与现场规则逐格一致;配对:每个角色的进场与离场模板属于同一组(`CTU01`→`CTU03`、`F01`→`F01`)。
- 三个场景生成的请求:源是 `RACK`+货架号;进场与离场的目标只接受 `RackPosition` 或 `Zone`(传入 `Rack` 等其它类型被拒绝),原样透传;换面使用可信当前位置;面值原样。
- 未知角色被拒绝且零写入;重放同号请求返回原任务。

## 5. 后续阶段(依赖入站结果与状态,本文不实现)

| 阶段 | 内容 | 依赖 |
|---|---|---|
| B | 目标点滚动 `CTU01` 下发窗口:按物理货架的进场生命周期占名额,离场被接纳或进场明确未接纳后释放并补发 pending | 能读取提交状态(可靠消息的 DONE/FAILED);「进场失败且终位在目标点外」的释放条件还需入站结果 |
| C | 取消/拒绝后的业务重试(新 `client_request_id`,指数退避上限约 60 秒,无次数上限) | 入站终态(T3/T4) |
| D | 换面时机、`departure_decide` 请求、整架任务完成 | 入站结果、WMS 决定、PickingTask 状态 |

## 6. 决策记录

- 场景在前、角色在后(用户 2026-10-22 指定):对外按场景分三个入口,角色决定模板;映射表唯一。
- CK04 现在加入(用户指定),风险见 §3.3。
- 阶段 A 不含窗口、重试与任何依赖入站结果的逻辑。
- 本层只提供业务任务可调用的接口,不关心具体业务实现(用户 2026-10-22):角色表去掉上游单据字段,业务字段到角色的映射由调用方负责;drain 货架并入五层货架,不单列角色。
- 目标位置统一为 `RackPosition | Zone` 两类(用户 2026-10-22),进场与离场共用一个内部实现。
