# 通用 ECS Status 查询

本能力查询指定 ECS 下全部或一台设备，返回设备描述、运行状态和查询来源。不要求设备已登记，不访问数据库或 Redis，不登记设备、不保存结果、不启动轮询，也不使用命令 outbox。

一个系统可以查询多个 ECS，每个 ECS 可返回多台设备。每次调用明确指定一个地址，不凭设备编码猜测地址或自动切换目标。设备编码跨 ECS 全系统唯一是设备管理的规则：通用层可分别返回两个 ECS 报告的同编码，保留各自来源；后续设备登记须人工处理地址冲突，不能自动合并或覆盖。

## Python 调用

```python
from app.integrations.ecs_status import query

# 示例只指向本地 Mock；先由调用方启动自己的模拟端。
result = await query("http://127.0.0.1:8001")
single = await query("http://127.0.0.1:8001", device_code="ARM03")
```

返回只读 `StatusQueryResult`：`capability_name`、规范化的 `ecs_origin`、`requested_device_code`、本方获取时间 `fetched_at` 和 `devices`。每个条目包含 `device` 和 `state`。设备描述与动态状态严格按已确认 Status 合同校验；未知扩展字段忽略，已知字段不做类型强转，设备身份必须一致且清单内唯一。设备编码保持 wire 原值和大小写，首尾空白拒绝。

输入是 HTTP(S) **IP origin**，可使用实际部署私网地址，但不能包含路径、凭据、query、fragment、域名、未指定或组播地址。路径固定为 `GET /api/v1/device/status`，仅可选 `device_code`。不跟随重定向、不使用环境代理，HTTPS 保持证书验证。

每次最多一次 HTTP 请求，总请求预算 5 秒、响应最多 256 KiB。客户端资源随调用关闭，不跨事件循环复用；底层响应清理及客户端关闭各有有界清理预算，因此不把 5 秒承诺为整个 HTTP 调试接口的严格延迟上限。支持注入 `client_factory(config)` 和时钟用于受控 Mock 验证；工厂必须返回现有 `HttpClient` 端口，调用结束由本能力关闭。

Status独占的具体AsyncHttpClient使用`aclose(timeout_s=1)`，到期终止底层关闭任务，避免留下无人管理的清理任务；其他既有调用仍按默认共享等待者语义关闭。自定义HttpClient工厂的aclose须配合取消，不能自行屏蔽后留下没有资源所有者的后台任务。

## 异常和空清单

HTTP 200 空 `devices` 是已知 ECS 差异的兼容结果，返回空 tuple，不推导设备离线或自动删除。单设备非空响应必须恰好一个匹配条目；全量响应中的重复编码、字段非法、身份冲突、非法 JSON/UTF-8、重复 JSON 键或非有限数，使整次响应无效，不返回部分设备。

查询失败抛 `StatusQueryError`，保留 `capability_name`、`ecs_origin`、`requested_device_code`、`completed_at`、`error_kind`、可用时的 `http_status` 和 `retry_after_at`。不携带对端原始正文或凭据。

| 情况 | 分类 |
| --- | --- |
| 单设备 404 | NOT_FOUND |
| 全量 404、其他非 200 HTTP | HTTP_ERROR |
| 401 / 403 | ACCESS_DENIED |
| 3xx | REDIRECT_REJECTED |
| 429 且 Retry-After 合法 | RATE_LIMITED，返回绝对等待时间 |
| 429 缺失/重复/非法或不可表示的 Retry-After | INVALID_RETRY_AFTER |
| 响应合同/媒体类型/JSON 错误 | INVALID_RESPONSE |
| 请求超时、响应超限等 | 现有 HTTP 客户端的安全 FailureKind，例如 READ_TIMEOUT、TOTAL_TIMEOUT、RESPONSE_TOO_LARGE |

Retry-After 支持非负整数秒及带时区的未来 HTTP 日期，不截短合法长等待；过去日期无有效等待。本模块只解析，不睡眠、重试、建立地址锁或保存等待期限。使用方负责限流、调度、是否落库及状态过期。接口可达不等于设备在线，ECS 的 OFFLINE/UNKNOWN 与本次查询成功可以同时成立；Status 不授权命令、不解封 UNKNOWN/HELD、不代表命令或业务完成。

## 正式 HTTP 查询入口与 Swagger

所有环境均挂载：`GET /api/v1/ecs/status`，标签 `ecs-status`。不保留重复 dev 路由。生产环境可直接调用正式接口；`/docs` 和 `/openapi.json` 是否公开继续由现有环境配置控制，默认只在local展示，与查询接口可用性无关。

1. 通过既有登录接口取得令牌，并在正式请求中携带 Authorization；local也可在 `/docs` 中 Authorize。
2. 当前用户需具备独立权限 `ecs:status:query`；已有数据库需按原种子/权限管理流程显式授予，不能因有查看、命令权限或旧dev查询权限就推导拥有正式查询权限。不会自动转移用户授权或删除已有权限数据。
3. 输入 `ecs_origin`，可选 `device_code`。查询前自行启动本地 Mock，例如在测试中使用受控 loopback ECS。
4. 返回通用 DTO，不保存查询结果、不创建设备、命令或可靠消息。GET 不生成管理写操作审计。

该入口复用现有登录、RBAC 和 API 请求限流（默认每IP、每路由每分钟100次）；这不改变 Python 查询模块的无基础设施依赖。此阶段不引入设备管理的 30 秒后台同步、地址级 5 秒协调、90 秒新鲜度或归档规则。接口不自动重试，返回 429 时使用者必须遵守 Retry-After；不提供绕过/忽略对端等待的参数。

非法输入 400，未登录 401，无权限 403；上游限流 429、查询/合同异常 502（UPSTREAM_ERROR）、超时 504（UPSTREAM_TIMEOUT），均使用现有 ErrorResponse。`detail` 是安全来源/分类的 JSON 字符串，不是对端正文；正常成功的空清单仍返回 200。

正式入口可访问调用方明确提供的合法IP地址，独立查询权限应授予受信任的调用者。每次只读查询，不登记、不落库、不发送命令；新增查询权限不授予 `dev:ecs:send_real`。实际供应商网络与合同仍须分别验证，本轮仅使用本地Mock。

## 统一接入目录与来源追踪

在现有 `GET /api/v1/dev/integrations/registry` 展示一个内置条目：

- `kind=ecs_status`、`name=ecs.device.status@v1`、`entry_type=builtin_capability`。
- `module/source_file/symbol` 由目录侧从真实 `query` 函数提取，`local_only=false`；通用模块不导入注册或应用基础设施。
- `method/path`、`query_schema/response_schema` 描述内置查询；`endpoint/payload_schema` 为 null，保留原 WMS 业务字段含义。
- 原三类注册项标记为 `business_registration`，注册与自动发现机制不变。三类业务集合全空时，内置 Status 条目仍存在。

这不是第四类业务注册表，不新增装饰器，不为每个 ECS、设备或类型注册代码。读取目录不触发 Status 请求，也不读取设备表或启动轮询；目录 HTTP 本身仍保留既有认证依赖及 local-only 可见性。源码来源说明哪个实现提供能力，运行结果中的 origin/编码/时间说明查询来源，两者均不证明供应商或物理现场已验收。

## 本地验证

```bash
rtk uv run pytest tests/integrations/ecs_status tests/http_client tests/architecture tests/controllers/test_dev_registry.py tests/controllers/test_ecs_status.py tests/integrations/test_registration.py tests/integrations/test_registration_discovery.py -q
```

通用查询测试使用注入 HTTP 传输、两个持有的 loopback Mock 和新进程导入隔离；不连接真实 ECS/WMS，查询能力本身不需要迁移，不发送设备命令。当前设备目录、工作线绑定及手动刷新已独立实现，入口和配置表迁移见[设备管理](device-management.md)；没有后台状态同步或周期性设备查询。
