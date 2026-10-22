# Transport 回调接收开发说明书

状态:**第四版(2026-10-22):Transport 回调改为在请求事务内直接更新 `transport_task`,取消证据表与后台应用(见 §0);§0–§13 为现行方案,§14 起的评审记录与末尾评审报告针对第三版方案,待对第四版重新评审** · 依据:`wes_backend/docs/contracts/transport-fulfillment-contract.md` §5(v0.4.0 Approved)、`wms-async-callback-envelope-contract.md`、`2026-10-21-transport-outbound-design.md`、`2026-10-22-rack-transport-integration-design.md` §5 · 范围:WMS → 本项目的统一回调入口,以及首个具体业务 `transport.task.member_position_changed@v1`、`transport.task.resulted@v1`

## 0. 第四版修订:在 `transport_task` 上直接应用(取代证据表与后台应用)

依据与原则:wes_backend 的合同可以优化而不是照搬,**对端可见的 wire 和 payload 尽量不变**。第三版为满足合同 §5.4「ACK 前不在 HTTP 请求内应用任务状态」设计了 `transport_evidence` 表、后台扫描、租约、类别配额与证据行/任务行锁顺序。该条描述的是 wes_backend 的内部处理顺序,并非对端能观察到的行为;对端观察到的行为(同一信封、相同的 `202/200/409/422/503` 语义、未知任务与事实冲突仍先返回 `202`、WMS 不需要重发)在第四版**完全不变**。因此取消证据表,处理器在请求事务内锁任务行并直接应用:

| 第三版 | 第四版 |
|---|---|
| `transport_evidence` 表、待处理索引、领取令牌与租约 | 无;状态直接落在 `transport_task` |
| 后台扫描、类别配额、满批续扫、锁顺序「证据→任务」 | 无;处理器内 `SELECT transport_task ... FOR UPDATE` 后应用 |
| 料箱结果先到则证据保持 `PENDING` | `transport_task.pending_outcome` 暂存最高版本结果,位置事件应用后在同一事务内完成 |
| 步骤 3–6 四步 | 步骤 3(接收并应用,含「收到回调即结束提交消息的发送」)一步;`STALLED` 定时对账取消(决定 10) |

代价与约束(已评估):(1) 未知任务没有任务行可写,只输出结构化告警日志,原文靠诊断快照保留 7 天,对端仍得 `202`;(2) 应用逻辑**对已通过 DTO 校验的消息不得抛异常**(冲突、未知任务、冻结缺失一律记录并返回 `Accepted`),否则会形成无法越过的毒消息,只有数据库/序列化等真异常才走通用 `503`;(3) 不再逐条留存每个位置事件与结果版本,审计只有任务行当前状态、版本摘要与快照,有需要再加表;(4) 请求内多一次任务行锁,同任务的回调串行,吞吐足够。

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
- 不改基础层 `app/http_client`、`app/reliable`(冻结,见 [[transport-domain-layering]])。

## 2. 已核实的现状

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
app/integrations/wms_transport/callback.py   Transport 的两个 operation:DTO 校验、冻结事实校验、直接应用到 transport_task(用装饰器注册)
app/core/strict_json.py                     严格 JSON 解析(出站适配器与通用接收共用)
app/models/callback.py                       CallbackReceipt、CallbackSnapshot(通用);Transport 不新增表,只给 transport_task 加列
app/tasks/callback_tasks.py                  快照清理
```

- 分三层:**通用接收**(第 1 步)→ **operation 注册**(第 2 步)→ **具体 operation**(第 3 步起)。通用层只认「operation 名 → 处理器」,不含 Transport 的列、表或规则。
- 信封合同 §6 规定 `ack_mode`/`ack_commit_facts` 不进入运行时 registry:装饰器**只注册 operation → 处理器**,不带 `ack_mode` 等元数据,这些继续只写在合同与文档里。
- 自动发现:`include_wms_events` 在启动时导入 `app/integrations/<业务>/callback.py`(`dev_callback.py` 仅 local),装饰器在导入时注册,新增 operation 不改控制器和 `main.py`(见 §3.1 实现说明)。
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
- **只告警的事实要有持久记录**:未知任务、冻结缺失、事实矛盾、终态矛盾、结果不完整这几种「返回 `202` 但不应用」的情形,处理器通过 `ctx` 附上诊断(原因码 + 规范化 `data`),通用层与收据同事务写入 `callback_receipt.diagnostic`(可空 JSON 列,随第 3 步的迁移新增;诊断内容超过 64 KiB 截断并标记)。快照有每进程每分钟上限,不能作为唯一记录。数据库、序列化等**意外异常**仍走通用 `503`(对端原样重发,不丢事实,修复后自愈),不吞成 `202`。
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
| 3 Transport 接收并应用 | 两个 operation(`callback.py`,`CallbackOperation` + `Payload`):DTO 校验、冻结事实校验、直接应用到 `transport_task`(位置、货架族与料箱族结果、版本登记、终态与前置规则);`transport_task` 新增列的迁移;`TransportService._register` 写 `frozen`;`callback_receipt.diagnostic` 列与 `ctx` 附加诊断;ACK 与 `409` 回显 `transport_task_id`(`echo_keys`);`applied_outcome` 保存完整规范化结果;`frozen` 从 `request_raw` 回填;回调事务 `lock_timeout`;结果应用后 `STALLED` 对账一次 | DTO 规则;未知任务/冻结缺失/事实矛盾/终态矛盾均 `202` 且告警、不抛异常;同版本相同 `data` 新身份 `200`、不同 `409`;低版本不回退;`UNKNOWN` 可收敛;料箱结果先到则暂存、位置到齐后完成;新登记任务带 `frozen`;PostgreSQL 并发:同任务回调串行、两个料箱的位置事件互不抹掉 |
| ~~4 `STALLED` 定时对账~~ | 已取消(决定 10) | — |

每步独立可验收、可合并;1、2 与业务无关,可先于任何具体 operation 完成并联调通用部分。**「可合并」不等于「可对 WMS 开放」**:第 1、2 步合并后入口上线,但此时真实 Transport 回调只会得到 `422 UNSUPPORTED_OPERATION`,WMS 按合同不再重试原内容,第 3 步上线也救不回,滚动发布期间打到旧实例同理。因此上线顺序约定(写入 `docs/deploy.md`):第 3 步在**全部接收实例与全部任务创建者(API、worker、脚本)**均已升级后,才在网关对 WMS 来源放行;在此之前联调只用测试 operation,不投真实 Transport 回调;滚动发布期间网关暂停来源。第 1 步另含两处对既有代码的小改动:`RateLimit` 增加 `limit is None` 豁免,`adapter.py` 的严格 JSON 解析提到 `app/core/strict_json.py`;第 3 步含 `TransportService._register` 写入 `frozen`。

第 3 步已实现(`app/integrations/wms_transport/callback.py`、`callback_payloads.py`、`callback_apply.py`、`settle.py`):`Payload` 模型承担消息自身的合法性(字段互斥、枚举、长度),`TaskCallbacks` 承担与冻结任务的对照和应用;位置事实按集合累积,`outcome_status` 对货架 `FAILED+position_unknown` 与料箱任一成员位置未知记为 `UNKNOWN`。`STALLED` 定时对账已取消,任务收到回调即 `settle` 提交消息(决定 10)。

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
- 锁超时:PostgreSQL 下任务行被另一事务长期占用时回调在预算内返回合同形状的 `503`,且不留收据与业务变更;「任务已更新、收据提交前失败」时任务、诊断、版本登记与 `resume` 全部回滚。
- 回归:`test_service.py` 原用例通过,且登记任务时 `frozen` 等于冻结的 `data`;迁移升降级,新列与 SQLite `create_all` 一致。

**4 `STALLED` 定时对账**:已取消(决定 10);「回调即结束发送」由第 3 步测试覆盖。

## 9. 待与对端确认(联调前)

- 回调认证方案(合同现为 `NONE`,见 `TODOS.md`)。
- 去重保留(合同变更):合同要求幂等记录不自动过期;我方将收据保留期对齐 `RELIABLE_RETENTION_DAYS`(90 天),需 WMS 确认同一消息身份最长重发时间不超过该期限,并同意把清理周期写入合同(合同要求先改文档)。

## 10. 明确不做

证据表与后台应用(第四版取消)、认证实现、`outcome_version` 发布、跨任务位置表与 `RackStateReader`、业务重试/窗口/换面、非 Transport operation、通用事件总线、控制台回调投递(暂缓)、基础层改动、收据与证据清理。

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

关键缺口(无测试、无处理且静默):无。「上线顺序」靠运维约束而非代码防呆,是已确认的取舍。

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
  - Files: `app/integrations/wms_transport/callback.py`、`tests/integrations/wms_transport/`。
  - Verify: PostgreSQL 实际 HTTP 并发得到 `202+200` 或 `202+409`,一份证据、两个可重放收据;赢家回滚后竞争者可接管;真正数据库失败仍 `503`。
- [ ] **T4 (P1, human: ~30–60min / CC: ~5–10min)** — 证据应用 — 在步骤 4 补全未知任务领取资格。
  - Surfaced by: Architecture R1 / D2 1A,普通 JOIN 无法到达未知任务分类路径。
  - Files: `app/integrations/wms_transport/callback.py`、`app/tasks/callback_tasks.py`、`tests/integrations/wms_transport/`。
  - Verify: 公开扫描入口同时证明未知任务 `CONFLICT` 并告警、支持任务推进、已知但未支持的证据仍 `PENDING`;SQLite/PostgreSQL 均覆盖。
- [ ] **T5 (P2, human: ~1–2h / CC: ~10–15min)** — 扫描预算 — 在步骤 4 限制每次最多 10 批且本轮不重复领取。
  - Surfaced by: Performance R4 / D5 4A,持续满批或等待租约反复到期可能形成长任务。
  - Files: `app/integrations/wms_transport/callback.py`、`app/tasks/callback_tasks.py`、`tests/integrations/wms_transport/`。
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

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| Eng Review | plan-eng-review,第四版 | Architecture / Code Quality / Tests / Performance | 本轮1;累计4 | clean(设计决定) | 12 项(原生 3 + 外部 9)均单独批准并写入;0 未决 |
| Outside Review | Codex CLI,host=claude | 独立意见 | 本轮1 | completed | 9 条,全部核实后处理(7 采纳、1 有意偏离、1 部分采纳) |
| CEO / Design / DX | 本轮未调用 | 无产品方向或 UI 变更 | 0 | 不适用 | 不新增前置评审 |

- **OUTSIDE COVERAGE:** codex,plan-review,completed,本轮 9 条。
- **VERDICT:** 第四版 ENG CLEARED FOR IMPLEMENTATION(第 3、4 步)。仅说明设计可实施,不证明回调代码、测试、WMS 合同同步或物理执行已完成,不授权实现、提交、推送或部署。位置事件先后规则仍是本方约定,联调前须与 WMS 确认。

NO UNRESOLVED DECISIONS
