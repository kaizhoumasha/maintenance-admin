# ECS 同步请求接收设计

状态:**待实施。** 本文只定义 WES 基础架构的第四类接入。开箱机业务、具体 `request_type` 和 WMS operation 不在本文。

依据:[ECS 事件回调接收设计](2026-10-24-ecs-event-callback-design.md)、[接入说明](../../integrations/registration.md)、[出站结果](../../integrations/outbound-results.md)。代码以 `app/integrations/ecs_callback/`、`app/integrations/callback_receipts.py`、`app/integrations/discovery.py`、`app/businesses/catalog.py` 为准。

## 1. 意图

ECS 有少数交互必须在同一次 HTTP 响应里拿到已经落盘的业务决定。现有 `POST /api/v1/callback/event` 的成功响应固定为 `{"code":200,"message":"ACK"}`，处理器运行在接收事务内，不能发 HTTP。这个入口继续只表示持久收件。

新增 `POST /api/v1/callback/request`。它接收 ECS 的同步请求，在决定写入收据之后返回该决定。等待 WMS 发生在事务外，仍由现有 Outbox 和唯一 Sender 投递。

## 2. 目标与非目标

目标

- 增加与 `@ecs_event` 平行的 `@ecs_request`。业务文件放在 `app/businesses/<业务>/ecs_request/`，由 `composition.ENTRY_MODULES` 显式列入。
- 当场可以决定的请求，在第一段事务提交后返回 `DECIDED`。
- 需要 WMS 的请求，先登记原出站消息并提交，释放锁后再等待公开 `get_result`。
- 同一身份重报返回已保存的正文。先写入的正文保持不变。
- 决定尚未落盘时返回 503。Outbox 继续投递，业务原有 `scan_pending` 继续推进。

非目标

- 不实现开箱机，不登记 `CARTON_VALIDATE` 或其他业务请求。
- 不修改 `receive_ecs_event` 的响应、身份和事务。
- 不新增 WMS 客户端、Sender、重试调度或配置项。
- 不解释 `ALLOW`、`REJECT`、`HOLD`。这些字段属于业务响应模型。

反目标

- 不把同步决定塞进 `/callback/event`。
- 不占用 `app/businesses/<业务>/request/` 和 `@request_operation`。那一组只表示 WES 调用 WMS。
- 不在持有工作线或设备锁时等待 WMS。
- 不把超时、未知交付或未注册请求写成 `DECIDED`。

## 3. 已核对的约束

- 事件身份是 `device_code`、`event_type`、`timestamp`、`data` 的 `canonical_digest`。收据 operation 为 `ecs.event`，协议 `ecs-event`，`interaction_kind="EVENT"`。
- `insert_receipt(..., defer_response=True)` 可以先落请求、暂不写回复。`save_receipt_response` 随后写入回复，但在 `make_current=True` 时会覆盖已有回复。同步请求必须先锁行，只有回复不存在或字节相同才写入。现有 `find_receipt` 对 `reply_wire=NULL` 会报证据完整性错误，因此同步请求必须增加能返回“尚无回复”的 pending receipt 读取路径，不能直接复用只读完成收据的 helper。
- `WmsRequests.send` 只在调用方事务内登记 Outbox。`app.integrations.wms_request.results.get_result` 按 `message_id`、`operation`、`operation_id` 读取终局证据。`response_state` 为 `NONE`、`VALID`、`INVALID`、`UNAVAILABLE`。
- `TargetConfig.timeout_ms` 的默认值是 `10000`，定义在 `app/reliable/config.py`。具体可靠目标可以覆盖它。Sender 使用 `ReliableConfig.poll_interval_s`，默认 `1` 秒。仓库里没有 `ReliableTarget` 这个类型。
- catalog 的 kind 现为 `ecs_event`、`callback`、`request`。同步 ECS 请求新增独立 kind `ecs_request`，不能复用表示 WES → WMS 的 `request`。`entry_modules` 在 local 环境按 kind 追加 `dev_ecs_event`、`dev_callback`、`dev_ecs_request`、`dev`。发现器、`validate_business_contracts` 与 `validate_registration_owners` 必须使用同一组 kind。
- 注册文件名规则是 `registration_filename(name)`。入口目录名必须在 `app/integrations/registration.py` 的 `_entrypoints` 中，否则逐项文件名校验不生效。
- 集成历史 `interaction_kind` 已允许 `REQUEST`。ECS HTTP 投影 `EcsHttpExchange` 目前只允许 `COMMAND`、`RESULT`、`EVENT`、`STATUS`。
- 事件控制器从事务中读取 `business_event_committed` 与 `business_committed`，并在提交后由 `after_commit` 唤醒。正式事件要求 `app.state.ecs_event_binding_resolver`。

## 4. 协议

### 4.1 请求

`POST /api/v1/callback/request`

正文上限 256 KiB，与事件相同。信封：

| 字段 | JSON 类型 | 约束 |
| --- | --- | --- |
| `device_code` | string | 非空。额外顶层字段忽略，不进入身份 |
| `request_type` | string | 非空。注册名仍须匹配 `^[A-Z][A-Z0-9_]*$` |
| `timestamp` | integer | `0` 到 `2**63 - 1`。重报保持原值 |
| `data` | object | 省略时按 `{}` |

身份是这四个字段的 `canonical_digest`。收据 `operation` 固定为 `ecs.request`，`operation_id` 为该摘要。协议名为 `ecs-request`。它与 `ecs.event` 不共用身份。

同毫秒、同内容的两个真实请求与重报无法区分。后到者返回先保存的决定。

### 4.2 响应

决定已经写入收据时，返回当时保存的原始字节。新决定的正文按下面的形状生成，JSON 使用 `ensure_ascii=False`、`separators=(",", ":")`、`allow_nan=False`，再编码为 UTF-8：

```json
{"code":200,"message":"DECIDED","data":{}}
```

`data` 是业务响应模型校验后的对象。平台 `message` 只有 `DECIDED` 表示成功决定。业务上的允许、拒绝和挂起都放在 `data` 内，HTTP 状态仍是 200。

其他响应不写入决定正文：

| HTTP | `message` | 条件 |
| --- | --- | --- |
| 400 | `INVALID_ENVELOPE` | 信封或 `data` 不合法。含 `error_detail.issues[]`，不回显字段值。不写收据 |
| 400 | `UNKNOWN_REQUEST` | 角色与 `request_type` 没有已注册处理器。不写收据 |
| 403 | `FORBIDDEN_SOURCE` | 沿用事件的来源授权钩子，且钩子拒绝。不写收据 |
| 413 | `PAYLOAD_TOO_LARGE` | 正文超过 256 KiB。不写收据 |
| 503 | `TEMPORARILY_UNAVAILABLE` | 决定尚未落盘、事务失败或等待预算用尽 |

503 不表示业务拒绝。ECS 按原请求重报。

未注册请求不落收据，是为了让后部署的处理器仍能接住原请求。因此处理器必须先于 ECS 启用该 `request_type` 发布到全部接收实例。

## 5. 注册

```python
@ecs_request(
    "CARTON_VALIDATE",
    device_role="STATION_SCAN1",
    data=CartonScan,
    response=CartonDecision,
)
async def carton_validate(ctx: EcsRequestContext, data: CartonScan) -> None: ...
```

上面的名称只说明形状。本文不注册它。

- `data` 使用 `CallbackPayload`，冗余字段忽略。
- `response` 是严格 Pydantic 模型，`extra="ignore"`。它不是 `CallbackPayload`。
- 注册键是 `(device_role, request_type)`。重复注册报错并指出双方来源。
- 文件名遵循 `registration_filename`。`CARTON_VALIDATE` 对应 `carton_validate.py`。
- 处理器是 `async def handler(ctx, data) -> None`。返回值被忽略。
- 导入期校验名称、角色、文件名、协程签名和 catalog 归属。未列入正式 `ENTRY_MODULES` 的业务来源阻止启动。
- `@ecs_request` 只接受入口目录 `ecs_request` 或 `dev_ecs_request`。`@request_operation` 只接受 `request` 或 `dev`。目录不符时导入失败，不写入任一注册表。

`EcsRequestContext` 至少包含事件上下文里处理器已经依赖的会话、收据、摘要、设备、时间戳、绑定、runtime、业务码和组件键。请求类型字段名为 `request_type`。另外提供：

- `deferred: DeferredRequest | None`：收据上已经记下的等待身份。`DeferredRequest` 只有 `message_id: int`、`operation: str`、`operation_id: str`。首次进入为 `None`。
- `respond(payload) -> None`：校验 `payload` 为声明的响应模型，并标记本事务要保存的决定。
- `defer(message_id: int, operation: str, operation_id: str) -> None`：标记本事务要等待的原出站消息。

同一次调用只能有一个结果。第二次 `respond` 或 `defer`，以及 `respond` 与 `defer` 混用，都抛出 `ValueError`。相同等待身份重复 `defer` 视为同一次。`respond` 的模型对象必须与本注册项的 `response` 类型一致。

处理器只使用 `ctx.session`。它可以调用 `WmsRequests.send` 和 `get_result`。它不提交、不回滚、不升级隔离级别，也不打开第二段事务。

推荐的处理器顺序：

```python
if ctx.deferred is None:
    reference = await capabilities.wms.send(ctx.session, model, operation_id=operation_id)
    ctx.defer(reference.message_id, operation, operation_id)
    return
result = await capabilities.wms.get_result(
    ctx.session,
    message_id=ctx.deferred.message_id,
    operation=ctx.deferred.operation,
    operation_id=ctx.deferred.operation_id,
)
if result.delivery_uncertain or (result.response_state == "NONE" and not result.delivery_terminal):
    ctx.defer(ctx.deferred.message_id, ctx.deferred.operation, ctx.deferred.operation_id)
    return
ctx.respond(decision_from(result))
```

当场已经能够决定时，处理器直接 `respond`，不调用 `defer`。

`stored_response` 只在投递状态为 `DONE` 或 `FAILED` 时给出 `VALID`、`INVALID` 或 `UNAVAILABLE`。`EXPIRED` 和 `ABANDONED` 的 `response_state` 仍是 `NONE`，但 `WmsRequestResult.delivery_terminal` 为真。结果尚未终局，或 `delivery_uncertain` 为真时，处理器只能 `defer` 同一身份。终局之后必须 `respond`，包括 `VALID`、`INVALID`，以及终局但仍是 `NONE` 或 `UNAVAILABLE` 的结果。业务上的拒绝和挂起放在响应模型里。终局后再次 `defer`，或两者都不调用，按第 6.3 节记 `REQUEST_HANDLER_INCOMPLETE`。

## 6. 接收状态机

控制器沿用事件的会话工厂。等待期间不持有该会话。

```text
accept(session, raw) -> Final | Deferred
等待只在 Deferred 之后、会话关闭之后进行
continue(session, deferred) -> Final
```

`accept` 与 `continue` 都不提交。控制器的 `session.begin()` 负责提交。

### 6.1 accept

顺序与事件接收器对齐：大小和 JSON、信封、身份、绑定解析、来源授权、注册查找、`data` 校验、写收据、执行处理器。

绑定解析复用 `app.state.ecs_event_binding_resolver`，锁序仍是工作线然后设备。共享组件的输入目标复用 `app.state.ecs_event_input_target`。来源授权复用 `app.state.ecs_event_authorizer`。

查重命中且回复正文已存在：返回该正文，`outcome="duplicate"`，不执行处理器。

查重命中但回复正文不存在：把已保存的等待身份放进 `ctx.deferred`，再次执行处理器。

新身份调用 `insert_receipt`。`insert_receipt` 会弹出 `body`，`CallbackReceipt.status_code` 和 `resp_timestamp` 也不能空。等待行传入 `status_code=503`、当前毫秒 `resp_timestamp`，以及一份不返回给 ECS 的 503 JSON `body`，并设 `protocol="ecs-request"`、`interaction_kind="REQUEST"`、`operation="ecs.request"`、`defer_response=True`。因此 `reply_wire` 为空。对外响应只读取 `reply_wire`：为空就是 503，不凭 `status_code` 返回成功。

同一事务写入与事件接收器相同的绑定快照：`owner_domain`、`component_key`、`work_line_id`、`device_id`、`device_role`、`device_version`、`work_line_version`。`continue` 按这个快照锁原工作线和原设备，锁序仍是工作线然后设备。绑定后来发生变化时不改锁另一行。原行已经不存在则回滚本段事务，收据保留，对外 503。

处理器结束后：

- `respond`：在同一事务锁住收据。回复为空则用 `save_receipt_response` 写入 `DECIDED` 字节；摘要相同则保持原字节；摘要不同则不覆盖，处理器结果改为冲突，本事务仍提交原正文。函数向控制器返回 `Final`。
- `defer`：在同一事务确认 `ReliableMessage` 可见，且 `message_id`、`operation`、`operation_id` 一致。在已有绑定快照上写入 `event_context["deferred_request"]`，不整块替换 `event_context`。不写 `reply_wire`。函数向控制器返回 `Deferred`。
- 两者都没有：抛出异常，控制器回滚，对外 503，不留收据。`insert_receipt` 返回空时走查重路径，不得再次 `send`。

正式业务处理器在事务内登记现有的 `after_commit` 唤醒，回调分别来自 `business_event_committed` 和 `business_committed`。发布失败不改变收据和已经提交的出站消息。

### 6.2 等待

控制器提交并关闭第一段会话后，才对 `Deferred` 等待。一个 HTTP 请求只等待一轮。

- 预算是一次 Sender 轮询加上该出站消息所属目标的读超时：`ReliableConfig.poll_interval_s * 1000 + TargetConfig.timeout_ms`。目标无法解析时用 `1000 + 10000`。不新增配置，不延长 Outbox 的 TTL。一个 HTTP 请求只等待这一轮，因此会占用一个 API 工作单元；这条路由只给少量必须当场决定的请求，高频事实仍走 `/callback/event`。
- 每 200 毫秒打开一次短会话，读取两处事实：收据是否已有回复；`get_result` 的当前投影。睡眠发生在会话外。
- 回复已存在：再打开一段事务，走查重返回，不执行处理器。
- `response_state` 为 `VALID` 或 `INVALID`：结束等待，进入 `continue`。
- `delivery_terminal` 为真且 `delivery_uncertain` 不为真：结束等待，进入 `continue`。这包括 `EXPIRED`、`ABANDONED`，以及投影仍是 `NONE` 或 `UNAVAILABLE` 的终局失败。处理器必须 `respond`。
- `delivery_uncertain` 为真，或 `response_state` 仍是 `NONE` 且投递尚未终局：继续等待。
- 预算用尽：返回 503。收据保留等待身份，`reply_wire` 仍为空，`status_code` 仍是 503。Sender 继续按原消息投递。

等待循环不调用 `WmsRequests.send`，不读取业务表。

### 6.3 continue

新会话在第一条业务锁之前沿用绑定解析器的原锁序。它再次调用同一个处理器。

- 处理器 `respond`：按 6.1 的先写入规则保存，`status_code` 改为 200，并返回 `DECIDED`。
- 结果尚未终局时，处理器再次 `defer` 同一身份：提交等待身份，返回 503。本轮 HTTP 不再等待。
- 结果已经终局，处理器仍 `defer` 或两者都不调用：回滚本段事务，收据不变，诊断记 `REQUEST_HANDLER_INCOMPLETE`，返回 503。
- 处理器 `defer` 另一条消息：回滚并返回 503。不改已保存的等待身份。

业务推进可以在另一条会话里调用同一个 `respond` 保存路径。HTTP 等待和业务推进谁先提交，谁的正文生效。后到的相同字节保持成功。后到的不同字节不覆盖，并在该事务的诊断里记下 `REQUEST_DECISION_CONFLICT`。

## 7. 持久化

收据复用 `callback_receipt` 与集成历史，不新建业务表。

| 字段 | 取值 |
| --- | --- |
| `operation` | `ecs.request` |
| `operation_id` | 请求摘要 |
| `protocol` | `ecs-request` |
| `interaction_kind` | `REQUEST` |
| `peer_system` | `ECS` |
| `event_context` | 绑定快照始终写入。`deferred_request` 只在等待时追加，含 `message_id`、`operation`、`operation_id` |
| `status_code` | 等待中为 503。`DECIDED` 写入后为 200。对外不凭此列返回成功 |
| `reply_wire` | 只有 `DECIDED` 字节。等待中为空 |
| 旧历史 `identity_kind` | 运行时权威未启用时，`protocol` 为 `ecs-request` 与 `ecs-event` 一样记 `EVENT_DIGEST`。启用 `CallbackReceipt` 的路径没有该列 |

ECS HTTP 投影增加 `REQUEST`。`EcsHttpExchange.interaction_kind` 的检查约束改为允许 `COMMAND`、`RESULT`、`EVENT`、`STATUS`、`REQUEST`。这需要一条 Alembic 迁移，保持单一 head。投影继续使用现有 `event_type` 列保存 `request_type`，不新增列。`capture_callback` 增加来源 `ecs_request`，映射为 `interaction_kind="REQUEST"`。

事件投影、事件收据和 `ecs.event` 的写入路径不改。

## 8. 发现与启动

以下位置把 kind 从三类扩成四类，第四类名字是 `ecs_request`：

- `app/integrations/registration.py` 的 `_entrypoints`、`DEV_MODULES`、`PLATFORM_FORMAL_MODULES`、`PLATFORM_LOCAL_MODULES`。
- `app/integrations/discovery.py` 的 `Kind` 与 `_kinds`。
- `app/businesses/catalog.py` 的 `entry_modules`、`validate_business_contracts` 和 `validate_registration_owners`。

正式平台清单里 `ecs_request` 是空 tuple。local 后缀名是 `dev_ecs_request`。本文不新增演示模块；没有对应 `ENTRY_MODULES` 时，local 加载结果为空。

`supported` 路径集合加入 `ecs_request` 和 `dev_ecs_request`。不属于这些目录的 `ENTRY_MODULES` 仍使启动失败。

`app/main.py` 在 `include_ecs_callback(...)` 之后调用 `include_ecs_request(...)`。两个入口共用已经装入 `app.state` 的绑定解析器、输入目标、运行时、来源授权和提交后回调。正式注册表非空而绑定解析器缺失时，启动失败，失败文本与正式事件的要求一致。

## 9. 文件

新增：

- `app/integrations/ecs_request/__init__.py`：导出装饰器、上下文和接收函数。
- `app/integrations/ecs_request/registry.py`：注册表、`EcsRequestContext`、`respond`、`defer`。
- `app/integrations/ecs_request/envelope.py`：请求信封。字段错误复用 `app.integrations.ecs_callback.envelope.issues_from`。
- `app/integrations/ecs_request/receiver.py`：`accept_ecs_request`、`wait_for_request`、`continue_ecs_request`。
- `app/controllers/ecs_request.py`：路由和两段会话。
- `migrations/versions/` 下一条迁移，只放宽 ECS HTTP 投影的 `interaction_kind`。
- `tests/integrations/ecs_request/test_registry.py`
- `tests/integrations/ecs_request/test_ecs_request_receiver.py`
- `tests/controllers/test_ecs_request.py`

修改：

- 第 8 节列出的注册、发现、catalog 和 `app/main.py`。
- `app/integrations/http_exchanges.py` 的回调投影来源。
- `app/models/http_exchange.py` 的检查约束。
- `app/integrations/callback_receipts.py`：旧历史路径在 `protocol` 为 `ecs-request` 时把 `identity_kind` 写成 `EVENT_DIGEST`；增加 pending receipt 读取结果，允许 `reply_wire=NULL`，并保留完成收据的完整性校验。不改 `reply_wire` 的覆盖函数；调用方先锁行并比较摘要。
- `docs/integrations/registration.md`：表格增加一行；把 ECS Status“不属于第四类业务注册表”改成“不属于业务注册表”，避免和本文的第四类接入混淆。
- `docs/integrations/business-packages.md`：增加一段，说明同步请求使用 `@ecs_request`，事件 ACK 不变。
- `scripts/check_integration_contracts.py`、`app/controllers/dev_registry.py`、`app/schemas/dev_console.py`：输出和校验第四类注册。
- `tests/integrations/test_registration_bootstrap.py`、`tests/integrations/test_registration_discovery.py`、`tests/scripts/test_check_integration_contracts.py`、`tests/controllers/test_dev_registry.py`、`tests/architecture/test_layering.py`：覆盖第四类来源、发现、输出和分层边界。

不修改 `app/integrations/ecs_callback/receiver.py`。

## 10. 验收

测试使用独立注册表。生产 catalog 不加入样例业务。

- 合法信封当场 `respond` 后，HTTP 200 的正文是 `DECIDED`，收据中的回复字节与响应字节相同。
- 同一原始正文再报一次，返回同一字节，处理器只在首次执行。
- 未注册 `request_type` 返回 400 `UNKNOWN_REQUEST`，收据行数为 0。随后注册该请求，原正文可以正常 `DECIDED`。
- 非法信封返回 400 `INVALID_ENVELOPE`，超限返回 413。两条路径都不写收据。
- `defer` 指向本事务看不见的消息时，事务回滚，对外 503，不留收据和 Outbox 行。
- 原消息尚未终局时，等待预算用尽返回 503。收据保留 `deferred_request` 和绑定快照，`status_code` 为 503，`reply_wire` 为空，Outbox 仍是原来的一条消息。预算等于测试里该目标的 `timeout_ms` 加上 `poll_interval_s`。
- `response_state="VALID"` 后，`continue` 执行处理器并返回 `DECIDED`。从 `accept` 到 `continue`，`WmsRequests.send` 只有一次。决定写入后 `status_code` 为 200。
- 投递状态为 `EXPIRED` 或 `ABANDONED` 时，即使 `response_state` 仍是 `NONE`，等待也结束。`continue` 后处理器 `respond`，HTTP 200 的正文是 `DECIDED`。
- 终局结果下处理器既不 `respond` 也不结束：收据回复仍为空，诊断含 `REQUEST_HANDLER_INCOMPLETE`，对外 503。
- `delivery_uncertain` 在预算内不进入 `continue`。
- 旧历史路径写入的请求收据 `identity_kind` 为 `EVENT_DIGEST`。
- 两个事务同时 `respond` 不同正文时，先提交的正文保留，后提交的诊断含 `REQUEST_DECISION_CONFLICT`。相同正文两次保存都成功。
- `tests/controllers/test_ecs_callback.py` 继续期望 `{"code":200,"message":"ACK"}`。`/callback/event` 不接受 `request_type` 作为事件身份的一部分。
- catalog 拒绝未列入 `ENTRY_MODULES` 的 `ecs_request` 模块，也拒绝把 WMS 出站文件放进 `ecs_request` 目录后用 `@request_operation` 冒充；正式注册项会进入 input target 和 owner 校验。
- pending receipt 在 runtime authority 与 legacy history 两条路径都能被重报和等待循环读取，`reply_wire=NULL` 不被误判为证据损坏；决定写入后仍校验原字节并保留先提交者。
- 静态合同检查和 local registry 都列出 `ecs_request`，架构测试禁止业务服务反向导入该入口目录。
- 迁移升级后，ECS HTTP 投影可以写入 `interaction_kind="REQUEST"`。旧的 `EVENT` 行仍可写入。

## 11. 实施边界

本文完成后，开箱机可以把需要当场授权的交互登记为 `@ecs_request`。事实事件继续用 `@ecs_event`。取箱、移动和完工继续用 `EcsCommands.create` 与 Result。

开箱机的响应字段、退箱、额度和离位事实属于下一份业务规格，不从本文推断。

## Scope record

feature answers: 未提议删减；功能清单保持规格第 2 节和第 9 节
structure: A，D1，原目录方案（新建 `app/integrations/ecs_request/`）
accepted scope: 第 9 节的文件清单，含 ECS HTTP 投影迁移和第四类注册
pending remedies: none

## Decision ledger

### R1: 等待中的收据占位列
Finding: SC1, P1, confidence 10/10, app/integrations/callback_receipts.py:195 与规格第 165 行, reviewer plan-eng-review
Plan baseline: 规格第 165 行只写 `insert_receipt(..., protocol="ecs-request", interaction_kind="REQUEST", operation="ecs.request", defer_response=True)`，没有 `body`、`status_code`、`resp_timestamp`
Runtime evidence: `insert_receipt` 无条件执行 `body = values.pop("body")`。`CallbackReceipt.status_code` 与 `resp_timestamp` 是非空列。WMS 延迟回复仍传入这三项，见 `app/integrations/wms_callback/receiver.py:375-384`。`defer_response=True` 时 `reply_wire` 为空。
Comparison grid:

| Choice | Current | A | B | C |
|---|---|---|---|---|
| R1 等待中的 status_code / body / reply_wire | 规格未写，无法按原文调用 insert_receipt | status_code=503，body 为 503 JSON，reply_wire 为空；对外只看 reply_wire | status_code=200，body 为空对象，reply_wire 为空；对外只看 reply_wire | 不改规格，实现时自行补参数 |
| SC2 终局非成功 | 未决 | 未决 | 未决 | 未决 |
| SC3 等待预算 | 未决 | 未决 | 未决 | 未决 |

Question D2:
D2 — 等待中的收据写什么
Project/branch/task: maintenance-admin 的 feature/ecs-request，评审 docs/superpowers/specs/2026-11-20-ecs-request-design.md。
ELI10: 收据表有两列不能为空：HTTP 状态码和时间戳。现有函数还要求调用方传入 body。规格说等待 WMS 时先不写回复，但没说这两列和 body 填什么。不填的话，插入会在弹出 body 时失败，决定永远写不进去。填成 200 的话，以后如果有人只看状态码，会把还在等待的请求当成已经决定。
Stakes if we pick wrong: 等待中的行要么插不进去，要么看起来像已经成功。ECS 重报时可能拿到错误结论。
Recommendation: A because 503 占位和空的 reply_wire 一致，决定写入后才变成 200 和 DECIDED 字节。
Completeness: A=10/10, B=7/10, C=3/10
Pros / cons:
A) 503 占位不写回复 (recommended)
  ✅ 等待行的状态码是 503，回复字节为空；DECIDED 写入后状态码改为 200，响应字节与收据字节相同。
  ✅ 和现有 WMS 延迟回复一样传入 body、status_code、resp_timestamp，插入不会缺参数。
  ❌ 收据行在决定落盘前带一个不会返回给 ECS 的 503 正文，读状态码的人必须同时看回复字节。
B) 200 占位不写回复
  ✅ 等待行也能插入，回复字节仍为空，对外响应仍由回复字节决定。
  ❌ 状态码 200 出现在还没有决定的行上，和真正的 DECIDED 难以区分。
  ❌ 后续若有人用状态码判断是否可返回，会把等待中的请求当成成功。
C) 保持规格原文
  ✅ 规格文字不动，实现者可以按现有函数签名自行补参数。
  ❌ 原文缺少 body、status_code、resp_timestamp，按字面调用会在 pop body 时失败。
  ❌ 三个人实现可能选出三种占位，测试无法对照同一条规则。
Net: 等待行必须能插入，并且不能伪装成已经决定。
Header: 等待收据占位
Options:
A) 503 占位不写回复
等待期间 status_code=503，resp_timestamp 为当前毫秒，body 为不返回给 ECS 的 503 JSON，defer_response=True 使 reply_wire 为空。HTTP 响应只从 reply_wire 读取。DECIDED 时用现有保存函数把 status_code 改为 200 并写入决定字节。需要改规格第 6.1 节和第 7 节，并在接收测试里断言等待行的状态码和空回复。人工约 30 分钟，实现助手约 10 分钟。
B) 200 占位不写回复
等待期间 status_code=200，body 为 {}，reply_wire 仍为空。对外规则与 A 相同，只是状态码不表达等待。规格写明禁止仅凭状态码返回成功。人工约 20 分钟，实现助手约 10 分钟。风险是以后的读取代码把 200 当成决定。
C) 保持规格原文
不补充占位规则。实现时再决定参数。不批准具体列值。

State: approved
Actual answer: A，用户回复「A,以后均采用推荐的方式」
Accepted scope: 等待行 status_code=503，resp_timestamp 为当前毫秒，body 为不返回的 503 JSON，defer_response=True 使 reply_wire 为空。HTTP 只读 reply_wire。DECIDED 写入后 status_code=200。规格第 6.1 节、第 7 节和第 10 节已按此修改。
History: 原状态 pending / unanswered / none

### R2: 终局结果必须写成决定
Finding: SC2, P1, confidence 9/10, app/integrations/wms_envelope.py:116 与规格原等待规则, reviewer plan-eng-review
Plan baseline: 只有 VALID 或 INVALID 结束等待；NONE 继续等。示例处理器只在 VALID 时 respond。
Runtime evidence: `stored_response` 在状态不是 DONE 或 FAILED 时返回 `response_state="NONE"`。`delivery_terminal` 对 EXPIRED 和 ABANDONED 仍为真，见 `app/integrations/wms_request/results.py:135`。
Comparison grid:

| Choice | Current | A | B |
|---|---|---|---|
| R2 终局但投影为 NONE | 继续等到预算，然后 503，重报重复 | 终局且非 uncertain 时进入 continue，处理器必须 respond | 保持原规格 |

State: approved
Actual answer: A，用户「以后均采用推荐的方式」
Accepted scope: 规格第 5、6.2、6.3、10 节已按 A 修改。
History: none

### R3: 等待预算使用目标超时
Finding: SC3, P1, confidence 9/10, app/reliable/config.py:26, reviewer plan-eng-review
Plan baseline: 固定 10000 毫秒，并引用不存在的 `ReliableTarget`。
Runtime evidence: 类型是 `TargetConfig`，默认 `timeout_ms=10000`。Sender 在 API 进程内运行，`poll_interval_s` 默认 1。现有 WMS 读超时测试常用 30000。
Comparison grid:

| Choice | Current | A | B |
|---|---|---|---|
| R3 等待预算 | 固定 10000 毫秒 | 一次轮询间隔加上该消息目标的 timeout_ms；目标缺失时 1000+10000 | 保持 10000 |

State: approved
Actual answer: A，用户「以后均采用推荐的方式」
Accepted scope: 规格第 3 节和第 6.2 节已改。不新增配置。
History: none

### R4: continue 锁接受时的绑定
Finding: 架构, P1, confidence 8/10, app/integrations/ecs_callback/receiver.py:284, reviewer plan-eng-review
Plan baseline: continue 重新解析绑定。请求收据只写 deferred_request。
Runtime evidence: 事件收据写入 owner_domain、component_key、work_line_id、device_id、device_role 和两个 version。
Comparison grid:

| Choice | Current | A | B |
|---|---|---|---|
| R4 绑定快照 | continue 可能锁到后来的绑定 | 接受时写入与事件相同的快照，continue 锁原行 | 每次重新解析 |

State: approved
Actual answer: A，用户「以后均采用推荐的方式」
Accepted scope: 规格第 6.1 节和第 7 节已改。原行不存在则 503，收据保留。
History: none

### R5: 旧历史身份种类
Finding: 代码质量, P2, confidence 8/10, app/integrations/callback_receipts.py:223, reviewer plan-eng-review
Plan baseline: 规格未提 identity_kind。
Runtime evidence: 未启用运行时权威时，只有 protocol `ecs-event` 写成 `EVENT_DIGEST`，其余为 `OPERATION_ID`。`ecs-request` 的 operation_id 是摘要，不是 UUID。
Comparison grid:

| Choice | Current | A | B |
|---|---|---|---|
| R5 identity_kind | 旧路径会写成 OPERATION_ID | ecs-request 与 ecs-event 一样写 EVENT_DIGEST | 不改旧路径 |

State: approved
Actual answer: A，用户「以后均采用推荐的方式」
Accepted scope: 修改 `callback_receipts.py` 这一处条件，并在第 10 节验收。
History: none

### R6: 不合并事件接收器
Finding: 代码质量共享代码, P2, confidence 8/10, app/integrations/ecs_callback/receiver.py, reviewer plan-eng-review
Plan baseline: 不修改 `ecs_callback/receiver.py`。
Runtime evidence: 事件路径在同一事务内写 ACK。同步请求要提交、释放锁、再等待。事务边界不同。
Comparison grid:

| Choice | Current | A | B |
|---|---|---|---|
| R6 共用接收器 | 分开 | 继续分开，只复用 issues_from、insert_receipt、get_result、绑定解析和来源授权 | 抽出共用 accept |

State: approved
Actual answer: A，用户「以后均采用推荐的方式」
Accepted scope: 不新增共用接收器。信封错误继续调用 `issues_from`。
History: none

Approval readiness: PASS。D1 结构 A；R1 用户选择 A；R2、R3、R4、R5、R6 按用户「以后均采用推荐的方式」取推荐项。事件 ACK 回归沿用规格原验收，文件定为 `tests/controllers/test_ecs_callback.py`。


## 评审记录

### 范围

已有能力够用：`insert_receipt(defer_response=True)`、`save_receipt_response`、`WmsRequests.send`、`get_result`、API 进程内的 Sender、事件的绑定解析和来源授权。Outbox 先提交再由独立发送循环投递。规格把数据库锁放在等待之外，和这个边界一致。

`@request_operation` 的现有类位于 `request/` 或 `app/integrations/wms_request/dev.py`。目录限制按类所在模块判断时，不会挡住现有启动。`coarse_sorting/integration.py` 只是调用方，类定义仍在 `request/`。

不删功能。文件布局保持第 9 节的新包。

### 1. 架构

```text
ECS
  POST /callback/request
    accept: 校验、锁工作线然后设备、写收据、处理器 send 或 respond
    commit，释放锁
    wait: 短会话轮询 reply_wire 和 get_result
    continue: 锁原绑定，处理器 respond
    commit
  200 DECIDED 或 503
Sender 在同一 API 进程，默认每 1 秒投递 Outbox
```

边界：新包只拥有同步请求。事件 ACK、WMS 出站和 ECS 命令不改协议。

失败：WMS 慢于目标读超时，或 Sender 没跑起来。调用方看到 503，收据留着等待身份，Outbox 继续投递。ECS 用原请求重报。决定写入后，重报返回同一字节。测试见第 10 节的预算用尽和 EXPIRED 两条。

安全：来源授权、未知请求不落收据、正式注册表缺少绑定解析器时启动失败，都沿用事件入口。

没有新的安装包或发布通道。

投影列 `interaction_kind` 是 `String(8)`，`REQUEST` 放得下。HTTP 投影增加 REQUEST 仍要迁移检查约束。

### 2. 代码质量

新代码放在 `app/integrations/ecs_request/`，与事件包分开。这是 D1。

不抽出共用接收器，见 R6。可复用的是 `issues_from`、`insert_receipt`、`get_result`、绑定解析器和来源授权。

错误路径：缺 body 会在插入时失败，已由 R1 补上参数。终局后处理器无决定，已由 R2 规定诊断和 503。旧历史把摘要写成 OPERATION_ID，已由 R5 改条件。

第 6 节的文字流程和第 7 节的字段表与上述决定一致。

### 3. 测试

框架是 pytest，命令是 `uv run pytest`。见 `AGENTS.md` 的 Testing Guidelines。

```text
CODE PATHS                                            USER FLOWS
[+] ecs_request/receiver.py（拟新增）                 [+] ECS 同步请求
  ├── accept                                          ├── [GAP] [→E2E] 当场 DECIDED，收据字节相同
  │   ├── [GAP] 合法信封 respond                      ├── [GAP] [→E2E] 等待后 VALID，send 只有一次
  │   ├── [GAP] 503 占位且 reply_wire 为空            ├── [GAP] [→E2E] EXPIRED 仍要 DECIDED
  │   ├── [GAP] defer 指向不可见消息，无收据          ├── [GAP] 同一正文重报返回原字节
  │   └── [GAP] insert 冲突不再 send                  ├── [GAP] 未知请求 400，随后注册仍可决定
  ├── wait                                            ├── [GAP] 预算用尽 503，Outbox 仍是一条
  │   ├── [GAP] 未终局等到预算                        └── [GAP] 终局但处理器不 respond，诊断 INCOMPLETE
  │   ├── [GAP] uncertain 不进入 continue
  │   └── [GAP] 终局 NONE 进入 continue           [+] 现有事件
  └── continue                                        └── [★★★ TESTED] ACK — tests/controllers/test_ecs_callback.py
      ├── [GAP] 先写入正文获胜
      └── [GAP] 相同字节两次成功

COVERAGE: 1/16 已有测试（6%） | 新路径: 0/15 | 事件回归: 1/1
QUALITY: ★★★:1 | GAPS: 15（3 条跨接收、等待和 WMS 的集成）
```

图例：★★★ 含边界和错误。GAP 表示仓库里还没有对应测试。第 10 节已经把这些 GAP 写成验收，实现时和功能一起补上。没有提示词或模型质量评估。

事件 ACK 是既有合同。回归断言留在 `tests/controllers/test_ecs_callback.py`。

### 4. 性能

等待循环每 200 毫秒打开短会话，不在等待期间持有工作线锁，也不是按集合逐条查询。成本是一个 API 工作单元会占用「轮询间隔 + 目标读超时」这么久。第 6.2 节已写明这条路由不承接高频事件。没有额外的缓存层。

### 失败时调用方看到什么

| 失败 | 调用方 | 测试 |
| --- | --- | --- |
| WMS 未在预算内返回 | 503，收据保留等待身份 | 第 10 节预算用尽 |
| 消息 EXPIRED 或 ABANDONED | 200 DECIDED，业务拒绝在 data 内 | 第 10 节终局 NONE |
| 处理器终局后不 respond | 503，诊断 REQUEST_HANDLER_INCOMPLETE | 第 10 节 |
| 两个决定冲突 | 先提交的正文保留，后到的请求仍返回该正文 | 第 10 节冲突 |
| 未知请求打到旧实例 | 400，不落收据。处理器须先于 ECS 全量发布 | 第 10 节未知请求 |

没有「无测试、无处理、调用方无感知」的失败。严重缺口数是 0。

### 已经存在

- 收据插入和延迟回复：`app/integrations/callback_receipts.py`
- 事件接收顺序和绑定快照：`app/integrations/ecs_callback/receiver.py`
- WMS 入队与结果投影：`app/reliable/outbox.py`、`app/integrations/wms_request/results.py`、`app/integrations/wms_envelope.py`
- Sender：`app/reliable_runtime.py` 在 API 生命周期里启动

同步请求的两段事务是新的。不重建 Outbox。

### 不在本文

开箱机的请求类型、响应字段、退箱、额度和离位事实。未注册事件的回放仍是 `TODOS.md` 里的既有项，不改事件的 200 ACK。本文的未知请求返回 400 且不落收据，避免旧实例占住身份。

### TODOS.md

没有新增待办。现有「确认事件重报窗口」和「未注册事件回放」继续只约束事件入口。

### 并行

顺序实现。注册表、收据写入和控制器是同一条协议，少于两条独立工作流。Sequential implementation, no parallelization opportunity.

## Final review addendum

### Scope and current-state check

保留已批准的 D1 原目录方案，以及 R1–R6 的既有决定。没有新增可独立选择的行为：本轮只把已批准协议接到所有现有注册、读取、投影、静态检查和测试边界。

### Architecture review

[已关闭] pending receipt 的读取是新协议的必要边界。现有 `find_receipt` 在 `app/integrations/callback_receipts.py:287-290` 明确执行 `if wire is None ... raise history.EvidenceIntegrityError(...)`，而规格第 6.1、6.2 节要求重复请求和等待循环识别“回复尚不存在”。实施计划增加 pending-aware receipt view，完成后再进入 `continue`；完成收据仍走原完整性校验。

[已关闭] catalog 归属校验必须覆盖新 kind。现有 `app/businesses/catalog.py:328-331` 的 `registrations` 只加入 `ecs_event` 和 `wms_callback`，`validate_registration_owners` 在 `:280-284` 也只遍历三类 kind。实施计划把 `ecs_request` registry 接入同一 consumers/targets/owner 校验，保留 `request` 给 WES → WMS。

### Code-quality and boundary review

[已关闭] 第四类不能只改运行时 discovery。`scripts/check_integration_contracts.py:24-43` 只导入三类 registry，`app/controllers/dev_registry.py:25-29` 和 `app/schemas/dev_console.py:46-49` 也硬编码三类。实施计划同步更新静态检查、local 输出和响应模型，避免声明已加载但工具不可见。

[已关闭] 新入口目录需要进入分层守卫。`tests/architecture/test_layering.py:107-109` 的 `entries` 不含 `ecs_request`；实施计划补上并增加第四类反向依赖回归。

### Test review

```text
CODE PATHS                                                USER FLOWS
[+] ecs_request/registry.py                               [+] 当场 DECIDED
  ├── [GAP] duplicate (device_role, request_type)          ├── [GAP] [→E2E] 200 + 原始决定字节
  ├── [GAP] response model / defer exclusivity             ├── [GAP] [→E2E] pending replay 不重复 send
  └── [GAP] ecs_request / request 目录隔离                 └── [GAP] unknown request 不落 receipt
[+] ecs_request/receiver.py
  ├── [GAP] accept → commit → wait → continue               [+] WMS 依赖路径
  ├── [GAP] pending receipt 可读且 reply_wire=NULL           ├── [GAP] [→E2E] VALID 后 DECIDED
  ├── [GAP] EXPIRED/ABANDONED 终局进入 continue              ├── [GAP] [→E2E] budget 503 保留 outbox
  ├── [GAP] uncertain 不进入 continue                        └── [GAP] [→E2E] 两事务冲突保留先提交正文
  └── [GAP] handler incomplete / wrong defer 503            [+] 既有 ECS event
[+] catalog / discovery / tooling                         └── [★★★ TESTED] ACK contract unchanged
  ├── [GAP] owner + input target + local/formal scope
  ├── [GAP] static checker lists ecs_request
  ├── [GAP] dev registry schema accepts ecs_request
  └── [GAP] layering guard covers ecs_request

COVERAGE: 1/21 existing contracts tested | new paths: 0/20 before implementation
QUALITY: ★★★:1 existing regression | GAPS: 20 (6 cross-layer integration paths)
```

测试框架为 pytest，执行顺序是先跑新增的 registry/receiver/controller/registration 测试，再跑迁移与完整 `uv run pytest`。等待循环的每次短会话都必须在会话外睡眠并重新建 session；SQLAlchemy 文档明确 AsyncSession 是单任务的可变事务对象，且高隔离事务会继续看到同一事务内的旧读值，因此不能复用第一段 session 轮询。[SQLAlchemy Session Basics](https://docs.sqlalchemy.org/en/20/orm/session_basics.html) [SQLAlchemy asyncio](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)

### Performance review

保留已批准的单请求预算：`poll_interval_s * 1000 + TargetConfig.timeout_ms`，睡眠不持有数据库连接或业务锁。新增 pending receipt 查询只按 `(operation, operation_id)` 唯一索引读取，不做集合扫描；静态检查和 dev registry 只读取已加载 registry，不触发 handler、factory 或数据库。

### Failure modes

| Failure | Required handling | Required proof |
|---|---|---|
| deferred receipt has no reply | pending view returns state, no evidence error | runtime + legacy receipt tests |
| ECS request declared but not owned | startup/static check fails before local load | catalog/bootstrap tests |
| local registry omits fourth kind | response schema and output include `ecs_request` | dev registry test |
| sender result is terminal but no response | `REQUEST_HANDLER_INCOMPLETE`, 503, receipt unchanged | controller/receiver integration test |
| old event path regresses | event ACK and identity unchanged | existing `test_ecs_callback.py` |

没有剩余的无测试、无错误处理、调用方无感知的关键失败。

### What already exists

复用 `insert_receipt`、`save_receipt_response`、`issues_from`、`WmsRequests.send`、`get_result`、事件绑定解析/授权、after-commit 唤醒、HTTP projection 和单一 Alembic head。新增只承担 ECS synchronous request 的 registry、两段事务和等待；不抽象或改写 `ecs_callback/receiver.py`。

### NOT in scope

开箱机具体 request type、response fields、WMS operation、Result/command semantics、认证策略、deployment and physical acceptance remain outside this design.

### TODOS.md

No new TODO. Existing ECS event replay and replay-window items remain unchanged.

### Implementation Tasks

### Task 1 — 建立 `ecs_request` registry、envelope、context — 实现 `(device_role, request_type)` 唯一键、严格 response model、respond/defer 互斥和入口目录守卫。
- [ ] **T1 (P1, human: ~4h / CC: ~25min)** — 建立 `ecs_request` registry、envelope、context — 实现 `(device_role, request_type)` 唯一键、严格 response model、respond/defer 互斥和入口目录守卫。
  - Surfaced by: 规格第 5 节；与 `ecs_event` 平行但不复用 WMS `request` registry。
  - Files: `app/integrations/ecs_request/{__init__,registry,envelope}.py`、`tests/integrations/ecs_request/test_registry.py`
  - Verify: `uv run pytest tests/integrations/ecs_request/test_registry.py`
### Task 2 — 实现两段接收事务 — accept 写收据/绑定快照，提交后 wait，最后 continue；等待期间不持锁、不 send 第二次，保留 503 占位。
- [ ] **T2 (P1, human: ~6h / CC: ~35min)** — 实现两段接收事务 — accept 写收据/绑定快照，提交后 wait，最后 continue；等待期间不持锁、不 send 第二次，保留 503 占位。
  - Surfaced by: 规格第 6 节与 R1–R4。
  - Files: `app/integrations/ecs_request/receiver.py`、`app/controllers/ecs_request.py`、`app/main.py`、`tests/integrations/ecs_request/test_ecs_request_receiver.py`、`tests/controllers/test_ecs_request.py`
  - Verify: `uv run pytest tests/integrations/ecs_request/test_ecs_request_receiver.py tests/controllers/test_ecs_request.py`
### Task 3 — 增加 pending receipt 读路径 — 允许 `reply_wire=NULL` 返回“等待中”，对已完成回复继续执行 sha256/json 完整性校验；runtime authority 与 legacy 都覆盖。
- [ ] **T3 (P1, human: ~2h / CC: ~15min)** — 增加 pending receipt 读路径 — 允许 `reply_wire=NULL` 返回“等待中”，对已完成回复继续执行 sha256/json 完整性校验；runtime authority 与 legacy 都覆盖。
  - Surfaced by: `app/integrations/callback_receipts.py:287-320` 的现有异常路径。
  - Files: `app/integrations/callback_receipts.py`、`tests/integrations/ecs_request/test_ecs_request_receiver.py`、`tests/integrations/wms_callback/test_task_contract_extensions.py` 或同等公共 receipt 测试
  - Verify: `uv run pytest tests/integrations/ecs_request/test_ecs_request_receiver.py tests/integrations/wms_callback/test_task_contract_extensions.py`
### Task 4 — 接入 formal/local catalog — 把 `ecs_request` 加入 registration/discovery/catalog 的 registrations、consumers、targets、owner checks，并在 `main.py` 挂载路由。
- [ ] **T4 (P1, human: ~3h / CC: ~20min)** — 接入 formal/local catalog — 把 `ecs_request` 加入 registration/discovery/catalog 的 registrations、consumers、targets、owner checks，并在 `main.py` 挂载路由。
  - Surfaced by: `app/businesses/catalog.py:328-331,443-451` 的三类硬编码。
  - Files: `app/integrations/registration.py`、`app/integrations/discovery.py`、`app/businesses/catalog.py`、`app/main.py`、`tests/integrations/test_registration_bootstrap.py`、`tests/integrations/test_registration_discovery.py`
  - Verify: `uv run pytest tests/integrations/test_registration_bootstrap.py tests/integrations/test_registration_discovery.py`
### Task 5 — 补齐投影与历史 — 写 `identity_kind=EVENT_DIGEST`、增加 `REQUEST` check constraint/迁移、映射 `ecs_request` 的 `request_type` 到现有 `event_type` 列。
- [ ] **T5 (P2, human: ~2h / CC: ~15min)** — 补齐投影与历史 — 写 `identity_kind=EVENT_DIGEST`、增加 `REQUEST` check constraint/迁移、映射 `ecs_request` 的 `request_type` 到现有 `event_type` 列。
  - Surfaced by: 规格第 7 节与 R5。
  - Files: `app/integrations/callback_receipts.py`、`app/models/http_exchange.py`、`app/integrations/http_exchanges.py`、`migrations/versions/`、`tests/migrations/`、`tests/controllers/test_ecs_callback.py`
  - Verify: `uv run pytest tests/migrations tests/controllers/test_ecs_callback.py`
### Task 6 — 更新工具与 local registry — 静态合同检查、dev registry schema/output、架构分层守卫都识别 `ecs_request`。
- [ ] **T6 (P2, human: ~2h / CC: ~15min)** — 更新工具与 local registry — 静态合同检查、dev registry schema/output、架构分层守卫都识别 `ecs_request`。
  - Surfaced by: `scripts/check_integration_contracts.py:24-43`、`app/controllers/dev_registry.py:25-29`、`app/schemas/dev_console.py:46-49`、`tests/architecture/test_layering.py:107-109`。
  - Files: `scripts/check_integration_contracts.py`、`app/controllers/dev_registry.py`、`app/schemas/dev_console.py`、`tests/scripts/test_check_integration_contracts.py`、`tests/controllers/test_dev_registry.py`、`tests/architecture/test_layering.py`
  - Verify: `uv run pytest tests/scripts/test_check_integration_contracts.py tests/controllers/test_dev_registry.py tests/architecture/test_layering.py`
### Task 7 — 更新接入文档与全量验证 — 把第四类注册、local 输出和历史约束写入文档，执行 lint、迁移和全量测试。
- [ ] **T7 (P2, human: ~1h / CC: ~10min)** — 更新接入文档与全量验证 — 把第四类注册、local 输出和历史约束写入文档，执行 lint、迁移和全量测试。
  - Surfaced by: 发现与启动/工具边界的文档一致性要求。
  - Files: `docs/integrations/registration.md`、`docs/integrations/business-packages.md`、`app/integrations/__init__.py`
  - Verify: `uv run ruff check . && uv run ruff format --check . && uv run pytest`

顺序实现，无并行化机会：T1 定义 registry 契约；T2/T3 依赖它并共享 receipt/receiver；T4/T6 共享 catalog/discovery 边界；T5 的 migration 必须在合入时保持单一 Alembic head；T7 在所有代码与测试完成后执行。

### Approval readiness

PASS。D1 与 R1–R6 的批准继续有效；本轮新增内容均为已批准协议的必要实现、测试和工具边界，没有未回答的行为选择。

### Completion summary

- Step 0: Scope Challenge — scope accepted as-is; D1 original arrangement retained
- Architecture Review: no remaining issues; pending receipt read path and catalog registration were added to the plan
- Code Quality Review: no remaining issues; static checker/dev registry/layering surfaces were added
- Test Review: diagram produced, 20 implementation gaps listed in the test plan
- Performance Review: no remaining issues; bounded wait and fresh sessions retained
- NOT in scope: written
- What already exists: written
- TODOS.md updates: 0 items proposed
- Failure modes: 0 critical gaps
- Unresolved decisions: 0
- Outside voice: claude-code unavailable (`CODEX_MODE: not_installed`); no native fallback capability in this host
- Parallelization: 1 sequential lane
- Lake Score: 10/10 = 1/1 coverage choices answered; kind/structure choices carried from prior approvals

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | not run | — |
| Outside Review | claude-code, plan-eng-review preflight | Independent 2nd opinion | 0 | unavailable | Claude Code 未安装，原生替代未启动 |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 2 | clear | final pass: 0 open findings; prior pass: 5 findings incorporated |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | not run | 无界面 |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | not run | — |

**OUTSIDE COVERAGE:** provider claude-code, phase plan-review, completion unavailable, findings none. `CODEX_MODE: not_installed`; native fallback unavailable in this host.
**VERDICT:** ENG CLEARED — ready to implement
NO UNRESOLVED DECISIONS
