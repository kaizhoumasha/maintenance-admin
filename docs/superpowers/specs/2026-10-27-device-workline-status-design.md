# 通用 ECS Status、设备管理与工作线管理设计

> **接入代码历史说明（2026-10-28）。** 本文的旧业务目录、发现器、字符串/字典 SDK 或手写绑定示例已被[单一 API 接入](../../integrations/business-development-quickstart.md)及[验收记录](../../integrations/business-development-single-api-acceptance.md)替代；三类 registry 的独立 discovery.py 已删除，当前使用共享加载器和 catalog/固定平台 manifest。原协议、冻结数据、身份、结果与历史执行记录保持其原有语境，本文不作为当前 SDK 示例。


日期：2026-10-27（Asia/Shanghai）
状态：阶段A已确认；首期已确认改为按需查询ECS状态，取消后台定期同步和device_observations。按需查询修订已实施，当前轮本地验证证据见实施计划。
当前交付边界：修订B1–B8已完成本地Mock及独立PostgreSQL/Redis验证，用户已授权提交、推送feature分支并创建面向develop的PR；不连接真实ECS/WMS、不部署。此前对现有业务数据库只读核对迁移版本，未执行迁移。独立RoleRoutes及路由校验已在develop落地，本分支保留DeviceRoutes数据库使用方及Status目录元数据注入。原工作区并行暂存保持未提交，未直接纳入本PR。

2026-10-27按需查询修订：列表和配置详情只读本地配置；详情页状态区域与手动刷新复用现有POST /devices/{id}/refresh，不新增重复状态路由。取消30秒后台同步、90秒新鲜度、最近状态落库及登记/迁移后的自动刷新；迁移决策改为直接精简尚未入库、未发布的基础迁移，不再创建观测表；已应用旧版的环境需另行前向移除，见§11.2。§10–§10.6的角色分发、配置失效停发、历史记录和原路由保留合同原文不改；其中“Status轮询”是旧用语，本首期无轮询，按需查询同样不递增管理版本。

2026-10-27角色分发与发送门禁修订：已按用户授权完成本地实现；按`device_role + event_type/task_type`注册分发，配置变化停止旧命令后续发送，保留执行记录和迟到结果。完整实施及本地验收见[角色增量计划](../plans/2026-10-27-ecs-role-dispatch.md#完成与验证)。该合同覆盖此前历史发送授权策略；历史P1/阶段A/B的CI数字不代替本次验收。该轮实施时未提交、推送、部署或连接真实ECS/WMS；当前交付和最终验证见[实施计划§5](../plans/2026-10-27-device-workline-status.md#5-当前文档自检与交付边界)。

## 1. 目的与设计原则

本项目首先需要独立于设备目录的通用 ECS Status 查询能力：对指定 ECS 查询全部或一台设备，返回可信结构和明确来源，结果可以直接使用而不落库。在此基础上，设备管理作为首个使用方，回答设备是谁、归属哪条工作线、当前 ECS 地址是什么、本次从 ECS 查询到什么。工作线只负责逻辑分组；执行意图和命令结果继续由现有业务调用方与 ECS 命令机制负责。

按第一性原理区分以下事实，避免把不同责任合并为一个“状态”：

1. device_code是ECS协议中的设备身份；当前配置记录可以删除并重新登记。同编码重登记生成新的本地配置身份，不接管旧配置的请求或历史命令。
2. 工作线归属是本地组织关系，不证明设备在线或具备业务执行许可。
3. 当前配置决定新命令及每次发送尝试是否获准；历史命令保留原地址、请求和执行证据用于查询，配置变化后不再按旧配置继续发送。
4. ECS Status 是有时间边界的观测，不是业务许可、执行结果或设备取消证明。
5. 获取设备清单是读取；设备和工作线管理（含查看、登记、迁移、删除及刷新）仅向管理员开放，并保留各操作权限。
6. 一个系统可以访问多个 ECS，每个 ECS 可管理多台设备；设备编码跨 ECS 全系统唯一。同编码出现在不同地址时，只能由人工确认地址迁移，不能以复合主键另建第二台同编码设备。
7. 通用层忠实返回带origin的单次查询结果；设备使用方只负责配置身份、权限和origin协调。查询不写业务数据库，也不把结果复制到设备表、Redis状态缓存或新的查询历史表。

DRY：发现预览、详情状态查询、手动刷新及其他使用方共用Status客户端、DTO和解析器；来源复用RegistrationSource，归属修改共用设备服务。KISS：配置读取与状态查询分开，详情与刷新复用同一已有入口，一次调用一个明确origin。SOLID：通用层处理协议，使用方处理权限/身份复核/Redis协调，配置事务负责设备/路由/审计。YAGNI：不引入状态持久化、状态历史、替代观测表、第四类注册表、新Sender或自动轮询。

## 2. 已确认的首期范围

### 2.1 纳入

- 系统通用 ECS Status：输入 ECS origin 和可选设备编码，查询并返回结果，不要求本地登记、不强制落库。
- 多 ECS、多设备；按 origin 区分来源，跨 ECS 设备编码仍全局唯一，异址同编码人工确认。
- 统一接入目录增加内置 Status 能力项，复用代码来源追踪，并返回运行时查询来源。
- 正式 `GET /api/v1/ecs/status` 在所有环境挂载，要求登录、独立 `ecs:status:query` 权限及既有API限流；Swagger可见性单独遵循项目原配置，不保留重复dev查询入口。
- 工作线基础信息维护与删除；所属设备查询、分配、转移和移出。
- 一台设备最多属于一条工作线，允许未分组。
- 设备基础资料、全系统唯一编码、正式 ECS 地址来源。
- ECS清单预览、勾选后确认登记；查询不自动新增或删除当前设备配置。
- 同编码不同 ECS 地址必须单独人工确认，不能由预览或状态查询覆盖。
- 允许删除当前设备配置，再从ECS预览确认登记；旧命令、结果、证据、审计及必要原路由记录保留。
- 当前设备目录跨ECS编码唯一；删除后同编码可以重新登记，历史路由不占用当前登记名额，不生成第二条同时存在的同编码配置。
- 设备和工作线全部管理入口及标准菜单仅向admin角色成员开放，具体操作仍受对应权限控制。
- 列表仅返回配置；GET详情同样只返回配置，详情页按权限调用已有刷新接口查询状态。
- 手动刷新与详情状态请求均为按需、单次查询，不持久化结果；失败只返回本次诊断。
- 管理员权限、配置事务审计、历史记录及原路由保留。
- 正式路由提供者与既有命令框架的连接；地址目录本身不提供正式 HTTP 发命令入口。

### 2.2 不纳入

- 工作线业务运行、启停、运行模式、插件、清线和任务编排。
- 工作位、上下游拓扑、设备类型字典、物理占用模型。
- 手工维护设备在线、空闲、故障等运行状态；人工启用开关。
- 设备归档/恢复、软删除、回收站、后台自动发现登记、自动迁移地址或自动删除。
- 设备急停、命令取消、人工重发、HELD 解封、结果纠正。
- 后台定期/启动轮询、登记或迁移后自动刷新、device_observations及替代状态存储。
- 列表运行状态/新鲜度字段和筛选、NEVER_FETCHED/FRESH/STALE及90秒过期、服务端上次成功状态回退。
- Status推送事件、状态曲线、告警平台、逐设备周期配置。
- 第四类业务注册表、逐设备/逐 ECS 注册、可插拔 Status 合同、跨 ECS 自动合并、重复dev状态查询入口。
- 真实设备发送或供应商完整验收；不因本功能完成而宣称现场可用。

## 3. 已选方案及替代方案

### 3.1 通用能力与业务使用方

| 方案 | 取舍 | 结论 |
| --- | --- | --- |
| 通用 Status 查询、一个内置目录项；设备管理作为首个使用方 | 可无设备表直接查询、可不落库；共享协议与来源追踪，使用方独立管理应用策略 | 用户已选择 |
| 第四类业务注册表，按设备类型或 ECS 注册处理器 | 统一 Status 协议没有可变 operation/event_type，增加注册负担和合同选择规则 | 用户已排除 |
| Status 查询全部内嵌设备服务 | 其他模块必须依赖设备表和同步策略，无法独立复用 | 不采用 |

### 3.2 设备登记

| 方案 | 取舍 | 结论 |
| --- | --- | --- |
| ECS预览后确认登记；设备保存配置和路由，状态按需查询 | 配置事务与网络分开；无需后台轮询或状态持久化 | 用户已选择 |
| 持久 ECS 入口并自动发现登记 | 可持续发现，但需要独立入口生命周期、自动登记和冲突策略 | 用户已排除 |
| 完全手工录入设备及状态 | 地址和身份容易录错，运行状态失真 | 不采用；状态必须来自 ECS |

实现层选择：复用现有PostgreSQL、Redis、AsyncHttpClient、RBAC、AuditLog和ECS服务。设备状态不使用Celery；项目其他任务仍按原配置运行。不新增依赖或第二个可靠发送器。既有路由历史仅用于来源追溯及预览变化检测，不授予旧命令继续发送的许可，不新增历史授权表或授权清单。

### 3.3 分层职责和依赖方向

| 层 | 职责 | 明确不承担 |
| --- | --- | --- |
| 通用 ECS Status | origin规范化；固定GET；严格解析；只读DTO；错误和Retry-After解析；源码及运行来源 | 查设备表、全局查重、调度/锁/冷却、落库、预览、注册业务处理器 |
| 使用方查询协调（设备按需查询服务） | origin级锁/代次/冷却/等待期限，按本地配置调用通用查询，返回前复核身份 | 后台节奏、状态落库、复制协议、命令可靠投递 |
| 设备管理应用 | 当前配置登记/删除、管理员及版本门禁、迁移冲突、配置读取和管理审计 | 状态持久化、删除执行历史、修改通用查询合同、用Status授予业务许可 |
| 统一接入目录 | 聚合实际业务注册项和已加载的内置能力，展示源码及合同 | 发起网络查询、保存地址/设备、自动登记、证明对端可用 |

依赖为设备管理/其他使用方 → 通用 ECS Status → 现有 app/http_client。Status 协议模块不依赖 DB、Redis、Celery、FastAPI、设备或工作线服务；目录描述是纯代码元数据，不改变这一依赖方向。设备/工作线/原路由三张业务表和Redis协调仅属于设备使用方，通用查询单独使用不需要这些数据。

具体导入边界：当前registration.py会经app.core.module_tree间接执行app.core.__init__并加载数据库/认证相关模块，因此通用query不能为来源元数据直接导入registration.py。由目录聚合侧使用现有registration_source(query)提取来源；query与DTO自身不导入app.core、registration、ecs_command或目录控制器。origin校验使用现有HTTP客户端配置校验及httpx/IP标准库能力，行为遵循现有ECS origin规则，不通过导入ecs_command.routing带入应用依赖。独立导入和调用必须纳入无应用配置/基础设施的协议测试。

### 3.4 多 ECS 与全局身份

例如 origin A=`http://10.24.209.27:8080`、B=`http://10.24.209.26:8080`，两者均使用固定Status路径，不作为默认配置或种子地址：

- query(A)只返回A的设备，query(B, device_code)只查询B的指定设备；不得只凭编码搜索、轮询或自动切换所有ECS。
- 一次通用调用只有一个origin。使用方可在资源预算内并发调用多个origin，逐目标返回/处理结果，不因B失败使A的独立查询失败。
- 通用DTO以(origin, device_code)表达来源，不以此建立本地复合设备身份；纯查询不访问全局设备表，无法自行证明跨ECS编码唯一。
- A/B均报告同编码时，两次查询可以分别协议成功；设备预览/登记按设备编码全局唯一约束产生冲突，只能人工决定是否迁移已有设备。不能自动合并状态、生成第二条同编码登记或优先采用最近响应。
- 设备刷新只查询该ID当前登记的origin和device_code；返回前核对同一配置ID/version/current_route_id及查询代次，不跨origin寻找同编码设备。冲突仅由发现预览/人工迁移处理，查询结果不覆盖配置。
- 锁、冷却、Retry-After和失败范围以规范化origin隔离；同一origin的不同设备共享查询边界。归属同一工作线也不让不同origin共享锁或失败状态。
- 不保存独立ECS入口清单，也不扫描任何origin；通用查询和发现预览可以显式查询未登记的ECS，设备刷新必须先找到当前配置。

## 4. 参考与实际合同核对

### 4.1 参考位置

- 本仓库：[ECS 接入参考](../../integrations/ecs-command.md)、[命令与结果设计](2026-10-25-ecs-command-result-design.md)。
- 本仓库：[现有三类注册及来源约定](../../integrations/registration.md)、`app/integrations/registration.py`、`app/controllers/dev_registry.py`、`app/schemas/dev_console.py`；Status复用来源和目录，不扩展为第四类业务注册机制。
- wes_backend：`src/app/device/models/device.py`、`src/app/workline/models/workline.py`，参考身份和归属，不搬入 WES 业务。
- wes_backend：`docs/integration/third_party_integration_whitepaper.md` §3.2、`src/app/device/contracts.py`、`src/app/device/ecs_adapter.py`。
- 供应商参考：wes_backend `docs/hardware/SMT分拣机ECS接口调用说明书V1-20260318.md` §4.1。

### 4.2 既有只读证据（历史记录，本轮未访问）

2026-10-27（北京时间）读取用户提供的 `http://10.24.209.27:8080`，未发送命令：

| 请求 | 实际结果 |
| --- | --- |
| GET `/api/v1/device/status` | HTTP 200，ARM03、ARM04 |
| GET `/api/v1/device/status?device_code=ARM03` | HTTP 200，仅 ARM03 |
| GET `/api/v1/device/status?device_code=CODEX_STATUS_UNKNOWN_20261027` | HTTP 200，`{"devices":[]}` |

两台设备当时报告 AUTO、OFFLINE、is_online=false。只能证明接口可达且对端如此报告，不能推导物理现场状态。该地址是核对证据，不写入种子数据或默认配置。

用户另提供 `http://10.24.209.26:8080` 作为多ECS场景示例，本轮未访问该地址；多目标支持和全局唯一是已确认需求，不是对该目标的连通性或合同验收结论。

差异：白皮书要求设备不存在返回 404，实际返回 200 空数组。本项目明确兼容空数组为“本次未找到”，不自动解释为离线。白皮书要求字段闭集，参考解析代码允许额外字段；本项目选择允许未知扩展字段，但已知字段严格校验。

## 5. 数据模型与维护权

本节仅定义设备管理使用方的数据；通用Status不创建表、不强制保存结果或查询历史。业务数据均使用现有 `enterprise_backend` schema、SQLAlchemy Base 和项目时间/ID约定。下列长度和内部字段为设计选择，实施时遵守现有数据库 ID/时间序列化规则。所有编码区分大小写；本地工作线输入去除首尾空白后判空。ECS设备编码按wire原值校验、保存和比对，首尾空白视为非法，不静默清洗为另一身份；不自动改大小写或拼接编码。

### 5.1 work_lines：逻辑分组

| 字段 | 约束与用途 |
| --- | --- |
| id | 主键 |
| line_code | 非空，最长 50，全表唯一，创建后不可改 |
| line_name | 非空，最长 100 |
| zone_name | 可空，最长 100，普通文本，不建区域字典 |
| description | 可空，最长 500 |
| sort_order | 整数，默认 0 |
| version | 正整数，管理修改时递增，用于乐观锁 |
| device_bindings | JSON对象，默认{}；逻辑角色→当前本线设备编码，校验和锁序见§10.2 |
| position_bindings | JSON对象，默认{}；位置角色→位置编码，随工作线version维护 |
| created_at / updated_at | 服务端时间 |

有任何当前设备配置关联时禁止删除工作线，外键RESTRICT兜底。设备转移、移出或配置删除后，无其他成员的工作线允许删除；其编码可重新使用。设备删除前的归属以审计before值留痕，不用历史路由阻止空工作线删除。

### 5.2 devices：当前设备配置与本地资料

| 字段 | 约束与用途 |
| --- | --- |
| id | 当前配置主键，删除后不重用；同编码重登记必须产生新ID |
| device_code | 非空，最长100，当前目录全表唯一；记录存在期间不可改，删除后允许重新登记 |
| display_name | 非空，最长 200；首次取 ECS 名称，名称为 null 时取编码 |
| work_line_id | 可空外键，单设备最多一条线 |
| description / sort_order | 本地维护，最长 500 / 整数默认 0 |
| current_route_id | 非空外键，指向device_routes.id；服务保证设备编码一致，当前地址只从该路由读取 |
| version | 当前配置管理版本，正整数；资料、归属和迁移时递增，删除须匹配；按需状态查询不递增 |
| created_at / updated_at | 本地管理资料时间，不因按需状态查询改变 |

首期不提供脱离ECS预览的手工新建设备接口；登记成功即有有效地址。display_name可手工编辑，后续ECS改名不覆盖。不设归档/恢复字段或隐藏的软删除记录。

设备及工作线整数主键均由数据库生成且不回收、不重置；PostgreSQL沿用序列，SQLite配置AUTOINCREMENT并用删除最后一行再插入的用例验证。不能仅靠同编码或重置后的version=1识别重登记；旧device_id的请求应404，不能作用于新配置。

### 5.3 device_routes：独立保留的原路由记录

| 字段 | 约束与用途 |
| --- | --- |
| id | UUIDv7字符串主键，登记或迁移时新建，不复用；正式RouteSnapshot.route_id的稳定来源 |
| device_code | 原设备协议编码，最长100，有普通索引；独立保存，不外键依赖devices，不要求历史记录内唯一 |
| ecs_origin | 规范化 HTTP(S) IP origin，最长 255，不含业务路径 |
| created_at / created_by | 记录版本创建时间和操作者 |

每条记录的身份、设备编码和origin创建后不可改。本期不提供历史路由删除或清理；删除当前设备配置不得级联删除路由。设备当前指针是新命令唯一选址来源，不在devices再存一份地址。初次登记、每次迁移、删除后同编码重登记都产生新的全局唯一route_id；A→B→A也不复用旧记录。改名、换线不创建新路由。

原记录证明该地址曾被管理员明确登记，只作为历史事实保留；删除配置和迁移不删除旧记录，但使依赖旧配置的命令停止后续发送。正式目录RouteSnapshot.version改为提供者拥有的配置修订标记，复用当前设备/工作线管理版本，合同见§10；不再固定为旧布局"1"，也不新增每设备路由序号或绑定版本表。同编码重登记不接管原命令。

现有EcsCommand.device_code、route和delivery_snapshot本就独立保存，无devices外键。历史命令、结果、发送证据及可靠消息保持原保留机制，不新增针对设备配置的CASCADE。查询使用原快照，迟到Result使用原DeviceAnnex校验；二者均不要求当前设备配置或旧路由仍获发送授权。

### 5.4 不保留状态观测模型

目标模型仅保留work_lines、devices、device_routes；工作线已有device_bindings/position_bindings等§10配置不变。移除DeviceObservation及device_observations，不把状态字段迁入devices，不另建状态缓存/同步日志/历史表。

ECS描述、mode/status/is_online、能力声明、current_command_code、scenario和对端updated_at仅存在于本次StatusQueryResult中。DeviceRead不再包含observation、freshness、time_anomaly或last_success/last_attempt诊断。查询不更新设备version、updated_at或任何业务数据。必要的安全访问日志保留，但不记录原始ECS响应。

发现预览仍可在Redis按300秒TTL保存用于确认的原快照；其用途仅是预览与登记，不能作为详情/刷新的缓存或上次状态回退。历史命令、结果、证据、审计及原路由的保留不依赖观测表。

### 5.5 临时协调数据

Redis只保存预览快照、规范化origin的查询租约/代次、冷却及Retry-After期限，不保存可供配置详情读取的状态结果。预览token绑定管理员、origin、已校验条目、配置ID/version/current_route_id、route_history_count、fetched_at及绝对expires_at；TTL300秒，不能由客户端数据替代。

发现、确认登记、迁移和刷新需要Redis时故障返回503；配置列表、配置详情、普通资料维护和删除不依赖设备查询协调缓存，但仍依赖原有认证/全局限流基础设施。删除不清理共享origin锁/等待键。

查询锁租期15秒，释放验证持有者，网络不在数据库事务内；返回状态前复核原配置及查询代次，禁止按device_code查找重登记的新行替代原ID。键丢失与获取必须在同一Lua流程判断。取消依赖device_observations.last_attempt_at的数据库恢复路径，不增设替代表；恢复保护和数据丢失限制见§7.2。

## 6. 用户流程和事务规则

### 6.1 发现与确认登记

1. 输入origin并查询固定路径；清单校验后，在同一DB一致性快照（单SQL或等价短只读事务）读取当前配置及按编码的历史路由条数，不能用两个不一致的时间点拼成NEW分类和变化标记。
2. 按当前目录分类NEW、EXISTING、ADDRESS_CONFLICT；删除后同编码可再次为NEW，历史路由不会形成第四类设备状态。
3. 预览不修改任何正式设备资料，仅生成临时快照。
4. 用户勾选 NEW，可选择一个 work_line_id；不选即未分组。EXISTING 不重复登记，冲突不混入批量登记。
5. 确认请求提交 preview_token 与 device_codes，不接受名称、状态或目标 origin 的客户端覆盖。确认时再次核对归属目标存在、选中编码属于预览、token用户及有效期、设备编码唯一性。
6. 一个确认请求全部成功或全部回滚；当前目录唯一约束处理并发登记，任一选中设备已存在则409。只接受预览中的NEW选择，不把EXISTING/ADDRESS_CONFLICT因删除自动转成NEW；需要重登记时重新预览。未选中条目并发变化不阻断本次。
7. 在同一数据库事务创建设备、初始路由和管理审计；不保存状态、不发ECS请求、不投递刷新任务。等待锁后及提交前复核预览绝对有效期，到期整笔回滚；预览获取时间不改成确认时间。
8. 同一预览允许登记其他尚未登记的条目，均受原 5 分钟期限限制。重复提交已成功条目返回 409，不产生重复设备；用户通过目录查询核对结果。

NEW条目确认前，按选中device_code排序取得同一命名空间的PostgreSQL事务级编码锁（稳定编码哈希，不能用进程随机hash），再检查当前目录不存在且route_history_count仍等于预览值。所有登记和迁移的路由追加都遵守同一编码锁，随后才按工作线ID、配置ID顺序加行锁；对多个选中编码只核对各自标记，未选条目变化不阻断。历史路由不删除，因此任何成功登记/迁移会持久增加条数；配置删除不会抹掉这个变化。

登记追加路由、创建配置和写审计在同一事务提交，拒绝分离提交。已登记后删除，再重放原token或另一张更早的NEW token时，历史条数不同，返回409 PREVIEW_CHANGED并要求重新预览；不依赖提交后写Redis consumed标记，因此DB已提交但进程退出也不会开放重放窗口。事务回滚则没有增加条数，原有效预览可重试。SQLite只验证对应行为，编码锁的并发保证用PostgreSQL验证，不新增墓碑/消费表。

空清单可形成零条预览，不允许确认空选择。ECS 报告 OFFLINE 仍可登记。ECS 返回元数据为 null 时，保留 null，不猜测类型、角色和能力。

删除后重登记是新配置登记，不是恢复旧行：新device_id、新route_id、本地名称重新取ECS，说明/排序重新取默认，工作线默认未分组或由本次明确指定。不继承旧状态或业务调用键，不改历史命令快照，不恢复原发送许可，也不把未决结果伪造成已取消。只有历史同编码时允许管理员确认新地址登记，旧地址仅作来源提示；不能覆盖旧路由，也不能在当前异址配置仍存在时绕过冲突分类。

### 6.2 地址迁移

只能从目标 ECS 的有效预览确认：preview_token 必须包含该 device_code，目标 origin 与当前不同。提交设备 version、expected_origin 和 reason（非空，最长 500）以及显式确认标记；服务端核对预览时的原设备版本，变化则 409。普通资料更新和批量登记不能修改 origin。

成功事务只追加独立路由记录、切换current_route_id、递增管理版本并写审计。工作线、本地名称和历史命令不变。目标预览须在原300秒期限内确认，状态不持久化，提交后不安排刷新；不以设备在线为迁移条件。配置已被删除或同编码已重新登记时，原device_id/version/token不得作用于新配置，应404或409并重新预览。

迁移不改动既有EcsCommand的消息字节、原路由/附录或期限；提交后旧配置不再通过发送门禁，不改投新地址。已在途请求不能撤回；UNKNOWN/HELD不自动重发或解封，结果接收按原命令身份继续处理，见§10。

### 6.3 删除当前配置与重新登记

管理员调用DELETE `/devices/{id}?version=...`，匹配当前记录版本后，在同一事务写入删除审计并删除当前devices记录。版本过期409，原ID不存在404；不提供软删除、归档、恢复或回收站。审计保留设备编码、本地名称、工作线编码/ID、current_route_id和原origin等allowlist前值；删除失败全部回滚。

删除提交后旧ID的配置读取、刷新和新命令路由解析均找不到当前配置。正在查询的旧成功/失败响应按§7.4丢弃；不存在后台扫描和新投递的单设备任务。不能通过迟到回调、预览缓存或按编码upsert重建设备。旧版本任务的退出属于§11迁移衔接。

删除不清除origin级锁、冷却或Retry-After，不能影响同ECS其他设备，也不能让同码重登记绕过原等待期限。即使没有剩余当前配置，临时键按原到期规则自然清理。

删除不以设备在线、空闲或无未决命令为前提，也不强制结束设备动作。删除提交后旧配置不能再通过发送门禁；历史命令及证据仍可查询，合法迟到Result继续按原命令身份和附录接收。停止本地后续发送不证明设备未执行、已取消或执行失败。

同编码重登记只恢复一份当前配置，不重用旧本地主键或原路由身份。原业务同键同参调用仍返回旧命令，不能据新地址改投；新的独立有效意图按当前业务许可及新配置处理，不因历史失败永久受阻，也不得借删除重建/换号重做原UNKNOWN/HELD动作。

### 6.4 工作线与归属

新建、编辑和删除工作线使用工作线version。当前设备归属修改统一走设备服务，提交每台配置的version，批量全部成功或全部回滚。目标线必须存在；配置删除同时解除其归属，不批量删除工作线，也不改其他成员。

工作线删除先锁线并确认无当前设备；归属设置与配置删除使用一致锁序（工作线按ID，再配置按ID），数据库外键兜底。归属变化不修改current_route_id或旧命令快照，但管理版本变化使旧命令停止后续发送。

## 7. 通用 ECS Status 合同与按需查询策略

### 7.0 通用查询入口和不落库结果

向Python使用方提供一个公开异步查询入口，概念签名 `query(ecs_origin, *, device_code=None)`，支持注入现有HttpClient/工厂供隔离测试和资源管理使用，不为每个ECS注册代码项。入参采用显式origin，不自动查设备表或从环境猜测。通用能力可在非local环境由受信任服务调用，与local-only目录的可见性无关。

合法HTTP 200响应返回只读StatusQueryResult：

| 字段 | 含义 |
| --- | --- |
| capability_name | 内部合同标识 ecs.device.status@v1，不进入ECS wire |
| ecs_origin | 实际请求的规范化origin，来自可信请求上下文，不采用对端回传地址 |
| requested_device_code | 原查询编码或null，表明全量/单台查询 |
| fetched_at | 本方完整接收并校验响应的时间 |
| devices | 只读设备描述/状态集合，沿用§7.1合同；允许兼容的空集合 |

单设备404和HTTP/网络/解析失败返回类型化StatusQueryError（或等价显式错误结果，公共行为一致），含同一capability_name、origin、requested_device_code、完成时间、安全error_kind、可用时http_status和解析后的retry_after_at；不得只抛出没有目标来源的原始HTTP异常。200空集合仍返回合法结果，使用方按请求范围判断未找到/未返回。

通用层每次至多一次HTTP请求、总预算5秒；只解析Retry-After，不等待、不重试、不建立锁、不持久化限流期限。调用方负责实施等待，不能声称通用查询本身已提供跨进程限流。结果直接用于展示、诊断或内存计算时，DB、Redis、设备目录均可不存在。

首期提供独立Python查询及正式 `GET /api/v1/ecs/status`。HTTP适配层要求登录、`ecs:status:query`及既有请求限流，不按环境限制挂载；Swagger UI/OpenAPI仍仅按原项目设置展示。HTTP层直接复用query，不创建设备记录或命令、不保存结果、不自动重试；设备使用方的origin锁/冷却/身份复核仍属于阶段B。统一接入目录只展示合同，不触发查询。未来使用方同样必须承担自身身份鉴别、权限和协调策略，不能借“通用能力”绕过它们。

### 7.1 请求与字段

通用层固定 `GET /api/v1/device/status`，唯一可选 query 为 device_code。设备详情状态区域与手动刷新按配置ID解析origin和编码后查询单台；发现预览查询指定origin的全量清单。三者共享该使用方origin锁、冷却和限流，不写状态、不更新设备配置。

输入只接受 HTTP(S) IP origin，允许实际部署的私网 IP；复用当前 ECS IP origin 规则，拒绝凭据、业务路径、query、fragment、非法/未指定/组播地址，规范化尾斜杠及默认端口。不接受任意 URL 路径、DNS、重定向和环境代理；HTTPS 正常验证证书。该选择沿用现有调试地址边界，不引入用户可配置认证头或私有证书选项。

响应预算沿用单次 HTTP 客户端 256 KiB，严格 UTF-8 JSON，拒绝重复 JSON 键、非有限数和类型强转。所有以下字段必须存在，可空字段允许显式 null；未知扩展字段忽略，不保存为业务配置。

| 对象 | 字段 | 合同 |
| --- | --- | --- |
| 顶层 | devices | 数组，允许空以兼容实际 ECS |
| device | device_code | 非空字符串，最长 100；不接受首尾空白，不静默trim |
| device | device_name | 非空字符串或 null，最长 200 |
| device | device_type、role | 非空字符串或 null，最长 100 |
| device | supported_commands、supported_events | 非空字符串元素数组或 null；不强转元素类型 |
| state | device_code | 必须与同项 device.device_code 完全一致 |
| state | mode | AUTO / MANUAL / MAINTENANCE / UNKNOWN |
| state | status | IDLE / RUNNING / ERROR / PAUSED / STOPPED / OFFLINE / UNKNOWN |
| state | is_online | 严格 boolean |
| state | current_command_code | 非空字符串或 null，最长 160 |
| state | scenario | 非空字符串或 null，最长 100，仅诊断 |
| state | updated_at | 正 int64 毫秒，拒绝 boolean、浮点和数字字符串 |

清单内编码重复、已知字段缺失/非法或身份不一致使整次响应无效，即使出错条目未登记也不部分更新。单设备成功响应必须恰好一个匹配条目；空数组是明确兼容的“本次未找到”，多个条目或错误编码是合同异常。

不跨字段猜测或重写状态（例如 is_online=false 不覆盖原 status）；展示 ECS 原报告。异常组合不授权执行，后续供应商若增加明确一致性规则再版本化收紧。supported_commands 不由本模块自动转成附录或放宽命令校验。

### 7.2 按需查询的时间和协调

| 参数/机制 | 首期合同 |
| --- | --- |
| 后台周期、自动刷新 | 取消；不启动循环，不投递状态任务 |
| 状态持久化、新鲜度 | 取消；无90秒过期、NEVER_FETCHED/FRESH/STALE或最后成功状态 |
| 单次HTTP总预算 | 5秒，响应最多262144字节；资源关闭沿用阶段A有界宽限，不宣称完整API严格5秒SLA |
| origin冷却 | 从实际请求开始至少5秒，同origin不同设备共享 |
| origin租约 | 15秒，释放核对持有者；冲突409 QUERY_IN_PROGRESS |
| 上游429等待 | 至少30秒，取合法Retry-After与该下限较晚值；返回429及Retry-After，不截短 |
| 预览有效期 | 从fetched_at起300秒，读取/确认不续期，锁后及提交前复核 |

详情状态请求、手动刷新、发现预览均只尝试一次；没有自动重试、全量轮询、其他origin探测或自动改址。不同origin的锁、冷却和失败相互隔离。迟到429仍可延长origin等待，但不能释放/替换新查询租约；旧状态不得作为当前结果返回。

取消基于数据库last_attempt_at的等待恢复。Redis键自然到期后正常按需获取；Redis最近重启时，沿用已知服务启动时间的30秒保护。键/数据丢失后无法从数据库恢复之前冷却或Retry-After，不承诺跨Redis数据丢失保持原等待；不通过新建数据库日志表补回。Redis不可用或代次无法核验时503且不返回未知来源结果；持久化/故障恢复的运维验收与本地开发验收分开。

### 7.3 配置展示与状态展示

列表及GET配置详情只返回DeviceRead配置DTO。详情页先读取配置，再在具有asset:device:refresh权限时调用现有POST /devices/{id}/refresh；点击手动刷新复用同一请求。未获刷新权限仍可按read权限查看配置，但不发ECS请求。前端页面不在本轮仓库实现范围，本节规定其调用合同。

状态区域只展示本次查询的ECS描述/状态、fetched_at和对端updated_at；不提供服务端上次成功状态或新鲜度标签。查询失败只展示本次安全错误，不将超时/不可达/404/空数组转成OFFLINE，也不删除配置。ECS报告OFFLINE或UNKNOWN仍可是一份合法成功响应。配置展示不因独立状态请求失败而失败，状态查询也不授予任何设备执行许可。

### 7.4 查询身份复核和异常响应

设备刷新以当前配置ID解析唯一origin/device_code；客户端不提交可覆盖的地址或编码。先在短只读事务固定device_id、version、current_route_id、origin、编码，再在事务外执行HTTP。返回成功或上游失败前，用新短只读事务/查询复核原ID/version/current_route_id及Redis代次，禁止保持数据库锁跨网络。

- 原ID已删除：404，不能按同编码重找新配置、返回旧响应或重建设备。
- 原配置版本/路由变化（含A→B→A）：409 DEVICE_CONFIG_CHANGED，丢弃本次成功或失败，不自动查新地址。
- 同配置但查询代次失效：409 QUERY_SUPERSEDED，不返回旧结果；迟到429的共享等待仍按§7.2处理。
- 身份有效：返回DeviceStatusRead，字段为device_id、device_version、current_route_id、result；result直接复用StatusQueryResult。单设备devices只允许0或1项，沿用阶段A严格校验。移除旧DeviceRefreshRead.applied及其device观测包装，不用虚假的applied=true兼容已取消的落库语义。

| 响应/异常 | 对设备使用方的行为 |
| --- | --- |
| 200合法单台 | 200返回本次结果；业务数据库零写入 |
| 200空数组 | 200返回devices=[]，表示本次未找到；不改配置、不推断离线 |
| ECS单台404 | 沿用StatusQueryError.error_kind=NOT_FOUND，经既有安全映射返回502；与本地配置404区分 |
| 全量404、401/403、其他非200/5xx | 本次查询失败，沿用安全502；不换认证，不读取旧状态或安排下轮 |
| 429 | 按§7.2共享等待，返回429和Retry-After |
| 3xx | 拒绝跳转，不改址，返回安全502 |
| 网络超时 | 安全504，无即时重试 |
| 超限、编码/JSON/合同非法 | 整次无结果，安全502；不返回部分合法条目 |
| 本方DB/Redis故障 | 503，无自动请求补偿或状态写入 |

输入错误400、未登录401、非管理员/缺权限403保持项目约定。Retry-After继续使用通用层解析的合法非负整数秒/HTTP日期，不截短、不复用命令HOLD/重试语义。查询不写设备、路由、状态、业务审计或配置版本；原安全访问日志保留。

### 7.5 统一接入目录与代码来源追踪

复用现有 `GET /api/v1/dev/integrations/registry`，保持local环境挂载和已登录访问约束、列表结构及(kind,name)排序。原三类业务注册项的名称、来源、endpoint和payload_schema语义不变；增加一个明确标识为builtin_capability的Status条目，不创建第四个_registry、装饰器、业务入口发现规则或逐设备注册。

| 目录字段 | Status条目约定 |
| --- | --- |
| kind / name | ecs_status / ecs.device.status@v1 |
| entry_type | builtin_capability；原三类统一为business_registration |
| module / source_file / symbol / local_only | 复用RegistrationSource，从实际查询入口取得；能力为正式内置，local_only=false |
| endpoint / payload_schema | null / null，保留旧字段含义，不把请求路径塞进WMS endpoint枚举或伪装业务Payload |
| method / path | GET / /api/v1/device/status |
| query_schema | origin及可选device_code的公开查询输入schema |
| response_schema | 成功DTO的schema，包含运行时来源包装与devices合同 |

entry_type是区分两种目录项的新增字段；method/path/query_schema/response_schema为可空补充字段，原三类不适用时返回null。原业务注册项来源仍指向原函数/模型；Status来源指向真实公开查询函数，不指向目录聚合器或固定假路径。首期不增加一个通用可插拔能力注册框架，聚合器直接加入实际加载的单一内置描述即可。

内部版本@v1描述本项目的合同，不由设备类型、origin或供应商名称生成；升级内部模型时评估版本，不修改wire。两个ECS、两百台设备仍只展示一个Status能力项。能力加载与正式模块初始化关联；Status条目的构造/展示不得发HTTP、读取设备表、增加DB/Redis依赖或启用轮询。现有目录HTTP接口仍保留get_current_user的数据库登录认证依赖，不宣称该HTTP路由在DB故障时可用。来源提取在目录侧的正常正式加载作用域执行，不让local_registration误标正式能力。

需要更新现有DevRegistrationOut的kind允许值和上述可选元数据，但这是目录展示模型扩展，不改变三类业务注册流程。非local仍不挂载该目录；内置能力在非local可用，不能把“目录仅local”误写为“协议仅local”。

### 7.6 运行时数据来源追踪

区分代码来源与查询来源：代码目录说明“哪个实现/合同”，运行结果说明“从哪个ECS、查询哪个编码、何时取得”。通用结果无需携带服务器绝对源码路径，每次请求至少含§7.0的来源字段；错误也必须保留同一来源，日志使用现有安全诊断和trace_id。

设备使用方返回结果前将origin与开始时固定的配置ID/version/current_route_id及查询代次核对；本次结果带配置身份和来源，不落状态库。设备配置删除后，命令持有的原路由快照仍可独立追溯origin。预览保存原origin和fetched_at，不在确认时换源或刷新时间；不信任响应扩展字段提供的origin。

不要求通用层持久化查询日志、原始响应、所有设备历史或调用方业务信息。source_file/symbol证明代码装配来源，fetched_at证明本方获取时间，均不证明供应商状态真实、业务许可成立或物理动作完成。

## 8. API 设计

统一前缀`/api/v1`，响应/分页/过滤/错误结构复用项目约定。除已交付的通用`/ecs/status`外，以下设备/工作线管理路由全部要求admin角色成员，并检查表中的操作权限。静态discovery路由先于ID路由注册。管理请求拒绝额外字段，状态字段不在资料写模型中；内部标签通过既有AppError/detail表达。

| 方法与路径 | 主要输入/输出 | 权限 |
| --- | --- | --- |
| GET `/ecs/status` | ecs_origin、可选device_code；只读StatusQueryResult及来源，所有环境挂载 | ecs:status:query |
| GET `/work-lines` | 分页，编码/名称/区域过滤；当前关联设备数量 | asset:workline:read |
| POST `/work-lines` | 编码、名称、区域、说明、排序；201 | asset:workline:create |
| GET `/work-lines/{id}` | 资料与 version | asset:workline:read |
| PUT `/work-lines/{id}` | version 及可编辑资料；不含编码 | asset:workline:update |
| DELETE `/work-lines/{id}?version=...` | 无关联设备才可删除；204 | asset:workline:delete |
| GET `/devices` | 仅配置分页；编码/名称、工作线/未分组、origin筛选；status/freshness筛选明确400 | asset:device:read |
| GET `/devices/{id}` | 仅本地配置、当前origin与version，不访问ECS | asset:device:read |
| PUT `/devices/{id}` | version、display_name、description、sort_order | asset:device:update |
| POST `/devices/assignments` | items:[{device_id,version}]、work_line_id 或 null；原子批量 | asset:device:assign |
| POST `/devices/discovery/preview` | ecs_origin；返回 token、fetched_at、expires_at、分类条目及已有版本 | asset:device:discover |
| POST `/devices/discovery/register` | preview_token、device_codes、work_line_id 或 null；201，返回登记记录 | asset:device:register |
| POST `/devices/{id}/route-migrations` | version、expected_origin、preview_token、reason、confirmed=true；返回新版本 | asset:device:migrate |
| DELETE `/devices/{id}?version=...` | 原子删除当前配置并写审计；保留历史执行及原路由；204 | asset:device:delete |
| POST `/devices/{id}/refresh` | 详情状态与手动刷新共用；按登记路由单次查询，返回DeviceStatusRead，不落库 | asset:device:refresh |

所有管理写接口的DeviceRead也只返回配置；移除observation/freshness/time_anomaly，不保留空字段伪兼容。此为已确认的响应合同变化，须同步Swagger、客户端示例和测试。除字段外，配置身份/版本/来源字段保持。

不新增GET /devices/{id}/status或其他重复状态入口；详情与按钮都复用现有POST刷新。也不提供归档/恢复路由、PUT status、通用手工POST device、独立ECS CRUD或发令接口。GET `/devices`按work_line_id已覆盖工作线设备查询，不重复建嵌套接口。

通用Status的Python入口、正式HTTP和统一目录合同见§7.0/§7.5。正式HTTP只由ecs:status:query明确授权；设备预览/刷新HTTP仍经过各自使用方权限和协调，不隐含拥有正式查询接口权限。返回DTO/失败诊断包含规范化origin与获取/完成时间。目录增加内置条目只表示协议能力已加载，不是授予地址查询权限。

读取返回只读 DTO，不能暴露路由授权 ORM、Redis token内容或底层可靠消息。状态查询接口不根据 current_command_code 暴露无关业务数据。

错误规则：未登录401，非管理员或缺操作权限403，配置已删除/资源不存在404；当前编码重复、版本冲突、非空工作线删除、归属并发冲突409；可读取token过期409 PREVIEW_EXPIRED（TTL已清除则按不存在404），所选NEW条目历史变化409 PREVIEW_CHANGED，token不存在/跨用户统一404。输入校验/非法origin为INVALID_REQUEST/400；限流429，上游/合同失败502，请求超时504，本方DB/Redis不可用503。错误不回显原始对端正文；后台仅安全诊断，发现失败不生成可确认token。

## 9. 权限、菜单和审计

管理员判定采用现有种子`admin`角色成员关系，复用User/Role/user_role和get_current_user；不按用户名、用户ID或是否拿到某个asset权限推断。新增共享`user_is_admin(session,user_id)`及`require_admin`依赖用于本模块；不新增用户标记、管理员类型表或重写RBAC。现有“最后管理员”权限恢复保护保持原样，不把它当作本模块身份定义。

设备/工作线所有管理接口（列表、详情、预览、登记、编辑、分组、迁移、删除、刷新）先验管理员身份，再验各操作权限；非admin即使误获全部asset权限也403，管理员缺相应权限也403。撤销admin角色后下一请求立即失去管理资格；角色名称采用种子的规范名admin，不引入可配置管理员角色列表。

权限注册到INITIAL_PERMISSIONS并同步测试；加入asset:device:delete，不创建archive/restore权限。操作不会授予真实命令发送权限。阶段A通用`ecs:status:query`和local目录访问规则不变：拥有查询权限的非管理员仍可查询ECS，但不能访问设备/工作线管理。不再有设备状态后台系统任务；业务内部路由读取沿用自身业务许可，不使用管理HTTP权限代替。详情页自动发起的状态请求仍需asset:device:refresh，不能仅凭read、菜单可见或通过通用入口绕过设备管理权限；原操作权限组合规则保持。

组合操作要求具备必要读取权限：设备写操作同时要求 asset:device:read；分组时目标线查询/选取要求 asset:workline:read。跨地址迁移同时要求 discover、migrate、device:read；登记同时要求 register、discover、device:read，选择工作线时另需 workline:read。仅有查看权限不能触发任何到 ECS 的 HTTP 请求。

标准“设备管理”“工作线管理”菜单除read权限外，使用同一admin成员判定收口；非管理员即有资产read权限也不返回对应菜单。复用动态菜单服务，仅过滤这两个模块，不改变其他模块菜单/RBAC规则。按钮按操作权限控制；服务端403才是权限边界，不能只靠隐藏菜单。配套前端代码不在本期交付范围。

管理审计复用AuditLog/AuditColumns，记录操作者、配置/工作线ID、编码、时间、trace_id及allowlist前后值：名称/归属、current_route_id和地址、登记、配置删除、工作线变更。删除before保留原配置及来源，after为空；不得随配置级联删除。迁移必填原因，批量登记/分组按设备写审计。按需状态查询不写管理变更审计，也不递增配置版本。

现有 AuditMiddleware 是响应后旁路写入，仅含请求字段，不能保证 before/after 与变更原子。本模块的管理服务在同一事务写 AuditLog；审计失败则管理操作回滚。实施时给这些成功事务标记“已由服务审计”，中间件仅针对该标记跳过重复记录，不改变其他模块既有行为。只读的预览/刷新 POST 不生成管理变更审计，可保留现有请求访问审计语义；不记录原始 ECS 响应、预览 token、凭据或堆栈原文。

## 10. 与现有 ECS 命令链路的边界

复用正式设备路由提供者及现有resolve / authorize_frozen、ECS生命周期和唯一Sender。当前配置只决定是否允许发送，冻结快照只保存原请求身份；不让通用ECS/可靠模块直接依赖工作线业务服务或用Status决定执行许可。

- resolve：按§10.2的一致锁序读取当前配置、工作线绑定及current_route_id；配置不存在即拒绝，禁止从历史路由挑默认地址。附录仅由§10.4的角色任务映射选择，不接受客户端annex_id；新命令窗口继续使用共享300秒常量。
- 状态观测、工作线归属不作为新命令许可；supported_commands 仅为 ECS 声明，不自动替代正式静态合同或授予调用业务权限。
- authorize_frozen：在每次发送门禁内核对当前配置与冻结设备编码、route_id/origin、配置修订标记及角色附录映射；明确删除、重登记、改址、版本或映射不匹配返回REVOKED。原快照缺少可验证的门禁版本、格式损坏返回UNKNOWN；DB故障抛出并按现有事务故障处理，不能伪装成未绑定或授权撤销。不根据历史地址/历史授权清单返回ALLOW，不改写旧快照。
- 旧调用同键同参首先只读返回旧命令，不因配置删除、重登记、改址或当前附录不可用改写历史；迟到Result仍按原command_code和原证据处理。
- 正式设备 provider 与 local Swagger provider 不能重复 configure_ecs。实施须在现有唯一装配路径内选择/组合正式与调试路由来源，保留旧 dev v3 快照授权；不能覆盖 app.state 或重复启动 Sender。组合通过已冻结 route_id 命名空间区分原授权来源，未知来源保持 UNKNOWN。
- WMS STATIC、原命令字节/期限和有效配置下的原退避规则保持；UNKNOWN/HELD仍不自动重放。配置失效停止与迟到Result接收按下述新增合同适配，不笼统宣称Result行为零改动；Status客户端不进入命令outbox。

配置修订标记复用现有RouteSnapshot.version字符串，由正式提供者生成并校验，例如以`"2:"`为布局前缀，对device_id、device_version、work_line_id、work_line_version、device_role组成的规范化对象调用现有canonical_digest；未绑定工作线角色的设备不创建正式角色命令。通用ECS只冻结该不透明字符串，不增加WorkLine业务字段或新版本表。发送门禁重算标记，核对原route_id、origin和当前附录身份/版本；角色/地址变更后再改回也不会因管理版本回退而恢复旧许可。既有正式布局"1"不能推算原管理版本，后续发送返回UNKNOWN并沿用暂停机制；查询及原结果接收仍可用，不回填猜测版本。local dev快照继续由原local provider核对，不套用正式目录门禁。

管理版本采用保守规则：相关Device.version或WorkLine.version递增即停止旧命令后续发送，包含资料编辑；Status轮询不递增管理版本，不触发停发。修改工作线绑定时递增原/目标线version；转移设备仍递增设备version。暂不拆分名称、绑定、位置等专用版本，避免另建失效机制。

| 情况 | 后续发送与记录 |
| --- | --- |
| 当前配置与冻结身份仍一致，且处于原投递窗口 | 继续沿用现有可靠投递分类与退避 |
| 配置失效或修订标记不匹配 | 门禁拒绝HTTP；复用HELD / AUTHORIZATION_REVOKED及原命令投影，不标记设备已取消/失败，不自动恢复 |
| 已有合法ACK、交付未知或门禁已提交的在途尝试 | 不产生新的发送义务；保留真实交付/执行状态和证据，继续按原命令接收结果 |
| 已结束或被停止后续发送的历史命令 | 仅查询及接收原结果；查询、同键重入、重启或恢复旧配置均不重新入队 |

沿用发送门禁的串行化边界：配置修改先提交，后续门禁不得放行；门禁先提交，该次请求视为在途，之后修改不能撤回它，但禁止后续尝试。Sender既有锁序为“命令→配置授权→可靠消息”，其中配置授权内部采用“工作线→设备”；配置管理不扫描/锁定旧命令或消息，避免反向锁序，不新增批量取消器或重试调度。

迟到Result不调用当前配置发送授权。仅因本节配置变化产生的AUTHORIZATION_REVOKED暂停不能作为拒收合法Result的理由；复用原发送证据及未封锁规则，有可能交付/ACK依据的匹配结果可更新原命令执行记录并结算原可靠消息，不重新发送、不自动推进新角色业务。没有可能交付依据、身份冲突、提前结果或原矛盾封锁仍按既有409/证据规则处理；停止发送本身不能制造成功或取消。此适配须落实到现有result.py的HELD原因判断，不新增结果接收器；保留原附录模型用于校验，保留模型不等于保留历史发送授权。

设备表可先提供正式地址解析，但只有可信命令附录/业务装配实际存在时才能创建正式命令。本期不提供内置通用正式业务附录，不把成功登记当成生产发令能力已验收。

### 10.1 已选定的逻辑角色分发

事件注册键为`(device_role, event_type)`，声明Payload和处理函数；命令注册键为`(device_role, task_type)`，声明现有DeviceAnnex的身份及版本。命令业务动作由调用服务决定，不再增加一层命令处理函数注册器。不根据data是否相同增加分类、自动探测或另一层合同选择器。`task_type`沿用当前ECS协议名称，不引入`command_type`别名。

| 概念 | 示例 | 维护权与用途 |
| --- | --- | --- |
| 设备身份device_code | STATION_SCAN13 | ECS全局唯一编码，定位实际设备 |
| ECS设备类型/描述角色 | SCANNER / SCAN_STATION | Status只读观测，不参与逻辑角色推断 |
| 逻辑device_role | SCAN1 | 工作线本地绑定，决定事件处理和命令入口 |
| 工作线 | 本例所在工作线 | 提供本组设备、位置和业务上下文，不进入静态注册键 |

同一个逻辑角色在各工作线具有相同业务职责；四组工作线复用同一组SCAN1–SCAN4注册项。设备编码、ECS地址、工作线ID均不进入静态注册键。相同注册键必须唯一，重复声明在启动时失败并指出双方RegistrationSource；角色语义不同应使用不同角色名，不以当前data内容选择处理器。

已排除：仅event_type无法区分不同工位；device_type加event_type仍无法区分同为SCANNER的SCAN1–SCAN4；按每台设备注册会重复四组代码。采用逻辑角色组合键，复用现有注册、模块发现、合同校验和来源追踪。

### 10.2 与设备、工作线和位置绑定的关系

每条工作线的业务配置持有`device_bindings`（逻辑角色→设备编码）和`position_bindings`（位置角色→位置编码），作为唯一绑定来源。后续角色分发增量在`work_lines`增加这两个JSON对象字段，默认`{}`，沿用工作线version、管理权限和事务审计；不新增角色字典、绑定管理平台或另一套工作线实体。§5及阶段B现有四表/API验收仍为基础目录范围，这两个字段及其管理合同随角色分发增量实施。

用户确认的一组真实配置为：

```json
{
  "device_bindings": {
    "SCAN1": "STATION_SCAN13",
    "SCAN2": "STATION_SCAN14",
    "SCAN3": "STATION_SCAN15",
    "SCAN4": "STATION_SCAN16"
  },
  "position_bindings": {
    "INLET": "CNV0401",
    "OUTLET": "CNV0402",
    "FIVE_RACK": "KT18",
    "TRANSFER_RACK": "OUT66",
    "RETURN_RACK": "OUT56"
  }
}
```

- `devices.work_line_id`表示组织归属；`device_bindings`表示该线内的业务职责。绑定设备必须有当前配置且归属该工作线；同一设备在一条线内只能绑定一个逻辑角色。两者须在同一事务内校验，不能出现归属A而角色绑定在B。
- 设备转移、移出或删除前须解除对应角色绑定，可在同一管理事务完成；不能留下引用已删除配置的有效绑定。同编码重新登记不自动继承旧角色。历史命令及其原路由仍按原保留规则处理。
- 角色不再复制到设备表或Status观测；不从数字后缀、device_type、ECS role、scenario或回调自行携带的角色推断。未分组或未绑定设备可以继续查询Status，但不能据此参与角色业务。
- `position_bindings`由业务解释，只向当前工作线的处理上下文提供本组位置；本设计不创建位置主数据、拓扑或物理占用模型。上述配置是合同示例，不写入种子或默认值。

绑定校验与事件/新命令创建共用设备/工作线服务中的一个解析实现：先按全局唯一device_code读取候选配置及所属线，按工作线ID升序锁原/目标工作线，再按设备ID升序锁配置，刷新并复核device_id、work_line_id、版本及本线device_bindings后返回上下文；反查只遍历该线绑定，不扫描全部工作线，不增加Redis角色缓存。归属已变化时，不持有旧线/设备锁再反向锁新线；回滚本次解析，事件返回503供原样重报，管理/新建入口返回既有冲突错误，不按永久未绑定持久ACK。

绑定修改、解绑、转移、移出、配置删除、事件处理及新角色命令创建采用这一一致锁序。取得有效绑定后保持相关锁至当前事务完成；版本与成员关系检查、修改和审计在同一事务。管理入口不得通过普通工作线更新绕过校验，或先提交JSON绑定再修改设备归属。已有编码事务锁的登记/迁移路径保持“编码锁→工作线→设备”，角色解析不在持有设备锁后补取编码锁。

### 10.3 事件注册与分发

在现有`ecs_callback.registry`扩展组合键，不另建接收器。拟定注册形式如下，仅为待实施合同：

```python
@ecs_event("SCAN_COMPLETED", device_role="SCAN1", data=Scan1CompletedData)
async def scan_completed(ctx: EcsEventContext, data: Scan1CompletedData) -> None: ...
```

每个注册项声明角色、事件名、Payload和处理函数；正式角色事件明确声明Payload。SCAN2–SCAN4各自注册，不增加按data差异选择模型的规则。复用现有递归发现及文件名校验，例如`app/integrations/<business>/ecs_event/scan1/scan_completed.py`，末级文件仍按event_type命名。local调试注册保持local隔离；不保留按event_type任意兜底到其他角色的规则。

绑定解析按现有DeviceRouteProvider的依赖注入模式接入：ECS接收侧声明最小异步解析契约`resolve_binding(session, device_code)`，返回含device_id/管理版本、工作线ID/版本、逻辑角色及本线位置的绑定上下文，稳定未绑定时返回None。设备/工作线服务提供§10.2的实现，应用装配注入接收器；命令路由提供者复用同一解析实现。receiver不得导入app.services；不新增DI容器、注册平台或额外服务层。已装配正式角色处理器却未提供解析器时启动失败，不能静默把所有事件当未知；local示例使用显式本地绑定实现，不依赖真实ECS或正式设备登记。

接收顺序固定：

1. 沿用严格JSON、信封校验、内容身份计算；已有receipt直接返回原响应，不重新查当前绑定或校验Payload。
2. 新事件调用装配注入的绑定解析器，按§10.2取得并保护唯一有效的绑定上下文；通用接收模块不导入具体业务服务或直接查询其表。
3. 用`(device_role, event_type)`查注册项，再按该项Payload校验data；失败沿用400和`data.<字段>`问题路径，写receipt之前完成。
4. 沿用现有receipt与外层事务执行处理器。EcsEventContext补充已解析的工作线身份、device_role及绑定上下文；保持已取得的绑定锁，避免处理期间换线造成跨线写入。处理器只写当前事务，外发意图沿用现有命令/outbox。

在锁后稳定确认无唯一有效绑定、或组合键未注册时，沿用持久接收、200 ACK和安全诊断，不执行业务；并发复核失败、锁超时或DB等基础设施故障回滚并返回503，不能降为未知绑定。ACK不代表业务已应用，也不增加未知事件自动重放；先ACK未绑定事件后再完成绑定，同一事件重传仍返回原ACK，不补做业务。绑定和角色不加入事件摘要，不改变原wire或收据身份。

对于设备换绑后首次到达的旧事件，当前绑定只提供分发上下文，不证明事件属于当前业务轮次。处理器仍须沿用自身业务关联与幂等检查；证据不足时记录诊断，不凭当前角色推进新业务。

### 10.4 命令注册、创建与结果处理

命令侧只维护一份可信`(device_role, task_type) → (annex_id, annex_version)`映射，附录模型仍放在现有annexes中（params/result/error模型及超时约束）。这份映射注入现有正式路由提供者；configure_ecs仍只负责装配routes/annexes及单一ECS服务，不把它描述为现成的角色注册器。声明进入映射前校验重复组合键、来源及引用附录，不能先构成dict而丢失重复声明；目录读取同一声明，不另维护设备展开表。

合法同一业务动作重入时，调用方先核对原业务意图，使用现有`get_by_call(session, owner, call_key)`只读返回原命令；此步骤在解析当前绑定之前，不重入发送队列。相同call_key改变业务输入仍应冲突，不能以查询替代同键同参校验；如再次调用create，须传原冻结设备参数。

没有原命令时：业务调用方指定工作线、逻辑角色和task_type → 按本线device_bindings取得候选device_code → 使用§10.2的共享解析实现加锁并核对预期工作线/角色 → 调用现有`EcsCommands.create(session, owner, call_key, device_code, task_type, timeout_ms, params)`。route.resolve在同一事务通过共享解析结果取得当前逻辑角色，仅从上述角色任务映射选附录并冻结配置修订标记；调用方不另选一份附录。原EcsCommands继续按route中的附录身份校验params，不新增角色参数或命令处理器基类。缺少组合键/附录、预期绑定不符或params不合规均拒绝创建，不从Status的supported_commands生成合同。

角色、工作线及创建时绑定上下文由调用方原有业务动作关联保存，发送门禁使用§10的提供者配置修订标记；不新增业务动作表、命令关联表或WorkLine字段到通用可靠层。请求仍使用现有device_code/task_type，路由、附录身份/版本、字节和期限继续由现有ECS命令快照冻结。不新增平行命令队列、Sender、重试调度或角色专用状态机。

角色解绑、换线、改址或删除配置后，原命令按§10停止后续发送；不维护historical_bindings来延续旧命令发送。Result继续按command_code和device_code关联原命令，使用冻结附录校验；原附录模型须在可信装配中保留，可复用已有history_annexes。业务消费按原动作关联核对当前有效性，不根据设备当前角色重分发旧结果；停止发送和接收迟到结果均不自动授权新角色业务。

### 10.5 复用范围与后续影响面

后续角色分发增量仅扩展现有ECS事件注册/上下文/接收、工作线配置与绑定服务、命令可信任务映射和业务调用入口；复用RegistrationSource、模块发现、文件命名、CallbackPayload、DeviceAnnex、EcsCommands、原Result与可靠层。统一目录展示事件/命令合同的device_role及组合键，继续返回原源码来源；Status始终只有一个内置协议能力项。

实施时同步相关调用方、local演示注册、目录/schema、文档和合同测试，不能只改registry字典。§10的门禁和配置停发Result适配也在影响面内，不修改通用可靠层的协议职责。阶段A的通用Status不增加DB、工作线、角色或业务模块依赖；WMS注册与STATIC、旧命令快照、UNKNOWN/HELD不自动重放和原重试窗口保持既有规则。

### 10.6 角色分发验收合同

以下为角色增量的本地Mock验收合同；执行映射及最终结果见角色增量计划。阶段A/B历史验收与本次结果分别记录；不连接真实ECS/WMS，不发送真实设备动作。

| 编号 | 场景 | 必须满足 |
| --- | --- | --- |
| R01 | 同事件多角色 | SCAN1–SCAN4均注册SCAN_COMPLETED可共存，各自Payload只交给对应处理器；相同组合键重复声明启动失败并指出来源 |
| R02 | 多工作线复用 | 两组设备使用同一组角色注册；STATION_SCAN13–16分别解析为本线SCAN1–SCAN4，仅能读写本组业务与位置上下文 |
| R03 | 绑定一致性与并发 | 跨线/同设备多角色/未解绑转移删除拒绝；重登记不继承；PG交错事务验证工作线→设备锁序及锁后复核，无跨线副作用；并发变化503，不误ACK为未知绑定 |
| R04 | 校验与幂等 | 角色Payload不合规在receipt前返回400；已有事件重传在换绑/模型变更后仍返回原ACK，无重复业务动作；摘要不含派生角色 |
| R05 | 同任务多角色与单一映射 | 相同task_type按角色选各自附录；同一设备换角色前后使用互斥params合同，缺注册/错误params零出站；新命令只查一份角色任务映射，无逐设备副本 |
| R06 | 停发、历史与迟到结果 | 换绑/改址/删除/同码重登记后旧命令零后续HTTP，原字节/期限保留；原动作同键重入只查原命令，新独立意图使用当前绑定；有可能交付依据的旧Result仍按原身份/附录接收，不转交新角色；从未可能发送的结果保持原冲突判定 |
| R07 | 框架与分层回归 | 单一ECS服务/Sender；解析契约装配注入，integrations不导入services；local示例用本地绑定；目录组合键来源准确；Status独立查询、WMS及UNKNOWN/HELD不自动重放保持 |
| R08 | 门禁与配置变更竞态 | PG验证配置先提交则门禁不放行；门禁先提交的尝试可在途，之后不得产生新尝试；A→B→A及角色改回不恢复旧许可，恢复配置/重启不解封HELD |
| R09 | 失效分类与保守版本 | 明确失效REVOKED，旧布局/损坏UNKNOWN，DB故障不伪装成撤销；相关资料编辑升版本也停发，Status轮询不升版本；未知绑定ACK后再绑定，原事件重传不补做业务 |

角色合同和发送校验复用develop已落地的独立RoleRoutes；DeviceRoutes仅提供工作线/数据库解析。Status能力由完整应用装配到目录，不成为角色框架或注册器的依赖。参见[角色框架设计](2026-10-27-ecs-role-framework-design.md)。

## 11. 模块组成及前向迁移边界

### 11.1 改动范围与复用

通用app/integrations/ecs_status、正式HTTP、统一目录/来源、HTTP客户端继续复用；不修改三类业务注册器或§10角色合同。设备管理控制器/服务/schemas收口为配置DTO及按需DeviceStatusRead；main仅装配按需服务，不启动后台扫描。

取消app/tasks/device_tasks.py、其Celery include/Beat项，以及登记/迁移后的schedule_refresh；不清空其他任务队列、不移除项目Celery或命令唯一Sender。移除DeviceObservation模型/导出、读写/筛选和相关夹具；device_observation.py中的转换/新鲜度函数失去调用方后删除，不保留无用途框架。

设备使用方只保留Redis预览/查询协调和返回前身份复核；配置事务负责devices、device_routes、AuditLog一致性。设备/工作线/原路由三张表的约束、§10工作线绑定字段、管理员与菜单、历史命令/结果/证据/审计保留均保持。查询不改变管理version，不影响命令发送门禁。

### 11.2 未发布迁移精简与已有旧库边界

本轮用户确认设备迁移尚未入库，并要求重新评估前向方案。只读核对现有本地PostgreSQL：版本为a025a20261025，devices/work_lines/device_routes/device_observations均不存在；此前验证应用仅在独立临时库。基于此未发布/未应用前提，直接精简11e090516bf1，去掉device_observations的创建、删除及降级计数；不增加“先建再删”的revision。保留工作线绑定c31f20261027及既有配置/历史非空拒绝破坏性降级的保护，唯一head仍为c31f20261027。

当前升级路径a025a20261025 → 11e090516bf1（三张配置表）→ c31f20261027（工作线绑定）；新装和从原父版本升级最终均不含观测表。使用独立SQLite/PostgreSQL验证DDL、空库升降级、有历史拒绝降级及绑定迁移；不迁移现有业务库、不回填/删除/重置设备、工作线、原路由、命令、结果、证据、可靠消息、审计或序列。

此决策不是通用的“改旧revision就能升级旧库”。若其他环境曾应用含观测表的旧11e090516bf1或c31f20261027，Alembic不会因同revision文件变化重跑DDL；该环境必须另行在唯一head后生成前向移除迁移，退出所有旧消费者后仅DROP观测表及自身约束。回退只能重建空表，不能恢复已删除状态快照。不通过整段downgrade、重建业务库或启动时自动DROP处理。

### 11.3 已运行旧版本环境的衔接（非本轮部署）

先停用设备状态Beat与登记/迁移自动投递，再让全部API/worker使用不读写观测表的新代码，确认旧状态任务/在途查询已退出，最后执行DROP。已有device.sync_registered/device.refresh_one待处理消息仅按类型处理或等其过期，不清空共享队列；任务30秒expires不等于已运行扫描一定在30秒结束，不能只等待固定时间就删表。

兼容窗口中，旧表可暂时存在而被新代码忽略；表是否已删不能成为新查询服务的依赖。不在普通API启动时偷偷DROP表或自动部署迁移。生产环境消费者盘点、备份与发布安排单独授权，不作为本轮本地开发完成条件。

## 12. 验收条件

以下是新修订的目标验收，不是已经通过的测试。使用本地受限Mock、独立PostgreSQL/Redis；不连接真实ECS/WMS、不发设备命令。既有阶段A/旧同步版本的证据不能替代本次验收。

| 编号 | 场景 | 必须满足 |
| --- | --- | --- |
| A01 | 工作线维护 | 唯一编码/版本；配置及§10绑定合同保持，无业务启停/状态接口 |
| A02 | 归属 | 单归属或未分组；批量冲突全回滚；配置删除解除归属 |
| A03 | 工作线删除竞争 | 有成员409，无成员可删，不因历史路由阻塞，无悬空外键 |
| A04 | 预览 | 配置/状态库零写入；分类/来源准确；只保存限时预览 |
| A05 | 确认登记 | 只选NEW；设备/路由/审计原子提交；无状态行、HTTP或刷新任务 |
| A06 | 预览安全/重放 | actor/绝对期限/选项/编码锁/history_count保持；锁后和提交前过期全回滚；删除后旧NEW token不能重建 |
| A07 | 并发登记 | 全局唯一赢家；失败无部分设备/路由/审计 |
| A08 | 删除重登记 | 新配置ID/route_id，不继承旧归属/状态/业务键；旧ID各操作404 |
| A09 | 异址同码 | 查询/预览不覆盖地址，只有有效预览+版本+人工确认+原因可迁移 |
| A10 | A→B→A | 新UUID路由；旧快照不改；查询旧成功/失败409，发送门禁仍按§10 |
| A11 | 删除边界 | 仅删当前配置；历史命令/结果/证据/审计/路由保留，迟到Result与停发合同不变 |
| A12 | 查询身份 | 网络期间无DB锁；返回前复核ID/version/route/token；删除404、变化409，不按code重找新行 |
| A13 | 列表纯配置 | 零ECS HTTP、零状态字段；仅配置筛选/分页；status/freshness筛选400 |
| A14 | 详情调用 | GET详情零ECS请求；详情状态和按钮同用POST刷新；失败不阻断配置读取 |
| A15 | 单次状态 | 成功只返回本次描述/状态与时间；无90秒标签或上次状态回退；不推断OFFLINE |
| A16 | 严格响应 | 缺字段/错类型/重复身份或JSON键/枚举或身份非法/超限，整次无结果、零状态写入 |
| A17 | 扩展/null | 未知扩展与允许null兼容，已知字段不强转 |
| A18 | 异常 | 本地404与上游404区分；429/502/504/503安全来源明确，无重试/跳转/改址 |
| A19 | origin协调 | 5秒冷却/15秒租约跨API实例有效，Retry-After不截短；迟到429不释放新租约 |
| A20 | 取消后台 | Beat/include/自动投递移除；业务读写、启动、空闲均不发状态请求；其他任务保持 |
| A21 | 查询零业务写入 | 不改设备名称/归属/version/updated_at，不建状态/查询历史/替代表，不写管理变更审计 |
| A22 | 管理员 | admin成员+操作权限、撤权、菜单与组合操作规则保持；read不授予refresh或网络查询 |
| A23 | 正式路由 | 当前配置/绑定决定新解析及发送；历史查询/迟到Result仍按§10，状态不授予执行许可 |
| A24 | 框架 | 单一Sender、WMS STATIC、local dev v3、角色注册、UNKNOWN/HELD与原合同回归保持 |
| A25 | 未发布迁移精简 | 从父版本/新装升级只有三张配置表；唯一head及绑定保持；空库升降级、有历史拒绝降级；不执行已有旧库删表 |
| A26 | Mock出口 | 只允许持有的loopback目标；非法输入/无权限/旧ID零HTTP，TLS/无代理保持 |
| A27 | 通用独立 | 无设备表/DB/Redis时注入客户端仍能查询；无业务存储/调度依赖 |
| A28 | 多ECS | 来源不串；单次设备刷新不聚合其他origin；A失败不使独立B请求失败 |
| A29 | 全局编码 | 跨origin当前目录唯一；历史同码不占用名额；无自动登记/删除/迁移 |
| A30 | 协调故障 | Redis不可用503；移除DB last_attempt恢复路径，启动保护及数据丢失限制如实验证，无新日志表 |
| A31 | 统一目录 | Status仍仅一条内置能力；不成为第四业务注册表 |
| A32 | 来源 | 查询响应带device_id/device_version/current_route_id及StatusQueryResult来源；预览时间不续写；历史路由可追溯 |
| A33 | 目录无副作用 | 目录构造零HTTP/设备扫描/状态存储，原认证/local可见性保持 |
| A34 | 单次预算 | HTTP5秒/256KiB及有界释放；无自动重试/补偿任务，协调不侵入通用层 |
| A35 | 正式HTTP | 通用入口所有环境挂载，权限独立、旧dev无副本、Swagger与原API限流保持 |

分层验证：通用协议独立Mock；配置/DTO/权限HTTP；一次性PG/Redis验证登记、迁移、身份复核及租约；独立迁移数据库验证精简后的新装/父版本升级、回退和历史守卫。共享工作区有并行修改时先固定同一基线，不把旧方案的通过数或并行基线失败算作本修订通过。

## 13. 当前确认与历史记录

已确认：通用Status/目录/来源独立；多ECS、全局设备编码、预览确认、人工迁移、删除后新ID重登记；管理员成员与操作权限并行；§10角色分发、配置失效停发、历史及迟到Result合同保持。

本次取消后台30秒同步、观测表、90秒新鲜度、列表状态筛选、最后成功状态回退及登记/迁移后的自动刷新。当前保留HTTP5秒/256KiB、origin冷却5秒/租约15秒、上游429至少30秒且不截短合法等待、预览300秒。配置/路由/审计仍事务一致，状态仅按需返回。独立观测表不是历史执行保留的条件。

本轮已按用户授权实施按需B1–B8，验证证据单列于实施计划。既有B1–B8、代码审查和simplify的测试记录只证明当时版本；保留在实施计划历史部分，不代替本轮验收。角色增量的实现状态及证据见[角色增量计划](../plans/2026-10-27-ecs-role-dispatch.md#完成与验证)；本次不重做或回退该专项，也不把历史状态同步通过数当作本修订证据。

迁移精简依据为用户确认及本地只读版本核对，不能以未提交推断数据库未升级；其他环境不得未经核对复用同revision修改。真实ECS字段/时间语义、现场规模/HTTP预算、Redis持久化、前端及发布仍未在本修订验收。不猜测供应商语义，不做真实发送，也不把现场验证纳入本轮开发完成条件。

历史设计审阅记录：2026-10-27，旧生命周期方案曾完成26项、随后34项设计审阅；这些是此前版本记录，不作为本次删除重登记方案的审阅结论。

此前删除重登记版本修订记录：取消归档/恢复及对应字段、状态、接口、权限；明确当前配置删除、同编码新ID重登记、独立原路由、管理员专用管理及当时的35项验收条件。独立builder check指出NEW预览在“登记→删除→重放”时缺少持久失效依据，已补齐历史路由条数标记、一致快照及编码事务锁/同事务提交规则，无额外消费表。该审阅针对当时版本，不作为本次角色分发、配置失效停发及迟到Result适配的通过结论；本次验收以更新后的合同为准。
