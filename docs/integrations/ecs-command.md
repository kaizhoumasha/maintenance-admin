# ECS 命令与结果闭环

**真实接入边界：** [真实ECS接入验收清单](ecs-acceptance-checklist.md)未启动，不属于本批次开发任务；实际对端/环境确定后再制定联调或上线计划。历史Task 10编号仅用于追溯。

本能力提供事务内命令登记、冻结投递、持久结果与授权查询。模板没有正式设备路由或附录，自动装配发送通道不等于授权真实设备发送。

首次接入从[短教程](ecs-command-tutorial.md)选择业务调用或Swagger调试；两者均不要求先运行框架验收。本文提供接口/边界参考，开发回归使用[现有测试](../testing/ecs-command-verification.md)。

PR #23已合并到develop，合并提交`479c48b41edbe1befd56e757ec92450f0098ca86`；[合并CI](https://github.com/kaizhoumasha/maintenance-admin/actions/runs/40275200539)为2322 passed、25 skipped，Ruff check及341文件格式检查通过。该数字为PR #23合并时验收证据；后续开发者体验收尾已随 PR #24 合入 develop（1ef1725）；当前回归入口见[现有测试](../testing/ecs-command-verification.md)，上列数字保留为 PR #23 的历史 CI，不作为当前运行结果。

## 设备身份与地址来源

DEVICE_CODE定义为全系统唯一，沿用wire字段device_code。地址由设备管理的ecs_origin或调试适配层提供，ECS通用模块只依赖DeviceRouteProvider；只有地址信息时不单建ECS配置表。设备管理已提供设备表及全局唯一约束；设备表是正式地址来源，不是通用模块的硬依赖。原命令地址快照、原授权及UNKNOWN/HELD边界保持不变。

Swagger联调已采用内置通用协议合同并自动装配，直接提供ecs_url、device_code、task_type和params，不逐动作手工登记；稳定client_request_id及执行timeout_ms仍保留。状态为GENERIC_PROTOCOL_VERIFIED / WINDOW_DECISION_APPROVED / SUPPLIER_CONFORMANCE_PENDING，当前回归覆盖该合同和原冻结期限，入口见[现有测试](../testing/ecs-command-verification.md)；真实设备仍受真实接入验收独立门槛约束。

## 一次性装配

本节适用于正式业务严格接入；Swagger通用入口由框架自动装配，不要求开发者手工维护动作合同。

应用启动前调用 `configure_ecs(app, routes=routes, annexes=annexes)`，从 `app.integrations.ecs_command` 导入。无需给RELIABLE_TARGETS添加ECS条目，也不用为每个ECS或DEVICE配置发送目标。`routes` 实现 `DeviceRouteProvider`：创建时解析生效快照，发送门禁中锁原授权记录并返回 ALLOW/REVOKED/UNKNOWN；A→B 不改写旧命令的 A，原授权是否撤销由路由提供方决定。`RoleRoutes` 按 `(device_role, task_type)` 选择严格附录，注入绑定与地址解析器；配置版本或地址变化即停止旧命令后续发送，保留执行记录及已可能交付命令的原身份迟到结果，见[角色接入](ecs-roles.md)。缺省 UnconfiguredRoutes 拒绝解析。

业务在 `app/businesses/<business>/` 的命令模型声明角色和版本化合同，并在 composition 写
`COMMANDS = (Model, ...)`；catalog 校验时内部编译为路由绑定和附录，应用启动装配直接使用该结果。
`annexes` 是平台装配与历史 Result 解析的内部表示；业务不直接构造或导入内部绑定/附录类型。
主应用共用 catalog 和唯一 Sender，独立平台装配的解析器注入见[角色接入](ecs-roles.md)。
测试 qty 模型不作为通用供应商合同。

configure_ecs统一装配协议适配器、EcsLifecycle、DynamicClients及app.state.ecs_commands，保留原WMS映射并拒绝重复/冲突装配。主应用lifespan将统一ECS逻辑通道加入原有Sender配置，并关联同一个outbox；启动完成前不可调用create，不再启动第二个Sender。local Swagger由框架自动调用联调装配；可信启动模块可按[角色接入](ecs-roles.md)一次组合正式角色与调试路由，不重复装配服务。低层EcsCommands(outbox, target, routes, annexes)仍供独立运行时/测试注入使用，不是主应用的手工配置步骤。

自动通道使用SNAPSHOT和空base_url；每条消息的origin来自设备路由或通过新登记权限/合同校验的请求并冻结。单次HTTP等资源预算沿用现有RELIABLE默认值，WMS配置不变。应用不接受手工ECS目标，启动报错会提示从RELIABLE_TARGETS删除对应条目；内部稳定标识只用于保持原消息身份，不是配置入口。当前快照仅接受已实现的 HOLD，快照 RETRY 明确拒绝；STATIC 保持原 RETRY 行为、三参数和 base_url 规则。正式ECS仍无默认路由或附录，结果入口在未装配时返回 503，不会 ACK 掉无法处理的结果。

实际接入时按[真实ECS接入清单](ecs-acceptance-checklist.md)确认参数合同、错误矩阵、跨重启去重及保留期、窗口、路由授权和回调来源控制。本文不提供真实地址或开箱即用的设备命令。

## 事务内创建与读取

唯一业务调用为 `await service.create(session, owner=owner, call_key=saved_key,
device_code=device_code, timeout_ms=timeout_ms, command=MoveTo(...))`，由调用者提交。
owner 来自受信任业务装配，不能由外部请求任意选择；没有生产 HTTP 发命令接口。
`command` 必须是精确声明模型，未声明类/子类或实际字段不合法时在入队前拒绝。

同业务调用键、同原输入返回旧命令；不同输入冲突。创建先查旧命令，再解析路由并生成一次 UUIDv7、时间和原始请求。回滚不留下命令/出站消息。技术重试保持 command_code、时间、字节和冻结 origin，不按设备历史失败封锁新独立意图。

`get_command(session, owner=..., command_code=...)` 和 `get_by_call(session, owner=..., call_key=...)` 返回只读 DTO，区分 command status、delivery status、实际 ACK、已应用结果、暂停原因和对账标记。查询不返回可靠层 ORM。消费方须在自己的事务中保证一次推进：一动作一命令、一个固定消费者、同库副作用时，可将原命令关联、冻结业务绑定/版本和消费标记合入业务动作行，通过受保护的WAITING_RESULT→SUCCEEDED/FAILED转换完成；多消费者等场景仍需按实际需求设计各自消费唯一性。不能凭 ACK/DONE 推断物理完成，也不自动撤销已应用业务。

调用方重复创建使用稳定身份：Swagger的client_request_id映射到现有call_key，不进入ECS wire，也不要求同时提交两个字段。请求URL版在调试模块额外比较最终命令的冻结origin：同键同输入同规范化URL返回原命令，换址409；不把URL拼进调用键。命令登记后的内部重试由可靠Sender沿用原持久记录，业务方不重新生成ID或调用create。通用 Status 查询已实现，所有环境提供正式 `GET /api/v1/ecs/status`，要求登录和独立的 `ecs:status:query` 权限，详见[通用 Status](ecs-status.md)；online/IDLE不证明执行许可或请求未接纳，不触发已接纳命令的重发或HELD解封。

## 模型声明与显式编译

以下仅为合同形状示例，不是正式动作或供应商附录。真实字段、错误、超时及 SUCCESS/FAILED 含义须先确认。
在原参数类上声明，不复制模型或更换原附录版本来迁语法：

```python
from pydantic import BaseModel, ConfigDict

from app.integrations.ecs_command import ecs_command
from app.integrations.ecs_command.contracts import ErrorDetail


class MoveResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    qty: int


@ecs_command(
    "MOVE",
    device_roles=("ARM", "ARM_BACKUP"),
    annex_id="example.arm.move",
    annex_version="1",
    result=MoveResult,
    error=ErrorDetail,
    timeout_min_ms=1,
    timeout_max_ms=30_000,
)
class MoveTo(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    qty: int


# composition.py 显式导入原模型；两角色须在 catalog definitions 中声明。
COMMANDS = (MoveTo,)
```

装饰器返回原精确类，`device_roles` 必须为非空、唯一角色 tuple。一个参数模型可供多个角色共享，
composition 只声明 `COMMANDS`。catalog 内部编译并只激活所属 definition 角色的交集；
模型至少覆盖本业务一个角色，所有声明角色须存在于显式 catalog 中，未列模型的其他业务不获得权限。
重复角色/动作、同附录键不同模型/范围、非法合同或 local 来源在资源构造前失败。
编译产物及声明元数据属于平台内部；业务不手造绑定/附录或修改元数据，不建立命令发现树。
标题保留“显式编译”锚点供历史链接跳转，编译由 catalog 负责。

参数、Result、error 及其嵌套合同须为受支持的严格类型模型，错误模型继承 `ErrorDetail`，超时范围明确。
严格模型保留各自 `extra="forbid"/"ignore"/"allow"` 策略，也支持严格对象 RootModel；不改写原类或 schema。
新请求字节遵循模型 JSON 序列化，包括别名、默认值、校验器、序列化器和 extra 策略；ignore 丢弃字段不会恢复。
这属于明确的不兼容输入边界，不能承诺与任意旧字典输入逐字节等价。

序列化前会重验实际字段，包括构造绕过、嵌套可变值，不修改调用方实例。
字段级 `Field(frozen=True)` 的 Pydantic dataclass，以及无法解析/无法唯一定位的同运行时类泛型
上下文会明确拒绝；使用显式嵌套模型表达合同。类级 frozen、唯一上下文及可由直接字段区分的泛型合同可用。

正式路由端口唯一为 `resolve_declared(session, device_code, declaration)`。
`RoleRoutes` 在同次绑定解析核对实际设备角色是否属于声明、task_type、活动附录、原模型及超时；
local 组合只委托正式路由，动态 JSON 调试由受信任适配器进入同一规范化核心和 Sender。
已有同 owner/call_key/规范输入先返回原命令，不受当前路由、换绑或活动合同变化影响；输入漂移仍冲突。
无法用模型表达历史输入时，通过原 owner/call_key/command_code 读原记录并核对，不换身份或目标重发。
历史冻结行、字节、路由、期限及 Result 合同保留；历史附录只解析原 Result，不授权新动作。
不兼容变化及专项证据见[单一 API 验收](business-development-single-api-acceptance.md)。

## 无设备表的ECS联调入口（内置协议合同）

新登记要求ENVIRONMENT=local、登录及dev:ecs:send_real。框架自动提供统一信封、ACK/错误和Result协议合同；task_type不查动作白名单，params/data接受并保留合法JSON对象。统一ErrorDetail(code,msg)、SUCCESS/FAILED条件、类型/大小/身份等协议校验保留，动作是否支持及业务字段语义由对端ECS/正式业务负责。正式消费仍校验当前许可、绑定/版本和结果，协议SUCCESS不等于业务已完成。

`POST /api/v1/dev/ecs/commands` 的 Swagger **Examples** 下拉保留平台 `MOVE` 演示，并为 catalog 已编译且实际生效的每个角色/动作提供完整六字段请求，例如 `CONVEYOR / MOVE_FORWARD`。主应用将同一份有效 `command_bindings` 传给开发路由，在挂载时固定样例；控制器不扫描业务目录、不重新编译或扩展声明中的其他角色。平台样例只取 `DevCommandIn` 的模型示例，业务 `params` 取原命令模型 JSON Schema `examples` 中首个对象；模型无对象样例时仍可选择，`params={}`，按提示查看注册目录 `command_contract` 条目的 `payload_schema` 后填写。样例超时以 30000ms 为基准，限制在该声明的最小/最大范围内。

样例设备码、ECS 地址及请求标识均为占位，使用前按模拟 ECS 修改；每个新调用换请求标识，同一次调用重试保留整份输入。角色/动作仅用于组织样例，不增加模式或选择器字段，不绑定设备，也不将调试入口切换到正式严格参数/Result 合同。`task_type` 仍可填写未登记动作。内置 Mock 只支持 `D1/MOVE` 的 ACK；`MOVE_FORWARD` 等其他动作须使用支持对应任务的模拟 ECS，不能把样例地址直接当作对应 Mock 已可执行的证明。

ecs_url必填且沿用HTTP(S) IP origin规范化、固定路径、无凭据/query/fragment/DNS/重定向/环境代理及非法地址拒绝。DEVICE_CODE全系统唯一；不新建ECS表、逐地址/动作清单或联网开关。新增设备、动作或改址只改请求；client_request_id保持稳定，timeout_ms仍为现有非负int64执行毫秒值，不新增默认值或请求窗口字段。

受限工厂及历史附录通过可信Python启动依赖传入，接口为configure_local_ecs(app, *, environment, history_annexes=None, client_factory=AsyncHttpClient, routes=None)。`routes` 可组合正式角色路由，`history_annexes` 同时提供其严格附录；非 local 显式传入角色路由会报错。普通主lifespan只传environment，自动使用内置合同；测试夹具先持有本轮模拟端并绑定受限client_factory，再进入真实主lifespan。它须在服务/provider均未预装时调用原生产装配函数一次，先检查冲突、合并合同并构造DynamicClients(factory=client_factory)，再发布服务/provider、关联outbox、启动唯一Sender。不得提前占用app.state.reliable_client_provider、先手工造服务绕过自动分支或在冲突时重置状态。底层configure_ecs只增加同名可选工厂依赖，现有冲突保护不放宽。

history_annexes仅来自可信启动模块的版本化原DeviceAnnex模型；装配前复制/校验，并与ecs.dev.protocol/1一次合并到同一个service.annexes。历史键须为非空字符串二元组、值为DeviceAnnex，内置保留键冲突必须拒绝，不能覆盖内置模型或启动后补字典。通用调试v3只选内置合同，其余键仅供该调试路径的历史Result精确校验；组合装配的正式角色仍使用各自严格附录；不恢复actions/annexes旧参数别名、动作登记或v2发送。实际启动须分别验证无历史输入的自动路径、有历史输入时合法原结果及严格拒绝、缺输入时首次Result 503/已有重复证据返回原响应；详见[设计§10.3.1–10.3.2及§10.7](../superpowers/specs/2026-10-25-ecs-minimal-business-tutorial-design.md)。这些依赖不是Swagger/.env选项，已通过实际主lifespan回归。

enterprise_admin已批准统一最低合同：W=300秒，从本方首次创建命令起算并冻结expires_at；D=对端ECS持久确认确定终态后至少24小时（86400秒），活动/未决命令去重依据不按TTL清理。W不是执行timeout或结果截止，重启不重算；原退避/Retry-After、原next_attempt_at前零发送/到期且窗口有效才重试、超期停止新尝试及合法迟到结果规则不变。数值是本项目规范选择，不是测试值推导或供应商既有证明，完整依据见[设计§10.4](../superpowers/specs/2026-10-25-ecs-minimal-business-tutorial-design.md)。在contracts.py定义共享PROTOCOL_RETRY_WINDOW=timedelta(seconds=300)，不新增请求/.env/逐动作窗口参数，已完成隔离验证。

W=300秒同时约束调试及正式新命令，唯一来源迁至contracts.py并从ECS包导出PROTOCOL_RETRY_WINDOW，dev.py/正式DeviceRouteProvider/service.py共同引用。共享规范化核心先查原调用：同键同规范输入只读返回原身份且保持原冻结期限，查询/响应字段不变，异输入仍冲突；仅新调用在路由解析后、生成/写入命令与outbox前检查retry_window为timedelta且精确300秒。不一致按既有AppError/HTTP 503机制拒绝，message/detail提示路由提供方引用共享常量，零新义务/HTTP；不以400业务参数错误解释，不宣称数据库连接故障，不新增错误码/协议字段。不取整、截短或放大路由值，也不豁免正式路径。 路由快照构造也将缺失、错误类型及非正窗口识别为配置503，避免在共享创建检查前误报400/500；发送时遇到损坏快照仍保守围栏，不套用当前300秒改写旧期限。

验收须覆盖正式路由300秒的命令/消息期限一致、60/600秒及亚秒偏差拒绝，以及历史60/600秒记录在当前路由不一致/不可用时仍可同参只读重入、异参冲突、存量按原安全重试/期限恢复。Sender/Result不重新套用当前常量，旧dev策略围栏、UNKNOWN/HELD和正式业务许可/原授权保持；保留原期限不等于放行旧策略。详见[设计§10.4.1](../superpowers/specs/2026-10-25-ecs-minimal-business-tutorial-design.md)。

D从对端自身持久确认终态的terminal_at起算，不用载荷finish_time或本方回调到达时间替代；未知terminal_at不启动清理倒计时，重启/时间异常不能缩短期限。ECS须在ACK/物理派发前持久接纳，正常/异常进程退出、主机重启及断电恢复后同号同载荷仍返回原接纳事实、不产生第二份执行义务，同号异载荷仍409。执行事实不确定先核对，不能重做或伪造终态；存储丢失/旧备份回滚/恢复出厂不能以空状态直接恢复受影响接纳。合法503/429未接纳仍按原身份重试，不能把可能已接纳谎报未接纳。去重保证有界，不因D到期重发旧命令或换号重做UNKNOWN/HELD；原Result重报义务不缩短，回报未确认时不得因D到期清理原结果/去重依据。本方长期证据/保留配置不改成24小时。

Task 17仅关闭设计决策；真实接入验收供应商版本/配置、持久保留、跨重启/断电及物理动作、来源/网络/许可仍未验收，只有单独授权并取证才可判符合。Task 18–19已完成本方实现/隔离验证，不自动获得真实发送许可。缺策略/非法策略503的防御仍只影响新登记，不阻断WMS/Result启动、原owner重入或改写/暂停既有v3快照。

使用route_id=dev.ecs.network/version="3"及内置合同ecs.dev.protocol/1，复用既有快照字段与唯一Sender；不增加表或迁移。v1/v2、旧grant及未知策略保守围栏，不转换、补授权、改版本/改址或自动重放；正式路由版本不变。切换须先停止全部旧策略Sender并确认，不混跑，本轮不实际停服务。

新旧owner同键同输入同规范化地址只读找回原身份，异输入/换址409；并发唯一赢家返回后、提交前比较持久origin，输家零发送。编号、字节、合同、窗口及deadline冻结，命令/outbox/快照仍同事务，URL/策略版本不进入调用键或wire。历史Result继续依赖原附录和发送证据，不以通用合同宽松解释旧严格结果；尚无持久结果证据且缺原附录时503、零副作用；已有持久重复结果沿用原响应。不承诺未发送/已围栏命令的Result可应用。

专用权限只管新登记，撤权不暂停既有可识别v3命令。停止后续发送须停止全部相关Sender并防止自动拉起；ECS/WMS共享主应用lifespan，停宿主影响两者出站及HTTP/Result。有界关闭、在途可能交付、shutdown unconfirmed、租约中断及合法迟到结果规则不变。恢复覆盖首次未发送、NOT_SENT及合法503/有效429判定NOT_RECEIVED后的PENDING，保留原退避、Retry-After、next_attempt_at、计数/证据和deadline；到期前不发，超期按原TTL，不重算或延长。UNKNOWN/HELD与无效/超限Retry-After不因重启、换址/换键或Status恢复而解封。

自动化测试通过现有夹具保护测试库和网络出口，直接断言非用例目标TCP零调用；这些不是Swagger或业务接入配置。新命令仍使用W=300秒，两条许可、两个P2、P3及消费竞争覆盖保留，测试通过不证明供应商D或跨重启保证。

## 最小业务单表状态机

一条动作行合并意图、调用关联和消费标记：稳定业务ID/可信owner/business_key、原动作输入、command_code、当前绑定/版本与冻结的原绑定/版本、业务state。READY→WAITING_RESULT与命令/outbox共同提交；安全消费才转SUCCEEDED/FAILED，与同库业务写共同回滚或提交。终态不可重用，同身份重入返回原命令。取消可从READY/WAITING_RESULT转CANCELLED，但只撤回业务许可，不代表设备取消。

新动作由受信任业务入口绑定business_key，登记事务通过`current_business(session, *, business_key)`读取当前许可、设备和业务版本；未知或不允许时拒绝，不从新行READY、客户端字段或路由版本推导许可。真实接入使用已有权威业务记录，不新建许可表，按“权威业务记录→动作行”统一锁序或等价版本条件保护；权威就在动作行时只锁该行。模板Reference仅注入测试事实替身，不将其当作生产业务聚合的事务证明。

同business_key取消/换绑/终态后，原动作同输入重入仍返回原命令；未重新取得当前有效许可或请求设备不符时，新动作按当前事实拒绝；当前版本从1变2且明确获准时，新动作冻结2，原动作保留原命令与冻结1。两条当前许可回归见[现有测试的回归映射](../testing/ecs-command-verification.md)，不能给新行预置READY或更换business_key来弱化断言。不同业务的独立有效意图不因同设备历史失败永久受阻，UNKNOWN也不能借新id重发。

取消、换绑、版本变化与消费锁同一动作行或采用已证明等价的条件更新；变更先提交旧结果不推进，消费先提交终态不被覆盖。业务状态与框架投递、DEVICE Status分开；UNKNOWN/HELD不自动变成业务FAILED，不因换键、改址或设备空闲解封。详见[教程登记与安全消费](ecs-command-tutorial.md)和[设计§11](../superpowers/specs/2026-10-25-ecs-minimal-business-tutorial-design.md)。

单表仅适用于一动作一命令、一个消费者及同库业务副作用；多命令、多消费者或外部副作用另按需求设计。框架命令/outbox/发送证据/结果证据表保持不变。Reference及调用测试统一使用单表，被替代的调用/消费表及旧接口已删除；两个P2、P3及原子登记、消费、回滚和竞争保持回归覆盖。Swagger本地Mock继续不创建业务表。

## 发送与结果

POST `/api/v1/device/command` 使用白皮书字段。仅合法 200/ACK 为已接纳；合法 503、有效 429 和可证明 NOT_SENT 可按原身份退避。UNKNOWN、409 身份冲突或租约中断进入 HELD，不自动重放；HELD 不支持普通 resume，不由 TTL 或终态保留清理。快照公共 hold/settle 必须注入原冻结协议的生命周期；ECS 裸 settle 在没有可应用的持久结果时被拒绝，不能凭基础设施 API 伪造接纳。STATIC 的空配置 settle 保持原行为。ECS Retry-After 不截短，超本地等待范围暂停；WMS 保留原截短与重试规则。

门禁提交当前尝试依据后才发 HTTP；首次或后续尝试仍可能交付时，Result 可先于 ACK 应用。历次均明确未接纳且没有新在途尝试的结果只保存冲突。提前 Result 封锁并撤销租约；封锁不由迟到结果解除。迟到响应只保存证据与必要的对账标记，不降级已应用结果。

POST `/api/v1/callback/result` 严格校验核心包络及冻结附录，忽略顶层诊断扩展；成功提交后返回 200/ACK。未知命令/设备不匹配 404，提前/矛盾 409，非法输入 400，超限 413，事务或原附录不可用 503。结果证据、收据、命令投影及 settle 同事务。长期证据在短期收据/可靠消息清理后仍提供原响应，不重新创建发送义务。Event 接口保持原语义。

参考目录、动作与安全消费只存在于tests，使用现有pytest验证，无需独立隔离设计或实施阶段；已有行为与回归入口见[现有测试说明](../testing/ecs-command-verification.md)。测试不代表供应商、生产路由或现场验收。

## 发布与验证边界

迁移 `a025a20261025` 扩展可靠状态/快照并创建命令、发送依据和结果证据表。旧 STATIC 数据保持可读。存在快照、HELD 或 ECS 证据时拒绝破坏性 downgrade。停用所有旧 Sender/维护进程后，执行 `uv run alembic upgrade head`，再启用新版本；不支持旧二进制回退后解释新消息。本地 `--reload` 仅重载代码，不会自动迁移数据库。若 Sender 日志出现 SQLSTATE `42703`（缺列）或 `42P01`（缺表），先用 `uv run alembic current` 对照 `uv run alembic heads` 检查迁移状态，按上述停机顺序升级，勿通过捕获后忽略异常继续发送。

本地快测使用 SQLite；并发、唯一键竞争、锁序、迁移与回滚必须使用显式一次性 PostgreSQL/Redis。测试与模拟对端不证明供应商去重、真实物理动作或业务现场验收。P1 原始范围不含 Status；当前[通用 Status](ecs-status.md)及[设备管理](device-management.md)已独立提供，不改变命令许可与原 Result 规则；具体业务 Event、结果纠正和人工重发仍需各自确认合同与实施范围。


## 后台日志排障

可靠发送器日志用 `phase` 区分快照维护（`maintain_snapshots`）、过期处理（`expire_pending`）、租约回收（`recover_expired`）、领取（`claim`）、派发（`dispatch`）及独立投递（`delivery`）；关闭资源时记录对应清理阶段。数据库异常保留 SQLSTATE、驱动异常类型及迁移提示，调用栈仅保留文件、行号和函数，不输出原始异常消息、SQL、参数或源码。

扫描首次失败输出完整诊断。同阶段、异常类型、驱动类型、SQLSTATE、缺失对象、约束名和抛出位置均相同的连续失败，在每 60 秒后的下一次失败时汇总总次数与本窗口抑制次数，不重复堆栈。每条汇总仍保留首次诊断的 SQLSTATE、驱动类型、项目内调用位置和修复提示；标准 PostgreSQL 缺列/缺表错误还给出受限格式的 `missing` 对象名，不回显任意驱动消息。错误变化时先用原诊断结算被抑制次数，再立即输出新诊断。完整扫描再次成功后记录一次 `scan recovered`，包含失败总次数、尚未汇总的抑制次数和持续时间；满并发而跳过领取的扫描不作为恢复证据。计数仅保存在当前 Sender 内存中，重启后重新开始。独立投递任务的异常逐条记录，避免合并不同消息的问题。

日志降噪不改变轮询间隔、重试、事务或未知交付处理。`scan recovered` 只表示扫描恢复，不证明消息已交付、设备已执行或业务已完成。
