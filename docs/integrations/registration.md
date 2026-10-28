# 新增接入与注册排障

新业务先按[快速开始](business-development-quickstart.md)确认合同与文件顺序；
处理器的受理、独立推进与恢复用[事务配方](business-transaction-recipes.md)，本页只说明现行注册规则。

三类接入都遵循：选目录 → 新建文件 → 用装饰器声明名称与 Payload → 接收入口再写处理函数。
WMS 接入及已装配解析器后的新增 ECS 处理器无需修改控制器或 `main.py`。ECS 首次接入需由应用注入运行时绑定解析器；角色静态声明不查设备或工作线。WMS 请求是发送数据声明，没有接收处理函数。

[通用 ECS Status](ecs-status.md) 是内置查询能力，可独立调用、不落库，纳入同一接入目录及来源追踪，**不属于第四类业务注册表**，无需添加装饰器或逐 ECS/设备声明。
正式查询接口 `GET /api/v1/ecs/status` 在所有环境可用，要求登录和 `ecs:status:query`；只有这里的接入目录仍限local挂载，二者不能混为一谈。

| 场景 | 目录 | 声明 | 数据约束 |
|---|---|---|---|
| ECS → 本项目 | `app/integrations/<业务>/ecs_event/` | `@ecs_event(name, device_role=ROLE, data=Model)` + async 函数 | `CallbackPayload` 严格类型、忽略冗余字段 |
| WMS → 本项目 | `app/integrations/<业务>/callback/` | `@callback_operation(name, data=Model)` + async 函数 | `CallbackPayload` 严格类型、忽略冗余字段 |
| 本项目 → WMS | `app/integrations/<业务>/request/` | `@request_operation(name, Endpoint)` 装饰模型 | `RequestPayload` 严格类型、禁止冗余字段 |

## 文件名只有一条规则

一个注册项一个文件，文件名为 `name.lower().replace('.', '_').replace('@', '_') + '.py'`。
`SCAN_COMPLETED` → `scan_completed.py`；`transport.task.resulted@v1` → `transport_task_resulted_v1.py`。
名称仍按协议校验：ECS 用大写字母开头的大写字母、数字、下划线；WMS 用小写点分名称加正整数版本，
例如 `example.done@v1`，最长 256 字符。ECS 的注册身份为 `(device_role, event_type)`，WMS 仍为完整名称；文件名不是身份。不同角色可用 `ecs_event/scan1/scan_completed.py` 与 `ecs_event/scan4/scan_completed.py`，复用递归发现。

目录需有 `__init__.py`，业务根目录也需有 `__init__.py`。原来的单文件 `ecs_event.py`、
`callback.py`、`request.py` 及对应入口包的 `__init__.py` 可用，不受逐项文件名约束；
入口模块不受逐项文件名约束；业务优先在目录中逐项建文件。
包内用于共享逻辑的文件可以自由命名，只要它本身不声明注册项。

## ECS：声明数据与处理函数

文件：`app/integrations/example/ecs_event/scan1/scan_completed.py`。

```python
from app.integrations.ecs_callback import CallbackPayload, EcsEventContext, ecs_event


SCAN1 = "SCAN1"


class ScanCompleted(CallbackPayload):
    barcode: str


@ecs_event("SCAN_COMPLETED", device_role=SCAN1, data=ScanCompleted)
async def scan_completed(ctx: EcsEventContext, payload: ScanCompleted) -> None:
    ctx.note("SCAN_OBSERVED", barcode=payload.barcode)
```

处理函数只能写当前事务；返回 None 后由平台提交并 ACK。业务矛盾用 `ctx.note` 表达；普通异常会整体
回滚并返回 503。不直接发 HTTP 或设备指令，外发意图同事务写 outbox。角色与 `data=` 必须明确声明；无需附加字段时使用 `CallbackPayload`。
运行时通过 `include_ecs_callback(..., resolve_binding=...)` 注入绑定解析器，正式处理器缺解析器时启动失败。稳定未绑定或组合键未注册的新事件按原协议 ACK 并留诊断，无自动回放；已有 receipt 先于绑定解析。处理器通过 `ctx.binding` 读取本线上下文。完整的命令角色映射、配置停发和独立 Mock 接入见 [角色接入](ecs-roles.md)。先部署接收器／处理器及绑定，再启用对端事件。

## WMS 回调：相同的函数声明，保留业务结果

文件：`app/integrations/example/callback/example_done_v1.py`。

```python
from app.integrations.wms_callback import (
    CallbackContext,
    CallbackPayload,
    callback_operation,
)


class Done(CallbackPayload):
    task_id: str


@callback_operation("example.done@v1", data=Done, echo_keys=("task_id",))
async def done(ctx: CallbackContext, payload: Done):
    return {"task_id": payload.task_id}
```

处理函数必须为 `async def handler(ctx, payload)`。可选 `title=` 用于 Swagger 标题，`echo_keys=`
沿用原有安全回显规则。`None` 或 dict 转为 Accepted；Accepted、Duplicate、Conflict、Rejected 可 return
或 raise。Rejected 回滚处理器保存点内写入，拒绝收据仍持久化；普通异常整体回滚并返回 503。
Duplicate、Conflict 必须在写业务行之前决定。不自行 commit/rollback，不产生事务外副作用。

注册项直接保存处理函数及模型，统一完成 `parse/handle`；信封、规范化、身份、收据、去重、保存点和
响应映射不变。Transport 的加锁、结算、诊断及结果应用由共享函数处理。

## WMS 请求：直接注册数据模型

文件：`app/integrations/example/request/example_report_v1.py`。

```python
from pydantic import ConfigDict

from app.integrations.wms_request import Endpoint, RequestPayload, request_operation


@request_operation("example.report@v1", Endpoint.FACTS, title="示例事实上报")
class Report(RequestPayload):
    task_id: str
    model_config = ConfigDict(json_schema_extra={"examples": [{"task_id": "T-1"}]})
```

字段定义在模型本身，无需外层 RequestOperation 和内嵌 Payload。示例按严格 JSON 模式在导入时校验。
可选 `ttl=timedelta(...)`；默认 None，事实不自动过期。标题、TTL 与模型保存在同一个注册项中。

业务调用可用原精确注册类的位置参数模型形式：

```python
reference = await wms_requests.send(session, Report(task_id="T-1"), operation_id=saved_operation_id)
```

这是业务事务片段，`wms_requests`、`session`、原 UUIDv7 `saved_operation_id` 由调用方提供。
旧 `send(session, "example.report@v1", data, operation_id=...)` 及原 session/operation/data 关键字形式保留；
没有 `payload=` 参数，模型与 data 混用为 TypeError。精确原类型须只匹配一个已加载 operation，
未注册/未声明子类拒绝，同模型多 operation 歧义拒绝；不以类名或可写属性猜 operation。
与业务记录共用调用方事务；调用方保存并复用 operation_id、operation 和返回的 message_id。平台冻结信封、可靠入队，不提交、不直接
发网。同身份重入、TTL、重试及 ACK 分类不变；用 `WmsRequests.get_result` 按原身份读取可信终局响应，
读模型、消费事务与保留边界见[出站结果契约](outbound-results.md)。真实 operation 先确认合同。

模型路径先按原字段上下文重验实际值，再保留原 JSON 序列化；别名、默认值、自定义校验/序列化行为须逐合同
证明等价。字段级 frozen 的 Pydantic dataclass、无法解析/不唯一的同运行时类泛型上下文明确拒绝，继续用旧
operation/data。类级 frozen、唯一上下文和可由直接字段区分的泛型合同可用，不修改原类或 schema。
同 target 去重不跨 target；配置变化后存量动作按保存的原引用读结果，不重调 send 选新目标恢复。

ECS 命令的 `@ecs_command` 和显式 `compile_commands` 见[命令声明](ecs-command.md#模型声明与显式编译)。
它们不构成第四类业务注册表，也不增加命令自动发现树或 `@ecs_result`；原三类入口、来源、文件名规则、
API/worker 加载范围、CLI 检查范围及 local HTTP 目录权限保持原样。验收与迁移资格见
[阶段 2–4 说明](business-development-phase2-4-acceptance.md)。

## 在一个地方检查全部注册项

应用启动前可用独立静态检查命令；它与下面的 HTTP local 目录分别使用，HTTP 行为和权限不变：

```sh
rtk uv run --frozen --no-sync python -m scripts.check_integration_contracts --environment production
rtk uv run --frozen --no-sync python -m scripts.check_integration_contracts --environment local
```

在项目根目录、Python 3.13+ 和锁定依赖下每次运行新进程。`--environment` 必填且仅接受这两个值，
不使用继承的 `ENVIRONMENT`；production 是正式声明范围，其他非 local 部署也用它，不连接生产。
无需数据库/Redis/JWT 配置或 `.env`。顺序是全部正式入口 → 原业务合同交叉校验 → 仅 local 的 dev 入口；
正式缺项不能由 local 声明修补。命令不装配资源/路由，不执行 handler、factory、建表或回放。

成功退出 `0` 并输出一个 JSON；声明/导入错误退出 `1`，只在 stderr 输出固定阶段、错误类型、安全
源码位置及固定修复指引，不回显原异常、配置或 Payload，不输出部分成功清单。参数错误退出 `2`；
`--help` 退出 `0`。参数处理先于应用导入。

JSON 包含 `environment`、排序的业务 code 数组 `businesses`、三类原来源元数据 `entries`、
原 `CommandBinding/DeviceAnnex` 校验结果 `command_contracts` 和 `scope.checked/excluded`。
仅检查现有发现器已加载的声明，正式业务来自显式 catalog；当前业务为空仍检查 Transport 正式回调。
不扫描任意源码，不收紧合法旧附录，不验证未来新声明的迁移资格。local 只列原 dev 三类入口，
通用 v3 调试命令、Status runtime、模型资源，以及路由、迁移、网络、许可和物理/业务完成均不在范围。
字段与排序约定见[快速开始](business-development-quickstart.md#启动应用前检查声明)。

仅 `ENVIRONMENT=local`，登录 Swagger 后打开 `GET /api/v1/dev/integrations/registry`。
每项包含 `kind`、`entry_type`、`name`、`device_role`、`module`、`source_file`、`symbol`、`local_only`、`endpoint`、`payload_schema`；命令合同另含 `response_schema`。三类业务项为 `business_registration`，角色命令合同为 `command_contract`，目录读取同一份可信声明，不新增业务注册器。
来源是原函数/原模型/原类，而不是适配器；清单按类型、角色和名称排序；未装配额外能力且无注册项时返回空列表。目录不依赖 Status，不做外部查询。
`local_only` 来自发现入口的加载作用域：`dev_ecs_event`、`dev_callback`、通用 `dev.py` 仅本地加载。
三类可运行的本地示例分别见：

- `app/integrations/ecs_callback/dev_ecs_event.py`：EXAMPLE_SCAN，显式 local 设备编码为 `LOCAL_EXAMPLE_SCAN`；
- `app/integrations/wms_callback/dev_callback.py`：example.echo@v1；
- `app/integrations/wms_request/dev.py`：example.fact.demo@v1、example.decision.demo@v1。

清单只表示当前进程实际加载的注册项，不证明消息已执行、对端接纳或物理完成。非 local 不挂载清单，
也不会由发现器加载这些示例；在同一 Python 进程切换环境不会撤销已经 import 的模块，请重新启动进程。

## 没生效时按此排查

1. 启动失败：读异常中的双方来源或目标文件名。重名时删除重复声明或更正名称；格式错误按示例修正；
   文件名错误按报错给出的名字重命名。非法函数必须改成异步双参数函数，data 必须指定 Pydantic 模型。
2. 启动成功但清单缺项：检查是否放进对应入口目录、目录是否有 `__init__.py`，以及是否误放进 local-only
   入口。普通模块内部的依赖导入错误会阻止启动，不会被静默跳过。
3. 清单存在但未执行：核对收到的完整名称、请求数据、历史收据和诊断；同身份重报可能直接重放首次响应，
   不会再次运行函数。修复处理器不会改写已持久化的历史结果。

清单无法在应用启动失败时提供 HTTP 服务，导入期错误必须靠启动日志自救。它不扫描未加载的源码，
也不执行未知事件回放。完整应用启动统一加载三类正式入口并交叉校验业务合同，然后才加载 local
入口和装配运行时；独立请求注册表读取仍可触发正式发现，发现作用域退出或异常后都会还原。

## 从旧类式声明迁移

WMS 回调只接受带 `data=` 的异步函数；请求只接受被装饰的 RequestPayload 模型，旧类注册与动态适配
已移除。回调把内嵌 Payload 提到模块级，把 execute(ctx, payload) 的业务体移入处理函数，title 和
echo_keys 写在装饰器上；原来共享的业务流程改用普通共享函数。请求把内嵌 Payload 作为被装饰模型，
title、ttl 写在装饰器上。ECS 事件还须补充业务角色常量与显式 Payload，应用首次装配运行时解析器；wire 的 event_type、字段与原收据身份保持。WMS operation 及执行顺序保持。

仓库内 Transport、开发示例、调用方和测试已迁移；仓库外接入代码须按此更新。历史设计文档中的类式
例子只用于解释当时的设计，当前接入以本指南为准。

设备管理完整应用通过app.state.integration_capabilities装配内置ECS Status能力，条目为kind=ecs_status、entry_type=builtin_capability，包含method/path/query_schema/response_schema及代码来源；运行时结果来源见[通用Status](ecs-status.md)。目录本身不导入Status，也不访问ECS；不装配该能力的独立角色应用可返回空目录，原三类业务注册身份不变。


## 工作线业务包入口

新增领域业务使用 `app/businesses/<业务>/`，固定 `ENTRY_MODULES` 由显式 composition/catalog 提供；
原 `app/integrations/<业务>/` 入口仍支持。两者复用同一三类注册表、文件名/来源保护及唯一 Sender，
不另建插件注册器。业务 definition 保持纯声明，服务和模型留在子包；入口通过控制器注入的
`BusinessRuntime` 调用具体服务。全部正式合同在任何 local/资源/factory 装配前交叉校验。
模型通过 composition 的 `MODEL_MODULES` 统一加载，不在应用启动建表。

最小接入、独立推进事务和两线验收见[业务包开发指南](business-packages.md)。正式业务清单当前为空，
测试接缝不代表完整真实业务交付；未确认协议不登记可发送 operation 或空占位业务。
