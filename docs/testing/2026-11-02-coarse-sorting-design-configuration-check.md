# 粗分双机现场配置的设计核对

状态：2026-11-02，**只读现场查询与设计推演记录，不是完整业务或现场动作验收**。本次仅查询一次用户指定的 ECS Status，不登记/修改设备或工作线，不下发 ECS 命令，不提交 F01 任务，不发送模拟扫码/Result。

## 1. 输入、来源与核对范围

| 项目 | 本次依据 |
| --- | --- |
| ECS Status | 用户提供的 [GET http://10.24.209.32:8080/api/v1/device/status](http://10.24.209.32:8080/api/v1/device/status) |
| ECS origin | `http://10.24.209.32:8080`，路径与 origin 分开使用 |
| 两个出料单层货架工作位 | 用户本轮确认的 `CLN01`、`CLN02` |
| 工作线名称与设备分组 | 沿[既有双机方案](../superpowers/specs/2026-10-29-coarse-sorting-site-configuration-design.md)的 RS01/RS02 及角色映射，核对本次返回的实际设备编码 |
| 目标实体与状态 | [粗分完整实体设计](../superpowers/specs/2026-11-02-coarse-sorting-data-model-design.md) |
| F01 原任务失败后成功 | [结果更正设计](../superpowers/specs/2026-11-02-transport-result-correction-design.md)，仍未实施 |

查询使用项目现有 `ecs_status.query`，执行单次 GET；正常返回表明本次 HTTP200、JSON 内容类型、严格 Status 数据及清单内设备身份唯一性均通过现有查询模块校验。本次获取时间为 **2026-11-02 11:18:52 PST（America/Los_Angeles），即 19:18:52 UTC**。

以下只是该次查询的文档证据，不是设备状态缓存、观察表或持续监控。未读取实际 WES 管理数据库，因此不能据此认定下表绑定已在当前部署配置中保存。Status 不提供 CLN01/CLN02 的真实货架在位证据。

## 2. 实测设备清单与设计角色

| device_code | Status角色 | supported_commands | supported_events | WES目标业务角色 |
| --- | --- | --- | --- | --- |
| `RS-INPUT-ARM-01` | INPUT_ARM | MOVE_TO_NG | ESTOP_PRESSED | RS01 / INPUT_ARM |
| `RS-CONVEYOR-01` | PIPELINE | MOVE_FORWARD | ESTOP_PRESSED | RS01 / CONVEYOR |
| `RS-OUTPUT-ARM-01` | OUTPUT_ARM | PUT_TO_BIN | ESTOP_PRESSED | RS01 / OUTPUT_ARM |
| `RS-INPUT-ARM-02` | INPUT_ARM | MOVE_TO_NG | ESTOP_PRESSED | RS02 / INPUT_ARM |
| `RS-CONVEYOR-02` | PIPELINE | MOVE_FORWARD | ESTOP_PRESSED | RS02 / CONVEYOR |
| `RS-OUTPUT-ARM-02` | OUTPUT_ARM | PUT_TO_BIN | ESTOP_PRESSED | RS02 / OUTPUT_ARM |

六台设备本次均返回 `mode=AUTO`、`is_online=true`、`status=UNKNOWN`。接口可达、设备在线标记和运行态未知可以同时成立；不能将本次查询解释为设备 IDLE、可接受命令或某个动作已经完成。

### 实际发现

1. **三类命令名符合目标分工。** 两组设备分别声明 MOVE_TO_NG、MOVE_FORWARD、PUT_TO_BIN，足以核对设计中的角色职责；仅命令名不能证明参数/Result 严格附录及物理完成语义已被设备实现。
2. **输送线角色名需配置映射。** Status 返回 PIPELINE，WES 内部设计角色为 CONVEYOR。通过受信任的工作线 `device_bindings` 显式映射即可，不需要更名实际设备或更改三实体；业务不能直接把 Status.role 当成业务角色，也不按设备后缀分发。
3. **主动扫码事件不依赖Status清单。** 两个入料机械臂本次 supported_events 都只列 ESTOP_PRESSED，没有 SCAN_COMPLETED，作为原响应观察保留。用户已明确 SCAN_COMPLETED 是 ECS 主动上报，WES 沿既有 INPUT_ARM 绑定受理，无需为事件监控独立状态或创建虚拟设备；不要求补齐Status能力描述后才开展设计/受理。后续只需按授权联调核对实际推送和源编码，这次GET本身没有验证事件投递。
4. **运行态仍未知。** 本次不确认设备可执行状态，不清除任何 UNKNOWN/HELD，也不触发自动发送或恢复。

## 3. 可代入完整设计的双机配置

以下为使用实测设备编码和用户给定架位的**设计配置方案**，不是本次已写入的数据库配置：

| workline_code | business_code | INPUT_ARM | CONVEYOR | OUTPUT_ARM | OUTPUT_RACK |
| --- | --- | --- | --- | --- | --- |
| RS01 | coarse_sorting | RS-INPUT-ARM-01 | RS-CONVEYOR-01 | RS-OUTPUT-ARM-01 | CLN01 |
| RS02 | coarse_sorting | RS-INPUT-ARM-02 | RS-CONVEYOR-02 | RS-OUTPUT-ARM-02 | CLN02 |

六台设备共享同一个 ECS origin 不影响隔离。新动作选择原工作线的角色绑定，已有命令只按原 command_code/device_code 和冻结路由消费；不以 ECS 地址作为整台粗分机或单盘作业的唯一身份。

位置绑定只保存工作位编码。入场 target_face 由各自 WMS incoming_rack 返回，不从 CLN01/CLN02、设备后缀或工作位配置猜测。尚未取得 WMS 原架号、方向和方案时，不具备创建实际 F01 的条件。

2026-11-02 核对时，仓库首阶段仅声明 INPUT_ARM/CONVEYOR 与 MOVE_FORWARD。完整设计中的 OUTPUT_ARM、PUT_TO_BIN、MOVE_TO_NG、分配/报告及 F01/更正当时尚需分阶段实施。当前 WES 交付状态见[分阶段实施计划](../superpowers/plans/2026-11-06-coarse-sorting-staged-implementation.md)，本记录保留当时的只读观察。

## 4. 用此配置逐场景推演

本节中的 Job-A/Job-B、Preparation-A/B 仅为文档符号，不是已创建的数据库记录或真实任务。推演只检查设计约束的对应关系，不执行应用代码或对端协议。

| 场景 | 代入配置后的设计预期 | 核对结果与边界 |
| --- | --- | --- |
| 两线同时扫码，条码甚至时间相同 | 设备码分别为 RS-INPUT-ARM-01、RS-INPUT-ARM-02，原事件摘要及 Job 归属独立，验证沿各自原 workline_code | 设计可区分，不依赖Status事件清单；真实推送未在本次GET中验证 |
| 正常输送和入箱 | Job-A 选择 RS-CONVEYOR-01/RS-OUTPUT-ARM-01；Job-B 选择对应02设备，不由同一 origin 选设备 | 与实测命令分工对应；未实际发命令 |
| 同工作线多盘需要货架 | RS01/CLN01 只建立一份活动 Preparation，等待盘关联它 | 原活动唯一约束可表达，不需要逐盘建立搬运实体；未实测并发约束 |
| 两线同时补架 | 两个 Preparation 分别固定目标 CLN01/CLN02，各自独立 client_request_id/task_id/cycle_id，均为 F01入场 | 不产生全场共享的一个架位任务；方向分别来自原 WMS方案 |
| 代次编号恰好相同 | RS01/CLN01 的 generation=1 不等于 RS02/CLN02 的 generation=1，比较须含原工作线/工作位 | 三实体的作用域可表达；不能只按数字代次关联 |
| RS01换架，RS02正常出料 | 仅 RS01/CLN01 活动准备作业拦住该位置新入箱；RS02仍按自己的状态推进 | 符合多盘/多线并行约定，不用全局机器锁 |
| RS01入场任务却回调CLN02 | 与原冻结目标CLN01冲突，不成为原任务已应用成功，两个准备作业均不能冒领该回调 | 原任务/位置校验应阻断；未向实际回调入口发包 |
| Job-A入箱Result使用OUTPUT-ARM-02 | 与原命令设备不符，不消费为Job-A成功 | 原ECS身份关联应阻断，不按最新设备或后缀猜测 |
| F01入场失败后原任务成功更正 | 仍用原 Preparation-A/task_id，目标CLN01及原target_face一致；原失败保留，合法已应用成功才继续 | 与目标更正规则对应；当前代码不支持该更正，未宣称实测成功 |
| F01退场 | source沿原CLN01或CLN02在位依据，target沿原departure READY；不把另一台工作位当默认退场点 | 方案表达完整；WMS实际去向及RCS执行未验证 |
| 已放料、报告未收妥 | 原 Job 保留报告动作和原工作线/目标，下一盘及另一条线不额外受阻 | 无需料格锁/本地高度累计，数据一致性仍由WMS负责 |
| 结果唤醒同一coarse_sorting业务 | 可以扫描两条线，但每条记录按原引用独立消费，不因一次通知将另一线推进 | 与原业务级唤醒及两聚合扫描方案一致 |

## 5. 结论与剩余验证

**给定配置可以代入现有三实体、两套状态机和F01更正规则，无需增加机器专用实体、处理器或另一套运行时。** 六个实际设备编码与三类命令分工匹配，CLN01/CLN02 分别作为两条线的原目标工作位；共享ECS地址不破坏身份隔离。

本次实际通过的仅是单次Status查询及严格响应校验；表内其他结论属于设计对应和场景推演。仍缺：

- SCAN_COMPLETED实际推送、原INPUT_ARM源编码及重传身份稳定性的联调；不将Status能力清单缺项列为阻塞。
- 当前WES数据库的真实设备/角色/位置绑定，以及完整阶段声明的装配验证。
- WMS实际分配、原target_face、退场去向、F01接纳与结果更正合同的签认/交互。
- 原严格设备参数/Result语义、PostgreSQL隔离并发及现场物理动作验收。

本次不自动登记任何Status设备，不保存应用Status缓存，不修改代码、迁移或已有现场配置，不提交或部署。
