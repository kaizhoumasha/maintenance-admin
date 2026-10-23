# WMS 请求端点联调界面开发说明书

状态:**T1~T5 已实施并验证(2026-10-24,见 §10)** · 依据:`app/integrations/wms_request/` 现状、`2026-10-22-transport-dev-console-design.md`、AGENTS.md 分层约定 · 范围:仅 `ENVIRONMENT=local`

## 1. 目标与边界

### 1.1 目标

`wms_request` 已提供 decisions、facts 两个端点和 operation 注册,但没有任何具体 operation,本地也没有办法把它端到端跑一遍:Mock WMS 不认识这两个路径,没有触发入口,没有按 `operation_id` 的状态查询,也看不到已注册了哪些 operation。本说明书补齐四块,全部放在联调控制台里:

1. **operation 目录**:列出已注册的 operation、所属端点、标题、TTL、Payload 的 JSON Schema 和示例。
2. **触发入口**:Swagger 里一个路由,Examples 下拉由注册表生成,选 operation 即带出示例,走真实的 `WmsRequests.send` 入队。
3. **状态查询**:按 `operation_id` 查投递状态、请求与响应原文。
4. **Mock WMS**:认识 `/api/v1/wes/facts`、`/api/v1/wes/decisions`,可切换响应行为。

### 1.2 非目标

- 不新增任何真实 operation。联调用的演示 operation 只在 local 登记。
- 不再改 `WmsRequests.send`(同身份重入语义已由独立小改动处理,见 E4),不改 `app/reliable`,不新增迁移和权限码。
- 不为每个真实 decisions operation 模拟业务数据:Mock 的 `DECIDED` 响应 `data` 恒为 `{}`。
- 非 local 环境这些路由不存在,演示 operation 不会被导入。
- 不做 operation 目录的生产接口。

## 2. 已核实的现状(设计前提)

| # | 事实 | 来源 |
|---|---|---|
| E1 | `get_session` 只 `yield`,不提交;不提交的话发送器看不到消息 | `app/core/database.py`、`dev_submit_service.submit` |
| E2 | `WmsRequests.send` 在调用方事务内入队,不提交;没有默认单例,`outbox` 与目标从 `RELIABLE_TARGETS` 惰性取得 | `wms_request/service.py` |
| E3 | `send` 入队的消息 `business_key` 为空;`ReliableMessage` 的唯一键以 `target` 开头,另有 `business_key` 索引,没有只含 `operation_id` 的索引 | `models/reliable.py`、`service.py` |
| E4 | `send` 每次调用都用当前时间生成 `timestamp`,所以同一 `operation_id` 再次调用的字节必然不同;原先因此一律抛 `DuplicateMessage`。**已由独立的小改动处理**:同身份、同 Payload(忽略 `timestamp`)返回原消息引用,Payload 不同才抛 `DuplicateMessage` | `service.py`、`Outbox.enqueue` |
| E5 | `lookup()`、`operations()` 读注册表前自动加载业务包的 `request` 模块 | `wms_request/operation.py` |
| E6 | 联调控制台只在 local 挂载,`include_dev_routers` 扫描每个 `app/integrations/<业务>/dev.py`;`wms_request` 目前没有 `dev.py` | `controllers/dev.py` |
| E7 | Mock WMS 只有 Transport 提交一个接收路径,模式存进程内存、全局共享,需单 worker | `controllers/dev_mock_wms.py` |
| E8 | 分层测试:`app/integrations` 不得导入 fastapi、controllers、services、schemas;controllers、services 可导入 integrations | `tests/architecture/test_layering.py` |
| E9 | `get_current_user` 保护联调路由,不用 `require_permission` | `controllers/dev.py` |
| E10 | 适配器对两个端点的响应分类:成功码、`503`、带 `retry_after_ms` 的 `429`、带合法 `reason_code` 的 `409`/`422`,其余为未知 | `wms_request/adapter.py` |
| E11 | 判别联合体验证(2026-10-24 临时原型,FastAPI 0.143.1 / Pydantic 2.14.1):以 `operation` 为判别字段、值含 `@` 的 `Literal` 可用;`/openapi.json` 得到 `oneOf` 加 `discriminator.mapping`;非法值和类型不符均为校验失败;单个成员时也正常 | 原型 |
| E12 | 同上原型:各 operation 的内嵌 `Payload` 同名时,OpenAPI 自动改用带模块路径的 schema 名,不冲突,但名字难看(如 `app__integrations__x__Payload`) | 原型 |

## 3. 架构(满足 E8)

```text
app/integrations/wms_request/operation.py   RequestOperation 增加 title、example;注册项增加 name、describe()
app/integrations/wms_request/dev.py         演示 operation(example.fact.demo@v1、example.decision.demo@v1),仅 local 被发现
app/integrations/wms_request/service.py     新增默认实例 wms_requests(与 transport_service 对称)
app/schemas/dev_console.py                  新增 DevOperationOut、DevRequestOut;MockWmsMode 增加 busy、prepare_accepted
app/services/dev_submit_service.py          抽出 DevMessageOut 的构造,message_status 与新查询共用
app/services/dev_wms_request_service.py     send(调 wms_requests.send 并提交、映射错误)、message_status_by_operation_id
app/controllers/dev_wms_request.py          目录、触发、状态三个路由
app/controllers/dev_mock_wms.py             新增 facts、decisions 两个接收路径
app/controllers/dev.py                      include_dev_routers 里挂载新路由,并对缺少的目标给出 WARNING
```

依赖方向:`controllers → services → integrations/core/models`;`integrations/wms_request/dev.py → 本包`。`wms_request` 包内不导入 fastapi。

## 4. 设计

### 4.1 注册表的描述能力(operation 目录的唯一来源)

`RequestOperation` 增加与入站 `CallbackOperation` 同名的两个可选属性:

- `title: ClassVar[str | None]`:Swagger 里的标题,缺省用 operation 名。
- `example`(属性):取 `Payload.model_json_schema()["examples"][0]`,没有则为 `None`。

注册时校验:有 `example` 的 operation,其示例必须通过自身 `Payload` 校验,否则抛 `ValueError`(导入期暴露,沿用 `dev_scenario` 的做法)。

`Registered` 增加 `name` 字段和 `describe() -> dict`,返回可直接序列化的:

```python
{"name", "endpoint", "title", "ttl_seconds", "payload_schema", "example"}
```

联调目录、触发入口的下拉、以后可能有的合同覆盖导出,都只读 `describe()`。

### 4.2 演示 operation(`wms_request/dev.py`)

`example.fact.demo@v1`(facts)和 `example.decision.demo@v1`(decisions),字段 `task_id: str`、`qty: int`,各带示例。文件头注释按 `wms_callback/dev_callback.py` 的写法,说明"照着它写一个真实 operation"。

它们不在 `request/` 目录里,所以不受"文件名等于注册名"测试检查;只在 local 被 `_load_dev_modules` 导入,生产进程不会注册它们。

### 4.3 路由(`controllers/dev_wms_request.py`,前缀 `/dev/wms/requests`,标签 `dev-wms-request`)

| 路由 | 说明 |
|---|---|
| `GET /operations` | 返回 `list[DevOperationOut]`,每项为 `describe()` |
| `POST ""` | 请求体 `{operation, operation_id?, data}`;`operation` 为判别字段,每个已注册 operation 一个成员,`data` 为其 `Payload`;Examples 来自 `example`,没有示例的 operation 不出现在下拉但仍可提交;返回 `DevRequestOut{operation, operation_id, message_id, status, target}`(`EnqueueResult` 不含目标名,`target` 由 `wms_requests.target(协议)` 得到) |
| `GET /status/{operation_id}` | 返回 `list[DevMessageOut]`,按 `operation_id` 查,没有则 404 |

约定:

1. 判别字段写在 `Body(discriminator="operation")` 上,不要放进 `Annotated` 的 `Field`(沿用 Transport 控制台的结论)。
2. 生成的请求模型名为 `DevWmsRequest` 加 operation 名各段(`.`、`@`、`_` 都当分隔符,版本号保留),避免 `v1`、`v2` 同名,也避开 Transport 场景已有的 `Dev*` 模型名。
3. `operation_id` 不传则服务端生成 uuid7。
4. 注册表在 local 至少有演示 operation,所以路由总会挂载;若为空则不挂载,和 `scenarios()` 为空时一致。
5. 触发先调用 `wms_requests.send`,成功后 `commit`(E1)。

### 4.4 状态查询

`dev_submit_service.message_status` 里把 `ReliableMessage → DevMessageOut` 的构造抽成一个函数,两个查询共用。新查询 `WHERE operation_id = :id`。因为没有对应索引(E3),这是 local 小数据量下的全表扫描,不为此加索引或改 `send` 去设置 `business_key`。

### 4.5 Mock WMS

新增两个接收路径,路径从 `ADAPTERS[协议].path` 取,不再写一份字面量。无认证,原因同现有 Transport 路径:发送器不带 token。

信封无法解析(不是 JSON、没有 `operation_id`、`data` 不是对象)返回空 `400`。否则按全局模式响应:

| 模式 | facts | decisions | Transport 提交(现有) |
|---|---|---|---|
| accept | 200 RECORDED `{}` | 200 DECIDED `{}` | 202 RECEIVED(不变) |
| duplicate | 200 DUPLICATE `{}` | 200 DECIDED `{}`(合同要求重放首次决定) | 200 DUPLICATE(不变) |
| prepare_accepted(新) | 同 accept | 202 PREPARE_ACCEPTED `{}` | 同 accept |
| conflict | 409 CONFLICT `{reason_code: IDEMPOTENCY_CONFLICT}` | 同左 | 不变 |
| reject | 422 REJECTED `{reason_code: INVALID_DATA}` | 同左 | 不变 |
| unavailable | 503 UNAVAILABLE `{}` | 同左 | 不变 |
| busy(新) | 429 BUSY `{retry_after_ms: 1000}` | 同左 | 429 BUSY,适配器判为未知 |
| garbage、hang | 与现有行为一致 | 同左 | 不变 |

Transport 提交路径的现有行为一个字都不改,只给 `busy` 补一行,`prepare_accepted` 沿用 accept。`DECIDED` 的 `data` 恒为 `{}`,真实 decisions operation 的业务数据不模拟(§1.2)。

### 4.6 本地配置

两个目标指向本应用自身,每个协议恰好一个(`WmsRequests.target` 的要求):

```bash
RELIABLE_TARGETS='{"targets":{"wms-facts":{"base_url":"http://127.0.0.1:8000","protocol":"wms-facts"},"wms-decisions":{"base_url":"http://127.0.0.1:8000","protocol":"wms-decisions"}}}'
```

`include_dev_routers` 挂载时,对没有目标的 `wms-facts`、`wms-decisions` 各输出一条 WARNING,带上面的配置示例。现有"完全没有目标"的 WARNING 保留。

## 5. 错误与运行约定

| 情况 | 响应 |
|---|---|
| 请求体不合法、`operation` 缺失或未知、Payload 不符、多余字段 | 400(全局映射) |
| `operation_id` 不是小写 canonical UUIDv7 | 400 |
| 同一 `operation_id`、同 Payload 再次触发(E4) | 200,返回同一 `message_id`,不新增消息 |
| 同一 `operation_id`、不同 Payload | 409 |
| 状态查询没有该 `operation_id` | 404 |
| 未登录 | 401 |
| 未配置对应协议的目标 | 500;挂载时 WARNING 已给出配置示例 |

## 6. 安全

- 仅 local 挂载;非 local 在导入阶段就不加载 `dev` 模块,演示 operation 不注册。
- 只依赖 `get_current_user`(E9),该取舍只在 local 成立,严禁放宽 `ENVIRONMENT` 判断。
- Mock WMS 的两个新接收路径无认证,与现有提交路径一致,同样只在 local 存在。
- 全局限流与审计中间件照常生效。

## 7. 测试(只固化长期合同)

1. **非 local 不挂载**:现有"production 下应用没有任何路径"的测试覆盖新路由,不新增。
2. **注册表描述规则**:对注册表里全部 operation 断言 `describe()` 的 `name`、`endpoint` 合法,有示例的能通过自身 Payload 校验;登记一个示例不合法的 operation 抛 `ValueError`。规则式,不枚举 operation。
3. **目录**:`GET /operations` 包含演示 operation,每项含端点、示例和 Schema。
4. **触发即可见**:对每个有示例的 operation 用其示例触发,得到的消息的 `target` 属于该 operation 所属端点,用全新会话能查到 `PENDING` 消息,状态接口返回信封原文。
5. **错误**:Payload 不符 400;非法 `operation_id` 400;同号同 Payload 重放返回同一 `message_id` 且只有一行消息,同号不同 Payload 409;未登录 401;未知 `operation_id` 状态 404。
6. **Mock 与适配器一致**:按端点和模式组合,用 Mock 的实际响应交给 `ADAPTERS[协议].classify_response`,断言结论(DELIVERED、PEER_TERMINAL、NOT_RECEIVED、UNKNOWN)。沿用 `test_dev_console.py` 的参数化写法。
7. **Transport 不回归**:`busy` 以外的模式下,现有 Transport 提交路径响应不变(现有测试已覆盖,不新增)。

不测:示例字段内容、响应字段快照、Swagger 页面渲染、私有 helper。

## 8. 实施任务与验收

| 阶段 | 任务 | 验收 |
|---|---|---|
| T1 | `operation.py` 的 `title`、`example`、`name`、`describe()`;`service.py` 的 `wms_requests` 默认实例;`dev.py` 演示 operation | §7-2 通过;`ruff`、分层测试仍绿 |
| T2 | `schemas`、`dev_submit_service` 抽取、`dev_wms_request_service`、`dev_wms_request.py`、`dev.py` 挂载与 WARNING | §7-3、§7-4、§7-5 通过 |
| T3 | `dev_mock_wms.py` 两个新路径与两个新模式 | §7-6 通过;现有 `test_dev_console.py` 不改动且通过 |
| T4 | README 联调控制台小节补配置示例和路由说明;`wms_request/__init__.py` docstring 补联调说明 | 文档与路由一致 |
| T5 | **浏览器验收**:local 起服务,Authorize 后在 Swagger 操作 | Examples 下拉出现两个演示 operation;触发后在状态接口看到 `PENDING`,发送器自调用 Mock 后变 `DONE`;切换 `unavailable`、`busy` 看到重试 |

收尾:`uv run ruff check . && uv run ruff format --check . && uv run pytest`。

## 9. 评审记录

### 9.1 验证

- **V1 通过**:以 `operation` 为判别字段、值含 `@` 的判别联合体,OpenAPI 产出 `oneOf` 加 `discriminator.mapping`;非法 `operation`、`data` 类型不符均校验失败;单成员也正常。
- **V2 观察**:各 operation 内嵌 `Payload` 同名时 schema 名自动带模块路径,不冲突,仅名字难看(E12)。接受。
- **未验证**:Swagger UI 在浏览器里的真实渲染(T5);Mock 与真实发送器的端到端自调用(T5);PostgreSQL 下的行为(联调控制台只在 local,用 SQLite 与本地 PostgreSQL 均可,未实测)。

### 9.2 评审发现与处理

| # | 级别 | 发现 | 处理 |
|---|---|---|---|
| F1 | 高 | E4:`send` 每次用新时间戳,同一 `operation_id` 再次调用必抛 `DuplicateMessage`。联调界面因此无法演示"同身份同内容重放",生产调用方也不能靠重复调用 `send` 实现幂等重入 | 已决定并实施为独立小改动:同身份、同 Payload 返回原引用(`canonical_digest` 比较 `data`,区分 `1` 与 `1.0`),不同 Payload 仍抛 `DuplicateMessage`;本说明书据此更新 §5、§7 |
| F2 | 中 | `wms_request` 当前没有 `title`、`example`,目录与下拉无物可读 | §4.1 补齐,并与入站侧同名对齐 |
| F3 | 中 | 示例若不合法,只有点开 Swagger 才暴露(与 Transport 控制台 F7 同类) | §4.1 注册期校验 + §7-2 |
| F4 | 中 | 本地只配 Transport 目标时,触发会得到难定位的 500 | §4.6 挂载时对缺失的协议给出 WARNING 与配置示例 |
| F5 | 中 | Mock 的 `DECIDED` 恒为 `{}`,与真实 decisions 的闭集业务数据不同,可能让人误以为已验证响应解析 | 写入 §1.2 与 §4.5;真实 decisions operation 加入时再设计 Mock 数据来源,不预留扩展点 |
| F6 | 中 | 路径字面量若在 Mock 里再写一份,会与适配器漂移 | §4.5 从 `ADAPTERS[协议].path` 取 |
| F7 | 中 | 抽取 `message_status` 的构造函数会碰到已有代码和测试 | 只抽函数体,行为不变,`test_dev_console.py` 不改动作为验收(T3) |
| F8 | 低 | 按 `operation_id` 查没有索引(E3) | local 小数据量,不加索引、不改 `send`(§4.4) |
| F9 | 低 | Mock 模式是进程内全局,三个接收路径共用一个开关,需单 worker | 沿用现有约定并写入 README;不拆成每端点一个开关 |
| F10 | 低 | 演示 operation 与真实注册表同处一个全局,可能被误认为生产 operation | 放在 `dev.py`,名字前缀 `example.`,不在 `request/`,不受文件名测试检查,生产不导入(§4.2、§6) |
| F11 | 低 | 触发入口与 Transport 的 `POST /dev/transport/tasks` 是两个入口,Swagger 里有两处"下拉" | 接受:Transport 的场景是业务封装,这里是裸 operation,抽象层次不同;标签分开(`dev-transport`、`dev-wms-request`) |
| F13 | 低 | Examples 下拉和请求模型在应用启动时由注册表生成,之后才注册的 operation 不会出现 | 接受:operation 都是启动期导入注册的;目录接口是实时读注册表,可以对照 |
| F14 | 低 | `EnqueueResult` 只有 `message_id` 和 `status`,`DevRequestOut.target` 不能直接取 | §4.3 注明 `target` 的来源 |
| F15 | 低 | 生成的请求模型若叫 `Dev*`,可能和 Transport 场景模型同名 | §4.3 约定 2 加 `DevWmsRequest` 前缀 |
| F12 | 信息 | `operations()` 在 `include_dev_routers` 里会触发业务包 `request` 模块的导入,导入错误会让 local 应用启动失败 | 有意为之:operation 缺失不应静默,与 `_load_dev_modules` 的做法一致 |

### 9.3 待用户决定

1. ~~是否让 `send` 在同身份、同 Payload 时返回原消息引用~~ **已决定:做,作为独立小改动**(F1)。
2. 触发入口的路径是否沿用 `/dev/wms/requests`(本文)还是并入现有 `/dev/transport/tasks` 的场景体系(F11)。建议保持独立。

### 9.4 评审局限

评审方式:作者逐条对照代码核实 E1 至 E12(含 `DuplicateMessage` 的触发条件、唯一键列、`ADAPTERS` 的 `path` 属性、分层测试规则、`production` 下无路由的现有测试),并做了 §9.1 的原型。未请独立评审者;尚无实现,未运行;Swagger UI 渲染、Mock 自调用、PostgreSQL 行为未实测。

## 10. 实施与验收记录(2026-10-24)

- **实现**:`wms_request/operation.py`(`title`、`example`、`name`、`describe()`、示例注册期校验)、`wms_request/dev.py`(演示 operation)、`wms_request/service.py`(默认实例 `wms_requests`)、`schemas/dev_console.py`、`services/dev_submit_service.py`(抽出 `message_out`)、`services/dev_wms_request_service.py`、`controllers/dev_wms_request.py`、`controllers/dev_mock_wms.py`、`controllers/dev.py`(挂载与 WARNING)。describe 的 Schema 字段最终命名为 `payload_schema`。
- **`send` 的同身份重入**(F1 的独立小改动):最初在 `WmsRequests.send` 里捕获 `DuplicateMessage` 后比较;全面 review 后改为 `Outbox` 的可选适配器端口 `same_request`,`WmsEndpointAdapter` 用 `canonical_digest` 比较 `operation_id`、`operation`、`data`(忽略 `timestamp`,区分 `1` 与 `1.0`),没有该方法的适配器仍要求字节完全相同。
- **review 后的修订**:联调请求体的 `data` 改为按 JSON 模式校验再以 Python 对象交给 `send`(兼容 datetime 等字段);`InvalidMessage` 映射为 400;Schema 缓存;`read_ack` 与目标查找辅助函数与 Transport 共用。
- **测试**:全量 `1247 passed, 366 skipped`,`ruff` 通过。新增 `tests/controllers/test_dev_wms_request.py`(§7-3 至 §7-6)、`test_operation.py` 的描述与示例校验、`test_service.py` 的重入用例。
- **本地端到端验收**(独立 SQLite 与 Redis 库 9,端口 8765,`wms-facts` 配 `max_not_received_attempts=3`):
  - facts 触发后发送器自调用 Mock,消息 `DONE`,响应 `RECORDED`。
  - decisions 在 `prepare_accepted` 下得 `202 PREPARE_ACCEPTED`,判为已送达。
  - `busy` 与 `unavailable` 下判为未送达,达到预算后 `STALLED`;同 `operation_id`、同 Payload 再触发返回同一 `message_id`。
  - Swagger UI 中出现 `dev-wms-request` 标签与三个路由,POST 的 Examples 下拉显示 operation 标题。
- **未实测**:PostgreSQL 下的联调控制台(验收用 SQLite);Examples 下拉选择后请求体自动填充的交互、`oneOf` 之外的页面细节只看了标签与下拉。
