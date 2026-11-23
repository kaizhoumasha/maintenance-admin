# WMS、ECS 与 Transport HTTP 往来表设计

日期为 2026-11-13。本文确定架构级 HTTP 往来表、采集规则和验收口径，供后续实现与评审使用。

## 1. 查询目标与范围

开发人员已知正在排查的对端时，拿到一个原始编号即可在一张表中按时间看到每次 HTTP 往来，定位哪一次、哪一侧、哪个阶段出了问题。每次往来保存自己的请求和响应，包含原始正文与可直接阅读的协议数据。后续采用、业务消费和发送前拦截由对应运行读口按原编号解释。

| 表 | 覆盖的正式接口 | 首选定位 |
| --- | --- | --- |
| `wms_http_exchange` | WMS 决策、事实请求和普通回调 | `operation_id` |
| `ecs_http_exchange` | ECS 命令、Result 回调、事件回调和同步 Status | `command_code`。独立事件及指定设备的 Status 用 `device_code` 加时间，全设备 Status 用对端和时间 |
| `transport_http_exchange` | 搬运提交、位置回调和结果回调 | `transport_task_id` |

三张表有相同的采集语义和字段命名。WMS 与 Transport 共用接收入口时，根据可信的操作注册归属把往来写到对应表。共享入口收到无法识别操作的报文时，落到 `wms_http_exchange`，并保留接口、时间、trace 和拒绝原因。已知是 Transport 操作时，即使任务号无法解析，也只写 Transport 表。联调模拟流量沿用相应协议表，并用可信装配产生的 `traffic_source` 区分。三类之外的可靠投递仍需保有自己的冻结请求和执行证据。

同一个原编号可以命中多个 HTTP 往来。一次重试、一次重复回调各生成一行。ECS 命令的同步 ACK 与该次命令请求配对，Result 与 WES 对该次回调准备的回复配对。Transport 的同步接纳与提交配对，后续位置和结果回调各自配对。入队、创建命令或派发前被业务拦截时，从运行状态解释是否开启过发送。

## 2. 字段和索引

### 公共字段

| 字段 | PostgreSQL 类型 | 规则 |
| --- | --- | --- |
| `id` | `uuid`，主键 | WES 为本次 HTTP 往来生成的 UUIDv7。入站在读取正文前分配。写入重试复用该 ID。 |
| `initiator`, `interaction_kind`, `traffic_source` | `text`，非空 | 请求由 WES 发起时为 `WES`，由对端发起时为 `PEER`。各表约束自己的交互类型。流量来源为 `PEER` 或 `MOCK`。 |
| `started_at`, `finished_at`, `response_observed_at` | `timestamptz` | `started_at` 非空。入站取接收入口时间，可靠出站取发送门槛记录时间，Status 取发起调用前的时间。`finished_at` 保存首次关闭尝试的原时间。出站 `response_observed_at` 取客户端收到响应头的时间，入站回复准备时间用 `finished_at`。这些时间随事实传入，补写时间另由日志记录。 |
| `http_method`, `endpoint_path`, `peer_endpoint`, `trace_id` | `text` | 方法和路径非空。路径不带查询串。对端取可信的逻辑目标或脱敏地址，未知时为空。trace 用于查日志和往来，允许跨行重复。 |
| `request_headers`, `response_headers`, `request_query` | `jsonb`，可空 | 仅采集白名单字段。首版请求头保留 `Content-Type`、`Accept`、`Content-Encoding`，响应头保留 `Content-Type`、`Content-Encoding`、`Retry-After`，Status 查询参数保留 `device_code`。 |
| `request_wire`, `response_wire` | `bytea`，可空 | 应用实际收到、交给 HTTP 客户端发送或交给框架返回的正文原字节。出站冻结字节可先落行，是否真正发出看 `attempt_state`。 |
| `request_payload`, `response_payload` | `jsonb`，可空 | 从该侧完整正文解析出的协议数据。WMS、Transport 取 `data`。ECS 命令取 `params`，事件及 Result 取 `data`，Status 响应取 `devices`。纯 ACK、没有数据段、解析失败或正文未完整取得时为空。 |
| `request_wire_state`, `response_wire_state` | `text`，非空 | `FULL`、`PREVIEW`、`UNAVAILABLE` 或 `PURGED`。确知空正文保存为零字节且标记 `FULL`。无可用字节保存 `NULL`。 |
| `request_wire_sha256`, `response_wire_sha256`, `request_wire_bytes`, `response_wire_bytes` | `text` / `bigint`，可空 | 对实际保存的字节计算摘要与长度。预览按实际保存的前缀计算。正文清理后可保留原摘要与长度。 |
| `attempt_state`, `failure_kind` | `text` | `attempt_state` 非空，记录首次关闭时的结论。未关闭为 `OPEN`，关闭结论为 `NOT_SENT`、`DELIVERY_UNKNOWN`、`RESPONSE_INCOMPLETE`、`RESPONSE_OBSERVED` 或 `RESPONSE_PREPARED`。失败原因用有界的内部枚举，例如 `CONNECT_TIMEOUT`、`READ_TIMEOUT`、`BODY_TOO_LARGE`。已观察到响应且关闭尚未落账时，可以同时看到 `OPEN` 和响应。 |
| `close_reason` | `text`，可空 | 可靠发送首次关闭租约的原因，由原运行事务确定。值见第 3 节。未关闭或无租约的往来为空。 |
| `http_status`, `protocol_code`, `protocol_verdict` | `smallint` / `text`，可空 | HTTP 状态、协议码和 WES 对响应的协议判定分别保存。WMS 和 Transport 投影报文 `code`，ECS 投影已识别的协议 `message`，例如 `ACK`、`COMMAND_NOT_FOUND`。ECS 报文的数值 `code` 保留在原文。协议码只能从可安全提取的字段取得，协议判定仍按原分类。 |
| `handling_outcome`, `reason_code`, `reported_result` | `text`，可空 | 当次处理结论、可安全展示的原因、从协议识别出的当次决定或结果。结论包括 `ACCEPTED`、`REJECTED`、`CONFLICT`、`REPLAYED`、`DUPLICATE` 和 `DEFERRED`。原因可为 `INVALID_ENVELOPE`、`INVALID_DATA`、`DEVICE_MISMATCH`、`UNKNOWN_TASK` 或 `STALE_REVISION`。结果可为 `ACCEPT`、`NG` 或 `SUCCESS`。只记录当次已知的事实，运行模型记录后续采用和业务消费。 |
| `replayed_from_id` | `uuid`，可空 | 仅 `REPLAYED` 可填写，值为已确认提供相同状态和回复字节的同表往来 ID。它是查询提示，允许目标到期后查不到，原回复仍由运行证据提供。切换前的原回复没有新往来 ID 时为空。 |
| `message_id`, `lease_id` | `bigint` / `text`，可空 | 可靠发送的原消息和发送租约身份。直接同步调用与入站均为空。二者同时有值或同时为空。 |
| `receipt_id` | `bigint`，可空 | 入站已确认关联的原整数收据 ID，可用于区分同一协议编号的不同受理实例。只在身份得到证明后填写。收据到期后该值仍可追溯。 |
| `late_response_at` | `timestamptz`，可空 | 响应在首次关闭后才取得，或运行层按原租约判定为迟到时，保存该次响应观测完成时间。该响应仍写到原行，保留首次 `attempt_state`、`failure_kind` 和 `finished_at`。 |

正文保存原字节，payload 保存对应字段的解析值。业务校验、补默认值和归一化后的值由使用它的运行模型持有。原文和 payload 使用相同的正文权限及审计，摘要查询只返回定位字段、时间、状态、原因和正文可用状态。普通数据库只读角色只授予摘要列的查询权限，正文走受控读取。白名单头、查询参数、路径和日志字段均按各自入口过滤，认证头、签名、Cookie 和凭据排除在元数据采集范围之外。

`FULL` 和 `PREVIEW` 要求有字节、长度与摘要，数据库校验字节长度。采集时计算 SHA-256。清理单侧正文时，原字节与该侧 payload 一起清除，状态改为 `PURGED`。接收超限正文遵守接口原读取上限，保存可用前缀与截断原因。空正文与响应不存在通过状态区分。HTTP 状态须在 100 至 599 之间。往来关闭时填 `finished_at`，未关闭的 `OPEN` 行保持为空。

### 专用字段

| 表 | 类型约束和专用字段 |
| --- | --- |
| `wms_http_exchange` | `interaction_kind` 为 `DECISION`、`FACT`、`CALLBACK`。增加 `operation_id uuid` 和 `operation text`，允许缺失。结构化编号使用通过协议格式校验的原 UUID。 |
| `ecs_http_exchange` | `interaction_kind` 为 `COMMAND`、`RESULT`、`EVENT`、`STATUS`。增加 `command_code text`、`device_code text`、`task_type text`、`event_type text`，按已取得的协议字段填写。Result 报文可按声明的命令号检索，`reason_code` 表达未匹配和设备不一致等情况。 |
| `transport_http_exchange` | `interaction_kind` 为 `SUBMIT`、`POSITION_CALLBACK`、`RESULT_CALLBACK`。增加 `transport_task_id text`、`task_match_state text`、`outcome_revision bigint`。`task_match_state` 区分 `MATCHED`、`UNMATCHED` 和 `UNKNOWN`。`outcome_revision` 是本次报文报告的版本。原 `operation_id` 随 wire 保存，独立列和索引省略。 |

编号解析成功就保留可安全存储的值，原任务是否存在、编号是否归属于本次运行由运行模型判断。输入无法安全投影为数据库文本时，对应列为空，原字节和校验原因仍按采集规则保存。

### 首版索引

| 范围 | B-tree 索引 |
| --- | --- |
| 三表 | 主键 `id`。时间索引 `(started_at, id)`。非空 trace 索引 `(trace_id, started_at, id) WHERE trace_id IS NOT NULL`。 |
| 三表 | 可靠发送唯一索引 `(message_id, lease_id) WHERE lease_id IS NOT NULL`，防止同一次发送被开行两次。 |
| WMS | `(operation_id, started_at, id) WHERE operation_id IS NOT NULL`。 |
| ECS | `(command_code, started_at, id) WHERE command_code IS NOT NULL`。另建 `(device_code, started_at, id) WHERE device_code IS NOT NULL`。 |
| Transport | `(transport_task_id, started_at, id) WHERE transport_task_id IS NOT NULL`。 |

定位编号允许重复，结果统一按 `started_at, id` 排序。按接口或失败类型排查时先限定时间范围。首版索引覆盖编号、trace 和时间查询，正文与 payload 直接按往来 ID 读取。无外部编号时，trace 和接口加时间都能作为入口。

## 3. 正常与异常采集规则

### 出站和写入失败隔离

1. 可靠发送先在原发送门槛事务中保存权威开启证明。该事务提交成功后才允许发送。事务失败时按原投递合同处理，不能用往来行代替开启证明。运行层接受的 ACK、终态响应和 Transport 接纳证明继续在各自权威事务中与状态一并提交。开启、观察响应和关闭均携带同一份尝试身份，记录实际选用的对端、方法和路径。
2. 往来表使用独立连接和短事务写入。发送门槛提交后安排 `OPEN` 写入，HTTP 发送无需等待它完成。响应观测和尝试结束后可各自补写。仅往来写入失败时，记录带往来 ID、trace、协议、阶段和原因的采集失败及指标，原发送、重试、回执和 Status 结果按原合同继续。写入预算和待处理数量均有界，达到上限时按同一失败路径报告。运行锁不等待往来写入。
3. Status 从设备刷新和显式 Status 查询共用的实际 HTTP 调用边界采集，沿用同步语义及原限流。HTTP 客户端只提供有界字节、响应头观察时间和网络结果，数据库写入由集成装配负责。连接失败、读超时和正文不完整按可证明的状态分类。发送门槛前的取消、配置拒绝或 Status 限流由对应运行读口或请求日志解释。

### 同一次发送的并发更新

- 可靠发送以原 `message_id + lease_id` 为唯一键。协议路由在消息装配时确定，开启、观察、关闭、重试落库和恢复都使用同一张表。运行开启证据保存往来 ID、原开始时间、请求快照引用和定位元数据。关闭或观测可用这些字段幂等插入缺失的行，开始时间使用原开启时间。缺原开启证明时由运行状态解释，禁止合成一次可靠发送。Status 以本次调用的往来 ID 幂等落账。
- 以唯一键执行插入或加锁更新，在同一行原子合并关闭事实和响应事实。开启写入只补行。采集接收原租约运行事务提交的 `attempt_state`、`failure_kind`、`finished_at` 和 `close_reason`。再次写入同一事实无副作用，冲突的关闭候选保留已记录结论并记录采集冲突。响应状态、白名单头、正文、摘要、协议码及判定作为同一份观察合并。已知同源时可从无正文补到预览或完整正文。完整响应保持原值，缺字段、较弱观察和冲突字节均不能覆盖它。冲突只记安全标识和指标，运行裁决沿原合同处理。
- Transport 接纳事务独立提交后即可触发对应响应补写。结束事务若失败，往来行可同时显示 `OPEN` 和已观察到的接纳响应，运行读口显示已保存的接纳证明。恢复依据原运行证据关闭尝试，复用同一行。响应先落或关闭先落时都比较事实时间及原租约迟到标记，补齐 `late_response_at`。旧租约的晚到写入只更新旧行，运行状态由原锁顺序和裁决规则决定。
- 未关闭的可靠发送往来从运行证据补齐关闭事实，Status 可按原调用超时恢复归档状态。清理和补写按同一行串行化，关闭行按原开始时间进入留存判断。过期行及已清理正文禁止因补写重建。仍需采用的迟到证据由运行模型保留，往来补写被跳过时留安全日志。

### 原租约的关闭责任

任何使原发送租约失效的运行路径，都在原锁顺序下读取并核对当前 `message_id + lease_id`。若该租约已有开启证明，在同一运行事务内先写入 `reliable_delivery_attempt` 的首次关闭时间、观测结论和关闭原因，再清除租约或变更消息状态。关闭事实随外层事务提交，回滚时保留原状态。首次关闭按原租约只写一次，往来表在提交后独立补写。仅领取过租约、尚无开启证明时，按原合同清除租约，HTTP 往来仍为零行。

| 使租约失效的路径 | 关闭事实由谁提交 | 往来行的关闭原因及响应规则 |
| --- | --- | --- |
| 发送正常结束，包括 HTTP 返回、客户端超时或确定未发送 | 可靠投递的 `finish` 事务，在同一事务更新尝试结论和消息状态。 | `FINISHED`。按实际客户端观测填写结论及失败原因，已有响应照实记录。 |
| 租约到期或发送中断后释放 | 到期恢复或释放事务，包括普通投递恢复与冻结路由投递的维护。重新核对原租约后，在清除它之前记录关闭。 | `LEASE_EXPIRED` 或 `LEASE_RELEASED`。按当时已有的证据判断，发出情况未证实时保持 `DELIVERY_UNKNOWN`。 |
| Transport 回调或 ECS Result 使原提交或命令结算 | 接收方外层事务内，由 `settle` 写入原租约关闭事实，与回调裁决、结算状态一并提交。 | `SETTLED_BY_CALLBACK`。回调的请求和回复保留在自己的入站行。原发送的同步响应尚未观察到时，其 HTTP 状态和响应字段为空，首次观测结论为 `DELIVERY_UNKNOWN`。对端已接收由运行结算证据表达。 |
| 暂停、任务或工作线停止，以及原请求替代流程使租约失效 | 负责 `hold`、`stop_scope` 或替代流程的原运行事务，在同一消息锁下处理。替代流程仍须遵守原发送门槛与未接纳判定。 | 已开启的尝试按实际路径记录 `HELD`、`SCOPE_STOPPED` 或 `SUPERSEDED`。未开启的租约只记录运行状态。已开启时若缺同步响应和确定未发送的证据，保持 `DELIVERY_UNKNOWN`。 |

关闭结论依据该事务当时可用的证据。独立提交的 Transport 接纳证明可以补入原发送行的同步响应，接纳事务自身负责保存接纳，原租约仍由表中使它失效的事务关闭。回调结算后若同步响应才返回，按旧租约补写原行的响应与 `late_response_at`，首次关闭时间、观测结论和原因保持原值。运行层是否采用该迟到事实继续按原协议裁决。

### 入站

1. 在正式入口分配往来 ID、采集 trace 并读取有界正文。按本次身份记录正常受理、重放、重复、冲突、未知命令或任务、信封或 payload 校验失败、超限、应用拒绝和暂时不可用。收到两次相同字节时保留两行。
2. 原请求受理、幂等回执和运行状态遵守原事务边界。待业务事务结束且最终回复已确定后，使用独立连接和有界预算补写往来。交给 HTTP 框架的状态和字节就是往来记录的响应，事务回滚时保存实际准备的错误回复。只有往来写入失败时，继续返回原已确定回复。权威回执或业务提交失败时，沿原合同返回暂时不可用。提交结果未知时记录 `COMMIT_UNKNOWN`，后续按原幂等身份核实实际提交结果。终局采集覆盖当前依赖后台快照的异常路径。独立写入无法完成时，按接口记录采集失败，日志只保存安全字段。
3. 重放分类由实际回复来源决定。逐字节复用原状态和原回复时记录 `REPLAYED`，能证明原往来 ID 时才填写 `replayed_from_id`。WMS 用原 `202 RECEIVED` 生成 `200 DUPLICATE` 时记录 `DUPLICATE`。同编号异内容当次新裁决的 `409` 记录 `CONFLICT`，已经保存的 `409` 被逐字节重报时记录 `REPLAYED`。现有 `pure_replay` 参与运行控制，采集使用明确的回复来源，字节恰好相等本身不足以证明重放。

### 采集缺口和网关定位

数据库不可用、进程中断或采集超时都可能留下缺口。应用接到的请求用 `X-Trace-Id` 查应用日志。网关提前拒绝时，网关若已部署并对外返回请求编号，则按该编号查网关日志。没有网关编号时，用接口、时间和 HTTP 状态查网关访问日志。应用生成的 trace 在未进入应用的请求上不可用。两条路径都保留安全的接口及失败阶段，正文仅通过往来读取权限查看。

协议状态和执行状态各有来源。WMS `DECIDED/NG`、ECS Result `SUCCESS` 和 Transport 报告版本表示对端的结论。入站 `RESPONSE_PREPARED` 表示 WES 已准备回复。结果何时被采用及由哪次往来触发，查对应的运行读口。

## 4. 运行证据的接管与旧表退出

下表固定权威落点与原身份。只有仍为判重、恢复、运行查询或留存所需的内容进入这些模型。往来 ID 可以附在证据上供排错，运行证据的存在和保留期独立于往来表。

| 旧字段或关系 | 新权威落点与身份 | 读写方及切换对账 |
| --- | --- | --- |
| `reliable_message.exchange_id`、`request_record_id`、`last_response_record_id`、`terminal_response_record_id` | 冻结请求字节和摘要、最近及终态响应快照归 `reliable_message`，保留原 ID、`target + operation + operation_id` 唯一性、消息归属、期限、租约和暂停状态。响应快照保留原字节、状态、判定及有来源的时间。 | Outbox、Sender 和 WMS/Transport 结果读口改读新位置。逐消息比较原字节、原响应状态、投递判定和原期限。仍在排队且没有 HTTP 往来的消息也须可恢复。 |
| `SEND_ATTEMPT_OPENED`、结束证据、WMS `SUPERSEDED_BY` | `reliable_delivery_attempt` 以原 `message_id + lease_id` 唯一，保存开启证明、有据可核对的关闭时间和原因，以及恢复所需的响应判定。WMS 替代关系归旧消息的 `superseded_by_message_id`。没有可靠租约身份的旧观测若仍参与裁决，按原证据身份保存于消息恢复记录。 | 发送门槛和恢复核对原租约。替代请求与原请求共用原锁及事务，逐条核对当前租约是否开启、是否存在未获证实的交付及其他原有准入条件。旧证据不足时保持原拒绝或待核对。 |
| `RECEIPT` 的整数 ID、当前响应指针、摘要、上下文与应用引用 | `callback_receipt` 使用原整数 `receipt_id` 和连续序列，保留原受理资格及生效时 `operation + operation_id` 唯一性。原 RAW/EFFECTIVE 比较、当前可重放状态和字节、回复时间、事件信封、工作线和业务应用上下文存于收据。 | WMS/Transport 回调接收器及 ECS 事件读口按原身份使用。逐收据核对摘要、重放字节、可重检状态和原业务引用，序列不得重用旧整数。权威写入回滚时释放本次受理。 |
| `ECS_COMMAND`、ACK 指针、`ECS_ATTEMPT_CONCLUSION` 和 `RESULT_EVIDENCE` | `ecs_command` 以原 `command_code` 唯一，保留 `owner_domain + call_key_digest` 唯一性并核对完整调用键。命令保存原输入、冻结路由和附录、期限、状态与 ACK 依据。`ecs_send_evidence` 按 `command_code + lease_id + attempt_revision` 保存裁决及其原发送依据。`ecs_result_evidence` 按 `command_code + semantic_digest` 唯一，保留归一化内容、原裁决、是否采用和原回复状态及字节。 | ECS 创建、发送门槛、Result 受理与结果读口核对原冻结字节、裁决修订顺序、ACK 依据和已采用的原 Result。保持长期重放，以及提前 Result、UNKNOWN、HELD 和迟到裁决行为。ECS 仍需的开启与裁决证据随命令留存，可靠消息到期后仍可读取。保留旧版仍有效的判重身份。 |
| `transport_task` 的提交和接纳指针、`RESULT_SOURCE`、`TRIGGER` | 在 `transport_task` 持有原提交消息身份及接纳快照，包括原接纳请求和响应字节、摘要、原任务与租约依据。待采用和已采用版本保存原结果收据身份，实际采用时再保存触发收据身份。沿用原任务 ID 唯一性和按版本的结果摘要。 | Transport 接纳观察器、回调和结果读口核对原接纳字节及来源、位置事实、待采用版本、已采用版本和两种来源。没有可证明来源的旧记录保留未知状态。即使触发与结果来自两次回调，采用过程仍按原事务提交。 |
| `BUSINESS_CONSUMED`、业务动作/任务/工作线引用和留存保护 | 业务的动作或消费模型记录原事实身份和版本、消费时间及后继意图。以原动作和来源版本表达幂等。工作线、未决业务及保护锚点引用相应的原事件、消息或运行证据，留存规则沿事实所有者的保护入口执行。 | 业务消费与清理在原工作线和原锁顺序下核对。盘点未决、找不到归属及仍被保护的引用，逐条确认等价保护。运行证据的来源不能改接到另一份同编号历史。 |

技术往来历史使用可配置的保留窗口。运行证据继续各自的保留合同，ECS 长期证据、活跃收据和业务保护锚点按原规则处理。往来 ID 的查询提示允许随过程记录到期失效，禁止用阻止历史删除的外键把两种保留期绑定在一起。

### 启用新写入的准入条件

1. 联调过程历史从新采集生效时开始，旧往来、快照和过程副本省略补迁。先在隔离库验证新读写路径和对账方式。联调切换窗口先备份，再排空旧 API、发送器、worker、Beat、清理和人工写入，停用依赖旧通用表的清理与补录入口。迁移按原运行身份可重入，重复执行须得到同一份证据。
2. 停写后逐项核对上表的有效运行依赖。每项都须有新落点，原身份、序列高水位、摘要、原回复字节、租约开启与关闭事实、版本和保护关系须对账。用同一组存量样本比较切换前后的回执重放、ECS Result 准入及未接纳判定、WMS 替代请求、Transport 接纳与结果来源。旧系统已有的未知交付和来源未知可原样迁移，仍未结束的租约按原身份恢复。
3. 对账报告分列原有未知与迁移新发现的缺失、歧义、冲突和运行依赖。任何必需证据未迁、字节或身份不一致、恢复判定不等价、仍需从旧三表读取的运行证据，均阻止启用新读写。保留旧表和原权威数据，修复后重新对账。只有检查通过才一并切换发送、回调、结果与排错读口、审计和清理。仅用于旧过程历史且无运行依赖的数据可以继续留在旧表等待删除。

### 删除旧表的准入条件

1. 在隔离库屏蔽旧三表读写后，通过重放、未决恢复、WMS 原请求替代、Transport 接纳和消费、清理保护及单编号排错。联调启用新写入后的验收记录须齐备，能按原身份核对运行事实及其新落点。缺少该验收时保留旧表，新写入可继续使用已对账的新模型。
2. 在独立的删除窗口再次核对实际运行版本、数据库外键与视图、旧清理与补录入口和人工脚本，确认当前运行路径无旧表依赖，旧写入者已排空且无法再接入。发现剩余依赖或无法确认写入者时阻止删除。保留两次检查的对账与验收证据，通过后才删除旧通用存储。切换后新增的运行事实由兼容新模型的恢复版本接管。

旧结构按以下规则退出。

- `integration_exchange` 中的命令、收据、投递身份交回上表对应的运行模型。移除 `role`、`identity_kind`、`identity_value` 和过程交互的旧通用指针。
- `integration_record` 中的幂等回复、ECS 裁决、发送门槛、原结果采用等核心事实迁到运行模型。清理用于过程记录拆行的 `record_kind`、`event_key`、同次请求与响应指针及正文副本。跨尝试的来源在运行证据内用明确关系表达。
- `integration_reference` 中，任务与命令归属改用各自原编号，消息归属使用明确的消息 ID。保留语义上不同的原结果来源、触发来源和请求替代关系。工作线、动作和业务任务的留存保护由事实所有者承担。对账通过后，清理冗余双向边、通用命名空间和仅用于旧过程历史补录的来源标记。
- 现有历史查询入口同步改用三张往来表与相应运行读口。按原编号定位、元数据权限、正文权限及访问审计继续可用。外部 WMS、ECS 和 Transport 协议字段按各自合同处理。

## 5. 单编号排错入口

使用 `GET /api/v1/integrations/diagnostics`，按 `peer=WMS|ECS|TRANSPORT` 和一个 `reference` 查询。服务按协议解释该值。WMS 用 `operation_id`，ECS 用 `command_code`，Transport 用 `transport_task_id`。历史仍可直接按第 2 节的索引查表。独立 ECS 事件、Status 和无编号异常继续用设备、对端、trace 或接口与时间查询往来。

| 对端 | 查询运行状态的方式 | 返回的排错信息 |
| --- | --- | --- |
| WMS | 按原 `operation_id` 查消息及入站收据。分别给这两个运行模型建 `operation_id` 起始的 B-tree 索引。使用原消息身份调用既有结果读口，由业务所有者按消息和结果版本解释消费证据。 | 消息 ID、目标、操作、方向及派发状态、拦截或暂停原因、协议结果、消费状态和已知替代请求编号。入站可显示原受理及回执状态。 |
| ECS | 按原 `command_code` 唯一键定位命令，按同一命令键取得原发送与 Result 裁决。 | 原命令状态、发送结论、ACK、Result 是否采用、拒绝或待核对原因。 |
| Transport | 按原 `transport_task_id` 唯一键读取任务与提交接纳，按版本读取待采用或已采用证据。 | 原提交状态、已报告及已采用版本、待满足的条件、原结果和触发采用来源。 |

接口复用历史元数据权限和现有分页上限，返回往来摘要及可定位的运行候选。往来零行时仍查询运行状态。存在多条候选时逐条返回各自的原身份、方向、操作与目标，按可证明的原消息、收据或命令任务身份关联。WMS 对端和方向相同且原编号重复使用时，也保留各自的运行实例。ECS 和 Transport 的未匹配回调保留在时间线，摘要显示未匹配。往来可见但权威证据已过期、业务未记录消费或无法确认关联时显示具体的未知原因。明确存在待办或消费事实时，才显示待采用或已消费。运行查不到且往来也为空时，结果只说明当前查询范围未找到记录。正文另按表和往来 ID 使用正文权限读取。

## 6. 验收

验收人只拿到下表给出的定位值和故障描述。先查单编号排错入口或在对应往来表用一个等值条件查时间线，异常阶段应从摘要直接读出。需要核对协议时，按往来 ID 查看两侧原文与 payload。涉及后续采用或未发送时，继续用同一个原编号查运行状态。

```sql
SELECT id, started_at, finished_at, interaction_kind, attempt_state,
       http_status, protocol_code, protocol_verdict, reported_result,
       handling_outcome, reason_code, failure_kind, close_reason, late_response_at
FROM enterprise_backend.wms_http_exchange
WHERE operation_id = :operation_id
ORDER BY started_at, id;
```

ECS 和 Transport 用各自表名与定位列执行同形查询。以有代表性的数据量检查执行计划，编号和 trace 查询应可走对应索引，返回顺序稳定。

| 只给验收人的定位值 | 注入的场景 | 验收人应能直接指出 |
| --- | --- | --- |
| WMS `operation_id` | 连接失败、发送结果未知、HTTP 503、重试成功 | 每次尝试各一行，能指出失败阶段和最终响应。没有观察到响应时可见缺失原因。 |
| WMS `operation_id` | HTTP 200 且 `DECIDED/NG`，另测待业务采用的 `ACCEPT` | 能区分 HTTP 成功、WMS 决定和 WES 是否已采用。 |
| WMS `operation_id` | 原回调返回 `202 RECEIVED`，相同内容重报得到 `200 DUPLICATE`，同编号异内容另得 `409` | 三次请求各自成对。新生成的重复回复显示 `DUPLICATE`，新冲突显示 `CONFLICT`，两行均无 `replayed_from_id`。 |
| ECS `command_code` | 同步命令 ACK、Result 回调 ACK，另测命令不存在和设备不符 | 同步和异步各自配对。未知命令也能用报文声明的编号找到，识别实际拒绝原因。 |
| ECS `command_code` | 先发生新 `409`，再两次重放该 `409` | 三行分别显示冲突或重放，已知原回复时可定位原往来；运行模型只保存一次原裁决。 |
| ECS `device_code` | 事件重复及 Status 响应异常 | 事件与 Status 各自成对。重复事件的 HTTP 行分开，业务只处理一次。 |
| Transport `transport_task_id` | 提交接纳、位置回调、结果先到后采用，另测未知任务和版本冲突 | 在同一表看到提交及回调顺序。能识别未匹配或冲突，并从运行读口按版本核对原结果与触发来源。 |
| Transport `transport_task_id` | 接纳已独立提交，随后结束事务失败 | 运行读口显示已保存的接纳，原尝试行已有响应且仍待关闭。恢复后仍只有同一行，响应字节不变。 |
| Transport `transport_task_id` 或 ECS `command_code` | 原发送已开启，回调先结算，发送进程随后退出且没有同步响应 | 入站回调有自己的请求和回执。原发送只有一行，显示 `SETTLED_BY_CALLBACK`、原关闭时间及未知发送观测，HTTP 状态和响应为空。运行读口显示回调已结算。 |
| Transport `transport_task_id` 或 ECS `command_code` | 原发送已开启，回调先结算，随后同步响应迟到 | 迟到响应只补原发送行，`late_response_at` 可见。首次关闭时间、原因和观测结论保持原值，入站回执与运行结算保持原有身份。 |
| 原消息编号 | 分别在开启后注入租约到期、释放、暂停和任务范围停止，另测未开启即取消 | 已开启的尝试各由对应运行事务只关闭一次，原因可查，事务回滚后仍保留原租约。未开启的租约只在运行读口可查，HTTP 往来零行。 |
| 任一编号或 `trace_id` | 旧租约关闭后才收到响应，同时已有新尝试。分别注入响应先落、关闭先落、重复写、首次开行失败和弱证据晚写 | 旧尝试行保留首次关闭结论，迟到响应只出现一次。缺行时按原开启证据补写，已有完整响应保持不变，新租约不被推进。 |
| 原编号或 `trace_id` | 仅往来表写入失败，分别注入可靠发送、入站成功回执、回调重放和 Status | 原收发结果与采集可用时一致，日志可查采集失败。另注入权威开启或回执提交失败，分别观察不发送或按原合同返回暂时不可用。 |
| `trace_id` | 非法 JSON、超限、应用事务回滚返回 503 | 能看到实际回复和失败阶段。区分截断、不可用、已确认空正文及清理后的正文。业务回滚后仍可查到 HTTP 往来。 |
| WMS `operation_id`、ECS `command_code` 或 Transport `transport_task_id` | 分别构造尚在排队或派发前被拦截的往来零行场景，WMS 另测同编号多候选 | 只凭原编号找到可确认的排队或拦截状态。候选可区分，查询结果无虚构的发送或归属。 |
| 应用 `X-Trace-Id` | 应用已接收，数据库不可用 | 应用日志可定位往来 ID 和失败阶段。 |
| 网关请求编号，或接口、时间和状态 | 网关在应用前拒绝 | 部署了编号返回与记录时能按编号找到拒绝。当前无编号的部署能按接口、时间和状态查到。两者均无应用往来行。 |

切换验收从现有运行证据选取样本，验证原消息恢复发送、原回执重放、ECS 原裁决和 Transport 未决结果都能按原身份找回。在隔离库屏蔽旧三表后重跑这些用例。

| 准入检查 | 故障注入 | 预期结果 |
| --- | --- | --- |
| 启用新写入 | 分别遗漏仍有效的 ECS 租约开启证据、修改活跃收据的回复字节、使 Transport 原结果来源映射不唯一。另测原库已标记未知的记录。 | 前三种故障报告原身份和对账差异，阻止切换及旧表删除。修复并重新对账后可通过。原有未知状态等价迁移时可通过。 |
| 删除旧表 | 新写入对账通过后，分别缺少旧表屏蔽验收、在实际删除窗口发现残留旧清理入口、外键或未知写入者。 | 阻止删除并保留旧表。已对账的新运行模型继续按新结构运行。补齐验收或消除依赖，再重新核对后才允许删除。 |

清理测试让过程报文到期，再重跑重放和恢复，验证运行证据仍可用，晚到补写也不会复活已清理报文。用带合成认证头的请求验证头部过滤、正文权限和审计。审计记录操作者、往来 ID 和所读侧，正文和凭据排除在审计内容之外。
