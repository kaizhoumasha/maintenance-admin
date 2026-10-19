# SPEC-019 非业务交互基础能力:HttpClient 与可靠交互分层

状态:设计 v4(**H 阶段设计及实现已复审通过**,首期纯 HttpClient 的 H1/H2/H4 已实现并验证;H3 后移,可靠交互层后续交付;Q2 已确定) · 分支:`feature/spec-019-interop-framework` · 承接:无

## 1. 范围

### 1.1 目标与当前交付

- **先交付纯异步 HttpClient**:支持构造参数 DI、超时、连接池、请求/响应大小限制;不含出口并发及速率限流器。只完成一次 HTTP 调用,不理解 JSON 信封、operation 或业务结果。
- **可靠交互层后续交付**:幂等、收件箱/发件箱、持久化重试、租约、崩溃恢复与消息证据均属于 `app/interop`,通过 DI 使用 HttpClient。
- **业务能力独立**:WMS、Transport 等业务域拥有自己的协议、payload、身份关联及业务/物理结果解释。已有可靠任务可以直接注入 HttpClient,不强制经过 Interop。
- H1/H2/H4 已实现并验证,H3、R 与业务接入未实施。“清理业务能力”指清理本设计中的业务耦合,不删除或修改 wes_backend、maintenance-agent 或本项目已有业务代码。

### 1.2 H 阶段非目标

- 不实现入站网关、operation 注册表、信封解析、UUIDv7、幂等表、Outbox、Celery 重试任务、completion、shadow/step、暂停或调试管理 API。
- 不新增任何 WMS/Transport 业务模块、DTO、设备模型、协议转换或兼容迁移;不改变既有 ACK、重复响应、摘要、重试及完成判定。
- 不实现自动重试、自动重定向、出口并发/速率限流(H3)、业务限流策略、跨进程共享限流、熔断、动态配置、配置 CRUD 或通用映射 DSL。
- 安全继续后移:业务鉴权、签名、来源识别、报文脱敏另立范围;底层 HTTPS 仍使用标准证书校验,不因安全后移而禁用 TLS 校验。
- 不添加 DI 容器或新的 HTTP 依赖;实现时复用现有 `httpx`,移入正式依赖。

### 1.3 阶段与设计适用范围

H = 纯 HttpClient;R = 后续可靠交互层;D = 独立业务域接入。H3 出口限流本次明确后移,不是 H 阶段验收项;连接池 max_connections 仍保留,它是网络资源限制,不是新增的出口准入调度器。安全独立后移,不包含在 H/R/D 的默认实现授权中。

| 内容 | 当前效力 |
|---|---|
| §2.1 HttpClient 与分层规则、§12 首期交付、§14.1 首期验收 | 当前交付基线 |
| §3–9 的信封、协议、持久化模型和可靠性规则 | **后续 Interop 标准协议方案**,不是 HttpClient 的能力或所有对端的强制协议;不得在首期提前实现 |
| §10 分层、§13 已定决策 | 按层适用 |
| §15 历史评审记录 | 保留追溯,历史阶段表述不覆盖当前交付范围 |

Q2 的字符串枚举、UUIDv7 与毫秒整数保留为后续标准协议决定;HttpClient 不生成、解析或校验这些字段。后续接入存量协议时由独立接入设计确定差异,不在 H 阶段增加协议可配置引擎。

## 2. 实现前现状

- 项目为 FastAPI + 异步 SQLAlchemy + Celery/Redis,分层 `controllers → services → models`,由 `tests/architecture/test_layering.py` 强制;**该测试不会自动约束新目录 `app/interop`**,需显式扩展。路由在 `app/main.py` 注册。
- 应用级依赖 `dependencies=[Depends(RateLimit(100))]`(`app/main.py:41`)对挂在 app 上的路由生效;校验失败统一映射为 `400`,业务错误走 `AppError`。协议端点的响应体与状态码由本 SPEC 规定,**不能**走这套全局约定。
- FastAPI 的 `app.dependency_overrides` **不会传播到 `app.mount` 的子应用**;子应用的数据库会话依赖、测试覆盖需要自己提供。
- `trace_id_middleware` 与 `AuditMiddleware` 已存在;审计仅记录挂了 `require_permission` 的写请求。
- 现有 Celery 审计任务按任务调用 `asyncio.run` + `NullPool`(`app/tasks/audit_tasks.py`),不适合作为长期 HTTP 池与未来限流器的生命周期。该任务保持不变;R 阶段发送器采用下述(a)约束,不照搬每任务新循环模式。
- `httpx` 目前只在 dev 依赖组,需升为正式依赖。Python ≥ 3.13,标准库没有 UUIDv7。
- 单元测试跑内存 SQLite、fake Redis、Celery eager;PostgreSQL 集成测试需 `TEST_DATABASE_URL`。SQLite 不支持 `FOR UPDATE SKIP LOCKED`。
- `app/integration/` 下仅有被忽略的 `.pyc`,无源码,可视为空白。

### 2.1 首期:纯 HttpClient

#### 分层与唯一生命周期所有者

```text
业务服务 / 协议适配器
  ├─ 已有可靠任务:直接注入 HttpClient,保留原生命周期
  └─ 新可靠消息:可靠交互层(Inbox / Outbox / 调度)
                         ↓ DI
                  纯 HttpClient
                         ↓
                  一次 HTTP 请求
```

一个逻辑消息只能有一个重试调度者。HttpClient 与其底层 HTTP transport 的自动重试均关闭,不得叠加接管已有业务服务的重试。HTTP 响应、协议接纳和业务/物理完成是三类不同事实。

#### 公开接口与 DI

`HttpClient.send(HttpRequest) -> HttpResult`、`HttpClient.aclose()` 是公开异步接口。接口用 Python Protocol 表达,由基于 `httpx.AsyncClient` 的实现提供;不包装成业务 Service,不引入 DI 框架。

| 对象 | 最小合同 |
|---|---|
| `HttpRequest` | method、相对于已配置 base_url 的 path、query、headers、body bytes;不提供业务 JSON 或 operation 字段。path 不得切换 origin,不得包含查询分隔符 `?`,查询统一通过 query 传入;H 阶段不提供逐请求超时覆盖,使用实例配置 |
| `HttpResult` | delivery_state、failure_kind、可选 HTTP status、响应 headers(保留重复项)、有界响应 body bytes;响应不完整时带错误类别,不得当作完整响应使用 |
| 客户端配置 | base_url、connect/read/write/pool timeout、total timeout、max_connections、max_keepalive_connections、keepalive_expiry、max_request_body_bytes、max_response_body_bytes、可选 tls_context(ssl.SSLContext)、destination_key(稳定的非敏感出口诊断标识) |
| DI | 协议适配器构造参数接收 HttpClient;测试直接注入 fake。生产实现接受可替换的 HTTP transport 和单调时钟,用于网络故障与截止时间计算测试;假时钟不控制事件循环的实际取消计时 |

基础层不定义目标管理系统;装配处以稳定 `destination_key` 记录出口诊断。同一进程、同一长生命周期事件循环、同一出口的消费者共享客户端以复用连接池,不跨循环使用。不为没有消费者的配置创建空客户端或增加业务调用方。

#### 首期需求

| ID | 要求 |
|---|---|
| REQ-HTTP-01 | 一次 send 至多发起一次网络请求。自动重试、自动重定向及认证握手式自动重发关闭;3xx/4xx/5xx 原样作为 HTTP 响应返回,不调用 raise_for_status,不解释 429/503 是否未接收。Retry-After 仅作为响应头交给上层 |
| REQ-HTTP-02 | 请求 body 按原字节发送,不 JSON 解码、重编码、改字段或生成身份;不强制 Content-Type。header 名值及 URL 基本合法性在发网前校验,拒绝 CR/LF 和冲突的正文 framing;Content-Length 由底层生成,不接受调用者覆盖 framing |
| REQ-HTTP-03 | 支持连接、读、写、连接池等待四类超时及总截止时间。总超时默认 10000ms,从 send 进入开始覆盖连接池等待、连接、发送、完整响应读取;阶段超时不得延长总期限。所有时间和资源配置启动时校验正值及连接池关系 |
| REQ-HTTP-04 | 复用连接池;限制总连接、keepalive 连接及空闲存活期。每个进程/事件循环独立持有客户端,不跨 fork 或 asyncio.run 的事件循环复用;同一生命周期共享使用,aclose() 幂等;关闭后再次发送抛 ClientClosedError 且不发网 |
| REQ-HTTP-05 | 请求/响应正文默认上限均为 256KiB,可配置。请求 bytes 超限在发网前拒绝;响应流式累计检查上限,不先 read 全文再判断,不只相信 Content-Length。精确等于上限允许,超过立即停止读取并关闭响应 |
| REQ-HTTP-06 | 首期仅接受 identity 响应编码,发送 Accept-Encoding: identity;遇到非 identity Content-Encoding 返回明确错误并关闭响应,不做自动解压以绕过大小限制。调用方不得覆盖此编码约束 |
| REQ-HTTP-07 | **后移,不属于 H 阶段**:出口并发/速率准入调度及 max_concurrency/rate_limit 配置暂不实现;不以连接池限制宣称具备 FIFO、滚动窗口或全局额度保证。未来限流方案另行明确作用域、准入与取消不变量 |
| REQ-HTTP-08 | R 阶段运行方向选 **(a)**:发送器使用长生命周期单事件循环 worker,客户端和将来限流器随该循环创建/关闭,不在每个发送任务内 asyncio.run 后重建。H 阶段不新增 worker、不改现有 Celery 任务。R 开工前明确启动/停机及调度接入;将来单循环额度不代表跨进程总额度 |
| REQ-HTTP-09 | 只在能证明未进入发送阶段时返回 NOT_SENT(如 pool/connect 失败或发送前总期限耗尽);写/读超时、发送后协议错误、响应超限/编码不支持为 DELIVERY_UNKNOWN。完整读取 HTTP 响应为 HTTP_RESPONSE_COMPLETE,不论 HTTP 状态和正文格式。保留可取得的 status/headers 与失败类别,不把部分正文冒充完整结果 |
| REQ-HTTP-10 | 调用者取消须清理响应/连接、归还连接池资源并继续传播 CancelledError,不吞掉或转换为业务失败;上层已有可靠任务按发送阶段和租约处理未知,本层不排队续送。进入发送阶段前取消不发网 |
| REQ-HTTP-11 | 不导入 app.core、数据库、Redis、Celery、controllers、app/interop 或任何业务域;配置由装配处传入,日志用标准 logging,不通过 app.core 获取 logger。日志只记录方法、出口标识、耗时、状态和错误类别,不输出正文、凭据、查询串或异常携带的原始 URL |
| REQ-HTTP-12 | **未来消费者装配约束,不属于 H 阶段行为验收**:由实际应用生命周期建立并关闭实例,FastAPI Depends 仅取得已装配实例;R 发送器按 REQ-HTTP-08 的常驻循环装配。H 阶段无消费者时不创建实例、不新增应用装配或 Celery 任务,只交付可注入组件,无新增路由/表/权限 |

#### 默认值与配置归属

默认 max_connections=100、max_keepalive_connections=20、keepalive_expiry=5s,四类阶段 timeout 各 5s,total timeout=10s;不提供 rate_limit 或 max_concurrency。参数取值为初始资源默认值,不代表现场吞吐承诺。只给**实际启用的**客户端实例装配参数,不新增 INTEROP_TARGETS、INTEROP_MODULES 或所有业务端点配置。

#### httpx 生产约束与失败表达

- 显式设置 `trust_env=False`、`follow_redirects=False`、`http2=False`、底层 `retries=0`,不使用代理环境变量或环境凭据;保持标准 TLS 证书校验;私有 CA 由调用方显式构造并注入 tls_context,未注入上下文时,仅 HTTPS 加载实际存在的系统 CA 文件/目录;系统 CA 均缺失则保留空信任库并维持证书校验,握手不能因此跳过验证。明文 HTTP 不加载 CA。不读取环境 CA 配置、不提供 verify=False。自定义测试 transport 也不得隐藏重试。
- URL 合成后校验实际 scheme/host/有效端口与 base_url 一致,拒绝绝对 URL、network-path reference、userinfo 及改变 origin 的 path;不允许调用者覆盖 Host/framing 绕过目标约束。
- 使用 streaming send,先检查 Content-Encoding(缺省或 identity 才允许),再直接迭代原始 `response.stream` 读取有界正文,完整读取后独立执行有界响应清理,避免 `aiter_raw()` 的 EOF 自动关闭异常覆盖完整响应;不先读取 `.content`/`.aread()` 或使用会自动解码的 `aiter_bytes()`。
- 对端空闲超时已知时,keepalive_expiry 配置为更短;未知时沿用默认值,不猜对端配置。即使较短也不能保证避免陈旧连接错误,按 REQ-HTTP-09 分类,不自动重发。

| 情形 | 统一表达 |
|---|---|
| 配置、请求参数非法(含请求正文超限) | 发网前抛 `ValueError` 或 `TypeError`,不生成 HttpResult |
| 客户端已关闭 | 抛 `ClientClosedError`,不发网 |
| 网络、连接池等待或总超时、响应超限/编码不支持 | 返回带明确 failure_kind 和发送事实的 HttpResult;不转为 AppError |
| 完整 HTTP 响应,包括 3xx/4xx/5xx | 返回 HTTP_RESPONSE_COMPLETE,正文保持原字节,交给协议适配器解释 |
| 调用者取消 | 清理资源并传播 CancelledError |

#### 两层结果映射(R 阶段使用)

HttpClient 与 Channel 使用独立结果类型,不得直接复用枚举。Channel 只映射协议语义,不改变底层 HTTP 证据。

| HttpResult | ChannelResult |
|---|---|
| NOT_SENT | NOT_SENT |
| DELIVERY_UNKNOWN | DELIVERY_UNKNOWN |
| HTTP_RESPONSE_COMPLETE,但非合法信封或引用身份不一致 | DELIVERY_UNKNOWN(REQ-CODE-02) |
| HTTP_RESPONSE_COMPLETE,合法信封且引用身份一致 | RESPONSE_RECEIVED;再按 REQ-CODE-01/03 判断有效接纳、拒绝或合同错误 |

#### 后续可靠交互层的边界

幂等身份与摘要、事务提交、消息表、退避抖动、次数/TTL、重试资格、租约及恢复由可靠交互层负责。它只通过 HttpClient 发起一次尝试,不持有数据库事务等待出口。协议层决定 HTTP 响应是否是有效信封、是否明确未接收;业务层决定业务结果。接入已有任务时不得创建第二个调度器。

以下 §3–9 保留已评审的后续标准协议设计,不作为 HttpClient 首期验收或存量 WMS/Transport 改造要求。

## 3. 后续标准协议:信封

### 3.1 信封(唯一冻结的 wire)

```
请求  { "operation_id": str, "operation": str, "timestamp": int, "data": object }
响应  { "operation_id": str, "code": str,      "timestamp": int, "data": object }
```

| ID | 要求 |
|---|---|
| REQ-ENV-01 | 请求信封**恰为**四个字段,全部必填;响应信封**恰为**四个字段,全部必填。任何层级的未知信封字段都拒绝。 |
| REQ-ENV-02 | `operation_id` 为 UUIDv7 的**小写 canonical** 字符串。响应必须回显请求的 `operation_id`。 |
| REQ-ENV-03 | `timestamp` 为 UTC Unix 毫秒整数(`0..2^63-1`),**仅用于审计与诊断**,不参与排序、判重或业务判断。 |
| REQ-ENV-04 | `operation` 匹配 `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+@v[1-9][0-9]*$`,大小写敏感,例如 `sys.ping@v1`。 |
| REQ-ENV-05 | `data` 必须是 object;无内容时为 `{}`,不得省略或为 `null`。 |
| REQ-ENV-06 | 错误响应(冲突、拒绝、繁忙、不可用)**同样是完整的四字段信封**。 |
| REQ-ENV-07 | 正文为严格 UTF-8 JSON,无 BOM,顶层为 object;任意层级重复 key 即拒绝;`1`、`1.0`、`"1"` 互不相等。 |
| REQ-ENV-08 | 正文上限默认 256 KiB(按原始 UTF-8 字节计,可配置)。超限在 JSON 解码前返回。 |
| REQ-ENV-09 | 解析额外限制:拒绝 `NaN`/`Infinity`;嵌套深度 ≤ 32;整数绝对值 ≤ `2^63-1`(超出或数字位数异常一律视为非法);单个字符串 ≤ 64 KiB。违反即视为"无法形成合法身份"(空体 `400`),**不得**抛 500。 |

### 3.2 `code` 词表

**Q2 已确定:**`code` 使用字符串枚举,`operation_id` 使用小写 canonical UUIDv7,`timestamp` 使用 UTC Unix 毫秒整数。保留词表如下:

| code | 含义 | HTTP |
|---|---|---|
| `OK` | `SYNC` 成功,结果在 `data` | 200 |
| `ACCEPTED` | `ACCEPT_THEN_CALLBACK` 已持久接收,结果稍后另发 | 202 |
| `RECORDED` | `FACT` 已记录 | 200 |
| `RECEIVED` | `NOTIFY` 已接收 | 200 |
| `CONFLICT` | 同身份异内容,或业务状态冲突;`data.reason_code` 区分 | 409 |
| `REJECTED` | 信封或 `data` 不合法、operation 不支持;`data.reason_code` 区分 | 422 |
| `BUSY` | 对端繁忙;`data.retry_after_ms`(1..60000) | 429 |
| `UNAVAILABLE` | 暂不可用,**明确未接收**;`data` 为 `{}` | 503 |

业务 operation 可追加自己的 `code`,声明时必须同时给出**HTTP 状态码**和**语义类别**(`SUCCESS` / `FAILURE_TERMINAL`);不得复用保留 `code` 表达别的含义。

| ID | 要求 |
|---|---|
| REQ-CODE-01 | 出站时,响应只有在 `Content-Type` 为 JSON、能解析为合法信封、`operation_id` 回显一致、`code` 为该模式允许的保留 code 或该 operation 声明的 code、HTTP 状态码与映射一致、且 `data` 通过对应模型校验时,才算"收到有效响应"。 |
| REQ-CODE-02 | 响应不是合法信封(`Content-Type` 非 JSON、无法解析、字段缺失或多余、`operation_id` 回显不一致,含**无信封体**的 `429`/`503`/`502` 等代理或网关响应)一律按 `DELIVERY_UNKNOWN` 处理,**不得**当作"明确未接收"。只有带合法信封的 `UNAVAILABLE`/`BUSY` 才表示明确未接收。 |
| REQ-CODE-03 | 响应是**合法信封且 `operation_id` 一致**,但违反约定(`code` 不属于该模式允许的成功 code、保留失败 code 或该 operation 声明的 code;HTTP 状态码与 `code` 的映射不一致;`data` 不符合对应模型,例如 `SYNC` 收到 `ACCEPTED`):对端已明确回应,且对端按 `operation_id` 去重,重发只会得到同样的重放,因此**不重试**。消息置 `FAILED`(`reason=RESPONSE_CONTRACT_VIOLATION`),`maybe_delivered=true`,写 `ERROR` 事件(含该次响应原文)并告警,**不标记成功**。该失败与"对端明确业务拒绝"不同:请求很可能已被接纳,因此消息保留 `fail_reason=RESPONSE_CONTRACT_VIOLATION`,允许其后到达的合法 completion 补证为 `DONE`(REQ-COR-06),原合同错误事件与 `fail_reason` 保留。该次响应原文只保存在事件 `response_raw`,消息的 `response_raw` 留空(§8.1)。 |

保留成功 code 按模式限定为 §4.2 对应的一项;额外成功 code 仅允许用于 `SYNC`。`SYNC` 的成功 `data` 使用声明的 `response` 模型;异步接纳响应的 `data` 固定为 `{}`。保留错误 code 使用框架错误模型(含 BUSY 的 `retry_after_ms`),自定义终态失败 code 须声明对应的 `data` 模型。SYNC 收到 ACCEPTED、或成功 data 不符合模型均按 REQ-CODE-03 处理。

无法取得合法 `operation_id` 的请求(JSON 非法、重复 key、非 UTF-8、违反 REQ-ENV-09、`Content-Type` 错误)返回**空响应体 `400`**;超限返回空响应体 `413`。二者**不**进入幂等记录。

### 3.3 调用方身份(R 阶段不做,预留一个点)

- R 阶段入站**不识别调用方**,也不做白名单。框架仅把请求的 `remote_addr` 记入消息,作为**诊断信息**,不参与任何判定。
- 前向兼容点:消息表含 `app_id` 列(默认空串),并**作为幂等唯一键的一部分**(§8.1)。后续引入 `app_id + app_secret` 时,验证通过后写入 `app_id`,凭据放 HTTP 头,**不改信封,也不迁移唯一键**。具体方案另立 SPEC。
- R 阶段 `app_id` 恒为空串,因此 R 阶段的幂等空间是全局的:不同对端使用相同 `operation` 与 `operation_id` 视为同一条消息。这依赖 UUIDv7 的随机性;对端误复用 id 且内容不同时,将得到 `409`,不会静默出错。

## 4. 核心概念

### 4.1 Operation(唯一扩展点)

| 属性 | 说明 |
|---|---|
| `name` | 满足 REQ-ENV-04;版本写进名字,不兼容变更发新版本,新旧并存 |
| `pattern` | `SYNC` / `ACCEPT_THEN_CALLBACK` / `FACT` / `NOTIFY` |
| `request` / `response` | 请求与同步结果的 pydantic 模型;异步模式的 `response` 为框架空对象 ACK 模型。默认 `extra="forbid"`;对宽松对端可单独声明 `tolerant=True`(忽略未知字段,仍校验已知字段)。**摘要与判重不受模型转换或 tolerant 影响**(§5.1) |
| `completion` / `ref_field` | 仅 `ACCEPT_THEN_CALLBACK`:回报结果的 operation 名,及 completion 的 `data` 中携带**原请求 `operation_id`** 的字段路径(JSON Pointer),见 §5.5 |
| `reply_to` | 仅作为**服务端**接收 `ACCEPT_THEN_CALLBACK` 时:回报 completion 的出站目标名(§4.5) |
| `business_key` | 可选;`data` 中的业务编号字段路径,**仅用于查询展示**,不参与关联 |
| `direction` | `inbound` / `outbound` / `both` |
| `ttl` | 出站消息默认有效期(调用方可覆盖);到期后**尚未确认送达**的消息不再发送,见 REQ-OUT-06 |
| `path` | 出站 HTTP 路径,默认 `/interop/v1/messages`;对路径已固定的存量对端可按 operation 覆盖 |
| `codes` | 额外声明的 `code`(含 HTTP 状态码与语义类别) |
| `example` | 显式声明的合法请求/响应样例(由测试校验其符合模型) |

handler 是普通 `async` 函数,入参为类型化请求与 `Ctx`。`SYNC` 返回类型化响应;异步模式返回 `None`,即时 ACK 由框架在 handler 执行前生成,不使用 handler 返回值。完成结果通过 `ctx.complete` 表达:

```python
@operation(
    "demo.job.submit@v1",
    pattern=ACCEPT_THEN_CALLBACK,
    request=SubmitReq,
    example=...,
    reply_to="target_a",
    completion="demo.job.done@v1",
    ref_field="/ref_operation_id",
)
async def submit(req: SubmitReq, ctx: Ctx) -> None:
    ctx.session.add(...)  # 业务写入使用框架持有的事务
    await ctx.send("target_b", "demo.step@v1", {...})  # 对外调用只能经 outbox
    # 结果立即可定时用 ctx.complete;延迟完成用 hub.complete(ref, ...)(REQ-COR-07),ref 来自 ctx.request_ref
    await ctx.complete(outcome="SUCCEEDED", payload=DonePayload(...))
    # handler 不得自行 commit
```

### 4.2 交互模式

| 模式 | 流程 | `code` |
|---|---|---|
| `SYNC` | 请求 → 业务结果 | `OK`(或声明的 `code`) |
| `ACCEPT_THEN_CALLBACK` | 请求 → ACK(仅表示接纳)→ 对端另发 `completion` 回报结果 | `ACCEPTED` |
| `FACT` | 报告已发生的事实 → 记录确认 | `RECORDED` |
| `NOTIFY` | 单向通知 → 接收回执 | `RECEIVED` |

`ACCEPTED` 只表示"已持久接收",**不表示业务完成**。消息有两条独立的轴:**投递状态**(`status`,§5.6)与**业务结果**(`outcome`,仅 `ACCEPT_THEN_CALLBACK` 的请求消息:`PENDING` / `SUCCEEDED` / `FAILED`)。`status=DONE` 且 `outcome=PENDING` 表示"已投递、结果未到"。

### 4.3 Message

所有收发消息落同一张表,另有追加式事件表保存证据(§8)。身份唯一键为 `(app_id, direction, operation, operation_id)`。

### 4.4 Channel(后续可靠交互层端口)

后续 `HttpChannel` 通过 DI 使用 §2.1 的 HttpClient,只承担标准协议请求/响应适配;不另建网络连接池或重试循环。`LoopbackChannel` 用于可靠层测试。

`Channel.send(target, request_bytes) -> ChannelResult`,按 §2.1 两层映射携带**投递状态**:`NOT_SENT` / `DELIVERY_UNKNOWN` / `RESPONSE_RECEIVED`。实现:`HttpChannel`、`LoopbackChannel`(进程内)。`RecordingChannel`(包装并记录原始报文)后移。

### 4.5 Target(出站目标,配置)

target = 一个**出站目标**:名称到地址的映射,以及作用于该目标的运行控制。**它只用于出站,不参与入站身份。**

| 字段 | 说明 |
|---|---|
| `name` | 唯一标识;`ctx.send(name, ...)` 与暂停开关均按此引用 |
| `base_url` | 不以 `/` 结尾 |
| `timeout_ms` | 默认 10000,覆盖连接、发送、完整响应读取 |
| `alert_after_attempts` | 默认 8;达到后**仅告警**,不终止重试 |
| `mode` | `live` / `shadow` / `step` |

- 与某目标共用地址、仅路径不同的下游,不需要单独建 target:用 operation 的 `path` 区分。
- 目标较多时,配置提供 `defaults`,各目标只写差异项。
- 每个目标须声明接入合同保证的 `dedup_retention_ms`(正整数,或 `persistent` 表示持久保留);有限窗口同时声明双方约定的 `delivery_margin_ms`(正整数,覆盖在途请求最大到达余量),要求 `ttl + delivery_margin_ms <= dedup_retention_ms`;无法保证有限到达余量时只能采用持久去重。按 REQ-DLV-05 校验。它是可靠性配置,不引入查询或对账协议。
- 业务对象(如设备)与 target 的对应关系属于业务域,不在基础层。

## 5. 可靠性语义

### 5.1 入站

事务边界:**框架持有事务**,handler 通过 `ctx.session` 写业务数据,不得自行 `commit`/`rollback`。

**幂等判定顺序(固定)**

1. 严格解析 + 取得身份:`(app_id, operation, operation_id)`。失败则按 §3.2 返回无体错误,不落库。
2. 对**原始解析结果**(未经 pydantic 转换)的 `operation` 与 `data` 计算摘要。
3. 按唯一键查已有记录:同摘要 → 重放首次响应(REQ-IN-03);异摘要 → `CONFLICT`(REQ-IN-04)。
4. **仅首次请求**才做:operation 是否注册、方向、`data` 的模型校验。失败则持久化为 `REJECTED` 并保存拒绝响应。

因此部署后 schema 变化,不会影响对已接纳或已拒绝消息的重放。

| ID | 要求 |
|---|---|
| REQ-IN-01 | `SYNC`:在**同一数据库事务**内完成 插入消息 → 执行 handler → 写入首次响应 → 标记 `DONE`,**提交后**才返回响应。handler 抛错则整个事务回滚,返回 `503 UNAVAILABLE`(不冻结,不留下"已接收"),失败详情另在独立短事务中写入事件表(`message_id` 为空,保留诊断身份)。 |
| REQ-IN-02 | 异步模式(`ACCEPT_THEN_CALLBACK`/`FACT`/`NOTIFY`):**事务 1** 插入消息(`RECEIVED`)并写入框架生成的首次响应(模式对应 code、`data={}`),提交后返回 ACK。**事务 2** 由 worker 执行 handler:业务写入、`ctx.send` 产生的 outbox 消息、消息标记 `DONE` 在同一事务提交;handler 失败则整事务回滚,失败详情独立记录并退避重试。 |
| REQ-IN-03 | 同身份、同摘要的重复请求:重放首次响应原文(含原 `timestamp`),`code` 不变,响应头带 `Idempotent-Replayed: true`。 |
| REQ-IN-04 | 同身份、不同摘要:`409 CONFLICT`(`reason_code=IDEMPOTENCY_CONFLICT`),写入冲突事件,不执行 handler。 |
| REQ-IN-05 | 同身份的并发首次请求收敛为一次接纳与一份首次响应(依赖唯一键冲突后回读,不依赖应用层锁)。 |
| REQ-IN-06 | 首次被 `REJECTED` 的请求保存摘要与拒绝响应;同身份同内容稳定重放该拒绝,改内容返回 `CONFLICT`。 |
| REQ-IN-07 | `503 UNAVAILABLE` 不得被冻结为后续重放的响应。 |
| REQ-IN-08 | 异步 handler 失败(含确定性异常)按退避**持续重试**,间隔上限默认 1 小时,不设终止次数;达到 `alert_after_attempts` 后告警。失败的事务全部回滚、不产生副作用,因此缺陷修复上线后消息自动处理,无需人工重处理。已返回的 ACK 不撤回。 |
| REQ-IN-09 | 未注册的 operation → `422 REJECTED`(`UNSUPPORTED_OPERATION`);`data` 校验失败 → `422 REJECTED`(`INVALID_DATA`,`data.field_path` 为 JSON Pointer,**不回显字段值**)。 |
| REQ-IN-10 | 异步 handler 的触发**以数据库扫描为准**:后台调度器的即时唤醒只是加速提示;进程在"提交后、通知后台调度器前"退出,消息仍由周期扫描领取并处理。 |
| REQ-IN-11 | 摘要 = SHA-256(规范化 JSON(`operation`, `data`)),基于原始解析值;`timestamp` 与 `operation_id` 不入摘要;规范化为键排序、紧凑分隔、`ensure_ascii=False`、类型敏感(`true`≠`1`≠`1.0`≠`"1"`)。 |

### 5.2 出站

| ID | 要求 |
|---|---|
| REQ-OUT-01 | `ctx.send(...)`/`hub.send(...)` 在**调用者的事务内**写入 `PENDING` 消息;事务回滚则消息不存在(事务发件箱)。 |
| REQ-OUT-02 | 消息创建时**冻结**:`operation_id`、`timestamp`、序列化后的 `data`;所有重试发送同一份内容。 |
| REQ-OUT-03 | `hub.call(target, op, req)` 用于需要立即得到响应的 `SYNC` 调用:先在**自己的短事务**中落库并提交(**不借用调用者事务;调用者须在进入本调用前结束自己的事务**),再发送并回写结果。handler 内禁止调用 `hub.call`,对外调用只能使用 `ctx.send`。 |
| REQ-OUT-04 | `hub.call` 同样受 `shadow`/`step`/暂停约束:`step` 与暂停时抛出明确异常(`ApprovalRequired`/`TargetPaused`),消息按相应状态保留;`shadow` 返回 `SHADOWED` 结果而不发送。调用方取消(协程被取消)时,已落库的消息保持 `SENDING`,由租约过期按"结果未知"处理(REQ-LSE-04)。 |
| REQ-OUT-05 | `shadow`:构造并落库,不发送,状态 `SHADOWED`。`step`:状态 `PENDING_APPROVAL`,经批准 API 后进入 `PENDING`。 |
| REQ-OUT-06 | 出站消息带 `expires_at`(= 创建时间 + `ttl`;无 `ttl` 则不过期)。到期时,`PENDING`/`PENDING_APPROVAL` → `EXPIRED`,不再发送;`SENDING` 中的消息在租约结束后同样处理。`maybe_delivered=true` 的消息仍为 `EXPIRED` 但保留该标记,其后到达的 completion 仍可作为送达证明(REQ-COR-06)。已送达的消息不受影响。过期只表示本地停止发起新发送尝试,不表示对端失败,也不撤销已经发出的请求;对端可能在本地过期后接收或执行。发送前必须再次检查期限。 |
| REQ-OUT-07 | `hub.call` 遇 `DELIVERY_UNKNOWN` 时抛出 `DeliveryUnknown`(携带消息 id);消息此后由后台按重试矩阵用原身份继续重试,调用方**不得**自行重发,最终有效结果响应可从 `response_raw` 读取;BUSY/UNAVAILABLE 不占用该结果字段。 |

### 5.3 领取、租约与崩溃恢复(REQ-LSE)

| ID | 要求 |
|---|---|
| REQ-LSE-01 | 领取在**短事务**内完成:选取到期消息(PostgreSQL 用 `FOR UPDATE SKIP LOCKED`),置 `SENDING`/`PROCESSING`,写入 `lease_id`(随机)与 `lease_until`(= 现在 + 超时 + 余量),提交。**网络请求与 handler 执行在领取事务之外**(出站),或在带租约校验的业务事务内(入站)。 |
| REQ-LSE-02 | worker 结果回写必须带条件 `WHERE id = ? AND lease_id = ? AND status = ?`,期望状态为出站 `SENDING` 或入站 `PROCESSING`。影响行数为 0 时,出站不得覆盖当前结果;入站必须**回滚整个 handler 事务**,包括业务写入、completion 关联与 outbox,不得仅忽略消息更新后提交业务。 |
| REQ-LSE-03 | 周期任务回收租约过期的消息:入站 `PROCESSING` 条件回收为 `RECEIVED` 并使旧租约失效;租约过期不能证明旧 worker 已退出或事务已回滚,仍存活的旧 handler 必须在提交前按 REQ-LSE-02 校验,失败则整体回滚;出站 `SENDING` 按"结果未知"处理(REQ-LSE-04)。 |
| REQ-LSE-04 | 过期的出站 `SENDING`:按 `SENDING` 与旧租约条件回收,使旧租约失效并置 `maybe_delivered=true`;未到期回到 `PENDING`(`attempts+1`,退避后原身份重试),已到期转 `EXPIRED`。不得把已由 completion 置 `DONE` 的记录回收到队列。 |
| REQ-LSE-05 | SQLite 测试环境降级为无行锁扫描,租约与条件回写仍生效;多 worker 竞争只在 PostgreSQL 集成测试中验证。 |

### 5.4 投递结果与重试矩阵(REQ-DLV)

投递状态由 `Channel` 按 §2.1 的两层结果映射产生,不在此重复定义底层发送判据;租约过期按结果未知处理。合法信封的内容是否符合约定按 REQ-CODE-01/03 判断。

**接入前提:对端按 `operation_id` 去重**,语义与 REQ-IN-03/04 相同(同身份同内容重放首次响应且不重复执行,异内容返回 `CONFLICT`)。因此送达未知时一律用原身份重发是安全的。对端不提供查询/对账接口,框架不设对账,也不区分重试类别。

| 结果 | 行为 |
|---|---|
| `NOT_SENT` | 退避后原 `operation_id` 重试 |
| 合法信封的 `UNAVAILABLE`/`BUSY`(明确未接收) | 同上,遵守 `retry_after_ms` |
| `DELIVERY_UNKNOWN` | 置 `maybe_delivered=true`,退避后原 `operation_id` 重试 |
| `CONFLICT` | `FAILED`(终态),停止重试;由来源系统纠正,业务以**新意图、新身份**重新发起 |
| `REJECTED` / 声明为 `FAILURE_TERMINAL` 的 `code` | `FAILED`,不重试 |
| 合法信封但违反契约(REQ-CODE-03) | `FAILED`(`RESPONSE_CONTRACT_VIOLATION`),不重试;置 `maybe_delivered=true`;可被后续 completion 补证为 `DONE` |
| 成功 `code` | `DONE` |

| ID | 要求 |
|---|---|
| REQ-DLV-01 | 重试永远复用冻结消息与原 `operation_id`,禁止生成新身份绕过冲突。 |
| REQ-DLV-02 | 退避为指数 + 全抖动:`delay = random(0, min(cap, base × 2^n))`,默认 base 1s、cap 5min,可按 operation 覆盖;随机源与时钟可注入。 |
| REQ-DLV-03 | 明确未接收与 `NOT_SENT` 类的暂时性失败**不设终止次数**,以退避持续重试(间隔上限默认 5 分钟),直到送达、永久失败或 `expires_at`;达到 `alert_after_attempts` 后告警。不静默丢弃,也不因"对端宕机时间长"而终止。 |
| REQ-DLV-04 | **对端必须按 `operation_id` 去重**,这是接入前提而非可选项:对端须通过一致性套件的幂等重放项(REQ-TST-07)才可上线。对端不具备去重能力时,本框架的重试与 `ACCEPT_THEN_CALLBACK` 可靠性承诺不成立,须先改造对端。 |
| REQ-DLV-05 | 接入合同必须约定去重保留窗口:消息的最大重试期限(含在途请求可能到达的余量)不得超过对端保证的去重期限;有限窗口目标的消息必须设置符合该约束的 `ttl`。无 TTL 的持续重试只适用于对端持久保留身份、摘要及首次响应或等价去重记录的目标。配置与建消息时校验该约束,不允许窗口失效后继续自动发送。本框架去重期限从对端首次接纳起计算,本方以消息创建起算 TTL 作保守约束。R 阶段不清理去重记录;后续归档/清理不得破坏这一合同。接入测试验证幂等行为,保留期限须由双方合同确认,不能由一次测试推断。 |

### 5.5 completion 关联(REQ-COR)

| ID | 要求 |
|---|---|
| REQ-COR-01 | completion 的 `data` 必须在 `ref_field` 处携带**原请求的 `operation_id`**。每个 completion operation R 阶段只对应一个原请求 operation,注册时由 `completion` 声明建立唯一反向映射,多义映射启动失败。入站 completion 只关联出站原请求,关联键为 `(app_id, direction=out, 原请求 operation, 该 operation_id)`;R 阶段 app_id 恒为空串。与业务编号无关。 |
| REQ-COR-02 | 关联成功时在 completion 的 handler 事务内更新原请求的 `outcome`。**首个终态胜出**:在事务内锁定原请求行后判断并更新 outcome;并发 completion 也必须串行判定。已有终态后,相同终态的重复 completion 不再次执行业务 handler;**矛盾**终态只写 `CONFLICT` 事件并告警,不覆盖也不执行业务 handler。 |
| REQ-COR-03 | 引用的 `operation_id` 找不到原请求时,completion 消息标记 `unmatched`,暂不执行其业务 handler,保留为 `RECEIVED` 并退避重试关联、告警。匹配后在同一 handler 事务中完成业务处理、关联和 `DONE`;已经 `DONE` 的 completion 不再次执行 handler。 |
| REQ-COR-04 | 同一业务编号的多轮请求互不影响,因为关联只依据 `operation_id`。`business_key` 仅供查询、展示。 |
| REQ-COR-05 | 作为服务端接收 `ACCEPT_THEN_CALLBACK` 请求时,框架提供 `ctx.complete(outcome, payload)`:outcome 显式取 `SUCCEEDED` 或 `FAILED`,框架将类型化 payload 与 outcome、`ref_field` 引用身份合成后,通过 completion 完整请求模型校验,并经 outbox 发往该 operation 的 `reply_to` 目标;同一事务更新入站原请求 outcome。completion 的 data 中固定保留 `outcome` 字段传递该枚举,`ref_field` 不得与该字段重叠,注册时校验;业务 payload 不得覆盖该字段或引用身份。接收方直接读取并校验 outcome,不猜测业务字段;该字段属于 completion 的 data 合同,不改四字段信封。单个原请求只能产生一个最终 completion,重复完成调用不得另建不同身份的 completion。`ctx.complete` 等价于 `hub.complete` 对当前请求的调用(REQ-COR-07)。 |
| REQ-COR-06 | **completion 是送达的证明。** 原请求处于 `PENDING`/`SENDING` 时,关联成功即置 `status=DONE` 并停止重试,同时设置 `outcome`;`EXPIRED` 且 `maybe_delivered=true` 时同样置 `DONE`(送达已被证明)并设置 `outcome`;`FAILED` 且 `fail_reason=RESPONSE_CONTRACT_VIOLATION` 时同样置 `DONE` 并设置 `outcome`,**保留** `fail_reason` 与原合同错误事件;已 `DONE` 则仅设置 `outcome`;证明送达的状态与 outcome 更新必须同事务完成,同时清空发送租约,使旧 worker 的回写及租约回收均无法命中;处于其他 `FAILED`(对端明确拒绝/冲突、审批拒绝)、`EXPIRED`(`maybe_delivered=false`)、`SHADOWED`、`PENDING_APPROVAL` 时只写 `CONFLICT` 事件并告警,不改状态。 |
| REQ-COR-07 | **延迟完成入口**:基础层公开 `hub.complete(ref, outcome, payload)`,`ref` 为入站原请求的 `(operation, operation_id)`(初始 handler 可经 `ctx.request_ref` 取得并自行保存)。它在**调用者的事务内**执行:锁定原请求行 → 确认其为入站 `ACCEPT_THEN_CALLBACK` → 以条件更新完成**首次终态判定**(`outcome` 仍为 `PENDING`)→ 构造 completion 并经 outbox 写入,同事务提交。已有终态时:`outcome` 与 payload 摘要均相同的重复调用幂等返回已有 completion;不同则抛 `AlreadyCompleted`;任何情况下一个原请求只产生一条最终 completion。业务域只提供原请求身份与业务结果,不接触消息表;何时触发延迟完成由业务域自行负责,框架不做流程编排。 |

### 5.6 消息状态

出站 `status`:`PENDING_APPROVAL` / `SHADOWED` / `PENDING`(含退避等待,由 `next_attempt_at` 区分)/ `SENDING` / `DONE` / `FAILED` / `EXPIRED`。
入站 `status`:`RECEIVED` / `PROCESSING` / `DONE` / `REJECTED`。
**停止自动发送/处理的状态**:`DONE`、`FAILED`、`EXPIRED`、`REJECTED`、`SHADOWED`。其中 `EXPIRED`(`maybe_delivered=true`)与 `FAILED`(`fail_reason=RESPONSE_CONTRACT_VIOLATION`)允许 REQ-COR-06 规定的事实补证转换至 `DONE`,不得重新进入发送队列。`PENDING_APPROVAL` 只由批准/拒绝或过期推进,其余开放状态由自动机制推进;**没有 `DEAD`**。
`outcome`(仅 `ACCEPT_THEN_CALLBACK` 请求):`PENDING` / `SUCCEEDED` / `FAILED`。
状态转换集中在一处实现,非法转换抛错。

## 6. HTTP 映射与自描述端点

| 端点 | 说明 |
|---|---|
| `POST /interop/v1/messages` | **入站网关**。按 `operation` 分发 |
| `GET /interop/v1/operations` | 注册表:名称、模式、方向、版本 |
| `GET /interop/v1/operations/{name}` | 请求/响应 JSON Schema 与声明的 `example` |

- 协议端点实现为**独立子应用**(`app.mount`),自带异常处理器、响应格式与数据库会话依赖;不继承全局限流与"校验失败统一 400"。测试通过子应用自身的依赖覆盖注入。
- 路径固定为 `/interop/v1/...`,不带项目其他前缀。
- 出站请求 `POST {target.base_url}{operation.path}`,头带 `Content-Type: application/json` 与 W3C `traceparent`;信封不携带追踪字段。
- 状态码由 `code` 决定,不由 handler 直接设置。

## 7. 可测试性

| ID | 要求 |
|---|---|
| REQ-TST-01 | handler 为纯 async 函数,可不经 HTTP 直接单测(传入测试用 `Ctx`)。 |
| REQ-TST-02 | 时钟、UUIDv7 生成、随机源、`Channel` 均依赖注入;提供 `FakeClock` 与确定性 ID/随机源。可靠性测试中**不得**出现真实 `sleep`。 |
| REQ-TST-03 | `LoopbackChannel` 让两个框架实例在进程内互相调用。**Mock 对端 = 同一框架 + 脚本化 handler**,不另写 mock 服务。 |
| REQ-TST-04 | 故障注入作为 `Channel` 包装器:延迟、请求落库后断连、重复投递、乱序、`503`、`429`、无信封的网关错误、同身份异内容。 |
| REQ-TST-05 | 信封解析/摘要的属性测试:往返无损、对键顺序与空白不敏感、对类型敏感(含 `true`/`1`/`1.0`)、重复 key 必拒绝、REQ-ENV-09 的各项限制必拒绝且不抛 500。 |
| REQ-TST-06 | 注册表导出契约(各 operation 的请求/响应 JSON Schema、模式、版本)并入库为快照;CI 对比快照,**任何差异都使测试失败**,需显式更新快照并在评审中确认。 |
| REQ-TST-07 | 提供对端一致性套件(conformance):脚本指向任意 base URL,逐项验证信封、幂等重放、冲突、拒绝、ACK 语义,输出逐项 PASS/FAIL。其中**幂等重放项**是对端的接入门禁(REQ-DLV-04);去重保留期限另按 REQ-DLV-05 由双方确认。 |
| REQ-TST-08 | 崩溃恢复用例(PostgreSQL):双 worker 竞争、领取后退出、发送后保存前退出、旧 worker 迟到回写、"提交后未通知后台调度器即退出"、"业务写入后 handler 抛错"、"提交后响应断开"——业务结果均不丢失、不重复。 |
| REQ-TST-09 | 所有 operation 的 `example` 由测试校验符合其模型。 |
| REQ-TST-10 | 长时间故障与自动恢复用例:对端宕机超过原先重试上限后恢复自动续送;`expires_at` 到期不再发送且 `maybe_delivered` 保留;过期后到达的 completion 对 `maybe_delivered=true` 的消息作为送达证明;每个出站重试都复用原 `operation_id` 与冻结内容(断言发送字节一致);入站 handler 缺陷修复后自动处理。 |
| REQ-TST-11 | PostgreSQL 并发:旧 handler 租约被接管后恢复时全部业务/outbox 回滚;completion 先提交后旧发送 worker 返回超时、BUSY 或拒绝,状态/outcome 不倒退;矛盾 completion 并发仅首个终态胜出。 |
| REQ-TST-12 | BUSY → UNAVAILABLE → OK 后 response_raw 为 OK 结果且事件保留各次响应;`SYNC` 收到 `ACCEPTED`、成功 code 与模式不符、HTTP 与 `code` 映射不一致或 `data` 不符合模型时置 `FAILED`(`RESPONSE_CONTRACT_VIOLATION`)、`maybe_delivered=true`、不重试、不标记成功;无信封体的网关错误与 `operation_id` 不一致仍为 `DELIVERY_UNKNOWN` 并重试;SYNC 回滚后仍可独立保存失败事件。 |
| REQ-TST-13 | ACK 不依赖异步 handler 执行;completion 成功/失败显式传递 outcome;提前 completion 匹配后只执行一次 handler;多义映射注册失败;step 拒绝与到期批准遵守状态规则;错误 ACK(如多余字段)之后收到合法成功或失败 completion,均自动收敛为 `DONE` 并设置 `outcome`,合同错误事件与 `fail_reason` 保留,其他原因的 `FAILED` 不被补证;延迟完成:初始 handler 返回并重启进程后,由另一个业务任务经 `hub.complete` 完成原请求,仅生成一条最终 completion,重复/矛盾调用不产生第二条;`response_raw` 为空的各场景(未发送过期、审批拒绝、shadow、合同错误、completion 补证)均给出正确的 `response_absent_reason`。 |
| REQ-TST-14 | 使用可注入时钟验证有限去重窗口限制 TTL,不允许窗口失效后新发送;无 TTL 仅允许持久去重目标;过期在途请求不被误断言为对端未执行。 |

**契约兼容性规则**(评审时遵循):对已发布版本,唯一允许的变更是"请求新增**可选**字段";其余(删字段、改类型、改必填、响应新增字段、改模式)一律发布新版本。

## 8. 数据模型

### 8.1 `interop_message`

迁移 `YYYY-MM-DD_interop_message.py`。

| 列 | 说明 |
|---|---|
| `id` | 主键 |
| `app_id` | 调用方身份,R 阶段恒为空串(§3.3) |
| `direction` / `operation` / `operation_id` | 唯一键 `(app_id, direction, operation, operation_id)` |
| `target` | 出站目标名(入站为空) |
| `remote_addr` | 入站请求的对端地址,仅诊断 |
| `digest` | 请求摘要(hex),基于**原始解析值** |
| `request_raw` / `response_raw` | 请求原文冻结不变。入站 `response_raw` 为不可变的首次响应。出站 `response_raw` 仅在**满足 REQ-CODE-01 的有效响应**使消息进入终态时写入(成功,或对端明确的拒绝/冲突/终态失败),与终态同事务保存;BUSY/UNAVAILABLE 只写尝试事件,不占用该字段。其余情形**允许为空且不得伪造响应**:尚未收到最终响应、`SHADOWED`、未发送即 `EXPIRED`、step 拒绝(`APPROVAL_REJECTED`)、合同错误(`RESPONSE_CONTRACT_VIOLATION`,响应原文只在事件 `response_raw`)、由 completion 补证送达 |
| `status` / `outcome` | §5.6 |
| `fail_reason` | `FAILED` 的原因:`PEER_CONFLICT` / `PEER_REJECTED` / `PEER_FAILURE`(对端明确回应)、`RESPONSE_CONTRACT_VIOLATION`(合法信封但违反约定)、`APPROVAL_REJECTED`(step 拒绝)。只有 `RESPONSE_CONTRACT_VIOLATION` 可被 completion 补证为 `DONE`(REQ-COR-06),其余永久;补证后 `fail_reason` 保留作历史 |
| `attempts` / `next_attempt_at` | 重试控制;索引 `(status, next_attempt_at)` |
| `maybe_delivered` | 出现过送达未知的标记(出站);过期后用于判断后续 completion 能否作为送达证明(REQ-COR-06) |
| `lease_id` / `lease_until` | 租约(§5.3) |
| `business_key` | 业务编号,仅展示/查询,索引 |
| `ref_operation_id` | completion 引用的原请求 `operation_id`,索引;`unmatched` 标记 |
| `expires_at` | 出站有效期截止(REQ-OUT-06),空表示不过期 |
| `trace_id` / `created_at` / `updated_at` | 沿用项目约定 |

### 8.2 `interop_message_event`(追加式,不更新不删除)

`message_id`(可空外键;SYNC 回滚后允许无消息行,独立保存 `app_id/direction/operation/operation_id` 诊断身份)、`at`、`kind`(`RECEIVED` / `ATTEMPT` / `RESPONSE` / `STATE` / `CONFLICT` / `ERROR`)、`attempt_no`、`http_status`、`code`、`detail`(**不含报文字段值**)、`response_raw`(可空,单次尝试实际收到的响应原文,受正文上限约束;未收到则为空,超限只记录错误类别)。原文只放专用字段,不写入 detail 或日志。用于导出历次尝试、冲突报文摘要和完整状态时间线。

### 8.3 原文保存

消息的 `request_raw` / `response_raw` 与事件的 `response_raw` 按上述方向和尝试语义原样保存,查询及证据导出返回相应原文。脱敏与访问控制随安全阶段引入,**不在R 阶段**;引入时只改查询与导出,不改摘要与可靠性逻辑(摘要始终基于原文)。

## 9. 联调易用性

### 9.1 `sys.*` 联调阶梯(内置 operation)

| 级别 | operation | 证明 |
|---|---|---|
| L1 | `sys.ping@v1` | 网络可达、信封可解析 |
| L2 | `sys.echo@v1` | 任意 `data` 无损往返(类型、中文、边界大小) |
| L3 | `sys.describe@v1` | 对端回报支持的 operation 与版本,生成**双方差异报告** |
| L4 | 业务 operation | 业务联调 |

### 9.2 调试能力

| ID | 要求 |
|---|---|
| REQ-DBG-01 | 报文查询 API:按 target、operation、状态、`operation_id`、`business_key`、时间范围筛选;返回原文与事件时间线;`response_raw` 为空时附带 `response_absent_reason`(`NOT_YET` / `SHADOWED` / `EXPIRED` / `APPROVAL_REJECTED` / `CONTRACT_VIOLATION` / `PROVEN_BY_COMPLETION`),不构造响应。 |
| REQ-DBG-02 | **框架不提供任何人工恢复入口**:人工裁决、原样/另存重发、重处理、改状态、解锁。异常的退出路径只有自动机制:重试续送、对端后续事实(REQ-COR-06)、`expires_at`。需要重新发起业务动作时,由**业务代码**以新意图、新 `operation_id` 发送。 |
| REQ-DBG-03 | `step` 模式的批准/拒绝 API(**仅联调使用**;生产目标不得配置 `step`)。仅允许对 `PENDING_APPROVAL` 条件更新:批准转 `PENDING`,拒绝转 `FAILED` 并置 `fail_reason=APPROVAL_REJECTED`、记事件,不生成对端响应;记录操作人。已到期按 `EXPIRED` 处理,不得再批准;拒绝后不提供恢复入口。 |
| REQ-DBG-04 | 证据导出:选定消息的请求/响应原文、事件时间线,单个 JSON 文件。 |
| REQ-DBG-05 | 异常记录可见性:达到 `alert_after_attempts` 的长期重试、`FAILED`、`unmatched` 的 completion 必须可经 REQ-DBG-01 按状态筛选,并产生告警日志;它们**只是诊断信息,不是继续运行的前置条件**。 |
| REQ-DBG-06 | **运行期暂停开关**(Redis 键),粒度为 target,可选 operation 名前缀。暂停期间匹配的出站消息保持 `PENDING` 不发送,入站仍接收并落库。 |
| REQ-DBG-07 | 调试 API 沿用项目 RBAC,权限码 `interop:message:read`、`interop:target:pause`、`interop:step:approve`;同步更新 `INITIAL_PERMISSIONS` 与权限测试。(项目管理接口的常规授权,不属于协议层安全。) |

## 10. 组件与分层

### 10.1 首期与后续目录边界

首期已实现目录(类型单独放在 types.py):

```text
app/http_client/
  __init__.py    公开 Protocol、请求/结果类型及生产实现
  client.py      单次 HTTP、连接池、超时、正文限制
  types.py       Protocol、配置、请求及结果类型
tests/http_client/   合成 HTTP 合同与故障测试
```

不得增加仅转发参数的包装层。首期的分层检查覆盖 `app/http_client` 不导入 app.core、app/interop、业务域、数据库、Redis 或 Celery;除静态规则外用隔离进程验证导入不需要数据库/Redis/JWT 配置,父包初始化不得引入这些依赖。下述 app/interop 目录和 REQ-ARC-* 是**后续可靠层**约束;首期不创建这些模块或接入任何业务域。

### 10.2 后续可靠交互与业务能力分离

```
基础能力  app/interop/                     不知道任何业务域的存在
  envelope.py      信封模型、严格解析与限制、规范化摘要、UUIDv7
  registry.py      @operation 装饰器、注册表、契约导出、业务域模块加载
  targets.py       target 配置与校验
  hub.py           入站分发 / 出站 send & call、Ctx
  outbox.py        状态机、租约、退避、关联
  channel.py       Channel 协议、HttpChannel、LoopbackChannel
  testing/         FakeClock、确定性随机源、MockPeer、故障注入、conformance
  ops/sys.py       sys.ping / echo / describe
app/models/interop_message.py      (含事件表;并在 app/models/__init__.py 导出)
app/controllers/interop.py         协议子应用 + 调试 API
后台调度器(载体及入口 R 开工前确定)  领取投递、租约回收、入站 handler、重新关联
scripts/interop_conformance.py     一致性套件入口

业务能力  app/integrations/<域>/            后续各自独立;本 SPEC 不含
```

| ID | 要求 |
|---|---|
| REQ-ARC-01 | `app/interop` **不得导入** `app/integrations` 的任何模块。 |
| REQ-ARC-02 | 各业务域之间**不得互相导入**;确需共用的能力必须上移到基础层,或各自实现。 |
| REQ-ARC-03 | 基础层通过**显式模块清单**(配置项 `INTEROP_MODULES`,R 阶段默认为空)加载业务域;不扫描目录,不在基础层内 `import` 业务域。清单中的模块加载失败则启动失败。 |
| REQ-ARC-04 | 基础层的测试**只使用 `sys.*` 与测试内自造的合成 operation**(`SYNC`/`ACCEPT_THEN_CALLBACK`/`FACT`/`NOTIFY` 各一个),不出现任何业务域的名词或 payload。 |
| REQ-ARC-05 | 业务域只能使用基础层的**公开接缝**:由 `app.interop` 公开导出的 `@operation`、`Ctx`(`session`/`send`/`complete`)、`hub.send` / `hub.call` / `hub.complete`、`Channel` 及 `app.interop.testing` 工具。业务类型化 client 使用 hub 公开入口;handler 内禁止 `hub.call`,只允许 `ctx.send`。不得依赖基础层的内部模块或直接读写 `interop_message*` 表。 |
| REQ-ARC-06 | 业务域各自拥有:payload 模型、handler、数据表与迁移、权限码、`example` 与夹具、对业务代码暴露的**类型化 client**。基础层只拥有 `interop:*` 权限码与 `interop_message*` 表。 |
| REQ-ARC-07 | 业务域新增 operation 不得要求修改基础层任何文件。若需要,说明基础层接缝不足,先改基础层并通过 REQ-ARC-04 的测试。 |

`Channel` 是基础层的收发端口;`transport` 一词在本项目中专指**搬运业务域**,二者不混用。

后续可靠交互层实施时才同步修改(不是首期清单):
- `app/main.py`:挂载子应用、注册调试路由;
- R 发送器:常驻单事件循环、客户端生命周期及调度接入按 REQ-HTTP-08 设计;不能仅增加 Celery include/beat 后照搬每任务 asyncio.run;
- 子应用自己的数据库会话依赖与测试覆盖;
- `tests/architecture/test_layering.py`:为 `app/interop` 增加规则(不得导入 `controllers`),并增加 REQ-ARC-01/02/05 的导入方向检查;
- 配置:`INTEROP_TARGETS`(JSON,含 `defaults`)、`INTEROP_MODULES`、`INTEROP_MAX_BODY_BYTES`、`INTEROP_DEFAULT_TIMEOUT_MS`;
- `httpx` 在 H 阶段已移入正式依赖,R 阶段复用,不重复迁移。

## 11. 错误处理

首期 HttpClient 遵循 REQ-HTTP-09/10,失败表达统一见 §2.1 的表格,取消透传;不产生信封、不转换为 AppError 或业务状态。以下仅适用于后续可靠协议层。

- 协议端点永不返回框架的 `AppError` 体系响应;一切失败映射为 §3 的信封或无体 `400/413`。
- `last_error` 与日志不得包含报文字段值,只含 `field_path`、错误类别、`operation_id`。
- handler 的未捕获异常处理见 REQ-IN-01 / REQ-IN-02。

## 12. 交付与风险

**H1/H2/H4 已实现、验证并复审通过;H3 后移。后续 R 与业务接入未实施,须另行授权。**

| 步 | 内容 | 验收 |
|---|---|---|
| H1 | HttpClient Protocol、bytes 请求/响应、DI、单次发送及交付事实分类 | fake 注入;method/path/query/headers/body;3xx/4xx/5xx 原样返回;不重试/重定向 |
| H2 | 超时、连接池、流式大小限制、取消与关闭 | 阶段/总期限、精确上限与超限、编码拒绝、连接释放、关闭后不发网 |
| H3(后移) | 出口速率与并发准入限流 | 不属于本次交付;无配置、实现或验收前置要求 |
| H4 | 聚焦回归及分层检查 | §14.1 全部验收,仅 app/http_client、聚焦测试与依赖必要改动;没有消费者时不新增应用装配;无业务代码迁移 |

后续 R 阶段保留 §3–9 的可靠层设计:先严格协议与模型,再持久 Inbox/Outbox、租约与恢复,再可选 completion 和调试。开工前复核其接入合同、独立交付清单和 §14.2;首期通过不代表 R 阶段完成。WMS/Transport 接入另立范围,保持现有能力不变。

**后移**:可靠交互层实现、安全、H3 出口限流及跨进程限流、业务域接入、自动样例、契约差异自动分类、录制与日志归档。

**风险**
- H 阶段仅有连接池资源限制,不保证出口请求速率或跨进程额度。HTTP 完整响应不证明协议接纳或业务完成,上层不得因此绕过原有身份与完成校验。
- **R 阶段网关未引入协议安全**:任何能访问网关的调用方都可以投递消息;完全依赖网络隔离。引入安全前不得暴露到不受信网络。
- **R 阶段全局幂等空间**(R 阶段 `app_id` 为空):对端生成的 `operation_id` 若在不同系统间撞车且内容不同,会得到 `409`;内容相同则被当作重复。引入 `app_id` 后改为按调用方隔离,无需迁移。
- **R 阶段**规范化摘要为 RFC 8785 的子集:浮点按 Python `repr`;第三方使用浮点时须在 operation 层约定用字符串承载。
- **R 阶段** `FOR UPDATE SKIP LOCKED` 在 SQLite 不可用,多 worker 竞争只能在 PostgreSQL 集成测试中验证。
- "子应用绕开全局限流""`dependency_overrides` 不传播"基于阅读,**尚未运行验证**,R 阶段挂载协议子应用时需加测试。
- **R 阶段**原文明文保存且 API 直接返回,含敏感业务数据时需在安全阶段处理。
- **R 阶段对端去重是接入前提**:对端若实际不按 `operation_id` 去重,重试可能造成重复执行;只能靠一致性套件的幂等重放项在接入时拦截,运行期无法检测。入站确定性失败的消息以低频持续重试,靠告警发现、靠缺陷修复收敛。

**兼容**:首期纯新增非业务客户端,不改动现有路由、表、权限及 WMS/Transport 行为。后续可靠层才涉及新增表、迁移与权限种子。

## 13. 已定决策与后续输入

| # | 问题 | 当前默认 | 影响 |
|---|---|---|---|
| Q2 | **已确定**:code 使用字符串枚举,operation_id 使用 UUIDv7,timestamp 使用 UTC Unix 毫秒整数 | 按 §3 固化,不支持数字 code 或并存 | 不再是阻塞项 |
| Q4 | 消息量与日志保留期?(不阻塞,归档任务后移) | 90 天仅为后续日志保留讨论值,R 阶段不清理;去重记录须满足 REQ-DLV-05 | 影响索引、分区、归档,不得据此删除去重依据 |

**D 阶段输入(不阻塞 H 阶段,业务域立项时回答)**:存量对端能否按本协议改造;现有 WES 与本项目的关系及其转发的下游的路径、报文是否可改;首个业务域的选择。

**已后移到安全阶段**:鉴权方式(`app_id + app_secret`)、来源识别、脱敏、原文保存策略。

## 14. 验收标准

### 14.1 首期 HttpClient

1. H 阶段生效需求 REQ-HTTP-01~06、09~11 均有可观察行为测试;07 后移、08 为 R 阶段运行约束、12 为未来装配约束;fake 注入不依赖业务模型或 operation。
2. 原始 bytes 不变;3xx/429/503/非 JSON 响应不自动重试、不解释业务语义;一次 send 至多一次网络发送。
3. 阶段及总超时覆盖慢速分块和连接池等待;请求/响应精确上限允许、超限拒绝,未声明或伪造 Content-Length 不绕过限制。
4. H3 不实施,不验收出口速率/并发准入。验证 trust_env=False、禁止重定向及重试、origin 约束和原始流读取;假时钟仅验证截止时间计算,不证明实际超时取消;本地网络测试使用真实事件循环、事件协调和硬截止时间验证实际取消。
5. 响应完整/异常/取消/超限均释放资源;客户端共享连接池、aclose() 幂等且关闭后抛 ClientClosedError 不再发送。通过可控本地 HTTP 测试验证实际 timeout/流式读取/连接复用,仅 fake 结果不足以证明这些行为;不调用外部业务系统。
6. app/http_client 分层及独立导入检查通过;无数据库表、迁移、业务路由、权限、Celery 任务或 WMS/Transport DTO。httpx 移入正式依赖并更新锁文件是未来实现清单的一部分。
7. 聚焦测试、ruff check 与 format --check 通过;现有项目行为不变。不将纯 HttpClient 测试称为幂等、可靠交付或物理完成验收。

### 14.2 后续可靠交互层(不属于首期)

1. 所有 R 阶段生效的 `REQ-*` 均有至少一个自动化测试且通过(`uv run pytest`);`ruff check`、`ruff format --check` 干净。
2. 契约快照测试存在,且任何 schema 差异都会使其失败。
3. 两个框架实例经 `LoopbackChannel` 完成 L1–L3 联调阶梯与一个 `ACCEPT_THEN_CALLBACK` 合成示例。
4. §5.4 矩阵每一行有故障注入用例;每个出站重试都复用原 `operation_id` 与冻结内容(断言发送的字节一致)。
5. REQ-TST-08 的全部崩溃恢复用例在真实 PostgreSQL 下通过。
6. 同一 `operation`/`operation_id` 的重复请求、并发首次请求、异内容冲突均符合 §5.1;`app_id` 列存在且已是唯一键的一部分。
7. 进程重启后,未完成的入站异步处理与出站投递均按原冻结内容继续,不丢失、不重复。
8. 一致性套件可对运行中的实例执行并输出逐项报告。
9. `tests/architecture/test_layering.py` 通过且已覆盖 `app/interop`;迁移单一 head。
10. REQ-ARC-01~05 的边界测试通过:基础层不导入业务域,业务域互不导入且只用公开接缝,基础层测试不含任何业务域名词。
11. 对端长时间不可用(超过原先的重试上限)后恢复:未过期的消息**自动续送**,无需任何人工操作;已过期的消息不再发送。
12. 入站异步 handler 因缺陷持续失败后,缺陷修复上线,消息**自动处理**;全程不存在人工恢复入口(REQ-DBG-02)。
13. REQ-TST-11~14 全部通过;并发租约失效不提交业务、completion 不被迟到发送覆盖、最终响应可读取、去重窗口约束有效。

14. R 发送器在长生命周期单事件循环内运行,客户端不随任务重建;以数据库扫描为恢复依据,即时唤醒缺失不丢消息。后台调度器具体载体、启动/停机与调度接入在 R 开工前确定,不得将本条解释为 H 阶段建设 worker。

## 15. 评审记录

| # | 来源 | 意见 | 处置 |
|---|---|---|---|
| 1 | 评审 P1 | 入站 peer 身份无来源 | **后移(用户决定:安全暂不考虑)**。v2 的来源识别(REQ-PEER-*)整体删除;保留 `app_id` 列并纳入唯一键作为前向兼容点(§3.3) |
| 2 | 评审 P1 | 入站事务与副作用边界 | **采纳**。REQ-IN-01/02/10 |
| 3 | 评审 P1 | 领取后崩溃恢复 | **采纳**。§5.3 租约与 REQ-TST-08 |
| 4 | 评审 P1 | 脱敏与冻结原文冲突 | **后移(安全)**。本期取消脱敏(原文保存、API 返回原文);摘要始终基于原文,日后引入脱敏只改查询与导出(§8.3) |
| 5 | 评审 P1 | RECONCILE 不可执行 | **采纳**。§5.4 对账协议、REQ-REC-*、人工裁决(人工裁决/重发部分已被 #18 取代,对账协议已被 #19 取代) |
| 6 | 评审 P1 | 回调关联不精确 | **采纳**。§5.5 |
| 7 | 评审 P1 | 调试重放绕过约束 | **采纳并更保守**。删除生产入站重放;原样重发限 `FAILED`/`DEAD` 且 SAFE(人工裁决/重发部分已被 #18 取代) |
| 8 | 评审 P2 | 幂等判定顺序 | **采纳**。§5.1 固定顺序 |
| 9 | 评审 P2 | HTTP 与 code 组合 | **采纳**。REQ-CODE-01/02 |
| 10 | 评审 P2 | 证据保存 | **采纳**。追加式事件表 |
| 11 | 评审 P2 | `hub.call` 语义 | **采纳**。REQ-OUT-03/04 |
| 12 | 评审 P2 | 严格 JSON 补充 | **采纳**。REQ-ENV-09 |
| 13 | 评审 P2 | 契约兼容性 | **部分采纳**。保守兼容规则;样例改为声明并校验;自动分类后移 |
| 14 | 评审 P2 | 交付顺序 | **采纳**。建表前移到 S2 |
| 15 | 评审 P2 | 后移项 | **采纳**。 |
| 16 | 用户 | 先建基础能力,基础与业务彻底分离 | **采纳**。§10.1、REQ-ARC-*;端口改名 `Channel` |
| 17 | 用户 | 先固化基础能力设计,不选业务域;安全后移(`app_id + app_secret`) | **采纳**。本 SPEC 收敛为基础能力固化稿(v3):peer 收缩为出站 `Target`,删除 REQ-PEER-*/REQ-RED-*、Q3/Q5/Q6/Q9,新增 §1.3 冻结清单;业务相关问题移入"D 阶段输入" |
| 18 | 用户 | 参照 wes 无阻塞执行设计,是否不应有人工裁决 API,而用后续重试覆盖或异常记录 | **采纳**。删除人工裁决、原样/另存重发;取消 `DEAD`,重试不设终止次数(`max_attempts`→`alert_after_attempts`);新增 `expires_at`/`EXPIRED`、REQ-COR-06(completion 即送达证明);异常保留为可诊断记录,不阻塞其他消息 |
| 19 | 用户 | 对端不提供自动对账接口 | **采纳**。删除对账协议(`reconcile` 声明、四种对账结果、REQ-REC-*);`retry` 改为 `SAFE` / `NO_RESEND` 且必须显式声明;状态 `RECONCILING` 改名 `UNKNOWN`,只由对端后续事实(completion)离开;新增 `maybe_delivered` 列、REQ-OUT-07、REQ-UNK-*;`SAFE` 的依据是一致性套件的幂等重放项(已被 #20 简化) |
| 20 | 用户 | 对端支持按 `operation_id` 去重 | **采纳**。去重成为接入前提(REQ-DLV-04);删除 `retry` 声明、`NO_RESEND`、`UNKNOWN` 状态与 REQ-UNK-*;重试一律复用原身份;保留 `maybe_delivered`(用于过期后 completion 的送达证明);一致性套件的幂等重放项为接入门禁 |
| 21 | 本轮复审 P1 | 旧 handler 租约失效仍可能提交业务 | **采纳**。REQ-LSE-02/03:条件更新失败回滚整个业务事务,租约过期不等于进程退出 |
| 22 | 本轮复审 P1 | completion 与发送回写竞争 | **采纳**。REQ-COR-02/06、REQ-LSE-02/04:原请求行锁、状态条件、清空租约 |
| 23 | 本轮复审 P1 | 首次 BUSY 占用最终响应 | **采纳**。§8 按方向保存响应,每次响应原文进入事件专用字段 |
| 24 | 本轮复审 P1 | 异步 ACK 与 completion 合同缺失 | **采纳**。框架空对象 ACK、显式 outcome、唯一反向映射、未关联先不执行 handler |
| 25 | 本轮复审 P1 | 无限重试超过去重保留期限 | **采纳**。REQ-DLV-05 与 target 去重保留合同;无 TTL 要求持久去重 |
| 26 | 本轮复审 P2 | 响应模型、孤立失败事件、过期补证、公开入口与 step 拒绝 | **采纳**。§3.2、§8.2、§5.6、REQ-ARC-05、REQ-DBG-03 同步修订 |
| 27 | 用户 | Q2 确定,仅修订设计 | **采纳**。字符串枚举、UUIDv7、毫秒整数;保持基础与业务分离、安全后移,不实现代码 |
| 28 | 本轮复核 | 响应违反契约不应无限重试;新增验收未映射到交付步骤 | **采纳**。新增 REQ-CODE-03(合法信封但违反契约 → 终态 `FAILED`,`RESPONSE_CONTRACT_VIOLATION`);REQ-CODE-02 收窄为"非合法信封";重试矩阵与 REQ-TST-12 同步;S3/S4 验收补充 REQ-TST-11~14 |
| 29 | 复审 | P1:`RESPONSE_CONTRACT_VIOLATION` 与 completion 补证冲突;P2:延迟完成缺少公开入口;`response_raw` 空值规则不一致 | **采纳**。新增 `fail_reason`(区分对端明确拒绝与合同错误,仅后者可补证);REQ-COR-06 允许 `FAILED(RESPONSE_CONTRACT_VIOLATION)` → `DONE`;新增 REQ-COR-07 `hub.complete`(`ctx.complete` 为其便捷调用);重写 `response_raw` 空值规则并在查询中返回 `response_absent_reason`;REQ-TST-13 补验收 |
| 30 | 用户 | 先交付纯 HttpClient,DI 注入业务;幂等及持久化重试上移 | **采纳**。§1/2.1/10/12/14 分离 H 与 R 阶段;清理本设计业务耦合,不修改或删除现有 WMS/Transport 能力;安全后移,本轮不实现代码 |
| 31 | 用户 | HttpClient 独立目录、选(a)、H3 后移、结果映射与 httpx 约束 | **采纳**。app/http_client;H 仅单次 HTTP;R 发送器常驻单事件循环;独立结果类型、统一错误表达及阶段用语;业务能力不变、安全后移,不实现代码 |
| 32 | 完整复审 | R 调度载体、未来装配验收与文字歧义 | **采纳**。R 正文统一后台调度器并补常驻循环验收;12 为未来装配约束;明确私有 CA 注入、假时钟边界、结果映射引用,清理 H4 措辞;仅文档修订 |
