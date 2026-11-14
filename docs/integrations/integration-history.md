# ECS / WMS 对接历史

## 三张 HTTP 往来表启用后的查询

启用独立运行证据后，日常查询使用 `GET /api/v1/integrations/diagnostics`。传入 `peer` 和一个原编号 `reference`。WMS 用 `operation_id`，ECS 用 `command_code`，Transport 用 `transport_task_id`。即使还没有 HTTP 往来，入口也会查询原运行状态。

### 启用新写入

先载入[联调 Compose 配置](../deploy.md#固定镜像部署与升级)，沿用原项目名和数据卷。维护窗口前，在新包目录检查固定镜像，留存旧包、共享配置和当前镜像 ID。预先确认数据库版本。已完成此前统一历史交接、停在 `coarse_sorting_mock_round` 的数据库可以执行本节。更早的版本按[历史迁移与制品 A](#历史迁移与制品-a)先完成所需交接。

```bash
cd "/srv/enterprise-backend/releases/<新包目录>"
python3 scripts/release_bundle.py load .
ln -s /srv/enterprise-backend/shared/.env .env  # 已有链接时核对指向
. /srv/enterprise-backend/shared/compose.sh
dc config --quiet
dc config --images
```

在当前发布目录关闭上游入口，暂停清理、补录、人工写入等所有旧写入者，再停服务。确认发送器已随 API 停止、原进程均已排空、没有外部或预备事务。备份用绝对路径，在异机存储中保存归档和摘要，并确认可取回。

```bash
cd /srv/enterprise-backend/current
dc stop nginx api worker beat
dc ps -a
dc exec -T postgres psql -X -U admin -d enterprise_backend -v ON_ERROR_STOP=1 \
  -c 'SELECT version_num FROM enterprise_backend.alembic_version' \
  -c "SELECT pid, application_name, state FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND backend_type='client backend' AND (xact_start IS NOT NULL OR state IS NULL)" \
  -c 'SELECT gid, prepared FROM pg_prepared_xacts WHERE database=current_database()'
backup="$(BACKUP_DIR=/srv/enterprise-backend/shared/backups/http-exchange scripts/backup_pg.sh)"
test -s "$backup"
dc exec -T postgres pg_restore --list < "$backup"
sha256sum "$backup"
```

保持停写和原共享配置，在新包目录执行最终对账。每条命令成功后才执行下一条。

```bash
cd "/srv/enterprise-backend/releases/<新包目录>"
dc run --rm --no-deps migrate alembic -x fence_legacy_writes=true upgrade http_exchange_initiator
```

迁移在同一事务核对冻结请求、原租约、回执、ECS 裁决及 Transport 结果来源，安装旧表写入围栏并替换查询视图。失败时保留原版本和旧表；继续停写，按错误给出的原身份调查，确认后重跑。未决旧租约缺少开启记录且无法证明未发送时，按原编号核对对端并保全交付事实。Transport 缺少结果来源时，只有此前留档能证明该结果原本就缺少来源，才按历史未知迁移。禁止填造记录、换号重发和手工改迁移版本。普通 `upgrade head` 会拒绝直接切换有存量的数据库。

已处于 `http_exchange_query_views` 的环境也在停写窗口执行同一条升级命令。新增迁移把已有 HTTP 记录的 `OUTBOUND` 映射为 `WES`、`INBOUND` 映射为 `PEER`，保留原编号和正文。旧三表已删除的环境继续使用 `INTEGRATION_RUNTIME_AUTHORITY=true`。恢复旧镜像时，在停写窗口先用新包执行 `alembic downgrade http_exchange_query_views`，把三张表和两张视图恢复到旧镜像能读取的形态，再启动对应旧版 API 和 worker。

迁移成功后，在共享 `.env` 写入 `INTEGRATION_RUNTIME_AUTHORITY=true`。启动 API 即可能恢复发送与新写入。从启动此步骤起保留新产生的事实，并用实际业务任务验证 worker。确认原收据重放、待办恢复、三类原编号查询、正文权限及审计后，再启动 nginx、切换 `current` 并恢复入口。

```bash
dc up -d --no-build --pull never --force-recreate --wait --wait-timeout 90 api worker beat
dc exec -T api python -c "import urllib.request as u; print(u.urlopen('http://localhost:8000/ready').read().decode())"
python3 scripts/release_bundle.py check . --project "$COMPOSE_PROJECT_NAME"
dc exec -T api alembic current
dc exec -T api alembic check
dc exec -T worker celery -A app.tasks.celery_app inspect ping --timeout=5
# 完成实际队列任务和业务验收后执行
dc up -d --no-build --pull never nginx
ln -sfn "releases/$(basename "$PWD")" /srv/enterprise-backend/current
```

已提前推进到无围栏的最终版本时，重跑指定迁移会失败。使用切换前备份恢复原版本前，须确认恢复点之后未产生新事实。已经产生新事实时，先完整保全现场数据库和对应镜像，再按下文恢复。

原 `/integrations/exchanges`、详情、`/records` 时间线和正文路径继续使用原权限。列表以一次 HTTP 尝试为一条，时间线给出请求及可确认的响应两侧。两侧标识由往来 UUID 派生，在往来保留期内稳定。正文路径可以使用时间线返回的 ID，也可用 `/integrations/http-exchanges/{peer}/{exchange_id}/{side}` 并以 `representation=payload` 查看结构化数据。原过程历史及旧正文标识省略补迁。

三张 HTTP 往来表、单编号诊断、历史列表和详情使用 `initiator` 表示 HTTP 请求发起方。WES 发起取 `WES`，对端发起取 `PEER`。列表使用 `initiator=WES` 或 `initiator=PEER` 筛选。两张视图和证据时间线都使用该字段，请求和响应的 `initiator` 相同，用 `side` 区分请求和响应。

`integration_exchange_summary` 与 `integration_record_summary` 保留原名称并继续排除正文和 payload。第一张视图按往来提供原编号、交付与应用状态，第二张按请求和响应提供正文可用性、摘要与长度。Transport 的任务编号在 `transport_task_id`，ECS 的命令编号在 `command_code`。查某个设备的 ECS 事件可按 `device_code` 和时间过滤。运行状态、HTTP 状态和本次响应各自表达对应事实。

普通数据库只读角色通过这两个视图查询。部署时核对该角色的继承权限和默认授权，确保它无法直接读取包含正文的往来表或运行表。

### 旧三表删除窗口

旧表写入围栏及查询迁移须已完成。验收证据 JSON 包含 `isolated_acceptance`、`integration_acceptance`、`running_version`、`backup` 的实际记录编号。将 `api`、`sender`、`worker`、`beat`、`background`、`one_off`、`manual`、`database_transactions` 逐项核对为 `DRAINED`，将 `cleanup`、`backfill`、`scripts` 逐项核对为 `RETIRED`。未确认项会阻止删除。

在独立删除窗口，用当前固定发布镜像挂载已核实的证据文件。预检在保存点内试行同一组 `RESTRICT` 检查并回滚。数据库依赖可自动阻止删除，字符串 SQL、人工脚本和外部调用由操作者核对留证。预检通过后，实际删除命令再次检查准入并在同一事务删表和留证。

```bash
dc run --rm --no-deps -v /absolute/path/evidence.json:/tmp/evidence.json:ro migrate \
  python -m scripts.retire_http_exchange_legacy --evidence /tmp/evidence.json
# 取得本次独立删除窗口的实际验收证据后执行
dc run --rm --no-deps -v /absolute/path/evidence.json:/tmp/evidence.json:ro migrate \
  python -m scripts.retire_http_exchange_legacy --evidence /tmp/evidence.json --apply
```

### 切换后恢复

保留含新运行证据的备份与同一发布包。先在隔离库用它们恢复并验收。现场需要恢复时，再关闭入口并排空全部写入者，保全待恢复的原现场，核对备份的摘要、恢复点及对应镜像，并在共享 `.env` 轮换 `JWT_SECRET_KEY`。数据库和 Redis 必须属于原 Compose 项目。在对应发布目录运行：

```bash
dc stop nginx api worker beat
# 核对恢复点与目标 Compose 项目后执行
JWT_ROTATED=yes CONFIRM=yes scripts/restore_pg.sh "/absolute/path/db-<恢复点>.dump"
# 有新运行证据的备份使用共享 .env 内的 INTEGRATION_RUNTIME_AUTHORITY=true
dc run --rm --no-deps migrate alembic upgrade head
dc up -d --no-build --pull never --force-recreate --wait --wait-timeout 90 api worker beat
dc exec -T api python -c "import urllib.request as u; print(u.urlopen('http://localhost:8000/ready').read().decode())"
python3 scripts/release_bundle.py check . --project "$COMPOSE_PROJECT_NAME"
# 完成原收据、原租约补写、视图、调试接口和实际队列任务验收后执行
dc up -d --no-build --pull never nginx
```

恢复脚本会清理原 Redis 登录会话及待执行和未确认队列任务。如果数据库已切换、Redis 清理报错，保持服务停止，修复后单独执行 `scripts/clear_redis_after_restore.sh`。已产生新受理或裁决时，直接恢复切换前备份会失去这些新事实。恢复切换前备份的旧模式路径须使用其原发布镜像及对应配置，详见[旧镜像回退](../deploy.md#使用旧镜像回退禁止现场重建)。

## 切换前的统一历史合同

以下各节保留旧统一历史结构的查询和运维合同，适用于切换前三张旧表仍承担运行权威的版本。启用新存储后的操作见本文开头。

此前统一历史模型为 `IntegrationExchange`（原交互与状态）、`IntegrationRecord`（原请求、回复、发送及采用证据）和 `IntegrationReference`（可证明的关联），加上保留执行职责的 `ReliableMessage` 与 `TransportTask`。原命令、收据、发送/Result 证据和诊断快照统一写入前三类；旧 ORM 只在迁移来源与制品 A 中保留。统一存储不改变原协议身份、幂等、冲突检测或长期 ECS Result 重放。

应用在 `app/reliable_runtime.py` 注入 `app.integrations.history` 作为 `Outbox(..., records=...)` 和 `MessageStore(..., records=...)` 的同一记录端口；独立运行时也须显式注入。可靠层通过端口保存证据，不导入协议或历史 ORM；原文由证据指针读取，不再从 ReliableMessage 的旧原文列取值。独立装配示例见[离线实验](../reliable-transport-lab.md)，业务仍使用公开结果读口。

[设计](../superpowers/specs/2026-11-06-integration-history-unification-design.md)与[实施计划](../superpowers/plans/2026-11-06-integration-history-unification-implementation.md)保留设计阶段的决策和任务；当前软件结果以[验证记录](../testing/integration-history.md)为准，历史计划不构成部署或现场验收证明。

ECS 未注册事件继续按原合同返回 ACK。事件名称不能安全投影为数据库文本时，`message_type` 为空，不能按该字段筛选；原请求字节、原事件身份及诊断不被改写，可按原身份或设备检索。已受理事件仍优先返回原回复，不因查询字段的存储限制重新裁决。

## 查询接口

| 接口 | 用途 | 权限 |
| --- | --- | --- |
| `GET /api/v1/integrations/exchanges` | 按原身份、明确引用、对端、方向、operation、设备和时间筛选交互实例 | `integration:history:read` |
| `GET /api/v1/integrations/exchanges/{id}` | 原实例摘要、证据指针与双向关联 | `integration:history:read` |
| `GET /api/v1/integrations/exchanges/{id}/records` | 原实例的分页时间线 | `integration:history:read` |
| `GET /api/v1/integrations/records/{id}/body` | 单独读取已保存字节 | `integration:payload:read` |

新内部主键为 UUIDv7。原 command_code、数值 receipt_id、operation_id、message_id、lease_id、业务键和摘要不改写。历史格式由 schema_version 标识；查询时间输出为 UTC 的 ISO 8601 Z，原协议的毫秒时间及原始字节仍保留其合同。

列表每页默认20、最多100，按 created_at / id 倒序；时间线按 recorded_at / id 倒序。返回 items、next_cursor；后页保留原筛选，仅替换 cursor。游标最长256字符，并绑定端点、筛选及明确时间参数。默认最近90天的上界保存在首个游标内，后续翻页不随时钟移动。一次详情读取使用独立的只读 REPEATABLE READ 会话；分页请求之间不承诺冻结整个数据集。

普通时间查询使用 from_at 与 until_at，必须同时提供，跨度大于零且不超过90天。时间条件位于结构化筛选的外层 AND，OR 不能扩大窗口。指定精确 identity_value 或单个消息、任务、动作的完整类型引用且未给时间时，覆盖该身份的合法留存期，返回每一个原实例。工作线等分组检索仍使用普通时间窗口。结构化 filter 复用项目的字段白名单，不能筛选原文或完整路由配置。

## 明确引用

引用查询同时提供 reference_kind、reference_namespace、reference_value。值命中摘要索引后仍比较完整原值，多个引用不会使交互分页出现重复行。查询同时包含经明确 EXCHANGE 引用直接关联的相邻实例，不作递归或按时间拼接。

| reference_kind | namespace | 原值 | 含义 |
| --- | --- | --- | --- |
| RELIABLE_MESSAGE | id | 原整数 message_id 的十进制文本 | 原出站消息，即使运行行已清理也可定位历史 |
| TRANSPORT_TASK | id | 原 transport_task_id | 原提交及已严格匹配的回调 |
| WORKLINE | id | 原数据库工作线整数的十进制文本 | 已确认的事件绑定或业务动作所属工作线 |
| WORKLINE | code | 原 TransportCaller.workline_id | 协议调用者原工作线编号，与数据库整数分开 |
| BUSINESS_TASK | coarse_sorting | 原 scan/job ID | 原扫描、决策、命令及可证明关联的结果 |
| BUSINESS_PREPARATION | coarse_sorting | 原 preparation ID | 共享货架准备的原事实 |
| BUSINESS_ACTION | 原 owner_domain | 原 action_id | 原意图及真实消费记录 |
| EXCHANGE | integration | 原 exchange UUID | 原实例之间可证明的关联 |

UNKNOWN_TASK、无法证明归属的旧快照和单凭 trace、条码、设备或相近时间的观察不拼接为业务事实。详情同时展示正向和反向引用，最多100条，超出时 references_truncated=true；有来源记录的关联可在分页时间线中继续追查。其他业务尚未接入的消费阶段为 UNKNOWN / 未采集，不从后续命令倒推成功。

## 分开理解结果

交付、协议结果和业务应用分列，系统不计算一个覆盖所有层次的“统一成功”。HTTP 200、ACK、WMS NG、Result 已采用和业务动作已消费分别保留原来源与版本。缺少原采用证据时，应用状态为 UNKNOWN，原存储状态可作为 recorded_state 供核对。

RESPONSE_PREPARED 表示服务器准备的协议回复，不证明对方已经收到。核心回复标为 PROTOCOL_PREPARED，本地调试的诊断标为 PROTOCOL_INNER，不把调试外层响应当原协议 wire。迟到响应保留自身记录，不能覆盖新租约的运行状态。

列表与详情不读取或返回原文、effective_payload、输入副本或完整路由。正文通过独立权限读取，固定 application/octet-stream，不作为 HTML 渲染。响应头 X-Integration-Body-State、X-Integration-Body-Sha256、X-Integration-Stored-Bytes 描述实际保存的字节。确认空字节返回200、长度0；记录不存在或正文不可用返回404，已存在记录的 detail 是固定 body_state。时间线继续显示摘要与原因。

| body_state | 含义 |
| --- | --- |
| FULL | 已采集的完整字节，可包含确认空字节 |
| PREVIEW | 仅有已保存前缀，不能当完整报文 |
| LEGACY_REENCODED | 旧结构化内容重新编码，不冒充原 wire 字节 |
| UNAVAILABLE | 无法证明曾采集或当前无法提供 |
| PURGED | 已按保留合同清空正文，摘要与长度仍可查询 |

成功正文读取单独写 AuditLog，只记录操作者、record UUID、固定路由及 trace；resource_id 保持 NULL。审计入口不接收正文，审计失败不改变已经准备的正文回复。普通摘要 GET 继续不审计，权限撤销在下次请求生效。

## 保留与上线边界

普通摘要与索引默认90天，诊断原文沿原来源默认7天。原收据受理资格和可靠终态沿原90天时钟，ECS 原调用唯一、Result 冲突检测、长期重放及未决恢复证据继续受原合同保护。历史存在不占据已经到期释放的受理资格，也不让运行 get_result 在运行证据到期后伪装 FOUND。

迁移、最终停写对账、来源清理保护与独立收缩的命令见下文。隔离软件验证见验证记录；本页不代表已经完成生产切换或现场设备验收。

## 历史迁移与制品 A

当前改造只能在受控维护窗口统一交接。准备阶段仍由旧程序与旧表承担权威，不能把仅写统一存储的新 API 或 worker 提前接入。保留原镜像、配置和可恢复的数据库备份，并先在隔离副本用实际数据测量窗口。停进程不代表设备没有执行；原在途和 UNKNOWN/HELD 保持原身份与冻结期限。

窗口顺序：

1. 停止并核对所有旧 API 副本及其 Sender、worker、唯一 Beat、清理、后台、one-off、人工写入口和旧数据库事务。核对清单包含不可自动发现的部署及人工入口；缺项保持 UNKNOWN。
2. 仅用新镜像的迁移命令升级至 `integrationhistoryexpand`，不启动新 API/worker。默认全部搬迁在此停写窗口执行。若预补录，必须先停用并排空旧清理及人工删除，仍由旧程序受理；最终窗口仍完整重扫。
3. 执行 `uv run python -m scripts.backfill_integration_history copy --source all --batch-size 500 --state /受保护目录/history-state.json`。连接由原部署的 `DATABASE_URL` 提供。来源按固定依赖顺序复制，批次提交后原子保存进度；提交后丢失进度可重复执行，不产生发送或业务推进。冲突批次不推进游标，保留旧来源。
4. 操作者准备 `writer-evidence.json`：`api`、`sender`、`worker`、`beat`、`background`、`one_off`、`manual`、`database_transactions` 均明确填 `DRAINED`/`RUNNING`/`UNKNOWN`，`attestation` 填现场证据编号（字母数字及 `-_.`，最长 128 字符）。`DRAINED` 是操作者的库存核对结论，不能根据脚本没有发现进程填写。
5. 执行 `uv run python -m scripts.backfill_integration_history verify --phase final --state /受保护目录/history-state.json --writer-evidence /受保护目录/writer-evidence.json`，留存安全 JSON 报告。只有退出码 0 且 `eligible=true` 才可继续。最终阶段从每个来源的起点重核可变行，接续旧实际序列、原整数及 JSON 事件引用的高水位；PostgreSQL 另查其他活动事务并以 NOWAIT 来源锁固定停写快照。任何未知 writer、旧事务、身份、版本、字节或引用不符均停止切换。
6. 在同一停写窗口升级至制品 A 的 `integrationhistoryauthority`。迁移再次执行冻结版本的原身份、来源快照版本、原字节/摘要/长度、收据整数、采用状态及原序列检查。旧表与旧重复原文仍保留。
7. 一次启动新 API、worker、唯一 Beat，按原身份检查创建、发送、回调、消费、重启及清理，记录设备/对端另行验证的结果。已有新事实后不能直接重启旧二进制；需要保全新事实的恢复适配或前向修复。

脚本 `copy`/`verify` 在 PostgreSQL 以单协调者 advisory lock 互斥，SQLite 批次用 BEGIN IMMEDIATE。`precopy` 报告不能宣告切换。检查点只含来源、游标、计数和格式版本；报告不输出正文、SQL 或连接串。旧 base64 始终仅作预览；非法编码保留为 UNAVAILABLE 并阻止收缩，修复必须留存来源证据，不能伪装空的完整报文。旧 JSON 回复标记 LEGACY_REENCODED；相同 trace 的快照保持独立，不能据此关联业务。

`release_bundle.check` 只证明所选 Compose 制品及服务，与上述独立 writer 库存和数据库最终对账分开留证。独立收缩属于后续制品 B；不能把其迁移预装进制品 A 的默认 head。

## 保留与清理

沿用 `callback.cleanup_snapshots`、`callback.cleanup_receipts` 和 `reliable.cleanup_terminal`。原收据清理任务在释放资格后调用普通历史清理；没有新增发送器或维护调度器。`INTEGRATION_HISTORY_RETENTION_DAYS` 默认 90，诊断正文仍默认 7 天，收据及可靠终态继续使用原 90 天合同。

清理先轻读候选，再合并相关工作线，按编号取得原业务锁、重读保护事实；之后才锁交互与证据，并核对本批发现后的新增引用。未知责任、未决业务、canonical/predecessor/当前锚点、仍需的原消息或共享正文引用均保留，批次继续向后处理。ECS 原命令及 Result 核心恢复证据没有新增统一自动到期。

正文到期转为 PURGED，保留摘要、原字节长度及清理时间；未知或从未采集的正文保持 UNAVAILABLE。普通历史按原事实时间处理，补录入库时间不续期；旧交互若有新的真实观察或裁决，其历史仍可保留，但原受理资格与必要正文不因此自动续期。已释放资格的整数事件在原运行读口返回 NOT_FOUND，历史查询可在合法窗口内继续看到旧实例。

扩展阶段未交接权威时，新清理入口拒绝清理迁移来源。旧诊断副本只有在来源版本、摘要和长度与已复制证据一致时，才随到期诊断正文清理；来源变更、非法编码或无法核验时保留。旧收据副本随已满足原保护条件的历史实例清理。历史中存在旧表、检查点或补录标记不代表能够重启旧程序；离线补录写入口在权威切换后拒绝继续覆盖运行数据。


## 只读 SQL 摘要

使用 `integration_exchange_summary` 和 `integration_record_summary` 直接查询统一格式。两者显式排除正文、完整路由、输入和业务 payload；交付、协议、业务应用与 coverage 分列。数据库视图本身不创建数据库账号或授权，运维应使用已配置的只读访问角色。运行关联不唯一时保留 UNKNOWN，不任取一个运行行。

原文到期后，已保存的安全协议 code、摘要和旧可靠消息尝试计数仍可查询。旧计数只代表来源快照，不补造逐次 HTTP 事实。开发环境软件及成本证据见 [验证记录](../testing/integration-history.md)。


## 独立收缩与制品 B

此前统一历史的第二阶段止于 `integrationhistorycontract`。本次 HTTP 往来表制品止于 `http_exchange_initiator`。执行前一阶段时须使用当时冻结的发布镜像。先使用已冻结且 head=authority 的 A，实际启用并完成原身份贯通验证，再在另一个维护窗口交付 B。开发测试中的成功不代替现场的 A 验收。

B 的收缩命令：

```sh
uv run alembic -x integration_history_contract_evidence=/受保护目录/contract-evidence.json upgrade integrationhistorycontract
```

非空数据库必须提供有界的 JSON 证明文件。它包含原八项 `api`、`sender`、`worker`、`beat`、`background`、`one_off`、`manual`、`database_transactions`，全部值为 `DRAINED`；另有 `attestation` 和 `authority_acceptance`，分别指向本次实际写入者核对与 A 实际验收证据编号。两个编号限1～128字符，仅字母数字及 `-_.`。缺项、未知写入者、额外活动数据库事务或未知旧表外键均拒绝收缩。文件是操作者的事实声明，数据库不能代替操作者证明所有旧二进制已退出。全新空数据库可直接安装最终结构。

收缩使用独立冻结的 v2 校验：旧来源身份、原文、原摘要和基线证据必须存在且一致；当前在途引用、原命令冻结请求、当前采用结果与接纳证明另行校验。切换后合法的新 ACK、收据重检、终态变化与新裁决不与已退休旧缓存作状态相等比较。已合法清理的诊断保留 PURGED 的摘要、长度与清理证明；不伪造可用原文。

只有全部校验通过才删除五个旧表和 ReliableMessage 的三列原文，并把 Transport 原接纳证明中的两段重复原文改为引用。原规范化业务事实、原整数事件 ID、统一原生分配器及长期 ECS 恢复证据保持。最终对接实体为三类统一历史，加原 ReliableMessage 与 TransportTask，共五个。

非空最终存储拒绝自动降级；它不会从旧表缓存重建新事实或提供旧镜像回退方案。空结构降级仅用于可丢弃测试数据库的迁移回归，不是生产恢复步骤。生产故障应保全当前事实，使用兼容统一存储的恢复版本或前向修复。此分支未执行生产收缩或现场验收。
