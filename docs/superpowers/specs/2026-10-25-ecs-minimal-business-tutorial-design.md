# ECS 最小业务示例与入门教程设计

日期：2026-10-26。状态：**GENERIC_PROTOCOL_VERIFIED / WINDOW_DECISION_APPROVED / SUPPLIER_CONFORMANCE_PENDING**。Task 17设计决策已批准，Task 18–19已实现并在无.env副本、一次性PostgreSQL/Redis与本轮模拟端验收。W=300秒、D=确定终态后至少24小时、活动/未决去重保留及跨重启保证仍为统一最低合同；真实供应商符合性留给Task 10。本轮不提交/推送/合并、不执行Task 10、不迁移本机数据库或连接设备，保留原暂存区、其他改动和stash。验证证据见实施计划。

## 1. 目标与交付选择

面向会编写普通 service 和数据库事务、不了解 ECS 底层实现的开发者，提供一篇能顺序阅读的中文教程。读者应能回答：需要提供什么合同和配置、一次性装配放在哪里、业务代码如何创建命令、如何安全消费结果，以及怎样在隔离环境观察完整过程。

用户已选择前轮推荐的最小业务示例与教程方案。具体载体采用以下第一项，为本次设计选择：

| 方案 | 收益与代价 | 决定 |
|---|---|---|
| 连续教程 + 已有可运行测试参考 | 教程完整解释业务代码；运行入口复用现有 pytest、参考事务及回环对端，避免两套逻辑漂移。运行示例仍需了解基础测试命令 | 推荐采用 |
| 新建独立 examples 程序或 CLI | 可脱离 pytest 演示，但会重复目录、业务表、装配和资源清理；当前没有必要 | 暂不建设 |
| 新增生产业务模块或生产控制台 | 增加业务、权限及真实误发送范围 | 不采用；仅提供§10的local-only薄联调入口 |

交付为[最小业务接入教程](../../integrations/ecs-command-tutorial.md)，从现有 ECS 接入说明和 README 可达；最小业务示例统一采用§11的单表状态机，现有Reference及调用测试已原地迁移，无平行示例或旧接口适配。Swagger入口只适配既有ECS服务，不复制业务消费实现或创建业务表；请求级ecs_url、通用协议合同和专用权限的新登记规则以§10为准，无启动地址回退或兼容入口。教程须自包含解释关键代码，不能仅要求读者自行研究测试夹具。当前可运行示例以明确命名的已有测试入口运行，不把摘录中的 pytest helper 宣称为框架 API。

本阶段不新增生产路由、业务模型、迁移、命令装饰器、事件处理器、消费框架、重试器、长期运行实验平台或依赖。不以指定外部业务项目为开工门槛；wes_backend 只作经验参考，不移植其业务规则。若盘点发现真实底座缺陷或无法在现有公开接口内演示的行为，先记录复现与最小范围，不借教程扩展协议或生产能力。

## 2. 当前底座及实际缺口

生产能力复用 `app/integrations/ecs_command/`、`app/reliable_runtime.py`、`app/controllers/ecs_result.py`。命令不是自动发现的 operation，当前没有 `@ecs_command`；不能套用 ECS Event/WMS 注册教程来声称完成命令装配。

| 已有内容 | 可复用来源 | 本阶段处理 |
|---|---|---|
| 参数、附录、SNAPSHOT 目标及 Outbox 装配 | `tests/integrations/ecs_command/conftest.py` 的 `Payload`、`ecs` fixture | 解释并摘录现有装配，保留源码定位 |
| 最小业务示例 | 同文件 `Reference`、`reference` fixture | 原地迁移为单表动作状态机，统一登记、消费及变更接口 |
| 一次命令与结果的真实回环 HTTP | `test_flow.py::test_loopback_command_result_and_reference_consumption`、`loopback_server` | 作为端到端运行入口，复用成功/失败与 ACK 前后参数 |
| 登记与消费事务、重复、并发及旧授权 | `test_service.py`、`test_query.py`、`test_concurrency.py` | 建立教程步骤到已有行为测试的映射，只补可复现缺口 |
| 接入参考说明 | `docs/integrations/ecs-command.md` | 保留参考属性，新增教程导航，不整体重写 |

当前源码为单表Reference、请求URL版Swagger及v3通用协议合同/专用权限门禁；逐动作合同清单、逐地址/设备清单、旧启动地址及联网开关已删除。§10为当前合同与实现，Task 18–19的新验证与Task 14–16历史证据在实施计划分开保留。Reference、两条许可、两个P2和P3用例保留，不自行合并PR或变更分支基线。

## 3. 最小业务合同与读者路径

教程统一采用“一条动作记录对应一条命令、一个固定业务消费者、同库副作用”的单表模型，字段及状态机见§11和教程§4–5。默认测试合同为D1/MOVE、严格qty:int、可信owner=test、附录(test,1)、执行timeout 1–60000ms/正常30000ms、五分钟发送窗口；这些值不代表供应商或生产默认配置。

读者依次准备合同与配置、一次性装配、事务内登记、观察ACK与Result、安全消费、验证失败及竞争边界、清理隔离资源。示例只在测试中建业务动作表，不新增生产模型或迁移。路由A/B测试数据只证明原授权与改址行为，不是生产ECS主数据模型。

## 4. 配置和一次性装配

本节说明正式业务/已有Reference的严格装配；Swagger通用入口按§10自动装配，不要求普通开发者复制此处的附录或路由代码。

教程提供两张配置表：必需的协议/权限输入，以及运行资源输入。每个字段标明来源、单位和使用阶段。

| 配置 | 必须解释的要求 |
|---|---|
| 逻辑通道 | 应用configure_ecs接收routes/annexes，生命周期自动加入统一SNAPSHOT通道；不用在RELIABLE_TARGETS配置ECS，地址来自设备路由/通过联网门禁的请求并冻结 |
| 路由与授权 | `DeviceRouteProvider.resolve(session, device_code, task_type)` 返回 `RouteSnapshot`；`authorize_frozen(session, route)` 检查原授权并返回 ALLOW/REVOKED/UNKNOWN。缺失/无法确定不能默认允许 |
| 附录映射 | `(annex_id, annex_version)` → `DeviceAnnex(params_model, result_model, error_model, timeout_min_ms, timeout_max_ms)`，原版本应可用；真实合同待另行确认 |
| 三类时间 | 命令执行 timeout、单次 HTTP timeout、允许发送的 retry_window 分别解释；等待结果没有自动失败/重发期限 |
| 运行资源 | 一次性数据库/Redis、测试 JWT、local 环境、空主应用目标；测试内部目标由 fixture 显式建立，不读取本机 .env |

明确区分以下两种装配，不能同时启动两个 Sender：

1. **本教程可运行入口**：使用已有 `ecs`/`reference` fixture 建立 Outbox、EcsCommands、附录及生命周期；回环测试建立最小 Result FastAPI 应用并注入测试 session factory/service，由唯一 `Sender`/`MessageStore` 与 `DynamicClients` 有界执行并关闭。不启动 `app.main`、Celery 或本机开发服务。
2. **应用一次性接入说明**：通过configure_ecs统一设置app.state.ecs_commands、适配器、生命周期及动态客户端；主应用lifespan自动合并统一ECS通道并关联当前outbox，保留WMS映射。local装配由dev入口自动调用，正式装配须显式注入设备路由和附录，不启用默认生产地址。

教程中可复制运行的命令必须给全环境及上下文；只解释应用迁移的片段须标明不可独立执行。不能把自动逻辑通道描述为真实地址授权或供应商合同确认；未装配 Result 返回503是保护行为。

## 5. 事务内创建与框架职责

现有Reference原地迁移为单表动作实现。已有命令关联按原输入重入；首次登记通过§11的current_business读取当前许可/绑定/业务版本，并以权威业务记录→动作行的统一锁序或等价版本保护后检查READY。动作关联、原绑定/版本、WAITING_RESULT与ECS命令/outbox同一调用方事务提交，savepoint不代替提交。字段和新接口以§11及实施计划为准，不保留旧调用映射/消费表或兼容入口。

框架生成并冻结编号、时间、字节、origin、附录与截止时间，唯一Sender负责投递及原规则重试；业务不直接发送或另套Celery。A→B生效不自动撤销A，旧命令保持原地址/身份，独立新意图检查当前授权；UNKNOWN不能靠换键或改址绕过。

## 6. 安全消费

保护权威业务事实及动作行，按可信owner查询公开DTO，检查原command_code、当前许可/绑定/版本与冻结原值一致、结果存在且无hold/待核对，再将WAITING_RESULT转换为SUCCEEDED/FAILED，与同库业务副作用共同提交。重复消费不推进，写失败共同回滚，重试不创建命令。

取消/换绑/版本变化与消费共用动作行锁或已证明等价的条件更新；变更先提交旧结果不推进，消费先提交终态不被覆盖。迟到矛盾保留证据并核对，不自动撤销已提交业务。消费由业务显式触发，不增加结果总线、轮询器或消费HTTP入口。具体状态转换及适用限制统一见§11。

## 7. 隔离运行的教程规格

隔离验收按下列要求执行；教程和Swagger路径分别记录实际命令及证据。所有命令从新建隔离目录复现，不依赖前轮临时日志、私有脚本或运行中的开发环境。

1. 从当前已审阅源码生成临时副本，排除 .env、.git、.venv、缓存和现场配置；不修改原工作区。使用 Python3.13+、uv及锁定依赖，在禁止外网运行阶段之前准备依赖或测试镜像，不新增仓库依赖。
2. 提供两条清楚分开的路径：SQLite 快速理解路径只说明可验证的顺序行为；pytest完整路径使用专属一次性PostgreSQL/Redis及internal Docker网络，不开放数据库端口、不挂宿主Docker socket。回环HTTP在测试容器内绑定127.0.0.1随机端口。Swagger浏览器路径只为API增加专属入口bridge（关闭IP masquerade）及宿主127.0.0.1随机端口，数据库/Redis仍仅在internal网络；关闭IP masquerade不等于出口隔离，正向发送必须由§10.6的测试客户端限制目的端。Sender使用请求选定并冻结的回环模拟origin；本应用Mock示例使用API容器自己的回环地址。
3. 完整路径显式注入 TEST_DATABASE_URL/TEST_REDIS_URL 及测试 DATABASE_URL、REDIS_URL、JWT、ENVIRONMENT=local、空主应用 RELIABLE_TARGETS；不继承代理或现场目标。连接异常必须失败，未配置而跳过不算完整验收。
4. 运行前列明资源名/标签、网络、连接目标和清理归属。`pg_url` fixture 会对 TEST_DATABASE_URL 执行 Alembic upgrade：只有确认是本次一次性库才允许运行；不能把“只是跑 pytest”当作无需检查数据库的理由。
5. 使用现有定向 pytest 入口依次观察创建/回滚、正常HTTP闭环、安全消费、重复、P2/P3及未知交付；保留成功和失败、ACK前后参数。每步给出预期的状态/副作用，不用固定全套通过数量代替行为断言。
6. 结束时在成功、失败、取消路径均关闭 Sender/回环服务、dispose engine、清理临时参考表；再只移除本次容器、匿名卷、网络和镜像标签。命令不得使用全局 prune、默认业务 compose down 或清空原工作区。

教程需给出具体可复制的资源创建、环境注入、运行和清理命令，不能止于安全原则。缺 Docker 的读者可以只跑已标注的 SQLite 路径，但不能据此报告 PostgreSQL 锁竞争已通过。

## 8. 复用覆盖与只补缺口

| 教程验收点 | 现有行为证据 |
|---|---|
| 同键重入、冻结及当前许可 | `test_duplicate_preserves_route_identity_and_bytes`、`test_new_reference_intent_requires_current_permission_but_replay_keeps_identity`、`test_new_authorized_reference_intent_uses_current_version` |
| 创建共同回滚和并发 | `test_reference_registration_and_outbox_rollback_together`、`test_concurrent_reference_registration_preserves_original_mapping` |
| P2路由及P3未撤销改址 | `test_pending_route_keeps_active_route_and_revocation_only_fences_old_call`；复用 revoked=False/True |
| 撤销/发送竞争及授权缺失 | `test_persistent_route_revocation_serializes_with_send_gate`、`test_missing_original_authorization_does_not_allow_dispatch` |
| 消费一次与业务失败回滚 | `test_example_consumer_advances_once_in_its_own_transaction`、`test_consumer_write_failure_rolls_back_marker_and_can_retry` |
| P2业务竞争及迟到冲突 | `test_consumption_and_business_change_follow_lock_order`、`test_result_does_not_override_owner_mapping_permission_or_conflict`、`test_late_result_conflict_does_not_undo_consumed_business` |
| 完整回环与未知 | `test_loopback_command_result_and_reference_consumption`、`test_unknown_delivery_is_held_and_restart_does_not_retry`、`test_interrupted_snapshot_lease_holds_until_matching_result` |
| 结果事务、原附录及长期证据 | `test_http_commit_failure_rolls_back_result_receipt_and_settlement`、`test_missing_original_annex_returns_retryable_503_without_result_evidence`、`test_result_survives_receipt_and_delivery_cleanup` |

先运行/阅读等价覆盖再决定是否补测试；将现有Reference及调用方统一迁移，保留原测试所证明的行为，不保留旧表/旧API/兼容别名，不复制第二套示例或协议/Sender，不将测试文件互相导入。文档链接/符号检查采用一次性核验，不保留精确文本快照、夹具接线 smoke test 或为本次重构存在的过渡测试。确有未覆盖的长期可观察行为时，才在所属现有测试文件加回归。

## 9. 文件与验收条件

| 文件 | 后续允许的修改 |
|---|---|
| `docs/integrations/ecs-command-tutorial.md` | 新增连续教程，含自包含讲解、真实API来源及隔离命令 |
| `docs/integrations/ecs-command.md`、`README.md` | 添加最短导航，保留原有参考及启用边界 |
| 本设计及对应实施计划 | 后续按实际实施/核验同步状态，明确历史与新证据 |
| `tests/integrations/ecs_command/conftest.py` 及上述已有测试 | 统一迁移Reference及调用方、清理被替代的表/helper；复用长期行为覆盖并只补缺口，不更改生产协议和语义 |

完成标准：读者无需先找外部项目或真实设备，能按文中步骤运行示例、辨认业务/命令/投递三种状态、说明ACK为何不能消费、复现重复/回滚/P2/P3/UNKNOWN边界，并安全清理本次资源；代码摘录与当前参考源码一致，命令在干净隔离副本实际执行且结果如实记录。

历史验证只在实施计划保留；请求URL版的实际命令、退出码、环境和测试定位须实施后新增记录，不得引用历史通过数代替本轮验证。供应商合同C1/C2/C3、实际授权C5、Result来源限制、发布迁移回退和现场容量/物理/业务验收仍未关闭；本教程不代替这些门槛。

实施任务见[实施计划](../plans/2026-10-25-ecs-minimal-business-tutorial-plan.md)。行为基线见[隔离参考设计](2026-10-25-ecs-first-consumer-isolation-design.md)和[ECS接入说明](../../integrations/ecs-command.md)。


## 10. 无设备表的ECS联调入口：内置通用协议合同

### 10.1 已选方案与当前实现

普通开发者直接在Swagger提供ecs_url、device_code、task_type和params，不逐动作编写模型、手工登记动作或维护动作清单。框架内置通用协议合同并在主lifespan自动装配，继续复用唯一ECS逻辑通道、outbox、DynamicClients及共享Sender，不另建直接HTTP转发路径、配置表、逐地址清单或联网开关。正式业务的设备路由、严格附录与业务许可继续由业务接入承担。

状态为GENERIC_PROTOCOL_VERIFIED / WINDOW_DECISION_APPROVED。Task 17设计决策已关闭，统一最低合同见§10.4；Task 18–19的v3通用合同、共享窗口与实际启动已隔离验收。新数字见实施计划，Task 10独立且未执行。

### 10.2 请求、权限及地址

- 保留POST /api/v1/dev/ecs/commands及既有字段形状。核心输入为ecs_url、device_code、task_type、params；client_request_id仍是必填稳定调用键，timeout_ms仍按现有非负int64毫秒传入，不新增默认执行timeout。两者不是逐动作装配。请求不接受owner、合同版本、窗口、mock/real类别或权限自报。
- DEVICE_CODE全系统唯一；新增设备、动作或改址只改请求。task_type按统一Text约束验证，不做本方动作白名单；合法但对端不支持的动作由ECS按既有协议拒绝，不由后台报“未装配动作”。
- ecs_url必填，沿用HTTP/HTTPS IP字面量origin规范化、固定/api/v1/device/command路径、无凭据/query/fragment/非根路径、无DNS/重定向/环境代理及非法/未指定/多播IP拒绝规则；不新增地址登记或探测。
- 仅ENVIRONMENT=local挂载，必须登录及具有dev:ecs:send_real；回环、外部模拟和真实目标规则一致。401/403不得产生新命令/outbox或HTTP；合法输入且框架统一策略就绪不再因动作未登记返回503。缺统一策略或存储/运行时依赖仍503并明确原因，不能把动作检查或统一策略缺失/非法伪装成数据库故障。

### 10.3 内置合同与职责边界

| 内容 | 框架统一校验 | 动作语义归属 |
|---|---|---|
| 命令 | 原CommandEnvelope闭集、字段类型、priority=1、非负int64 timeout/timestamp、JSON合法性及原体积限制；params必须为对象，允许嵌套合法JSON，不裁剪/强转业务字段 | 动作是否支持、params的具体字段和值由对端ECS校验；正式业务创建前另做当前许可及业务输入校验 |
| ACK/错误 | 复用原完整响应、JSON媒体类型、严格code/message及状态组合分类，包括429/Retry-After、503和UNKNOWN | 不将HTTP成功或ACK视为设备/业务完成，不放宽供应商协议 |
| Result | 原command/device身份、SUCCESS/FAILED、finish_time及ErrorDetail(code,msg)条件规则不变；data按通用JSON对象校验并保留全部内容，顶层诊断扩展处理沿用原规则 | 合法Result只证明收到ECS报告；正式消费仍校验业务结果、当前绑定/版本/许可并与业务副作用同事务 |

调试适配已内置唯一合同标识ecs.dev.protocol/1，复用DeviceAnnex端口和现有创建/Result校验，不新建合同平台或注册表。params/data为开放JSON对象，不混用测试qty模型或(test,1)，也不通过extra忽略业务字段。执行timeout范围使用现有统一信封值域0..2**63-1，不把测试附录的1–60000ms当统一设备保证；供应商动作限制属于对端/业务合同。

新调试路径已删除逐动作装配参数及按task_type查附录的旧用法，不保留双模式、兼容别名或“找不到动作才通用回退”。核心configure_ecs/DeviceAnnex仍供正式严格接入与历史结果使用；普通开发者不手工装配通用合同。测试专用启动仅建立本轮模拟端、受限客户端工厂及可选历史依赖，沿用生产W=300秒，不覆盖另一个测试窗口或重新登记动作。

### 10.3.1 唯一装配与受限工厂的注入顺序（P2修订）

可信Python接口为configure_local_ecs(app, *, environment, history_annexes=None, client_factory=AsyncHttpClient)，普通主lifespan继续只传environment。底层configure_ecs(app, *, routes, annexes, client_factory=AsyncHttpClient)仅增加可选客户端工厂依赖，复用DynamicClients(factory=client_factory)；不增加provider实例输入或放宽现有重复装配保护。工厂仍是同步的HttpClientConfig→客户端构造函数，真实发送由原客户端/原Sender负责。history_annexes与client_factory仅供受信任启动/隔离测试代码注入，不是Swagger字段、环境变量、动作登记或新运行模式。

装配顺序固定如下：

1. 隔离启动先建立并持有本轮两个模拟端，捕获规范化origin集合，再绑定受限client_factory、已批准的生产300秒策略及可选历史附录输入。此时不提前调用装配函数，不写app.state.ecs_commands/reliable_client_provider，不替换为测试服务或测试通用模型。
2. 进入真实主lifespan。它发现尚无ECS服务，唯一一次调用生产configure_local_ecs；测试可在该调用点用启动包装/现有monkeypatch绑定可信依赖，包装必须委托原函数，不绕过主lifespan分支。正式历史升级也可由受信任启动模块一次调用同一生产函数并传history_annexes；这种显式启动不代替默认自动路径的独立验收。
3. 原装配首先检查服务/provider/协议映射冲突，校验并合并合同，再构造路由、EcsCommands、EcsLifecycle及使用受限工厂的唯一DynamicClients；构造完成后统一发布app.state映射。不能先把provider实例塞进app.state来传依赖，也不能遇到冲突就清空/覆盖原状态。
4. 主lifespan将该服务关联唯一outbox，再启动原共享Sender。启动Sender前provider已受限，借用客户端时先验证origin，不获准即在TCP前拒绝；不能启动不受限Sender后补限制。关闭先结束Sender/客户端，再退出模拟端和测试注入上下文，成功/异常均清理并恢复测试包装。

同一装配在普通启动选择默认工厂，在隔离上下文选择受限工厂，通用模型/服务/lifecycle均是生产实现。测试不得提前造出完整ECS服务使主lifespan跳过自动装配，也不得为了过冲突校验而重置本次启动中的已装配状态。这些步骤已通过真实主lifespan的隔离回归，不预装替代服务。

### 10.3.2 历史附录的可信合并路径（P2修订）

history_annexes是可选的(annex_id, annex_version)→DeviceAnnex映射，来源仅限受信任启动模块已版本化的原Python模型；无历史依赖时默认为空。它不来自请求、数据库中的可执行内容或环境字符串，不新增自动发现、历史合同表或注册平台。原annexes/actions参数已从调试入口删除，不作为history_annexes的别名继续接受。

生产configure_local_ecs在创建服务前复制并校验该映射，和框架内置ecs.dev.protocol/1一次合并到同一个service.annexes；路由/lifecycle/Result共享这一个服务。历史键必须是非空字符串二元组，值必须为DeviceAnnex；与内置保留键("ecs.dev.protocol", "1")冲突一律拒绝，即使调用方认为模型相同也不能覆盖。无效输入在发布新服务之前报明确装配错误，不修改输入映射、现有服务或WMS映射。缺少某个历史键不扫描数据库补模型、不阻断启动，该命令尚未持久化的Result按原规则503。

新v3路由始终选择内置合同，authorize_frozen仍先验证可识别v3策略与内置合同标识；历史映射不是允许动作表，也不赋予v1/v2/grant快照发送资格。绝不通过第二次configure_ecs创建另一套服务，不在启动后更新service.annexes来冒充合并，不把旧严格模型替换为通用JSON模型。原结果校验继续按命令冻结的原键精确查找：有原模型则严格校验，无原模型且尚无持久结果证据则503、零副作用；持久重复证据优先返回原响应。是否可应用仍取决于原发送证据、封锁及共同事务规则。

### 10.4 enterprise_admin统一ECS最低合同（Task 17设计决策已批准）

决策日期2026-10-26，依据为本轮用户明确批准的项目最低合同。参考白皮书v1.1规定同command_code/原载荷不得重复驱动，但没有给出保存期；本项目现在主动规定下表数值及保证范围，不将它们追认为供应商既有承诺。状态为WINDOW_DECISION_APPROVED / SUPPLIER_CONFORMANCE_PENDING，Task 17仅在设计层关闭；v3已完成本方隔离验收，Task 10仍独立未完成。

| 项目 | 已批准最低要求 | 依据、起点与范围 |
|---|---|---|
| 本方投递窗口W | W=300秒（5分钟） | 从本方首次创建命令时起算，含排队/退避/停机；expires_at=created_at+300秒。给短时故障和剩余窗口足够时的合法Retry-After留恢复空间，并限制长故障后的陈旧发送；不从首次发送或重启重新计时 |
| 对端去重保留期D | D=确定终态后至少24小时（86400秒） | 对端ECS持久确认该命令确定终态后，至少继续保留去重依据24小时；覆盖日内维护/恢复与延迟重复，允许更长。不是首次接收后24小时无条件清理，也不是本方可靠记录的90天保留配置 |
| 活动/未决命令 | 去重依据不按TTL清理 | 已接纳但排队、执行、恢复核对或结果未决时持续保留，不因经过24小时或重启而清空；不能通过临时写一个终态来启动清理倒计时 |
| 适用范围 | 本项目统一ECS Command/Result最低合同的所有接入目标 | 内置ecs.dev.protocol/1是本方模型身份，dev.ecs.network/v3是本方快照策略版本，不等于供应商协议版本；不自动扩展到WMS/RCS或任意HTTP接口 |

W=300秒与既有测试窗口同值，但本次采用依据是已批准的时效性/恢复取舍，不是测试通过数，也不从设备执行timeout、HTTP timeout、Retry-After上限或Transport TTL推导。W只是本方开始新发送尝试的期限，不保证设备在5分钟内接纳/执行完，也不是等待Result的截止时间。D=86400秒是本项目最低规范选择，不是通用行业常数或现成供应商保证；数值上满足0<W≤D，并通过未决期持续保留覆盖跨终态边界。

**内部策略落点：**Task 18已在app/integrations/ecs_command/contracts.py定义唯一PROTOCOL_RETRY_WINDOW=timedelta(seconds=300)，由调试及正式路由共同引用，EcsCommands.create统一校验新命令后冻结；configure_local_ecs只负责自动装配，不承担唯一的W校验。普通请求/可信启动均不增加窗口参数、.env项或逐动作/地址配置。D是对端合同，不是本方新增清理任务/数据表，不把本方既有长期证据或可靠记录改成24小时删除。隔离启动同样使用生产300秒策略，不再覆盖另一个测试默认；TEST ONLY只标识模拟目标、历史夹具和隔离环境。

**D的计时与有限保证：**terminal_at指对端ECS自身持久确认确定终态的时刻，不用本方收到回调的时刻或载荷finish_time代替。清理去重依据的最早时刻为terminal_at+24小时；未确定terminal_at时禁止按该期限删除。重启、时间异常或最后一次重复请求不能缩短保证期；允许保留更久。至少保留原命令身份、原载荷指纹、原接纳事实、执行/终态标识和原协议要求的结果。该合同保证活动/未决全过程及终态后至少24小时，不宣称永久去重；原Result重报义务不缩短，回报未确认时不得因D到期清理原结果及去重依据。本方仍不得在W后自动重发旧命令，D到期也不构成换号重做UNKNOWN/HELD的许可。

**跨重启最低保证：**

1. ECS进程正常重启/异常退出、容器或主机重启及断电恢复后，原接纳记录及去重依据仍可恢复，不能只保存在进程内存。持久接纳记录先于ACK与物理派发，同身份并发请求不能产生第二份执行义务。
2. 在保证期内，同command_code/相同原载荷先查原接纳事实，再作新任务容量判断；已接纳的重复请求返回原接纳事实，不因设备busy/服务恢复而改称503/429未接纳。同号不同载荷按原协议409冲突。真正未接纳的合法503/429仍允许后续按原身份接纳，不能缓存成永久拒绝。
3. 数据库持久化与机械执行之间的崩溃窗口仍存在。无法确认原动作是否已触发/完成时进入恢复核对，不将原命令当新动作再次启动，不伪造SUCCESS/FAILED；核对/继续原命令须有ECS既有恢复依据，不新增wire状态或取消协议。
4. 恢复出厂、去重存储丢失、回滚旧备份或无法证明恢复完整时，不能以空状态直接恢复受影响控制域接纳，须先核对。503只能用于能够证明本命令未接纳的情形，不能把可能已接纳的原命令谎报为明确未接纳；本方仍按原UNKNOWN/HELD处理。
5. 重启不重算本方W或缩短对端D。窗口更新/当前新登记策略缺失只影响新登记，不改写既有v3原窗口/deadline，也不据此暂停存量。原退避、Retry-After、TTL、合法迟到ACK/Result及共同事务不变。

**Task 10边界：**本轮批准的是本项目规范，供应商当前版本、配置、D保留、跨重启/断电恢复及物理行为均未验收。Task 18–19只实现和隔离验证本方；Mock/故障注入可说明预期响应及本方处理，不能证明真实ECS满足D或跨重启要求，也不要求本项目另做一套供应商ECS存储实现。Task 10须单独授权并取得供应商协议/配置、持久恢复/保留证据、原身份重复与冲突、实际物理执行及Result来源/网络/现场许可。对端不满足最低合同则不判通过，不按某地址静默缩短D或降级重试。W/D不新增wire字段、真实发送开关或逐地址清单。

实现保留统一策略缺失/非法时新登记503、零命令/outbox/网络的防御；它不是批准真实发送的替代门槛，不阻断主应用/WMS/Result启动、旧owner只读重入或既有合法快照。该负向用例独立于自动内置300秒的正向启动验证。Task 17的数值、来源及范围已决，后续改变须显式修订合同，不能在实现中自行调整；Task 18–19已隔离验收，本轮未执行Task 10。

### 10.4.1 调试与正式新命令的统一创建约束（P2修订）

W=300秒是所有通过EcsCommands.create创建的新ECS命令的固定规则，包含Swagger/调试和正式业务，覆盖各owner、target及正式路由版本；不是只对dev.ecs.network/v3生效的默认值，也不是由实现者决定是否采用的上限。D和供应商验收边界不变。仅新增命令受该规则约束；旧调用/存量消息仍按原冻结事实处理，不追溯迁移。

**唯一策略来源：**在app/integrations/ecs_command/contracts.py定义PROTOCOL_RETRY_WINDOW=timedelta(seconds=300)，通过app/integrations/ecs_command/__init__.py导出。dev.py、正式DeviceRouteProvider及service.py均引用同一常量，不从dev层反向依赖正式核心，不保留另一份常量/可覆盖默认。正式提供方保留RouteSnapshot.retry_window字段及原接口，返回共享常量；不从请求、.env或逐动作配置取值。既有源码/测试的五分钟字面值虽等价，Task 18已将示例调用方迁移为共享常量。

**创建顺序及不一致处理：**

1. EcsCommands.create仍先按可信owner/原call_key查找已存在记录并比较原输入。已存在同输入时直接返回原只读视图，不解析当前路由、不校验其当前窗口，也不要求旧冻结窗口改成300秒；异输入保留原身份冲突。调用键、输入比较和查询语义不增加策略/窗口字段。CommandView及Swagger响应字段不变；“保留原期限”通过命令/消息持久状态核对，不新增deadline响应字段。
2. 仅在首次创建路径，解析并验证原路由/设备/动作后，由共享service.py在生成新command_code/字节及插入命令/outbox之前统一检查：route.retry_window必须为timedelta且精确等于PROTOCOL_RETRY_WINDOW。60秒、600秒及299.999/300.001秒均拒绝；不取整、不静默截短、不放大、不忽略路由值，也不增加正式路径豁免。
3. 不一致属于服务端路由/协议配置错误，沿用现有AppError/HTTP 503错误映射和响应信封；message/detail明确“ECS retry_window必须为300秒，请让路由提供方引用PROTOCOL_RETRY_WINDOW”，不归为400非法业务参数，不宣称数据库连接失败，不新增全局错误码或改WMS/对端wire错误矩阵。RouteSnapshot构造也将缺失、错误类型及非正窗口识别为配置503，避免先报400/500；发送重建损坏快照仍保守围栏，不按当前300秒改写旧期限。非HTTP调用方同样收到明确配置错误；服务不自行提交事务，拒绝零新增命令、快照和outbox，零HTTP。
4. 校验通过后沿用原首次创建时刻，冻结同一300秒到route JSON、EcsCommand.expires_at和消息ttl/expires_at，三者一致。继续复用原唯一键竞争及最终赢家校验，取得最终赢家后提交前的origin复核不弱化；事务失败仍共同回滚。

**存量与权限边界：**共享常量只参与首次创建，不能在Sender领取/发送/恢复、Result或旧owner只读重入时用它重新推导存量deadline。合法旧快照即使冻结60秒或600秒，也不改成300秒；正式原授权仍按原规则，dev v1/v2/grant的已有围栏仍保留，不因保留旧期限而放行旧策略发送。UNKNOWN/HELD不解封。正式业务许可、原路由授权/严格附录不变，不把dev:ecs:send_real移入正式核心；统一W也不提供真实发送许可。

**验收必须包含正式入口：**现有ECS测试中新增/扩展正式DeviceRouteProvider→EcsCommands.create路径，300秒时命令/消息冻结deadline一致；60/600/299.999/300.001秒新调用均配置拒绝且零义务/HTTP，与调试入口一致。用合法历史夹具准备原冻结60/600秒记录：同键同参在当前路由返回非法窗口或抛错时仍只读返回原身份和原窗口，异输入仍冲突；不调用当前resolve。对原授权仍有效、符合原安全重试条件的正式存量消息，Sender停启继续使用原next_attempt_at/期限/字节，旧HELD保持HELD；历史dev策略仍围栏。以上P2回归已在Task 18–19执行，实际新证据见实施计划。

### 10.5 v3策略、旧快照与事务

新调试快照沿用route_id=dev.ecs.network，version="3"；冻结ecs.dev.protocol/1、请求动作、原origin、窗口/deadline、编号和原字节。只扩展调试新登记的协议接受范围，正式路由及其授权版本不变；URL、策略/合同版本和窗口不进入owner/call_key或ECS wire。复用既有RouteSnapshot字段，不新增表或迁移。

v1/v2、旧grant及未知策略均保守围栏，不改版本、补授权、转换、改址或自动重放，不保留旧发送兼容路径。原owner同键同输入同规范化地址可只读重入原命令；异输入/换址409，并发唯一键最终赢家返回后、提交前复核持久origin，输家零发送。命令/outbox/快照仍共同提交或回滚。

统一窗口更新或当前新登记策略缺失，不改写既有v3的原窗口/deadline，也不据此暂停存量；发送仍核对可识别原策略、原合同和冻结快照，不回退当前值。

原附录及原发送证据保留用于历史Result，不以通用合同解释原严格附录。合法在途/ACK/UNKNOWN结果仍按原可应用条件及原附录处理；尚无持久结果证据且缺原附录时503、零副作用；已有持久重复结果优先沿用原响应，不重新解释附录。未发送或因策略围栏的命令不因此取得可应用Result的资格。历史结果依赖留存不是v2发送兼容或新登记动作注册。切换先停止全部旧策略Sender并确认，禁止v2/v3混跑；本轮不操作本机服务。

### 10.6 Sender、期限与安全消费

撤回专用权限/退出登录只阻止新登记，不取消或暂停已登记v3命令；发送按原策略、原合同及原快照，不重查用户角色或重读最新地址。停止后续领取仍须停止全部相关Sender并防止自动拉起，停一个实例不足。当前Sender随主应用lifespan运行，ECS/WMS共享，停宿主影响出站与该实例HTTP/Result；不新增按协议暂停API或发送进程平台。停止有生效窗口，在途仍可能交付；有界关闭、租约中断、shutdown unconfirmed及合法迟到结果规则不变。

| 停止前状态 | 恢复要求 |
|---|---|
| 首次未发送、NOT_SENT、合法503或有效Retry-After的429判定NOT_RECEIVED后PENDING | 保留原next_attempt_at、退避、Retry-After、deadline、计数和证据；到原计划时间前零发送，到期且期限有效才用原身份/地址/字节重试，不因attempts>0排除，不重算等待 |
| 安全重试PENDING跨过原deadline | 按原TTL过期，零新HTTP，不延长窗口或创建替代命令 |
| 无效/超限Retry-After、UNKNOWN/HELD、可能交付或租约中断 | 保留原暂停及交付判定；重启/改址/换键/Status不自动解封或重放 |

Result、收据、命令投影及settle继续同事务，重复/矛盾/提前/迟到结果、清理后长期证据不变。通用JSON结果不能直接更新正式业务；安全消费继续按§6及§11单表、原锁序、当前许可/绑定/版本和唯一推进规则。Status仍仅规划为诊断，online/IDLE不是执行许可或未接纳证据。

### 10.7 隔离验收与复用

复用test_dev.py、控制器、test_restart.py、test_flow.py、共享模拟端与原Sender测试，只补通用合同缺口：默认自动装配，不手工动作登记；共享创建同时覆盖正式路由，不只验证Swagger；两个不同task_type及不同JSON结构均走原字节真实HTTP，并正确接收不同形状data与统一错误；权限/地址/JSON非法拒绝零义务。窗口缺失/非法拒绝与生产300秒正向分别验证，不能把默认503当自动装配正向证明。

tests.ecs_swagger按§10.3.1绑定受限工厂依赖，由主lifespan唯一一次创建生产服务/provider并启动Sender；不预装服务或provider。只允许本轮自行启动并持有的模拟origin；其他有效地址在TCP连接前拒绝，直接断言连接入口零调用及零发送依据，保持PENDING。非回环仅用计数替身，不探测现场，不以internal网络/关闭NAT或零HTTP命中替代证明；生产不新增地址清单。

实际启动验收必须分三组独立上下文：默认自动装配（无历史依赖）、可信历史映射升级启动、缺历史映射的新启动。默认组进入主lifespan前服务/provider均不存在，启动后生产装配恰好一次、仅一个Sender、同一outbox且provider使用受限工厂；两个模拟端真实HTTP和非本轮TCP零调用同时通过，再次装配仍报冲突并保留原服务。历史组用隔离合法历史夹具准备v2 ACK/UNKNOWN命令及原严格模型，不能让v3发送旧策略来造证据；启动输入合并原模型后，首次合法Result可应用、旧模型非法data仍400、命令/结果/收据/settle同事务且重复只一次。缺映射组对另一条尚无结果证据的旧命令返回503零副作用；对已有持久结果证据的重复请求返回原响应。验证保留键冲突/无效映射在发布服务前拒绝，输入及原服务不变；不得在测试中事后补字典来伪装实际启动路径。

旧v2/缺原附录/原owner只读重入与合法历史Result分别覆盖；正式两条许可、两个P2、P3及消费/取消/换绑/版本竞争保持原断言，不改成调试权限测试。保留原503/429停启矩阵、并发最终赢家及输家零发送、原回滚/WMS/STATIC/HTTP/安全日志回归。按无.env副本、一次性PG/Redis实跑教程，分别记录新通过数、skip、成功/失败清理和工作区/index/stash保护；Task 14–16历史数字不认证v3。

## 11. 最小业务单表状态机

### 选择与适用范围

已选择“单条业务动作保存意图、调用关联和消费标记”。原地迁移Reference及其行为测试，统一为单表示例；删除被替代的业务调用/消费表、冗余helper及旧表述，不提供兼容路径。路由授权测试数据和框架证据不属于这些被替代业务表。相较继续拆分调用/消费表，单表降低入门成本；相较合并框架证据表，单表不破坏多次尝试、冲突及长期追溯。框架持久化结构和协议不在本次合并范围内。

本模型限定一动作一命令、一个固定业务消费者、同库业务副作用。复用已有业务表或只在测试中建一张动作表，不新增生产模型/迁移、通用状态机库、消费框架或示例程序。多命令、多消费者和跨系统副作用须按真实需求另行设计，不能靠本例终态提供其幂等保证。

### 当前业务许可与等价迁移合同

business_key关联业务当前事实，action id标识一次独立意图；新id不能自行产生READY许可。业务方提供`current_business(session, *, business_key)`，返回can_register、当前device_code及业务version；未知/不确定拒绝。真实接入从已有权威业务记录读取，由原业务service维护取消、换绑、终态和许可版本，不新建许可/调用/消费表。version不是路由版本、客户端输入或新动作行默认值。

已登记同owner/id/business_key/原输入的重入先找回原命令，不要求当前许可再次成立。首次登记包括已预建READY的动作，均在事务中按统一锁序取得保护并重查命令关联，再重读权威事实、检查新动作许可及设备，取得该业务version后才写动作当前值/冻结原值及命令/outbox；拒绝不产生新动作或义务，已有READY保持原状。真实业务对许可判定至登记提交采用同事务行锁/等价版本保护；登记、消费、取消/换绑/版本变更统一按权威业务记录→动作行锁序，权威事实本就在该行时不重复建表或加锁层。

隔离Reference显式注入同名只读事实替身，按business_key提供已提交事实；不默认允许未知业务、不接受客户端许可/版本、不在事务里做不可回滚的内存写。测试在业务变更提交后配置对应事实；只认证来源读取与拒绝/版本映射，不认证生产聚合的事务锁。PostgreSQL动作行上的消费/业务变化竞争仍须真实执行。真实业务跨多个动作的权威变化须按business_key维护事实及受影响动作，不能把一行预置READY当作全业务重新授权。

两条原回归以教程§4的迁移矩阵为准：同B1原A1取消/换绑/终态后，原调用同参可重入、新A2依当前事实拒绝且零新增命令/义务；同B1版本从1变2且新意图获准时，A2冻结来自事实源的2，A1仍保留原命令和冻结1。拒绝场景保留cancel/rebind/SUCCESS/FAILED全部参数，不能换成无关business_key或新READY行弱化断言。另验证不同业务的独立有效意图可使用同一设备，不以历史失败永久封锁设备；UNKNOWN不因新id获得重发许可。

### 单条记录与状态转换

字段及完整转换表以[教程§4–5](../../integrations/ecs-command-tutorial.md)为同名合同：稳定id、可信owner_domain及不可换绑的business_key；当前device_code/version；原task_type/timeout_ms/params；唯一可空command_code；冻结intent_device_code/intent_version；state。owner内以`action:<id>`映射现有call_key，独立动作一条记录、终态不复用。业务ID必须在重入前稳定关联，不能请求重试即新建行。Swagger client_request_id仍直接映射call_key，既不要求同时传动作ID，也不创建该表。

- READY→WAITING_RESULT：首次登记命令、保存原命令关联和原绑定/版本，与命令/outbox同事务；失败共同回滚。
- WAITING_RESULT→SUCCEEDED/FAILED：在同一动作行锁或已证明等价条件更新下，校验可信owner、原命令、原输入关联、当前许可/绑定/版本及可应用结果后，与业务副作用同事务提交。终态即本消费者已消费标记；重复消费不再更新，失败回滚后可重试原结果。
- READY或WAITING_RESULT→CANCELLED：只撤回业务许可；已登记命令继续由框架跟踪，不代表设备取消或未执行。消费不能推进CANCELLED。
- 换绑/版本变化遵循权威业务记录→动作行锁序、推进权威业务版本并同步动作当前绑定/version，不能覆盖原绑定/版本、命令号及原输入。消费先提交则变更不能覆盖终态；变更先提交则旧结果不推进、不留成功消费标记。WAITING_RESULT不退回READY，不自动创替代命令。
- 已登记同身份同输入重入找回原命令，不受当前READY检查误拒绝；异输入冲突。不允许把业务变更后的输入伪装为原调用重入；CANCELLED且未登记不能再登记。新独立意图新建记录并核对当前许可，不可绕过原动作交付不明。

业务WAITING_RESULT与框架命令同名状态是两个维度，不必同步；业务等待期间框架可能QUEUED、WAITING_RESULT或HELD。ACK不是业务终态；FAILED只能来自合法可消费FAILED结果，不能由断网、Status、等待过久或HELD推导。业务终态不覆盖框架长期证据，迟到矛盾仍按原规则核对且不自动撤销已提交业务。

### 边界、实现位置与证明

框架ecs_command、reliable_message、ecs_send_evidence、ecs_result_evidence保留；创建幂等仍由owner/call_key及原输入判定，内部重试原记录/字节/origin不变。UNKNOWN/租约中断仍HELD，不增加普通resume、换键解封、Status等待器或第二调度器。Result事务、合法在途早于ACK与真正提前Result封锁均不改。

在现有`tests/integrations/ecs_command/conftest.py`原地迁移的`Reference`/`reference`，复用ecs及公开服务；不新增平行示例、复杂路由或Sender副本。相关行为放入现有test_service/test_query/test_flow/test_concurrency，不互相import测试文件。两个P2/P3的测试场景、参数和行为断言保留；必要时修改fixture使用和表访问，使其真正执行单表实现，不保留验证旧结构的测试。

Task 4补齐单表登记/重入/共同回滚及消费/业务变更的两种提交顺序；Task 5维持不建业务表的Swagger闭环；Task 6用隔离PostgreSQL直接验证单表消费一次、业务写失败回滚、取消/换绑/版本竞争及正常回环，再兑现教程运行步骤。既有路由P2（A撤销B有效）和P3（改址未撤销）继续复用原测试；如单表示例引入新路由接线，只补该新组合的行为缺口。

wire协议、事务、结果处理与可靠调度边界不变；新命令共享W校验及v3自动装配已实现；单表及Swagger实际验证结果集中于实施计划，历史摘要不代替本轮证据。真实设备目录/唯一约束、供应商合同、原授权、Result来源限制及现场验收门槛保持未关闭。
