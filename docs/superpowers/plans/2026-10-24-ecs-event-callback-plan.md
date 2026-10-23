# ECS 事件回调接收 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `POST /api/v1/callback/event`,按白皮书 v1.1 接收 ECS 设备事件:校验、幂等、持久接收、ACK、按 `event_type` 分发到函数式处理器,ECS 的 wire 不变。

**Architecture:** 新包 `app/integrations/ecs_callback/` 提供独立入口和函数式处理器注册表;复用 `callback_receipt`、诊断快照、`canonical_digest`、`json_object`、`import_module_tree`、`CallbackPayload`。共享底座做三处小改(请求体读取、诊断队列、快照限额按来源分组),迁移加宽 `callback_receipt.operation_id` 并给 `callback_snapshot` 加可空 `source` 列。

**Tech Stack:** Python ≥3.13、FastAPI、SQLAlchemy 2 async、Pydantic v2、Alembic、pytest(SQLite 与 PostgreSQL 双参数,PostgreSQL 需 `TEST_DATABASE_URL`)。

**Spec:** `docs/superpowers/specs/2026-10-24-ecs-event-callback-design.md`(已通过两轮工程评审)。白皮书:`../wes_backend/docs/integration/third_party_integration_whitepaper.md` §4.2。

## Global Constraints

- ECS wire 一个字节不改:路径 `/api/v1/callback/event`;成功 `{"code":200,"message":"ACK"}`;`400 INVALID_ENVELOPE` + `error_detail.issues[]`;`413 PAYLOAD_TOO_LARGE`;`503 TEMPORARILY_UNAVAILABLE`;错误响应都含整数 `code` 和字符串 `message`。
- 入口上限 `256 * 1024` 字节;未注册事件落库诊断整体上限 32 KiB,预览 1 KiB,超限时 `device_code`/`event_type` 截 128 字符;日志与告警里不可信字段用 `repr` 并截 64 字符。
- 收据:`operation="ecs.event"`、`operation_id`=规范化身份 SHA-256 十六进制 64 位、`compare_mode="RAW"`、`resp_timestamp`取当前毫秒(`app.core.clock.unix_ms`)。
- 分层(`tests/architecture/test_layering.py`):`app/integrations` 不得导入 `fastapi`、`app.controllers`、`app.services`、`app.schemas`、`celery`。
- 测试只固化长期合同,不为填覆盖率加测试。不引入 `Operation`/`CallbackOperation`/`Outcome` 到 ECS。UUID 只走 `app/core/identifiers.py`(本特性不生成 UUID)。通用机制命名用 `callback`。
- 验证命令:`uv run pytest -ra`、`uv run ruff check .`、`uv run ruff format --check .`。提交信息用中文约定式提交,**不加 Co-Authored-By 署名行**(用户偏好)。

## Review Focus

- 请求体 `{"data":{"n":1e400}}`(解析成 `inf`)、空正文、非 UTF-8 字节、数组根:回 `400`,不写收据,不是 `503`(Task 7)。
- 正文很小但 `device_code` 长达 200,000 字符的未注册事件:落库诊断总大小 ≤32 KiB,日志行有界(Task 8)。
- 事件先按未注册类型 ACK,随后注册要求某个 `data` 字段的处理器:同一事件原样重报仍得 `200 ACK`(Task 7)。
- `is_debug` 为 `"true"`/`1`/`null`、`data` 为 `null`、`timestamp` 为 `true`:`400` 且 `issues[]` 不含客户端的值(Task 5、7)。
- 并发重发同一事件:处理器只执行一次,两个请求都回 `200 ACK`(Task 10)。

## File Structure

| 文件 | 职责 |
|---|---|
| `app/core/request_body.py`(新) | `read_body_capped`:分块读请求体并在上限处截断,WMS 与 ECS 控制器共用 |
| `app/integrations/callback_diagnostics.py`(新) | `queue_diagnostics`:诊断入队 + 提交后输出告警日志 + 回滚丢弃,WMS 与 ECS 共用 |
| `app/integrations/wms_callback/snapshots.py`(改) | `allow_snapshot(source)` 按来源分组限额;`record_snapshot` 写 `source` |
| `app/integrations/wms_callback/receiver.py`(改) | `_handle` 改调 `queue_diagnostics`,行为不变 |
| `app/models/callback.py`、`migrations/versions/2026-10-24_ecs_event_receipt.py` | `operation_id` 加宽到 64、`callback_snapshot.source` |
| `app/integrations/ecs_callback/envelope.py` | `EcsEventEnvelope`、`issues_from` |
| `app/integrations/ecs_callback/registry.py` | `ecs_event` 注册表、`EcsEventContext` |
| `app/integrations/ecs_callback/discovery.py` | 导入 `<业务>/ecs_event[/]`、`dev_ecs_event` |
| `app/integrations/ecs_callback/receiver.py` | `receive_ecs_event`、未注册处理、诊断上限 |
| `app/controllers/ecs_callback.py`、`app/main.py` | 路由、提交后最终日志、快照、注册 |
| 测试 | `tests/core/`、`tests/integrations/wms_callback/test_snapshots.py`、`tests/migrations/test_ecs_event_receipt.py`、`tests/integrations/ecs_callback/`、`tests/controllers/test_ecs_callback.py` |

并行建议:Task 1–4(共享底座与迁移)互相独立可并行;Task 5–6 依赖无;Task 7 起依赖前面全部。

---

### Task 1: 共享请求体读取

**Files:**
- Create: `app/core/request_body.py`
- Modify: `app/controllers/wms_events.py`(用共享函数替换内联循环)
- Test: `tests/core/test_request_body.py`(新);回归 `tests/controllers/test_wms_events.py`

**Interfaces:**
- Produces: `async def read_body_capped(request: Request, limit: int) -> bytes` — 返回长度至多 `limit + 1`;`len(result) > limit` 表示超限(调用方回 413,仍可用返回的前缀做快照)。

- [ ] **Step 1: 回归基线** — Run: `uv run pytest tests/controllers/test_wms_events.py -q` — Expected: PASS(记下通过数)。
- [ ] **Step 2: 写失败测试** `tests/core/test_request_body.py`:`test_returns_whole_body_within_limit`、`test_stops_reading_one_byte_past_limit`(分三块流入 300 字节、limit=100,断言 `len(result) == 101` 且没有读完全部块)、`test_empty_body_is_empty_bytes`。用一个只有 `stream()` 异步迭代器的 `SimpleNamespace` 伪造 request。Run → FAIL(模块不存在)。
- [ ] **Step 3: 实现** `read_body_capped`,逻辑照搬 `wms_events.py` 现有循环(`raw.extend(chunk[: limit + 1 - len(raw)])`,`len(raw) > limit` 即 break)。`app/core` 允许导入 starlette/fastapi。
- [ ] **Step 4: 改 `wms_events.py`** 调用 `read_body_capped(request, MAX_BODY_BYTES)`,其余逻辑(413、except 兜底、快照)不动。
- [ ] **Step 5: 验证** — Run: `uv run pytest tests/core/test_request_body.py tests/controllers/test_wms_events.py -q` — Expected: PASS,WMS 通过数与基线相同。
- [ ] **Step 6: 提交** `refactor: 请求体分块读取抽到 app/core 共用`

### Task 2: 迁移与模型(operation_id 加宽、snapshot.source)

**Files:**
- Create: `migrations/versions/2026-10-24_ecs_event_receipt.py`(`revision="a024a20261024"`,`down_revision="a023a20261024"`)
- Modify: `app/models/callback.py`(`CallbackReceipt.operation_id: String(64)`;`CallbackSnapshot.source: Mapped[str | None] = mapped_column(String(16))`)
- Test: `tests/migrations/test_ecs_event_receipt.py`

**Interfaces:**
- Produces: 表结构 `callback_receipt.operation_id VARCHAR(64)`、`callback_snapshot.source VARCHAR(16) NULL`。

- [ ] **Step 1: 写失败测试**,结构照 `tests/migrations/test_callback.py`(`callback_db` 参数 sqlite/pg、`migrate` 辅助,`BASE="a023a20261024"`、`REVISION="a024a20261024"`):
  - `test_upgrade_accepts_64_char_receipt_identity_and_snapshot_source`:升级后插入 `operation_id="a"*64` 的收据和 `source="ecs"` 的快照,读回相等。
  - `test_downgrade_is_refused_when_64_char_identity_or_source_exists`:有 64 位身份行时 `downgrade` 返回码非 0 且数据保留;清掉身份行但留 `source` 非空快照同样被拒。
  - `test_empty_schema_roundtrips`:无数据时升降级往返成功。
  Run → FAIL(修订不存在)。
- [ ] **Step 2: 实现迁移**。upgrade:PostgreSQL 用 `op.alter_column("callback_receipt","operation_id", existing_type=sa.String(36), type_=sa.String(64), existing_nullable=False)`(SQLite 不强制长度,跳过);两种库都 `op.add_column("callback_snapshot", sa.Column("source", sa.String(16)))`。downgrade:PostgreSQL 先 `LOCK TABLE ... ACCESS EXCLUSIVE`(沿用 `2026-10-22_callback_receipts_and_snapshots.py` 的写法),若存在 `length(operation_id) > 36` 或 `source IS NOT NULL` 行则 `raise CommandError(...)`,否则收窄列并 `op.drop_column`。
- [ ] **Step 3: 改模型**(见 Files)。
- [ ] **Step 4: 验证** — Run: `uv run pytest tests/migrations -q`(PostgreSQL 分支需 `TEST_DATABASE_URL`,未设会 skip)— Expected: PASS;另跑 `uv run alembic heads` 只有一个头 `a024a20261024`。
- [ ] **Step 5: 提交** `feat(db): callback_receipt.operation_id 加宽到 64,callback_snapshot 加 source 列`

### Task 3: 快照限额按来源分组

**Files:**
- Modify: `app/integrations/wms_callback/snapshots.py`、`tests/conftest.py`(`callback_store` 夹具重置新状态)
- Test: `tests/integrations/wms_callback/test_snapshots.py`(新);回归 `tests/controllers/test_wms_events.py`

**Interfaces:**
- Consumes: Task 2 的 `CallbackSnapshot.source`。
- Produces: `allow_snapshot(source: str = "wms") -> bool`;`record_snapshot(factory, *, preview, truncated, response, request_id, received_at, source: str | None = None)`。

- [ ] **Step 1: 写失败测试**:`test_each_source_has_its_own_quota`(`SNAPSHOTS_PER_MINUTE` 用 monkeypatch 设为 2:`ecs` 连取 2 次后第 3 次 False,此时 `wms` 仍 True);`test_default_source_is_wms`;`test_record_snapshot_stores_source`(`source="ecs"` 落库为 `"ecs"`,缺省为 `None`)。Run → FAIL。
- [ ] **Step 2: 实现**:把 `_window_start/_written/_dropped` 三个模块全局改成按来源的字典(单一 `_windows: dict[str, ...]` 亦可),窗口与丢弃告警日志保持原语义,日志里带上来源名。
- [ ] **Step 3: 更新 `tests/conftest.py` 的 `callback_store`**:原来 monkeypatch 的三个属性换成新状态的重置;确认 `tests/controllers/test_wms_events.py:192` 对 `SNAPSHOTS_PER_MINUTE` 的用法仍有效。
- [ ] **Step 4: 验证** — Run: `uv run pytest tests/integrations/wms_callback tests/controllers/test_wms_events.py tests/integrations/wms_transport -q` — Expected: PASS。
- [ ] **Step 5: 提交** `feat(wms_callback): 快照限额按来源分组并记录 source`

### Task 4: 共享诊断队列

**Files:**
- Create: `app/integrations/callback_diagnostics.py`
- Modify: `app/integrations/wms_callback/receiver.py`(`_log_diagnostics`、`_drop_diagnostics` 及 `_handle` 里入队那段迁走;保存点执行留在原处)
- Test: 无新增;必跑回归 `tests/integrations/wms_transport/test_callback.py`(含 `test_abnormal_callback_alerts_only_after_its_receipt_commits`、`test_callback_diagnostic_follows_the_callers_savepoint`)、`tests/integrations/wms_callback/`、`tests/controllers/test_wms_events.py`

**Interfaces:**
- Produces: `def queue_diagnostics(session: AsyncSession, details: list[dict]) -> None` — 首次调用时给 `session.sync_session` 注册 `after_commit`(输出 `logger.warning("callback diagnostic %r", detail, extra=detail)`)和 `after_rollback`(丢弃),按当前(嵌套)事务排队,嵌套事务提交时并入父事务。`detail` 形如 `{"operation":..., "operation_id":..., "diagnostic": {...}}`,日志 record 的属性必须仍是 `operation`、`operation_id`、`diagnostic`(现有测试断言这些属性)。

- [ ] **Step 1: 回归基线** — Run 上面三组回归测试 — Expected: PASS(记下通过数)。
- [ ] **Step 2: 抽取**:把 `receiver.py` 里 `sync_session.info["callback_diagnostics"]` 的监听注册与入队、`_log_diagnostics`、`_drop_diagnostics` 原样搬到新模块并导出 `queue_diagnostics`;`_handle` 调用它,行为不变。不抽保存点与 `_answer`。
- [ ] **Step 3: 验证** — Run 同一组回归 — Expected: 通过数与基线相同;`uv run pytest tests/architecture -q` PASS。
- [ ] **Step 4: 提交** `refactor(wms_callback): 诊断队列抽到 callback_diagnostics 共用`

### Task 5: ECS 信封与 issues 翻译

**Files:**
- Create: `app/integrations/ecs_callback/__init__.py`(先只放包说明,Task 11 补全)、`app/integrations/ecs_callback/envelope.py`
- Test: `tests/integrations/ecs_callback/__init__.py`、`tests/integrations/ecs_callback/test_envelope.py`

**Interfaces:**
- Produces: `class EcsEventEnvelope(BaseModel)`:`device_code: str`(非空)、`event_type: str`(非空)、`timestamp: int`(严格,`0 ≤ x ≤ 2**63-1`,排除 `bool`)、`is_debug: bool = False`(严格,拒绝 `null`/字符串/数字)、`data: dict = {}`(拒绝 `null`),`extra="ignore"`。
  `def issues_from(error: ValidationError, prefix: str = "") -> list[dict]` — 每项只含 `field`、`code`,有期望类型时加 `expected`;`prefix` 如 `"data"`。

- [ ] **Step 1: 写失败测试**(`test_envelope.py`):
  - 合法最小体通过;省略 `is_debug`→`False`、省略 `data`→`{}`;多余顶层字段被忽略。
  - 参数化非法体 → `issues_from` 结果:缺 `device_code`→`[{"field":"device_code","code":"FIELD_REQUIRED"}]`;`timestamp=True`→`INVALID_TYPE`,`expected="integer"`;`timestamp=-1`→`INVALID_VALUE`;`is_debug="true"`/`1`/`None`→`INVALID_TYPE`,`expected="boolean"`;`data=None`→`INVALID_TYPE`,`expected="object"`;`device_code=""`→`INVALID_VALUE`。
  - `test_nested_array_error_uses_index_path_without_client_values`:`class M(CallbackPayload): samples: list[float]`,输入 `{"samples": [1.0, "SECRET"]}` → `issues_from(err, "data") == [{"field":"data.samples[1]","code":"INVALID_TYPE","expected":"number"}]`,且 `"SECRET"` 不在 `json.dumps(issues)` 里。
  - `test_unknown_pydantic_error_falls_back_to_invalid_value`(`Literal` 取值不符 → `INVALID_VALUE` 且无 `expected`)。
  Run → FAIL。
- [ ] **Step 2: 实现**。映射规则见设计 §5.3:`missing`→`FIELD_REQUIRED`;`*_type` 类错误→`INVALID_TYPE` 并按 `str/int/float/bool/dict/list` 映射 `string/integer/number/boolean/object/array`;其余一律 `INVALID_VALUE`;路径点分、数组下标 `[i]`、模型级错误取父路径;绝不透传 `msg/input/ctx`。
- [ ] **Step 3: 验证** — Run: `uv run pytest tests/integrations/ecs_callback/test_envelope.py -q` — Expected: PASS。
- [ ] **Step 4: 提交** `feat(ecs_callback): ECS 事件信封与 issues[] 翻译`

### Task 6: 处理器注册表与发现

**Files:**
- Create: `app/integrations/ecs_callback/registry.py`、`app/integrations/ecs_callback/discovery.py`
- Modify: `tests/integrations/test_operation_files.py`(加 ECS 事件文件名规则)
- Test: `tests/integrations/ecs_callback/test_registry.py`、`tests/integrations/ecs_callback/test_discovery.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True, kw_only=True) class EcsEventContext`:`session: AsyncSession`、`receipt_id: int`、`digest: str`、`device_code: str`、`event_type: str`、`timestamp: int`、`is_debug: bool`、`notes: list[dict] = field(default_factory=list)`;方法 `note(self, reason_code: str, **detail) -> None`。
  - `def ecs_event(event_type: str, data: type[CallbackPayload] | None = None)` — 装饰 `async def handler(ctx: EcsEventContext, data) -> None`;`event_type` 必须匹配 `^[A-Z][A-Z0-9_]*$`,否则 `ValueError("invalid event type ...")`;重复注册 `ValueError` 且消息含两个函数的 `module.qualname`。
  - `@dataclass(frozen=True) class Registered: func, data_model`;`def lookup(event_type: str) -> Registered | None`;`def events() -> dict[str, Registered]`;注册表为模块级字典 `_registry`。
  - `discovery.candidate_modules(environment: str) -> list[str]`(每个 `app.integrations.<包>` 下的 `ecs_event`,`local` 再加 `dev_ecs_event`)、`load_event_modules(environment: str) -> None`(用 `app.core.module_tree.import_module_tree`)。

- [ ] **Step 1: 写失败测试**:`test_registered_handler_is_found`、`test_invalid_event_types_rejected`(参数化 `scan_completed`、`1A`、`A-B`、`""`)、`test_duplicate_names_both_functions`、`test_context_is_independent_kw_only_dataclass`(构造时缺必填字段 `TypeError`;`note("X", a=1)` 追加 `{"reason_code":"X","a":1}`)、`test_dev_module_only_in_local`(类比 `wms_callback/test_discovery.py`)。夹具用 `monkeypatch.setattr(registry, "_registry", {})`。Run → FAIL。
- [ ] **Step 2: 实现**以上接口。`ecs_event` 返回原函数,不实例化。
- [ ] **Step 3: 文件名规则**:在 `tests/integrations/test_operation_files.py` 增加对 `app.integrations.<包>.ecs_event.<文件>` 的检查——文件名必须是 `event_type.lower()`;并仿其现有两个测试补一个「规则覆盖两个方向、单模块形式不检查」的用例。
- [ ] **Step 4: 验证** — Run: `uv run pytest tests/integrations/ecs_callback tests/integrations/test_operation_files.py tests/architecture -q` — Expected: PASS。
- [ ] **Step 5: 提交** `feat(ecs_callback): 事件处理器注册表、上下文与发现`

### Task 7: 接收器——校验、身份、查重(无处理器)

**Files:**
- Create: `app/integrations/ecs_callback/receiver.py`
- Test: `tests/integrations/ecs_callback/test_receiver.py`

**Interfaces:**
- Consumes: Task 5 `EcsEventEnvelope`/`issues_from`、Task 6 `lookup`、Task 2 模型;`app.core.strict_json.json_object/canonical_digest`;`app.integrations.wms_callback.receiver.LOCK_TIMEOUT`、`CallbackResponse(status_code, body, outcome)`。
- Produces: `MAX_BODY_BYTES = 256 * 1024`;`ACK = {"code": 200, "message": "ACK"}`;`def event_identity(envelope: EcsEventEnvelope) -> str`(`canonical_digest({device_code,event_type,timestamp,is_debug,data})`);`async def receive_ecs_event(session: AsyncSession, raw: bytes) -> CallbackResponse`。outcome 取值 `received`/`duplicate`/`invalid`/`unregistered`/`too_large`。

测试用夹具 `callback_store`(sqlite+PostgreSQL),每个用例 `async with factory() as session, session.begin(): resp = await receive_ecs_event(session, raw)`。

- [ ] **Step 1: 写失败测试**(此任务尚无执行路径,合法新事件暂按未注册 ACK 之前的校验与查重覆盖):
  - `test_oversize_body_is_413_with_code_and_message`:`MAX_BODY_BYTES + 1` 字节 → `413`,body `{"code":413,"message":"PAYLOAD_TOO_LARGE"}`。
  - `test_bad_json_variants_are_400_invalid_json`:参数化 `b""`、`b"\xff\xfe"`、`b"[]"`、`b'{"a":1,"a":2}'`、`b'{"data":{"n":1e400}}'`(合法信封字段齐全但含 `1e400`)→ `400`,`message=="INVALID_ENVELOPE"`,`issues==[{"field":"$","code":"INVALID_JSON"}]`,收据表为空。
  - `test_envelope_errors_are_400_without_receipt`:缺 `event_type` → `issues` 含 `{"field":"event_type","code":"FIELD_REQUIRED"}`,收据为空,响应体里不含任何客户端值。
  - `test_identity_ignores_extra_top_level_fields_and_default_is_debug`:`event_identity` 对「省略 `is_debug`」与「显式 `false`」、「省略 `data`」与「`{}`」、多余顶层字段相同。
  - `test_registered_data_failure_is_400_with_data_path`:注册一个 `data=Model(bin_code: str)` 的处理器后发 `data={}` → `issues==[{"field":"data.bin_code","code":"FIELD_REQUIRED"}]`,收据为空。
  Run → FAIL。
- [ ] **Step 2: 实现** `receive_ecs_event` 的步骤 1–5(设计 §5.2):大小→`json_object`→信封→`event_identity` 与 `raw_digest` 计算(`ValueError`/`RecursionError` → 同样的 `400 INVALID_JSON`)→**先按 `(operation="ecs.event", operation_id=identity)` 查收据,命中则返回其保存的响应(outcome `duplicate`)**→新身份才 `lookup` 并校验 `data`。写收据与执行留给 Task 8,本任务在这一步之后先返回 `ACK`(`received`)并**不写收据**,Task 8 补上。
- [ ] **Step 3: 验证** — Run: `uv run pytest tests/integrations/ecs_callback/test_receiver.py -q` — Expected: PASS。
- [ ] **Step 4: 提交** `feat(ecs_callback): 接收器校验、身份与先查重`

### Task 8: 接收器——写收据、执行处理器、未注册处理

**Files:**
- Modify: `app/integrations/ecs_callback/receiver.py`
- Test: `tests/integrations/ecs_callback/test_receiver.py`(追加)

**Interfaces:**
- Consumes: Task 4 `queue_diagnostics`、Task 6 `EcsEventContext`/`lookup`。
- Produces: `MAX_DIAGNOSTIC_BYTES = 32 * 1024`、`PREVIEW_BYTES = 1024`、`IDENTITY_FIELD_CHARS = 128`、`LOG_FIELD_CHARS = 64`;`def render_untrusted(value: str, limit: int = LOG_FIELD_CHARS) -> str`(`repr` 后截断,换行被转义);`def unregistered_note(event: dict) -> dict` — 事件规范化 JSON ≤32 KiB 时返回 `{"reason_code":"UNREGISTERED_EVENT_TYPE","device_code":…,"event_type":…,"data": <完整规范化事件>}`,超限时 `device_code`/`event_type` 截 128 字符并以 `{"digest","size","preview"(前 1 KiB)}` 代替 `data`,不在其他键重复完整字段。

- [ ] **Step 1: 写失败测试**:
  - `test_new_event_gets_ack_and_one_receipt`:收据 `operation=="ecs.event"`、`len(operation_id)==64`、`compare_mode=="RAW"`、`effective_digest is None`、`body==ACK`、`resp_timestamp>0`。
  - `test_replay_returns_same_ack_and_handler_runs_once`:注册计数处理器,同一事件发两次 → 都 `200 ACK`,计数为 1,第二次 outcome `duplicate`。
  - `test_replay_after_handler_validation_tightened_still_acks`:先无处理器发事件(未注册 ACK),再注册要求 `barcode` 的处理器,原样重发 → 仍 `200 ACK`。
  - `test_handler_exception_rolls_back_receipt_and_business_write`:处理器先写一行再抛 → `receive_ecs_event` 抛出,外层事务回滚后收据与业务行都不存在。
  - `test_unregistered_event_is_acked_with_full_event_in_diagnostic`:`diagnostic[0]` 的 `reason_code=="UNREGISTERED_EVENT_TYPE"` 且 `data` 等于规范化后的完整事件。
  - `test_oversized_unregistered_event_keeps_digest_size_preview_only`:`data` 约 40 KiB → 诊断无完整 `data`,含 `digest`、`size`、1 KiB `preview`,`len(json.dumps(diagnostic)) <= MAX_DIAGNOSTIC_BYTES`。
  - `test_long_device_code_with_tiny_data_stays_within_cap`:`device_code="D"*200_000`、`data={}` → 诊断总字节 ≤32 KiB,`device_code` 被截到 128 字符。
  - `test_log_alert_renders_untrusted_fields_bounded`:`caplog` 中告警记录的渲染串长度 ≤ 合理上限,换行已转义,且不含 `data` 键。
  - `test_ctx_note_is_persisted_and_alert_follows_commit`:注册处理器调用 `ctx.note("X", k=1)` → 收据 `diagnostic` 含该条,告警在提交后输出。
  Run → FAIL。
- [ ] **Step 2: 实现**设计 §5.2 步骤 6–7:`SET LOCAL lock_timeout`(PostgreSQL,沿用 `LOCK_TIMEOUT`)→`INSERT … ON CONFLICT DO NOTHING RETURNING id`(PostgreSQL/SQLite 方言分支照 WMS 接收器)→冲突则回到查重分支读取已提交收据→在外层事务直接 `await func(ctx, data)`(`data` 为模型实例,无模型时为原始 dict)→把 `ctx.notes` 写入收据 `diagnostic` 并经 `queue_diagnostics` 入队(入队的日志副本把 `device_code`/`event_type` 经 `render_untrusted` 渲染、剔除 `data`)。未注册走内置分支生成 `unregistered_note`,outcome `unregistered`。处理器异常不捕获(控制器映射 503)。
- [ ] **Step 3: 验证** — Run: `uv run pytest tests/integrations/ecs_callback -q` — Expected: PASS(sqlite 与可用的 postgres 分支)。
- [ ] **Step 4: 提交** `feat(ecs_callback): 收据写入、处理器执行与未注册事件处理`

### Task 9: 控制器、装配、最终日志

**Files:**
- Create: `app/controllers/ecs_callback.py`
- Modify: `app/main.py`(在 `include_wms_events(...)` 旁加 `include_ecs_callback(app, environment=settings.ENVIRONMENT)`)
- Test: `tests/controllers/test_ecs_callback.py`

**Interfaces:**
- Consumes: Task 1 `read_body_capped`、Task 3 `allow_snapshot("ecs")`/`record_snapshot(source="ecs")`、Task 7–8 `receive_ecs_event`、Task 6 `load_event_modules`。
- Produces: `router`(`POST /callback/event`,`dependencies=[Depends(RateLimit(None))]`,tag `"ECS callbacks"`),`def include_ecs_callback(app: FastAPI, *, environment: str, prefix: str = "/api/v1") -> None`。控制器流程:`read_body_capped` → 超限直接 `413` → `async with factory() as session, session.begin(): result = await receive_ecs_event(...)` → 任何异常映射 `CallbackResponse(503, {"code":503,"message":"TEMPORARILY_UNAVAILABLE"}, "error")` → **事务提交/异常映射之后**输出一行最终请求日志(字段 `event_type`、`device_code`〔均经 `render_untrusted`〕、`outcome`、摘要前 8 位、耗时毫秒;`event_type`/`device_code`/摘要可从响应侧不可得时省略)→ `allow_snapshot("ecs")` 通过则后台 `record_snapshot(source="ecs", …)`。

- [ ] **Step 1: 写失败测试**(夹具 `client`、`callback_store`,`PATH="/api/v1/callback/event"`):
  - `test_valid_event_is_acked`、`test_bad_envelope_returns_issues_without_echoing_values`、`test_oversize_is_413_with_json_body`。
  - `test_commit_failure_returns_503_and_logs_error_outcome`:仿 `tests/integrations/wms_transport/test_callback.py::test_abnormal_callback_alerts_only_after_its_receipt_commits` 用 `event.listen(engine.sync_engine, "commit", fail_once)` 让提交失败 → `503`、收据表为空、最终日志的 `outcome=="error"`(不是 `received`)。
  - `test_snapshot_is_recorded_with_ecs_source_and_wms_quota_unaffected`:发 `SNAPSHOTS_PER_MINUTE+1` 个 ECS 事件后,再发一个 WMS 回调仍产生快照;ECS 快照行 `source=="ecs"`。
  - `test_wms_events_endpoint_cannot_reach_ecs_handlers`:`POST /api/v1/wms/events` 发 `operation="ecs.event"` → `422` `UNSUPPORTED_OPERATION`,ECS 处理器计数为 0。
  Run → FAIL。
- [ ] **Step 2: 实现**控制器与 `include_ecs_callback`(沿用 `callback_session_factory` 的取法;Swagger 请求体显示核心字段 schema)。
- [ ] **Step 3: 验证** — Run: `uv run pytest tests/controllers/test_ecs_callback.py tests/controllers/test_wms_events.py -q` — Expected: PASS。
- [ ] **Step 4: 提交** `feat(ecs_callback): POST /api/v1/callback/event 路由与装配`

### Task 10: 接收机制的幂等与原子性合同

**Files:**
- Test: `tests/integrations/ecs_callback/test_reliability.py`

**Interfaces:** 仅消费 Task 8 的 `receive_ecs_event` 与 `callback_db`/`pg_sessions` 夹具。

- [ ] **Step 1: 写测试**(PostgreSQL 分支用 `callback_db` 的 `postgres` 参数,其余合同 sqlite 同跑):
  - `test_concurrent_identical_events_run_handler_once`:`asyncio.gather` 两个独立会话同时收同一事件 → 处理器计数 1,两个响应都 `200 ACK`,收据 1 行(仅 postgres 参数;sqlite 参数 `pytest.skip`)。
  - `test_waiter_takes_over_when_first_request_rolls_back`:首个会话在处理器后抛错回滚,等待中的第二个会话成功接管,最终收据 1 行、处理器成功执行 1 次。
  - `test_business_write_and_receipt_roll_back_together`、`test_commit_failure_leaves_no_ack_receipt`(`event.listen(..., "commit", fail_once)`)、`test_lost_response_replay_returns_same_ack`(提交成功后重放)。
- [ ] **Step 2: 运行** — Run: `TEST_DATABASE_URL=<独立测试库> uv run pytest tests/integrations/ecs_callback/test_reliability.py -q` — Expected: PASS(无 PostgreSQL 时 postgres 参数 skip,不得计作验证通过,收尾时必须在有库环境跑一次)。若发现实现缺陷,回 Task 8 修复。
- [ ] **Step 3: 提交** `test(ecs_callback): 接收机制的幂等与原子性合同`

### Task 11: 文档收尾与全量验证

**Files:**
- Modify: `app/integrations/ecs_callback/__init__.py`(包说明:职责、不负责什么、怎么新增一个事件类型含 `@ecs_event` 示例、处理器契约四条——只写当前事务/外发走 `Outbox`、自带幂等键、已通过校验不得抛异常、发布顺序)、`CHANGELOG.md`(`[Unreleased]` 下加「ECS 事件回调接收」条目,写明新增端点、迁移、三处共享改动)、`docs/superpowers/specs/2026-10-24-ecs-event-callback-design.md`(状态改为已实现并注明验证范围)
- Modify: `docs/superpowers/specs/device-command-integration.md`(只在「全链路与归属」处加一行指向 ECS 事件入口,不动出站部分)

- [ ] **Step 1: 写文档**(照 `app/integrations/wms_callback/__init__.py` 的体例)。
- [ ] **Step 2: 全量验证** — Run: `uv run ruff check . && uv run ruff format --check . && uv run pytest -ra` — Expected: 全绿;有 `TEST_DATABASE_URL` 时再跑一次 PostgreSQL 分支并在 CHANGELOG/收尾说明里写清是否执行过。
- [ ] **Step 3: 提交** `docs: ECS 事件回调接入说明与 CHANGELOG`

---

## Self-Review

- **Spec coverage:** §5.1 结构→T5–9;§5.2 流程各步→T7(1–5)、T8(6–7);§5.3→T5;§5.4 身份与迁移→T2、T7–8;§5.5 契约→T6、T11 文档;§5.6 响应表→T7、T9;§5.7 控制器与共享读取→T1、T9;§5.8 日志→T8、T9;§6 决定 1/2→T8、T2;§7 共享改动→T1、T3、T4;§8 测试→T5–10;§9 风险是文档项。
- **Type consistency:** `read_body_capped`、`allow_snapshot(source)`、`record_snapshot(..., source)`、`queue_diagnostics(session, details)`、`EcsEventContext`、`receive_ecs_event(session, raw) -> CallbackResponse`、`ecs_event`/`lookup` 在各任务中名称一致。
- **Proportion:** 11 个任务,代码块只含签名与断言;实现体留给实现者。
