# 纯异步 HttpClient

状态:**已实现并复审通过**(首期交付);出口限流后移 · 分支:`feature/spec-019-interop-framework`

> 本文自完整设计中摘出,是当前交付基线,自成一体。上层的可靠交互等后续方案不属于本文验收。

## 1. 范围

- **先交付纯异步 HttpClient**:支持构造参数 DI、超时、连接池、请求/响应大小限制;不含出口并发及速率限流器。只完成一次 HTTP 调用,不理解 JSON 信封、operation 或业务结果。
- **业务能力独立**:WMS、Transport 等业务域拥有自己的协议、payload、身份关联及业务/物理结果解释。已有可靠任务可以直接注入 HttpClient,不强制经过 Interop。
- 首期交付 已实现并验证,出口限流、可靠交互层与业务接入未实施。“清理业务能力”指清理本设计中的业务耦合,不删除或修改 wes_backend、maintenance-agent 或本项目已有业务代码。

### 1.1 非目标

- 不实现入站网关、operation 注册表、信封解析、UUIDv7、幂等表、Outbox、Celery 重试任务、completion、shadow/step、暂停或调试管理 API。
- 不新增任何 WMS/Transport 业务模块、DTO、设备模型、协议转换或兼容迁移;不改变既有 ACK、重复响应、摘要、重试及完成判定。
- 不实现自动重试、自动重定向、出口并发/速率限流、业务限流策略、跨进程共享限流、熔断、动态配置、配置 CRUD 或通用映射 DSL。
- 安全继续后移:业务鉴权、签名、来源识别、报文脱敏另立范围;底层 HTTPS 仍使用标准证书校验,不因安全后移而禁用 TLS 校验。
- 不添加 DI 容器或新的 HTTP 依赖;已复用现有 `httpx` 并移入正式依赖(`pyproject.toml`)。

三个层次:HttpClient、可靠交互层、业务域接入(各业务域独立)。出口限流明确后移,不是 首期验收项;连接池 max_connections 仍保留,它是网络资源限制,不是新增的出口准入调度器。安全独立后移。

## 2. 设计

**实现状态图例**:✅ 已实现(代码在 `app/http_client/`,测试在 `tests/http_client/`,当前 154 项测试含分层检查通过)· 📐 设计,未实施 · ⏸ 已后移。下表 ID 后的符号即该条状态:REQ-HTTP-01~06、09~11 已实现;07 后移;08、12 是对未来消费者的约束,尚未实施。

### 分层与唯一生命周期所有者

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

### 公开接口与 DI

`HttpClient.send(HttpRequest) -> HttpResult`、`HttpClient.aclose()` 是公开异步接口。接口用 Python Protocol 表达,由基于 `httpx.AsyncClient` 的实现提供;不包装成业务 Service,不引入 DI 框架。

**后续公开校验接缝(📐 已确定,尚未实现)**:`validate_request(config: HttpClientConfig, request: HttpRequest) -> None` 为同步、无网络副作用的模块级函数,**只依赖客户端配置**(base_url、正文上限等),不需要客户端实例,因此在没有客户端的进程(如 Celery worker、脚本)中也可调用,且不改变 `HttpClient` Protocol(`send` / `aclose` 不变)。非法参数仍抛 `ValueError` / `TypeError`。实现时与 `send` 复用同一套 method、path、query、headers、body 与 origin 校验规则,不得在调用方或协议适配器复制;`send` 保留自己的校验,不能假定请求已经预校验。该函数与测试夹具随可靠交互层接入时交付;当前代码仍只有 `send` / `aclose`,校验逻辑在 `send` 内部,本轮仅修订设计。

| 对象 | 最小合同 |
|---|---|
| `HttpRequest` | method、相对于已配置 base_url 的 path、query、headers、body bytes;不提供业务 JSON 或 operation 字段。path 不得切换 origin,不得包含查询分隔符 `?`,查询统一通过 query 传入;不得包含 `.` / `..` 路径段(含百分号编码形式),使 base_url 前缀不可被逃逸;首期不提供逐请求超时覆盖,使用实例配置 |
| `HttpResult` | delivery_state、failure_kind、可选 HTTP status、响应 headers(保留重复项)、有界响应 body bytes;响应不完整时带错误类别,不得当作完整响应使用 |
| 客户端配置 | base_url、connect/read/write/pool timeout、total timeout、max_connections、max_keepalive_connections、keepalive_expiry、max_request_body_bytes、max_response_body_bytes、可选 tls_context(ssl.SSLContext)、destination_key(稳定的非敏感出口诊断标识) |
| DI | 协议适配器构造参数接收 HttpClient;测试直接注入 fake。生产实现接受可替换的 HTTP transport 和单调时钟,用于网络故障与截止时间计算测试;假时钟不控制事件循环的实际取消计时 |

基础层不定义目标管理系统;装配处以稳定 `destination_key` 记录出口诊断。同一进程、同一长生命周期事件循环、同一出口的消费者共享客户端以复用连接池,不跨循环使用。不为没有消费者的配置创建空客户端或增加业务调用方。

### 首期需求

| ID | 要求 |
|---|---|
| REQ-HTTP-01 ✅ | 一次 send 至多发起一次网络请求。自动重试、自动重定向及认证握手式自动重发关闭;3xx/4xx/5xx 原样作为 HTTP 响应返回,不调用 raise_for_status,不解释 429/503 是否未接收。Retry-After 仅作为响应头交给上层 |
| REQ-HTTP-02 ✅ | 请求 body 按原字节发送,不 JSON 解码、重编码、改字段或生成身份;不强制 Content-Type。header 名值及 URL 基本合法性在发网前校验,拒绝 CR/LF 和冲突的正文 framing;Content-Length 由底层生成,不接受调用者覆盖 framing |
| REQ-HTTP-03 ✅ | 支持连接、读、写、连接池等待四类超时及总截止时间。总超时默认 10000ms,从 send 进入开始覆盖连接池等待、连接、发送、完整响应读取;阶段超时不得延长总期限。所有时间和资源配置启动时校验正值及连接池关系 |
| REQ-HTTP-04 ✅ | 复用连接池;限制总连接、keepalive 连接及空闲存活期。每个进程/事件循环独立持有客户端,不跨 fork 或 asyncio.run 的事件循环复用;同一生命周期共享使用,aclose() 幂等;关闭后再次发送抛 ClientClosedError 且不发网 |
| REQ-HTTP-05 ✅ | 请求/响应正文默认上限均为 256KiB,可配置。请求 bytes 超限在发网前拒绝;响应流式累计检查上限,不先 read 全文再判断,不只相信 Content-Length。精确等于上限允许,超过立即停止读取并关闭响应 |
| REQ-HTTP-06 ✅ | 首期仅接受 identity 响应编码,发送 Accept-Encoding: identity;遇到非 identity Content-Encoding 返回明确错误并关闭响应,不做自动解压以绕过大小限制。调用方不得覆盖此编码约束 |
| REQ-HTTP-07 ⏸ | **后移,不属于首期**:出口并发/速率准入调度及 max_concurrency/rate_limit 配置暂不实现;不以连接池限制宣称具备 FIFO、滚动窗口或全局额度保证。未来限流方案另行明确作用域、准入与取消不变量 |
| REQ-HTTP-08 📐 | 可靠交互层运行方向选 **(a)**:发送器装配为 FastAPI lifespan 内的后台任务,共用 api 进程的长生命周期单事件循环,客户端和将来限流器随该循环创建/关闭,不在每个发送任务内 asyncio.run 后重建。首期不新增 worker、不改现有 Celery 任务。R 开工前明确启动/停机及调度接入;将来单循环额度不代表跨进程总额度 |
| REQ-HTTP-09 ✅ | 只在能证明未进入发送阶段时返回 NOT_SENT(如 pool/connect 失败或发送前总期限耗尽);写/读超时、发送后协议错误、响应超限/编码不支持为 DELIVERY_UNKNOWN。完整读取 HTTP 响应为 HTTP_RESPONSE_COMPLETE,不论 HTTP 状态和正文格式。保留可取得的 status/headers 与失败类别,不把部分正文冒充完整结果 |
| REQ-HTTP-10 ✅ | 调用者取消须清理响应/连接、归还连接池资源并继续传播 CancelledError,不吞掉或转换为业务失败;上层已有可靠任务按发送阶段和租约处理未知,本层不排队续送。进入发送阶段前取消不发网 |
| REQ-HTTP-11 ✅ | 不导入 app.core、数据库、Redis、Celery、controllers、app/reliable 或任何业务域;配置由装配处传入,日志用标准 logging,不通过 app.core 获取 logger。日志只记录方法、出口标识、耗时、状态和错误类别,不输出正文、凭据、查询串或异常携带的原始 URL |
| REQ-HTTP-12 📐 | **未来消费者装配约束,不属于首期行为验收**:由实际应用生命周期建立并关闭实例,FastAPI Depends 仅取得已装配实例;R 发送器按 REQ-HTTP-08 的常驻循环装配。首期无消费者时不创建实例、不新增应用装配或 Celery 任务,只交付可注入组件,无新增路由/表/权限 |
| REQ-HTTP-13 📐 | 公开请求校验函数 `validate_request(config, request)`:仅依赖 `HttpClientConfig`,与 `send` 复用同一规则,供可靠交互层在入队时校验;未实现,随可靠交互层接入交付。 |

### 默认值与配置归属

默认 max_connections=100、max_keepalive_connections=20、keepalive_expiry=5s,四类阶段 timeout 各 5s,total timeout=10s;不提供 rate_limit 或 max_concurrency。参数取值为初始资源默认值,不代表现场吞吐承诺。只给**实际启用的**客户端实例装配参数,不新增 INTEROP_TARGETS、INTEROP_MODULES 或所有业务端点配置。

### httpx 生产约束与失败表达

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


## 3. 目录与分层

首期已实现目录(类型单独放在 types.py):

```text
app/http_client/
  __init__.py    公开 Protocol、请求/结果类型及生产实现
  client.py      单次 HTTP、连接池、超时、正文限制
  types.py       Protocol、配置、请求及结果类型
tests/http_client/   合成 HTTP 合同与故障测试
```


不得增加仅转发参数的包装层。分层检查**当前**覆盖 `app/http_client` 不导入 app.core、业务域、数据库、Redis、Celery 等;对可靠交互层(`app.reliable`)的禁止项**尚未写入测试**,随可靠交互层落地的同一变更补入分层与导入隔离测试;除静态规则外用隔离进程验证导入不需要数据库/Redis/JWT 配置,父包初始化不得引入这些依赖。

## 4. 交付与验收

### 4.1 交付步骤

| 步 | 内容 | 验收 |
|---|---|---|
| 发送与结果分类 | HttpClient Protocol、bytes 请求/响应、DI、单次发送及交付事实分类 | fake 注入;method/path/query/headers/body;3xx/4xx/5xx 原样返回;不重试/重定向 |
| 超时、连接池与限制 | 超时、连接池、流式大小限制、取消与关闭 | 阶段/总期限、精确上限与超限、编码拒绝、连接释放、关闭后不发网 |
| 出口限流(后移) | 出口速率与并发准入限流 | 不属于本次交付;无配置、实现或验收前置要求 |
| 回归与分层检查 | 聚焦回归及分层检查 | §4.2 全部验收,仅 app/http_client、聚焦测试与依赖必要改动;没有消费者时不新增应用装配;无业务代码迁移 |

### 4.2 验收标准

1. 首期生效需求 REQ-HTTP-01~06、09~11 均有可观察行为测试;07 后移、08 为可靠交互层运行约束、12 为未来装配约束;fake 注入不依赖业务模型或 operation。
2. 原始 bytes 不变;3xx/429/503/非 JSON 响应不自动重试、不解释业务语义;一次 send 至多一次网络发送。
3. 阶段及总超时覆盖慢速分块和连接池等待;请求/响应精确上限允许、超限拒绝,未声明或伪造 Content-Length 不绕过限制。
4. 出口限流不实施,不验收出口速率/并发准入。验证 trust_env=False、禁止重定向及重试、origin 约束和原始流读取;假时钟仅验证截止时间计算,不证明实际超时取消;本地网络测试使用真实事件循环、事件协调和硬截止时间验证实际取消。
5. 响应完整/异常/取消/超限均释放资源;客户端共享连接池、aclose() 幂等且关闭后抛 ClientClosedError 不再发送。通过可控本地 HTTP 测试验证实际 timeout/流式读取/连接复用,仅 fake 结果不足以证明这些行为;不调用外部业务系统。
6. app/http_client 分层及独立导入检查通过;无数据库表、迁移、业务路由、权限、Celery 任务或 WMS/Transport DTO。httpx 已移入正式依赖并更新锁文件。
7. 聚焦测试、ruff check 与 format --check 通过;现有项目行为不变。不将纯 HttpClient 测试称为幂等、可靠交付或物理完成验收。

**后续接缝验收(尚未实施,不计入上述首期已通过结论)**:公开校验接缝对非法 method、path、query、headers、非 bytes 或超限 body 的拒绝与 `send` 一致;精确正文上限及合法请求通过,校验过程传输调用数为零。同步验证公开 Protocol、生产实现与测试替身可替换,并回归现有 `send` 行为不变。

### 4.3 风险

- 首期仅有连接池资源限制,不保证出口请求速率或跨进程额度。HTTP 完整响应不证明协议接纳或业务完成,上层不得因此绕过原有身份与完成校验。
- 无熔断与出口限流:下游故障时,调用方并发使用会直接传导到本服务;可靠交互层上线发送器前应先补出口限流。

**兼容**:纯新增非业务客户端,不改动现有路由、表、权限及 WMS/Transport 行为。
