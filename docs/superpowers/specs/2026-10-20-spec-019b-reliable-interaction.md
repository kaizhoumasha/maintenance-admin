# 可靠交互层(出站优先,协议无关)

状态:**出站首版已实现，离线 PostgreSQL 验证通过(保留已接受风险)** · 未启用真实目标、未部署 · 目录:`app/reliable/`

> 本文自完整设计中摘出,自成一体。首版只做**出站可靠**:事务发件箱、原身份持久化退避重试、租约领取与崩溃恢复、消息行上的尝试记录。同步调用、去重窗口校验、调试 API 与逐次事件记录已设计完整但**后移**,条文保留在 §12。核心**不理解信封、operation、`code` 词表**,这些属于协议适配器(由各业务域对接时提供)。
> HttpClient 合同：`send(HttpRequest) -> HttpResult`、`aclose()`；公开同步 `validate_request(config, request) -> None` 已实现，只依赖 `HttpClientConfig`，不需要客户端实例、事件循环或网络，与 `send` 复用请求校验。`HttpResult` 含 `delivery_state`(`NOT_SENT` / `DELIVERY_UNKNOWN` / `HTTP_RESPONSE_COMPLETE`)、`failure_kind`、HTTP 状态码、响应头、有界响应字节；一次 `send` 至多一次网络请求，不自动重试。

> **实现状态**：§1.3 首版已实现，永久测试覆盖 SQLite 状态合同与真实 PostgreSQL 并发/骤停恢复；配置与接入见[可靠投递参考](../../integrations/reliable-delivery.md)，验证入口见[开发测试](../../testing/README.md)。§12、安全、出口限流、业务域接入仍未实施。离线脚本化对端不构成真实协议、对端去重或业务/物理完成验收。

## 1. 范围

### 1.1 目标

- 对**不透明字节消息**提供:事务发件箱、原身份持久化退避重试、租约领取与崩溃恢复、消息行上的尝试记录与结构化状态日志、终态消息保留期清理。
- 通过 DI 使用 `HttpClient`,每次尝试只发一次 HTTP;**一个逻辑消息只有一个重试调度者**(本层)。
- 通过 **ProtocolAdapter 端口**(§2.2,首版仅两个)把"请求怎么构造、响应怎么分类"留给协议适配器,核心不 import 任何协议实现。

### 1.2 非目标

- 不解析 JSON、不识别 operation、不规定 `code` 词表、不生成 UUIDv7。
- **后移(设计保留在 §12)**:同步调用 `Outbox.call`;去重窗口校验;调试查询 API、证据导出与权限码;逐次事件记录与折叠。
- **首版只承诺投递,不提供程序化结果消费**:业务代码读不到异步出现的 `FAILED`/`EXPIRED`,只能看告警日志或直接查库;只读查询接缝与调试 API 一并后移。
- **移出范围**:`shadow` / `step` 审批模式与运行期暂停开关——没有真实对端需求,且会连带引入审批状态机、Redis 键、读取失败语义与额外权限码。
- 不含业务 payload、handler、WMS/Transport 接入(业务域接入)。
- 不含安全(鉴权、签名、来源识别、脱敏),见 §10。
- 不含出口并发/速率限流与熔断;不含人工恢复入口(REQ-DBG-02)。
- 不要求已有可靠任务迁入本层;它们可直接注入 HttpClient,**不得再叠加第二个调度器**。

### 1.3 首版范围

| 能力 | 状态 |
|---|---|
| 事务发件箱 `enqueue`(含重复入队语义与入队校验)、原身份冻结(REQ-OUT-01/02/09/10) | 首版 |
| 持久化退避重试、重试矩阵、`expires_at`(REQ-DLV-*、REQ-OUT-06) | 首版 |
| 租约领取、崩溃恢复、条件回写(REQ-LSE-*) | 首版 |
| 消息行尝试记录与结构化状态日志(§8、§9) | 首版 |
| 终态消息保留期清理(REQ-RET-01) | 首版 |
| 执行预算与发送器 lifespan 载体(§6) | 首版 |
| `Outbox.call`、去重窗口校验、调试 API、逐次事件 | 后移(§12) |

## 2. 分层与适配器端口

### 2.1 分层

```text
业务域 handler / 类型化 client                 (业务域接入)
        │
协议适配器  各业务域对接时自带   ← 实现 ProtocolAdapter
        │
可靠交互核心  app/reliable (本文)
        │ DI
HttpClient  app/http_client
```

导入方向:`app/reliable` 只依赖 `app/http_client`、`app.core` 与存储;**不得导入任何业务域或协议适配器实现**。适配器在装配处以名称注册到核心(`protocol` 名 → 适配器实例),核心按目标上的 `protocol` 取用。

### 2.2 ProtocolAdapter 端口(首版)

核心只保存、比较、重放字节,不窥探内容;凡需要理解内容的判断,都经由下表端口交给适配器。

| 端口 | 输入 → 输出 | 核心用它做什么 |
|---|---|---|
| `build_request` | 冻结的出站消息、目标配置 → `HttpRequest` | 决定路径与请求头;**请求体必须是冻结的 `request_raw`,不得改写** |
| `classify_response` | 出站消息、`HttpResult`(仅 `HTTP_RESPONSE_COMPLETE`)→ `Verdict` | 判定完整 HTTP 响应的协议含义,见 §4 |

适配器的约束:纯函数式、无副作用、不访问数据库;`build_request` **必须把能让对端去重的稳定标识(通常是 `operation_id`)带在请求里**,否则该目标不得启用自动重试(REQ-DLV-04 的前提);所有时间、随机与 ID 生成由装配注入。

### 2.3 公开接缝(业务域与协议适配器可用)

| 接缝 | 说明 |
|---|---|
| `Outbox.enqueue(session, draft)` | 在**调用者事务内**写入 `PENDING` 消息(REQ-OUT-01);`draft` 含目标、`operation`、`operation_id`(必须由调用方提供)、冻结的 `request_raw`、`ttl`、`business_key` |
| `testing` | `FakeClock`、确定性随机源、脚本化 `HttpClient` 替身、故障注入包装器 |

业务域**不得**直接读写 `reliable_message*` 表,也不得依赖核心内部模块。

## 3. 核心概念

### 3.1 Message

消息落 `reliable_message` 表(§8);状态变化写结构化日志,不设事件表。出站身份唯一键为 `(target, operation, operation_id)`(同一身份发往不同目标互不冲突):

- `target`:发给哪个对端,配置里的出站目标名(§3.2)。
- `operation`:这条消息属于哪一类业务动作,如 `task.create`。核心不解析,只存储并交给适配器,适配器据此决定请求路径、请求头与响应解读。
- `operation_id`:这一条消息的身份,**由调用方提供**:同一业务动作的重试必须产出相同的值,否则重复入队无法收敛、对端也无法去重。

`operation` 与 `operation_id` 在核心里都是**不透明字符串**,核心不校验格式(不要求 UUIDv7),也**不等于线上协议字段**:是否发给对端、放在请求头还是请求体,由适配器的 `build_request` 决定。

### 3.2 Target(出站目标,配置)

target = 一个**出站目标**:名称到地址的映射,以及作用于该目标的运行控制。

| 字段 | 说明 |
|---|---|
| `name` | 唯一标识;`enqueue` 按此引用 |
| `base_url` | 不以 `/` 结尾,装配 `HttpClientConfig` |
| `protocol` | 适配器名,决定 `build_request` / `classify_response` |
| `timeout_ms` | 默认 10000,覆盖连接、发送、完整响应读取 |
| `alert_after_attempts` | 默认 8;达到后**仅告警**,不终止重试 |

- 目标较多时配置提供 `defaults`,各目标只写差异项。
- 与某目标共用地址、仅路径不同的下游不需要单独建 target,路径由适配器 `build_request` 决定。
- 业务对象(如设备)与 target 的对应关系属于业务域。
- 配置项 `RELIABLE_TARGETS`(JSON,含 `defaults`)、`RELIABLE_MAX_BODY_BYTES`、`RELIABLE_DEFAULT_TIMEOUT_MS`、`RELIABLE_RETENTION_DAYS`、`RELIABLE_MAX_RETRY_AFTER_S`;执行预算(REQ-RUN-01)的 `RELIABLE_MAX_CONCURRENCY`、`RELIABLE_CLAIM_BATCH_SIZE`、`RELIABLE_DB_POOL_SIZE`。后台检查间隔与批量、租约余量、停机宽限、连接池等待为内部参数,不暴露为环境变量。

## 4. 响应分类与结果映射

### 4.1 两层映射

`HttpResult` 与 `Verdict`(适配器)使用独立类型,不得复用枚举;`Verdict` 只表达协议含义,不改变底层 HTTP 证据。

| `HttpResult.delivery_state` | 核心行为 |
|---|---|
| `NOT_SENT` | 视为明确未发出,退避后原身份重试 |
| `DELIVERY_UNKNOWN` | 退避后原身份重试 |
| `HTTP_RESPONSE_COMPLETE` | 交 `classify_response` 取得 `Verdict` |

### 4.2 Verdict

| Verdict | 含义 | 核心行为 |
|---|---|---|
| `DELIVERED(response)` | 合法的成功响应 | `DONE`,保存 `response_raw` |
| `NOT_RECEIVED(retry_after_ms?)` | 适配器**明确**判定"对端没接收"(如合法的忙/不可用应答) | 同 `NOT_SENT`,遵守 `retry_after_ms` |
| `PEER_TERMINAL(reason)` | 对端明确的终态答复,`reason` ∈ `CONFLICT` / `REJECTED` / `FAILURE` / `CONTRACT_VIOLATION`(响应形式合法、身份一致,但违反约定) | `FAILED(reason)`,不重试;`CONTRACT_VIOLATION` 额外写错误日志并告警;保存 `response_raw` |
| `UNKNOWN` | 响应不是合法协议响应(含无协议体的 429/502/503 等网关/代理响应、身份回显不一致) | 等同 `DELIVERY_UNKNOWN` |

规则:适配器**不得**把无法证明协议有效的响应归为 `NOT_RECEIVED`;只有带合法协议体、身份一致的"明确未接收"才可以。判别细则属协议适配器。

## 5. 可靠性语义

### 5.1 出站

| ID | 要求 |
|---|---|
| REQ-OUT-01 | `Outbox.enqueue` 在**调用者的事务内**写入 `PENDING` 消息;事务回滚则消息不存在(事务发件箱)。 |
| REQ-OUT-02 | 消息创建时**冻结**:`operation`、`operation_id`、`request_raw`;所有重试发送同一份字节。 |
| REQ-OUT-06 | 出站消息带 `expires_at`(= 创建时间 + `ttl`;无 `ttl` 则不过期)。到期时,`PENDING` → `EXPIRED`,不再发送;`SENDING` 中的消息在租约结束后同样处理。已送达的消息不受影响。过期只表示本地停止发起新发送尝试,不表示对端失败,也不撤销已经发出的请求;对端可能在本地过期后接收或执行。发送前必须再次检查期限。 |
| REQ-OUT-09 | **重复入队**:仅在消息行存续期间保证本地幂等。同一出站身份 `(target, operation, operation_id)` 再次 `enqueue`——`request_raw` 相同则返回已有消息(不改变其状态);不同则抛 `DuplicateMessage`,不覆盖已冻结字节。实现用 `INSERT … ON CONFLICT DO NOTHING` 后回读(或 SAVEPOINT 包裹),冲突处理**不得使调用者的外层事务进入失败状态**;并发首次入队收敛为一条。终态行按 REQ-RET-01 清理后不保留本地去重凭据,相同身份再次入队会成为新投递;调用者必须保证不跨清理边界重放旧动作,新业务意图使用新身份。 |
| REQ-OUT-10 | **新入队校验**：先识别存续身份行，同字节返回已有引用、异字节抛 DuplicateMessage；仅真正新入队调用适配器 `build_request` 和公开 `validate_request(目标的 HttpClientConfig, request)`，不创建客户端、不发网。非法构建/校验转为 `InvalidMessage`，不落库，不复制 HTTP 校验规则。返回已有引用不代表重发，实际发送仍按当前配置重新构建和校验；清理后的身份须重新走新入队校验。公开校验接缝已实现 |

#### TTL 的含义与到期行为

`ttl` 是 **Time To Live（消息有效时长）**，限制同一条消息发起新发送或重试的期限，不是单次请求等待时长，也不是数据库记录保留时长。

- **起算点**：新消息在 `Outbox.enqueue` 中创建时写入的 `created_at`；固定截止时间为 `expires_at = created_at + ttl`，不是事务提交或首次发送的时间。不设置 `ttl` 时，`expires_at` 为空，不因有效期截止自动过期，但仍可能进入成功或失败终态。
- **重试不延期**：所有重试复用原 `expires_at`；退避、对端 `retry_after_ms` 等待提示及同身份返回已有行的重复入队均不延长或重置 TTL。即使下一次重试时间晚于截止时间，到期后也不再发起该次发送。
- **未发送消息到期**：到期的 `PENDING` 消息由后台检查标为 `EXPIRED`；实际发起网络请求前再次检查 `expires_at`，截止时间已到则不发送，不依赖后台检查及时完成才能阻止新发送。
- **在途请求到期**：TTL 到期不强行取消已发出的请求，也不立即释放 `SENDING` 的租约；请求仍按原 HTTP 超时与协议分类处理，结果回写仍受当前 `lease_id` 和状态条件保护。合法的 `DELIVERED` 响应可能在 TTL 到期后回写为 `DONE`；若需后续重试，也不得再发起过期消息的新请求。租约到期回收时，若 TTL 已过则转为 `EXPIRED`。
- **不推断对端结果**：`EXPIRED` 仅表示本地不再发起新的发送，不证明对端未接收、未执行或执行失败，也不能撤销对端动作。

| 时间限制 | 起算点 | 控制范围 |
|---|---|---|
| TTL（`ttl` / `expires_at`） | 消息入队创建的 `created_at` | 一条消息发起新发送或重试的有效期，重试不重新计时 |
| HTTP 超时（目标 `timeout_ms`） | 每次 HTTP 调用开始 | 限制该次调用的执行与等待时长；超时可能是结果未知，不能直接当作对端未执行 |
| 租约时长（`lease_until`） | 每次领取消息时 | 目标 HTTP 超时加租约余量，用于处理权分配、条件回写与崩溃恢复；不延长 TTL，租约到期也不证明旧请求已结束 |
| 终态保留期（`RELIABLE_RETENTION_DAYS`） | 进入 `DONE` / `FAILED` / `EXPIRED` 时写入的 `updated_at` | 默认保留终态记录 90 天，超过后由定时任务清理；不是发送有效期，不影响已完成的投递事实 |

### 5.2 领取、租约与崩溃恢复(REQ-LSE)

| ID | 要求 |
|---|---|
| REQ-LSE-01 | 领取在**短事务**内完成:选取到期消息(PostgreSQL 用 `FOR UPDATE SKIP LOCKED`),置 `SENDING`,写入 `lease_id`(随机)与 `lease_until`(= 现在 + 超时 + 余量),提交。**网络请求在领取事务之外**。 |
| REQ-LSE-02 | worker 结果回写必须带条件 `WHERE id = ? AND lease_id = ? AND status = 'SENDING'`。影响行数为 0 时不得覆盖当前结果。 |
| REQ-LSE-03 | 周期任务回收租约过期的 `SENDING` 消息,按"结果未知"处理(REQ-LSE-04);租约过期不能证明旧 worker 已退出,迟到的旧回写由 REQ-LSE-02 挡住。 |
| REQ-LSE-04 | 过期的出站 `SENDING`:按 `SENDING` 与旧租约条件回收,使旧租约失效;未到期回到 `PENDING`(`attempts+1`,退避后原身份重试),已到期转 `EXPIRED`。 |
| REQ-LSE-05 | SQLite 测试环境降级为无行锁扫描,租约与条件回写仍生效;多 worker 竞争只在 PostgreSQL 集成测试中验证。 |

### 5.3 投递结果与重试矩阵(REQ-DLV)

**接入前提:对端按身份去重**——同身份同内容重放首次响应且不重复执行,异内容返回冲突。因此送达未知时一律用原身份重发是安全的。对端不提供查询/对账接口,本层不设对账,也不区分重试类别。

| 结果 | 行为 |
|---|---|
| `NOT_SENT` / `Verdict.NOT_RECEIVED` | 退避后原身份重试;`NOT_RECEIVED` 遵守 `retry_after_ms` |
| `DELIVERY_UNKNOWN` / `Verdict.UNKNOWN` | 退避后原身份重试 |
| `PEER_TERMINAL` | `FAILED`(终态),不重试;`CONFLICT` 由来源系统纠正,业务以**新意图、新身份**重新发起 |
| `DELIVERED` | `DONE` |
| 本地错误(适配器抛异常、构造非法请求) | `FAILED(LOCAL_ERROR)`,不重试,写错误日志并告警 |

| ID | 要求 |
|---|---|
| REQ-DLV-01 | 重试永远复用冻结消息与原身份,禁止生成新身份绕过冲突。 |
| REQ-DLV-02 | 本地退避为指数 + 全抖动:`local_delay = random(0, min(cap, base × 2^n))`,默认 base 1s、cap 5min,可按目标覆盖;随机源与时钟可注入。`NOT_RECEIVED` 带合法 `retry_after_ms` 时,最终等待为 `max(local_delay, min(retry_after_ms / 1000, max_retry_after))`;无提示则使用 `local_delay`。`cap` 只限制本地抖动,不截断对端提示;但对端提示被采纳的上限为 `max_retry_after`(`RELIABLE_MAX_RETRY_AFTER_S`,默认 3600 秒,可按目标覆盖),提示超过上限时按上限等待并写告警日志,避免错误或恶意的超长提示让没有 `ttl` 的消息无声停滞。 |
| REQ-DLV-03 | 明确未接收与 `NOT_SENT` 类的暂时性失败**不设终止次数**,以退避持续重试(本地抖动上限默认 5 分钟,最终等待可因对端提示而更长,但不超过 `max_retry_after`),直到送达、终态失败或 `expires_at`;达到 `alert_after_attempts` 后告警。不静默丢弃,也不因"对端宕机时间长"而终止;对端提示不延长 TTL,过期后不再发起新发送。**可选目标级预算**(`max_not_received_attempts`,默认不设):只有「确认未送达」(`NOT_SENT` 或 `NOT_RECEIVED`)计数;未知结果(`DELIVERY_UNKNOWN`、`UNKNOWN` 判定、租约中断/回收)既不计数也不清零,并持久标记 `delivery_uncertain`,此后预算不再适用。无未知事实且计数达到预算时转 `STALLED`(见下)。可选 `fixed_retry_interval_s` 使本地延迟固定为该值,等待提示规则不变。 |
| REQ-DLV-04 | **对端必须按身份去重**,这是接入前提而非可选项:对端须通过接入时的幂等重放测试才可上线。对端不具备去重能力时,本层的重试可靠性承诺不成立,须先改造对端(见 §10)。去重保留窗口由接入合同约定:有限窗口的目标须给消息设置不超过窗口的 `ttl`;窗口的配置化校验后移(§12.2)。 |
| REQ-DLV-06 | **本地错误隔离**:发送阶段单条消息的异常必须被隔离为该条消息的 `FAILED(LOCAL_ERROR)`;发送循环自身的未预期异常记录日志后带退避继续下一轮,循环不得因单条消息或单次异常退出。 |

### 5.4 消息状态

出站 `status`:`PENDING`(含退避等待,由 `next_attempt_at` 区分)/ `SENDING` / `STALLED` / `DONE` / `FAILED` / `EXPIRED`。
**停止自动发送的状态**:`DONE`、`FAILED`、`EXPIRED`,以及仅在配置了未送达预算的目标上出现的 `STALLED`(非终态:保留原身份与字节,无 `next_attempt_at`,不被领取、不被过期、不被保留期清理);其余开放状态均由自动机制推进;**没有 `DEAD`**。`STALLED` 只能经调用方显式 `Outbox.resume`(回 `PENDING`、清零计数、不清未知标记)或将来的权威结果收敛退出,基础层不自行恢复。
状态转换集中在一处实现,非法转换抛错。

```text
enqueue -> PENDING --到期前领取--> SENDING --DELIVERED----------> DONE
              |                     +----PEER_TERMINAL/本地错误--> FAILED
              |                     +----未发出/未知/明确未接收--> PENDING(退避)
              |                     +----租约回收且未到期------> PENDING(退避)
              +--TTL 到期--> EXPIRED <----租约回收且 TTL 已到期--+

SENDING --确认未送达预算耗尽且无未知--> STALLED --显式 resume--> PENDING
```

所有发送回写均受当前租约与 `SENDING` 状态条件保护;过期只停止新尝试,不推断已经发出的请求失败。

## 6. 运行时与装配

- 运行方向采用 **(a)**:发送器使用长生命周期单事件循环 worker,HttpClient 与将来的限流器随该循环创建/关闭,不在每个发送任务内 `asyncio.run` 重建。
- **发送器载体(已定)**:装配为 FastAPI lifespan 内的后台任务,与 api 共用进程与事件循环;不使用 Celery 的每任务 `asyncio.run` 模式。api 可多实例,每个实例各跑一个发送器,正确性由租约与 `SKIP LOCKED` 保证。
- **优雅停机**：停止领取、等待在途、取消确认、条件释放租约、关闭客户端及 dispose 专用池，共用一个默认 15 秒 monotonic 整体预算，各阶段为后续清理保留机会。不确认停止的任务、未确认的回写留待租约自然恢复，不强行解锁；原 API 池、Redis 和整个进程退出不在该预算保证内。
- **关闭调用合同**：同一 `Sender` 的并发及后续 `aclose()` 调用共享同一次清理任务与首次调用建立的截止时间，默认 15 秒整体预算不因等待者取消或重复调用而重新起算。取消某个等待者仍向该调用者传播 `CancelledError`，但不取消共享清理任务，也不影响其他等待者继续等待。后续调用观察同一任务的结果；清理任务自身的失败或取消仍向等待者传播，不静默视为清理成功。该合同不改变未确认租约留待过期恢复的策略。
- **REQ-RUN-01 执行预算**:单进程的最大领取批量、最大并发发送数,以及发送器专用数据库连接池配额均有保守默认值并可配置,使对端故障不能挤占管理 API 的连接与事件循环。这不是出口限流或熔断,不替代它们。领取按**空闲发送槽位**进行:领取批量不得大于空闲槽位,避免消息在排队时已消耗租约;租约自领取起计。已知限制:一个持续超时的目标仍可能长期占用槽位,跨目标公平调度属出口限流范畴,后移。
- **专用数据库引擎**:发送器(领取、回写、租约回收、过期标记)使用**独立的异步引擎与连接池**,不与 FastAPI 的 `app.core` 引擎共用:池大小为 `RELIABLE_DB_POOL_SIZE`(默认 2),不允许溢出(`max_overflow=0`),获取连接有超时,使对端故障占满发送器连接时不影响管理 API。`Outbox.enqueue` 则使用**调用者自己的会话**(事务发件箱要求消息与业务数据同一事务提交),不走专用池。终态消息清理由 Celery 任务使用自己的短生命周期引擎。代价:每个 api 实例多占 `RELIABLE_DB_POOL_SIZE` 个数据库连接(N 个实例即 N × 该值),数据库 `max_connections` 须计入;应用关闭时需额外释放该引擎(见优雅停机)。
- **领取范围**:只领取本进程**已配置、已创建客户端且已注册适配器**的目标的消息;其余消息保持 `PENDING`,不因配置漂移或滚动部署被终态失败。
- 恢复以数据库扫描为准;即时唤醒缺失不丢消息。
- 发送尝试完成后唤醒本实例发送器;领取批量小于空闲槽位时继续有限批次领取以填充并发预算。空闲时保留轮询兜底,扫描异常时仍等待一个后台检查间隔再试,完成通知不能绕过异常退避。唤醒不是新的重试调度器,不绕过 `next_attempt_at`、TTL 或租约。
- **后台检查间隔与触发条件**:后台检查在正常运行期间执行,不需要停机,间隔默认 1 秒。发送器启动首轮、轮询计时器到点、扫描异常等待该间隔后重试时执行后台检查;发送完成唤醒或连续补位时,只有距上次后台检查已满该间隔才执行,否则仅领取发送。直接调用 `Sender.tick()` 默认仍执行后台检查。
- **每次后台检查**:先扫描 TTL 到期的 `PENDING` 消息并标为 `EXPIRED`,再回收租约到期的 `SENDING` 消息;回收时未过 TTL 的转回 `PENDING` 并退避,已过 TTL 的转为 `EXPIRED`。两项检查默认各最多处理 100 行,批量独立于发送领取,不新增环境变量,不无界清空积压。发送槽满或空目标时仍按上述条件执行,空目标不创建 HTTP 客户端。TTL 扫描不依赖 `next_attempt_at` 或空闲发送槽位,不撤销 `SENDING`、不增加未发送尝试次数;租约回收不推断对端未执行。
- **时钟同步部署前提**:发送器、入队生产者和清理进程所在主机须使用 NTP/chrony 等保持时钟同步并监控偏差、跳变。`lease_until`、`next_attempt_at`、`expires_at` 等持久时间使用应用 UTC 墙钟,尚未统一由数据库授时;UTC-aware 仅统一时区表达,不校正时钟偏差。实例间时钟偏差与调度、回写延迟合计须远小于默认 5 秒租约余量。超出此前提可能提前接管仍在发送的租约;`lease_id` 只阻止旧回写,不能撤销已发请求或防止重复投递,仍依赖 REQ-DLV-04 对端去重。
- 装配处按 `RELIABLE_TARGETS` 为**实际启用**的目标创建 HttpClient 并注册适配器;不为没有消费者的目标创建空客户端。
- 核心不依赖 FastAPI(lifespan 装配在应用层);数据库迁移 `YYYY-MM-DD_reliable_message.py`,`app/models/` 新增并导出消息模型。

## 7. 可测试性

| ID | 要求 |
|---|---|
| REQ-TST-02 | 时钟、随机源、`HttpClient` 均依赖注入;提供 `FakeClock` 与确定性随机源。可靠性测试中**不得**出现真实 `sleep`。 |
| REQ-TST-03 | 脚本化 `HttpClient` 替身可按预设顺序返回 `HttpResult`(含延迟、断连、重复投递),作为 mock 对端;不另写 mock 服务。 |
| REQ-TST-04 | 故障注入作为 `HttpClient` 包装器:延迟、请求落库后断连、`503`、`429`、无协议体的网关错误。 |
| REQ-TST-08 | 崩溃恢复用例(PostgreSQL):双 worker 竞争、领取后退出、发送后保存前退出、旧 worker 迟到回写——消息均不丢失、重试后原字节重发。 |
| REQ-TST-10 | 长时间故障与自动恢复:对端宕机超过原先重试上限后恢复自动续送;`expires_at` 到期不再发送;每个出站重试都复用原身份与冻结内容(断言发送字节一致)。 |
| REQ-TST-12 | `NOT_RECEIVED` → `NOT_RECEIVED` → `DELIVERED` 后消息为 `DONE` 且 `response_raw` 为成功响应;`PEER_TERMINAL` 的 `CONFLICT`/`REJECTED`/`FAILURE`/`CONTRACT_VIOLATION` 各一例均置 `FAILED(reason)` 且不重试;`Verdict.UNKNOWN` 仍按 `DELIVERY_UNKNOWN` 重试。 |
| REQ-TST-15 | **核心与协议无关**:核心的全部测试只使用一个测试内自造的脚本化适配器,不依赖具体协议的信封、operation 词表或业务名词;允许使用不透明的 `operation` 占位值验证通用身份与重复入队语义,不把接口字段名本身视为业务耦合。 |
| REQ-TST-19 | **发送器生命周期**(PostgreSQL):lifespan 停机时先停领取并释放租约;**消息不丢**,重投保持原身份与原字节,旧租约的迟到回写不能覆盖新结果(对端可能已收到请求,重投正是设计允许的行为);两个 api 实例的发送器并发运行不重复领取同一消息。 |
| REQ-TST-20 | **执行预算**:对端全部超时时,并发发送数不超过预算,管理 API 请求仍能取得数据库连接。 |
| REQ-TST-24 | **尝试记录**:对端持续宕机时,消息行的 `attempts`、`last_*` 字段随重试更新,不产生额外行;状态变化写结构化日志,不含报文字段值。 |
| REQ-TST-25 | **事务入队语义**:调用者事务回滚后消息不存在;同键同字节重复 `enqueue` 返回已有消息;同键不同字节抛 `DuplicateMessage`;PostgreSQL 下两个事务并发首次入队同一身份,收敛为一条且一方回滚不破坏另一方的事务。 |
| REQ-TST-26 | **告警日志**:达到 `alert_after_attempts` 与进入 `FAILED` 时产生含 `target`、`operation`、`operation_id`、`attempts` 的告警日志,不含报文字段值。 |
| REQ-TST-27 | **退避上下界**:注入随机源,断言本地抖动位于 `[0, min(cap, base × 2^n)]`,且不超过 cap;分别验证提示缺省、低于本地抖动、高于 cap 及高于 `max_retry_after` 时,最终等待为 `max(local_delay, min(retry_after_ms / 1000, max_retry_after))`(缺省时为 `local_delay`),不得提前重试,仅在超过 `max_retry_after` 时才截断并写告警日志;等待期间仍遵守原 TTL,到期后不再发起发送。 |
| REQ-TST-28 | **本地错误隔离**:`build_request` 在发送阶段抛异常时该条消息置 `FAILED(LOCAL_ERROR)`,其它消息继续发送且循环未退出;通过公开 `enqueue` 验证适配器返回非法 method/path/query/headers、非 bytes 或超限 body 时抛 `InvalidMessage`,不落库且传输调用数为零;合法请求及精确正文上限允许入队。公开校验函数仅依赖 `HttpClientConfig`,在没有客户端实例的进程(如 Celery worker)中可调用,现有 `send` 行为保持不变。 |
| REQ-TST-29 | **保留期清理**:以进入 `DONE`/`FAILED`/`EXPIRED` 时写入且此后不变的 `updated_at` 起算,仅在超过保留期时删除;精确截止时刻不删,刚超过时可删。覆盖三种终态、创建已久但刚进入终态仍保留完整窗口、重复入队不刷新终态时间;`PENDING`/`SENDING` 与未超期的终态消息不受影响。通过公开 `enqueue` 验证行存续期间同身份同字节返回已有消息、异字节冲突,清理后同身份再次入队会成为新消息,不宣称具备跨清理边界的本地幂等。 |

## 8. 数据模型

### 8.1 `reliable_message`

| 列 | 说明 |
|---|---|
| `id` | 主键 |
| `target` / `operation` / `operation_id` | 出站唯一键 `(target, operation, operation_id)`;`operation`/`operation_id` 为不透明字符串 |
| `request_raw` | 请求原文,冻结不变 |
| `response_raw` | 仅在 `DELIVERED` 或 `PEER_TERMINAL` 使消息进入终态时写入,与终态同事务保存;其余情形**允许为空且不得伪造响应** |
| `status` / `fail_reason` | §5.4;`FAILED` 的原因 `CONFLICT` / `REJECTED` / `FAILURE` / `CONTRACT_VIOLATION` / `LOCAL_ERROR` |
| `attempts` / `next_attempt_at` | 重试控制;索引 `(status, next_attempt_at)` |
| `last_attempt_at` / `last_http_status` / `last_failure_kind` / `last_verdict` | 最近一次尝试的结果,逐次覆盖 |
| `last_response_raw` | 最近一次尝试的真实有界诊断响应(可空,逐次覆盖)；DONE/对端终态原文只存 `response_raw`，此列清空以免重复存储；本地分类错误若取得完整响应可保留在此 |
| `lease_id` / `lease_until` | 租约(§5.2) |
| `business_key` | 业务编号,仅展示/查询,索引 |
| `expires_at` | 出站有效期截止,空表示不过期；普通复合索引 `(status, expires_at)` 配合独立有限扫描 |
| `trace_id` / `created_at` / `updated_at` | 追踪标识、创建及更新时间;每次进入 `DONE`/`FAILED`/`EXPIRED` 时与状态同事务写入 `updated_at`,终态后不再更新(含重复入队),作为终态保留期的起算点 |

### 8.2 原文保存

`request_raw` / `response_raw` / `last_response_raw` 原样保存。脱敏与访问控制随安全阶段引入,**不在当前范围**。

## 9. 运行可见性与保留期

| ID | 要求 |
|---|---|
| REQ-DBG-02 | **不提供任何人工恢复入口**:人工裁决、原样/另存重发、重处理、改状态、解锁。异常的退出路径只有自动机制:重试续送、`expires_at`;唯一例外是启用未送达预算的目标上的 `STALLED`,仅可由调用方经 `Outbox.resume` 放回,不提供改状态或改字节的入口。需要重新发起业务动作时,由**业务代码**以新意图、新身份发送。 |
| REQ-DBG-05 | 达到 `alert_after_attempts` 的长期重试与 `FAILED` 产生告警日志(含 `target`、`operation`、`operation_id`、`attempts`,不含报文字段值);它们**只是诊断信息,不是继续运行的前置条件**。排查直接查库。 |
| REQ-DBG-08 | 消息状态变化(含 `FAILED` 的原因)写结构化日志,字段含 `target`、`operation`、`operation_id`、`from_status`、`to_status`,**不含报文字段值**;逐次尝试结果在消息行 `last_*` 字段。 |
| REQ-RET-01 | **终态消息保留期**:从进入 `DONE`/`FAILED`/`EXPIRED` 时写入且此后不变的 `updated_at` 起算,超过保留期(`RELIABLE_RETENTION_DAYS`,默认 90 天)后由定时任务删除;条件为终态且 `updated_at < now - retention`,精确截止时刻不删除。沿用项目审计归档任务的 Celery beat 模式;`PENDING`/`SENDING` 不受影响,长期重试后刚进入终态的消息仍保留完整窗口。清理整行也删除本地重复入队的比较依据,不另存去重凭据;REQ-OUT-09 的本地幂等仅在消息行存续期间成立,对端去重不能作为跨清理边界的本地幂等证明。 |

查询 API、证据导出与权限码后移(§12.3)。REQ-OUT-05、REQ-OUT-08(目标快照)、REQ-DBG-03、REQ-DBG-06 随 `shadow`/`step`/暂停开关移出范围,编号保留不复用。

## 10. 风险、开放问题与后移

**风险**
- **对端去重是接入前提**:对端若实际不按身份去重,重试可能造成重复执行;只能靠接入时的幂等重放测试拦截,运行期无法检测。
- **本地幂等有保留边界**:终态行清理后,相同身份再次入队会成为新投递;若对端去重窗口也已过,旧动作可能再次执行。调用者不得跨清理边界重放旧动作;首版不提供持久去重凭据。只有出现必须跨该边界重放旧调用的真实需求时,再设计持久去重方案。
- **未引入安全**:依赖网络隔离;引入安全前不得暴露到不受信网络。
- **原文明文保存**,含敏感业务数据时需在安全阶段处理。
- `FOR UPDATE SKIP LOCKED` 在 SQLite 不可用,多 worker 竞争只能在 PostgreSQL 集成测试中验证。
- 无出口限流与熔断:发送器上线前应先补出口限流,否则租约重试在下游故障时会放大流量。
- 无跨目标公平调度:一个持续超时的目标可能长期占用发送槽位,其它目标排队(属出口限流范畴,后移)。
- **适配器端口未经真实协议验证**:首版只有两个出站端口,以脚本化适配器测试;端口形状待第一个真实业务对接后定稿。

**开放问题**
- **不具备去重能力的存量对端**:REQ-DLV-04 把去重列为硬前提。若业务域接入要接入无法改造的对端,需另定策略(如目标级 `dedup=none`:`DELIVERY_UNKNOWN` 不自动重试,转为需业务确认的终态)。本文不预设。
- **目标迁移**:重试始终使用目标的**当前**配置(不冻结快照)。把目标指向另一套系统(新的去重库)前须先排空该目标的存量消息,否则带原身份的重试会在新系统上被当作新消息;适配器行为变化同样作用于存量消息。这是运维约束,本层不做程序化保护。
- 消息量与保留期:终态消息 90 天后删除(REQ-RET-01),`PENDING`/`SENDING` 不受影响。

**后移**:§12 列出的设计、安全、出口限流及跨进程限流、熔断、业务域接入、录制包装器与日志归档。

## 11. 验收标准

1. 本文生效的 `REQ-*` 均有至少一个自动化测试且通过(`uv run pytest`);`ruff check`、`ruff format --check` 干净。
2. §4.2 的 `Verdict` 与 §5.3 矩阵每一行有故障注入用例;每个出站重试都复用原身份与冻结字节(断言发送字节一致)。
3. REQ-TST-08 的崩溃恢复用例在真实 PostgreSQL 下通过;REQ-TST-10/12/19/20/24~29 通过。
4. 进程重启后,未完成的出站投递均按原冻结内容继续,不丢失。
5. 对端长时间不可用后恢复:未过期消息**自动续送**,已过期消息不再发送;全程不存在人工恢复入口(REQ-DBG-02)。
6. 发送器在长生命周期单事件循环内运行,客户端不随任务重建;以数据库扫描为恢复依据。
7. REQ-TST-15 成立;`tests/architecture/test_layering.py` 已覆盖 `app/reliable`:不得导入 `controllers`、业务域或任何适配器实现;迁移单一 head。
8. 分层与导入隔离测试补入 `app.reliable` 对 `app/http_client` 的反向禁止项,与 `app/reliable` 落地在同一变更中完成。

## 12. 后移设计(完整条文保留,非首版)

以下能力已评审并设计完整,但没有真实协议与对端支撑,首版不实施。启用任何一项时,须先复核与首版数据模型的衔接。

### 12.1 同步调用 `Outbox.call`

| ID | 要求 |
|---|---|
| REQ-OUT-03-D | `Outbox.call` 用于需要立即得到响应的 `SYNC` 调用:先在**自己的短事务**中落库并提交(**不借用调用者事务;调用者须在进入本调用前结束自己的事务**),再发送并回写结果。handler 内禁止调用 `Outbox.call`,对外调用只能经 `enqueue`。 |
| REQ-OUT-04-D | `call` 在调用方取消(协程被取消)时,已落库的消息保持 `SENDING`,由租约过期按"结果未知"处理(REQ-LSE-04)。 |
| REQ-OUT-07-D | `call` 遇 `DELIVERY_UNKNOWN`(含 `Verdict.UNKNOWN`)时抛出 `DeliveryUnknown`(携带消息 id);消息此后由后台按重试矩阵用原身份继续重试,调用方**不得**自行重发,最终有效结果响应可从 `response_raw` 读取;`NOT_RECEIVED` 不占用该结果字段。 |

### 12.2 去重窗口校验

target 增加 `dedup_retention_ms` / `delivery_margin_ms`。

| ID | 要求 |
|---|---|
| REQ-DLV-05-D | 接入合同必须约定去重保留窗口:消息的最大重试期限(含在途请求可能到达的余量)不得超过对端保证的去重期限;有限窗口目标须声明 `dedup_retention_ms` 与 `delivery_margin_ms`,要求 `ttl + delivery_margin_ms <= dedup_retention_ms`,无法保证有限到达余量时只能采用 `persistent` 去重;有限窗口目标的消息必须设置符合该约束的 `ttl`,无 TTL 的持续重试只适用于 `persistent` 目标。配置与建消息时校验,不允许窗口失效后继续自动发送。去重期限从对端首次接纳起计算,本方以消息创建起算 TTL 作保守约束。终态行按 REQ-RET-01 清理只影响本方记录,不影响对端的去重期限。保留期限须由双方合同确认,不能由一次测试推断。 |
| REQ-TST-14-D | 使用可注入时钟验证有限去重窗口限制 TTL,不允许窗口失效后新发送;无 TTL 仅允许持久去重目标;过期在途请求不被误断言为对端未执行。 |

### 12.3 调试查询 API、证据导出与权限码

| ID | 要求 |
|---|---|
| REQ-DBG-01-D | 报文查询 API:按 target、operation、状态、`operation_id`、`business_key`、时间范围筛选;返回原文与事件时间线;`response_raw` 为空时附带 `response_absent_reason`(`NOT_YET` / `EXPIRED` / `CONTRACT_VIOLATION`),不构造响应。 |
| REQ-DBG-04-D | 证据导出:选定消息的请求/响应原文、事件时间线,单个 JSON 文件。 |
| REQ-DBG-05-D | 异常记录可见性:达到 `alert_after_attempts` 的长期重试、`FAILED` 必须可经 REQ-DBG-01-D 按状态筛选,并产生告警日志;它们**只是诊断信息,不是继续运行的前置条件**。 |
| REQ-DBG-07-D | 调试 API 位于 `app/controllers/reliable.py`,沿用项目 RBAC,权限码 `reliable:message:read`;同步更新 `INITIAL_PERMISSIONS` 与权限测试。(项目管理接口的常规授权,不属于协议安全。) |
| REQ-TST-18-D | **调试合同**:权限码 `reliable:message:read` 对未授权调用方拒绝;查询按各筛选条件返回并附 `response_absent_reason`;证据导出内容与原文一致;长期重试、`FAILED` 可经查询筛选并产生告警。 |

### 12.4 逐次事件记录与折叠

首版不设事件表:逐次尝试结果放在消息行的 `last_*` 字段,状态变化写结构化日志。若需要完整的每次尝试证据与状态时间线,启用本项(新增事件表):

`message_id`(指向 `reliable_message.id` 的外键)、`at`、`kind`(`RECEIVED` / `ATTEMPT` / `RESPONSE` / `STATE` / `CONFLICT` / `ERROR`)、`attempt_no`、`http_status`、`verdict`、`detail`(**不含报文字段值**)、`response_raw`(可空,单次尝试实际收到的响应原文,受正文上限约束;未收到则为空,超限只记录错误类别)。原文只放专用字段,不写入 detail 或日志。用于导出历次尝试、冲突报文摘要和完整状态时间线。

**折叠规则**:连续的 `ATTEMPT`/`RESPONSE` 事件若 `http_status`、`verdict`、失败类别均与上一条相同,则不新增行,只更新上一行的 `repeat_count` 与 `last_at`(新增两列);结果发生变化、进入终态或收到有效响应时新增一行并保存 `response_raw`。折叠行保留首次的 `response_raw`。除计数与末次时间外,事件不更新不删除。

| ID | 要求 |
|---|---|
| REQ-TST-23-D | **事件折叠**:对端持续宕机时,连续相同结果只增加计数与末次时间,结果变化、终态与有效响应仍逐条落库并保留 `response_raw`。 |
| REQ-TST-12-D | `NOT_RECEIVED` → `NOT_RECEIVED` → `DELIVERED` 后 `response_raw` 为成功结果且事件记录各次结果(连续相同的折叠计数,见 REQ-TST-23-D);`CONTRACT_VIOLATION` 置 `FAILED`、不重试、不标记成功;`Verdict.UNKNOWN` 仍按 `DELIVERY_UNKNOWN` 重试。 |
