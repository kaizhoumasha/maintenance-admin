# Changelog

## [Unreleased]

### 粗分双机配置与角色路由

- 粗分工作线声明唯一出料单层货架位 OUTPUT_RACK，支持 RS01→CLN01、RS02→CLN02，不配置输送线出入口或面向。
- 继续从 device_code 解析工作线角色绑定，按原角色选择命令设备；新 MOVE_FORWARD 附录 /2 保留数量校验，Result.data 仅保留 actual_qty，执行位置由 ECS 按设备及动作管理。错工作线设备不能完成原命令，未知/失败结果不会另发替代命令。
- 双线闭环与配置按 MOCK 和内部验证验收；联调部署及只读 Status 检查另行记录，不代表现场设备动作或完整粗分业务通过。

### 粗分扫码、首个输送闭环与 ECS 调试

- 粗分可通过同一套正式业务装配接收扫码，按工作线向配置的 WMS 验证；仅验证成功后下发原条码的 MOVE_FORWARD，并消费原 Result。成功后等待箱格分配，业务 NG 保留原响应等待下一阶段；NG 搬运、箱格分配和入箱仍未实施。
- 开发者可在现有 Swagger 选择 ECS Event 或有效命令声明的完整请求样例，投递事件并按摘要查询持久收据及业务投影。命令调试继续沿用通用合同、权限、稳定请求标识和结果查询。
- 粗分合同统一使用 barcode 和工作线上下文，扫码及验证请求不重复携带 data 内时间字段；设备、位置和验证对端由既有配置提供，Transport 保留自己的交互合同。
- 扫码事实、原请求身份及输送证据可跨重启和收据清理保留；并发唤醒、投递未知及不完整结果不会产生替代命令或自动推进后续阶段。新增迁移保留既有事实，并拒绝丢失业务证据的降级。
- 以上能力按 MOCK 与内部验证验收，正式清单当前登记粗分的上述阶段；不表示真实对端、设备、部署或现场验收通过。

### 业务开发计划状态

- 阅读业务开发计划时，可明确区分已交付能力、历史实施约束与当前验证边界，避免将已完成增量误认作待实施任务。

### 工作线业务交互草案与内部验收

- 人工、粗分和自动线可从统一索引查阅拟议的共用货架离场合同，明确三个请求字段、未知面向及尚未注册、实施的边界。
- 新增自动线逐盘授权、位置上报和架面推进草案，补充同码盘次关联、原报告重放、冲突拒绝及待实施的 MOCK 验收场景。
- 粗分草案补全正常、NG、补架和换架交互，明确固定位置责任及总高度类型相对设备 v3.3 的拟议调整；合同确认与内部验证、真实设备验收分别说明。
- 忽略本地 `.DS_Store` 目录元数据文件。

### 业务注册 catalog 归属校验

- 正式校验通过后，业务工厂或方法首次导入仍须满足 catalog 入口归属；非法来源在注册前立即失败且不占用注册键。失败复验不再重新开放首次注册阶段，补齐三类入口的懒导入与合法恢复回归，同步设计、计划和 MOCK 验收说明。
- 正式 ECS Event、WMS callback/request 注册必须属于显式 catalog 声明的入口模块或包；间接导入未列业务、同业务未声明入口或相似名称均阻止启动与目录输出，保留合法跨业务引用及平台/local 边界。补齐冷启动负向与重新校验回归，同步接入规则和 MOCK 验收记录。


### 业务开发单一 API 与 MOCK 验证

- 新增业务只需使用模型发送 WMS 请求、创建 ECS 命令；命令通过 `device_roles` 和 composition 的 `COMMANDS` 声明，由显式 catalog 装配。业务目录统一为 `app.businesses`，移除旧字符串/字典调用、手写绑定回退及 integrations 根目录扫描。这是业务 Python 接入方式的不兼容变化。
- API 与 worker 都在初始化前加载并交叉校验全部正式合同，再加载 local 入口；缺失或不匹配的合同会阻止启动，避免后台进程带病工作。
- 合法命令模型保留原字段别名、日期/元组的 JSON 表达和自定义序列化结果；原字段先校验，序列化输出不再被误当作 Python 输入二次校验。调试入口仍严格拒绝非法 JSON 并执行原权限检查。
- 单据和设备两类 MOCK 闭环覆盖并发受理、原身份重入、冻结目标、结果消费与恢复；历史命令、字节、路由、期限和 Result 合同保留。正式业务清单仍为空，内部验证不代表真实对端或现场验收。
- 接入指南、设计与验收记录统一到唯一用法，九份已完整替代的旧文档移出仓库原样归档，仓库引用转向现行文档。

### 授权与 WMS/Transport 边界修复

- 删除角色与授权变更共用事务锁，避免并发分配后被级联解绑，导致最后一个管理员丢失权限。
- WMS 响应中的溢出数字保持 UNKNOWN；读取历史终局响应时不再暴露为有效业务数据，原始响应字节仍保留。
- Transport 回调接受合同允许的完整货架、料箱、槽位和位置编码，保留任务身份、面值、非法字符与正文大小的独立校验。

### 业务接入简化阶段一

- 可在应用启动前独立检查 WMS operation、ECS Event 及现有 Command/Result 静态合同，明确正式/local 范围、原声明来源、失败阶段和未检查边界；无需数据库、Redis 或 JWT 配置，不执行业务处理或运行时装配。
- 合同发现不再因 core 公共导入而初始化运行设施，保留原 API/worker 导出、配置校验、日志对象及旧 WMS/ECS 合同；补齐原身份、请求字节、期限和历史 Result 的兼容回归。
- 提供当前 API 的接入路线、受理事务、独立快照推进和失败恢复配方，并记录隔离验证。正式业务清单仍为空；后续模型调用、命令声明和目录收敛已实现，现行方式及验证边界见上方单一 API 说明。

### 工作线业务子包与出站结果消费

- 工作线可关联已部署的业务，按业务声明维护设备角色和位置绑定；已有工作线保留未选择状态，已选业务首期不可切换或清空，业务选项复用现有管理权限。
- 业务通过 Python 集中声明、显式清单和简单工厂接入已有 WMS、Transport、ECS 与 Outbox；启动先加载正式入口并交叉校验实际合同，再装配业务服务，避免入口缺失或角色附录不匹配时带病启动。
- WMS 请求和搬运任务可按原身份读取可信终局响应、已应用结果及明确前序状态；未知位置和历史里程碑分开，提交接纳不等于搬运完成。业务推进以独立统一快照核对整组前序，消费依据与下一条意图同事务提交，失败后整组重读。
- ECS 事件忽略额外调试字段，由 ECS 自行决定调试状态；已有收据保留原身份与原响应，升级不回放历史事件。部署须协调切换身份算法，不支持新旧算法实例混跑。
- 新增工作线业务字段迁移 `a6d464b20ab7`，保留旧数据并拒绝丢失已选业务的降级。本次交付公共接入能力，真实业务清单保持为空；人工分拣与粗分业务尚未接入，登记完整业务前不能新建业务工作线。

### 设备、工作线与按需 ECS 状态查询

- 管理员可从多个 ECS 预览设备后确认登记，维护工作线和设备归属；设备编码全系统唯一，同编码换址须明确确认。删除当前配置后可重新预览登记，保留原路由、命令、结果、证据和审计历史。
- 正式 `GET /api/v1/ecs/status` 在所有环境提供需登录、独立权限和限流的状态查询，带回 ECS、设备和查询时间来源；统一接入目录可查看能力及代码来源，不增加第四类业务注册表。
- 设备列表和 GET 详情仅返回配置，详情状态与手动刷新共用原刷新接口；状态按需获取，不建观测表、不后台同步。查询后复核配置与请求代次，拒绝删除重登、换址或后发请求替代后的旧结果。
- 设备管理以适配器接入已有独立 ECS 角色框架，保留原路由校验和发送门禁；工作线列表保留分页选中的配置并批量统计设备，避免并发删除导致整页失败。
- 新增配置及绑定迁移 `11e090516bf1`、`c31f20261027`，历史非空时拒绝破坏性降级；未发布、未应用的基础迁移直接精简，不创建 `device_observations`。

### ECS 独立角色分发与运行时绑定

- 相同事件或任务可按业务角色分别声明 Payload、处理函数和严格命令附录；角色以代码常量定义，注册阶段无需设备、工作线管理或 Status。
- 运行时注入工作线绑定和路由解析，配置变化阻止旧命令后续发送，保留原执行记录及有交付依据的迟到结果；沿用原收据、outbox 和单一 Sender。
- 角色目录与 local 协议调试可独立装配，模拟器和真实 ECS 复用既有地址、权限及合同边界；设备管理、Status 和相关迁移另行交付。

### 后台正确性与开发入口加固

- 多位管理员同时停用用户或移除管理角色/权限时，串行保护检查并保留至少一位有效管理员；普通资料修改保持原流程。
- 缩短刷新令牌有效期后，全部登出仍能撤销旧、新有效会话；签发和轮换保留刷新索引原有的较长寿命及永久索引。
- 可靠投递中的异常提供安全的失败阶段和源位置，保留原 STATIC/SNAPSHOT 状态、交付证据、取消与落库失败传播，不记录原始异常正文、SQL 或凭据。
- 多个路由可独立声明同一合法权限码，继续逐请求鉴权、即时撤权并保留审计动作和 OpenAPI 403 说明。
- 当前接入文档归并可靠投递、Transport 合同及开发测试入口；历史计划和过期指南在保留有效合同后移出仓库，原文可按归档清单核验。

### ECS 通用命令接入与开发教程

- local Swagger 可直接指定 `ecs_url`、全系统唯一的 `device_code`、`task_type` 和 JSON `params` 联调，保留稳定的 `client_request_id` 与执行 `timeout_ms`；主启动自动装配通用协议合同和唯一发送通道，无需逐动作登记、逐地址清单、联网开关或手工配置 ECS 可靠目标。新登记仍需登录及专用发送权限，模板不提供默认真实设备地址。
- 调试与正式新命令统一使用 300 秒投递窗口，路由窗口缺失、非法或不一致时返回明确的配置错误；同身份重入及历史命令保持原编号、地址、字节和冻结期限，Sender 停启不重算退避或 Retry-After。UNKNOWN/HELD 保留证据，不自动重放、换址或解封，WMS/STATIC 行为不变。
- 启动可从可信模块保留历史版本附录；新调试结果支持完整 JSON 对象，旧结果继续按原严格模型验证。缺原模型的首次结果拒绝，已持久化的重复结果仍返回原响应；旧调试策略不恢复发送资格。
- 最小业务示例以单条动作记录保存意图、命令关联和消费状态，事务内登记与结果消费共同回滚；消费核对当前权威许可、设备绑定与业务版本，保留旧路由撤销/未撤销改址及取消/换绑/版本竞争的回归。
- 统一最小业务接入与 Swagger 短教程，直接使用本地 Mock 或开发者指定的 ECS 地址；不要求额外的 ECS 隔离阶段。测试复用普通 pytest 夹具和真实启动流程，模拟地址出口保护仅属于测试。移除独立测试应用包装及旧隔离文档，历史记录移出项目；供应商、现场和生产验证独立于开发结项。

### ECS 命令与结果闭环

- 新增事务内命令登记与 owner 限定查询，冻结调用身份、请求字节、设备路由与截止时间；结果入口 `POST /api/v1/callback/result` 持久保存首次响应，结果、命令状态与停止投递同事务完成，短期收据清理后仍可安全重报。
- ECS 接纳 ACK 与设备成功/失败分开表达。发送前复核原路由授权；结果未知、租约中断或身份冲突进入 HELD，保留证据并等待对账，不自动重放、不因 TTL 或终态清理丢失未决状态。现有 STATIC/WMS 协议、事务及重试行为保持不变。
- 新增迁移 `a025a20261025`，保留旧 STATIC 数据，存在 ECS 证据或快照/HELD 数据时拒绝破坏性降级。升级前须停用旧 Sender 与维护进程；模板不提供真实设备路由、附录或默认发送装配。

### 安全日志诊断

- HTTP、Sender、维护任务与 Celery 共用安全错误诊断，提供失败阶段、SQLSTATE、受限缺失对象/约束名和调用位置；不输出原始异常、SQL、参数、异常链或源码，原响应及任务执行结果保持不变。
- Sender 各实例独立汇总连续故障与记录扫描恢复，同一 SQLSTATE 下缺失对象或约束变化立即报告；满并发跳过领取不误报恢复。Celery 兼容 `throws=None`，失败与重试日志保留诊断和脱敏，不改写任务结果、重试或 ACK/requeue。


### 接入注册统一(破坏性内部 API 变更)

- ECS 事件和 WMS 回调统一声明名称、Payload 模型与异步处理函数；WMS 请求直接注册 `RequestPayload` 模型，新增接入无需定义外层 operation 类。迁移说明见 [接入指南](docs/integrations/registration.md)。
- local 开发控制台新增需登录的 `GET /api/v1/dev/integrations/registry`，一处查看三类实际注册项、来源文件、local-only、请求端点和 Payload Schema；非 local 环境不挂载。
- 文件名统一为注册名先转小写，再把 `.`、`@` 换成 `_` 并加 `.py`；非法名称、重名、文件名或声明错误给出来源与修复方法。保留单模块和入口包根声明，不强制逐项文件名。
- local-only 按实际接入入口及注册时作用域记录；生产项在 local 作用域内首次注册会报错，名称中包含 `dev` 不会绕过检查，缺失入口和已缓存模块的发现仍可安全执行。
- 移除旧 `CallbackOperation`、`RequestOperation`、`TransportCallback` 类及动态适配；Transport 共享流程改为普通函数，协议、ACK/回显、身份重放、TTL、加锁与事务回滚语义保持。
- 独立临时 PostgreSQL/Redis 全套验证 `1870 passed, 7 skipped`，跳过均为 SQLite 参数下不适用的 PostgreSQL 专属检查；补齐注册失败不占名称、入口兼容、原始事件清单与本地示例回放的行为测试。

### ECS 事件回调接收

- 新增 `POST /api/v1/callback/event`(`app/integrations/ecs_callback/`、`app/controllers/ecs_callback.py`):接收 ECS 设备事件,wire 按白皮书不变。校验信封,非法回 `400 INVALID_ENVELOPE` 加 `issues[]`(不回显值);身份是 `device_code`、`event_type`、`timestamp`、`is_debug`、`data` 的 SHA-256 摘要,写入 `callback_receipt`(operation `ecs.event`,operation_id 为 64 位摘要);先去重再校验 `data`,重报返回首次的 `200 ACK`;按 `event_type` 分发到 `@ecs_event` 函数式处理器;未注册的事件类型回 `200 ACK`,事件完整存入收据诊断(至多 32 KiB,更大时只存摘要、大小和 1 KiB 预览)。目前没有任何具体事件类型。
- 校验错误列表保持首次出现顺序并按字段、错误码及期望类型去重;大量数组错误不再逐条扫描已有列表。补充 HTTP 合同测试,覆盖错误路径与脱敏、合法模型事件接收及重报只应用一次。
- 迁移 `a024a20261024`:`callback_receipt.operation_id` 加宽到 64 位;`callback_snapshot` 新增可空列 `source`(ECS 写 `ecs`,旧行与 WMS 为空)。降级遇到 64 位身份行或非空 `source` 时拒绝而不丢数据。
- 共享改动(WMS 回调行为基本不变,两处小差异:读取请求流中途失败时,WMS 的 503 快照现在存空请求体而不是已读前缀;诊断告警的 logger 名现为 `app.integrations.callback_diagnostics`):诊断快照限额按来源分组并记录 `source`(ECS 高频不占 WMS 的 120 条/分钟,限额按进程分别计数);诊断队列抽到 `app/integrations/callback_diagnostics.py`;收据插入与查询抽到 `app/integrations/callback_receipts.py`;请求体分块读取抽到 `app/core/request_body.py`。
- 限制:不内置 `ESTOP_PRESSED` 回 400(白皮书 §4.2 条款待供应商确认);未注册事件没有回放;同毫秒同内容的两个真实事件会被当作重报;新增事件类型须先部署处理器、再在 ECS 侧启用,否则旧实例会按未注册 ACK 并占住身份。处理器契约见包说明。部署前提:对外开放 `/api/v1/callback/event` 之前,网关必须先对它做 IP 白名单或限流(与 WMS 回调入口同样的信任假设,但每个未注册事件可持久化约 32 KiB,而不是约 0.5 KiB 的摘要行)。
- 验证范围(2026-10-24,包含校验错误去重优化和合法模型事件重报测试):`ruff check`、`ruff format --check` 通过,`alembic heads` 单头 `a024a20261024`;对一次性本地 PostgreSQL 17 容器(`TEST_DATABASE_URL`)执行 `pytest -ra`:1781 通过、22 跳过、0 失败。跳过项:15 个 Redis 用例(未设置 `TEST_REDIS_URL`,未运行),7 个仅 PostgreSQL 的用例在 sqlite 参数下跳过(PostgreSQL 参数下已执行)。未验证:生产 PostgreSQL 与真实数据的 schema 迁移、真实 ECS 设备或供应商实现的联调。

### 内部整理

- Celery 任务的数据库入口合并为 `app/tasks/_db.py` 的 `run_with_factory`（NullPool 引擎、单次 `asyncio.run`、同循环内释放）；联调控制台的模块扫描改用 `import_module_tree`。行为不变。
- `test_service` 的配置辅助函数隔离进程环境里的 `RELIABLE_TARGETS`，避免本机环境变量污染用例。

### WMS 请求端点与 operation 组织

- 修复请求端点 ACK 校验：非法 code/reason_code 类型和不符合公共响应联合闭集的内容均按 `UNKNOWN` 保留不确定性并重试，不能转成本地永久失败或误判终局；校验 facts/准备接纳的空 data 及拒绝响应可选 `field_path`，不解释 `DECIDED` 业务结果、不改变 Transport 分类。
- operation 的 JSON Schema 示例改用严格 JSON 模式验证，合法 datetime/date/UUID/Decimal 字符串可正常注册，非法示例仍在导入期报错；增加完整投递链和注册行为回归。
- 新增 `app/integrations/wms_request`：WES → WMS 的 `decisions`、`facts` 两个端点（协议 `wms-decisions`、`wms-facts`），提供 operation 注册、信封冻结、公共响应分类和事务内入队 `WmsRequests.send`；目前没有任何具体 operation，每个 operation 对照合同确认后再单独添加。两个端点都走可靠层，业务暂时读不到 WMS 响应。
- 入站与出站 operation 的发现支持「单个模块或目录」：`<业务>/callback`、`<业务>/request` 为目录时，一个 operation 一个文件并整目录导入，文件名为注册名把 `.`、`@` 换成 `_`，有测试检查一致。Transport 回调据此拆为 `wms_transport/callback/`，行为不变。
- `WmsRequests.send` 同身份、同 Payload 重入时返回原消息引用:`Outbox` 新增可选的适配器端口 `same_request(existing_raw, new_raw)`,适配器可以声明字节不同但内容等价(例如只有时间戳变了);没有该方法时仍要求字节完全相同,同身份不同内容仍抛 `DuplicateMessage`。
- 联调界面(仅 `ENVIRONMENT=local`):`/api/v1/dev/wms/requests` 提供 operation 目录、触发入口(Examples 由注册表生成)和按 `operation_id` 的状态查询;Mock WMS 增加 `/api/v1/wes/facts`、`/api/v1/wes/decisions`,新增 `busy`(429 带等待提示)与仅 decisions 生效的 `prepare_accepted` 模式;联调需在 `RELIABLE_TARGETS` 里为 `wms-facts`、`wms-decisions` 各配一个目标,缺少时启动给出 WARNING。联调请求体按 JSON 模式校验 Payload,超过目标 `max_body_bytes` 返回 400。
- Transport 与 `wms_request` 共用合同 §4 的响应前置检查 `read_ack`,以及 `targets_for_protocol`、`target_config_hint` 两个目标查找辅助函数;Transport 的响应判定与报错文字不变。
- 补全 `app/integrations` 各 `__init__.py` 的职责与开发方式说明；接入指南和离线实验同步到当前代码，实验改用 `create_database_engine` 建表。

### PostgreSQL Schema 隔离

- ORM、API、可靠发送器与 Celery 数据库连接统一使用 `enterprise_backend` schema，SQLite 测试映射为空；自动索引保留原名称。
- 在线 Alembic 原子移动已确认归属且版本为 `a023a20261024` 的完整 12 张项目表及版本表，保留数据、索引、序列与对象身份，不触及其他 public 表；更早版本、缺表或同名冲突拒绝搬迁。新库直接创建独立 schema，PostgreSQL 离线 SQL 明确拒绝。
- 增加旧库数据保全、同名冲突回滚、外部 ledger 隔离、运行连接及自动迁移差异检查；部署文档补充停机迁移与回退边界。

### 项目改名与定位

- 项目统一命名为 `enterprise-backend`，定位为面向 WMS、MES、WES 等生产企业业务开发的通用企业级后端管理平台模板，提供第三方与内部 API 对接基础能力。
- 同步应用默认名、日志器及 Celery 应用名、镜像默认名、项目元数据、模板改名脚本、文档和现有测试引用；可靠交互层仍属后续范围。
- 固定默认 Compose 项目名为 `enterprise-backend`，新项目的数据卷使用该前缀；已有部署切换前须先备份并迁移数据，或显式指定原项目名继续访问原卷。
- 数据库名统一为 `enterprise_backend`，独立测试库使用 `enterprise_backend_test`；同步 Compose、本地环境示例、CI、模板改名脚本及文档。已有库先备份再原地改名，保留数据及数据库身份，相关客户端同步连接名；仅修改 `POSTGRES_DB` 不会改名已初始化的数据库。
- API 路径及权限码不变；已有部署继续沿用原 Compose 项目名、数据卷和站点配置，切换前须完成备份和数据迁移。历史发布标签及固定镜像不因改名而变化。

### 待发布：纯 HTTP 客户端（SPEC-019）

- 新增可注入的纯异步 HTTP 客户端，随应用安装提供；支持单次原始字节请求、阶段与总超时、连接池和请求/响应大小限制。
- 完整响应不受关闭异常覆盖，取消和异常后可释放连接资源；诊断日志不输出正文、凭据或查询值。
- 查询统一通过独立参数传入，拒绝内联查询和路径点段以保护目标前缀；明文 HTTP 不依赖系统 CA 文件，HTTPS 保持证书与主机名校验。

### 待发布：可靠层发送预算与 WMS Transport 出站（SPEC-019）

- 可靠层新增按目标可选的未送达预算（`max_not_received_attempts`，默认不设）：只统计「确认未送达」，达到预算且从未出现过结果未知的消息转为 `STALLED`——保留原身份与字节、不再发送、不过期、不被保留期清理，并输出告警日志；仅调用方显式 `Outbox.resume` 可放回 `PENDING`（提交后记日志）。曾出现结果未知（响应丢失、租约中断或回收）的消息不再受预算约束，继续以原身份重提。
- 新增可选 `fixed_retry_interval_s`，使本地重试等待固定；等待提示规则不变。数据库迁移新增 `not_received_count`、`delivery_uncertain` 与 `STALLED` 状态，升级时把存量 `PENDING`/`SENDING` 消息保守标记为未知；降级遇到挂起消息或未知事实时拒绝。
- 新增 `app/integrations/wms_transport`：WMS Transport 提交协议适配器（合同 §4.2 的 ACK 分类）与能力层 `TransportService` 的四个搬运方法（`move_rack`、`rotate_rack`、`move_bins`、`exchange_bins`）——校验、冻结请求字节、`client_request_id` 同事务幂等入队（先抢唯一键，仅赢家入队）；位置与当前面的有效性交给 WMS 在接纳前校验（主动偏离合同 §3.2 的本地失败关闭：`rotate_rack` 的 `position` 只接受精确位置 `RackPosition`，原样写入源与目标，错误由 WMS 以 409/503 返回）。新增表 `transport_task`，降级时非空即拒绝。本变更不启用任何真实目标，入站回调与位置投影不在范围内。
- 新增货架搬运业务接入层 `rack_scenarios.cycle(role=, rack_id=, caller=)`：以不可变的生命周期句柄表达「进场 → [换面…] → 离场」，角色只给一次，进离场模板的配对由构造保证（五层货架 `CTU01/CTU02/CTU03`，转运与退料货架 `F01/CK04/F01`），目标位置统一为 `RackPosition | Zone`；不强制步骤顺序，句柄可被多个并发调用方共享。默认实例首次使用时从 `RELIABLE_TARGETS` 惰性取得 `Outbox` 与目标，业务代码无需接线；`app/main.py` 已把 `ADAPTERS` 注册给可靠发送器，是否真正发送由部署环境是否配置 `wms-transport` 目标决定（`.env.example` 有示例）；协议没有适配器的目标会在启动日志中告警。`move_rack` 与 `rotate_rack` 按场景限定 RCS 模板（`CK04` 仅换面），换面模板必须显式传入。
- 新增料箱业务接入层 `bin_scenarios`：`feed_bins`（投料：货架储位 → 滚筒线入料位置）、`return_bins`（回架：出料位置 → 货架储位）、`exchange_bins`（1～2 对交换）；方向由场景固定，合同规则由能力层执行，字节由契约测试固化。
- 可靠层配置错误现在会指明配置名与出错目标（`RELIABLE_TARGETS target 'wms': …`），异常类型不变。

### 待发布：联调控制台（Swagger 统一出站入口）

- 仅 `ENVIRONMENT=local` 可用：在 Swagger 里用 `POST /api/v1/dev/transport/tasks` 的 Examples 下拉选择业务场景（货架进场、换面、离场，料箱投料、回架、交换），经真实业务接入层登记出站，再用 `GET /api/v1/dev/transport/tasks/status/{business_key}` 查看投递状态与请求、响应原文；服务层显式提交事务，保证发送器能看到消息。
- 业务接入层新增 `app/integrations/<业务>/dev.py`，用 `@dev_scenario` 登记场景即可自动出现在下拉里，无需改 `main.py`；非法名字、重名、非法示例在导入期报错。
- 内置 Mock WMS（`POST /api/v1/wes/transport-requests`，`PUT /api/v1/dev/mock-wms/mode` 切换 accept、duplicate、conflict、reject、unavailable、garbage、hang 七种响应），把 `RELIABLE_TARGETS` 指向本应用自身即可端到端验证重试与终态；响应与 Transport 适配器的判定由契约测试保持一致。
- 新增 WMS 回调通用接收 `POST /api/v1/wms/events`（Transport 回调接收设计第 1 步）：有界读取（256 KiB）、严格 JSON（拒绝重复 key 与 NaN）、`operation + operation_id` 幂等收据（原信封指纹相同则稳定重放首次响应；已注册 operation 的规范化有效字段相同也可重放，否则 409）、合同形状的 `422 REJECTED`/`503 UNAVAILABLE`、独立诊断快照（≤64 KiB，每进程每分钟限量，默认保留 7 天，由 `CALLBACK_SNAPSHOT_RETENTION_DAYS` 配置并每日清理）。`operation` 上限 256 字符，超长或缺失返回 `422` 且不建收据。该路径豁免全局限流（`RateLimit(None)`）；严格 JSON 解析提到 `app/core/strict_json.py` 与出站适配器共用。未注册的 operation 得到持久化的 `422 UNSUPPORTED_OPERATION`；Transport 注册状态见下文，启用门槛见 `docs/deploy.md`「WMS 回调接收启用门槛」。新增表 `callback_receipt`、`callback_snapshot`，收据表非空时拒绝降级。
- 联调控制台新增回调投递（仅 local）：`POST /api/v1/dev/transport/{transport_task_id}/callbacks`（`succeeded`、`position_unknown` 两个场景）按任务冻结事实构造样例位置事件与结果，逐条走真实 `receive` 管线并返回各条响应与诊断；`GET /api/v1/dev/transport/{transport_task_id}` 查看终态、已应用结果、料箱位置与暂存结果。结果版本自动取已应用版本加一，可先 `position_unknown` 再 `succeeded` 观察收敛。
- 新增 Transport 回调接收与应用（回调接收设计第 3 步）：`transport.task.member_position_changed@v1` 与 `transport.task.resulted@v1` 两个 operation（`wms_transport/callback.py`，自动发现），在请求事务内锁 `transport_task` 行后直接应用位置里程碑与结果（版本登记、终态规则、货架/料箱判定表、料箱结果等位置到齐后在同一次处理里完成），不再设证据表与后台扫描。所有 ACK 与身份冲突的 `409` 回显 `transport_task_id`（`CallbackOperation.echo_keys`）；未知任务、冻结缺失、事实矛盾等只告警的情形仍返回 `202`，诊断写入 `callback_receipt.diagnostic`（`ctx.note`）；PostgreSQL 回调事务设置 `lock_timeout`，超时返回 `503`。迁移 `a021a20261023` 给 `transport_task` 增加 `frozen/member_positions/outcome_status/applied_outcome/applied_outcome_revision/outcome_digests/pending_outcome`，并从 `reliable_message.request_raw` 回填既有任务的 `frozen`；已保存回调结果时拒绝降级。登记任务时写入 `frozen`。任务收到任何回调即用新增的 `Outbox.settle` 把其提交消息（`PENDING/SENDING/STALLED`）转为 `DONE`、结束发送（不写响应；正在发送的租约失效）。不做 `STALLED` 定时对账：`STALLED` 仅在目标配置了未送达预算时出现，默认不会出现，对端长期不可用靠告警与显式 `Outbox.resume`。位置事实按集合累积、与到达先后无关，`POSITION_UNKNOWN` 事件只留诊断不存状态（wire 不变）。尚未启用真实目标，网关放行门槛见 `docs/deploy.md`。
- 新增卡住任务告警：Celery beat 每 10 分钟扫描 `transport_task`，创建后超过 `TRANSPORT_STUCK_ALERT_MINUTES`（默认 120，暂定）仍无确定终态的任务输出结构化告警日志（`SUBMIT_NOT_DELIVERED`、`RESULT_MISSING`、`RESULT_WAITING_FOR_POSITIONS`、`OUTCOME_UNKNOWN`），只读，不改任务状态。
- 修复告警遗漏：卡住任务按主键每批 200 条续扫，避免持续卡住的前批遮蔽后批；回调诊断在收据事务成功提交后输出结构化告警，提交失败不输出已接纳告警。
- 评审加固：无认证回调入口的回显与 Transport 文本字段限长 256 字符并拒绝无法按 UTF-8 编码的值（超长或孤立代理字符不再被写入收据，避免永远无法渲染的响应）；`transport_task` 迁移回填只取该任务自己的提交消息；迁移不再读取列结构不符的外部 `public.alembic_version`；卡住任务告警不再重复告警创建超过 7 天仍无结果的任务。
- 升级提示（仅影响联调前的开发库）：迁移 `a020a20261022` 在发布前被原地改写——表名由早期的 `inbound_*` 改为 `callback_receipt`/`callback_snapshot`，版本号未变。曾用旧版本升级过的开发库会停在 `a020` 却没有 `callback_*` 表，之后 `a021` 报 `relation "callback_receipt" does not exist`。处理：确认旧表只含联调数据后删除旧名字的表，`alembic stamp a019d20261022`，再 `alembic upgrade head`；或直接重建开发库。
- `transport_task.cycle_id`（迁移 `a023a20261024`）：货架一轮生命周期（进场、换面、离场）的本地分组键，不进入 wire、冻结请求与幂等摘要；`RackCycle` 自动生成，后续步骤重建句柄时传入同一个值，料箱场景提供可选参数；控制台任务状态与换面/进离场请求体同步支持。
- Transport 提交消息统一带 1 小时超时（`SUBMIT_TTL`，暂定）：1 小时仍未送达的提交转 `EXPIRED`，不再重发；联调前与 WMS 确认其重发窗口同样不超过 1 小时。
- 回调收据按 `RELIABLE_RETENTION_DAYS`（默认 90 天）每日清理（`callback.cleanup_receipts`，迁移 `a022a20261023` 为 `callback_receipt.created_at` 加索引）；合同要求的清理周期变更须在联调前与 WMS 确认。
- 新增回调 operation 注册（回调接收设计第 2 步；写法已简化为继承 `CallbackOperation` + 内嵌 `Payload(CallbackPayload)` 声明字段 + 实现 `execute(ctx, payload)`，校验、规范化、422 响应与 Swagger 示例由 `Payload` 生成，`Accepted/Duplicate/Conflict/Rejected` 既可 return 也可 raise，返回 dict/None 即接纳）：`@callback_operation("域.名称@vN")` 注册处理器类（`parse(data)` 纯函数校验业务 DTO、`handle(ctx)` 在保存点内写业务行并返回 `Accepted/Duplicate/Conflict/Rejected`）。通用层先占收据再调用处理器，`Rejected` 显式回滚处理器写入，先构造并校验响应再写回收据；处理器异常或响应不可序列化时整体回滚并返回合同形状的 `503`。已注册 operation 的结构合法消息启用有效摘要比较（仅冗余字段不同视为同一消息，`timestamp` 或已定义字段改变为 `409`）；此前保存的 `422` 在注册上线后仍稳定重放。新增示例 operation `example.echo@v1`（`outcome` 取 `accept/duplicate/conflict/reject` 演示四种结果，不写业务表），位于 `wms_callback/dev_callback.py`，仅 `ENVIRONMENT=local` 被自动发现；生产环境自动注册 Transport 的两个 operation。operation 模块自动发现：`app/integrations/<业务>/callback.py` 所有环境导入、`dev_callback.py` 仅 local，类属性 `title`/`example` 自动生成 Swagger 请求体示例，新增 operation 无需修改控制器或 `main.py`。
- 部署必须显式设置 `ENVIRONMENT`：默认值 `local` 会同时挂出该控制台；控制台只校验登录、不校验权限码，Mock 接收端无认证，仅限本地联调。

### 既有 0.2.0 发布准备记录

以下保留原项目 `maintenance-admin` 的 **0.2.0** 发布准备记录；该版本已建立 `v0.2.0` 发布标签。记录中的候选基线验收不自动覆盖后续改名或 HTTP 客户端变更；dirty 验收包不能作为正式制品。

### 发布范围与支持平台
- 本版本交付系统管理平台的后端模板:认证与会话、用户/角色/权限、动态菜单接口、审计与归档、列表筛选分页、队列任务,以及单机 Compose 部署和备份恢复工具。
- 固定镜像交付目前仅验收 **linux/arm64**。其他架构镜像(包括 linux/amd64)须单独构建并完成部署、升级及回退验收,不能直接使用 arm64 制品。
- 配套管理端前端不在本次发布范围内;动态菜单接口验收不等于真实管理端浏览器联调通过。
- 不包含实际生产站点部署、站点 TLS 证书或真实配置、业务数据库备份和前端静态文件。站点配置按 `docs/deploy.md` 在发布包外独立保存、审阅和挂载。

### Added
- 生产站点 nginx 配置通过覆盖文件只读挂载包外副本,发布包原件继续校验;恢复完成后要求选择目标发布目录,不再提示开发启动命令。
- 离线发布准备工具、含摘要的发布清单、固定镜像生产配置与旧镜像加载/核对流程;四个应用服务复用同一镜像。
- 容器部署：nginx 同域反代（前端静态文件 + `/api`）、api 可多实例、Redis AOF 持久化、服务自动重启、Postgres 备份/恢复脚本，以及部署/升级/回退文档 `docs/deploy.md`。
- 用户、审计和归档列表支持字段白名单筛选、条件组合与严格输入校验。

### Changed
- 管理员初始化复用用户创建输入约束,非法参数不写种子数据且命令行不回显密码;合法参数的授权及重复执行行为不变。
- 容器内应用读取 `.env` 的全部配置（令牌时长、审计保留天数等）；数据库密码改用 `POSTGRES_PASSWORD`；api 不再发布宿主机 8000 端口，经 nginx 访问。
- 回归测试改为验证公开接口契约，减少对内部实现的依赖，并集中覆盖分页参数与 OpenAPI 错误响应。
- 审计和归档列表改用 `limit/cursor`，响应改为 `{items,next_cursor}`；旧 `page/page_size` 返回 400，三个列表均拒绝未知查询参数。
- OpenAPI 统一声明参数校验、认证、权限和限流错误，保留各路由的业务错误，移除不会返回的 422 声明。

### Removed
- 删除不再使用的 `PageQuery` 别名；用户列表继续使用原有分页和总数。

### 升级注意事项
- 审计和归档列表调用方须迁移到 `limit/cursor` 与 `{items,next_cursor}`;用户列表仍使用 `page/page_size`。三个列表均拒绝未知查询参数,完整契约见 README。
- 生产入口改为 nginx,API 不再发布宿主机 8000 端口;Compose 要求至少 2.24.4。前端与 API 须同域,生产 refresh Cookie 须经 HTTPS 使用,网关信任地址须按实际站点限定。
- 管理员初始化及重复执行均须提供合法用户名、邮箱和密码;已存在账户的邮箱、密码保持原值,仍会补齐 admin 授权并允许多端登录。
- 升级前先备齐新旧固定制品,停止 nginx/api/worker/beat 后制作最终备份;备份成功并校验后才迁移启动。恢复点之后的所有写入都会丢失,回退前须确认损失窗口可接受。
- 回退使用旧镜像归档,不现场重建、不依赖 Alembic downgrade。恢复前更换 JWT 密钥;数据库切换后 Redis 清理失败时仅重试清理,成功后明确选择旧发布目录启动。

### 验收结果与剩余边界
- 干净 develop 基线 `b8ee483d943e330b519dbf92d395ca69a652fde8` 的 0.2.0 候选包已确认 `source_dirty=false`,并完成 linux/arm64 隔离固定镜像部署、包外站点配置重载、停机备份取回恢复及旧镜像回退;回退目标是此前保存的 0.2.0 验收快照,不是实际生产旧版本。
- 该基线完整隔离 PostgreSQL/Redis 测试为 366 项通过、0 跳过,PR 与合并后 CI 通过。上述记录属于候选基线证据,不自动覆盖后续提交或重新构建的正式制品。
- 待验收:真实管理端浏览器联调、真实异机传输与取回权限、其他架构镜像、宿主机或 Docker 守护进程重启后的整套恢复。独立目录复制不能替代异机验收,进程异常重启不能替代宿主机重启验收。
- 模板发布不代表生产站点可直接上线。实际站点上线前须核对 TLS/可信代理/密钥/目录权限/项目名,备齐真实旧制品及恢复点,完成异机备份取回;离线交付还须保存清单指定的 PostgreSQL/Redis/nginx 镜像。
- 后续变更正式发布前仍须完成 Git Flow 发布流程,固定最终提交与制品并验收实际交付包;本节记录不代表当前分支制品或生产站点验收通过。
