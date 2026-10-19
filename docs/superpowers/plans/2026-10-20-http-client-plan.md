# SPEC-019 H 阶段实施计划:纯异步 HttpClient

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. H1/H2/H4 基线已实施、验证并复审通过,已形成本地提交 f4e9427、9beb783;后续评审修复单独记录,不自动暂存、提交或推送。

**Goal:** 交付可通过构造参数注入的单次异步 HTTP 客户端,只覆盖 H1/H2/H4。

**Architecture:** `app/http_client` 只依赖标准库和 httpx,以 Protocol 隔离调用方与生产实现。连接池跟随客户端事件循环生命周期,网络结果与参数异常分开表达;没有业务消费者时不装配实例。

**Tech Stack:** Python ≥3.13、httpx ≥0.28、pytest、pytest-asyncio、标准库 asyncio 本地 HTTP fixture。

**Spec:** [SPEC-019 v4](../specs/2026-10-20-interop-framework-design.md),仅 §2.1、§10.1、§12 H1/H2/H4、§14.1。

## Global Constraints

- 正文默认上限请求/响应均 256KiB;总超时 10000ms;connect/read/write/pool 各 5s。
- max_connections=100、max_keepalive_connections=20、keepalive_expiry=5s。
- trust_env=False、follow_redirects=False、http2=False、retries=0;TLS 校验保持启用,可显式注入 SSLContext。
- 不实现 H3、R、业务接入、应用装配、路由、表、权限、Celery、重试或信封处理。
- 已从 develop 创建 feature/spec-019-interop-framework 分支;保护已有暂存文件,不自动暂存、提交或推送。

## Review Focus

- 伪造 Content-Length/分块边界不能绕过累计上限,精确等于上限必须成功(T4)。
- 慢速分块每块未超时但整体超过期限时必须结束,并释放池资源(T5)。
- 环境代理、URL authority 与受管 header 不得改变目标或触发第二次发送(T2/T3/T5)。
- 父 conftest 已导入 app.core 不能掩盖包隔离问题;使用全新子进程(T6)。
- 响应超限、编码拒绝和取消后不能耗尽连接池或泄露正文/凭据(T4/T5/T6)。

---

对应设计:`docs/superpowers/specs/2026-10-20-interop-framework-design.md`(契约以 SPEC 为准,实现措辞已随复审修正同步)。**范围仅限 H 阶段**:`app/http_client/` 与其测试、依赖变更。H3(出口并发/速率限流)、R 阶段(可靠交互层)、业务域接入、应用装配(lifespan / Depends)、Celery 任务均**不纳入**。本计划记录实施步骤与验证结果,**H1/H2/H4 基线已实现、验证并复审通过,已本地提交、未推送;后续修复见 §8**。

## 0. 前置复审结论

对 SPEC v4 的完整复审已通过:上轮 P2(R 阶段调度载体措辞、REQ-HTTP-12 的可验收性)与 P3(H4 措辞、Channel 判据重复、私有 CA、假时钟边界)均已在正文闭环;H 阶段范围(§1.2/1.3)、需求(REQ-HTTP-01~06、09~11 生效,07 后移,08/12 为约束)、步骤(§12 H1/H2/H4)与验收(§14.1)互相一致。

本次复审逐段覆盖全文,§3–9 仅作为后续方案核对,不进入 H 任务。未发现阻塞 H 阶段的问题,不据此批准 R 开工。实现时对总超时保守分类:调用底层 send 前已耗尽期限为 NOT_SENT;一旦进入底层 send,若没有明确 pool/connect 异常证据,总超时归 DELIVERY_UNKNOWN。不引入 httpcore trace、内部事件名或跨层状态探针。

## 1. 交付物与边界

| 交付 | 说明 |
|---|---|
| `app/http_client/types.py` | `HttpRequest`、`HttpResult`、`DeliveryState`、`FailureKind`、`HttpClientConfig`、`ClientClosedError`、`HttpClient` Protocol |
| `app/http_client/client.py` | 基于 `httpx.AsyncClient` 的生产实现 |
| `app/http_client/__init__.py` | 公开导出以上名称 |
| `tests/http_client/` | 合同测试、故障测试、导入隔离测试(含 `conftest.py`) |
| `tests/architecture/test_layering.py` | 增加 `http_client` 的禁止导入规则 |
| `pyproject.toml`、`uv.lock` | `httpx` 由 dev 组移入正式依赖 |

> `types.py` 将类型定义与实现分开,避免 `__init__.py` 承载定义;SPEC §10.1 已同步实际目录。不涉及任何行为或依赖变化。

不做:路由、表、迁移、权限、Celery 任务、lifespan 装配、限流器、重试、信封/JSON 处理、业务 DTO、对现有任何模块的修改(除上表两处测试/依赖文件)。

## 2. 数据流与测试覆盖图

```text
调用方
  HttpClient.send(HttpRequest)
    |
    +-> 请求校验(发网前,抛 ValueError / TypeError)
    |     method / path / query / headers / body 合法性              [T2]
    |     禁止调用方设置 Host、Content-Length、Transfer-Encoding、Accept-Encoding   [T2]
    |     body 超限                                                  [T2]
    |     合成 URL 后 origin 与 base_url 一致                        [T2]
    |
    +-> 客户端已关闭 -> ClientClosedError,不发网                     [T5]
    |
    +-> 单次发送(streaming,至多一次网络请求)
    |     3xx / 4xx / 5xx 原样返回,不重试、不重定向                  [T3]
    |     响应头先检查 Content-Encoding(缺省或 identity 才继续)      [T4]
    |     原始 response.stream 有界读取;累计超限即停并关闭响应                [T4]
    |     阶段超时 + 总期限                                          [T5]
    |
    +-> 结果表达
          NOT_SENT / DELIVERY_UNKNOWN / HTTP_RESPONSE_COMPLETE
          + failure_kind + 可取得的 status / headers / 有界 body      [T3]
          调用方取消 -> 清理资源,传播 CancelledError                 [T5]

aclose():幂等,释放连接池                                            [T5]
日志:仅方法、destination_key、耗时、状态、错误类别                   [T6]
包导入:不依赖 app.core / 数据库 / Redis / Celery / FastAPI           [T6]
```

## 3. 关键决策(计划层,不改变 SPEC)

| # | 决策 | 理由 |
|---|---|---|
| D1 | `base_url` 允许可选路径前缀(根地址按 httpx 的 `/` 规范化,配置不额外要求调用者书写尾部 `/`);path 中拒绝 `?`,查询统一由 query 提供;最终 URL 由 httpx 合并,**之后**校验 scheme / host / 有效端口与 `base_url` 完全一致 | SPEC 只要求 path 不得切换 origin;前缀由 httpx 原生支持,无需自写拼接 |
| D2 | `method` 仅接受大写标准 token;`query` 与 `headers` 用 `tuple[tuple[str, str], ...]` | 保持顺序、保留重复项,调用方不被迫选择映射类型 |
| D3 | `FailureKind` 取值(计划自定,SPEC 只要求"明确的 failure_kind"):`POOL_TIMEOUT` `CONNECT_TIMEOUT` `CONNECT_ERROR` `WRITE_TIMEOUT` `WRITE_ERROR` `READ_TIMEOUT` `READ_ERROR` `REMOTE_PROTOCOL_ERROR` `TOTAL_TIMEOUT` `RESPONSE_TOO_LARGE` `CONTENT_ENCODING_UNSUPPORTED` `TRANSPORT_ERROR` | 覆盖 SPEC 列出的全部情形;`TRANSPORT_ERROR` 兜底其余 httpx 传输异常,按保守的 `DELIVERY_UNKNOWN` |
| D4 | 总期限耗尽:进入底层 send 前能证明未发送时为 NOT_SENT;进入后总超时保守归 DELIVERY_UNKNOWN。明确的 PoolTimeout/ConnectTimeout/ConnectError 仍按 NOT_SENT | 不依赖 httpcore 内部 trace 或“未出现事件”推断未发送,也不因注入 transport 改变公开语义 |
| D5 | 总期限用 `asyncio.timeout`(事件循环计时)强制;注入的单调时钟只用于计算单次请求传给 httpx 的四类阶段超时(取各阶段值与发送前剩余期限的较小者)和耗时统计 | 与 SPEC"假时钟只验证截止时间计算,不证明实际取消"一致 |
| D6 | 调用方传入的 `Host`、`Content-Length`、`Transfer-Encoding`、`Accept-Encoding` 一律拒绝(ValueError);`Accept-Encoding: identity` 由客户端固定添加 | SPEC REQ-HTTP-02/06:不接受调用者覆盖 framing 与编码约束 |
| D7 | 日志用 `logging.getLogger("app.http_client")`;成功完整响应用 DEBUG,`NOT_SENT` / `DELIVERY_UNKNOWN` 用 WARNING;字段仅方法、`destination_key`、耗时 ms、HTTP 状态、`delivery_state`、`failure_kind` | REQ-HTTP-11:不通过 `app.core` 取 logger,不记录正文、凭据、查询串、原始 URL |
| D8 | `destination_key` 限 `^[a-z0-9][a-z0-9_.-]{0,63}$` | 非敏感诊断标识,防止把 URL/密钥误填进去 |

## 4. 测试矩阵

测试名与注释按行为命名,不使用 SPEC 编号(项目约定);断言可观察结果,不测私有函数。

| 文件 | 必须验证 |
|---|---|
| `tests/http_client/conftest.py` | 假单调时钟;本地 asyncio 原始 TCP 服务器 fixture(可脚本化:延迟响应、分块慢速、`Content-Length` 声明大于/小于/缺失实际、中途断开、接受后不响应、统计接受的连接数与收到的请求);共享断言工具 |
| `tests/http_client/test_request_validation.py` | method 非法/小写;path 绝对 URL、`//host`、含 userinfo、反斜杠/转义 authority 等可疑形式及最终 origin 校验;普通 `..` 不作为改变 origin 的证据;query/headers 含 CR/LF、非 str、空名;禁止的 4 个 header(大小写不敏感)全部拒绝;body 非 bytes;body 恰等于上限通过、超 1 字节拒绝且**不发网**(传输未被调用);配置:各超时/上限为非正、`max_keepalive_connections > max_connections`、`base_url` 非 http(s) 或含 userinfo/query/fragment、`destination_key` 非法均启动即拒绝 |
| `tests/http_client/test_result_mapping.py` | 3xx/4xx/5xx(含 429/503、非 JSON 正文)逐一原样返回为 `HTTP_RESPONSE_COMPLETE`,传输被调用恰好 1 次(不重试/不重定向);`Retry-After` 等头原样保留,重复头不合并;各 httpx 异常(`PoolTimeout`/`ConnectTimeout`/`ConnectError`/`WriteTimeout`/`WriteError`/`ReadTimeout`/`ReadError`/`RemoteProtocolError`/其他传输异常)映射到 D3 表的状态与 `failure_kind`;请求 body 按原字节到达传输;固定 `Accept-Encoding: identity` 已发出 |
| `tests/http_client/test_response_body.py` | 响应恰等于上限通过、超 1 字节立即停止并返回 `RESPONSE_TOO_LARGE`(保留 status/headers,body 不冒充完整);`Content-Length` 缺省/偏大或 chunked 仍受累计上限约束;偏小按 HTTP framing 边界处理,不声称能观察到框架未交付的尾部字节;**累计正文超过上限时停止继续读取并清理响应**(可跟踪流断言未消费后续块,本地网络测试验证池可继续使用);`Content-Encoding: gzip`/`br` 被拒绝为 `CONTENT_ENCODING_UNSUPPORTED` 且不读取正文;缺省与 `identity` 通过;正文保持原字节(含非 UTF-8) |
| `tests/http_client/test_network_behavior.py` | 本地真实 TCP:慢速分块各块都在阶段超时内但总时长超过总期限 → `TOTAL_TIMEOUT`;阶段读超时 → `READ_TIMEOUT`;`max_connections=1` 且已被占用时 → `POOL_TIMEOUT`(`NOT_SENT`);连接被拒绝 → `CONNECT_ERROR`(`NOT_SENT`);接受后不响应 → `READ_TIMEOUT`(`DELIVERY_UNKNOWN`);服务器中途断开 → 未知类;连续两次请求复用同一连接(服务器统计只接受 1 个连接);**调用方取消**:CancelledError 被传播、连接被释放(随后同一客户端仍可发送);`aclose()` 调两次不报错;关闭后 `send` 抛 `ClientClosedError` 且服务器未收到新连接;总期限耗尽:调用底层发送前耗尽 → NOT_SENT;已进入发送 → 保守 DELIVERY_UNKNOWN;fake 时钟只验证计算,不验证真实取消 |
| `tests/http_client/test_logging.py` | 发送含敏感 query、header、正文的请求并走成功/失败路径,`caplog` 中**不出现**正文、查询串、header 值、完整 URL;出现 `destination_key` 与状态类字段 |
| `tests/http_client/test_import_isolation.py` | 子进程、干净环境(不设 `DATABASE_URL`/`JWT_SECRET_KEY`/`REDIS_URL`)中 `import app.http_client` 成功,且 `sys.modules` 不含 `app.core`、`sqlalchemy`、`redis`、`celery`、`fastapi`、`app.models`、`app.services`、`app.controllers`、`app.interop` |
| `tests/architecture/test_layering.py` | `FORBIDDEN` 增加 `"http_client"`:`app.core`、`app.interop`、`app.integrations`、`app.services`、`app.controllers`、`app.models`、`app.schemas`、`app.tasks`、`sqlalchemy`、`redis`、`celery`、`fastapi`;并补一个检测违规的小用例(沿用现有 `test_checker_detects_violation` 写法) |

TLS:本期不新增专用握手端到端测试(SPEC §14.1 无此项,且避免为证书新增依赖)。只验证:传入 `tls_context` 时被用于传输、未传入时 HTTP 不加载 CA、HTTPS 仅加载实际存在的默认 CA 路径且不读取环境 CA 变量、无 `verify=False` 路径。

## 5. 失败模式

| 路径 | 现实失败 | 处理与证明 | 调用方表现 |
|---|---|---|---|
| 请求校验 | 非法 method/path/header、body 超限 | 发网前 `ValueError`/`TypeError`;传输未被调用 | 编程错误,立即暴露 |
| 连接池等待 | 池耗尽 | `POOL_TIMEOUT` → `NOT_SENT` | 可由上层安全重试 |
| 建立连接/TLS/DNS | 超时、拒绝、证书错误 | `CONNECT_*` → `NOT_SENT` | 同上 |
| 写请求 | 写超时/中断 | `WRITE_*` → `DELIVERY_UNKNOWN` | 上层按其幂等合同处理 |
| 等待/读响应 | 读超时、对端关闭、陈旧长连接被关闭 | `READ_*`/`REMOTE_PROTOCOL_ERROR` → `DELIVERY_UNKNOWN`;**不自动重发** | 同上;`keepalive_expiry` 在对端空闲超时已知时设得更短,未知时沿用默认 |
| 响应过大 | 对端发送超限正文 | 累计超过上限即停止继续读取并关闭响应 → `RESPONSE_TOO_LARGE`(`DELIVERY_UNKNOWN`) | 带 status/headers,正文不可当作完整 |
| 响应编码 | 对端返回 gzip/br | 读正文前拒绝 → `CONTENT_ENCODING_UNSUPPORTED` | 同上 |
| 总期限 | 整体超时 | `asyncio.timeout` → `TOTAL_TIMEOUT`,分类见 D4 | 视证据为 `NOT_SENT` 或 `DELIVERY_UNKNOWN` |
| 取消 | 调用方取消 | 关闭响应、归还连接、传播 `CancelledError` | 不被吞掉 |
| 关闭 | 关闭后继续使用 | `ClientClosedError`,不发网 | 明确失败 |

## 6. 任务与验收

### 公开接口(任务间合同)

- `HttpRequest(method: str, path: str, query: tuple[tuple[str, str], ...] = (), headers: tuple[tuple[str, str], ...] = (), body: bytes = b"")`:冻结 dataclass。
- `HttpClientConfig(base_url: str, destination_key: str, connect_timeout_s=5.0, read_timeout_s=5.0, write_timeout_s=5.0, pool_timeout_s=5.0, total_timeout_s=10.0, max_connections=100, max_keepalive_connections=20, keepalive_expiry_s=5.0, max_request_body_bytes=262144, max_response_body_bytes=262144, tls_context: ssl.SSLContext | None = None)`:冻结 dataclass;数值严格拒绝 bool、非有限值及非正值,连接数及字节限制必须为整数。
- `HttpResult(delivery_state: DeliveryState, failure_kind: FailureKind | None, status_code: int | None, headers: tuple[tuple[str, str], ...], body: bytes | None)`:冻结 dataclass;完整空响应为 b"",失败不返回伪完整正文(body=None);已收到的头/状态保留。
- `DeliveryState`:NOT_SENT / DELIVERY_UNKNOWN / HTTP_RESPONSE_COMPLETE;`FailureKind` 见 D3。
- `HttpClient` Protocol:`async send(request: HttpRequest) -> HttpResult`,`async aclose() -> None`。
- `AsyncHttpClient(config: HttpClientConfig, *, transport: httpx.AsyncBaseTransport | None = None, clock: Callable[[], float] = time.monotonic)` 实现以上接口;拥有并关闭传入 transport,调用方不得共享同一 transport 给多个 owner。`ClientClosedError(RuntimeError)` 对外导出。
- `tls_context` 必须启用证书与 hostname 校验,不接受放松校验的上下文;未注入时,HTTP 使用不加载 CA 的校验型 context;HTTPS 仅用实际存在的 OpenSSL 默认 CA 文件/目录构造标准库默认 context,两者均不存在则保持空信任库和证书校验,不读取环境 CA。生产 transport 上显式设置池/verify/retries/trust_env,避免只在外层 AsyncClient 配置导致自建 transport 忽略参数。

T1 产出配置/请求/结果/Protocol,T2–T5 消费这些公开接口并完成 AsyncHttpClient;T6 只验证公开行为与依赖边界,不改变接口。


按顺序执行;每个任务先写失败测试再实现,结束时运行该任务的聚焦测试。不并行修改同一文件。

| 任务 | SPEC 步骤 | 内容 | 完成条件 |
|---|---|---|---|
| T1 | H1 | 分支与依赖:从 `develop` 建 `feature/spec-019-interop-framework`(暂存的 SPEC 随之带入);`httpx` 移入正式依赖并更新 `uv.lock`;建 `app/http_client/` 骨架与 `types.py`(配置校验在构造时完成) | `uv sync` 通过;配置校验测试(§4 第二行配置部分)通过 |
| T2 | H1 | 请求校验与 URL/origin 校验;`send` 的最小路径(经注入的 MockTransport 把请求交给传输) | `test_request_validation.py` 全绿;非法输入传输调用数为 0 |
| T3 | H1 | 结果表达与异常映射(D3 表);3xx/4xx/5xx 原样返回;单次发送 | `test_result_mapping.py` 全绿;传输调用数恒为 1 |
| T4 | H2 | 流式有界读取、`Content-Encoding` 检查、原始 `response.stream`;`Content-Length` 不被信任 | `test_response_body.py` 全绿(验证精确上限成功、跨块超限停止继续读取及资源释放) |
| T5 | H2 | 阶段超时 + 总期限(D5)、连接池参数、取消、`aclose()` 幂等、关闭后抛错、总期限分类(D4) | `test_network_behavior.py` 全绿;所有计时断言使用事件协调 + 硬截止时间,不依赖真实 `sleep` 的精确时长 |
| T6 | H4 | 日志约束(D7)、导入隔离、分层规则 | `test_logging.py`、`test_import_isolation.py`、`test_layering.py` 全绿 |
| T7 | H4 | 全量验证与记录:`uv run pytest`;`uv run ruff check .`;`uv run ruff format --check .`;核对 §14.1 逐条映射;检查 git diff 仅含 `app/http_client/`、`tests/http_client/`、`tests/architecture/test_layering.py`、`pyproject.toml`、`uv.lock` | 全部通过;结果记入 §8 |

### 逐任务执行记录(已完成项已勾选)

每个任务遵循 RED → GREEN,不把所有测试等到最后才写;下列命令均在仓库根运行。

- [x] T1:记录 git 状态并核对 develop 起点,保护已有暂存设计/计划;写配置非法值测试(非正、bool、NaN、池关系),运行 `rtk uv run pytest tests/http_client/test_request_validation.py -q` 确认缺失接口失败;移动 httpx 依赖并最小定义公开类型,更新锁文件后运行同组测试通过。
- [x] T2:按测试矩阵补请求校验用例,断言 `ValueError/TypeError` 且 transport 调用数为 0;运行同文件见 RED,实现 URL/受管头/bytes 校验后重跑见 GREEN。
- [x] T3:写 `test_result_mapping.py`,逐一断言状态码、重复头、原字节和发送次数==1,以及 D3 异常映射;运行 `rtk uv run pytest tests/http_client/test_result_mapping.py -q` 见 RED,实现单次 streaming send 后见 GREEN。用永久测试证明坏代理环境不影响本地请求,不只断言构造参数。
- [x] T4:写 `test_response_body.py`,断言 262144 字节成功、262145 字节 RESPONSE_TOO_LARGE 且 body=None;gzip 在读流前拒绝、非 UTF-8 原样;运行对应文件见 RED,实现原始流累计与 finally 有界关闭后见 GREEN。偏小 Content-Length 的额外线缆字节属于 HTTP framing,不得承诺客户端可越过声明边界读取;用跟踪流验证累计限制,真实畸形 framing 允许被底层拒绝且不得标记完整超限响应。
- [x] T5:在 `tests/http_client/conftest.py` 建本地服务并用事件协调,写网络测试;先运行 `rtk uv run pytest tests/http_client/test_network_behavior.py -q` 见 RED,实现 deadline、池与取消后见 GREEN。覆盖池=1阻塞、阶段读超时、持续慢块总超时、取消后续可调用、两次请求一个连接、双重关闭及关闭后异常;连接/写超时的确定性分类另用 fake,避免依赖公网或操作系统不可控拥塞。
- [x] T6:写日志与独立导入测试并运行见 RED;最小补 logging 和分层规则后运行 `rtk uv run pytest tests/http_client/test_logging.py tests/http_client/test_import_isolation.py tests/architecture/test_layering.py -q` 全绿。独立进程 cwd 用临时空目录、sys.path 显式加入仓库、移除数据库/Redis/JWT 环境并禁用 dotenv 路径影响;根 conftest 已导入模块不算隔离证明。
- [x] T7:运行 `rtk uv run pytest tests/http_client tests/architecture/test_layering.py -q`、`rtk uv run pytest`、`rtk uv run ruff check .`、`rtk uv run ruff format --check .`、`rtk git diff --check`;预先检查 TEST_DATABASE_URL/TEST_REDIS_URL 是否指向独立可丢弃环境,不在本次范围操作公共库。记录通过/跳过/失败及相关限制,核对变更白名单;不自动提交。

### 与 SPEC §14.1 验收的映射

| §14.1 | 由哪些任务/测试证明 |
|---|---|
| 1 需求均有可观察行为测试 | T2–T6 全部测试文件 |
| 2 原字节不变、不重试、至多一次发送 | `test_result_mapping.py`、`test_request_validation.py` |
| 3 超时与上限、伪造 Content-Length | `test_network_behavior.py`、`test_response_body.py` |
| 4 trust_env / 重定向 / 重试 / origin / 原始流读取;假时钟与真实取消的区分 | `test_result_mapping.py` + `test_response_body.py` + `test_network_behavior.py` |
| 5 资源释放、连接池复用、aclose 幂等、本地真实 HTTP | `test_network_behavior.py` |
| 6 分层与独立导入、无表/迁移/路由/权限/Celery/业务 DTO、httpx 移入正式依赖 | `test_layering.py`、`test_import_isolation.py`、T7 的 diff 核对 |
| 7 聚焦测试、ruff 通过、现有行为不变 | T7(全量 pytest) |

## 7. 提交与交付约定

- 分支 `feature/spec-019-interop-framework`(来自 `develop`);只在用户要求时提交,不推送。
- 提交信息沿用历史风格:`feat: ...` / `test: ...` / `chore: ...`,简明中文,可带 `(SPEC-019)`;依赖变更单独一个 `chore:` 提交。
- PR 说明需写清:仅新增非业务 HttpClient、无路由/表/权限变化、`httpx` 移入正式依赖、H3/R/D 未做。
- 不得把本交付称为幂等、可靠投递或物理完成的验收(SPEC §14.1 第 7 条)。

## 8. 计划自检与验证记录

计划自检已完成:首期每项需求均映射到 T1–T7;07/08/12 不落入首期实现;配置/结果命名一致;测试覆盖 Review Focus 五类条件。SPEC 的 H 阶段范围与行为合同保持不变,原始流读取及独立清理的实现措辞已同步。用户已明确授权实施 H1/H2/H4,仍禁止暂存、提交或推送。

| 需求 | 任务 |
|---|---|
| REQ-HTTP-01/02 | T2/T3 |
| REQ-HTTP-03/04 | T1/T5 |
| REQ-HTTP-05/06 | T2/T4 |
| REQ-HTTP-09/10 | T3/T5 |
| REQ-HTTP-11 | T6 |


2026-10-20 执行记录:

- 分支:feature/spec-019-interop-framework,由 develop 创建;原有两份暂存文档保留,未执行 git add/commit/push。
- T1–T4:配置与请求边界测试先出现缺失模块/接口失败,随后实现通过;冻结类型、单次收发、异常映射、原文大小限制均有永久测试。
- T5:本地真实 TCP 验证超时、池等待、慢速分块、复用、畸形响应、取消释放;修正测试服务器关闭顺序(Python 3.13 wait_closed 会等待仍连接的客户端),未改变产品行为来迁就测试。
- T6:分层违规测试先失败(旧规则未检查新目录),补规则后通过;隔离进程无需应用配置即可导入。日志测试先复现 httpx URL 泄露;独立复核再复现 httpcore DEBUG 响应 Cookie 泄露,新增真实网络永久回归,修复后通过。并发无关调用日志不受影响。
- Ruling:使用 AsyncClient 构造请求和管理连接池生命周期,单次发送调用公开 AsyncBaseTransport 接口,避免 AsyncClient 的完整 URL 日志;不自行实现网络传输。httpcore 日志仅在本调用 ContextVar 范围过滤,不改变其他调用日志级别。默认 SSLContext 显式使用 OpenSSL 默认 CA 路径,忽略环境 CA 覆盖;调用方可注入校验开启的私有 CA context。
- 聚焦验证:`rtk uv run pytest tests/http_client tests/architecture/test_layering.py -q` → 130 passed。
- 全量验证:`rtk uv run pytest -q` → 464 passed,30 skipped;TEST_DATABASE_URL/TEST_REDIS_URL 均未配置,未连接公共存储。
- `rtk uv run ruff check .` → 通过;`rtk uv run ruff format --check app tests` → 100 files already formatted;git diff --check → 通过。
- 当次全库 `ruff format --check .` 存在原有暂存 SPEC Markdown 代码示例格式差异(1 file would be reformatted);为保护用户既有文档现场未改该文件。该限制不是客户端 Python 代码检查失败。
- 一次最终只读独立复核已完成;唯一发现的凭据日志缺陷已通过回归修正。H3/R/业务接入/应用装配均未实现,未声明可靠交付或物理完成验收。

### 2026-10-20 P2 修复验证

- 先新增回归,观察到 7 项失败(清理阻塞/抛错、预载正文边界、关闭取消/并发),另验证关闭失败后重试也先失败,随后修复。
- 响应清理使用 `min(1s, total_timeout_s)` 的有界额外宽限;超时或抛错只记无敏感值的清理告警,不覆盖已判定的发送结果,不吞掉 CancelledError。该宽限独立于网络总期限,不新增配置或后台重试。
- aclose 禁止新请求后共享同一关闭任务,shield 保护资源关闭不随等待者取消;后续/并发调用等待同一任务,关闭失败时允许重新释放 transport。资源关闭重试不是 HTTP 重发。
- 注入传输的预载响应仅在 Content-Encoding 缺省/identity 时读取缓存并校验大小;gzip 等仍拒绝,不把已解码压缩正文当作 raw。流式生产路径使用原始 response.stream,关闭统一在有界清理块执行。
- 新增取消读取且清理抛错的组合回归,验证取消透传及日志不含异常敏感文本。
- 聚焦测试与分层检查:140 passed;全量 pytest:474 passed,30 skipped;ruff check .、ruff format --check app tests、git diff --check 均通过。
- 未实现 H3/R/业务接入/装配,未暂存、提交或推送;先前全库 Markdown 示例格式差异保持不动。

### 2026-10-20 完整响应清理 P2 修复验证

- 先新增正文正常 EOF 后关闭抛错/阻塞的两条回归,两条均先失败:完整响应被改判为 DELIVERY_UNKNOWN。
- 直接迭代公开原始 response.stream,累计大小限制不变;响应关闭统一进入已有的有界 finally 清理块,避免 aiter_raw 自动关闭覆盖完整结果。同步 §2.1 的实现措辞,不改变交付范围。
- 回归验证完整正文、status/headers 与 HTTP_RESPONSE_COMPLETE 均保留,failure_kind 为空,清理告警不携带敏感异常文本。
- 聚焦测试 142 passed;全量测试 476 passed、30 skipped;ruff check .、ruff format --check app tests、git diff --check 均通过。
- 仍仅 H1/H2/H4,未加入 H3/R/业务接入或应用装配,未暂存、提交或推送。

### 2026-10-20 文档状态与格式清理

- v4 与计划统一为 H1/H2/H4 已实现、验证并复审通过;分支已创建,H3/R/业务接入及应用装配仍未实施。历史评审与各轮验证结果保留追溯。
- 仅调整文档状态说明及 SPEC Python 示例格式,未改变代码、行为合同或 H 阶段范围。
- 全库 ruff format --check . 通过(119 files already formatted);此前记录的 SPEC 示例格式差异已消除。git diff --check 通过。
- 本轮仅改这两份文档,未暂存、提交或推送。

### 2026-10-20 本地提交后 P2 修复

- 基线已提交为 f4e9427(运行时依赖)、9beb783(H1/H2/H4 实现、测试和文档),均未推送;此前各轮未提交描述为当时状态。未改写或拆分既有提交。
- 先新增 4 条回归并观察失败:含内联查询的 path(有/无 query)、缺失 CA 路径的明文 HTTP 请求、缺失 CA 路径的 HTTPS 校验上下文。
- path 含字面 ? 时发网前拒绝,query 是唯一查询输入;日志回归改为通过 query 传值,继续验证敏感查询不泄露。
- HTTPX 构造 transport 时始终需要 TLS 上下文,故 HTTP 使用不加载 CA 的校验型上下文;HTTPS 才加载存在的 OpenSSL 默认 CA 路径,缺失时保持空信任库与证书校验。显式 tls_context 不变,不读取环境 CA,不提供 verify=False。
- 聚焦测试及分层检查 146 passed;全量 pytest 480 passed、30 skipped;ruff check .、全库 ruff format --check .、uv lock --check、git diff --check 均通过。
- 本轮修复未暂存、提交或推送,仅 H1/H2/H4;其余 P3 不扩展处理。
