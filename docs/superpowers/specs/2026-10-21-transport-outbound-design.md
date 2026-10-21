# WMS Transport 出站:协议适配器与能力层开发说明书

状态:**P0～P2 工程评审完成,待实施;生产启用条件仍见 §9** · 合同依据:`wes_backend/docs/contracts/transport-fulfillment-contract.md`(Approved,0.4.0,下称「合同」)· 承接:`2026-10-20-interop-framework-design.md`、`2026-10-21-reliable-interaction-plan.md`

评审已确认范围(2026-10-21,D1 A):首轮交付 P0～P2;P3 只保留后续设计,其 DTO、规则、冻结及 `RackStateReader` 端口均延后,随可信位置投影或明确调用方推进。本文相关 P3 描述不属于首轮实施与验收。

> 本文覆盖两部分:**P0 可靠层扩展**(未送达预算、未知事实保护和可选固定间隔,§5),以及 Transport 业务域的**两层**:协议适配器、能力层。业务接入层(货架入站/退库、料箱投料/回架)随后续业务逐步扩展,本文只为它预留接缝。入站回调(合同 §5)、结果消费、位置投影不在本文范围。

## 1. 目标与边界

### 1.1 目标

工作线/业务代码能用合同 §3.2 的四个方法(`move_rack`、`rotate_rack`、`move_bins`、`exchange_bins`)在**自己的数据库事务内**登记一次搬运义务;义务由已有可靠层冻结、投递、重试,直到 WMS 给出确定 ACK。

### 1.2 分层与位置

```text
业务接入层(后续)   货架入站/退库、料箱投料/回架,只调用能力层
        │
能力层             app/integrations/wms_transport/   校验、冻结、幂等、入队        ← 本文
        │
协议适配器         app/integrations/wms_transport/   实现 ProtocolAdapter          ← 本文
        │ ProtocolAdapter 端口
app/reliable → app/http_client    基础层,仅可靠层按 P0 扩展
```

- Transport 是**业务域**,不属于基础层。协议适配器也属于业务域,它只是实现基础层的端口。
- 依赖只能向下:`app.integrations` 可导入 `app.reliable`、`app.http_client`、`app.models`、`app.core`;基础层不得导入 `app.integrations`(`tests/architecture/test_layering.py` 已有该条)。新增约束见 §8。
- 将来其它业务域(如 ECS)是 `app/integrations/` 下的平级包,互不依赖。

### 1.3 非目标

- 不做入站:`/api/v1/wms/events` 路由、`transport.task.member_position_changed@v1`、`transport.task.resulted@v1`、Evidence、位置投影、`outcome_version` 发布。
- 不做业务接入层,不选货架/储位,不做业务重试(合同 T1:确定拒绝/取消后的重试属于调用方,且必须换新 `client_request_id`)。
- 除 §5 的 P0(预算、未知事实保护与可选固定重提间隔)外,不改 `app/http_client`、`app/reliable`;不新增通用重试策略表/配置 DSL。
- 不生产启用:不在 `app/main.py` 注册适配器、不配置真实 `RELIABLE_TARGETS`。启用须等 §9 的偏差项被负责人确认(与接入指南 §5.2 一致)。
- 不提供取消、暂停、轮询、人工重发入口(合同 §10)。

## 2. 前置事实(已核对)

| 事实 | 来源 |
|---|---|
| 可靠层唯一身份 `(target, operation, operation_id)`;`MessageDraft(target, operation, operation_id, request_raw, ttl, business_key)`;入队时会调用适配器 `build_request` 并 `validate_request` | `app/reliable/outbox.py` |
| 适配器端口仅 `build_request(message, target)`、`classify_response(message, result)`;`Verdict` 四种:DELIVERED / NOT_RECEIVED / PEER_TERMINAL / UNKNOWN | `app/reliable/types.py` |
| 发送器对 DELIVERED 置 `DONE`;PEER_TERMINAL 置 `FAILED(reason)`;NOT_RECEIVED、UNKNOWN、非完整响应都**持久退避重试,不设终止次数,只告警** | `app/reliable/delivery.py` |
| 适配器抛异常 → `FAILED(LOCAL_ERROR)`,不重试 | 同上 |
| `TargetConfig` 没有「提交路径」字段,路径由适配器给出 | `app/reliable/config.py` |
| Python 3.13 标准库没有 `uuid.uuid7`;项目未依赖 UUIDv7 库 | 环境 |
| 离线实验已有可抄的 `RACK_MOVE` 请求构造与 ACK 分类雏形(学习切片) | `docs/reliable-transport-lab.md` |

## 3. 协议适配器

位置:`app/integrations/wms_transport/adapter.py`、`protocol.py`。同步、纯函数式:不查库、不发网、不读时钟、不写日志副作用。

### 3.1 常量(`protocol.py`,合同条款为唯一来源)

- `OPERATION = "transport.task.submit@v1"`,可靠层 `protocol` 注册名 `"wms-transport"`。
- `REJECTION_REASONS`:`INVALID_ENVELOPE`、`UNSUPPORTED_OPERATION`、`INVALID_DATA`、`COORDINATED_BIN_EXCHANGE_UNSUPPORTED`,以及合同 §4.2 的完整中英文拼接字面量(逐字复制,不得改动)。
- `RCS_TEMPLATES = {CTU01, CTU02, CTU03, F01}`;`KINDS = {RACK_MOVE, RACK_ROTATE, BIN_MOVE, BIN_EXCHANGE}`。

### 3.2 `TransportSubmitAdapter(submit_path)`

- 构造参数 `submit_path`,默认 `/api/v1/wes/transport-requests`;按合同 §4.1 校验:以 `/` 开头、≤2048 字符、不含 query/fragment/反斜杠/`.` `..` 段,违反即 `ValueError`。(大小写保持原样。)
- `build_request(message, target)`:
  1. 要求 `message.operation == OPERATION`;`request_raw` 为 JSON 对象,其 `operation`、`operation_id` 与 `message` 一致,`data.transport_task_id` 为非空字符串;否则 `ValueError`(→ 可靠层记 `LOCAL_ERROR`)。
  2. 返回 `HttpRequest("POST", submit_path, headers=accept/content-type: application/json, body=message.request_raw)`。**正文原样透传**,可靠层会校验未被改动。
  3. 不重复做 DTO 业务校验——那是能力层的职责(§4.2),适配器只做身份自洽的防御检查。
- `classify_response(message, result)`(仅在 `HTTP_RESPONSE_COMPLETE` 时被调用,但仍要对 `delivery_state`/`failure_kind` 不符的输入返回 UNKNOWN):

| 条件 | Verdict |
|---|---|
| 状态 400 或 413,且 body 为 `b""` | `PEER_TERMINAL / REJECTED` |
| body 非严格 UTF-8 JSON 对象(含非法 UTF-8、任意层重复 key、`NaN/Infinity`、非对象) | `UNKNOWN` |
| `operation_id` 不是请求原值,或 `timestamp` 不是 `0..2^63-1` 的整数(bool 不算),或 `data` 不是对象 | `UNKNOWN` |
| `data.transport_task_id` 缺失或不等于请求值(对本适配器所发的合法请求,所有分支都必须回显) | `UNKNOWN` |
| (202, `RECEIVED`)、(200, `DUPLICATE`) | `DELIVERED`,`response == result.body` 原字节 |
| (409, `CONFLICT`) | `PEER_TERMINAL / CONFLICT` |
| (422, `REJECTED`) 且 `data.reason_code` 是 `REJECTION_REASONS` 之一(精确匹配) | `PEER_TERMINAL / REJECTED` |
| (422, `REJECTED`) 且 reason 缺失/未知/类型错误 | `UNKNOWN` |
| (503, `UNAVAILABLE`) | `NOT_RECEIVED`,无等待提示 |
| 其它任何组合:200+RECEIVED、202+未知 code、429、502、401、HTML、空 body 的其它状态 | `UNKNOWN` |

解析口径(相对离线实验的 T1 增量):已定义字段严格校验;**冗余字段忽略**,包括冗余字段里的浮点数(实验一律拒绝浮点是学习限制,不是合同要求);`reason_code` 以外的冗余 `reason_code` 不进入结果;不检查响应 `Content-Type`(合同未对 ACK 响应头设保证,正文才是权威)。

## 4. 能力层

位置:`app/integrations/wms_transport/`(`types.py`、`wire.py`、`service.py`),模型 `app/models/transport.py`(一张表,一个迁移)。

### 4.1 公共类型

首轮仅实现 `move_rack` 所需公共类型;`RackBinSlot`/`HandoffPosition` 及料箱相关 DTO 属 P3 后续设计,此处列出不代表首轮实现。

- `TransportCaller(workline_id: str, station_id: str | None)`:只落本地库,**不进入线上请求**(合同 §3.1、§4.1)。
- `TransportHandle(transport_task_id, client_request_id)`:只证明义务已登记,**不证明** WMS 已接纳或搬运完成(合同 §3.1)。
- 位置:`Rack(code)`、`Zone(code)`、`RackPosition(code)`、`RackBinSlot(rack_id, rack_face, slot_id)`、`HandoffPosition(code)`,均为冻结 dataclass,字符串字段拒绝空串与 NUL。
- 异常:复用 `app.reliable.InvalidMessage` 不合适(那是入队层),新增 `InvalidTransportRequest(ValueError)`、`IdempotencyConflict(ValueError)`。

### 4.2 校验(纯函数,先于任何数据库写入;规则全部来自合同 §3.2/§3.3/§4.1,测试从条款推导)

下表仅 `move_rack` 属首轮;其余三个方法及 `RackStateReader` 端口均为 P3 后续设计,不要求首轮实现或验收。

| 方法 | 线上 `kind` | 规则要点(完整规则见合同对应条款) |
|---|---|---|
| `move_rack` | `RACK_MOVE` | 位置 ∈ RACK/ZONE/RACK_POSITION;`Rack.code == rack_id`;source ≠ target;模板 ∈ 闭集,省略即 `F01`;`target_face` 可选,`None` 时线上**省略该字段**(不发 `null`) |
| `rotate_rack` | `RACK_ROTATE` | position ∈ RACK/RACK_POSITION;`target_face` 必填;需可信当前位置与当前面,目标面 ≠ 当前面;wire 的 `source`=传入位置,`target`=可信精确原点位 |
| `move_bins` | `BIN_MOVE` | 成员 1..4;`bin_code` 不重复;位置 ∈ RACK_BIN_SLOT/HANDOFF_POSITION 且至少一端是 RACK_BIN_SLOT;单成员 source ≠ target;`RACK_BIN_SLOT` 全请求不重复;同一 `rack_id` 只有一个 `rack_face`,且等于可信当前面 |
| `exchange_bins` | `BIN_EXCHANGE` | 1..2 个交换对;展开成 2 或 4 个成员,形成 1..2 个互不重叠的二元闭环;位置仅 RACK_BIN_SLOT;涉及的 (rack_id, rack_face) 组 ≤2;同货架不混面;面等于可信当前面 |

- `target_face`、`rack_face`:非空字符串,1..10 个 Unicode code point,无 NUL,按 code point 精确保存,不做 trim/大小写/规范化(合同 §5.3 的持久化口径;§3.2 写「不限长度」,取更严的 §5.3 并列入 §9 待确认)。
- 「可信当前面/位置」来自位置投影,模板里**没有**。能力层只定义一个端口 `RackStateReader`(异步,`rack_id → (position, face) | None`);**未注入或返回 None 一律失败关闭**。因此依赖它的方法分阶段交付(§7)。

### 4.3 冻结:身份与字节(`wire.py`)

- `operation_id`:UUIDv7,小写 canonical。标准库无实现,自写约 10 行(RFC 9562:48 位毫秒时间 + 版本 7 + 变体),时钟与随机源可注入。
- `transport_task_id`:`"transport-" + uuid4`(与合同示例同形态;格式约束未在已读条款中明文,列入 §9)。
- `timestamp`:UTC Unix 毫秒整数,取注入时钟。
- 信封键序固定 `operation_id, operation, timestamp, data`;`data` 键序固定 `transport_task_id, kind, rack_id, rcs_template_id, source, target, target_face`(货架族)/ `transport_task_id, kind, moves`(料箱族);`moves` 按 `container_id` 升序,每项 `container_id, source, target`;`BinMove.bin_code` 在线上改名 `container_id`。
- 序列化 `json.dumps(ensure_ascii=False, separators=(",",":"))` → UTF-8 bytes。冻结后**永不重新生成**:重放读回已存字节。冻结边界检查最终 `len(request_raw) <= 262144`,超限在插入任务前抛 `InvalidTransportRequest`,不留任务或消息;该固定协议上限不受目标 `max_body_bytes` 调大影响。基础 `validate_request` 继续执行目标自身的限额校验,不替代 Transport 固定上限。(评审问题 7A 已批准。)

### 4.4 幂等与入队(`service.py` + 表 `transport_task`)

首轮 `TransportService(outbox, *, target="wms", clock, uuid7, uuid4)` 仅公开 `move_rack`,签名与合同 §3.2 一致,另在最前加 `session: AsyncSession`;P3 再加入 `state_reader` 及其余三个方法:

```text
move_rack(session, *, client_request_id, caller, rack_id, source, target,
          target_face=None, rcs_template_id="F01") -> TransportHandle
```

表 `transport_task`(只存能力层独有事实,请求字节仍只在 `reliable_message.request_raw` 一处,不复制):

| 列 | 说明 |
|---|---|
| `id` | 主键 |
| `client_request_id` | **唯一**;首次形成输入时由调用方生成(UUIDv7),与调用方业务记录同事务保存 |
| `transport_task_id` | **唯一**,能力层首次接纳时生成 |
| `operation_id` | **唯一**,等于 submit 的 `operation_id` |
| `kind` | 四种之一 |
| `input_digest` | 对「方法名 + 规范化入参(已套默认值)」的 SHA-256,**不含**时间戳/`operation_id`——重放时线上字节不同,但输入相同 |
| `workline_id`、`station_id` | 本地路由/诊断用 |
| `created_at` | |

流程(全部在调用方传入的 `session` 内,**不 commit**):

1. 校验输入(§4.2)→ 失败抛 `InvalidTransportRequest`,**零写入**。
2. 按 `client_request_id` 查 `transport_task`:存在且 `input_digest` 相同 → 返回原 `TransportHandle`(不生成新身份、不入队);digest 不同 → `IdempotencyConflict`。
3. 不存在 → 在同一保存点内生成 `operation_id/transport_task_id/timestamp`、冻结字节,先插入 `transport_task`(`on_conflict_do_nothing` 于 `client_request_id`)。只有插入成功者才调用 `outbox.enqueue(session, MessageDraft(target, OPERATION, operation_id, bytes, ttl=None, business_key=transport_task_id))`;竞争失败者不入队,重读赢家并按第 2 步处理。
4. 保存点覆盖本次任务插入和入队,入队失败须回滚本次任务行并抛出异常;调用方即使捕获异常后提交外层事务,也不得留下半成品。不得回滚调用方已有业务写入;实现时须考虑 `AsyncSession.begin_nested()` 进入保存点前的自动 flush,避免把调用方待写入状态误认为保存点内写入。
5. 调用方事务回滚 → 两行一起消失。并发同号同输入最终只能提交一条任务和一条可靠消息;同号异输入抛 `IdempotencyConflict`,不得留下竞争失败者的可靠消息。(评审问题 1A 已批准。)

`ttl=None`:合同没有搬运提交的过期语义,不引入。

### 4.5 接口预留(本文不实现)

业务接入层需要的「提交状态」读取(`reliable_message` 状态 → `PENDING / ACCEPTED / REJECTED / CONFLICT / ERROR`)与「业务重试需新 `client_request_id`」均在首个业务动作落地时再加,避免没有调用方的接口。

## 5. P0:基础层改动——确认未送达的发送次数预算

依据合同 §4.3:「`NOT_SENT` 与明确未接纳的 `503` 最多发送 3 次;接收结果未知时不受该预算截断;预算耗尽不能形成 `REJECTED`,必须保留原身份与字节」。已决定(2026-10-21):**改基础层,次数 N 作为配置参数,耗尽后停发并挂起**。

### 5.1 语义

- **计数对象**:一次发送尝试以「确认未送达」结束才计数——`HttpResult.delivery_state == NOT_SENT`,或适配器判定 `VerdictKind.NOT_RECEIVED`(Transport 的明确 `503`)。`DELIVERY_UNKNOWN`、`UNKNOWN` 判定、`PENDING` 重试以外的任何其它结果**不计数、也不清零**(合同:未知结果的重提不受预算截断)。
- **未知事实优先(评审问题 2A 已批准)**:增加持久化布尔列 `delivery_uncertain`,默认 `false`。出现 `DELIVERY_UNKNOWN`、适配器 `UNKNOWN`,或发送中断/租约回收时置为 `true`;后续单次 `NOT_SENT`/`NOT_RECEIVED` 不能清除它。该标记随结果写回或租约回收在同一事务保存,并受现有租约/状态条件隔离,旧 worker 不得覆盖新状态。消息一旦具有未知事实,在取得确定结论前不得因未送达预算进入 `STALLED`,而应持续原身份重提;未送达计数仍累计供观测。进程重启不得丢失该事实。
- **存量迁移(评审问题 4A 已批准)**:升级时对所有既有 `PENDING`/`SENDING` 消息保守回填 `delivery_uncertain=true`,原因是旧表仅保存最后结果,不能证明历史从未出现未知。升级后新建消息默认 `false`;迁移不改变原身份、正文、状态或租约,不从总尝试数推算历史未送达次数(`not_received_count` 从 0 开始)。存量消息中即使存在实际未发送者,也优先保持原身份自动消歧,不以猜测恢复预算约束。
- **安全降级(评审问题 6A 已批准)**:`transport_task` 非空时拒绝删除该表;可靠消息存在 `STALLED`,或活跃消息带有 `delivery_uncertain=true`/非零 `not_received_count` 时,拒绝移除新状态与字段。拒绝不得删除任务、改写原身份/正文/状态或推进降级版本;检查与变更须在可防止并发写入绕过检查的迁移事务/锁保护下完成。不能保留现有事实时,明确拒绝即为正确降级结果,不得把 `STALLED` 改为可发送状态来凑降级成功。
- **配置**:`TargetConfig.max_not_received_attempts: int | None = None`,`None` 表示不设上限(现有目标行为不变);随 `RELIABLE_TARGETS` 的 `defaults`/单目标覆盖配置,按正整数校验。Transport 目标设 `3`。
- **耗尽**:仅当 `delivery_uncertain == false` 时,第 N 次确认未送达的结果落库使消息进入新状态 `STALLED`(而不是 `PENDING`):
  - 保留 `operation_id`、`request_raw` 与已有计数/诊断事实,不写 `fail_reason`,**不是**拒绝、不是终态;既有表保存总尝试数和最后结果,本文不增加逐次尝试表;
  - 不再被领取、不再发送、不设 `next_attempt_at`;不参与 `expires_at` 过期与保留期清理(清理只针对终态);
  - 输出一条告警日志(含 `target`、`operation`、`operation_id`、`attempts`、`not_received_attempts`,不含报文字段)。
- **退出**:只有两种途径,均由调用方显式触发,基础层不自行恢复:
  1. `Outbox.resume(session, target, operation, operation_id)`:`STALLED → PENDING`,`not_received_count` 清零,`next_attempt_at=now`,保持原字节,不得清除 `delivery_uncertain`。
  2. 权威结果到达时由业务域把它结束掉(T3,属入站,**本文不实现**;需要时再设计结束接口)。
- **固定重提间隔(评审问题 3A 已批准)**:`TargetConfig.fixed_retry_interval_s: float | None = None`,配置时必须为正、有限数值且不是 bool,支持 `RELIABLE_TARGETS` 的 defaults/单目标覆盖。现有 `retry_delay_s` 在配置该值时使用固定的本地延迟 `local`,否则保留随机退避;正常发送重试与租约恢复共用该函数,不增加独立调度器。合法等待提示仍按既有公式 `max(local, min(retry_after_ms / 1000, max_retry_after_s))` 处理,因此通用协议的提示可以延长本地固定值;Transport 不读取等待提示,最终计划延迟即配置固定值,真实装配时须显式配置。固定的是失败后到下次计划尝试的等待时间,实际发送受轮询和运行负载影响。此公式是对已批准等待提示兼容规则的展开,不新增策略。

### 5.2 改动清单

```text
app/reliable/types.py        MessageStatus 增 STALLED
app/reliable/config.py       TargetConfig 增 max_not_received_attempts(默认 None,正整数校验)、fixed_retry_interval_s(默认 None,正有限数校验)
app/reliable/policy.py       retry_delay_s 支持可选固定间隔,默认保留原随机退避及等待提示规则
app/reliable/delivery.py     判定「确认未送达」;无未知事实且达到 N → AttemptOutcome(STALLED)
app/reliable/store.py        finish 写 not_received_count/delivery_uncertain 并支持 STALLED;中断/租约回收持久化未知标记;新增 resume;领取/过期/清理查询保持只认 PENDING / 终态
app/reliable/outbox.py       暴露 resume
app/models/reliable.py       列 not_received_count(默认 0)、delivery_uncertain(默认 false);状态 CHECK 增 STALLED;next_attempt_at 允许 STALLED 无计划时间
migrations/versions/<日期>_reliable_stalled.py   加列 + 改 CHECK/next_attempt_at 空值约束 + 存量回填 + 安全降级保护(SQLite 与 PostgreSQL)
tests/reliable/*             见 §8
docs/…/spec-019b-reliable-interaction.md        REQ-DLV-03 改为「默认不设上限;配置预算后耗尽转 STALLED」;状态表与「无人工恢复入口」原则同步修订
```

### 5.3 与既有原则的冲突(须在修订 019b 时一并处理)

019b 原则:「没有 DEAD 状态、无人工恢复入口、出路只有重试续送和 `expires_at`」。`STALLED` 不是终态,但 `resume` 是一个恢复入口。本文的取舍:预算是**按目标可选**的,不启用预算的目标原则完全不变;启用预算的目标才出现 `STALLED`,其退出路径窄且显式(`resume` 与将来的权威结果收敛)。`resume` 由谁调用(运维脚本、业务接入层、T3 入站处理)留给后续决定,本文只保证接口存在、语义明确。

## 6. 文件清单

```text
app/integrations/__init__.py
app/integrations/wms_transport/{__init__,protocol,adapter,types,wire,service}.py
app/models/transport.py            + app/models/__init__.py 导出
migrations/versions/<日期>_transport_task.py   (命名沿用 2026-10-21_reliable_message.py)
tests/integrations/wms_transport/{test_adapter,test_wire,test_service,test_e2e}.py
tests/architecture/test_layering.py   增加 integrations 的禁止项
```

## 7. 交付顺序(每步可独立验证)

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 基础层预算 | §5 | 预算计数/耗尽/不计数/`resume`/默认 `None` 兼容的永久测试通过;`tests/reliable` 全量保持绿;迁移在 SQLite 与 PostgreSQL 升降级通过 |
| P1 适配器 | §3 | 合同 §4.2 ACK 矩阵(接入指南 §5.3 的 9 行为基线)+ 边界用例全过;无 DB/网络 |
| P2 冻结与 `move_rack` | §4.1~§4.4 的 `move_rack`、`transport_task` 表与迁移 | 合同 §4 示例请求的字节与黄金样例一致;幂等/冲突/回滚/并发唯一性通过;经真实 `Outbox`+`Sender`+`ScriptedHttpClient` 到 `DONE`;另以 PostgreSQL+真实 `AsyncHttpClient`+本地 HTTP 对端验证完整发送和丢响应恢复 |
| P3 后续,不在首轮交付 | `move_bins`/`exchange_bins`/`rotate_rack` 的 DTO、规则、冻结;`RackStateReader` 端口 | 随可信位置投影或明确调用方推进;届时规则条款全覆盖、未注入端口时失败关闭 |

P3 的方法在位置投影落地前不对业务暴露(不导出到包的公共接口)。

## 8. 验证与测试原则

- 只固化**长期合同**,每条用例能指回合同条款或接入指南 §5.3 矩阵;不为凑覆盖率写测试。
- P0 预算:只有 `NOT_SENT`/`NOT_RECEIVED` 计数,`DELIVERY_UNKNOWN`/`UNKNOWN` 既不计数也不清零。未出现未知事实时第 N 次后 `STALLED`,身份与字节不变、不再被领取、不被过期或清理;未知后再遇三次确认未送达仍须可重试。发送中断、租约回收及重启后保留未知事实;旧租约写回不得改变新状态。`resume` 后回 `PENDING` 且计数清零,不清除未知标记;预算为 `None` 时与改动前行为一致。
- P0 间隔:不同尝试次数均使用配置的固定延迟,发送重试与租约恢复一致;拒绝零、负数、bool、NaN/Infinity;未配置时保持原随机退避和等待提示行为。
- P0 存量升级:SQLite 与 PostgreSQL 均验证旧 `PENDING`/`SENDING` 回填未知标记、终态消息与新建消息保留正确默认语义,原身份/正文/状态/租约不变;升级后存量活跃消息不因预算挂起。
- 安全降级:两种数据库空库往返成功;存在 Transport 幂等映射时拒绝删表,存在 `STALLED` 或活跃安全事实时拒绝丢弃新状态/字段;拒绝后数据、约束和迁移版本不变。`next_attempt_at` 的模型、迁移和写回行为一致支持 STALLED 无计划时间,恢复到旧非空约束前须确认不存在不兼容行。
- 适配器:ACK 矩阵;重复 key、非法 UTF-8、非对象、bool 时间戳、错误/缺失任务 ID、错误 `operation_id`、未知 code/reason、429/502/401/HTML、冗余浮点字段(接受)、已定义字段浮点(拒绝)。
- 冻结:黄金字节、`target_face=None` 省略、`moves` 升序、UUIDv7 版本/变体位、`transport_task_id` 形态。
- Transport 固定上限:目标 `max_body_bytes` 调大后仍拒绝超过 262144 字节的请求;精确边界允许、超出 1 字节拒绝;中文等多字节输入按最终 UTF-8 bytes 判断,失败时任务和消息皆无写入。`moves` 排序测试属于 P3 后续验收,不进入首轮。
- 能力层:同号同输入返回原 handle 且 `reliable_message` 仍 1 行;同号异输入冲突;校验失败零写入;事务回滚两表皆空。真实 PostgreSQL 并发同号同输入只留一条任务和一条可靠消息;并发同号异输入冲突且无多余可靠消息;入队失败后调用方捕获异常并提交,仍无本次任务或消息半成品,调用方原有写入保留。
- HTTP 集成(评审问题 5A 已批准):`tests/integrations/wms_transport/test_e2e.py` 使用独立 PostgreSQL、真实 `TransportService`/`Outbox`/`Sender`/`AsyncHttpClient` 与本地 HTTP 对端。验证实际请求路径、headers、原始正文字节及身份;首次 ACK 后不再发送;对端完整收请求后在响应前断开连接,重新创建发送器后以相同身份与相同字节重提,由合法 `DUPLICATE` 收敛。此测试不调用真实 WMS,不证明物理完成。复用现有本地服务器模式,需共享的夹具放公共 conftest/helper,不得跨测试文件导入;脚本客户端继续覆盖完整 ACK/故障矩阵。
- 分层:`integrations` 禁止导入 `app.controllers`、`app.schemas`、`app.services`、`fastapi`、`celery`;`reliable`/`http_client` 反向依赖测试保持。
- 全量:`uv run pytest tests/integrations tests/architecture tests/reliable`;ruff 与现有配置一致。

## 9. 偏差与待确认

**须负责人确认(影响真实启用):**

| # | 事项 | 现状与建议 |
|---|---|---|
| D1 | 合同「`NOT_SENT`/`503` 最多 3 次」(T2) | **已决定**:改基础层,次数进配置参数,耗尽停发挂起;曾出现未知事实时禁止预算挂起。增加可选 `fixed_retry_interval_s`,Transport 装配时显式配置,不再用相等的退避上下限近似固定间隔(§5) |
| D2 | `409 CONFLICT`:合同进 `RECONCILING` 并告警 | 可靠层为终态 `FAILED(CONFLICT)`,仅状态记录,无专门告警。需确认是否足够,或增加告警出口 |
| D3 | 已接纳前原任务权威结果先到(T3);`STALLED` 的权威结果收敛出口 | 属入站,不在本文;期间可能多发幂等重提,对端按身份去重 |

**我的判断(可评审时推翻):**

- `resume` 的调用方、`STALLED` 的运维告警渠道,首个真实启用前再定。
- `target_face` 采用 1..10 code point 的较严口径;不检查 ACK 响应 `Content-Type`;`transport_task_id` 用 `transport-<uuid4>`——合同已读条款未写任务 ID 格式约束,联调前向 WMS 核对。
- 启用前须确认 WES 到 WMS 的链路上没有会返回空 400/413 的网关或代理:按合同,空 400/413 判为确定拒绝(终态 `REJECTED`),基础设施偶发的空响应也会因此永久拒绝一条合法请求。
- `Outbox.resume` 的恢复日志在调用方事务提交后输出,回滚则不输出(已实现并有测试)。
- 提交路径作为适配器构造参数,不新增 Settings 项;配置化随首次真实启用再做。
- `transport_task` 只存能力层事实,请求字节不复制;表无清理策略(体量小,与 90 天的可靠消息清理脱钩)。
- 位置投影不在本文;`rotate_rack`、`move_bins`、`exchange_bins` 的面校验因此分到 P3,且失败关闭。

## 10. 明确不做

入站路由与 Evidence、T3 权威结果结束接口、`outcome_version`、业务重试循环、容量名额/滚动窗口、取消/改派、WMS/RCS 轮询、动态 target、全局注册表改写、`main.py` 生产注册、业务接入层模块。

## 11. 工程评审记录(2026-10-21～22)

目标为本文,采用 `plan-eng-review`。评审只修改设计,未实现 Transport、未运行迁移或新测试,未启用真实目标。首轮交付范围已收敛为 P0～P2。

### 11.1 决策与证据

| 问题 | 级别/置信度 | 原方案与证据 | 已批准方向 |
|---|---|---|---|
| 范围 D1 | 范围决策 | 原 §7 P3 无投影、暂不公开,但拟先实现 DTO/规则/冻结 | 用户 A:首轮 P0～P2,P3 延后;不撤销既定 P0 预算方向 |
| 1. 并发幂等 | P1,9/10 | 原 §4.4「先 outbox.enqueue,再插 transport_task」;Outbox 只按 target/operation/operation_id 去重,不同 operation_id 的竞争失败者消息不会被撤销 | 用户 1A:先争任务唯一键,仅赢家入队,保存点保护两步和调用方事务 |
| 2. 未知后预算 | P1,8/10 | 原 §5.1「未知不计数也不清零;第 N 次确认未送达即 STALLED」;后续单次未送达不能证明此前未知请求未被接纳 | 用户 2A:持久化未知事实,后续未送达不触发预算挂起,覆盖中断/回收/重启/旧租约隔离 |
| 3. 固定间隔 | P2,10/10 | 原 §5.1「retry_base_s == retry_cap_s 近似固定间隔」;policy.py 实际为 uniform(0,ceiling),10/10 参数实测可返回 0～10 秒 | 用户 3A:可选 fixed_retry_interval_s,正常发送和租约恢复共用,默认行为兼容 |
| 4. 存量未知 | P1,9/10 | 新标记默认 false;旧模型只有最后结果且 store.finish 会覆盖 last_* 字段,不能重建完整未知历史 | 用户 4A:存量 PENDING/SENDING 保守回填 true,新建默认 false,不改身份正文状态租约 |
| 5. HTTP 组合证据 | P2,9/10 | 原 P2 仅「Outbox+Sender+ScriptedHttpClient 到 DONE」,没有 Transport 接到真实 AsyncHttpClient 的组合验证 | 用户 5A:独立 PostgreSQL+本地 HTTP,验证实际报文、ACK 后停发及丢响应后原身份字节恢复 |
| 6. 数据降级 | P1,9/10 | 原 §7 只要求 SQLite/PostgreSQL「升降级通过」;现有基础迁移已对非空表拒绝破坏性降级 | 用户 6A:已有任务映射或 STALLED/活跃安全事实时拒绝破坏性降级,保持数据约束版本 |
| 7. 固定协议上限 | P2,9/10 | 原 §4.3 声称 validate_request 兜底256KiB;实际只校验可调大的目标max_body_bytes | 用户 7A:冻结边界按最终UTF-8字节数检查262144,插入任务前抛领域错误并保证零写入 |

以上均为设计缺陷或验收缺口,不是已实现 Transport 代码的运行事故。固定间隔结论已通过调用现有函数验证;其余拟定行为必须由实施测试证明。

### 11.2 What already exists

- `Outbox.enqueue`:同事务登记、可靠消息身份去重、冻结正文和公开 `validate_request` 校验;直接复用,业务唯一键由 Transport 层负责。
- `ProtocolAdapter`/`Verdict`:请求构造和 ACK 分类端口已具备,不另建协议引擎。
- `MessageStore`/`Sender`:带租约条件写回、回收、有限批次领取与发送并发;P0 在原路径增加预算和未知保护,不叠加调度器。
- `retry_delay_s`:正常发送与恢复共用的延迟计算接缝,可选固定间隔在此实现。
- `AsyncHttpClient`:真实 HTTP、正文上限、单次访问结果分类及可注入传输,不为 Transport 改写。
- 现有 PostgreSQL 并发测试、本地 HTTP 服务器模式和分层测试可复用;共享夹具放公共文件,不跨测试文件导入。

### 11.3 NOT in scope

- P3 料箱搬运/交换/换面的 DTO、校验、冻结、状态读取端口:用户已选择延后,随可信投影或明确调用方推进。
- 入站路由、Evidence、位置投影、T3 权威结果结束提交、outcome_version:保持原有后续边界,首轮不证明物理完成。
- 409 的业务 RECONCILING/专门告警、STALLED 运维渠道和 resume 调用方:仍是 §9 的真实启用前条件,本评审不把它们视为已完成。
- 生产注册、真实 WMS/RCS 联调、部署、提交与推送:本次只审设计,没有这些动作。
- 通用策略 DSL、独立重试调度器、逐次尝试表、新发布物或发布流水线:目标可由现有设施满足,不新增。

没有新增独立 TODO 候选,`TODOS.md` 更新提案为 0;已有延期事项继续保留在本文,不另建 TODO 文件。

### 11.4 测试覆盖与失败模式

pytest/pytest-asyncio 为现有框架。下图标注的是已批准的测试要求,不是现有实现覆盖率;未开发代码不能标为 TESTED,也不计算虚构的行覆盖率。

```text
调用方流程                                  计划验证层级
move_rack(session, input)
  +-- 非法输入 -> InvalidTransportRequest     单元/服务:零写入
  +-- 已有 client_request_id
  |     +-- 同输入 -> 原 handle               服务:不生成新消息
  |     `-- 异输入 -> IdempotencyConflict      服务:原数据保留
  `-- 首次登记/并发登记:先冻结并检查固定字节上限,超限零写入
        +-- 任务唯一键失败 -> 重读赢家         PostgreSQL:同/异输入、赢家提交/回滚
        `-- 唯一键赢家 -> enqueue              保存点:失败无半成品,外层写入保留
              +-- 外层回滚 -> 两表皆空        [->E2E] 调用方事务
              `-- 外层提交 -> Sender
                    +-- 合法接纳 ACK -> DONE [->E2E] 真实客户端+本地HTTP,不再发送
                    +-- 明确拒绝/冲突        适配器矩阵+可靠状态
                    +-- NOT_SENT/503
                    |     +-- 无未知且耗尽 -> STALLED -> resume
                    |     `-- 已有未知 -> 原身份继续重提
                    `-- 未知/中断/租约回收
                          `-- 持久保护 -> 重建Sender -> DUPLICATE [->E2E]

固定间隔 -> 正常重提/回收共用;未配置 -> 保持原退避
迁移升级 -> 旧活跃消息未知保护;新建消息默认false
迁移降级 -> 空库往返;有受保护事实 -> 拒绝且数据/版本不变 [->E2E]
```

| 路径与生产失败场景 | 计划测试位置/类型 | 错误处理和可见结果 |
|---|---|---|
| 校验/冻结:非法位置、NUL、错误类型、面值边界、UUID或编码错误 | test_wire.py/test_service.py,单元+服务 | InvalidTransportRequest,零写入;默认值与省略字段按合同验证 |
| 幂等:两个调用方同时登记同号,或入队失败被外层捕获 | test_service.py/test_e2e.py,PostgreSQL | 原handle或明确冲突;赢家仅一个消息,失败保存点回滚且不伤外层事务 |
| ACK:错误任务身份、重复key、异常状态/code或未知reason | test_adapter.py+可靠层测试,单元/服务 | UNKNOWN继续原身份重提;合法拒绝/冲突仅结束提交消息,不伪造物理结果 |
| 预算:未送达累计耗尽 | tests/reliable,单元+存储 | STALLED及告警;不领取/过期/清理;resume保留原身份正文 |
| 未知:响应丢失后又连续连接失败,或worker退出 | tests/reliable及test_e2e.py,PostgreSQL+HTTP | 持久未知保护,原身份继续重提;旧租约写回无效 |
| 间隔:相等退避参数仍随机,或恢复路径忽略固定值 | test_config.py/test_delivery.py,单元+服务 | 显式固定值共用计算;非法配置启动失败,默认目标兼容 |
| 升级:旧最后结果无法反映此前未知 | tests/migrations,SQLite+PostgreSQL | 保守回填;原始数据不改,不推断发送历史 |
| 降级:丢任务映射、新状态或活跃安全字段 | tests/migrations,SQLite+PostgreSQL | 明确拒绝,数据约束版本不变;不静默恢复发送 |
| 固定上限:目标HTTP限额调大放行超合同正文 | test_wire.py/test_service.py,单元+服务 | 最终bytes超262144时在任务插入前抛InvalidTransportRequest,零写入 |

测试审查新增缺口 2 项(HTTP组合、安全降级),均已批准完整验收。其余新分支的证明随对应已批准修正实施;新目标不是回归事故。默认无预算、默认随机退避、原TTL/清理与租约隔离的兼容性为关键回归要求。没有 LLM/prompt 产品改动,无需模型质量 eval。

性能审查:No issues found。P2 单次登记使用唯一键查询/插入,没有批量业务扫描或逐成员查询;冻结正文最多256KiB,不增加缓存。现有领取/过期/租约/保留期查询有状态与时间索引,STALLED 排除于领取和终态清理。共享发送器已有有限并发和批次。本结论来自源码与设计检查,没有做负载测试,不宣称吞吐上限。

### 11.5 Worktree parallelization strategy

| Step | Modules touched | Depends on |
|---|---|---|
| P0 预算、未知保护、固定间隔和迁移保护 | app/reliable/, app/models/, migrations/, tests/reliable/, tests/migrations/, docs/ | — |
| P1 Transport协议适配器 | app/integrations/wms_transport/, tests/integrations/wms_transport/ | —(使用现有端口) |
| P2 move_rack登记与真实HTTP集成 | app/integrations/wms_transport/, app/models/, migrations/, tests/integrations/wms_transport/, tests/conftest.py | P0+P1 |

Lane A:P0顺序实施,共享可靠层及迁移模块。Lane B:P1独立实施。A+B可在独立feature worktree并行;合并并核对单Alembic head后再实施P2。P2与A共享models/migrations、与B共享Transport模块,不得并行修改这些模块;共享测试夹具在P2统一接入。此处仅分析并行机会,本次未创建worktree或派发实现任务。

### 11.6 独立复核结果

外部 Claude Code CLI 执行返回完成,实际modelUsage报告`glm-5.3`,但结果结尾没有要求的`Recommendation: ... because ...`完成标记,校验失败。外部覆盖记为unavailable,不能当作有效外部通过;原始意见已在会话完整展示,使用本机独立子审查作fallback,不把它冒充外部覆盖。

本机独立复核问题7(P2,9/10):§4.3 声明正文≤256KiB且由`validate_request`兜底;但该函数只检查可覆盖的目标限额,基础`TargetConfig.max_body_bytes`没有协议固定上限。目标调大后,Transport可能冻结和投递超合同正文。

D8 比较7A(冻结时按最终字节数拒绝,抛领域错误)和7B(适配器构造请求时拒绝,由Outbox校验路径报错);二者都不改通用目标配置,都测试精确边界、多字节和目标调大。State:approved;Actual answer:用户`7A`;Accepted scope:在Transport冻结边界检查最终正文≤262144字节,超限在任务插入前抛InvalidTransportRequest,无任务/消息半成品,并实施相应边界测试。

### 11.7 Suppressed findings

- 外部F1 caller摘要歧义(4/10):原文「方法名+规范化入参」可以包含caller,没有已实现digest函数可证明漏算;合同中的client_request_id还是全局唯一UUIDv7,不是按workline局部唯一。保留原规范化输入语义,不把外部猜测当作已证实缺陷或自动改变路由策略。
- 外部F2等待提示疑问:3A已经明确保留现有提示规则,现将公式展开;「基础层自动从响应头提取提示」的假设与delivery.py通过适配器Verdict读取提示的现有实现不符,不成立。
- 外部F3 resume接口细节(4/10):既有PENDING过期规则仍适用,Transport本身ttl=None;原文仅允许STALLED→PENDING,没有证据表明会修改其它状态。不以尚未选择的返回类型推定状态缺陷;首轮不增加TTL延长或任意状态重发语义。
- 外部F4 P2/P3描述范围:按已批准D1 A补充阶段标签及首轮构造器说明,不增加新范围。
- 外部F5缩小存量回填:4A已明确接受包括实际未发送旧消息在内的保守保护,保持批准方案;不借独立建议重开既定取舍。

本机独立意见与已批准修正没有新的冲突;唯一新增具体问题7已逐项获用户7A批准。没有未经批准吸收外部意见。

### 11.8 Completion Summary

- Step 0:Scope reduced per recommendation,首轮P0～P2,P3延期。
- Architecture Review:3 issues found,1A/2A/3A均批准。
- Code Quality Review:1 issue found,4A批准。
- Test Review:diagram produced,2 gaps identified,5A/6A均批准;测试要求已记录,没有声称新测试已通过。
- Performance Review:0 issues found;未负载测试。
- Independent challenge:1具体问题,7A批准;本机复核完成,外部CLI结果未通过完成标记校验。
- NOT in scope / What already exists:均已写明。
- TODOS.md updates:0 items proposed,不创建无新增内容的TODO文件。
- Failure modes:0 unresolved critical gaps in the reviewed plan;实现及其证明仍待完成。
- Outside voice:attempted Claude Code(modelUsage:glm-5.3),outside_status=unavailable;native fallback completed,不等同有效外部覆盖。
- Parallelization:2 lanes(A=P0,B=P1)可并行,P2在两者合并后顺序实施。
- Lake Score:4/4完整覆盖选项被选择(2A/3A/5A/6A);其余为范围/机制选择,不计覆盖分。
- Unresolved decisions:0 in P0～P2 review;§9真实启用前条件仍保留,本次不授权启用。

实施时建议仅在`app/reliable/store.py`状态/租约转换及`app/integrations/wms_transport/service.py`保存点流程确有必要时放置简短ASCII图,解释为什么旧写回无效、为什么只有赢家入队;普通DTO/纯函数不加装饰性图。本文已提供完整数据流图。

STATUS:DONE_WITH_CONCERNS。原因:已批准的7项修正均进入设计,但外部覆盖不可用,且实施/运行验收未开始。下一步按feature分支实施P0/P1,二者合并后实施P2;无需UI设计评审,本次也不扩大到产品方向评审。

### 11.9 参考与评审产物

[Layer 1] 使用现有Outbox、数据库唯一键和保存点,不另造事务或重试平台。[SQLAlchemy保存点文档](https://docs.sqlalchemy.org/en/20/orm/session_transaction.html#using-savepoint)说明begin_nested进入前会flush,因此保存点测试需保留调用方事务语义;[PostgreSQL INSERT文档](https://www.postgresql.org/docs/current/sql-insert.html)定义ON CONFLICT与RETURNING,可用于识别唯一键赢家。[Python 3.13 uuid文档](https://docs.python.org/3.13/library/uuid.html)没有UUIDv7生成函数,本设计仍需小型实现及版本/变体测试。

测试计划:`~/.gstack/projects/kaizhoumasha-maintenance-admin/kaizhou-develop-eng-review-test-plan-20261022-000007.md`。
任务JSONL:`~/.gstack/projects/kaizhoumasha-maintenance-admin/tasks-eng-review-20261022-000007.jsonl`。

## Implementation Tasks

仅从本次发现派生,原有P0/P1/P2基础实施仍见§7。估时为粗略工作量,不代表已经执行。

- [ ] **T1 (P1, human: ~2–3h / CC: ~20–35min)** — Transport幂等 — 先插业务任务,仅唯一键赢家入队。
  - Surfaced by: Architecture,问题1;原§4.4并发竞争失败者会留下独立可靠消息。
  - Files: app/integrations/wms_transport/service.py, tests/integrations/wms_transport/test_service.py, tests/integrations/wms_transport/test_e2e.py。
  - Verify: PostgreSQL同号同/异输入竞争、赢家提交/回滚、入队失败捕获后外层提交及外层回滚。
- [ ] **T2 (P1, human: ~0.5d / CC: ~30–60min)** — 可靠发送 — 持久保存未知事实并保护预算判定。
  - Surfaced by: Architecture,问题2;未知后续遇未送达不能推断原请求未接纳。
  - Files: app/reliable/{types,delivery,store,outbox}.py, app/models/reliable.py, tests/reliable/。
  - Verify: 未知后连续未送达仍重提、全明确未送达挂起、resume、中断/回收/重启、旧租约隔离、默认目标兼容。
- [ ] **T3 (P2, human: ~1–2h / CC: ~15–30min)** — 重提间隔 — 在共享延迟计算中支持可选固定值。
  - Surfaced by: Architecture,问题3;uniform(0,ceiling)不是固定间隔。
  - Files: app/reliable/config.py, app/reliable/policy.py, tests/reliable/test_config.py, tests/reliable/test_delivery.py。
  - Verify: 正有限配置、正常发送与回收一致、跨尝试次数不变、默认退避及等待提示兼容。
- [ ] **T4 (P1, human: ~1–2h / CC: ~15–25min)** — 存量迁移 — 对既有活跃消息保守回填未知标记。
  - Surfaced by: Code Quality,问题4;旧表无法恢复完整未知历史。
  - Files: migrations/versions/<date>_reliable_stalled.py, tests/migrations/test_reliable_message.py。
  - Verify: SQLite/PostgreSQL旧PENDING/SENDING、新消息及终态行;原身份/正文/状态/租约不变。
- [ ] **T5 (P2, human: ~0.5d / CC: ~30–60min)** — HTTP集成 — 验证实际请求及丢响应后原身份恢复。
  - Surfaced by: Tests,问题5;原ScriptedHttpClient闭环没有真实HTTP边界证据。
  - Files: tests/integrations/wms_transport/test_e2e.py, tests/conftest.py(仅在共享夹具需要时)。
  - Verify: 独立PostgreSQL+本地HTTP,真实客户端捕获路径/headers/bytes,首次ACK停止发送,丢响应后重建发送器并由DUPLICATE收敛。
- [ ] **T6 (P1, human: ~0.5d / CC: ~30–45min)** — 安全降级 — 拒绝删除任务映射和活跃安全事实。
  - Surfaced by: Tests,问题6;仅空库往返不足以证明数据安全。
  - Files: migrations/versions/<date>_{reliable_stalled,transport_task}.py, tests/migrations/test_reliable_message.py, tests/migrations/test_transport_task.py。
  - Verify: 空库往返,有任务/挂起/活跃安全事实时拒绝且数据约束版本不变;检查与变更不能被并发写入绕过。
- [ ] **T7 (P2, human: ~0.5h / CC: ~5–10min)** — Transport固定上限 — 冻结边界拒绝超256KiB正文。
  - Surfaced by: Independent challenge,问题7;通用HTTP可配置限额不能保证固定协议上限。
  - Files: app/integrations/wms_transport/wire.py, tests/integrations/wms_transport/test_wire.py, tests/integrations/wms_transport/test_service.py。
  - Verify: 目标限额调大仍受262144字节限制,精确边界/超出1字节/多字节输入,失败零写入。

_No new tasks from Performance._

实施后的相关检查:`uv run pytest tests/integrations/wms_transport tests/architecture tests/reliable tests/http_client tests/migrations`;PostgreSQL需显式配置可丢弃独立测试库,跳过不算验收通过。另运行`uv run ruff check .`和`uv run ruff format --check .`。本次只评审,未执行这些验收。

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | — | 本次未改变产品方向 | 0 | NOT RUN | 不要求 |
| Outside Review | Claude Code CLI → native fallback,host=codex | 独立质疑 | 本次外部1+本机1 | 外部 unavailable;本机 completed | 外部结果缺完成标记;本机1项已获7A批准 |
| Eng Review | `/plan-eng-review` | Architecture & tests(required) | 7天内5,本次1 | CLEAR(PLAN,SCOPE_REDUCED) | 架构3+代码质量1+测试2+独立复核1=7,均已确认;未决0 |
| Design Review | — | 无UI范围 | 0 | NOT REQUIRED | — |
| DX Review | — | 本次未做独立DX审查 | 0 | NOT RUN | — |

**OUTSIDE COVERAGE:** phase=plan-review,host=codex,provider=claude-code;CLI报告modelUsage为glm-5.3,执行返回但完成标记校验失败,outside_status=unavailable。本机fallback完成,模型身份未知,不等同有效外部覆盖。原始意见中既定3A/4A范围保持,未自动采纳新建议。

**VERDICT:** ENG CLEARED,可以按P0～P2实施。STATUS=DONE_WITH_CONCERNS:外部覆盖不可用,新实现/测试/迁移均待执行;§9条件未关闭,不得据此启用真实目标。没有授权提交、推送、部署或生产调度。

NO UNRESOLVED DECISIONS
