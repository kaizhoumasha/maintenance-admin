# Transport 回调接收开发说明书

状态:**工程复核完成(2026-10-22 第三轮):六项补强已逐项确认,可按六步实施;尚未实现或运行回调验收,真实联调门槛未关闭;本轮外部完成标记校验未通过,已完成原生独立复核** · 依据:`wes_backend/docs/contracts/transport-fulfillment-contract.md` §5(v0.4.0 Approved)、`wms-async-callback-envelope-contract.md`、`2026-10-21-transport-outbound-design.md`、`2026-10-22-rack-transport-integration-design.md` §5 · 范围:WMS → 本项目的统一回调入口,以及首个具体业务 `transport.task.member_position_changed@v1`、`transport.task.resulted@v1`

## 1. 目标与边界

### 1.1 目标

与出站的实施方式一致:先做**统一的 FastAPI 接收 API**(信封、幂等、ACK、诊断快照),再做 **operation 注册装饰器**,最后**按业务需要逐个实现具体 operation**。Transport 是第一个具体业务:可靠接纳(收据 + 证据同事务)后返回 ACK,再由后台把证据应用到 `transport_task`,并让出站侧 `STALLED` 有权威结果出口。这是货架接入文档 §5 后续阶段(滚动窗口、业务重试、换面)的前置条件。

### 1.2 非目标

- 来源认证:合同 §5.1 现行为部署认证 `NONE`,暂不实施,已记 `TODOS.md`;路由层留一个认证检查位,不实现。
- 通用层不认识任何具体业务:不含 Transport 的字段、表或规则。
- 滚动窗口、业务重试、`departure_decide`/换面:依赖本文产物,另行设计。
- `outcome_version` 对外发布(合同 §5.4 第 8 步):没有下游消费者前不做。
- 跨任务的货架/料箱当前位置表与 `RackStateReader` 项目内实现:没有调用方,不做。
- 非 Transport 的 WMS operation:通用层为其预留注册点,本文不实现。
- 联调控制台的「回调投递」场景:已记 `TODOS.md`。
- 不改基础层 `app/http_client`、`app/reliable`(冻结,见 [[transport-domain-layering]])。

## 2. 已核实的现状

| 事实 | 来源 |
|---|---|
| `transport_task` 只有身份、`kind`、摘要(哈希)、产线、工位、创建时间;**没有成员、目标、面向、`rack_id`,也没有状态列和终位列** | `models/transport.py` |
| 冻结请求字节只在 `reliable_message.request_raw`(紧凑 JSON),终态消息 `RELIABLE_RETENTION_DAYS`(90 天)后被物理清理 | `service.py`、`reliable/store.py` |
| `Outbox` 只有 `enqueue`、`resume` 两个公开方法;`STALLED` 只能被 `resume` 放回 `PENDING`;没有按 `business_key` 置终态的接口。`resume` 会把 `not_received_count` 清零 | `reliable/outbox.py`、`store.py` |
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
app/integrations/wms_transport/callback.py   Transport 的两个 operation:DTO 校验、证据保存、后台应用(用装饰器注册)
app/core/strict_json.py                     严格 JSON 解析(出站适配器与通用接收共用)
app/models/callback.py                       CallbackReceipt、CallbackSnapshot(通用);TransportEvidence(Transport 专属,放 transport 模型)
app/tasks/callback_tasks.py                  快照清理;后台证据应用扫描与 STALLED 对账(Transport 业务引入后加入)
```

- 分三层:**通用接收**(第 1 步)→ **operation 注册**(第 2 步)→ **具体 operation**(第 3 步起)。通用层只认「operation 名 → 处理器」,不含 Transport 的列、表或规则。
- 信封合同 §6 规定 `ack_mode`/`ack_commit_facts` 不进入运行时 registry:装饰器**只注册 operation → 处理器**,不带 `ack_mode` 等元数据,这些继续只写在合同与文档里。
- 注册靠导入触发:`main.py` 在已有的 `wms_transport` 导入处保证 Transport 模块在启动时被导入。
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
  handle(ctx, parsed) -> Outcome   在通用层打开的保存点内执行,写业务自己的行(如证据)
ctx = {session, receipt_id, operation_id, timestamp, parsed};处理器不保存请求状态
Outcome = Accepted(data) → 202 RECEIVED | Duplicate(data) → 200 | Conflict(data) → 409 | Rejected(reason_code) → 422
```

- **信封归通用层**:通用层校验必需的 `operation_id/operation/timestamp/data` 四个字段(`timestamp` 为 `0..2^63-1` 的整数,不接受 bool),顶层冗余字段忽略;Transport 处理器的纯 `parse` 按具体合同忽略业务各层冗余字段并规范化有效字段。处理器只返回 `Outcome`,不拼响应。公共信封所说的“完整内容”在 Transport operation 上按具体合同 §5.4 的规范化有效字段解释,不据此宣称公共合同文字已同步(D3 / 2A 已确认)。
- 收据由通用层写(身份、摘要、首次响应);处理器的业务行与收据**同一事务**。
- **事务顺序与四种 Outcome 规则**:事务内先占收据(保存点外)→ 开保存点调用处理器 → `Accepted/Duplicate/Conflict` 释放(提交)保存点,`Rejected` 显式回滚保存点 → **先构造并校验响应**,再把首次响应写回收据 → 提交。`Duplicate/Conflict` 要求处理器在返回前不留业务行(先判定后写入);处理器不得自行 `commit/rollback`,也不得产生无法随数据库回滚的外部副作用;意外异常回滚整个接收事务并返回 `503`。收据因此永远带完整可重放的响应,不存在业务已提交而响应构造失败的情况。
- 业务专属的幂等规则由处理器判定:例如 `resulted` 的「同任务同结果版本」登记返回 `Duplicate/Conflict`;通用层的摘要比较只管同身份重试。
- **结果版本登记的并发败者(D7 / 6A 已确认)**:Transport 处理器在现有保存点中,针对 `(transport_task_id, outcome_revision)` 唯一键执行 `INSERT ... ON CONFLICT DO NOTHING RETURNING`;真正插入证据者返回 `Accepted`,未插入者重读赢家的规范化 `data`,相同返回 `Duplicate`,不同返回 `Conflict`,不保存第二份证据。当前消息收据仍由通用层在外层接收事务中保存首次 `200/409`,不是回滚当前收据后让正常竞争先返回 `503`。若赢家回滚,等待者可插入并成为实际赢家。复用 `TransportService._register` / `Outbox.enqueue` 的原子插入后重读模式,不笼统捕获 `IntegrityError` 后继续使用失效事务;连接、序列化等真正无法可靠完成的异常仍整体回滚并返回不冻结的 `503`。
- 处理器抛出的任何意外异常由通用层兜底为合同形状的 `503`。
- **先占身份再执行有副作用的处理器**(并发同身份只写一份的实现方式):通用层在事务内先 `INSERT callback_receipt ... ON CONFLICT DO NOTHING RETURNING`(沿用 `Outbox.enqueue` 的写法)占住 `(operation, operation_id)`;没拿到则重读已有收据并按 §5 比较;拿到才执行 `handle`,事务末尾再把首次响应写回收据。纯 `parse` 可用于有效消息比较,但不得写数据库或触发外部副作用。PostgreSQL 下并发的第二个请求会在唯一索引上等第一个提交,随后读到已有收据。不使用「先查后插」替代原子占位。
- **注册沿用 `@dev_scenario` 的做法**:重复注册同名 operation 报错并同时指出两个处理器;注册表是模块级全局状态,测试用 `monkeypatch` 替换成空表隔离(参照 `tests/core/test_dev_scenarios.py`)。Transport 的处理器模块由 `wms_transport/__init__.py` 导入,`main.py` 已导入该包,因此启动即注册。

## 4. 数据模型

### 4.1 通用表(第 1 步)

| 表 | 关键列 | 约束/用途 |
|---|---|---|
| `callback_receipt` | `operation`、`operation_id`、`raw_digest`、`effective_digest?`、`compare_mode`(RAW/EFFECTIVE)、首次响应 `status_code`+`body`+`resp_timestamp`、`created_at` | `UNIQUE(operation, operation_id)`;精确原信封优先重放;未知 operation 或非法消息的拒绝使用 RAW,结构合法且已知 operation 使用 EFFECTIVE;**不含任何业务列**。第 1 步即建齐两种摘要元数据,第 2 步启用纯 parse 有效字段比较(D3 / 2A) |
| `callback_snapshot` | `request_id`、`received_at`、`body_b64`(≤64 KiB)、`truncated`、`http_status`、`response_body`、`outcome` | 仅诊断,无唯一约束,不参与幂等 |

`operation` 长度上限为 256 个字符(code point),`callback_receipt.operation` 为 `VARCHAR(256)`,物理唯一约束直接为 `UNIQUE(operation, operation_id)`(最长约 1 KiB + 36 字节,远低于 PostgreSQL B-tree 索引项限制),不再需要 `operation_digest` 与摘要碰撞核对。超过上限的 `operation` 按 §5 第 2 条处理,不建收据。

### 4.2 Transport 专属(第 3 步起)

| 表/列 | 内容 | 引入步 |
|---|---|---|
| `transport_evidence` | `receipt_id`(唯一外键)、`kind`(POSITION/RESULT)、`transport_task_id`、`outcome_revision?`、`data`(JSON)、`evidence_status` PENDING/APPLIED/CONFLICT、领取令牌与租约截止 | 第 3 步 |
| 〃 唯一约束 | `UNIQUE(transport_task_id, outcome_revision)`:`resulted` 版本登记靠它原子完成;位置事件的 `outcome_revision` 为空,PostgreSQL 与 SQLite 都把空值视为互不相同,不需要局部索引 | 第 3 步 |
| 〃 待处理索引 | 模型与 Alembic 迁移同时声明服务实际领取查询的 `PENDING` 访问路径,支持类别、稳定 `id` 顺序与租约筛选。可用 `(kind, id)` 的 `PENDING` 条件索引或等效复合索引,最终键顺序按实际 SQL 确认,不将唯一约束当成待处理索引。PostgreSQL 迁移与 SQLite `create_all` 均建立对应索引(D6 / 5A 已确认) | 第 3 步 |
| `transport_task.frozen`(JSON) | 登记任务时存下冻结的 `data`(成员、目标、`target_face`、`rack_id`);由 `TransportService._register` 写入。**与 Transport 回调接收同一步上线**(此前 operation 未注册,回调只会得到 `422`,不存在「已 ACK 却无冻结信息」的窗口);此前登记的任务(无真实目标,仅联调数据)值为空,应用时按「冻结信息缺失」标 `CONFLICT` | 第 3 步 |
| `transport_task.outcome_status`(`NULL` 或 `SUCCEEDED/FAILED/CANCELLED/UNKNOWN`)、`final_position`(JSON)、`applied_outcome_revision` | 已应用的权威结果;料箱任务的逐箱结果放进同一 JSON | 第 4 步 |
| `transport_task.member_positions`(JSON) | 任务内每个料箱成员的位置里程碑 | 第 5 步 |

## 5. 请求处理(通用接收,与合同 §5.4 对齐)

1. 原始 body > 256 KiB → 空体 `413`(解析前);非法 JSON/UTF-8、任意层重复 key、NaN/Infinity、无法提取合法 `operation_id`(含非 UUIDv7)→ 空体 `400`。二者不建收据。
2. `operation` 缺失、非字符串、含 NUL、无法编码为 UTF-8 或**长度超过 256 个字符**(`operation_id` 合法)→ `422 REJECTED / INVALID_EVIDENCE`,因形成不了可存储的 `(operation, operation_id)` 身份**不建收据**,只写快照(WMS 按合同不再重试原内容)。长度上限同时封住「未认证请求用超长 `operation` 撑大不清理的收据表」这条增长路径。
3. 按 `(operation, operation_id)` 原子占位或取得既有收据。`raw_digest` 是严格解析后**完整原信封(含冗余字段)**的规范 JSON sha256,与 handler 无关;既有收据的原信封指纹相同即优先重放首次响应(`RECEIVED` 改报 `200/DUPLICATE`,其它确定响应保留原状态、时间和 data),不重新执行 handler。这样未注册时保存的 `422` 在注册上线后仍稳定重放,已保存的拒绝不会变成功。
4. 原信封指纹不同:既有收据为 RAW 时返回 `409/CONFLICT`;为 EFFECTIVE 时仅调用当前 operation 版本的纯 `parse`,以规范化有效信封 `{operation, operation_id, timestamp, data: parsed}` 的 sha256 比较 `effective_digest`。同有效摘要重放首次响应,不同有效摘要或修改后已定义字段非法返回 `409`,不执行 `handle`。若原有效收据对应的规范化器不可用而无法可靠比较,返回不持久化的 `503`,不能将既有身份重新判为未知 operation。有效字段规则在同一 operation 版本内冻结,语义不兼容变更须新版本,不再承诺任意修改同一 `@v1` 的 DTO 语义后仍能稳定比较。两个摘要均保留 `timestamp`:改变时间戳仍冲突;合法 Transport 消息仅冗余字段变化不冲突。新身份且同任务同结果版本的兼容重发仍由 handler 用规范化 `data` 做版本登记,不改变结果版本身份(D3 / 2A 已确认)。
5. 新身份的拒绝一律为 HTTP `422`,body `{operation_id, code:"REJECTED", timestamp, data:{reason_code}}`:字符串 `operation` 但未注册 → `reason_code=UNSUPPORTED_OPERATION`(通用层);已注册 operation 的信封非法或纯 `parse` 判断 DTO 非法 → `INVALID_EVIDENCE`;均以 RAW 模式原子保存原信封指纹与首次拒绝响应。结构合法的已知 operation 保存原信封指纹与有效摘要,以 EFFECTIVE 模式记录 handler 的首次确定响应。信封校验不再拒绝顶层冗余字段。
6. 合法:调用处理器 `handle`,与收据同事务提交;按 `Outcome` 返回 `202/200/409/422`;提交失败 `503/UNAVAILABLE`(不冻结为幂等响应)。
7. **意外异常兜底**:路由对通用接收整体 `try/except Exception`(不吞 `CancelledError`):已取得合法 `operation_id` 返回 `503 {operation_id, code:UNAVAILABLE, timestamp, data:{}}`,否则空体 `503`;记结构化错误日志并写快照(`outcome=error`)。

响应只用合同允许的 `202/200/409/422/503` 与空体 `400/413`;无 `429`、无 `Retry-After`、本阶段无 `401`。

## 6. Transport 的后台应用(证据 → `transport_task`,第 4 步起)

### 6.1 通用机制

- Celery beat 定时扫描(间隔为配置项),小批量领取 `PENDING`:**批次内 POSITION 与 RESULT 各保留至少一半名额(一类不足由另一类补满)**,类内按 `id`;满批可续扫,但**每次公开扫描调用最多领取 10 批**(首版常量),无新资格行或达到预算就返回,剩余工作交给下一次既有 beat,不自投递新任务、不新增 Redis 调度锁。通过每类本轮游标等局部状态保证**本轮已领取证据不重复领取**,即使等待证据的租约在本轮内到期也不回头重领;下次调用重置本轮状态,仍按既有租约恢复。每批独立事务,局部状态有界。10 批只约束领取轮数,不是墙钟耗时或容量 SLA;批大小和扫描间隔须按现场吞吐验证,不宣称 beat 自动防止任务重叠(D5 / 4A 已确认)。
- 写领取令牌与 30 秒租约(常量,不做配置);领取沿用 `app/reliable/store.py` 的 `order_by + limit + with_for_update(skip_locked=True)` 模式(不抽象成共用工具);租约过期可重领,旧令牌不得写回。首版不做提交后唤醒。**领取以证据为主,资格为「任务不存在,或任务存在且其 kind 与证据 kind 属本步支持组合」**;可用 `NOT EXISTS / EXISTS` 判断,不能只用普通 JOIN 后按任务 kind 过滤,否则未知任务证据不可达。领取阶段只锁证据行,应用阶段再按既定顺序锁任务行。本步支持组合:第 4 步为 `RACK_MOVE/RACK_ROTATE` 的 RESULT;第 5 步起加入 POSITION;第 6 步加入 `BIN_MOVE/BIN_EXCHANGE`。已知但不支持的保持 `PENDING`。三种情况分开处理与告警:**未支持**(保持 `PENDING`)、**未知任务**(被领取后标 `CONFLICT` 并告警)、**冻结信息缺失**(标 `CONFLICT`,原因不同)。不新增未知任务恢复机制(D2 / 1A 已确认)。
- **应用时的锁顺序固定为「证据行 → 任务行」**:同一事务内先 `SELECT transport_evidence ... FOR UPDATE`,校验领取令牌与租约未过期(不匹配则回滚),再 `SELECT transport_task ... FOR UPDATE`(合同 §5.4 第 6 步),重读 `applied_outcome_revision`/终态/`member_positions`,更新任务并标证据 `APPLIED`/`CONFLICT`;全局固定该顺序避免死锁;SQLite 下无行锁,按事务串行。
- 等待前置的证据(如料箱成功结果等位置事件)**保持租约直到到期**,不立即释放;配合类别配额,被堵结果不会占满批次,也不会饿死它所等待的位置事件。
- **不看出站消息状态**:只要任务存在就应用结果(权威结果是物理事实,出站消息可能还在 `PENDING/SENDING/STALLED`)。
- 校验冻结事实(任务存在、`rack_id`/`container_id` 属冻结对象、目标匹配)读 `transport_task.frozen`,不依赖出站消息保留期。
- 无法与冻结任务单调收敛的证据标 `CONFLICT`,输出结构化告警日志(任务号、证据类型、原因,仿 `reliable stalled`),不新增查询接口。

### 6.2 `resulted`

- 仅当 `outcome_revision` 高于 `applied_outcome_revision` 时,同事务更新任务结果并标 `APPLIED`;低版本标已处理、不回退。
- `SUCCEEDED/FAILED/CANCELLED` 已确定后任何后续结果(即使版本更高)标 `CONFLICT`;`UNKNOWN` 可被更高版本收敛。
- 货架族 `SUCCEEDED` 缺少到达面时,用冻结的 `target_face` 补齐应用结果,证据保留规范化后的空值(合同 §5.3)。
- 料箱族成功回架(冻结目标为 `RACK_BIN_SLOT`)的前置规则:所需 `TARGET_PLACED` 均已 `APPLIED` 才完成任务,否则保持 `PENDING` 并保持租约直到到期(见 §6.1)。
- **收敛 `STALLED`(对账式)**:抽出一个幂等步骤——找出「任务已有确定结果(`outcome_status` 为 `SUCCEEDED/FAILED/CANCELLED`,不含 `UNKNOWN`)且 `reliable_message WHERE business_key = transport_task_id AND status = 'STALLED'`」的行(只读引用基础层模型),对命中的每一行用它自己的 `(target, operation, operation_id)` 调用现有 `Outbox.resume`。结果应用成功后调用一次(不受冷却限制);后台扫描任务里也定时调用一次,覆盖「结果先到、消息后来才进入 `STALLED`」,**定时调用只处理 `updated_at` 早于当前时间 30 分钟的 `STALLED` 消息**(常量,不做配置),避免 WMS 不可达时 `resume` 清零预算后无限重发。不依赖 `RELIABLE_TARGETS`/`transport_service.target`,也不假设「一任务一消息」。**验收只承诺「消息被置回 `PENDING`」**;是否变 `DONE` 取决于有目标配置的发送进程在运行。不改基础层、不新增重试调度。

### 6.3 `member_position_changed`

- 只维护**任务内位置状态**(`member_positions`),足够支撑料箱回架前置规则;跨任务的当前位置表与 `RackStateReader` 等有调用方时再做。
- 先后规则(**合同未明确,为本方约定,联调时须与 WMS 确认**):`SOURCE_PICKED` 低于 `TARGET_PLACED`;同一料箱已有同级或更高里程碑时,后来的事件标 `APPLIED` 且不改状态;`TARGET_PLACED` 应用后不可再被改;`POSITION_UNKNOWN` 可被后来的 `TARGET_PLACED` 收敛,不能被后来的 `SOURCE_PICKED` 覆盖;`TARGET_PLACED` 的 `final_position` 与已应用值不同标 `CONFLICT`。

## 7. 保留期与诊断快照

| 数据 | 配置 | 默认 | 清理 |
|---|---|---|---|
| `callback_snapshot` | 新增 `CALLBACK_SNAPSHOT_RETENTION_DAYS` | 7 | 到期即删,独立于收据;第 1 步即上线清理任务 |
| `callback_receipt`、`transport_evidence` | 无 | — | **本阶段不清理**。删除收据同时删除消息身份与结果版本登记的依据,等价于隐含去重窗口,而合同没有给窗口;清理还必须分别定义消息去重、版本登记、诊断证据(孤儿拒绝收据、`CONFLICT`)的生命周期,且「确定结果」须排除 `UNKNOWN`。已记 `TODOS.md` |

- 快照清理沿用现有模式:`*_RETENTION_DAYS` 配置 + `positive()` 校验 + `run_*` 批量清理 + Celery beat。
- 快照:路由在响应后用 FastAPI 后台任务尽力写入(原始字节 Base64、≤64 KiB 并标截断、最终响应体、HTTP 状态、请求 ID、结果类别);写失败/超时只打日志,**不改响应、不返回 503**;覆盖成功、重复、冲突、非法 JSON/UTF-8、超限、处理异常;不承担幂等与恢复。
- 快照独立成表,不复用 `audit_log`。认证引入后快照须脱敏请求头(与认证 TODO 同步复核)。
- **暴露面约束**(认证 `NONE` + 限流豁免 + 每请求写快照叠加,可被任意速率写满磁盘):(1) `docs/deploy.md` 写明该路径在认证落地前必须由网关限制为 WMS 来源;(2) 快照写入加进程内每分钟条数上限(常量,不做配置),超额请求照常处理、只丢快照并输出计数告警日志;多进程下上限按进程计。

## 8. 实施步骤

| 步骤 | 内容 | 验收 |
|---|---|---|
| 1 通用接收 | 路由、信封校验、严格 JSON(提到 core)、收据幂等与稳定重放、未注册 operation 的 `422`、意外异常 `503` 兜底、`RateLimit` 豁免、快照与其保留期配置/清理/写入速率上限、`docs/deploy.md` 的部署约束说明、迁移(收据、快照) | `413/400`;`operation` 缺失 `422` 不建收据;未知 operation `422 UNSUPPORTED_OPERATION` 并稳定重放;同身份同摘要重放、异摘要(含仅 `timestamp` 不同)`409`;并发同身份只写一份;快照写失败不影响响应;超过 100 次/分钟不返回 `429` |
| 2 operation 注册 | 注册装饰器、处理器契约(`parse`/`handle`/`Outcome`)、与收据同事务、`Rejected` 回滚处理器写入、处理器异常 `503` | 用**测试里注册的测试 operation** 验证 `202/200/409/422`;`Rejected` 不留业务行;处理器异常合同形状 `503`;注册重复名报错 |
| 3 Transport 接收 | 两个 operation 的 DTO 校验与证据保存(`PENDING`)、`transport_evidence` 与唯一约束及待处理索引、结果版本登记、`transport_task.frozen` 及登记时写入、迁移 | DTO 规则;同版本相同 `data` 新身份 `200/DUPLICATE` 无第二份证据、不同 `data` `409`;新登记任务带 `frozen`;并发同版本只有一个赢家;待处理索引在 PostgreSQL 迁移与 SQLite 模型下均成立 |
| 4 货架结果应用 | `transport_task` 结果三列(迁移);后台扫描、类别配额与满批续扫、租约、应用时锁任务行;货架族结果应用与版本/终态规则;`STALLED` 对账式收敛;`CONFLICT` 告警 | 低版本不回退;已确定终态不被改写;未知任务 202 后标 `CONFLICT`;结果先于出站接纳也能应用;`STALLED` 消息被置回 `PENDING`;同任务多证据并发不互相覆盖 |
| 5 位置事件应用 | `member_positions` 列;位置事件应用与先后规则 | 倒序不回退;`TARGET_PLACED` 终位等于冻结目标;终位矛盾标 `CONFLICT` |
| 6 料箱结果应用 | 料箱族结果应用(逐箱结果、完整覆盖冻结成员);成功回架前置规则 | 前置位置缺失时结果保持 `PENDING`;位置到齐后完成;成员缺失/多余不接受为确定终态 |

每步独立可验收、可合并;1、2 与业务无关,可先于任何具体 operation 完成并联调通用部分。**「可合并」不等于「可对 WMS 开放」**:第 1、2 步合并后入口上线,但此时真实 Transport 回调只会得到 `422 UNSUPPORTED_OPERATION`,WMS 按合同不再重试原内容,第 3 步上线也救不回,滚动发布期间打到旧实例同理。因此上线顺序约定(写入 `docs/deploy.md`):第 3 步在全部接收实例就绪后,才在网关对 WMS 来源放行;在此之前联调只用测试 operation,不投真实 Transport 回调;滚动发布期间网关暂停来源。第 1 步另含两处对既有代码的小改动:`RateLimit` 增加 `limit is None` 豁免,`adapter.py` 的严格 JSON 解析提到 `app/core/strict_json.py`;第 3 步含 `TransportService._register` 写入 `frozen`。

### 8.1 测试计划

测试按 AGENTS.md:只固化长期合同,通过公开入口断言,不测私有函数,不为凑覆盖率写测试。

**1 通用接收**
- `tests/controllers/test_wms_events.py`:按合同条款参数化,只走 HTTP 入口——`413`;`400`(非法 JSON/UTF-8、任意层重复 key、NaN/Infinity、`operation_id` 缺失或非 UUIDv7);`operation` 缺失、非字符串或超过 256 个字符→`422` 且不建收据,恰好 256 个字符可建收据并稳定重放;未注册 operation→`422 REJECTED / UNSUPPORTED_OPERATION` 完整响应体并稳定重放;同身份同摘要重放、同身份不同摘要(含仅 `timestamp` 不同)`409`;提交失败 `503` 且不冻结为幂等响应;意外异常仍返回合同形状的 `503`;超过 100 次/分钟也不返回 `429` 且不访问 Redis;快照写入、超 64 KiB 截断、写失败不改变响应、超过每分钟上限只丢快照且响应不变。
- PostgreSQL 集成(需 `TEST_DATABASE_URL`):并发同身份只写一份收据(先占身份再调用处理器);赢家回滚后竞争者接管;提交后丢响应再重试;任何已提交收据都有可重放响应。
- `tests/migrations`:新迁移升降级。
- 回归(既有行为被改动,必须保留):`tests/core/test_ratelimit.py`、`tests/integrations/wms_transport/test_adapter.py` 原用例不变且通过;`RateLimit(100)` 仍照常返回 `429`。

**2 operation 注册**:每个测试用 `monkeypatch` 隔离注册表;测试里注册的测试 operation 覆盖 `Accepted/Duplicate/Conflict/Rejected`;`Rejected` 显式回滚处理器写入(不留业务行);响应构造失败时回滚整个接收事务;通用层拒绝必需顶层字段缺失与非法 `timestamp`,不因顶层冗余字段拒绝。经公开 HTTP 入口验证:精确原信封优先重放;合法消息只改变顶层或业务各层冗余字段仍重复,有效字段或 timestamp 变化冲突;未知 operation 的原 `422` 在注册后仍重放,已保存拒绝不会变成功;已接纳身份无法取得对应规范化器且原指纹不同时 `503`,不重新判为未知 operation;有效字段比较只运行纯 `parse`,不执行有副作用的 `handle`。处理器异常合同形状 `503`;新身份 `parse` 非法→`422 INVALID_EVIDENCE`;重复注册报错(D3 / 2A 已确认)。

**3 Transport 接收**:两个 operation 的 DTO 规则(货架族/料箱族 `resulted`、位置事件);`resulted` 同版本相同 `data` 新身份 `200/DUPLICATE` 且无第二份证据、不同 `data` `409`;PostgreSQL 并发:同 `(transport_task_id, outcome_revision)` 只有一个赢家;唯一约束在 PostgreSQL 迁移与 SQLite `create_all` 下均生效(位置事件的空版本不冲突);回归:`test_service.py` 原用例通过,且登记任务时 `frozen` 等于冻结的 `data`。

Transport 的实际两个 operation 另经公开 HTTP 入口验证 D3 / 2A 的消息等价性:顶层及业务嵌套冗余字段忽略,已定义字段严格校验,同身份只改变有效字段或 timestamp 才冲突;原信封重放与同结果版本新身份重发是两条独立验收路径。公共信封“完整内容”的解释须在联调材料中同步为上述具体合同有效字段口径,本轮不修改外部合同。

**步骤 4/6 的最小贯通事务验收(D4 / 3A 已确认)**:在 `tests/integrations/wms_transport/` 通过 HTTPX 调用实际 HTTP 路由,使用启动导入的真实 Transport 注册处理器、真实收据/证据持久化和公开后台扫描入口,复用 SQLite 与显式独立 PostgreSQL fixture。不得以直接插入证据的分段测试替代以下两条流程,不得将接收或应用整段替换为 mock 后声称贯通;不要求真实 WMS 或真实网络发送。
- 步骤 4:先由真实能力层登记含 `frozen` 的货架任务;HTTP 接纳结果后,新会话确认收据和证据已持久化、任务尚未在 HTTP 内同步应用;再调用公开后台扫描入口,新会话确认任务结果与证据状态共同提交。原回调重放不重复创建证据、不改变原提交身份与冻结请求。
- 步骤 6:先提交成功回架结果并运行扫描,前置位置缺失时证据仍为 `PENDING`、任务未完成;逐箱 `TARGET_PLACED` 经同一实际 HTTP 入口接纳并应用后,后续扫描才完成任务。结果和位置回调重放不产生重复义务,不改写原出站身份或冻结内容。配合既有失败回滚与并发用例,不重复基础层 ACK/租约测试,不把 `202` 当物理完成。

**单次扫描资源边界(D5 / 4A 已确认)**:通过公开后台扫描入口验证持续满批时最多领取 10 批即返回,后续调用仍可推进剩余证据;将时钟推进越过 30 秒,等待前置的证据本轮不被重复领取、仍为 `PENDING`,下一轮按租约可重领;POSITION/RESULT 配额仍使两类证据有进展。SQLite 与显式独立 PostgreSQL 均验证结果,不将私有 SQL 或游标内部形状固定为合同。

**待处理索引验收(D6 / 5A 已确认)**:步骤 3 核验 PostgreSQL 迁移升级建立、降级移除对应索引,SQLite `create_all` 与迁移后的模型约束一致;步骤 4 在大量 `APPLIED/CONFLICT` 历史和少量待处理证据下检查实际领取查询计划及公开扫描行为,确认访问路径合理。查询计划检查是实施时的数据库核验,不把某段 `EXPLAIN` 文本、特定索引名或成本数字固化为长期行为测试。不宣称每次必然顺序扫描,也不以索引代替容量验证。

**结果版本并发败者验收(D7 / 6A 已确认)**:步骤 3 的显式独立 PostgreSQL 用例通过实际 HTTP 接收两个不同消息身份、相同任务和版本的并发请求。相同规范化 `data` 返回一份 `202` 加一份 `200`,不同 `data` 返回一份 `202` 加一份 `409`;新会话确认一份证据、两份各自可稳定重放的收据,原任务身份和出站冻结内容不变。另验证暂时领先的插入事务回滚后,竞争者可接管成为实际赢家。SQLite 覆盖相同业务结果,不把 SQLite 当作 PostgreSQL 唯一索引等待行为的证明。

**4 货架结果应用**:版本单调与低版本不回退;已确定终态不被更高版本改写,`UNKNOWN` 可被收敛;经公开后台入口验证三类领取结果:未知任务 `202` 后被领取、标 `CONFLICT` 并输出告警,已支持任务能推进,已知但未支持的任务保持 `PENDING`;SQLite 与显式独立 PostgreSQL 均覆盖,不测试私有 SQL 形状(D2 / 1A 已确认);结果先于出站接纳也能应用;`STALLED` 消息被置回 `PENDING`(含未配置 `RELIABLE_TARGETS` 的进程、结果先到而消息后进入 `STALLED`、30 分钟冷却);租约过期重领、旧令牌不得写回(含「令牌检查后被重领」的竞争);第 4 步运行时已接纳的料箱结果不被误应用或误判冲突;PostgreSQL 并发:同任务多条证据不互相覆盖;持续位置流量下结果仍能推进;缺少到达面时用冻结 `target_face` 补齐。

**5 位置事件应用**:倒序不回退、`TARGET_PLACED` 终位等于冻结目标、终位矛盾标 `CONFLICT`、`UNKNOWN` 被后来的 `TARGET_PLACED` 收敛;PostgreSQL 并发:同任务两个料箱的位置事件不互相抹掉。

**6 料箱结果应用**:结果必须完整且仅覆盖冻结成员;前置位置缺失时结果保持 `PENDING`,位置到齐后完成;类别配额使被堵结果不会饿死它所等待的位置事件。

## 9. 待与对端确认(联调前)

- 位置事件的先后规则(§6.3)。
- 回调认证方案(合同现为 `NONE`,见 `TODOS.md`)。
- 去重保留:合同未给窗口;本阶段收据不清理,清理策略待对端承诺后再定(见 §7)。

## 10. 明确不做

认证实现、`outcome_version` 发布、跨任务位置表与 `RackStateReader`、业务重试/窗口/换面、非 Transport operation、通用事件总线、控制台回调投递(暂缓)、基础层改动、收据与证据清理。

## 11. What already exists(复用与新增)

| 能力 | 现有位置 | 本文做法 |
|---|---|---|
| 严格 JSON 解析 | `adapter.py` 私有函数 | 提到 `app/core/strict_json.py`,出站与回调共用 |
| 注册装饰器模式 | `core/dev_scenarios.py` 的 `@dev_scenario` | operation 注册沿用同风格 |
| UUIDv7 校验 | `app/core/identifiers.py` | 直接复用 |
| `validate_text/validate_face/require_uuid7`、位置类型 | `wms_transport/types.py` | Transport DTO 校验复用 |
| 「`on_conflict_do_nothing` + 重读」幂等写入 | `Outbox.enqueue` | 收据写入沿用,不用先查后插 |
| 领取 `order_by + limit + skip_locked` | `reliable/store.py` | 沿用模式,不抽象成共用工具 |
| 保留期配置 + `run_*` 批量清理 + beat | `audit_tasks`、`reliable_tasks` | 快照清理沿用 |
| `STALLED` 的唯一出口 | `Outbox.resume` | 对账式调用,不改基础层 |
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
   Celery beat 扫描 ─ 证据资格、类别配额、最多10批且本轮不重领、30s 租约 ─▶ 锁证据行 → 锁任务行
                                 ▼
          应用(版本/终态/位置规则)→ 证据 APPLIED/CONFLICT → STALLED 对账(30 分钟冷却)
```

| 新路径 | 现实失败 | 测试 | 错误处理 | 对端/运维所见 |
|---|---|---|---|---|
| 路由读 body | 超 256 KiB | 有 | 空体 `413` | 明确 |
| 占收据 | 并发同身份 | 有(PG) | 唯一约束,输家重读 | 一份收据 |
| 处理器 | `Rejected` / 意外异常 | 有 | 显式回滚保存点 / 整体回滚 + `503` | 明确 |
| 响应构造 | 序列化失败 | 有 | 提交前校验,失败整体回滚 | `503`,WMS 重试 |
| 快照 | 写失败 / 超速率 | 有 | 只打日志与计数,响应不变 | 日志可见(按设计) |
| 上线顺序 | 真实回调早于第 3 步 | 无(运维约束) | 网关控制来源 | 部署文档约定 |
| 后台应用 | worker 崩溃 / 令牌后被重领 | 有 | 租约、锁证据行再锁任务行 | 稍后重试 |
| 不支持/未知任务/缺冻结 | 无法应用 | 有 | 分三类处理与告警 | 日志 |
| `STALLED` 对账 | WMS 不可达 | 有 | 30 分钟冷却 | 日志告警 |

关键缺口(无测试、无处理且静默):无。「上线顺序」靠运维约束而非代码防呆,是已确认的取舍。

## 13. 并行实施策略

六步按依赖串行:通用接收 → operation 注册 → Transport 接收 → 货架结果应用 → 位置事件应用 → 料箱结果应用,共享 `app/models/`、`app/integrations/`、`app/tasks/`、`migrations/`。第 1 步内部 `RateLimit` 豁免(`app/core/`)与严格 JSON 提取(`app/core/`+`adapter.py`)互不依赖、可并行,其余串行。

## 14. 评审决定记录(2026-10-22 第二轮)

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
独立旁路 ─ 尽力快照;STALLED对账只调用既有resume,不证明物理完成
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

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| Eng Review | plan-eng-review,第三轮 | Architecture / Code Quality / Tests / Performance | 本轮1;累计3 | clean(设计决定) | 6项均单独批准并写入;0未决,0剩余关键计划缺口 |
| Outside Review | Claude Code CLI,host=codex | 独立意见 | 本轮1 | unavailable | glm-5.3 返回5条信息性意见,完成标记不合规;不计完整外部覆盖 |
| Native Fallback | Codex in-host只读副代理 | 实际源码及合同交叉核验 | 本轮1 | completed | 2项确认缺口均已批准写入;不是外部复核通过 |
| CEO / Design / DX | 本轮未调用 | 无产品方向或UI变更 | 0 | 不适用本轮 | 不新增前置评审 |

- **OUTSIDE COVERAGE:** unavailable,完成标记缺失;原生回退已完成,不能替代外部覆盖。
- **VERDICT:** ENG CLEARED FOR IMPLEMENTATION / DONE_WITH_CONCERNS — 保留六步路线,六项修正均已决定;外部覆盖限制如上。仅说明设计可实施,不证明回调代码、测试、WMS合同同步或物理执行已完成,不授权实现、提交、推送或部署。

NO UNRESOLVED DECISIONS
