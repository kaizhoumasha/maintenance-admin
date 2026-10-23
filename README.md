# enterprise-backend

面向生产企业的通用企业级后端管理平台模板，适用于 WMS、MES、WES 等业务开发，提供通用、易用的第三方与内部 API 对接基础能力。

平台提供认证与会话、用户/角色/权限、动态菜单、审计、列表查询、异步任务和部署工具；具体业务规则由使用模板的项目承载。API 对接已实现独立异步 HTTP 客户端和协议无关的出站可靠交互首版（事务发件箱、持久化重试、租约与恢复）；不含入站、业务协议或业务/物理完成判定。

最近发布基线为 `v0.2.0`，当前分支包含改名及 HTTP 客户端等后续未发布变更。固定镜像目前仅验收 `linux/arm64`，配套前端及实际站点交付另行验收。发布范围、兼容变化及剩余边界见 [发布说明](CHANGELOG.md)；固定镜像部署、旧制品保存及回退见 [部署说明](docs/deploy.md#6-发布准备与旧制品保存)。

默认 Compose 项目名固定为 `enterprise-backend`，数据卷为 `enterprise-backend_pgdata` 和 `enterprise-backend_redisdata`，不再随仓库目录名变化。数据库名统一为 `enterprise_backend`，独立测试库使用 `enterprise_backend_test`；现有 API 路径及权限码不变。已有部署在完成备份和数据迁移前，须通过 `-p` 或 `COMPOSE_PROJECT_NAME` 指定原项目名，沿用原数据卷和站点配置；仅修改项目名不会迁移数据。旧数据库原地改名及工具连接同步见 [数据库改名](docs/deploy.md#数据库改名)。显式设置了 `APP_NAME` 或 `APP_IMAGE` 的环境继续以配置值为准，按需更新。

开发约定见 [AGENTS.md](AGENTS.md) 和 [CLAUDE.md](CLAUDE.md)；变更记录见 [CHANGELOG.md](CHANGELOG.md)。

## 技术栈

- FastAPI + SQLAlchemy 2.0 async（Postgres，Alembic 迁移）
- Redis（refresh 会话、限流、Celery broker / result backend）
- Celery（异步任务）
- uv（依赖）、Docker / Compose（容器化）、GitHub Actions（CI）

## 快速开始

### 1. 启动依赖

```bash
docker compose up -d
docker compose ps   # 确认 postgres / redis 都是 healthy
```

### 2. 安装与运行

```bash
uv sync
cp .env.example .env
uv run alembic upgrade head     # 在线校验归属并迁入 enterprise_backend schema，再应用迁移
# 改了 models 之后才需要生成新迁移:
# uv run alembic revision --autogenerate -m "说明"   # 生成后先检查再 upgrade
uv run uvicorn app.main:app --reload
uv run celery -A app.tasks.celery_app worker -l info
uv run celery -A app.tasks.celery_app beat -l info    # 定时任务(审计归档,每天 03:00 Asia/Shanghai);单独进程,只启动一个
uv run pytest        # 默认使用 SQLite/FakeRedis；未显式配置的集成测试会跳过
scripts/lint.sh      # ruff check --fix + format
```

PostgreSQL 与 Redis 集成测试需显式设置 `TEST_DATABASE_URL`、`TEST_REDIS_URL`，均须指向独立测试库/实例，不能复用业务数据。PostgreSQL 测试会执行迁移；显式配置后连接失败会报错，不会静默跳过。

项目 PostgreSQL 表和 Alembic 版本表固定放在 `enterprise_backend` schema，API、可靠发送器与 Celery 连接均使用该 schema，不回退到 `public`。已有完整 `a023a20261024` 旧库运行在线 `alembic upgrade head` 时，只原地移动本项目 12 张表及版本表；其他系统表不动。更早的旧库先用旧代码升级到该基线。SQLite 测试将 schema 映射为空。迁移边界、停机步骤与验证见 [Schema 隔离](docs/deploy.md#schema-隔离)。

```bash
TEST_DATABASE_URL=postgresql+asyncpg://admin:admin123@localhost:25432/enterprise_backend_test \
TEST_REDIS_URL=redis://localhost:26379/15 uv run pytest
```

运行前创建独立的 `enterprise_backend_test` 数据库，并确保 Redis 的 15 号库专用于测试。Docker Compose 的依赖端口仅绑定 `127.0.0.1`：Postgres 25432、Redis 26379；本机开发 API 默认 8000，`ENVIRONMENT=local` 时接口文档在 `/docs`。

### 3. 容器运行

默认 `docker compose up -d` 只起依赖。连应用一起起（迁移 → api / worker / beat，beat 只能一个实例）：

```bash
# JWT_SECRET_KEY（openssl rand -hex 32）写进 .env：只在 shell 里临时导出的话，之后的 run / restart 会因密钥为空而启动失败
docker compose --profile app up -d --build
docker compose --profile app ps -a    # migrate 应为 Exited (0)，api 为 healthy
# 全新库没有管理员，先创建一个（无输出即成功；省略 ADMIN_PASSWORD 则交互输入密码）
docker compose --profile app run --rm -e ADMIN_PASSWORD='<密码>' api python -m scripts.seed_admin <用户名> <邮箱>
```

容器里 `ENVIRONMENT` 默认 `production`（隐藏 API 文档，要求 `JWT_SECRET_KEY` 至少 32 字符，否则启动失败）；compose 会读项目根的 `.env`，其中 `ENVIRONMENT=local` 会让容器也按 local 运行。生产部署（nginx 同域反代前端、多实例、备份、升级回退）见 [docs/deploy.md](docs/deploy.md)。健康检查：`/health`（存活）、`/ready`（数据库和 Redis 都可用才 200，否则 503）。CI 见 `.github/workflows/ci.yml`（ruff + pytest，含 PostgreSQL/Redis 集成测试）。

### 4. 配置项

| 变量 | 说明 |
|---|---|
| `APP_NAME` | 项目名：OpenAPI 标题、Celery 应用名、日志器名 |
| `ENVIRONMENT` | `local` 开启 API 文档；非 local 要求 JWT 密钥 ≥ 32 字符 |
| `DATABASE_URL` / `REDIS_URL` | 必填 |
| `JWT_SECRET_KEY` | 必填；`openssl rand -hex 32` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` / `REFRESH_TOKEN_EXPIRE_DAYS` | 默认 30 分钟 / 7 天 |
| `CORS_ORIGINS` | 前端源，逗号分隔；同域部署（nginx 反代）留空，留空则不启用 CORS |
| `AUDIT_RETENTION_DAYS` | 审计日志保留天数，默认 90，超过的由每日任务归档 |
| `POSTGRES_PASSWORD` / `NGINX_PORT` / `FRONTEND_DIST` / `FORWARDED_ALLOW_IPS` | 仅容器部署使用，见 `docs/deploy.md` |

### 5. 初始化管理员(RBAC)

用户接口需要权限,首个管理员无法通过 API 创建,用脚本引导(可重复执行;admin 角色会被授予表里的全部权限):

```bash
uv run python -m scripts.seed_admin <username> <email>   # 密码走 ADMIN_PASSWORD 环境变量或交互输入
```

初始化参数复用用户创建接口的用户名、邮箱和密码校验,非法输入在任何种子写入前拒绝,不会回显密码。重复执行也须提供合法参数;已有用户的邮箱和密码仍保持原值。

权限存在 `permission` 表里(`GET/POST /permissions` 管理);权限码统一三段式 `<module>:<resource>:<action>`(如 `admin:user:create`,小写字母/数字/下划线),且一个权限码只能用于一个路由。路由加 `dependencies=[Depends(require_permission("admin:user:read"))]` 即可鉴权,无权限返回 403 `FORBIDDEN`。格式错误或重复使用会在导入时抛 `ValueError`;但码没写进 seed 或库里时只会让所有人 403,`tests/core/test_permission_code.py` 会检查路由用到的码与 `INITIAL_PERMISSIONS` 完全一致。新增权限码后重跑一次 `scripts.seed_admin` 即可挂给 admin 角色。
模型关系:User ↔ user_role ↔ Role ↔ role_permission ↔ Permission。

用户管理（改资料、启停、改密码、重置密码）：停用用户和改/重置密码都会撤销该用户全部 refresh 会话。**保留管理员**：停用用户、改用户角色、改角色权限这三种操作，如果会让系统里没有任何活跃用户拥有 `admin:role:set_permissions`，返回 409 并回滚。角色仍绑定用户时不能删除（409）。

### 6. 认证

- `POST /api/v1/auth/login`（JSON，每 IP 每分钟 5 次）返回 `access_token`；refresh token 放在 HttpOnly cookie `refresh_token`（路径 `/api/v1/auth`，`SameSite=Strict`，非 local 环境带 `Secure`），前端读不到。因为是 `Strict`，前后端必须同站（同域或开发代理）；跨站部署需改 `samesite` 并补 CSRF 防护。
- `POST /auth/refresh` 凭 cookie 换新的 token 对，旧 refresh 立即作废；`/auth/logout` 撤销当前设备，`/auth/logout-all` 撤销该用户全部设备。
- 用户有 `allow_multi_login`：默认 `False`（单点登录，新登录会顶掉旧会话），admin 为 `True`。
- access token 无状态、不进黑名单，登出后最长 `ACCESS_TOKEN_EXPIRE_MINUTES` 内仍可用；用户被停用则立即失效（每次请求都校验 `is_active`）。

### 7. 菜单（前端动态路由）

`menu` 表的字段对应 vue-router 4 的 `RouteRecordRaw`：`GET /api/v1/users/me/menus` 返回当前用户可见的路由树（`name/path/component/redirect/meta{title,icon,hidden,keepAlive,permission}/children`，空值不输出；`component` 是字符串，由前端解析为真实组件；`Layout` 表示目录）。

- 菜单与权限的关系只有一个字段：`menu.permission_code`。用户拥有该权限码才看得到这个菜单；为空表示所有登录用户可见。没有"角色 ↔ 菜单"授权表，授权界面只有"角色 ↔ 权限"一个。
- 菜单可见**不等于**有权访问，接口鉴权仍由路由上的 `require_permission` 负责。
- `hidden` 菜单（详情页等）有权限时照常返回，前端据 `meta.hidden` 决定是否进侧边栏；目录下没有可见子项时整体不返回。
- 顶级菜单 `path` 以 `/` 开头，子级是相对路径；`name` 全局唯一；管理接口 `/api/v1/menus`（`admin:menu:*`）。
- 初始菜单在 `scripts/seed_admin.py` 的 `INITIAL_MENUS`，按 `name` 幂等写入、不覆盖已有；升级后重跑一次 seed 即可补上新菜单和新权限码。

### 8. 目录结构（Controller / Service / Model 三层）

```bash
enterprise-backend/
  app/
    main.py              # 创建 app、中间件、include_router、lifespan
    controllers/         # C：路由，只做参数接收与返回，不写业务（auth / users / roles / permissions / menus / audit / health）
    services/            # 业务逻辑，持有事务与数据库查询（xxx_service.py）
    models/              # M：SQLAlchemy ORM 表
    schemas/             # V：请求/响应模型（Pydantic）
    core/                # config / database / redis / security / errors / handlers / ratelimit / logger / trace 上下文
    http_client/         # 独立纯 HTTP 客户端：单次 bytes 收发、超时、连接池、大小限制
    reliable/            # 出站事务发件箱、租约、原身份重试、TTL；不含协议实现
    reliable_runtime.py  # lifespan 发送器与专用连接池装配
    integrations/        # 业务接入层（wms_transport）；各包内 dev.py 登记联调场景，仅 local 挂载（见下方「联调控制台」）
    middlewares/         # trace_id（X-Trace-Id + 访问日志）、审计日志
    tasks/               # Celery，可单独部署 worker
  migrations/            # Alembic（文件名 YYYY-MM-DD_slug）
  tests/                 # 镜像 app/ 结构；architecture/ 放分层约束
  scripts/               # lint.sh、seed_admin.py（管理员 / 权限 / 菜单种子）、rename_project.sh
  Dockerfile  docker-compose.yml  .github/workflows/ci.yml
```

调用方向约定为 controllers → services → models，不要反向依赖（`tests/architecture/test_layering.py` 强制：models/schemas/core 不能导入 services 或 controllers，services 不能导入 controllers）。
`app/http_client/` 独立于 `app.core`、业务模块和存储依赖；公开同步 `validate_request(config, request)` 与单次 `send` 复用校验，不创建客户端、不发网。合同见 [SPEC-019A](docs/superpowers/specs/2026-10-20-spec-019a-http-client.md)。`app/reliable/` 通过它在调用者事务内校验新消息，并由 FastAPI lifespan 的发送器投递；不迁移现有任务。可靠层合同见 [SPEC-019B](docs/superpowers/specs/2026-10-20-spec-019b-reliable-interaction.md)，配置、接入示例及离线验证见 [可靠交互实施计划](docs/superpowers/plans/2026-10-21-reliable-interaction-plan.md)。

Transport 出站能力已提供 `app.integrations.wms_transport.TransportService.move_rack`，通过调用方 `AsyncSession` 同事务登记任务及可靠消息，支持 `Rack`、`Zone`、`RackPosition`。调用方保存并重用规范小写 UUIDv7 `client_request_id`；同号同输入返回原句柄，同号变更输入（含 caller 路由信息）抛 `IdempotencyConflict`。方法不提交外层事务；句柄只证明登记义务，不证明 WMS 接纳或物理完成。请求按最终 UTF-8 字节冻结，固定上限 256 KiB；迁移 `a019d20261022` 新增 `transport_task`，表非空时拒绝降级删除。装配及生产门槛见 [Transport 出站设计](docs/superpowers/specs/2026-10-21-transport-outbound-design.md)，没有自动注册真实目标。

货架场景入口为 `RackScenarios(transport_service)`，从同一包导入 `RackRole` 与 `RackDestination`；`enter_rack`、`rotate_rack`、`depart_rack` 按显式角色选择模板，复用原会话和能力层，不提交、不新增生命周期存储。进场/离场目标仅接受 `RackPosition | Zone`，换面可信状态仍由能力层读取。调用方须固定同一生命周期的角色；RETURN 换面 CK04、进场 Zone 等真实启用前确认项见[货架场景设计](docs/superpowers/specs/2026-10-22-rack-transport-integration-design.md)，工程测试通过不代表 WMS 接纳或物理完成。

真实业务接入先跑[第一个可靠投递实验](docs/reliable-transport-lab.md)，再按 [ECS 与 Transport 接入开发指南](docs/superpowers/specs/device-command-integration.md)的任务表开发。实验给出完整业务模型、事务入队、假客户端发送、ACK 分类和终态测试，不需要真实对端。指南区分 ECS 动态选址与 Transport 静态 target，列明文件路径、负责人、验收条件及环境准备；当前的动态路由和完整 Transport 生命周期缺口仍阻塞真实启用。

WES → WMS 的 `decisions` / `facts` 出站端点已建好但没有任何具体 operation：`app.integrations.wms_request` 提供 operation 注册（`@request_operation`，在 `app/integrations/<业务>/request.py` 自动发现）、信封冻结和公共响应分类，`WmsRequests.send` 在调用方事务内入队；两个端点分别配置 `protocol` 为 `wms-facts`、`wms-decisions` 的目标（路径 `/api/v1/wes/facts`、`/api/v1/wes/decisions`）。每个 operation 须先对照 WMS 合同确认再添加；当前业务还读不到 WMS 的响应，需要同步决定的 operation 在确认时再定调用方式。

默认 `RELIABLE_TARGETS={}` 仍以专用数据库池扫描/恢复存量消息，但不创建 HTTP 客户端；只领取本进程已配置、已注册适配器且有客户端的目标，其余未过期消息保留 `PENDING`，不因配置漂移转为终态失败。本地退避 cap 默认 300 秒；`RELIABLE_MAX_RETRY_AFTER_S=3600` 只限制对端等待提示的采纳，可由 JSON `defaults` / target 覆盖，等待不延长 TTL。终态按 `updated_at` 保留 90 天，清理后可靠层不再保证本地幂等；Transport 独立任务映射仍保留，不因可靠消息清理而重新入队。原文明文存储；对端去重验收、安全与出口限流仍是启用真实目标前的门槛，当前没有真实目标注册。
发送尝试完成后唤醒本实例发送器补位；领取批量小于并发预算时继续填充空槽。后台检查在系统正常运行时执行，不需要停机；后台检查间隔默认 1 秒。发送器启动首轮、轮询计时器到点、扫描异常等待该间隔后重试时执行后台检查；发送完成唤醒或连续补位时，只有距上次后台检查已满该间隔才执行，否则仅领取发送。

每次后台检查先扫描 TTL 到期的 `PENDING` 消息，再回收租约到期的 `SENDING` 消息，默认各最多处理 100 行；两项上限独立于发送领取批量，不新增环境变量，发送槽满或没有目标时仍按上述条件执行。TTL 扫描将到期消息标为 `EXPIRED`；租约回收将未过 TTL 的消息转回 `PENDING` 并退避，已过 TTL 的转为 `EXPIRED`。这些处理不代表对端未执行，仍须按稳定身份去重。

**TTL（Time To Live，消息有效时长）**决定一条消息还能在多长时间内发起发送或重试。以新消息入队创建时写入的 `created_at` 起算，固定截止时间为 `expires_at = created_at + ttl`，不是从事务提交或首次发送时起算；重试、退避、对端等待提示及返回已有消息的重复入队都不会延长或重置 TTL。不设置 `ttl` 时不因有效期截止自动过期，但仍可能进入成功或失败终态。

例如，10:00 入队创建、TTL 为 30 分钟，则从 10:30 起不再发起新的发送或重试。到期的 `PENDING` 消息由后台检查标为 `EXPIRED`，实际发送前也会再次检查截止时间。已经发出的在途请求不会因 TTL 到期被强行取消，仍受原 HTTP 超时和租约回写条件约束，合法成功响应可能在到期后将消息转为 `DONE`；租约到期回收时若 TTL 已过，则转为 `EXPIRED`。TTL 到期不代表对端未执行，也不撤销对端动作。

可选的目标级未送达预算（`max_not_received_attempts`）只统计「确认未送达」（请求未发出，或对端明确未接纳）；一旦出现过结果未知（响应丢失、租约中断或回收），该消息不再受预算约束，继续以原身份重提。未出现未知且次数达到预算的消息转为 `STALLED`：保留原身份与字节，不再发送，不会过期，也不会被保留期清理，并输出 `reliable stalled` 告警。只有调用方显式调用 `Outbox.resume` 才能把它放回 `PENDING`（计数清零，未知标记保留；提交后记 `reliable resumed` 日志）。可选 `fixed_retry_interval_s` 把本地重试等待固定为该值。

| 时间限制 | 起算点与作用 |
|---|---|
| TTL | 从消息入队创建的 `created_at` 起算，限制发起新发送或重试的期限 |
| HTTP 超时 | 从一次 HTTP 调用开始计时，限制本次调用的执行和等待时长；超时不一定代表对端未接收或未执行 |
| 租约时长 | 从发送器领取消息时起算，时长为目标 HTTP 超时加租约余量，用于处理权分配与崩溃恢复，不会延长 TTL |
| 终态保留期 | 从进入 `DONE` / `FAILED` / `EXPIRED` 时的 `updated_at` 起算，默认保留 90 天，再由定时任务清理记录；不是发送有效期 |

多实例部署前，所有发送器、入队生产者及清理进程所在主机必须启用 NTP/chrony 等时钟同步并监控偏差和跳变。持久时间目前来自应用 UTC 墙钟，不是数据库统一授时；实例间偏差与调度、回写延迟合计应远小于默认 5 秒租约余量。不能满足此前提时可能提前接管并重复投递；租约围栏只保护回写，不替代对端按稳定身份去重。

新增资源（以 `xxx` 为例）：`controllers/xxx.py`、`services/xxx_service.py`、`models/xxx.py`、`schemas/xxx.py`，然后在 `main.py` 注册 router、在 `models/__init__.py` 导出模型；有异步任务再加 `tasks/`。

#### 联调控制台(仅 `ENVIRONMENT=local`)

Swagger 里 `POST /api/v1/dev/transport/tasks` 是统一出站入口:在 Examples 下拉选业务场景(货架进场/换面/离场、料箱投料/回架/交换),走真实的业务接入层登记出站;`GET /api/v1/dev/transport/tasks/status/{business_key}` 查看投递状态与请求、响应原文。没有真实对端时,把目标指向本应用自身即可用内置 Mock WMS(需单 worker,`uvicorn --reload` 即是),再用 `PUT /api/v1/dev/mock-wms/mode` 切换响应行为:

```bash
RELIABLE_TARGETS='{"targets":{"wms-transport":{"base_url":"http://127.0.0.1:8000","protocol":"wms-transport"}}}'
```

回调侧:`POST /api/v1/dev/transport/{transport_task_id}/callbacks` 选 `succeeded` 或 `position_unknown`,按该任务的冻结事实构造样例回调并走真实接收管线,返回每条的响应与诊断;`GET /api/v1/dev/transport/{transport_task_id}` 看任务状态。

WMS 请求端点(decisions、facts)的联调在 `/api/v1/dev/wms/requests`:`GET /operations` 是已注册 operation 的目录(端点、标题、TTL、Payload Schema、示例);`POST` 在 Examples 下拉选 operation 触发,走真实的 `WmsRequests.send` 入队;`GET /status/{operation_id}` 看投递状态与请求、响应原文。同一 `operation_id` 同 Payload 重复触发返回同一条消息,Payload 不同返回 409。Mock WMS 同时认识 `/api/v1/wes/facts`、`/api/v1/wes/decisions`,模式开关同一个(`PUT /dev/mock-wms/mode`,新增 `busy` 与仅 decisions 生效的 `prepare_accepted`);decisions 的 `DECIDED` 响应 `data` 恒为 `{}`,不模拟真实业务数据。两个端点各需一个目标,同样指向本应用自身:

```bash
RELIABLE_TARGETS='{"targets":{"wms-transport":{"base_url":"http://127.0.0.1:8000","protocol":"wms-transport"},"wms-facts":{"base_url":"http://127.0.0.1:8000","protocol":"wms-facts"},"wms-decisions":{"base_url":"http://127.0.0.1:8000","protocol":"wms-decisions"}}}'
```

演示 operation 在 `app/integrations/wms_request/dev.py`,只在 local 登记;真实 operation 的写法见该文件头注释。

新增业务接入层时,在 `app/integrations/<业务>/dev.py` 里用 `@dev_scenario` 登记场景即可自动出现在下拉里,详见 `docs/superpowers/specs/2026-10-22-transport-dev-console-design.md`。非 local 环境这些路由不存在。**部署时必须显式设置 `ENVIRONMENT`(如 `production`)**:它的默认值是 `local`,漏设会同时挂出该控制台(含无认证的 Mock WMS 接收端)、开放 API 文档并关闭 Cookie 的 Secure。示例里的 Zone、储位、滚筒线编码是占位值,联调前按现场替换。

### 9. 统一错误处理

- 新增路由统一用 `app.core.routing.new_router()` 创建 router；OpenAPI 根据参数和依赖自动补 400/401/403/429，路由只需声明额外的业务错误（如 404/409/503），参数校验统一返回 400，不声明 422。
- 业务错误一律 `raise AppError(ErrorCode.X, "中文消息", detail)`，路由里不手写错误响应。
- 新错误码同时加到 `core/errors.py` 的 `ErrorCode` 和 `HTTP_STATUS`（状态码需唯一），并补 `tests/core/test_errors.py`。
- 响应体统一为 `{trace_id, code, message, detail}`；参数校验、404/405 和未捕获异常都由 `core/handlers.py` 转成同一格式，未知异常详情只进日志。
- 数据库异常（`SQLAlchemyError`）也由 handler 统一处理：约束冲突 → 409 `CONFLICT`，连接/超时 → 503 `DATABASE_UNAVAILABLE`，其余 → 500。响应只含 `trace_id` 和异常类型名；用 `trace_id` 到日志里查 SQLSTATE、约束名、SQL 语句和堆栈（不记 SQL 参数）。service 里不要 `try/except SQLAlchemyError` 吞掉异常。

### 10. 限流(SPEC-015)

Redis 滑动窗口(ZSET + Lua 脚本,一次往返、原子),key 为 `ratelimit:{key}:{窗口秒数}`(如 `ratelimit:ip:1.2.3.4:/api/v1/users:60`、`ratelimit:login:1.2.3.4:60`)。

- 全局默认:100 次/分钟/IP/路由(`main.py` 的 `RateLimit(100)`)。
- 路由覆盖:在路由装饰器加 `dependencies=[Depends(RateLimit(10))]`,同名的全局默认自动跳过。
- 登录:`login:{ip}` 5 次/分钟(`RateLimit(5, name="login", per_route=False)`),与全局默认叠加。
- 超限返回 429 `TOO_MANY_REQUESTS`(统一错误结构),`Retry-After` 为最早一次请求滑出窗口的剩余秒数;被拒请求不计入窗口。
- 只作用于已匹配的路由,404/405 不计数。
- Redis 连接/超时故障时 **fail-open**(放行并记 warning 日志);脚本报错不放行,会变成 500:限流是保护不是门禁。
- IP 取对端地址;部署在反向代理后需改为解析可信的 `X-Forwarded-For`,否则所有用户共用一个配额。

### 11. 从模板新建项目

本仓库设为 GitHub Template Repository，新项目用 "Use this template" 生成，然后：

1. 在初始化数据库前运行 `scripts/rename_project.sh <项目名> <数据库名>`（例：`demo-admin demo`），替换项目名、数据库名及相关配置、文档和测试引用，并刷新 `uv.lock`；之后可删除该脚本。此脚本用于新项目，不迁移已有数据库或数据卷。
2. 手改 `pyproject.toml` 的 `description`、`app/main.py` 的应用描述和本文件的简介；`AGENTS.md` 的项目定位按新项目调整。
3. `cp .env.example .env`，换掉 `JWT_SECRET_KEY`（`openssl rand -hex 32`）。
4. `docker compose up -d && uv sync && uv run alembic upgrade head`，再 `uv run python -m scripts.seed_admin <用户名> <邮箱>`。
5. 按需删除示例：`app/controllers/maintenance.py`（手动触发审计归档，对应权限码 `admin:maintenance:archive_audit` 也要从 `INITIAL_PERMISSIONS` 和 `main.py` 里去掉）；`INITIAL_MENUS` 里按实际页面增删。
6. `uv run pytest` 全绿后再开始写业务。

权限码前缀 `admin:` 只是约定（`<module>:<resource>:<action>`），业务模块用自己的 module 即可。

## 列表筛选与分页（SPEC-018）

完整契约见 [SPEC-018 设计](docs/superpowers/specs/2026-10-17-list-query-design.md)，实施与验收记录见 [实现计划](docs/superpowers/plans/2026-10-17-list-query-plan.md)。

`GET /api/v1/users`、`/api/v1/audit-logs`、`/api/v1/audit-logs/archive` 接受可选的 `filter` JSON 字符串。根为条件组,`couple` 支持 `and`（默认）、`or`、`not`;组不能为空,最多三层,`not` 只能有一个子项。错误字段、算子、值或未知键均返回 400 `INVALID_REQUEST`,不静默忽略条件。

```bash
curl -G "$HOST/api/v1/users" -H "Authorization: Bearer $TOKEN" \
  --data-urlencode 'page=1' --data-urlencode 'page_size=20' \
  --data-urlencode 'filter={"conditions":[{"field":"username","op":"ilike","value":"%kai%"},{"field":"is_active","op":"eq","value":true}]}'

curl -G "$HOST/api/v1/audit-logs" -H "Authorization: Bearer $TOKEN" \
  --data-urlencode 'limit=20' \
  --data-urlencode 'filter={"conditions":[{"field":"user_id","op":"eq","value":1}]}'
# 后续页沿用 filter,增加 --data-urlencode "cursor=$NEXT_CURSOR"；归档列表用 /audit-logs/archive。
```

Postman 使用 GET,在 **Params** 表填 `filter` 和原始 JSON（不手工 URL 编码）,另填对应的分页参数,Authorization 选 Bearer Token。

| 列表 | 筛选字段及算子 |
|---|---|
| 用户 | `username/email`: `eq/in/ilike`;`is_active`: `eq`;`created_at`: `ge/le/between`;`role_id`: `eq/in` |
| 审计、归档 | `user_id`: `eq/in/is_null/not_null`;`action/resource/resource_id`: `eq/in`;`created_at`: `ge/le/between` |

`in` 为 1–100 个非 null 值,`between` 为两个非 null 端点（闭区间）,`eq/ge/le` 不接受 null。ID 必须为 JSON 整数 1–2147483647,布尔必须为 `true/false`;日期必须为带时区的 ISO 日期时间,如 `2026-10-17T13:00:00+08:00`,按 UTC 比较。数字时间戳、无时区、UTC 转换越界均拒绝。`is_null/not_null` 的值省略或为 null;`not` 遵循 SQL 三值逻辑,需要包含 NULL 行时显式加 `or` + `is_null`。

`ilike` 由调用方传完整 pattern（最多 64 字符）,服务端不补 `%`:

| pattern | 含义 | JSON 中的 value |
|---|---|---|
| `foo` | 忽略大小写精确匹配 | `"foo"` |
| `foo%` / `%foo%` | 前缀 / 包含 | `"foo%"` / `"%foo%"` |
| `foo\%` / `foo\_` / `foo\\` | 匹配字面 `%` / `_` / `\` | `"foo\\%"` / `"foo\\_"` / `"foo\\\\"` |

字符串不能含 NUL,pattern 不能以奇数个反斜杠结尾。

**兼容变化**:用户仍返回 `{items,total,page,page_size}`,参数为 `page/page_size/filter`。审计与归档改为 `{items,next_cursor}`,删除 `total/page/page_size`;参数为 `limit`（默认 20,1–100）、`cursor`、`filter`,旧的 `page/page_size` 返回 400。三个列表均拒绝未知 URL 参数（如 `filer`）,调用方须先移除前端专用参数。

审计响应示例:`{"items":[{"id":42,"user_id":1,"action":"admin:user:create","resource":"users","resource_id":7,"detail":null,"ip":null,"created_at":"2026-10-17T12:00:00Z"}],"next_cursor":"eyJpZCI6IDQyfQ"}`。归档条目另有 `archived_at`。`next_cursor=null` 表示末页;按 `id desc` 向后翻页,不能随机跳页。改变筛选条件时丢弃旧游标。翻页期间新记录不会重复进入后续页,记录被归档后会从热表后续页消失;较小 ID 的并发事务晚提交时可能漏过本轮遍历,重新查询可见。

`filter` 上限为 **2048 UTF-8 字节**,URL 编码后最多约 6KB,部署时还须计入路径和其他参数,核对代理的请求行长度限制。筛选值可能含用户名、邮箱等个人信息;应用日志不记录 query string,反向代理和网关也应避免记录完整查询参数。筛选不保证所有组合都走索引,出现慢查询后再评估索引。
