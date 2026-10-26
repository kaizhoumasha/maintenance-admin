# SPEC-018 列表查询:筛选 DSL 与游标分页

状态:设计 v4(D1–D6 已确认;S1–S7 已实现并完成真实 PostgreSQL 验证,结果见 PLAN §6) · 分支:`feature/spec-018-list-query` · 承接 SPEC-016 中"查询筛选与游标分页后做"的遗留项

## 1. 目标与非目标

**目标**
- 为 `/users`、`/audit-logs`、`/audit-logs/archive` 三个列表增加筛选能力。
- 审计与归档列表改用游标分页,去掉对大表的 `COUNT(*)` 与深 offset。
- 筛选协议沿用 wes_backend 的 `FilterGroup` 词汇(and/or/not + 同名算子),便于两个项目共用前端筛选组件。

**非目标(本期不做)**
- 自定义排序(`sort`),列表固定 `id desc`。
- 关联查询语法(wes_backend 的 `$rel.`);需要关联的条件以白名单里的自定义字段提供。
- 按 JSON 列(`detail`)、`ip`、`trace_id`、`user_agent` 筛选。
- 新增索引。
- `/users` 的游标分页,仍用 offset 并返回 `total`。
- wes_backend 里本期没有字段用到的算子(`ne gt lt nin`),等有字段需要时再加。

**反目标**:任何实现都不得让客户端读取或探测白名单之外的列(尤其 `hashed_password`),也不得因为筛选写错而静默返回更多数据,更不得把客户端输入变成 500。

## 2. 实现前现状

- `app/services/pagination.py::paginate` 是唯一分页入口:`COUNT(*)` 加 offset/limit,返回 `Page{items,total,page,page_size}`。
- 三个列表均固定 `order_by id desc`,没有任何筛选。
- 请求校验失败走 `validation_error_handler`,筛选业务校验失败用 `AppError(INVALID_REQUEST)`,统一 400；数据库 `DataError` 等执行错误走 `db_error_handler` 的 500 分支,连接/资源不可用通常为 503(`IntegrityError` 另映射 409)。因此筛选输入的类型和范围错误必须在执行 SQL 前拦住,不能依赖驱动报错再转换。
- 分层由 `tests/architecture/test_layering.py` 强制:`schemas` 不能导入 `services/controllers`,`services` 不能导入 `controllers`。
- 审计中间件只处理 `POST/PUT/PATCH/DELETE` 且挂了 `require_permission` 的成功请求;GET 不写审计。
- 应用访问日志只记 path,不记 query string。
- 单元测试跑内存 SQLite;PostgreSQL 集成测试需设置 `TEST_DATABASE_URL`,CI 已设置。

## 3. 接口约定

### 3.1 传输方式

筛选用 **GET**,条件放在 query 参数 `filter`(JSON 字符串)。不用 POST:POST 查询会被审计中间件记为写操作,并把筛选条件写入 `detail`,导致"查询审计日志也产生审计日志"。

| 参数 | 适用 | 约束 |
|---|---|---|
| `filter` | 三个列表 | 可选;JSON 字符串,UTF-8 **字节数** ≤ 2048;未传表示不筛选 |
| `page`、`page_size` | `/users` | 沿用现有约束 |
| `limit`、`cursor` | 审计、归档 | `limit` 1–100,默认 20;`cursor` 可选,长度 ≤ 64;**传入 `page`/`page_size` 返回 400**(`extra="forbid"`),避免旧客户端被静默当成第一页 |

三个列表均只接受各自声明的查询参数,未知参数(例如 `filer`)返回 400。用户列表仅在新增 `UserListParams` 上设置 `extra="forbid"`,共享 `PageParams` 不变；原有调用方附带的前端专用参数需要在发送前清理。

调用示例(JSON 必须做 URL 编码):

```
curl -G "$HOST/api/v1/audit-logs" -H "Authorization: Bearer $TOKEN" \
  --data-urlencode 'limit=20' \
  --data-urlencode 'filter={"couple":"and","conditions":[{"field":"user_id","op":"eq","value":1}]}'
```

注意:上限按 UTF-8 字节数计(不是字符数):百分号编码把每个字节展开为 3 个字符,最坏约 6KB,在常见代理的请求行上限(如 nginx 默认 8KB)以内。email、username 的搜索值属于个人信息,反向代理/网关的访问日志会记录 query string,部署说明需提示。

### 3.2 DSL

```
FilterGroup     = { "couple": "and" | "or" | "not" (默认 "and"), "conditions": [FilterCondition | FilterGroup] }
FilterCondition = { "field": str, "op": "eq" | "in" | "ilike" | "ge" | "le" | "between" | "is_null" | "not_null", "value": any }
```

`filter` 的根必须是 `FilterGroup`,解析用 `FilterGroup.model_validate_json`(自带递归保护;不用 `json.loads`,深层嵌套会抛 `RecursionError` 变成 500)。

`FilterGroup`、`FilterCondition` 均设置 `ConfigDict(extra="forbid")`。根组、嵌套组和叶节点的未知键,以及混合组与叶节点字段的对象,一律返回 400；不新增节点类型标记,不静默丢弃输入字段。

`ilike` 不自动添加 `%`:模式 `foo` 为忽略大小写的精确匹配,`foo%` 为前缀匹配,`%foo%` 为包含匹配；模式 `foo\%` 匹配字面值 `foo%`。在 JSON 字符串中反斜杠需再转义,例如 `"value":"foo\\%"`；SQLAlchemy 调用显式传 `escape="\\"`。

| 算子 | value |
|---|---|
| eq ge le | 单个标量,**不接受 null** |
| in | 非空数组,长度 ≤ 100,元素不接受 null |
| ilike | 字符串,长度 ≤ 64;**完整 pattern**(如 `%foo%`),`\` 转义 `%`、`_`、`\`(与 wes_backend 一致);以奇数个 `\` 结尾的 pattern 拒绝 |
| between | 恰好 2 个元素 `[下界, 上界]`,闭区间 |
| is_null / not_null | 不传或为 null,传其他值报错 |

结构限制:`conditions` 至少 1 项;`not` 恰好 1 项;嵌套深度 ≤ 3(根组为第 1 层)。节点总数由 2048 字节上限间接限制,不单设上限。

值的类型校验(由白名单声明的类型决定):
- 整数:`StrictInt`;对数据库 int32 列(`user_id`、`resource_id`、`role_id`)复用 `DatabaseId`(1–2147483647),超界 400。
- 布尔:`StrictBool`,只接受 JSON `true/false`。
- 字符串:拒绝含 `\u0000`(PostgreSQL 不接受 NUL)。
- datetime:只接受 ISO 8601 **日期时间字符串**且必须带时区:先用 `datetime.fromisoformat` 解析,再检查 `tzinfo`,最后统一转 UTC。不能用 `AwareDatetime` 做格式校验:它会把数字字符串(如 `"1700000000"`、`"1700000000000"`,甚至 `"20261017"`)当作 Unix 时间戳接受,`"20261017"` 会被解析成 1970 年；归一化超出 Python 可表示范围时,在同一值校验边界转为 400 `INVALID_REQUEST`,仅返回固定原因与位置,不回显输入。覆盖单值与 `between` 两端,不额外限制业务年份。

语义说明:`not` 遵循 SQL 三值逻辑,`not(user_id = 1)` 不会返回 `user_id` 为 NULL 的行;需要时用 `or` 加 `is_null` 显式表达。

### 3.3 响应

- `/users`:`Page[UserRead]`,结构不变。
- 审计两个接口:**破坏性变更**。`Page` 改为 `CursorPage[T]{items, next_cursor}`:去掉 `total`、`page`、`page_size`,新增 `next_cursor`(`null` 表示没有下一页)。

## 4. 字段白名单

每个资源在 service 中声明 `字段名 → (列, 类型, 允许的算子)`。不在表内的字段、表内字段使用了未允许的算子,一律 400。

**用户 `/users`**

| 字段 | 允许算子 | 备注 |
|---|---|---|
| `username` | eq in ilike | 唯一索引 |
| `email` | eq in ilike | 唯一索引 |
| `is_active` | eq | |
| `created_at` | ge le between | 无索引,用户表规模小,接受 |
| `role_id` | eq in | 自定义子句:`exists().where(user_role.c.user_id == User.id, user_role.c.role_id == …)`;`user_role` 无以 `role_id` 开头的索引,接受 |

**审计与归档(共用)**

| 字段 | 允许算子 | 备注 |
|---|---|---|
| `user_id` | eq in is_null not_null | 已有索引 |
| `action` | eq in | **不开放 ilike**:PG 的 btree 索引不支持 ilike,大表会全表扫描 |
| `resource` | eq in | 无索引且取值只有 users/roles/menus 等少数几个,选择性低;单独使用会扫描,管理员接口,接受,出现慢查询再评估 |
| `resource_id` | eq in | 已有索引 |
| `created_at` | ge le between | 已有索引 |

永不开放:`hashed_password`、`detail`、`ip`、`trace_id`、`user_agent`;归档表的 `archived_at` 无索引,本期不开放。

## 5. 游标分页

### 5.1 语义
- 排序固定 `id desc`。查询为 `WHERE id < :cursor_id ORDER BY id DESC LIMIT limit+1`。
- 多取的那一条只用来判断是否还有下一页,不返回;`next_cursor` 编码的是**最后一条被返回行**的 id,不是多取的那一行。
- 筛选只增加 WHERE 条件,与游标条件 AND 叠加。

### 5.2 游标编码
- `base64url(JSON {"id": <int>})`,去掉 `=` 填充;对客户端不透明。
- 解码失败、不是对象、`id` 不满足 `DatabaseId`,一律 400 `INVALID_REQUEST`("cursor 无效")。
- 游标不绑定筛选条件;客户端更换 `filter` 后应丢弃旧游标。

### 5.3 一致性与已知限制
- 翻页期间新写入的行(id 更大)不影响后续页。
- 归档任务把行从 `audit_logs` 搬走时,后续页自然少这些行,不会重复。
- 并发事务中 id 较小的行晚提交,可能被一次遍历漏掉(窗口极小),下次从头查询可见;对审计查看场景可接受。

## 6. 组件与分层

| 位置 | 内容 |
|---|---|
| `app/schemas/query.py`(新) | `FilterCondition`、`FilterGroup`(含深度与算子/值形状校验)、`CursorPage[T]`；`UserListParams(PageParams)` 增加原始 `filter` 字符串并设置 `extra="forbid"`,不改共享 `PageParams`；`CursorParams` 包含 `limit/cursor/filter` 且 `extra="forbid"`；`filter` 字节长度 ≤ 2048(字段校验器里 `len(raw.encode()) <= 2048`;`Field(max_length=…)` 按字符计,不足以保证字节上限),`parse_filter(raw)` 用 `model_validate_json` 解析,失败转 400 |
| `app/services/filtering.py`(新) | `FieldSpec(column, type, ops, clause=None)`;`apply_filter(stmt, group, specs)`:校验字段/算子/值类型,构造 SQLAlchemy 条件;失败抛 `AppError(INVALID_REQUEST)` |
| `app/services/pagination.py` | 新增 `paginate_cursor(session, stmt, id_col, params)` 与游标编解码;`paginate` 不变 |
| `user_service` / `audit_service` | 各自声明白名单,在 `select()` 上应用筛选后调用分页 |
| controllers | 每个列表仅绑定一个完整的查询参数模型,再调用 `parse_filter(params.filter)`,不再用独立的 Query 依赖提取 `filter`；审计两个接口 `response_model` 改为 `CursorPage`;三个列表路由补 `responses=error_responses(400)`,`filter` 字段写 description 与 example |

约束:`filtering.py` 不导入 controllers;`schemas/query.py` 不导入 services。审计中间件、权限码、`INITIAL_PERMISSIONS` 不变。无数据库迁移。

## 7. 错误

| 情形 | 响应 |
|---|---|
| `filter` 不是合法 JSON、超长、结构不符、超深度 | 400 `INVALID_REQUEST` |
| 未知字段与被禁止字段(如 `hashed_password`) | 400,**两者消息完全相同**("筛选字段不支持"),不暴露哪些列存在 |
| 字段不允许该算子、值类型/形状/范围不对、含 NUL、pattern 非法 | 400,detail 只由 `位置 + 原因` 拼成 |
| 非法 `cursor`、`limit` 越界、审计接口传 `page`/`page_size` | 400 |
| 无权限 | 403(沿用现有权限码,无新增) |

规则:detail 不回显用户传入的值,也不直接 `str()` pydantic 的 `ValidationError`(会带出 `input_value`)。

## 8. 测试

**单元测试(SQLite)**,放在 `tests/services/` 与 `tests/controllers/`:
- 过滤:每个算子至少一例;嵌套 and/or/not;`not` 多子项被拒;eq/in 的 null 被拒;`ilike` 中 `%`、`_` 的转义;`in` 超 100;深度 4;`filter` 超 2048 字节(用汉字构造多字节边界);深层 `[[[[…` 不返回 500。
- 安全:`hashed_password`、`detail`、未知字段被拒且消息一致;`action` 使用 `ilike` 被拒;无时区 datetime 与数字时间戳被拒。
- 结构:根组、嵌套组和叶节点的未知键、混合组与叶节点对象均返回 400,错误不回显输入值；合法节点保持原有语义。
- 游标:完整遍历无重无漏;末页 `next_cursor` 为 null;`next_cursor` 对应最后返回行;非法游标;`limit` 边界;审计接口传 `page` 返回 400;筛选与游标叠加;归档表同样成立。
- `/users`:各筛选字段、`role_id` 的 EXISTS、筛选叠加分页后 `total` 正确。
- 路由层:两个审计接口覆盖 `filter+limit+cursor` 合法组合、无筛选默认请求、`page/page_size` 拒绝和非法 `filter` 返回 400；用户接口覆盖 `filter+page/page_size` 共存；OpenAPI 中查询参数按平铺字段展示。
- URL 参数:三个列表的未知查询参数均返回 400；确认用户默认请求与合法分页/筛选组合保持正常,共享 `PageParams` 本身的行为不变。
- 修改既有测试:`tests/controllers/test_audit.py` 的 `_total` 辅助函数读 `["total"]`,被多处使用,要换成其他计数方式(游标遍历或直接查库);`test_audit_archive.py`、`test_pagination.py` 里对 `total/page/page_size` 的断言一并改。

**PostgreSQL 集成测试**(CI 已配置 `TEST_DATABASE_URL`),覆盖 SQLite 复现不了的:
- int32 溢出(`user_id`、`resource_id`、`role_id`、`cursor.id` 传超大值)返回 400 而非 500。
- 含 NUL 的字符串、以 `\` 结尾的 ilike pattern 返回 400。
- 时区比较正确(`12:00Z` 与 `13:00+08:00` 的大小关系)。SQLite 存储会丢失时区偏移,所以这条**只能**在 PG 上验证;`ILIKE` 的大小写行为同理。
- UTC 归一化上下溢和 `between` 任一端点溢出返回 400,响应与日志不含输入值；可表示的邻近值及正常跨时区比较不受影响。复用 `pg_sessions` 和 HTTPX 请求验证最终行为(Redis 沿用 FakeRedis,不需要真实 Redis);需要新增自包含的 PG fixture(数据库、审计会话工厂一并切换并恢复,账号与角色自建自清,不调用会重置公共 `admin` 角色的 `seed_admin`);现有 `client`、`admin` 固定绑 SQLite,不能与它混用。

本功能已实现；合同与验收条件见本文，筛选/分页的现有回归入口见[开发测试](../../testing/README.md)。

## 9. 兼容与交付

- 审计接口响应字段对照:删除 `total`、`page`、`page_size`;新增 `next_cursor`。前端不在本仓库,需在 PR 描述和 README 中同步通知。
- 用户列表新增的未知查询参数拒绝行为也须在 README 和 PR 描述中说明；既有调用方若携带前端专用参数,需先清理再发送。
- README 补 `curl -G --data-urlencode` 与 Postman(Params 表填 `filter`)用法,以及代理日志提示。
- 按 Git Flow 在 `feature/spec-018-list-query` 开发,完成后合并回 `develop`;是否因破坏性变更发布 0.2.0 由发布时决定。

## 10. 验收标准

1. 三个列表在不传 `filter` 且仅携带已声明参数时行为与现在一致(审计除响应结构外)；未知查询参数均返回 400。
2. 白名单之外的任何字段、算子、值都得到 400,没有任何写法能读到 `hashed_password`,没有任何客户端输入能触发 500。
3. 审计列表翻页不执行 `COUNT(*)`,游标遍历结果与一次性全量查询一致。
4. `uv run pytest`(含配置了 `TEST_DATABASE_URL` 的 PG 集成测试)、`uv run ruff check .`、`uv run ruff format --check .` 通过,分层测试通过。

## 11. 评审后的决定与待确认

**已按评审采纳(无需再议,如不同意请指出)**
- 编号改为 SPEC-018(SPEC-017 已用于用户管理/角色改名)。
- 删除新增复合索引;`resource` 保留 `eq/in`;不开放 `archived_at`。
- 算子收缩为 8 个实际用到的;删除"节点数 ≤ 20"和"未来按 created_at 排序"的说明。
- 审计接口拒绝 `page`/`page_size`;`cursor` 长度上限改为 64。
- datetime 必须带时区、仅接受字符串、统一转 UTC。
- D1(范围确认):用户选择 1A,本期完整交付三个列表筛选与审计游标分页,不拆分或削减功能。
- D2(架构问题 1):用户选择 1A,每个列表使用一个完整查询参数模型,再解析模型内的 `filter`；保留用户原分页约束和审计 `page/page_size` 返回 400 的契约,补齐组合请求及 OpenAPI 测试。该调整防止合法 `filter` 被审计模型的 `extra="forbid"` 误拒。
- D3(协议确认 2):用户选择 2A,`ilike` 沿用 WES 的完整 pattern 约定,不做服务端自动包含转换；保持 64 字符上限、绑定参数和 PostgreSQL 转义测试,文档提供精确、前缀、包含及字面量搜索示例。
- D4(代码质量问题 3):用户选择 3A,`FilterGroup` 与 `FilterCondition` 均采用 `extra="forbid"`,使用模型自身约束拒绝未知键与混合节点；补齐各层结构错误和输入值不回显测试。
- D5(测试问题 4):用户选择 4A,UTC 归一化溢出在输入校验处转换为 400 `INVALID_REQUEST`,不新增业务日期范围；用永久测试覆盖上溢、下溢、`between` 任一端点失败及合法邻近值,真实 PostgreSQL 测试验证请求结果。
- D6(独立复核问题 5):用户选择 5A,新增 `UserListParams` 也设置 `extra="forbid"`,拒绝未知 URL 查询参数,共享 `PageParams` 不变；补默认、合法组合与未知参数测试,README/PR 明确兼容变化。

**仍待你确认**
无。本轮所有取舍已逐项确认；本方实现已完成；现有回归入口见开发测试说明，不以历史执行数字代替当前验证。

## 12. 工程评审:已有能力与范围

**复用的现有能力**
- `app/schemas/base.py`:复用 `PageParams` 的页码、大小和 int64 OFFSET 约束,以及 `DatabaseId` 的 int32 范围；筛选值还须严格拒绝布尔、浮点和数字字符串,不能因复用范围约束而接受类型转换。
- `app/services/pagination.py::paginate`:用户列表继续复用,筛选先应用到查询再计算 `total`；不为用户另建分页服务。
- `app/core/handlers.py`、`error_responses`、现有权限依赖:复用统一错误响应和权限码；只把输入解析、形状、值和 UTC 转换错误转成 400,真实数据库故障继续按现有 500/503 处理。
- `AuditColumns`、两个审计列表服务:复用相同字段规则,但列必须绑定本次查询的实际模型,不可把热表的 ORM 列直接用于归档查询。
- WES 的 `FilterGroup` 与算子词汇:复用协议语义,不直接引入会忽略未知字段的 `QueryBuilder` 实现。
- pytest、HTTPX、SQLite fixture、`pg_sessions`:复用现有测试基础和 CI 的真实 PostgreSQL,本功能不需要 Redis 新 fixture 或新依赖。

**本期不做(补充)**
- 新索引、查询缓存、计数缓存、搜索基础设施:本期只替换审计分页并开放明确的筛选白名单；`resource` 扫描限制已接受,出现实际慢查询再评估。
- 通用关联语法、动态排序、用户游标分页、更多算子:不阻塞当前三个列表的目标,延续第 1 节边界。
- 签名游标、绑定筛选的游标、跨页一致性快照:游标只定位记录,权限仍独立检查；沿用第 5 节已说明的一致性限制。
- 前端实现、WES 后端修改:前端不在本仓库,通过 README 和 PR 说明交付新契约；不修改 WES 的运行行为。
- 容器或发布流水线重构:本期没有新增交付产物,复用现有应用镜像和 CI；不为本功能新增发布机制。
