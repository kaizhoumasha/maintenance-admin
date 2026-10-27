# 通用 ECS Status、设备管理与工作线管理 Implementation Plan

> **For agentic workers:** B1–B8已按修订方案实施，原步骤及历史证据保留。当前用户已确认按需查询调整并授权本地feature提交，不授权推送或部署；角色框架隔离专项保持未提交。

**Goal:** 复用阶段A，把阶段B收口为纯配置列表/详情和按需状态查询，取消设备状态后台任务与device_observations；管理员、配置生命周期、原路由及历史记录合同保持。

**Architecture:** GET设备列表/详情只读配置；详情页状态与按钮共用已有POST /devices/{id}/refresh。按配置固定身份后复用通用query，返回前复核，结果不入业务数据库；Redis仅承载预览与查询协调。配置、路由、审计仍原子提交。未发布且尚未入库的基础迁移直接去掉观测DDL，保留角色绑定revision；已应用旧版的其他环境另行前向处理。

**Tech Stack:** 现有Python/FastAPI/Pydantic、SQLAlchemy/Alembic/PostgreSQL、Redis、HTTP客户端、pytest/HTTPX；不新增依赖。项目其他Celery任务与可靠Sender保留，设备状态不再使用Celery。

**Spec:** [当前设计](../specs/2026-10-27-device-workline-status-design.md)，尤其§5.4、§7.2–§7.4、§8和§11。§10–§10.6角色分发/配置失效停发/迟到Result合同不变，角色增量已由用户后续授权实施，其证据见[角色增量计划](2026-10-27-ecs-role-dispatch.md#完成与验证)。本次只回归、不重做或回退角色专项；旧历史原授权描述只在§6留痕，不恢复为新目标。

## Global Constraints

- 当前获准将已验证的相关功能本地提交至feature分支；不推送、不部署、不连接真实ECS/WMS。使用隔离提交快照，保留无关并行源码及暂存内容。
- 下列B1–B8是按需查询修订任务；旧同步版本B1–B8记录仅在§6留痕，本轮证据单列。
- 后续实施复用适合的feature工作区，不重置用户脏工作或暂存；开始前盘点并行修改，固定验证基线，不覆盖角色绑定专项。
- 当前目录device_code跨ECS唯一、大小写敏感且wire不trim；删除后新配置ID/UUID路由，不接管旧业务键或原命令。
- 列表和GET详情仅配置，零ECS请求；详情状态与手动刷新均调用现有POST刷新，保持admin+asset:device:refresh；read不授权查询，不新增重复GET状态入口。
- 状态查询和发现预览每次一个规范化IP origin、固定GET /api/v1/device/status，可选device_code；5秒HTTP预算/262144字节，无重定向、代理、自动认证替换、即时重试或自动改址。
- origin冷却5秒、租约15秒；上游429至少30秒且合法更长Retry-After不截短；预览从fetched_at起300秒，锁后/提交前复核，不续期。
- 取消30秒后台同步、90秒新鲜度、状态缓存/上次成功回退、状态筛选、登记/迁移后自动刷新。移除device_observations，不建替代状态/查询日志表，不把状态字段移入devices。
- Redis只保存预览和查询协调；取消数据库last_attempt恢复；保留Redis近期重启保护，明确键/数据丢失无法重建既有冷却/Retry-After。不可用503，不放行未知代次结果。
- 删除只改当前配置及管理审计；命令、结果、证据、可靠消息、审计历史和原路由不删改。发送门禁及迟到Result遵循SPEC§10，不因状态存储调整改变UNKNOWN/HELD规则。
- 所有设备/工作线管理仍是admin成员+操作权限，菜单相同判定；通用ecs:status:query与三类业务注册/目录独立，不新增授权或真实命令入口。
- 后续验证仅本地Mock及明确的一次性PG/Redis；不清理既有业务库。未来每任务先观察新合同失败、再最小实现、验证并核对范围，不自动commit。

## Review Focus

1. **隐式网络和权限扩大**：GET配置或返回写操作结果不能借read_device触发HTTP；只有read时，详情不查询状态。B2/B3/B6分别负向验证。
2. **返回错误地址结果**：查询期间删除、同码重登记、A→B→A或管理版本变化，旧成功/失败都不返回为当前状态，不按code重找。B3真实PG/Redis验证。
3. **遗漏观测依赖**：删除模型不能只删表；列表筛选、CRUD、Redis恢复、夹具和导出不能再读写观测。B2–B5用无表运行及零DML证明。
4. **迁移误删历史或旧消费者冲突**：当前仅精简未应用DDL，不整段downgrade；已应用旧版的环境必须另行前向移除并退出旧worker/API。B5的未发布迁移、新装/回退及历史守卫验证，部署另行授权。
5. **并行工作掩盖回归**：角色分发、原路由发送门禁、通用Status独立导入和WMS保留；全量基线失败不能当作本次通过。B6/B8对照SPEC与固定基线验收。

## 0. 执行前核对

实施前已核对SPEC和源码：旧状态服务含观测写入、扫描与DB恢复，Celery有设备任务，API共用DeviceRead，因此本轮整体移除调用方与夹具依赖，不能只停Beat或只DROP表。

输入错误沿用400；上游429/502/504和本方503复用现有错误模型。DeviceRead将主动删除状态字段，不用null或applied=true伪兼容；客户端/Swagger/示例同步。当前静态迁移为11e090516bf1 → c31f20261027，实际实施时确认唯一head及已应用版本，未提交不等于未应用。用户随后确认尚未入库；只读本地库为父版本且无设备表，故B5直接精简未发布DDL，不新增revision。

## 1. 文件地图和复用点

| 范围 | 改动文件 | 复用/保留 |
| --- | --- | --- |
| 取消自动触发 | app/tasks/celery_app.py、app/controllers/devices.py；移除app/tasks/device_tasks.py | 其他Beat/worker任务与可靠Sender不变 |
| 配置DTO/读取 | app/schemas/device.py、app/services/device_service.py、devices控制器 | 现有分页/过滤、Device/DeviceRoute、版本和事务审计 |
| 按需状态 | app/services/device_status_service.py、controllers/devices.py及asset_dependencies.py、main.py必要装配 | 阶段A query/StatusQueryResult/安全错误；Redis租约/冷却/429 |
| 删除旧观测 | app/models/device.py、app/models/__init__.py、device_service.py；移除app/services/device_observation.py | workline/DeviceRoute及§10绑定模型/发送门禁保留 |
| 未发布DDL精简 | migrations/versions/2026-10-27_device_workline.py与迁移套件 | 绑定revision及历史非空降级守卫保留 |
| 测试/夹具 | tests/conftest.py、现有services/test_device_*.py、controllers/test_devices.py、test_device_management_flow.py、tests/tasks/test_tasks.py、migrations/test_device_workline.py | 原通用协议、原路由/迟到Result、管理员、角色及WMS测试复用 |
| 使用说明 | docs/integrations/device-management.md、README.md、必要AGENTS.md和OpenAPI示例 | 本SPEC/计划为当前目标；旧验证记录不改成新通过数 |

不为凑文件名增加重复服务或演示测试。旧tests/tasks/test_device_tasks.py随能力取消，保留有意义的“无自动投递”断言到现有任务/HTTP测试。新增迁移专项用例可集中在tests/migrations/test_device_workline.py，不从其他测试文件导入夹具。

## 2. 阶段A：通用 ECS Status、统一目录和来源追踪

### Task A1：纯协议模型、严格JSON复用与输入边界

**Files:** 新建`app/strict_json.py`、`app/integrations/ecs_status/contracts.py`、`client.py`、`__init__.py`；修改`app/core/strict_json.py`、`tests/architecture/test_layering.py`；新增`tests/integrations/ecs_status/test_contracts.py`。

**Interfaces:**
- 保留`app.core.strict_json.json_object(raw: bytes) -> dict | None`、`canonical_digest(value: object) -> str`，兼容导出同一实现。
- contracts.py定义冻结/只读`EcsDeviceInfo`、`EcsDeviceState`、`EcsStatusResponse`；字段、可空、枚举、长度严格按设计§7.1。
- client.py公开`normalize_status_origin(value: str) -> str`，只做规范化/校验，无网络、无基础设施；非法输入ValueError。
- contracts.py定义冻结的`StatusQueryResult(capability_name, ecs_origin, requested_device_code, fetched_at, devices)`及`StatusQueryError`，后者固定属性`capability_name, ecs_origin, requested_device_code, completed_at, error_kind, http_status, retry_after_at`；datetime均UTC aware，devices为tuple。
- `CAPABILITY_NAME = "ecs.device.status@v1"`；错误类型不继承AppError，不携带原始响应或异常正文。公开导出仅包含本能力所需合同/查询。

- [x] 添加`test_known_fields_are_strict`：is_online=1、updated_at=True/浮点/字符串、缺字段、未知mode/status均拒绝；null仅用于设计允许字段，未知扩展忽略。
- [x] 添加`test_identity_is_preserved`：首尾空白、同项身份不一致、清单内重复编码均拒绝；大小写不自动合并；空devices合法。
- [x] 添加`test_origin_normalization_has_no_application_dependencies`：尾斜杠/默认端口等价；私网IPv4/IPv6合法，凭据/path/query/fragment/DNS/未指定/组播/作用域地址拒绝。
- [x] 运行`rtk uv run pytest tests/integrations/ecs_status/test_contracts.py -q`，确认新增行为用例因实现缺失失败，而非夹具错误。
- [x] 最小实现模型及origin校验，无语义变化迁移strict JSON并保留旧导入；收紧ecs_status子树的架构规则，不改其他集成规则。
- [x] 同命令转绿；再运行`rtk uv run pytest tests/architecture tests/core tests/integrations/test_registration.py -q`，确认共享解析和来源机制未退化。

**验收映射:** A16、A17、A26、A27；本任务不引入Status客户端池、设备类型注册或DB模型。

### Task A2：有来源的单次查询、错误分类与资源释放

**Files:** 实现`app/integrations/ecs_status/client.py`并导出；新增`tests/integrations/ecs_status/test_query.py`、`test_import_isolation.py`；复用`tests/helpers.py::loopback_app`，新公共Mock数据如有需要放共享helpers，不从其他test文件导入。

**Interfaces:**
- `async query(ecs_origin: str, *, device_code: str | None = None, client_factory: Callable[[HttpClientConfig], HttpClient] = AsyncHttpClient, clock: Callable[[], datetime] = utc_now) -> StatusQueryResult`。
- 每次构造一个有5秒总预算/262144字节上限的client，单次send，finally关闭自己创建的client；不跨事件循环复用，无DB/Redis/日志持久化。复用A1的json_object与canonical_digest完整检查，后者还拒绝JSON数字溢出为无限值（如1e999，包括扩展字段中的值），不改变旧解析helper语义。
- 合法200返回DTO；单设备空集合仍成功、404抛`StatusQueryError(error_kind="NOT_FOUND")`。网络、HTTP、非法响应均转本能力安全错误；取消通过CancelledError传播并释放资源，不转成查询成功。
- Retry-After仅在错误中返回解析的绝对时间，重复头/非法值/不可表示范围明确分类；整数0合法、过去日期按无有效等待处理，不截短合法长等待，不使用ecs-command解析器。

- [x] 添加`test_query_preserves_origin_and_request_scope`：全量无query；单台仅device_code，结果/错误带原origin、编码及clock时间；响应扩展伪造origin不被采用。
- [x] 添加`test_one_attempt_and_no_storage`：429/5xx/timeout/3xx各恰好一次HTTP；无睡眠、任务/存储调用；Retry-After合法秒数/日期返回、未截短。
- [x] 添加`test_query_rejects_untrusted_response_atomically`：错误编码/多项单台、重复JSON键、NaN/1e999非有限数、UTF-8错误、超限与失败正文无泄漏；整次不返回部分设备。
- [x] 添加`test_clients_close_on_success_failure_and_cancel`及两个loopback ECS隔离用例；一方超时不污染另一结果，注入工厂只能访问用例持有的origin。
- [x] 新进程`test_public_import_needs_no_application_environment`：清除DATABASE_URL、REDIS_URL、JWT_SECRET_KEY，在子进程直接导入公开入口、用注入HttpClient查询；确认未加载app.core/app.models/app.main/sqlalchemy/redis/fastapi/celery，无环境文件依赖。根conftest的成功导入不能替代此检查。
- [x] 先运行新增测试确认失败，再实现上述query和安全分类；严格解析复用A1，不调用app.core或ecs_command。
- [x] 运行`rtk uv run pytest tests/integrations/ecs_status tests/http_client tests/architecture -q`，期望全部通过。5秒为HTTP预算，资源关闭使用现有有界机制，不重写底层客户端计时。

**验收映射:** A15–A18、A26–A28、A32、A34。

### Task A3：内置能力目录与代码来源

**Files:** 修改`app/controllers/dev_registry.py`、`app/schemas/dev_console.py`、`app/integrations/__init__.py`、`tests/controllers/test_dev_registry.py`；新增`docs/integrations/ecs-status.md`；更新`docs/integrations/registration.md`，来源实现继续复用registration.py、不修改三类注册器。

**Interfaces:**
- 保留GET `/api/v1/dev/integrations/registry`返回list和排序；Status条目`kind=ecs_status,name=ecs.device.status@v1,entry_type=builtin_capability`。
- 原条目`entry_type=business_registration`；补充可空method/path/query_schema/response_schema，原endpoint/payload_schema语义不变。
- 目录侧`registration_source(query)`取得真实实现来源，`local_only=false`；Status中不反向导入registration。一个内置描述直接加入目录，不建通用能力注册框架。

- [x] 扩展`test_registry_lists_all_three_actual_registries`为分别断言三类业务集合原样保留、内置项恰好一个；断言源码符号对应真实query。
- [x] 把“空注册表返回空列表”长期规则改为`test_builtin_capability_remains_when_business_registries_are_empty`；不是仅放宽expected集合。
- [x] 添加`test_catalog_does_not_query_ecs_or_device_storage`、`test_status_is_formal_but_catalog_remains_local`：元数据加载零网络/设备查询/Redis依赖，原认证仍保留；production可独立import/query但目录404，来源不因local首次导入误标。
- [x] 新增测试先失败，再实现schema/目录最小扩展；文档解释“业务注册项”和“内置协议能力”，给出无落库Python调用示例，不使用真实地址。
- [x] 运行`rtk uv run pytest tests/controllers/test_dev_registry.py tests/integrations/test_registration.py tests/integrations/test_registration_discovery.py tests/architecture -q`。

**阶段A验收闸口:** 运行`rtk uv run ruff check .`、`rtk uv run ruff format --check .`及`rtk uv run pytest tests/integrations/ecs_status tests/http_client tests/architecture tests/controllers/test_dev_registry.py tests/controllers/test_ecs_status.py tests/integrations/test_registration.py tests/integrations/test_registration_discovery.py -q`。全部通过后记录证据，阶段B仍需后续讨论授权；阶段A不产生迁移、不依赖设备管理。用户已明确确认下列正式HTTP查询在所有环境挂载，Swagger展示仍遵循原项目设置。

### Task A4：用户确认的正式状态查询与 Swagger 展示

**Files:** 新增`app/controllers/ecs_status.py`、`tests/controllers/test_ecs_status.py`；在`app/main.py`无条件挂载，从`app/controllers/dev.py`移除旧入口；修改`app/core/errors.py`、`scripts/seed_admin.py`、README与Status指南。502/504适配从B7前移至本任务，B7后续直接复用。

**Interfaces:** `GET /api/v1/ecs/status?ecs_origin=...&device_code=...`直接调用A2 query；local/production/test均挂载，要求登录及`ecs:status:query`，继承API限流。GET不生成管理写审计，不登记/保存状态/创建命令，不引入阶段B的地址同步协调。200返回StatusQueryResult；400/401/403/429/502/504保持统一ErrorResponse与安全来源。Swagger文档可见性保留原环境策略。

- [x] 先确认新增权限、路由、目录元数据测试失败，按最小控制器实现；新增状态查询权限，不借用真实发送权限。
- [x] 验证OpenAPI参数/结果/错误、三环境均挂载、旧dev入口不存在、仅正式查询权限用户可查询、100次后API限流、本轮Mock外目标零请求、结果不落库及原命令通道零调用；Swagger/OpenAPI可见性独立验证。
- [x] 两台loopback ECS验证来源独立、一方429不影响另一方；Swagger HTTP入口经过实际loopback TCP查询全部和单台设备。
- [x] 独立审阅发现关闭超时遗留shield任务，补RED→GREEN用例；具体AsyncHttpClient增加可选独占关闭期限，默认行为保持，Status不再遗留关闭任务。

执行裁定：用户先要求Swagger查询，再明确确认正式HTTP不受环境限制，覆盖原“无HTTP入口”边界。只有一个固定协议的正式查询入口，不保留dev副本，不扩展设备管理。自定义HttpClient关闭须可取消；Status结果不强制存储，认证仍复用本项目基础设施。

## 3. 阶段B：按需查询修订任务

执行顺序B1取消自动触发 → B2纯配置读取 → B3无落库按需查询 → B4移除观测模型/写入 → B5未发布迁移精简 → B6权限/历史/并行合同回归 → B7说明与过渡核对 → B8整体验收。本计划是同一改动的连续任务，不发布中间不完整状态；生产部署另行安排。

### Task B1：取消状态任务与自动投递

**Files:** 修改app/tasks/celery_app.py、app/controllers/devices.py；删除app/tasks/device_tasks.py及失去用途的tests/tasks/test_device_tasks.py；修改tests/tasks/test_tasks.py和tests/controllers/test_device_management_flow.py。

**Interfaces:** 登记/迁移成功仍提交配置事务并返回原身份，但不调用schedule_refresh，不向任何状态队列发送消息。Celery不再加载设备状态任务，Beat无device.sync_registered；其他已注册任务及周期条目保持。

- [x] 以`test_preview_register_migrate_delete_reregister_with_two_local_ecs`验证：预览之后的登记/迁移/资料/分组/删除及API启动/空闲均零状态HTTP、零设备任务投递；其他任务注册保持。先运行确认在旧自动投递处失败。
- [x] 移除两个提交后投递调用、相应导入、任务include/Beat及任务文件；不purge共享broker或操作现有worker。
- [x] 调整旧任务/流程测试，删除已取消的“提交后自动刷新/30秒Beat”预期，保留无自动触发的长期合同。
- [x] 运行`rtk uv run pytest tests/tasks/test_tasks.py tests/controllers/test_device_management_flow.py -q`，确认负向用例转绿。此步不执行真实任务或迁移。

### Task B2：列表、GET详情与管理响应只返回配置

**Files:** app/schemas/device.py、app/services/device_service.py、app/controllers/devices.py；tests/controllers/test_devices.py、tests/services/test_asset_management.py、tests/controllers/test_device_management_flow.py。

**Interfaces:** `DeviceRead`只保留id、device_code、display_name、work_line_id、description、sort_order、current_route_id、version、created_at、updated_at、ecs_origin。`list_devices`/`read_device`只读取配置与原路由；所有管理写操作返回同一纯配置DTO，不为读详情增加HTTP。

- [x] 以`test_configuration_reads_never_query_ecs`合并验证纯配置读取与DTO字段边界，覆列表/GET详情/管理响应以及ECS不可达；请求观察器禁止所有非预览/显式刷新网络。旧字段必须缺失，不是null。
- [x] 写`test_runtime_filters_are_rejected`：status/freshness筛选400，编码/名称/工作线/未分组/origin配置筛选及总数/分页保持；先看失败，再改DTO/SQL/控制器。
- [x] 去掉DeviceObservation关联、状态SQL case、freshness/time_anomaly序列化；保留get_device身份和版本语义、配置只读的事务行为。
- [x] 运行`rtk uv run pytest tests/controllers/test_devices.py tests/services/test_asset_management.py tests/controllers/test_device_management_flow.py -q`，对照受影响用例的新合同；完整状态刷新合同在B3切换。

### Task B3：无落库的按需状态查询与协调

**Files:** app/services/device_status_service.py、app/schemas/device.py、app/controllers/devices.py、asset_dependencies.py及main.py必要装配；tests/services/test_device_status.py、test_device_status_concurrency.py、tests/controllers/test_device_management_flow.py。

**Interfaces:** 保留`DeviceStatusService(factory,redis,*,query_fn=query,clock=utc_now)`和`fetch(origin,*,device_code=None) -> StatusQueryResult`供预览复用。`refresh_device(device_id) -> DeviceStatusRead`返回device_id、device_version、current_route_id、result:StatusQueryResult，不修改DB；POST路由不变。短只读事务固定配置，HTTP外置，再短只读查询复核原ID/version/route及Redis代次；删除404、变化409 DEVICE_CONFIG_CHANGED、代次失效409 QUERY_SUPERSEDED，之后才返回结果或上游错误。

- [x] 写`test_refresh_returns_query_source_without_database_writes`、`test_detail_status_uses_existing_refresh_endpoint`；无观测表也可完成单次查询；不返回applied、旧配置包装或旧状态回退。观察SQL DML及HTTP请求计数，旧实现必须失败。
- [x] 写200合法/空数组、上游404安全502、429/502/504/503用例；200空数组不等于配置被删；配置仍可独立GET。复用通用解析器，不放宽完整响应校验。
- [x] 改查询流程，删除_apply、sync_registered及仅为落库存在的Attempt/计数等中间结构；查询只返回本次DTO，安全错误复用现有status_error，不重复写协议客户端。
- [x] 取消DB last_attempt恢复查询，保留5秒冷却/15秒租约、Redis近期重启30秒保护和429至少30秒/合法更长等待；Lua原子判断键丢失，未知代次拒绝返回，迟到429只延长origin等待。
- [x] PG/Redis复跑删除重登记、A→B→A、旧成功/旧失败、新查询先完成和取消释放；断言原配置ID404/409且新配置不受影响、网络期间无DB锁、不按code重找或upsert。数据丢失不宣称能重建旧期限。
- [x] 以专项和最终全量运行覆盖`tests/services/test_device_status.py tests/services/test_device_status_concurrency.py tests/controllers/test_device_management_flow.py tests/integrations/ecs_status`；origin隔离和单次资源关闭保持，5秒不写成整个API耗时保证。

### Task B4：配置事务移除观测写入与模型

**Files:** app/services/device_service.py、app/models/device.py、app/models/__init__.py；删除app/services/device_observation.py；tests/conftest.py、tests/services/test_device_configuration.py、test_device_discovery.py、test_asset_management.py及相应并发/HTTP流程。

**Interfaces:** register_devices/migrate_device_route只操作Device/DeviceRoute/AuditLog；delete_device不主动访问观测模型。预览DTO/TTL/分类/history_count及确认协议保持，确认时不再复制ECS状态。模型仅删DeviceObservation，不删DeviceRoute/工作线绑定或改原命令证据表。

- [x] 以`test_preview_register_migrate_delete_reregister_with_two_local_ecs`的SQL/HTTP观察及原并发回滚套件验证：预览后确认零HTTP、零新任务、无观测写入；审计失败全回滚；旧NEW token及更早token在登记删除后仍409，新预览仍可登记。
- [x] 从CRUD和fixtures移除观测导入/创建/清理，删除失去用途的新鲜度/转换helper及旧测试预期；不把字段移到Device或Redis状态缓存。
- [x] 更新原删除/重登记测试：新ID/route_id、原路由/命令/结果/证据/审计不变，旧ID各操作404；保留actor、锁序、绝对预览期限检查及PG编码锁，不借删除观测改绑定或发送规则。
- [x] 运行`rtk uv run pytest tests/services/test_device_configuration.py tests/services/test_device_discovery.py tests/services/test_asset_management.py tests/services/test_device_status_concurrency.py tests/controllers/test_device_management_flow.py -q`。

### Task B5：精简未发布且未入库的基础迁移

**Files:** migrations/versions/2026-10-27_device_workline.py、tests/migrations/test_device_workline.py；保留2026-10-27_workline_bindings.py原文。

**Ruling:** 用户确认迁移未入库，并要求重新评估前向方案。只读现有本地库为a025a20261025、无设备相关表；直接去掉尚未发布11e090516bf1中的观测表DDL，不添加先建后删revision。此前已应用的独立临时测试库不作为共享环境，由本轮自有新库验证。若前提在其他环境不成立，须另行新增前向移除；同revision修改不会自动更新已应用数据库。

- [x] 在SQLite/PostgreSQL执行真实Alembic升级，断言三张配置表存在且无观测表；旧DDL测试先失败，再最小删去创建/删除/计数引用。
- [x] 保留配置/路由历史非空拒绝降级、绑定非空拒绝降级、空库升降级和唯一head；不重置序列或执行历史。既有命令/结果保留由B6回归验证。
- [x] 仅在本轮独立迁移库执行，不升级/降级现有业务库。HEAD保持c31f20261027；既有旧版环境的前向移除与空表回退为条件性部署方案，本轮不生成或执行。
- [x] 运行`rtk uv run pytest tests/migrations/test_device_workline.py tests/migrations/test_workline_bindings.py tests/migrations/test_transaction_guards.py -q`，显式独立迁移URL；`rtk uv run alembic heads`确认单head。

### Task B6：权限与历史/角色接缝回归

**Files:** 复用tests/controllers/test_devices.py、test_ecs_status.py、test_menus.py、tests/integrations/ecs_command/test_device_directory.py、test_role_delivery.py、tests/services/test_device_routes.py、test_device_bindings.py及原回调/注册测试；产品权限/命令代码原则上不改。

**Interfaces:** 管理API仍admin+原操作权限；GET配置read、POST状态refresh。只读用户不触发详情状态请求，通用query权限仍独立；所有状态查询不升Device/WorkLine版本，不改变§10角色/发送门禁、幂等、迟到Result和历史冻结快照。

- [x] 复跑全部14管理入口的未登录/非admin误授asset/admin缺操作码/撤权测试和菜单；OpenAPI只有已有刷新入口，无重复GET状态路由，无字段伪兼容。
- [x] 复跑原命令/路由/绑定/Result与WMS回归；删除配置后历史与迟到Result保留，后续发送仍严格按§10，不把旧“历史授权继续有效”恢复成目标。
- [x] 不为不变逻辑增镜像测试；仅在本次改动导致的真实回归时补失败用例并最小修复，不顺带修复/回退并行角色专项。
- [x] 运行`rtk uv run pytest tests/controllers/test_devices.py tests/controllers/test_ecs_status.py tests/controllers/test_menus.py tests/services/test_device_routes.py tests/services/test_device_bindings.py tests/integrations/ecs_command tests/integrations/ecs_callback tests/integrations/wms_transport -q`；区分本次与并行基线失败。

### Task B7：使用说明、接口与迁移衔接

**Files:** docs/integrations/device-management.md、README.md、必要AGENTS.md及OpenAPI/客户端示例；本SPEC/计划只更新实际证据，不把历史通过数替换成新通过数。

**Interfaces:** 详情页先GET配置，再按refresh权限调用POST；按钮复用同一接口，无定时器。部署说明与开发验收分离：当前直接精简未入库DDL；只有已应用旧版的其他环境，才需先取消设备状态投递、切换所有不依赖观测的API/worker、确认旧在途任务退出，再另行前向DROP；不purge共享队列、不自动迁移或停止整个Celery。

- [x] 更新文档中的四表/状态筛选/90秒标签/缓存回退/自动刷新的现行说明，明确新响应字段和错误语义；来源fetched_at与ECS updated_at保持不同含义。
- [x] 记录实际head、迁移精简前提、已有旧版环境的任务处理及前向回退限制；30秒expires不保证正在运行的旧扫描已结束。环境发布待单独授权，不以真实部署作为开发闭环门槛。
- [x] 核对使用指南、SPEC§8/§11、OpenAPI一致；不修改管理员权限或角色注册合同。

### Task B8：本地端到端、全量验证与交付

**Files:** tests/controllers/test_device_management_flow.py及上述已有套件；验证记录写入本计划，保留当次源码/环境基线。

- [x] 用两台loopback Mock执行预览→登记→配置列表/详情→显式状态查询→分组→人工迁移→删除→新预览登记；记录所有HTTP，仅预览/显式查询发出，状态失败不影响配置、不制造任务/观测。
- [x] 对照SPEC A01–A35及§10 R01–R09记录已测/未测：身份并发、冷却、单次预算、精简后的DDL、历史/绑定/权限必须在独立PG/Redis验证，不用SQLite替代。
- [x] 运行`rtk uv run ruff check .`、`rtk uv run ruff format --check .`、`rtk git diff --check`、单head检查及显式独立存储的`rtk uv run pytest -q -rs`；配置缺失/连接失败不得充当新增用例验收通过。
- [x] 并行源码变化时固定同一基线，必要时做before/after对照；若全量仍有共同失败，完整报告，不用“无新增失败”替代全量通过，也不覆盖无关工作。
- [x] 保存证据，清理仅本轮拥有的Mock/临时容器；保留用户工作区和暂存。不提交、不推送、不部署、不连接真实ECS/WMS。

## 4. 覆盖与验证环境

| SPEC验收 | 任务 | 主要证据 |
| --- | --- | --- |
| A01–A03、A06–A11 | B4/B6 | 配置事务、预览重放、PG归属/登记竞争、原路由/执行历史 |
| A04–A05、A20–A21 | B1/B4/B8 | 状态请求与任务投递计数、注册/迁移只写配置及审计 |
| A12、A18–A19、A28–A30、A32 | B3 | 本次DTO/安全错误、身份/代次竞态、Redis等待与故障 |
| A13–A15、A22 | B2/B3/B6 | 配置字段/过滤与权限HTTP、详情双请求、无缓存回退 |
| A16–A17、A26–A27、A31、A33–A35 | 阶段A复用、B3/B8 | 独立导入、严格单次Mock、目录与正式入口回归 |
| A23–A24、R01–R09 | B6 | 现行角色/停发/迟到Result及单Sender/WMS合同，不引用旧授权证据代验 |
| A25 | B5 | 独立迁移库旧head→新head、新装、空表回退、数据守恒/单head |

单元/HTTP复用现有fixture；FakeRedis不证明TTL或原子租约。并发用显式一次性TEST_DATABASE_URL/TEST_REDIS_URL和pg_sessions/wait_for_lock；迁移用独立DEVICE_MIGRATION_DATABASE_URL，不能降级业务测试schema。网络仅注入客户端或受限loopback_app，非本轮目标零TCP。只清理用例自有键，不FLUSHDB。

## 5. 当前文档自检与交付边界

SPEC各条均有任务/测试归属；DTO名字、字段、路径、权限和时间参数在B2/B3固定，其他任务不另造状态入口/缓存。当前执行证据单列于§7；任务勾选以本轮实际行为验证为准，历史通过数不能代替本轮结果。

§6及既有代码审查/simplify报告保留的是先前同步方案或并行基线的历史证据。新方案的零任务投递、查询零写、无表运行及迁移精简独立验收；不恢复被SPEC§10覆盖的旧授权规则。用户已完成实施确认并随后授权本地feature提交，仍未授权推送或部署；原实施记录中的“未提交”描述当时状态。

## 6. 阶段 B 实施记录（2026-10-27）

> **历史记录，不是当前目标：** 下文原样保留旧同步/观测方案的实施和验证。旧B1–B8编号指当时任务，新的按需任务以本计划§3为准；历史原授权规则已被SPEC§10覆盖。所有通过数仅证明当时版本，不代表本次按需方案或前向移除已实施。

B1–B8产品范围已实施，使用当前 `feature/ecs-status-query` 工作区；原暂存设计保持不变，未提交或推送。上面的任务清单保留原验收意图，具体测试按职责合并到下列文件，不为凑原文件名创建重复套件：

| 任务 | 实现及实际验证位置 |
| --- | --- |
| B1 | 四表及唯一head `11e090516bf1`；`tests/services/test_device_configuration.py`、`tests/migrations/test_device_workline.py`验证非复用ID、历史路由保留、空库升降级和有历史拒绝降级（SQLite/独立PG）。 |
| B2/B7 | `test_asset_management.py`和`tests/controllers/test_devices.py`验证资料/原子分组/删除/事务审计、非空线限制、admin+权限、误授权限及菜单隐藏、撤销成员关系；HTTP删除审计仅一条。 |
| B3/B5 | `test_device_status.py`、`test_device_status_concurrency.py`、`tests/tasks/test_device_tasks.py`验证新鲜度、失败保留、origin租约/冷却/长Retry-After、Redis故障零请求、缓存丢失恢复、分组扫描、取消释放和每轮资源。PG验证A→B→A旧成功/旧失败、更新代次先完成及工作线竞争。 |
| B4 | `test_device_discovery.py`及PG并发套件验证actor/到期/选择边界、未选设备可继续登记、删除重放、跨origin竞争唯一赢家、审计失败回滚后可重试；HTTP流验证人工迁移、新ID/新路由。 |
| B6 | `test_device_routes.py`与`tests/integrations/ecs_command/test_device_directory.py`验证原独立授权、预加载对象迁移后刷新、旧同键冻结身份/HELD保留、新独立意图及删除后的实际迟到Result/证据。原lifespan/dev v3/历史严格附录与WMS回归保持。 |
| B8 | `tests/controllers/test_device_management_flow.py`通过两台实际loopback Mock执行预览→登记→手动刷新→后台扫描→分组→人工迁移→删除→新预览登记，零真实ECS/WMS、零新增命令。使用说明见`docs/integrations/device-management.md`。 |

技术适配：预览TTL已清除按不存在404，仍可读取但超时409；删除服务不依赖协调Redis，但HTTP保留原认证/限流；local默认正式附录映射为空，正式项目通过已有configure_ecs显式装配。原计划中每个微观组合不等于各有独立测试：例如审计更新失败与删除失败复用同一事务回滚机制，审计删除/登记失败已实测，其余由实现审阅及公共事务机制覆盖。

最终独立审阅发现一个Important（ORM缓存旧路由）和一个Minor（非法expected_origin返回500）；均已用实际失败回归复现，并在同一修复批次收口，未留下延期项。最终全量：2478 passed、25 skipped、0 failed，146.76秒；Ruff通过，376个Python文件格式检查通过，git diff --check通过，Alembic单head为11e090516bf1。25项skip为既有按后端/场景限制跳过的用例，不代表本轮PG/Redis未配置；本轮专用迁移、PG竞争、Redis租约和完整管理流均已运行通过。

后续全面审查与修复记录：[阶段B代码审查](../reviews/2026-10-27-stage-b-review.md)。修复预览排队过期、协调键丢失竞争、旧429等待丢失和同步Broker阻塞API四项问题；最终独立PG/Redis全量2486 passed、25 skipped、0 failed，仍未提交或推送。


## 7. 按需查询实施与验证（2026-10-27，本轮）

本轮按修订B1–B8实施，未提交、推送或部署；未连接真实ECS/WMS，未对现有业务数据库执行迁移。初始暂存区保留，改动在工作副本。角色/发送门禁专项与工作线绑定migration原文不变；SPEC§10保持原合同。

### 迁移裁定

用户确认当前迁移未入库，要求思考是否直接优化。只读现有本地PostgreSQL得到a025a20261025且四张设备相关表均不存在；基础迁移仍是未发布的新文件。故直接从11e090516bf1移除观测表的创建/删除/计数DDL，不新增先建后删revision；head保持c31f20261027，绑定migration不变。此前独立验证库不是共享发布环境，本轮重新用自有新库验收。若其他环境已经应用旧版，须另行生成前向移除，不能靠同revision文件修改更新已应用数据库；这是该裁定前提错误时的风险。

### 改动与复用

- 配置列表/GET详情和管理响应统一纯DeviceRead，状态筛选400；原分页、配置过滤、审计、版本和预览确认保留。
- 详情/手动查询仍共用POST refresh，返回DeviceStatusRead，包含原配置身份及通用StatusQueryResult；无applied/观测包装，不新增GET状态入口。
- 删除观测模型/服务/读写/fixture以及设备状态任务、Beat/include、登记/迁移后投递；复用通用query、来源、安全错误、Redis租约/冷却/429。查询网络外置、零业务DML，成功和上游失败都复核原ID/version/route与代次。
- 管理员+原操作码、工作线绑定、旧命令停发与迟到Result、历史路由/请求/证据保留；通用查询的独立权限及三类业务注册不变。

### RED → GREEN 与证据

| 范围 | 本轮证据 |
| --- | --- |
| B1自动触发 | HTTP流先在实际Celery投递`[1] != []`失败；移除后任务/流4 passed |
| B2纯配置 | 旧DTO多出状态字段、status过滤仍200，新增用例先失败；配置/管理13 passed |
| B3/B4无存储查询 | 旧刷新包装/落库/代次合同先失败，登记SQL观测写入用例先失败；核心PG/Redis/HTTP26 passed；额外PG并发14 passed |
| B5迁移 | SQLite真实迁移先因观测表仍存在失败；精简后SQLite/独立PG迁移、绑定及事务守卫6 passed；最终PG无观测表，保留三张配置表及head |
| B6权限和历史 | 资产/菜单/通用查询、命令/角色/回调/WMS及状态较大范围1131 passed、26 skipped；HTTP新合同15 passed |
| B7文档和装配 | OpenAPI字段/原POST/无重复GET、Celery无设备include/Beat检查通过；文档相对链接通过；README/AGENTS/指南/SPEC同步 |
| B8最终全量 | **2531 passed、27 skipped、0 failed，160.82秒**；Ruff check通过，384个Python文件格式检查通过，工作副本git diff --check通过，Alembic唯一head c31f20261027 |

测试环境为本轮新建的PostgreSQL17/Redis7容器（loopback 35432/36379），业务测试库、设备迁移库、绑定迁移库相互独立；与现有25432/26379服务分离。最终全量额外加载进程级socket连接保护，仅允许loopback/本地连接，HTTP使用内建Mock或显式注入客户端。执行`pytest -q -rs`时显式设置TEST_DATABASE_URL、TEST_REDIS_URL、DEVICE_MIGRATION_DATABASE_URL、BINDING_MIGRATION_DATABASE_URL与空RELIABLE_TARGETS。27项skip来自既有后端/场景限制；本轮新增PG/Redis和两类PG迁移均实际执行，不以skip代替验证。

计划微观测试名合并到已有公共HTTP/服务套件，避免重复测试和测试文件互相导入。无后台任务的启动/空闲部分通过源码装配与Beat/include检查，完整管理流观察实际任务投递和HTTP；没有另加定时空闲等待测试。代码审查确认无残留生产者、消费者或启动扫描。

最终独立只读审查：Critical/Important/Minor均无。审查核对身份/代次复核、迟到429、纯配置读取、原权限、删除任务、迁移守卫和历史接缝；未独立重跑测试、访问真实服务、核验外部库或评估前端/生产容量。父任务对这些未判断项裁定为范围边界：测试计数由本轮实际运行证明；外部库需条件性前向迁移，真实系统/前端/容量/Redis持久化不作为本地开发闭环条件。未延期代码问题。

验证日志保留在本地执行工作区/临时日志，交付后清理本轮自有测试容器，不停止或清理用户现有服务/队列/数据。本轮没有提交或推送，后续发布另行安排。


## 8. 后续按需查询代码审查（2026-10-27）

按用户要求全面复核移除完整性、查询并发/配置身份、迁移精简、管理员和历史保留。发现并修复1项P2：分页后逐条重读会在并发删除时整页404，在并发改址时返回不符合地址筛选的新配置。修复为序列化本次选中行并批量读取其保留路由，复用详情的DTO/路由校验，不更改状态/权限/写事务合同。两个独立PG回归先失败后通过；相关专项45 passed，迁移专项6 passed。最终全量2533 passed、27 skipped、0 failed（160.99秒），本轮新增PG用例均实际执行。完整发现、验证和范围见[按需查询审查记录](../reviews/2026-10-27-on-demand-status-review.md)。未提交、推送或连接真实ECS/WMS；原暂存和HEAD保持，本轮自有测试容器已清理。


## 9. 最终提交核对（2026-10-27）

用户确认按需调整并授权提交相关功能至feature/ecs-status-query，不授权推送或真实接入。此前通用Status、设备/工作线及原角色/发送门禁前置尚未提交，故本地提交收录完整可运行功能，而非缺失前置的状态差异片段。角色框架隔离增量（RoleRoutes、local组合移动、Status目录注入、独立角色文档/测试及SPEC§10.7）留在原工作区和暂存区，不纳入本次提交。

最终文档与代码对应：列表/GET详情及管理响应纯配置；详情状态和按钮复用原POST刷新；无后台设备状态任务或观测存储；管理员与刷新权限、配置/代次复核、历史/原路由及配置失效停发保持。迁移仍按用户已确认的未发布/未入库前提精简基础DDL，head不变；已应用旧版的其他环境另行前向处理。

simplify前后相关专项均45 passed，合并工作区全量2547 passed、27 skipped；这包含未提交的角色框架隔离，不能代替待提交快照验证。待提交快照已另行导出，并以新建的独立PostgreSQL/Redis及loopback-only Mock验证：2533 passed、27 skipped、0 failed，165.35秒；385个Python文件Ruff/格式检查、完整提交差异检查及文档链接核对均通过。27项为既有后端/场景限制，本轮PG/Redis与两类PG迁移均实际执行。角色框架隔离文件及其SPEC§10.7/指南段落保留在原工作区和暂存区，不纳入本地提交。验证后只更新本节和当前授权说明，产品代码与已测快照一致。
