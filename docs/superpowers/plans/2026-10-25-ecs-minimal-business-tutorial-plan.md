# ECS 最小业务示例与入门教程实施计划

> **For agentic workers:** 状态为GENERIC_PROTOCOL_VERIFIED / WINDOW_DECISION_APPROVED / SUPPLIER_CONFORMANCE_PENDING。Task 17设计决策已关闭，Task 18–19共10项已实现并隔离验收：v3通用合同自动装配、调试/正式新命令共享W=300秒、可信历史模型合并与实际主启动/受限出口。Task 10的3项及原C3供应商/现场项仍未关闭。实现与simplify阶段保持未提交；现按用户后续授权整理为本地提交，不推送/合并PR。无本机迁移/服务停止/真实设备连接，其他改动和stash保持。新证据及提交范围见下节。

**Goal:** Swagger直接填写ecs_url、device_code、task_type、params，以框架内置通用协议合同自动登记和闭环，不逐动作手工装配，保留专用权限及事务/幂等/未知交付边界。

**Architecture:** 主lifespan自动装配dev.ecs.network/v3与ecs.dev.protocol/1，通用JSON对象复用原DeviceAnnex校验端口、唯一ECS通道、outbox、DynamicClients和Sender。窗口收敛为可信协议级策略并逐命令冻结，正式严格附录与历史结果依赖保留，不增加注册平台、直发路径或重试器。

**Tech Stack:** 现有Python3.13+、FastAPI/Pydantic、SQLAlchemy、pytest/HTTPX，无新依赖、表或迁移。

**Spec:** [设计§10](../specs/2026-10-25-ecs-minimal-business-tutorial-design.md)为合同来源；[教程§11](../../integrations/ecs-command-tutorial.md)已在本轮隔离环境兑现。Task 10独立未完成。

## Global Constraints

- 本轮按用户授权实施Task 18–19及同步四份文档；仅修改必要ECS装配/共享创建校验、配置注释和回归。无新增表/迁移/依赖；实现及simplify阶段保留原暂存区，现按后续授权重新暂存最终改动并本地提交；不推送/合并，不操作本机数据库、服务或真实设备，其他改动和stash保留。
- local+登录+dev:ecs:send_real控制所有新登记，保留原URL/IP/路径/无代理/重定向校验；不加开关、地址/动作清单、设备表前置条件或mock/real豁免。DEVICE_CODE全系统唯一，正式地址来自设备管理，调试来自请求。
- ecs_url/device_code/task_type/params直接请求；client_request_id仍必填稳定调用键，timeout_ms仍为非负int64执行毫秒，不新增默认timeout或请求合同/窗口字段。不同动作使用不同JSON对象，框架保留内容，对端/业务校验语义。
- 原CommandEnvelope/ResultEnvelope、统一ErrorDetail、ACK/错误分类、JSON/体积约束不变；删除调试ActionBinding/actions及逐动作查模型，不保留双模式或通用回退。正式configure_ecs/DeviceAnnex能力及原结果依赖不删除。
- 已批准项目最低合同：W=300秒，从本方首次创建命令起算并冻结；D=对端ECS持久确认确定终态后至少24小时（86400秒），对端已接纳的活动/未决去重依据不按TTL清理，原Result未确认时也不因D到期清理其回报依据。最低合同适用于本项目统一ECS Command/Result目标，不是供应商符合证明；跨重启/断电必须保留原接纳/去重事实，执行不确定先核对，不盲目重做或伪造终态。详见设计§10.4；本方长期证据/可靠层保留规则不改为24小时。
- Task 18在contracts.py定义并从ECS包导出唯一PROTOCOL_RETRY_WINDOW=timedelta(seconds=300)，调试/正式路由共同引用，EcsCommands.create对所有新命令统一校验后冻结；无请求/启动/.env窗口参数或逐动作/地址配置。隔离同用生产300秒，不再替换测试默认；缺策略/非法策略仍新登记503零义务/网络，不改单次直发或禁重试绕过，不阻断WMS/Result、原owner重入或暂停/改写原v3。Task 10须另行核实真实版本、去重保留与恢复，不以本轮规范批准或模拟通过替代。
- 新快照dev.ecs.network/version="3"、ecs.dev.protocol/1，复用原字段，正式路由不改。v1/v2、旧grant/未知策略保守围栏，不改版本/改址/补授权/自动重放；原owner只读重入与原附录/发送证据上的合法Result保留，尚无持久结果证据且缺原附录时503，已有持久重复结果沿用原响应，不用通用合同解释旧严格结果。切换须先停全部旧Sender并确认，禁止混跑，本轮不执行停机。
- 原身份/字节/origin/合同/窗口/deadline冻结，URL/策略版本不进调用键或wire。最终赢家返回后、提交前复核持久origin，并发输家零发送；创建/outbox/快照及Result/证据/settle各自同事务。
- 原退避、Retry-After、next_attempt_at、计数、TTL、停启/租约/unconfirmed和UNKNOWN/HELD不变。恢复包含首次未发送、NOT_SENT及合法503/有效429的NOT_RECEIVED；到原时间前不发，超期零新HTTP，不重算/截短/延长或自动解封。撤权仅管新登记，停全部共享Sender影响ECS/WMS及主应用HTTP/Result。
- 两条许可、两个P2、P3及消费/取消/换绑/版本竞争保留，通用Result不代替正式业务安全消费。无.env副本/一次性PG/Redis，测试客户端先于Sender限制本轮自持模拟origin，非本轮地址在TCP前拒绝并断言连接入口零调用，非回环只用计数替身，不探测现场。

## Review Focus

1. 是否仍要求开发者写动作模型/清单，或通用模型裁剪、强转params/data字段；未知但协议合法的动作应进入原链路，由对端判定支持性。
2. 是否正确采用已批准W=300秒、D=终态后至少24小时及活动/未决不清理，起点不混用finish_time/重启时间；项目合同批准不得当作供应商已验收，不能从timeout推导或改单次直发绕过。
3. 是否只在dev设置W而遗漏正式EcsCommands.create；新路由窗口不一致须拒绝，旧调用/存量期限不重算。是否把v2旧快照、原严格Result按v3通用模型放行；原owner查询、缺原附录503、原证据及事务不能丢。
4. 默认自动装配正向是否实际使用统一路径，测试是否先限制出口再启Sender；零义务/命中不能代替TCP零调用证明。
5. 是否弱化权限/URL验证、并发最终赢家、原503/429停启、期限/UNKNOWN/HELD，或用调试撤权覆盖正式两条许可、P2/P3与消费竞争。

## Task 10：真实联调输入与发送门槛（待确认，单独授权）

本轮不执行，也不由Task 17–19离线通过自动完成；统一协议策略不替代现场供应商/执行许可，实际操作证据不恢复逐地址清单。

- [ ] 确认实际scheme/IP/port、全系统唯一DEVICE_CODE、动作、params/data/error、附录id/version、timeout、供应商ACK/错误/429、跨重启去重保留期及允许重试窗口、TLS/鉴权。核实实际版本符合已批准W=300秒、D=确定终态后至少24小时、活动/未决不按TTL清理及跨进程/主机/断电恢复的最低合同，取得保留配置/持久记录/原身份重复与冲突及物理行为证据；不能以Mock、加速时钟或本方测试替代真实对端符合性；正式业务另校验动作语义与业务许可，不要求Swagger逐动作登记，不使用测试窗口冒充真实保证，不新增ECS表或兼容协议层。
- [ ] 核实专用权限、现场执行许可、设备/动作/窗口、停止核对、回报网络及Result来源控制、原附录/证据留存、发布/备份/迁移回退；Swagger登录、online/IDLE或可达都不替代。未满足真实条件时不得开始现场发送；停止Sender不代表取消在途或设备急停，同服务Result停机/对端重报须明确。
- [ ] 单独取得本次真实发送批准后才安排单设备/单动作现场联调，记录原身份和合法真实Result，不手工伪造完成。ACK不等于物理/业务成功，UNKNOWN/HELD不自动重放/改投；异常先停后核对，现场与离线分别留证。

## Task 17：统一投递窗口与最低去重合同（设计决策已关闭）

**完成范围:** 2026-10-26用户明确批准并授权仅同步四份文档。本节关闭的是项目协议设计决策，没有修改运行代码、执行测试或验收供应商；Task 18–19与Task 10保持独立。

- [x] 核对现有资料：白皮书v1.1要求同身份/原载荷不重复驱动，但未给保留时长；核查当时retry_window由ActionBinding提供，300秒当时仅为测试值，10000ms HTTP超时/3600秒Retry-After上限/90天本方可靠保留均不能推出统一窗口。该资料结论保留，不把后续批准追认为旧供应商证据。
- [x] 批准enterprise_admin统一最低合同：W=300秒从首次本方创建起算；D=ECS持久确认确定终态后至少24小时（86400秒），活动/未决依据不按TTL清理；覆盖正常/异常进程退出、容器/主机重启和断电恢复，持久接纳先于ACK/物理派发，恢复不确定先核对，不重复创建执行义务。0<W≤D，真实符合性归Task 10，存储丢失/旧备份回滚/恢复出厂不能空状态直接恢复受影响接纳。
- [x] 同步四份文档，固定后续contracts.py中共享PROTOCOL_RETRY_WINDOW=timedelta(seconds=300)，不增请求/启动/.env参数，不恢复清单/开关/动作注册；隔离沿用生产值。保留v3/旧策略围栏、原快照、事务/幂等/UNKNOWN/HELD及两项P2装配/实际启动验收。数值、来源及适用范围已决，供应商C3/Task 10未决。

**关闭依据:** 本轮用户明确批准本项目规范，不是现有设备测试结果。Task 18的设计前置由此满足；本轮用户另行授权后完成Task 18–19实现与隔离验证。GENERIC_PROTOCOL_VERIFIED来自下述新证据，仍不是供应商PASS。

## Task 18：通用合同自动装配、v3与历史围栏（已实现并隔离验收）

**Dependency:** Task 17设计已关闭；本轮已取得Task 18–19实施及隔离验证授权。

**Files:** 修改app/integrations/ecs_command/contracts.py（共享常量）、service.py（首次创建统一窗口检查）、routing.py（无效窗口配置503分类）、__init__.py（导出）、app/integrations/ecs_command/dev.py、app/integrations/ecs_command/runtime.py、app/controllers/dev_ecs_command.py及必要的app/main.py/.env.example注释和导出调用方；保持EcsCommands.create及DeviceRouteProvider公开签名，复用result/routing端口，不改通用reliable/WMS。更新tests/controllers/test_dev_ecs_command.py、tests/integrations/ecs_command/{test_dev,test_service,test_flow,test_restart}.py及必要tests/helpers.py、tests/ecs_swagger.py，不新建平行示例或迁移。

**Interfaces:** PROTOCOL_RETRY_WINDOW从contracts.py定义、ECS包导出，供正式/调试路由和共享创建引用；EcsCommands.create及CommandView/Swagger响应字段不变，原期限通过持久记录验证；先查原记录，仅新调用检查route.retry_window为timedelta且精确300秒，不一致沿用AppError/HTTP 503机制和修复路标，不取整/截短/放大。configure_local_ecs(app, *, environment, history_annexes=None, client_factory=AsyncHttpClient)自动装配内置协议合同及W=300秒统一策略。history_annexes为可信原(annex_id, annex_version)→DeviceAnnex映射；client_factory为同步HttpClientConfig→客户端构造函数。普通主lifespan仍只传environment，这两项仅供可信Python启动/测试，不是Swagger/.env选项，不恢复actions/annexes旧参数别名。底层configure_ecs(app, *, routes, annexes, client_factory=AsyncHttpClient)复用DynamicClients(factory=client_factory)，原服务/provider/协议映射冲突保护不放宽。核心EcsCommands/DeviceAnnex职责保留；tests.ecs_swagger仍用create_app，仅绑定生产原函数的依赖，由真实主lifespan唯一一次调用，不能提前装配替代服务。

- [x] 先补公开HTTP失败回归：在tests/controllers/test_dev_ecs_command.py增加test_debug_accepts_unregistered_actions_with_generic_json与test_missing_protocol_policy_only_blocks_new_registration，分别验证无需动作登记即可接受两种不同task_type/JSON结构，以及策略未就绪的范围；窗口未就绪503及缺权限403零义务，且主应用/WMS/Result与原owner重入仍可用；非法对象/JSON/URL/类型400，client_request_id与timeout_ms保持原契约。先在隔离副本运行并观察旧代码因缺动作/严格qty模型失败，再做实现。新增test_new_commands_require_protocol_retry_window覆盖正式DeviceRouteProvider→EcsCommands.create：300秒时命令/outbox的expires_at和冻结窗口一致；60/600/299.999/300.001秒均配置拒绝、零义务/HTTP。旧实现仍接受非300秒路由，应先复现RED；服务错误沿现有503映射，明确修复常量来源，不报400或数据库断连，不新增错误码。
- [x] 用唯一ecs.dev.protocol/1内置合同复用原端口，params/data接受完整合法JSON对象，ErrorDetail/Result/ACK矩阵原样；默认主lifespan自动装配v3并引用contracts.py的PROTOCOL_RETRY_WINDOW，正式提供方同样引用；共享service.py在旧调用查找/输入比较之后、仅首次创建的route解析之后和命令/字节生成/写入之前做精确窗口检查，移除ActionBinding/actions/旧演示模型装配及所有调用方，不加回退或双模式。顺序为检查冲突→复制/校验history_annexes并合并内置合同→构造生产路由/service/lifecycle及受限DynamicClients→一次发布状态→关联outbox/启动唯一Sender；不得先写app.state provider或在冲突时清空状态。历史键须为非空字符串二元组、值为DeviceAnnex，内置保留键冲突必须拒绝；校验失败不发布新服务，不改输入/既有状态。错误明确区分协议非法、统一策略未就绪、运行时/存储故障，清理“未装配动作”旧路标。
- [x] 迁移原调试撤权/合同测试为权限、策略就绪、通用JSON与v3围栏；新请求有自动内置合同，不再期待“空动作503”。保存非字符串/布尔混入整数字段、非有限/深层非法JSON、完整字段保留及不同Result.data形状的回归，并验证当前窗口变化/策略缺失不改写或暂停既有v3快照，不能放宽统一协议。
- [x] 覆盖v1/v2/旧grant/未知策略不发送、不转换/改址；旧owner同输入同址只读重入、异址409；原附录可用时合法旧在途Result/重复/矛盾仍按原规则，尚无结果证据且缺原附录时503、零副作用，已持久重复结果沿用原响应。新增test_existing_calls_preserve_frozen_retry_window：合法历史60/600秒夹具同参在当前resolve窗口不一致/抛错时仍返回原身份/期限且不调用resolve，异参仍冲突；不能为通过新规则改写旧记录。新增test_startup_merges_history_annexes_for_original_results，以实际启动输入合并原模型，不在测试中事后补字典；在隔离历史ACK/UNKNOWN夹具上验证首个合法旧Result可应用、违反旧严格data仍400，缺映射的新启动对另一条未持久结果503/已持久重复结果返回原响应。新增test_history_annexes_cannot_override_builtin_contract，断言保留键冲突/无效键值在发布服务前拒绝且输入与既有服务不变；历史集合缺键不阻断启动或扫描DB自动补模型。不得让v3发送旧策略来制造历史证据，不得为保留Result而恢复旧策略发送授权或通用模型回退。
- [x] 在一次性PG验证同键同址/异址最终赢家、输家零发送及创建/Result共同回滚；复用正式两条许可、两个P2/P3及消费竞争。运行`uv run pytest tests/controllers/test_dev_ecs_command.py tests/integrations/ecs_command/ -q -rs`验证GREEN，保留原回归与参数，不在本机库运行。

**验收:** 调试及正式新命令共享固定W=300秒，不一致在写入前配置拒绝；原调用及存量期限不重算。新键不需要动作注册；通用合同不丢字段，默认路径可用性与缺策略拒绝分别有证据；协议/事务/幂等、正式路由和历史Result边界一致，无旧用法兼容层。该接受范围变化不是行为不变的simplify。

## Task 19：隔离出口、停启矩阵及教程兑现（已实现并隔离验收）

**Dependency:** Task 17策略已批准，Task 18完成；本轮按同一用户授权执行隔离验收。

**Files:** tests/ecs_swagger.py、tests/helpers.py、tests/integrations/ecs_command/{test_dev,test_service,test_restart,test_flow}.py及现有tests/reliable/、WMS/权限种子回归；同步本四份文档，不新增生产进程控制功能。

- [x] 测试专用入口移除逐动作模型/清单，先建立并持有模拟端，沿用生产W=300秒、不注入替代测试窗口，绑定受限client_factory及可选history_annexes；TEST ONLY仅指模拟目标/数据/隔离环境；进入真实主lifespan前ECS服务/provider均不存在，不提前调用装配函数。复用现有启动包装/monkeypatch委托原生产函数，不增加生产启动平台。添加test_main_lifespan_assembles_ecs_once_with_restricted_factory，证明主lifespan实际触发一次生产装配、一个Sender、同一outbox，provider通过绑定工厂创建且在Sender前受限；再次装配仍冲突并保留原服务，不重置状态避开冲突。退出先关Sender/客户端，再退出模拟端及注入上下文。
- [x] 无.env副本/一次性PG/Redis内实跑教程§11：两个本轮模拟端、至少两种task_type和不同params/data，真实HTTP原字节、ACK与模拟SUCCESS/FAILED、重复Result/同键重入/换址及owner；无权限/非法输入/缺策略/非法策略独立负向零义务。默认无历史输入与可信历史合并分别用新启动上下文验证，原模型缺失/持久重复结果另取缺历史输入的新启动；沿Task 18的首个旧Result/严格拒绝/缺依赖503/重复原响应断言，不以直接构造服务或事后改service.annexes替代。非本轮有效地址TCP前拒绝，直接断言连接入口零调用及零发送证据；非回环仅计数替身，不连接现场。
- [x] 复用test_restart.py的首次未发送/NOT_SENT/合法503/有效429在原next_attempt_at前后及跨deadline矩阵，不重算等待或期限；无效/超限Retry-After、UNKNOWN/HELD和租约中断不重放，合法在途结果按原证据。保留单Sender停止不足、有界关闭/unconfirmed及共享ECS/WMS宿主影响，增加/扩展test_formal_pending_commands_keep_frozen_deadline_after_restart，以原授权有效且安全重试的正式历史60/600秒记录验证继续原next_attempt_at/期限/字节，过原期限零HTTP；当前共享W不延长/缩短旧记录，旧HELD不解封，旧dev版本围栏仍保留。不操作本机服务。D及对端跨重启属于最低合同和Task 10验收，本方不另造一套供应商ECS持久存储/恢复实现；测试替身可模拟原记录存在/丢失与原响应，标注这不是供应商24小时保留/断电恢复证据。本方长期证据/HELD原保留断言不改。
- [x] 在一次性存储运行`uv run pytest tests/controllers/test_dev_ecs_command.py tests/integrations/ecs_command/ tests/reliable/ tests/integrations/wms_request/ tests/integrations/wms_transport/ tests/scripts/test_seed_admin.py tests/core/test_permission_code.py -q -rs`；按风险完成全仓与Ruff，Docker CLI专用用例独立执行。PG专用参数实际通过，SQLite skip不冒充PG，不改skip/放宽断言；新数量单列，不复用历史。
- [x] 成功/故意失败路径仅按本轮标签清理容器/卷/网络/镜像并保留日志；核对仅授权文件变化、index/HEAD/stash与其他文件未动，补齐命令/退出码/skip/命中/零TCP证据后再标GENERIC_PROTOCOL_VERIFIED。不提交/推送/合并，不执行Task 10。

## 本轮Task 18–19实施与验证结论

2026-10-26，Task 18–19共10项已完成。默认主lifespan装配dev.ecs.network/v3及ecs.dev.protocol/1，删除逐动作ActionBinding/actions与旧调试参数，不保留兼容别名。通用params/data保留原JSON内容，正式严格DeviceAnnex端口不变。共享PROTOCOL_RETRY_WINDOW只约束新创建，旧调用查找及输入比较在其之前；历史正式60/600秒、原next_attempt_at、期限、字节不重算。无新增表、迁移、依赖、重试器或生产地址。

**新验证证据（无.env副本、Python3.13、一次性PostgreSQL17/Redis8及本轮回环模拟端）：**

| 验证 | 命令/检查 | 退出码与结果 |
|---|---|---|
| 修改前基线 | `uv run pytest tests/controllers/test_dev_ecs_command.py tests/integrations/ecs_command/ tests/reliable/test_sender.py -q -rs` | 0；351通过、16跳过 |
| 新行为RED | 定向运行通用动作、正式统一窗口、历史保留键用例 | 1；14失败、2通过；复现未登记动作503、非300秒新命令被接受及缺历史注入参数 |
| 实际主启动 | `test_main_lifespan_assembles_ecs_once_with_restricted_factory`及`test_startup_merges_history_annexes_for_original_results` | 0；4通过、4个SQLite参数跳过；对应PG参数均通过，最终定向与全仓再次包含 |
| 独立复核P2修复 | HTTP缺失/错误类型/零负窗口及正式入口构造快照 | RED：7个HTTP、10个正式参数失败；GREEN：相关29项通过；统一配置503及修复路标，旧调用仍先返回 |
| 最终影响范围 | `uv run pytest tests/controllers/test_dev_ecs_command.py tests/integrations/ecs_command/ tests/reliable/ tests/integrations/wms_request/ tests/integrations/wms_transport/ tests/scripts/test_seed_admin.py tests/core/test_permission_code.py -q -rs` | 0；1342通过、22跳过，51.83秒 |
| 全仓 | `uv run pytest -q -rs -k 'not test_site_override_mounts_external_configuration_read_only'` | 0；2310通过、25跳过、2排除，277.33秒 |
| Docker CLI专用参数 | 在无.env独立副本单独运行`tests/scripts/test_release_bundle.py::test_site_override_mounts_external_configuration_read_only` | 0；2通过；容器未挂宿主Docker socket |
| Ruff | `uv run ruff check . --no-cache`及`uv run ruff format --check . --no-cache` | 均0；341文件格式已符合 |
| 教程§11 | 一次性库初始化/SEED，`uv run uvicorn tests.ecs_swagger:create_app --factory`，真实HTTP登录/OpenAPI/登记/查询/Result | 0；2个本轮模拟端、MOVE/LIFT不同JSON、2 ACK、SUCCESS/FAILED各1、重复Result与同键重入各2、换址409各2；非本轮地址登记后PENDING |
| 隔离出口 | `test_isolated_sender_rejects_unowned_origin_before_tcp` | 回环未持有端口及保留IP参数通过；直接断言AutoBackend.connect_tcp零调用及零发送证据；非回环未真实连接 |
| 清理与保护 | 正常退出0、故意退出42清理演练；按本轮标签清理实际资源，核对文件/index/HEAD/.env/stash指纹 | 本轮容器、匿名卷、网络、镜像均清理，剩余0；原暂存区/HEAD/.env/stash及其他文件未变 |

跳过仅为SQLite不能证明的PostgreSQL锁/并发/独立连接等原参数；已配置一次性PostgreSQL的对应参数实际通过，不改skip或放宽断言。两条许可、两个P2/P3、消费/取消/换绑/版本竞争、原ACK/Result/未知交付及WMS/STATIC回归均保留。历史正式恢复复用原矩阵断言；60秒窗口的合法429使用30秒等待，原300/600秒矩阵保留120秒Retry-After，不用超出原期限的等待冒充“窗口内恢复”。

可信history_annexes在启动输入中合并，不事后补模型；隔离合法历史v2 ACK/UNKNOWN快照不经旧策略发送生成。实际PG验证首次旧Result、原严格data拒绝、矛盾409、缺原模型503零副作用与已持久重复原ACK。初次补历史HELD夹具时触发原next_attempt_at约束，已按原状态约束修正夹具；未修改生产约束。独立只读终审发现的配置分类P2已复现并修复；没有未处理的Critical/Important或Minor。复核未自行判断的最终测试/文档/清理由本轮证据补齐，供应商现场项继续留Task 10。

临时教程验证脚本的初次就绪探测受宿主代理/启动瞬间断连影响，已仅在临时脚本显式禁用代理并等待就绪后实跑；失败路径同样清理API容器，未改变产品HTTP客户端行为。测试专用工厂仍只接受本轮自持origin，不新增生产地址清单。

日志保留在`/var/folders/bk/mwmtsjpn10dbcmzzp5zn21wr0000gn/T/ecs-generic-z_c_t9p7`：baseline.log、red.log、formal-red.log、review-red.log、review-green.log、final-target.log、full.log、docker-cli.log、ruff-check.log、ruff-format.log、tutorial.log、tutorial-api.log及cleanup.json/cleanup-probe.json。测试仅在副本运行，一次性数据库初始化/迁移/SEED不涉及本机数据库。PLAN历史正文保留原文，新通过数不追认旧实现；未提交、推送或合并PR，未停止本机Sender。

**Task 18–19 simplify（2026-10-26）：** 收拢内置不可变DeviceAnnex定义，生产装配与测试夹具复用同一合同；冻结授权直接使用内置合同构造预期快照并复用已转换commands，保留完整比较及异常围栏。移除单一调用方的客户端包装，启动回归复用ACK轮询并直接使用ecs夹具；正式历史恢复夹具直接使用正式路由及原严格模型，不先创建调试快照再转换。4个代码/测试文件净减少22行，未改变共享W校验、装配冲突/历史合并顺序或原用例断言。

本轮在无.env副本、一次性PostgreSQL17/Redis8及受限回环出口复验同一影响范围：修改前1342通过/22跳过（50.95秒），修改后1342通过/22跳过（51.54秒），退出码均0；1364个用例ID及顺序完全一致，对应PG参数实际通过。临时只读对比另验证210组有效/异常冻结JSON的授权结果一致，不新增永久重构测试。Ruff检查/格式检查均0，341文件格式符合；本轮未重跑全仓，前文全仓数字属于Task 18–19实施验收。旧调用重入、60/600秒原期限、503/429停启、UNKNOWN/HELD、真实主启动、历史Result及非本轮地址TCP零调用回归均包含在本次复验中。

仅按本轮标签清理容器、匿名卷、网络和镜像，剩余0；原暂存区、HEAD、本机.env、其他文件及stash保持，设计稿/教程原改动和PLAN历史正文保留。不提交/推送/合并，不执行Task 10、不迁移本机数据库或连接真实设备。日志保留在`/var/folders/bk/mwmtsjpn10dbcmzzp5zn21wr0000gn/T/ecs-simplify1819-9k47dlwj`：before.log、after.log、cases-before.log、cases-after.log、case-preservation.json、frozen-equivalence.log、ruff-check.log、ruff-format.log、simplify.diff及cleanup.json。

**本地提交整理（2026-10-26）：** 按用户后续授权，将Task 18–19及simplify最终版本重新暂存并整理为本地提交；现有暂存区中的ECS单表参考实现、Swagger入口、专用权限种子和入门教程是该最终版本的同一功能前置，一并纳入，未包含其他业务、迁移、依赖或本机配置。Task 18–19保持10/10，Task 10保持0/3；原验证记录不覆盖、不追认供应商符合性。提交前核对全部源码/测试/配置与simplify隔离通过副本内容一致，复用1342通过/22跳过及210组冻结JSON等价证据，重新执行Ruff及差异检查；没有把实现阶段的全仓数字改写为simplify后新跑的结果。不推送、不合并PR、不执行Task 10、不迁移本机数据库或连接真实设备，其他改动和stash保留。

**剩余待办：**Task 10保持0/3；真实设备目录/DEVICE_CODE唯一约束、供应商实际版本/持久去重D与跨重启断电恢复、来源与现场网络/执行许可仍未验收。GENERIC_PROTOCOL_VERIFIED只认证本方通用实现和隔离闭环，不自动授权真实发送。D与对端持久化仍是合同要求，不另建供应商ECS实现；UNKNOWN/HELD不自动重放、改址、换号或解封。

## 历史：按动作合同策略Task 14–16及simplify（已验收，不作为新方案配置指南）

以下原完成项、失败修正及验证数字保留，证明当时的v2实现。空动作503、ActionBinding/actions及按动作装配只描述历史，不得复制为v3入门要求；当前源码尚未切换，历史通过数不认证新方案。

## Task 14：删除联网开关、收敛新登记（已完成）

**Files:** app/core/config.py、app/integrations/ecs_command/dev.py、app/main.py及必要装配调用方、.env.example；复用tests/helpers.py、tests/ecs_swagger.py、tests/controllers/test_dev_ecs_command.py、tests/integrations/ecs_command/test_dev.py；权限码及既有SEED/权限一致性测试保留。

- [x] 先迁移公开HTTP用例：有可信合同但缺权限403，具备权限但无/未知/歧义/演示合同503，参数/timeout/地址非法400，拒绝零命令/outbox/命中；回环、外部模拟和真实目标策略一致，非local不挂载，不由mode/is_mock/附录字段绕过。替换旧开关条件用例，不留旧参数拒绝烟测。
- [x] 原地移除开关配置、real_send_enabled参数/属性/读取/判断及所有调用方，可信actions/annexes仍一次装配且默认空；普通app.main与测试专用启动均接通新签名。不增加别名、同义开关、地址表/清单或第二Sender/重试器；不改权限码、核心create/DTO或ECS/WMS wire。
- [x] 清理.env.example、README/相关说明及测试中的旧启停开关用法；内置权限与SEED保持一致，普通角色按需授权，不把admin名称当豁免。不写或执行本机配置/数据更新；验证新设备/合法URL不改启动配置，缺依赖仍明确拒绝。

## Task 15：存量策略及Sender停止/恢复（已完成）

**Files:** 调试适配及既有test_restart/test_dev/test_snapshot/test_service/test_query/test_concurrency、tests/reliable/test_sender.py等关闭/恢复回归；仅补缺口，不改通用reliable、ECS创建/Result事务、正式路由或锁序，不新增运行控制API。

- [x] 新调试命令冻结dev.ecs.network/v2；发送只验证原可识别策略与原附录，不依赖当前用户权限/新动作绑定。复用PG同键同址/异址最终赢家及输家零发送、回滚、只读owner重入；新增/更新/移除绑定不改原地址/合同/字节/窗口/期限。撤权或移除动作绑定阻止新登记，原v2投递仍按原合同允许；不把URL/策略版本拼入身份键。
- [x] v1/旧grant/未知策略在原发送门禁保守围栏，原记录不转换或补授权；旧owner可只读重入，合法原Result仍按原附录/发送证据处理。缺原附录拒绝/暂停，提前、重复、矛盾、迟到及结果/证据/settle共同事务原断言保留。两条许可、两个P2/P3及消费竞争不改成调试撤权测试。
- [x] 复用并补设计§10.6停止/恢复矩阵：停止所有受控Sender后不再领取，单实例停止不证明其他实例停发。恢复覆盖首次未发送、NOT_SENT及合同合法503/有效Retry-After且未触发合同暂停的429判定NOT_RECEIVED后的PENDING；在原next_attempt_at前恢复仍零发送，到期且deadline有效才按原command_code/调用键/origin/字节重试，保留原退避、Retry-After、证据和not_received_count，不重算调度/重置计数或因attempts大于0排除。停机跨过deadline则零HTTP且按原TTL过期；429的Retry-After无效/超出既有等待范围、可能交付/租约中断和UNKNOWN/HELD保持原规则，不借恢复重放/解封，合法在途Result按原证据可应用。复用test_flow.py的Retry-After/NOT_RECEIVED及Sender关闭/快照/过期用例，只补缺口；不新增生产拆分/暂停功能、不操作本机服务，保留shutdown unconfirmed和HTTP/WMS共享停机边界。

## Task 16：隔离出口、教程兑现及验证（已完成）

**Files:** 复用tests/ecs_swagger.py、tests/helpers.py、已有ECS/controller/runtime测试和四份文档；不建立平行示例/长期实验平台。

- [x] 在无.env副本、独立一次性PG/Redis及两个本轮回环模拟端验证：合同+权限齐备后真实HTTP原字节、一次义务、不同DEVICE/URL无需清单或重启；缺合同/权限分别503/403零新增。主应用先实际注入受限工厂再启Sender；非本轮有效origin在TCP连接前拒绝且连接入口零调用，保持PENDING/零发送依据。非回环用计数替身，不探测现场；不以全部禁网、默认空合同拒绝或假ACK替代正向。
- [x] 复用两条许可、两个P2/P3、重启/关闭/租约/UNKNOWN/HELD/Result事务、ECS/WMS/STATIC、HTTP/日志及权限种子回归，按范围跑专项/全仓/Ruff；对应PG参数实际通过，SQLite skip不冒充PG，Docker依赖用例独立执行，不放宽断言/更改skip。新结果与历史数量分开。
- [x] 从空环境照做教程§11：无开关普通启动、真实登录、空合同503、测试合同/受限工厂正向启动、OpenAPI/持久URL/ACK/模拟Result/重复/换址/owner，以及成功/失败清理。补齐实际命令/退出码/skip/命中与零TCP证据后才标新策略已验证；核对工作区/index/stash，只清理本轮资源，不提交/推送/合并或进入Task 10。

## 本轮实施与验证（Task 14–16）

已原地删除联网开关配置、装配参数、属性和调用分支；默认空动作/附录，可信合同与专用权限控制新登记。新调试快照为dev.ecs.network/v2；原v1/未知策略保守围栏，不转换记录，不保留发送兼容层。原owner只读重入、原附录/发送证据上的合法Result保留。核心创建/Result、通用reliable、正式路由及ECS/WMS/STATIC协议未改，无新依赖、表、迁移或运行控制API。

复用FakeClock、ScriptedHttpClient、MessageStore、DynamicClients及原Sender，新增test_restart.py：首次未发送、NOT_SENT、合法503与Retry-After=120的429，分别覆盖期限内关闭重建、计划时间前零发送/到期重试和跨期限零HTTP；原身份/路由/字节/期限/已有证据及not_received_count保持。无效/缺失/超出等待范围的429、UNKNOWN保持HELD；单实例停止不影响另一个实例的领取。原租约中断、有界关闭/unconfirmed及Result事务用例继续运行。停启是测试中Sender对象的关闭/重建，不等于执行部署进程重启或证明所有线上实例已停止。

### 本轮隔离验证证据

- 无.env副本，镜像python:3.13-slim+uv0.12.13、锁定依赖；runner只读根与源码、tmpfs，无宿主socket，仅本轮内部网络；PG17/Redis8均一次性。本轮标识zz0yx0q，日志位于`/var/folders/bk/mwmtsjpn10dbcmzzp5zn21wr0000gn/T/ecs-actions-only-zz0yx_0q`。
- 任务前基线：`uv run pytest tests/controllers/test_dev_ecs_command.py tests/integrations/ecs_command/ -q -rs`，295 passed、16 skipped。RED证据：省略开关后公开HTTP登记被旧开关403拦截；旧v1在发送门禁仍获准，回归分别失败后才删除开关及升级v2。
- 最终全仓：`uv run pytest -q -rs -k 'not test_site_override_mounts_external_configuration_read_only'`，2238 passed, 23 skipped, 2 deselected in 260.50s，退出码0。使用独立final数据库与Redis14，不与专项/教程共享存储。SQLite不适用的PG行锁/真实独立连接参数skip，对应PG参数实际通过；未改skip或放宽断言。
- 最终专项：`uv run pytest tests/controllers/test_dev_ecs_command.py tests/integrations/ecs_command/ tests/reliable/test_sender.py -q -rs`，351 passed、16 skipped，28.94s、退出码0；使用新的targeted_final数据库与Redis13。停止后先推进原等待的一半再重建Sender，确认不是以重启时刻重新等待；该增强断言及最终格式在此专项复核。
- Docker CLI两参数用例：清空继承环境、独立HOME、无.env副本、锁定依赖，单独运行`tests/scripts/test_release_bundle.py::test_site_override_mounts_external_configuration_read_only`，2 passed、退出码0；runner不挂宿主socket。Ruff全仓check与format --check均通过、退出码0。
- 教程空环境实跑：仅在本轮ecs_tutorial_final数据库执行Alembic/SEED；启动普通app.main:app，OpenAPI中ecs_url必填，真实登录200、空合同503，command/message/send-evidence计数0/0/0。移除本轮API，再以tests.ecs_swagger:create_app --factory启动；无需额外开关，两个本轮随机回环模拟端各通过真实HTTP取得ACK，随后模拟SUCCESS/FAILED及重复Result均200，同键等价地址重入200、换址409，持久计数2/2/2。非本轮回环origin登记后保持PENDING、无ACK/发送证据，计数3/3/2。底层TCP入口零调用另由完整回归中的隔离出口用例直接断言，不以计数或PENDING代替。
- 成功与故意失败路径均核对本轮标签后清理API及web网络；最终按本轮标签清理runner/PG/Redis共3个容器、2个匿名卷、1个内部网络和本轮镜像，余留本轮资源0；API及web网络已在成功/失败路径清理。两条许可、两个P2、P3、并发最终赢家/输家零发送、消费竞争及HTTP/诊断/WMS回归保留。
- 新上下文独立只读终审：Critical/Important/Minor均0。Task 10现场合同、来源控制/许可、全部部署实例停启等属于独立门槛，未执行、未宣称现场通过。

### 验证过程修正与保护范围

首轮新回归发现两处旧403预期及八处过期转换计数预期不符：移除动作后有权限的新登记应503；原生命周期过期转换也计入attempts，零HTTP必须以客户端请求/发送证据判断。仅修正断言，未改可靠语义。重复启动用例曾因50ms/100ms轮询积累触发HTTP限流；保留生产限流，将测试轮询调至500ms并补第二查询状态断言。Ruff首次因只读缓存失败，改用--no-cache；删除配置后的空行按格式器修正。

教程一次性核查脚本最初未指定enterprise_backend schema，后又将FAILED的error_detail误写为error；均仅修正临时核查脚本，保留失败日志并使用新的本轮空数据库重跑，没有修改协议或放宽生产校验。故意失败的清理探针及最终成功路径均通过，教程最终退出码0。

本轮不提交/暂存/推送/合并、不执行Task 10、不改本机配置、不迁移本机数据库、不连接真实设备、不停本机服务。工作区相对基线只增加本任务生产/测试/文档改动；基线哈希核对确认既有暂存区、HEAD与stash保持，变更仅限本任务12个文件，其他文件不改。Task 10三项仍未完成。

## 历史：带开关策略Task 11–13及simplify（已验收，不作为当前配置指南）

下方保留原完成项和失败/通过/清理证据，说明当时带开关策略的实际实现；其中开关true/false、关闭后阻止发送及v1语义已被上方待实施设计取代，不是新策略的使用步骤。不能复制历史配置、取消旧策略围栏、复用通过数或恢复兼容层。已有单表、P2/P3及事务证据继续作为回归基线。

## Task 11：统一联网门禁与按动作装配（已完成）

**Files:** app/core/config.py、app/integrations/ecs_command/dev.py、app/controllers/dev_ecs_command.py、app/controllers/dev.py及必要的主应用装配调用方、.env.example；复用tests/helpers.py、现有test_dev.py/test_dev_ecs_command.py和权限种子/测试。runtime.py的统一通道与公共configure_ecs保留，不把ECS逻辑放入app/reliable。

**Interfaces:** DevCommandIn/Out保持ecs_url与原业务字段，不增加kind/mode/annex选择。configure_local_ecs增加受信任actions并复用annexes：task_type到唯一附录标识/版本/正数timedelta发送窗口绑定。动作表由接入模块在启动注入，默认空，无新模型环境变量/自动发现。现有RBAC权限和开关名称保留，语义统一为联网许可。

- [x] 先写公开HTTP失败用例：有/无权限、开关false/true、回环/外部地址策略、kind/mode伪造；所有真实HTTP路径含本应用Mock均不得豁免，默认拒绝零命中/义务。非法URL仍400，合法地址无需清单；非local仍不挂载。
- [x] 实现唯一动作绑定和非演示附录/窗口校验，未知/歧义/缺失合同503、参数/timeout400。一次装配服务多个DEVICE/URL，接通实际主应用启动输入，补可照做片段；离线用显式测试合同，不能默认开放演示附录或把测试确认当供应商确认。
- [x] 原地删除ECS_DEV_TARGET_GRANTS/DevTargetGrant、逐设备地址/kind分流及旧字段/示例/调用方；复用现有开关、权限、统一通道和客户端池。旧分流测试按新行为迁移，不保留旧参数拒绝等过渡测试或兼容路径；不迁移本机数据库。

## Task 12：冻结、幂等、暂停与存量边界（已完成）

**Files:** 调试适配及上述已有ECS/HTTP测试，必要时扩展共享fixture；不改核心create签名、CommandView、ECS/WMS wire或Result事务，不新增表/锁序。

- [x] 保留原owner只读重入，不因新登记权限/开关撤回而误拒；异业务输入/异origin409。PG同键同址/异址最终赢家核对、输家零发送、登记/提交失败共同回滚全部复用，URL/动作版本不进入owner/call_key/params/wire。
- [x] 新登记按当前动作绑定，后续绑定/URL变化不改原合同/字节/窗口；移除新动作入口不自动取消旧命令，原附录仍可用于原命令与合法Result。发送门禁根据全局开关、可识别原策略/原合同判定，缺失/不确定暂停；旧grant快照不补授权/改址/转换或兼容重放。
- [x] 关闭联网开关阻止后续发送，保留合法在途Result、重复/矛盾/提前Result及共同事务；重开不恢复UNKNOWN/HELD。保留正式路由A撤销B有效与P3未撤销改址、消费/业务取消/换绑/版本两种提交顺序及两条许可回归；逐grant测试迁为全局门禁测试时不得削弱这些独立覆盖。

## Task 13：隔离验证、教程兑现与清理（已完成）

**Files:** 复用已有ECS/controller/runtime/共享测试及四份文档；tests/ecs_swagger.py只负责本轮模拟端与主应用测试启动，回环服务从现有fixture移到tests/helpers.py复用。按范围运行WMS/日志等回归，不新建业务示例或长期实验平台。

- [x] 新无.env副本、一次性PG/Redis与两个回环桩实跑：复用DynamicClients客户端工厂注入，允许集合仅取本轮自行启动并持有的模拟端实际监听origin（规范化scheme/IP/端口），所有ECS HTTP尝试在TCP连接前核对。关闭开关/缺权限拒绝，演示或未知合同拒绝；可信测试动作/附录+开关+权限后，新增DEVICE/在两个模拟端间改URL无需生产清单或重启，各桩经真实HTTP收到原字节/一次义务，PG并发输家零命中。另测开关开启、权限/合同齐备、非本轮有效origin在连接前拒绝，底层TCP连接入口调用次数为零，不以零HTTP命中替代；复用连接计数替身，非回环值不实际探测。测试集合不进入生产配置，不改冻结地址、ACK/Result、事务、重试或UNKNOWN/HELD。
- [x] 复用两条许可、两个P2/P3、ECS/WMS/STATIC、HTTP/日志、重启/UNKNOWN/HELD/Result事务，全仓及Ruff按范围执行；对应PG参数实际通过，SQLite skip不冒充PG证明，Docker依赖用例独立真实运行，不修改skip或放宽断言。新结果与历史数量分开。
- [x] 从空环境实际照做教程§11：默认关闭启动及403，提供能通过主应用装配入口同时注入测试动作/附录及受限客户端工厂的正向命令，证明该启动实际对两个模拟端发送真实HTTP、对非本轮地址在TCP连接前拒绝，再核对OpenAPI/owner查询/ACK与模拟Result、重复/换址/清理。补齐实际命令/退出码/skip/失败及成功清理后才标新策略已验证；核对工作区/index/stash，不自动提交/推送/合并或进入Task 10。

## 本轮实施与验证（Task 11–13）

已移除逐地址清单、分类及演示合同默认装配；统一开关与专用权限覆盖所有HTTP新登记。ActionBinding按动作绑定附录及正数timedelta窗口；原命令发送只核对原策略/原附录，不随当前动作绑定改写。未知旧快照保持围栏，不提供转换/兼容层。

Ruling：默认本地装配从路由挂载移到主lifespan，仅在尚未显式装配时创建空动作服务；可信启动模块可在lifespan之前调用一次configure_local_ecs，不重置app.state、不新增生产app factory。代价是仅导入app但不启动lifespan时调试服务仍未就绪，接口沿用503。

Ruling：测试客户端工厂在构造真实HTTP客户端前限制本轮模拟origin，拒绝沿用PoolUnavailable的既有未发送路径，命令可已登记但不建立TCP连接、不产生发送依据；不伪造ACK或将拒绝误判UNKNOWN。测试允许集合只在内存，不进入生产配置/设备授权。代价是误填地址的隔离调试命令保持PENDING至原期限，需从测试证据辨别，而非把登记200当发送成功。

本轮验证副本不含.env，PG/Redis及runner在专属internal网络，数据库无发布端口且runner无宿主socket；Swagger只发布宿主回环随机端口，开启联网时另注入本轮模拟端限制。日志与执行记录保留在临时验证目录，不纳入仓库。

验收过程的失败与修正不隐藏：
- 基线专项296 passed、16 skipped。旧mock在关闭开关时登记200而预期403，完成门禁回归RED→GREEN；删除测试工厂限制的反证实验触发TCP入口计数断言，恢复后4个SQLite/PG负向参数通过，未真正连接非本轮地址。
- 首轮全仓2208 passed、23 skipped、3 failed、2 errors：并行专项与迁移用例共享临时库导致ECS证据阻止降级（1失败、2fixture错误）；runner没有Docker CLI导致两项Compose用例失败。改为全新一次性库顺序执行全仓；Compose两项在无.env副本用宿主CLI独立运行，不向runner挂socket、不修改skip或断言。
- 第二轮全仓2209 passed、23 skipped、2 deselected、2 failed：新临时PG库复用了上轮Redis，`test_wrong_password_does_not_wait_for_user_lock`及`test_login_rechecks_user_after_unlocked_verification[deactivate]`读到旧测试会话。ECS启动回归已补齐对自身用户会话的撤销清理；最终全仓改用全新PG库和Redis容器顺序执行，不改登录业务代码或断言。
- 首次临时Swagger核查脚本用原生SQLAlchemy连接，未设置项目search_path而找不到表；改用app.database工厂后，默认403及零命令/outbox核对通过，应用代码未因此修改。

最终隔离验收（所有命令退出码0，故意失败清理除外）：

| 验证 | 本轮结果 |
|---|---|
| 最终全仓 | 全新ecs_gate_clean数据库与redis-final容器，顺序执行`uv run pytest -q -rs -k 'not test_site_override_mounts_external_configuration_read_only'`：2211 passed、23 skipped、2 deselected，257.06s |
| Compose依赖 | 无.env副本在宿主运行`tests/scripts/test_release_bundle.py::test_site_override_mounts_external_configuration_read_only`：2 passed；使用真实Compose config，不向runner挂socket |
| 连接前拒绝 | 四个SQLite/PG×非本轮回环/保留IP参数通过；TCP入口零调用，PENDING且零发送依据，未探测现场 |
| Swagger主启动 | 从空的ecs_tutorial库及无.env副本，按教程默认关闭/测试专用启动块实跑：OpenAPI必填ecs_url、真实登录、默认403零命令/outbox；两个模拟origin分别接收D1/D2，ACK后WAITING_RESULT、重复登记原编号、换址409、重复模拟Result一致，非本轮地址PENDING零发送依据。不是浏览器交互或现场验收 |
| 保留语义 | 两条当前许可、两个P2/P3、原授权锁序/消费竞争、UNKNOWN/HELD、Result共同事务、WMS/STATIC和全局日志由最终全仓覆盖；核心service/result/lifecycle、reliable及正式回归文件与任务开始前一致 |
| Ruff | `ruff check --no-cache .`及`ruff format --check --no-cache .`通过，340 files already formatted |
| 独立工程复核 | 只读增量复核未发现可落实问题；随后只删重复动作校验并补测试会话清理，最终全仓通过 |
| 教程清理 | 正向服务清理退出0；故意exit7后EXIT trap仍清理本轮runner/网络且保留退出7，不删除其他标签资源 |

收尾核对：本轮主runner、PG及两个Redis容器共4个，匿名卷3个、internal网络和自建镜像已按本轮标签移除，标签资源剩余0；Swagger API/入口bridge及故意失败的清理资源也已移除。HEAD、暂存区与stash指纹保持原样；相对任务开始仅15个预期文件变化，其他原有内容未变。最终Python源码与通过全仓的副本逐一比对一致；四份文档链接/片段语法及diff空白检查通过。未提交、推送、合并、执行Task 10或迁移本机数据库，未启用真实设备发送。

本轮日志和临时副本保留在`/var/folders/bk/mwmtsjpn10dbcmzzp5zn21wr0000gn/T/ecs-network-gate-a3xblalb`，最终全仓为`full-clean.log`，Compose为`docker-compose-tests.log`，Swagger为`swagger-positive-probe.log`。实际资源与保护核对见收尾记录。Task 10仍三项未勾选、未执行，不由离线验收自动满足现场门槛。

## Task 11–13 simplify（行为保持，已验证）

本轮仅将DevEcsRoutes单一调用的动作选择校验并入resolve；共享测试Sender构造/关闭上下文，保持关闭发生在模拟端退出前；测试动作工厂改用显式关键字参数，去掉嵌套默认值合并。三份Python文件净减少35行，没有增加生产配置、协议或兼容入口。

冻结快照校验、URL校验、仅允许本轮模拟端的工厂、Swagger测试启动及会话清理保持原样；测试用例、参数和断言均未删改。正式许可、两个P2/P3、原地址/合同/字节、事务、幂等、重试及UNKNOWN/HELD边界继续由现有回归覆盖。

| 本轮验证 | 实际结果 |
|---|---|
| 精简前/后ECS及调试HTTP专项 | 均为295 passed、16 skipped；各用独立临时PG库，PG参数实际执行，SQLite不适用参数跳过 |
| 最终全仓 | 独立final临时PG库与初始为空的Redis测试库，顺序执行`uv run pytest -q -rs -k 'not test_site_override_mounts_external_configuration_read_only'`：2211 passed、23 skipped、2 deselected，255.36s |
| Compose依赖 | 无.env副本、独立HOME下使用宿主真实Compose config：2 passed；不向runner挂载宿主socket |
| Ruff | 检查及格式检查通过，340 files already formatted |
| 静态复核 | 原断言及参数不变；resolve保留原校验与快照构造；冻结授权、模拟端限制工厂及独立Swagger启动未改变 |

本轮验证目录为`/var/folders/bk/mwmtsjpn10dbcmzzp5zn21wr0000gn/T/ecs-gate-simplify-3h0uk_2u`，日志分别为`baseline-tests.log`、`simplified-tests.log`、`full-tests.log`、`compose-tests.log`。副本无.env，PG/Redis及runner仅在专属internal网络，存储不发布端口、runner无宿主socket；不执行Task 10、本机迁移或真实设备连接，不提交、推送、合并。原有文档内容保留，仅在此补记simplify证据。

本轮3个容器、2个匿名卷、internal网络及自建镜像已按唯一标签清理，标签资源剩余0；HEAD、暂存区及stash指纹保持原样，相对任务开始仅上述3份Python文件及此PLAN补记变化，其余文档和其他原有改动未变。最终源码与通过测试的副本一致。

## 以下为历史基线，不是当前配置指南

历史完成项、失败修正及验证数字保留，支持追溯原有改动；其中逐grant/kind和模拟豁免只描述旧实现，已被上方新设计取代。不得从历史段复制旧配置、宣称新门禁通过或恢复兼容层。

## 历史完成状态

Task 0–3教程及simplify共25项已完成；Task 4–6单表原地迁移、启动地址版Swagger及隔离验收共15项已完成。两个阶段均保留完成状态，后续联调入口另列Task 7–9，真实门槛另列Task 10，不能覆盖历史结果或复用勾选表示新功能完成。

## 历史：Task 4–6启动配置地址版的执行证据（2026-10-26）

Task 4–6已完成15项，包括单表迁移、启动配置地址版Swagger和隔离验收；完成状态保留。本段只记录当时实现，不能认证后文请求URL迁移，也不作为当前操作指南。生产ECS/WMS协议、Result处理及可靠调度核心文件未修改；修改仅涉及单表测试参考、本地配置/装配、薄HTTP入口和配套文档。

| 当时实际检查 | 结果及边界 |
|---|---|
| 隔离基线ECS专项 | 198 passed/12 skipped；先确认原基线，再迁移；不作为新能力证据 |
| 新许可用例RED | 新action_id接口尚不存在时4个参数失败，随后单表迁移GREEN |
| 单表ECS专项 | 202 passed/12 skipped，两个P2/P3及两条许可回归已在新承载上执行 |
| 本地装配/HTTP RED→GREEN | 缺模块/路径时失败，最初21项通过；Swagger示例先因缺示例失败，补齐后通过 |
| ECS+HTTP阶段验证 | 231 passed/13 skipped（后续FAILED参数、提交失败、提前Result及Swagger示例由最终全仓覆盖，不累计子集数量） |
| 最终全仓（internal容器） | `uv run python -m pytest -q -rs -k 'not test_site_override_mounts_external_configuration_read_only'`：2151 passed/21 skipped/2 deselected，退出码0 |
| 两项Docker专用回归 | 临时源码副本/独立host venv仅执行`tests/scripts/test_release_bundle.py::test_site_override_mounts_external_configuration_read_only`：2 passed，退出码0；使用唯一Compose项目及临时挂载，不启动业务栈、不访问数据库 |
| 实际应用独立启动 | 从新建容器专属库迁移/seed、真实环境配置导入app.main、/docs/OpenAPI、登录、创建/重入/异参409、唯一Sender命中本应用Mock、ACK与Result分离、人工Result/重复/owner查询通过；数据库确认命令/发送证据/结果证据各一次，未建参考业务或设备表 |
| Swagger教程逐块复现 | 原样提取教程Bash块1/3/5，在新的无.env副本运行；仅API专属入口bridge/宿主127随机端口，PG/Redis无发布端口；/docs、OpenAPI、seed/login、Mock ACK、人工/重复Result、owner查询通过，退出码0；本次API/runner/存储/两网络/镜像均按标签清理 |
| 独立最终工程复核 | 新上下文审查实际工作文件及未跟踪新文件，无需要修复的具体问题；未代替实际测试或生产验证 |

21个skip是SQLite不适用的PostgreSQL锁/并发参数及真实应用Sender的两项独立连接用例；对应PostgreSQL参数均实际执行。两项Docker回归单独补验，不能把deselected写成容器内通过；阶段专项是全仓子集，不累加通过数。

首轮完整套件有2项失败（runner无Docker CLI/daemon），其余2150项通过；验证环境修正为上述分环境运行，没有改测试跳过或伪造Docker命令。实际应用一次性核验脚本首轮查询遗漏enterprise_backend schema，在修正核验脚本后完整重跑通过，没有修改应用表结构。Swagger教程首轮因Docker internal网络不发布宿主端口退出，原失败资源已清理；修订网络步骤后从新副本完整复现通过。

Ruling：在用户授权的当前feature分支原地修改，验证使用临时源码副本和一次性容器，不新建Git工作树、不暂存或提交。Reference只要求显式action_id，其余演示参数具有固定测试默认值，旧key参数/位置consume接口及旧业务调用/消费表均已删除。

Ruling：不挂宿主Docker socket到验证runner；两项需要实际Compose运行的回归在临时副本独立执行，项目名和资源由原测试唯一命名/清理。仅Swagger API增加专属入口bridge（关闭IP masquerade）及宿主回环随机端口，数据库/Redis仍仅在internal网络，命令地址始终是API容器自己的回环Mock。

Ruling：FastAPI会移除OpenAPI示例对象中的null值，保留required nullable schema并以实际HTTP验证result=null，不为示例改变全局OpenAPI生成规则。

测试事实替身只证明当前事实被读取和映射；生产业务聚合锁、正式设备目录及唯一约束、供应商接纳/去重、Result来源限制和真实物理/业务验收仍未关闭。本轮没有提交、推送、PR操作、本机迁移或真实设备连接。

最终静态检查：隔离副本`uv run ruff check --no-cache .`与`uv run ruff format --check --no-cache .`通过（338 files already formatted）；工作区及暂存区diff --check通过。应用/测试Python文件逐一与最终全仓副本比对一致。主验证容器、匿名卷、internal网络及自建测试镜像已按本轮标签移除；教程失败及成功路径的资源亦已清理，临时源码/日志保留供核验。HEAD、暂存区及stash指纹与实施前一致，除本轮文件外的原有文件内容未变；未提交、推送、操作PR或启用真实发送。


## 历史：Task 7：联调合同、授权装配与旧方式清理（已完成）

**Files:** app/controllers/dev_ecs_command.py、app/integrations/ecs_command/dev.py、app/controllers/dev.py、app/core/config.py、.env.example；复用tests/controllers/test_dev_ecs_command.py和tests/integrations/ecs_command/test_dev.py。真实权限同步scripts/seed_admin.py的INITIAL_PERMISSIONS及tests/core/test_permission_code.py，不新增权限表。

**Interfaces:** DevCommandIn必填ecs_url，设备/动作严格非空、params按服务端附录验证；创建/查询调试DTO返回持久原origin，框架CommandView不改。DevEcsRoutes提供联调适配；configure_local_ecs接收受信任授权清单/真实开关及显式annexes装配，不接收默认地址；主lifespan合并自动通道，核心create不变。

- [x] 公开HTTP回归先复现必填/语法400、等价origin、登录/owner隔离、授权匹配/歧义拒绝、mock/real不可由请求伪造、真实开关/权限403、缺附录503及OpenAPI合同；使用显式测试附录，不将演示合同用于real。语法/拒绝零连接、零新增义务。
- [x] 最小实现精确授权、请求不可变上下文及共享原快照授权，复用outbox/连接池/唯一Sender；调试DevRouteSnapshot复用核心RouteSnapshot及既有JSON，冻结grant_id/version、kind、origin、设备/动作/附录/窗口；kind变化也须围栏原发送。两个请求交错、A后B、A撤销B有效与改址未撤销均验证；非local拒绝误配，不覆盖WMS/ECS映射，不加生产回退。
- [x] 删除旧启动地址字段/参数及旧provider/读取/说明，迁移全部调用方；新增授权清单和默认false真实开关，例子仅明确mock。同步RBAC种子/权限测试，真实附录仅显式装配。不保留兼容别名/默认物理地址/旧参数过渡测试，静态搜索清理不留源码快照测试。

## 历史：Task 8：同事务地址幂等、并发与原授权（已完成）

**Files:** 调试协调在app/integrations/ecs_command/dev.py，controller调用并提交；复用上述测试及现有PG/HTTP fixture，不互相import测试文件。

**Interfaces:** create_debug_command接收session/service/可信owner/client_request_id/ecs_url及原业务输入，另注入服务端authorize_real权限检查回调（不是请求字段），复用核心create后读取最终赢家冻结origin，核对后返回(CommandView, origin)；get_debug_command按owner/code返回同形或None。不改核心create/DTO/表。已登记重入先沿原身份查回，不按新grant重解释原合同；新登记才校验当前授权/权限，最终唯一键仲裁后仍核对地址。

- [x] 同owner/键同输入同规范化地址返回原编号，含已完成/HELD及新登记许可撤回；异输入/换址409。断言原字节/时间/快照不变，返回持久原地址，不echo输入。登记/核对/提交失败共同回滚，零新增义务。
- [x] PG并发同键同址只一命令/义务且同编号；同键异址一赢家、另一方取得赢家后409，输家地址零发送，不回滚赢家。复用owner/call_key唯一键，不将URL/kind/grant拼入键、不加幂等表或新锁序，不能只在创建前查一次。
- [x] A/B各自冻结并命中各自模拟端；重试只用原origin/编号/字节/窗口。撤销原grant/版本或关闭真实开关阻止后续发送门禁，不改投B、不保证取消在途；依赖缺失/不获准沿用既有暂停/503。保留原附录及合法Result，重开开关/改址/换ID不解封UNKNOWN/HELD；覆盖提前/在途、重复/矛盾及结果事务。

## 历史：Task 9：隔离回归、教程兑现及证据（已完成）

**Files:** 复用上述ECS/HTTP测试和四份文档，按影响运行WMS/诊断等既有回归；不新建平行示例。

- [x] 新无.env副本、一次性PG/Redis及两个回环模拟端验证请求URL全链路、原字节/一次义务/并发输家零命中。真实类别门禁用回环桩+非演示的测试专用附录验证，不连接真实设备、不声称合同已获供应商确认；非回环精确授权做纯策略测试，不探测现场。PG竞争参数实际运行，SQLite跳过不算通过。
- [x] 复用两条许可回归、两个P2/P3及WMS/UNKNOWN/HELD/重启/Result事务，仅补薄入口缺口。按范围运行全仓/Ruff，Docker用例独立真实执行，不改skip/断言骗过；新证据与历史摘要分开，部署来源控制不能用单元测试冒充已落实。
- [x] 从空隔离环境照做教程§11的mock配置/必填地址/OpenAPI/查询与清理步骤；核对未授权回环也403、空清单零权限、真实开关默认关闭。记录命令/退出码/skip/命中及失败清理，独立复核工作区/index/stash保护后才标已验证；不自动提交/推送/合并或进入Task 10。

## 历史：自动通道补充任务（本轮，已完成）

范围：app/integrations/ecs_command/runtime.py及公开导出、dev装配、app/main.py、薄HTTP守卫、既有夹具/回归、四份文档和.env.example。通用app/reliable、协议、事务及Result处理不变；验证使用当前feature的无.env副本，不提交/推送/合并，不迁移本机数据库或连接真实设备。

- [x] 先复现无ECS配置不装配的问题；统一configure_ecs(app, routes, annexes)，local自动调用并删除targets参数。正式接入保持设备路由/附录显式注入，无默认生产地址或新设备表。
- [x] 生命周期自动加入唯一SNAPSHOT逻辑通道，沿用既有资源默认值及Sender/outbox；保留原持久目标标识。新增WMS配置保留/冲突拒绝、共享预算、不同DEVICE动态地址冻结及单Sender真实回环回归；空授权、真实门禁、原幂等/未知交付用例复用。
- [x] 同步教程和四份文档，清理手工ECS配置用法；无ECS目标配置的隔离实跑、全仓/Ruff及独立工程复核，记录本轮结果、清理与保护核对。Task 10继续未执行。

Ruling：内部持久目标标识保持稳定，只移除用户配置职责；不改写历史消息或添加别名/兼容层。误改目标标识可能影响原消息领取与身份，因此不在本轮重命名。

## 历史：自动通道补充验证结果（2026-10-26）

| 本轮检查 | 实际结果与边界 |
|---|---|
| RED→GREEN | 原逻辑在未配置ECS目标时不装配，新增回归1 failed；修复后SQLite专项73 passed/19 skipped，仅作快速路径，不认证PG；随后ECS/HTTP/runtime专项299 passed/16 skipped，PG竞争与真实应用连接实际执行 |
| 最终全仓 | internal runner中`uv run python -m pytest -q -rs -p no:cacheprovider -k 'not test_site_override_mounts_external_configuration_read_only'`：2213 passed、23 skipped、2 deselected，退出码0 |
| 实际Docker用例 | 独立host venv/源码副本执行上述Docker节点：2 passed，退出码0；专属CLI配置/唯一Compose项目，不挂socket到runner、不启动业务栈 |
| 教程从空副本照做 | §6/§8/§11原样提取，先测试再Swagger；回环8 passed，完整ECS253 passed/16 skipped，Ruff通过（335 files）；与全仓子集不累加 |
| 无手工ECS配置的真实HTTP | RELIABLE_TARGETS={}启动：/docs/OpenAPI、登录、必填ecs_url、未授权403、等价重入、换址409、Mock ACK、人工模拟Result/重复及持久原地址查询均通过；一条命令/义务，无设备或参考业务表 |
| 自动通道关键回归 | 原有WMS目标与资源预算保留；两项应用SUCCESS/FAILED参数均捕获到唯一实际Sender及同outbox。正式装配注入设备路由，不同DEVICE冻结不同origin、共用一个逻辑目标；配置冲突不覆盖WMS |
| 语义复用 | 两条许可回归、两个P2/P3、真实门禁、UNKNOWN/HELD、并发唯一赢家、Result事务、WMS/STATIC和全局日志用例由最终全仓执行；核心协议/命令/结果/可靠调度代码本轮未改 |
| 最终工程复核 | 独立只读审查本轮baseline差异、生命周期/发送器/授权和四份文档，无具体待修复问题 |
| 静态与保护 | Ruff check/format --check全仓通过（339 files）；19处本地链接、5个Bash语法块、2个JSON示例、工作区与暂存区diff --check通过；应用/测试/脚本/迁移与最终验证副本一致 |

23个skip为SQLite不适用的PG锁/竞争或独立Sender连接等参数，对应PG参数实际执行；两项Docker测试另行补验，不把deselected算作runner内通过。旧手工目标方式只在历史证据中保留，不是当前操作说明；不保留接受旧配置的兼容路径。

本轮临时源码无.env，使用一次性PG/Redis与internal网络，runner/存储无发布端口、无宿主socket；仅教程API有本轮专属bridge及宿主回环端口。失败路径与成功路径均按本轮唯一标签清理，主runner/存储/匿名卷/网络/自建镜像已移除，临时源码和日志保留供核验。

四份文档和.env.example已同步，原有单表/请求URL文档改动保留并更新；HEAD、暂存区及stash指纹不变，范围外文件保持原内容。未提交、推送、合并PR、迁移本机数据库或连接真实设备；Task 10仍三项未完成，实际供应商合同/来源控制/现场许可待确认。

## 历史：Task 7–9 请求URL版本执行记录（2026-10-26）

本段为自动通道补充前的已验证基线，不作为当前手工配置指南；本轮新增证据另列。

实施仅涉及调试入口/适配、配置/RBAC种子、已有测试及四份文档；核心ECS/WMS协议、创建/Result事务和可靠调度未修改。不新增表、迁移、兼容层、客户端池或Sender；当前原地址/合同快照继续保留，不为旧调试命令补造新授权或改写原记录。

Ruling：调试快照在既有JSON中额外冻结kind。否则同id/version的real改成mock可能削弱原发送门禁；回归先出现SQLite/PG两项失败，再修正为类别变化即围栏。框架RouteSnapshot及wire保持不变。

Ruling：真实附录不得使用内置演示模型，即使改名注册为其他annex_id也拒绝；对应公开HTTP回归先返回200而失败，修复后503且零义务。显式测试附录用于门禁测试，不作为供应商确认。

Ruling：独立复核发现IPv6展开/压缩形式未共用canonical IP（P2）；公开HTTP回归先403失败，调试规范化写回address.compressed后，匹配授权并返回同一原命令。核心normalize_origin不改；补验IPv6及演示附录两项通过，复核无新增问题。

验证准备修正：首轮全仓2199 passed、23 skipped、2 deselected、1 failed，失败为临时副本copyfile丢失shell脚本执行位，恢复脚本回归无法启动；改用copy2保留权限，没有修改业务脚本或放宽断言。两项Docker专用用例先因独立HOME不含Compose插件而失败，使用本轮专属CLI配置指向已安装插件/本机Docker socket后2 passed；不向runner挂载socket。

教程首轮自动HTTP早于服务就绪失败，补就绪等待；第二轮实际HTTP通过，但先人工登记的长期结果证据污染后续空库计数。顺序改为先测试、后Swagger手工演示，分别从空副本重跑；失败/成功资源均按本次标签清理，不清理用户数据或全局Docker资源。

最终验证使用无.env副本、internal网络、一次性PG/Redis；存储和runner无发布端口、无宿主socket挂载。应用/测试/脚本/迁移源码逐文件与最终验证副本比对一致。中间一次全仓因独立复核发现IPv6问题主动中止，不作为通过证据；修复后完整重跑如下。

| 本轮最终检查 | 实际结果与边界 |
|---|---|
| 全仓（internal runner） | `uv run python -m pytest -q -rs -p no:cacheprovider -k 'not test_site_override_mounts_external_configuration_read_only'`：2205 passed、23 skipped、2 deselected，退出码0 |
| 两项实际Docker回归 | 独立host venv/临时源码/专属CLI配置执行上述Docker节点：2 passed，退出码0；唯一Compose项目按测试清理，不启动业务栈、不访问本机数据库 |
| 最终教程专项 | 从空副本原样执行§6/§8准备与验证块：闭环8 passed；完整ECS专项245 passed、16 skipped；Ruff通过（334 files already formatted）。专项是全仓子集，不累加通过数 |
| 实际Swagger HTTP | 原样执行§11启动配置，/docs/OpenAPI、必填ecs_url、登录、未授权目标403、等价地址重入、同键换址409、持久原地址查询、唯一Sender到本应用Mock ACK、人工模拟Result/重复/查询通过；数据库仅一条命令/义务，未建业务参考表或设备表 |
| 新薄入口与竞争 | PG同键同址/异址唯一键竞争后，实际两个回环端只命中赢家；不同键A/B分别命中各自地址且原字节一致。真实门禁使用测试附录/桩验证，未连接真实ECS |
| 行为保留 | 两条许可回归、两个P2/P3、消费/业务变化竞争、WMS、诊断、UNKNOWN/HELD/重启/Result事务均由最终套件复用；撤销权限后的原owner重入、真实开关关闭后合法在途Result仍可应用有新增覆盖 |
| 独立工程复核 | 发现并修复IPv6 P2后复核，无新增具体问题；静态复核不替代运行测试 |
| 最终静态检查 | 全仓Ruff check及format --check通过（338 files）；文档19处本地链接、5个Bash语法块、3个JSON示例及工作区/暂存区diff --check通过 |

23个skip为SQLite不适用的PG锁/竞争、独立应用Sender连接等参数，对应PG参数实际执行；两项Docker用例在host独立补验，不将deselected算作runner内通过。Task 7–9共9项完成，Task 10三项门槛均未执行。真实地址、附录确认、Result来源控制及现场许可保持未关闭。

主验证与教程的容器、匿名卷、网络及本轮自建镜像按唯一名称/标签清理；临时源码与日志保留供核查。HEAD、暂存区和stash指纹保持，其他原有文件内容保持；没有提交、推送、PR操作、本机迁移或真实设备连接。


## 历史：自动通道 simplify（2026-10-26，已完成）

本轮只精简自动通道装配及测试重复逻辑，不修改四份文档的接口/合同：生命周期只读取一次已装配服务；目标配置合并为一个字典，原资源默认值与覆盖顺序不变；两个测试入口共用reset_ecs_assembly，WMS资源/冲突用例共用已验证Settings夹具。不新增生产抽象、兼容层或可配置项。

移除只断言service存在的装配烟测；未配置ECS目标也可创建/发送、同Sender/outbox及SUCCESS/FAILED结果的实际应用生命周期回归保留。设备动态地址、必填ecs_url、地址与真实门禁、两条许可、两个P2/P3、事务/幂等/UNKNOWN/HELD等行为测试保留。

| 当前检查 | 结果与边界 |
|---|---|
| simplify前隔离专项 | 300 passed、16 skipped，退出码0 |
| simplify后隔离专项 | 299 passed、16 skipped，退出码0；仅删除上述重复烟测，不以减少PG参数或放宽断言获得通过 |
| 最终全仓 | internal runner中`uv run python -m pytest -q -rs -p no:cacheprovider -k 'not test_site_override_mounts_external_configuration_read_only'`：2212 passed、23 skipped、2 deselected，退出码0 |
| Docker专用回归 | 独立host venv/源码副本执行上述Docker节点：2 passed，退出码0；唯一Compose项目真实运行并清理，不挂socket到runner |
| 静态检查 | 全仓Ruff check/format --check通过（339 files）；19处文档本地链接、5个Bash语法块、2个JSON示例及工作区/暂存区diff --check通过 |
| 差异与保护核对 | 仅五个代码/测试文件与本段PLAN记录变化；应用/测试/脚本/迁移与最终验证副本逐文件一致，HEAD/暂存区/stash指纹不变，范围外文件内容不变 |

验证使用无.env副本、一次性PG/Redis及internal网络，无主机存储端口或宿主socket挂载。Task 10三项门槛保持未执行；不提交、推送、合并PR、迁移本机数据库或连接真实设备，保留原有改动、暂存区和stash。

23个skip为SQLite不适用的PG锁/竞争或独立Sender连接等参数，对应PG参数实际执行；两项Docker用例另行补验，不将deselected写作runner内通过。相比simplify前全仓仅减少一个重复装配烟测，实际HTTP、两条许可、两个P2/P3、门禁、事务及未知交付等回归均保留并执行，专项数量不与全仓累加。

本轮容器、匿名卷、internal网络及自建镜像按唯一名称/标签清理，日志和临时源码保留供复核。接口/教程合同未变，本轮以实际HTTP生命周期回归验证，未重复执行整段手工Swagger教程或现场联调。
