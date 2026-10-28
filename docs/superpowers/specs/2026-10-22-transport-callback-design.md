# Transport 回调接收开发说明书

> **接入代码历史说明（2026-10-28）。** 本文的旧业务目录、发现器、字符串/字典 SDK 或手写绑定示例已被[单一 API 接入](../../integrations/business-development-quickstart.md)及[验收记录](../../integrations/business-development-single-api-acceptance.md)替代；三类 registry 的独立 discovery.py 已删除，当前使用共享加载器和 catalog/固定平台 manifest。原协议、冻结数据、身份、结果与历史执行记录保持其原有语境，本文不作为当前 SDK 示例。


状态:**第四版已实现,2026-10-23 首轮代码评审见 §18,解除环境限制后的续验见 §19。通用接收、operation 注册、Transport 请求事务内应用均已落地;完整套件、PostgreSQL 并发与本机 Mock 五层架链路已通过。真实 WMS/设备联调按用户最新决定改为本机 Mock,不计作现场验收。§0–§13 为现行方案,§14–§16 为第三版历史记录,§17 为第四版设计评审记录** · 依据:`wes_backend/docs/contracts/transport-fulfillment-contract.md` §5(v0.4.0 Approved)、`wms-async-callback-envelope-contract.md`、`2026-10-21-transport-outbound-design.md`、`2026-10-22-rack-transport-integration-design.md` §5 · 范围:WMS → 本项目的统一回调入口,以及首个具体业务 `transport.task.member_position_changed@v1`、`transport.task.resulted@v1`

## 0. 第四版修订:在 `transport_task` 上直接应用(取代证据表与后台应用)

依据与原则:wes_backend 的合同可以优化而不是照搬,**对端可见的 wire 和 payload 尽量不变**。第三版为满足合同 §5.4「ACK 前不在 HTTP 请求内应用任务状态」设计了 `transport_evidence` 表、后台扫描、租约、类别配额与证据行/任务行锁顺序。该条描述的是 wes_backend 的内部处理顺序,并非对端能观察到的行为;对端观察到的行为(同一信封、相同的 `202/200/409/422/503` 语义、未知任务与事实冲突仍先返回 `202`、WMS 不需要重发)在第四版**完全不变**。因此取消证据表,处理器在请求事务内锁任务行并直接应用:

| 第三版 | 第四版 |
|---|---|
| `transport_evidence` 表、待处理索引、领取令牌与租约 | 无;状态直接落在 `transport_task` |
| 后台扫描、类别配额、满批续扫、锁顺序「证据→任务」 | 无;处理器内 `SELECT transport_task ... FOR UPDATE` 后应用 |
| 料箱结果先到则证据保持 `PENDING` | `transport_task.pending_outcome` 暂存最高版本结果,位置事件应用后在同一事务内完成 |
| 步骤 3–6 四步 | 步骤 3(接收并应用,含「收到回调即结束提交消息的发送」)一步;`STALLED` 定时对账取消(决定 10) |

代价与约束(已评估):(1) 未知任务没有任务行可写,诊断与收据同事务持久化,提交成功后输出结构化告警日志,原文另靠诊断快照保留 7 天,对端仍得 `202`;(2) 应用逻辑**对已通过 DTO 校验的消息不得抛异常**(冲突、未知任务、冻结缺失一律记录并返回 `Accepted`),否则会形成无法越过的毒消息,只有数据库/序列化等真异常才走通用 `503`;(3) 不再逐条留存每个位置事件与结果版本,审计有收据诊断、任务行当前状态、版本摘要与快照,有需要再加表;(4) 请求内多一次任务行锁,同任务的回调串行,实际并发能力待 PostgreSQL 验收。

### 已确认的决定(2026-10-22 讨论)

1. **合同可优化而不是照搬**,对端可见的 wire 和 payload 尽量不变;偏离合同文字时在文档里说明原因(第四版取消证据表即是一例)。
2. **取消证据表与后台应用**,回调在请求事务内锁 `transport_task` 行后直接应用;未知任务、事实矛盾、终态矛盾只告警并返回 `202`,应用逻辑对通过 DTO 校验的消息不抛异常。
3. **搬运层不预查、不阻塞**(§6.1):先后依赖由调用方按场景决定,依据任务的闭合状态;被拒(`409/422`)算闭合,结果不明(超时/无响应)不算未下发。现场事故(回架未完成就旋转、放箱未完成就离场)表明「WMS 接纳」不等于「顺序正确」。
4. **不新增按货架/料箱查询的关联表**,`frozen` 是事实来源,将来由它回填;「闭合」语义与「卡住任务告警」已记 `TODOS.md`。
5. **写法尽量简单**:继承 `CallbackOperation` + 内嵌 `Payload` + 实现 `execute`;自动发现与 Swagger 示例自动生成。
6. **命名**:通用机制不使用仓储业务词 `inbound`(入库),统一用 `callback`。
7. **步骤**:通用接收、operation 注册、Transport 接收并应用;第 1、2、3 步已实现。`STALLED` 定时对账已取消(决定 10)。
8. **第四版评审补充(第一轮)**:ACK 一律回显 `transport_task_id`;只告警的事实写入 `callback_receipt.diagnostic`;JSON 列整体赋值并用新会话读回断言;(原定的对账 beat 已取消,见决定 10)。
9. **第四版评审补充(外部审阅,2026-10-23)**:身份冲突的 `409` 通过 `echo_keys` 回显首次消息的任务编号;`pending_outcome` 复用同一个应用函数并带跨版本失效规则;任务行保存完整规范化结果 `applied_outcome`;成功结果的目标匹配按任务类型/状态区分(判定表见 §6.2);未知任务的「新身份同版本」重发返回 `202` 是有意偏离;结果应用后的 `STALLED` 恢复限定 Transport 提交消息;回调事务设置 `lock_timeout`;迁移从 `request_raw` 回填 `frozen`。
10. **取消 `STALLED` 定时对账,回调即结束发送(2026-10-23)**:`STALLED` 只在「确认未送达」(请求未发出,或对端明确 `503 UNAVAILABLE`)累计达到目标的 `max_not_received_attempts` 时产生,默认不设预算,即默认永不进入 `STALLED`;结果未知(超时、中断、无法解析)不计数、不进 `STALLED`,始终以原身份按退避重试。重试策略按「能否保证不重复执行」区分:确认未送达与结果未知持续退避重试(间隔上限与 `alert_after_attempts` 告警),对端明确拒绝(`400/413/409/422`)直接 `FAILED`。过期的物理指令不应在很久之后自行下发,时效由调用方通过消息 `expires_at` 控制。因此不做定时盲目 `resume`;改为**任务一旦收到任何回调(位置事件或结果,不论是否与冻结任务相符),就结束该任务提交消息的发送**(`Outbox.settle`,见 §6.4)。对端长期不可用靠卡住任务告警(`TODOS.md`)与显式 `Outbox.resume` 处理。
11. **位置里程碑顺序无关(2026-10-23)**:位置事实记成集合,唯一判定依赖是「已有等于冻结目标的 `TARGET_PLACED`」;`POSITION_UNKNOWN` 不再存储,只写诊断。合同枚举与 wire 不变。这样不需要向 WMS 要顺序承诺。
12. **收据保留期对齐 `RELIABLE_RETENTION_DAYS`(2026-10-23)**:`callback_receipt` 按该配置每日清理,不新增配置项;`created_at` 加索引(迁移 `a022a20261023`)。合同要求的清理周期变更在 §9 向 WMS 确认。
13. **提交消息 1 小时超时(2026-10-23,暂定)**:同一消息身份的最长重发时间不超过 1 小时,超过后再发已无意义。出站侧 Transport 提交消息统一带 `ttl = SUBMIT_TTL`(1 小时,`wms_transport/protocol.py`),到期未送达的消息由发送层转为 `EXPIRED` 而不再发送;对 WMS 的回调侧同样按 1 小时作为其重发窗口提请确认(§9)。过期后任务没有结果,由卡住任务告警(`TODOS.md`)暴露,由上游决定是否用新 `client_request_id` 重新下发。
14. **合同符合性复核后的取舍(2026-10-23)**:(1) **创建前不做可信位置/当前面校验**是有意偏离(合同 §3.2、§4.1 要求换面前有可信精确位置与当前面、料箱同架面须等于可信当前面):没有位置投影,由 WMS 在接纳时兜底(`409/503`),我方只做请求内结构校验,不做补救。(2) **来源认证降为低优先级,联调完成后再实施**。(3) **换面模板 `CK04` 与 `CTU02` 同为旋转动作**:`ROTATE_TEMPLATES = {CTU02, CK04}`,转运/退料架用 `CK04`、五层架用 `CTU02`;合同文本只列 `CTU01/CTU02/CTU03/F01`,待合同同步。
15. **货架生命周期分组键 `cycle_id`(2026-10-24)**:进场 → [换面…] → 离场作为一个货架的完整生命周期;`transport_task.cycle_id`(可空、有索引)是纯本地分组键,**不进入 wire、冻结请求与幂等摘要**,所以无需与 WMS 重新商定合同。每一步仍是独立任务(自己的 `transport_task_id`、版本、终态、重试隔离);不共用 `transport_task_id`,因为回调靠它区分物理任务,共用会让终态不可改写规则和重试隔离失效。`RackCycle` 创建时生成 `cycle_id`,后续步骤在别的进程重建句柄时传入首次的值;同一 `client_request_id` 重放以首次登记为准。料箱场景提供可选 `cycle_id` 参数,同一面的多批搬运想归组时才传。只做分组,不新建批次表,也不定义批次何时结束(留在 TODOS 的闭合语义)。
16. **卡住任务告警(2026-10-24)**:`transport.scan_stuck`(beat 每 10 分钟,`app/tasks/transport_tasks.py`)扫描创建超过 `TRANSPORT_STUCK_ALERT_MINUTES`(默认 120,暂定)仍无确定终态的任务(无结果、`UNKNOWN`、料箱成功结果暂存),输出结构化告警日志 `transport task stuck`,`reason` 为 `SUBMIT_NOT_DELIVERED / RESULT_MISSING / RESULT_WAITING_FOR_POSITIONS / OUTCOME_UNKNOWN`。只读,不改状态,不伪造结果;仍卡住的任务每次扫描重复告警;每批最多 200 条,按任务主键继续扫描后批,避免最老任务持续遮蔽其余任务。通知渠道与责任人由现场 SOP 决定。

## 1. 目标与边界

### 1.1 目标

与出站的实施方式一致:先做**统一的 FastAPI 接收 API**(信封、幂等、ACK、诊断快照),再做 **operation 注册装饰器**,最后**按业务需要逐个实现具体 operation**。Transport 是第一个具体业务:在请求事务内把位置与结果直接应用到 `transport_task`(与收据同事务)后返回 ACK,并让出站侧 `STALLED` 有权威结果出口。这是货架接入文档 §5 后续阶段(滚动窗口、业务重试、换面)的前置条件。

### 1.2 非目标

- 来源认证:合同 §5.1 现行为部署认证 `NONE`,暂不实施,已记 `TODOS.md`;路由层留一个认证检查位,不实现。
- 通用层不认识任何具体业务:不含 Transport 的字段、表或规则。
- 滚动窗口、业务重试、`departure_decide`/换面:依赖本文产物,另行设计。
- `outcome_version` 对外发布(合同 §5.4 第 8 步):没有下游消费者前不做。
- 跨任务的货架/料箱当前位置表与 `RackStateReader` 项目内实现:没有调用方,不做。
- 非 Transport 的 WMS operation:通用层为其预留注册点,本文不实现。
- 联调控制台的「回调投递」场景:已实现(`POST /api/v1/dev/transport/{transport_task_id}/callbacks`,仅 local),不属于本文范围外的未决项。
- 不改 `app/http_client`;`app/reliable` 仅允许已确认的 `Outbox.settle` 扩展(§6.4),不增加业务规则或第二套重试调度。

## 2. 实施前基线(历史,当前进度见 §18)

下表保留设计时的缺口,不代表当前源码状态;当前模型、注册能力、清理与验收边界以 §18 为准。

| 事实 | 来源 |
|---|---|
| `transport_task` 只有身份、`kind`、摘要(哈希)、产线、工位、创建时间;**没有成员、目标、面向、`rack_id`,也没有状态列和终位列** | `models/transport.py` |
| 冻结请求字节只在 `reliable_message.request_raw`(紧凑 JSON),终态消息 `RELIABLE_RETENTION_DAYS`(90 天)后被物理清理 | `service.py`、`reliable/store.py` |
| `Outbox` 原有 `enqueue`、`resume` 两个公开方法;`STALLED` 只能被 `resume` 放回 `PENDING`;没有按 `business_key` 置终态的接口。`resume` 会把 `not_received_count` 清零(第 3 步新增 `settle`,见 §6.4) | `reliable/outbox.py`、`store.py` |
| `ReliableMessage.business_key = transport_task_id`,仅普通索引,不唯一 | `models/reliable.py` |
| 分层测试:`app/integrations` 不得导入 `fastapi`、`controllers`、`schemas`、`services` | `tests/architecture/test_layering.py` |
| 全局默认 `RateLimit(100)` 挂在 app 上;`RateLimit` 只支持同 name 实例覆盖上限,没有豁免;`new_router` 会自动声明 `429` | `main.py`、`core/ratelimit.py`、`core/routing.py` |
| 全局异常处理器对未捕获异常返回 `500` 加项目错误 JSON,不是合同允许的响应 | `core/handlers.py` |
| 出站适配器里已有严格 JSON 解析(拒绝重复 key、NaN) | `wms_transport/adapter.py:30-50` |
| 出站已有注册装饰器先例:`@dev_scenario`,新增业务只新增一个文件 | `core/dev_scenarios.py` |
| 已有两套保留期配置与 `run_*` 批量清理 + beat 模式 | `core/config.py`、`tasks/` |
| `AuditMiddleware` 为纯 ASGI,只记成功写操作且有体积上限,**不能复用**于回调快照 | `middlewares/audit_middleware.py` |
| 单测用 `Base.metadata.create_all`(不跑迁移),PostgreSQL 集成测试跑 alembic | `tests/conftest.py` |
| `operation_id` 必须是 UUIDv7,校验走 `app/core/identifiers.py` | 合同、项目约定 |

## 3. 架构

```text
app/controllers/wms_events.py               POST /api/v1/wms/events:有界读 body → 交给通用接收 → 回写响应;预留认证位;响应后写快照
app/integrations/wms_callback/               通用接收(无 fastapi,不认识任何业务):信封解析、收据幂等、ACK、operation 分发与注册装饰器
app/integrations/wms_transport/callback/   Transport 的两个 operation:DTO 校验、冻结事实校验、直接应用到 transport_task(用装饰器注册)
app/core/strict_json.py                     严格 JSON 解析(出站适配器与通用接收共用)
app/models/callback.py                       CallbackReceipt、CallbackSnapshot(通用);Transport 不新增表,只给 transport_task 加列
app/tasks/callback_tasks.py                  快照清理
```

- 分三层:**通用接收**(第 1 步)→ **operation 注册**(第 2 步)→ **具体 operation**(第 3 步起)。通用层只认「operation 名 → 处理器」,不含 Transport 的列、表或规则。
- 信封合同 §6 规定 `ack_mode`/`ack_commit_facts` 不进入运行时 registry:装饰器**只注册 operation → 处理器**,不带 `ack_mode` 等元数据,这些继续只写在合同与文档里。
- 自动发现:`include_wms_events` 在启动时导入 `app/integrations/<业务>/callback`(模块或包,包会连同子模块一起导入;`dev_callback` 仅 local),装饰器在导入时注册,新增 operation 不改控制器和 `main.py`(见 §3.1 实现说明)。
- 路由是唯一入口,不另开第二路径或旁路持久化。
- **限流豁免**:全局 `RateLimit(100)` 会对回调返回 `429`,与合同冲突。`RateLimit.__call__` 增加 `limit is None` 直接放行的分支,回调路由用 `RateLimit(None)` 覆盖默认;路由使用普通 `APIRouter`(不用 `new_router`,否则 OpenAPI 会自动声明 `429`)。
- **共用严格 JSON 解析**:`adapter.py` 里的 `_unique_keys / _reject_constant / _json_object` 提到 `app/core/strict_json.py`,适配器与通用接收共用(拒绝任意层重复 key 和 NaN/Infinity),`test_adapter.py` 现有用例作回归保护。

### 3.1 处理器契约(第 2 步引入)

> **推荐写法(降低心智负担)**:继承 `CallbackOperation`(`wms_callback/operation.py`),内嵌 `Payload(CallbackPayload)` 声明业务字段(严格 JSON 类型、忽略冗余字段、`json_schema_extra.examples` 作 Swagger 示例),只实现 `async execute(ctx, payload)`:返回 `dict`/`None` 即 `Accepted`,`Accepted/Duplicate/Conflict/Rejected` 既可 `return` 也可在深层调用里 `raise`(接收器统一接住,`Rejected` 回滚保存点)。基类自动提供下面契约里的 `parse`/`handle`;直接手写 `parse`/`handle` 的结构化契约仍受支持。示例见 `dev_callback.py`。

> 第 2 步已实现(`app/integrations/wms_callback/registry.py`、`receiver.py`):operation 名须匹配 `域.名称@vN`(小写字母/数字/下划线,版本从 v1 起,≤256 字符);已接纳路径先写临时响应占住身份,处理器返回后在同一事务内覆盖;`Rejected` 显式回滚保存点;测试用独立的 `callback_probe` 表验证「Rejected 不留业务行」。**自动发现**(`wms_callback/discovery.py`):`app/integrations/<业务>/callback.py` 所有环境导入,`dev_callback.py`(含示例 `example.echo@v1`)仅 local 导入;类属性 `title`/`example` 自动生成 Swagger 请求体示例,`main.py` 只有 `include_wms_events(...)` 一处接线,新增 operation 不改控制器和 `main.py`。

```text
@callback_operation("<operation>@v<N>")
处理器提供两个阶段:
  parse(data) -> 规范化 data       纯函数,只校验业务 DTO、剔除冗余字段;非法则抛 InvalidPayload(reason_code)
  handle(ctx, parsed) -> Outcome   在通用层打开的保存点内执行,写业务自己的行(如更新任务状态)
ctx = {session, receipt_id, operation_id, timestamp, parsed};处理器不保存请求状态
Outcome = Accepted(data) → 202 RECEIVED | Duplicate(data) → 200 | Conflict(data) → 409 | Rejected(reason_code) → 422
```

- **信封归通用层**:通用层校验必需的 `operation_id/operation/timestamp/data` 四个字段(`timestamp` 为 `0..2^63-1` 的整数,不接受 bool),顶层冗余字段忽略;Transport 处理器的纯 `parse` 按具体合同忽略业务各层冗余字段并规范化有效字段。处理器只返回 `Outcome`,不拼响应。公共信封所说的“完整内容”在 Transport operation 上按具体合同 §5.4 的规范化有效字段解释,不据此宣称公共合同文字已同步(D3 / 2A 已确认)。
- 收据由通用层写(身份、摘要、首次响应);处理器的业务行与收据**同一事务**。
- **事务顺序与四种 Outcome 规则**:事务内先占收据(保存点外)→ 开保存点调用处理器 → `Accepted/Duplicate/Conflict` 释放(提交)保存点,`Rejected` 显式回滚保存点 → **先构造并校验响应**,再把首次响应写回收据 → 提交。`Duplicate/Conflict` 要求处理器在返回前不留业务行(先判定后写入);处理器不得自行 `commit/rollback`,也不得产生无法随数据库回滚的外部副作用;意外异常回滚整个接收事务并返回 `503`。收据因此永远带完整可重放的响应,不存在业务已提交而响应构造失败的情况。
- 业务专属的幂等规则由处理器判定:例如 `resulted` 的「同任务同结果版本」登记返回 `Duplicate/Conflict`;通用层的摘要比较只管同身份重试。
- **结果版本登记的并发(第四版)**:同任务的回调在 `transport_task` 行锁下串行,版本登记读写 `outcome_digests`,不需要唯一键抢占:同版本相同规范化 `data` 返回 `Duplicate`,不同返回 `Conflict`。当前消息收据仍由通用层在外层接收事务中保存首次 `200/409`。第三版基于 `transport_evidence` 唯一键的做法(D7 / 6A)已取消。
- 处理器抛出的任何意外异常由通用层兜底为合同形状的 `503`。
- **先占身份再执行有副作用的处理器**(并发同身份只写一份的实现方式):通用层在事务内先 `INSERT callback_receipt ... ON CONFLICT DO NOTHING RETURNING`(沿用 `Outbox.enqueue` 的写法)占住 `(operation, operation_id)`;没拿到则重读已有收据并按 §5 比较;拿到才执行 `handle`,事务末尾再把首次响应写回收据。纯 `parse` 可用于有效消息比较,但不得写数据库或触发外部副作用。PostgreSQL 下并发的第二个请求会在唯一索引上等第一个提交,随后读到已有收据。不使用「先查后插」替代原子占位。
- **注册沿用 `@dev_scenario` 的做法**:重复注册同名 operation 报错并同时指出两个处理器;注册表是模块级全局状态,测试用 `monkeypatch` 替换成空表隔离(参照 `tests/core/test_dev_scenarios.py`)。

## 4. 数据模型

### 4.1 通用表(第 1 步)

| 表 | 关键列 | 约束/用途 |
|---|---|---|
| `callback_receipt` | `operation`、`operation_id`、`raw_digest`、`effective_digest?`、`compare_mode`(RAW/EFFECTIVE)、首次响应 `status_code`+`body`+`resp_timestamp`、`created_at` | `UNIQUE(operation, operation_id)`;精确原信封优先重放;未知 operation 或非法消息的拒绝使用 RAW,结构合法且已知 operation 使用 EFFECTIVE;**不含任何业务列**。第 1 步即建齐两种摘要元数据,第 2 步启用纯 parse 有效字段比较(D3 / 2A);第 3 步的迁移追加可空 JSON 列 `diagnostic`(处理器附加的诊断,见 §6.1) |
| `callback_snapshot` | `request_id`、`received_at`、`body_b64`(≤64 KiB)、`truncated`、`http_status`、`response_body`、`outcome` | 仅诊断,无唯一约束,不参与幂等 |

`operation` 长度上限为 256 个字符(code point),`callback_receipt.operation` 为 `VARCHAR(256)`,物理唯一约束直接为 `UNIQUE(operation, operation_id)`(最长约 1 KiB + 36 字节,远低于 PostgreSQL B-tree 索引项限制),不再需要 `operation_digest` 与摘要碰撞核对。超过上限的 `operation` 按 §5 第 2 条处理,不建收据。

### 4.2 Transport 专属(第 3 步)

不新增表,只给 `transport_task` 增加列(一个迁移):

| 列 | 内容 |
|---|---|
| `frozen`(JSON) | 登记任务时存下冻结的 `data`(成员、目标、`target_face`、`rack_id`);由 `TransportService._register` 写入,用于校验回调事实,不依赖出站消息保留期。**与 Transport operation 注册同一步上线**(此前回调只会得到 `422`,不存在「已 ACK 却无冻结信息」的窗口);迁移时对 `frozen` 为空的现存任务,按 `business_key = transport_task_id` 取 `reliable_message.request_raw`,解析出 `data`(去掉 `transport_task_id`)回填;消息已被保留期清理的保持为空,回调到达时按「冻结信息缺失」记录诊断并返回 `202` |
| `member_positions`(JSON) | `{container_id: {"SOURCE_PICKED": true, "TARGET_PLACED": <final_position>}}`,任务内每个料箱成员已到达的位置事实(集合,与到达先后无关;`POSITION_UNKNOWN` 不存) |
| `outcome_status` | `NULL` 或 `SUCCEEDED/FAILED/CANCELLED/UNKNOWN`;已应用的权威结果 |
| `applied_outcome`(JSON)、`applied_outcome_revision` | 已应用版本的**完整规范化结果**:`status`、`final_position` 或 `position_unknown`、补齐后的 `arrival_face`、`failure_code`、料箱的 `results[]` 逐箱状态;`outcome_status` 保留为可查询的标量。摘要是单向的,下游(重试、换面)要读的事实都在这一列 |
| `outcome_digests`(JSON) | `{revision: sha256(规范化 data)}`,用于同版本判重:相同 `Duplicate`、不同 `Conflict` |
| `pending_outcome`(JSON,可空) | 最高版本的、因前置位置缺失而暂存的料箱成功结果 `{revision, data}`;失效与完成规则见 §6.2 |

**JSON 列约定**:SQLAlchemy 不检测 JSON 列里 dict 的原地修改,一律构造新 dict 后整体赋值(不引入 `MutableDict`);所有应用测试以「请求结束后用新会话读回任务行」断言,不读同一会话的内存对象。

不新增按货架/料箱查询的关联表:`frozen` 保存了货架、料箱和位置的完整事实,是事实来源;将来整架依赖检查(见 `TODOS.md` 的闭合语义条目)需要查询时,再由 `frozen` 回填索引或关联表,当前没有消费方,不提前建。

## 5. 请求处理(通用接收,与合同 §5.4 对齐)

1. 原始 body > 256 KiB → 空体 `413`(解析前);非法 JSON/UTF-8、任意层重复 key、NaN/Infinity、无法提取合法 `operation_id`(含非 UUIDv7)→ 空体 `400`。二者不建收据。
2. `operation` 缺失、非字符串、含 NUL、无法编码为 UTF-8 或**长度超过 256 个字符**(`operation_id` 合法)→ `422 REJECTED / INVALID_EVIDENCE`,因形成不了可存储的 `(operation, operation_id)` 身份**不建收据**,只写快照(WMS 按合同不再重试原内容)。长度上限同时封住「未认证请求用超长 `operation` 撑大不清理的收据表」这条增长路径。
3. 按 `(operation, operation_id)` 原子占位或取得既有收据。`raw_digest` 是严格解析后**完整原信封(含冗余字段)**的规范 JSON sha256,与 handler 无关;既有收据的原信封指纹相同即优先重放首次响应(`RECEIVED` 改报 `200/DUPLICATE`,其它确定响应保留原状态、时间和 data),不重新执行 handler。这样未注册时保存的 `422` 在注册上线后仍稳定重放,已保存的拒绝不会变成功。
4. 原信封指纹不同:既有收据为 RAW 时返回 `409/CONFLICT`;为 EFFECTIVE 时仅调用当前 operation 版本的纯 `parse`,以规范化有效信封 `{operation, operation_id, timestamp, data: parsed}` 的 sha256 比较 `effective_digest`。同有效摘要重放首次响应,不同有效摘要或修改后已定义字段非法返回 `409`,不执行 `handle`。若原有效收据对应的规范化器不可用而无法可靠比较,返回不持久化的 `503`,不能将既有身份重新判为未知 operation。有效字段规则在同一 operation 版本内冻结,语义不兼容变更须新版本,不再承诺任意修改同一 `@v1` 的 DTO 语义后仍能稳定比较。两个摘要均保留 `timestamp`:改变时间戳仍冲突;合法 Transport 消息仅冗余字段变化不冲突。新身份且同任务同结果版本的兼容重发仍由 handler 用规范化 `data` 做版本登记,不改变结果版本身份(D3 / 2A 已确认)。
5. 新身份的拒绝一律为 HTTP `422`,body `{operation_id, code:"REJECTED", timestamp, data:{reason_code}}`:字符串 `operation` 但未注册 → `reason_code=UNSUPPORTED_OPERATION`(通用层);已注册 operation 的信封非法或纯 `parse` 判断 DTO 非法 → `INVALID_EVIDENCE`;均以 RAW 模式原子保存原信封指纹与首次拒绝响应。结构合法的已知 operation 保存原信封指纹与有效摘要,以 EFFECTIVE 模式记录 handler 的首次确定响应。信封校验不再拒绝顶层冗余字段。
6. 合法:调用处理器 `handle`,与收据同事务提交;按 `Outcome` 返回 `202/200/409/422`;提交失败 `503/UNAVAILABLE`(不冻结为幂等响应)。
7. **意外异常兜底**:路由对通用接收整体 `try/except Exception`(不吞 `CancelledError`):已取得合法 `operation_id` 返回 `503 {operation_id, code:UNAVAILABLE, timestamp, data:{}}`,否则空体 `503`;记结构化错误日志并写快照(`outcome=error`)。

响应只用合同允许的 `202/200/409/422/503` 与空体 `400/413`;无 `429`、无 `Retry-After`、本阶段无 `401`。

## 6. Transport 回调的应用(请求事务内,第 3 步)

### 6.1 通用机制

- 处理器在通用层打开的保存点内执行:先 `SELECT transport_task ... FOR UPDATE`(按 `transport_task_id`;SQLite 下按事务串行),再按下面规则更新任务行;同任务的回调因此串行,互不覆盖。
- **应用逻辑对已通过 DTO 校验的消息不得抛异常**:未知任务、冻结信息缺失、事实与冻结任务矛盾、与已确定终态矛盾,一律输出结构化告警日志(任务号、operation、operation_id、原因)并返回 `Accepted`(对端仍得 `202`,不要求重发);原文靠诊断快照保留 7 天。只有数据库、序列化等真异常才走通用 `503`。
- **ACK 必须回显 `transport_task_id`**(合同 §5.1 第 506 行:带任务 ID 的 ACK 回显冻结消息中的任务 ID,否则 WMS 不结束发送义务)。Transport 两个 operation 的**所有** `Accepted/Duplicate/Conflict` 响应,包括只告警的情形(未知任务、冻结缺失、事实矛盾、终态矛盾),`data` 都带 `{"transport_task_id": <本条消息解析出的任务 ID>}`;处理器内统一用一个小函数构造,避免遗漏。
- **只告警的事实要有持久记录**:未知任务、冻结缺失、事实矛盾、终态矛盾、结果不完整这几种「返回 `202` 但不应用」的情形,处理器通过 `ctx` 附上诊断(原因码 + 规范化 `data`),通用层与收据同事务写入 `callback_receipt.diagnostic`(可空 JSON 列,随第 3 步的迁移新增;受请求体与 DTO 大小约束,不另做 64 KiB 截断)。提交成功后输出 `callback diagnostic` 结构化告警,含 operation、operation_id 与诊断中的原因、任务号,不重复输出完整 `data`;事务回滚不输出已接纳诊断告警。快照有每进程每分钟上限,不能作为唯一记录。数据库、序列化等**意外异常**仍走通用 `503`(对端原样重发,不丢事实,修复后自愈),不吞成 `202`。
- **不看出站消息状态做应用判断**:只要任务存在就应用(权威结果是物理事实,出站消息可能还在 `PENDING/SENDING/STALLED`);反过来,收到回调会结束该任务提交消息的发送(§6.4)。
- 校验冻结事实(`rack_id`/`container_id` 属冻结对象、目标匹配)读 `transport_task.frozen`。

- **身份冲突的 `409` 也要回显**:同一 `operation + operation_id` 内容被改时,通用层直接返回 `409`,不进入处理器。`CallbackOperation` 增加类属性 `echo_keys`(Transport 两个 operation 为 `("transport_task_id",)`),通用层从**已保存的首次响应** `data` 中只取 `echo_keys` 里的键;首次没有(如首次任务编号非法)则为 `{}`。测试覆盖:同身份把任务 A 改成 B、首次被拒绝但任务编号合法、首次任务编号非法三种。
- **锁等待有边界**:PostgreSQL 下回调事务开头 `SET LOCAL lock_timeout = '5s'`(常量,不做配置);拿不到任务行或消息行的锁就整体回滚并返回合同形状的 `503`(WMS 用原身份重发)。锁顺序固定为「收据唯一键 → 任务行 → `reliable_message` 行」,应用时的对账只处理当前任务。SQLite 不适用。
- **未知任务的有意偏离**:任务不存在时没有任务行可锁,也没有版本登记处,因此对未知任务不保证「新身份同版本」重发的 `200/409`,一律 `202` 并写诊断。任务编号由我们生成并下发,WMS 只会回调下发过的任务,这个情形只会出现在环境错配或测试数据里;为它恢复一张版本登记表与「合同可优化、不为极端情形增加机制」的原则相反。

**搬运层不阻塞、先后依赖交给调用方(设计原则)**:下发前不查询 `transport_task` 窗口,也不裁决跨任务占用(合同第 29、447、804 行已如此约定,`TransportService` 现状一致),能否下发由 WMS/RCS 在接纳时裁决。依据:无阻塞设计避免了「我方状态误挡后续任务」这一类事故。但「WMS 接纳」不等于「顺序正确」——现场出现过回架未完成就旋转同架、放箱未完成就离场,WMS 均接纳并执行(`wes_backend/tests/mock/BAD_CASES.md`),因为它不知道业务编排意图。所以:(1) 搬运层保持不预查、不阻塞;(2) 同一货架上是否等前序任务闭合,由调用方按场景决定,依据是任务的闭合状态(见 `TODOS.md` 闭合语义条目);(3) 下发的「失败」分两种:明确被拒(`409/422`)记录原因并算**闭合**,调用方可用新 `client_request_id` 重试;结果不明(超时/无响应)不能当作未下发,保留原身份继续重试(不计入未送达预算,不会进入 `STALLED`)或等权威结果。被拒的任务必须算闭合,否则会变成又一个永久未结束的状态。

### 6.2 `resulted`

- **版本登记**:`outcome_digests[revision]` 已存在:规范化 `data` 摘要相同返回 `Duplicate`(`200`),不同返回 `Conflict`(`409`);否则记录该版本摘要。
- **版本与终态**:`outcome_revision` 低于 `applied_outcome_revision` 的新版本只登记摘要、返回 `202`,不回退;`SUCCEEDED/FAILED/CANCELLED` 已确定后任何后续结果(即使版本更高)告警并返回 `202`、不改写;`UNKNOWN` 可被更高版本收敛。
- 货架族 `SUCCEEDED` 缺少到达面时,用冻结的 `target_face` 补齐应用结果(合同 §5.3)。
- **料箱族成功回架**(冻结目标为 `RACK_BIN_SLOT`):**每个成功回架的成员**所需的 `TARGET_PLACED` 均已应用才完成任务;否则存入 `pending_outcome` 并返回 `202`。结果必须完整且仅覆盖冻结成员,否则写诊断并返回 `202`、不应用。
- **`pending_outcome` 的失效与完成规则**:(1) 只保留最高版本,更高版本的结果到达直接替换;(2) 任何版本被应用后(确定终态或 `UNKNOWN`),若 `pending_outcome` 的版本不高于 `applied_outcome_revision` 就清除并写诊断;(3) 位置到齐后完成 `pending_outcome` 时,**与首次应用走同一个应用函数**,版本(须高于已应用版本)、终态规则、前置位置检查全部重做,不另写第二份逻辑,因此旧版本不会把更新的 `UNKNOWN` 或终态覆盖回去。
- **目标匹配判定表**:

| 任务/结果 | 校验 |
|---|---|
| 货架族 `SUCCEEDED`,冻结目标为 `RACK_POSITION` | `final_position` 必须与冻结目标精确相等 |
| 货架族 `SUCCEEDED`,冻结目标为 `RACK` 或 `ZONE` | 我们没有货架位置和区域归属的主数据,接受任意 `RACK_POSITION`(信任 WMS/RCS 的解析),在文档与注释中明写 |
| 货架族 `SUCCEEDED`,冻结了 `target_face` | 回调给出非空 `arrival_face` 时必须相等,不相等写诊断不应用;缺省则用冻结 `target_face` 补齐 |
| 货架族 `FAILED` / `CANCELLED` | 终位允许不在目标,不做目标校验;仍须满足合同的字段互斥规则 |
| 料箱族 `SUCCEEDED` 成员 | `final_position` 必须等于该成员冻结的目标 |
| 料箱族 `FAILED` 成员 | 不校验目标;`position_unknown` 与 `final_position` 严格二选一 |
| 位置事件 `TARGET_PLACED` | `final_position` 等于该料箱冻结的目标 |
- 任务存在即结束其提交消息的发送(§6.4),不论本次回调是否被应用。

### 6.3 `member_position_changed`

- 校验 `container_id` 属冻结成员、`TARGET_PLACED` 的 `final_position` 等于冻结目标,通过后合并进 `member_positions`;之后检查并完成 `pending_outcome`。只维护任务内位置状态;跨任务当前位置表与 `RackStateReader` 等有调用方时再做。
- **位置事实是集合,与到达先后无关**(决定 11):每个料箱记录收到过的 `SOURCE_PICKED` 与 `TARGET_PLACED`(终位必须等于冻结目标,否则告警不记);同一事实重复到达幂等。唯一的判定依赖是「该成员已有等于冻结目标的 `TARGET_PLACED`」(料箱成功回架结果的前置条件)。`POSITION_UNKNOWN` 事件照常接纳(合同枚举不变),只做成员校验并写诊断 `POSITION_UNKNOWN_REPORTED`,不存状态;任务是否为 `UNKNOWN` 只由结果消息决定。因为不存在先后约束,也就没有需要与 WMS 确认的顺序规则。

### 6.4 回调即结束提交消息的发送(无定时对账)

`STALLED` 由可靠层产生,仅当目标配置了 `max_not_received_attempts` 且「确认未送达」累计达到预算时出现(默认不设,默认不会出现);结果未知不进入 `STALLED`。任务存在时收到任何回调,都说明对端已收到该任务的提交,继续重试没有意义。处理器在锁住任务行后,对 `reliable_message WHERE business_key = transport_task_id AND operation = 提交 OPERATION AND status IN ('PENDING','SENDING','STALLED')` 的行逐条 `SELECT ... FOR UPDATE` 锁住,再用其 `(target, operation, operation_id)` 调用基础层新增的 `Outbox.settle`(同事务提交):这些消息转为 `DONE`,不写响应(`response_raw` 为空,以区别于收到对端应答的 `DONE`),不再被领取发送;正在发送的租约随之失效,迟到的发送结果被忽略(`finish` 按租约与 `SENDING` 状态更新)。未知任务不 settle;不依赖 `RELIABLE_TARGETS`,不假设「一任务一消息」,不动同 `business_key` 的其他 operation 消息。这是对「不改基础层」约束的有意突破,理由是只有基础层能安全地按身份终止发送。风险:来源认证目前为 `NONE`,持有 `transport_task_id` 的伪造回调可以终止重试;`transport_task_id` 含 UUIDv7 随机部分,认证落地后消除。

**不做定时对账的理由**:(1) 默认配置不会产生 `STALLED`;(2) 对端不可用时盲目 `resume` 只是把预算清零后继续重发,与不设预算的持续退避等价,没有新增能力;(3) 本系统不维护货架/料箱终态,无法判断很久之前的指令现在是否仍该下发,时效由调用方的 `expires_at` 与上游决定;(4) 对端长期不可用应由告警和显式 `Outbox.resume` 处理。

### 6.5 货架取消结果与生命周期

- **本系统怎么记**:货架族 `CANCELLED`(必须带权威 `RACK_POSITION final_position`,不带失败码/到达面)按终态保存:`outcome_status=CANCELLED`,`applied_outcome.final_position` 记录实际终位;同时 `settle` 结束该任务提交消息的发送。它只终止当前执行实例,**不是失败,也不撤销业务需求**。
- **本系统不做什么**:不自动重试,不改同一 `cycle_id` 下其他步骤的状态,也不替调用方判断后续步骤是否还该下发(搬运层不预查、不阻塞,有效性交给上游)。
- **调用方怎么处理**(合同 T1):需求仍有效时,以全新 `client_request_id` 创建新任务(新 `transport_task_id`),指数退避、上限约 60 秒、次数无上限。重试仍属于同一生命周期,用保存的 `cycle_id` 重建 `RackCycle` 即可,一个 `cycle_id` 下同一步可以有多次尝试。重试前看 `final_position`:仍在目标点则视为已到位、复用,不在目标点则重新进场。
- **迟到与重复**:同版本重复按幂等处理;已是终态后再来的更高版本(含迟到的 `CANCELLED`)记诊断 `TERMINAL_CONFLICT`,不改写;`CANCELLED` 先到、之后才到的成功结果同样不改写已确定终态。成员取消通知可能比 Transport 取消晚 3~180 秒,不据此推断需求撤销。
- **缺口**:没有向调用方发布结果的出口(`outcome_version` 发布是非目标),调用方目前只能读 `transport_task`(`outcome_status`、`applied_outcome`)判断,见 `TODOS.md`。

## 7. 保留期与诊断快照

| 数据 | 配置 | 默认 | 清理 |
|---|---|---|---|
| `callback_snapshot` | 新增 `CALLBACK_SNAPSHOT_RETENTION_DAYS` | 7 | 到期即删,独立于收据;第 1 步即上线清理任务 |
| `callback_receipt` | 复用 `RELIABLE_RETENTION_DAYS` | 90 | 到期即删(`callback.cleanup_receipts`,每日 beat,按 `created_at` 索引分批)。合同要求幂等记录不自动过期,清理周期属于合同变更,须在联调前与 WMS 确认(§9)。业务幂等不依赖收据:结果版本与摘要在任务行 `outcome_digests`,位置事实是集合,所以旧身份在清理后重放,最坏只是拿不到缓存的原响应,或「同身份不同内容」不再返回 `409` 而按业务规则处理 |

- 快照清理沿用现有模式:`*_RETENTION_DAYS` 配置 + `positive()` 校验 + `run_*` 批量清理 + Celery beat。
- 快照:路由在响应后用 FastAPI 后台任务尽力写入(原始字节 Base64、≤64 KiB 并标截断、最终响应体、HTTP 状态、请求 ID、结果类别);写失败/超时只打日志,**不改响应、不返回 503**;覆盖成功、重复、冲突、非法 JSON/UTF-8、超限、处理异常;不承担幂等与恢复。
- 快照独立成表,不复用 `audit_log`。认证引入后快照须脱敏请求头(与认证 TODO 同步复核)。
- **暴露面约束**(认证 `NONE` + 限流豁免 + 每请求写快照叠加,可被任意速率写满磁盘):(1) `docs/deploy.md` 写明该路径在认证落地前必须由网关限制为 WMS 来源;(2) 快照写入加进程内每分钟条数上限(常量,不做配置),超额请求照常处理、只丢快照并输出计数告警日志;多进程下上限按进程计。

## 8. 实施步骤

| 步骤 | 内容 | 验收 |
|---|---|---|
| 1 通用接收 | 路由、信封校验、严格 JSON(提到 core)、收据幂等与稳定重放、未注册 operation 的 `422`、意外异常 `503` 兜底、`RateLimit` 豁免、快照与其保留期配置/清理/写入速率上限、`docs/deploy.md` 的部署约束说明、迁移(收据、快照) | `413/400`;`operation` 缺失 `422` 不建收据;未知 operation `422 UNSUPPORTED_OPERATION` 并稳定重放;同身份同摘要重放、异摘要(含仅 `timestamp` 不同)`409`;并发同身份只写一份;快照写失败不影响响应;超过 100 次/分钟不返回 `429` |
| 2 operation 注册 | 注册装饰器、处理器契约(`parse`/`handle`/`Outcome`)、与收据同事务、`Rejected` 回滚处理器写入、处理器异常 `503` | 用**测试里注册的测试 operation** 验证 `202/200/409/422`;`Rejected` 不留业务行;处理器异常合同形状 `503`;注册重复名报错 |
| 3 Transport 接收并应用 | 两个 operation(`callback/`,每个 operation 一个文件,`CallbackOperation` + `Payload`):DTO 校验、冻结事实校验、直接应用到 `transport_task`(位置、货架族与料箱族结果、版本登记、终态与前置规则);`transport_task` 新增列的迁移;`TransportService._register` 写 `frozen`;`callback_receipt.diagnostic` 列与 `ctx` 附加诊断;ACK 与 `409` 回显 `transport_task_id`(`echo_keys`);`applied_outcome` 保存完整规范化结果;`frozen` 从 `request_raw` 回填;回调事务 `lock_timeout`;收到回调即 settle 提交消息 | DTO 规则;未知任务/冻结缺失/事实矛盾/终态矛盾均 `202` 且告警、不抛异常;同版本相同 `data` 新身份 `200`、不同 `409`;低版本不回退;`UNKNOWN` 可收敛;料箱结果先到则暂存、位置到齐后完成;新登记任务带 `frozen`;PostgreSQL 并发:同任务回调串行、两个料箱的位置事件互不抹掉 |
| ~~4 `STALLED` 定时对账~~ | 已取消(决定 10) | — |

每步独立可验收、可合并;1、2 与业务无关,可先于任何具体 operation 完成并联调通用部分。**「可合并」不等于「可对 WMS 开放」**:第 1、2 步合并后入口上线,但此时真实 Transport 回调只会得到 `422 UNSUPPORTED_OPERATION`,WMS 按合同不再重试原内容,第 3 步上线也救不回,滚动发布期间打到旧实例同理。因此上线顺序约定(写入 `docs/deploy.md`):第 3 步在**全部接收实例与全部任务创建者(API、worker、脚本)**均已升级后,才在网关对 WMS 来源放行;在此之前联调只用测试 operation,不投真实 Transport 回调;滚动发布期间网关暂停来源。第 1 步另含两处对既有代码的小改动:`RateLimit` 增加 `limit is None` 豁免,`adapter.py` 的严格 JSON 解析提到 `app/core/strict_json.py`;第 3 步含 `TransportService._register` 写入 `frozen`。

第 3 步已实现(`app/integrations/wms_transport/callback/`、`callback/payloads.py`、`callback/apply.py`、`settle.py`):`Payload` 模型承担消息自身的合法性(字段互斥、枚举、长度),`TaskCallbacks` 承担与冻结任务的对照和应用;位置事实按集合累积,`outcome_status` 对货架 `FAILED+position_unknown` 与料箱任一成员位置未知记为 `UNKNOWN`。`STALLED` 定时对账已取消,任务收到回调即 `settle` 提交消息(决定 10)。

### 8.1 测试计划

测试按 AGENTS.md:只固化长期合同,通过公开入口断言,不测私有函数,不为凑覆盖率写测试。

**1 通用接收**
- `tests/controllers/test_wms_events.py`:按合同条款参数化,只走 HTTP 入口——`413`;`400`(非法 JSON/UTF-8、任意层重复 key、NaN/Infinity、`operation_id` 缺失或非 UUIDv7);`operation` 缺失、非字符串或超过 256 个字符→`422` 且不建收据,恰好 256 个字符可建收据并稳定重放;未注册 operation→`422 REJECTED / UNSUPPORTED_OPERATION` 完整响应体并稳定重放;同身份同摘要重放、同身份不同摘要(含仅 `timestamp` 不同)`409`;提交失败 `503` 且不冻结为幂等响应;意外异常仍返回合同形状的 `503`;超过 100 次/分钟也不返回 `429` 且不访问 Redis;快照写入、超 64 KiB 截断、写失败不改变响应、超过每分钟上限只丢快照且响应不变。
- PostgreSQL 集成(需 `TEST_DATABASE_URL`):并发同身份只写一份收据(先占身份再调用处理器);赢家回滚后竞争者接管;提交后丢响应再重试;任何已提交收据都有可重放响应。
- `tests/migrations`:新迁移升降级。
- 回归(既有行为被改动,必须保留):`tests/core/test_ratelimit.py`、`tests/integrations/wms_transport/test_adapter.py` 原用例不变且通过;`RateLimit(100)` 仍照常返回 `429`。

**2 operation 注册**:每个测试用 `monkeypatch` 隔离注册表;测试里注册的测试 operation 覆盖 `Accepted/Duplicate/Conflict/Rejected`;`Rejected` 显式回滚处理器写入(不留业务行);响应构造失败时回滚整个接收事务;通用层拒绝必需顶层字段缺失与非法 `timestamp`,不因顶层冗余字段拒绝。经公开 HTTP 入口验证:精确原信封优先重放;合法消息只改变顶层或业务各层冗余字段仍重复,有效字段或 timestamp 变化冲突;未知 operation 的原 `422` 在注册后仍重放,已保存拒绝不会变成功;已接纳身份无法取得对应规范化器且原指纹不同时 `503`,不重新判为未知 operation;有效字段比较只运行纯 `parse`,不执行有副作用的 `handle`。处理器异常合同形状 `503`;新身份 `parse` 非法→`422 INVALID_EVIDENCE`;重复注册报错(D3 / 2A 已确认)。

**3 Transport 接收并应用**(全部通过实际 HTTP 入口,SQLite 与显式独立 PostgreSQL 均覆盖):
- 两个 operation 的 DTO 规则(货架族/料箱族 `resulted`、位置事件);顶层及业务嵌套冗余字段忽略,已定义字段严格校验;同身份只改变有效字段或 `timestamp` 才冲突。
- 应用:位置事实集合(重复幂等、先后无关、`TARGET_PLACED` 终位等于冻结目标、`POSITION_UNKNOWN` 只留诊断);结果版本与终态规则;缺少到达面时用冻结 `target_face` 补齐;料箱结果先到则暂存、位置到齐后在同一请求事务内完成,成员缺失/多余不应用。
- 不抛异常:未知任务、冻结信息缺失、事实矛盾、终态矛盾均返回 `202`,任务行不变,并有告警日志;应用逻辑在这些情形下不产生 `503`。
- 判重:同版本相同 `data` 新身份 `200 DUPLICATE`、不同 `data` `409`,不改任务行;PostgreSQL 并发:同任务同版本两个不同消息身份只有一个 `202`;同任务两个料箱的位置事件并发不互相抹掉。
- 结果先于出站接纳也能应用;收到任一回调后该任务的提交消息(`PENDING/SENDING/STALLED`)转为 `DONE`,其他 operation 与其他任务的消息不受影响,未知任务不 settle。
- ACK 回显:`202/200/409` 的 `data` 均含 `transport_task_id`,包括未知任务与只告警的情形。
- 只告警的事实有持久记录:未知任务、事实矛盾、终态矛盾、结果不完整时 `callback_receipt.diagnostic` 含原因码与规范化 `data`,即使诊断快照被限速丢弃也在;意外数据库异常仍 `503` 且不落收据。
- 迟到与隔离:出站消息 `STALLED`/超时之后到达的回调仍自动应用,重复到达不重复;一个任务的异常回调(未知任务、矛盾)不影响同时进来的其他任务;结果先于位置事件到达只暂存该任务,不影响其他任务。
- JSON 列:所有应用结果以请求后新会话读回任务行断言(防原地修改未落库)。
- `409` 回显:同身份把任务 A 改成 B、首次被拒绝但任务编号合法、首次任务编号非法,三种情形的 `data` 分别为首次的任务编号、首次的任务编号、`{}`。
- `pending_outcome` 交错:v1 暂存后 v2 应用为 `UNKNOWN`,位置到齐不得重新应用 v1;v3 暂存后迟到的 v2 得到确定终态,v3 被清除或按规则重新评估;部分成功的料箱结果逐成员检查前置位置。
- 目标判定表逐格:`RACK_POSITION` 精确相等;`RACK`/`ZONE` 目标的成功结果被接受;面向不一致写诊断不应用;`FAILED/CANCELLED` 终位偏离目标仍被应用。
- 完整结果落库:`applied_outcome` 含 `arrival_face`、`failure_code`、逐箱状态;未知任务、矛盾、不完整结果的诊断含原因码与规范化 `data`。
- 迁移回填:现存任务的 `frozen` 从 `request_raw` 回填,消息已清理的保持为空;revision 取 Int64 边界值。
- 锁超时:PostgreSQL 下任务行被另一事务长期占用时回调在预算内返回合同形状的 `503`,且不留收据与业务变更;「任务已更新、收据提交前失败」时任务、诊断、版本登记与 `settle` 全部回滚。
- 回归:`test_service.py` 原用例通过,且登记任务时 `frozen` 等于冻结的 `data`;迁移升降级,新列与 SQLite `create_all` 一致。

**4 `STALLED` 定时对账**:已取消(决定 10);「回调即结束发送」由第 3 步测试覆盖。

## 9. 待与对端确认(联调前)

- 回调认证方案:联调完成后再处理(合同现为 `NONE`,见 `TODOS.md`「低优先级」)。
- 去重保留(合同变更):合同要求幂等记录不自动过期;我方将收据保留期对齐 `RELIABLE_RETENTION_DAYS`(90 天),需 WMS 确认同一消息身份最长重发时间不超过 1 小时(暂定,远小于该期限),并同意把清理周期写入合同(合同要求先改文档)。

## 10. 明确不做

证据表与后台应用(第四版取消)、认证实现、`outcome_version` 发布、跨任务位置表与 `RackStateReader`、业务重试/窗口/换面、非 Transport operation、通用事件总线、除 `Outbox.settle` 以外的可靠层扩展。控制台回调投递与收据清理已实现,不再列为暂缓项。

## 11. What already exists(复用与新增)

| 能力 | 现有位置 | 本文做法 |
|---|---|---|
| 严格 JSON 解析 | `adapter.py` 私有函数 | 提到 `app/core/strict_json.py`,出站与回调共用 |
| 注册装饰器模式 | `core/dev_scenarios.py` 的 `@dev_scenario` | operation 注册沿用同风格 |
| UUIDv7 校验 | `app/core/identifiers.py` | 直接复用 |
| `validate_text/validate_face/require_uuid7`、位置类型 | `wms_transport/types.py` | Transport DTO 校验复用 |
| 「`on_conflict_do_nothing` + 重读」幂等写入 | `Outbox.enqueue` | 收据写入沿用,不用先查后插 |
| 保留期配置 + `run_*` 批量清理 + beat | `audit_tasks`、`reliable_tasks` | 快照清理沿用 |
| 结束提交消息的发送 | 基础层新增 `Outbox.settle`;`STALLED` 另有 `Outbox.resume` | 回调证据触发 settle,运维显式 resume |
| 响应后旁路写入的思路 | `AuditMiddleware` | 参考,不复用 |
| 限流 | `RateLimit` | 增加 `limit is None` 豁免 |

## 12. 数据流与失败模式

```text
WMS ──POST /api/v1/wms/events──▶ 路由(有界读 ≤256KiB;RateLimit(None))
                                 │ 413/400/operation 缺失 ─▶ 空体或 422(不建收据,只写快照)
                                 ▼
                           通用层:严格解析 → 必需信封字段校验 → 原信封指纹
                                 │ 事务内先占收据(ON CONFLICT DO NOTHING)
                                 │ 已存在:同原指纹→重放;否则 RAW→409 / EFFECTIVE→纯 parse 后比较
                                 ▼ 占到
                           纯 parse 规范化 → 有效摘要;保存点内执行 handle → Outcome
                                 │ Accepted/Duplicate/Conflict 释放保存点;Rejected 显式回滚
                                 ▼ 先构造并校验响应 → 写回收据 → 提交
                                 │ 提交失败/意外异常 → 整体回滚 → 503
                                 ▼ 202/200/409/422(响应后写快照,受速率上限)
   (第四版)handle 内:锁 transport_task 行 → 应用位置/结果规则(冲突只告警、返回 202)
                                 ▼
                       settle 提交消息:任何回调到达即执行(无定时)
```

| 新路径 | 现实失败 | 测试 | 错误处理 | 对端/运维所见 |
|---|---|---|---|---|
| 路由读 body | 超 256 KiB | 有 | 空体 `413` | 明确 |
| 占收据 | 并发同身份 | 有(PG) | 唯一约束,输家重读 | 一份收据 |
| 处理器 | `Rejected` / 意外异常 | 有 | 显式回滚保存点 / 整体回滚 + `503` | 明确 |
| 响应构造 | 序列化失败 | 有 | 提交前校验,失败整体回滚 | `503`,WMS 重试 |
| 快照 | 写失败 / 超速率 | 有 | 只打日志与计数,响应不变 | 日志可见(按设计) |
| 上线顺序 | 真实回调早于第 3 步 | 无(运维约束) | 网关控制来源 | 部署文档约定 |
| 应用 | 未知任务 / 缺冻结 / 事实矛盾 | 有 | 告警日志并返回 `202`,不抛异常(第四版) | 日志 |
| 应用 | 同任务并发回调 | 有(PG) | 锁任务行串行 | 不互相覆盖 |
| 结束发送 | 回调到达而提交消息仍在重试或 `STALLED` | 有 | 锁消息行后 `settle`,迟到的发送结果按租约忽略 | 日志 |

本表的「有」表示存在相关测试,不表示全部场景已运行通过;PostgreSQL 并发与真实联调边界见 §18。「上线顺序」靠运维约束而非代码防呆,是已确认的取舍。

## 13. 并行实施策略

按依赖串行:通用接收 → operation 注册 → Transport 接收并应用,共享 `app/models/`、`app/integrations/`、`app/tasks/`、`migrations/`,无并行收益。第 1 步内部 `RateLimit` 豁免与严格 JSON 提取互不依赖、可并行。

## 14. 评审决定记录(2026-10-22 第二轮)

> 注:本节及以下 §15、§16 与末尾评审报告针对第三版方案(证据表 + 后台应用),第四版取消了该部分,见 §0,需重新评审。

本文经 `/plan-eng-review` 重新评审。范围:按你指定的「通用接收 → 注册 → 具体 operation」6 步推进(范围问题被你中断,按你的分层指示视为已认可)。本轮已采纳的修订:部署约束加快照写入速率上限、摘要只用原始信封(与处理器无关)、通用层校验完整信封并定义 `ctx`、事务顺序与四种 `Outcome` 规则(先构造响应再提交)、锁顺序「证据行 → 任务行」、领取按任务 kind 过滤并区分三种情况、上线顺序约定。外部审阅(Codex)7 条意见全部核实后采纳。

### 14.1 本次复核(第三轮,已完成)

- D1 A:保留既有六步完整评审,不授权实施。
- D2 / 1A:补全以证据为主的领取资格,包含未知任务与本步已支持组合;已知但未支持组合保持 `PENDING`,未知任务进入既有 `CONFLICT` 告警路径。公开后台入口在 SQLite/PostgreSQL 验证三类结果,不增加存储或恢复机制。
- D3 / 2A:以具体 Transport 合同 §5.4 的有效字段口径为准,替代此前“所有消息仅按原信封摘要比较”的决定;收据记录原信封指纹、有效摘要与比对模式,精确原信封优先重放,未知或非法消息的拒绝保持 RAW 模式。规范化固定在 operation 版本内;补齐仅冗余字段变化、有效字段与 timestamp 变化、跨注册拒绝重放等公开 HTTP 测试。本轮不修改外部合同,不授权实现。
- D4 / 3A:在步骤 4/6 明确实际 HTTP 路由、真实 Transport 注册处理器、真实收据/证据事务和公开后台扫描入口的贯通验收,SQLite/PostgreSQL 均运行;不新增步骤或运行时机制,不提前要求步骤 1/2 支持 Transport,不授权实现或测试代码编写。
- D5 / 4A:满批续扫每次最多 10 批,本轮不重复领取已访问证据;达到预算由下一次既有 beat 接续,不增加调度器或 Redis 锁。公开扫描入口验证持续满批返回、下次推进、等待证据保持 `PENDING` 和类别配额,不把批数上限当耗时 SLA。
- D6 / 5A:步骤 3 的模型与迁移明确待处理索引,SQLite/PostgreSQL 均核验;步骤 4 用实际领取 SQL 和历史数据检查查询计划。最终形状由查询决定,不提前锁死索引形状或引入通用队列。
- D7 / 6A:版本唯一键 `ON CONFLICT DO NOTHING RETURNING` 的败者重读赢家,按规范化 `data` 当次返回 `200/409`,保留当前消息收据;PostgreSQL 验证两个身份、一份证据、两个可重放收据以及赢家回滚后接管。正常竞争不依赖额外 `503` 重试,真正数据库故障仍按原 `503` 合同处理。
- 六项修正均已获得各自选择,无未决方案;设计可实施不等于运行验收已通过。当前代码核验基线为 `8a90e2b`:出站已采用 `RackCycle` 与显式位置参数,不再假设已删除的 `RackStateReader` 或应用共享门面装配文件仍存在。
- 实施经验继续沿用:基础层与业务层解耦;同调用方事务的公开入口证据不能由参数替身替代;SQLite 保存点行为与 PostgreSQL 并发证据分别验证;模型 `create_all` 与 Alembic 迁移双路径;只提交授权文件,真实联调、提交、推送、部署分别授权。本轮仅修改本文工作副本,不提交、不推送、不实现。

## 15. Implementation Tasks(本轮补强)

> 第四版对照:T1 仍适用;T2(待处理索引)、T4(未知任务领取资格)、T5(扫描预算)随证据表与后台扫描一并取消;T3(版本登记并发)改为 `transport_task.outcome_digests` 加行锁(§6.2);T6(贯通验收)改为实际 HTTP 入口直接应用到任务行的验收,没有后台扫描环节(§8.1 的 3、4)。以下原文为第三版记录。

仅从六项已确认发现派生,不取代 §8 的六步顺序,也不新增第七步。任务编号是本轮补强清单编号,不是执行顺序;步骤 1/2 仍可用测试 operation 独立验收,不提前依赖 Transport 处理器。共享文件与迁移继续串行推进,不创建额外并行工作区。

- [ ] **T1 (P1, human: ~2–4h / CC: ~15–30min)** — 通用接收 — 明确原信封重放与有效字段比较。
  - Surfaced by: Architecture R2 / D3 2A,具体合同 §5.4 的有效字段口径与旧单摘要规则冲突。
  - Files: `app/integrations/wms_callback/`、`app/models/callback.py`、`migrations/versions/`、`tests/controllers/test_wms_events.py`。
  - Verify: 步骤 1 建齐收据元数据,步骤 2 用测试 operation 验两种比对模式、跨注册拒绝重放及有效字段/timestamp 冲突,步骤 3 在实际 Transport operation 验证相同规则;只比较时不执行 `handle`。
- [ ] **T2 (P2, human: ~1–2h / CC: ~10–15min)** — 证据存储 — 在步骤 3 声明待处理访问索引。
  - Surfaced by: Performance R5 / D6 5A,外部意见经原生复核和合同第 736 行校验。
  - Files: `app/models/transport.py`、`migrations/versions/`、`tests/migrations/`、`tests/integrations/wms_transport/`。
  - Verify: PostgreSQL 升降级与 SQLite `create_all` 索引一致;步骤 4 用实际领取 SQL 核对历史数据下查询计划,不固定私有 SQL 形状。
- [ ] **T3 (P2, human: ~1–2h / CC: ~10–15min)** — 版本登记 — 在步骤 3 落定正常并发败者响应。
  - Surfaced by: Code Quality / concurrency R6 / D7 6A,唯一约束不等于败者的收据事务已定义。
  - Files: `app/integrations/wms_transport/callback/`、`tests/integrations/wms_transport/`。
  - Verify: PostgreSQL 实际 HTTP 并发得到 `202+200` 或 `202+409`,一份证据、两个可重放收据;赢家回滚后竞争者可接管;真正数据库失败仍 `503`。
- [ ] **T4 (P1, human: ~30–60min / CC: ~5–10min)** — 证据应用 — 在步骤 4 补全未知任务领取资格。
  - Surfaced by: Architecture R1 / D2 1A,普通 JOIN 无法到达未知任务分类路径。
  - Files: `app/integrations/wms_transport/callback/`、`app/tasks/callback_tasks.py`、`tests/integrations/wms_transport/`。
  - Verify: 公开扫描入口同时证明未知任务 `CONFLICT` 并告警、支持任务推进、已知但未支持的证据仍 `PENDING`;SQLite/PostgreSQL 均覆盖。
- [ ] **T5 (P2, human: ~1–2h / CC: ~10–15min)** — 扫描预算 — 在步骤 4 限制每次最多 10 批且本轮不重复领取。
  - Surfaced by: Performance R4 / D5 4A,持续满批或等待租约反复到期可能形成长任务。
  - Files: `app/integrations/wms_transport/callback/`、`app/tasks/callback_tasks.py`、`tests/integrations/wms_transport/`。
  - Verify: 持续满批按预算返回,下一次可继续,本轮不重领、等待证据仍 `PENDING`,类别配额仍有效;不增加调度器或 Redis 锁。
- [ ] **T6 (P2, human: ~2–3h / CC: ~15–25min)** — 贯通验收 — 在步骤 4/6 覆盖真实接收至应用的事务链。
  - Surfaced by: Test Review R3 / D4 3A,分段测试不能替代实际注册与接线证据。
  - Files: `tests/integrations/wms_transport/`、必要共享夹具仅 `conftest.py` 或 `tests/helpers.py`。
  - Verify: §8.1 两条实际 HTTP → 收据/证据持久化 → 公开后台扫描 → 新会话结果核验流程,含回架前置乱序及重放,SQLite/PostgreSQL 均运行。

## 16. 测试覆盖与复核边界

下图全部为**实施时要求,尚未运行**。本轮只有静态源码/合同核对和内存 SQL 路径验证,没有回调实现可供端到端验收。测试框架沿用 pytest、pytest-asyncio、HTTPX 和既有显式独立数据库 fixture;没有 LLM/prompt 变更或 eval 要求。

```text
HTTP(要求) ─ 原始 body/严格 JSON ─ 信封与身份
  ├─ 400/413/operation 缺失 -> 不建收据,只作有限诊断
  └─ 原子占收据 -> 原指纹重放 / EFFECTIVE 纯 parse 比较 / 409
       └─ 新身份 pure parse -> 保存点 handle -> 校验响应 -> 同事务提交
            ├─ Reject -> 回滚业务保存点,保存拒绝收据
            └─ 意外/提交失败 -> 整体回滚,不冻结503
步骤3(要求) ─ 结果版本唯一键 -> 赢家202 / 败者200或409 -> 各自收据稳定重放
后台(要求) ─ 证据资格/索引 -> 类别配额/10批预算/租约 -> 先锁证据再锁任务
  ├─ 未知任务 -> CONFLICT + 告警;未支持 -> PENDING
  ├─ 货架 -> 冻结事实/版本/终态 -> 任务与证据同事务
  └─ 料箱 -> 等待已应用位置 -> 任务与证据同事务
[→集成] 实际HTTP货架结果 -> 接纳后新会话 -> 后台 -> 新会话确认
[→集成] 实际HTTP料箱结果先到 -> 位置后到 -> 后台按前置完成 -> 重放无重复
独立旁路 ─ 尽力快照;回调即settle提交消息,不证明物理完成
```

回归边界:严格 JSON 提取后现有出站适配器用例不变;`RateLimit(100)` 仍限流,只豁免回调路由;增加 `frozen` 后现有出站公开入口提交/回滚/重放与失败隔离仍通过。SQLite 无法替代 PostgreSQL 的并发等待、行锁和赢家回滚证据。当前所有测试条目是计划,不是通过率声明。

### 独立复核与校准

- 外部通道:host=codex,provider=claude-code,运行时报告模型 `glm-5.3`。一次调用传输成功并返回五条意见,但缺少要求的 `Recommendation: ... because ...` 完成标记,校验结果为 `outside_status=unavailable`;不将 CLI 名称当作模型身份,不声称本轮完整跨模型覆盖通过。输入为前 30,000 字节设计及已核验上下文,明确标注截断。
- 按技能回退为一次只读原生副代理复核,不计作外部覆盖。原生复核核对实际合同及当前代码,确认 R5/R6 两处补强,分别已由 D6 5A、D7 6A 决定并写入。没有自动采纳外部意见或重新派发复审。
- 原生主审覆盖 Architecture、Code Quality、Tests、Performance 四节;本轮共六项已决定补强,未保留未决方案。代码组织不要求额外抽象或通用队列,并发败者直接复用当前出站原子插入模式。

### Suppressed findings

- 外部 F1 关于 `Outbox.resume` 必须按 target 组装 HTTP/凭证的前提不成立:`ReliableMessage.target` 实际存在,`resume(session, target, operation, operation_id)` 仅更新数据库,不读取 targets/adapters 或创建 HTTP 客户端。不能据此增加目标解析或通信依赖。
- 外部 F2 的“必然每次顺序扫描”过强;保留经合同与源码核验的 R5 索引计划缺口,不把未运行的查询计划当实测性能。
- 外部 F4 不作为独立阻塞项:现有网关限制覆盖整条回调路径,不只保护快照。收据/证据增长仍是 §7 及已有 TODO 的残余容量风险,快照限速不代表其增长受限;本轮不增鉴权、通用限流或清理窗口。
- 外部 F5 属于 canonical JSON 的实施注意事项,并非已观察到使用默认 `json.dumps` 的代码缺陷。实施时应以键重排/空白变化不改变消息等价性的公开行为证明规范化语义,不升级成通用数值规范化引擎。
- 不重开已确认的认证 NONE、先接纳后应用、无 outcome 发布/跨任务位置投影、未知任务分类策略和网关控制开放顺序。协议接纳、任务应用和真实物理事实仍分开验收。

## 17. 第四版评审记录(2026-10-23)

`/plan-eng-review` 重新评审第四版(范围:剩余第 3、4 步,不触发复杂度警示)。原生评审 3 项:ACK 回显 `transport_task_id`(合同第 506 行)、只告警事实的持久诊断、JSON 列原地修改与对账间隔。外部审阅(Codex)9 条,核实后:第 2、3、5、8、9 条成立并采纳;第 1 条记录为有意偏离(未知任务不保证「新身份同版本」的 200/409);第 4 条改文案(承认低频持续重试)并限定 Transport 提交消息;第 6 条加 `lock_timeout`;第 7 条采纳完整结果落库(诊断大小由 DTO 限定,64 KiB 截断那半条不成立)。12 项均已逐项确认并写入 §0、§4、§6、§8、§8.1。

## GSTACK REVIEW REPORT

> 以下为 §17 的历史设计评审报告;当前代码评审与验收进度见 §18,不能用设计放行代替实现验收。

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| Eng Review | plan-eng-review,第四版 | Architecture / Code Quality / Tests / Performance | 本轮1;累计4 | clean(设计决定) | 12 项(原生 3 + 外部 9)均单独批准并写入;0 未决 |
| Outside Review | Codex CLI,host=claude | 独立意见 | 本轮1 | completed | 9 条,全部核实后处理(7 采纳、1 有意偏离、1 部分采纳) |
| CEO / Design / DX | 本轮未调用 | 无产品方向或 UI 变更 | 0 | 不适用 | 不新增前置评审 |

- **OUTSIDE COVERAGE:** codex,plan-review,completed,本轮 9 条。
- **VERDICT:** 第四版 ENG CLEARED FOR IMPLEMENTATION(第 3、4 步)。仅说明设计可实施,不证明回调代码、测试、WMS 合同同步或物理执行已完成,不授权实现、提交、推送或部署。位置事件先后规则仍是本方约定,联调前须与 WMS 确认。

NO UNRESOLVED DECISIONS

## 18. 首轮代码评审与实施进度同步(2026-10-23,历史记录)

以下保留首轮权限受限时的结果;PostgreSQL、完整套件与 Mock 联调的最新结论以 §19 为准。

核验基线:`feature/transport-callback-receiver`,HEAD `ea6a4c9`;工作区起始干净。GitHub PR 查询与远程刷新因本机代理连接被沙箱拒绝而失败;按本仓库 Git Flow 以本地 `origin/develop` 为集成基线,merge base 为 `8a90e2b`。本节记录代码与本地测试证据,不表示已合并、部署、开放真实回调或完成物理验收。

Scope Check: CLEAN。主线为通用接收 → operation 注册 → Transport 请求事务内应用;控制台、settle、时效、收据清理、cycle_id 与卡住告警已有 §0 的决定记录。历史第三版证据表与后台扫描已取消,不再作为缺失要求。

| 项目 | 当前状态 | 证据与边界 |
|---|---|---|
| 1 通用接收 | DONE(实现) | `controllers/wms_events.py`、`wms_callback/receiver.py`、`strict_json.py`;信封、幂等、快照、保存点回滚与 HTTP 测试已存在 |
| 2 operation 注册 | DONE(实现) | `registry.py`、`operation.py`、`discovery.py`;生产也自动发现 Transport 的两个 operation,仅示例 `example.echo@v1` 限 local |
| 3 Transport 应用 | DONE(实现),验收 PARTIAL | `callback.py`、`callback/payloads.py`、`callback/apply.py`;版本、终态、暂存结果、冻结事实、回显与新会话读回已有测试;不能据此认定 PostgreSQL 并发验收完成 |
| 4 STALLED 定时对账 | CANCELLED | 已由回调触发 `Outbox.settle` 取代,不恢复旧扫描设计 |
| 模型与迁移 | DONE(源码),运行验收 PARTIAL | `a020` 至 `a023`;`alembic heads` 仅 `a023a20261024`;迁移运行测试依赖显式独立 PostgreSQL,本轮未运行 |
| 配套能力 | DONE(实现) | local 控制台回调投递、1 小时提交 TTL、90 天收据清理、cycle_id、卡住任务告警均有实现和相应用例 |
| 第三版 T1 / T3 / T6 | T1 已实现;T3 / T6 按第四版替换,验收 PARTIAL | 有效字段比较、任务行版本登记、HTTP 直接应用已有覆盖;不再要求证据行或后台应用入口 |
| 第三版 T2 / T4 / T5 | CANCELLED | 随证据表、待处理索引、领取扫描一并取消;§15 复选框仅保留历史记录 |
| WMS 合同与现场验收 | UNVERIFIABLE / 待联调 | 收据清理、1 小时重发窗口、CK04 等合同同步及现场 SOP 尚无本轮外部确认;来源认证仍按既定低优先级安排 |

### 本轮发现及处理

- **[P2] confidence 10/10,已按 D1 A 授权修复**:原 `transport_tasks.py` 使用 `.order_by(TransportTask.created_at, TransportTask.id).limit(limit)`,且扫描不改变任务。201 条持续卡住任务连续扫描两次,每次只告警相同的前 200 条。现按主键分批续扫,批大小仍为 200;回归 `test_every_stuck_task_is_alerted_even_when_older_tasks_remain` 验证两轮均覆盖所有任务。
- **[P2] confidence 10/10,已按 D1 A 授权修复**:原 `CallbackContext.note` 只有 `self.notes.append(...)`,接收器只有 `diagnostic=context.notes or None`,没有设计要求的告警日志。未知任务无任务行,也无法由卡住扫描补偿。现通用接收沿用 Outbox 的事务分层日志模式,保存点提交仅把诊断提升至父事务,外层事务成功提交后才输出告警,任一级回滚则丢弃。回归 `test_abnormal_callback_alerts_only_after_its_receipt_commits` 与 `test_callback_diagnostic_follows_the_callers_savepoint` 验证接纳后告警、提交失败与保存点回滚均不误报。
- **验收缺口,不虚报完成**:当前 Transport 回调测试没有同任务双身份同版本竞争、同任务两个料箱位置并发、处理器改完任务后提交失败的完整事务回滚场景。§8.1 的这些验收要求仍保留,不能用 SQLite 顺序测试或已有通用收据并发测试代替。
- **机械文档同步**:修正顶部“待评审”、历史基线、已取消对账、仍称暂缓的控制台/收据清理、诊断截断与基础层冻结的矛盾;部署说明和变更记录同步实际 operation 注册状态。

### 验证与覆盖限制

初次定向检查(修改前):512 passed、263 skipped(20.76 秒),覆盖接收路由、控制台、callback、Transport、settle、清理与告警、迁移目录、限流和分层。新增回归先得到预期失败:扫描只返回 200 而非 201,未知任务没有告警;提交失败不告警路径原本通过。修复后的最终检查记录在下表。

| 检查 | 结果 |
|---|---|
| 修复后最终定向测试 | 517 passed、267 skipped(21.37 秒),覆盖与初次定向检查相同的目录并增加上述回归;没有失败 |
| 完整套件尝试 | 第一轮修复后运行:1152 passed、348 skipped、17 failed(40.42 秒)。15 项 HTTP 实网测试在绑定 `127.0.0.1` 时收到 `PermissionError: [Errno 1] operation not permitted`;2 项站点挂载测试的 Docker Compose 命令被当前环境权限阻止。随后保存点修正由最终定向测试覆盖,未重复执行已知受环境阻断的全量套件;不能宣称全量通过 |
| 静态与迁移结构 | `ruff check .` 通过,`ruff format --check .` 为 236 文件均已格式化,`git diff --check` 通过;Alembic 单 head 为 `a023a20261024` |
| PostgreSQL | 未配置 `TEST_DATABASE_URL`,相关参数分支与迁移运行跳过;SQLite 不替代行锁、唯一索引等待与并发证据 |
| 原生主审 + 独立对抗复核 | 两者均确认上述两项 P2;修复后复核发现保存点回滚误报并已修正,最终复核无剩余确定缺陷。独立 SQLite 验证保存点回滚、根事务回滚均为 0 日志/0 收据,两层提交为 1 日志/1 收据;独立复核对测试仅看 diff stat,主审读取测试并执行本地用例 |
| 外部 Claude Code | unavailable:调用超时 30 秒,无审阅正文;stderr 报 `unrecognized_model`、配置模型 `glm-5.3`;不计作通过或完整跨模型覆盖 |
| 技能评审日志 | `gstack-review-log --start review` 写 `~/.gstack/.../.review-starts` 被沙箱以 EPERM 拒绝;本文件保留报告,不声称已保存可供 `/ship` 使用的评审凭据。已尝试记录“只读监控固定前批会饥饿”的经验,`gstack-learnings-log` 同样被 EPERM 拒绝,未写入全局经验库 |

完整套件失败用例(保留名字便于在具备权限的环境复验):

- `tests/http_client/test_logging.py`: `test_real_transport_does_not_log_response_credentials`、`test_other_callers_httpcore_logs_remain_visible`。
- `tests/http_client/test_network_behavior.py`: `test_plain_http_does_not_require_ca_files`、`test_connections_reused_environment_ignored_and_close_is_idempotent`、`test_real_timeout_stops_waiting_and_releases_connection`(2 个参数)、`test_pool_timeout_is_not_sent_and_cancel_does_not_leak_pool`、`test_slow_chunks_are_bounded_by_total_deadline`、`test_real_stream_errors_preserve_headers_and_free_pool`(4 个参数)、`test_connect_refusal_does_not_prove_business_failure`、`test_real_redirect_does_not_issue_a_second_request`、`test_cancellation_during_body_read_releases_pool`。
- `tests/scripts/test_release_bundle.py`: `test_site_override_mounts_external_configuration_read_only`(True/False)。

**结论:DONE_WITH_CONCERNS。** 本轮两项发现及复核发现的保存点问题均已修复,本地相关回归通过;设计第 1–3 步为“已实现、PostgreSQL 与联调验收未完成”,不标为全部验收通过。来源认证、网关开放、合同确认与调用方结果出口继续按既定范围推进。

本轮没有改 WMS 合同、真实目标或数据,未提交、推送、部署。评审仅确认本节列明的实现和本地验证证据,其余验收仍按 §8.1 与 §9 推进。

## 19. 解除环境限制后的续验(2026-10-23)

用户要求继续推进首轮未完成的检查。权限解除后已刷新 `origin/develop`,仍为 `8a90e2b`;当前分支没有 GitHub PR。既有应用、worker、beat、PostgreSQL 和 Redis 未重启或改配置。测试单独创建临时 PostgreSQL/Redis,使用与 CI 相同的 TimescaleDB 镜像与 Redis 8;完整套件显式设置独立 `TEST_DATABASE_URL`、`TEST_REDIS_URL`,不借用业务库。

### 验收补齐

在 `tests/integrations/wms_transport/test_callback.py` 新增以下公开入口验收,均已在独立 PostgreSQL 通过:

- `test_concurrent_results_have_one_winner_and_replayable_receipts`:同任务同版本、两个消息身份并发,相同结果为 `202+200`,不同结果为 `202+409`;只有一个结果应用,两份收据各自稳定重放。
- `test_concurrent_member_positions_do_not_overwrite_each_other`:同任务两个料箱位置并发到达,两份位置都落库,暂存成功结果随前置位置到齐完成。
- `test_failed_commit_rolls_back_task_revision_diagnostic_and_settlement`:正常结果与冻结事实矛盾结果两种路径,提交失败均返回 `503`,任务应用、版本登记、收据诊断与提交消息 settle 全部回滚,无已提交诊断告警。

| 检查 | 本轮结果 |
|---|---|
| PostgreSQL 回调与迁移定向验收 | **212 passed、5 skipped**,47.35 秒;5 个跳过为只适用于 PostgreSQL 的 SQLite 参数分支 |
| 完整套件(含显式 PostgreSQL/Redis) | **1526 passed、5 skipped**,79.66 秒;首轮 17 个端口绑定/Docker 权限失败均不再出现 |
| 静态检查 | `ruff check .`、`ruff format --check .`、`git diff --check` 均通过 |
| 原生独立对抗复核 | 无剩余源代码层确定问题;测试按 summary 模式查看,通过结论以主审实际测试结果为准 |
| 外部结构审阅 | **completed / gate PASS**,provider=claude-code,运行时模型 `glm-5.3`;无 P1/P2,1 项 P3 扫描索引建议。主审保留为未测量的容量观察项,不新增迁移;原文与核对结论见[外部审阅记录](../reviews/2026-10-23-transport-callback-review.md) |
| 外部对抗审阅 | **unavailable**,540000ms(9 分钟)超时,无审阅正文;stderr 为配置模型 `glm-5.3` 的 `unrecognized_model` 提示。没有把超时当作无发现;原生独立对抗复核与外部结构审阅是已完成的覆盖 |

### 本机 Mock 五层架联调

用户最初指定真实 WMS `10.24.199.217:8284`、货架 `510046`、点位 `KT18`、库位 `WH01`。只读探测确认 Swagger 上存在合同提交入口,但单架查询与 WH01 地码查询均返回 `ERR001 / 传输异常`,且提交合同没有逐任务回调地址字段。**在任何真实 Transport POST 之前,用户改选本机 Mock 并指定五层模板**;本轮没有向真实 WMS 发送任务、变更回调地址或触发设备动作。

本机另建 `transport_callback_mock` 数据库,使用隔离 Redis DB 14,在 `127.0.0.1:18423` 启动当前工作区 API。`RELIABLE_TARGETS` 仅指向该 loopback 上的内置 Mock WMS;进程级环境变量未写回 `.env`。通过登录后的控制台 HTTP 提交任务,由真实 Sender/AsyncHttpClient 发往 Mock WMS,再经实际 `POST /api/v1/wms/events` 投递模拟结果;不是直接调用应用私有函数。朝向 90/270 是 Mock 输入,不是对真实货架朝向的判断。

共同 `cycle_id`: `01a165ad-1c01-7743-a2f7-eab6862246d0`。

| 步骤 | 模板 | 独立 transport_task_id | 模拟终位 | 数据库结果 |
|---|---|---|---|---|
| 进场 | CTU01 | `transport-01a165ad-1c03-7ef5-85a1-7a763870d5c6` | KT18 | SUCCEEDED,revision 2 |
| 原地换面 | CTU02 | `transport-01a165ad-1f06-7f73-8ea8-3c91cfc10fd5` | KT18 | SUCCEEDED,revision 1 |
| 回库 | CTU03 | `transport-01a165ad-2339-716b-98a9-3774a75d9d79` | WH01 | SUCCEEDED,revision 1 |

额外验证:三次提交各实际发送 1 次、HTTP `202` 后消息 `DONE`,此时任务仍无结果;进场先收到 `FAILED/POSITION_UNKNOWN` 得到 `UNKNOWN`,再由较高版本成功结果收敛。三步的同身份回调重放均 `200 DUPLICATE`,改 timestamp 均 `409` 并回显原任务号;相同 client_request_id 再提交均返回原任务。独立数据库读回确认 3 个独立任务、3 条 DONE 提交消息、4 份回调收据,共同 cycle_id 不进入 wire。上述 SUCCEEDED 仅为 Mock 数据库结果,不证明真实物理到位。

验收后已优雅停止临时 API,并删除本轮创建的两个测试容器;Mock 任务为纯模拟数据,关键任务号与读回结果保留在本文。既有 Compose 的 5 个服务仍为原镜像、原运行实例,API/PostgreSQL/Redis 健康,没有修改 `.env` 或留下真实 WMS target。

### 剩余边界

真实 WMS/设备验收本轮按用户改选不执行;收据清理周期、1 小时重发窗口与对端去重、CK04 合同同步、认证与现场 SOP 仍按 §9 和 `TODOS.md` 推进。没有提交、推送或生产部署。

**结论:DONE_WITH_CONCERNS。** 本轮选定的 PostgreSQL、完整套件与本机 Mock 验收已完成;外部结构审阅完成,另一个外部对抗调用超时,完整双外部通道覆盖仍缺一项。唯一外部 P3 建议为长期容量观察,尚无现场规模/执行计划证据,不等于已发生的性能故障。

## 20. 本机数据库 Schema 隔离（2026-10-23）

用户随后批准将本项目迁入 `enterprise_backend` schema，只搬本项目表和数据，其他系统表不动。本节为 §19 之后的环境变更记录；§19 的“原镜像、原运行实例”只描述当时 Mock 验收结束的状态，不再代表当前本机状态。

已统一 ORM、在线 Alembic、API、可靠发送器及 Celery 连接。迁移仅覆盖当前 12 张 ORM 表及版本表，停写窗口对比确认全部 75 张表的 OID、行数和内容摘要一致；13 张原地迁入新 schema，public 剩余 62 张保持不变，全部 384 个关系对象 OID 与 47 个序列状态保全。原始连接的全局 search_path 仍为 `"$user", public`，只给本项目连接设置独立 schema。

本项目 API、worker、beat 已更新本机镜像；宿主机 8001 reload 进程在核对窗口暂挂后恢复。ORM 读取原有用户数据正常，容器和宿主机 `/ready` 均为 200，worker 返回 `pong`，beat 正常启动。`alembic current` 为 `a023a20261024 (head)`，`alembic check` 无差异。共享 PostgreSQL/Redis、其他项目容器、连接凭据和数据卷未替换，没有启用真实 WMS 目标。

迁移碰撞、缺表、早期 public revision 及无法识别操作的 PostgreSQL 程序化调用均拒绝，不猜测归属；离线 `--sql` 不支持。完整边界、独立复核与回归证据见 [Schema 隔离实施记录](../reviews/2026-10-23-project-schema-isolation-review.md) 和 [部署说明](../../deploy.md#schema-隔离)。本轮不改变 §19 的真实 WMS/设备与外部双通道审阅边界，未提交、推送或生产部署。
