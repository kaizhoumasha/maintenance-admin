# 部署(单机 Compose)

结构:`nginx`(同域提供前端静态文件 + 反代 `/api`)→ `api`(可多实例)→ `postgres` / `redis`;`worker` 可多个,`beat` 只能一个,`migrate` 每次升级跑一次。需要 Docker Compose ≥ 2.24.4。第 1–5 节用于本地构建演练;正式交付用第 6 节的固定制品,不在部署机重新构建。

默认 Compose 项目名固定为 `enterprise-backend`，对应数据卷为 `enterprise-backend_pgdata` 和 `enterprise-backend_redisdata`。已有部署未完成备份和数据迁移时，所有 Compose 命令须用 `-p <原项目名>` 或 `COMPOSE_PROJECT_NAME` 覆盖默认名，以继续访问原数据卷；仅改项目名不会迁移数据。

## 1. 首次部署

```bash
cp .env.example .env
# 编辑 .env:删除 ENVIRONMENT=local 一行(容器默认 production);
# 设置 JWT_SECRET_KEY(openssl rand -hex 32)、POSTGRES_PASSWORD(openssl rand -hex 16)、CORS_ORIGINS 留空(同域)
mkdir frontend-dist && cp -r <前端仓库>/dist/* frontend-dist/    # 或设置 FRONTEND_DIST 指向前端 dist
docker compose --profile app build api                          # 只构建一次,四个应用服务共用同一镜像
docker compose --profile app up -d --no-build
docker compose --profile app ps -a                               # migrate 应为 Exited (0),api 为 healthy
docker compose --profile app run --rm -e ADMIN_PASSWORD='<密码>' api python -m scripts.seed_admin <用户名> <邮箱>
```

验收:生产启动后直接检查 API 的文档已关闭(经 nginx 访问 `/docs` 会落入前端 SPA 路由,不能用它判断):`docker compose --profile app exec -T api python -c "import urllib.request as u; u.urlopen('http://localhost:8000/docs')"` 应报 `HTTP Error 404`;浏览器经 https 访问能登录、刷新、退出;`.env` 里改 `ACCESS_TOKEN_EXPIRE_MINUTES` 后重建容器,`docker compose --profile app exec api env | grep EXPIRE` 能看到新值。

## 2. HTTPS

本模板只提供 HTTP(`deploy/nginx/default.conf`),证书由外层 nginx/网关管理,并转发 `X-Forwarded-For`。注意:

- refresh Cookie 带 `Secure`(非 local),浏览器只在 https(或 localhost)下保存。外层必须终止 TLS,不能让用户用 `http://<ip>` 访问。
- 本层 nginx 总是把 `X-Forwarded-For` 覆盖成它看到的对端地址(客户端自带的头不会传给 api)。在外层网关之后时,对端是网关 IP,需要在站点专属 nginx 配置的 `server` 里加 `set_real_ip_from <网关地址>; real_ip_header X-Forwarded-For;` 还原,且只信任明确的网关地址,否则限流和审计 IP 会失真。生产制品内的 `deploy/nginx/default.conf` 不可修改,按第 6 节在包外配置并挂载。
- nginx 访问日志不记录查询串和 Referer(用户列表的邮箱/用户名筛选在查询串里)。nginx 的 error 日志在上游出错时仍会带完整请求行,不要把它转发到公共位置。
- 前端与 `/api` 必须同域(Cookie 是 `SameSite=Strict`)。

## 3. 多实例

- 同机扩容:`docker compose --profile app up -d --scale api=3`(nginx 通过 Docker DNS 轮询,无需重启)。`worker` 同理;**`beat` 不能扩**。
- 多机:每台机器跑 api/worker,指向同一个 Postgres/Redis;nginx 换成 `upstream` 列表,并把 `FORWARDED_ALLOW_IPS` 改成 nginx 的地址(不要用 `*`)。`migrate` 只在一处执行。
- 停止宽限:`api` 的 `stop_grace_period` 为 20 秒,必须大于出站发送器的优雅停机预算(默认 15 秒);自行改写 Compose 或使用其它编排时保持这一关系,否则停机时容器会在清理完成前被 SIGKILL,未确认的发送只能靠租约到期恢复。

## 4. 数据与备份

- **Postgres 是唯一的业务数据源**:用户、角色、权限、菜单、审计。每天备份并拷到另一台机器:

  ```bash
  scripts/backup_pg.sh              # 输出到 ./backups,默认保留 7 天(BACKUP_DIR / BACKUP_KEEP_DAYS)
  # crontab: 30 2 * * * cd /path/to/project && scripts/backup_pg.sh
  ```

- **Redis** 存 refresh 会话、限流窗口、Celery 队列,已开 AOF(每秒刷盘),不单独备份。数据丢失的后果:全员重新登录;队列中未执行的任务丢失(每日归档任务次日自动重跑)。若以后加了"不能丢"的一次性任务,任务要幂等,待办落 Postgres。
- 恢复。必须按顺序,换 JWT 密钥是**必做步骤**:数据库回到旧状态后,用户 ID 会被复用,恢复前签发的 access/refresh token(都用 `JWT_SECRET_KEY` 签名)若仍有效,就可能被当成新用户的凭证。

  ```bash
  docker compose --profile app stop nginx api worker beat
  # 把 .env 里的 JWT_SECRET_KEY 换成新值(openssl rand -hex 32)
  JWT_ROTATED=yes CONFIRM=yes scripts/restore_pg.sh backups/db-<时间>.dump
  docker compose --profile app up -d --force-recreate     # 让所有进程加载新密钥
  ```

  脚本做的事:恢复到全新的临时库(单事务,失败整体回滚并丢弃临时库,原库不动);成功后在**同一个事务里**把原库改名为 `<库>_old_<时间>`、临时库改为原库名(要么都成功,要么都不变);再调用 `scripts/clear_redis_after_restore.sh` 删除 Redis 里的 `refresh:*`、`user_refresh:*` 会话键,以及 Celery 队列(`celery*`,含 kombu 的 `unacked`、`unacked_index` 未确认消息),并校验已清空;限流等其它键不动。没有设 `JWT_ROTATED=yes` 脚本会拒绝执行。
  - 数据库已切换但 Redis 清理失败时,脚本报错退出:保持 api/worker/beat 停止,排除 Redis 故障后单独执行 `scripts/clear_redis_after_restore.sh`,**不要重新恢复数据库**。
  - `JWT_ROTATED=yes` 只是你的操作确认,脚本不会校验密钥真的换了;启动前确认 `.env` 里的 `JWT_SECRET_KEY` 与恢复前不同。
  - 切换失败时原库保持原名,临时库 `<库>_restore` 保留;排除原因(例如连接占用)后直接重跑即可。
  - 恢复后检查:能登录,用户/角色/菜单数量与备份时一致,审计列表可查,恢复前签发的 token 返回 401;确认无误后手动删旧库(`DROP DATABASE "<库>_old_<时间>"`,先 `docker compose exec postgres psql -U admin -d postgres`)。
- 备份文件含密码哈希、邮箱、授权和审计信息:脚本用 `umask 077`、备份目录 `chmod 700`,文件仅当前用户可读。**升级前已存在的备份不会被追溯修正**,请手动 `chmod 600` 并检查拷贝到其它机器后的权限。
- 脚本的隔离验收:`scripts/test_backup_restore.sh`(起一次性 Postgres 容器,覆盖备份权限、回退后无残留且可再次建表(不等于真实 Alembic 版本回退后再升级)、切换失败(含第二次改名失败)整体回滚并可重试、Redis 会话/队列/未确认消息被清且不误删其它键、Redis 故障时恢复报告未完成且可单独重试清理、损坏备份不破坏原库、备份失败不留文件、未换密钥拒绝恢复)。改动备份/恢复脚本后要跑。

## 5. 升级与回退(允许停机)

升级:

先构建、再停机、最后备份:停机之后才不会再有业务写入,这份备份就是回退点,恢复点之后没有"漏掉的写入"。

```bash
docker compose --profile app images          # 记下当前镜像 ID,回退时要用到旧版本(构建会覆盖同名标签,需要时先 docker tag 保留)
git pull && docker compose --profile app build
docker compose --profile app stop nginx api worker beat
scripts/backup_pg.sh                         # 停机后的最终备份;把它拷到另一台机器
docker compose --profile app up -d          # migrate 先跑,成功后才启动 api/worker/beat
docker compose --profile app ps -a          # migrate 必须 Exited (0)
docker compose --profile app run --rm -e ADMIN_PASSWORD='<密码>' api python -m scripts.seed_admin <用户名> <邮箱>   # 仅当新版本新增了权限/菜单
```

`seed_admin` 的副作用:把全部权限赋给 `admin` 角色、把该用户设为允许多端登录;已存在用户的密码不会被修改。

回退:**不要依赖 `alembic downgrade`**(归档迁移在归档表非空时拒绝降级,权限码迁移的降级会放宽权限)。停应用、换新 `JWT_SECRET_KEY`、恢复备份后,按第 6 节选择已保存的旧镜像和配置启动,不要重新构建旧代码。**恢复点之后的所有写入都会丢失**:按上面的顺序,恢复点就是停机后的最终备份,所以丢失的只是升级后到回退前的写入;如果用的是更早的备份(例如每日定时备份),备份到那次停机之间的写入也会丢失,回退前要先确认这个窗口可以接受。

## 6. 发布准备与旧制品保存

最近发布基线为 **v0.2.0**，当前分支的改名及 HTTP 客户端等后续变更尚未发布。正式交付遵循 AGENTS.md 的 Git Flow：功能分支合入 develop 后，在 release 分支整理版本和发布说明，最终 main 的发布提交打 annotated Git 标签；这些动作必须另行授权。`pyproject.toml` 和 FastAPI 版本须一致。审计游标分页和 nginx 入口属于兼容变化，调用方须按 README 迁移。

### 准备交付目录(不会提交、打 Git 标签或推送)

在源码仓库根目录,Python 3.13+、uv、Docker 可用时执行。选择部署机支持的平台构建,不要把 arm64 镜像用于 amd64。以下固定标签仅为格式示例,替换 `<提交短SHA>`:

```bash
uv run python scripts/release_bundle.py prepare \
  --image enterprise-backend:0.2.0-<提交短SHA> \
  --output releases/0.2.0-<提交短SHA>
```

默认拒绝未提交源码、已存在的输出目录、已存在的镜像标签及 latest/dev 标签。未提交修改仅做隔离验收时加 `--allow-dirty`,清单会记录 `source_dirty=true`、基线提交及实际源码指纹;不能把它标成正式发布。需要重建就用新标签,不能覆盖已验收标签。

工具只构建一次应用镜像,四个应用服务共用它。目录包含:

- `manifest.json`:应用版本、完整源码提交、dirty 标记、源码指纹、构建平台、应用 image ID、Alembic head、基础设施镜像引用和文件摘要。
- `image.tar`、`image.env`:已构建应用镜像归档及固定标签。标签供选择,image ID 供核对;归档 SHA-256 是文件校验值,不是镜像仓库摘要。
- `docker-compose.yml`、`docker-compose.release.yml`、`docker-compose.site.yml`、nginx 配置原件、恢复脚本、本工具和文档。生产叠加配置移除 build;基础设施引用在打包时固定为本地已取得的仓库摘要。站点覆盖文件挂载包外配置,原件继续参与制品校验。
- `SHA256SUMS`:覆盖清单及所有交付文件。不包含真实 `.env`、密码、数据库备份或前端文件。校验只能检测传输损坏;接收时还应从可信渠道核对该校验文件的摘要。

```bash
python3 scripts/release_bundle.py verify releases/0.2.0-<提交短SHA>
```

将整个目录复制到独立存储,取回后再次校验。保留当前版本、上一稳定版本以及仍处于回退窗口内的版本,不能只依赖部署机标签或源码。`image.tar` 只含应用镜像;PostgreSQL/Redis/nginx 必须按清单摘要提前拉取并在回退窗口内保留,离线站点另行 `docker image save` 保存这些基础设施制品。数据库备份独立保存,记录它对应的发布清单、时间及摘要,权限与异机传输按第 4 节执行。

### 固定镜像部署与升级

以下为新站点示例，部署根目录 `/srv/enterprise-backend`，固定 Compose 项目名 `enterprise-backend-prod`。已有站点必须继续使用原部署目录、Compose 项目名及数据卷，不因本次改名迁移；例如原项目名为 `maintenance-prod` 时，以下命令的 `-p`、`COMPOSE_PROJECT_NAME` 和 `--project` 均须使用该原值。仓库目录改名也须显式指定原项目名，否则 Compose 会创建另一套数据卷。数据库名统一为 `enterprise_backend`，旧库按下节原地改名；显式配置的 `APP_NAME` 和 `APP_IMAGE` 按站点需要更新，旧镜像继续保留用于回退。每个发布目录只放制品原件；真实配置放 `/srv/enterprise-backend/shared/.env`，权限 600。`FRONTEND_DIST` 使用绝对路径，如 `/srv/enterprise-backend/shared/frontend-dist`。

首次部署把包内 `deploy/nginx/default.conf` **复制**到包外的 `/srv/enterprise-backend/shared/nginx/default.conf`,在这份副本中配置明确的 HTTPS 网关信任地址;不要编辑包内文件或重新生成包的校验值。shared/.env 设置 `SITE_NGINX_CONFIG=/srv/enterprise-backend/shared/nginx/default.conf`,后续所有生产 Compose 命令在 release 配置后再加载 `docker-compose.site.yml`。覆盖文件按相同挂载目标替换 nginx 原配置,只读挂载且不会自动创建缺失文件。即使没有外层网关也使用该外部副本,无需增加信任地址。

站点文件不属于制品完整性校验范围,须单独审阅和留档:保存站点配置副本及 `sha256sum /srv/enterprise-backend/shared/nginx/default.conf` 的结果,修改后先 `nginx -t` 再重新加载。升级/回退前检查它与目标版本是否兼容,必要时使用该版本留档的站点副本;不能用“发布包校验通过”代替站点安全检查。

取回候选发布目录后,先校验/加载应用镜像,再把同一真实配置链接到该发布目录。不要把真实配置重新打包。以下在选定发布目录执行:

```bash
python3 scripts/release_bundle.py load .
ln -s /srv/enterprise-backend/shared/.env .env     # 仅首次创建,已存在时先确认指向正确
docker compose -p enterprise-backend-prod --env-file .env --env-file image.env \
  -f docker-compose.yml -f docker-compose.release.yml -f docker-compose.site.yml --profile app config --images
```

`config --images` 只核对镜像引用;不要把包含真实环境变量的完整 `config` 输出写进普通日志/清单。部署机须已取得清单中基础设施镜像,核对平台和磁盘空间。首次部署无需备份;升级时先在当前版本目录停应用、制作最终备份并校验/异机保存,备份失败就保持停止,不得继续升级:

```bash
# 在当前版本目录执行,沿用同一项目名及配置文件组合
docker compose -p enterprise-backend-prod --env-file .env --env-file image.env \
  -f docker-compose.yml -f docker-compose.release.yml -f docker-compose.site.yml --profile app stop nginx api worker beat
scripts/backup_pg.sh
```

备份/恢复脚本默认没有 `-p` 参数;此流程必须先 `export COMPOSE_PROJECT_NAME=enterprise-backend-prod`,并从对应发布目录执行。不要并行运行升级、恢复或人工业务写入。

在候选发布目录,先单独执行 migrate,成功后再启动应用。每条失败都应停止后续操作,不允许整段无条件继续:

```bash
docker compose -p enterprise-backend-prod --env-file .env --env-file image.env \
  -f docker-compose.yml -f docker-compose.release.yml -f docker-compose.site.yml --profile app run --rm --no-deps migrate alembic upgrade head
docker compose -p enterprise-backend-prod --env-file .env --env-file image.env \
  -f docker-compose.yml -f docker-compose.release.yml -f docker-compose.site.yml --profile app up -d --no-build --pull never
python3 scripts/release_bundle.py check . --project enterprise-backend-prod
```

迁移前须确认 PostgreSQL 已运行(首次部署先启动 postgres/redis)。`up` 仍会保留并执行 migrate 服务,其成功状态供运行核对。`check` 核对四个应用服务的实际 image ID、迁移成功及 beat 单实例,不代替健康/业务检查。之后检查 `/ready`、登录、权限/菜单、审计分页及真实队列任务,按版本需要补种子。保留成功验收记录,不要记录 token/密码。

### WMS 回调接收启用门槛

`POST /api/v1/wms/events` 已实现通用接收，并在所有环境自动注册 `transport.task.member_position_changed@v1` 与 `transport.task.resulted@v1`；示例 `example.echo@v1` 仅在 `ENVIRONMENT=local` 注册。合法但未注册的 operation 返回持久化的 `422 REJECTED / UNSUPPORTED_OPERATION`，不是临时失败；此前已保存的不支持响应仍按原消息身份稳定重放。代码具备 Transport 接收能力不等于所有部署实例已升级或真实回调已启用。

在鉴权方案落地前，网关必须限制 WMS 来源，禁止向公网无保护开放该接口。完成设计第 3 步并确认所有实例已加载实际 Transport 接收能力、所有任务创建者(API、worker、脚本)均已升级之前，不得启用真实回调；滚动升级时先暂停网关回调入口，全部实例就绪后再恢复，避免旧实例将合法业务消息永久记录为不支持。详细协议和后续启用条件见[回调接收设计](superpowers/specs/2026-10-22-transport-callback-design.md)。

请求体上限为 256 KiB；独立诊断快照最多保存 64 KiB 的 base64 内容，按进程限量、尽力写入，不影响协议响应。`CALLBACK_SNAPSHOT_RETENTION_DAYS` 默认为 7，必须为正整数，由现有 Celery beat 每日清理过期快照；幂等收据不随快照清理,而是按 `RELIABLE_RETENTION_DAYS`（默认 90 天）由 beat 每日清理。`TRANSPORT_STUCK_ALERT_MINUTES`（默认 120，必须为正整数，按现场 SOP 调整）：beat 每 10 分钟扫描创建后超过该时长仍无确定终态的 Transport 任务，输出结构化告警日志 `transport task stuck`（含 `transport_task_id`、`cycle_id`、`reason`、`waiting_minutes`），不改任务状态；`reason` 为 `SUBMIT_NOT_DELIVERED`（提交已过期或被拒）、`RESULT_MISSING`（已提交无结果）、`RESULT_WAITING_FOR_POSITIONS`（料箱成功结果等不到位置事件）、`OUTCOME_UNKNOWN`。仍卡住的任务每次扫描都会重复告警，直到结果到达。上线前先迁移数据库，非空收据表禁止降级删除，以免丢失消息身份及首次响应。

### Schema 隔离

PostgreSQL 固定使用 `enterprise_backend` schema；数据库名称与 schema 是两个独立概念，`DATABASE_URL` 仍连接原数据库。`app.database.create_database_engine` 统一给 API、可靠发送器及各 Celery 数据库连接设置会话 `search_path=enterprise_backend`，ORM 元数据也显式限定 schema。没有新增环境变量，不要对共享数据库执行全局 `ALTER DATABASE/ROLE ... SET search_path`。SQLite 测试通过 schema 映射继续使用无 schema 表。

在线 Alembic 入口在同一事务中校验旧库归属、创建 schema、移动现有表，再运行历史迁移，版本表同时移入新 schema。历史 revision 不重写，当前 head 仍为 `a023a20261024`。迁移白名单为 `user`、`role`、`permission`、`user_role`、`role_permission`、`menu`、`audit_logs`、`audit_logs_archive`、`reliable_message`、`transport_task`、`callback_receipt`、`callback_snapshot`，另含 `alembic_version`。`ALTER TABLE SET SCHEMA` 原地保留数据及对象身份，所属索引和序列随表移动。

- 仅自动搬迁版本为 `a023a20261024` 且白名单完整的旧 public 库。更早的旧库须先使用旧代码升级到该基线；不按完整白名单猜测历史版本的表归属。缺表、目标同名表冲突或等待锁超过 5 秒时整笔回滚，不覆盖、不合并、不清空。
- 新库直接在独立 schema 建表。未知 revision 的 public 版本表视为其他系统所有，不读取为项目迁移进度、不移动；只读 `current`/`check` 不触发搬迁。已有项目 schema 版本表后不再扫描 public 同名表。
- `public` 中的其他表（包括未纳入当前 ORM 的历史 `inbound_receipt`、`inbound_snapshot`）不移动、不删除。项目白名单不能替代人工确认旧库确属本项目；混合来源或手工改过 ledger 的库先人工核对。
- PostgreSQL 通过 Alembic CLI 在线执行，不支持 `--sql` 或没有 CLI 命令上下文的 Python API 调用：无法确定操作及在线检查归属和冲突时明确失败。`downgrade` 仅回退业务 revision，不把 schema 迁回 public；旧版应用不能直接连接迁移后的表。回退应用前须单独制定并批准反向迁移，禁止用 `stamp` 伪造完成。

既有站点操作顺序：确认数据库和白名单归属 → 停本项目 API/worker/beat（不停止共享 PostgreSQL 或其他系统）→ 记录本项目和其他系统表的 OID、行数及内容摘要 → 用新版本运行 `uv run alembic upgrade head` → 对比迁移前后对象/数据及其他系统表不变 → 用新版本启动应用 → 检查 `alembic current`、`alembic check`、`/ready`、worker 响应和业务查询。只运行一个迁移进程；保留原数据卷、数据库名及连接凭证，不重跑管理员种子。

### 数据库改名

默认数据库为 `enterprise_backend`，独立测试库为 `enterprise_backend_test`。PostgreSQL 镜像仅在空数据卷首次初始化时使用 `POSTGRES_DB`，修改此变量不会改名已有数据库。已有 `maintenance` 库采用原地 `ALTER DATABASE` 改名，保留数据库 OID、表、数据、权限和扩展，不另建空库或复制出两套可写数据。

1. 盘点本地 `.env`、其他进程环境、数据库工具和自动化任务中的连接配置。仅将实际连接旧库的客户端切到新库；使用独立数据库或 PGLite 的工具无需修改。保留主机、端口、用户名、密码和数据卷。
2. 在当前部署目录停止所有旧库使用方，再执行 `scripts/backup_pg.sh` 保存最终备份并校验；同时记录数据库 OID、表行数和必要的数据摘要。以下命令均沿用该站点原 Compose 项目名及配置文件组合。

   ```bash
   docker compose --profile app stop api worker beat
   scripts/backup_pg.sh
   docker compose exec -T postgres psql -U admin -d maintenance -v ON_ERROR_STOP=1 \
     -c 'SELECT _timescaledb_functions.stop_background_workers()'
   docker compose exec -T postgres psql -U admin -d postgres -v ON_ERROR_STOP=1 \
     -c 'ALTER DATABASE maintenance RENAME TO enterprise_backend'
   docker compose exec -T postgres psql -U admin -d enterprise_backend -v ON_ERROR_STOP=1 \
     -c 'SELECT _timescaledb_functions.start_background_workers()'
   ```

   模板默认使用 TimescaleDB 镜像，调度后台进程也会占用旧库连接，上述暂停及恢复命令仅用于已安装 TimescaleDB 的库；普通 PostgreSQL 不执行这两条扩展函数调用。

3. 将本地及其他实际使用方连接 URL 的数据库部分改为 `/enterprise_backend`，Compose 的 `DATABASE_URL` 和 `POSTGRES_DB` 保持一致。仅在旧库没有活动连接、新库名尚不存在时执行改名；不能连接正在被改名的库来执行此命令。
4. 在目标发布目录按固定镜像流程重建 PostgreSQL 容器和应用，保持原数据卷；验证新库 OID、表行数和数据摘要与改名前一致，迁移成功、`/ready` 正常、worker 可响应，并逐一核对相关工具连接。改名不要求清空 Redis、轮换 JWT 密钥或重跑管理员种子。

如需回退，先停止使用方，将数据库原地改回原名并恢复相应连接配置；不要仅启动仍连接旧库名的历史镜像。备份恢复属于数据损坏时的恢复手段，不是正常改名步骤。

### 使用旧镜像回退(禁止现场重建)

1. 停机前取回旧版本完整发布目录和正确恢复点,执行 `verify`、`load`,确认应用 image ID、平台及基础设施制品可用。首次给历史版本补档时,逐个记录正在运行的 api/worker/beat/migrate 的镜像 ID;若四者不同就分别打保留标签、`docker image save` 并保存原配置,不能只保存 API 镜像或从旧代码重建冒充原制品。
2. 在当前版本目录停止 nginx/api/worker/beat,确认无写入。确认恢复点及其后的数据损失可接受,并在 shared/.env 换成全新 JWT 密钥。
3. 使用已验收的恢复工具恢复该备份(`JWT_ROTATED=yes CONFIRM=yes scripts/restore_pg.sh <备份>`);配置 `COMPOSE_PROJECT_NAME` 保持原项目。Redis 清理失败只重试清理,不重复恢复数据库。
4. 恢复脚本只报告数据库/Redis 完成,不会选择或启动应用。进入目标旧版本目录,链接同一 shared/.env,确认 `image.env` 选择的是旧镜像及站点配置与旧版本兼容。按上述相同命令(包含 release 和 site 覆盖文件)先检查旧镜像迁移,成功后 `up -d --no-build --pull never --force-recreate`。不得使用 `build`、`--build` 或当前版本的 image.env。恢复前确认待启动配置和数据卷仍属于同一 Compose 项目。
5. 执行旧包的 `check` 并检查数据库版本,再验收旧版本接口、新登录、旧 access/refresh 均 401、菜单权限、审计及队列任务。全部成功前保留备份、旧库和新旧两套制品。

最近发布基线 `v0.2.0` 的范围为后端模板,固定镜像目前仅验收 `linux/arm64`;发布说明及兼容变化见 [CHANGELOG](../CHANGELOG.md)。其他架构镜像须单独构建验收。当前分支的改名及 HTTP 客户端等后续变更尚未发布。

剩余验收边界:
- 没有配套前端,真实管理端浏览器联调待验收;接口与 Cookie 的后端验收不能替代真实浏览器联调。
- 独立目录复制不等于真实异机传输。生产站点须完成异机保存、取回、摘要及权限检查,并备齐实际旧镜像、配置与可接受的恢复点;验收快照回退不能替代该站点旧版本回退准备。
- 服务进程异常重启已验收,宿主机或 Docker 守护进程重启后的整套恢复仍待验收。
- 实际站点 TLS、可信代理、密钥、目录权限及 Compose 项目名须在上线前核对;离线交付须额外保存基础设施镜像。

这些待验收项不应写成已通过,也不能用模板版本发布代替生产交付验收。正式发布前须固定最终源码提交与制品,验证 `source_dirty=false` 并验收实际交付包;已有候选包的验收不自动覆盖重新构建的镜像。
