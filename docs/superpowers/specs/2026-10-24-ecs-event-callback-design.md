# ECS 事件回调接收设计

状态:**已实现(2026-10-24)。实施前工程评审两轮(均含外部独立意见),决定已并入正文;正文为设计时的表述,以代码和 `app/integrations/ecs_callback/` 包说明为准。验证:一次性本地 PostgreSQL 17 容器上全量 1781 通过、22 跳过(15 个 Redis 用例未运行)、0 失败;生产 PostgreSQL 与真实数据迁移、真实 ECS 联调未验证,详见 CHANGELOG「ECS 事件回调接收」。** · 依据:`wes_backend/docs/integration/third_party_integration_whitepaper.md`(v1.1,Approved)§1–2、§4.2、§5;[WMS 回调接收设计](2026-10-22-transport-callback-design.md);[ECS 接入指南](../../integrations/ecs-command.md) · 范围:`POST /api/v1/callback/event` 的接收机制。结果回调、命令出站、状态查询另行设计。

## 1. 意图

2026-10-27后续设计补充：正式业务事件改按`(device_role, event_type)`注册，从设备所属工作线的`device_bindings`解析逻辑角色，再校验该注册项Payload并执行处理器。完整合同见[设备、工作线设计§10.1–§10.6](2026-10-27-device-workline-status-design.md#101-已选定的逻辑角色分发)。该增量已实现，本地验证见[角色增量计划](../plans/2026-10-27-ecs-role-dispatch.md#完成与验证)；本文下述仅event_type注册和示例描述已交付基线，后续角色分发以新合同为准。原wire、事件摘要、先查重、持久收据、ACK及事务规则继续复用。

本次工程评审接缝已补入新合同：解析契约由ECS侧声明，设备/工作线服务实现并在装配期注入，receiver不反向导入app.services；事件与命令复用同一绑定解析实现及“工作线→设备”的锁序。绑定解析在receipt查重之后、Payload校验之前；锁后归属变化/基础设施失败回滚503，稳定未绑定或未注册组合才持久ACK。配置变化停止旧命令后续发送，但不阻止原身份的迟到Result接收；这不改变事件摘要或引入未知事件自动重放。以上已在角色增量中实现并用本地Mock及独立测试库验证，不代表真实ECS联调。

ECS 通过 `POST /api/v1/callback/event` 主动上报设备事件(例如 `SCAN_COMPLETED`)。先把这个入口的**机制**建好:校验、幂等、持久接收、ACK、按 `event_type` 分发。具体事件类型等对照设备合同附录逐个确认后再加,与 `wms_request`、`wms_callback`「先机制、后逐个确认」的做法一致。

**ECS 的 wire 和 payload 一个字节都不改**:路径、字段、`{"code":200,"message":"ACK"}`、`400 INVALID_ENVELOPE` + `issues[]` 都按白皮书。

## 2. 目标、非目标、反目标

目标
- 一个独立入口,按 `event_type` 分发到**函数式处理器**;新增一个事件类型只需要一个文件、一个函数。
- 复用现有回调底座(`callback_receipt`、诊断快照、摘要与 JSON 工具、模块发现、`CallbackPayload`),不复制第二套。
- 对 ECS 可见的行为完全按白皮书。

非目标
- 不实现任何具体 `event_type`;不实现 `ESTOP_PRESSED` 的 `400`、`ECS_TEST_SOURCE_EXPLICIT_DEBUG` 的 `409`、`is_debug=true` 触发的联调命令(都是具体事件的业务规则,依赖出站命令)。
- 不做结果回调、命令出站、状态查询、来源认证(白皮书不规定认证,与 WMS 回调同样靠网关限制来源)。

反目标
- 不把 ECS body 合成 WMS 信封再调 `receive()`。
- 不造假的 UUIDv7。
- 不引入 `Operation` 概念:ECS 事件没有 `operation` 字段,也不使用 `CallbackOperation` 基类(原因见 §4)。
- 不让 `POST /wms/events` 能触达 ECS 的处理器。

## 3. 约束

来自白皮书
- 事件无身份字段。身份是核心字段 `device_code`、`event_type`、`timestamp`、`is_debug` 和 `data`;省略 `is_debug` 等同 `false`,省略 `data` 按空对象。其他顶层字段只作诊断,不进身份。
- ACK 只表示**持久接收**,不夹带下一条命令。
- 最大 256 KiB。错误响应必须含整数 `code` 和字符串 `message`;`400` 必须含 `error_detail.issues[]`,不回显字段值。
- 事件重报与「同毫秒同内容的两个真实事件」无法区分,白皮书 T0 登记为 UNKNOWN。本设计按白皮书的身份规则去重,接受这一已知限制。

来自现有代码(已核对)
- `receive()` 写死 WMS 信封:必须有 UUIDv7 `operation_id`、按 `operation` 查表、422/202 响应,不能承载 ECS wire。
- `callback_receipt` 唯一键 `(operation, operation_id)`,`operation_id` 是 `String(36)`,`resp_timestamp` 非空。
- `CallbackOperation` 只适合 WMS 语义:`parse()` 用 `raise InvalidPayload from None` 吞掉 pydantic 错误,拿不到字段级 `issues[]`;`handle()` 再校验一遍;注册时实例化单例;`is_operation_name` 只接受小写的 `域.名称@vN`,`SCAN_COMPLETED` 过不了;`CallbackContext` 末尾有带默认值的 `notes`,子类追加必填字段会 `TypeError`。
- 诊断快照共用每分钟 120 条的限额(`allow_snapshot`,模块级全局计数)。**评审决定**:`allow_snapshot` 加来源参数,计数、窗口、丢弃数改成按来源的字典,WMS 与 ECS 各自 120 条/分钟,WMS 默认行为不变;需补一个「ECS 高频不影响 WMS 额度」的回归测试。该来源取值同时写入新增的 `callback_snapshot.source` 列(见 §5.4)。

## 4. 方案

入口形态

| 方案 | 做法 | 取舍 |
|---|---|---|
| A 合成信封 | 控制器把 ECS body 包成 WMS 信封,调 `receive()`,再把响应翻译回 ECS 格式 | 要造 `operation_id`(假 UUIDv7 或改共享接收器);400 校验和响应翻译仍要自己写,实际省不了多少 |
| **B ECS 独立入口(选定)** | 独立路由与 `receive_ecs_event`,复用底座 | wire 原样;不碰 WMS 回调代码 |
| C 完全独立 | 自己的表、自己的处理器抽象 | 隔离最干净,但重复收据、保留期清理、诊断 |

处理器形态(2026-10-24 讨论后选定)

| 方案 | 取舍 |
|---|---|
| 继承 `CallbackOperation`,整个事件是 `ecs.event@v1` 这一个 Operation | 外层 Operation 不参与分发,只是收据标签;`Conflict`/`Rejected`/`echo_keys`/有效摘要比较在 ECS 都用不到;§3 列出的 5 个摩擦点要逐个绕开 |
| **ECS 自己的函数式处理器(选定)** | `@ecs_event("SCAN_COMPLETED", data=Model)` 装饰一个 `async def`;处理器可直接调用测试;接收器自己把校验错误翻译成 `issues[]`;只复用 `CallbackPayload` 与诊断落库 |

## 5. 设计

### 5.1 结构

```
app/integrations/ecs_callback/
  __init__.py      # 说明、对外导出
  registry.py      # @ecs_event(event_type, data=Model);EcsEventContext;重复注册报错并指出两个函数
  receiver.py      # receive_ecs_event(session, raw) -> EcsResponse
  envelope.py      # EcsEventEnvelope(pydantic)+ ValidationError → issues[]
  discovery.py     # 导入 app/integrations/<业务>/ecs_event[.py|/];dev_ecs_event 仅 local
app/controllers/ecs_callback.py   # POST /callback/event
```

具体事件类型放在 `app/integrations/<业务>/ecs_event/`,一个事件类型一个文件,文件名是小写的 `event_type`(`SCAN_COMPLETED` → `scan_completed.py`),有测试检查两者一致。**注册时校验(第二轮评审决定)**:`event_type` 必须匹配 `^[A-Z][A-Z0-9_]*$`,不符合在导入期报错;白皮书若将来出现小写或连字符的类型,到时再放宽规则。目录名带 `ecs_` 前缀,避免与 WMS 的 `callback` 目录自动发现混在一起。

### 5.2 处理流程(`receive_ecs_event`)

1. 超过 256 KiB → `413 PAYLOAD_TOO_LARGE`(带 `code`/`message`,与 WMS 入口的空 body 不同)。
2. `json_object` 解析(沿用 strict_json:重复键、NaN、非对象都拒绝)→ 失败 `400`,`issues=[{field:"$", code:"INVALID_JSON"}]`。
3. 用 `EcsEventEnvelope` 校验核心字段(见 §5.3),失败 → `400 INVALID_ENVELOPE` + `issues[]`。
4. 计算身份(§5.4)与 `raw_digest`。**摘要计算失败**(`ValueError`、`RecursionError`;例如 `1e400` 能被 `json_object` 解析成 `inf`,却无法序列化)→ `400 INVALID_ENVELOPE`,`issues=[{field:"$", code:"INVALID_JSON"}]`,不写收据,与 WMS 接收器一致;否则 ECS 会对一个永远不会成功的请求无限重试 `503`。然后**先查重**:收据已存在 → 直接返回首次保存的响应(`200 ACK`),outcome 记 `duplicate`,不再校验 `data`。事件没有 `409`:身份就是内容,同身份必然同内容。这样处理器后来新增或收紧校验,也不会让已 ACK 事件的原样重报变成 `400`(白皮书:相同事件重复上报返回相同 ACK)。
5. 新身份才查处理器:已注册则用它的 `data` 模型校验 `data`,失败 → `400`,`issues` 路径为 `data.<字段>`。**步骤 1–5 没有任何写入,不写收据**。
6. `INSERT … ON CONFLICT DO NOTHING` 写收据(占位 `200 ACK`)。插入冲突(并发同身份)→ 回到步骤 4 的查重,读取已提交的收据。随后在**外层事务**里直接执行处理器(未注册则执行内置的未注册处理,见决定 1),把最终响应写回收据。本版处理器不能拒绝,所以不需要保存点。
7. 处理器抛普通异常,或 PostgreSQL 行锁等待超过 5 秒(沿用 `LOCK_TIMEOUT`)→ 整个事务回滚(收据一并撤销),`503 TEMPORARILY_UNAVAILABLE`,ECS 可原样重报。

DTO 校验放在写收据之前、查重之后,所以占位的 `ACK` 就是最终响应。

### 5.3 核心字段校验与 `issues[]`

`EcsEventEnvelope`:`device_code`、`event_type` 非空 `str`;`timestamp` 严格 `int` 且 `0 ≤ x ≤ 2^63−1`(排除 `bool`);`is_debug` 可选严格 `bool`(`null`、字符串、数字都拒绝);`data` 可选 `dict`(`null` 拒绝);其余顶层字段忽略。

pydantic 错误按下表翻译,只写路径、码和期望类型,**不回显值**。处理器 `data` 模型的错误用同一个翻译函数,路径加 `data.` 前缀:

| 条件 | `code` | `expected` |
|---|---|---|
| 缺字段 | `FIELD_REQUIRED` | — |
| 类型不符 | `INVALID_TYPE` | `string`/`integer`/`number`/`boolean`/`object`/`array` |
| 值不合法(空串、越界) | `INVALID_VALUE` | — |
| 正文不是合法 JSON 对象 | `INVALID_JSON`(路径 `$`) | — |

翻译规则(第二轮评审决定,覆盖 `CallbackPayload` 支持的 `float`、`list`、`Literal`、嵌套模型):
- **路径**:点分字段名,数组下标用 `[i]`,例如 `data.samples[0]`;模型级错误(没有具体字段)用其父路径;`data` 之外的信封字段无前缀。
- **类型映射**:`str`→`string`,`int`→`integer`,`float`→`number`,`bool`→`boolean`,`dict`/嵌套模型→`object`,`list`→`array`;`Literal` 取值不符属于 `INVALID_VALUE`,不带 `expected`。
- **兜底**:未识别的 pydantic 错误类型一律落到 `INVALID_VALUE`。
- **只输出** `field`、`code`、`expected`;绝不透传 pydantic 的 `msg`、`input`、`ctx`,因为它们会回显客户端的值。用嵌套数组错误(如 `samples: list[float]` 的第 0 项)验证路径格式。

简化:根不是对象(如数组)也报 `INVALID_JSON`,不细分成 `INVALID_TYPE`;`json_object` 现在不区分二者。

### 5.4 收据身份

- `operation` = 常量 `"ecs.event"`,只是收据上的来源标签,不进 `wms_callback` 的注册表,所以 `POST /wms/events` 不能触达。
- `operation_id` = 规范化身份的 SHA-256 十六进制全长(64 位):`canonical_digest({device_code, event_type, timestamp, is_debug(默认 false), data(默认 {})})`。
- `raw_digest` = 整个请求体的 `canonical_digest`;`compare_mode="RAW"`,`effective_digest` 为空(满足现有 CHECK 约束)。`resp_timestamp` 必填,取接收时的当前毫秒(不出现在 ECS 响应里)。
- 为此把 `callback_receipt.operation_id` 从 `String(36)` 加宽到 `String(64)`,一个迁移。WMS 的 UUID 身份不受影响,与 64 位摘要也不会相撞。
- 同一个迁移给 `callback_snapshot` 加可空的 `source` 列(第二轮评审决定):ECS 写 `'ecs'`,旧行与 WMS 写入的为空,视为 WMS。快照表原来没有任何字段能区分来源,而 ECS 与 WMS 的 `outcome` 取值重名;`record_snapshot` 相应加 `source` 参数,限额分组与此同一个来源取值。降级时若存在 `source` 非空的行或 64 位身份行,按现有迁移的做法拒绝而不是丢数据。

### 5.5 处理器契约

```python
@ecs_event("SCAN_COMPLETED", data=ScanCompletedData)  # ScanCompletedData 继承 CallbackPayload
async def scan_completed(ctx: EcsEventContext, data: ScanCompletedData) -> None:
    return None  # 业务写入在这里;返回 None 即 ACK
```

- `data=Model` 可省略(白皮书里事件的 `data` 是可选字段):省略时不校验 `data`,处理器收到原始 `dict`(缺省为 `{}`),签名为 `async def handler(ctx, data: dict)`。
- `EcsEventContext` 是独立的 `@dataclass(frozen=True, kw_only=True)`:`session`、`receipt_id`、`digest`、`device_code`、`event_type`、`timestamp`、`is_debug`、`notes`;`ctx.note(reason_code, **detail)` 与 WMS 一致,随收据同事务保存,提交后输出告警日志。
- 处理器在接收器打开的外层事务里运行,不得自行 commit/rollback。**对已通过校验的消息不得抛异常**:冲突、未知设备这类业务情况用 `ctx.note` 记录并返回(仍 ACK);抛异常会让这条消息永远回 503,成为无法越过的毒消息。
- **只写当前数据库事务**(评审决定):不得在处理器里直接调用设备、HTTP 或其他外部服务。外发意图用同事务的 `Outbox.enqueue` 登记,提交后由发送器执行。否则外部动作成功而数据库提交失败时,ECS 原样重报会再触发一次外部动作。
- **业务幂等由处理器自己负责**(评审决定):收据按 `RELIABLE_RETENTION_DAYS`(90 天)清理,清理后同一身份再来会被当作新事件。处理器必须以自己的持久键(业务行)保证重入安全,不能依赖收据。ECS 的最大重报窗口登记为待确认项(TODOS.md)。
- 本版处理器只能返回 `None`,响应恒为 `200 ACK`。具体事件需要特定 `4xx`(如 `ESTOP_PRESSED`)时,在那一步再引入 `EventRejected`,不预留。
- 同一 `event_type` 重复注册报错并指出两个函数。注册表是模块级字典,测试用 `monkeypatch` 换空。

### 5.6 响应

| 情形 | 协议状态 | 正文 |
|---|---|---|
| 接收(新事件、重报、未注册类型) | 200 | `{"code":200,"message":"ACK"}` |
| 信封或处理器 `data` 不合规 | 400 | `{"code":400,"message":"INVALID_ENVELOPE","error_detail":{"issues":[…]}}` |
| 超过 256 KiB | 413 | `{"code":413,"message":"PAYLOAD_TOO_LARGE"}` |
| 事务回滚、行锁超时 | 503 | `{"code":503,"message":"TEMPORARILY_UNAVAILABLE"}` |

### 5.7 控制器

`app/controllers/ecs_callback.py` 照 `wms_events.py` 的写法:`callback_session_factory`、`RateLimit(None)`、`BackgroundTasks` 记录快照。**评审决定**:「分块读取并在 256 KiB 处截断」抽成 `app/core/` 下的共用函数,`wms_events.py` 与本控制器都调用它(WMS 控制器已有的超限 413 测试作为回归保护)。路径 `/callback/event`,挂在 `/api/v1` 前缀下。`app/main.py` 里和 `include_wms_events` 并列注册。Swagger 请求体 schema 显示核心字段,已注册事件类型的 `data` 示例由各模型的 `json_schema_extra` 生成。

### 5.8 可观察性

**最终请求日志由控制器输出**(第二轮外部意见):接收器只返回 `outcome`,控制器在事务提交或异常映射之后才输出这一行。否则提交失败时日志已记 `received`,真实响应和快照却是 `503/error`。每个请求一行结构化日志,字段:`event_type`、`device_code`、`outcome`(`received`/`duplicate`/`invalid`/`unregistered`/`error`)、摘要前 8 位、耗时。`outcome` 同时作为诊断快照的 `outcome`。若以后接指标,`event_type` 来自不可信输入,标签只放已注册类型,未注册的一律记为 `UNREGISTERED`。

**不可信字段的渲染(第二轮评审决定)**:`device_code`、`event_type` 只要求非空 `str`,入口上限 256 KiB,单个字段可近 256 KiB。写入结构化日志和提交后的告警日志时一律用 `repr` 并截到 64 字符(换行被转义,不会伪造日志行);完整值仍保存在收据诊断里(受决定 1 的 32 KiB 上限约束)。不在信封里加长度上限,wire 不变。`ctx.note` 的告警输出走同一个渲染函数。

## 6. 推荐决定(请确认)

### 决定 1:未注册的 `event_type` → `200 ACK`,事件完整存入收据诊断

做法:查不到处理器时走内置的未注册处理:不写业务行,`ctx.note("UNREGISTERED_EVENT_TYPE", device_code=…, event_type=…, data=<规范化后的完整事件>)`。`data` 键随收据同事务落库(`callback_receipt.diagnostic`,保留期随 `RELIABLE_RETENTION_DAYS`),告警日志里剥掉 `data`,沿用 WMS 回调现有行为。

**上限(工程评审决定)**:写入诊断的事件正文最多 32 KiB(规范化 JSON 的字节数)。超过时只存 `{digest, size, preview(前 1 KiB)}`,并输出 error 级日志,ACK 不变。上限约束的是**最终落库的整个诊断**(note 的所有键,不只是 `data`):超限时 note 里只保留摘要、大小、预览,`device_code`、`event_type` 也截到 128 字符,不在其他键里重复保存完整字段(第二轮外部意见:200,000 字符的 `device_code` 曾使整条诊断仍有约 201 KB);测试覆盖「正文很小、设备编号很长」。入口上限是 256 KiB 且不限流,不设上限会让单行收据比现在大约 500 倍;设备事件的 `data` 通常只有几十字节,超限只可能是异常。这是对「完整存入」的有意例外,放大倍数降到约 64 倍。上限和预览大小的取值是我的暂定值。

理由
- 白皮书 ACK = 持久接收,错误表里没有「不认识的事件类型」这一项。回 `400` 会让 ECS 按「修正载荷后重报」处理,但重报永远不会成功;回 `503` 会让 ECS 无限重试。
- 不能只靠诊断快照:它限额每分钟 120 条且可能被丢弃,ACK 却声明「已持久接收」。所以必须把事件本身写进收据。

代价:未知事件类型会无声累积在收据里,只有告警日志可见;要靠告警渠道发现,并在加处理器前评估是否回放(本设计不做回放,回放能力登记为 TODO)。

**发布顺序(评审决定)**:新增事件类型时,先把处理器部署到所有接收实例,再让 ECS 侧启用该事件类型。否则滚动发布期间,还没升级的实例会把新类型按未注册 ACK 并占住身份,升级后的实例收到重报只返回重复,业务效果补不上。这是流程约束,不是机制保证。

### 决定 2:收据身份 → 摘要全长放 `operation_id`,加宽该列到 64

理由:不造假 UUIDv7(`app/core/identifiers.py` 是 UUID 的单一入口,摘要不是 UUID);全长不截断,碰撞概率可忽略;`varchar(36)` 加宽到 `varchar(64)` 在 PostgreSQL 是元数据变更,不重写表。备选(截断成 36 字符)省迁移但无谓缩短摘要,不选。

## 7. 其他我的决定(请核对)

- 包名 `ecs_callback`,事件目录名 `ecs_event`(对应 `wms_callback`/`callback`)。
- 处理器 `data` 校验失败时不写收据;信封非法同样不写。与 WMS 回调(DTO 非法也留收据)不同,原因是 ECS 没有独立身份字段,非法载荷没有可去重的身份。
- **评审决定**:只把诊断队列的入队与提交后输出日志(`wms_callback/receiver.py` 里的 `_log_diagnostics`、`_drop_diagnostics` 及 `_handle` 中入队那一段)提成公共函数,WMS 与 ECS 共用。保存点执行和 `_handle` 本身留在 WMS 接收器,ECS 不用保存点(外部评审指出 ECS 本版没有局部回滚场景)。WMS 回调路径因此被改动,现有 `tests/integrations/wms_callback/`、`tests/controllers/test_wms_events.py` 是回归保护,重构前后必须全绿;还必须包含 `tests/integrations/wms_transport/test_callback.py` 里的 `test_abnormal_callback_alerts_only_after_its_receipt_commits`(`commit_fails` 参数化),它才真正验证「提交后才告警、回滚不告警」。

## 8. 留给实现者

- 迁移文件放在当前 Alembic 头之后,沿用 `migrations/versions/` 的命名;降级时若已有 64 位身份行,按现有迁移的做法拒绝而不是截断;迁移测试沿用 `tests/migrations/test_callback.py` 的 sqlite+PostgreSQL 双参数。
- 测试只固化长期合同,不为填覆盖率加测试:
  - 协议与身份:§5.6 响应表、§5.4 身份等价(省略与显式 `false` 的 `is_debug`、省略与空 `data`、多余顶层字段不改变身份)、`issues[]` 的字段路径、未注册类型的 ACK 与落库(含 32 KiB 上限的两种形态)、`/wms/events` 触达不到 ECS 处理器、迁移往返(sqlite+PostgreSQL)、文件名与注册名一致、重复注册报错。
  - 摘要失败与诊断大小:`{"data":{"n":1e400}}` 回 `400` 且不写收据;「正文很小、设备编号很长」的未注册事件,落库诊断总大小受 32 KiB 上限约束;`issues[]` 对 `list[float]` 的嵌套数组错误给出 `data.samples[0]`、`number`,且不含客户端的值。
  - 先查重:事件先按未注册类型 ACK,随后注册要求某个 `data` 字段的处理器,同一事件原样重报仍得到相同 `200 ACK`。
  - 接收机制的幂等与原子性(PostgreSQL 参数化):并发同身份只执行一次;首请求回滚后等待者能接管;业务写入与收据一起回滚;提交失败不留下 ACK 收据;提交成功但响应丢失后的重报返回同一 ACK。
  - 回归保护(共享代码被改动):`allow_snapshot` 按来源分组后,ECS 高频不占用 WMS 的额度;请求体读取和诊断队列抽取后,现有 WMS 回调测试全绿。

## 9. 风险与未验证

- 决定 1 是从白皮书推断的,需要 ECS 供应商确认「未知事件类型回 200」不会被他们的重报逻辑误判。
- 白皮书 §4.2 要求 `ESTOP_PRESSED` 回 `400`、不写入。评审决定:机制**不**内置这条拒绝(用户:合同如不合理可建议优化,目前不需要接受 ESTOP 的这条规则)。因此在对应处理器出现前,误发的 `ESTOP_PRESSED` 会按未注册类型 ACK 并落库。建议后续与 ECS 供应商确认该条款是否保留。
- 滚动发布窗口:见 §6 的发布顺序。靠流程约束,机制不保证;已被占位的未注册事件没有自动补处理(回放登记为 TODO)。
- 未注册事件超过 32 KiB 只存摘要和预览,是对「持久接收」的有意例外,此类事件无法回放。
- 同毫秒同内容的两个真实事件会被当作重报,第二个被吞(白皮书 T0 已登记为 UNKNOWN)。
- 本设计只对照了代码和文档,没有运行任何东西;`kw_only` 数据类在仓库 Python 版本上的行为、加宽列对 PostgreSQL 的影响未实测。
