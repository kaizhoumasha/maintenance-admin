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
uv run pytest        # 默认使用 SQLite/FakeRedis；未显式配置的集成测试会跳过
scripts/lint.sh      # ruff check --fix + format
```

PostgreSQL 与 Redis 集成测试需显式设置 `TEST_DATABASE_URL`、`TEST_REDIS_URL`，均须指向独立测试库/实例，不能复用业务数据。PostgreSQL 测试会执行迁移；显式配置后连接失败会报错，不会静默跳过。

```bash
TEST_DATABASE_URL=postgresql+asyncpg://admin:admin123@localhost:25432/maintenance_test \
TEST_REDIS_URL=redis://localhost:26379/15 uv run pytest
```

运行前创建独立的 `maintenance_test` 数据库，并确保 Redis 的 15 号库专用于测试。Docker Compose 的依赖端口仅绑定 `127.0.0.1`。

### 2.5 初始化管理员(RBAC)

用户接口需要权限,首个管理员无法通过 API 创建,用脚本引导(可重复执行;admin 角色会被授予表里的全部权限):

```bash
uv run python -m scripts.seed_admin <username> <email>   # 密码走 ADMIN_PASSWORD 环境变量或交互输入
```

权限存在 `permission` 表里(`GET/POST /permissions` 管理);权限码统一三段式 `<module>:<resource>:<action>`(如 `admin:user:create`,小写字母/数字/下划线),且一个权限码只能用于一个路由。路由加 `dependencies=[Depends(require_permission("admin:user:read"))]` 即可鉴权,无权限返回 403 `FORBIDDEN`。格式错误或重复使用会在导入时抛 `ValueError`;但码没写进 seed 或库里时只会让所有人 403,`tests/core/test_permission_code.py` 会检查路由用到的码与 `INITIAL_PERMISSIONS` 完全一致。新增权限码后重跑一次 `scripts.seed_admin` 即可挂给 admin 角色。
模型关系:User ↔ user_role ↔ Role ↔ role_permission ↔ Permission。

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

调用方向约定为 controllers → services → models，不要反向依赖（目前靠评审保证，没有工具强制）。
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
