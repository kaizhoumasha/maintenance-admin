# maintenance-admin

维保协同后台管理系统（RBAC）。

## 技术栈

- FastAPI + SQLAlchemy 2.0 async（Postgres，Alembic 迁移）
- Redis（Celery broker / result backend）
- Celery（异步任务）

## 快速开始

### 1. 启动依赖

```bash
docker-compose up -d
docker-compose ps   # 确认 postgres / redis 都是 healthy
```

### 2. 安装与运行

```bash
uv sync
cp .env.example .env
uv run alembic upgrade head     # 应用 migrations/versions 下已有的迁移
# 改了 models 之后才需要生成新迁移:
# uv run alembic revision --autogenerate -m "说明"   # 生成后先检查再 upgrade
uv run uvicorn app.main:app --reload
uv run celery -A app.tasks.celery_app worker -l info
uv run celery -A app.tasks.celery_app beat -l info    # 定时任务(审计归档,每天 03:00 Asia/Shanghai);单独进程,只启动一个
uv run pytest        # 默认使用 SQLite/FakeRedis；未显式配置的集成测试会跳过
scripts/lint.sh      # ruff check --fix + format
```

PostgreSQL 与 Redis 集成测试需显式设置 `TEST_DATABASE_URL`、`TEST_REDIS_URL`，均须指向独立测试库/实例，不能复用业务数据。PostgreSQL 测试会执行迁移；显式配置后连接失败会报错，不会静默跳过。

```bash
TEST_DATABASE_URL=postgresql+asyncpg://admin:admin123@localhost:25432/maintenance_test \
TEST_REDIS_URL=redis://localhost:26379/15 uv run pytest
```

运行前创建独立的 `maintenance_test` 数据库，并确保 Redis 的 15 号库专用于测试。Docker Compose 的依赖端口仅绑定 `127.0.0.1`。

### 2.2 容器运行

默认 `docker-compose up -d` 只起依赖。连应用一起起（迁移 → api / worker / beat，beat 只能一个实例）：

```bash
JWT_SECRET_KEY=$(openssl rand -hex 32) docker compose --profile app up -d --build
```

容器里 `ENVIRONMENT` 默认 `production`（隐藏 API 文档，要求 `JWT_SECRET_KEY` 至少 32 字符，否则启动失败）；compose 会读项目根的 `.env`，其中 `ENVIRONMENT=local` 会让容器也按 local 运行。健康检查：`/health`（存活）、`/ready`（数据库和 Redis 都可用才 200，否则 503）。CI 见 `.github/workflows/ci.yml`（ruff + pytest，含 PostgreSQL/Redis 集成测试）。

### 2.3 配置项

| 变量 | 说明 |
|---|---|
| `APP_NAME` | 项目名：OpenAPI 标题、Celery 应用名、日志器名 |
| `ENVIRONMENT` | `local` 开启 API 文档；非 local 要求 JWT 密钥 ≥ 32 字符 |
| `DATABASE_URL` / `REDIS_URL` | 必填 |
| `JWT_SECRET_KEY` | 必填；`openssl rand -hex 32` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` / `REFRESH_TOKEN_EXPIRE_DAYS` | 默认 30 分钟 / 7 天 |
| `CORS_ORIGINS` | 前端源，逗号分隔；同域部署（nginx 反代）留空，留空则不启用 CORS |
| `AUDIT_RETENTION_DAYS` | 审计日志保留天数，默认 90，超过的由每日任务归档 |

### 2.5 初始化管理员(RBAC)

用户接口需要权限,首个管理员无法通过 API 创建,用脚本引导(可重复执行;admin 角色会被授予表里的全部权限):

```bash
uv run python -m scripts.seed_admin <username> <email>   # 密码走 ADMIN_PASSWORD 环境变量或交互输入
```

权限存在 `permission` 表里(`GET/POST /permissions` 管理);权限码统一三段式 `<module>:<resource>:<action>`(如 `admin:user:create`,小写字母/数字/下划线),且一个权限码只能用于一个路由。路由加 `dependencies=[Depends(require_permission("admin:user:read"))]` 即可鉴权,无权限返回 403 `FORBIDDEN`。格式错误或重复使用会在导入时抛 `ValueError`;但码没写进 seed 或库里时只会让所有人 403,`tests/core/test_permission_code.py` 会检查路由用到的码与 `INITIAL_PERMISSIONS` 完全一致。新增权限码后重跑一次 `scripts.seed_admin` 即可挂给 admin 角色。
模型关系:User ↔ user_role ↔ Role ↔ role_permission ↔ Permission。

用户管理（改资料、启停、改密码、重置密码）：停用用户和改/重置密码都会撤销该用户全部 refresh 会话。**保留管理员**：停用用户、改用户角色、改角色权限这三种操作，如果会让系统里没有任何活跃用户拥有 `admin:role:set_permissions`，返回 409 并回滚。角色仍绑定用户时不能删除（409）。

### 2.6 菜单（前端动态路由）

`menu` 表的字段对应 vue-router 4 的 `RouteRecordRaw`：`GET /api/v1/users/me/menus` 返回当前用户可见的路由树（`name/path/component/redirect/meta{title,icon,hidden,keepAlive,permission}/children`，空值不输出；`component` 是字符串，由前端解析为真实组件；`Layout` 表示目录）。

- 菜单与权限的关系只有一个字段：`menu.permission_code`。用户拥有该权限码才看得到这个菜单；为空表示所有登录用户可见。没有"角色 ↔ 菜单"授权表，授权界面只有"角色 ↔ 权限"一个。
- 菜单可见**不等于**有权访问，接口鉴权仍由路由上的 `require_permission` 负责。
- `hidden` 菜单（详情页等）有权限时照常返回，前端据 `meta.hidden` 决定是否进侧边栏；目录下没有可见子项时整体不返回。
- 顶级菜单 `path` 以 `/` 开头，子级是相对路径；`name` 全局唯一；管理接口 `/api/v1/menus`（`admin:menu:*`）。
- 初始菜单在 `scripts/seed_admin.py` 的 `INITIAL_MENUS`，按 `name` 幂等写入、不覆盖已有；升级后重跑一次 seed 即可补上新菜单和新权限码。

### 3. 目录结构（Controller / Service / Model 三层）

```bash
maintenance-admin/
  app/
    main.py              # 创建 app、include_router
    controllers/         # C：路由，只做参数接收与返回，不写业务
      users.py
    services/            # 业务逻辑，持有事务与数据库查询
      user_service.py
    models/              # M：SQLAlchemy ORM 表
      base.py  user.py
    schemas/             # V：请求/响应模型（Pydantic）
      user.py
    core/                # config / database / security(密码哈希) / errors / handlers / logger / trace 上下文
    middlewares/         # trace_id 中间件（X-Trace-Id + 访问日志）
    tasks/               # Celery，可单独部署 worker
  migrations/            # Alembic（文件名 YYYY-MM-DD_slug）
  tests/                 # 镜像 app/ 结构
  scripts/lint.sh
```

调用方向约定为 controllers → services → models，不要反向依赖（`tests/architecture/test_layering.py` 强制：models/schemas/core 不能导入 services 或 controllers，services 不能导入 controllers）。
新增资源（以 `xxx` 为例）：`controllers/xxx.py`、`services/xxx_service.py`、`models/xxx.py`、`schemas/xxx.py`，然后在 `main.py` 注册 router、在 `models/__init__.py` 导出模型；有异步任务再加 `tasks/`。

### 4. 统一错误处理（沿用 maintenance-agent）

- 业务错误一律 `raise AppError(ErrorCode.X, "中文消息", detail)`，路由里不手写错误响应。
- 新错误码同时加到 `core/errors.py` 的 `ErrorCode` 和 `HTTP_STATUS`（状态码需唯一），并补 `tests/core/test_errors.py`。
- 响应体统一为 `{trace_id, code, message, detail}`；参数校验、404/405 和未捕获异常都由 `core/handlers.py` 转成同一格式，未知异常详情只进日志。
- 数据库异常（`SQLAlchemyError`）也由 handler 统一处理：约束冲突 → 409 `CONFLICT`，连接/超时 → 503 `DATABASE_UNAVAILABLE`，其余 → 500。响应只含 `trace_id` 和异常类型名；用 `trace_id` 到日志里查 SQLSTATE、约束名、SQL 语句和堆栈（不记 SQL 参数）。service 里不要 `try/except SQLAlchemyError` 吞掉异常。

### 限流(SPEC-015)

Redis 滑动窗口(ZSET + Lua 脚本,一次往返、原子),key 为 `ratelimit:{key}:{窗口秒数}`(如 `ratelimit:ip:1.2.3.4:/api/v1/users:60`、`ratelimit:login:1.2.3.4:60`)。

- 全局默认:100 次/分钟/IP/路由(`main.py` 的 `RateLimit(100)`)。
- 路由覆盖:在路由装饰器加 `dependencies=[Depends(RateLimit(10))]`,同名的全局默认自动跳过。
- 登录:`login:{ip}` 5 次/分钟(`RateLimit(5, name="login", per_route=False)`),与全局默认叠加。
- 超限返回 429 `TOO_MANY_REQUESTS`(统一错误结构),`Retry-After` 为最早一次请求滑出窗口的剩余秒数;被拒请求不计入窗口。
- 只作用于已匹配的路由,404/405 不计数。
- Redis 连接/超时故障时 **fail-open**(放行并记 warning 日志);脚本报错不放行,会变成 500:限流是保护不是门禁。
- IP 取对端地址;部署在反向代理后需改为解析可信的 `X-Forwarded-For`,否则所有用户共用一个配额。

### 从模板新建项目

本仓库设为 GitHub Template Repository，新项目用 "Use this template" 生成，然后：

1. `scripts/rename_project.sh <项目名> <数据库名>`（例：`demo-admin demo`），替换项目名、数据库名并刷新 `uv.lock`；之后可删除该脚本。
2. 手改 `pyproject.toml` 的 `description` 和本文件的标题/简介；`AGENTS.md` 第一段按新项目调整；本文件「统一错误处理」一节标题里的 `maintenance-agent` 是出处说明，按需删除。
3. `cp .env.example .env`，换掉 `JWT_SECRET_KEY`（`openssl rand -hex 32`）。
4. `docker-compose up -d && uv sync && uv run alembic upgrade head`，再 `uv run python -m scripts.seed_admin <用户名> <邮箱>`。
5. 按需删除示例：`app/controllers/maintenance.py`（手动触发审计归档，对应权限码 `admin:maintenance:archive_audit` 也要从 `INITIAL_PERMISSIONS` 和 `main.py` 里去掉）；`INITIAL_MENUS` 里按实际页面增删。
6. `uv run pytest` 全绿后再开始写业务。

权限码前缀 `admin:` 只是约定（`<module>:<resource>:<action>`），业务模块用自己的 module 即可。
