# ECS 命令与结果闭环

本能力提供事务内命令登记、冻结投递、持久结果与授权查询。模板没有正式设备路由或附录，自动装配发送通道不等于授权真实设备发送。

首次接入可从[最小业务示例与入门教程](ecs-command-tutorial.md)开始：最小业务示例统一采用单表状态机，Reference及调用测试已原地迁移；实际验证证据见实施计划。

## 设备身份与地址来源

DEVICE_CODE定义为全系统唯一，沿用wire字段device_code。地址由设备管理的ecs_origin或调试适配层提供，ECS通用模块只依赖DeviceRouteProvider；只有地址信息时不单建ECS配置表。正式设备表/全局唯一约束当前尚未实现，设备表是正式地址来源，不是通用模块的硬依赖。原命令地址快照、原授权及UNKNOWN/HELD边界保持不变。

Swagger联调已采用内置通用协议合同并自动装配，直接提供ecs_url、device_code、task_type和params，不逐动作手工登记；稳定client_request_id及执行timeout_ms仍保留。状态为GENERIC_PROTOCOL_VERIFIED / WINDOW_DECISION_APPROVED / SUPPLIER_CONFORMANCE_PENDING，Task 17设计已关闭，Task 18–19已实现并在一次性存储/本轮模拟端验收。新验证见实施计划，真实设备仍受Task 10独立门槛约束。

## 一次性装配

本节适用于正式业务严格接入；Swagger通用入口由框架自动装配，不要求开发者手工维护动作合同。

应用启动前调用 `configure_ecs(app, routes=routes, annexes=annexes)`，从 `app.integrations.ecs_command` 导入。无需给RELIABLE_TARGETS添加ECS条目，也不用为每个ECS或DEVICE配置发送目标。`routes` 实现 `DeviceRouteProvider`：创建时解析生效快照，发送门禁中锁原授权记录并返回 ALLOW/REVOKED/UNKNOWN；A→B 不改写旧命令的 A，也不自动撤销 A。缺省 UnconfiguredRoutes 拒绝解析。

`annexes` 使用 `(annex_id, annex_version)` 映射到 `DeviceAnnex(params_model, result_model, error_model, timeout_min_ms, timeout_max_ms)`，必须是已确认的参数、结果、错误及 timeout 合同；测试附录在tests中。local v3自动提供内置协议合同，详见下节。正式业务仍可保留严格版本化附录，测试qty模型不作为通用合同或供应商保证。

configure_ecs统一装配协议适配器、EcsLifecycle、DynamicClients及app.state.ecs_commands，保留原WMS映射并拒绝重复/冲突装配。主应用lifespan将统一ECS逻辑通道加入原有Sender配置，并关联同一个outbox；启动完成前不可调用create，不再启动第二个Sender。local Swagger由框架自动调用联调装配，不要重复装配正式路由。低层EcsCommands(outbox, target, routes, annexes)仍供独立运行时/测试注入使用，不是主应用的手工配置步骤。

自动通道使用SNAPSHOT和空base_url；每条消息的origin来自设备路由或通过新登记权限/合同校验的请求并冻结。单次HTTP等资源预算沿用现有RELIABLE默认值，WMS配置不变。应用不接受手工ECS目标，启动报错会提示从RELIABLE_TARGETS删除对应条目；内部稳定标识只用于保持原消息身份，不是配置入口。当前快照仅接受已实现的 HOLD，快照 RETRY 明确拒绝；STATIC 保持原 RETRY 行为、三参数和 base_url 规则。正式ECS仍无默认路由或附录，结果入口在未装配时返回 503，不会 ACK 掉无法处理的结果。

正式发送前必须关闭设计稿 C1/C2/C3/C5，确认 params/source、错误矩阵、跨重启去重及保留期、窗口与路由授权。回调来源限制必须落实。本文不提供真实地址或开箱即用的设备命令。

## 事务内创建与读取

调用 `await service.create(session, owner=CommandOwner(domain), call_key=..., device_code=..., task_type=..., timeout_ms=..., params=...)`，由调用者提交。owner 来自受信任业务装配，不能从外部请求任意选择；没有生产 HTTP 发命令接口。

同业务调用键、同原输入返回旧命令；不同输入冲突。创建先查旧命令，再解析路由并生成一次 UUIDv7、时间和原始请求。回滚不留下命令/出站消息。技术重试保持 command_code、时间、字节和冻结 origin，不按设备历史失败封锁新独立意图。

`get_command(session, owner=..., command_code=...)` 和 `get_by_call(session, owner=..., call_key=...)` 返回只读 DTO，区分 command status、delivery status、实际 ACK、已应用结果、暂停原因和对账标记。查询不返回可靠层 ORM。消费方须在自己的事务中保证一次推进：一动作一命令、一个固定消费者、同库副作用时，可将原命令关联、冻结业务绑定/版本和消费标记合入业务动作行，通过受保护的WAITING_RESULT→SUCCEEDED/FAILED转换完成；多消费者等场景仍需按实际需求设计各自消费唯一性。不能凭 ACK/DONE 推断物理完成，也不自动撤销已应用业务。

调用方重复创建使用稳定身份：Swagger的client_request_id映射到现有call_key，不进入ECS wire，也不要求同时提交两个字段。请求URL版在调试模块额外比较最终命令的冻结origin：同键同输入同规范化URL返回原命令，换址409；不把URL拼进调用键。命令登记后的内部重试由可靠Sender沿用原持久记录，业务方不重新生成ID或调用create。Status查询尚未实现，仅规划为诊断；online/IDLE不证明执行许可或请求未接纳，不触发已接纳命令的重发或HELD解封。

## 无设备表的ECS联调入口（内置协议合同）

新登记要求ENVIRONMENT=local、登录及dev:ecs:send_real。框架自动提供统一信封、ACK/错误和Result协议合同；task_type不查动作白名单，params/data接受并保留合法JSON对象。统一ErrorDetail(code,msg)、SUCCESS/FAILED条件、类型/大小/身份等协议校验保留，动作是否支持及业务字段语义由对端ECS/正式业务负责。正式消费仍校验当前许可、绑定/版本和结果，协议SUCCESS不等于业务已完成。

ecs_url必填且沿用HTTP(S) IP origin规范化、固定路径、无凭据/query/fragment/DNS/重定向/环境代理及非法地址拒绝。DEVICE_CODE全系统唯一；不新建ECS表、逐地址/动作清单或联网开关。新增设备、动作或改址只改请求；client_request_id保持稳定，timeout_ms仍为现有非负int64执行毫秒值，不新增默认值或请求窗口字段。

受限工厂及历史附录通过可信Python启动依赖传入，接口为configure_local_ecs(app, *, environment, history_annexes=None, client_factory=AsyncHttpClient)。普通主lifespan只传environment，自动使用内置合同；隔离入口先持有本轮模拟端并绑定受限client_factory，再进入真实主lifespan。它须在服务/provider均未预装时调用原生产装配函数一次，先检查冲突、合并合同并构造DynamicClients(factory=client_factory)，再发布服务/provider、关联outbox、启动唯一Sender。不得提前占用app.state.reliable_client_provider、先手工造服务绕过自动分支或在冲突时重置状态。底层configure_ecs只增加同名可选工厂依赖，现有冲突保护不放宽。

history_annexes仅来自可信启动模块的版本化原DeviceAnnex模型；装配前复制/校验，并与ecs.dev.protocol/1一次合并到同一个service.annexes。历史键须为非空字符串二元组、值为DeviceAnnex，内置保留键冲突必须拒绝，不能覆盖内置模型或启动后补字典。新v3只选内置合同，原键仅供历史Result精确校验；不恢复actions/annexes旧参数别名、动作登记或v2发送。实际启动须分别验证无历史输入的自动路径、有历史输入时合法原结果及严格拒绝、缺输入时首次Result 503/已有重复证据返回原响应；详见[设计§10.3.1–10.3.2及§10.7](../superpowers/specs/2026-10-25-ecs-minimal-business-tutorial-design.md)。这些依赖不是Swagger/.env选项，已通过实际主lifespan回归。

enterprise_admin已批准统一最低合同：W=300秒，从本方首次创建命令起算并冻结expires_at；D=对端ECS持久确认确定终态后至少24小时（86400秒），活动/未决命令去重依据不按TTL清理。W不是执行timeout或结果截止，重启不重算；原退避/Retry-After、原next_attempt_at前零发送/到期且窗口有效才重试、超期停止新尝试及合法迟到结果规则不变。数值是本项目规范选择，不是测试值推导或供应商既有证明，完整依据见[设计§10.4](../superpowers/specs/2026-10-25-ecs-minimal-business-tutorial-design.md)。在contracts.py定义共享PROTOCOL_RETRY_WINDOW=timedelta(seconds=300)，不新增请求/.env/逐动作窗口参数，已完成隔离验证。

W=300秒同时约束调试及正式新命令，唯一来源迁至contracts.py并从ECS包导出PROTOCOL_RETRY_WINDOW，dev.py/正式DeviceRouteProvider/service.py共同引用。共享EcsCommands.create先查原调用：旧同键同输入只读返回原身份且保持原冻结期限，查询/响应字段不变，异输入仍冲突；仅新调用在路由解析后、生成/写入命令与outbox前检查retry_window为timedelta且精确300秒。不一致按既有AppError/HTTP 503机制拒绝，message/detail提示路由提供方引用共享常量，零新义务/HTTP；不以400业务参数错误解释，不宣称数据库连接故障，不新增错误码/协议字段。不取整、截短或放大路由值，也不豁免正式路径。 路由快照构造也将缺失、错误类型及非正窗口识别为配置503，避免在共享创建检查前误报400/500；发送时遇到损坏快照仍保守围栏，不套用当前300秒改写旧期限。

验收须覆盖正式路由300秒的命令/消息期限一致、60/600秒及亚秒偏差拒绝，以及历史60/600秒记录在当前路由不一致/不可用时仍可同参只读重入、异参冲突、存量按原安全重试/期限恢复。Sender/Result不重新套用当前常量，旧dev策略围栏、UNKNOWN/HELD和正式业务许可/原授权保持；保留原期限不等于放行旧策略。详见[设计§10.4.1](../superpowers/specs/2026-10-25-ecs-minimal-business-tutorial-design.md)。

D从对端自身持久确认终态的terminal_at起算，不用载荷finish_time或本方回调到达时间替代；未知terminal_at不启动清理倒计时，重启/时间异常不能缩短期限。ECS须在ACK/物理派发前持久接纳，正常/异常进程退出、主机重启及断电恢复后同号同载荷仍返回原接纳事实、不产生第二份执行义务，同号异载荷仍409。执行事实不确定先核对，不能重做或伪造终态；存储丢失/旧备份回滚/恢复出厂不能以空状态直接恢复受影响接纳。合法503/429未接纳仍按原身份重试，不能把可能已接纳谎报未接纳。去重保证有界，不因D到期重发旧命令或换号重做UNKNOWN/HELD；原Result重报义务不缩短，回报未确认时不得因D到期清理原结果/去重依据。本方长期证据/保留配置不改成24小时。

Task 17仅关闭设计决策；Task 10供应商版本/配置、持久保留、跨重启/断电及物理动作、来源/网络/许可仍未验收，只有单独授权并取证才可判符合。Task 18–19已完成本方实现/隔离验证，不自动获得真实发送许可。缺策略/非法策略503的防御仍只影响新登记，不阻断WMS/Result启动、原owner重入或改写/暂停既有v3快照。

使用route_id=dev.ecs.network/version="3"及内置合同ecs.dev.protocol/1，复用既有快照字段与唯一Sender；不增加表或迁移。v1/v2、旧grant及未知策略保守围栏，不转换、补授权、改版本/改址或自动重放；正式路由版本不变。切换须先停止全部旧策略Sender并确认，不混跑，本轮不实际停服务。

新旧owner同键同输入同规范化地址只读找回原身份，异输入/换址409；并发唯一赢家返回后、提交前比较持久origin，输家零发送。编号、字节、合同、窗口及deadline冻结，命令/outbox/快照仍同事务，URL/策略版本不进入调用键或wire。历史Result继续依赖原附录和发送证据，不以通用合同宽松解释旧严格结果；尚无持久结果证据且缺原附录时503、零副作用；已有持久重复结果沿用原响应。不承诺未发送/已围栏命令的Result可应用。

专用权限只管新登记，撤权不暂停既有可识别v3命令。停止后续发送须停止全部相关Sender并防止自动拉起；ECS/WMS共享主应用lifespan，停宿主影响两者出站及HTTP/Result。有界关闭、在途可能交付、shutdown unconfirmed、租约中断及合法迟到结果规则不变。恢复覆盖首次未发送、NOT_SENT及合法503/有效429判定NOT_RECEIVED后的PENDING，保留原退避、Retry-After、next_attempt_at、计数/证据和deadline；到期前不发，超期按原TTL，不重算或延长。UNKNOWN/HELD与无效/超限Retry-After不因重启、换址/换键或Status恢复而解封。

隔离验收仅允许本轮自持模拟端；受限客户端工厂先于Sender装入，其他有效origin在TCP连接前拒绝并直接断言连接入口零调用。隔离沿用已批准的生产W=300秒，TEST ONLY仅指模拟目标和数据，不证明供应商已满足D或跨重启合同。允许端仍走真实HTTP，保留两条许可、两个P2、P3及消费竞争；实施步骤见[PLAN Task 17–19](../superpowers/plans/2026-10-25-ecs-minimal-business-tutorial-plan.md)，本轮已完成隔离验证，Task 10未执行。

## 最小业务单表状态机

一条动作行合并意图、调用关联和消费标记：稳定业务ID/可信owner/business_key、原动作输入、command_code、当前绑定/版本与冻结的原绑定/版本、业务state。READY→WAITING_RESULT与命令/outbox共同提交；安全消费才转SUCCEEDED/FAILED，与同库业务写共同回滚或提交。终态不可重用，同身份重入返回原命令。取消可从READY/WAITING_RESULT转CANCELLED，但只撤回业务许可，不代表设备取消。

新动作由受信任业务入口绑定business_key，登记事务通过`current_business(session, *, business_key)`读取当前许可、设备和业务版本；未知或不允许时拒绝，不从新行READY、客户端字段或路由版本推导许可。真实接入使用已有权威业务记录，不新建许可表，按“权威业务记录→动作行”统一锁序或等价版本条件保护；权威就在动作行时只锁该行。模板Reference仅注入测试事实替身，不将其当作生产业务聚合的事务证明。

同business_key取消/换绑/终态后，原动作同输入重入仍返回原命令；未重新取得当前有效许可或请求设备不符时，新动作按当前事实拒绝；当前版本从1变2且明确获准时，新动作冻结2，原动作保留原命令与冻结1。两条原回归的等价迁移见教程§4，不能给新行预置READY或更换business_key来弱化断言。不同业务的独立有效意图不因同设备历史失败永久受阻，UNKNOWN也不能借新id重发。

取消、换绑、版本变化与消费锁同一动作行或采用已证明等价的条件更新；变更先提交旧结果不推进，消费先提交终态不被覆盖。业务状态与框架投递、DEVICE Status分开；UNKNOWN/HELD不自动变成业务FAILED，不因换键、改址或设备空闲解封。详见[教程§4–5](ecs-command-tutorial.md)和[设计§11](../superpowers/specs/2026-10-25-ecs-minimal-business-tutorial-design.md)。

单表仅适用于一动作一命令、一个消费者及同库业务副作用；多命令、多消费者或外部副作用另按需求设计。框架命令/outbox/发送证据/结果证据表保持不变。Reference及调用测试统一使用单表，被替代的调用/消费表及旧接口已删除；两个P2、P3及原子登记、消费、回滚和竞争保持回归覆盖。Swagger本地Mock继续不创建业务表。

## 发送与结果

POST `/api/v1/device/command` 使用白皮书字段。仅合法 200/ACK 为已接纳；合法 503、有效 429 和可证明 NOT_SENT 可按原身份退避。UNKNOWN、409 身份冲突或租约中断进入 HELD，不自动重放；HELD 不支持普通 resume，不由 TTL 或终态保留清理。快照公共 hold/settle 必须注入原冻结协议的生命周期；ECS 裸 settle 在没有可应用的持久结果时被拒绝，不能凭基础设施 API 伪造接纳。STATIC 的空配置 settle 保持原行为。ECS Retry-After 不截短，超本地等待范围暂停；WMS 保留原截短与重试规则。

门禁提交当前尝试依据后才发 HTTP；首次或后续尝试仍可能交付时，Result 可先于 ACK 应用。历次均明确未接纳且没有新在途尝试的结果只保存冲突。提前 Result 封锁并撤销租约；封锁不由迟到结果解除。迟到响应只保存证据与必要的对账标记，不降级已应用结果。

POST `/api/v1/callback/result` 严格校验核心包络及冻结附录，忽略顶层诊断扩展；成功提交后返回 200/ACK。未知命令/设备不匹配 404，提前/矛盾 409，非法输入 400，超限 413，事务或原附录不可用 503。结果证据、收据、命令投影及 settle 同事务。长期证据在短期收据/可靠消息清理后仍提供原响应，不重新创建发送义务。Event 接口保持原语义。

本项目内的通用能力隔离参考接入与端到端验收见[隔离设计](../superpowers/specs/2026-10-25-ecs-first-consumer-isolation-design.md)与[实施计划](../superpowers/plans/2026-10-25-ecs-first-consumer-isolation-plan.md)。参考目录、记录及消费只存在于 tests；通过不代表供应商、生产路由或现场验收，也不要求先指定外部业务项目。

## 发布与验证边界

迁移 `a025a20261025` 扩展可靠状态/快照并创建命令、发送依据和结果证据表。旧 STATIC 数据保持可读。存在快照、HELD 或 ECS 证据时拒绝破坏性 downgrade。停用所有旧 Sender/维护进程后，执行 `uv run alembic upgrade head`，再启用新版本；不支持旧二进制回退后解释新消息。本地 `--reload` 仅重载代码，不会自动迁移数据库。若 Sender 日志出现 SQLSTATE `42703`（缺列）或 `42P01`（缺表），先用 `uv run alembic current` 对照 `uv run alembic heads` 检查迁移状态，按上述停机顺序升级，勿通过捕获后忽略异常继续发送。

本地快测使用 SQLite；并发、唯一键竞争、锁序、迁移与回滚必须使用显式一次性 PostgreSQL/Redis。测试与模拟对端不证明供应商去重、真实物理动作或业务现场验收。Status、具体 Event、结果纠正、人工重发与交互控制台不在 P1。


## 后台日志排障

可靠发送器日志用 `phase` 区分快照维护（`maintain_snapshots`）、过期处理（`expire_pending`）、租约回收（`recover_expired`）、领取（`claim`）、派发（`dispatch`）及独立投递（`delivery`）；关闭资源时记录对应清理阶段。数据库异常保留 SQLSTATE、驱动异常类型及迁移提示，调用栈仅保留文件、行号和函数，不输出原始异常消息、SQL、参数或源码。

扫描首次失败输出完整诊断。同阶段、异常类型、驱动类型、SQLSTATE、缺失对象、约束名和抛出位置均相同的连续失败，在每 60 秒后的下一次失败时汇总总次数与本窗口抑制次数，不重复堆栈。每条汇总仍保留首次诊断的 SQLSTATE、驱动类型、项目内调用位置和修复提示；标准 PostgreSQL 缺列/缺表错误还给出受限格式的 `missing` 对象名，不回显任意驱动消息。错误变化时先用原诊断结算被抑制次数，再立即输出新诊断。完整扫描再次成功后记录一次 `scan recovered`，包含失败总次数、尚未汇总的抑制次数和持续时间；满并发而跳过领取的扫描不作为恢复证据。计数仅保存在当前 Sender 内存中，重启后重新开始。独立投递任务的异常逐条记录，避免合并不同消息的问题。

日志降噪不改变轮询间隔、重试、事务或未知交付处理。`scan recovered` 只表示扫描恢复，不证明消息已交付、设备已执行或业务已完成。
