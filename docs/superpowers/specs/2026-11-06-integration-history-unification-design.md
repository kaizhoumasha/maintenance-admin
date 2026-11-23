# ECS / WMS 对接记录统一设计

日期为 2026-11-06。当前状态为工程评审与共同理解确认完成，按 D11 / C 进入实施计划。尚未实施数据库迁移或产品代码变更。

## 1. 用户目标与本次设计判断

用户要求对 ECS / WMS 对接记录做深度优化，解决记录分散、跨表查询困难的问题，并尽量统一数据格式。

本设计统一内部的交互身份表达、证据格式、报文保存和查询入口。外部协议继续由各自已确认的合同定义。各项结构、阶段和保留策略以第 16 节记录的实际答案为准，其余提案继续接受工程评审。

验收目标如下。

- 使用一个原始编号定位交互，直接取得请求、已记录的发送尝试、响应、回调和业务引用。
- 使用一个业务任务编号检索其明确关联的 ECS 与 WMS 交互，展示各自的交付、协议结果和业务应用情况。
- 新的收发证据使用相同字段、时间和报文表示。原始身份、原始字节与各自的幂等摘要保持可核对。
- 历史查询返回证据来源、采集范围和缺失情况，历史补录可中断续跑且不会产生发送行为。
- 完成存储收敛后，共享证据表保存技术收发证据，各运行表保存执行职责与必要的状态投影。
- 并发、回滚、迟到响应、未知交付、清理及服务重启仍沿原始身份恢复。

## 2. 现状与已确认的约束

本次从 origin/develop 的 1338e25 建立 feature/integration-history-unification 独立工作区。只检查仓库与离线迁移头，尚未连接生产库或取得线上执行计划。

| 现有存储 | 当前职责与需要处理的差异 |
| --- | --- |
| ecs_command | 原命令、业务调用键、冻结字节与路由、执行状态、ACK 存在信息及采用的 Result 身份 |
| reliable_message | ECS / WMS 出站意图、冻结请求、租约和重试、最近一次尝试及可用的终局响应 |
| ecs_send_evidence | 按 command_code 与 lease_id 保存 ECS 发送证据，Result 受理会读取它判断原命令是否可能发出 |
| ecs_result_evidence | 按 command_code 与 digest 保存 Result 内容、采用情况和原回复，参与冲突检测与幂等重放 |
| callback_receipt | 原入站身份与摘要、原响应、部分 ECS 事件上下文与业务应用引用，参与幂等 |
| callback_snapshot | 独立后台事务记录收包预览及回复，正文使用 base64，存在限流、截断与写入失败 |
| transport_task | 原搬运身份、提交接纳、位置、已应用结果与版本及待核对结果 |
| 各业务任务 / 动作表 | 原消息与命令引用、结果消费证据、阶段与后续意图 |

直接依据包括 app/models/{ecs_command,reliable,callback,transport}.py、app/integrations/ecs_command/{service,lifecycle,result}.py、app/integrations/callback_receipts.py、app/reliable/{store,lifecycle,delivery}.py 和 app/tasks/{callback_tasks,reliable_tasks}.py。

需要特别保留的行为如下。

1. EcsLifecycle.before_send 比较命令与 Outbox 中的冻结请求及快照。两份 request_raw 当前提供独立核对依据，收敛时必须同时提供等价的校验。
2. receive_ecs_result 读取发送证据判断是否可能派发。技术证据的迁移会影响 Result 准入，必须逐项回归。
3. WMS 原请求结果由公开结果读取接口提供。业务通过自身事务消费结果，两个业务动作可以分别消费同一结果版本。
4. WMS 回调存在 archive_guard 和 recheck，原身份的可重检响应可能更新。共同历史需要追加新响应并记录采用关系，保留历次已提交响应。
5. callback_snapshot 当前每来源每进程每分钟最多记录 120 条，预览约 48 KiB，后台写入预算为两秒，不能据其证明全部收包均已保存。
6. 默认诊断快照保留 7 天，公共收据与 Outbox 终态清理使用默认 90 天配置，并有业务保护例外。ECS 长期证据另有原始恢复用途。
7. 可靠消息清理后，同外部身份可能成为新的投递实例。历史记录不得重新授予旧执行权，也不能把两个生命周期合并。

## 3. 方案取舍

| 方案 | 收益与代价 | 本设计的选择 |
| --- | --- | --- |
| 仅提供统一视图 | 交付快，直接查询现有事实。格式差异、逐次证据缺口和重复报文仍由各处承担 | 用于迁移对照与过渡查询 |
| 统一交互与证据存储，保留有独立职责的运行表 | 新证据格式一致，可逐步收敛原文，执行模型的职责清晰。需要迁移写入、读取和清理路径 | 建议采用 |
| 将命令、投递、搬运、收据和业务状态合成一个通用任务表 | 要重写多个状态机、锁顺序、幂等与恢复合同，NULL 字段和分支大幅增加 | 当前需求无需承担此范围 |

三个步骤共同构成本次建议的完整改造。第一步的归集只是迁移阶段，第二步要真正迁移技术证据的权威存储，第三步通过校验后才收缩旧字段。

## 4. 统一后的三类数据

### 4.1 integration_exchange

一行代表一次原始交互实例，使用本地 UUIDv7 主键。外部协议身份作为属性保留，同一外部身份在清理后形成的新实例使用新的 exchange_id。

| 字段 | 统一规则 |
| --- | --- |
| id | UUIDv7，API 返回标准 UUID 字符串 |
| schema_version | 正整数，首版为 1 |
| peer_system | ECS 或 WMS，由可信装配及接收入口确定 |
| protocol | 现有协议名称，保留 ecs-command、wms-decisions、wms-facts、wms-transport 等区别 |
| direction | OUTBOUND 或 INBOUND，以 WES 为参照 |
| interaction_kind | COMMAND、EVENT、REQUEST 或 CALLBACK |
| operation / message_type | 分别保留原 operation 与 task_type / event_type，二者不混用 |
| identity_kind / identity_value | 原 command_code、operation_id 或事件摘要，保留原值与类型语义 |
| target_key / endpoint_path | 原逻辑目标与路径。ECS 使用原冻结来源，STATIC 发送的实际目标由每次尝试记录 |
| owner_domain | 创建或受理时已确认的原归属，历史未知使用 NULL |
| created_at | 原交互登记或受理时间，UTC timestamptz |
| recorded_at | 本实例首次进入统一存储的时间，与 created_at 分开 |
| capture_origin | LIVE 或 BACKFILL |
| coverage | DECLARED、PARTIAL 或 LEGACY，表示已承诺且能够解释的采集范围 |

exchange_id 不进入 ECS / WMS 报文，不替换 command_code、operation_id、digest 或业务主键。运行表在迁移中增加 exchange_id 引用以固定原实例。

D3 / A 已确认由本实体承接原 CallbackReceipt 的入站受理职责。入站角色保存 receipt_id、原 operation / operation_id、raw_digest / effective_digest / compare_mode、当前回复引用及原事件上下文与应用引用。receipt_id 保留现有数值事件身份与排序语义，也覆盖之后的新事件；已有业务引用和公开事件读口继续按该身份访问。

仍占受理资格的入站交互保有原 operation / operation_id 的等价唯一约束，历史留存与受理资格分别管理。到期清理先核对原业务保护，再释放受理资格，历史记录可以继续存在。RAW / EFFECTIVE 比较、WMS guard / recheck、当前回复更新和保存点边界继续由原接收器负责。新物理身份映射和序列迁移必须保持该合同，不能默认将既有数值事件引用替换为 UUID。

D4 / A 已确认由本实体承接 EcsCommand 的原命令根职责。ECS 命令角色保存原 command_code 的唯一身份、owner_domain + call_key_digest 唯一关系、完整调用键与业务输入核对依据、冻结路由 / 附录及原发送期限，以及执行状态、命令投递阶段、核对标记和已采用的 ACK / Result 引用。命令独有状态使用明确的类型约束，与入站角色的受理资格分开表达。

命令原业务调用身份和恢复证据沿原合同长期保留，收据资格到期释放和普通历史窗口不能清除命令调用唯一关系。公开命令创建、按原命令 / 调用查询、重入找回、未知交付与迟到 Result 的现有合同由原 ECS 服务承接。命令行锁在原调用路径定位到相应命令 exchange，随后仍锁对应 Outbox。

列表的 delivery_status、protocol_result、application_state 来源于拥有这些事实的读模型，并携带来源引用。可以暂时为空。首版不额外维护一份统一的可写成功状态。

### 4.2 integration_record

一行代表一条已经保存的观察或事实。采用追加方式，迟到结果与纠正追加记录，并通过原实例、attempt_id、record_id 和原版本指明关系。

| 字段 | 统一规则 |
| --- | --- |
| id / exchange_id | UUIDv7 主键与交互外键 |
| record_kind | REQUEST_REGISTERED、SEND_ATTEMPT_OPENED、SEND_ATTEMPT_FINISHED、CALLBACK_RECEIVED、RESPONSE_PREPARED、RESULT_EVIDENCE、RESULT_APPLIED、RESULT_REJECTED、DELIVERY_TRANSITION、BUSINESS_CONSUMED 或 LEGACY_SNAPSHOT |
| event_key | 有界的内部防重键，同一事实的事务重试复用该键 |
| attempt_id | 出站使用原 lease_id，入站使用服务端为该次 HTTP 接收生成的 UUID，trace_id 单独保存 |
| parent_record_id | 明确指向本次尝试的开始记录或对应收包，允许 NULL |
| direction | 本条报文或观察的方向，OUTBOUND、INBOUND 或 INTERNAL |
| occurred_at / recorded_at | 来源事实时间与本地记录时间分开，时间未知保持 NULL |
| http_status | 可空整数，仅保存已观察到的 HTTP 状态 |
| network_state | 现有 HttpResult 的投递层事实 |
| protocol_verdict | 原协议分类，例如 DELIVERED、NOT_RECEIVED、UNKNOWN 或 HOLD |
| protocol_code | 便于检索的文本投影，原报文中的数值 / 字符串类型保存在原文 |
| business_result / application_state | 只有业务或协议所有者提供明确事实时填写 |
| late / outcome_revision | 原迟到标记与原结果版本，历史未知保留 NULL |
| raw_body | BYTEA，单条报文一次保存，未取得正文使用 NULL，确实为空使用空字节 |
| body_sha256 / stored_bytes | 对实际保存的字节计算摘要与长度，区别于协议幂等摘要 |
| body_state | FULL、PREVIEW、LEGACY_REENCODED、UNAVAILABLE 或 PURGED |
| metadata | 小型白名单 JSONB，只保存扩展索引或诊断字段 |
| source_kind / source_key | 原证据或来源行的稳定引用，支持补录防重和来源审计 |

D2 / A 已确认将 EcsResultEvidence 的权威语义证据归入本表。RESULT_EVIDENCE 类型另有 semantic_digest、effective_payload、response_record_id 与确定的采用状态。effective_payload 保存按原合同归一化的 core，有独立的格式和体积校验，不使用诊断 metadata 容纳结果正文。该类型按原命令实例与原 Result digest 保证唯一，重复 HTTP 接收仍分别登记观察。原回复引用与 core 的相等核对继续支持原响应重放，原命令及其核对义务保护全部仍需重放的 Result 裁决。

同一交互的技术重试复用 REQUEST_REGISTERED 原文引用。每次尝试单独保存结果，重复收包也保留独立接收身份。入站响应使用实际准备返回的编码字节，随后由同一份字节构造 HTTP 响应。

SEND_ATTEMPT_OPENED 只证明发送前登记已提交。RESPONSE_PREPARED 只证明本方准备了回复。它们各自不能证明对端收到，也不能证明物理动作完成。

D8 / A 已确认由本实体承接原 EcsSendEvidence 的权威发送证据。每个原命令实例与原 lease_id 的开始唯一，后续观察按原 ECS 串行事务给出明确替代顺序，由 ECS 专属读取适配器取得当前有效结论。同次尝试的完成结论取代历史 OPEN，不能按所有历史行中是否出现 OPEN 判断可能派发，也不能靠时间戳或 UUID 猜测替代关系。另一原租约的未决尝试单独解释，失效租约仍沿原栅栏记录迟到并维护必要核对。开始、完成、原响应引用及其恢复依据继续按原合同保护，普通摘要 90 天与诊断 7 天不能覆盖这项保护。

D9 / A 已确认由本实体承接 CallbackSnapshot 的请求预览及回复诊断，迁移核对和等价回归通过后退出旧快照实体。诊断与核心证据使用明确类型及写入合同，诊断仍在原独立故障边界内尽力保存，沿原每来源每进程 120 条 / 分钟、48 KiB 预览、两秒预算和来源保留期限。同次接收的核心原文明确存在时使用引用，不能额外复制一份预览；无法可靠关联的旧快照保留独立诊断身份，不产生受理资格或重放权威。旧 base64 仅还原已保存字节，保留截断与原 source / NULL 来源解释；旧 JSON 回复标记重编码，流失败的空预览不冒充完整空请求，调试内层协议回复与外层 HTTP 回复分别说明。

唯一约束覆盖 exchange_id 与 event_key。已有 event_key 遇到不同内容时按冲突处理，不能静默覆盖。记录时间加主键用于稳定分页，业务因果关系使用原引用和版本表达。

### 4.3 integration_reference

一条交互可以关联多个任务、动作或业务主体，使用关系表表达，并支持反向索引。

字段包含 exchange_id、reference_kind、namespace、reference_value、value_digest、relation_role、source_record_id 与 created_at。reference_kind 使用 COMMAND、WMS_OPERATION、TRANSPORT_TASK、BUSINESS_TASK、ACTION、BARCODE、DEVICE、WORKLINE、RECEIPT 或 EXCHANGE。

namespace 固定身份所属的业务 / 协议命名空间。EXCHANGE 类型通过单独的 related_exchange_id 外键引用本地交互，relation_role 表达方向，reference_value 保留可检索的原 UUID 字符串。其余类型的 related_exchange_id 必须为空。长 reference_value 用有界摘要索引，命中后核对原值。

同一个条码或设备的匹配只形成搜索候选。业务关联由原任务 / 动作持久化引用和可信组装器提供。原字段 business_key 在不同路径中含义不同，需按来源解释后添加明确类型的引用。

## 5. 数据格式约定

- 新增物理列使用 snake_case，普通枚举使用大写固定值，原 operation、task_type、event_type 保留原值。
- 新内部主键统一 UUIDv7。现有自增主键、协议 UUID 与事件摘要继续保持原始表示，并通过 identity_kind / reference_kind 区分。
- 数据库时间统一 UTC timestamptz，API 使用带 Z 的 ISO 8601。对端时间戳的原单位和值留在报文里，只有已确认单位的字段才产生 occurred_at 投影。
- 原文统一 BYTEA。普通查询不附带报文，原文接口按需返回原始字节，JSON 导出时明确标注 encoding、content、body_state 和摘要。
- 长期检索字段使用普通类型化列。metadata 采用 JSONB，并限制键、深度与字节数。其投影失败可标记 unavailable，不得因为诊断扩展内容改变业务校验结果。
- NULL 表示缺失或未知，空字节表示确认空报文。数值 0、false 与空集合均保留自己的含义。
- body_sha256 是保存字节的完整性摘要。现有 raw_digest、effective_digest、Result digest 和 ECS Event 身份继续使用各自原算法与输入范围。
- 统一的 schema_version 描述历史记录格式，对端协议版本单独保存。

JSONB 会改变对象键顺序并舍弃重复键，因此原始字节继续承担冻结请求和原文证据的职责。参考 [PostgreSQL JSON 类型说明](https://www.postgresql.org/docs/current/datatype-json.html)。

## 6. 写入与事务边界

### 6.1 出站

创建意图、建立 exchange、登记原请求和保存运行表引用使用调用方原事务。ECS 和 WMS 的业务工厂继续经既有命令 / 请求公开接口调用，可靠层只处理协议无关的投递事实。

在现有 before_send 门禁事务中记录 SEND_ATTEMPT_OPENED，完成登记提交后才可能调用网络。STATIC 和 SNAPSHOT 两条发送路径都接入。结果回写在原 finish / apply 事务中追加尝试结果，并在持有有效租约时更新投递状态。

既有 WorkLine、Device、RackJob、宿主主体、RackAction 及命令 / 消息的锁顺序继续由原入口负责。承接原命令 / 收据职责的 exchange 行在各自原协议路径的加锁位置获取，附加证据行的锁放在既有锁之后，同类附加证据行按主键排序。历史清理先调用对应来源的原保护入口取得必要业务锁并重读保护事实，再锁定和复核证据，禁止先锁证据再回头等待业务主体。

已失效租约的迟到结果可以沿原 attempt_id 追加观察，现有租约栅栏继续决定其是否有权改变执行状态。进程在网络完成与数据库提交之间退出时，历史保留 OPEN / UNKNOWN，后续恢复继续由原协议决定。

app/reliable 通过注入的协议无关记录端口调用统一存储，装配位于 app/reliable_runtime.py 与正式集成装配处。端口仅接收本事务会话、原消息 / 尝试引用和已有 HttpResult / Verdict，不解析业务载荷，也不创建事务或发送器。

### 6.2 入站

有合法原始身份且进入正式接收流程的请求，将核心请求原文、响应准备记录、收据和业务变化放在接收者原事务内。业务处理回滚时这些已受理事实一起回滚，对端收到既有临时不可用回复并沿原身份恢复。

重复收包生成新的本地接收 attempt_id，复用原收据 / exchange。冲突收包保留独立观察与冲突回复，不覆盖原请求。WMS recheck 的新提交响应追加记录，收据只更新当前响应引用。

D5 / A 已确认纯重复观察尽力保存。适用前提为已读取原持久裁决、完成原身份比较及必要 guard / recheck，并确定没有新增业务、协议或核对变化。观察写入使用与原裁决事务隔离的故障边界，沿现有诊断预算处理，失败仍返回原协议的重放回复，查询明确声明此类观察可能缺失。

是否纯重放由原接收路径明确提供，不能仅凭相同 operation_id、HTTP 状态或 outcome 文本判断。WMS _handle 的 Duplicate / Conflict、recheck 产生的新结果及新 Result 的核对状态变化仍可能包含核心写入，继续按原事务原子提交。读取原裁决或必要检查失败仍沿原错误路径处理。被丢弃的诊断事务不能污染原裁决事务，不能吞掉数据库错误后复用已经中止的事务。

无合法身份的请求形成独立诊断交互，遵循现有入口体积、速率与时间预算。事务失败的尝试可在独立诊断事务保存失败观察，标记未受理。数据库不可用或预算耗尽时无法保证落库，查询必须公开这一采集边界。HTTP 回复准备成功与对端接收成功分别表达。

统一记录中的核心权威证据写入失败随原事务处理。可丢弃的诊断扩展和非法请求预览沿现有诊断策略处理。两类路径在接口和测试中分开。

### 6.3 业务处理

RESULT_APPLIED 表示具体协议结果已被拥有者应用。BUSINESS_CONSUMED 由业务动作自己的结果消费事务记录，携带 action_id 与原版本。同一结果被多个动作消费时分别记录。

首个贯通用例覆盖粗分与共享 Transport。其他正式业务只投影已存在的持久化消费证据，尚未接入的消费阶段返回 UNKNOWN / 未采集，不从后续命令反推消费成功。查询服务无执行副作用，执行恢复仍使用既有公开结果读取接口。

## 7. 物理存储收敛

| 原结构 | 目标结构与准入条件 |
| --- | --- |
| ecs_send_evidence | D8 / A 已确认发送证据迁入 integration_record，ECS Result 准入通过专属读取适配器读取相同原命令实例 / lease_id 的当前有效结论，同次完成结论取代 OPEN。保留原唯一性、门禁原子登记、租约栅栏、迟到核对及恢复保护，迁移核对与等价回归通过后退出旧表 |
| callback_snapshot | D9 / A 已确认请求预览及回复诊断由 integration_record 承接，保留来源、限流、截断、独立失败边界及原期限，正文使用 BYTEA。旧快照不猜测关联，新历史入口展示可证明的诊断；迁移核对与等价回归通过后退出旧表 |
| ecs_result_evidence | D2 / A 已确认迁入 integration_record 的 RESULT_EVIDENCE 类型，保留原命令与 Result digest 的等价唯一约束、归一化 core、采用状态和原回复引用。迁移核对及原幂等、冲突、长期重放回归通过后退出旧表 |
| callback_receipt | D3 / A 已确认由 integration_exchange 入站角色承接原身份、摘要比较、幂等、当前回复引用和事件上下文。保留历史及新事件的数值 ID、顺序和到期释放受理资格，迁移验证通过后退出旧表 |
| reliable_message | 保留租约、重试、原 target / operation / operation_id 与状态。请求、最近响应和终局响应逐步转为 record 引用 |
| ecs_command | D4 / A 已确认由 integration_exchange 的 ECS 命令角色承接原命令身份、业务调用唯一关系、冻结配置、执行状态和恢复证据引用。保留原输入核对、门禁、锁顺序和长期重放，迁移验证后退出旧表 |
| transport_task 与业务表 | 保留各自事实、版本、消费证据与事务职责，补充明确关联引用 |

ECS request_sha256 在创建原字节时冻结。发送门禁加载引用字节后，同时校验记录自身摘要、命令独立冻结摘要与原路由快照，并继续核对适配器生成正文等于原冻结字节。原命令与 Outbox 的引用若不一致则拒绝发送。历史两份原文不一致的记录进入迁移异常清单并阻止该项收缩。

技术证据收敛后的公开 DTO 继续提供既有调用方需要的语义字段。需要保留的状态缓存逐项说明权威来源与更新事务，每个重复字段都必须具有明确读取或恢复用途。

## 8. 查询入口与权限

提供只读服务，并通过以下正式管理接口公开。

- GET /api/v1/integrations/exchanges
- GET /api/v1/integrations/exchanges/{id}
- GET /api/v1/integrations/exchanges/{id}/records
- GET /api/v1/integrations/records/{id}/body

列表支持原始编号、明确类型的任务引用、对端、方向、operation、设备、工作线和时间窗口。列表按 created_at / id 使用游标分页，详情时间线按 recorded_at / id 分页，每页最多 100 条。D10 / A 已确认普通时间范围检索单次最大跨度为 90 天，原始身份精确检索可覆盖整个合法留存期。相同外部身份的不同原生命周期分别展示，不只返回当前活动实例。

可直接用 SQL 查询 integration_exchange、integration_record 和 integration_reference，配套只读摘要视图统一提供交付、协议结果、业务应用与 coverage 字段。详情查询在独立只读 REPEATABLE READ 事务内取得一致视图，并保留字段的原证据引用。

新增 integration:history:read 与 integration:payload:read 权限，注册到 INITIAL_PERMISSIONS 并覆盖权限测试。普通列表与错误日志不返回原文、凭据或完整目标配置。原文接口使用独立权限，按现有管理操作审计模式留痕。

现有整数游标不能直接承接时间 / UUID 顺序。新查询参数在原 20 条默认 / 100 条上限及筛选白名单基础上，使用有界、版本化的时间 / UUID 复合游标，限制 256 字符并严格核对规范编码、时间、UUID、查询范围及排序类型。旧整数游标接口保持原合同。每页读取 limit + 1，不使用全表 COUNT 或深 OFFSET；一次详情读取的快照不等于跨所有分页请求冻结数据集。

原审计中间件跳过普通 GET，因此正文 GET 需要显式接入既有审计写入机制，沿现有旁路失败策略。记录操作者、动作、记录 UUID、路由和 trace，不缓冲或写入正文供审计；UUID 放入明确的 detail 引用，不强塞整数 resource_id。普通摘要 GET 保持现有不审计合同，正文读取自身不触发补录、结果应用或发送。

## 9. 索引与容量

新增索引围绕以下访问路径建立。

- exchange 的 peer_system / created_at / id，用于对端加时间的列表。
- exchange 的 created_at / id，用于跨对端时间窗口。
- reference 的 reference_kind / namespace / value_digest / exchange_id，用于原编号、任务和设备定位。
- record 的 exchange_id / recorded_at / id，用于交互时间线。
- record 的 exchange_id / event_key 唯一键。补录防重键由 source_kind / source_key、record_kind 与已保存源版本共同派生。
- 运行表的 exchange_id 与证据引用索引，覆盖迁移核对和清理检查。

任何可变长外部键先确认长度合同。需要使用摘要索引的字段命中后再比较完整原值，索引列只承载有界值。metadata 首版根据已确认查询添加所需表达式索引。

新表先建索引再启动补录。既有大表新增索引需要单独安排并发构建，检查失败留下的无效索引，并与 Alembic 事务边界协调。组合索引顺序按真实过滤条件验证。参考 [多列索引](https://www.postgresql.org/docs/current/indexes-multicolumn.html) 与 [并发创建索引](https://www.postgresql.org/docs/current/sql-createindex.html)。

用隔离 PostgreSQL 数据集分别验证标识符检索、90 天列表、单交互长时间线及清理批次，记录 EXPLAIN (ANALYZE, BUFFERS)、扫描行数、写入开销和报文体积。性能目标在取得线上规模与环境后确定。当前不预先承诺响应毫秒数。

## 10. 历史补录与切换

D6 / A 已确认采用受控维护窗口统一交接权威。准备与可选预补录阶段由旧存储承担运行权威，来源清理保护先于预补录启用。最终窗口确认所有 API 副本 / Sender、worker / 清理、后台写入及人工入口均已退出，取得稳定源数据并完成最终补录、原身份 / 版本 / 字节对账后，再统一启用新权威。原未决和在途命令、结果、冻结期限及租约继续按原身份保留，不能由停进程推断物理未执行。

窗口前核对对端原身份补报能力与现场维护条件，并按实际数据演练确定时间。最终对账包含预补录后发生改变的旧行，不能只扫源主键游标之后的新行。新权威已有受理或结果提交后，旧表存在不能单独证明可回退；回退适配或前向修复必须保住这些已提交新事实。旧表收缩继续是独立准入步骤。

1. 扩展迁移只新增表、可空引用与所需索引，保持一个 Alembic head。先完成 SQLite 与隔离 PostgreSQL 的升级 / 降级检查。
2. 在待切换版本中完成新记录接入原事务及运行引用适配，并以隔离库验证并发登记复用原唯一赢家、关联与业务一起提交 / 回滚。准备及预补录时仍保持旧权威，不提前启用仅写新存储的核心路径。
3. 使用按源表主键游标推进的有界批次补录，每批提交并保存进度。来源行与实时写入沿相同映射防重，失败可以重跑。
4. 对可变源行先按原顺序锁定并读取当前版本，使用 guarded update 绑定 exchange_id。回填只设置仍为空且版本一致的引用，已经被实时路径绑定则复用其实例。历史提取记录 source_version，有原版本时使用原版本，无版本时对本批实际读取的字段计算快照摘要。快照摘要只标识观察到的快照，不能充当结果业务版本或推导未知的发生时间。静态摘要不覆盖实时事实。
5. 补录按来源固定保存可用字节与字段。旧 WMS 尝试次数只记录次数及可用最近 / 终局响应，旧 Result core 重编码标记 LEGACY_REENCODED，不补造已丢失报文、完成时间或发送尝试。
6. 在 D6 的最终停写窗口确认已列明 API / Sender、worker / Beat、后台及人工写入者与旧数据库写事务退出，取得稳定来源后重核所有可变行，再作分来源计数、身份、字节摘要、采用状态、版本和代表性交互对照。尚不能证明退出或发现不一致时保留来源并阻止切换，不能以脚本未发现未知写入者冒充排空证明。
7. 公开读口、内部恢复读口、调试服务、设备历史保留检查与清理任务全部适配且最终对照通过后，统一启动新权威；验证完整的创建、发送、回调、消费、重启和清理周期。原在途身份及未知事实按原合同恢复。
8. 读写路径全部切换且对照通过后，实施独立收缩迁移，移除已失去用途的旧技术证据表及重复原文字段。

补录与来源清理不能并发删除同一批尚未迁移的证据。上线顺序先升级清理准入，要求候选证据已迁移且必需引用存在，再开启补录和收缩。清理保护只对迁移涉及的候选记录生效。

扩展阶段可回退兼容应用版本，保留已新增证据。收缩阶段需要全部 API / worker 实例已升级并排空旧进程，随后重新核对清单。数据已写入时降级保护阻止删除证据。任何持久化历史不重新登记运行意图。

## 11. 保留与清理

运行恢复、结果消费和历史查询的保留期限分别管理。阶段一延续现有业务保护与原默认值，阶段二在引用切换前接入原清理流程。

运行表持有的证据外键采用 RESTRICT。仍被原命令、收据、未决任务、结果校验或业务消费义务引用的证据必须保留。外键保护记录存在，报文字段的修改 / 清空还需要同一清理事务核对使用者并取得锁，禁止绕过引用清空。

清理沿“候选轻读 → 按原保护入口锁定必要主体 → 重读保护事实 → 按原顺序锁根与证据 → 最终复核 → 本批清理”的次序执行。整数事件列、JSON application_ref、canonical / predecessor 事件、业务消息与结果消费引用均纳入原保护检查，不能只检查数据库外键。

清空正文与删除记录使用同一保护准入。取得必要主体锁后，才对候选记录取得 FOR UPDATE 并最终核对引用；新增或切换运行证据引用也沿自己的原锁顺序核对正文可用性、摘要和长度。无法核对保护义务或并发资格不确定的候选保留，后续批次继续处理。记录只允许保留流程修改正文可用性及清理时间，历史事实字段保持追加式。

历史元数据与报文体积分别统计。已满足原清理准入、无运行引用的诊断报文可以转为 PURGED，并保存摘要、长度及清理时间。不能确定是否曾采集的旧数据使用 UNAVAILABLE / LEGACY，不标记为已清理。

原运行表按自身合同清理后，历史可继续在其配置窗口内查询。历史存在不阻止合法的新实例，也不改变公开结果读取接口在运行证据到期后的 NOT_FOUND / UNAVAILABLE 行为。

原保留合同分别继承如下。

| 来源 / 用途 | 原默认值及起算 | 统一存储中的约束 |
| --- | --- | --- |
| 诊断预览与补充观察原文 | 7 天，沿原接收 / 观察时间 | 仅无核心恢复用途且无业务保护的诊断正文可以按此到期 |
| 原收据受理资格及必要回复 / 事件依据 | 90 天，沿原收据 created_at | 保留原业务保护，到期只按原准入释放入站受理资格，命令角色身份独立 |
| 可靠消息终态 | 90 天，沿原终态 updated_at | 仅原 TERMINAL 候选且无保护责任时可清理，HELD / 未决仍沿原合同 |
| ECS 原命令、Result 裁决和原回复 | 沿原长期恢复合同，无新增统一自动到期 | 原调用唯一、Result 冲突检测及长期重放继续可用 |
| 未决、核对、待消费及必要前序 / 锚点 | 责任仍需事实时继续保护 | 业务主体终态、TTL 到期或表面完成不能单独证明责任收口 |

补录 recorded_at、重复通知和普通维护不重置原资格或证据的保留时钟。诊断原文与普通历史摘要分开统计和清理。D7 / A 已确认无额外运行保护的普通历史摘要及查询索引默认保留 90 天，沿原事实时间起算。原文继续服从原来源期限与上述保护，摘要存在不保证原文仍在；该窗口不能覆盖运行和恢复证据的原合同。

## 12. 改动面与验证

实现会涉及 app/models 及导出、app/integrations 下的共享历史模块和各协议读写口、app/reliable 的中立记录端口、app/reliable_runtime.py、三类回调控制器、正式历史查询控制器、原调试查询服务、清理任务、首批业务关联提供者、权限种子、迁移与集成文档。

现有 tests/reliable、tests/integrations/{ecs_command,ecs_callback,wms_request,wms_callback,wms_transport}、tests/tasks、tests/controllers 和 tests/migrations 的行为合同继续回归。新增测试聚焦公开查询与下述长期行为。

| 验证情境 | 必须成立 |
| --- | --- |
| 创建 / 受理 / Result 回滚 | 原运行变化与权威历史一起回滚，无已应用假记录 |
| 同身份并发创建 | 一个原执行实例，统一引用匹配唯一赢家 |
| 两次发送，一次租约过期 | 两次尝试可区分，迟到观察无权覆盖新租约状态 |
| ECS 超时与重启 | 保持原身份，未知交付进入原恢复路径 |
| 原 Result 重报与冲突 Result | 分开记录观察与采用结果，原冲突检测与重放行为保持 |
| WMS NG、HTTP 成功与业务消费 | 三类状态可分别查询 |
| WMS guard / recheck | 每次已提交响应可追踪，当前原响应引用正确 |
| 多动作消费一个结果版本 | 分动作呈现真实消费证据 |
| 旧补录与实时回调并发 | 实例引用一致，旧快照不覆盖新结果 |
| 补录中断、重跑与来源清理 | 无重复证据、无来源丢失、无额外发送 |
| 原文不一致、错误引用与摘要异常 | 阻止相应发送或收缩，保留核对依据 |
| 历史未采集、预览与清理 | 返回对应 body_state 和 coverage |
| 公开读口与权限 | 原调用方合同兼容，列表无原文，正文权限独立 |
| PostgreSQL 迁移与索引 | 单 head，锁顺序与约束通过，记录实际执行计划 |

第一步交付统一格式、归集查询、补录与读权限。第二步迁移技术证据及原文权威来源，完成恢复和清理闭环。第三步执行已验证的存储收缩与查询性能调整。三步的验证结果分别报告。

## 13. 已确认设计与实施前数据核对

本设计建议采用上述三步完整改造，以内部记录格式统一、三类存储与逐步收缩为范围。当前默认调用方是后台排查人员，交付数据库查询与正式只读 API。普通历史摘要及查询索引按 D7 / A 默认保留 90 天，原文按来源期限保留，运行和恢复证据继续服从既有保护。

数据库版本、生产表规模、最长任务持续时间、现场维护条件和实际可用的维护时间尚未核对。这些数据影响索引上线、容量与最终清理配置，进入部署阶段前必须取得。普通摘要 90 天及普通单次查询跨度 90 天已分别按 D7 / A 与 D10 / A 确认，不能把尚缺容量数据重新表述为这些设计取值未决定。

## 14. 本次已执行的设计基线核对

- 离线 Alembic heads 返回单一 coarserackstage3。
- 在独立工作区运行现有 ECS flow / Result、WMS 原结果读取、租约栅栏和收据清理测试，结果为 82 passed、49 skipped。该次显式留空 TEST_DATABASE_URL 与 TEST_REDIS_URL，PostgreSQL 专项未执行。
- 上述测试验证当前实现的基线行为，尚不能证明本设计的迁移、并发或性能结果。

## 15. 工程评审的范围记录

评审目标固定为本设计文档。报告和决定记录也保存在本文件，当前评审仍在进行。

feature answers 为无功能裁剪提案，完整三步范围仍是原提案。structure 为 D1 / B，实际答复为“选 B，按现有分层收敛，但我希望能把现有模型合并，减少重复字段在不同的实体中，你有什么建议？”。accepted scope 为沿既有项目分层安排代码，不另建历史管理器或逐协议历史服务。该答复确认模块组织，并提出进一步合并模型的目标，未确认具体模型的退出和字段删除。

已确认的安排如下。

- 三个共享数据模型集中于 app/models/integration_history.py。
- 共享历史写入与原文读取放在 app/integrations/history.py。
- 正式查询放在 app/services/integration_history_service.py，对应现有 controllers 与 schemas 分层。
- 补录入口为 scripts/backfill_integration_history.py，记录端口声明和装配复用原 reliable / reliable_runtime 边界。
- ECS 准入继续位于原 ECS 模块，清理复用现有 tasks。具体数据模型数量与内容按后续模型合并审查调整。

R3a 清理与引用保护按原合同承接，R3b 普通历史窗口已由 D7 / A 确认。R1 的核心原子持久化由原合同及 D2～D4 固定，纯重复观察的补充策略 R1a 已由 D5 / A 确认，权威切换 R2 已由 D6 / A 确认。R4、R5、R6 分别由 D2 / A、D3 / A、D4 / A 确认，R7 发送证据归属已由 D8 / A 确认，R8 诊断快照归集已由 D9 / A 确认，R9 查询跨度已由 D10 / A 确认。四个工程评审阶段的设计选择均已收口，无未回答的工程选择；继续独立复核可用性检查与报告收口。

Scope Challenge 的 F-S1 Result 实体归属、F-S2 收据实体归属、F-S3 命令实体归属均已由各自实际答案确认合并；当前没有等待答案的 Scope Challenge 选择。三个事项的迁移与验证作为必要工作继续保留，后续进入 Architecture、Code Quality、Tests、Performance 的顺序评审。

## 16. Decision ledger

### R4. ECS Result 语义证据的物理归属

Finding 为 F-S1 / P2 / confidence 9/10，由当前主评审提出。依据本设计第 7 节原提案中的“ecs_result_evidence | 保留原命令与 Result digest 的唯一约束、采用状态和语义校验依据，原入站 / 回复字节通过 record 引用取得”。原提案对该模型主要迁出原文，用户当前希望进一步合并实体、减少跨实体重复字段，因此需要明确是否也归集 Result 的权威语义证据。这是设计取舍，尚无运行缺陷结论。

Plan baseline 现为 D2 / A，EcsResultEvidence 的权威语义证据合入 integration_record，并保留原幂等、冲突检测和长期重放。D1 / B 的既有项目分层安排继续有效。

Runtime evidence 如下。

- app/models/ecs_command.py 第 50 行为 `__table_args__ = (UniqueConstraint("command_code", "digest"),)`，第 54～57 行包含 core、status_code、response 和 applied。
- app/integrations/ecs_command/result.py 第 85～88 行在已有证据时比较 core 并返回原 status_code / response，然后才会在第 89 行解析历史合同。
- 同文件第 169～188 行分别向 EcsResultEvidence 和 CallbackReceipt 写入原 status_code 与 response.body。
- tests/integrations/ecs_command/test_flow.py 的 test_result_survives_receipt_and_delivery_cleanup 覆盖 Outbox / 收据清理与历史合同不可用后的原 Result 重放和原命令找回。

Comparison grid 如下。

| 选择项 | 当前原提案 | A | B | 依据与状态 |
| --- | --- | --- | --- | --- |
| R4 Result 权威语义证据 | EcsResultEvidence 独立保存 | integration_record 保存，迁移通过后退出 EcsResultEvidence | 继续独立保存，正文改用公共记录引用 | D2 / A 已确认 |
| 原 command_code + digest 的唯一性 | 原表约束 | 统一记录上等价约束，Result 类型与其他观察隔离 | 保留原表约束 | 既有幂等合同，两项共同要求 |
| 原归一化 core 与原回复 | 原表字段 | 类型明确的持久结果事实及原回复引用 | 原语义字段及原回复引用 | 既有原响应重放合同，两项共同要求 |
| 原 Result 长期重放 | 不依赖公共收据和 Outbox 存续 | 由原命令关联和等价保留准入保护 | 由原证据行和原命令保护 | 现有公开行为与回归用例，两项共同要求 |
| 代码组织 | D1 / B | 沿现有分层 | 沿现有分层 | 用户 D1 / B 已确认 |
| R1、R2、R3 | 待评审 | 仍待决定 | 仍待决定 | 本题不改变其状态 |

Question D2 如下。

D2 · 是否把 ECS Result 证据也合入统一证据模型？

项目为 maintenance-admin，分支为 feature/integration-history-unification，D1 已选择沿现有分层收敛。原稿第 7 节仍保留独立 EcsResultEvidence，现在需要决定是否进一步合并这个实体。

ECS Result 的原回复目前重复保存在结果证据和公共收据中，归一化内容保存在结果证据中。合并后，一条权威结果事实可以提供原内容、采用情况和回复引用，但它必须在公共收据及投递记录清理后继续支持原样重放。合并失误会使设备重报得到不同回复，或者让已经确认的结果丢失。

Recommendation 为 A，因为它更符合当前减少实体和重复字段的目标，并且能够把已有的唯一约束和长期重放合同一起迁移。

两项区别在模型归属，覆盖相同的既有行为，不作完整度评分。工时为本项数据模型、读取适配和回归验证的粗估，未包含整项改造或生产上线。

Header 为 Result证据合并。

Options 如下。

A) 合入统一证据 (recommended)

Result 的归一化内容、采用情况和原回复引用进入 integration_record，迁移核对通过后退出 EcsResultEvidence，能实质减少一个独立模型及跨实体的回复副本。数据库继续保证原命令与 Result 摘要的唯一性，重复接收观察单独记录，并沿原命令保护长期重放。代价是需要迁移 ECS 的受理、冲突检测和公开读取路径，粗估人工 1～2 天 / CC 2～4 小时，风险集中在约束与清理保护的等价性。

B) 保留结果语义表

继续由 EcsResultEvidence 保存 Result 语义，只把原报文和回复改为共享记录引用，能够减少大字段重复并延续现有准入查询结构。独立结果表继续承载原唯一约束和长期重放，采用情况与历史观察之间通过明确引用关联。代价是继续维护两个证据实体和相应读取边界，模型数量减少较少，粗估人工 0.5～1 天 / CC 1～2 小时。

Net 为两项都必须保住原 Result 的幂等和长期重放，本题选择是否同时收敛其独立实体。Pending remedies not decided here 为 R1、R2、R3。请回复 A 或 B。<gstack-qid:plan-eng-review-result-model-consolidation>

State 为 approved。

Actual answer 为用户 D2 / A，“选 A，把 EcsResultEvidence 合入统一证据模型，保留原幂等、冲突检测和长期重放能力，继续工程评审。”

Accepted scope 为将 EcsResultEvidence 的归一化内容、采用情况和原回复引用迁入 integration_record，通过等价唯一约束及原命令保留保护维持原幂等、冲突检测和长期重放。必要的读取适配、迁移核对与回归证明属于同一选择。旧表的退出遵循既定切换和收缩准入，当前阶段继续执行设计评审。

History 为原提案保留独立 EcsResultEvidence，D2 首次提出合入统一记录并保存 A / B 完整比较，用户选择 A 后按上述范围更新。

### R5. 收据实体是否由交互主实体承接

Finding 为 F-S2 / P2 / confidence 9/10，由当前主评审提出。第 7 节原提案保留 callback_receipt，第四节又为相同入站交互创建 exchange。用户当前要求尽量合并实体，因此需决定是否取消两者的一对一绑定、由 exchange 承接原收据职责。这是候选结构评估，不代表现有接收行为存在缺陷。

Plan baseline 现为 D3 / A，由 integration_exchange 承接 CallbackReceipt 的入站受理职责，保留原数值事件 ID、顺序、幂等及到期释放受理资格。D1 / B 与 D2 / A 继续有效。

Runtime evidence 如下。

- app/models/callback.py 第 21 行为 `UniqueConstraint("operation", "operation_id")`，第 25～26 行为 RAW / EFFECTIVE 与 effective_digest 的 CHECK，第 31～45 行保存数值 id、原身份、摘要、响应和事件上下文。
- app/integrations/callback_receipts.py 的 insert_receipt 使用原 operation / operation_id 抢占唯一身份，find_receipt 的 for_update 路径锁原收据。
- app/integrations/ecs_callback/results.py 第 19 行为 `canonical_event_id: int`，第 113～115 行按数值 event_id 读取收据，第 141～144 行按收据 id 做游标与倒序检索。粗分任务及 BIN 回流另有持久化事件引用。
- app/tasks/callback_tasks.py 第 41～43 行说明超期旧身份按新消息处理，第 67～70 行先计算业务保护再删除候选收据。
- app/integrations/wms_callback/receiver.py 的 _receive 在已有收据上做摘要比较、guard 和可选 recheck，_handle 在原保存点及事务内更新响应。

Comparison grid 如下。

| 选择项 | 当前提案 | A | B | 依据与状态 |
| --- | --- | --- | --- | --- |
| R5 入站交互 / 收据根 | 两个实体一对一绑定 | 同一个 exchange 承接入站收据职责，验证后退出 CallbackReceipt | CallbackReceipt 保留为精简受理实体并引用 exchange / record | D3 / A 已确认 |
| 原数值收据身份与顺序 | 原自增 id 及业务引用 | 保留历史及新事件的稳定数值身份，原引用及顺序可继续读取 | 沿原表身份保留 | 现有业务与公开读取合同，两项共同要求 |
| 受理幂等键 | 有效收据上的 operation / operation_id | 仅仍占受理资格的交互保持等价唯一性，过期释放资格，历史可继续查询 | 原收据清理后释放唯一键，独立历史继续查询 | 现有清理与新实例合同，两项共同要求 |
| 摘要比较、guard / recheck 与回复 | 原接收器与当前回复 | 保留原比较和事务路径，更新当前回复引用并追加已提交历史 | 原收据保存同等比较状态与当前回复引用 | 现有接收合同，两项共同要求 |
| D2 Result 合并 | 已确认 A | integration_record 的 RESULT_EVIDENCE | integration_record 的 RESULT_EVIDENCE | D2 / A，保持已确认值 |
| EcsCommand 吸收候选 R6 | 尚未决定 | 仍待单独评估和决定 | 仍待单独评估和决定 | 本题不包含该候选 |
| R1、R2、R3 | 待评审 | 仍待决定 | 仍待决定 | 本题不改变其状态 |

Question D3 如下。

D3 · 是否把 CallbackReceipt 并入交互主实体？

maintenance-admin 的代码组织已按 D1 / B 确认，Result 证据已按 D2 / A 确认合并。本题继续评估非业务子包实体，只决定收据与入站交互是否共用一个主实体。

一条入站消息在原提案里会有一个收据和一个交互主记录，两边重复表达原身份和受理时间。可以让交互主实体直接承担收据职责，省去一张表及其绑定；但原数值事件 ID 已被业务保存并用于排序，收据过期后的受理资格也必须能够释放。处理失误会使原事件读不到、顺序改变，或者使合法新实例被旧历史挡住。

Recommendation 为 A，因为入站交互与原收据指向相同的一次受理，将其合并能直接消除重复实体和字段。

两项区别在模型归属，覆盖相同的现有接收合同，不作完整度评分。以下是本项设计、接收器适配与回归的粗估，未包含整项改造或生产上线。

Header 为 收据实体合并。

Options 如下。

A) 并入交互主实体 (recommended)

将原收据的身份、摘要比较状态、当前回复引用及事件上下文归入 integration_exchange 的入站角色，能够消除两张根表之间的一对一绑定和重复交互字段。保留历史及新事件的稳定数值身份、原重报处理与到期释放受理资格的行为，已提交的回复历史继续进入统一记录。代价是要迁移原事件读口、清理准入和收据锁定位，粗估人工 2～3 天 / CC 3～6 小时，维护重点是原业务引用与新旧实例切换。

B) 保留精简收据实体

CallbackReceipt 继续承载原数值身份、摘要比较和受理资格，只把报文与回复改为统一证据引用，能够沿用现有事件读取和收据清理入口。交互主记录仍提供统一查询，原收据的并发唯一与重检事务较容易逐项对照。代价是保留两个实体及其生命周期关联，原身份和部分索引信息仍需跨实体管理，粗估人工 1～2 天 / CC 2～4 小时。

Net 为两项都保留原受理、重报和数值身份合同，本题选择是否把收据职责直接放进交互主实体。Pending remedies not decided here 为 R1、R2、R3 和命令吸收候选 R6。请回复 A 或 B。<gstack-qid:plan-eng-review-receipt-entity-consolidation>

State 为 approved。

Actual answer 为用户 D3 / A，“选 A，把 CallbackReceipt 并入交互主实体，保留原数值事件 ID、顺序、幂等和到期释放受理资格的行为，继续评审 EcsCommand 的合并。”

Accepted scope 为由 integration_exchange 的入站角色承接原收据身份、摘要比较状态、当前回复和事件上下文，迁移验证后退出 CallbackReceipt。原数值事件身份及排序覆盖历史和新事件，原重报、并发唯一、guard / recheck、保存点和到期释放行为属于必要兼容合同，相关适配及回归一并承接。当前继续执行设计评审。

History 为原提案保留独立 CallbackReceipt，D3 首次比较吸收与精简保留两个安排，用户选择 A 后按上述范围更新。

### R6. 命令实体是否由交互主实体承接

Finding 为 F-S3 / P2 / confidence 9/10，由当前主评审提出。第 7 节原提案继续保留 ecs_command，第四节又创建共享交互主实体，两个根表达原命令身份、归属、类型与创建时间。用户已要求进一步减少非业务子包中的实体，并在 D3 后明确要求继续评审 EcsCommand 合并，现需确定命令根的物理归属。

Plan baseline 现为 D4 / A，由 integration_exchange 的 ECS 命令角色承接 EcsCommand，保留原身份、调用唯一性、冻结配置、执行状态与恢复合同。D1 / B、D2 / A、D3 / A 继续有效。

Runtime evidence 如下。

- app/models/ecs_command.py 第 11 行为 `__table_args__ = (UniqueConstraint("owner_domain", "call_key_digest"),)`，第 12 行以 command_code 为主键。第 16～31 行保存业务输入、冻结路由和请求、期限、执行与投递阶段、ACK、核对状态及采用的 Result。
- app/integrations/ecs_command/service.py 第 126～128 行比较完整 call_key 和原规范化业务输入，第 130～140 行命中原调用时直接返回；未命中才在第 160 行附近创建新 command_code，并在第 198～220 行登记唯一赢家及 Outbox。
- app/integrations/ecs_command/lifecycle.py 第 17～25 行先锁原命令，第 38～48 行执行发送门禁。app/reliable/lifecycle.py 的 SnapshotTransactions.locked 随后锁原 ReliableMessage，Result 接收也沿相同的命令先于消息顺序。
- tests/integrations/ecs_command/test_flow.py 第 303 行的 test_result_survives_receipt_and_delivery_cleanup 在 Outbox / 公共收据删除且当前附录清空后，要求原 Result 重放返回原回复，原业务调用仍返回原命令且没有新 Outbox。
- 当前命令与 Outbox 的阶段各有意义。test_first_result_before_http_response_applies_once 和 test_sender_matrix_and_command_projection 覆盖 Result 早于 ACK 以及交付与命令投影差异。

Comparison grid 如下。

| 选择项 | 当前提案 | A | B | 依据与状态 |
| --- | --- | --- | --- | --- |
| R6 命令根物理归属 | EcsCommand 独立，绑定 exchange | exchange 的 ECS 命令角色保存原命令根，验证后退出 EcsCommand | EcsCommand 保留为精简执行实体，引用共享交互与证据 | D4 / A 已确认 |
| 原命令身份和业务调用唯一性 | command_code，owner + call_key_digest，完整输入核对 | 同等角色约束与完整输入核对，沿原命令长期保留 | 原命令表约束及输入核对，沿原命令长期保留 | 既有命令及原调用重入合同，两项共同要求 |
| 原冻结内容与恢复 | 原请求、路由 / 附录、期限、状态与核对标记 | 命令角色及证据引用持有，保持原校验与恢复行为 | 精简命令实体及证据引用持有，保持原校验与恢复行为 | 既有发送与 Result 合同，两项共同要求 |
| 执行与投递状态 | 命令投影与 Outbox 状态各有语义 | 分别表达，原公共 CommandView 语义兼容 | 分别表达，原公共 CommandView 语义兼容 | 现有公开读取合同，两项共同要求 |
| 原锁顺序及租约 | 原命令 → 对应 Outbox，原租约栅栏 | 命令 exchange → 对应 Outbox，原租约栅栏 | 精简命令 → 对应 Outbox，原租约栅栏 | 现有并发及回滚合同，两项共同要求 |
| 收据资格释放 | D3 / A 已确认 | 仅作用于入站受理角色，命令原调用唯一性继续保留 | 同样保留 D3 / A 与原命令寿命 | D3 / A 及原恢复合同，均为固定值 |
| Result 权威证据 | D2 / A 已确认 | integration_record 的 RESULT_EVIDENCE | integration_record 的 RESULT_EVIDENCE | D2 / A，均为固定值 |
| R1、R2、R3 | 待评审 | 仍待决定 | 仍待决定 | 本题不改变其状态 |

Question D4 如下。

D4 · 是否把 EcsCommand 并入交互主实体？

maintenance-admin 已确认沿现有分层组织代码、归集 Result 证据，并由交互主实体承接入站收据。本题只决定 ECS 命令是否也使用这个主实体保存原身份与执行状态。

命令表与交互主记录会重复表达原命令身份、归属和创建信息。合并能够减少一张表及其绑定，但交互主实体也将承担原命令的恢复职责。收据到期释放受理资格时，命令的业务调用身份仍须长期保留；否则旧任务恢复可能生成新 command_code，再次触发设备动作。

Recommendation 为 A，因为它能继续减少独立实体和重复交互字段，同时通过明确的 ECS 命令角色保留原执行合同。

两项区别在模型归属，覆盖相同的现有命令行为，不作完整度评分。以下是本项设计、命令读写适配与回归的粗估，未包含整项改造或生产上线。

Header 为 命令实体合并。

Options 如下。

A) 并入交互主实体 (recommended)

将原 command_code、业务调用唯一关系、冻结路由及命令执行状态归入 integration_exchange 的 ECS 命令角色，能够消除独立命令根和重复交互字段。原幂等、输入核对、发送门禁、ACK / Result 引用、核对标记和长期重放均按现有合同承接，命令角色用明确约束表达，并继续先锁原命令再锁对应 Outbox。代价是共享主实体的类型约束和清理准入需要同时覆盖命令与收据，粗估人工 2～3 天 / CC 3～6 小时，维护重点是角色隔离及恢复行为的等价性。

B) 保留精简命令实体

EcsCommand 继续保存原调用唯一关系、冻结配置和命令执行状态，报文与结果通过统一证据引用读取，能够减少正文重复并沿用现有命令锁定位。共享交互负责统一查询，命令恢复仍直接定位原执行实体，类型边界较容易逐项对照。代价是继续维护命令与交互两个实体及其绑定，重复交互字段减少较少，粗估人工 1～2 天 / CC 2～4 小时。

Net 为两项都保留命令原身份、原执行与恢复合同，本题选择是否将其原命令根归入共享交互。Pending remedies not decided here 为 R1、R2、R3。请回复 A 或 B。<gstack-qid:plan-eng-review-command-entity-consolidation>

State 为 approved。

Actual answer 为用户 D4 / A，“选 A，继续后续评审”。该答复对应当前唯一未回答的 D4 命令实体归属选择。

Accepted scope 为由 integration_exchange 的 ECS 命令角色承接原 command_code、业务调用唯一关系、冻结配置、执行状态及恢复证据引用，迁移验证后退出 EcsCommand。原输入核对、发送门禁、命令投递阶段语义、ACK / Result、核对标记、命令先于 Outbox 的锁顺序及长期重放为必要兼容合同，相关适配和回归一并承接。当前继续执行设计评审。

History 为原提案保留独立 EcsCommand，D4 首次比较吸收与精简保留两个安排，用户选择 A 后按上述范围更新。

### R1a. 纯重复回调的补充观察写失败时如何回复

Finding 为 F-A1 / P1 / confidence 9/10，由当前主评审提出。第 6.2 节同时规定“重复收包生成新的本地接收 attempt_id，复用原收据 / exchange”和“统一记录中的核心权威证据写入失败随原事务处理”，但没有定义已取得原裁决的纯重放与其补充观察是否使用相同的成功条件。合表后的原裁决与新增收包观察具有不同用途，需明确后者是否增加回复依赖。

Plan baseline 现为 D5 / A，纯重复观察尽力保存，写入失败不影响原重放回复；新受理和新裁决仍保证权威证据原子提交。D2 / A、D3 / A、D4 / A 的原身份和恢复合同继续有效。

Runtime evidence 如下。

- app/integrations/ecs_callback/receiver.py 第 146～148 行命中原收据后 `return duplicate(existing)`，沿原收据返回并跳过后来收紧的业务校验。
- app/integrations/ecs_command/result.py 第 85～88 行核对已有 core 后 `return EcsResponse(evidence.status_code, evidence.response, "duplicate")`。新 digest 的拒绝、核对标记和 Outbox 变更属于另外的核心裁决。
- app/integrations/wms_callback/receiver.py 第 78 行的 _replay 保留原 RECEIVED 转为 DUPLICATE 的规则。原身份比较和 guard 必须先完成；recheck 返回 None 才能沿对应原重放路径返回。_handle 的 Duplicate / Conflict 可以保留业务写入，不能按 outcome 文本判断为纯重放。
- app/controllers/wms_events.py 第 71～94 行先完成原接收事务再挂快照后台任务；app/integrations/wms_callback/snapshots.py 第 49～71 行使用独立事务及预算保存预览，并自行处理失败。tests/controllers/test_wms_events.py 的 test_snapshot_storage_failure_does_not_change_committed_ack 与 test_snapshot_timeout_does_not_change_ack 覆盖观察失败不改原 ACK。

Comparison grid 如下。

| 选择项 | 当前行为 / 提案 | A | B | 依据与状态 |
| --- | --- | --- | --- | --- |
| R1a 纯重复观察写失败后的回复 | 现有诊断失败不改回复，新统一记录未明确 | 按原协议重放，观察尽力保存 | 本次观察成功持久化才重放，否则返回可重试 503 | D5 / A 已确认 |
| 纯重放准入 | 原身份及 guard / recheck 分支决定 | 已取得原持久裁决，原比较 / guard / recheck 确认无新变化 | 相同前提 | 两项共同前提，不能仅凭 HTTP 状态或 outcome 推断 |
| 核心状态与证据 | 原受理 / Result 原子提交 | 新受理、重检变化、裁决和核对标记仍核心原子提交 | 同样核心原子提交 | 既有事务合同与 D2～D4，两项固定 |
| 重复处理的业务副作用 | 原幂等及重放合同 | 沿原回复规则，避免重新应用 | 沿原回复规则，503 只请求原身份重报 | D2～D4，两项固定 |
| 历史采集声明 | 原快照可限流 / 截断 / 失败 | 重复观察明确声明尽力采集，行数不能证明总收包次数 | 仅成功保存本次观察才重放原回复，数据库故障时仍不能声称采集了所有失败请求 | 两项需准确声明各自采集边界 |
| 原角色合并 | D2 / A、D3 / A、D4 / A | 保持已确认值 | 保持已确认值 | 已确认固定值 |
| R2 / R3 | 待评审 | 仍待决定 | 仍待决定 | 本题不决定切换或清理机制 |

Question D5 如下。

D5 · 纯重复回调的观察记录写失败，要不要影响原回复？

maintenance-admin 已确认 Result、收据和命令的归集。本题只讨论已经读取原持久裁决、完成原身份比较及必要 guard / recheck，并确定没有新增业务或协议变化的纯重放。

设备重复上报一个已经处理的结果时，系统能够查到原结果和原回复。此时新增“这次又收到一次”的观察可能保存失败。需要决定是仍按原协议回复，还是要求对端稍后用原身份再报一次。选错会造成重复观察缺失，或者让已经完成的交互因为补充记录故障持续收到 503。

Recommendation 为 A，因为核心事实已经持久化，补充观察使用独立故障边界能够延续现有诊断失败不改变回复的行为。

两项区别在回复和采集策略，不作完整度评分。以下为本项故障隔离和回归的粗估，未包含整项改造或生产上线。

Header 为 重复观察失败。

Options 如下。

A) 保持原重放回复 (recommended)

纯重复观察采用尽力保存，失败时仍按原协议返回已有回复，能够使已完成交互在补充采集故障时继续收口，并延续当前诊断快照的故障边界。原裁决读取、身份比较及必要 guard / recheck 仍必须成功，新受理、重检变化和新裁决继续与权威证据原子提交。代价是重复观察可能缺失，查询需要明确采集范围，不能用记录条数宣称全部收包次数，粗估人工 0.5～1 天 / CC 1～2 小时。

B) 保存观察后才重放

纯重复回调也要求本次观察持久化成功才返回原回复，否则返回可重试 503，能够让每次成功重放都具有对应的已保存观察。原业务状态继续沿用已有裁决，原身份和必要 guard / recheck 仍按原协议处理，对端再次重报也不重新执行。代价是补充采集成为每次重放的成功依赖，存储压力下可能增加对端重报与数据库负载，粗估人工 0.5～1 天 / CC 1～2 小时。

Net 为两项都保护原裁决与业务幂等，本题取舍是重复观察可用性与重放回复可用性。Pending remedies not decided here 为 R2、R3。请回复 A 或 B。<gstack-qid:plan-eng-review-duplicate-observation-failure>

State 为 approved。

Actual answer 为用户 D5 / A，“选 A，纯重复观察尽力保存，写入失败不影响原重放回复；新受理和新裁决仍保证权威证据原子提交，继续后续评审。”

Accepted scope 为在已读取原持久裁决并完成原比较 / guard / recheck，确认没有新增变化的纯重放路径中，补充观察尽力保存且失败不改变原回复。必要的故障隔离、查询采集声明及 ECS Event / Result / WMS 各分支回归随同承接。新受理、重检变化、新裁决及核对标记继续核心原子提交，原裁决读取和检查失败仍沿原错误路径。

History 为从 R1 拆出的独立策略，原提案未明确该补充记录对回复的影响，D5 保存完整 A / B 比较后用户选择 A；核心证据原子持久化继续由原合同及 D2～D4 固定。

### R2. 新旧权威来源的切换路线

Finding 为 F-A3 / P1 / confidence 9/10，由当前主评审提出。第 10 节要求“新记录接入原事务”“读写路径全部切换且对照通过后”收缩旧结构，并要求全部 API / worker 实例升级，但没有给旧版本写入者退出、混合版本读写与权威交接的完整机制。仅按源主键完成一次补录，不能证明此后旧版本不会继续修改已经复制的行。

Plan baseline 现为 D6 / A，受控维护窗口统一交接权威，保留原在途命令与结果，确认全部旧写入者退出且最终对账完成后再启用新权威。D1～D5 继续有效。

Runtime evidence 如下。

- docs/deploy.md 第 278～297 行已有停 nginx / api / worker / beat、最终备份、独立迁移、启动并核镜像的部署流程。该文档是仓库部署基线，当前实际现场状态尚未核验。
- app/main.py 第 83 行及 app/reliable_runtime.py 第 78 行会在每个 API lifespan 启动 Sender，撤下网关不能停止 Sender 的领取、发送、回写及关闭过程。
- app/tasks/business_tasks.py 第 20～45 行在 worker 中装配 EcsCommands / Outbox 并推进业务；app/tasks/celery_app.py 第 35～60 行安排推进与清理。停 Beat 不代表已排队、预取或运行中的 worker 任务已停止。
- app/tasks/callback_tasks.py 和 app/tasks/reliable_tasks.py 会删除收据、快照与候选终态消息，预补录必须保护尚未迁移且需要保留的来源。
- migrations/env.py 第 96～111 行提供迁移事务及 schema 所有权检查，当前核对的集成装配与迁移路径未见本次统一历史的双写或旧二进制权威切换栅栏。投递 lease 保护原发送尝试，不能代替版本切换证明。
- scripts/release_bundle.py 的 check 核对选定 Compose 项目的镜像、迁移和 Beat，但不能证明其他实例、one-off 容器或人工写入者已排空。

Comparison grid 如下。

| 选择项 | 当前提案 | A | B | 依据与状态 |
| --- | --- | --- | --- | --- |
| R2 权威交接方式 | 扩展 / 补录 / 切换，具体交接未明确 | 准备阶段旧侧权威，受控窗口确认全部旧写入者退出，最终核对后统一启用新权威 | 全写入面部署兼容版本，事务双写 / 补录对照 / 版本栅栏后在线切换 | D6 / A 已确认 |
| 核心写入可用性 | 部署基线有停写流程 | 最终交接期间暂停受理与派发，窗口由演练和现场条件确定 | 设计目标为保持核心写入连续，所有中间版本须有完整兼容合同 | 选择路线的可见运行差异 |
| 预补录及来源保护 | 原提案需要批次恢复与保护 | 保护来源后可预补录，窗口内重新核对可变旧行及原引用 | 保护来源后补录，并与兼容事务双写一致性核对 | 保留全部已确认功能，两项共同要求 |
| 原在途执行 | 原身份、原期限及原租约 | 未确认完成的原尝试保持原事实 / 未知状态，沿原协议恢复 | 相同原身份、期限及租约合同 | D2～D4，两项固定 |
| D5 纯重复观察 | 已确认尽力保存 | 保持 D5 / A | 保持 D5 / A，不把诊断双写升级为核心要求 | 已确认固定值 |
| 切换后的回退 | 需保护已提交事实 | 新权威接受写入后，回退须覆盖其新增事实；旧表存在不等于旧程序可直接接回 | 兼容版本须覆盖切换后事实及回退，旧表存在同样不足 | 已确认原受理与恢复事实，两项共同要求 |
| 旧结构收缩 | 独立收缩迁移及准入 | 等全部读写 / 清理路径切换并验证后执行 | 相同准入 | 原提案与已确认模型合并，两项固定 |
| 维护时长、生产操作 | 尚未取得现场条件 | 尚未确定或授权实际执行 | 尚未确定或授权实际执行 | 本题只选择设计路线 |
| R3 保留与清理 | 待评审 | 仍待决定 | 仍待决定 | 本题不决定长期保留取值 |

Question D6 如下。

D6 · 权威数据切换采用维护窗口，还是全程在线兼容迁移？

maintenance-admin 已确认模型归集和原恢复合同。本题只选择从旧表交接到新权威存储的路线，两项都覆盖完整的模型、字段和历史迁移。

补录完成后，一个残留旧 API 的 Sender 或旧 worker 仍可能只向旧表登记发送证据。新版本收到 Result 时就可能在新存储中找不到这次原派发。仓库已有停止应用后迁移的部署流程，因此需要决定沿用受控停写交接，还是增加覆盖全部写入者的在线兼容协议。

Recommendation 为 A，因为它贴合现有部署方式，并能通过明确的旧写入者退出点交接权威，新增的混合版本逻辑较少。

两项区别在上线连续性和迁移机制，覆盖相同的已确认功能，不作完整度评分。以下为本项迁移工具、核对与演练的粗估，维护窗口长度另由实际数据量和演练结果确定。

Header 为 权威切换路线。

Options 如下。

A) 受控窗口统一切换 (recommended)

准备阶段保持旧存储为权威，在保护来源后可以预补录；最终确认 API / Sender / worker / 清理及人工写入者均已退出，完成最终补录与对账后统一启用新权威，能够复用现有维护部署流程并减少混合版本读写逻辑。窗口内重新核对已复制但后来改变的旧行，原在途命令与结果按原身份保留，恢复后沿原合同续行。代价是最终交接需要暂停核心受理与派发，必须先验证对端原身份补报及现场维护条件，粗估人工 2～4 天 / CC 4～8 小时。

B) 全程在线兼容迁移

先部署覆盖 API Sender、回调、worker 和清理的兼容版本，在原事务中维护旧合同和新权威候选，再补录对账并通过版本栅栏在线切换，能够降低整体停写需求并逐批升级实例。每个中间版本及切换后的读写和回退都有明确合同，已确认的原身份与核心原子性继续适用，纯重复观察仍按 D5 尽力保存。代价是需要新增完整的双写一致性、旧版本退出和混合版本验证机制，维护面更大，粗估人工 5～8 天 / CC 10～16 小时。

Net 为两项都完成同一数据收敛，本题取舍是受控暂停窗口与额外在线兼容复杂度。Pending remedies not decided here 为 R3。请回复 A 或 B。<gstack-qid:plan-eng-review-authority-cutover>

State 为 approved。

Actual answer 为用户 D6 / A，“选 A，采用受控维护窗口统一切换，保留原在途命令和结果，确认所有旧写入者退出并完成最终对账后再切换新权威，继续评审保留与清理策略。”

Accepted scope 为旧侧权威准备 / 来源保护 / 可选预补录及受控停写后的统一权威交接，全部原写入者退出、可变旧行最终核对、原身份及在途事实保留为必要准入。迁移工具、写入者清单和演练随同承接；维护时长和实际生产操作继续按现场条件另行落实，旧表收缩仍遵循独立验证准入。

History 为原提案只给出阶段顺序，D6 保存受控窗口及全程在线兼容路线的完整比较，用户选择 A 后按上述范围更新。

### R3a. 原保留合同及清理并发保护的必要承接

Finding 为 F-A4 / P1 / confidence 9/10。初稿第 6.1 节写“历史清理只锁历史行，通过外键及非加锁引用检查判断资格”，第 11 节又要求在记录锁内核对使用者，未覆盖原业务保护入口的工作线锁与非外键引用。

Plan baseline 为 D2 / A、D3 / A、D4 / A 已确认的原幂等、数值身份、未决责任与长期恢复合同，加上用户提供 AGENTS.md 中规定的原锁顺序及原业务证据保留规则。这里修正原提案的实现推断，不新增或缩短任何留存合同。

Runtime evidence 为 app/businesses/_components/bin_roller/retention.py 第 15～25 行按 ID 锁 WorkLine，第 33～54 行的 protected_event_ids 发现候选后取得原工作线锁并重读；第 187～202 行的 protected_message_ids 明确将首次查询视为 hint，在工作线锁下第二次完整核对。事件身份还存在于整数列和 JSON application_ref，不全由数据库外键约束。

必要修正为沿原保护入口发现 / 锁定 / 重读后再进入根与证据的最终清理，不引入证据先于业务主体的反向锁顺序。正文转 PURGED 和删除行使用相同保护条件，未知保护状态继续保留。保留来源原时钟，不能以补录时间重新计时。

State 为 approved，依据为上述原合同及 D2 / A、D3 / A、D4 / A 的必要承接，未提出新的运行取舍问题。

Actual answer 为既有 D2～D4 对原恢复、幂等、身份及长期重放的明确保留答复，以及用户 AGENTS.md 的原业务保护和锁顺序要求。

Accepted scope 为复用并适配原保护入口、原期限和时钟，原锁下重读与正文 / 行清理一致准入，并增加合表后的等价回归验证。现有 test_cleanup_and_new_passage_association_share_the_original_line_lock、test_closed_member_and_face_stay_while_original_step_and_rack_need_them、test_wait_fact_protects_original_message_after_task_checkpoint_cleanup、test_protected_first_page_does_not_starve_later_expired_rows 为原合同证据，新 schema 仍须重新验证。

History 为根据现有清理代码和原行为测试修正初稿“只锁历史行”的推断；本项没有改变原保留时长或新增长期归档承诺。

### R3b. 普通历史摘要及查询索引的默认窗口

Finding 为 F-A5 / P2 / confidence 8/10。第 11 节原提案建议对无额外业务保护的普通历史采用 90 天默认窗口，D7 / A 已确认。该窗口影响普通历史定位能力和容量，不决定运行证据可否删除。

Plan baseline 现为 D7 / A，新增普通历史摘要和查询索引默认保留 90 天。原诊断 7 天、收据 / 可靠终态 90 天及相应原时钟、ECS 长期证据与所有未决责任保护按 R3a 固定。D1～D6 的已确认值继续有效。

Runtime evidence 为 app/core/config.py 第 34 行 CALLBACK_SNAPSHOT_RETENTION_DAYS 默认 7，第 57 行 RELIABLE_RETENTION_DAYS 默认 90；原清理入口采用各自来源时间。新统一历史的摘要窗口尚无独立运行实现，当前缺少线上体量和容量测量，不能承诺具体存储开销。

Comparison grid 如下。

| 选择项 | 当前提案 | A | B | 依据与状态 |
| --- | --- | --- | --- | --- |
| R3b 普通摘要与查询索引窗口 | 建议 90 天 | 默认 90 天 | 默认 180 天 | D7 / A 已确认 |
| 起算与回填 | 原事实时间，补录不重置 | 相同来源时间，补录不重置 | 相同来源时间，补录不重置 | 固定规则，两项共同要求 |
| 诊断及原文 | 原来源期限及保护 | 诊断原文沿原 7 天，其他原文沿其原来源与运行保护 | 同样沿原来源及保护，不随摘要延长 | 原来源合同，两个选项都不扩大正文承诺 |
| 收据资格 / 可靠终态 | 原 90 天与业务保护 | 保持 R3a | 保持 R3a | D2～D4 和原合同固定 |
| ECS 与未决责任 | 长期恢复 / 责任保护 | 保持 R3a，优先于摘要窗口 | 保持 R3a，优先于摘要窗口 | 已确认合同固定 |
| D5 / D6 | 纯重复尽力保存 / 受控窗口切换 | 保持已确认值 | 保持已确认值 | 固定值 |
| 清理与迁移验证 | 必须验证并发和保护 | 同一套完整保护验证 | 同一套完整保护验证 | 必要证明，无深度裁剪 |

Question D7 如下。

D7 · 普通历史摘要和查询索引默认保留多久？

maintenance-admin 已确认受控窗口切换。本题只确定已无额外运行保护的普通历史摘要和检索信息的窗口，原命令、Result、未决责任及原收据资格的保留合同均已固定。

保留更久的摘要，能在更久以前的编号、状态和摘要之间追查关联，但会增加历史表和索引占用。原文有自己的来源期限，摘要还在并不保证每份原文也仍在。需要为新增历史查询选择默认窗口。

Recommendation 为 A，因为 90 天与当前主要对接记录的默认窗口一致，作为首个容量基线更容易核对和测量。

两项区别在查询窗口与容量策略，不作完整度评分。窗口以原事实时间起算，补录不重新计时；原文和运行保护在两个选项中相同。以下只估算本项策略与边界回归，容量需要实际数据验证。

Header 为 历史摘要期限。

Options 如下。

A) 默认 90 天 (recommended)

普通历史摘要与查询索引默认保留 90 天，能够覆盖当前主要对接记录的常规窗口，并形成较小、容易测量的首版容量基线。ECS 长期证据、未决与待消费责任继续按原合同保护，原文可用性在查询中单独展示。代价是没有额外保护的更早摘要会到期退出普通检索，跨更长时间的历史追查范围较短，粗估人工 0.5 天 / CC 0.5～1 小时。

B) 默认 180 天

普通历史摘要与查询索引默认保留 180 天，能够提供更长时间的编号和状态追查，并让跨季度的旧交互继续通过统一入口定位。运行证据保护与原文期限保持同一规则，查询明确显示摘要存在但原文已到期的情况。代价是更大的历史表与索引占用需要容量验证，180 天摘要不能被理解为同等时长的完整报文，粗估人工 0.5 天 / CC 0.5～1 小时。

Net 为两项都维持原恢复及清理安全合同，本题只取舍普通摘要的查询范围与容量。请回复 A 或 B。<gstack-qid:plan-eng-review-history-summary-retention>

State 为 approved。

Actual answer 为用户 D7 / A，“选 A，普通历史摘要和查询索引默认保留 90 天，原文按来源期限保留，运行和恢复证据继续按原合同保护，继续后续工程评审。”

Accepted scope 为无额外运行保护的普通历史摘要和查询索引默认保留 90 天，沿原事实时间起算，补录不重新计时。原文按来源期限保留，运行和恢复证据继续按原合同保护；来源期限、到期边界、并发保护与原文可用性的验证作为同一选择的必要证明承接。

History 为原提案建议 90 天，D7 独立提出新增查询留存取值并保存完整比较，用户选择 A 后确认上述范围；原来源期限及保护合同不重开。

### R7. ECS 发送证据的权威归属

Finding 为 F-A6 / P1 / confidence 9/10，由当前主评审根据拟议追加模型与现有运行读取的差异提出。原提案将 ecs_send_evidence 迁入 integration_record，第 17 节原将该候选列为待确认，现已按 D8 / A 收口；当前没有运行缺陷结论。

Plan baseline 现为 D8 / A，由 IntegrationRecord 承接原 EcsSendEvidence 权威发送证据，ECS 专属读取适配器取得当前有效结论。D1 / B 的现有分层、D2～D4 的 Result / 收据 / 命令合并、D5 纯重复观察尽力保存、D6 受控窗口切换及 D7 普通摘要 90 天均固定。

Runtime evidence 如下。

- app/models/ecs_command.py 第 33～46 行按 command_code / lease_id 唯一保存一行当前发送结论及响应。
- app/integrations/ecs_command/lifecycle.py 第 38～61 行在原 before_send 事务登记 OPEN_ATTEMPT，第 64～103 行将同一租约行更新为 ACK、REJECTED、NOT_RECEIVED 或 UNKNOWN，迟到拒绝可设置原 needs_reconciliation。
- app/integrations/ecs_command/result.py 第 110～116 行读取命令的原发送尝试，仅在当前结论含 OPEN_ATTEMPT / UNKNOWN / ACK 时认为可能派发。原命令与 Outbox 锁仍先于这些读取。
- tests/integrations/ecs_command/test_flow.py 的 test_first_result_before_http_response_applies_once、test_known_not_received_conflicts_but_new_inflight_attempt_accepts、test_definite_nonacceptance_result_is_persistently_conflicting、test_result_and_late_rejection_keep_result_and_evidence，及 test_snapshot.py 的 test_scope_stop_fences_snapshot_and_preserves_original_execution_evidence 共同固定开始 / 完成 / 新租约 / 迟到 / 范围停止差异。
- 本轮显式留空 TEST_DATABASE_URL 与 TEST_REDIS_URL，定向执行上述五个测试函数及参数组合，6 passed、6 skipped。跳过的是 PostgreSQL 参数，不能证明新模型或其并发正确。

Comparison grid 如下。

| 选择项 | 当前提案 | A | B | 依据与状态 |
| --- | --- | --- | --- | --- |
| R7 权威发送证据 | 迁入统一记录 | IntegrationRecord 承接，通过 ECS 读取适配器取得当前有效结论，验证后退出旧实体 | EcsSendEvidence 保留当前有效结论，响应原文改为统一引用 | D8 / A 已确认 |
| 原命令 / 租约唯一性 | 原 command_code / lease_id | 同一原实例与租约的开始唯一，原事务重试防重，后续观察有明确替代顺序 | 保留原行唯一约束 | 原行为，两项共同要求 |
| OPEN 与完成结论 | 同行更新会替换 OPEN | 同租约完成结论取代 OPEN，按原串行事务顺序解释后续观察，不靠时间戳或 UUID 猜测替代关系 | 原行保存当前有效结论，追加历史用于时间线 | 不以历史 OPEN 误判可能派发 |
| 多次尝试与迟到 | 每个原租约独立 | 不混合租约，已知未受理不掩盖其他原未决尝试，迟到仍沿原核对规则 | 同样保留原租约与迟到规则 | 原租约栅栏，不覆盖已采用 Result |
| 发送前后原子性 | 原门禁与 finish 事务 | 核心登记 / 裁决随原事务，F-A2 存储异常边界继续适用 | 同等原子性与异常边界 | D2～D4 与 F-A2 的必要承接 |
| 期限及保护 | 无新增统一自动过期 | 继续原长期恢复合同，不降为 7 天诊断或普通 90 天摘要 | 相同原保护 | D7 不能缩短运行证据合同 |
| 其他已确认选择 | D1～D7 | 全部保持 | 全部保持 | 固定值 |
| CallbackSnapshot 归集 | 原提案，未确认 | 仍待单独评审 | 仍待单独评审 | 本题不决定该实体 |
| 必要验证 | 原行为基线已有部分覆盖 | 新模型证明开始 / 完成 / 新租约 / 迟到 / 回滚 / 重启 / 清理等价 | 引用迁移及原行为同等回归 | 不裁剪验证深度 |

Header 为 发送证据归集。

Question D8 如下，完整选项为其中的 A / B 两段。

D8 · 是否把 EcsSendEvidence 也合入统一证据模型？

请回复 A 或 B。<gstack-qid:plan-eng-review-send-evidence-consolidation>

maintenance-admin 已按 D7 / A 确认普通历史摘要和查询索引默认保留 90 天。继续收敛实体时，需要确定 ECS 发送证据的权威归属。

发送证据会参与判断一个 Result 能否被接纳。例如某次尝试最初登记“准备发送”，随后确认“对方未受理”，合并后的读取必须认出后者已经取代前者。若只要历史中出现过“准备发送”就认为可能执行，会接纳原本应该拒绝的 Result。这是拟议合并需要防住的风险，当前实现通过更新同一发送证据行处理。

Recommendation 为 A，因为它符合减少实体和重复字段的目标，并可将原发送判断完整迁入统一记录；ECS 的准入规则继续由原 ECS 模块负责。

两项区别在证据归属，覆盖相同的原行为，不作完整度评分。D1～D7 的已确认值保持不变，CallbackSnapshot 的归集仍待单独评审。以下为本项迁移、读取适配和回归的粗估，不含整项改造或生产上线。

A) 合入统一证据 (recommended)

将原命令、原租约对应的发送开始与后续观察保存在 IntegrationRecord，通过 ECS 专属读取适配器取得每次尝试的当前有效结论，能够取消独立发送证据实体及其响应原文副本。保留原唯一性、发送前原子登记、租约栅栏、迟到核对和原长期恢复保护，已完成结论必须取代同次尝试的 OPEN，另一次尚未结束的尝试仍保留自己的意义。代价是要验证追加记录与原可变行的判断等价，不能直接扫描全部历史状态来判断是否可能派发，粗估人工 1～2 天 / CC+gstack 2～4 小时。

B) 保留精简发送证据实体

继续由 EcsSendEvidence 保存每个原命令和租约的当前有效结论，响应原文通过统一记录引用取得，能够延续现有 Result 准入读法并减少大字段重复。统一时间线仍可展示开始、完成与迟到观察，原幂等、租约、迟到核对和恢复保护保持同等完整。代价是继续维护独立发送证据实体及它与统一记录的一致性，实体收敛较少，粗估人工 0.5～1 天 / CC+gstack 1～2 小时。

Net 为两项都保留完整的原执行与恢复合同，本题只决定是否同时取消独立发送证据实体。

本轮仅修改设计并核对原发送行为，定向基线测试为 6 passed、6 skipped。合表后的等价性与 PostgreSQL 并发尚未验证。

State 为 approved。

Actual answer 为用户 D8 / A，“选 A，继续后续评审”。

Accepted scope 为将原命令 / 租约的权威发送证据合入 IntegrationRecord，通过 ECS 专属读取适配器取得每次尝试当前有效结论，验证通过后退出 EcsSendEvidence。保留原唯一性、发送前原子登记、租约栅栏、迟到核对及长期恢复保护，同次完成结论取代历史 OPEN，其他原租约独立判断；必要的读取适配、替代顺序、迁移核对及等价回归作为本项完整承接，不改变 D1～D7。

History 为原提案迁出技术发送证据，第 17 节将其列为待评审候选。D8 保存可变原结论与追加观察差异及完整比较，用户选择 A 后按上述范围确认归集，CallbackSnapshot 仍待独立评审。

### R8. 诊断快照实体的归集

Finding 为 F-A7 / P1 / confidence 9/10，由当前主评审根据原诊断与拟议权威记录的合同差异提出。第 7 节原提案迁出 callback_snapshot，第 17 节原将归集列为待确认，现已按 D9 / A 收口。该项是新路径设计风险，没有现有运行缺陷结论。

Plan baseline 现为 D9 / A，IntegrationRecord 承接诊断预览及回复观察，迁移验证后退出 CallbackSnapshot。D1～D8 已确认值固定，特别是 D5 的诊断失败边界、D7 的普通摘要窗口和原文来源期限，以及 D8 的权威发送证据保护。实体归属不改变这些行为。

Runtime evidence 如下。

- app/models/callback.py 第 48～61 行的 CallbackSnapshot 保存独立 id、request_id、received_at、base64 预览、truncated、HTTP 状态、JSON 回复、outcome 及可空 source，没有直接指向原收据或命令的外键。
- app/integrations/wms_callback/snapshots.py 第 16～36 行提供每来源每进程每分钟 120 条的诊断配额和 48 KiB 预览常量，第 39～77 行在独立两秒预算事务内保存并处理失败。
- app/controllers/wms_events.py 第 71～104 行、app/controllers/ecs_callback.py 第 63～108 行在原事务及异常处理后添加后台观察；source 分别沿默认 WMS 及显式 ecs。request_id 来自 trace_id，可能为空，不能充当每次 HTTP 接收的唯一身份。
- app/controllers/ecs_result.py 的 result_callback 当前没有快照写入；其新增重复观察按 D5、新 Result 权威证据按 D2 的合同接入，不能称为已有快照路径。
- 旧 request_id / source / received_at 不证明唯一关联，收据没有共同 trace 关联字段；重复、冲突、失败、超限及旧资格释放后复用身份均需保留未关联可能。新路径只有明确传递的本次接收及原根引用可以建立关联。
- WMS 现有快照 source 为 NULL，而配额默认来源为 wms，补录保存原 NULL 及来源解释，不补造原端点。旧 base64 只证明已保存的预览字节；流读取异常时控制器可能仍保存初始化的空字节，不能仅凭 truncated=False 宣称完整空请求。旧 JSON 回复需要标记重编码，NULL 不自动变为 JSON null 或对象。
- app/controllers/dev_ecs_event.py 的投递复用 receive_event_request，外层返回调试 DTO；快照保存的是内层协议回复，不代表外层调试 HTTP 回复的原编码。统一记录必须明确回复层次和来源，不能以一个“完整响应”标签掩盖差异。
- 本轮检索 app 中 CallbackSnapshot 的全部引用，现有调用为模型 / 导出、后台写入与清理，未见读取该表的快照查询 API。app/services/dev_ecs_event_service.py 的 event_state 读取原收据与注入业务投影；新历史 API 是新增读取入口。
- app/tasks/callback_tasks.py 第 29～37 行沿 received_at 清理原快照。tests/controllers/test_wms_events.py 的 test_unassociated_invalid_requests_return_empty_400、test_oversize_stream_is_bounded_and_snapshotted、test_callback_has_no_rate_limit_and_snapshot_budget_does_not_change_ack、test_snapshot_storage_failure_does_not_change_committed_ack、test_snapshot_timeout_does_not_change_ack，及 ECS 来源、snapshot 配额和清理边界测试提供原行为基线。
- 本轮显式留空 TEST_DATABASE_URL 与 TEST_REDIS_URL，执行上述定向原测试及 tests/integrations/wms_callback/test_snapshots.py，共 20 passed、19 skipped。此结果不能证明新模型、迁移或 PostgreSQL 并发。

Comparison grid 如下。

| 选择项 | 当前提案 | A | B | 依据与状态 |
| --- | --- | --- | --- | --- |
| R8 快照实体归属 | 迁入统一证据 | IntegrationRecord 承接诊断观察，验证后取消 CallbackSnapshot | CallbackSnapshot 保留诊断索引，正文改为统一记录引用 | D9 / A 已确认 |
| 诊断与核心事务 | 原诊断独立，核心沿原受理事务 | 共用存储格式，各自明确写入合同，诊断故障不改变原回复 | 相同故障边界，诊断索引与内容沿原独立预算保存 | D5 及原行为固定 |
| 诊断采集预算 | 每来源每进程 120 条 / 分钟、48 KiB 预览、两秒 | 保持原预算，不因 BYTEA 扩大预览或升级为全量承诺 | 相同预算 | 不引入全局限流或额外重试调度 |
| 原文与重复字段 | base64 请求预览及 JSON 回复 | 请求字节及回复观察进入记录，同次核心原文明确存在时引用，避免另存一份 | 快照索引引用相同统一记录 | 两项都统一正文表示 |
| 新接收与旧快照关联 | 原 request_id 不是唯一身份，没有收据引用 | 新接收用原本次 attempt_id / 可信引用；无法证明关联的旧快照保留独立诊断身份 | 保留相同可证明关联及未关联边界 | 不按时间、trace_id 或条码猜测原实例 |
| 历史采集真实性 | 可能预览 / 缺失，回复为 JSON | 原 base64 仅解码已保存字节，truncated 保留；旧 JSON 回复标记 LEGACY_REENCODED；流失败的空预览不冒充完整空请求，调试内层协议回复不冒充外层 wire 字节 | 相同真实性约束 | 不重建丢失字节或把准备回复称为对端已收 |
| 来源与保留 | 沿原来源、received_at、默认诊断 7 天 | 保留原 source 及 NULL 的来源解释；保持原时钟；原文、90 天普通摘要与运行保护分别管理 | 相同来源与保留规则 | D7 固定，补录不重新起算 |
| 权限与查询 | 统一历史是新增正式入口 | 已要求的摘要 / 正文权限和采集边界表达全部保留 | 相同新增查询及权限 | 无既有快照查询 API 可直接复用 |
| 已确认模型与运行职责 | D1～D8 | 全部保持 | 全部保持 | 本题不合并 ReliableMessage 或 TransportTask |
| 必要验证 | 原行为部分已有测试 | 新写入、引用、限流、截断、失败、到期及迁移等价回归 | 相同完整验证 | 不以实体保留替代新引用证明 |

Header 为 诊断快照归集。

Question D9 如下，完整选项为其中的 A / B 两段。

D9 · 是否把 CallbackSnapshot 也合入统一证据模型？

请回复 A 或 B。<gstack-qid:plan-eng-review-callback-snapshot-consolidation>

EcsSendEvidence 已按 D8 / A 确认合并。现在评审最后一个技术证据实体 CallbackSnapshot，它保存 ECS Event / WMS 回调的收包预览及回复观察。

这类快照可能被限流、截断或因保存失败而缺失。合入同一张表后，仍要区分“诊断观察”和“已受理的权威证据”，否则可能让排查记录故障影响原回复，或把一份预览误当成完整请求。旧快照没有直接指向收据的引用，也不能仅凭时间或 trace_id 强行建立关联。

Recommendation 为 A，因为它能继续减少实体，并让预览、原文可用性和查询格式统一；诊断与核心证据继续使用各自已经确认的事务和保留规则。

两项区别在实体归属，覆盖相同的行为，不作完整度评分。D1～D8 保持不变；现有诊断的每来源每进程每分钟 120 条、48 KiB 预览、两秒写入预算和默认 7 天来源期限在两项中相同，普通历史摘要仍按 D7 保留 90 天，运行证据继续按原合同保护。

A) 合入统一证据 (recommended)

用 IntegrationRecord 承接快照中的请求预览和回复观察，通过类型及明确引用区分诊断记录与核心证据，迁移验证通过后取消 CallbackSnapshot。原文统一使用字节保存，已存在同次接收的核心原文时使用明确引用，无法可靠关联的旧快照保留独立诊断身份，能够减少独立实体和重复原文。代价是需要适配后台写入与清理，并验证保存失败不影响回复、预览不冒充完整报文，粗估人工 0.5～1 天 / CC+gstack 1～2 小时。

B) 保留精简快照实体

保留 CallbackSnapshot 作为独立诊断索引，请求预览和回复内容改为统一记录引用，统一查询仍可展示这些快照。这样可以延续独立诊断入口和清理结构，同时统一正文格式、来源与可用性表达，原保存失败边界保持不变。代价是继续维护快照索引实体与统一记录之间的关系，实体收敛较少，粗估人工 0.5～1 天 / CC+gstack 1～2 小时。

Net 为两项都保留诊断的尽力保存性质，本题只决定是否取消独立快照实体；合表不会把诊断观察升级为受理凭据。

本轮仅修改设计，原快照行为的定向测试为 20 passed、19 skipped。新模型、迁移和 PostgreSQL 并发尚未验证。

State 为 approved。

Actual answer 为用户 D9 / A，“选 A，继续后续评审”。

Accepted scope 为 IntegrationRecord 承接原请求预览及回复诊断，迁移验证通过后取消 CallbackSnapshot。保留原诊断配额、预览和独立预算、失败不改变回复、来源时间及保留规则；核心证据与诊断按明确类型和原事务区分，同次核心原文明确存在时引用，未证明关联的旧快照保持独立诊断身份，不补造完整原文、外层 HTTP 回复字节或受理资格。后台写入、清理、补录、新查询和等价回归为同一归集的必要工作，D1～D8 保持。

History 为原提案将快照迁入统一证据，D9 根据当前诊断调用、旧采集边界及没有现有快照查询 API 的事实独立保存完整比较，用户选择 A 后确认上述归集范围。

### R9. 普通历史查询的最大单次时间跨度

Finding 为 F-P2 / P2 / confidence 9/10，由当前主评审发现。第 8 节初稿建议单次最多 31 天，当时尚未确认这一查询政策；D7 只确认保留 90 天普通摘要，不能据此推定单次查询上限，也没有实际体量 / 执行计划证明 31 天是必要条件。现已另按 D10 / A 确认最大单次跨度为 90 天。

Plan baseline 现为 D10 / A，普通时间范围检索的最大单次跨度为 90 天。D1～D9 固定，精确原身份检索继续覆盖其合法留存期。默认页面时间选择、批量导出或新缓存不属于本题。

Runtime evidence 为现有 CursorParams 每页默认 20 / 最大 100，paginate_cursor 使用 limit + 1；新历史接口与新索引均尚未实现。既有查询 / 结果读口证明需要保留显式批量装配、无副作用及原字节验证，不能证明新历史在 31 / 90 天的运行延迟。当前没有生产数据库版本、行数、每天写入量、报文分布或执行计划。

Comparison grid 如下。

| 选择项 | 当前提案 | A | B | 依据与状态 |
| --- | --- | --- | --- | --- |
| R9 最大单次普通时间范围 | 初稿建议 31 天 | 90 天 | 31 天 | D10 / A 已确认 |
| 普通摘要留存 | D7 / A 默认 90 天 | 保持 90 天 | 保持 90 天 | 不改变留存政策 |
| 三个月时间排查 | 初稿需要分段 | 同一检索条件下连续翻页 | 拆分时间窗口后分别查询汇总 | 单次跨度的可见差异 |
| 每页和正文 | 游标 / 最多 100 / 列表无原文 | 相同限制 | 相同限制 | 第 8 节既有计划合同 |
| 精确原身份检索 | 整个合法留存期 | 不受普通时间跨度替代 | 同样不受替代 | 原身份可重复实例需分别返回 |
| 执行计划与负载证明 | 未取得 | 验证 90 天、选择性与长时间线 | 验证 31 天及跨窗口使用 | 同等必需深度，不将短窗口当成性能证明 |
| 运行 / 恢复 / 原文 | D2～D9 与原来源期限 | 全部保持 | 全部保持 | 固定值 |
| 新默认界面 / 导出 / 缓存 | 无新增范围 | 不在本题引入 | 不在本题引入 | 不增加产品范围 |

Header 为 历史查询跨度。

Question D10 如下，完整选项为其中的 A / B 两段。

D10 · 普通历史的单次时间范围查询，允许覆盖 90 天还是最多 31 天？

请回复 A 或 B。<gstack-qid:plan-eng-review-history-query-time-window>

实体归集已按 D2～D9 收口，代码组织与测试要求也已完成本轮审查。性能评审发现，初稿还建议“单次时间窗口最多 31 天”，这一限制尚未确认，也没有实际数据量或执行计划证明它是必需的。

普通摘要虽然保留 90 天，若单次最多查 31 天，按时间排查三个月记录仍需分段查询。本题只决定普通时间范围检索的最大单次跨度；两项都按游标分页、每页最多 100 条，列表不读取原文，原始身份精确检索继续覆盖其合法留存期。

Recommendation 为 A，因为它更符合减少零散查询的目标。性能应通过索引、明确筛选、分页和实际执行计划验证，31 天限制本身不能保证查询足够快。

两项区别在单次检索范围和操作便利性，不作完整度评分。D7 的 90 天普通摘要保留、原文来源期限、运行证据保护及全部原协议行为保持不变；两项都必须完成相应规模验证，不预先承诺响应时间。

A) 单次允许查询 90 天 (recommended)

普通时间范围查询允许一次覆盖 90 天，再逐页取得摘要，能够在同一检索条件下连续追查整段常规历史，减少人工拆分和边界遗漏。继续通过时间 / UUID 游标、关联索引及按需读取正文控制查询成本。代价是需要验证更宽时间范围下的实际扫描、排序和数据库负担，若计划不达标必须先优化，粗估人工 0.5～1 天 / CC+gstack 1～2 小时。

B) 单次最多查询 31 天

保留初稿的 31 天最大单次跨度，超过范围时明确提示拆分，能够限制每次时间检索的跨度，并让使用者主动缩小排查范围。90 天普通摘要仍然保留，原始身份精确定位仍按完整合法留存期查询。代价是跨三个月排查需要多次检索和人工汇总，且较短范围仍需要索引和执行计划验证，粗估人工 0.5～1 天 / CC+gstack 1～2 小时。

Net 为两项都保留 90 天普通历史，本题取舍是一次连续检索，还是要求按较短时间分段查询。

本轮仍只修改设计与测试计划，未实施产品代码或数据库迁移。没有生产数据规模与 PostgreSQL 执行计划，当前不能声称查询性能已达标。

State 为 approved。

Actual answer 为用户 D10 / A，“按建议，继续”，对应本题唯一推荐选项 A。

Accepted scope 为普通时间范围检索允许单次最大跨度 90 天，沿原游标分页、每页最多 100 条、列表不读原文，精确原身份检索继续覆盖其合法留存期。完成 90 天代表性选择性、扫描、排序和数据库负载验证；未达标先优化，不把窗口当作已取得的延迟保证。D1～D9 保持，不增加导出、缓存或界面默认值范围。

History 为初稿提出 31 天建议，D7 确认普通摘要保留 90 天；D10 独立区分留存期限与单次查询跨度并保存完整比较，用户按唯一推荐选择 A 后确认 90 天单次跨度。

### Approval readiness 核对

| 记录 / 必要适配 | 实际批准或原合同 | 核对结果 |
| --- | --- | --- |
| 模块组织 | D1 / B，原有分层及减少实体 / 重复字段的用户要求 | 保持原分层，无额外框架 |
| R4 / R5 / R6 | D2 / A、D3 / A、D4 / A 的实际答案 | Result / 收据 / 命令归属及原身份、恢复合同已确认 |
| R1a / R2 | D5 / A、D6 / A 的实际答案 | 纯重复尽力保存与核心原子性、受控维护窗口及最终对账已确认 |
| R3a / R3b | 原 AGENTS 保留与锁顺序、D2～D4 原合同及 D7 / A 实际答案 | 原来源保护保留，普通摘要默认 90 天 |
| R7 / R8 / R9 | D8 / A、D9 / A、D10 / A 的实际答案 | 发送证据、诊断快照归集及单次查询 90 天已确认 |
| F-A2 / F-A4 | 上述原执行 / 恢复 / 锁顺序与 D5 / D6 / D7，必要适配 | 存储异常不改成对端事实，清理使用原锁下重读保护 |
| F-C1 / F-C2 | 已确认时间 / UUID 格式及查询、D9 保持的摘要 / 正文权限和审计要求，原整数 / 审计合同 | 局部复合游标、显式正文留痕为原要求的实现，不改变普通 GET 或审计失败政策 |
| F-T1～T8 | D2～D10 与原公开调用、数值事件 / 原结果 / 原保留合同 | 仅补必需等价证明，无裁剪测试深度或新增业务规则 |
| F-P1～P3 | 已确认分页 / 原文隔离、D2 / D8 原定向恢复读取及 D10 / A | 窄字段、避免重复根与 N+1、定向有效结论及 90 天实测要求承接 |

Approval readiness: PASS。已核对 D1～D10 及 R1a / R2 / R3a / R3b / R4～R9，当前工程选择均有实际答案或明确原合同依据，没有未经确认的新模型归属、保留政策或功能裁剪。本项表示评审选择可收口，不表示代码已实现、生产可切换或现场已完成；下一步共同理解确认属于导航，不重开上述选择。

## 17. 非业务子包实体的合并评估

本轮检查 app/models 内的 17 个实体与 2 张账号授权关联表。其中 7 个实体直接属于对接记录及运行链路。以下是候选评估，只有标明实际答案的取值属于已确认设计。

| 原实体 | 合并评估 | 状态 |
| --- | --- | --- |
| EcsResultEvidence | 权威 Result 语义证据进入 integration_record，保留原唯一、冲突及长期重放合同 | D2 / A 已确认 |
| EcsSendEvidence | 权威发送证据进入统一记录，ECS 专属读取适配器承接每个原 command / lease 当前有效结论，同次完成结论取代历史 OPEN，保留迟到核对和原恢复保护 | D8 / A 已确认 |
| CallbackSnapshot | 收包及回复诊断归入 IntegrationRecord，保留原独立故障边界、预算及期限，不将旧快照强行关联为受理凭据 | D9 / A 已确认 |
| CallbackReceipt | 由交互主实体的入站角色承接，保留原数值事件身份、活动受理唯一性与原接收器事务 | D3 / A 已确认 |
| EcsCommand | 由交互主实体的 ECS 命令角色承接，保留 owner + call_key_digest 唯一、原 command_code、冻结路由、独立请求校验、执行状态及命令寿命 | D4 / A 已确认 |
| ReliableMessage | 租约、重试、技术终态、范围停止及调度约束有独立用途；收敛重复正文与非必要投影，保留原投递调度职责 | 现有职责及原提案，字段逐项评审 |
| TransportTask | 原搬运身份、接纳、位置、成员事实和结果版本有独立用途；用明确引用关联请求与证据 | 现有职责及原提案，字段逐项评审 |

按已确认的 R4～R8 归集安排，当前 7 个对接实体收敛为 IntegrationExchange、IntegrationRecord、IntegrationReference、ReliableMessage、TransportTask 共 5 个。该数量包含新引入的引用实体，不把逻辑查询视图计作额外实体。字段数量仍由协议约束和真实事实决定，减少模型并不意味着删除独有的执行或幂等状态。

EcsCommand 的吸收会使 exchange 同时承担 ECS 原命令执行根职责。ECS 类型独有状态必须使用受约束字段或明确版本化的协议数据，不能放入任意诊断 metadata。命令 exchange 的原寿命和“命令根 → 对应 Outbox”锁顺序继续适用；通用历史记录的普通 90 天期限不能覆盖命令恢复合同。

CallbackReceipt 的吸收会使 exchange 同时承担受理根职责。原数值事件身份包含历史和之后的新事件。历史可查询与仍占有受理幂等资格是两个独立属性，不能用一个永久唯一的历史身份键同时表达。协议入口中的原收据锁将定位到相应 exchange，并沿每条现有调用路径原位置获取，通用写入方法不预先取得所有 exchange 锁。

其余实体的评估依据如下。

- Device、DeviceRoute、WorkLine、WorkLinePosition 共 4 个资产实体分别保存配置、不可变路由登记、工作线及位置容量。Device.current_route_id 引用不可变 DeviceRoute，保留这一关系支持配置删除后的原命令核对。
- User、Role、Permission、Menu 共 4 个实体与 user_role / role_permission 两张关联表承载账号、授权与导航，相关身份与通信历史通过明确引用和权限检查关联。
- AuditLog、AuditLogArchive 共 2 个实体已通过 AuditColumns 复用字段；archive_old 在同一事务迁移原审计身份及时间。其归档用途与权限有现有读写合同，当前对接记录优化没有证明需要改造它们的物理保存方式。

上述评估依据当前模型、原接收 / 读取 / 清理接口与已有回归用例。模型减少和字段收敛仍需在后续迁移、并发与清理验证中证明等价。

## 18. Architecture review

Scope Challenge 的结构与三个实体归属选择已经按实际答案收口。本节保存架构发现及推演，7 项发现均已按实际答案或原合同必要适配处置，阶段结论见本节末。

架构边界如下。

```text
business original transaction
  -> original ECS / WMS protocol service
       -> IntegrationExchange (typed command / receipt root)
       -> IntegrationRecord (request / reply / semantic evidence)
       -> ReliableMessage (original outgoing intent, when required)
  -> COMMIT or ROLLBACK together

original Sender
  -> original protocol gate + durable attempt opening
  -> one network request
  -> original lease-fenced result transaction
       -> original execution / delivery transition + evidence

pure original replay (identity + guard / recheck already resolved)
  -> read original durable decision
  -> original replay response
  -> supplementary duplicate observation in isolated failure boundary
       -> saved, or bounded diagnostic failure [D5 / A]
```

### F-A1. 纯重复观察失败的回复策略

P1，confidence 9/10。问题和原证据记录在 R1a，D5 / A 已确认纯重复观察尽力保存且失败不改变原重放回复。新 Result 拒绝、重检新结果、业务写入及核对标记均属于核心事实，不纳入可丢弃观察。

### F-A2. 新增记录端口的存储异常可能进入网络结果分类

P1，confidence 9/10。这是新增设计路径的风险，尚无现有运行缺陷结论。计划第 6.1 节将统一记录端口接入发送前与发送后事务，现有 app/reliable/delivery.py 第 106～119 行的 STATIC 通用异常分支会生成 FAILED / LOCAL_ERROR，第 258～271 行的 SNAPSHOT 分支会依据 gated 产生 HELD 及相应原因。

新增核心证据存储失败必须作为本地持久化失败传播并使当前事务回滚，发送前失败阻止网络调用，发送后失败保留已提交原身份及待恢复状态，继续沿原协议与租约恢复。该失败不能直接产生“对端拒绝”或“物理失败”事实，也不能因为保存观察失败登记新外部身份。新的端口异常需在原分类边界之外识别，不能只把 SQL 异常交给现有通用处理。

该项属于 D2～D4 保留原执行、结果与恢复合同的必要适配与证明，直接带入实施及回归要求。新增验证须区分发送前记录失败、网络已完成后的回写失败、失效租约的迟到观察，以及已采用 Result 后的原身份恢复。当前的存储写入、原幂等与租约约束继续由各原事务承担。

核心受理 / 结果事务写入失败时，沿既有控制器路径整体回滚并返回原协议的临时不可用回复。非法请求预览的现有限额、诊断失败边界与原确定性校验回复继续沿原入口处理。补充重复观察按 D5 / A 尽力保存，不以保存失败后的缺行证明对端未发送。

### F-A3. 补录与权威切换之间缺少完整的旧写入者退出机制

P1，confidence 9/10。问题、证据与路线比较记录在 R2，D6 / A 已确认受控窗口统一交接权威。真实写入者包含 API 内 Sender、回调与后台快照、worker 业务推进、清理任务及独立人工入口，迁移事务或网关撤流本身不能证明其退出。

设计失败路径推演如下。

```text
t0  old command QUEUED copied to new storage
t1  traffic removed, but one old API Sender still holds a claim
t2  backfill / comparison ends and authority is declared switched
t3  old Sender opens attempt in old tables and sends original command
t4  new callback receiver sees Result but lacks that original attempt
```

这条推演不是已发生的现场事故。两条候选路线都要证明 source-only 旧版本在最终交接后不能继续写入。最终核对必须覆盖之前已复制后又变化的行，不能只扫描 PK 游标之后的新行。未确认完成的在途请求继续保留原 OPEN / UNKNOWN 及租约事实，停止进程不能证明设备未执行。

切换后已有新受理或新 Result 提交时，直接启动只读旧表的旧版本会丢失这些新事实。保留旧表不能单独证明可回退，回退兼容或前向修复必须保住已确认原事实。分离结构迁移与独立数据搬迁符合 [Alembic 的迁移建议](https://alembic.sqlalchemy.org/en/latest/cookbook.html#data-migrations-general-techniques)，全程在线路线则额外要求中间版本的应用兼容逻辑。

### F-A4. 原清理保护依赖业务锁和非外键引用

P1，confidence 9/10。R3a 已将原合同的必要承接和初稿修正记录入案。新清理不能先持有证据行再等待工作线，否则可能与持有工作线、随后关联证据的业务事务形成反向等待。即使仅清空正文而不删除行，外键 RESTRICT 也不能证明该正文已无恢复用途。

```text
candidate hints
  -> original source protection entry
       -> original required business locks
       -> reread pending / consumed / predecessor / anchor references
  -> lock candidate root and records in the original compatible order
  -> recheck eligibility and source-based deadline
       -> protected / unknown: retain and advance batch cursor
       -> eligible: delete or purge body under the same gate
```

现有 PostgreSQL 测试保护清理与新事件关联共用原工作线锁，合表后的删除和正文清空都需要同等交错验证。原诊断、收据、可靠终态及命令 / Result 的期限和时钟已经按来源记录在第 11 节。

### F-A5. 新增普通历史窗口

P2，confidence 8/10。新历史摘要与索引的默认窗口已按 R3b / D7 / A 确认为 90 天。该窗口不能覆盖原 ECS 长期恢复、未决责任、原收据受理资格或原文来源期限。容量仍需依据实际数据测量，本轮未取得生产体量。

### F-A6. 追加发送观察不能直接替代原当前结论

P1，confidence 9/10。第 7 节拟取消 ecs_send_evidence，第 4.2 节拟保留开始与后续观察，但原 Result 读取基于每个原租约的当前有效结论。若将所有 SEND_ATTEMPT_OPENED 永久纳入 possible 判断，即使同次尝试已确定 NOT_RECEIVED，旧 OPEN 也会错误保留准入资格。R7 / D8 / A 已确认统一记录承接权威证据，原命令与租约的有效结论、后续替代顺序、迟到核对和原恢复保护作为必要等价要求落实。当前尚未实现或证明新读取路径。

```text
same original command / lease
  OPEN committed -> NOT_RECEIVED committed
       -> current effective conclusion: NOT_RECEIVED
       -> old OPEN remains historical, does not grant Result acceptance

another original lease still OPEN / UNKNOWN / ACK
       -> evaluate independently under the original ECS rules

Result applied -> late rejection of original attempt
       -> preserve adopted Result + original reconciliation obligation
```

### F-A7. 诊断快照不能因合表而取得受理含义或核心写入待遇

P1，confidence 9/10。原 CallbackSnapshot 的后台独立事务、每来源预算和截断使其只能表达已保存的诊断观察。R8 / D9 / A 已确认 integration_record 承接该实体，诊断写入失败不能改变原回复，诊断缺行不能证明未收包，旧预览也不能证明完整原文。原 request_id 来自 trace_id，可能为空或重复，模型没有原收据引用，补录不能仅按时间或 trace_id 猜测关联。必要适配与等价验证承接原诊断边界和运行证据保护。

```text
original callback transaction -> committed response / original error reply
  -> bounded best-effort diagnostic transaction
       -> request preview + response observation, or no saved observation

legacy snapshot -> preserve source key / saved preview / capture limits
  -> proven original reference exists: associate explicitly
  -> no proven reference: keep independent diagnostic identity
       -> never create receipt rights or replay authority
```

### Architecture 阶段处置

7 个设计风险均有明确处置。F-A1 / A3 / A5 / A6 / A7 分别按 D5 / D6 / D7 / D8 / D9 收口；F-A2 / A4 是已确认原执行、幂等、恢复及保留合同的必要适配。没有待回答的架构选择。新增存储仍使用现有 PostgreSQL / SQLAlchemy / Alembic、API / worker / Beat 和发布制品，不引入新服务、独立发送器或新的部署渠道。数据库、进程、权限种子和迁移版本必须随原制品一起校验，实际切换沿 D6 的独立运行准入。

## 19. Code Quality review

本阶段检查原调用、已有工具及字段用途，提出 2 项计划适配缺口。以下均是统一查询及已确认模型归集的必要实现要求，不新增模型归属、保留策略或验证深度选择。尚未修改运行代码。

### F-C1. 整数游标与拟议复合顺序不兼容

P2，confidence 10/10。app/services/pagination.py 第 24～59 行仅支持整数 id，并要求编码长度不超过 64；app/schemas/query.py 的 CursorParams 同样限制为 64。新列表按 created_at / UUID 排序，记录按 recorded_at / UUID 排序，不能只换排序列却继续解码整数。

在新 history schema / service 处使用小型、显式的版本化复合游标，复用已有筛选校验、每页数量及 limit + 1 模式，保留旧整数接口。以原生字段参数化比较时间 / UUID，游标区分列表 / 时间线及查询范围，拒绝非法类型、非 UTC 时间、非规范编码或换筛选条件后继续使用旧游标。不要为这两类查询改造全项目分页抽象。

本轮用标准库对包含版本、UTC 微秒时间、UUID 和查询摘要的示例格式作有界编码探针，结果为 157 字节 JSON / 210 字符游标，证明旧 64 字符上限不适用；新局部上限 256 为该明确格式的有界容纳，不是性能或安全证明。tests/services/test_pagination.py 已覆盖旧游标的类型、规范性与无 COUNT / 缺页合同，新复合格式另需行为回归。处置为纳入第 8 节及必需验证，无待选策略。

### F-C2. 正文 GET 不会自动得到现有管理审计

P1，confidence 10/10。app/middlewares/audit_middleware.py 第 53～55 行直接跳过非写方法，app/core/deps.py 的 require_permission 只提供 audit_action / user，不完成审计写入。tests/controllers/test_audit.py 的 test_reads_are_not_audited 明确普通 GET 不产生审计。初稿“按现有管理操作审计模式留痕”需要明确接入点。

正文读取显式复用原审计写入机制及旁路失败合同，仅保存操作者、动作、UUID 引用、路由与 trace，不解析或缓冲敏感原文供审计。原 AuditLog.resource_id 是整数，记录 UUID 采用现有 detail 内明确字段，不扩张整个审计模型。保留普通 GET、401 / 403 与原审计失败行为，验证新正文 GET 成功留痕、摘要权限不能读正文、撤权即时生效、审计中没有原文及凭据。处置为第 8 节原审计要求的必要接入，无新增阻断读取策略。

### 字段收敛与原事实边界

统一物理字节不改变事实的所有者。以下是本轮对现有写入与读取用途的逐组判断，字段清单用于实施核对，不以字段数作为删除目标。

| 当前字段 / 重复内容 | 收敛办法 | 必须保留的独立合同 |
| --- | --- | --- |
| 命令 request_raw 与 Outbox.request_raw | 共指原 REQUEST_REGISTERED 字节记录，原 DTO 仍装配 bytes | 命令独立冻结摘要、原路由及 snapshot、两侧引用一致性、适配器生成字节核对 |
| 命令 input_raw、call_key 及其摘要 | 保留原输入与全值核对 | input_raw 是业务调用输入，冻结请求另含自动 command_code / timestamp / priority，二者不是相同原文 |
| 命令 ACK、Outbox.response_raw / last_response_raw、发送响应 | 同一实际 HTTP 响应只存一次，分别保存 ACK / 终局 / 最近响应选择引用 | 最近尝试不等于终局回复；Result 可先于 ACK，DONE 也可没有 HTTP 回复，不能由状态反推 ACK |
| Result core 与原接收字节 | 按 D2 分别保存有效语义及原字节引用 | semantic_digest / core 判重与 raw_body 的 SHA256 用途不同，不能互相替换 |
| 收据原请求 / 当前回复及历次重检回复 | 原根保存当前回复引用，历史追加实际已提交回复 | guard / recheck、保存点、RAW / EFFECTIVE 比较与数值 receipt_id 保持 |
| 快照 base64、已知同次核心原文的预览副本 | 按 D9 还原保存字节或引用同次核心原文，旧 JSON 回复标记重编码 | 原诊断预算、独立事务、采集范围、来源及未关联状态 |
| Transport 接纳依据中的 request_raw / response_raw 字符串副本 | 仅重复原文改为明确记录引用，原公开 AcceptanceBasis 按原字节装配 | 原 message / task / attempt / lease / saved_at、请求和接纳摘要逐项等价核对；不能靠最新 DONE 代替接纳事实 |
| TransportTask.frozen / input_digest | 本轮保留原冻结投影与独立摘要，关联统一请求 | 目标 / 成员 / 面的回调校验和长期读取，不能因 Outbox 到期丢失 |
| TransportTask.applied_outcome / pending_outcome / outcome_digests | 保留原规范化、待应用和逐版本判重事实，统一历史引用其来源与版本 | 已应用结果可能按冻结面补齐，原上报摘要与补齐结果不相同；待前置事实不能当作已应用 |
| TransportTask.member_positions / position_observation | 保留原聚合及独立观察职责 | 多事件累积与异常位置报告有不同用途，不能从最后一份原文覆盖 |
| 命令执行状态、命令投递阶段、Outbox.status / lease / scope / dispatch 时间 | 保留各自原状态机与索引字段，不新增统一可写成功状态 | claim 与 gate 阶段不同；命令 dispatch 时间与范围的首次 dispatch 时间也不同 |

依据包括 EcsCommands._enqueue 的原输入比较、EcsLifecycle.before_send / record、MessageStore.finish、SnapshotTransactions.apply、TransportService._register、TaskCallbacks.register_revision / apply_result / _normalized、acceptance._basis / _saved 及 WMS stored_response。Transport 的原规范化业务事实不在本轮扩大为第二套统一裁决模型，避免把“减少重复字段”变成重写搬运状态机。

### 复用与变更量核对

已确认的新共享历史写入位于 app/integrations/history.py，协议无关端口沿现有 app/reliable/lifecycle.py 声明，装配仍在 reliable_runtime。当前首方调用依据包括 app/reliable/store.py 的 before_send / finish、app/reliable/lifecycle.py 的 SnapshotTransactions.gate / apply，以及 ECS lifecycle / Result 和原诊断 writer。它们共享保存记录及引用的格式，事务所有者、异常分类与必需 / 可丢弃语义不同，不能合并为一个自行开事务并吞异常的记录器。

先在调用方原事务保存类型明确的记录，再适配原公开读口，最后按 D6 / D7 收缩旧来源。可靠层继续使用注入端口，不直接导入业务或协议历史实现；同步 OutgoingMessage 装配不能通过 ORM 属性引入隐式异步 I/O。WMS get_result 与 Transport read_rows 通过显式联接装配必要原文字节，保留 no_autoflush、脱离 ORM 旧值、无写入 / 无锁 / 无发送及现有批量读取合同。

当前属于功能与存储迁移，不是已实现的减行成果。对共同记录写入 / 引用装配这一组，粗估原重复实现移除 40～70 行，新增共享实现及调用适配 60～120 行，净节省为 -80～+10 行；连同必要合同与调用回归，粗估移除 60～100 行、新增 160～300 行，净节省为 -240～-60 行。该估算不含模型、迁移、补录和新 API，不与 5 个旧实体的移除重复计数；没有净减行承诺。主要收益是权威正文与验证入口集中，以及 7 个对接实体收敛为 5 个。

方案使用现有模块、标准库及数据库约束，按 [Layer 1] 复用，不新增依赖、缓存、通用工作流或审计框架。角色 CHECK 与等价唯一约束由新模型明确表达，收据资格按原清理事务显式释放，不能将当前时间直接写入唯一索引谓词来自动释放资格。数据库索引表达式要求不可变，见 [PostgreSQL CREATE INDEX](https://www.postgresql.org/docs/current/sql-createindex.html)。

Code Quality 阶段结论为 2 项必需适配及上述字段核对，均回收到已确认行为的实现与测试要求，没有待回答的新增选择。新实现尚待验证，继续 Tests。

## 20. Tests review

框架依据仓库 AGENTS.md 的 Testing Guidelines，使用 pytest / pytest-asyncio / HTTPX，沿现有 SQLite 与显式隔离 PostgreSQL 参数，不新增框架。本阶段先以独立工具调用读取具体 Result、WMS 接收 / 重检、业务清理保护、并发及迁移测试，再绘制下列路径图。图中的“原合同 ★★★”表示现有测试覆盖了原行为及边界，不表示拟议存储已通过。所有新组件仍是计划。

```text
CODE PATHS / PROPOSED BRANCHES                         USER / OPERATOR FLOWS

G1 original create / enqueue -> typed exchange         submit / retry original business intent
  |-- invalid identity / different full input -> reject   |-- same call -> same command, no extra send
  |-- original winner -> compare bytes / scope            |-- concurrent winner rollback -> waiter can own
  |-- concurrent insert -> one winner / takeover          |-- outer rollback -> no accepted phantom
  '-- root + request + references + outbox atomic          '-- [GAP][->E2E] new model parity
  [original contract ★★★; new storage GAP]

G2 STATIC / SNAPSHOT Sender -> original gate             recover interrupted original delivery
  |-- revoked / expired / scoped stop / drift -> no send   |-- never-dispatched remains never-dispatched
  |-- core OPEN write / commit fails -> rollback, no HTTP  |-- unknown -> original HOLD / recovery
  |-- one HTTP -> known / unknown / ACK / rejection        |-- Result arrives before HTTP response
  |-- current lease -> finish + evidence together          '-- [GAP][->E2E] stale lease cannot reschedule
  '-- old lease -> original late observation / reconcile
  [original contract ★★★; new storage GAP]

G3 ECS Event / WMS receive -> original identity           peer retries / changes content / rechecks
  |-- invalid / too large / no identity -> bounded preview |-- reject unchanged when preview is lost
  |-- new receipt -> guard + handler + core atomic         |-- repeated trace is not one receive attempt
  |-- same identity -> original guard / RAW-EFFECTIVE      |-- WMS duplicate can still contain new writes
  |-- recheck unchanged -> rollback savepoint, replay      '-- [GAP][->E2E] diagnostics fail, ACK preserved
  |-- recheck changes -> core fact + response atomic
  '-- pure replay -> isolated best-effort observation
  [original contract ★★★; new storage GAP]

G4 original Result -> command / device / digest           peer reports original or conflicting Result
  |-- invalid / unknown original -> original response     |-- receipt / outbox gone -> replay still works
  |-- known semantic digest -> core compare -> replay     |-- adopted Result + late refusal -> reconcile
  |-- missing annex -> temporary failure, no new verdict  |-- public WMS / Transport read original results
  |-- early / blocked / impossible -> durable disposition '-- [GAP][->E2E] no false success or new execution
  '-- applicable -> result + settlement + notify-on-commit
  [original contract ★★★; new storage GAP]

G5 history list / detail / records / body [NEW]           search number -> related exchanges -> expand body
  |-- 401 / 403 / invalid filter / cursor -> safe error     |-- no results / expired body / legacy preview
  |-- identity/reference -> verify full value -> keyset    |-- same-time UUID rows across several pages
  |-- detail -> own read-only RR -> coherent sources       |-- revoke body permission between requests
  '-- body -> independent permission + metadata-only audit '-- [GAP][->E2E] no raw data in summary or audit
  [old helper contracts ★★★; all new endpoints GAP]

G6 backfill / final reconcile / authority [NEW]           operator prepares and resumes a migration
  |-- protect source -> bounded map + progress             |-- stop midway -> rerun without duplicates
  |-- changed source / digest mismatch -> retain, refuse   |-- copied row changes -> final read sees change
  |-- old writer / unresolved transaction -> refuse switch |-- residual writer -> no success declaration
  '-- stable source + exact parity -> cutover eligibility '-- [GAP][->E2E] new facts survive safe rollback
  [existing release checks are not cutover proof; GAP]

G7 cleanup -> original business lock order               operator cleans old ordinary history
  |-- source deadline not passed -> retain                 |-- 7-day body / 90-day summary differ visibly
  |-- pending / predecessor / anchor / unknown -> retain   |-- protected first batch does not starve later rows
  |-- new reference vs purge -> lock + reread               |-- expired receipt slot can accept new instance
  '-- eligible -> release slot / purge / delete separately '-- [GAP][->E2E] long ECS replay remains protected
  [original contract ★★★; new storage GAP]

G8 expand / schema / sequences / shrink [NEW]            operator upgrades and inspects schema
  |-- preserve original integer IDs + allocated highwater  |-- migrated event lookup and ordering unchanged
  |-- role checks / partial unique / FK / single head      |-- foreign tables and schema ownership untouched
  |-- metadata vs migrated PG -> equivalent constraints    |-- evidence exists -> unsafe downgrade refused
  '-- mismatch / old reader-writer remains -> no shrink    '-- [GAP][->E2E] full upgrade / protected rollback
  [original migration contracts ★★★; new migration GAP]

Legend: original contract ★★★ = existing behavioral / edge / error tests
GAP = not verified on the proposed storage
[->E2E] = HTTP / services / disposable PostgreSQL integration, no site claim
```

覆盖判断为 8 组计划路径全部需要新存储回归，其中 7 组可复用现有行为基线，G6 尚无对应最终权威交接的端到端证明。当前已验证的新存储路径为 0 组；尚无实现，不能计算代码行覆盖率。图中的组数用于核对范围，不是逐分支测量的覆盖率。没有 LLM、提示词或模型行为变更，不需要 eval。

### 必须补齐的 8 组证明

以下缺口均由已确认 D2～D9 和原调用合同直接产生，按必需回归承接，不再询问是否省略。P1 项为运行 / 证据 / 迁移风险，P2 为新增读取行为；confidence 均为 9/10。文件名标为“拟新增”的尚未创建。

| Finding / 路径 | 现有原行为依据 | 新路径必须断言的可观察行为 | 测试位置与层次 |
| --- | --- | --- | --- |
| F-T1 / G1，P1，CRITICAL | ECS test_service 的 duplicate / outer rollback，test_concurrency 的并发赢家；ECS / WMS 收据并发及赢家回滚测试 | 同调用只产生一个执行身份；外层回滚无根 / 正文 / 引用残留，等待者接管；角色唯一性不互相误占；旧及新 event_id、canonical / predecessor 顺序不变；命令身份不随收据槽过期释放 | 扩展原 create / receiver 测试；拟新增 tests/integrations/test_integration_history.py；PostgreSQL 并发集成 |
| F-T2 / G2，P1，CRITICAL | ECS test_flow 的 Result-before-ACK / known-not-received / late-rejection，test_snapshot 的 finish failure，reliable test_leases | STATIC / SNAPSHOT 发送前核心登记或提交失败均无 HTTP；发送后存储失败不假装对端拒绝；同租约完成结论替代 OPEN，新租约独立；失效租约不更新新状态；原身份 / 原期限 / 原恢复保持 | 扩展原 delivery / snapshot / flow / leases；真实事务失败及重启集成 |
| F-T3 / G3，P1，CRITICAL | WMS extensions 的 storage rollback / guard unavailable / recheck serialization，snapshots 配额 / 截断 / 失败测试 | 新受理、重检变化、新裁决中任何权威记录失败均整体回滚且无 after-commit 通知；真正纯重放在观察保存 / 提交失败或超时后仍返回原协议回复；不复用 aborted PG 事务；诊断保留原预算与来源，空正文不变 JSON null | 扩展原接收 / HTTP 测试，拟新增 tests/integrations/test_integration_history.py；单元及 HTTP + PG 集成 |
| F-T4 / G4，P1，CRITICAL | test_result_survives_receipt_and_delivery_cleanup，WMS test_missing_terminal_response_and_retention_never_mean_success，Transport test_result_reader_never_writes_locks_flushes_or_sends | 原 Result core / digest 冲突与长期重放；原数据到期、无当前 annex、迟到响应不造成功；WMS / Transport 终局回复选择、接纳证据摘要和 DTO 等价；读口无自动 flush、旧 ORM 污染、加锁、补录或发送；保持现有批量 / 单 SELECT 合同 | 扩展原 ECS / WMS Request / Transport 结果与接纳测试；公开服务集成 |
| F-T5 / G5，P2，正文权限部分 P1，CRITICAL | 原 pagination 类型 / 规范性 / 分页规则，permission grant / revoke / seed exact，audit GET / failure 合同 | 同时间多 UUID 分页无重复遗漏；非法 / 超长 / 跨筛选游标安全拒绝；0 / 1 / 100 条及时间边界；精确编号全值复核；详情只读一致；独立正文权限及撤权；FULL / PREVIEW / LEGACY_REENCODED / UNAVAILABLE / PURGED、NULL / 空字节区别；正文审计无原文、普通 GET 不变 | 拟新增 tests/controllers/test_integration_history.py 及 tests/services/test_integration_history.py；扩展原权限 / 审计测试；HTTP / PG 集成及纯格式单元 |
| F-T6 / G6，P1，CRITICAL | 现有 release bundle checks 仅证明选定制品 / 服务，不证明旧 writer 排空 | source key 防重及全值 / 字节核对；批次失败不推进已失败进度，重跑不重复、不发送；已复制后改变的旧行进入最终对账；遗留 API Sender / worker / one-off / 未结束写事务时不能宣告切换；新权威事实在回退路径保住；不一致阻止收缩 | 拟新增 tests/scripts/test_backfill_integration_history.py；迁移 / PG 集成，真实演练另行留证 |
| F-T7 / G7，P1，CRITICAL | BIN test_cleanup_and_new_passage_association_share_the_original_line_lock，protected-first-page / closed-member / wait-fact；可靠终态边界 | 清理与新增引用分别先取锁的交错都不丢正文 / 原事件；正文清空与删行使用同一保护；检查整数 / JSON 引用及原锚点，未知保留且批次推进；7 / 90 天原时钟、回填不续期、收据新资格与 ECS 长期命令身份分离 | 拟新增 tests/tasks/test_integration_history_retention.py，扩展原业务 retention；真实 PostgreSQL 锁与事务交错 |
| F-T8 / G8，P1，CRITICAL | test_project_schema 的 schema / foreign tables / sequence 所有权，以及原迁移保护测试 | 旧整数事件 ID 原样保留；新 ID 高于旧序列已分配高水位，即使最高旧行已删也不复用；新角色约束、等价唯一性、FK 和索引真实生效；单 head，Base 与迁移 schema 一致；旧预览字节 / NULL 来源 / 原时间不丢，未知关联不拼接，旧 JSON 不冒充 wire；有证据时拒绝破坏性降级 | 拟新增 tests/migrations/test_integration_history.py，继续运行 tests/migrations/test_project_schema.py；SQLite 与隔离 PostgreSQL 全升级检查 |

F-T5 的普通查询单次最大跨度已按 D10 / A 确认为 90 天，须验证恰好 90 天、超过上限、UTC 边界及精确原身份检索的不同合同。其他必需证明均已承接，无新增测试策略或深度选择。测试须通过原公开入口保护长期行为，不能只断言“旧表已消失”；生产业务 Base 不加入测试业务模型，沿原独立 MetaData / fixtures 清理约定。

### 验证门槛与当前证据

先完成原受影响合同回归，再在显式隔离 PostgreSQL 中验证迁移、角色约束、数值序列、并发、清理和只读快照，最后用原服务与本地受控 HTTP 对端贯通创建 / 发送 / Result / 消费 / 重启 / 清理。配置了隔离服务但不可连接应失败，不通过取消配置掩盖必需证明。角色门禁、原保持中的未知事实、越权和数据丢失必须阻止相应切换。

当前仅有第 14 节的原基线、R7 的 6 passed / 6 skipped 和 R8 的 20 passed / 19 skipped。这些运行范围存在重叠，不能相加成新覆盖成果；没有新测试套件、新 schema 或现场验收结果。PostgreSQL 未配置，相关跳过继续是未验证事项。

本阶段完成 8 组缺口核对，均进入必需回归范围，没有新测试深度选择。QA 测试计划另存于 /Users/kaizhou/.gstack/projects/kaizhoumasha-maintenance-admin/kaizhou-feature-integration-history-unification-eng-review-test-plan-20261106-214039.md，性能阶段如有实际取值变化将同步其时间范围边界。

## 21. Performance review

本阶段检查列表、时间线、恢复读口、写入、补录和清理的数据库成本。新增路径尚无执行计划或线上体量，以下为设计适配与待验证要求，不是已发生的慢查询报告。

### F-P1. 统一列表若联接全部记录 / 关联或加载 ORM 原文，会放大行数与读取

P2，confidence 9/10。第 4.3 节允许一条交互有多个 reference，第 8 节列表又需要关联检索和多类状态。直接将全部 reference、全部 record 和运行行联接后分页，会使同一 exchange 重复并使每页数量失真；逐行查询当前结果也可能产生 N+1。新根同时含有角色数据，直接序列化整行还可能读取不必要大字段。

按索引限定根候选，关联筛选使用 EXISTS 或等价的先去重候选集合，按 created_at / id 做 keyset 后，显式联接当前选择引用或批量读取本页所需的轻量事实。列表及普通记录页明确只 SELECT 摘要 / 引用 / 可用性，不加载 raw_body / effective_payload / 完整配置，不作全表 COUNT 或深 OFFSET。详情以本次独立只读 RR 得到一致证据，正文端点按权限显式取字节；不引入查询缓存掩盖代价。处置为原统一查询合同的必要实现要求，验证多引用不重复、固定查询数量和列表不取正文。

### F-P2. 初稿 31 天单次跨度不是已确认或已证明的性能约束

P2，confidence 9/10。第 8 / 9 节原建议 31 天，D7 / A 已确认普通历史保留 90 天。R9 / D10 / A 已另行确认普通时间范围检索单次最大跨度 90 天，正文与 QA 边界已同步。该选择不代表已证明运行性能，其余已确认范围保持。

### F-P3. 原恢复读取不能扫描整个新增观察时间线

P2，confidence 9/10。原 Result 按命令 / digest 读取语义证据，再检查原租约当前结论。归集后统一记录会同时包含诊断、重复收包、发送观察与业务关联。若恢复适配器按“这个 exchange 的所有记录”装入内存再判断，诊断增长会拖慢原核心锁内事务，并可能把历史 OPEN 当作有效结论。

按 record_kind 与原身份使用定向等价唯一键，Result 的原命令实例 / semantic_digest 直接定位权威裁决；发送有效结论按原实例 / lease 与明确替代顺序定向读取，不加载正文或其他诊断。新模型据真实查询安排相应有界索引，不能用最大 UUID / 最新 recorded_at 替代 D8 的有效结论。保留原 WMS / Transport 显式单 SELECT / 批量投影，正文引用使用定点联接而非每行延迟加载。处置为 D2 / D8 与原公开结果读取合同的必要性能适配，没有新增状态机或投影实体选择。

### 容量、写入与维护核对

- 原身份 / 类型化 reference、exchange 的时间 / UUID、record 的 exchange / 时间 / UUID、event_key，以及原命令 / digest 和原租约有效结论访问路径分别验证，不为 metadata 或每个枚举一律建索引。长外部值通过有界摘要命中后核对全值；宽正文和大 JSON 不进入覆盖索引。
- 使用原生行比较或等价的参数化时间 / UUID 条件，与排序和索引方向一致，时间为空的 occurred_at 不作为分页键。参见 [PostgreSQL 行比较](https://www.postgresql.org/docs/current/functions-comparisons.html)。
- 新表索引先于补录建立；既有大表索引按实际锁预算安排，若使用并发构建，处理独立事务及无效索引。受控停写窗口不等于所有准备阶段操作都可无限持锁，参见 [PostgreSQL CREATE INDEX](https://www.postgresql.org/docs/current/sql-createindex.html)。
- 从实际 record_kind 的每日数量、去重后平均原文字节、各来源期限、90 天普通元数据、受保护存量和索引计算容量，再计入迁移期间旧 / 新并存及 WAL / 备份空间。保留与压缩的实际影响单独测量，不用未经测量的压缩率或总 GB 数承诺成本。
- 原诊断预算仅限制诊断，不能拿来丢弃权威新受理或裁决。追加记录的索引和 WAL 成本、发送门禁与回写事务耗时、原业务锁等待需与原基线比较。
- 补录及清理沿原有界批次和已确认锁顺序，记录每批事务耗时、扫描 / 删除行数、来源校验和待保护比例。被保护行不阻塞后续候选；记录超时保持原恢复 / 保留责任，不另建重试调度器。

### 待执行的性能证明

在显式隔离 PostgreSQL 的代表性数据上，覆盖精确编号、单 / 多任务关联、ECS / WMS 或跨对端时间列表、同时间大量 UUID、长交互时间线、受保护旧结果读取及清理批次。按 D10 实际选择测试最大普通跨度，并加入不同筛选选择性；不通过仅测空表或首个 100 条证明整体性能。

保存 EXPLAIN (ANALYZE, BUFFERS)、实际访问 / 返回行、排序 / 临时文件、查询数量、总字节、核心事务与锁等待，以及补录 / 清理的批次时长。方案不预先承诺毫秒级延迟，具体验收阈值依据部署环境和原基线确定。此处是已确认功能的必要性能验证计划，本轮没有创建或运行新基准套件。

当前 Performance 有 3 项发现。F-P1 / P3 为必要适配；F-P2 / R9 / D10 已按实际答案 A 收口。本阶段无未回答选择，继续独立复核与报告收口，性能执行证明仍待实现阶段取得。

## 22. Outside Voice 可用性与覆盖

独立复核已按默认流程检查，codex_reviews 当前为 enabled。预检返回 CODEX_MODE: not_installed，指定 Claude Code CLI 未安装。当前会话也没有声明 Plan 型 Agent、TaskOutput 和 TaskStop，不能按备用流程启动可控评审；未改用普通协作代理冒充独立复核。

已构造包含文件边界、只读约束、批判任务、完整 D1～D10 和全部阶段结论的挑战提示，THE PLAN 正文仅取首 30,000 字节并注明截断，其余上下文未截断；未向缺失的提供方发起调用。私有提示临时文件在核对后已删除。没有外部结果、跨模型一致意见或独立通过信用。

记录为 host=codex、outside_provider=claude-code、phase=plan-review、source=none、outside_status=unavailable。该缺失不阻止继续整理实施任务，但必须在最终报告保留，不能报告为 clean。当前没有 Outside Voice 新发现可进入决策。

## 23. 范围、现有能力与失败模式收口

### NOT in scope

没有已接受功能被另行延期。D6 / A 选择受控窗口，未采用全程在线双写版本迁移协议；统一历史不会重写独立 Outbox / Transport 状态机，也不会把其他资产、账号授权或审计用途强行合表。上述边界分别来自实际路线选择和第 17 / 19 节的独立职责核对，保持完整的统一查询、原文归集、补录与技术存储收缩目标。

实际生产维护窗口与现场操作尚未执行，按 D6 在取得写入者清单、稳定来源和演练证据后落实，不因本评审收口而取得现场完成结论。

### What already exists

复用 [原发送器及租约事务](/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/reliable/store.py)、[ECS 原准入与迟到处理](/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/integrations/ecs_command/lifecycle.py)、[WMS 原接收器](/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/integrations/wms_callback/receiver.py)、[原筛选白名单](/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/services/filtering.py)、[原审计机制](/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/middlewares/audit_middleware.py)和[原保留保护入口](/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/businesses/_components/bin_roller/retention.py)。新增记录 / 查询 / 补录属于已确认职责，现有整数游标和普通 GET 审计并不能直接满足新查询，按 F-C1 / C2 补齐。

共享写入的调用、事务差异、净变更估算及回归证据在第 19 / 20 节，不从类似字段名称推导可删。新统一存储暂时会增加适配和验证代码，收益由权威正文归集与五个旧实体退出实现。没有新增消息总线、发送器、定时重试系统或数据库外缓存。

### Failure modes

| 新路径 | 现实失败推演 | 错误处理 / 使用者可见结果 | 必需证明 |
| --- | --- | --- | --- |
| 登记与共享记录 | 新根提交失败而原意图误留 | 原事务整体回滚，原协议临时失败；无已受理假记录 | F-T1 / T3，唯一赢家与提交失败 |
| 发送记录端口 | 保存 OPEN 失败或 HTTP 后保存失败 | 前者无网络，后者保留原身份 / 未知恢复，不能假装对端拒绝 | F-A2 / T2，两发送模式及原租约 |
| Result / 有效发送结论 | 历史 OPEN 错误越过已知未受理，或迟到拒绝覆盖采用结果 | ECS 专属有效结论适配、原冲突 / 核对规则与原回复 | F-A6 / T2 / T4 |
| 回调与诊断 | 重检包含新写入却当成纯重复，或快照异常改变 ACK | 新事实核心原子，纯重放隔离诊断；采集缺失明确显示 | F-A1 / A7 / T3 |
| 统一查询 / 正文 | 多关联重复分页、权限不足仍读正文，或列表自动加载全部原文 | 窄字段 / 去重候选、严格游标与独立正文权限，安全 400 / 401 / 403 / 不可用状态 | F-C1 / C2 / P1 / T5 |
| 业务消费与原结果读取 | 最近响应被当终局，未应用回调被展示为成功 | 保持原公开读口、规范化状态 / 版本及来源，未知不改成功 | F-T4，原 DTO / 接纳摘要与无副作用 |
| 预补录 / 最终交接 | 源行后来改变或残留旧进程继续写旧表 | 最终重采变化，未证明稳定则拒绝切换，来源继续保留 | F-A3 / T6，旧 writer 与未结束事务 |
| 保留与清空 | 并发新增业务引用时正文被清空 | 原锁序及重读、正文 / 行清理同资格；未知保留且批次推进 | F-A4 / A5 / T7 |
| schema / 收缩 | 迁移重用事件数值 ID，或有新事实仍直接降级 | 原序列高水位及引用保留；不一致 / 新事实未保住则拒绝收缩或降级 | F-T8，全 head / schema / 外表保护 |

critical_gaps=0，指上述设计路径没有“无处理、无验证要求且静默发生”的未定义失败模式；不是已通过 0 缺陷或新存储测试。所有必需证明仍要在实现阶段完成，尤其 PostgreSQL 并发、迁移和实际容量。

### TODOS.md 交叉核对

已读取现有 ECS / WMS 重报与保留合同、Transport 时效、来源认证、现场责任接管等 TODO。它们继续保留原状态，供应商重报与现场维护条件会影响 D6 的实际窗口准入；本次不以历史归集关闭这些合同或现场事项。没有另外提出需要延期的新功能，TODOS.md 新提案数为 0，文件未改动，必要实现 / 验证全部留在本评审任务。

## 24. Worktree parallelization strategy

Sequential implementation, no parallelization opportunity.

本次先以一个工作流实施。模型、记录读写、原协议事务和迁移均共享核心引用合同，拆成并行写入工作区会同时修改同一模型及历史模块；目前没有两个已证明独立且能单独交付的实现流。继续使用当前 feature/integration-history-unification 独立工作区，保留原工作区及现有暂存内容。

| Step | Modules touched | Depends on |
| --- | --- | --- |
| T1 角色模型与扩展约束 | app/models, migrations, tests/migrations | — |
| T2 原事务与中立记录端口 | app/integrations, app/reliable, app runtime | T1 |
| T3 ECS 命令 / 发送 / Result 适配 | app/integrations/ecs_command, app/integrations/ecs_callback | T2 |
| T4 WMS / ECS 接收与诊断 | app/integrations/wms_callback, app/controllers | T3 |
| T5 字节引用与原公开读取 | app/reliable, app/integrations/wms_request, app/integrations/wms_transport | T4 |
| T6 正式查询与正文审计 | app/services, app/controllers, app/schemas, app/middlewares, scripts | T5 |
| T7 补录、最终对账及运行准入 | scripts, tests/scripts, docs/integrations | T6 |
| T8 原保留与清理保护 | app/tasks, app/businesses/_components, tests/tasks | T7 |
| T9 代表性查询与核心事务性能 | app/services, app/integrations, tests/services, docs/testing | T8 |
| T10 收缩、完整回归与交付文档 | migrations, tests, docs/integrations | T9 |

执行顺序为 T1 → T2 → T3 → T4 → T5 → T6 → T7 → T8 → T9 → T10，均在前项合同稳定后推进。运行启用仍按 D6 的受控窗口和最终对账，代码任务顺序不能解释为提前激活新权威。复杂模型 / 锁序 / 切换图在实施时随 app/models/integration_history.py、app/integrations/history.py、原 ECS lifecycle / result、补录脚本和清理入口更新，避免与本设计的 ASCII 图分离。

## Implementation Tasks

下列任务由本评审发现推导，供后续实施逐项验证，当前均未执行。估时针对改造与隔离验证，不包含现场维护时长、供应商等待或实际生产操作；涉及原事务和恢复合同，按较保守的工程压缩比例估算，各任务边界不得重复计算同一回归工作。

- [ ] **T1 (P1, human: 约 3 天 / CC+gstack: 约 6 小时)** — 统一模型与扩展迁移 — 建立带角色约束的交互根、证据和引用，保留数值事件序列
  - Surfaced by: F-S1/S2/S3, F-T1/T8。
  - Files: `/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/models/integration_history.py`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/models/__init__.py`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/migrations/versions`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/tests/migrations/test_integration_history.py`。
  - Verify: 在隔离 PostgreSQL 验证角色唯一、整数事件高水位、外层回滚及 schema 等价。

- [ ] **T2 (P1, human: 约 2 天 / CC+gstack: 约 4 小时)** — 核心事务与可靠端口 — 接入原会话中的核心记录并隔离持久化异常与协议结果分类
  - Surfaced by: F-A1/A2, F-T2/T3。
  - Files: `/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/integrations/history.py`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/reliable/lifecycle.py`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/reliable/store.py`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/reliable/delivery.py`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/reliable_runtime.py`。
  - Verify: 两发送模式登记失败均无 HTTP；发送后回写失败保持原恢复，不产生假拒绝。

- [ ] **T3 (P1, human: 约 2 天 / CC+gstack: 约 4 小时)** — ECS 命令与结果适配 — 迁移命令、发送有效结论和 Result 原重放读取
  - Surfaced by: F-A6, F-T1/T2/T4。
  - Files: `/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/integrations/ecs_command`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/integrations/ecs_callback`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/tests/integrations/ecs_command`。
  - Verify: 按原租约判断当前结论，保留原调用身份、迟到核对及清理后长期重放。

- [ ] **T4 (P1, human: 约 1.5 天 / CC+gstack: 约 3 小时)** — 回调与诊断 — 适配入站收据与快照，区分核心新事实和纯重复观察
  - Surfaced by: F-A1/A7, F-T3/T8。
  - Files: `/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/integrations/wms_callback`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/controllers/wms_events.py`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/controllers/ecs_callback.py`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/controllers/ecs_result.py`。
  - Verify: guard/recheck 及保存点等价；诊断配额、截断、超时、失败不改变原回复。

- [ ] **T5 (P1, human: 约 2 天 / CC+gstack: 约 4 小时)** — 原文引用及公开读口 — 去重冻结正文与响应副本并保持公开结果和接纳依据
  - Surfaced by: F-C1/C2 以外的字段审查, F-T4, F-P3。
  - Files: `/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/integrations/wms_request/results.py`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/integrations/wms_envelope.py`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/integrations/wms_transport`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/reliable/types.py`。
  - Verify: 公开 DTO 与原接纳摘要等价，无 flush/锁/发送，保持单 SELECT 或原批量读取。

- [ ] **T6 (P1, human: 约 2 天 / CC+gstack: 约 4 小时)** — 正式历史查询与审计 — 实现窄字段查询、复合游标、独立正文权限和留痕
  - Surfaced by: F-C1/C2, F-P1/P2, F-T5。
  - Files: `/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/services/integration_history_service.py`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/controllers/integration_history.py`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/schemas/integration_history.py`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/middlewares/audit_middleware.py`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/scripts/seed_admin.py`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/main.py`。
  - Verify: 90 天查询和同时间多 UUID 分页，越权/撤权拒绝正文，普通 GET 与原审计失败行为不变。

- [ ] **T7 (P1, human: 约 3 天 / CC+gstack: 约 6 小时)** — 补录与权威交接 — 实现可恢复补录、最终可变行对账和受控切换准入
  - Surfaced by: F-A3, F-T6/T8。
  - Files: `/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/scripts/backfill_integration_history.py`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/scripts/release_bundle.py`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/tests/scripts/test_backfill_integration_history.py`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/docs/integrations`。
  - Verify: 中断重跑不重复或发送；残留 writer/写事务或对账不符拒绝切换；不猜旧快照关联。

- [ ] **T8 (P1, human: 约 2 天 / CC+gstack: 约 4 小时)** — 保留与清理 — 沿原锁序适配保护、受理槽释放及独立来源期限
  - Surfaced by: F-A4/A5, F-T7。
  - Files: `/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/tasks/callback_tasks.py`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/tasks/reliable_tasks.py`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/businesses/_components/bin_roller/retention.py`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/tests/tasks/test_integration_history_retention.py`。
  - Verify: PG 双向交错不丢保护正文；7/90 天边界、来源时钟、长期 ECS 身份与批次推进等价。

- [ ] **T9 (P2, human: 约 1 天 / CC+gstack: 约 2 小时)** — 查询与核心事务性能 — 按代表性 PostgreSQL 数据验证索引、90 天查询和核心读取成本
  - Surfaced by: F-P1/P2/P3, F-T5。
  - Files: `/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/services/integration_history_service.py`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/app/integrations/history.py`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/tests/services/test_integration_history.py`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/docs/testing`。
  - Verify: 保存执行计划、扫描/返回行、查询数量、排序、字节及锁等待；列表不读原文，无 N+1。

- [ ] **T10 (P1, human: 约 2 天 / CC+gstack: 约 4 小时)** — 收缩迁移及完整验证 — 在新读写全适配且对照通过后退出旧表与重复原文字段
  - Surfaced by: F-T1-T8, F-A3/A4。
  - Files: `/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/migrations/versions`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/tests/migrations/test_integration_history.py`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/tests/migrations/test_project_schema.py`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/tests/integrations`、`/Users/kaizhou/.codex/worktrees/integration-history-unification/maintenance-admin/docs/integrations`。
  - Verify: 完整创建/发送/回调/消费/重启/清理回归，单 head/schema/外表保护；新事实未保住时拒绝破坏性降级。

任务 JSONL 已保存于 /Users/kaizhou/.gstack/projects/kaizhoumasha-maintenance-admin/tasks-eng-review-20261106-215148.jsonl，共 10 项，由每项独立 jq -nc 构造并回读校验。QA 测试计划见第 20 节，实际代码 / 迁移完成后才可勾选对应任务。

## 25. Completion summary

- Step 0 / Scope Challenge：功能范围保持完整，按 D1 / B 收敛现有分层；F-S1～S3 按实际答案合并 Result / 收据 / 命令。
- Architecture：7 项发现，全部有实际选择或原合同下的必要适配，详见 F-A1～A7。
- Code Quality：2 项发现，复合游标与正文 GET 审计接入已加入实施要求。
- Tests：代码 / 用户路径 ASCII 图已生成，8 组新存储回归缺口已列入必需证明；原测试不能替代新 schema 验证。
- Performance：3 项发现，90 天单次跨度已确认，列表与恢复读口按定向 / 窄字段访问验证。
- issues_found：20，等于 7 + 2 + 8 + 3，包含已决定但尚待实施的发现，不包含 Scope Challenge 的 3 项重复结构选择。
- NOT in scope / What already exists：已写入，第 23 节。
- TODOS.md：新提案 0，原待办保持。
- Failure modes：critical_gaps=0，没有无处理且无验证计划的静默失败路径；实现与执行证明仍未完成。
- Unresolved engineering decisions：0。
- Outside Voice：claude-code / plan-review / unavailable，CLI 未安装，规定的原生备用能力未声明；不计独立复核通过。
- Parallelization：1 个顺序工作流，0 并行 / 10 顺序任务。
- Lake Score：N/A，0 个按覆盖程度评分的选择；本次选择均为结构、策略或路线取舍。
- Completion status：DONE_WITH_CONCERNS。设计选择已收口，共同理解已按 D11 / C 确认，进入实施计划；20 项实施 / 验证要求和缺少独立复核仍需明确保留。
- 当前事实：未修改产品代码，未实施数据库迁移、生产操作、提交或推送。设计文件已有的暂存内容保持，后续工作区修改未重新暂存。

## 26. 后续导航

导航保持第 24 节的任务依赖与顺序，不批准新的实施改变；不计入未回答的工程选择。

Header 为 评审后续。

Question D11 如下。

D11 · 确认评审结论，进入实施计划？

请回复 C，或直接指出需要调整的结论。<gstack-qid:plan-eng-review-implementation-navigation>

本次后端工程评审已收口，D1～D10 的工程选择没有未回答项。20 项发现已映射为 10 个实施任务，当前尚未完成代码、迁移或性能验收。

Recommendation 为 C：按已确认设计进入实施计划，沿现有独立工作区和 T1 → T10 的依赖顺序安排工作。这里仅确认共同理解及下一阶段，不增加功能、改动任务前置条件或提前执行生产切换。

这是后续导航，各取值的覆盖程度不可比较，不作完整度评分。

C) 确认结论，进入实施计划 (recommended)

保留本次已确认的五模型结构、原执行与恢复合同、90 天普通历史及查询跨度，并把已有 10 个任务展开为具体实施步骤，能直接沿当前工作区继续。代价是迁移、PostgreSQL 并发与实际容量证明仍需在对应任务中取得，当前不能据设计结论宣布可上线。

Net 为工程取值已收口，本题只确认结束本轮质询并进入实施计划。

这次共同理解确认来自你指定的 [grill-me](/Users/kaizhou/.cc-switch/skills/grill-me/SKILL.md) 所调用的 [grilling 规则](/Users/kaizhou/.cc-switch/skills/grilling/SKILL.md)：“Do not act on it until the user confirms you have reached a shared understanding.”

State 为 approved_navigation。

Actual answer 为用户 D11 / C，“选 C，确认评审结论，按已确认的设计进入实施计划。”

Accepted scope 为确认本轮共同理解并按已确认设计编写实施计划，沿当前独立工作区和 T1 → T10 的任务依赖；D1～D10 与工程评审取值保持，实际实现、数据库迁移和生产切换仍沿计划及原运行准入推进。

## Suppressed findings

没有隐藏的 P1 / P2 发现或外部评审结果。未把原输入与冻结 wire、最近与终局响应、已应用与待前置事实、资产配置与原路由、账号授权与审计归档当作重复实体；判断及保留原因在第 17 / 19 节。没有证据支持新增大范围抽象或承诺已修复运行缺陷，因此未提出这些改造。

## GSTACK REVIEW REPORT

本次 Eng 行从当前收口摘要生成，在报告保存时尚未登记日志，标为 not persisted；保存回读后再按顺序登记，发布仪表板以实际日志为准。此前无其他评审记录，Outside 行已有本次不可用记录。

| Review | Trigger | Why | Runs | Status | Findings |
| --- | --- | --- | --- | --- | --- |
| CEO Review | /plan-ceo-review | Scope & strategy | 0 | 未运行，本次未新增战略选择 | — |
| Outside Review | claude-code / plan-eng-review | Independent 2nd opinion | 1 | unavailable | host=codex, source=none, phase=plan-review；CLI 未安装，规定备用能力缺失 |
| Eng Review | /plan-eng-review | Architecture & tests (required) | 1 | ISSUES OPEN (PLAN; not persisted at report generation) | 20 issues，0 critical gaps，0 unresolved engineering decisions |
| Design Review | /plan-design-review | UI/UX gaps | 0 | 未运行，当前为后端范围 | — |
| DX Review | /plan-devex-review | Developer experience gaps | 0 | 未运行 | — |

**OUTSIDE COVERAGE:** provider=claude-code，host=codex，phase=plan-review，outside_status=unavailable，未完成独立复核，无外部结论或跨模型信用。

**VERDICT:** 设计选择已收口，发现均映射为必需实施 / 验证工作，当前没有 CLEAR 的工程实现验收记录；eng review required。issues_open 按规则计入已决定但尚待实现的 20 项发现，不等于存在未回答设计问题或已验证运行缺陷。后续导航不改变发布门禁。

NO UNRESOLVED DECISIONS
