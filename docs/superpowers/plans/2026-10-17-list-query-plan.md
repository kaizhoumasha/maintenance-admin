# SPEC-018 实现计划与验收记录:测试矩阵、失败模式与任务

对应设计:`docs/superpowers/specs/2026-10-17-list-query-design.md`(契约以 SPEC 为准)。第 1–3 节保留实现前的评审记录,其中 `计划` 表示当时拟实现的路径;S1–S7 完成状态见第 4 节,实际验证结果见第 6 节。

## 1. 数据流与测试覆盖图

以下均为拟实现路径；`计划`不是“已通过”。现有权限、用户分页和 GET 不写审计测试须保留,不宣称未来筛选代码已有覆盖。

```text
controllers/users.py、controllers/audit.py
  权限依赖 -> 未登录 401 / 无权限 403                  [已有,保留]
  完整 Query 模型 -> 默认 / 合法组合 / 越界 400        [计划,D2]
    三个列表未知参数 -> 400；共享 PageParams 不变     [计划,D6]
    |
    +-> schemas/query.py::parse_filter(raw)
    |     None -> 无筛选                              [计划]
    |     空串/JSON null/非组/坏 JSON/过长 -> 400       [计划]
    |     FilterGroup/FilterCondition
    |       未知键/混合节点 -> 400                    [计划,D4]
    |       空组/not 非单项/深度超过 3 -> 400           [计划]
    |       合法递归树 -> services                    [计划]
    |
    +-> services/filtering.py::apply_filter
    |     无组 -> 原 stmt                             [计划]
    |     and/or/not -> 递归构造条件                   [计划,含 NULL 三值逻辑]
    |     字段/算子不在白名单 -> 固定原因 400          [计划]
    |     值形状 -> 标量 / 非空 in / 两端 between      [计划,含拒绝分支]
    |     值类型 -> StrictInt / StrictBool / 字符串    [计划,范围/NUL]
    |     ilike -> 模式+转义校验 -> 绑定参数           [计划,D3,PG 集成]
    |     datetime -> 字符串+时区 -> UTC              [计划,PG 集成]
    |       解析或归一化溢出 -> 固定原因 400           [计划,D5]
    |     role_id -> 相关 EXISTS                      [计划,避免重复用户]
    |
    +-> user_service::list_users -> paginate
    |     筛选后 COUNT -> 0 则空 items / 否则 offset   [原逻辑,新增筛选组合回归]
    |     Page[UserRead] -> 无密码哈希                 [原契约,保留]
    |
    +-> audit_service 两个列表 -> paginate_cursor
          cursor 无/有 -> 解码、JSON 对象、严格 id     [计划]
          编码/形状/范围错误 -> 400                  [计划]
          白名单 WHERE AND id < cursor               [计划]
          ORDER BY id DESC LIMIT limit+1,无 COUNT     [计划,SQL 断言]
          0..limit 行 -> next_cursor=null             [计划]
          >limit 行 -> 截断,编码最后返回行 id         [计划]
          热表/归档各绑定自己的列                    [计划,隔离数据集]

用户流程:筛选 -> 连续翻页 -> 末页                     [计划,HTTP 集成]
错误流程:修正输入后重试 -> 200,响应/日志不含输入值    [计划,HTTP+caplog]
GET 成功/失败 -> 审计记录数量不变                     [原测试扩展]
新高 id 插入/热表记录归档 -> 后续页不重复             [计划,PG 集成]
```

测试文件及可执行断言(拟新增文件按既有 `test_*.py` 命名):

| 文件 | 必须验证 |
|---|---|
| `tests/services/test_filtering.py`(新) | 8 个算子、3 个组合的结果；空 `conditions`、not 0/2 项、深度 3 通过/4 拒绝、未知键与混合节点；非法根和空字符串；`in` 0/1/100/101 项与含 null；`between` 长度 0/1/2/3、端点类型/null；整数拒绝 true/浮点/数字字符串,布尔拒绝 0/1/字符串；datetime 拒绝无时区、仅日期、数字类型和数字字符串(`"1700000000"`、`"1700000000.5"`、`"1700000000000"`、`"20261017"`)，接受 `Z` 与 `+08:00` 并统一转 UTC；2048/2049 字节边界(含用汉字构造的多字节用例)；错误不回显值；未知和禁止字段消息一致 |
| `tests/services/test_pagination.py`(新) | 空结果、少于/恰好/超过 limit、limit=1/100、游标指向最后返回行；坏 base64/UTF-8/JSON、非对象、缺失/null/bool/字符串/越界 id；多页与筛选交集不重不漏；捕获目标表查询 SQL,断言游标路径不含 COUNT/OFFSET,不把权限查询算作分页查询 |
| `tests/controllers/test_pagination.py` | 保留用户 offset/total 和密码哈希不返回测试；移除“两类列表形状相同”的旧契约；分别断言审计 CursorPage 和用户 Page；D2 的参数组合及 OpenAPI 平铺字段；D6 三个列表未知参数 400,共享 PageParams 不变 |
| `tests/controllers/test_audit.py`、`test_audit_archive.py` | 改为游标遍历与新响应字段断言；保留各自权限测试；GET 筛选、翻页和坏输入不产生审计；两个表使用不同匹配值验证筛选没有串表；不通过生产接口重新引入 count |
| `tests/controllers/test_users.py` | 所有用户白名单字段、`role_id eq/in`；一个用户命中多个角色时只返回一次且 total 正确；角色不存在返回空结果；原创建、更新和登录行为不变 |
| `tests/controllers/test_filtering_postgres.py`(新) | 新增自包含的 `pg_client` fixture(数据库、Redis、审计会话工厂一并切换并恢复,见 T6)；int32 范围与严格类型、NUL、奇偶反斜杠、字面 `%/_/\\`、大小写、时区与 UTC 溢出；三个路由的无权限/坏输入响应,必要的依赖替换在 finally 恢复 |

分层约束已由 `tests/architecture/test_layering.py` 的现有 `FORBIDDEN` 覆盖(schemas、services 不得反向依赖),无需新增用例。

现有断言质量:权限与分页测试检查实际响应和结果(★★★),不是只检查“不抛错”。新用例目标同样为结果、边界和错误路径(★★★)；尚无新增实现,不计算虚假的新代码覆盖百分比。

## 2. 失败模式与性能结论

| 拟议路径 | 现实失败 | 拟定处理与证明 | 调用方表现 |
|---|---|---|---|
| 参数绑定 | 合法 filter 被 extra 拒绝或拼错参数被忽略 | D2 完整模型、D6 端点 forbid 与组合测试 | 合法请求 200,未知参数/审计旧分页参数 400 |
| JSON/递归模型 | 非组、过深、混合节点、拼错键 | Pydantic 解析与 D4 forbid,长度/深度边界测试 | 400,不静默减少条件 |
| 白名单 | 字段/算子错误或探测密码列 | 只从声明映射取列,固定消息,检查响应与日志 | 400,不透露列是否存在 |
| 值类型与形状 | bool 被当 int、int32 溢出、in/between 形状错误 | 严格值校验及参数化用例,真实 PG 验证最终响应 | 400,不把驱动异常当输入校验 |
| ilike | NUL、非法转义、与前端模式语义不一致 | D3 模式契约与真实 PG 结果测试 | 坏输入 400,合法字面搜索准确 |
| 日期 | 无时区、数字及数字字符串输入、归一化上下溢 | `datetime.fromisoformat` 解析(拒绝数字字符串)+ 时区检查 + 转 UTC、D5 溢出处理与 PG 测试 | 400,合法跨时区比较准确 |
| role_id | join 导致用户重复及 total 偏大 | 相关 EXISTS,多角色命中与空结果测试 | 每用户一条,总数正确 |
| 游标 | 坏编码、类型错误、末页多发游标、跳过多取的一行 | 严格解码和值校验,limit+1 及整段遍历测试 | 400 或准确 next_cursor/null |
| 热表/归档 | 共用错 ORM 列导致额外 FROM 或串表 | 绑定实际模型,不同数据集测试 | 各自只返回本表匹配记录 |
| 数据库 | 连接超时或真实执行故障 | 沿用数据库异常处理及现有错误测试 | 500/503,错误码可见且隐藏参数 |
| 审计/日志 | GET 查询反过来生成审计或错误泄露值 | 保留 GET 排除路径,记录数与 caplog 断言 | 查询无写副作用,错误可定位 |

当前识别的关键错误路径均已映射到处理方式和永久测试要求；这是设计闭合,不是实现验证完成。

性能评审没有新增阻塞发现。审计只取 limit+1 条,避免 COUNT 和深 offset；筛选不承诺所有组合都走索引,不把“使用游标”误写成“任何条件都常数时间”。用户表保留计数,角色筛选用 EXISTS；2048 字节、100 项 in、3 层组限制查询构造规模。本期不增加缓存或索引,沿用已接受的 resource 扫描风险。

## 3. 实施顺序与并行边界

| 步骤 | 涉及模块 | 依赖 |
|---|---|---|
| 明确并实现请求/响应模型与校验 | `schemas/`、`tests/` | 已确认的 D1–D6 |
| 实现白名单表达式、值校验和游标分页 | `services/`、`tests/` | schemas 契约 |
| 接入三个列表并迁移原分页测试 | `controllers/`、`services/`、`tests/` | 查询与分页能力 |
| 更新示例、兼容说明并完成完整验证 | 文档、`tests/` | 列表契约接入 |

Sequential implementation, no parallelization opportunity. 筛选和游标虽然逻辑可分,本仓库的服务与测试目录高度重合,规模不足以值得拆 worktree；建议按上述依赖顺序实现。核心流程图保留在本文,实现时只在递归条件构造或游标截断处确有解释价值时加短注释,不为简单模型复制图。

## 4. 实施任务(完整交付)

依赖顺序 S1 → S2 → S3 → S4 → S5 → S6 → S7,与第 3 节一致。括号里的 T 编号是并入该任务验收的评审整改项,明细见第 5 节。完整范围以 SPEC 第 6 节为准。

- [x] **S1 (P1, human: ~2–3h / CC: ~15–25min)** — 请求/响应模型与解析(T1 模型部分、T3、T5 的 `UserListParams`)。
  - Files: `app/schemas/query.py`、`tests/services/test_filtering.py`(结构用例部分)。
  - 内容: 8 个算子的 `FilterOp`;`FilterCondition`/`FilterGroup`(均 `extra="forbid"`、`conditions` ≥ 1、`not` 恰 1 项、深度 ≤ 3、算子与值形状);`CursorPage[T]`;`CursorParams`(`limit/cursor/filter`,forbid);`UserListParams(PageParams)` 加 `filter`(forbid,不改共享 `PageParams`);`parse_filter`(字节数 ≤ 2048,`model_validate_json`,失败转 400,不回显值)。
  - Verify: 根/嵌套/叶节点未知键、混合节点、空 `conditions`、`not` 0/2 项、深度 3 通过/4 拒绝、非法根、空串、2048/2049 字节(含汉字构造)、深层 JSON 不返回 500;分层测试通过。
- [x] **S2 (P1, human: ~3–4h / CC: ~20–30min)** — 白名单与条件编译(T2、T4)。
  - Files: `app/services/filtering.py`、`tests/services/test_filtering.py`。
  - 内容: `FieldSpec`/`apply_filter`;8 个算子与 and/or/not 递归;严格值校验(整数 `StrictInt` + `DatabaseId`、布尔 `StrictBool`、字符串拒绝 NUL、`in` ≤ 100、`between` 形状、eq/in 拒绝 null);`ilike` 完整 pattern、显式 `escape="\\"`、尾部奇数个反斜杠与超长拒绝;datetime 先 `datetime.fromisoformat` 再检查时区再转 UTC,溢出转 400;未知字段与被禁止字段同一消息;错误不回显输入。
  - Verify: 全部算子、组合与拒绝分支的 SQLite 单测;时区、溢出、大小写行为留给 S6。
- [x] **S3 (P1, human: ~1–2h / CC: ~10–15min)** — 游标分页。
  - Files: `app/services/pagination.py`、`tests/services/test_pagination.py`。
  - 内容: `base64url(JSON {"id"})` 编解码(`DatabaseId` 严格校验、长度 ≤ 64);`paginate_cursor`(`limit+1`、`next_cursor` 取最后一条被返回行的 id、不含 COUNT/OFFSET)。
  - Verify: 空、不足、恰好、超过 limit,limit=1/100,整段遍历无重无漏,坏 base64/UTF-8/JSON、非对象、缺失/null/bool/字符串/越界 id,捕获 SQL 断言无 COUNT/OFFSET。
- [x] **S4 (P1, human: ~1–2h / CC: ~10–20min)** — 用户列表接入(T1 用户部分、T5)。
  - Files: `app/services/user_service.py`、`app/controllers/users.py`、`tests/controllers/test_users.py`、`tests/controllers/test_pagination.py`。
  - 内容: 用户白名单(`username`/`email`/`is_active`/`created_at`/`role_id` 的 EXISTS);绑定 `UserListParams`;筛选后再 COUNT 与分页;路由补 `responses=error_responses(400)` 与 `filter` 的 description/example。
  - Verify: 各字段;`role_id eq/in`(多角色只返回一次且 total 正确、角色不存在返回空);`filter` 与 `page/page_size` 共存;未知参数 400;不返回密码哈希;401/403 不变。
- [x] **S5 (P1, human: ~2–3h / CC: ~15–25min)** — 审计与归档接入,迁移旧测试(T1 审计部分)。
  - Files: `app/services/audit_service.py`、`app/controllers/audit.py`、`tests/controllers/test_audit.py`、`tests/controllers/test_audit_archive.py`、`tests/controllers/test_pagination.py`。
  - 内容: 白名单按实际模型(`AuditLog`/`AuditLogArchive`)各自构造,不共用同一张表的 ORM 列;响应改 `CursorPage`;`page/page_size` 与未知参数返回 400;补 `responses=error_responses(400)` 与 example;`test_audit.py` 的 `_total` 辅助函数改用游标遍历或直接查库;删除"两类列表响应形状相同"的旧契约。
  - Verify: 游标遍历、筛选叠加游标、两表使用不同匹配值验证不串表、GET 不产生审计记录、归档同样成立、权限不变。
- [x] **S6 (P1, human: ~2–3h / CC: ~20–30min)** — PG 测试基础与集成测试(T6)。
  - Files: `tests/conftest.py`、`tests/controllers/test_filtering_postgres.py`。
  - 内容: `pg_client` fixture(见 T6:不调用 `seed_admin`,账号与角色自建自清);集成用例覆盖 int32 溢出(含 `cursor.id`)、NUL、尾部反斜杠、字面 `%`/`_`/`\\`、`ILIKE` 大小写、时区比较(`12:00Z` 与 `13:00+08:00`)、UTC 上下溢与 `between` 任一端点、三个路由的无权限与坏输入。
  - Verify: 配置 `TEST_DATABASE_URL` 时全部通过且可重复运行;未配置时跳过。
- [x] **S7 (P2, human: ~1h / CC: ~10min)** — 文档与完整验证。
  - Files: `README.md`。
  - 内容: `curl -G --data-urlencode` 与 Postman(Params 表填 `filter`)示例;`ilike` 精确/前缀/包含/字面量示例;代理日志与 URL 长度提示;审计响应字段对照;用户列表未知参数 400 的兼容说明。
  - Verify: 配置真实测试库后 `uv run pytest -ra`、`uv run ruff check .`、`uv run ruff format --check .` 通过,并对照 SPEC 第 10 节验收标准逐条确认。

## 5. 评审整改项明细

T1–T6 来自设计评审,验收内容已随上面的 S1–S7 完成并同步勾选;以下保留任务来源与证据,不单独执行。

- [x] **T1 (P1, human: ~1–2h / CC: ~10–20min)** — 查询参数 — 采用完整查询模型并避免合法 filter 被拒绝。
  - Surfaced by: Architecture,问题 1/D2；原 CursorParams 与独立 filter 依赖组合不相容。
  - Files: `app/schemas/query.py`、`app/controllers/users.py`、`app/controllers/audit.py`、`tests/controllers/test_pagination.py`。
  - Verify: 三个路由的参数组合、审计旧参数拒绝、无筛选默认行为与 OpenAPI 平铺字段断言。
- [x] **T2 (P2, human: ~30–60min / CC: ~5–10min)** — 模式契约 — 实现并记录完整 ilike pattern 与转义规则。
  - Surfaced by: Architecture,协议确认 2/D3；原设计仍有待确认项。
  - Files: `app/services/filtering.py`、`tests/controllers/test_filtering_postgres.py`、`README.md`。
  - Verify: 精确/前缀/包含及字面 `%/_/\\` 搜索,大小写与非法尾部转义；PG 集成测试。
- [x] **T3 (P2, human: ~30–60min / CC: ~5–10min)** — 筛选模型 — 拒绝所有层级的未知键和混合节点。
  - Surfaced by: Code Quality,问题 3/D4；默认忽略额外字段与拒绝错误筛选的目标不符。
  - Files: `app/schemas/query.py`、`tests/services/test_filtering.py`、相关路由输入测试。
  - Verify: 根组/嵌套组/叶节点/混合节点返回 400,合法组语义不变且错误不回显值。
- [x] **T4 (P2, human: ~30–60min / CC: ~5–10min)** — 日期校验 — 将 UTC 归一化上下溢转为 400。
  - Surfaced by: Tests,问题 4/D5；带时区解析成功不保证 UTC 归一化成功。
  - Files: `app/services/filtering.py`、`tests/services/test_filtering.py`、`tests/controllers/test_filtering_postgres.py`。
  - Verify: 单值及 between 任一端点溢出、合法邻近值、正常时区比较；响应与日志不含输入。
- [x] **T5 (P2, human: ~30–60min / CC: ~5–10min)** — 用户查询 — 拒绝未知 URL 参数并说明兼容变化。
  - Surfaced by: Independent Review,问题 5/D6；用户模型与原覆盖图的额外参数策略不一致。
  - Files: `app/schemas/query.py`、`tests/controllers/test_pagination.py`、`README.md`。
  - Verify: 用户默认、合法 filter+page/page_size 请求正常,未知参数 400；共享 PageParams 不变,README/PR 记录变化。
- [x] **T6 (P2, human: ~1–2h / CC: ~10–20min)** — PG 测试基础 — 新增自包含的 `pg_client` fixture(同时提供客户端与管理员请求头),数据库与审计会话工厂切到 PG,Redis 使用 FakeRedis。
  - Surfaced by: SPEC 复审；现有 `admin` fixture 依赖 SQLite `client`,而该 `client` 会再次覆盖 `get_session`,所以新 client 不能和现有 `admin` 搭配。`pg_sessions` 已被 `test_rbac.py`、`test_menu.py`、`test_password_concurrency.py` 等使用,不是只用于迁移与冒烟测试。
  - Files: `tests/conftest.py`(或 `tests/controllers/conftest.py`)、`tests/controllers/test_filtering_postgres.py`。
  - 接线要求(参照 `tests/services/test_password_concurrency.py::account`):
    - 先 `copy()` 保存 `app.dependency_overrides` 与 `app.state.audit_session_factory`；`get_session` 指向 `pg_sessions`,`get_redis` 用 `FakeRedis`(不需要真实 Redis),`audit_session_factory` 设为 `pg_sessions`；`finally` 里清空并恢复。
    - 测试账号自建,**不调用 `seed_admin`**:它会补齐公共权限、写入固定名称的菜单,还会复用名为 `admin` 的角色并把其权限重置为权限表全部内容(`scripts/seed_admin.py` 的 `role.permissions = list(existing.values())`),库里原有 `admin` 角色若权限集合不同就会被改掉,结束时无法恢复。fixture 直接创建唯一用户和唯一角色(名称带用例后缀),给该角色挂本用例所需的权限(`admin:user:list`、`admin:audit:read`、`admin:audit:read_archive` 等,按用例取用);权限码已存在则只复用,不存在则创建并记录(权限并非只由种子脚本填充:归档迁移也会补入 `admin:user:list`、`admin:audit:read_archive`、`admin:maintenance:archive_audit`,因此不能假设新库的权限表为空或完整);登录取请求头,不依赖 `admin` fixture。另提供一个无权限账号用于 403 用例。`ASGITransport(client=(suffix, 123))` 给每个用例独立客户端地址,避免限流计数串扰。
    - 清理(`finally`):删除本用例创建的用户、角色及其 `user_role`/`role_permission` 关联行;只删除本 fixture 新建且已无其他角色引用的权限;审计测试数据使用本用例专属的 `action`/`resource` 值写入,并按这些值删除。不触碰公共 `admin` 角色、既有权限和菜单。
  - Verify: 配置 `TEST_DATABASE_URL` 时 `pg_client` 能登录、查询,结束后依赖覆盖与审计工厂已恢复,同一测试库连续运行两次互不影响(含运行前后既有 `admin` 角色的权限集合、权限表和菜单表行数一致)；未配置时用例跳过。

_No new tasks from Performance._ 实现后统一运行配置真实测试库的 `uv run pytest -ra`、`uv run ruff check .`、`uv run ruff format --check .`；不得用一次性脚本替代永久回归测试。

## 6. 实施与验证结果

- S1–S7 已完成,永久测试覆盖结构、严格值校验、全部算子、角色去重、两类分页、OpenAPI 与真实 PostgreSQL 行为。
- 独立临时 PostgreSQL、Redis 配置 `TEST_DATABASE_URL`、`TEST_REDIS_URL` 后,`uv run pytest -ra --tb=short --show-capture=no`: **311 passed,0 skipped**。
- 同一数据库再次运行 `tests/controllers/test_filtering_postgres.py`: **10 passed**;fixture 每次清理后均检查公共 `admin` 权限集合、权限/菜单行数及依赖恢复。未配置 `TEST_DATABASE_URL` 时,这 10 项按约定跳过。
- `uv run ruff check .`、`uv run ruff format --check .`、`git diff --check` 通过。独立只读最终评审未发现 Critical/Important/Minor 问题。
- 两个新增回归先复现再修复:日期解析允许 NUL 分隔符（统一提前拒绝 NUL）;可空筛选类型的 OpenAPI 描述落入分支（元数据移至外层）。权限种子说明已核对归档迁移并更正。
- 沿用 SPEC 边界:游标不签名/不绑定筛选,不提供跨页快照;筛选不保证走索引,本期不新增索引/缓存/排序;前端接入另行处理;日期沿用 `fromisoformat` 加时区检查,不新增业务年份限制。
- 按用户要求仅修改工作区,不暂存、提交或推送,未编写一次性验证脚本;原有两份文档的暂存内容保持不变。
