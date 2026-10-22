# 出站联调控制台(Swagger 统一入口)开发说明书

状态:**T1~T5 已实施并验证(2026-10-22,见 §10);T6 已补(README「联调控制台」小节)** · 依据:`2026-10-21-transport-outbound-design.md`、`2026-10-22-rack-transport-integration-design.md`、AGENTS.md 分层约定 · 范围:仅 `ENVIRONMENT=local`

## 1. 目标与边界

### 1.1 目标

在 FastAPI Swagger 里提供**一个**联调入口 `POST /api/v1/dev/outbound`:从下拉里选业务场景(货架进场、货架换面、货架离场、料箱投料、回架、交换),填写请求体即可走**真实的业务接入层**登记出站义务,再用统一的状态节点查看投递结果。以后新增 RELIABLE 业务接入层时,新增一个文件即可出现在下拉里,不改通用代码、不改 `main.py`。

### 1.2 非目标

- 不暴露 `app/reliable` 本身(基础设施),不做通用的消息管理(重试、取消、resume)。
- 不改任何生产模块的行为与签名;不新增权限码、不新增迁移。
- 非 local 环境这些路由**不存在**(不是隐藏文档)。
- 不展示 `TransportTask` 里的业务字段(第一版只展示 reliable 消息快照)。

## 2. 已核实的现状(设计前提)

| # | 事实 | 来源 |
|---|---|---|
| E1 | `get_session` 只 `yield`,**不提交**;`SessionDep` 同一请求内共享 | `app/core/database.py:14` |
| E2 | 业务门面(`RackCycle.*`、`BinScenarios.*`、`TransportService.*`)只登记义务、不提交,"由调用方提交或回滚" | `rack_scenarios.py`、`service.py:_register` |
| E3 | 分层测试禁止 `app/integrations` 导入 `fastapi`、`app.controllers`、`app.schemas`、`app.services`;`app/core` 只禁导入 services/controllers | `tests/architecture/test_layering.py` |
| E4 | 校验失败统一映射 400;`new_router` 自动补全 401/429/400 文档;`drop_422` 去掉 422 | `app/core/routing.py` |
| E5 | `require_permission` 要求权限码全局唯一且需同步 `INITIAL_PERMISSIONS` 与权限测试;`get_current_user` 无此负担 | `app/core/deps.py`、AGENTS.md |
| E6 | `ReliableMessage.business_key` 有索引;Transport 的 `business_key = transport_task_id` | `models/reliable.py`、`service.py` |
| E7 | `TransportService` 默认实例从 `RELIABLE_TARGETS` 惰性取目标,未配置时抛 `RuntimeError`(含配置示例) | `service.py:target` |
| E8 | 发送器在应用 lifespan 内运行(同一进程、独立引擎);提交后的消息自动被发送 | `app/reliable_runtime.py`、`main.py` |
| E9 | 适配器 ACK 分类:202 `RECEIVED` / 200 `DUPLICATE` 成功;409 `CONFLICT`、422 `REJECTED`(原因须在白名单)终态;503 `UNAVAILABLE` 可重试;其余一律 UNKNOWN | `adapter.py:classify_response` |
| E10 | FastAPI 0.143.1、Pydantic 2.14.1 | `.venv` |

## 3. 架构(满足 E3)

```text
app/core/dev_scenarios.py          注册表 + @dev_scenario 装饰器 + DevRequest/DevOutcome  (仅 pydantic,无 fastapi)
app/integrations/<业务>/dev.py     场景定义:请求模型 + 被装饰的 handler               (调用本业务的门面;无 fastapi)
app/schemas/dev_outbound.py        响应契约 DevSubmitOut、DevMessageOut
app/services/dev_outbound_service.py   submit(调 handler + 映射错误 + 提交)、message_status(按 business_key 查消息)
app/controllers/dev.py             include_dev_routers(app, environment):扫描 + 构建路由 + 挂载
app/main.py                        一行:include_dev_routers(app, environment=settings.ENVIRONMENT)
```

依赖方向:`controllers → services → core/models`;`integrations/<业务>/dev.py → core`。扫描逻辑在 controllers(允许导入任何层),`core` 不导入 `integrations`。

## 4. 设计

### 4.1 注册表与装饰器(`app/core/dev_scenarios.py`)

```python
class DevRequest(BaseModel):            # 所有场景请求体的基类
    model_config = ConfigDict(extra="forbid")
    client_request_id: str | None = None   # 不传由服务端生成 uuid7;传同一个即验证幂等重放

@dataclass(frozen=True)
class DevOutcome:                        # handler 的返回
    business_key: str                    # 用于状态查询(Transport 为 transport_task_id)
    client_request_id: str

@dev_scenario(name, title, *, example, errors=None)   # 装饰 async def handler(session, req: <DevRequest 子类>)
def scenarios() -> list[DevScenario]                  # 按 name 排序
```

装饰器在**登记时**(导入期)立即校验,失败即抛 `ValueError`:

1. `name` 匹配 `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$`(如 `rack.enter`);重名报错并带上两个 handler 的 `__module__.__qualname__`。
2. handler 是协程函数,恰两个参数(`session` 可以不带标注,所以按 `inspect.signature` 取第二个参数,再用 `get_type_hints` 解析其标注);它必须是 `DevRequest` 子类,且不自带 `scenario` 字段。
3. `example` 能通过该模型校验(不合法的示例在启动时就暴露,而不是点开 Swagger 才发现)。
4. 用 `create_model(f"Dev{CamelCase(name)}", __base__=<req 类>, scenario=(Literal[name], name))` 生成带判别字段的模型。**模型名由 name 派生**,避免不同业务出现同名模型(如两个 `FeedIn`)造成 OpenAPI schema 冲突。

`errors: dict[type[Exception], ErrorCode]` 是业务异常到统一错误码的映射。未列出的异常不拦截,走全局 handler(500)。

### 4.2 路由(`app/controllers/dev.py`)

`include_dev_routers(app, *, environment, prefix="/api/v1")`:

1. `environment != "local"` 直接返回,**不导入任何 dev 模块、不挂载任何路由**。
2. 扫描:`pkgutil.iter_modules(app.integrations.__path__)`,按包名排序,对每个包尝试 `importlib.import_module(f"app.integrations.{pkg}.dev")`;**仅当该模块不存在(`ModuleNotFoundError` 且 `name` 恰为该模块)时跳过**,模块内部的导入错误必须原样抛出,让联调环境启动即失败。
3. `scenarios()` 为空则不挂载。
4. 构建联合体:`body = Union[tuple(s.model for s in scenarios())]`;`examples = {s.name: {"summary": s.title, "value": {"scenario": s.name, **s.example}}}`。
5. 两个路由(`new_router(prefix="/dev/outbound", tags=["dev-outbound"])`,依赖 `get_current_user`):
   - `POST ""`:`req: Annotated[body, Body(discriminator="scenario", openapi_examples=examples)]` → `DevSubmitOut`
   - `GET "/status/{business_key}"` → `list[DevMessageOut]`(无消息 404)
6. 挂载时若已配置的 `RELIABLE_TARGETS` 里没有任何目标,打一条 WARNING(含 `.env.example` 的配置示例),否则联调时会因 E7 得到 500。

> **实现硬约束**:判别器必须写在 `Body(discriminator=...)` 上。把 `Field(discriminator=...)` 放进 `Annotated` 别名会被 FastAPI 静默丢弃(§9 V2)。

### 4.3 服务(`app/services/dev_outbound_service.py`)

- `submit(session, scenario, req) -> DevOutcome`:`await scenario.handler(session, req)`,成功后 **`await session.commit()`**(E1/E2,否则发送器永远看不到消息);捕获 `scenario.errors` 里列出的异常,`rollback` 后抛 `AppError(code, str(exc))`。
- `message_status(session, business_key) -> list[DevMessageOut]`:`select(ReliableMessage).where(business_key == …).order_by(id)`。`request_raw`/`response_raw`/`last_response_raw` 解码为 JSON(失败则 UTF-8 `replace` 文本)。

### 4.4 响应契约(`app/schemas/dev_outbound.py`)

- `DevSubmitOut`:`scenario`、`title`、`business_key`、`client_request_id`。**不含 `replayed`**:门面只返回句柄,无法区分新建与重放(重放可再提交同号并比较 `business_key` 一致来观察)。
- `DevMessageOut`:`message_id`、`target`、`operation`、`operation_id`、`status`、`fail_reason`、`attempts`、`not_received_count`、`delivery_uncertain`、`next_attempt_at`、`last_attempt_at`、`last_http_status`、`last_failure_kind`、`last_verdict`、`request`、`response`、`last_response`、`created_at`、`updated_at`。

### 4.5 Transport 场景(`app/integrations/wms_transport/dev.py`)

扁平 pydantic 输入模型,在 handler 里转成领域 dataclass(联合类型不直接暴露给 Swagger):

| 场景 name | 标题 | 调用 | 请求体字段 |
|---|---|---|---|
| `rack.enter` | 货架进场 | `rack_scenarios.cycle(...).enter` | `workline_id`、`station_id?`、`role`(`RackRole` 枚举)、`rack_id`、`target{kind: position\|zone, code}`、`target_face?` |
| `rack.rotate` | 货架换面 | `.rotate` | 同上除 `target`;`position_code`(原地,只能是 RackPosition)、`target_face` |
| `rack.depart` | 货架离场 | `.depart` | 同 `rack.enter` |
| `bin.feed` | 料箱投料 | `bin_scenarios.feed_bins` | `workline_id`、`station_id?`、`moves[{bin_code, source{rack_id, rack_face, slot_id}, target{handoff_code}}]` |
| `bin.return` | 料箱回架 | `.return_bins` | 同上,方向相反:`source{handoff_code}`、`target{rack_id, rack_face, slot_id}` |
| `bin.exchange` | 料箱交换 | `.exchange_bins` | `workline_id`、`station_id?`、`exchange_pairs[{left_bin_code, left_location{…}, right_bin_code, right_location{…}}]` |

- `errors={InvalidTransportRequest: INVALID_REQUEST, IdempotencyConflict: CONFLICT}`。
- `client_request_id` 为空时 handler 用 `app.core.identifiers.uuid7()` 生成(UUID 单一入口),并随 `DevOutcome` 返回。
- 示例值取现场已出现的编码(货架 `510004`、位置 `KT16`、面 `90`);`Zone`、储位、滚筒线编码为**占位示例,联调前按现场替换**。
- 门面方法一律走模块级 `rack_scenarios` / `bin_scenarios`(测试可 `monkeypatch.setattr(..., "transport", rig.service)` 替换)。

### 4.6 扩展约定(新增业务接入层)

新建 `app/integrations/<业务>/dev.py`,为每个场景写一个请求模型 + 一个被 `@dev_scenario` 装饰的函数。无需改 `main.py`、`controllers/dev.py` 或通用代码;重启后下拉自动出现。该模块不得导入 `fastapi`、`app.controllers`、`app.services`、`app.schemas`(由现有分层测试强制)。

### 4.7 阶段二(待确认,可独立交付):Mock WMS

没有对端时消息只会 PENDING 后重试到耗尽,看不到 ACK 路径。`app/controllers/dev_mock_wms.py`(`POST /api/v1/wes/transport-requests`,无认证,因为发送器不带 token;`PUT /api/v1/dev/mock-wms/mode` 切换行为):

| 模式 | 响应 | 预期适配器判定 |
|---|---|---|
| `accept`(默认) | 202 `RECEIVED` | DELIVERED → `DONE` |
| `duplicate` | 200 `DUPLICATE` | DELIVERED |
| `conflict` | 409 `CONFLICT` | PEER_TERMINAL → `FAILED` |
| `reject` | 422 `REJECTED` + 白名单 `reason_code` | PEER_TERMINAL → `FAILED` |
| `unavailable` | 503 `UNAVAILABLE` | NOT_RECEIVED → 重试 |
| `garbage` | 200 非 JSON | UNKNOWN |
| `hang` | 睡眠超过目标超时 | 超时 → UNKNOWN |

ACK 体回显请求里的 `operation_id`、`data.transport_task_id` 并带整数毫秒 `timestamp`(E9 的自洽要求);模式存进程内存。使用方式:`RELIABLE_TARGETS` 的 `base_url` 指向本应用自身。约束:单 worker(`uvicorn --reload` 即是);全局限流每 IP 每路由 100 次/分钟对重试风暴有影响,联调时留意。该文件在 `controllers`(可导入 `integrations` 的协议常量),**显式**加入挂载,不走自动扫描。

## 5. 错误与运行约定

| 情况 | 响应 |
|---|---|
| 请求体不合法 / `scenario` 缺失或未知 / 额外字段 | 400(全局映射) |
| `InvalidTransportRequest`(模板、方向、角色等场景规则) | 400 |
| `IdempotencyConflict`(同号不同入参) | 409 |
| 未登录 | 401 |
| 未配置 Transport 目标(E7 的 `RuntimeError`) | 500;挂载时 WARNING 已给出配置示例 |

## 6. 安全

- 仅 local 挂载;非 local 导入阶段即不加载 dev 模块。
- 只依赖 `get_current_user`(任何已登录用户可调),不用 `require_permission`(E5)。该取舍只在 local 成立;**严禁**把 `ENVIRONMENT` 判断放宽。
- 全局限流与审计中间件照常生效。

## 7. 测试(只固化长期合同)

1. **非 local 不挂载**:`include_dev_routers(FastAPI(), environment="production")` 后应用里没有任何 `/dev` 路径,且未导入 `*.dev` 模块。
2. **注册表规则**:对已加载的全部场景断言:名字唯一且合规、`example` 通过自身模型校验、`/openapi.json` 里每个场景都出现在判别映射中。规则式,不枚举当前场景。
3. **提交即可见(E1/E2)**:经 `dev_outbound_service.submit` 提交一个 Transport 场景后,用**全新会话**能查到 `TransportTask` 和 `PENDING` 的 `ReliableMessage`。
4. **幂等与冲突**:同一 `client_request_id` 重放得到同一 `business_key`;同号不同入参为 409;方向错误的投料为 400。
5. **状态节点**:无消息 404;有消息时返回解码后的 `request`。

不测:示例值内容、响应字段快照、Swagger 页面渲染、私有 helper。

## 8. 实施任务与验收

| 阶段 | 任务 | 验收 |
|---|---|---|
| T1 | `core/dev_scenarios.py`(基类、装饰器、注册表、登记期校验) | 规则测试(§7-2)通过;重名/非法 name/非法 example 各抛 `ValueError` |
| T2 | `schemas`、`services`、`controllers/dev.py`、`main.py` 一行 | §7-1、§7-3、§7-5 通过;分层测试仍绿 |
| T3 | `wms_transport/dev.py` 六个场景 | §7-4 通过;`/openapi.json` 的 `oneOf` 含 6 个场景且带 `discriminator.mapping` |
| T4 | **浏览器验收**(local 起服务,Authorize 后在 Swagger UI 操作) | Examples 下拉出现 6 项;选 `bin.feed` 点 Execute 得 `business_key`,状态节点看到 `PENDING` 消息 |
| T5(阶段二) | `dev_mock_wms.py` + 模式开关 | 逐模式验证消息终态与 §4.7 表一致 |
| T6 | `README` 加一小节:如何启用与配置 `RELIABLE_TARGETS` | — |

收尾:`uv run ruff check . && uv run ruff format --check . && uv run pytest`。

## 9. 评审记录

### 9.1 验证实验(临时原型,不在仓库内)

原型在 FastAPI 0.143.1 / Pydantic 2.14.1 上验证了以下判断:

- **V1 通过**:装饰器内 `create_model(__base__=req, scenario=Literal[name])` + `Union[tuple(models)]` + `Body(discriminator="scenario", openapi_examples=…)`,`/openapi.json` 产出 `oneOf` + `discriminator.mapping` + `examples`(每个场景一项,含 `summary`),两个场景分别路由正确。
- **V2 发现陷阱**:`Annotated[Union[...], Field(discriminator="scenario")]` 作为 body 类型时,OpenAPI 退化为 `anyOf` 且无 `discriminator`;非法 `scenario` 的错误列出所有分支的全部字段错误。→ §4.2 写成硬约束。
- **V3 通过**:未知 `scenario` → `union_tag_invalid`(经全局映射为 400);缺少 `scenario` → 校验失败。
- **V4 通过**:只有一个场景时 `Union[(M,)]` 加 `discriminator` 正常,不需要特殊分支。
- **未验证**:Swagger UI 在浏览器里对 `oneOf` + Examples 下拉的实际渲染(见 T4);mock 与真实 Sender 的端到端自调用(见 T5)。

### 9.2 评审发现与处理

| # | 级别 | 发现 | 处理 |
|---|---|---|---|
| F1 | 阻断 | 此前讨论的"业务包里放路由/mock"违反分层测试(E3:integrations 不得导入 fastapi) | 注册表放 `core`(仅 pydantic),路由放 `controllers`,业务包只放场景;mock 放 `controllers` |
| F2 | 阻断 | `get_session` 不提交(E1),直接调门面会"返回成功但消息不发" | 服务层 `submit` 成功后显式 `commit`;§7-3 固化 |
| F3 | 高 | 判别器写法陷阱(V2) | §4.2 硬约束 + T3 验收检查 `discriminator.mapping` |
| F4 | 中 | 此前承诺的响应字段 `replayed` 无法获得(门面只返回句柄) | 删除;改为"同号重放比较 `business_key`" |
| F5 | 中 | 不同业务同名请求模型会冲突 OpenAPI schema 名 | 生成模型名由 `name` 派生(§4.1-4) |
| F6 | 中 | 扫描若吞掉导入错误,场景会静默缺失 | 仅忽略"模块本身不存在",其余原样抛出(§4.2-2) |
| F7 | 中 | 示例不合法只有点开 Swagger 才暴露 | 登记期校验 example(§4.1-3)+ §7-2 |
| F8 | 中 | 未配置目标时得到 500 且难定位 | 挂载时 WARNING 带配置示例;不为此新增错误码(只在 local,收益小) |
| F9 | 低 | 不用 `require_permission` 意味着任何登录用户可触发 | 仅 local;§6 明文约束,不放宽 |
| F10 | 低 | mock 的模式存进程内存、需单 worker、受全局限流影响 | 写入 §4.7 约束;阶段二且待确认 |
| F11 | 低 | 注册表是模块级全局,测试里不宜清空 | 测试采用规则式断言(§7-2),不依赖清空/重建 |
| F12 | 信息 | 示例中的 Zone/储位/滚筒线编码无现场依据 | 标为占位,联调前替换(§4.5) |

### 9.3 待用户决定

1. **是否做阶段二 Mock WMS(T5)**。不做则本说明书只能验证"入队 + 重试/过期",看不到 ACK 终态;做则 `RELIABLE_TARGETS` 需指向本机自身。建议做,但可在 T1~T4 落地后再做。
2. 之前讨论中的模块名 `dev_scenarios` 改为 `dev`(同一文件既放场景,以后也可放该业务的其它 dev 辅助),如无异议按此执行。

### 9.4 评审局限

本评审为作者自审加原型实验,未请独立评审者;未运行仓库测试(尚无实现);Swagger UI 的真实渲染与 Sender 自调用未实测。

## 10. 实施与验收记录(2026-10-22)

- **实现**:`app/core/dev_scenarios.py`、`app/schemas/dev_outbound.py`、`app/services/dev_outbound_service.py`、`app/controllers/dev.py`、`app/controllers/dev_mock_wms.py`、`app/integrations/wms_transport/dev.py`,`app/main.py` 加一行挂载。测试:`tests/core/test_dev_scenarios.py`、`tests/controllers/test_dev_outbound.py`(按 §7,另加"Mock WMS 的响应经 `TransportSubmitAdapter` 判定与预期一致"这一条长期合同)。
- **自动化**:`ruff check`、`ruff format --check` 通过;全量 `pytest` 1030 passed、243 skipped(跳过项是未配置 `TEST_DATABASE_URL` 的既有集成测试)。
- **端到端(临时 SQLite 库 + 随机端口 + `RELIABLE_TARGETS` 指向自身,未碰开发库)**:真实发送器自调用 Mock WMS,终态与 §4.7 一致:`accept`→DONE、`duplicate`→DONE、`conflict`→FAILED/CONFLICT、`reject`→FAILED/REJECTED、`unavailable`→重试后 STALLED、`garbage`→PENDING(UNKNOWN,待重试)。`hang`(目标超时 2 秒)→ 首次投递以 `TOTAL_TIMEOUT` 结束并标记 `delivery_uncertain`,随后按固定间隔重试(轮询时第 2 次投递在途,状态 SENDING)。
- **浏览器(Swagger UI)**:`POST /api/v1/dev/outbound` 的 Examples 下拉出现 6 项,选中后请求体自动填充,§9.1 未验证项已关闭。
- **实施中的调整**:嵌套请求模型加 `Transport` 前缀(`Slot`、`Route` 等泛名会在不同业务间冲突 OpenAPI schema 名)。
- **Postgres 实测**:在本机 25432 实例里临时新建库、`alembic upgrade head`、种子管理员后,同一组 6 种模式的终态与 SQLite 完全一致;库已删除,未碰开发库。请求日志里的 3 条 ERROR 是 `unavailable` 模式有意返回的 503。
- **仍未做**:无。
