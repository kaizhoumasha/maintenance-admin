# 部署到联调服务器(单机 Compose)

默认访问地址为 **[http://100.94.216.118:18080](http://100.94.216.118:18080)**，Swagger 为 **[http://100.94.216.118:18080/docs](http://100.94.216.118:18080/docs)**。客户端需能通过 Tailscale 访问该地址，不需要 SSH 转发；SSH 运维入口仍为 `ssh CANTAISYS@100.94.216.118`。本项目与现有 WES 共机独立运行。实际部署从[第 6 节](#6-发布准备与旧制品保存)开始，使用固定制品，不在联调服务器重新构建。第 1–5 节保留通用说明及本地构建演练命令；联调服务器上的应用操作统一使用第 6 节的 `dc` 函数，完整加载发布和站点配置。

同一网页入口也开放在内网 **[http://10.24.199.219:18080](http://10.24.199.219:18080)**，Swagger 为 **[http://10.24.199.219:18080/docs](http://10.24.199.219:18080/docs)**。内网客户端使用该地址；PostgreSQL 和 Redis 仍仅通过 Tailscale 地址开放，不随网页入口扩展到内网。

结构:`nginx`(同域提供前端静态文件 + 反代 `/api`)→ `api`(可多实例)→ `postgres` / `redis`;`worker` 可多个,`beat` 只能一个,`migrate` 每次升级跑一次。需要 Docker Compose ≥ 2.24.4。

2026-10-27 已部署并完成基础验收，当前站点配置如下；每次更新前仍须重新核对端口、架构和现有项目：

| 项目 | 联调服务器默认方案 |
| --- | --- |
| 主机 | `HOIB4-MESWES1`，Linux `x86_64`；镜像使用 `linux/amd64` |
| 现有 WES | Compose 项目 `wes_integration`，目录 `/srv/wes/app/current-single`；已映射宿主机 80、443、5555 |
| 本项目 | 根目录 `/srv/enterprise-backend`，Compose 项目 `enterprise-backend-int` |
| 数据隔离 | 独立 PostgreSQL、Redis、网络及数据卷，不复用 WES 的数据库、队列和配置 |
| 网页入口 | 默认 `100.94.216.118:18080`，内网 `10.24.199.219:18080`；分别绑定这两个明确 IP，不绑定所有网卡 |
| 本地数据库工具 | PostgreSQL `100.94.216.118:25432`、Redis `100.94.216.118:26379`，均需各自密码；只绑定该 Tailscale IP |
| 联调环境 | `ENVIRONMENT=local`；此设置只用于联调，正式生产切回 `production` |
| Swagger | FastAPI 内置 `/docs` 和 `/openapi.json`，由当前 API 动态生成，nginx 直接反代 |

[本次部署结果](#本次部署结果2026-10-27)、[后续应用更新](#后续应用更新联调服务器)及[快速修改入口](#快速修改候选入口与切换到-80-端口)见下文。

此方案无需停止 WES。现有 WES 映射了 443 不代表证书或 TLS 配置可直接复用。当前源码正式业务清单登记粗分扫码、WMS 验证及首个输送结果阶段；具体服务器使用的提交和阶段以固定制品清单及部署记录为准，部署成功不代表完整分拣业务或现场验收通过。既有 WMS 目标配置保留；部署验证只读检查设备 Status，业务闭环使用隔离 MOCK，不向现场设备发送测试命令。

仓库基础 Compose 项目名为 `enterprise-backend`，对应数据卷为 `enterprise-backend_pgdata` 和 `enterprise-backend_redisdata`；联调服务器按第 6 节显式使用 `enterprise-backend-int`。已有部署未完成备份和数据迁移时，所有 Compose 命令须用 `-p <原项目名>` 或 `COMPOSE_PROJECT_NAME` 覆盖基础名，以继续访问原数据卷；仅改项目名不会迁移数据。

## 1. 本地首次部署演练

```bash
cp .env.example .env
# 联调保留 ENVIRONMENT=local；正式生产改为 production(容器缺省值也是 production)
# 设置 JWT_SECRET_KEY(openssl rand -hex 32)、POSTGRES_PASSWORD(openssl rand -hex 16)、CORS_ORIGINS 留空(同域)
mkdir frontend-dist && cp -r <前端仓库>/dist/* frontend-dist/    # 或设置 FRONTEND_DIST 指向前端 dist
docker compose --profile app build api                          # 只构建一次,四个应用服务共用同一镜像
docker compose --profile app up -d --no-build
docker compose --profile app ps -a                               # migrate 应为 Exited (0),api 为 healthy
docker compose --profile app run --rm -e ADMIN_PASSWORD='<密码>' api python -m scripts.seed_admin <用户名> <邮箱>
```

验收:联调 `local` 的 API 内置 `/docs`、`/openapi.json` 应返回 200，HTTP 同域登录、刷新和退出可用。正式生产切到 `production` 后，直接检查容器内 `/docs` 应为 404，并通过 HTTPS 验收登录会话。`.env` 里改 `ACCESS_TOKEN_EXPIRE_MINUTES` 后重建容器,`docker compose --profile app exec api env | grep EXPIRE` 能看到新值。

## 2. HTTPS

本模板只提供 HTTP(`deploy/nginx/default.conf`),证书由外层 nginx/网关管理,并转发 `X-Forwarded-For`。注意:

- 当前联调使用 `local`，refresh Cookie 不带 `Secure`，保留 `HttpOnly` 和 `SameSite=Strict`，支持当前 HTTP 同域登录和刷新。正式生产必须切回 `production` 并配置 HTTPS，届时 Cookie 带 `Secure`；联调的 HTTP 验证不替代生产 HTTPS 验收。
- 本层 nginx 总是把 `X-Forwarded-For` 覆盖成它看到的对端地址(客户端自带的头不会传给 api)。在外层网关之后时,对端是网关 IP,需要在站点专属 nginx 配置的 `server` 里加 `set_real_ip_from <网关地址>; real_ip_header X-Forwarded-For;` 还原,且只信任明确的网关地址,否则限流和审计 IP 会失真。生产制品内的 `deploy/nginx/default.conf` 不可修改,按第 6 节在包外配置并挂载。
- nginx 访问日志不记录查询串和 Referer(用户列表的邮箱/用户名筛选在查询串里)。nginx 的 error 日志在上游出错时仍会带完整请求行,不要把它转发到公共位置。
- 前端与 `/api` 必须同域(Cookie 是 `SameSite=Strict`)。

## 3. 多实例

- 联调服务器使用第 6 节的完整配置，例如 `dc up -d --no-build --pull never --scale api=3`；不要直接运行下面省略站点配置的本地命令。
- 同机扩容:`docker compose --profile app up -d --scale api=3`(nginx 通过 Docker DNS 轮询,无需重启)。`worker` 同理;**`beat` 不能扩**。
- 多机:每台机器跑 api/worker,指向同一个 Postgres/Redis;nginx 换成 `upstream` 列表,并把 `FORWARDED_ALLOW_IPS` 改成 nginx 的地址(不要用 `*`)。`migrate` 只在一处执行。
- 停止宽限:`api` 的 `stop_grace_period` 为 20 秒,必须大于出站发送器的优雅停机预算(默认 15 秒);自行改写 Compose 或使用其它编排时保持这一关系,否则停机时容器会在清理完成前被 SIGKILL,未确认的发送只能靠租约到期恢复。

## 4. 数据与备份

联调服务器须先按第 6 节导出 `COMPOSE_PROJECT_NAME=enterprise-backend-int`，从对应发布目录调用备份/恢复脚本；应用停启使用 `dc stop nginx api worker beat` / `dc up -d --no-build --pull never --force-recreate`，保留完整配置和原项目数据卷。

- **Postgres 是唯一的业务数据源**:用户、角色、权限、菜单、审计。每天备份并拷到另一台机器:

  ```bash
  scripts/backup_pg.sh              # 输出到 ./backups,默认保留 7 天(BACKUP_DIR / BACKUP_KEEP_DAYS)
  # crontab: 30 2 * * * cd /path/to/project && scripts/backup_pg.sh
  ```

- **Redis** 存 refresh 会话、限流窗口、Celery 队列,已开 AOF(每秒刷盘),不单独备份。数据丢失的后果:全员重新登录;队列中未执行的任务丢失(每日归档任务次日自动重跑)。若以后加了"不能丢"的一次性任务,任务要幂等,待办落 Postgres。
- 联调 Redis 通过包外 ACL 启用密码，容器内 `REDISCLI_AUTH` 供现有健康检查和恢复清理脚本使用。保留该环境变量和 ACL 挂载；不要为排查连通性运行恢复或清理脚本，它们会删除会话和队列。
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

## 5. 本地升级与回退演练(允许停机)

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

在选定干净提交的源码仓库根目录，Python 3.13+、uv、Docker 可用时执行。联调服务器是 `linux/amd64`，应在匹配架构的构建环境生成并验收应用和基础设施镜像。工具当前没有 `--platform` 参数，已有 arm64 候选包不能直接作为该服务器的交付包。以下固定标签仅为格式示例，替换 `<提交短SHA>`；`integration-...` 表示联调候选标识，不代表已完成正式版本发布：

```bash
uv run python scripts/release_bundle.py prepare \
  --image 'enterprise-backend:integration-<提交短SHA>' \
  --output 'releases/integration-<提交短SHA>'
```

默认拒绝未提交源码、已存在的输出目录、已存在的镜像标签及 latest/dev 标签。未提交修改仅做隔离验收时加 `--allow-dirty`,清单会记录 `source_dirty=true`、基线提交及实际源码指纹;不能把它标成正式发布。需要重建就用新标签,不能覆盖已验收标签。

工具只构建一次应用镜像,四个应用服务共用它。目录包含:

- `manifest.json`:应用版本、完整源码提交、dirty 标记、源码指纹、构建平台、应用 image ID、Alembic head、基础设施镜像引用和文件摘要。
- `image.tar`、`image.env`:已构建应用镜像归档及固定标签。标签供选择,image ID 供核对;归档 SHA-256 是文件校验值,不是镜像仓库摘要。
- `docker-compose.yml`、`docker-compose.release.yml`、`docker-compose.site.yml`、nginx 配置原件、恢复脚本、本工具和文档。生产叠加配置移除 build;基础设施引用在打包时固定为本地已取得的仓库摘要。站点覆盖文件挂载包外配置,原件继续参与制品校验。
- 文档：从部署说明、README、CHANGELOG 出发，按 CommonMark 解析实际内联和引用式 Markdown 链接，忽略代码块、行内代码及注释中的示例。仓库内 Markdown 依赖递归随包交付，保留相对路径并纳入 manifest.json / SHA256SUMS；只能引用本次源码指纹文件集合中的依赖，不存在或被 Git 忽略的本地依赖会明确报错，`--allow-dirty` 不放宽此限制。外部 URL 不下载，跨仓库参考及源码/测试链接仍需对应源码，不随文档复制代码或真实配置。
- `SHA256SUMS`:覆盖清单及所有交付文件。不包含真实 `.env`、密码、数据库备份或前端文件。校验只能检测传输损坏;接收时还应从可信渠道核对该校验文件的摘要。

准备阶段通过 `uv run` 使用锁定的 Markdown 解析开发依赖；部署机运行 `verify`、`load`、`check` 仍只需 Python 标准库，无需安装该解析器。

```bash
python3 scripts/release_bundle.py verify 'releases/integration-<提交短SHA>'
```

将整个目录复制到独立存储,取回后再次校验。保留当前版本、上一稳定版本以及仍处于回退窗口内的版本,不能只依赖部署机标签或源码。`image.tar` 只含应用镜像;PostgreSQL/Redis/nginx 必须按清单摘要提前拉取并在回退窗口内保留,离线站点另行 `docker image save` 保存这些基础设施制品。数据库备份独立保存,记录它对应的发布清单、时间及摘要,权限与异机传输按第 4 节执行。

### 固定镜像部署与升级

以下默认在 `CANTAISYS@100.94.216.118` 新建本项目部署。部署根目录为 `/srv/enterprise-backend`，固定 Compose 项目名为 `enterprise-backend-int`，数据卷为 `enterprise-backend-int_pgdata` / `enterprise-backend-int_redisdata`。已有本项目站点必须继续使用其原目录、项目名及数据卷，不能仅改名后启动空卷；以下 `COMPOSE_PROJECT_NAME` 和 `check --project` 也须沿用该原值。不要使用 WES 的 `wes_integration` 项目名或目录。

首次由有权限的运维人员创建根目录并授权部署用户写入，仅操作本项目目录。将完整发布包上传到 `releases/<发布标识>`；发布目录只放制品原件及指向真实 `.env` 的链接，不覆盖旧包。目录布局为：

```text
/srv/enterprise-backend/
├── releases/<发布标识>/
├── current -> releases/<当前发布标识>
├── records/
└── shared/
    ├── .env
    ├── initial-admin.env
    ├── database-access.env
    ├── compose.sh
    ├── docker-compose.integration.yml
    ├── nginx/default.conf
    ├── redis/users.acl
    └── frontend-dist/
```

首次部署将包内 `.env.example` 和 `deploy/nginx/default.conf` 分别复制到包外的 `shared/.env` 和 `shared/nginx/default.conf`，已有文件不得覆盖。`.env` 权限设为 600，至少配置以下值；尖括号内容须先替换，随机值可分别用 `openssl rand -hex 32` 和 `openssl rand -hex 16` 生成，不沿用 WES 的密钥和密码：

```dotenv
ENVIRONMENT=local
COMPOSE_PROJECT_NAME=enterprise-backend-int
JWT_SECRET_KEY=<单独生成的至少32字符随机密钥>
ACCESS_TOKEN_EXPIRE_MINUTES=4320
REFRESH_TOKEN_EXPIRE_DAYS=7
POSTGRES_PASSWORD=<单独生成的十六进制随机密码>
REDIS_PASSWORD=<用openssl rand -hex 32单独生成的64位十六进制密码>
CORS_ORIGINS=
RELIABLE_TARGETS={"targets":{"wms-transport":{"base_url":"http://10.24.199.217:8285","protocol":"wms-transport","max_not_received_attempts":3},"wms-facts":{"base_url":"http://10.24.199.217:8285","protocol":"wms-facts"},"wms-decisions":{"base_url":"http://10.24.199.217:8285","protocol":"wms-decisions"}}}
NGINX_PORT=18080
FRONTEND_DIST=/srv/enterprise-backend/shared/frontend-dist
SITE_NGINX_CONFIG=/srv/enterprise-backend/shared/nginx/default.conf
```

Compose 中应用仍通过 `postgres:5432/enterprise_backend` 和 `redis:6379/0` 访问本项目依赖，不绕行宿主机端口。基础 Compose 固定了连接 URL，联调覆盖文件还须给 API、worker、beat、migrate 四个服务设置带密码的 `REDIS_URL`；只在 `.env` 填同名 URL 不会覆盖基础配置。不要改成 WES 的连接。将配套前端构建产物放到 `FRONTEND_DIST`，发布包不包含前端文件。联调阶段明确保留 `ENVIRONMENT=local`，由 FastAPI 直接提供 Swagger，同时挂载项目已有的调试控制台和 Mock；正常鉴权、权限及消息身份规则保持。正式生产再切到 `production`，关闭调试和内置文档，不沿用联调设置对外发布。

真实 WMS 和 Transport 的站点地址均为 `http://10.24.199.217:8285`，当前已按上述配置为三个协议分别登记一个目标；`base_url` 只填服务地址，不拼接口路径。新站点若先做完全隔离验收，可暂用 `RELIABLE_TARGETS={}`，不得据此改写或清除历史消息。

适配器分别追加 `/api/v1/wes/transport-requests`、`/api/v1/wes/facts`、`/api/v1/wes/decisions`。具体 operation、重试约定和回调入口仍按对应合同验收，配置地址不代表已验证对端接口。修改目标前先停本项目 API、worker、beat 并核对出站队列，发现待发送、发送中或不确定消息时先调查，避免重启后自动发送存量义务；保留原消息身份和证据，不另建任务重发。修改后重建应用进程以加载配置，不是只重建 nginx。当前目标已经是真实地址，之后在 Swagger 提交任务或 facts/decisions 请求会走真实目标；本次检查不调用这些业务接口。

ECS 地址按设备动态解析，部署 `.env` 不配置全局 ECS URL，也不要在 `RELIABLE_TARGETS` 手工添加 ECS 目标，启动时会拒绝该配置。管理员提供 `ecs_origin` 查询预览，再明确登记设备；正式命令从设备当前路由取得地址，创建时冻结到原命令，之后改址不会改写旧命令的目标。这里的动态寻址不包含自动发现或自动改址。设备登记、工作线角色绑定，以及正式业务包的命令合同装配仍须完成，见[设备管理](integrations/device-management.md)和[ECS 命令接入](integrations/ecs-command.md)。当前 `local` 调试请求通过 `ecs_url` 指定地址，真实调试发送还要求 `dev:ecs:send_real` 权限；空 WMS 目标不等于禁用 ECS 调试发送，验收不填写真实 ECS 地址或提交真实命令。正式生产不开放此调试入口。

首次部署还须在包外创建 Redis ACL。以下命令读取已填好的 64 位十六进制 `REDIS_PASSWORD`，仅保存 SHA-256 密码摘要，拒绝覆盖已有 ACL；已有站点升级直接沿用原文件，不重新生成密码：

```bash
python3 - <<'PY'
import hashlib
from pathlib import Path

shared = Path('/srv/enterprise-backend/shared')
values = dict(line.split('=', 1) for line in (shared / '.env').read_text().splitlines()
              if line and not line.startswith('#') and '=' in line)
password = values['REDIS_PASSWORD']
assert len(password) == 64 and all(c in '0123456789abcdef' for c in password)
folder = shared / 'redis'
folder.mkdir(mode=0o755, exist_ok=True)
acl = folder / 'users.acl'
with acl.open('x') as output:
    output.write(f'user default reset on #{hashlib.sha256(password.encode()).hexdigest()} ~* &* +@all\n')
acl.chmod(0o644)  # 仅含摘要；容器中的 Redis 用户需要读取
PY
```

`.env` 保持权限 600；ACL 不含明文密码，通过只读挂载加载。创建后在包外配置 `shared/docker-compose.integration.yml`：

```yaml
services:
  postgres:
    ports: !override
      - "100.94.216.118:25432:5432"
  redis:
    ports: !override
      - "100.94.216.118:26379:6379"
    environment:
      REDISCLI_AUTH: ${REDIS_PASSWORD:?请设置独立Redis密码}
    command: ["redis-server", "--appendonly", "yes", "--appendfsync", "everysec", "--protected-mode", "yes", "--aclfile", "/usr/local/etc/redis/users.acl"]
    volumes:
      - type: bind
        source: /srv/enterprise-backend/shared/redis/users.acl
        target: /usr/local/etc/redis/users.acl
        read_only: true
        bind:
          create_host_path: false
  nginx:
    ports: !override
      - "100.94.216.118:${NGINX_PORT:-18080}:80"
      - "10.24.199.219:${NGINX_PORT:-18080}:80"
  api:
    environment:
      REDIS_URL: redis://:${REDIS_PASSWORD:?请设置独立Redis密码}@redis:6379/0
  worker:
    command: celery -A app.tasks.celery_app worker -l info --concurrency=2
    environment:
      REDIS_URL: redis://:${REDIS_PASSWORD:?请设置独立Redis密码}@redis:6379/0
  beat:
    environment:
      REDIS_URL: redis://:${REDIS_PASSWORD:?请设置独立Redis密码}@redis:6379/0
  migrate:
    environment:
      REDIS_URL: redis://:${REDIS_PASSWORD:?请设置独立Redis密码}@redis:6379/0
```

该文件将 PostgreSQL、Redis 的宿主机端口仅绑定到 Tailscale IP；nginx 同时绑定明确的 Tailscale 和内网 IP。PostgreSQL 沿用原密码认证，Redis 启用独立密码，worker 限制为 2 个并发。原命名数据卷继续保留，Redis 只增加 ACL 文件挂载。无需 Swagger 目录挂载或导出脚本，文档路由直接反代到 API，见下文。不能用普通 `ports` 列表叠加，否则可能保留基础配置中的宿主机端口。`!override` 的行为见 [Docker Compose 合并规则](https://docs.docker.com/reference/compose-file/merge/)。

在包外 nginx 副本中配置本站点路由；当前直接访问时不额外信任代理地址，后续接入 HTTPS 网关时只信任明确的网关来源。不要修改包内文件或重新生成其校验值。后续所有部署命令均在 release、site 配置后追加联调覆盖文件。site 挂载只读且不会自动创建缺失的 nginx 配置文件。

站点文件不属于制品完整性校验范围,须单独审阅和留档:保存站点配置副本及 `sha256sum /srv/enterprise-backend/shared/nginx/default.conf` 的结果,修改后先 `nginx -t` 再重新加载。升级/回退前检查它与目标版本是否兼容,必要时使用该版本留档的站点副本;不能用“发布包校验通过”代替站点安全检查。

部署机需 Python 3.11+ 运行制品工具，当前服务器的 `python3` 为 3.12.13；应用所需 Python 3.13 由镜像提供，无需替换宿主机 Python。从可信渠道核对校验文件后，在选定发布目录执行；同一终端中定义 `dc`，切换发布目录后其相对路径即指向该版本。真实配置不重新打包：

```bash
python3 scripts/release_bundle.py load .
ln -s /srv/enterprise-backend/shared/.env .env     # 仅首次创建,已存在时先确认指向正确
export COMPOSE_PROJECT_NAME=enterprise-backend-int
dc() {
  docker compose -p "$COMPOSE_PROJECT_NAME" \
    --env-file /srv/enterprise-backend/shared/.env --env-file image.env \
    -f docker-compose.yml -f docker-compose.release.yml -f docker-compose.site.yml \
    -f /srv/enterprise-backend/shared/docker-compose.integration.yml \
    --profile app "$@"
}
dc config --quiet
dc config --images
```

`config --quiet` 校验合并配置，`config --images` 只输出镜像引用；不要把包含真实环境变量的完整 `config` 输出写进普通日志/清单。部署机须按发布清单提前取得匹配 `linux/amd64` 的基础设施镜像，核对平台、端口占用和磁盘空间。首次部署仅在确认本项目尚无既有数据时无需备份，先启动依赖：

```bash
dc up -d --no-build --pull never postgres redis
dc ps
```

确认 PostgreSQL、Redis 均为 healthy 后再迁移。升级时，在当前版本目录加载同一 `dc` 配置，停止本项目应用、制作最终备份并校验/异机保存；备份失败就保持停止，不继续升级：

```bash
# 在当前版本目录执行,沿用同一项目名及配置文件组合
dc stop nginx api worker beat
scripts/backup_pg.sh
```

备份/恢复脚本默认没有 `-p` 参数，因此必须保留上面导出的 `COMPOSE_PROJECT_NAME`，从对应发布目录执行。不要并行运行升级、恢复或人工业务写入，不执行 `down -v` 或清理 WES 的容器、网络和数据卷。

在候选发布目录,先单独执行 migrate,成功后再启动应用。每条失败都应停止后续操作,不允许整段无条件继续:

```bash
dc run --rm --no-deps migrate alembic upgrade head
dc up -d --no-build --pull never
dc ps -a
python3 scripts/release_bundle.py check . --project "$COMPOSE_PROJECT_NAME"
```

`up` 仍会保留并执行 migrate 服务，其成功状态供运行核对。`check` 核对四个应用服务的实际 image ID、迁移成功及 beat 单实例，不代替健康/业务检查。直接检查容器内 `/ready`，避免被 nginx 的 SPA 回落页面误导：

```bash
dc exec -T api python -c "import urllib.request as u; print(u.urlopen('http://localhost:8000/ready').read().decode())"
```

首次初始化管理员时，在 Bash 中交互输入密码，不将真实密码写进命令历史；用户名和邮箱替换后执行：

```bash
read -r -s -p '管理员密码: ' ADMIN_PASSWORD
export ADMIN_PASSWORD
dc run --rm --no-deps -e ADMIN_PASSWORD api python -m scripts.seed_admin '<用户名>' '<邮箱>'
unset ADMIN_PASSWORD
```

`seed_admin` 的副作用见第 5 节；已有用户不会重置密码，升级仅在确需补权限/菜单时运行。

在可访问对应地址的客户端直接检查，不建立 SSH 转发：

```bash
curl --noproxy '*' --fail http://100.94.216.118:18080/ready
curl --noproxy '*' --fail --head http://100.94.216.118:18080/docs
curl --noproxy '*' --fail --head http://10.24.199.219:18080/docs
```

当前没有配套管理前端，首页只提供后端说明。先通过接口检查登录、权限/菜单和任务进程；通过 Swagger 调用 `/api/v1/auth/login` 后，取响应中的 `access_token` 填入 Authorize，业务接口仍执行原有鉴权和权限检查。当前 `local` 的 HTTP Cookie 支持同域刷新和退出，但管理前端仍待单独验收，正式生产还需 HTTPS。入口可达也不等于已授权真实 WMS/ECS 回调，启用前仍按来源限制和协议完成验收。保留验收记录，不记录 token/密码。

### 容器内部连接与本地工具访问地址

这两组地址用途不同，更新部署时必须分别保留：

| 使用方 | PostgreSQL 地址 | Redis 地址 | 配置位置 |
| --- | --- | --- | --- |
| 本项目 API、worker、beat、migrate 容器 | `postgres:5432` | `redis:6379` | Compose 中应用服务的 `DATABASE_URL` / `REDIS_URL` |
| 本地数据库工具 | `100.94.216.118:25432` | `100.94.216.118:26379` | 工具的连接设置；凭据记录在 `shared/database-access.env` |

容器内部通过 Docker 网络解析 Compose 服务名 `postgres`、`redis`，使用容器端口 5432、6379。内部连接的数据库分别为 `enterprise_backend` 和 Redis DB `0`；连接 URL 保留密码认证。25432、26379 是宿主机映射端口，只供容器外的本地工具访问。调整对外地址或端口不需要改变应用内部连接。

**不要将 Tailscale IP 或宿主机映射端口填入上述四个应用服务的 `DATABASE_URL` / `REDIS_URL`，也不要将工具凭据文件 `database-access.env` 用作应用的 `env_file`。**应用继续使用 `postgres:5432`、`redis:6379`；本地工具才使用下节的 Tailscale 地址。

### 本地工具通过 Tailscale 直连 PostgreSQL / Redis

客户端连接 Tailscale 后，使用下列参数，不配置 SSH 隧道：

| 参数 | PostgreSQL | Redis |
| --- | --- | --- |
| 主机 | `100.94.216.118` | `100.94.216.118` |
| 端口 | `25432` | `26379` |
| 用户 | `admin` | `default` |
| 数据库 | `enterprise_backend` | `0` |
| Schema | `enterprise_backend`，工具中选择该 schema | 不适用 |
| 密码 | 原 PostgreSQL 密码 | 本次新增的独立 Redis 密码 |
| 传输 | 普通 PostgreSQL TCP，当前未配置数据库 TLS | 普通 Redis TCP，当前未配置 Redis TLS |

连接流量由 Tailscale 加密。端口只绑定 `100.94.216.118`，可达性仍受客户端的 Tailscale 网络权限限制。账号拥有本项目写权限，仅为连通性检查时使用只读查询/PING，不修改业务数据，不清空 Redis；其中包含线上联调会话和 Celery 队列。

当前连接参数和凭据保存在服务器 `/srv/enterprise-backend/shared/database-access.env`，权限为 600。需要配置本地工具时，由运维用户在自己的终端查看：

```bash
ssh CANTAISYS@100.94.216.118 'cat /srv/enterprise-backend/shared/database-access.env'
```

该文件仅记录本地工具连接参数；应用实际运行配置由 Compose 基础文件、站点覆盖文件及 `shared/.env` 共同决定，Redis 服务端另外加载 ACL。工具记录中的 `POSTGRES_HOST` / `REDIS_HOST` 不用于应用内部寻址。数据库密码不等于 Swagger 的 `admin` 登录密码，勿把凭据复制到文档、普通验收报告或发布包。密码变更须同步维护实际配置及此记录；Redis 还须同步 ACL 摘要和四个应用服务的连接配置，不能只改 `.env` 或只重启 Redis。

本次从未映射端口切为直连时，短暂停本项目应用，备份并记录数据摘要，然后仅重建本项目 PostgreSQL、Redis 及需加载密码的 API、worker、beat，再启动 nginx。继续挂载 `enterprise-backend-int_pgdata` 和 `enterprise-backend-int_redisdata`；未迁移或清空数据，也未重跑管理员种子。以后只更新应用时沿用当前站点覆盖和 ACL，不重复此基础设施切换。

验收须包含本地工具的实际带密码连接、PostgreSQL 错误密码拒绝、Redis 无密码拒绝、应用 `/ready` 和 worker ping，以及 WES 操作前后容器基线对比。宿主机 TCP 探测不能替代数据库认证验证。

### 本次部署结果(2026-10-27)

| 项目 | 已核实结果 |
| --- | --- |
| 当前发布 | `integration-a1eccce7-amd64-20261027-01`，`current` 指向该发布目录 |
| 应用源 | `a1eccce7bf35fafb82ca408a07ff834d31324e36`，`source_dirty=false`，`linux/amd64`；属于联调候选，未发布新版本 |
| 数据库 | Alembic head 为 `a6d464b20ab7`，迁移成功；`alembic check` 无新增迁移操作 |
| 服务 | API、PostgreSQL、Redis healthy，worker ping 正常，beat 单实例，nginx 正常；migrate 为 Exited (0) |
| 入口变更 | 从 `127.0.0.1:18080` 改为 `100.94.216.118:18080`，只重建本项目 nginx |
| 内网入口补充 | nginx 新增 `10.24.199.219:18080` 绑定，保留 Tailscale 入口；两个地址的 `/ready`、`/docs`、`/openapi.json` 均返回 200，本地客户端访问两个 `/docs` 也均为 200；只重建本项目 nginx，其他本项目服务及 WES 9 个容器基线不变 |
| 接口验收 | 本次 local 的 14 项检查通过：标准 CookieJar 在 HTTP 下完成登录、自动携带 Cookie 刷新、退出及旧会话撤销；未登录设备查询和调试目录为 401，管理员授权查询为 200 |
| 当前环境与 Swagger | 联调切为 `local`，FastAPI 内置 Swagger 已在浏览器渲染，动态返回 51 个正式及 local 调试路径；入口 OpenAPI 与 API 容器响应一致，不再使用 36 路径的旧静态快照 |
| Swagger 配置部署 | 沿用原固定镜像，重建本项目 API、worker、beat 和 nginx；当次未执行迁移，PostgreSQL、Redis 和 WES 保持运行 |
| 数据库直连变更 | PostgreSQL `100.94.216.118:25432`、Redis `100.94.216.118:26379`；原 PostgreSQL 密码保留，Redis 新增独立密码，原镜像及命名数据卷不变 |
| 直连认证验收 | 本地客户端无 SSH 隧道完成 PostgreSQL 只读查询和 Redis PING；PostgreSQL 错误密码、Redis 无密码均被拒绝 |
| 数据保留 | 应用停止期间比对 PostgreSQL schema 内 19 张表，行数及内容摘要一致；Redis 校验标记经容器重建仍保留，验证后仅删除该自有临时标记，AOF 继续启用 |
| 变更前备份 | 已生成完整自定义格式 PostgreSQL 备份，`pg_restore --list` 通过；pg_dump 提示 TimescaleDB `continuous_agg` 循环外键警告，未执行恢复演练，不能据此宣称恢复已验收 |
| WES 保护 | 现有 WES 的 9 个容器 ID、镜像、启动时间、重启次数、健康状态和端口与操作前一致 |
| 管理员 | `admin`，邮箱暂用 `admin@localhost.invalid`；初始凭据在 `shared/initial-admin.env`，权限 600，文档不记录密码 |
| 联调登录有效期 | 新签发的 access token 为 3 天(4320 分钟)，refresh 会话保留 7 天；旧 token 不自动延长，须重新登录或刷新后使用新 token |
| WMS/Transport 目标 | `wms-transport`、`wms-facts`、`wms-decisions` 均配置到 `http://10.24.199.217:8285`；本次仅做 TCP 与根路径 HTTP 检查，未调用业务接口 |
| 连通性结果 | 宿主机和 API 容器 TCP 均成功，`GET /` 均返回 200，约 2 秒；`HEAD /` 在 5 秒内未响应，不据此判断业务接口状态 |
| 真实任务 | 配置前核对出站消息和 Transport 任务均为 0；设备和工作线目录为空，不提交真实任务或 ECS 命令 |
| 验证范围 | 构建源测试 2226 passed / 748 skipped，Ruff 检查通过；实际新库迁移及后端验收已完成 |

服务器记录保存在 `/srv/enterprise-backend/records/`：初次部署为 `integration-a1eccce7-amd64-20261027-01/`，直接访问变更为 `entry-18080-tailscale-20261027-01/`，已停用的静态 Swagger 为 `swagger-20261027-01/`，原生 Swagger 和 `local` 为 `fastapi-local-20261027-01/`，WMS 目标及连通性检查为 `wms-connectivity-20261027-01/`，数据库直连与认证为 `database-tailscale-20261027-01/`。后者含变更前配置、私有校验数据和 `backup/db-20261027-175322.dump`，目录权限 700；不得整目录复制到普通报告或发布包，只能单独提取不含凭据的验证结果。原始记录保留各次操作当时的状态，不因后续更新重写历史结果。旧导出脚本和静态文件仅归档，不再挂载或运行。配套前端、WMS 业务协议与真实 WMS/ECS 联动、宿主机重启恢复尚未验收。

内网网页入口补充记录在 `/srv/enterprise-backend/records/entry-lan-18080-20261027-01/`，保留原覆盖配置、容器基线和切换前后 HTTP 检查。其变更仅为 nginx 增加明确内网 IP 的映射，不开放数据库内网端口。

### 粗分双机的配置增量（2026-10-29）

本轮范围及验收步骤见[双机设计](superpowers/specs/2026-10-29-coarse-sorting-site-configuration-design.md)和[实施计划](superpowers/plans/2026-10-29-coarse-sorting-site-configuration.md)。上节 2026-10-27 的制品及验收记录保留为历史基线；本轮具体镜像、迁移及执行结果另存 `/srv/enterprise-backend/records/`，不覆盖旧制品或记录。

| 工作线 | INPUT_ARM | CONVEYOR | 出料单层货架位 OUTPUT_RACK |
| --- | --- | --- | --- |
| RS01 | RS-INPUT-ARM-01 | RS-CONVEYOR-01 | CLN01 |
| RS02 | RS-INPUT-ARM-02 | RS-CONVEYOR-02 | CLN02 |

ECS origin 为 `http://10.24.209.32:8080`，只读 Status 为 `/api/v1/device/status`。沿用原六台设备及路由，下料机械臂归属对应工作线，本阶段不增加其业务命令。工作线只声明 OUTPUT_RACK 位置，不配置输送线出入口或面向。具体配置通过管理员资产接口完成，使用当前 version 并保留审计；启动代码不自动创建现场绑定。

新命令附录为 `coarse_sorting.move_forward/2`。事件按设备的工作线角色分发，输送设备由同一工作线 CONVEYOR 绑定选择；Result 按原 command_code + device_code 核对，SUCCESS 继续要求 actual_qty 严格整数 1，Result.data 仅声明 actual_qty，不携带 location。业务成功仍止于等待箱格分配。

本轮不升级基础设施。新制品加载和备份完成后，只对 migrate、api、worker、beat 使用 `dc up --no-deps --no-build --pull never`；先等待唯一 migrate 成功退出，再启动三个常驻应用服务。原 PostgreSQL、Redis 容器、镜像及数据卷保留。nginx 仅在应用就绪后启动或重启以重新解析 API 地址，保留现有映射、共享配置及基础设施镜像。不运行会重新选择基础设施镜像的整套 `up`。校验新应用同一镜像、迁移与业务配置后才切换 current 指针。

### 联调 Swagger 的开放与更新

`ENVIRONMENT=local` 时，现有 FastAPI 自动提供 `/docs`、`/openapi.json` 和 `/docs/oauth2-redirect`；不新增文档开关，不改应用代码。页面和定义直接来自当前运行的 API，随应用更新自动变化，不再手工导出或维护静态定义。Swagger UI 的 JS/CSS 使用 FastAPI 默认 CDN，客户端需能访问该 CDN。

在包外 nginx 配置的 `server` 中保留下列反代路由，并移除旧的静态 alias 和 `/docs-assets/` 路由；同一站点保留 `/api/` 原反代，以及 `/ready`、`/health` 到 `http://api:8000` 的转发：

```nginx
location /docs {
    set $api http://api:8000;
    proxy_pass $api;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    add_header Cache-Control "no-store";
}
location = /openapi.json {
    set $api http://api:8000;
    proxy_pass $api;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    add_header Cache-Control "no-store";
}
```

应用更新并 healthy 后直接验证当前路由，不运行旧导出脚本：

```bash
dc exec -T api python -c "from app.main import app, settings; assert settings.ENVIRONMENT == 'local'; assert app.docs_url == '/docs' and app.openapi_url == '/openapi.json'; print('FastAPI local Swagger enabled')"
curl --noproxy '*' --fail --head http://100.94.216.118:18080/docs
curl --noproxy '*' --fail http://100.94.216.118:18080/openapi.json -o /dev/null
```

修改 `.env` 的环境值后须重建本项目 API、worker、beat，单纯 restart 不会应用新容器环境；只改 nginx 路由时先 `dc exec -T nginx nginx -t`，成功后 reload，移除旧目录挂载则须只重建 nginx。此流程不需要新镜像或数据库迁移。`local` 调试页的可用性不代表业务协议已验收；当前 WMS 目标是真实地址，连通性阶段不触发调试任务或请求。ECS 调试仍按地址、权限和合同单独约束。

### 联调登录有效期

本站点在包外 `.env` 设置 `ACCESS_TOKEN_EXPIRE_MINUTES=4320`，使 Swagger Authorize 使用的新访问令牌有效 3 天；刷新会话仍为 `REFRESH_TOKEN_EXPIRE_DAYS=7`。源码缺省的访问令牌有效期仍为 30 分钟，此覆盖只用于本联调站点。

更改有效期后，在当前发布目录加载 `shared/compose.sh`，执行 `dc up -d --no-deps --no-build --pull never --force-recreate api`，只重建签发令牌的 API。已有令牌携带原来的 `exp`，不会自动延长；重新登录后将新 `access_token` 填入 Swagger Authorize，或使用刷新接口取得新令牌。

访问令牌是无状态 JWT。退出登录撤销 refresh 会话，不会立即撤销已签发的 access token；配置为 3 天后，其最长可用窗口也相应变长。正式生产按生产会话策略设置，不直接沿用联调时长。

### 后续应用更新(联调服务器)

先在构建机从选定干净提交生成、验收新的 amd64 固定制品，上传到新的 `releases/<新发布标识>`，不要覆盖当前目录或标签。在服务器提前加载新镜像、核对基础设施引用与 `shared` 站点文件兼容，并记录本项目及 WES 容器基线。共享 `.env`、站点覆盖文件和 `shared/redis/users.acl` 沿用；它们不随新包覆盖，不更换项目名、数据卷或数据库凭据。加载配置前确认 ACL 存在、`.env` 保留 `REDIS_PASSWORD`；保留两个仅绑定 Tailscale IP 的数据库映射、nginx 的 Tailscale/内网双地址映射和四个应用服务的带密码 Redis URL，不能退回无密码基础配置。

更新前核对 API、worker、beat、migrate 的合并配置：`DATABASE_URL` 主机/端口必须为 `postgres:5432`，`REDIS_URL` 必须为 `redis:6379`，并保留数据库名、密码和 Redis DB `0`。不要将本地工具使用的 `100.94.216.118:25432` / `100.94.216.118:26379` 回填到应用配置，也不要将含密码的完整合并配置打印到普通日志。

停机前准备好新包、旧包和站点配置副本。在当前版本目录执行最终备份；成功校验并异机保存后再更新：

```bash
cd /srv/enterprise-backend/current
. /srv/enterprise-backend/shared/compose.sh
dc stop nginx api worker beat
scripts/backup_pg.sh
```

备份失败保持应用停止，先排除故障，不继续升级。下面 `<新发布标识>` 替换后，在新目录逐条执行，任一步失败即停止后续操作：

```bash
cd '/srv/enterprise-backend/releases/<新发布标识>'
python3 scripts/release_bundle.py load .
ln -s /srv/enterprise-backend/shared/.env .env  # 仅首次，已有链接先核对
. /srv/enterprise-backend/shared/compose.sh
dc config --quiet
dc run --rm --no-deps migrate alembic upgrade head
dc up -d --no-build --pull never api worker beat
dc ps api  # 确认 API healthy 后，才继续执行下面的检查
dc exec -T api python -c "import urllib.request as u; print(u.urlopen('http://localhost:8000/ready').read().decode())"
dc up -d --no-build --pull never
python3 scripts/release_bundle.py check . --project "$COMPOSE_PROJECT_NAME"
dc exec -T api alembic current
dc exec -T api alembic check
dc exec -T worker celery -A app.tasks.celery_app inspect ping --timeout=5
```

先确认 API healthy 再运行 `/ready` 和内置 Swagger 检查，不能把启动中的暂时失败当成已验收。联调更新继续保留 `ENVIRONMENT=local`；检查当前 `/openapi.json` 与当前 API 一致、Swagger 页面、HTTP 登录刷新退出、权限菜单、审计和队列，确认 WES 容器没有重启或端口变化。按新版本需要补种子，旧管理员密码不重置。全部通过后在同一新版本目录切换当前指针：

```bash
ln -sfn "releases/$(basename "$PWD")" /srv/enterprise-backend/current
```

保留旧制品、站点配置及最终备份。应用回退若涉及数据库恢复，使用下文“使用旧镜像回退”流程，不能仅切换 `current`；回退启动后验证 FastAPI 返回旧镜像的接口定义，无需导出文档。

### 快速修改候选入口与切换到 80 端口

候选端口只在包外 `/srv/enterprise-backend/shared/.env` 的 `NGINX_PORT` 修改，不改发布包、镜像、项目名或数据卷。例如从 18080 改成已确认空闲的 18081：

```dotenv
NGINX_PORT=18081
```

在当前运行版本的发布目录，使用前面定义的 `dc` 执行：

```bash
dc config --quiet
dc up -d --no-deps --no-build --pull never --force-recreate nginx
dc ps nginx
curl --noproxy '*' --fail --head http://100.94.216.118:18081/
```

只重建本项目 nginx，入口会短暂中断，API、worker、beat、数据库和 Redis 不重启，也不执行迁移。`restart nginx` 不会应用新端口映射。两条映射共用 `NGINX_PORT`，修改后可分别访问 `http://100.94.216.118:18081` 和 `http://10.24.199.219:18081`，都须先确认新端口空闲；Swagger 同步位于对应地址的 `/docs`，两个入口都要验证。同域相对路径无需调整文档，不需要 SSH 转发。

已有 HTTPS 网关也须同步更新上游地址。验证失败时将 `.env` 改回旧端口，再用同一命令重建 nginx；不要重建整套应用。前端 HTTP 检查不能替代容器内 `/ready` 和登录验收。

改成 80 时同样设置 `NGINX_PORT=80`，但必须先解决占用：2026-10-27 检查时 `wes_nginx_int` 已绑定 `0.0.0.0:80`，因此本项目连 `100.94.216.118:80` 也不能同时绑定。可选择：

- 保留 WES 的 80/443，由现有网关按本项目独立域名转发到候选端口。此时本项目 `NGINX_PORT` 继续为 18080 等独立端口；两个域名分别提供各自前端和 `/api`，避免同路径混流。修改现有网关前须确认 WES 影响并完成配置检查，再安排 reload。
- 经协调将 WES 的入口迁到其他端口/IP，确认原 80 端口已释放，再将本项目 `NGINX_PORT` 改为 80 并只重建本项目 nginx。不要直接停止 WES 来抢占端口。

现有 WES nginx 在容器内，其 `127.0.0.1` 指向该容器，不能配置成访问本项目的上游。选择网关转发时须验证它到 `100.94.216.118:18080` 的可达性和来源限制；需要专用入口网络时单独设计，不合并两套应用的数据库网络。

网页映射绑定 `100.94.216.118` 和 `10.24.199.219`，不绑定所有网卡；只有第一个地址经 Tailscale，内网地址使用普通 HTTP。若需其他网卡访问，在包外覆盖文件中配置明确的服务器 IP，并核对防火墙、来源限制及 HTTPS 方案；只改 HTTP 端口不会启用 TLS。`local` 是本次联调选择，正式生产不得沿用该设置替代 HTTPS。

### WMS 回调接收启用门槛

`POST /api/v1/wms/events` 已实现通用接收，并在所有环境自动注册 `transport.task.member_position_changed@v1` 与 `transport.task.resulted@v1`；示例 `example.echo@v1` 仅在 `ENVIRONMENT=local` 注册。合法但未注册的 operation 返回持久化的 `422 REJECTED / UNSUPPORTED_OPERATION`，不是临时失败；此前已保存的不支持响应仍按原消息身份稳定重放。代码具备 Transport 接收能力不等于所有部署实例已升级或真实回调已启用。

在鉴权方案落地前，网关必须限制 WMS 来源，禁止向公网无保护开放该接口。完成设计第 3 步并确认所有实例已加载实际 Transport 接收能力、所有任务创建者(API、worker、脚本)均已升级之前，不得启用真实回调；滚动升级时先暂停网关回调入口，全部实例就绪后再恢复，避免旧实例将合法业务消息永久记录为不支持。详细协议和后续启用条件见[回调接收设计](superpowers/specs/2026-10-22-transport-callback-design.md)。

请求体上限为 256 KiB；独立诊断快照最多保存 64 KiB 的 base64 内容，按进程限量、尽力写入，不影响协议响应。`CALLBACK_SNAPSHOT_RETENTION_DAYS` 默认为 7，必须为正整数，由现有 Celery beat 每日清理过期快照；幂等收据不随快照清理,而是按 `RELIABLE_RETENTION_DAYS`（默认 90 天）由 beat 每日清理。`TRANSPORT_STUCK_ALERT_MINUTES`（默认 120，必须为正整数，按现场 SOP 调整）：beat 每 10 分钟扫描创建后超过该时长仍无确定终态的 Transport 任务，输出结构化告警日志 `transport task stuck`（含 `transport_task_id`、`cycle_id`、`reason`、`waiting_minutes`），不改任务状态；`reason` 为 `SUBMIT_NOT_DELIVERED`（提交已过期或被拒）、`RESULT_MISSING`（已提交无结果）、`RESULT_WAITING_FOR_POSITIONS`（料箱成功结果等不到位置事件）、`OUTCOME_UNKNOWN`。仍卡住的任务每次扫描都会重复告警，直到结果到达或任务创建超过 7 天（视为已搁置，不再告警）。上线前先迁移数据库，非空收据表禁止降级删除，以免丢失消息身份及首次响应。

### Schema 隔离

PostgreSQL 固定使用 `enterprise_backend` schema；数据库名称与 schema 是两个独立概念，`DATABASE_URL` 仍连接原数据库。`app.database.create_database_engine` 统一给 API、可靠发送器及各 Celery 数据库连接设置会话 `search_path=enterprise_backend`，ORM 元数据也显式限定 schema。没有新增环境变量，不要对共享数据库执行全局 `ALTER DATABASE/ROLE ... SET search_path`。SQLite 测试通过 schema 映射继续使用无 schema 表。

在线 Alembic 入口在同一事务中校验旧库归属、创建 schema、移动现有表，再运行历史迁移，版本表同时移入新 schema。历史 revision 不重写，schema 隔离的搬迁基线仍为 `a023a20261024`；本次部署 head 为 `a6d464b20ab7`，后续发布以 `uv run alembic heads` 核对。迁移白名单为 `user`、`role`、`permission`、`user_role`、`role_permission`、`menu`、`audit_logs`、`audit_logs_archive`、`reliable_message`、`transport_task`、`callback_receipt`、`callback_snapshot`，另含 `alembic_version`。`ALTER TABLE SET SCHEMA` 原地保留数据及对象身份，所属索引和序列随表移动。

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

最近发布基线 `v0.2.0` 的范围为后端模板，其已发布固定镜像验收范围为 `linux/arm64`；发布说明及兼容变化见 [CHANGELOG](../CHANGELOG.md)。本次 `linux/amd64` 联调候选的验证见“本次部署结果”，不改变旧发布制品的验收范围。当前分支的改名及 HTTP 客户端等后续变更尚未发布。

剩余验收边界:
- 没有配套前端,真实管理端浏览器联调待验收;接口与 Cookie 的后端验收不能替代真实浏览器联调。
- 独立目录复制不等于真实异机传输。生产站点须完成异机保存、取回、摘要及权限检查,并备齐实际旧镜像、配置与可接受的恢复点;验收快照回退不能替代该站点旧版本回退准备。
- 服务进程异常重启已验收,宿主机或 Docker 守护进程重启后的整套恢复仍待验收。
- 实际站点 TLS、可信代理、密钥、目录权限及 Compose 项目名须在上线前核对;离线交付须额外保存基础设施镜像。

这些待验收项不应写成已通过,也不能用模板版本发布代替生产交付验收。正式发布前须固定最终源码提交与制品,验证 `source_dirty=false` 并验收实际交付包;已有候选包的验收不自动覆盖重新构建的镜像。
