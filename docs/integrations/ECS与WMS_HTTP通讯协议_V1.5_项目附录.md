# ECS与WMS HTTP通讯协议 V1.5（项目业务附录·评审稿）

日期：2026-11-13。适用：WMS开发方与ECS开发方。业务依据为当前流程及XRAY V3.4、机械臂 V3.4设备协议。

本稿沿用《仓库执行系统（WES）第三方设备统一接口白皮书》V1.1的四个固定HTTP接口、公共包络、ACK、结果及错误语义；本项目动作和params/data业务字段在此细化，须双方评审冻结。


本版替代V1.4中的HTTP字段定义和示例。ECS上传WMS的事件与结果data保留reel_id（原料盘周期UUID）和本阶段的barcode；WMS下发命令params继续使用barcode + source_event_id，不要求回传reel_id。机械臂设备编号为XRAY-ARM01。CCD复核使用old_barcode/new_barcode，复核OK后的正常出料与搬运统一以新码作为barcode。设备TCP内的reelId/pkg/oldPkg/newPkg保持原约定；第13节中的TCP恢复示例仍使用这些设备字段。

WCS说明：当前项目没有独立WCS接口合同和实现，出料到位MATERIAL_ARRIVED统一上报WMS/WES；本文不新增独立WCS服务。若WCS承担调度，由上层系统内部协调，ECS当前仍按本协议对接WMS。

扫码失败且没有有效条码时，HTTP事件data.barcode为null。复核前的barcode使用实际进料条码。WMS对CCD双码复核通过后，从APPLY_VERIFY_DECISION（OK）开始，正常出料、到料、搬运和放料结果的barcode使用同一盘CCD实际读到且复核通过的新码。复核NG和免清点分支沿用进料条码。相同条码可能出现在不同周期，ECS通过source_event_id定位原事件并核对该阶段的barcode。

升级说明：旧HTTP回调保留原正文、event_id和时间并暂停待对账，不用同一event_id发送改名后的正文；新产生消息按本版发送。机械臂HELLO.device必须为XRAY-ARM01，设备编号由ECS约定校验，HELLO.version仍为3.4。


命名修订（2026-11-13，程序0.3.7）：ECS与WMS所有协议JSON字段统一snake_case，包括event_id、reel_id、scan_stage、scan_status、barcode、template_id、source_event_id、old_barcode、new_barcode、count_status、ready_for_pick、reason_code、reason_message、failure_stage、actual_qty、actual_target等。原有device_code、command_code、finish_time等不变。枚举值（如SCAN_COMPLETED、BYPASS_COUNT）保持原值。

范围例外：label.variables中的ID、PHHPN、SPKG、MMfrPN、VVendor、QQTYPC、Location、DDC、DateTime、LLC为已确认的标签模板绑定名称，必须原样保留；它们不是通信包络字段。XRAY与机械臂TCP继续使用原reelId/pkg/oldPkg/newPkg等命名，不随HTTP协议改名。

下发指令的params使用barcode + source_event_id，上传事件/结果data同时保留reel_id + barcode。首次升级时，已保存的旧驼峰HTTP回调保留原文、原事件号与时间并暂停待对账，不用同一事件号改写正文后发送；原已接纳命令的完全相同请求仍幂等响应，新命令严格按本版字段校验。普通V1.5消息重启后继续按原事件号补报。

## 1. 通信双方、接口及共同规则

| 方向 | 调用方 | 服务提供方 | 方法与路径 | 用途 |
| --- | --- | --- | --- | --- |
| WMS → ECS | WMS | ECS | POST /api/v1/device/command | 清点前判定、标签变量、复核判定、机器人搬运任务 |
| WMS → ECS | WMS | ECS | GET /api/v1/device/status | 查询单设备或全部设备状态 |
| ECS → WMS | ECS | WMS | POST /api/v1/callback/event | 原PKG、点料结果、新旧双码、正常出口到料、NG排出 |
| ECS → WMS | ECS | WMS | POST /api/v1/callback/result | 原WMS命令的物理终态，即ReportResult |

双方基础地址ECS_BASE_URL/WMS_BASE_URL由现场提供，本稿不固定IP和端口，不披露内部部署、存储或程序实现。

- Content-Type: application/json，正文UTF-8，最大256KiB。时间字段使用整数纪元毫秒，耗时整数毫秒。
- 命令顶层严格为device_code、command_code、task_type、priority、timeout、timestamp、params。业务输入必须在params，不新增顶层template_id/barcode等字段。
- 命令业务参数放在 `params`，事件与结果的业务数据放在 `data`。按 2026-11-13 修订，HTTP JSON 的公共包络和业务字段统一使用 `snake_case`。`label.variables` 内的模板绑定键保留原名，设备 TCP 字段沿用原协议。新版 HTTP 报文不混用旧字段名。
- 同步ACK只确认接纳/持久接收，不代表业务判定或物理完成；事件ACK中不得附带下一步指令或标签变量。WMS另行调用command接口发送判定/变量。
- 本项目不增加取消接口、私有认证字段或私有路径；网络与访问配置按双方部署合同确认。
- 生产事件is_debug省略或false，禁止true；ESTOP_PRESSED不发到WMS事件接口，设备安全及原命令恢复独立处理。

## 2. 业务闭环及完成边界

1. XRAY自行扫码，ECS上报进料PKG及预先固定template_id，等待WMS检查仓库发料及未使用记录。
2. WMS以APPLY_PKG_DECISION返回BYPASS_COUNT、COUNT_REQUIRED或NG；仅COUNT_REQUIRED分支返回本盘reel_thickness，ECS持久保存后随COUNT转发XRAY。免清点直接到正常出口，不点料、不贴标、不做本次CCD；需要清点才执行COUNT；明确NG直接排NG。
3. 需要清点的盘实际点料结束，ECS上报原清点许可命令终态，并独立上报XRAY_COUNT_COMPLETED，包含PKG、数量及固定template_id。
4. WMS按template_id的变量定义及业务结果，另发APPLY_COUNT_DECISION。OK时带完整十个变量；NG时要求XRAY排NG。
5. ECS转发变量及同盘已保存的reel_thickness，XRAY使用预先部署的本地模板打印，并按本盘厚度执行贴标。LABEL_RESULT形成原打印贴标命令的结果回调；随后XRAY自动CCD，ECS将旧PKG和实际新PKG一起上报WMS复核。
6. WMS另发APPLY_VERIFY_DECISION。OK正常出料，NG由XRAY自行排入NG位。
7. 正常出口实际到位、停稳、自动保持且可取后，ECS上报MATERIAL_ARRIVED，并完成对应正常出料命令；WMS只据一次到料事实派一次搬运，不因结果和事件各派一次。
8. WMS发PUT_TO_BIN / PUT_TO_RACK，ECS控制机械臂从固定正常出口取盘，放入指定单层料箱格位或五层料架单元格。实际放料且位置核对后回传原搬运命令结果。

### 2.1 模板与标签职责

- template_id是事先约定的固定配置；ECS向WMS上传，WMS据此取变量定义。本次固定值为XX123456789，与返回变量ID一致。
- ECS上传的固定template_id与WMS返回的label.variables.ID是同一模板标识，本次值均为XX123456789。WMS下发十个变量时包含ID，但不下发模板文件/路径，也不靠ID临时选择其他模板。ECS及XRAY均核对该值与原周期固定配置一致。
- 固定template_id在本盘周期中不改变；有未结盘时不静默替换模板或字段定义。物理模板和打印内容由XRAY本地生成。
- 外部事件收件ACK不是变量响应。ECS未收到完整有效变量前不打印、不正常放行，也不使用上一盘变量补缺。

### 2.2 异常处置闭环

| 触发 | ECS → WMS原事实/结果 | 设备下一步 | 实际排出后ECS → WMS |
| --- | --- | --- | --- |
| 进料最终扫码NG | SCAN_COMPLETED，scan_stage=ENTRY、scan_status=NG；barcode填实际已知条码，无码时为null | XRAY自主排NG，不等WMS指令；不COUNT、不BYPASS | NG_DISCHARGED，stage=ENTRY |
| 进料业务明确拒收 | WMS下APPLY_PKG_DECISION，decision=NG、reason必填 | ECS下ROUTE NG，不反复请求同一个拒收判定 | 原业务命令SUCCESS、decision=NG、discharged=true；另报NG_DISCHARGED，stage=ENTRY |
| COUNT已确认失败且动作已结束 | 原APPLY_PKG_DECISION命令FAILED；XRAY_COUNT_COMPLETED、count_status=NG，省略qty并填原因 | XRAY自主排NG，不等待APPLY_COUNT_DECISION | NG_DISCHARGED，stage=COUNT；原清点命令仍FAILED |
| PRINT_LABEL已确认打印或贴标失败且动作已结束 | 原APPLY_COUNT_DECISION命令FAILED，error_detail说明真实失败，data可附failure_stage | XRAY自主排NG，不自动补打、不继续正常CCD复核 | NG_DISCHARGED，stage=LABEL；原打印贴标命令仍FAILED |
| VERIFY读码NG或业务复核NG | SCAN_COMPLETED，scan_stage=VERIFY，带实际读码结果 | 读码NG仍由WMS发APPLY_VERIFY_DECISION NG；未回复则按业务等待重试规则处置 | 对应NG命令实际排出才SUCCESS；另报NG_DISCHARGED，stage=VERIFY |
| 三阶段WMS业务判定等待耗尽 | 保留原事件及等待状态，不编造Result | ECS向XRAY发送WMS_TIMEOUT报警，保持本盘，不自动排NG | 报警后拒绝该盘新WMS指令；实际人工清料后在ECS确认 |
| 设备失联、动作可能仍在运行、位置不明 | 原任务保持恢复/待核对，不先编造FAILED | 查询原任务、保持物理保护，不自动重做或排NG | 得到实际事实后补报，不按超时时刻伪造完成时间 |
| NG位满、堵塞或排NG失败 | 不报NG_DISCHARGED；有原排NG命令且已确认失败时报告其FAILED | 保持该盘并本地报警，人工处理或设备确认后再恢复 | 只有实际排出事实才报NG_DISCHARGED；不得改写先前失败命令为SUCCESS |

“自主排NG”属于XRAY在已冻结技术异常条件下执行的本地处置，不新增WMS命令，也不伪造设备动作id。原SCAN_RESULT/RESULT/LABEL_RESULT须在自主排NG前由XRAY持久记录，随后NG_DISCHARGED也须持久记录，断线后分别补报。ECS应先还原前置事实再处理NG事实；乱序时暂存待关联，不丢弃事实，也不把未知条码补成上一盘。

设备只有在曝光/运动已安全结束、料盘可识别、排料路径和NG容量满足时执行排NG。无法满足则保持并报警；不要求在设备故障仍未解除时强行移动。XRAY报警由XRAY自行清除，无ECS清除报警信号。

## 3. 关联字段、幂等及多盘并行

| 字段 | 位置及生成方 | 含义/规则 |
| --- | --- | --- |
| command_code | WMS命令顶层，WMS生成 | 全局唯一命令身份，建议UUID；结果必须回原值。重提不换号，不与设备任务id混用 |
| reel_id | 事件/结果data，ECS沿用设备生成值 | 一次料盘处理周期UUID；仅上传WMS保留，WMS下发仍使用barcode + source_event_id |
| barcode | 业务params/data，本阶段有效条码 | 复核通过前、复核NG及免清点使用实读进料条码。复核OK放行及后续正常流程使用该盘已复核的实读新码。命令按source_event_id定位原事件并核对条码 |
| event_id | 事件data，ECS生成并持久保存 | 一次北向事实事件唯一号；同一设备事实重报映射同一event_id、原timestamp及原data，不新建第二件业务 |
| source_event_id | 命令params，WMS回填 | 原事件data.event_id。复核OK命令的barcode对照原VERIFY.new_barcode，搬运命令的barcode对照原MATERIAL_ARRIVED.barcode |
| template_id | 进料和点料事件data，ECS固定配置 | 预先约定模板ID，不是文件路径；由ECS上传WMS，WMS在打印变量ID中回传同值，不能临时改变 |
| old_barcode / new_barcode | CCD复核事件data | 同一消息的旧条码和实际读到新条码；正常复核均必填，不能要求WMS自行查历史补齐旧码 |

event_id、command_code建议UUIDv4、36字符、不带大括号。barcode使用本阶段实读条码字符串。source_event_id原样复制，不解析编号内容。ACK/查询不创建新的物理业务身份。

A盘复核时B盘进料，两盘分别用barcode、event_id和command_code处理，允许消息交错。状态查询仅诊断，不能以XRAY整机RUNNING或有另一个未终态命令就阻止WMS发另一盘请求；ECS在接纳时按逐盘阶段、物理工位、共享路径/打印/曝光资源及容量原子裁决。机械臂首版单任务，忙时明确未接纳，不隐式无限排队。

### 3.1 事件重报稳定性

同一真实事实固定event_id、timestamp和业务data（包括barcode、数量、条码、template_id、原因）。首次生成后持久保存，网络重试、WMS重试判定、ECS重启不得重建身份或刷新时间。相同身份改内容返回409并停止自动处理；不能换event_id绕过冲突。扫码事件与实际排NG事件是两个不同事实，各有自己的固定event_id和真实发生时刻。

## 4. 公共包络字段

### 4.1 Command：WMS → ECS

POST <ECS_BASE_URL>/api/v1/device/command

| 字段 | 中文含义 | 类型 | 必填 | 可选值/规则 |
| --- | --- | --- | --- | --- |
| device_code | 目标逻辑设备 | string | 是 | XRAY01 / XRAY-ARM01 |
| command_code | 原命令唯一号 | string | 是 | WMS生成，全局唯一，建议UUID |
| task_type | 业务动作 | string | 是 | APPLY_PKG_DECISION / APPLY_COUNT_DECISION / APPLY_VERIFY_DECISION / PUT_TO_BIN / PUT_TO_RACK |
| priority | 优先级 | integer | 是 | 本轮固定1 |
| timeout | 期望完成预算 | integer，ms | 是 | >0，各分支建议见第10节；不是超时自动取消/重做许可 |
| timestamp | 命令创建时间 | integer，纪元ms | 是 | 原命令重提不更新 |
| params | 业务参数 | object | 是 | 每动作见第6节，不允许null/数组/字符串代替 |

确定接纳必须同时满足HTTP 200、Content-Type为application/json、JSON对象code为整数200、message="ACK"。可选trace_id非null string仅诊断；其他未识别顶层字段按原白皮书忽略，不从中读取业务许可。接纳ACK只表示ECS已承担本条任务，不代表设备已经完成。

```json
{
  "code": 200,
  "message": "ACK"
}
```

**通信方向：ECS → WMS，以上为command的同步响应正文；不是结果回调。**

### 4.2 Event Callback：ECS → WMS

POST <WMS_BASE_URL>/api/v1/callback/event

| 字段 | 中文含义 | 类型 | 必填 | 可选值/规则 |
| --- | --- | --- | --- | --- |
| device_code | 事实所属设备 | string | 是 | 本项目这些事件均XRAY01 |
| event_type | 事件类型 | string | 是 | SCAN_COMPLETED / XRAY_COUNT_COMPLETED / MATERIAL_ARRIVED / NG_DISCHARGED |
| timestamp | 实际事实发生时间 | integer，纪元ms | 是 | 从设备原事实映射，重报不刷新，不是网络发送时间 |
| is_debug | 联调开关 | boolean | 否 | 生产省略或false，不填null，不以true驱动正式业务 |
| data | 业务事件数据 | object | 本附录要求是 | 按第5节，不移到顶层；不能null |

成功收件WMS必须先持久保存，再返回HTTP200及上述code/message ACK。它不是库存已更新、复核OK或下一步已开始的证明。

### 4.3 Result Callback / ReportResult：ECS → WMS

POST <WMS_BASE_URL>/api/v1/callback/result

| 字段 | 中文含义 | 类型 | 必填 | 可选值/规则 |
| --- | --- | --- | --- | --- |
| command_code | 被完成的原WMS命令 | string | 是 | 必须与原command_code完全相同 |
| device_code | 原执行逻辑设备 | string | 是 | 必须与原命令设备一致 |
| result | 物理动作终态 | string | 是 | SUCCESS / FAILED，不能填OK、NG、TIMEOUT或LABEL_RESULT |
| finish_time | 实际完成时间 | integer，纪元ms | 是 | 原设备实际终态时间，补报不变 |
| data | 业务结果 | object | 本附录要求是 | 与动作分支匹配，见第7节；不能null |
| error_detail | 失败描述 | object或null | FAILED时必填 | FAILED仅含code、msg两个非空string；SUCCESS省略或null |

LABEL_RESULT是XRAY设备消息type，不是本HTTP结果顶层枚举。WMS用原command_code识别是哪条打印/搬运命令；LABEL_RESULT成功映射result=SUCCESS。业务判定NG但要求的排NG动作成功时，本命令result也为SUCCESS，data仍标decision=NG，不能把它当合格入库。

### 4.4 Status：WMS → ECS

GET <ECS_BASE_URL>/api/v1/device/status[?device_code=XRAY01]

唯一可选查询参数是device_code。不传返回两个设备；指定时必须且只返回该设备一项；未知设备HTTP404。成功顶层仅devices数组，每项仅device和state对象。

| 对象/字段 | 类型 | 必填/空值 | 规则 |
| --- | --- | --- | --- |
| device.device_code | string | 必填非null | XRAY01/XRAY-ARM01 |
| device.device_name | string或null | 必填，可null | 设备名称，诊断展示 |
| device.device_type | string或null | 必填，可null | 设备类型，诊断展示 |
| device.role | string或null | 必填，可null | 设备角色，诊断展示 |
| device.supported_commands | string[]或null | 必填，可null | 声明当前合同命令，XRAY三类判定，ARM搬运 |
| device.supported_events | string[]或null | 必填，可null | 当前设备事件声明 |
| state.device_code | string | 必填非null | 与同项device一致 |
| state.mode | string | 必填非null | AUTO/MANUAL/MAINTENANCE/UNKNOWN |
| state.status | string | 必填非null | IDLE/RUNNING/ERROR/PAUSED/STOPPED/OFFLINE/UNKNOWN |
| state.is_online | boolean | 必填非null | ECS在线判断，不等于可立即执行 |
| state.current_command_code | string或null | 必填 | 无活动命令null；XRAY多命令时选最早接纳且未终态的诊断主命令，不代表完整任务清单 |
| state.scenario | string或null | 必填，可null | 仅诊断，不塞入整份逐盘JSON或作为物理许可 |
| state.updated_at | integer，纪元ms | 必填非null | 状态更新时间 |

北向不添加reels/tasks/resources等新顶层数组。多盘用事件/原结果关联；WMS不拿单一current_command_code或状态快照代替接纳互斥，不需要先查IDLE才发送。ECS实际执行许可必须在接纳时重新核对。

## 5. 每类事件的独立业务字段表

以下每张表的字段均在data内，公共包络仍按4.2节。正常事件按第3节使用本阶段有效条码。通过reel_id区分处理周期，通过event_id区分事件；barcode不作为单独的唯一身份。

### 5.1 SCAN_COMPLETED，scan_stage=ENTRY：进料PKG检查

**方向：ECS → WMS。** 触发：XRAY最终进料扫码结果。

| data字段 | 中文含义 | 类型 | 必填条件 | 可选值/范围 |
| --- | --- | --- | --- | --- |
| event_id | 本次事实号 | string | 是 | 唯一UUID，补报不变 |
| reel_id | 料盘处理周期ID | string | 是 | 原设备料盘UUID，同一次处理周期保持不变 |
| barcode | 原进料条码 | string或null | 是 | 成功时为实际进料条码；最终扫码失败且无码时为null |
| scan_stage | 扫码阶段 | string | 是 | ENTRY |
| scan_status | 设备读码状态 | string | 是 | OK/NG，不代表业务放行 |
| template_id | 固定模板ID | string | 是 | 双方预先确认的配置值 |
| reason_code | 读码错误码 | string | NG必填 | 非空稳定码，如SCAN_NO_READ/CCD_NO_READ |
| reason_message | 读码失败说明 | string | NG必填 | 非空文本 |

WMS对OK检查仓库发料及未使用记录，另发APPLY_PKG_DECISION。扫码NG不自动生成COUNT/BYPASS，不把缺码当满盘；XRAY自主排NG，并以独立NG_DISCHARGED报告实际排出。

### 5.2 XRAY_COUNT_COMPLETED：点料结果与模板ID

**方向：ECS → WMS。** 触发：原COUNT设备任务实际成功或已确认失败终态。

| data字段 | 中文含义 | 类型 | 必填条件 | 可选值/范围 |
| --- | --- | --- | --- | --- |
| event_id | 本次事实事件号 | string | 是 | UUID，补报不变 |
| reel_id | 料盘处理周期ID | string | 是 | 原设备料盘UUID，同一次处理周期保持不变 |
| barcode | 本盘处理身份 | string | 是 | 原周期非空进料条码，不随消息类型改变 |
| template_id | 固定模板ID | string | 是 | 同该周期配置，WMS据此获取变量定义 |
| count_status | 点料技术结果 | string | 是 | OK/NG；与下一步WMS业务判定区分 |
| qty | 实际元件数量 | integer | OK必填，NG省略 | >=0，单位固定PCS；0须为真实测量结果 |
| reason_code | 点料错误码 | string | NG必填 | 非空稳定码，如COUNT_FAILED |
| reason_message | 点料失败说明 | string | NG必填 | 非空文本，不能用0冒充失败原因 |

WMS仅对有效OK点料事实生成正常标签业务判定。原清点许可命令Result与此事件分别幂等处理，不能各登记一次数量、各派一次打印。

### 5.3 SCAN_COMPLETED，scan_stage=VERIFY：CCD新旧双码复核

**方向：ECS → WMS。** 触发：XRAY贴标后自动CCD完成。

| data字段 | 中文含义 | 类型 | 必填条件 | 可选值/范围 |
| --- | --- | --- | --- | --- |
| event_id | 本次事实事件号 | string | 是 | 原事件UUID，补报不变 |
| reel_id | 料盘处理周期ID | string | 是 | 原设备料盘UUID，同一次处理周期保持不变 |
| barcode | 本盘处理身份 | string | 是 | 原周期非空进料条码 |
| scan_stage | 扫码阶段 | string | 是 | VERIFY |
| scan_status | 实际CCD读码结果 | string | 是 | OK/NG，不等于WMS复核OK |
| old_barcode | 同盘进料旧码 | string | 是 | 与原ENTRY.barcode一致 |
| new_barcode | CCD实际新码 | string | OK必填，NG无读码省略 | 非空实际读取值，不用待打印variables.SPKG冒充 |
| template_id | 本周期模板追溯 | string | 可选 | 如填写，必须为原固定ID |
| reason_code | 读码错误码 | string | NG必填 | 非空稳定码，如SCAN_NO_READ/CCD_NO_READ |
| reason_message | 读码失败说明 | string | NG必填 | 非空文本 |

正常复核须两个条码同时存在于同一事件，不能拆成两条后依赖“最后条码”拼接。无读码NG是异常通知，不是正常双码校验；WMS不得给无有效新码的盘正常OUTLET许可。

### 5.4 MATERIAL_ARRIVED：正常出口到料

**方向：ECS → WMS。** 触发：XRAY的OUTLET_ARRIVED，该设备消息同时结束原正常出料命令。

| data字段 | 中文含义 | 类型 | 必填条件 | 可选值/范围 |
| --- | --- | --- | --- | --- |
| event_id | 本次事实事件号 | string | 是 | 稳定UUID |
| reel_id | 料盘处理周期ID | string | 是 | 原设备料盘UUID，同一次处理周期保持不变 |
| barcode | 本盘有效条码 | string | 是 | COUNTED使用已复核的实读新码，BYPASS使用实读进料条码 |
| mode | 处理路径 | string | 是 | BYPASS/COUNTED |
| ready_for_pick | 已满足到料条件 | boolean | 是 | 固定true，实际到位、停稳、保持且可取才发送 |
| new_barcode | 已确认标签新码 | string | COUNTED正常分支必填；BYPASS省略 | COUNTED时与barcode相等，均为已复核的CCD实读新码 |

WMS依据该事件安排一次PUT_TO_BIN / PUT_TO_RACK；持续占位只补报原事件，不新建业务。mode=BYPASS不代表执行过CCD，不上报虚构数量或复核OK。历史到位不能代替机械臂实时安全许可。

### 5.5 NG_DISCHARGED：XRAY实际排NG

**方向：ECS → WMS。** 触发：XRAY已确认实际排入NG位。

| data字段 | 中文含义 | 类型 | 必填条件 | 可选值/范围 |
| --- | --- | --- | --- | --- |
| event_id | 本次事实事件号 | string | 是 | 稳定UUID |
| reel_id | 料盘处理周期ID | string | 是 | 原设备料盘UUID，同一次处理周期保持不变 |
| barcode | 原进料条码 | string或null | 是 | 原进料条码；最终扫码失败且无码时为null |
| stage | NG所属阶段 | string | 是 | ENTRY/COUNT/LABEL/VERIFY |
| reason_code | 排NG原因码 | string | 可选，建议填 | SCAN_FAILED / BUSINESS_REJECTED / COUNT_FAILED / LABEL_FAILED等稳定码；WMS_DECISION_TIMEOUT仅兼容旧实际排出记录 |
| reason | 排NG原因 | string | 是 | 非空文本，业务/技术原因区分 |

不创建机械臂NG搬运任务，不发送正常MATERIAL_ARRIVED。不把NG事件ACK当作合格入库或库存已扣记。reason_code用于机器分类，reason仍必填，不依赖解析中文判定。扫码、清点、打印贴标失败与排NG分别是独立事实；实际处置和上报顺序按2.2节执行，原失败命令不因排NG成功改成SUCCESS。

## 6. 每种WMS命令的独立params表

所有命令均使用4.1节公共包络，业务字段只在params。

### 6.1 APPLY_PKG_DECISION：清点前判定

**方向：WMS → ECS，device_code=XRAY01。** 针对ENTRY成功事件。

| params字段 | 中文含义 | 类型 | 必填 | 可选值/规则 |
| --- | --- | --- | --- | --- |
| barcode | 原料盘周期 | string | 是 | 复制原事件barcode |
| source_event_id | 被判定事件 | string | 是 | 原ENTRY事件event_id |
| decision | 清点前业务判定 | string | 是 | COUNT_REQUIRED / BYPASS_COUNT / NG |
| reel_thickness | 本盘厚度 | number | COUNT_REQUIRED必填；BYPASS_COUNT/NG省略 | >0，毫米，可为15.3等小数；必须在XRAY已确认能力范围内 |
| reason | 业务拒收原因 | string | decision=NG时必填，其余省略 | 非空文本 |

COUNT_REQUIRED只在实际点料结束时报告该命令终态；数量通过独立XRAY_COUNT_COMPLETED供后续决策。BYPASS_COUNT只在正常出口真实到位时成功，跳过本次点料、贴标和CCD。NG明确业务拒收不重复请求同一判定，由ECS下发ROUTE NG；实际排出后原command_code回SUCCESS，data包含barcode、decision=NG、discharged=true，完成边界及字段见7.4节；另上报5.5节NG_DISCHARGED。接纳ACK不表示已排出。

厚度来自本次进料事件对应的WMS判定，按barcode+source_event_id关联；不属于variables标签变量。COUNT_REQUIRED缺少、非数值、非正数或超设备能力范围时HTTP422 ANNEX_VALIDATION_FAILED明确未接纳，不先驱动再猜厚度。NG拒收不要求厚度，以免阻塞排NG。

SPKG不存在等明确业务拒收使用decision=NG、reason非空；临时无法判定按8.3节等待重试规则执行，不默认COUNT。

### 6.2 APPLY_COUNT_DECISION：点料业务判定与标签变量

**方向：WMS → ECS，device_code=XRAY01。** 针对有效点料事件。

| params字段 | 中文含义 | 类型 | 必填条件 | 可选值/规则 |
| --- | --- | --- | --- | --- |
| barcode | 原料盘周期 | string | 是 | 原点料事件barcode |
| source_event_id | 被判定点料事实 | string | 是 | 原点料事件event_id |
| decision | 业务判定 | string | 是 | OK/NG，不是技术动作结果 |
| label | 标签输入 | object | OK必填；NG省略 | 仅variables，包含固定模板标识ID，不另加template_id或模板路径 |
| label.variables | 本盘完整变量 | object | OK必填 | 十个业务变量，全部string |
| reason | 业务拒绝原因 | string | NG必填 | 非空文本 |

| label.variables键 | 中文含义 | 类型 | 必填/范围 |
| --- | --- | --- | --- |
| ID | 模板ID | string | 必填，固定XX123456789，必须等于该事件template_id |
| PHHPN | 鸿海/客户料号 | string | 必填非空，示例100-0277-000HF |
| SPKG | 新标签PKG | string | 必填非空，示例SXCN0031482261000009NVD，不覆盖进料旧barcode |
| MMfrPN | 制造商料号 | string | 必填非空，示例PZ2012S601AT000 |
| VVendor | 供应商编号 | string | 必填非空，示例M000002870，保留原内容 |
| QQTYPC | 标签元件数量 | string | 必填非空数字字符串，示例"4000"，单位PCS |
| Location | 标签显示库位 | string | 键必填，允许""，独立于物理位置字段 `location_id` |
| DDC | 日期码 | string | 必填非空，示例2623，按业务编码原样填写 |
| DateTime | 标签显示日期时间 | string | 必填非空，格式yyyy-MM-dd HH:mm:ss，示例2026-10-29 14:43:50 |
| LLC | 批次码 | string | 必填非空，示例1T3330022677，保留字母/前导零 |

PRINT_LABEL另携本盘已冻结的数值reel_thickness，由ECS从6.1保存的同盘判定取得；WMS此处不必重复发送，不能放入label.variables。缺少可恢复的同盘厚度时不驱动贴标，保持核对。

十个键大小写固定，全部string；Location可空，其余不得为空。ID是固定模板标识，不是命令command_code/id，也不是料盘barcode。最大长度由实际模板冻结，不自动截断。QQTYPC由WMS业务输出；若业务要求与实测qty一致，需明确规则，不允许ECS偷偷改写WMS值。


OK分支完成边界是实际贴标完成（设备LABEL_RESULT），不是CCD读码成功或WMS复核通过。NG分支完成边界是XRAY实际排入NG位。

### 6.3 APPLY_VERIFY_DECISION：CCD双码复核判定

**方向：WMS → ECS，device_code=XRAY01。** 针对VERIFY事件。

| params字段 | 中文含义 | 类型 | 必填条件 | 可选值/规则 |
| --- | --- | --- | --- | --- |
| barcode | 本盘有效条码 | string | 是 | OK取原VERIFY事件的new_barcode，NG取原VERIFY事件的barcode |
| source_event_id | 被判定复核事实 | string | 是 | 原VERIFY事件event_id |
| decision | 双码业务判定 | string | 是 | OK/NG |
| reason | 复核不通过原因 | string | NG必填 | 非空文本 |

OK待正常出口真实到位才成功；NG待实际排NG才成功。没有合法双码、旧码不符、前置贴标/点料事实不完整、盘已被其他流程消费时，不接纳正常出料许可。

### 6.4 PUT_TO_BIN / PUT_TO_RACK：从正常出口取盘并放入指定目标

**方向：WMS → ECS，device_code=XRAY-ARM01。** 针对正常MATERIAL_ARRIVED事件。两条命令均包含固定源位取一盘及目标放一盘；不是要求机械臂预先持盘的“仅放料”动作。不再下发旧PICK_AND_PUT。

| params字段 | 中文含义 | 类型 | 必填条件 |
| --- | --- | --- | --- |
| barcode | 待搬料盘的有效条码 | string | 必填，原到料事件barcode。COUNTED为已复核的实读新码，BYPASS为实读进料条码 |
| source_event_id | 本次到料事件 | string | 必填，原MATERIAL_ARRIVED.event_id |
| target | 目标位置，五层架另含本盘尺寸 | object | 必填，按下表区分 |

params不再单独包含reel对象。target内部按本版snake_case字段命名，公共包络和barcode/source_event_id不变。WMS按目标类型选择task_type；ECS按同一类型生成设备命令，不根据可疑字段猜分支。

#### 6.4.1 PUT_TO_BIN字段

| target字段 | 中文含义 | 类型 | 必填条件 | 规则 |
| --- | --- | --- | --- | --- |
| location_id | 目标对接站 | string | 必填 | 已配置站点，示例为STATION_B |
| location_type | 目标位置类型 | string | 必填 | 固定ONE_LAYER_RACK，与PUT_TO_BIN对应 |
| bin_type | 料箱类型 | string | 必填 | 固定字符串"1" |
| bin_location | 本次选中的箱位 | string | 必填 | 按本次任务取A、B、C、D之一，重发沿用原箱位 |
| bin_cell_location | 箱内放料位编号 | integer | 必填 | 固定JSON整数8 |

单层料箱由人工管理。WMS命令省略rack_id、bin_id、reel_layer、reel_thickness、reel_diameter、reel_totalthickness。ECS按站点及箱位映射定位，执行前核对实物就绪与安全条件。所需控制参数由ECS依据已标定的设备配置或现场检测取得，条件无法确认时按未接纳规则处理。

#### 6.4.2 PUT_TO_RACK字段

| target字段 | 中文含义 | 类型 | 必填条件 | 规则 |
| --- | --- | --- | --- | --- |
| location_id | 目标对接站 | string | 必填 | 已配置站点，示例STATION_A |
| location_type | 目标类型 | string | 必填 | 固定RETURN_RACK，必须与PUT_TO_RACK对应 |
| rack_id | 目标货架编号 | string | 必填 | 非空且唯一匹配，如RETURN-RACK-01 |
| rack_face | 货架面 | string | 必填 | A / B |
| slot_id | 储位编号 | string | 必填 | 非空，按现场储位表，如A-03 |
| rack_layer | 货架层 | integer | 必填 | 1～5 |
| rack_column | 货架格/列 | integer | 必填 | >=1，且不超过本货架标定范围 |
| reel_thickness | 本次料盘厚度 | number | 必填 | >0，毫米，如15.3 |
| reel_diameter | 本次料盘直径 | number | 必填 | 7 / 13 / 15，英寸 |

slot_id与rack_face/rack_layer/rack_column须同时匹配同一储位映射，不按字符串格式猜坐标。示例slot_id=A-03、A面2层3列是同一个现场映射示例，不把A-03强行解读成第1层；若实际点位表不一致则明确拒绝，不自行优先选某个字段。RACK任务不发送料箱bin_*或已有堆叠reel_layer/reel_totalthickness。

#### 6.4.3 唯一定位及实际结果

PUT_TO_BIN按location_id、location_type和bin_location定位站点箱位。PUT_TO_RACK须提供rack_id，按location_id、location_type和rack_id唯一匹配已配置料架，再核对架面和储位。ECS在接纳前校验对应分支的必填字段和位置映射。

actual_target只回传实际位置身份字段：

- PUT_TO_BIN回传location_id、location_type、bin_type、bin_location、bin_cell_location。
- PUT_TO_RACK：location_id、location_type、rack_id、rack_face、slot_id、rack_layer、rack_column。

actual_target仅包含对应分支的实际位置字段。单层箱位回传实际放入的A、B、C或D。五层架回传实际rack_id和储位。实际位置不同于原目标时保留真实位置和诊断，按原任务失败或对账规则处理。失败且实际位置未知时省略actual_target。

目标源位仍固定，一条任务固定一盘，不增加source或qty。target.location_id是本次目标站点；不得按旧版规则省略该字段。新增命令、站点、料箱/储位映射、格号及尺寸必须按双方同一配置校验。

WMS指令接纳后HTTP200 ACK只表示接纳；实际取放结束后才Result。实际源位未就绪、抓手持盘、忙碌及安全许可不满足仍按原503/429规则处理，结构/类型/映射不合法按HTTP422 ANNEX_VALIDATION_FAILED明确未接纳。

## 7. 按动作区分的结果data字段

公共Result包络按4.3节，每条结果都含data.barcode。下面是不同命令分支的独立字段定义。

| 原命令及分支 | data必填字段 | 可选值/意义 | 物理完成证据 |
| --- | --- | --- | --- |
| APPLY_PKG_DECISION COUNT_REQUIRED成功 | barcode、decision=COUNT_REQUIRED、qty | qty为实际PCS整数≥0 | 原COUNT设备任务成功终态 |
| APPLY_PKG_DECISION BYPASS_COUNT成功 | barcode、decision=BYPASS_COUNT、mode=BYPASS | 不含虚构qty/CCD复核值 | XRAY OUTLET_ARRIVED |
| APPLY_PKG_DECISION NG成功 | barcode、decision=NG、discharged=true | 进料业务拒收已实际排出，非业务合格 | 已确认实际排NG |
| APPLY_COUNT_DECISION OK成功 | barcode、decision=OK、label_status=APPLIED | 只证明贴标已完成 | XRAY LABEL_RESULT SUCCESS |
| APPLY_COUNT_DECISION NG成功 | barcode、decision=NG、discharged=true | 排NG动作成功，业务仍NG | 已确认实际排出 |
| APPLY_VERIFY_DECISION OK成功 | barcode、decision=OK、mode=COUNTED | 到位后等待WMS搬运 | XRAY OUTLET_ARRIVED |
| APPLY_VERIFY_DECISION NG成功 | barcode、decision=NG、discharged=true | 不是合格入库 | 已确认实际排NG |
| PUT_TO_BIN / PUT_TO_RACK成功 | barcode、actual_qty=1、position=TARGET、actual_target | actual_qty单位盘；actual_target仅含6.4.3定义的实际位置身份字段，不含reel_*上下文 | 机械臂实际放料且目标核对一致 |

所有失败Result.data至少包含原命令的barcode；有可靠位置时附position=SOURCE/GRIPPER/TARGET/OTHER/UNKNOWN及可选failure_stage。搬运失败建议actual_qty=0仅在可证实未放入预期目标时填写；不确定数量不虚填0。顶层FAILED必须error_detail={code,msg}。

原设备COUNT/PRINT_LABEL等id和LABEL_RESULT type不强制对WMS暴露；WMS依command_code、barcode与event_id关联即可。finish_time使用原物理终态时刻，不因发送延迟更新。

### 7.1 APPLY_PKG_DECISION：COUNT_REQUIRED成功

**通信方向：ECS → WMS；接口：POST /api/v1/callback/result。** 下表字段全部位于data，公共顶层按4.3节。

| data字段 | 类型 | 必填条件 | 可选值/含义 |
| --- | --- | --- | --- |
| reel_id | string | 是 | 原设备料盘UUID，同一次处理周期保持不变 |
| barcode | string | 是 | 原周期非空进料条码 |
| decision | string | 是 | COUNT_REQUIRED |
| qty | integer | 是 | 实际元件数量>=0，PCS |

### 7.2 APPLY_PKG_DECISION：BYPASS_COUNT成功

**通信方向：ECS → WMS；接口：POST /api/v1/callback/result。** 下表字段全部位于data，公共顶层按4.3节。

| data字段 | 类型 | 必填条件 | 可选值/含义 |
| --- | --- | --- | --- |
| reel_id | string | 是 | 原设备料盘UUID，同一次处理周期保持不变 |
| barcode | string | 是 | 原周期非空进料条码 |
| decision | string | 是 | BYPASS_COUNT |
| mode | string | 是 | BYPASS，未执行本次点料/贴标/CCD |

### 7.3 APPLY_COUNT_DECISION：打印贴标成功

**通信方向：ECS → WMS；接口：POST /api/v1/callback/result。** 下表字段全部位于data，公共顶层按4.3节。

| data字段 | 类型 | 必填条件 | 可选值/含义 |
| --- | --- | --- | --- |
| reel_id | string | 是 | 原设备料盘UUID，同一次处理周期保持不变 |
| barcode | string | 是 | 原周期非空进料条码 |
| decision | string | 是 | OK |
| label_status | string | 是 | APPLIED，实际已贴标，不代表复核通过 |

### 7.4 APPLY_PKG_DECISION / APPLY_COUNT_DECISION / APPLY_VERIFY_DECISION：业务NG且排出成功

**通信方向：ECS → WMS；接口：POST /api/v1/callback/result。** 下表字段全部位于data，公共顶层按4.3节。

| data字段 | 类型 | 必填条件 | 可选值/含义 |
| --- | --- | --- | --- |
| reel_id | string | 是 | 原设备料盘UUID，同一次处理周期保持不变 |
| barcode | string | 是 | 原周期非空进料条码 |
| decision | string | 是 | NG，业务仍不合格 |
| discharged | boolean | 是 | 固定true，表示该原命令要求的排NG已实际完成；不是仅收到命令或准备排出 |

discharged仅用于对应原WMS排NG命令的成功完成结果。未实际排出不能发送该SUCCESS；已确认失败按7.7节FAILED，未知保持恢复。独立NG_DISCHARGED描述同一次排出事实，两种回调各自幂等，不能重复记账。自主技术异常排NG没有原WMS排NG命令时，不编造command_code或Result；业务等待耗尽仅报警，不生成排NG结果或排出事件。

### 7.5 APPLY_VERIFY_DECISION：正常出料成功

**通信方向：ECS → WMS；接口：POST /api/v1/callback/result。** 下表字段全部位于data，公共顶层按4.3节。

| data字段 | 类型 | 必填条件 | 可选值/含义 |
| --- | --- | --- | --- |
| reel_id | string | 是 | 原设备料盘UUID，同一次处理周期保持不变 |
| barcode | string | 是 | 原出料许可命令中的已复核实读新码 |
| decision | string | 是 | OK |
| mode | string | 是 | COUNTED，实际正常出口到位 |

### 7.6 PUT_TO_BIN / PUT_TO_RACK：实际放料成功

**通信方向：ECS → WMS；接口：POST /api/v1/callback/result。** 下表字段全部位于data，公共顶层按4.3节。

| data字段 | 类型 | 必填条件 | 可选值/含义 |
| --- | --- | --- | --- |
| reel_id | string | 是 | 原设备料盘UUID，同一次处理周期保持不变 |
| barcode | string | 是 | 原搬运命令的barcode。COUNTED为已复核的实读新码，BYPASS为实读进料条码 |
| actual_qty | integer | 是 | 固定1，单位盘，不是元件数量 |
| position | string | 是 | TARGET |
| actual_target | object | 是 | 真实已核实位置，子字段及条件按6.4.3，只报实际位置身份，不回填已有高度/层数，不得抄期望目标冒充 |

### 7.7 任意动作已确认失败

**通信方向：ECS → WMS；接口：POST /api/v1/callback/result。** 下表字段全部位于data，公共顶层按4.3节。

| data字段 | 类型 | 必填条件 | 可选值/含义 |
| --- | --- | --- | --- |
| reel_id | string | 是 | 原设备料盘UUID，同一次处理周期保持不变 |
| barcode | string | 是 | 原命令的barcode。正常放行和搬运失败也沿用原命令冻结的条码 |
| position | string | 可选，物料位置可信时提供 | SOURCE/GRIPPER/TARGET/OTHER/UNKNOWN |
| failure_stage | string | 可选 | COUNT/TEMPLATE/PRINT/LABEL/TRANSPORT/PICK/MOVE/PUT等对应真实环节 |
| actual_qty | integer | 搬运失败且能证明未完成目标放料时可附 | 0，未知时省略，不虚报 |
| actual_target | object | 失败但真实位置已知时可附 | 位置身份模型同6.4.3，与期望位置不同须保留实际证据 |

FAILED的error_detail是结果顶层条件必填字段，独立如下：

| 字段 | 类型 | 必填条件 | 可选值/含义 |
| --- | --- | --- | --- |
| error_detail.code | string | FAILED必填 | 稳定错误码，如COUNT_FAILED、LABEL_FAILED、PICK_FAILED、PUT_FAILED |
| error_detail.msg | string | FAILED必填 | 非空可核实错误说明 |

SUCCESS时error_detail省略或null，不允许在error_detail中增加其他键。未知物理结果保持恢复/对账，不虚构FAILED回调。

清点、打印贴标失败后的实际处置按2.2节执行：原动作保持FAILED，后续实际排NG另报5.5节事件；未知不按失败或自动排NG处理。

## 8. 幂等、异常和错误响应

### 8.1 WMS命令交付

| HTTP状态 | message | 是否接纳及处理 |
| --- | --- | --- |
| 200 | ACK | 确定接纳后等待原command_code结果，不持续重送命令 |
| 400 | INVALID_ENVELOPE | 未接纳，包络错误 |
| 404 | DEVICE_NOT_FOUND | 未接纳，目标不存在 |
| 405 | METHOD_NOT_ALLOWED | 未接纳，方法错误 |
| 409 | IDEMPOTENCY_CONFLICT | 同号不同载荷，停止自动发送并核对，不换身份绕过 |
| 413 | PAYLOAD_TOO_LARGE | 未接纳，超过256KiB |
| 422 | ANNEX_VALIDATION_FAILED | 未接纳，业务字段、事件关联、阶段或目标不合法 |
| 429 | CAPACITY_EXCEEDED | 明确未接纳；只有同时有有效Retry-After时按原身份原载荷延后重提 |
| 500/502/504 | TEMPORARILY_UNAVAILABLE | 交付结果未知，进入对账，不自动重放 |
| 503 | TEMPORARILY_UNAVAILABLE | 明确未接纳，可原身份原载荷延后重提 |

成功或错误响应code均为整数并与HTTP状态对应，message为稳定文本；不能用{"code":"200","message":"OK"}代替确定ACK。429的Retry-After初值建议2秒，不把设备毫秒字段直接当HTTP秒值。

同command_code相同完整原载荷，ECS返回原接纳事实、不重复驱动；同号改变timestamp、timeout、变量等任何合同载荷都返回409。不同command_code指向已消费的同盘同阶段/同到料事实，ECS也须按业务与资源校验阻止重复动作。

### 8.2 ECS回调交付

| HTTP状态/code | message | ECS处理 |
| --- | --- | --- |
| 200 | ACK | 确定持久收件，停止原消息补报；不等于库存已记账 |
| 400 | INVALID_ENVELOPE | WMS返回error_detail.issues[]，按拒收问题修正；不得改写已被接收的原事实绕过去重 |
| 404 | COMMAND_NOT_FOUND | 仅Result，WMS不认识原command_code，不推进业务，核对记录 |
| 409 | IDEMPOTENCY_CONFLICT | 停止自动重报并对账，不换event_id或command_code |
| 409 | RESULT_BEFORE_DISPATCH | 仅Result，原命令未下发即收到结果，按白皮书封锁并核对，禁止自动重报/重做 |
| 413 | PAYLOAD_TOO_LARGE | 修正过大载荷后按正确身份处理，不拆成多个业务动作 |
| 503 | TEMPORARILY_UNAVAILABLE | 尚未持久接收，保留原载荷延后补报 |

400的issues[]每项field、code必填，expected可选；错误码INVALID_JSON/FIELD_REQUIRED/INVALID_TYPE/EXTRA_FORBIDDEN/INVALID_VALUE，不能回显原始载荷或未处理异常。其他表内回调错误不带error_detail，按白皮书执行。

未收到确定200 ACK的回调可按原身份、原timestamp/finish_time及data重报；若200但message不为ACK或正文非法，不当作已接收。已持久接收相同回调仍200 ACK且仅业务处理一次。不同命令恢复结果矛盾时留存对账，不按HTTP先后、时间戳大小或新身份覆盖。

### 8.3 WMS业务判定重试：三次无判定后仅报警（2026-11-06修订）

| 等待阶段 | 原业务事件 | 后续业务指令 |
| --- | --- | --- |
| PKG_DECISION | SCAN_COMPLETED，ENTRY且OK | APPLY_PKG_DECISION |
| COUNT_DECISION | XRAY_COUNT_COMPLETED，count_status=OK | APPLY_COUNT_DECISION |
| VERIFY_DECISION | SCAN_COMPLETED，VERIFY | APPLY_VERIFY_DECISION |

每盘每阶段持久保存原event_id、正文、等待起点、已发次数和请求窗口结束点。正常在t=0、5、10秒发送原事件，总共3次（含首次），每轮等待5000ms。t=15秒仍未持久接纳有效判定时，标记窗口耗尽、封锁该盘后续新WMS指令，并向XRAY生成WMS_TIMEOUT通知其报警并保持本盘。保持原WAIT阶段，等待人工实际清料及ECS人工确认，不创建ROUTE NG或虚构Result。

HTTP200 ACK仅表示WMS持久收件，不等于业务判定；已经ACK的原事件仍可在5秒和10秒按原身份重报，WMS应回原业务指令而非新建重复动作。三轮结束后不再启动新的业务请求轮次；如果原事实尚未获得确定HTTP ACK，可靠回调队列仍保存并补报同一正文，此类送达补报不重置窗口、不再次生成报警、不驱动物料。结果回调不受三次业务轮数限制，但受8.4节送达暂停及次数上限约束。

三轮耗尽并报警后，该barcode的后续新COUNT_REQUIRED/BYPASS_COUNT/标签变量/复核OK或NG指令均返回HTTP422 ANNEX_VALIDATION_FAILED，不再驱动物料。已接纳原command_code的相同载荷重试仍幂等ACK，不再次执行；同号改载荷仍409。该封锁只针对该盘，其他料盘仍按各自状态和资源校验。

400/404/409/413/422等确定合同错误保持BLOCKED待对账，不当作普通无回复耗尽；不自动排料。明确业务NG不反复请求，仍按原规则执行ROUTE NG；清点/打印贴标已确认失败后的XRAY本地技术排NG规则独立保留。

WMS_TIMEOUT每盘每等待阶段只创建一个ID，未获XRAY RECEIVED时原样补报；重连/重启保持原ID、时间和业务内容。人工清料确认完成后停止该盘通知补报，不发送清除报警命令。XRAY不得因该通知自主排NG或认为物料已到位。停机期间计入原等待，重启不重新给三轮，也不突发补齐错过的轮次。

### 8.4 回调送达暂停及人工恢复（2026-11-08）

为避免持续刷请求，HTTP401/403立即暂停该回调；其他未获确定收件ACK的送达失败，默认每轮最多发送10次（含首次），达到上限暂停，次数可由ECS配置。暂停优先于自动发送，包括原业务事件重问；已获收件ACK的正常三轮业务重问不受送达次数上限限制。原400/404/409/413/422合同错误保持BLOCKED核对，不由恢复按钮绕过。

操作员可在ECS回调队列逐条暂停或恢复。暂停及原因持久保存，重启不自动恢复；不删除事件、不伪造WMS已收件，不取消设备任务。恢复沿用原event_id、timestamp及完整正文，累计发送次数保留，重新给予一轮送达预算；不重置15秒业务截止、不解除超时料盘封锁。消息发送中须等响应/超时后操作。对历史重报WMS仍按event_id去重；三轮耗尽后未确认事实仅在未暂停且送达预算未耗尽时自动补报。以上规则同样适用于result回调，暂停不等于结果丢弃。

## 9. 多盘及故障边界

- A盘WAIT_VERIFY时B盘ENTRY/COUNT可并行，前提真实工位与共享资源允许。ECS不把另一盘变量、新码或判定填到当前盘。
- 等WMS变量、清点前判定或复核期间，按本盘保留位置，不因HTTP ACK默认放行；8.3节三轮业务等待耗尽后仅通知XRAY报警并保持，报警后拒绝该盘后续新业务判定，等待人工清料确认。
- 设备失联、结果未知、抓手持盘或工位身份不明时保留物理保护，原命令待恢复/对账，不能换command_code再执行一次。
- NG盘由XRAY自行排入NG位，WMS不派机械臂NG搬运；满料/堵塞不能回成功。
- 事件/结果可能乱序到达。WMS按barcode、source_event_id和command_code处理证据，若下一步尚未达到ECS可接纳条件可能返回明确未接纳；不假设先收到某条HTTP响应才会收到另一消息。
- 标签DateTime是打印业务字符串，和JSON timestamp/finish_time不同；不可由ECS擅自改为当前时间。actual_qty是搬运盘数，qty/QQTYPC是元件数量，不能混用。

### 9.1 接纳后重启恢复

ECS在回复WMS接纳ACK前，须持久保存完整原请求、command_code、barcode、source_event_id、对应设备任务id及动作内容、资源占用和接纳状态。设备在ACK ACCEPTED前须持久保存原任务及接纳事实。

| 重启窗口 | 恢复要求 |
| --- | --- |
| WMS命令接纳已持久保存，但可确定尚未发送设备 | 重新核验原料盘/资源后，允许使用原设备任务id继续发送；不新建id |
| 设备下发可能已发生，或设备已接纳但ECS未收到ACK | 按原设备id发QUERY_TASK，同时核对STATUS.reels/tasks；禁止盲目重发物理动作 |
| 查询ACCEPTED/RUNNING | 恢复原任务跟踪及资源占用 |
| 查询已终态 | 设备返回完整原RESULT/LABEL_RESULT/OUTLET_ARRIVED，ECS按原WMS关联生成或找回原结果 |
| 查询UNKNOWN/NOT_FOUND，或记录关联不一致 | 保持物料与资源保护，进入对账；NOT_FOUND不证明任务从未执行 |

恢复必须核对所有未结料盘和任务，不能只恢复最后一盘。持久状态损坏不得清空后当作新任务运行。物理动作未知时不以WMS timeout、心跳恢复或系统重启伪造动作FAILED/SUCCESS。

### 9.2 物理完成至WMS ACK之间重启

设备在给出完成事实前持久保存原实际结果，包括原设备任务id、设备侧reelId、time和完整业务数据；原事实未获ECS收件确认则补报。ECS将终态、命令完成关联及待发送Result/事件作为一致的持久提交，提交成功才确认设备收件。HTTP上传同时保留reel_id和本阶段的barcode。设备原始事实仍保留原TCP字段。

ECS在WMS回调ACK前重启，继续补报原command_code的原Result，不新建finish_time或修改data/error_detail。WMS已收件但ACK丢失时仍对相同回调200 ACK，只应用业务一次。ECS尚未保存设备事实时，通过设备原任务查询/补报恢复；双方记录均缺失则对账，不假定成功。

### 9.3 保留与清理

| 记录 | 最低保留规则 |
| --- | --- |
| 未结任务、UNKNOWN、待对账、仍在机/持有/位置未知的料盘 | 不自动过期；直至事实确认并闭环 |
| 未获对方确定ACK的事件、Result、设备终态补报 | 不自动过期，不因达到发送次数或90天丢弃 |
| 已闭环命令、设备任务终态、事件映射、回调及其ACK/去重记录 | 在线至少90天，自最后业务闭环且相关回调获ACK的较晚时刻计算 |
| 归档记录 | 可归档，但必须一起保留command_code、设备id、barcode、event_id、原请求/结果、实际时间与ACK证据的关联，归档期限由双方运维另行冻结 |

ECS、WMS、XRAY及机械臂均遵守各自记录的上述最低恢复/去重窗口。依赖方尚有未结关联或恢复未确认时，不单独清掉设备终态。清理需联动核对，不仅按创建日期删除；最长停机恢复和补报周期大于90天时，相应延长保留。

超出在线窗口的旧身份只能查档/对账，不当作全新任务自动执行。设备QUERY_TASK.NOT_FOUND和已归档事实仍遵守禁止自动重做规则；WMS重放已过期关联的新命令应明确未接纳并核对原业务。不得通过清理重置活动命令、业务等待次数或源事件消费标记。

## 10. 时间预算建议

下列均为联调初值，按真实设备配方、路径及WMS节拍确认，不是真机实测承诺。

| 项目 | 建议值 | 说明 |
| --- | --- | --- |
| HTTP命令ACK等待 | 3000ms | 未获确定ACK按交付未知处理，不直接重做 |
| 回调单次请求 | 5000ms | 未获确定ACK保留原载荷 |
| 回调重报退避 | 1、2、5、10、30秒封顶 | 409等阻断错误不自动套用重试 |
| 三种业务等待：SPKG/变量/复核 | 总共3次请求，每轮5000ms，总截止15000ms | 按8.3节执行，三轮耗尽报警并封锁，报警后拒绝该盘后续新业务判定，等待人工清料确认；已接纳动作退出此等待 |
| APPLY_PKG_DECISION COUNT_REQUIRED timeout | 70000ms | 南向COUNT初值60000加余量 |
| APPLY_PKG_DECISION BYPASS_COUNT timeout | 25000ms | 到实际正常出口 |
| APPLY_COUNT_DECISION OK timeout | 40000ms | 到实际贴标完成，不等待后续WMS复核 |
| APPLY_COUNT_DECISION NG timeout | 20000ms | 到实际排NG |
| APPLY_VERIFY_DECISION OK/NG timeout | 25000/20000ms | 正常到位/实际排NG |
| PUT_TO_BIN / PUT_TO_RACK timeout | 90000ms | 覆盖实际机器人取放初值60000及编排/通信余量，无额外软件锁定释放 |
| 状态诊断查询 | 建议1000～5000ms | 不作为WMS发任务前物理授权，避免以高频轮询替代事件 |

北向timeout小于已冻结南向动作预算时，发送前修正或明确未接纳，不能接纳后改原载荷重放。任何预算到期都不直接证明物理动作已停止；实际失败终态须设备确认。

## 11. JSON示例

每个例子明确标注通信方向与接口。多例为字段展示，非按排列顺序可全部执行的真实会话。UUID、SPKG、模板ID及目标位置均为示意，正式值需现场约定。


### 11.1 进料PKG及固定模板ID

**通信方向：ECS → WMS。接口：`POST /api/v1/callback/event`。**

```json
{
  "device_code": "XRAY01",
  "event_type": "SCAN_COMPLETED",
  "timestamp": 1793413800000,
  "data": {
    "event_id": "1b2daf25-09b1-4734-ab69-57f40524e3be",
    "reel_id": "18520712-e29b-4197-b145-1aefa4c14cca",
    "barcode": "PKG-OLD-A",
    "scan_stage": "ENTRY",
    "scan_status": "OK",
    "template_id": "XX123456789"
  }
}
```


### 11.2 WMS许可本盘清点

**通信方向：WMS → ECS。接口：`POST /api/v1/device/command`。**

```json
{
  "device_code": "XRAY01",
  "command_code": "46381f71-3e91-40cb-848d-8f79abdd112a",
  "task_type": "APPLY_PKG_DECISION",
  "priority": 1,
  "timeout": 70000,
  "timestamp": 1793413801000,
  "params": {
    "barcode": "PKG-OLD-A",
    "source_event_id": "1b2daf25-09b1-4734-ab69-57f40524e3be",
    "decision": "COUNT_REQUIRED",
    "reel_thickness": 15.3
  }
}
```


### 11.3 ECS命令接纳ACK

**通信方向：ECS → WMS。接口：`HTTP 200 response /api/v1/device/command`。**

仅确认接纳，不能据此认定点料完成；其他command也使用同样ACK。

```json
{
  "code": 200,
  "message": "ACK"
}
```


### 11.4 清点许可命令实际完成

**通信方向：ECS → WMS。接口：`POST /api/v1/callback/result`。**

```json
{
  "command_code": "46381f71-3e91-40cb-848d-8f79abdd112a",
  "device_code": "XRAY01",
  "result": "SUCCESS",
  "finish_time": 1793413808000,
  "data": {
    "reel_id": "18520712-e29b-4197-b145-1aefa4c14cca",
    "barcode": "PKG-OLD-A",
    "decision": "COUNT_REQUIRED",
    "qty": 4000
  }
}
```


### 11.5 点料结果及固定模板ID事件

**通信方向：ECS → WMS。接口：`POST /api/v1/callback/event`。**

11.4完成原命令，11.5驱动点料后的业务判定，不能各派一次贴标。

```json
{
  "device_code": "XRAY01",
  "event_type": "XRAY_COUNT_COMPLETED",
  "timestamp": 1793413808000,
  "data": {
    "event_id": "5ad0f02e-e98d-456d-bb52-dff0d8706865",
    "reel_id": "18520712-e29b-4197-b145-1aefa4c14cca",
    "barcode": "PKG-OLD-A",
    "template_id": "XX123456789",
    "count_status": "OK",
    "qty": 4000
  }
}
```


### 11.6 WMS返回标签变量，ECS才转发XRAY打印

**通信方向：WMS → ECS。接口：`POST /api/v1/device/command`。**

变量ID回传同一固定模板标识，不另加template_id选择指令，不下发模板路径或文件。PRINT_LABEL/LABEL_RESULT属于设备接口，不替换本HTTP包络。

```json
{
  "device_code": "XRAY01",
  "command_code": "fd59e7c9-1a8a-4728-991d-0a9e3b71516f",
  "task_type": "APPLY_COUNT_DECISION",
  "priority": 1,
  "timeout": 40000,
  "timestamp": 1793413809000,
  "params": {
    "barcode": "PKG-OLD-A",
    "source_event_id": "5ad0f02e-e98d-456d-bb52-dff0d8706865",
    "decision": "OK",
    "label": {
      "variables": {
        "ID": "XX123456789",
        "PHHPN": "100-0277-000HF",
        "SPKG": "SXCN0031482261000009NVD",
        "MMfrPN": "PZ2012S601AT000",
        "VVendor": "M000002870",
        "QQTYPC": "4000",
        "Location": "",
        "DDC": "2623",
        "DateTime": "2026-10-29 14:43:50",
        "LLC": "1T3330022677"
      }
    }
  }
}
```


### 11.7 实际贴标完成结果

**通信方向：ECS → WMS。接口：`POST /api/v1/callback/result`。**

由设备LABEL_RESULT映射，随后自动CCD，尚未表示WMS双码复核通过。

```json
{
  "command_code": "fd59e7c9-1a8a-4728-991d-0a9e3b71516f",
  "device_code": "XRAY01",
  "result": "SUCCESS",
  "finish_time": 1793413813000,
  "data": {
    "reel_id": "18520712-e29b-4197-b145-1aefa4c14cca",
    "barcode": "PKG-OLD-A",
    "decision": "OK",
    "label_status": "APPLIED"
  }
}
```


### 11.8 同一消息上传旧码和实际新码

**通信方向：ECS → WMS。接口：`POST /api/v1/callback/event`。**

```json
{
  "device_code": "XRAY01",
  "event_type": "SCAN_COMPLETED",
  "timestamp": 1793413815000,
  "data": {
    "event_id": "daf9008f-8d39-4b8f-9d53-3c0d2e7e3ce1",
    "reel_id": "18520712-e29b-4197-b145-1aefa4c14cca",
    "barcode": "PKG-OLD-A",
    "scan_stage": "VERIFY",
    "scan_status": "OK",
    "old_barcode": "PKG-OLD-A",
    "new_barcode": "SXCN0031482261000009NVD",
    "template_id": "XX123456789"
  }
}
```


### 11.9 WMS复核OK，许可正常出料

**通信方向：WMS → ECS。接口：`POST /api/v1/device/command`。**

```json
{
  "device_code": "XRAY01",
  "command_code": "5ea6b228-ec3c-4e59-a45e-055b627fba4c",
  "task_type": "APPLY_VERIFY_DECISION",
  "priority": 1,
  "timeout": 25000,
  "timestamp": 1793413816000,
  "params": {
    "barcode": "SXCN0031482261000009NVD",
    "source_event_id": "daf9008f-8d39-4b8f-9d53-3c0d2e7e3ce1",
    "decision": "OK"
  }
}
```


### 11.10 实际到正常出口，原出料命令成功

**通信方向：ECS → WMS。接口：`POST /api/v1/callback/result`。**

设备只发一次OUTLET_ARRIVED事实，ECS分别形成原命令结果和一次MATERIAL_ARRIVED事件。

```json
{
  "command_code": "5ea6b228-ec3c-4e59-a45e-055b627fba4c",
  "device_code": "XRAY01",
  "result": "SUCCESS",
  "finish_time": 1793413818000,
  "data": {
    "reel_id": "18520712-e29b-4197-b145-1aefa4c14cca",
    "barcode": "SXCN0031482261000009NVD",
    "decision": "OK",
    "mode": "COUNTED"
  }
}
```


### 11.11 正常到料事件，WMS据此派一次搬运

**通信方向：ECS → WMS。接口：`POST /api/v1/callback/event`。**

```json
{
  "device_code": "XRAY01",
  "event_type": "MATERIAL_ARRIVED",
  "timestamp": 1793413818000,
  "data": {
    "event_id": "b9cf825f-2fcf-4f4e-b5d1-4275808dc832",
    "reel_id": "18520712-e29b-4197-b145-1aefa4c14cca",
    "barcode": "SXCN0031482261000009NVD",
    "mode": "COUNTED",
    "ready_for_pick": true,
    "new_barcode": "SXCN0031482261000009NVD"
  }
}
```


### 11.12 单层料箱搬运任务

**通信方向：WMS → ECS。接口：`POST /api/v1/device/command`。**

```json
{
  "device_code": "XRAY-ARM01",
  "command_code": "dd30a926-d460-4b72-b048-7c3ac80858d8",
  "task_type": "PUT_TO_BIN",
  "priority": 1,
  "timeout": 90000,
  "timestamp": 1793413819000,
  "params": {
    "barcode": "SXCN0031482261000009NVD",
    "source_event_id": "b9cf825f-2fcf-4f4e-b5d1-4275808dc832",
    "target": {
      "location_id": "STATION_B",
      "location_type": "ONE_LAYER_RACK",
      "bin_type": "1",
      "bin_location": "D",
      "bin_cell_location": 8
    }
  }
}
```


### 11.13 实际放料完成ReportResult

**通信方向：ECS → WMS。接口：`POST /api/v1/callback/result`。**

```json
{
  "command_code": "dd30a926-d460-4b72-b048-7c3ac80858d8",
  "device_code": "XRAY-ARM01",
  "result": "SUCCESS",
  "finish_time": 1793413828000,
  "data": {
    "reel_id": "18520712-e29b-4197-b145-1aefa4c14cca",
    "barcode": "SXCN0031482261000009NVD",
    "actual_qty": 1,
    "position": "TARGET",
    "actual_target": {
      "location_id": "STATION_B",
      "location_type": "ONE_LAYER_RACK",
      "bin_type": "1",
      "bin_location": "D",
      "bin_cell_location": 8
    }
  }
}
```


### 11.14 五层料架任务格式（替代11.12，不同时下发）

**通信方向：WMS → ECS。接口：`POST /api/v1/device/command`。**

同一盘同到料不能同时下单层和五层任务；此例只展示另一目标结构。

```json
{
  "device_code": "XRAY-ARM01",
  "command_code": "6a79c7eb-d52d-4227-b476-6bd7ad00dbe5",
  "task_type": "PUT_TO_RACK",
  "priority": 1,
  "timeout": 90000,
  "timestamp": 1793413819000,
  "params": {
    "barcode": "SXCN0031482261000009NVD",
    "source_event_id": "b9cf825f-2fcf-4f4e-b5d1-4275808dc832",
    "target": {
      "location_id": "STATION_A",
      "location_type": "RETURN_RACK",
      "rack_id": "RETURN-RACK-01",
      "rack_face": "A",
      "slot_id": "A-03",
      "rack_layer": 2,
      "rack_column": 3,
      "reel_thickness": 15.3,
      "reel_diameter": 7
    }
  }
}
```


### 11.15 另一盘B进料，可与A复核并行

**通信方向：ECS → WMS。接口：`POST /api/v1/callback/event`。**

```json
{
  "device_code": "XRAY01",
  "event_type": "SCAN_COMPLETED",
  "timestamp": 1793413815100,
  "data": {
    "event_id": "765f35e6-5650-49fd-b027-6e917a5893ac",
    "reel_id": "1342c68c-4574-45a9-babf-878d2e735301",
    "barcode": "PKG-OLD-B",
    "scan_stage": "ENTRY",
    "scan_status": "OK",
    "template_id": "XX123456789"
  }
}
```


### 11.16 WMS按未使用满盘记录许可B免清点

**通信方向：WMS → ECS。接口：`POST /api/v1/device/command`。**

```json
{
  "device_code": "XRAY01",
  "command_code": "67db4473-429b-4259-9f43-d1536375fa68",
  "task_type": "APPLY_PKG_DECISION",
  "priority": 1,
  "timeout": 25000,
  "timestamp": 1793413817000,
  "params": {
    "barcode": "PKG-OLD-B",
    "source_event_id": "765f35e6-5650-49fd-b027-6e917a5893ac",
    "decision": "BYPASS_COUNT"
  }
}
```


### 11.17 B免清点实际到出口后原任务成功

**通信方向：ECS → WMS。接口：`POST /api/v1/callback/result`。**

```json
{
  "command_code": "67db4473-429b-4259-9f43-d1536375fa68",
  "device_code": "XRAY01",
  "result": "SUCCESS",
  "finish_time": 1793413829000,
  "data": {
    "reel_id": "1342c68c-4574-45a9-babf-878d2e735301",
    "barcode": "PKG-OLD-B",
    "decision": "BYPASS_COUNT",
    "mode": "BYPASS"
  }
}
```


### 11.18 B免清点正常到料，不构造数量和CCD值

**通信方向：ECS → WMS。接口：`POST /api/v1/callback/event`。**

```json
{
  "device_code": "XRAY01",
  "event_type": "MATERIAL_ARRIVED",
  "timestamp": 1793413829000,
  "data": {
    "event_id": "f7d8497f-e237-462f-8430-ac78d44de73e",
    "reel_id": "1342c68c-4574-45a9-babf-878d2e735301",
    "barcode": "PKG-OLD-B",
    "mode": "BYPASS",
    "ready_for_pick": true
  }
}
```


### 11.19 复核NG分支（替代11.9，不同时下发）

**通信方向：WMS → ECS。接口：`POST /api/v1/device/command`。**

```json
{
  "device_code": "XRAY01",
  "command_code": "6024c2f9-acfa-4a2b-97bc-ac32fea8b6a9",
  "task_type": "APPLY_VERIFY_DECISION",
  "priority": 1,
  "timeout": 20000,
  "timestamp": 1793413816000,
  "params": {
    "barcode": "PKG-OLD-A",
    "source_event_id": "daf9008f-8d39-4b8f-9d53-3c0d2e7e3ce1",
    "decision": "NG",
    "reason": "新旧PKG业务关系不匹配"
  }
}
```


### 11.20 实际排NG成功，业务判定仍NG

**通信方向：ECS → WMS。接口：`POST /api/v1/callback/result`。**

```json
{
  "command_code": "6024c2f9-acfa-4a2b-97bc-ac32fea8b6a9",
  "device_code": "XRAY01",
  "result": "SUCCESS",
  "finish_time": 1793413818000,
  "data": {
    "reel_id": "18520712-e29b-4197-b145-1aefa4c14cca",
    "barcode": "PKG-OLD-A",
    "decision": "NG",
    "discharged": true
  }
}
```


### 11.21 实际排NG事实，无机械臂任务

**通信方向：ECS → WMS。接口：`POST /api/v1/callback/event`。**

```json
{
  "device_code": "XRAY01",
  "event_type": "NG_DISCHARGED",
  "timestamp": 1793413818000,
  "data": {
    "event_id": "53cb2274-48eb-44d2-9818-95045e0a684d",
    "reel_id": "18520712-e29b-4197-b145-1aefa4c14cca",
    "barcode": "PKG-OLD-A",
    "stage": "VERIFY",
    "reason": "新旧PKG业务关系不匹配"
  }
}
```


### 11.22 点料业务NG（替代11.6）

**通信方向：WMS → ECS。接口：`POST /api/v1/device/command`。**

不携带label，不正常打印，实际排NG后回原结果。

```json
{
  "device_code": "XRAY01",
  "command_code": "e899a491-bf61-4e92-bf2a-ed42045bb232",
  "task_type": "APPLY_COUNT_DECISION",
  "priority": 1,
  "timeout": 20000,
  "timestamp": 1793413809000,
  "params": {
    "barcode": "PKG-OLD-A",
    "source_event_id": "5ad0f02e-e98d-456d-bb52-dff0d8706865",
    "decision": "NG",
    "reason": "点料业务检查不通过"
  }
}
```


### 11.23 已确认打印贴标失败

**通信方向：ECS → WMS。接口：`POST /api/v1/callback/result`。**

这是11.7的失败替代示例，不能对同一真实任务先报成功再报失败。

```json
{
  "command_code": "fd59e7c9-1a8a-4728-991d-0a9e3b71516f",
  "device_code": "XRAY01",
  "result": "FAILED",
  "finish_time": 1793413813000,
  "data": {
    "reel_id": "18520712-e29b-4197-b145-1aefa4c14cca",
    "barcode": "PKG-OLD-A",
    "decision": "OK",
    "label_status": "NOT_APPLIED",
    "failure_stage": "LABEL"
  },
  "error_detail": {
    "code": "LABEL_FAILED",
    "msg": "打印完成但贴标失败，未进入正常复核"
  }
}
```


### 11.24 CCD无读码异常，省略new_barcode

**通信方向：ECS → WMS。接口：`POST /api/v1/callback/event`。**

不进入正常双码复核OK；独立异常示例，其他C盘前序略。

```json
{
  "device_code": "XRAY01",
  "event_type": "SCAN_COMPLETED",
  "timestamp": 1793413830000,
  "data": {
    "event_id": "36d762ce-edad-43ab-94db-e96333f6b0a1",
    "reel_id": "aa320965-7d9d-40f5-975a-0a4beabde511",
    "barcode": "PKG-OLD-C",
    "scan_stage": "VERIFY",
    "scan_status": "NG",
    "old_barcode": "PKG-OLD-C",
    "reason_code": "CCD_NO_READ",
    "reason_message": "CCD未读到新标签条码"
  }
}
```


### 11.25 搬运已确认取料失败结果

**通信方向：ECS → WMS。接口：`POST /api/v1/callback/result`。**

示意另一个原已下发搬运命令的失败结果，不可无命令先报。

```json
{
  "command_code": "d8cf7cfe-032e-4d25-8cc4-584bc530aeb6",
  "device_code": "XRAY-ARM01",
  "result": "FAILED",
  "finish_time": 1793413831000,
  "data": {
    "reel_id": "aa320965-7d9d-40f5-975a-0a4beabde511",
    "barcode": "PKG-OLD-C",
    "actual_qty": 0,
    "position": "SOURCE",
    "failure_stage": "PICK"
  },
  "error_detail": {
    "code": "PICK_FAILED",
    "msg": "已确认未取走且原任务结束"
  }
}
```


### 11.26 WMS对事件/结果持久收件ACK

**通信方向：WMS → ECS。接口：`HTTP 200 response /api/v1/callback/event 或 /api/v1/callback/result`。**

不是标签变量响应，不附下一步业务许可。

```json
{
  "code": 200,
  "message": "ACK"
}
```


### 11.27 容量满，明确未接纳

**通信方向：ECS → WMS。接口：`HTTP 429 response /api/v1/device/command`。**

响应同时包含HTTP头Retry-After: 2。允许重提时保持原command_code及全部载荷。

```json
{
  "code": 429,
  "message": "CAPACITY_EXCEEDED"
}
```


### 11.28 设备不就绪，明确未接纳

**通信方向：ECS → WMS。接口：`HTTP 503 response /api/v1/device/command`。**

```json
{
  "code": 503,
  "message": "TEMPORARILY_UNAVAILABLE"
}
```


### 11.29 同号改载荷，停止并核对

**通信方向：ECS → WMS。接口：`HTTP 409 response /api/v1/device/command`。**

```json
{
  "code": 409,
  "message": "IDEMPOTENCY_CONFLICT"
}
```


### 11.30 WMS拒收不合法回调

**通信方向：WMS → ECS。接口：`HTTP 400 response /api/v1/callback/event`。**

```json
{
  "code": 400,
  "message": "INVALID_ENVELOPE",
  "error_detail": {
    "issues": [
      {
        "field": "data",
        "code": "INVALID_TYPE",
        "expected": "object"
      }
    ]
  }
}
```


### 11.31 WMS拒收相同身份的矛盾结果

**通信方向：WMS → ECS。接口：`HTTP 409 response /api/v1/callback/result`。**

```json
{
  "code": 409,
  "message": "IDEMPOTENCY_CONFLICT"
}
```


### 11.32 查询单台状态成功响应

**通信方向：ECS → WMS。接口：`HTTP 200 response GET /api/v1/device/status?device_code=XRAY01`。**

```json
{
  "devices": [
    {
      "device": {
        "device_code": "XRAY01",
        "device_name": "XRAY点料机",
        "device_type": "XRAY_COUNTER",
        "role": "COUNT_STATION",
        "supported_commands": [
          "APPLY_PKG_DECISION",
          "APPLY_COUNT_DECISION",
          "APPLY_VERIFY_DECISION"
        ],
        "supported_events": [
          "SCAN_COMPLETED",
          "XRAY_COUNT_COMPLETED",
          "MATERIAL_ARRIVED",
          "NG_DISCHARGED"
        ]
      },
      "state": {
        "device_code": "XRAY01",
        "mode": "AUTO",
        "status": "RUNNING",
        "is_online": true,
        "current_command_code": "5ea6b228-ec3c-4e59-a45e-055b627fba4c",
        "scenario": "CONNECTED",
        "updated_at": 1793413817000
      }
    }
  ]
}
```


### 11.33 查询全部状态成功响应

**通信方向：ECS → WMS。接口：`HTTP 200 response GET /api/v1/device/status`。**

```json
{
  "devices": [
    {
      "device": {
        "device_code": "XRAY01",
        "device_name": "XRAY点料机",
        "device_type": "XRAY_COUNTER",
        "role": "COUNT_STATION",
        "supported_commands": [
          "APPLY_PKG_DECISION",
          "APPLY_COUNT_DECISION",
          "APPLY_VERIFY_DECISION"
        ],
        "supported_events": [
          "SCAN_COMPLETED",
          "XRAY_COUNT_COMPLETED",
          "MATERIAL_ARRIVED",
          "NG_DISCHARGED"
        ]
      },
      "state": {
        "device_code": "XRAY01",
        "mode": "AUTO",
        "status": "RUNNING",
        "is_online": true,
        "current_command_code": "5ea6b228-ec3c-4e59-a45e-055b627fba4c",
        "scenario": "CONNECTED",
        "updated_at": 1793413817000
      }
    },
    {
      "device": {
        "device_code": "XRAY-ARM01",
        "device_name": "分拣机械臂",
        "device_type": "ROBOTIC_ARM",
        "role": "SORTER_ARM",
        "supported_commands": [
          "PUT_TO_BIN",
          "PUT_TO_RACK"
        ],
        "supported_events": []
      },
      "state": {
        "device_code": "XRAY-ARM01",
        "mode": "AUTO",
        "status": "IDLE",
        "is_online": true,
        "current_command_code": null,
        "scenario": "CONNECTED",
        "updated_at": 1793413817000
      }
    }
  ]
}
```


## 12. 双方交付及验收清单

- 确认四个端点、设备编码、固定template_id及十个变量定义，模板文件不由WMS任务下发。
- 确认barcode、source_event_id、command_code关联和幂等保存期；事件ACK、命令ACK、物理Result和业务入账分别记录。
- 真机/模拟分别覆盖免清点、需清点→变量→贴标→双码复核→上架、单层/五层目标、业务NG、CCD无读码、贴标失败、机械臂失败、出口保持/安全离区及多盘并行。
- 覆盖ACK丢失、相同消息重报、同号改载荷、不同命令试图重复消费同盘事实、原结果恢复、消息乱序、回调409及进程重启。
- WMS原物料/库存规则及打印QQTYPC的业务口径由WMS负责；HTTP收件成功或设备物理成功不能自动视为库存已入账。


变量修订（2026-10-31）：标签变量更新为ID、PHHPN、SPKG、MMfrPN、VVendor、QQTYPC、Location、DDC、DateTime、LLC。ID就是预先固定的模板ID，值XX123456789；Location允许空字符串，DateTime按yyyy-MM-dd HH:mm:ss。此修订只更新文档，不代表软件已实现。

## WMS等待超时的设备通知（2026-11-02增补）

ECS等待本附录APPLY_PKG_DECISION、APPLY_COUNT_DECISION或APPLY_VERIFY_DECISION超出约定阈值时，按barcode及阶段向XRAY发WMS_TIMEOUT；该通知不新增WMS HTTP事件或Result，不把WMS延迟构造为设备动作FAILED。无效指令或429/503未接纳响应不解除本盘等待；实际人工清料并在ECS确认后停止补报对应超时，报警由XRAY自行清除。具体TCP通知见XRAY V3.4厂商版第12节，报警后WMS迟到的新判定（含NG）返回422；原已接纳命令重试保持幂等，不因超时重复建盘或推断已放行；三轮耗尽报警并封锁，不自动排NG，规则以8.3节为准。

## 13. 异常规则索引与具体JSON示例

本节原补充规则已统合至第2、3、5、6、7、8、9、10节；13.1～13.8保留原编号作为查阅索引，13.10保留完整JSON示例。字段以对应主章节表格为准，不另维护补充字段表。本次整理保留既定策略；ECS侧已于2026-11-05纳入第13节，设备端本地处置和保存要求仍需各方落实。业务明确NG、技术已确认失败、通信结果未知仍须区分。

### 13.1 异常处置闭环

规则已统合至**2.2节异常处置闭环**。本节不再另列一套处置规则；具体JSON见13.10。

### 13.2 APPLY_PKG_DECISION新增业务NG

字段已统合至**6.1节APPLY_PKG_DECISION**：decision=COUNT_REQUIRED/BYPASS_COUNT/NG，NG时reason必填。NG完成结果统一见**7.4节**，示例见13.10.3～13.10.6。

### 13.3 WMS业务判定重试：总共3次，每次5秒

规则已统合至**8.3节WMS业务判定重试**，包括三次请求、每轮5秒、耗尽仅报警、报警后封锁及人工清料确认及可靠送达的区别。示例见13.10.12～13.10.15。

### 13.4 NG_DISCHARGED补充字段

字段已统合至**5.5节NG_DISCHARGED**：可选reason_code建议填写，reason必填。稳定事件身份及原始时间要求见3.1节；各阶段排NG示例见13.10。

### 13.5 接纳后重启恢复

恢复要求已统合至**9.1节接纳后重启恢复**；设备任务查询示例见13.10.16～13.10.18。

### 13.6 物理完成至WMS ACK之间重启

恢复及补报要求已统合至**9.2节**；原结果补报和WMS幂等确认示例见13.10.19～13.10.21。

### 13.7 事件重报稳定性

原event_id、timestamp及完整data不变的要求已统合至**3.1节**；示例见13.10.22～13.10.23。

### 13.8 保留与清理

保留规则已统合至**9.3节**：未结/未知/未确认记录不自动过期，已闭环关联及去重记录在线至少90天，归档保留完整关联。

### 13.9 实施边界

本版包含进料decision=NG、三轮业务等待后仅报警、技术失败自主排NG以及跨方保留约定。2026-11-06取消旧的超时封锁和自动排NG策略。上线前须ECS、WMS、XRAY和机械臂开发方共同落实并联调；原有“支持持久化”不等于已具备本节全部异常策略。设备报警仍由设备自行清除。

### 13.10 异常处置与恢复的具体JSON示例

下列案例相互独立，不能将同盘的互斥分支连续执行。时间为示意纪元毫秒。HTTP示例仅展示正文；设备TCP示例仅用于说明原任务恢复关联，不是新增WMS接口，线上仍按XRAY V3.3单行JSON加LF发送。所有UUID均为示例。

#### 13.10.1 进料扫码NG上报

通信方向：ECS → WMS。HTTP POST /api/v1/callback/event。

此盘无码，barcode为null。XRAY自主排NG，不等待WMS发COUNT或BYPASS。

```json
{
  "device_code": "XRAY01",
  "event_type": "SCAN_COMPLETED",
  "timestamp": 1793664000000,
  "data": {
    "event_id": "70fca40d-a78a-4c79-9b64-cac165242c2e",
    "reel_id": "2e368e46-073b-4381-851d-a3b740082575",
    "barcode": null,
    "scan_stage": "ENTRY",
    "scan_status": "NG",
    "template_id": "XX123456789",
    "reason_code": "SCAN_FAILED",
    "reason_message": "最终未读到进料条码"
  }
}
```

#### 13.10.2 同盘扫码NG实际排出

通信方向：ECS → WMS。HTTP POST /api/v1/callback/event。

沿用13.10.1的barcode，排出是新事实，使用独立event_id及实际排出时间。没有WMS原命令，不额外编造Result。

```json
{
  "device_code": "XRAY01",
  "event_type": "NG_DISCHARGED",
  "timestamp": 1793664003000,
  "data": {
    "event_id": "3b11af55-2e69-44ee-b099-67f5547f1eb8",
    "reel_id": "2e368e46-073b-4381-851d-a3b740082575",
    "barcode": null,
    "stage": "ENTRY",
    "reason_code": "SCAN_FAILED",
    "reason": "最终扫码失败，已实际排入NG位"
  }
}
```

#### 13.10.3 进料业务明确拒收

通信方向：WMS → ECS。HTTP POST /api/v1/device/command。

假定该盘之前已上报ENTRY成功事件，event_id为此处source_event_id。明确业务NG不再重试相同判定。

```json
{
  "device_code": "XRAY01",
  "command_code": "4344a8df-115e-473d-925d-fdbe940093e9",
  "task_type": "APPLY_PKG_DECISION",
  "priority": 1,
  "timeout": 20000,
  "timestamp": 1793664001000,
  "params": {
    "barcode": "PKG-REJECT-001",
    "source_event_id": "f97c2624-9cd1-4110-9a6d-a9e34f816ee2",
    "decision": "NG",
    "reason": "该PKG不在允许入库记录内"
  }
}
```

#### 13.10.4 拒收命令接纳确认

通信方向：ECS → WMS。上述command请求的HTTP200响应。

这里只是接纳成功，还不能认定已排出。

```json
{
  "code": 200,
  "message": "ACK"
}
```

#### 13.10.5 拒收原命令实际完成

通信方向：ECS → WMS。HTTP POST /api/v1/callback/result。

沿用13.10.3的command_code；SUCCESS表示排NG动作完成，不表示该盘业务合格。

```json
{
  "command_code": "4344a8df-115e-473d-925d-fdbe940093e9",
  "device_code": "XRAY01",
  "result": "SUCCESS",
  "finish_time": 1793664006000,
  "data": {
    "reel_id": "180c0e35-1634-4e20-a102-b182c1628528",
    "barcode": "PKG-REJECT-001",
    "decision": "NG",
    "discharged": true
  }
}
```

#### 13.10.6 业务拒收实际排NG事件

通信方向：ECS → WMS。HTTP POST /api/v1/callback/event。

```json
{
  "device_code": "XRAY01",
  "event_type": "NG_DISCHARGED",
  "timestamp": 1793664006000,
  "data": {
    "event_id": "93f4dbba-bc7b-43d6-9368-4ac7fac0e0bd",
    "reel_id": "180c0e35-1634-4e20-a102-b182c1628528",
    "barcode": "PKG-REJECT-001",
    "stage": "ENTRY",
    "reason_code": "BUSINESS_REJECTED",
    "reason": "该PKG不在允许入库记录内，已实际排NG"
  }
}
```

#### 13.10.7 清点原命令已确认失败

通信方向：ECS → WMS。HTTP POST /api/v1/callback/result。

command_code指此前已接纳并下发COUNT的APPLY_PKG_DECISION命令，不能无原命令直接上报。不得填qty=0冒充清点结果。

```json
{
  "command_code": "7220345a-5fdb-4052-8b27-be9f452d858c",
  "device_code": "XRAY01",
  "result": "FAILED",
  "finish_time": 1793664008000,
  "data": {
    "reel_id": "5e259bce-1630-48d6-b4e2-ad84680ad310",
    "barcode": "PKG-COUNT-001",
    "failure_stage": "COUNT"
  },
  "error_detail": {
    "code": "COUNT_FAILED",
    "msg": "清点任务已结束，未获得可信数量"
  }
}
```

#### 13.10.8 清点失败业务事件

通信方向：ECS → WMS。HTTP POST /api/v1/callback/event。

```json
{
  "device_code": "XRAY01",
  "event_type": "XRAY_COUNT_COMPLETED",
  "timestamp": 1793664008000,
  "data": {
    "event_id": "1ec0a780-7283-442a-851b-51ba21109ade",
    "reel_id": "5e259bce-1630-48d6-b4e2-ad84680ad310",
    "barcode": "PKG-COUNT-001",
    "template_id": "XX123456789",
    "count_status": "NG",
    "reason_code": "COUNT_FAILED",
    "reason_message": "清点任务已结束，未获得可信数量"
  }
}
```

#### 13.10.9 清点失败后实际排NG

通信方向：ECS → WMS。HTTP POST /api/v1/callback/event。

设备满足排料条件后自主排NG。13.10.7原命令始终保持FAILED，不因本事件改为SUCCESS。

```json
{
  "device_code": "XRAY01",
  "event_type": "NG_DISCHARGED",
  "timestamp": 1793664011000,
  "data": {
    "event_id": "02875582-439b-4a3e-948a-5c004862a5ed",
    "reel_id": "5e259bce-1630-48d6-b4e2-ad84680ad310",
    "barcode": "PKG-COUNT-001",
    "stage": "COUNT",
    "reason_code": "COUNT_FAILED",
    "reason": "清点确认失败，已实际排NG"
  }
}
```

#### 13.10.10 打印贴标原命令已确认失败

通信方向：ECS → WMS。HTTP POST /api/v1/callback/result。

原命令为APPLY_COUNT_DECISION OK。若实际失败在模板加载或打印环节，failure_stage按实际填写TEMPLATE或PRINT，并提供相应真实错误，不继续正常CCD、不自动补打。

```json
{
  "command_code": "3632b503-7ff9-4717-be44-71ae1d1a03a6",
  "device_code": "XRAY01",
  "result": "FAILED",
  "finish_time": 1793664008000,
  "data": {
    "reel_id": "fa69bfd5-9f09-4df8-a78b-7b9659773bda",
    "barcode": "PKG-LABEL-001",
    "failure_stage": "LABEL"
  },
  "error_detail": {
    "code": "LABEL_FAILED",
    "msg": "打印已完成，但贴标确认失败，任务已结束"
  }
}
```

#### 13.10.11 贴标失败后实际排NG

通信方向：ECS → WMS。HTTP POST /api/v1/callback/event。

```json
{
  "device_code": "XRAY01",
  "event_type": "NG_DISCHARGED",
  "timestamp": 1793664011000,
  "data": {
    "event_id": "c6b9e705-e0d6-452a-8c80-1657c03366a9",
    "reel_id": "fa69bfd5-9f09-4df8-a78b-7b9659773bda",
    "barcode": "PKG-LABEL-001",
    "stage": "LABEL",
    "reason_code": "LABEL_FAILED",
    "reason": "贴标确认失败，已实际排NG"
  }
}
```

#### 13.10.12 WMS判定三次请求时序

| 相对首次上报时间 | ECS行为 | 等待状态 |
| --- | --- | --- |
| t=0 | 第1次发送下述原事件 | 等待APPLY_VERIFY_DECISION |
| t=5秒 | 未接纳有效判定则第2次原样发送 | HTTP200 ACK不结束业务等待 |
| t=10秒 | 仍未接纳有效判定则第3次原样发送 | event_id、timestamp、data均不变 |
| t=15秒 | 生成WMS_TIMEOUT通知XRAY报警并保持 | 封锁该盘新指令，等待人工清料确认 |

#### 13.10.12-1 第1次复核事件请求

通信方向：ECS → WMS。HTTP POST /api/v1/callback/event。

本次实际发送时刻为t=0秒；正文timestamp仍为实际读码发生时刻，不填重发时刻。

```json
{
  "device_code": "XRAY01",
  "event_type": "SCAN_COMPLETED",
  "timestamp": 1793664000000,
  "data": {
    "event_id": "2197be95-9dbb-4cac-9a29-443b79094796",
    "reel_id": "74a36c27-9c6d-4b1a-9bd0-6846b636a04d",
    "barcode": "PKG-OLD-001",
    "scan_stage": "VERIFY",
    "scan_status": "OK",
    "old_barcode": "PKG-OLD-001",
    "new_barcode": "PKG-NEW-001"
  }
}
```

#### 13.10.12-2 第2次复核事件请求

通信方向：ECS → WMS。HTTP POST /api/v1/callback/event。

本次实际发送时刻为t=5秒；正文timestamp仍为实际读码发生时刻，不填重发时刻。

```json
{
  "device_code": "XRAY01",
  "event_type": "SCAN_COMPLETED",
  "timestamp": 1793664000000,
  "data": {
    "event_id": "2197be95-9dbb-4cac-9a29-443b79094796",
    "reel_id": "74a36c27-9c6d-4b1a-9bd0-6846b636a04d",
    "barcode": "PKG-OLD-001",
    "scan_stage": "VERIFY",
    "scan_status": "OK",
    "old_barcode": "PKG-OLD-001",
    "new_barcode": "PKG-NEW-001"
  }
}
```

#### 13.10.12-3 第3次复核事件请求

通信方向：ECS → WMS。HTTP POST /api/v1/callback/event。

本次实际发送时刻为t=10秒；正文timestamp仍为实际读码发生时刻，不填重发时刻。

```json
{
  "device_code": "XRAY01",
  "event_type": "SCAN_COMPLETED",
  "timestamp": 1793664000000,
  "data": {
    "event_id": "2197be95-9dbb-4cac-9a29-443b79094796",
    "reel_id": "74a36c27-9c6d-4b1a-9bd0-6846b636a04d",
    "barcode": "PKG-OLD-001",
    "scan_stage": "VERIFY",
    "scan_status": "OK",
    "old_barcode": "PKG-OLD-001",
    "new_barcode": "PKG-NEW-001"
  }
}
```

#### 13.10.13 三轮耗尽后通知XRAY报警

通信方向：ECS → XRAY，设备TCP。保持WAIT_VERIFY_DECISION，不下ROUTE NG，不新增WMS事件或Result。

```json
{
  "type": "WMS_TIMEOUT",
  "id": "d7551824-7420-4cfa-b89e-f16807c6dfe6",
  "reelId": "74a36c27-9c6d-4b1a-9bd0-6846b636a04d",
  "stage": "VERIFY_DECISION",
  "timeoutMs": 15000,
  "time": 1793664015000
}
```

#### 13.10.14 报警后拒绝该盘新业务判定

通信方向：ECS → WMS，为报警后该盘新command请求的HTTP422响应。即使barcode、source_event_id及参数正确，也不能恢复这盘动作。已接纳原command_code的同载荷重试仍返回原幂等ACK。

```json
{
  "code": 422,
  "message": "ANNEX_VALIDATION_FAILED"
}
```

#### 13.10.15 XRAY确认报警收件

通信方向：XRAY → ECS，设备TCP。RECEIVED只停止通知补报，不表示WMS已判定或物料已排出。

```json
{
  "type": "ACK",
  "id": "d7551824-7420-4cfa-b89e-f16807c6dfe6",
  "for": "WMS_TIMEOUT",
  "status": "RECEIVED"
}
```

报警后，WMS迟到的NG也不能再驱动物料。操作员实际取走该盘后，在ECS“料盘流程”选择该盘，点击“确认所选料盘已人工清料”并确认。此操作只关闭本地记录并停止通知补报，不生成NG_DISCHARGED或虚构Result，不向XRAY下发清报警/复位命令；报警由XRAY本地清除。若仍有活动/UNKNOWN任务或待处理设备事实，不允许确认。原reelId永久保持封锁，重新进料必须使用新reelId。设备STATUS须如实更新，不能继续包含已清走的旧盘。

#### 13.10.16 ECS接纳命令后重启恢复时序

前提：ECS已持久接纳一条APPLY_PKG_DECISION COUNT_REQUIRED，其 HTTP 命令的 `command_code`、`params.source_event_id` 与原料盘 `reel_id`（设备 TCP 中为 `reelId`）、设备 `COUNT.id` 的关联未丢失；下发可能已经发生。重启恢复先QUERY_TASK，不新建COUNT任务id。下面13.10.17～13.10.20为同一恢复案例。

#### 13.10.17 查询原COUNT任务

通信方向：ECS → XRAY。设备TCP，不调用WMS HTTP接口。

```json
{
  "type": "QUERY_TASK",
  "id": "dc21bd6d-4a94-47bb-9bb6-687add7a8f22"
}
```

#### 13.10.18 设备返回原完整终态

通信方向：XRAY → ECS。设备TCP。

若实际仍在运行则回RUNNING；UNKNOWN/NOT_FOUND不能解释为未执行，保持并对账，不重做。此处实际已经完成，返回原time与qty。

```json
{
  "type": "TASK_STATUS",
  "id": "dc21bd6d-4a94-47bb-9bb6-687add7a8f22",
  "status": "SUCCESS",
  "result": {
    "type": "RESULT",
    "id": "dc21bd6d-4a94-47bb-9bb6-687add7a8f22",
    "reelId": "a0e6edef-5b15-40a7-bd4c-26633e2ca6cc",
    "status": "SUCCESS",
    "qty": 4000,
    "time": 1793664008000
  }
}
```

#### 13.10.19 恢复后向WMS补报原结果

通信方向：ECS → WMS。HTTP POST /api/v1/callback/result。

command_code为重启前原命令号，与设备QUERY_TASK.id不是同一个字段/编号；finish_time取设备原time。

```json
{
  "command_code": "87360b43-fb2e-40f8-8909-4afc86e99bd7",
  "device_code": "XRAY01",
  "result": "SUCCESS",
  "finish_time": 1793664008000,
  "data": {
    "reel_id": "a0e6edef-5b15-40a7-bd4c-26633e2ca6cc",
    "barcode": "PKG-RECOVER-001",
    "decision": "COUNT_REQUIRED",
    "qty": 4000
  }
}
```

#### 13.10.20 WMS收到结果但ACK丢失，再次重启后的补报

通信方向：ECS → WMS。HTTP POST /api/v1/callback/result。

与13.10.19正文完全相同。WMS已业务处理过时只重复ACK，不重复应用数量。

```json
{
  "command_code": "87360b43-fb2e-40f8-8909-4afc86e99bd7",
  "device_code": "XRAY01",
  "result": "SUCCESS",
  "finish_time": 1793664008000,
  "data": {
    "reel_id": "a0e6edef-5b15-40a7-bd4c-26633e2ca6cc",
    "barcode": "PKG-RECOVER-001",
    "decision": "COUNT_REQUIRED",
    "qty": 4000
  }
}
```

#### 13.10.21 WMS确认原结果

通信方向：WMS → ECS。上述result回调请求的HTTP200响应。

```json
{
  "code": 200,
  "message": "ACK"
}
```

#### 13.10.22 恢复的原清点事件

通信方向：ECS → WMS。HTTP POST /api/v1/callback/event。

若该设备事实已有北向事件映射，必须取回该原event_id；只有确实尚未创建过映射时才首次创建并持久保存。

```json
{
  "device_code": "XRAY01",
  "event_type": "XRAY_COUNT_COMPLETED",
  "timestamp": 1793664008000,
  "data": {
    "event_id": "5310ee43-1dcc-476f-9dfe-62215b9e2b60",
    "reel_id": "a0e6edef-5b15-40a7-bd4c-26633e2ca6cc",
    "barcode": "PKG-RECOVER-001",
    "template_id": "XX123456789",
    "count_status": "OK",
    "qty": 4000
  }
}
```

#### 13.10.23 原事件再次补报

通信方向：ECS → WMS。HTTP POST /api/v1/callback/event。

与13.10.22完全相同，不能重新生成event_id、timestamp、qty或template_id。原任务、原结果及事件关联保留期限按9.3，不新增私有查询/清理HTTP接口。

```json
{
  "device_code": "XRAY01",
  "event_type": "XRAY_COUNT_COMPLETED",
  "timestamp": 1793664008000,
  "data": {
    "event_id": "5310ee43-1dcc-476f-9dfe-62215b9e2b60",
    "reel_id": "a0e6edef-5b15-40a7-bd4c-26633e2ca6cc",
    "barcode": "PKG-RECOVER-001",
    "template_id": "XX123456789",
    "count_status": "OK",
    "qty": 4000
  }
}
```


## 14. V1.2机械臂任务修订说明

本节沿用V1.2机械臂任务及目标字段修订；异常处理、重试、恢复、保留策略继续保留原约定。V1.5另在6.1和第15节加入本盘厚度链路，配套XRAY V3.4、机械臂V3.4；ECS侧已于2026-11-05按本版及第13节同步；设备端仍须按各自协议落实并联调。

| 原定义 | 本版定义 |
| --- | --- |
| PICK_AND_PUT + target.type=BIN/RACK | PUT_TO_BIN + ONE_LAYER_RACK，或PUT_TO_RACK + RETURN_RACK |
| target.binType整数1 | target.bin_type字符串"1" |
| cell固定8 | bin_cell_location仍固定整数8，仅字段名称改变 |
| stackHeightMm为放入后总高度 | PUT_TO_BIN的HTTP命令和结果均省略堆叠高度 |
| target/reel分开 | PUT_TO_BIN的target包含站点和箱位字段，PUT_TO_RACK的target另含本盘尺寸 |
| BIN四个尺寸/高度均必填 | PUT_TO_BIN省略reel_layer、reel_thickness、reel_diameter、reel_totalthickness |
| RACK面/层/列 | RETURN_RACK增加目标站点、slot_id，面字段为rack_face；厚度/直径必填 |
| 料架和料箱按实物编号定位 | PUT_TO_BIN按已配置站点和箱位定位，省略rack_id及bin_id。PUT_TO_RACK仍须提供rack_id |

旧字段与新字段不并存；不能同时传type、binType、cell、stackHeightMm和新snake_case目标字段。以下新增完整会话示例与前文例子均为字段演示，不能当作同盘重复搬运。

### PUT_TO_BIN完整WMS任务

通信方向：WMS → ECS；HTTP POST /api/v1/device/command。

```json
{
  "device_code": "XRAY-ARM01",
  "command_code": "287df229-867e-403b-b4eb-1c23d09f2bcc",
  "task_type": "PUT_TO_BIN",
  "priority": 1,
  "timeout": 90000,
  "timestamp": 1793664000000,
  "params": {
    "barcode": "PKG-NEW-001",
    "source_event_id": "ae674c2d-635a-471e-bb2e-0dfa924c8a04",
    "target": {
      "location_id": "STATION_B",
      "location_type": "ONE_LAYER_RACK",
      "bin_type": "1",
      "bin_location": "D",
      "bin_cell_location": 8
    }
  }
}
```

### 接纳确认

通信方向：ECS → WMS；原command请求HTTP200响应。

```json
{
  "code": 200,
  "message": "ACK"
}
```

### PUT_TO_BIN完成结果

通信方向：ECS → WMS；HTTP POST /api/v1/callback/result。

```json
{
  "command_code": "287df229-867e-403b-b4eb-1c23d09f2bcc",
  "device_code": "XRAY-ARM01",
  "result": "SUCCESS",
  "finish_time": 1793664020000,
  "data": {
    "reel_id": "61089566-df14-4130-962c-373bef5d809f",
    "barcode": "PKG-NEW-001",
    "actual_qty": 1,
    "position": "TARGET",
    "actual_target": {
      "location_id": "STATION_B",
      "location_type": "ONE_LAYER_RACK",
      "bin_type": "1",
      "bin_location": "D",
      "bin_cell_location": 8
    }
  }
}
```

### PUT_TO_RACK完整WMS任务

通信方向：WMS → ECS；HTTP POST /api/v1/device/command。

```json
{
  "device_code": "XRAY-ARM01",
  "command_code": "7ef542a1-0579-49c7-b52f-2c9f0c454148",
  "task_type": "PUT_TO_RACK",
  "priority": 1,
  "timeout": 90000,
  "timestamp": 1793664000000,
  "params": {
    "barcode": "PKG-NEW-001",
    "source_event_id": "446b8d6d-13c5-4583-ab8b-a50e99898649",
    "target": {
      "location_id": "STATION_A",
      "location_type": "RETURN_RACK",
      "rack_id": "RETURN-RACK-01",
      "rack_face": "A",
      "slot_id": "A-03",
      "rack_layer": 2,
      "rack_column": 3,
      "reel_thickness": 15.3,
      "reel_diameter": 7
    }
  }
}
```

### 接纳确认

通信方向：ECS → WMS；原command请求HTTP200响应。

```json
{
  "code": 200,
  "message": "ACK"
}
```

### PUT_TO_RACK完成结果

通信方向：ECS → WMS；HTTP POST /api/v1/callback/result。

```json
{
  "command_code": "7ef542a1-0579-49c7-b52f-2c9f0c454148",
  "device_code": "XRAY-ARM01",
  "result": "SUCCESS",
  "finish_time": 1793664020000,
  "data": {
    "reel_id": "dec43959-c4ab-4b9f-8245-adfb2f1fa6c6",
    "barcode": "PKG-NEW-001",
    "actual_qty": 1,
    "position": "TARGET",
    "actual_target": {
      "location_id": "STATION_A",
      "location_type": "RETURN_RACK",
      "rack_id": "RETURN-RACK-01",
      "rack_face": "A",
      "slot_id": "A-03",
      "rack_layer": 2,
      "rack_column": 3
    }
  }
}
```


## 15. 进料判定返回厚度与贴标参数（2026-11-04新增）

1. XRAY自主进料扫码，SCAN_RESULT仅按原规则提供原pkg和reelId，不要求设备在此时已知道WMS厚度。
2. ECS转发ENTRY事件，WMS在对应APPLY_PKG_DECISION的params中提供number类型reel_thickness，单位毫米；仅COUNT_REQUIRED必填，BYPASS_COUNT及NG省略。
3. ECS 持久保存该厚度及其与原料盘 `reel_id`、HTTP 命令 `params.source_event_id` 和 `command_code` 的关联，再随原设备 `COUNT` 命令转发；不新建“厚度设置”独立指令，不把ACK当成业务回复。
4. 点料成功、WMS返回原十个标签变量后，ECS在PRINT_LABEL顶层再次携带同一reel_thickness，使贴标命令参数完整。XRAY保存并核对同盘厚度，据实际标定控制贴标高度/行程；不是ECS猜测机械坐标。
5. BYPASS不传厚度、不点料、不贴标、不CCD，直接正常出料。NG分支不因缺少厚度阻塞已授权异常处置。

厚度为WMS提供的本次料盘参数，不宣称XRAY已实测。字段是单盘厚度，不是reel_totalthickness已有堆叠高度，也不是reel_layer层数。十个标签变量不增减，不把reel_thickness塞进variables。五层料架搬运仍由WMS按6.4.2提供本盘厚度，单位与口径保持一致。单层料箱搬运按6.4.1省略厚度。

同盘从进料判定至贴标使用同一冻结值。缺失、格式错误、越界接纳前明确拒绝；PRINT_LABEL厚度与原同盘厚度不一致时ACK REJECTED、code=INVALID_PARAMS，说明原因，不驱动、不覆盖原记录。已接纳原id改厚度属于同号改载荷冲突；不以新命令id绕过同盘冻结。确需修正时先保持并受控核对，不静默改参数。

重复事实、网络重试和重启不能刷新厚度或混盘；ECS及XRAY均持久保存本盘命令参数。恢复时查询原任务和关联记录，不知道厚度就保持并核对，不使用默认值或最后一次厚度。该新增链路不改变既有失败排NG、超时报警自行清除及待确认异常策略。

以下为同盘链路示例；其中TCP报文用于展示转发关系，不是新增WMS端点。贴标前原清点终态、WMS变量返回的既有步骤仍须正常完成。

### POST /api/v1/device/command

通信方向：WMS → ECS。

```json
{
  "device_code": "XRAY01",
  "command_code": "309ca51c-9ef1-49ba-b165-e19da88f22a2",
  "task_type": "APPLY_PKG_DECISION",
  "priority": 1,
  "timeout": 70000,
  "timestamp": 1793750400000,
  "params": {
    "barcode": "PKG-OLD-001",
    "source_event_id": "09ebc6bc-621e-4f4a-8930-31766d1e9ffc",
    "decision": "COUNT_REQUIRED",
    "reel_thickness": 15.3
  }
}
```

### TCP COUNT，先保存本盘厚度再点料

通信方向：ECS → XRAY。

```json
{
  "type": "COUNT",
  "id": "c24d374e-92a6-4400-a65e-7ad14a16f2e1",
  "reelId": "74849c40-37b4-4677-9e7c-5dbd0ff659fd",
  "reel_thickness": 15.3
}
```

### TCP ACK，仅表示持久接纳

通信方向：XRAY → ECS。

```json
{
  "type": "ACK",
  "id": "c24d374e-92a6-4400-a65e-7ad14a16f2e1",
  "for": "COUNT",
  "status": "ACCEPTED"
}
```

### TCP PRINT_LABEL，既有点料及WMS变量步骤完成后发送

通信方向：ECS → XRAY。

```json
{
  "type": "PRINT_LABEL",
  "id": "471c63c6-f43a-42c5-ac7b-8de07e540e11",
  "reelId": "74849c40-37b4-4677-9e7c-5dbd0ff659fd",
  "reel_thickness": 15.3,
  "variables": {
    "ID": "XX123456789",
    "PHHPN": "100-0277-000HF",
    "SPKG": "SXCN0031482261000009NVD",
    "MMfrPN": "PZ2012S601AT000",
    "VVendor": "M000002870",
    "QQTYPC": "4000",
    "Location": "",
    "DDC": "2623",
    "DateTime": "2026-11-04 10:00:00",
    "LLC": "1T3330022677"
  }
}
```



2026-11-05实施对齐：ECS侧支持本版命令/字段、第13节异常闭环及PICKED交接；取料感应器判断、设备本地技术失败自主排NG和原任务保存/查询由对应设备端实现。设备本地未满足排料条件时保持，不以ECS接纳或收件ACK替代物理完成。


2026-11-06修订：三轮业务请求无判定后改为WMS_TIMEOUT报警并保持，取消旧自动ROUTE NG和时间封锁。明确业务NG和已确认技术失败的处置规则不变。
