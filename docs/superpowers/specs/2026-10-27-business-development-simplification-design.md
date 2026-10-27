# WMS / Transport 与 ECS 接入开发简化重构设计

日期：2026-10-27。状态：**正式设计稿，待评审、未实施。本文记录已讨论的方向，不表示代码、业务合同或现场接入已经获验收。**

本轮只整理文档；不修改应用、测试、迁移、配置或生产数据，不连接真实 WMS/ECS，不提交或推送。后续实现须按本文阶段另行形成实施计划。现行接口仍以[注册指南](../../integrations/registration.md)、[业务包指南](../../integrations/business-packages.md)、[ECS 命令](../../integrations/ecs-command.md)及[出站结果契约](../../integrations/outbound-results.md)为准；本文明确标记的新增 API 当前不可调用。

## 1. 目标、范围与文档分工

面向会写 Python、Pydantic 和普通数据库业务代码，但不熟悉投递、设备协议及并发控制的开发人员。降低新增接入时必须同时掌握的概念、重复声明、字符串/字典拼接及跨层修改数量；可靠性、业务身份与完成条件仍须可追溯。

目标开发路径：声明数据合同 → 注册薄入口/命令声明 → 在业务服务中登记意图、读取原结果、转换状态 → 按经过验证的事务与恢复配方验收。

| 本轮检查的未提交文档 | 定位及处理 |
| --- | --- |
| 本文、[业务包指南](../../integrations/business-packages.md)、[业务 API 索引](../../integrations/workline-business-api.md) | 本文升级为重构主设计，两个入口说明现有/拟新增边界 |
| [人工分拣参考](../../integrations/manual-picking-api.md) | 保留旧系统来源、15 项参考业务 operation 和迁移阻断项，不转成已注册能力 |
| [粗分机草案](../../integrations/coarse-sorting-api-draft.md) | 保留三项候选 WMS 接口、经过身份和设备附录等未决项，不把演示模型当正式合同 |
| [部署说明](../../deploy.md)及 .env.example | 属于部署/配置变更，本设计不修改，也不据此宣称已部署或已启用真实目标 |

当前代码核对基线为 a1eccce7bf35fafb82ca408a07ff834d31324e36（PR #28 合入 develop 后），正式业务 catalog 为空。未提交文档中的接口参考和隔离接缝不等于完整人工/粗分业务已交付。

### 1.1 已选方向与取舍

| 方案 | 收益及代价 | 决定 |
| --- | --- | --- |
| 只补文档和复制配方 | 平台变化少，但字符串、声明接线和重复错误仍在 | 作为第一阶段，不能单独完成目标 |
| 现有业务包 + 少量声明/模型调用扩展 + 事务配方和行为验收 | 统一开发体验，保留原协议与责任边界，可逐项迁移 | 本次采用 |
| 通用 execute、流程 DSL、Saga/插件基类或统一 Result 分发器 | 增加新概念，容易藏住身份、许可、补偿和事务边界 | 不采用 |

不新增业务流程引擎、全局业务表、通用业务 worker、第二个 Sender/投递重试器、动态插件发现或统一“完成”布尔值。不借此修改 HTTP 路径、wire 信封、UNKNOWN/HELD、固定投递窗口或 Transport 结果语义。真实业务可因自身状态存储增加迁移，但不属于这组声明 API 的实现范围。

## 2. 现有接口与拟新增接口

### 2.1 已实现，继续保留

| 接入 | 现有声明/接口 | 使用和责任 |
| --- | --- | --- |
| WMS 出站 | @request_operation(name, Endpoint, title=..., ttl=...) 装饰 RequestPayload；WmsRequests.send(session, operation, data, operation_id=...) | 调用方事务内校验、冻结、Outbox 入队，不提交、不直接发网 |
| WMS 入站 | @callback_operation(name, data=Payload, ...) + async handler(ctx, payload) | 接收器负责原收据/去重/响应，业务负责受理与待办 |
| ECS Event | @ecs_event(name, device_role=ROLE, data=Payload) + async handler(ctx, payload) | 按当前设备绑定与角色受理设备事实；正式处理器需注入 BindingResolver |
| ECS Command | CommandBinding + DeviceAnnex；EcsCommands.create(session, owner=..., call_key=..., device_code=..., task_type=..., timeout_ms=..., params=...) | 路由提供方解析地址及合同，平台冻结原命令、路由和投递义务 |
| ECS Result | POST /api/v1/callback/result；EcsCommands.get_command/get_by_call | 按原命令冻结的附录验证、保存证据和应用；具体业务安全消费 |
| Transport | TransportService.move_rack/rotate_rack/move_bins/exchange_bins、get_result、list_unclosed_predecessors | 新场景组合既有能力，通常不新增协议 operation 或适配器 |
| 业务装配 | definition、composition 的 ENTRY_MODULES/MODEL_MODULES/COMMAND_BINDINGS/ANNEXES/build_service、显式 catalog | API/worker 共用正式清单；正式入口与合同先校验，再加载 local 入口和资源/工厂 |
| 排障 | 三类注册表共享命名/来源规则；local 的 GET /api/v1/dev/integrations/registry | 展示当前进程已加载项，不能证明实际执行或物理完成 |

代码依据：[WMS 声明](../../../app/integrations/wms_request/operation.py)、[WMS 服务](../../../app/integrations/wms_request/service.py)、[ECS 合同](../../../app/integrations/ecs_command/contracts.py)、[ECS 服务](../../../app/integrations/ecs_command/service.py)、[Result 接收](../../../app/integrations/ecs_command/result.py)、[发现](../../../app/integrations/discovery.py)、[catalog 校验](../../../app/businesses/catalog.py)。

### 2.2 拟新增，未实施

| 名称 | 目标接口/输出 | 明确限制 |
| --- | --- | --- |
| WMS 模型调用 | WmsRequests.send(session, payload, operation_id=...) → 原 EnqueueResult | 从精确模型类型唯一定位已加载正式/允许作用域内注册项；旧字符串调用保留 |
| ECS 命令声明 | @ecs_command(task_type, device_role=..., annex_id=..., annex_version=..., result=..., error=..., timeout_min_ms=..., timeout_max_ms=...) 装饰参数 BaseModel | 单参数类对应单角色/动作声明；共享模型的多绑定场景保留手工方式；无全局业务注册表、数据库或网络副作用 |
| 命令声明编译 | compile_commands(*command_models) → tuple[CommandBinding, ...], dict[(annex_id, annex_version), DeviceAnnex] | 显式清单转换，使用现有合同对象和冲突校验，不扩展初始入口类型 |
| ECS 模型调用 | EcsCommands.create(session, owner=..., call_key=..., device_code=..., command=payload, timeout_ms=...) → 原 CommandView | 从声明取 task_type/参数；新建时还须校验实际角色及生效附录，不能只按 task_type 匹配 |
| 角色解析内部扩展 | RoleRoutes.resolve_declared(session, device_code, declaration) → 原 RouteSnapshot | 与现有 resolve 共用一次绑定解析和快照生成；校验角色/动作/附录/模型，保持 RouteSnapshot 字段及 authorize_frozen 不变 |

以上是目标调用形态，不是要求以一个含大量可选参数的函数实现。实现可用 typing.overload 描述两种调用，内部用明确的参数分派；不得另起一套入队/创建流程。

此阶段不新增 WMS 响应注册器、@ecs_result、业务 Result URL、通用消费表或结果引用包装器。WMS 业务响应模型与请求放在一起；响应读取、分类及原身份参数继续使用现有接口。

## 3. Event、Command/Result 与 Transport 的边界

Event 表达设备事实，根据角色和 event_type 选择受理函数；可否创建业务执行由 definition 的 initial_entries 明确声明。

Result 表达某条原命令的执行结果，根据 command_code 找原命令并核对设备，使用其冻结的 annex_id/version 验证。当前换线、换绑或合同升级不能改选原 Result 合同。结果接收与原业务消费分别拥有事务；有效原结果不自动授予当前执行许可。

因此统一的是声明和调用体验：Event 用模型 + 受理函数，Command 用参数 + Result/错误合同；Result 仍走现有通用接收器，业务从公开 DTO 消费。命令声明不注册“成功后回调函数”，不把下一步业务写入 HTTP Result 接收事务。

Transport 继续使用 transport.task.submit@v1 和已有两项回调。CLOSED 不等于成功，也不授权下一动作；历史 TARGET_PLACED 不覆盖更新已应用 UNKNOWN，pending/成员/版本规则继续遵循出站结果契约。

## 4. 目标目录和开发者职责

以仓库根目录为起点，下面是目标示意；不在本轮创建这些业务或源文件。省略 __init__.py，实际入口包和父包必须具备它。

~~~text
app/businesses/<business>/
├── definition.py                 # 业务、角色、位置槽、正式初始入口；纯声明
├── composition.py                # 固定清单、命令编译、可信服务工厂
├── service.py                    # 业务受理、登记意图、结果消费和状态转换
├── models.py                     # 业务执行/动作、持久待办和消费依据
├── recovery.py                   # 具体业务独立推进及待办恢复
├── request/
│   └── example_sort_route_v1.py   # 请求模型、operation 常量、业务响应模型
├── callback/
│   └── example_sort_task_issued_v1.py
├── ecs_event/
│   └── scan_completed.py
└── ecs_command/
    └── move_to.py                # 参数、Result、错误、角色与版本声明

app/integrations/
├── wms_request/                  # 增加模型调用，复用原注册及发送
├── ecs_command/
│   ├── declaration.py            # 新增声明和纯编译函数
│   ├── roles.py                  # 内部声明感知的角色校验
│   ├── service.py                # 增加模型调用，复用原创建
│   └── result.py                 # 原接收机制保留
└── ...                           # 其余现有内核

tests/businesses/<business>/      # 实际业务行为、受理、推进及恢复
~~~

不要求为不存在的能力建空目录；小业务可继续用单文件 request.py/callback.py/ecs_event.py。合同优先与入口相邻；真正共享后才抽 contracts.py，不先拆一组单行转发文件。

WMS/Event 继续遵循 registration_filename；新业务的命令文件推荐按 task_type 小写命名，如 MOVE_TO → move_to.py。命令编译不强制这条新命名约定，允许在既有 contracts.py 等文件的原参数模型上附加声明后直接迁移；仍检查原类来源、业务归属与 local 作用域。模板按推荐命名生成，不要求为迁声明先搬文件；历史手工 CommandBinding 的路径规则保持原状。

| 部位 | 普通开发人员写什么 | 平台或固定配方承接什么 |
| --- | --- | --- |
| 合同和角色 | 字段、业务含义、允许联合分支、版本及超时 | 严格校验、名称/来源诊断、一致性校验 |
| 薄入口 | 声明名称、Payload/角色，调用本业务受理方法 | 信封、原收据、技术去重与响应映射 |
| 业务服务 | 经过/任务关联、业务幂等、当前许可、状态和完成条件 | 现有能力注入、冻结、Outbox/唯一 Sender |
| 结果消费/恢复 | 完整直接依赖、状态/消费记录、待办发现和受控恢复策略 | 经过验证的独立事务、锁序、有界重试配方和测试助手 |

业务依然必须实现模型和恢复入口；下列 service 方法不是已经存在的通用 API。模板不能取代具体业务的锁范围、许可和完成合同。

## 5. 普通开发人员代码示例

所有 example.*、设备角色、字段和超时只用于说明目标形态。假定的合同未获供应商确认，不进入正式 catalog、迁移或生产 Base；实现阶段的示例仅放在隔离测试/文档，沿现有夹具清理规则。

### 5.1 definition.py：单据启动，扫码推进已有任务

~~~python
from app.businesses.definitions import BusinessDefinition, DeviceRole, InitialEntry

SCAN = DeviceRole(role_key="EXAMPLE_SORT_SCAN", display_name="扫码设备")
ARM = DeviceRole(role_key="EXAMPLE_SORT_ARM", display_name="搬运机械臂")
TASK_ISSUED = "example.sort.task_issued@v1"
SCAN_COMPLETED = "SCAN_COMPLETED"

DEFINITION = BusinessDefinition(
    code="example_sorting",
    display_name="示例分拣",
    device_roles=(SCAN, ARM),
    initial_entries=(InitialEntry(kind="wms_callback", name=TASK_ISSUED),),
)
~~~

definition 不导入 Pydantic、ORM、服务或实际配置。实际设备码、地址和位置保留在工作线绑定中；同一个角色对象供其他文件引用。本例不把扫码注册为初始入口；设备驱动业务须单独声明真实的初始 Event。

### 5.2 WMS 请求与响应：现有声明，拟新增模型调用

文件 request/example_sort_route_v1.py：

~~~python
from pydantic import BaseModel, ConfigDict
from app.integrations.wms_request import Endpoint, RequestPayload, request_operation

OPERATION = "example.sort.route@v1"


@request_operation(OPERATION, Endpoint.DECISIONS)
class ChooseRoute(RequestPayload):
    task_id: str
    bin_code: str


class RouteDecision(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    task_id: str
    bin_code: str
    target_code: str
~~~

本例只展示“已获得目标”的业务响应；实际 WAIT/NG/拒绝等必须按确认合同建完整模型，不从通用端点能力推导。service 方法中的新旧调用等价目标：

~~~python
# 当前接口：operation 和 data 分开传递。
message = await self.wms.send(
    session,
    OPERATION,
    {"task_id": action.task_id, "bin_code": action.bin_code},
    operation_id=action.wms_operation_id,
)

# 拟新增接口：替换上面的调用，不是再发送一次。
message = await self.wms.send(
    session,
    ChooseRoute(task_id=action.task_id, bin_code=action.bin_code),
    operation_id=action.wms_operation_id,
)
action.wms_message_id = message.message_id
~~~

action 已在本业务事务中稳定保存 operation_id 和请求输入；调用不提交、不发网。读取继续用原 message_id + OPERATION + operation_id 的 get_result。只有 FOUND、终局、无投递不确定且响应 VALID，并按该 operation 合同允许使用的 code，才能进一步严格解析 response_data、核对 task_id/bin_code 及业务许可。

RouteDecision.model_validate 不是“是否推进”的全部条件。PREPARE_ACCEPTED 继续等待计划；facts 的 RECORDED/DUPLICATE 只表示收妥；缺失、拒绝、非法或未知分别保存等待/失败/待核对原因。解析异常不能被转为成功。业务保存已消费的原身份/必要决定，与下一意图同事务提交。

### 5.3 WMS 通知：现有函数装饰器不变

文件 callback/example_sort_task_issued_v1.py：

~~~python
from app.businesses.runtime import require_runtime
from app.integrations.wms_callback import CallbackPayload, callback_operation
from ..definition import DEFINITION, TASK_ISSUED


class TaskIssued(CallbackPayload):
    task_id: str
    workline_code: str
    bin_code: str


@callback_operation(TASK_ISSUED, data=TaskIssued)
async def task_issued(ctx, payload: TaskIssued):
    service = require_runtime(ctx).services[DEFINITION.code]
    return await service.accept_task(ctx, payload)
~~~

accept_task 必须校验工作线业务、稳定身份与当前许可，并在 ctx.session 中保存受理/待办。Accepted/Rejected/Conflict/Duplicate 和保存点规则保持；业务 Duplicate/Conflict 在写业务行之前决定，不自行 commit/rollback，不产生事务外副作用。

### 5.4 ECS Event：现有函数装饰器不变

文件 ecs_event/scan_completed.py：

~~~python
from app.businesses.runtime import require_runtime
from app.integrations.ecs_callback import CallbackPayload, ecs_event
from ..definition import DEFINITION, SCAN, SCAN_COMPLETED


class ScanCompleted(CallbackPayload):
    task_id: str
    passage_id: str
    bin_code: str


@ecs_event(SCAN_COMPLETED, device_role=SCAN.role_key, data=ScanCompleted)
async def scan_completed(ctx, payload: ScanCompleted):
    service = require_runtime(ctx).services[DEFINITION.code]
    await service.accept_scan(ctx, payload)
~~~

此例假定双方已确认 passage_id 的稳定性。真实协议没有此字段时不能擅自要求设备增加；内容派生收据身份、物料码及接收时间不能单独证明本次业务经过。accept_scan 使用已注入绑定，保存与原执行的唯一关联及持久待办；业务矛盾按现有 ctx.note 语义处理，存储故障向外抛出以回滚并返回 503。

### 5.5 ECS Command/Result：拟新增声明，原 Result 接收

文件 ecs_command/move_to.py：

~~~python
from typing import Literal
from pydantic import BaseModel, ConfigDict
from app.integrations.ecs_command import ecs_command
from app.integrations.ecs_command.contracts import ErrorDetail
from ..definition import ARM


class MoveResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    bin_code: str
    target_code: str


class MoveError(ErrorDetail):
    code: Literal["BLOCKED", "DEVICE_FAULT"]


@ecs_command(
    "MOVE_TO",
    device_role=ARM.role_key,
    annex_id="example.sort.arm.move",
    annex_version="1",
    result=MoveResult,
    error=MoveError,
    timeout_min_ms=1,
    timeout_max_ms=60_000,
)
class MoveTo(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    bin_code: str
    target_code: str
~~~

示例假定 SUCCESS/FAILED 都回显这两个字段；如真实失败 data 可为空，必须使用适配该合同的严格模型/联合，不复制此假设。声明检查参数/结果为严格 BaseModel、禁止多余字段，错误模型继承 ErrorDetail，角色/动作及附录身份合法，超时为合法整数范围；校验失败不能留下半份声明。合同模型及版本一经用于命令不得原地改变语义。

业务服务先核对当前权威许可及绑定，再使用拟新增调用：

~~~python
from app.integrations.ecs_command import CommandOwner

command = await self.ecs.create(
    session,
    owner=CommandOwner(DEFINITION.code),
    call_key=action.ecs_call_key,
    device_code=action.device_code,
    command=MoveTo(bin_code=action.bin_code, target_code=action.target_code),
    timeout_ms=action.timeout_ms,
)
action.command_code = command.command_code
action.state = "WAITING_RESULT"
~~~

action 的 owner/call_key、设备、输入和执行依据稳定保存。timeout_ms 是动作执行超时，仍由具体业务明确提供；新命令的投递窗口保持 PROTOCOL_RETRY_WINDOW=300 秒，存量命令保留原期限。

无需 @ecs_result。原通用入口验证、持久化并提交后，业务独立推进用 get_by_call(session, owner=原 owner, call_key=原 call_key) 或 get_command 读取。消费必须同时满足：

1. 原 command_code/动作关联匹配，仍处在该动作允许消费的状态；
2. 已按工作线→设备→业务动作取锁，重读当前许可，并核对原冻结设备/版本与当前许可；不从 Status 或新建 READY 行获得许可；
3. 没有 hold_reason/needs_reconciliation，有可用已应用 result，成功/失败及 data/error 符合原版本合同；
4. 结果的物料、目标及完成范围与原动作匹配，不能只看 status 或 ack_received；
5. 状态转换、消费依据、必要结果及下一 Outbox 意图同事务提交。

一动作一命令一消费者时，本业务终态可作为消费标记；多命令/多消费者/成员版本则使用本业务明确的消费记录，不新增平台全局消费位。更高版本或冲突结果的处理保持原协议，不把原动作成功直接当整单结束。

### 5.6 composition.py：固定模板，显式编译

~~~python
from app.integrations.ecs_command import compile_commands
from .definition import DEFINITION
from .ecs_command.move_to import MoveTo

ENTRY_MODULES = (
    "app.businesses.example_sorting.request",
    "app.businesses.example_sorting.callback",
    "app.businesses.example_sorting.ecs_event",
)
MODEL_MODULES = ("app.businesses.example_sorting.models",)
COMMAND_BINDINGS, ANNEXES = compile_commands(MoveTo)


def build_service(capabilities):
    from app.core.database import session_factory
    from .service import SortingService

    return SortingService(capabilities, session_factory=session_factory)
~~~

编译器输出 CommandBinding.source 必须是原参数类，来源指向 move_to.py 的 MoveTo，不指向 declaration.py。此处没有第四种 ENTRY_MODULES 类型；ecs_command 由 composition 显式导入。新增命令在 compile_commands 清单列出，新增 request/callback/Event 在已声明入口下递归发现。

一个角色/task_type 只能有一份活动绑定。迁移期可在 composition 组合其他手工绑定及附录，但不能重复登记同键；编译器和现有全局校验都要报告冲突双方来源。历史附录只补解析能力，不生成活动绑定。

新建完整真实业务由维护者显式加入 catalog；没有合同/资源/恢复实现的示例不能为了演示登记。工厂无活动 session、无网络和作业状态。

## 6. 新接口的确定行为

### 6.1 WMS 模型反查与参数分派

- 旧调用包括原位置/关键字参数继续有效；新模型调用不能混入 operation/data。混合或缺失形态报 TypeError，不能猜测优先级。
- 使用 type(payload) 精确反查同一现有注册表，不依赖类名、模块顺序或用户可写的字符串属性。不新增平行注册表。
- 未注册类型报 UnknownOperation；同一个模型被既有注册方式用于多个 operation 时，旧调用继续支持，模型调用因歧义报 InvalidRequestPayload。未注册子类不能隐式继承发送权限。
- 不能只依赖 isinstance 或 model_validate(instance) 信任模型。对 model_construct、后续变异、嵌套模型/列表重新执行原合同强校验，再进入原序列化/冻结/入队路径。保留别名、默认值、显式 null 和合法 JSON 语义；不能用 exclude_none 等改写原字段。
- 新旧调用在同一 target 标签、相容端点配置、相同 operation/operation_id 和规范化输入下命中同一原消息；异输入沿原 DuplicateMessage 语义。同 target 重入保留原消息字节、时间戳与 TTL，不因换调用语法重造。
- **现有静态目标限制继续存在**：WmsRequests.send 每次从当前配置选 target，Outbox 去重键为 (target, operation, operation_id)。改 target 标签后再次 send 可能建立另一条消息；同标签改 base_url 也不等于冻结原地址。本设计不新增跨目标去重或 WMS 地址快照。存量动作按已保存的原 message_id/operation/operation_id 读取和恢复，不能把重调 send 选新目标当恢复；改变目标标签、地址或端点配置须另行核对存量义务，不属于纯调用语法迁移。

### 6.2 ECS 声明、模型调用与路由

- 声明元数据绑定精确原类、不可变；未声明子类不自动获得合同。一个参数类只能有一份新式命令声明，不能用重复装饰器覆盖角色/版本。
- **共享合同兼容限制**：现有多个角色/动作共用同一参数模型和同一 DeviceAnnex 是合法能力。本轮新声明只覆盖单参数类对应单角色/动作的场景；共享模型的多绑定继续保留手工 CommandBinding/DeviceAnnex 和旧 create，不要求模型化迁移。不能复制/继承参数类再用同一附录键编译（模型身份不同会冲突），也不能为迁语法人为新增合同版本。即使其中一个模型已有新式声明，共享绑定的其他调用仍可使用旧形态；是否对共享合同扩展声明对象须另行设计。
- **模型迁移资格**：单绑定也不代表可直接迁入。原参数、结果模型必须已经满足新声明的严格类型和 extra="forbid" 要求，错误模型及超时范围也符合新声明约束；还须核对嵌套模型、默认值、别名、校验器和序列化行为与原合同等价。当前角色接入示例复用的 CallbackPayload 为 strict=True、extra="ignore"，因此不满足直接迁移资格，应继续手工声明和旧调用。不能为通过新装饰器检查而原地修改同一附录版本的模型配置，也不能复制模型、换附录键绕过等价验证；收紧合同属于独立升级，须确认新版本并保留原 Result 解析和业务消费模型。
- compile_commands 每次按显式清单纯转换，无全局累加；重复角色/task_type、同附录键不同合同、非法字段/模型或 local-only 来源进入正式清单必须失败。历史手工声明仍支持，新增严格规则不追溯收紧其旧调用。
- create 的两种形态互斥：旧 task_type+params，或新 command；owner/call_key/device_code/timeout_ms 均保留。混用报 TypeError；未声明/非法模型沿 ValueError 失败；现有 AppError、数据库错误和事务行为保持。
- 模型路径重验全部参数，序列化成与旧 params 同形的 JSON 输入，不把类名、声明元数据或合同版本加入 wire/input_raw，也不改变去重键。
- **已有调用优先**：先按 owner/call_key 查原行并比较原规范输入；同输入返回原 CommandView，不先查当前绑定、重算期限或另建命令。当前声明不能重新解释原结果；跨版本消费须用原动作保存的版本和保留的原模型。新模型已不能表达旧输入时，用旧接口/公开读接口恢复原动作，不能换 call_key。
- **仅新建时进行声明感知解析**：RoleRoutes.resolve_declared 在同一次可信绑定解析中核对精确设备、声明角色、task_type、活动附录 id/version 及参数/结果/错误模型一致，然后生成原 RouteSnapshot。仅核对 task_type/附录不足以防止两个角色共用同合同却串用声明。
- 不在通用 create 中另查设备、工作线或 Status。新式模型调用要求提供上述声明感知能力；普通 DeviceRouteProvider/通用 dev 路由缺少能力时拒绝新建，不静默降级。现有旧调用及端口无需新增必选方法；local 的正式角色组合装配须显式委托该能力。
- RouteSnapshot 当前不含 device_role；本设计不增加冻结字段，以免破坏 authorize_frozen 的精确字段校验。实际角色只在可信绑定解析中核对，发送门禁、原 route_id/version、origin 及迟到结果规则不变。

原合法结果/收据/证据不因开发接口变化而删除、重放或改写；UNKNOWN/HELD 不自动 resume、换号或换址。所有校验失败不得残留新命令/Outbox，不得提交调用方业务事务。

### 6.3 启动、来源和排障

保持既有正式入口完整加载 → 合同交叉校验 → local 入口 → 资源与工厂装配的顺序，API/worker 共用 catalog。definition 仍纯声明；受理上下文由控制器注入 BusinessRuntime，业务不得在请求中选择动态 import/工厂。

命令装饰器在 composition 导入期只保存声明；正式编译须核对原来源和 local 作用域，不能以“没有注册表”为由绕过现有防误标保护。新旧声明统一出现在既有 command_contract 目录项，不重复列项，不扩大 local HTTP 目录的环境或权限。

可独立运行的静态合同检查是阶段性工具目标，复用同一发现/编译/校验函数，使用新进程和明确环境；不装配资源/工厂，不连接 DB/Redis/WMS/ECS，不调用业务 handler，不回放未知消息。它不能证明数据库迁移、地址可达、运行时绑定或执行许可。未加载源码与当前加载项必须区分。

## 7. 事务、消费与恢复配方

声明 API 不接管业务事务。模板和测试助手降低重复劳动，业务仍显式拥有受理、推进和恢复。

| 边界 | 必须保留的规则 |
| --- | --- |
| 受理 A | 使用 ctx.session；原收据、业务受理和持久待办同事务。不能换隔离、另开推进写事务、commit/rollback 或直接发网络。只依赖本次输入和当前许可的初始意图可按业务合同登记 |
| 独立推进 B | 提交后通知只携带原 execution_id，允许丢/重；持久待办扫描调用本业务同一推进入口。新 session 首条 SQL 前 REPEATABLE READ；按工作线 ID→设备 ID→业务动作固定顺序取锁并重读许可 |
| 读组 | 固定完整直接前序和原身份/caller/cycle/资源范围；100 条一批，分批/跨轮/混合查询仍处在同一快照。READ COMMITTED 不是统一快照；快照不替代物理锁或派发授权 |
| 写组 | 业务进度、消费原身份/准确版本/成员范围/必要决定、下一 Outbox 意图原子提交；禁止业务锁内 settle/hold 原框架任务形成反向锁序 |
| 恢复 | 40001 关闭失败 session，用新 session 重读完整组并重试整个事务；重试上限/耗尽策略由具体业务计划明确。耗尽保留待办和原因；其他存储故障不提交部分消费，提交结果未知时按原动作核对 |
| 保留 | 沿既有保留期限；业务在窗口内保存所需决定和消费依据。NOT_FOUND/UNAVAILABLE 不等于成功或可重发；UNKNOWN 的成员投影仍可随合法版本变化 |

第一步提供验证过的业务配方，不新增通用 transaction runner/worker。两种实际业务验证后，只提取责任稳定且重复的机械性代码；锁哪些业务行、允许什么动作及何时完成仍由本业务决定。

## 8. 兼容、迁移与回退

### 8.1 兼容矩阵

| 对象 | 兼容要求 |
| --- | --- |
| WMS 装饰器、字符串调用、回调/Event 函数 | 保留原签名/注册身份/异常/响应；新增模型调用是可选入口，无强制移除期 |
| 手工 CommandBinding/DeviceAnnex、旧 create | 继续可用；编译输出相同类型，不能同时重复启用同一个角色/动作 |
| wire/存储 | HTTP 路径、operation/task_type、身份、JSON 规范化、冻结路由字段、投递窗口及保留策略不变；这组 API 不新增表/迁移 |
| 结果/证据 | 保留原 command_code/operation_id/Transport 身份及全部收据、Evidence、结果、待办。结果读取仍是原公开 DTO |
| 发现/目录 | 原 integrations/<business> 与 businesses/<business> 入口继续支持；不新增 ECS command 自动发现树或 Result 注册器 |
| local 调试 | 通用 v3 dev 合同和权限保持，不拿其宽松参数替代正式严格合同；不会把声明模型接入变成生产发送开关 |

本次不重新引入已移除的类式 WMS 注册接口；“旧接口”指本文件基线中仍受支持的接口。

### 8.2 按业务逐项迁移

1. **先建立等价基线。** 盘点正式/本地声明、原 operation/角色/附录/模型/超时及消费方；补公开行为用例，不改合同内容。
2. **部署兼容平台能力。** 先上线支持新旧声明/调用的版本，旧业务继续原路径。新语法调用方不能先于支持它的 API/worker 发布。
3. **WMS 逐调用迁移。** 保持同一注册模型、名称、端点、target 标签及配置、TTL、原业务身份及保存字段，只替换调用形式。同模型多 operation 或自定义序列化未证等价时保留字符串调用，不自动批量改写。已登记动作优先按保存的原 message_id 读取；不能为迁语法再次选当前 target 入队。
4. **命令先迁声明。** 先检查是否共享模型/附录，再按第 6.2 节检查原参数/结果模型的 strict/extra、错误模型、超时及校验/序列化等价性。共享或不满足新约束的合同保持手工声明和旧 create，不为迁移收紧原模型、复制模型或人为升级附录。只有单绑定且通过资格检查的场景，才在原参数模型上加新声明，复用原结果/错误模型、角色、附录 id/version/范围；替换该项手工绑定/附录生成，保持其他声明。不要同时重命名模型、搬目录、改变字段或增加版本。公共导入路径可用原对象重导出保留，不复制第二个模型类。
5. **再迁新动作创建。** 验证声明感知路由后替换 create 调用；存量待办仍持有原 owner/call_key/command_code、输入和版本，按原动作恢复。已有调用跨新旧语法在输入等价时返同一原行，不重发。
6. **合同升级单独进行。** V2 新动作绑定 V2；可信 startup 同时保留 V1 附录用于原 Result，业务保留 V1 消费模型。一个活动角色/动作只选一版，历史附录不扩大发送权限。
7. **目录整理最后进行。** 独立验证 filename/source/local-only 和公开导入兼容；真实业务完整并验收后才加入正式 catalog。旧调用的清理需要另行决定，不作为本次完成条件。

同一 annex_id/version 不能用不同模型对象/语义覆盖。merge_annexes 的冲突语义保持：迁声明时尽量复用原模型对象；若存在同键不一致，启动应失败，不能为了“兼容”覆盖任一方。

### 8.3 回退与混合版本

- 设计的声明 API 不改变持久格式，回退无需改写业务行、原消息或冻结数据。
- 平台和使用新语法的业务代码须一起回退到相容版本；不能让旧进程 import 新接口。回退到尚未迁调用但仍支持相同 wire/附录的版本，可继续处理原消息。
- API/worker 必须保留一致活动绑定和历史附录；混合版本期间不能顺便升级/移除合同。若候选旧版缺少仍可能收到 Result 的原附录，不能直接回退，应保留支持它的兼容代码或暂停相关创建并另作计划。
- 不批量改 owner/call_key、operation_id、目标、截止时间、状态或消费进度；保留 UNKNOWN/HELD 和原证据。无新 Schema 不等于任意旧版本都可回退。

## 9. 分步实施顺序

以下是后续实施顺序及完成门槛，不是本轮执行代码的授权；代码任务须另写计划。阶段 1–4 可在没有正式业务包时用隔离夹具完成，不能把演示包加入正式 catalog。

| 阶段 | 范围/交付 | 进入下一步的证据 |
| --- | --- | --- |
| 0：文档与决策（本轮） | 本设计、现有/新增矩阵、示例、兼容与验收、索引 | 文档链接/示例语法可检查；未实施标记明确；设计评审完成后才启动代码 |
| 1：兼容基线与接入配方 | 公开行为回归、最短接入路线/流程卡、受理和独立推进配方；独立合同检查复用现有校验 | 当前入口、两种声明来源、冷启动/local 隔离和原身份行为有基线；不引入通用运行框架 |
| 2：ECS 声明与编译 | declaration.py、显式 compile_commands、来源/严格性/冲突校验、包导出和目录元数据；仍用旧 create | 编译产物与原合同等价，无新增全局 registry，无正式示例，catalog/发现顺序不变 |
| 3：WMS 模型调用 | 精确模型反查和参数分派，共用原发送路径；保留旧签名 | 新旧调用等价、歧义/未注册/构造绕过被拒、相同身份重入/回滚/TTL/目标证据通过 |
| 4：ECS 模型调用 | 参数分派、声明感知 RoleRoutes 及 local 正式角色委托，共用原创建路径 | 错角色即使同动作/同附录也拒绝；缺能力不降级；既有调用优先、发送门禁和迟到结果不变 |
| 5：首个真实最小闭环 | 合同齐备切片的真实模型/受理/独立推进/持久待办恢复，按新体验接入 | 两线、重复/乱序/崩溃恢复、消费与下一意图原子；独立 PostgreSQL 证据；本方闭环和供应商验收分别记录 |
| 6：另一触发方式及按证据提取 | 用单据/设备另一种方式验证，再决定提取稳定重复或补新建工具 | 真实开发者上手指标有记录，业务差异仍清楚；无第二 Sender/通用 execute，旧接口保持兼容 |

阶段 5 按合同成熟度选切片，不预定“先粗分”。人工 SCAN1 Transport 证据应用口、Return 接纳交接、资源约束/正式设备附录，以及粗分经过身份、GRN/储位/机器人/NG/失败完成规则，分别见两份业务参考，不由本设计自动解决。

## 10. 验收标准

以下均为待实施验收，不表示本轮已执行或已通过。以公开入口/服务和可观察行为断言，避免只快照内部产物。

| 编号/层级 | 必须验证的行为 | 证据方式 |
| --- | --- | --- |
| A1 合同/来源 | 装饰器返回原类；严格字段/嵌套验证；非法声明/命名/重复键无半注册；未声明子类不能发送 | 声明/编译公开入口测试；错误含原文件/符号及修复方向 |
| A2 发现/隔离 | 三类正式入口完整加载后校验，任何错误阻止资源/工厂装配；local 不污染正式；API/worker 同清单 | 新进程冷启动及现有 registration bootstrap/discovery 测试扩展 |
| A3 WMS 等价 | 同 target/相容端点配置下字符串与模型形态同输入/身份同消息，默认值/别名/null/嵌套/序列化等价；异输入冲突；未注册/歧义/混用失败；存量业务不因配置变化重调 send | WmsRequests.send/get_result 公开服务；固定时钟下比较冻结信封，同 target 重入比较原 message_id/字节/期限；配置变化时业务仍读取原消息 |
| A4 ECS 等价 | 只有单绑定且满足原模型 strict/extra 等迁移资格的合同才逐项替换；共享模型或不满足新约束的原合同继续手工/旧 create 有效，不修改原模型或附录语义；新旧 create 同原调用；输入漂移/非法 timeout 拒绝，业务回滚不残留命令/Outbox | EcsCommands.create/get_by_call、原请求/命令身份及事务回滚证据；覆盖两角色共享合同，以及 CallbackPayload 的 extra="ignore" 参数/结果：旧调用和含原合同允许扩展字段的迟到 Result 仍有效，新声明明确拒绝不合资格模型 |
| A5 角色门禁 | 同 task_type、同附录但不同角色仍不得串用新声明；未启用合同/错误模型/不支持声明的路由拒绝；local 正式角色组合保持一个 Sender | 注入绑定/路由，公开 create 和 local 组合装配测试；拒绝时无新命令 |
| A6 历史恢复 | V1 命令在新配置/V2 发布、重启、换绑/撤销后按原合同接收合法迟到 Result；重复/冲突/先于 ACK 保持协议；历史合同不授予新发送权限 | 原 Result HTTP/服务、get_by_call、发送门禁与证据；保留原 bytes、origin、身份和 expires_at |
| A7 原子受理/消费 | 原收据+受理+待办一起提交/回滚；消费与下一意图原子；重复受理/唤醒不多发；业务写失败后仍消费原结果 | 真实独立 PostgreSQL，公共入站入口与具体业务推进入口故障注入 |
| A8 快照/恢复 | 跨轮/分批/混合读不拼版本；更新 UNKNOWN 不被旧里程碑覆盖；40001 新 session 整组重读；耗尽保留待办；丢通知/重启可恢复 | 双连接受控并发、实际隔离级别及原版本/成员/下一意图断言，SQLite 不替代 |
| A9 完成边界 | ACK、DONE、RECORDED、PREPARE_ACCEPTED、CLOSED、旧成员里程碑均不能单独使业务成功；缺证据/清理不重新发送 | 业务场景用例覆盖等待/拒绝/失败/待核对，不仅覆盖正常链 |
| A10 回退/上手 | 旧调用继续可用、持久格式/快照字段不变；候选回退版本保留所需合同；开发者可完成切片且不改协议内核/直读框架表 | 相容版本恢复演练、逐项迁移行为用例、实际开发过程记录 |

优先扩展现有用例：[WMS 服务](../../../tests/integrations/wms_request/test_service.py)、[注册冷启动](../../../tests/integrations/test_registration_bootstrap.py)、[来源与发现](../../../tests/integrations/test_registration_discovery.py)、[角色合同门禁](../../../tests/integrations/ecs_command/test_role_contract_guards.py)、[重启](../../../tests/integrations/ecs_command/test_restart.py)、[业务结果消费](../../../tests/businesses/test_result_consumption.py)。这些用例当前存在，不代表已经覆盖本设计的新 API。

实现时运行变更对应的单元/接缝套件以及 Ruff；事务、锁、快照与重试必须在显式 TEST_DATABASE_URL/TEST_REDIS_URL 指向的可丢弃环境验证。未配置则按现有规则跳过并报告，配置了却连接失败必须失败。外部使用 fake/loopback；真实供应商、真实设备和现场验收另列，不以 Mock/测试通过代替。

开发体验记录首次完成时间、阅读内核文件数、跨层修改数、求助次数及误注册/错误消费位置。硬性结果是新增业务接入无需修改控制器/Sender/可靠层、无需直读框架表，原身份与状态可定位；未实测前不承诺时间或代码减少百分比。

## 11. 责任与后续待定项

| 决定 | 当前状态/责任 |
| --- | --- |
| 统一声明体验、保留三类注册表和现有 Result/Transport 机制 | 本设计确定，进入后续代码评审 |
| ecs_command / compile_commands / 模型 send/create 及 resolve_declared 边界 | 本设计给出目标行为；内部存储、重载实现和测试组织由实施者按最小改动决定 |
| 独立合同检查命令/新建脚手架 | 检查复用边界已确定；具体命令形态由阶段 1 决定，脚手架等模板稳定及实际重复出现后再做 |
| 首个真实切片、完整合同/超时/状态/消费版本及恢复策略 | 由业务及 WMS/ECS 责任方关闭相应阻断项，在该业务实施计划中确定；不阻止阶段 1–4 的隔离机制验证 |
| 通用事务 runner、Result 处理器、补偿引擎 | 不在范围，不能在实现时顺手加入 |
| 正式业务发布与现场启用 | 完整业务与供应商/现场独立验收后另行安排；本轮不改变正式清单和部署状态 |

继续遵循[已合并业务基础设计](2026-10-27-workline-business-packages-design.md)和[基础实施计划](../plans/2026-10-27-workline-business-foundation.md)。本文细化开发接口与迁移方式，不削弱原事务、启动校验、资源许可和现场完成边界。
