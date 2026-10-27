# 业务子包与工作线对接设计

日期：2026-10-27（Asia/Shanghai）  
状态：设计草案，供审阅；不是实现完成报告。  
代码基线：已合并 develop `5e3383061532bd123d57e48c2451b4ac35d65959`。  
本轮范围：只编写设计；不开发产品代码、不执行迁移、不提交或推送。

## 1. 先理解我们要做什么

新建一条工作线时，管理员选择“人工分拣”或“粗分作业”。系统据此知道这条线接受什么业务、需要哪些设备角色，以及应该调用哪个业务子包。管理员再把本线的实际设备和位置绑定到这些角色。

业务子包拥有完整业务：协议合同、触发受理、编排、业务状态、结果消费和完成判断。已有 ECS、Transport、WMS、Outbox 提供通信与执行机制。工作线提供现场配置，应用启动负责把这些部分接起来。

本方案采用 Python 集中声明、显式业务清单和简单工厂。没有插件安装、热加载、通用流程引擎或 Saga 引擎；也不要求所有业务继承同一个基类、实现同一个 execute 方法。

普通开发人员新增业务的主要工作是：建立子包，声明角色和严格合同，编写自己的流程服务，在业务清单加入一项，补齐测试。本文后半部分给出可模仿的步骤。

## 2. 当前已有与本方案新增

| 能力 | develop 现状 | 本方案如何处理 |
| --- | --- | --- |
| 工作线、设备管理 | 已有管理 API、版本、审计、设备归属 | 直接复用 |
| 设备／位置绑定 | WorkLine 已有 device_bindings、position_bindings | 直接复用，不另建角色绑定表 |
| 角色解析 | resolve_binding、resolve_role 已有锁及版本复核 | 直接复用，增加业务适用性校验 |
| ECS 事件 | 按角色＋事件注册；处理器获得 DeviceBinding | 复用；需要接入业务依赖上下文 |
| ECS 命令 | CommandBinding、DeviceAnnex、RoleRoutes、DeviceRoutes | 复用；由业务提供正式合同 |
| WMS 请求／回调 | 已有注册、收据与可靠发送 | 复用；具体 operation 由业务确认 |
| 结果消费 | ECS 有原命令查询；WMS 请求及 Transport 尚缺正式业务消费接口 | 前置 Task 0 定义并实现最小读取契约，见 §10.2–10.6；业务编排不得绕过平台读表 |
| 业务选择、清单、工厂 | 尚无 | 本方案新增的小型应用装配能力 |
| 完整业务子包及流程 | 尚无 manual-picking／粗分实现 | 后续按各自业务范围实现 |

设备 Status 目前按需查询，不持久化状态、不运行周期同步任务。它不能充当业务状态库、执行许可或物理完成证据。

代码参考：[角色框架](../../integrations/ecs-roles.md)、[设备管理](../../integrations/device-management.md)、[注册约定](../../integrations/registration.md)、[ECS 命令与结果](../../integrations/ecs-command.md)。

## 3. 选择的组织方式与取舍

| 方案 | 优点与成本 | 结论 |
| --- | --- | --- |
| 完整业务放在 app/businesses，显式清单连接现有注册器 | 业务集中，平台与业务边界明确；需要适配入口发现、来源检查和分层测试 | 本文建议 |
| 全部放在 app/integrations 下 | 可沿用目录发现；但该目录目前代表集成层，并禁止依赖 app.services 等应用服务，完整编排会与现有边界混淆 | 不采用 |
| 业务模型、服务分别放顶层 models、services | 延续原顶层结构，但一个业务散在多处 | 已按讨论排除 |
| 独立插件包及运行框架 | 可独立安装，但增加加载、兼容、生命周期成本 | 当前不需要 |

第一项是本设计提出的新目录约定，不是当前代码已经支持的功能。实施时必须同步修改相关边界检查，不能把业务目录当作绕过检查的通道。现有平台文件不因本方案整体搬迁。

## 4. 各部分分别负责什么

| 部分 | 负责 | 不负责 |
| --- | --- | --- |
| definition.py | 不可变的业务身份、设备角色、位置需求和初始入口说明 | 导入合同／服务、构造工厂、查设备、发 HTTP |
| command_contracts.py | 使用现有 CommandBinding、DeviceAnnex 装配严格命令合同 | 保存现场设备、流程状态 |
| composition.py | 引用定义／合同，列出入口模块，提供简单工厂 | 定义另一套角色、创建第二个 Sender |
| 业务清单 | 显式列出本制品提供的业务；校验重复编码；提供创建工作线的选项 | 执行流程、动态安装代码 |
| 简单工厂 | 将平台依赖传入业务服务，返回无工作线私有状态的服务对象 | 按事件调度步骤、读取设备、创建执行 |
| 工作线配置 | 选择业务；维护本线实际设备／位置绑定 | 存 Python 路径、协议模型或 ECS 地址副本 |
| 协议入口 | 校验／接收协议，调用对应业务服务，保持原 ACK 语义 | 长事务等网络、编写主要编排规则 |
| 子包内 services | 受理、状态迁移、事务、下一步意图、结果消费、异常和完成判断 | 另写可靠 Sender 或放宽协议 |
| 子包内 models | 本业务执行、关联及消费进度 | 复制通用命令、可靠消息和收据表 |
| 平台集成模块 | 严格协议、收据、Outbox、命令／Transport 履约与证据 | 决定粗分或人工分拣的下一步 |
| 应用装配 | 加载声明／入口，汇总附录，创建唯一通信运行时并注入业务服务 | 把某条线的运行进度保存在内存 |

## 5. 业务子包目录

```text
app/
  businesses/
    __init__.py              # 保持轻量，不隐式装配所有业务
    definitions.py           # 小型不可变声明类型，只依赖标准库
    catalog.py               # 显式业务清单和加载入口
    runtime.py               # 平台依赖与业务服务的应用级装配
    manual_picking/
      __init__.py
      definition.py
      composition.py        # 引用定义／合同，提供入口模块和工厂
      contracts.py            # 严格协议模型；多时再拆分
      command_contracts.py   # CommandBinding、DeviceAnnex 集合
      services/
      models/
      schemas/                # 仅在存在管理或业务 API 时需要
      ecs_event/
      request/
      callback/
    coarse_sorting/
      __init__.py
      definition.py
      composition.py
      contracts.py
      command_contracts.py
      services/
      models/
      ecs_event/
      request/
      callback/
tests/
  businesses/
    manual_picking/
    coarse_sorting/
```

空目录不创建。简单业务可以只用 services.py、models.py。WMS callback/request 包仍一个 operation 一个文件，例如 outbound.picking_task.issued@v1 对应 outbound_picking_task_issued_v1.py。角色事件可用 ecs_event/scan1/scan_completed.py，叶子文件名遵循现有规则。

如果业务需要自己的 HTTP API，controllers 也放在业务子包内，路由由应用显式挂载；平台设备／工作线控制器保留原位置。业务内部继续遵守 controllers／协议入口 → services → models，不让 models 导入服务或控制器。

## 6. Python 集中声明：声明什么，不声明什么

### 6.1 参考 WES：定义和运行装配分开

已直接对照 wes_backend 的 manual-picking／automatic-picking 的 definition.py，以及 wes_plugin_sdk 的声明类型。它们值得借鉴的是：身份和资源需求使用不可变对象，业务直接引用同一对象；definition 不依赖运行实现。不是照搬插件版本、安装对象和专用生命周期。

参考文件为 wes_backend/workline_plugins/manual-picking/src/manual_picking/definition.py、workline_plugins/automatic-picking/src/automatic_picking/definition.py 和 src/wes_plugin_sdk/src/wes_plugin_sdk/definition.py。仅参考组织与约束方式，本系统不依赖或安装 wes_plugin_sdk。

本系统将上一版混在 BusinessDefinition 中的工厂、入口模块和命令附录移到 composition.py／command_contracts.py。definition.py 只使用标准库声明类型和本业务常量，单独导入不加载 ORM、Pydantic、集成框架、服务或网络。

建议的小型类型如下。它们是纯数据对象，不是业务基类；当前尚未实现。示例省略 __post_init__ 的校验实现，实施时按本节约束补齐。

```python
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True, kw_only=True)
class DeviceRole:
    role_key: str
    display_name: str
    required: bool = True


@dataclass(frozen=True, slots=True, kw_only=True)
class PositionSlot:
    slot_key: str
    display_name: str
    required: bool = True


@dataclass(frozen=True, slots=True, kw_only=True)
class InitialEntry:
    kind: Literal["wms_callback", "ecs_event"]
    name: str
    device_role: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class BusinessDefinition:
    code: str
    display_name: str
    device_roles: tuple[DeviceRole, ...] = ()
    position_slots: tuple[PositionSlot, ...] = ()
    initial_entries: tuple[InitialEntry, ...] = ()
```

类型放在 app/businesses/definitions.py。用一个 required 字段代替两份必需／可选列表，角色和位置都有显示名称；不再维护平行的字符串清单。入口也使用不可变对象，避免冻结对象中夹着可变 dict。

必要的声明校验：业务 code 使用稳定小写下划线格式；角色／位置 key 按现有绑定规则为大写、最长 100 字符；名称非空且有合理长度；required 必须是 bool；集合为对应类型的 tuple，角色 key／位置 key／初始入口不能重复。ECS 初始入口必须引用本定义内的角色，name 符合现有事件名规则；WMS 初始入口使用合法 operation 且不填写 device_role。字段校验和引用完整性由构造校验／装配检查完成，不留到实际来件时才发现。

本系统尚无对应位置类型／容量／货架资源主账，因此不照搬 WES 的 position_type、location_type、allowed_rack_kind 等约束字段。将来有实际校验依据时再扩展；不能增加只能展示、无法验证的“能力”。也不增加 plugin_key、plugin_version、supported_line_types；business_code 与初始入口已表达当前选择，严格协议另有附录版本。

### 6.2 一份 definition.py 长什么样

```python
from app.businesses.definitions import BusinessDefinition, DeviceRole, InitialEntry

COARSE_CONVEYOR = DeviceRole(role_key="COARSE_CONVEYOR", display_name="粗分读取与分流设备")
COARSE_ARM = DeviceRole(role_key="COARSE_ARM", display_name="粗分机械臂")
SCAN_COMPLETED = "SCAN_COMPLETED"

DEFINITION = BusinessDefinition(
    code="coarse_sorting",
    display_name="粗分作业",
    device_roles=(COARSE_CONVEYOR, COARSE_ARM),
    # 本例没有已确认的固定位置角色，不猜测实际位置。
    initial_entries=(
        InitialEntry(
            kind="ecs_event",
            name=SCAN_COMPLETED,
            device_role=COARSE_CONVEYOR.role_key,
        ),
    ),
)
```

这是粗分需求的声明示例，不证明 SCAN_COMPLETED 及其 Payload 已获供应商确认。实际接入只声明已确认入口；初始入口是只读说明，必须与真实注册匹配，不另建执行分发器。

人工分拣的相同写法：

```python
from app.businesses.definitions import (
    BusinessDefinition,
    DeviceRole,
    InitialEntry,
    PositionSlot,
)

SCAN1 = DeviceRole(role_key="MANUAL_SCAN1", display_name="料箱入口校验扫码")
SCAN2 = DeviceRole(role_key="MANUAL_SCAN2", display_name="人工工作位扫码")
SCAN3 = DeviceRole(role_key="MANUAL_SCAN3", display_name="退箱检验扫码")
SCAN4 = DeviceRole(role_key="MANUAL_SCAN4", display_name="退箱入队扫码")
INLET = PositionSlot(slot_key="INLET", display_name="料箱投料口")
OUTLET = PositionSlot(slot_key="OUTLET", display_name="料箱出料口")
TRANSFER_RACK = PositionSlot(slot_key="TRANSFER_RACK", display_name="转运货架位", required=False)
ISSUED = "outbound.picking_task.issued@v1"

DEFINITION = BusinessDefinition(
    code="manual_picking",
    display_name="人工分拣",
    device_roles=(SCAN1, SCAN2, SCAN3, SCAN4),
    position_slots=(INLET, OUTLET, TRANSFER_RACK),
    initial_entries=(InitialEntry(kind="wms_callback", name=ISSUED),),
)
```

人工分拣例子只展示声明写法，不是完整可运行的资源清单；完整移植还须按合同增加来源架、直接取料架等实际使用的位置需求。转运架位的可选性参考 WES 当前人工线可无 target_rack 的合同；涉及它的具体动作仍须校验本次任务和绑定，不把 optional 理解为任何路径都不需要。

角色对象同时供表单、配置校验和代码引用。不同业务语义／合同使用不同角色 key，多条同业务工作线复用相同对象。ECS Status 的 role、supported_commands 不能自动产生业务绑定或正式附录。

### 6.3 与现有字符串框架怎样兼容

不修改现有 ecs_event、CommandBinding、device_bindings 的字符串合同。在边界显式取 key：

| 使用位置 | 使用方式 |
| --- | --- |
| ECS 事件装饰器 | device_role=COARSE_CONVEYOR.role_key |
| CommandBinding | 第一个参数为 COARSE_CONVEYOR.role_key |
| resolve_role | role 参数传角色对象的 .role_key |
| 工作线保存设备绑定 | JSON key 仍是 COARSE_CONVEYOR 等字符串 |
| 工作线保存位置绑定 | JSON key 为位置对象的 .slot_key |
| 工作线表单 | 用 display_name 展示，用 key 保存；required 生成缺项提示 |

声明对象不能直接当作 device_bindings 的 JSON key，也不依赖 str(role) 隐式转换。不另外维护同名 key 字符串常量。命令 source 仍指向真实 Payload 模型，不改成角色对象，以保留合同源码追踪。

### 6.4 合同和工厂放在哪里

contracts.py 保存严格模型，command_contracts.py 引用模型和角色对象，组装现有合同类型。以下片段省略模型导入，所引用模型与超时必须来自确认合同，不能拿示例空模型代替：

```python
from app.integrations.ecs_command import CommandBinding, DeviceAnnex
from .definition import COARSE_CONVEYOR

COMMAND_BINDINGS = (
    CommandBinding(
        COARSE_CONVEYOR.role_key,
        "MOVE_FORWARD",
        ANNEX_ID,
        ANNEX_VERSION,
        MoveForwardParams,
    ),
)
ANNEXES = {
    (ANNEX_ID, ANNEX_VERSION): DeviceAnnex(
        params_model=MoveForwardParams,
        result_model=MoveForwardResult,
        error_model=MoveForwardError,
        timeout_min_ms=CONFIRMED_TIMEOUT_MIN_MS,
        timeout_max_ms=CONFIRMED_TIMEOUT_MAX_MS,
    ),
}
```

不同参数／结果形状使用匹配附录，不能因设备相同就共用宽 dict。应用装配必须执行下述声明与实际合同的交叉校验，不能只验证对象自身字段。WMS 请求和回调继续使用原装饰器及严格模型，不移入 definition。

composition.py 汇集定义、合同及入口模块，提供简单工厂：

```python
from .definition import DEFINITION
from .command_contracts import ANNEXES, COMMAND_BINDINGS

ENTRY_MODULES = ("app.businesses.coarse_sorting.ecs_event",)


def build_service(capabilities):
    from .services.material_flow import CoarseSortingService

    return CoarseSortingService(ecs=capabilities.ecs, wms=capabilities.wms)
```

若业务有 WMS 请求／回调，将实际模块追加到 ENTRY_MODULES。它是可信代码内的固定路径，不从 WORKLINE 或用户输入取得。模型加载仍单独登记，不靠服务导入带出模型。

导入方向为：definition → 标准库声明类型；合同／入口／服务 → definition；composition → definition 和合同，factory 调用时才导入服务。contracts.py 不反向导入 composition／服务／入口；业务 __init__.py 保持轻量。这样查看资源需求不会顺便加载运行实现。

### 6.5 声明与实际合同怎样核对

catalog.py 提供待实现的 validate_business_contracts()：在全部正式入口导入成功后，读取显式清单、CommandBinding／DeviceAnnex 及原三类注册表，按下表校验，返回已检查的命令集合和合并附录。它只检查元数据，不调用 factory、不解析现场设备、不创建客户端或 Sender。现有 RoleRoutes 构造阶段检查命令条目、角色／任务重复键和附录存在，却不接收业务定义，不能代替这项校验。

| 检查对象 | 必须满足 | 错误示例 |
| --- | --- | --- |
| 每个业务的 CommandBinding | device_role 在本业务 DEFINITION.device_roles 中；引用在本业务 composition.ANNEXES 中明确声明，且为现有 DeviceAnnex | 业务只声明 COARSE_CONVEYOR，却登记 OTHER_ROLE 的命令 |
| 本业务实际注册的正式 ECS 事件 | 按真实 RegistrationSource.module 归属业务，device_role 在该业务声明中 | 事件可注册，但角色没有出现在本业务声明中 |
| initial_entries 的 ECS 项 | 原注册表有准确的 (device_role, name)，对应正式 handler 来源属于本业务 | 名称相同但角色错误，或用另一业务／local handler 冒充 |
| initial_entries 的 WMS 项 | 原回调注册表有准确 operation，对应正式 handler 来源属于本业务；device_role 为空 | 只注册了 request，未注册 callback，或只有 dev callback |
| 汇总命令 | 全部业务的 (device_role, task_type) 不重复，不能后写覆盖 | 两个 composition 登记同一角色命令 |
| 汇总附录及需保留的历史附录 | 相同 (annex_id, annex_version) 只能对应同一合同 | 同键的参数／结果／错误模型或超时不同 |

注册项归属按业务包的完整模块前缀及原 RegistrationSource 判断，不按字符串包含关系猜测；平台内置和 local 示例不冒充业务自有入口，也不因没有 BusinessDefinition 被纳入业务角色成员检查。只读定义查询不为核对这些关系而导入 handler；完整校验属于应用装配阶段。后续事件只需核对角色归属，不强制列入 initial_entries。

附录合并逐项检查，不使用未检查的 dict.update 或字典解包覆盖。同键复用要求 params_model、result_model、error_model 分别为同一模型类，timeout_min_ms／timeout_max_ms 相同；另建同形模型不能自动当作同一合同。相同合同允许通过各业务 ANNEXES 显式引用复用，不能因另一业务或历史集合恰好有同名键就补上本业务漏声明的命令合同；历史附录可仅供旧 Result 校验，不因此给新命令增加角色或动作权限。

完整应用启动顺序为“全部正式入口加载成功 → 交叉校验成功 → local 入口（仅 local）→ 通信资源与 factory”。缺项、错角色、错来源、重复命令或附录冲突均使装配失败，并指出业务编码、合同键及声明来源；失败时不能构造 DeviceRoutes、启动 Sender、调用 factory 或对外受理。不是把错误留到第一条真实事件才暴露，也不把启动校验变成第二套路由。

## 7. 业务清单与简单工厂

### 7.1 显式清单

业务清单在代码中明确列出本制品的装配模块，不再把运行依赖塞进 BusinessDefinition：

```python
# catalog.py 的逻辑示例，不是已实现代码。
def entries():
    from .manual_picking import composition as manual
    from .coarse_sorting import composition as coarse

    return (manual, coarse)
```

调用方从 entry.DEFINITION 取得只读资源声明，从 entry.build_service 取得工厂，从 entry.ENTRY_MODULES、COMMAND_BINDINGS、ANNEXES 取得装配数据。模块是显式 import 的普通 Python 模块，不需要再定义一个有生命周期的 BusinessRegistration 基类。

使用惰性函数延后装配模块导入；声明类型独立放 definitions.py，避免 catalog 与各 definition 循环依赖。汇总时检查重复 code、附录冲突和角色任务重复键；同名不同附录不得静默覆盖。业务编码在对接数据中稳定，不按目录重命名自动改变。清单可以加载合同元数据，但绝不调用工厂来生成管理选项；definition 本身仍可独立纯读取。

数据库只存 business_code，不存导入路径。未知业务编码拒绝；未部署业务不能出现在新建选项中。清单不扫描磁盘寻找未知业务，不从用户输入执行 import。

### 7.2 简单工厂

每个业务在 composition.py 提供普通 build_service 函数，写法见 §6.4；服务类在函数内导入，不在 composition 顶层导入。清单加载装配元数据时不应带入服务／ORM，只有实际构造运行时才加载业务实现。

Capabilities 是应用显式注入的平台能力对象，不是任意服务定位器；只包含实际需要的能力。factory 不创建第二个 Sender、数据库或 Redis 连接，也不实例化每条工作线。

服务对象可在进程启动时构造一次，但不持有当前 session、workline 或某笔作业状态。每次调用明确传入当前事务和业务上下文。无需构造依赖的纯规则可直接用函数。

两种业务不强制相同执行方法。人工分拣服务可以提供 accept_issued、apply_plan_delta，粗分服务可以提供 accept_material_scan、apply_validation_result；工厂只构造，不根据步骤名反射调用。

## 8. WORKLINE 创建与配置

### 8.1 管理员看到的流程

1. 新建工作线，选择“人工分拣”或“粗分作业”。
2. 保存编码、名称和 business_code；不要求此时设备已齐全。
3. 登记／分配实际设备到该线。
4. 根据业务声明，填写 device_bindings、position_bindings。
5. 系统给出缺少角色、未知角色或业务位置不合法等配置问题。
6. 新作业受理／实际执行前重新校验，不以页面曾显示完整为依据。

“工作线创建成功”“配置完整”“当前业务允许执行”是三个不同结论。不新增一个把三者混为一谈的通用 RUNNING 状态。

### 8.2 新增合同

- WorkLine 新增可空 business_code。迁移已有线时保留 null，不能按名称、已有角色猜测用途。
- 新建工作线要求明确选择已部署业务。这是 WorkLineCreate 的新增必填项，需要同步调用方、示例和测试；不能声称旧创建请求完全兼容。
- 已有 null 工作线可以通过原更新入口明确选择一次；首期不提供已选业务的通用切换或清空。后续确需变更时另设计迁移语义。
- 工作线更新模型、读取模型、过滤及审计同步支持 business_code。原管理仍要求管理员及 asset:workline:* 权限。
- 原有 null 工作线继续可读、可维护；不能作为新业务的默认承接线。正式业务入口必须校验 business_code，不因存在旧角色就绕过。
- 建议新增 GET /api/v1/businesses，供工作线表单列出 code、display_name、触发说明及角色／位置对象的 key、display_name、required；复用管理员＋asset:workline:read，不返回工厂、模型或源码路径。
- 业务清单 API 是管理选项；原 local 集成目录继续展示协议注册与源码，两者用途不同，不互相替代。

有设备绑定时，更新后的角色必须属于本业务声明。缺少必需角色可以暂存为未配置完整；未知角色、跨线设备、重复设备等明确错误拒绝。暂时保留已有 null 线的原管理规则；选择业务时必须校验现有绑定。

位置绑定目前没有完整资源主账。字符串存在不证明位置可用；业务需按已确认合同验证具体位置，不能通过声明虚构现场能力。首期不为此建设通用拓扑或占用平台。

## 9. 两种触发如何进入业务

### 9.1 单据触发

人工分拣的 issued 入口只注册一次。处理器按单据 workline_code 找当前工作线，校验其 business_code=manual_picking，保存原任务和工作线归属，再按业务规则排队或推进。指定粗分线应明确拒绝，不能自动换线。

plan_delta 和其他后续通知通过原 task_id／operation_id 找原执行，不重新选业务。入库／出库方向沿用 manual-picking 合同的 business_type=inbound|outbound，不拆成两种工作线业务。

### 9.2 设备触发

```text
device_code
→ 已有 resolve_binding
→ 当前工作线、角色及管理版本
→ 已有 (device_role, event_type) 分发
→ 业务校验本线 business_code
→ 创建或关联本次执行
```

例如 COARSE_CONVEYOR＋SCAN_COMPLETED 进入粗分受理。角色不能代替业务校验：当前绑定也可能误配到不适用的工作线。受理校验与创建执行在同一事务内，并保护所依据的工作线／设备绑定。

不修改 ECS wire 增加自报业务编码，也不根据 payload 形状、设备名或物料码猜业务。初始入口只描述“如何创建业务执行”；manual-picking 后续同样接收设备事件，粗分后续同样消费 WMS 结果。

新 ECS 事件的协议结构／已注册 Payload 非法时，保持现有 INVALID_ENVELOPE 行为。结构合法但工作线业务稳定不匹配、必需角色未配置等情况，记录受控 diagnostic，不创建执行，保留收据并返回原 ACK；配置修复后不自动回放。若业务要求这些输入必须留待执行，需另定义持久待办及有效期，不能默认 ACK 就会再次执行。

数据库故障、锁后绑定竞态等临时失败继续抛出，使收据与业务同事务回滚，沿用原重试语义；不能捕获所有异常并 ACK。WMS 单据按原 callback 的 Rejected／Conflict 及已确认 reason_code 合同处理稳定拒绝，不把 ECS 的“收据＋诊断 ACK”策略套到 WMS。

### 9.3 调试状态由 ECS 管理

按本轮明确决定，WES 清理 is_debug 参数，不根据该字段区分正式／调试业务。ECS 自行决定调试状态，以及是否向 WES 上报业务事件；上报给 WES 的有效业务事件统一按本线业务合同受理。WES 的业务声明、入口和编排不增加调试分支，local 开发入口及 Mock 仍按已有环境与权限规则隔离。

后续清理覆盖 EcsEventEnvelope、EcsEventContext、控制器 OpenAPI、事件规范化与摘要、包文档及相关测试。若旧 ECS 仍发送 is_debug，把它视为现有 extra=ignore 规则下的额外字段：不校验其类型、不传给业务，也不影响新消息身份。新身份仅取 device_code、event_type、timestamp、data 的规范化摘要；不能仅改业务示例而让该字段继续改变消息身份。

当前收据摘要包含 is_debug，因此删除字段还必须保持升级前后的查重。历史收据和原业务关联身份不重算、不合并、不删除；新入口查重时，除新摘要外，同时检查相同规范化事件在旧规则下 is_debug=false（含原省略字段）及 true 的摘要。找到任一有效期内原收据即沿用原 ACK，不再次调用处理器；两条历史收据同时存在也不合并原执行或创建第三笔执行。新来件按新摘要建立收据并保证同身份并发只执行一次，业务自身仍需持久幂等，不能靠收据保留期永久兜底。

混用新旧身份算法的接收器不能共同受理同一设备事件；升级时应统一接收实例的算法版本后恢复接收，保留对端原消息重试能力。这里规定后续实现和发布必须满足的兼容边界，本轮不部署。验收覆盖旧 false／true／省略字段的历史重报、历史双收据、额外字段被忽略、并发新消息只受理一次，以及历史未注册事件不因本次升级自动进入新业务。

### 9.4 业务依赖怎样交给处理器

当前 EcsEventContext／CallbackContext 没有业务服务对象。不能在示例里直接假设 ctx.app 或 ctx.business 已存在。

建议应用装配一个 BusinessRuntime：保存本制品定义及各 factory 构造的无作业状态服务。由现有 include_ecs_callback、include_wms_events 的装配参数和接收器，把同一可信 runtime 引用传入处理器上下文；作为可选字段保留非业务使用方兼容。没有装配所需 runtime 时，正式业务入口启动校验失败，不等到请求来才报错。

处理器只从这个受限 runtime 取本业务受理服务，使用 ctx.session 完成下述受理事务；不建立模块级可变“当前业务”，不从请求 JSON 取依赖。独立测试注入 fake runtime。此为待开发的明确接缝，不是现有 API。

示意伪代码（require_runtime／configuration_issue 为待实现适配函数）：

```python
@ecs_event(SCAN_COMPLETED, device_role=COARSE_CONVEYOR.role_key, data=MaterialScan)
async def scan_completed(ctx, payload):
    runtime = require_runtime(ctx)
    issue = await configuration_issue(ctx.session, ctx.binding, "coarse_sorting")
    if issue is not None:
        ctx.note(issue)  # 仅允许受控原因码；临时故障由函数抛出。
        return
    service = runtime.services["coarse_sorting"]
    await service.accept_material_scan(
        ctx.session,
        binding=ctx.binding,
        trigger_id=ctx.digest,
        payload=payload,
    )
```

accept_material_scan在原ctx.session中保存受理状态及持久待推进依据，不在该事务中执行需要多次结果读取的推进，也不自行开启第二个写事务；独立推进入口见§10.1。

此处 trigger_id 表示原消息身份，不自动等于物料过线业务身份。两种身份是否等价必须由 ECS 合同证明；同物料合法再次投入不能按物料码合并。实际代码需要明确导入本业务角色／Payload和公共适配函数，不把主要编排写在处理器里。

## 10. 业务编排与状态归属

业务状态和编排全部放在业务子包。受理与结果推进分为两个事务，职责见§10.1。一次独立推进执行以下工作：

1. 按下述锁序取得所需配置及本次执行，核对原触发、请求／命令和当前业务前提。
2. 判断该结果是否已消费；相同结果不重复创建动作。
3. 更新业务状态，必要时通过现有服务登记下一步外部意图。
4. 同一事务提交业务状态、消费进度和 Outbox 意图。
5. 等待发生在事务外；结果到来或恢复扫描时再推进。

不把 HTTP 放在业务事务内，不等待整条流程结束再回复触发消息。WMS／ECS 接收 ACK、命令成功和业务完成分别保存。业务等待扫描只发现待推进业务，不接管 Outbox 的投递重试。

ECS 原命令可通过 get_command／get_by_call 查询，但业务仍需保存稳定动作键、command_code、当前许可及消费标记。多动作流程不能直接套一动作一命令的参考表。WMS 请求和 Transport 的正式结果消费接口由前置 Task 0 按 §10.2–10.6 补齐；其公开读模型用于本业务保存消费进度及重复／重启恢复。接口放在对应集成／应用适配层，不放进协议无关 app/reliable，业务不得直接导入 ReliableMessage／TransportTask 或自行解析 response_raw。

队列唤醒只携带稳定执行身份，事务提交后发出。通知可重复或丢失，持久待办扫描兜底；恢复不得更换原命令／请求身份来重发未知物理动作。

### 10.1 事务边界与锁序

**受理事务由现有接收控制器拥有。** ECS／WMS处理器使用原ctx.session完成原收据、配置与合同校验、业务受理状态和持久待推进依据的原子写入；原ACK／Rejected／Conflict及临时失败语义保持。只依赖本次输入和当前许可、无需消费前序结果的初始意图可按具体业务合同在该事务登记。需要统一快照的结果消费／前序汇总一律留给独立推进，不在处理器中升级隔离级别、提交／回滚原事务或另开写事务尝试推进。现有接收器在调用处理器前已执行收据／绑定SQL，因此不能将“进入业务服务”当成事务开始。受理阶段临时故障由原控制器回滚收据及业务写入；稳定拒绝沿原协议保留收据且不创建待推进依据，不能将Rejected一律视作控制器异常；受理已提交后的推进失败不撤销受理、不改原ACK，不要求重放受理才能恢复。

**独立推进事务由各业务的推进入口拥有。** 提交后唤醒及持久待办扫描只将原execution_id交给同一业务推进入口；它创建新的session，先按§10.6配置本事务REPEATABLE READ，再按既有锁序核对本次执行、全部前序及当前许可，调用本业务推进服务，并原子提交进度、消费依据、下一步Outbox意图。事务入口负责session的创建／关闭及commit／rollback；推进服务和平台读方法只使用传入session，不接管事务。入口和服务均属于具体业务，可有自己的名称，不在BusinessRuntime新增通用execute或平台流程引擎。所需session factory由应用／worker可信装配注入事务入口；复用现有数据库引擎及连接池，只为推进事务选择隔离级别，不修改全局session_factory、接收事务或WMS STATIC路径。不会在工厂构造的无作业状态服务中保存活动session。

原始结果接收／应用仍在框架事务中完成，不把其持有的任务／命令／消息锁带入推进。结果接收成功本身不代表业务推进已成功；唤醒重复／丢失由本业务幂等和持久待办扫描兜底。Task 0以独立测试侧动作表和事务入口验证此边界，不新增生产待办表或通用worker；Task 5接线保持受理事务职责，Task 6用测试业务贯通真实接收路径到独立推进。每个真实业务的后续实施计划必须落实自身持久待办、事务入口和装配，不能只提供薄处理器便宣称结果编排可用。

需要当前配置的业务推进统一按“工作线（多行按 ID）→设备（多行按 ID）→本业务执行／动作（固定 ID 顺序）”取得锁。ECS 入口的 resolve_binding 已先锁工作线／设备，业务服务顺着锁业务行；恢复扫描、WMS 消费先无锁定位原执行的工作线和设备，取得上述锁后重新读取、校验，不允许先锁业务行再反向调用角色解析器。

框架发送门禁当前先锁命令，再检查配置，不能简单宣称全系统都遵循上述业务锁序。因此业务在持有配置锁时只能按现有只读接口核对既有命令结果，不主动锁、settle、hold 或改写既有框架命令／可靠消息；否则会与 Sender 形成反向锁等待。业务取消先记本业务事实，框架暂停仍走原机制；新增取消能力另行设计。

原 ECS Result 的留存／结算维持原框架事务，不能在其持有命令／消息锁时直接调用需锁工作线的业务服务。业务消费通过提交后唤醒或扫描，在后续独立事务按业务锁序推进。WMS／Transport 结果已保存后也采用同样原则：不把持有框架执行锁的接收路径直接嵌入业务推进。

新意图仍与业务状态在同一事务登记；首次插入和同键只读找回沿用原服务。若实现引入对既有消息的写锁、唯一键等待或多资源锁，必须补 PostgreSQL 交叉并发用例，不以本文锁序证明其天然无死锁。网络请求始终在事务外。

### 10.2 Task 0：出站结果消费契约与最小查询接口

先完成本节的契约和平台读接口，再实现 manual-picking／粗分依赖结果的编排；不能将接口定型继续留到具体业务已经开始读表之后。Task 0 不改发送、重试、原回调应用或既有终态规则，不新增事件总线、通用消费表或第二个调度器。ECS 继续复用现有 get_command／get_by_call，不强行统一三个系统的状态名。

新增结果类型放在 app/integrations/wms_request/results.py、app/integrations/wms_transport/results.py；前序查询放在 wms_transport/predecessors.py，由原 WmsRequests／TransportService 暴露方法。下表定义拟议公开字段，不是现有接口。读模型为只读外壳与独立数据副本，不返回 ORM、会话绑定对象或可靠层原始响应列；业务 data 仍须由所属业务的严格合同校验。

| 读模型 | 最小字段及含义 |
| --- | --- |
| WmsRequestResult | message_id、operation、operation_id、原target；availability；delivery_status、delivery_terminal、delivery_uncertain、fail_reason；response_state、http_status、response_code、response_timestamp、response_data。投递状态与响应分开，不提供混淆含义的业务completed字段 |
| TransportTaskResult | transport_task_id、client_request_id、operation_id、原workline_id／station_id、cycle_id、kind；availability；submission_state（AVAILABLE／UNAVAILABLE／AMBIGUOUS／INVALID）、submission_message_id、submission_target、submission_delivery_status、submission_delivery_uncertain、submission_fail_reason、submission_response_state、submission_http_status、submission_response_code、submission_response_timestamp、submission_response_data；已应用outcome／outcome_revision、member_facts（原任务已记录的历史位置里程碑副本，不表示当前终位／占用）；has_pending_outcome、pending_revision；closure、closure_reason、closure_basis。结果包含原合同的整架／逐箱结果、终位或位置未知、到达面及失败原因，不只返回一个status |
| PredecessorQueryResult | 原caller、cycle_id、资源类型／编码、requested_ids；items（每项含任务身份、所查资源的closure、原task_closure、相关member_ids、reason、closure_basis、已应用版本及原拒绝证据引用）；unresolved（从items派生的非CLOSED项）；complete。仅 complete=True 且 unresolved为空才能证明本次明确列出的前序均闭合，不代表当前物理可执行 |

availability 取 FOUND／NOT_FOUND／IDENTITY_MISMATCH／INVALID；取不到原行只返回NOT_FOUND及请求身份，不冒充PENDING。Transport的submission_state单独表示提交关联是否可用或歧义；该标记属于提交证据，不抹掉已存在的有效执行结果。response_state 取 NONE（尚无可消费终局响应）、VALID（已确认的协议响应）、INVALID（内容或状态证据矛盾）、UNAVAILABLE（原消息／终局响应已不可取得）。未知标量字段取None，不猜默认状态。

### 10.3 原身份读取与响应可信度

- WmsRequests.get_result(session, *, message_id, operation, operation_id) -> WmsRequestResult。业务登记时保存 send 返回的原 message_id 及自行生成的 operation／operation_id；读取同时核对三者。只按原行取得target，不调用当前 target() 重新选目标；即使配置已改、删除或新增同operation目标，也不能串到另一条消息。身份不符返回IDENTITY_MISMATCH且不泄露另一行内容。
- TransportService.get_result(session, *, transport_task_id, client_request_id, caller) -> TransportTaskResult。按原任务及请求身份读取，并核对原 TransportCaller；调用方使用业务动作中保存的原引用，不按同编码新工作线接管历史。caller是原数据范围，不等于当前配置的执行许可。
- 当前Transport没有持久化提交消息主键，Task 0先不新增列或迁移：在平台内部以 task.operation_id＋固定 transport.task.submit@v1＋business_key=transport_task_id 关联原消息，跨target查找但必须唯一。零条为UNAVAILABLE、多条为AMBIGUOUS，不能按当前配置或“最新一条”选。已应用物理结果不依赖可靠消息继续保留。
- WMS有效响应仅来自同身份的终局投递证据，复用已有信封和端点分类规则，返回已验证的code／timestamp／data；不把 last_response_raw 的503、429或未知响应当决定结果。response_data只通过公共协议校验，具体DECIDED字段由业务严格解析。DONE却无终局响应时返回UNAVAILABLE，不造空成功响应。
- WMS的DONE只表示请求送达；RECORDED／DUPLICATE表示事实接口接收，PREPARE_ACCEPTED表示等待后续结果，DECIDED才可按具体合同读取决定。FAILED／EXPIRED是投递终态，不能直接当作业务失败或证明物理动作未执行；LOCAL_ERROR、delivery_uncertain及错误响应分别保留。只看HTTP状态或可靠消息status不能完成业务。
- Transport只将已应用 outcome／outcome_revision 作为执行结果。pending_outcome仅暴露“有待应用结果及版本”，不把其内容作为已完成结果消费。原回调可能在结果未应用时就settle提交消息，所以DONE不证明搬运完成，也不能凭同一次回调的202 ACK完成业务。

### 10.4 Transport前序闭合判定矩阵

closure取CLOSED／OPEN／UNDETERMINED。CLOSED只表示该原任务已经有可用的闭合依据，不表示成功、不授权新动作；OPEN表示仍等待，UNDETERMINED表示证据不足或冲突，需要核对。优先使用有效已应用结果，再评估提交证据；未应用或不匹配事实不能覆盖已确定结果。

| 当前原任务事实 | closure／依据或原因 | 消费边界 |
| --- | --- | --- |
| 有效已应用SUCCEEDED，终位／成员位置满足原回调合同 | CLOSED／APPLIED_RESULT | 可消费原成功事实，下一步仍检查本业务前提 |
| 有效已应用FAILED或CANCELLED（CANCELLED仅限原合同支持的货架族），位置明确；料箱族所有成员位置均明确 | CLOSED／APPLIED_RESULT | 记录失败／取消及终位，不等于默认重试或允许换面 |
| 有效已应用UNKNOWN，或任一成员位置未知 | UNDETERMINED／UNKNOWN_POSITION | 不猜空位／未执行；更高合法结果可收敛；不写伪造终态 |
| 有pending结果但所需TARGET_PLACED未齐，且无更高有效终态 | OPEN／AWAITING_POSITION；若已应用UNKNOWN则仍UNDETERMINED | 位置到齐由原回调逻辑应用，读接口不替它补齐 |
| 同身份提交被原适配器明确判为409 CONFLICT或422 REJECTED，且不存在相矛盾的同任务有效位置里程碑或已应用／暂存执行结果 | CLOSED／SUBMIT_REJECTION | 仅说明原提交已明确被拒；新请求由业务重新判断，不能把UNKNOWN回调掩盖成拒绝 |
| 有已确认RECEIVED／DUPLICATE，或回调使提交DONE，但无已应用执行终态 | OPEN／AWAITING_RESULT | 仍等执行结果；回调诊断未应用、位置事件或DONE均不算完成 |
| 提交尚在PENDING／SENDING／STALLED，且未出现投递不确定或未知位置 | OPEN／AWAITING_SUBMISSION_OR_RESULT | 沿原投递机制继续；不另建重试或新动作键 |
| 仅有超时／无响应／delivery_uncertain、HELD、LOCAL_ERROR、EXPIRED或不可解释的终局响应，且无有效闭合依据 | UNDETERMINED／DELIVERY_OR_RESULT_UNCERTAIN | 即使不再发送也不能当作从未下发 |
| 原任务／冻结事实缺失、身份或范围不符、应用结果结构不一致；或只有拒绝证据但同时存在相矛盾的有效位置里程碑、待应用／UNKNOWN执行结果 | UNDETERMINED／TASK_NOT_FOUND、SCOPE_MISMATCH、MISSING_FROZEN或INVALID_EVIDENCE | 明确报告异常，不返回空结果冒充全部闭合 |
| 原提交消息已清理或关联不唯一，且无有效已应用结果 | UNDETERMINED／SUBMISSION_UNAVAILABLE或AMBIGUOUS_SUBMISSION | 查不到不等于未下发；若已有有效已应用终态，仍按前两行CLOSED，不因消息清理倒退 |

以上使用现有TaskCallbacks的结果定义：SUCCEEDED／FAILED／CANCELLED为确定结果，UNKNOWN可收敛；料箱成功回架需要位置事实齐备。读接口不重放、settle、修订或补偿任务。独立新作业不继承所有历史异常，未知任务也不能被登记成“永久阻塞整个货架”的全局状态；当前直接依赖不确定时，由业务等待本轮权威事实或进入待核对路径。

### 10.5 未闭合前序查询的范围和完整性

TransportService.list_unclosed_predecessors(session, *, caller, cycle_id, predecessor_ids, rack_id=None, container_id=None) -> PredecessorQueryResult。predecessor_ids为业务动作保存的明确前序任务ID元组，不能为None；最多100个，重复ID及超过上限明确拒绝，不静默截断。空元组表示本动作没有列出前序，不自动扫描历史。cycle_id必须为原UUIDv7轮次；rack_id与container_id必须且只能填一项。

查询只检查这些明确ID，并核对原caller、cycle_id及冻结事实的资源归属。货架按整架匹配，不按rack_face缩小；料箱按冻结成员匹配，货架关联含料箱来源／目标架槽。料箱批次按所查资源相关成员计算前序项closure：已有应用结果中其他架／其他箱成员UNKNOWN，不应把本资源已明确终位的成员误判未闭合；同时保留原task_closure和相关member_ids，不把局部闭合改写成整批成功。整批仍UNKNOWN时，成员投影没有整任务确定终态的不可变保证；后续动作前须重读当前已应用版本，高版本可重新改变本资源的判定，不能把曾消费过的局部闭合当作永久授权。若相关成员的成功结果仍只在pending中，即便已有位置里程碑也不将其当已应用结果；前序项仍等待。member_facts只能作为历史里程碑使用：既有TARGET_PLACED可在后续已应用结果变为UNKNOWN后继续保留；它没有足以证明当前位置的新鲜度／版本顺序，不能覆盖更新的已应用UNKNOWN、绕过pending等待、推导当前占用或授权下一动作。只有符合原合同的新权威结果／事实才能解除相应不确定性，不能把历史里程碑当作缺失的新事实；Task 0不清空历史、不改变原回调位置前置条件，也不新建位置台账。只有提交拒绝且尚无已应用结果时，同任务已接纳的SOURCE_PICKED／与冻结目标匹配的TARGET_PLACED构成拒绝闭合的冲突证据，返回UNDETERMINED／INVALID_EVIDENCE；不将其用作当前完成依据。POSITION_UNKNOWN仍沿原回调只记诊断；后续有效已应用结果按矩阵优先，可以解除该冲突。其他工作线、旧轮次、其他货架、同架后续任务或独立作业不自动进入前序集合；业务不能只靠created_at推导直接依赖。跨轮的真实直接依赖由业务显式保存原身份并用get_result逐项读取，不让自动查询扩展整个历史。

明确ID中的缺任务、错范围、冻结信息不足必须返回UNDETERMINED项且complete=False，不能被过滤后变成“零未闭合”。items完整返回本次每个ID的判定，包括CLOSED项及其证据引用，供业务持久保存闭合依据；OPEN项与UNDETERMINED项均出现在派生的unresolved；只有所有ID都得到确定分类时complete=True（仍可能存在OPEN），全CLOSED才有complete=True且unresolved为空。超出100条的真实依赖可由业务拆分，但必须预先固定完整身份集合，按§10.6在统一快照内检查所有批次后才汇总；不能以一批为空代替全部完成。同一次前序判断中的跨轮get_result逐个读取、混合批量／逐个读取遵守同一规则，不能通过改用单条接口绕开。

### 10.6 一致性、消费进度和保留边界

统一快照的建立入口是§10.1的独立业务推进事务入口，不能是已执行SQL的受理处理器；以下多次读取规则适用于该入口所拥有的整个推进事务。

读接口使用调用方session，仅读数据库列并返回副本；不commit／rollback／flush、不FOR UPDATE、不修改任务／消息、不发HTTP，使用no_autoflush避免读取隐式刷新调用方写入。读取必须执行SQL取得当前事务可见数据，不能沿用session中旧ORM对象缓存。Transport任务与提交消息，以及一个有界前序批次的结果，在同一SQL语句快照中取得；不能拼接不同时间读取的拒绝／执行结果来错误判闭合。数据库／锁超时等临时读取错误原样抛出，不能捕获后返回NOT_FOUND或complete=True；已读到但结构／身份不合法的证据才按读模型报告INVALID或不可判定。单语句一致性不能替代下述跨查询一致性，也不是执行授权或物理占用锁。

**统一快照由业务调用方负责建立。** 凡将多次读取合并为同一次前序判断，必须在事务首条SQL前配置PostgreSQL REPEATABLE READ，并在同一session、同一事务内完成全部批次／逐个get_result读取；事务首个查询建立的快照贯穿本组判断。逻辑组预先固定完整原身份集合及各自caller／cycle／资源范围，覆盖超过100条、跨轮直接依赖及批量与逐条混用。默认READ COMMITTED即使共用事务，每条SELECT仍可看到不同快照，禁止据此汇总“全部闭合”。单次有界查询仍保留单SQL规则，不为所有普通读操作全局提高隔离级别。参见[PostgreSQL事务隔离说明](https://www.postgresql.org/docs/current/transaction-iso.html)。

读方法不自行升级隔离级别、不提交／回滚调用方事务；实施文档须给出调用方在事务首条SQL前配置隔离级别的用法。若已在READ COMMITTED事务中执行SQL，调用方必须放弃本组汇总，在其拥有的事务边界重新开始；不得中途切换隔离或将旧结果带入新事务。任一次读取异常／缺项／不可判定，均不能得出全闭合；异常重试、跨事务、重启恢复或新的业务动作必须重新读取整个尚需核对的集合，不能拼接旧批次片段。保存的整体确定终态证据仍按下文保留规则处理，不把可变成员投影当成这类证据。

统一快照只保证本组事实曾同时成立，不保证提交／派发时仍是最新事实。需要采纳新到更高版本时，结束旧判断并在新事务重读全组；不能在同一REPEATABLE READ事务内承诺读到外部新提交。若与动作进度／下一意图同事务提交发生序列化失败，由§10.1的独立推进事务入口回滚并关闭失败session，在新session／新事务内重读整组前序及当前许可，重试整个数据库事务并沿用原execution_id／动作身份；不能复用旧DTO片段或只重试最后一批／commit。其他临时数据库失败同样不得提交部分消费／意图，按本业务持久待办恢复；不把身份错误或UNKNOWN当作可重试成功。真实业务须明确单次唤醒的有界重试与耗尽后的待推进／受控失败处理，不能无限忙重试或丢弃原待办。受理控制器只负责自身受理事务的失败语义，不承担已提交后的推进重试；读接口不重试，原Outbox负责提交后的投递重试，不重试外部物理执行、不新增第二个发送调度。当前有效许可、业务并发门禁及原发送门禁仍由既有机制和具体业务负责，快照本身不锁定位置或保证后续动作安全。

业务在自己的事务中锁本次动作，保存已消费的原请求／任务身份及结果版本、必要决定／闭合证据，再登记下一步意图，与状态一并提交。WMS终局响应以原message_id＋operation＋operation_id标记消费；Transport使用原transport_task_id＋已应用revision，拒绝则保存原提交消息身份及已验证拒绝依据。消费唯一性属于本业务动作／消费者，不是全局“一个结果只能消费一次”；成员投影的证据引用还包含资源及member_ids。不同动作可以各自消费同一批次版本，不互相吞掉推进。不增加平台消费位、分发总线或另一套状态机；重复读取／唤醒不产生副作用。读取同一UNKNOWN结果不能无限生成新动作，恢复不更换原身份。

沿用当前RELIABLE_RETENTION_DAYS（默认90天）和原清理机制，不承诺未消费结果无限保留。业务须在保留窗口内读取并持久保存所需结果及消费进度；跨重启继续使用自身已保存的决定，不从已清理消息重新推导。超过保留期未保存证据时返回NOT_FOUND／UNAVAILABLE并进入业务待核对，不能默认为成功、失败或重新下发。Transport已应用结果仍从任务事实读取；已保存整体确定闭合证据的前序可从本业务待核集合移除，不因可靠消息清理倒退；整批仍UNKNOWN的成员投影须保留重新核对最新版本的依据，新到矛盾事实也不能被历史消费标记吞掉。尚未保存且已丢失的拒绝证据不能伪造恢复；本次不新增无界结果档案或改变可靠层清理策略。

Task 0契约在Task 5的结果能力接线前确定，最小接口及隔离验收必须在任一真实业务开始依赖这些结果编排前完成。Task 1的字段清理、Tasks 2–4及Task 5中不消费结果的交叉校验／上下文步骤不依赖其运行结果，可独立推进；Task 6整合验收必须包含Task 0读接口，不允许测试业务绕过平台直接读表。

## 11. 配置变化与历史执行

业务记录保存工作线原 ID／编码快照、business_code、原触发身份和必要配置依据；动作关联原 operation_id、command_code 或 Transport。历史记录不得级联依赖可删除的当前配置；删除／重建同编码工作线或设备不能让旧结果归入新执行。

新的执行／新动作检查当前有效配置和业务许可。原结果只进入原执行；需要在旧执行上继续动作时也重新验证许可，不能把原快照当成永久授权。

当前绑定 revision 使用设备和工作线管理版本。新 business_code 必须纳入同一管理 version 递增／审计路径，不另建一套绕过发送门禁的版本。普通资料编辑也可能暂停原正式命令后续发送，这是现有行为；本设计不悄悄改为“仅角色变更才停发”。

解绑、改址、删除和配置变化不证明设备取消。符合原派发依据及冻结附录的迟到 Result 可以留存／结算，但能否推进业务由原执行的消费规则判断。UNKNOWN／HELD 不转成默认业务失败，不通过新动作键自动重放。

当前 ECS 事件没有完整的本次过线／绑定代次标识。设备换线后的首次迟到事件，不能仅凭当前归属认定为新作业；这必须由具体事件合同、因果关联或待核对路径解决。已有收据先去重，未绑定事件的持久 ACK 不因后来绑定而自动回放。

首期不改变当前配置删除的基本语义：绑定设备先解绑，空工作线可删除。业务运行记录独立保留，不新增针对历史执行的 CASCADE；新动作因配置缺失而不能继续创建。若业务需要额外删除限制，必须另明确评审，不能借本方案默默禁止所有有历史记录的删除。

## 12. manual-picking 示例

业务编码示例 manual_picking，工作线 SORT-01。以下流程以此前核对的 wes_backend 合同为参考，不是本系统已实现能力。

| 阶段 | 业务子包的行为 |
| --- | --- |
| 单据受理 | 接收 outbound.picking_task.issued@v1，核对工作线业务，保存 task_id、business_type、初始队列事实 |
| 准备 | 按规则选择任务，可靠发送 outbound.picking_task.prepare@v1；PREPARE_ACCEPTED 不等于计划就绪 |
| 计划 | 接收 outbound.picking_task.plan_delta@v1，按修订应用，不重复执行旧计划 |
| 货架／供箱 | 创建原货架搬运，按准确到位事实请求 outbound.bin.inbound_batch@v1，再按冻结分配分段搬箱 |
| 扫码／人工 | SCAN1 核对入口；SCAN2 请求 outbound.manual_bin.work_admission_decide@v1；WORK_REQUIRED 等匹配完成决定，NO_WORK 正常直通，WAIT 保持等待 |
| 回程 | SCAN3／SCAN4 建立本次回程；首次 SCAN4 排序，匹配放行 SUCCESS 后取得退箱资格 |
| 回箱／换面／离场 | return_batch 分配目标；按当前架面运动前求值、直接依赖和 departure_decide 推进 |
| 完成 | 按各业务责任判断，不能以某个 ACK 或一次供箱结束代表整单完成 |

本例至少需要任务／计划、货架服务、料箱经过、回程等协作状态；不要压成一个“当前第几步”。供箱完成不等所有箱回架，退箱不要求原架原面；全线 FIFO 不被某架私有化。Return 因搬运 ACCEPTED 完成交接，也不证明箱已物理放回；直接依赖仍约束换面／离场。

PDA 直接取料架是另一条并行路径，当前合同不接收滚筒线回箱。平台不新增分拣分支；相关规则留在本子包。角色如 MANUAL_SCAN1～MANUAL_SCAN4，位置根据实际合同声明，是否必需按人工线有效需求逐项确认，不从旧示例补默认值。

## 13. 粗分机示例

业务编码示例 coarse_sorting，工作线 COARSE-01，角色示例 COARSE_CONVEYOR、COARSE_ARM。若读取和分流由同一设备编码完成，使用一个角色声明其事件和多种命令，不重复绑定同一设备。

| 阶段 | 业务子包的行为 |
| --- | --- |
| 初始触发 | 接收本次物料读取，核对工作线业务及本次经过身份，保存执行 |
| 验证／绑定 | 向 WMS 请求物料验证和 GRN 绑定，保存原请求身份 |
| 分流 | 明确 OK 创建 MOVE_FORWARD；明确 NG 创建 MOVE_NG；未知／非法响应不猜 NG |
| 流水线结果 | 匹配 MOVE_FORWARD 成功后才申请储位；ACK 不够 |
| 分配 | 保存 WMS 返回的有效储位和原分配身份；等待分配不重做验证或流水线放行 |
| 机械臂 | 按有效分配和当前许可创建搬运命令，消费原执行结果 |
| 报告 | 向 WMS 报告本次结果；动作成功、报告待交付、业务闭合分别表达 |

机械臂成功但报告失败时只履行原报告义务，不重新搬运。MOVE_NG 成功后是否报告及如何闭合尚待合同确认。验证／GRN 绑定、分配、报告的 operation、字段、异常及超时尚未确认，本文不发明真实 operation 或可直接发送的 Payload。

本次过线身份、分配等待、绑定副作用、NG 和各类失败处置由本业务定义。机械臂或流水线未知结果不自动补偿；只有明确业务合同才允许解绑／释放等新动作。

## 14. 应用如何装配：一次接线，各处复用

### 14.1 静态装配与运行时资源分开

1. 所有进程从同一份显式清单取得定义。声明只构造元数据，不访问 DB／网络。
2. 先合并内置 integrations 与业务清单的正式入口路径，完成全部 request／callback／ecs_event 正式注册；之后执行 §6.5 交叉校验。来源保持指向真实 handler／模型；BusinessDefinition 不保存加载路径。
3. 交叉校验成功后才进入 local 入口阶段（仅 local），再使用已校验的 CommandBinding、DeviceAnnex 构造执行通道；任何失败都不能被缓存为装配成功。
4. 按当前模式创建一份 DeviceRoutes 和 ECS 服务。local 用 configure_local_ecs 组合正式与调试；非 local 用 configure_ecs。不能在两处重复配置。
5. 原 reliable_lifespan 提供可用 Outbox 后，注入同一 ECS 服务及 WmsRequests(outbox) 等能力，再调用各业务 factory。
6. 将构造好的 BusinessRuntime 放入应用状态并传给入口上下文。HTTP 对外服务前必须完成；独立业务应用也可显式装配，不强制导入 app.main。

业务目录选项和 schema 可在没有运行中 Sender 时读取定义；不能为了列出业务选项就创建网络客户端。worker 由自己的启动代码装配所需运行时，不共享 API 进程的内存、session 或跨事件循环 HTTP 客户端。

### 14.2 发现与来源：需要补的适配

现有三类 discovery 只认 app.integrations 下的路径，registration.py 的文件名及 local_only 保护也只覆盖这个根。本方案需要：

- 原内置 integrations 的发现范围保持；先与业务清单固定路径合并，再按正式／local 分期。不能先执行包含 dev 的完整内置发现循环，再追加业务正式入口；不是扫描整个 businesses。
- 必须声明的业务入口不存在时启动失败；不能沿用“可选模块不存在就跳过”的行为静默缺失。空类型不声明入口即可。
- registration.py 对 app.businesses/<业务> 的相同入口布局执行同样文件名／local_only 检查；保留真实源码来源，不额外复制装饰器。
- API、worker、WMS 请求惰性 lookup、Mock 与独立测试都使用相同加载路径，不靠 import app.main 才能找到请求合同。
- 正式入口全局先加载，而不是每个包或每类注册器内部各自先正式后 dev；任何 dev 模块及 dev 控制台首次加载前，必须完成三类正式入口的加载与业务交叉校验。dev 入口只在 local 加载。
- catalog 模块不导入 main、控制器或运行时服务；definition 不导入事件入口。入口在加载阶段独立导入，避免循环注册。

业务 initial_entries 的匹配规则及失败时点见 §6.5；后续事件不是新的初始入口，也不在清单另建一套可执行路由。

统一加载协调函数放在 app/integrations/discovery.py，复用三类发现器的候选路径和原 import_registration_module，不新增注册表。load_formal_modules(kind=None) 只加载正式项，kind 可限定为 request、callback、ecs_event；load_local_modules(*, environment, kind=None) 仅在 local 加载原本允许的 local 项。二者是元数据加载函数，不装配业务服务。完整应用由 main 在 include_wms_events／include_ecs_callback／include_dev_routers 之前先调用全量正式加载及交叉校验；这些 include 函数不再通过旧的混合发现循环提前加载 dev。独立业务应用也必须先显式完成同一元数据序列再挂载入口；只读请求目录查询不等于完整业务应用已装配。

当前 import_registration_module(local_only=False) 使用 nullcontext，仍继承外层 local_registration。后续应新增仅由受信正式发现协调器使用的 formal_registration() 作用域：进入时把注册标记显式设为 False，退出或抛错时还原原标记。不得把通用 import_registration_module 的 False 改成随意清除外层标记，否则 dev 普通导入链可以绕过保护。

独立 WMS requests.operations()／lookup() 的冷启动调用委托同一协调器的 load_formal_modules(kind="request")；即使调用方位于 local_registration 内，所发现正式请求仍记录 local_only=False，退出后调用方仍处于原 local 作用域。该路径只加载正式 request，不顺带加载 callback、ecs_event、dev、main 或运行时；直接在 dev 模块中普通 import 尚未加载的正式入口仍应失败。正式入口导入时不得发起依赖完整注册目录的查询；发现复入须明确失败，不能把部分注册结果作为完整目录返回。

每个进程只将完整成功的加载标记为完成，导入失败原样上抛，不对外返回半注册目录；配置／代码修复后的失败场景在干净进程验证，不承诺原地修复所有已导入模块。平台目录、独立请求发现与完整业务启动使用同一固定候选来源，但纯声明查询和模型加载不得触发上述入口加载或 factory。

### 14.3 模型、迁移与分层

业务 ORM 模型在子包内，复用同一 Base。保留现有 app.models 平台导出；迁移环境在读取 Base.metadata 前，通过显式业务模型加载函数导入全部受管理业务模型，测试 schema 建立前执行相同加载。

模型加载不导入 services／factory／事件入口，不创建 Sender，不依赖某条线是否已选择业务。否则暂停或未配置业务会让 autogenerate 误判表应删除。

仍在 migrations/versions 使用一条 Alembic head，业务负责其迁移内容，应用统一发布；有数据的回退不得直接丢业务历史。不在应用启动 create_all，也不为每个业务建立独立 alembic_version。

分层测试覆盖 businesses 下的入口→services→models；通用 http_client、reliable、core、integrations 机制不得反向导入具体业务包。仅应用装配、业务发现适配、迁移／测试模型加载可以引用显式清单；平台 workline 服务通过注入的定义查询／配置校验使用业务声明，不能直接 import coarse_sorting。

## 15. 普通开发人员新增业务的步骤

1. **先写合同与边界**：初始触发是什么、业务身份是什么、完成给谁看；确认涉及的 ECS／WMS 字段。未确认协议不写可发送的正式注册项。
2. **建立子包**：按第 5 节最小目录建立，例如 app/businesses/my_business；不要复制通用 Sender、收据或设备目录。
3. **编写 definition.py**：用 DeviceRole／PositionSlot 对象定义角色、名称和 required，使用 tuple 汇总到 BusinessDefinition；角色 key 与实际设备码分开。初始入口引用同一角色 key／协议名常量。
4. **编写严格合同**：contracts.py 定义参数、结果、错误及超时；command_contracts.py 用角色 .role_key 组装 CommandBinding、DeviceAnnex。相同模型不在多个文件复制。
5. **编写编排服务和状态模型**：用具体业务方法表达受理和推进；保存原执行／动作身份、等待原因和消费进度。
6. **编写薄入口**：在 ecs_event、request、callback 声明已确认入口；处理器复用当前事务，调用子包服务。
7. **编写 composition.py 和 factory**：引用同一 DEFINITION、合同及固定 ENTRY_MODULES；factory 接收所需依赖，不保存当前作业。不要把它们塞回 definition。
8. **登记到显式清单**：应用启动汇总声明／合同，API 与 worker 使用同一清单，模型加载函数覆盖新模型。
9. **加入迁移和测试**：沿原迁移链增加业务表；先用 fake 能力验证规则和推进，再验证协议、事务与真实数据库并发。
10. **创建两条测试线复用本业务**：绑定不同设备和位置，验证同一 handler 不串线；配置不完整不开始作业。

完成这些步骤不自动代表现场验收。真实发送仍要求供应商合同及现场接入确认；本地测试只使用受控 Mock。

## 16. 必须验证的行为

| 场景 | 期望 |
| --- | --- |
| 新业务加入清单 | 管理列表出现；未知 code、重复 code 明确失败 |
| 纯声明及引用完整性 | 独立导入 definition 不加载服务／ORM／网络；对象不可变，重复 key、未知角色引用明确失败 |
| 实际合同交叉校验 | 未声明命令／事件角色、未注册或错角色／错来源的初始入口、缺附录、重复命令键、附录冲突均在资源装配前失败；相同合同可复用，历史附录不增加新命令权限 |
| 创建／配置工作线 | 新建必须选业务；已有 null 不猜测；缺少角色可暂存但不能执行；误角色或跨线设备拒绝 |
| 多线共用业务 | 相同角色 handler 使用各线绑定，命令和位置不串线 |
| 两业务共用事件名称 | 不同角色正确选择各自严格 Payload／handler |
| 单据指定错误业务线 | 明确拒绝，不自动换线 |
| 清理 is_debug 与历史查重 | 新身份不含该字段；新信封与上下文无此参数；兼容额外字段，历史摘要重报不产生新执行，历史记录不改写 |
| 消息重放与同物料再次经过 | 原消息不重复执行；明确的新经过能独立建立执行 |
| 出站结果读模型 | 原身份查询、不按当前target改选；NOT_FOUND／INVALID与等待分开；只暴露已验证响应及已应用结果，不消费last响应或pending内容 |
| 前序闭合矩阵 | §10.4逐行验证；消息DONE／超时／EXPIRED不推导物理完成；明确拒绝闭合，UNKNOWN可收敛，消息清理不抹掉已应用终态 |
| 前序范围与完整性 | 明确ID＋caller／cycle＋整架或料箱；无默认历史扫描，旧轮及独立任务不自动阻塞；缺ID／错范围不伪装零未闭合，超限不截断 |
| 只读与结果恢复 | 不写、不隐式flush、不锁框架行；同一语句快照；多批／逐个／跨轮合并判断在首条SQL前建立同一REPEATABLE READ事务快照，禁止READ COMMITTED拼接；新事务见最新版本，异常后重读全组；消费前崩溃可重读，消费后崩溃不重复意图；保留期后未知不重新下发 |
| 历史位置里程碑 | TARGET_PLACED历史保留后，更高已应用版本将相关成员改为UNKNOWN，get_result及前序成员投影仍为UNDETERMINED，历史member_facts不覆盖新事实／pending、不授权新意图；原回调前置条件保持 |
| 受理与独立推进 | 真实ECS／WMS入口先提交收据、受理状态及持久待办；处理器不升级隔离／另开推进事务；提交后由新session在首条SQL前配置REPEATABLE READ，推进失败不撤销原ACK，丢唤醒可恢复 |
| 同事务推进与重试 | 独立推进的业务更新、消费标记、下一步Outbox意图全部提交或回滚；事务入口处理序列化失败，新session重读全组，原动作身份不变；重试耗尽仍可按业务规则恢复，无额外物理发送 |
| 合法成员投影并发 | 两个整批始终UNKNOWN的任务，通过合法高版本回调改变相关成员；101条分批／跨轮逐条／混合查询断言完整ID、逐项revision、成员closure及整批task_closure；同快照精确读旧版本，新事务读新版本，另有全闭合正向验收 |
| 崩溃／丢唤醒 | 持久待办可恢复，重复唤醒不重复发动作 |
| 配置变更／删除重登记 | 原发送门禁保留；旧结果归原执行，新动作不自动接管旧身份 |
| 合同升级 | 原附录仍可解析所需历史结果；新命令使用已确认新合同 |
| 框架回归 | WMS STATIC、local dev v3、唯一 Sender、UNKNOWN／HELD 和管理权限保持 |
| 新目录适配 | 缺入口、错文件名、local 误标、缺模型、反向依赖都能被检查发现 |
| 正式入口冷启动 | 清空模块／注册／成功缓存的隔离进程中，全量正式入口先于任何 dev；operations 和 lookup 在各自独立冷启动进程的 local 作用域首次发现真实正式 request 成功且来源仍正式，退出／异常后作用域还原；dev 直接普通导入正式入口仍失败 |

测试规则和服务时注入 fake；单次网络／协议用 loopback Mock；行锁、并发领取、幂等赢家及原子消费用显式独立 PostgreSQL／Redis。SQLite 不能证明真实行锁。文档示例和测试默认不连接真实 WMS／ECS。

## 17. 后续实现范围与待确认项

后续实现先完成Task 0出站结果读模型、闭合矩阵及最小查询接口，再进入依赖结果的业务编排。另需涉及：清理 ECS is_debug 参数及保持历史查重；业务目录／装配；WorkLine 模型、迁移、schema、管理服务／审计及选项 API；三类 discovery 和 registration 来源／文件名保护；入口 runtime 注入；模型加载与分层测试；具体业务的合同、状态、编排和结果消费。现有角色解析／设备路由／Sender 优先复用，不再建设平行机制。

本设计建议并待审阅的选择：app/businesses 目录；新建 business_code 必选、旧数据 null 且只允许初次指定；管理业务选项 API；小型定义及显式清单；入口上下文注入受限 BusinessRuntime。不是对现有代码已具备这些接口的声明。

仍待具体业务合同确认：粗分本次经过身份、WMS operation 和字段、NG／失败／资源等待闭合规则；manual-picking 的目标合同版本和本系统移植范围；无法判定归属的迟到首次设备事件；正式位置编码及严格命令附录。它们不阻止确认组织方式，但阻止把占位示例当成可执行合同。

不在本设计中扩展：在线插件、热切换业务、通用步骤 DSL、自动补偿、设备状态轮询、通用物理占用平台和新消息中间件。

设计检查记录：首版独立工程审阅提出事务锁序与稳定拒绝反馈两处缺口，已在 §9.2、§10.1 补齐并经复核。后续参考 WES 两个插件的 definition.py 和 SDK definition 类型，收敛为纯不可变定义、独立合同与 composition；检查范围和结果见本次交付说明。示例不是产品实现；审阅不证明并发无死锁、功能已实现或现场验收通过，也不代替用户批准。

声明优化复核：独立审阅未发现重要架构缺口；按建议删除重复工厂片段，统一引用含函数内服务导入的示例，防止清单读取反向加载业务运行实现。已检查 7 段 Python 示例语法，并自检纯声明及两个业务定义示例的不可变性与入口角色引用；尚未实现或验证真实模块的独立导入。

## 18. 工程审查记录

审查目标：本文；审查方式：plan-eng-review；范围限设计，不开发产品代码。

### 范围决定

- feature answers：未提出功能裁剪，保留原设计范围。
- structure：D1 / A，用户回复“A”；采用 Original arrangement。
- accepted scope：保留 definitions.py、catalog.py、runtime.py；各业务保留 definition.py、composition.py、contracts.py、command_contracts.py，以及按规模组织的 services、models 和实际需要的协议入口。保留两种触发、工作线业务选择、角色绑定、业务编排和既定合同、安全、错误及测试要求。
- pending remedies：none；本次范围决定不批准后续工程修正。

本范围记录仅批准文件组织；后续工程决定见下方账本，完整审查结论见文末。

### Scope Challenge C

No issues found：已有角色解析、严格合同、Outbox、工作线管理与构建发布路径可复用；业务目录属于应用内新增源码，不需要新制品安装渠道。文件组织已由 D1/A 确认，不提出功能裁剪或另一套通信机制。

## Decision ledger

### R1：清理 ECS is_debug 参数（已决定）

Finding：1 / [P1] / confidence 8/10；原问题为未来正式入口对 is_debug 的语义未定义。用户明确改变前提，取消 WES 调试参数，原 A/B 均不采用。

Plan baseline：D1/A 保留原文件组织；原平台包含 is_debug 且参与摘要。新决定以本次用户原话为准。
Runtime evidence：envelope.py:27 声明字段；receiver.py:101 把它计入身份、:180 注入上下文；test_receiver.py:88 验证 true 改变身份。当前无本文正式业务实现；不能声称字段已经清除。

Comparison grid（实际答复后的范围核对）：

| 项目 | 原提案 | 用户实际决定及必要兼容工作 |
| --- | --- | --- |
| WES 对调试状态的责任 | D2 提议选择拦截或继续执行 | 删除 WES 参数与业务调试分支，由 ECS 决定自身 debug 状态 |
| 信封、上下文、OpenAPI、文档和测试 | 有 is_debug | 后续统一清理；旧来件的额外字段按现有 extra=ignore 忽略 |
| 消息身份 | 包含 is_debug | 新摘要不含该字段；旧 false/true 摘要仅用于历史查重，不改历史原身份 |
| 回放、并发、历史执行 | 原 §9–11 合同 | 同一来件不因升级重复受理；不合并历史执行；新并发身份仅一次 |
| 本轮范围 | 只改设计 | 只修改本文，清理产品代码／测试和升级操作均尚未执行 |
| 其他合同、配置、local 联调 | 原范围 | 保留；本决定不扩展或放宽这些合同 |

State: approved
Actual answer: 用户在 D2 后直接回复“可以清理 is_debug 参数，由 ECS 自行决定 debug 状态”；不是 A/B 选择。
Accepted scope: 本文 §9.3 和 §16 增加字段清理、职责归属和必要历史查重验收；不增加业务调试门禁。本轮不改产品代码、不迁移、不部署。

#### History：D2 原提案（未采纳，保留审查轨迹）

### R1：正式业务如何处理 ECS 调试事件

Finding：1 / [P1] / confidence 8/10 / 本文:377–390 / reviewer: Codex。拟议正式入口未规定 is_debug 的执行语义，不是已观察到的产品误动作。

Plan baseline：原提案 §9.2–9.3 只规定角色与业务校验；示例 `await service.accept_material_scan(`（原第385行）未传入或检查 is_debug。D1/A 仅批准原文件组织，没有批准本项处理政策。

Runtime evidence：app/integrations/ecs_callback/envelope.py:27 为 `is_debug: bool = False`；receiver.py:135 为 `registered = lookup(binding.device_role, event_type) if binding else None`，:180 将 `is_debug=envelope.is_debug` 传入上下文，:184 为 `await registered.func(ctx, data)`。tests/integrations/ecs_callback/test_receiver.py:88 证明 true/false 的消息身份不同。原 ECS 接收设计 §2 将 is_debug 触发行为明确留给具体事件业务；当前尚无本文两种正式业务。现有平台不会自动阻止调试事件推进未来业务。

Comparison grid：

| 决策项 | 当前值与批准依据 | A | B |
| --- | --- | --- | --- |
| 正式业务对 is_debug=true 的处理 | 未规定；本项 pending | 只接收和诊断，不创建或推进正式执行，不登记外部意图 | 按普通事件受理与推进，可登记真实外部意图 |
| 调整层与范围 | D1/A 保留原目录 | 两个业务的入口／受理边界；不改通用接收器 | 两个业务明确记载透传语义；不改通用接收器 |
| 协议、消息身份、查重与 local 联调 | 原 §9.2、§14 保留既有合同 | 保留 | 保留 |
| 普通事件及其业务身份、配置和动作规则 | 原 §9–11 | 保留 | 保留 |
| 必要验收 | 原 §16 要求协议、重复、事务与框架回归 | 调试正常接收、受控诊断、零正式推进／外部意图、重报不执行；普通事件正常 | 调试与普通事件按相同业务规则推进；重复身份不重复动作 |
| 本次实施范围 | 仅设计，用户历次明确要求 | 修改本文；不实现代码 | 修改本文；不实现代码 |
| 其他待确认合同 | 原 §17 | 仍待确认，不随本项批准 | 仍待确认，不随本项批准 |

Question D2:
**D2 — 调试事件能否推进正式业务？**

项目：工作线业务子包设计；分支 `feature/workline-business-design`。请回复 **A 或 B**。<gstack-qid:plan-eng-review-debug-event-policy>

现有 ECS 信封有 `is_debug` 字段，但接收器只按“角色＋事件名”选择处理器。设计文档第 377～390 行的粗分示例直接调用业务受理，没有说明调试事件如何处理。接上真实业务后，一条标为调试的扫码也可能进入 WMS 验证、放行和机械臂流程；这是拟议实现的风险，当前尚无这些正式业务。

**建议 A：正式业务入口只接收并记录调试事件，不推进正式作业。** 规则放在业务入口／受理边界，继续复用通用接收器，保持原协议、收据身份、重复消息响应及 local 联调机制。

两项决定的是行为，功能覆盖相同，不进行完整度评分。此次只修改设计；相应行为测试作为后续实现要求。

**A）仅接收调试事件（recommended）**  
✅ manual-picking 和粗分的正式 ECS 入口遇到 `is_debug=true`，保留原收据与 ACK，记录受控原因码，不创建或推进业务执行，也不登记 WMS、Transport、ECS 外部意图。✅ 补充正常事件、调试事件和调试事件重报的验收要求，普通开发人员可以照此编写新的正式业务。❌ 现场若希望调试事件驱动设备，需要另外明确受控联调合同，不能直接使用这两个正式业务入口；完善本文预计人工约 20 分钟／CC＋gstack 约 5 分钟，不含实现。

**B）允许推进正式业务**  
✅ 明确 `is_debug` 在这两个正式业务中仅作记录，调试事件与普通事件均可受理、推进，并按正常业务规则登记真实外部意图。✅ 无需增加调试拦截，仍要求通过相同业务身份、配置和动作幂等规则，后续测试验证这一行为。❌ 对端发送调试扫码时也可能触发真实 WMS 交互与设备动作，接入合同必须明确接受这一后果；完善本文预计人工约 20 分钟／CC＋gstack 约 5 分钟，不含实现。

**Net：A 将调试来件止于接收记录，B 允许它进入正式执行；我建议 A。**

Header: 调试事件规则
Options:
A) 仅接收调试事件
✅ manual-picking 和粗分的正式 ECS 入口遇到 `is_debug=true`，保留原收据与 ACK，记录受控原因码，不创建或推进业务执行，也不登记 WMS、Transport、ECS 外部意图。✅ 补充正常事件、调试事件和调试事件重报的验收要求，普通开发人员可以照此编写新的正式业务。❌ 现场若希望调试事件驱动设备，需要另外明确受控联调合同，不能直接使用这两个正式业务入口；完善本文预计人工约 20 分钟／CC＋gstack 约 5 分钟，不含实现。
B) 允许推进正式业务
✅ 明确 `is_debug` 在这两个正式业务中仅作记录，调试事件与普通事件均可受理、推进，并按正常业务规则登记真实外部意图。✅ 无需增加调试拦截，仍要求通过相同业务身份、配置和动作幂等规则，后续测试验证这一行为。❌ 对端发送调试扫码时也可能触发真实 WMS 交互与设备动作，接入合同必须明确接受这一后果；完善本文预计人工约 20 分钟／CC＋gstack 约 5 分钟，不含实现。

原状态：pending；原问题未选择 A/B，已被用户直接提出的新方案取代。


### 1. Architecture review

发现 1 项（R1，拟议业务入口的 is_debug 语义，P1 / confidence 8/10），已按用户直接决定改为“WES 清理参数，ECS 管理调试状态”，必要历史查重兼容已进入 §9.3。其余未发现需要新增机制的架构问题。既有配置解析和设备路由提供锁与冻结授权；业务保存自身执行和消费进度；框架保存投递及结果证据；结果提交后业务再推进。不能从“设计有锁序”推断真实 PostgreSQL 并发已验证。

### 2. Code Quality review

No issues found（设计层）：沿 D1/A 保留纯声明、合同、装配、编排与模型的明确边界。现有 registration_filename／import_registration_module、CommandBinding／DeviceAnnex、resolve_binding／resolve_role 均可直接复用，不建议另抽象插件基类、第二套注册器或通用 execute。app/businesses 尚不存在，其入口、模型加载及依赖边界均为拟议改动，不能按当前目录检查通过来声称已支持。

### 3. Test review

框架：按 AGENTS.md 使用 pytest、pytest-asyncio、HTTPX；并发证明需要显式独立 PostgreSQL／Redis。以下是未来实现的验收追踪图；PLANNED 代表本文已有要求但产品尚未实现，BASE TEST 代表已读取的平台测试，不代表本轮已运行或覆盖新业务。

```text
管理用户                       新目录／应用启动
  创建／更新工作线 [PLANNED]       definitions → catalog → composition [PLANNED]
  ├─未知/空业务拒绝               ├─非法/重复声明/缺入口/缺附录 → 启动失败
  ├─旧null首次选择/禁止换业务       ├─formal先于local；非local无dev注册
  ├─部分绑定暂存/执行前重验         ├─definition不加载ORM/服务；工厂无作业状态
  └─权限/版本冲突/审计 [→E2E]       └─API/worker/独立请求/迁移同一清单
         |
单据入口 [PLANNED →E2E]          ECS入口 [BASE TEST + PLANNED →E2E]
  WMS → 信封/收据 → issued        信封/新摘要 + 旧摘要查重
  ├─错业务线→明确拒绝              ├─历史已收/重报→原ACK，不调用业务
  ├─同任务→原执行，计划按修订        ├─非法信封/Payload→400，无业务写入
  └─后续通知→原任务归属            ├─绑定/数据库临时失败→回滚，503
                                 ├─未知绑定/稳定不适用→诊断ACK，无执行
                                 └─有效角色+本线业务+本次经过→受理
                                             |
业务执行 [PLANNED →E2E] ←─────────────────────────┘
  锁配置→锁执行→核对原动作/结果/消费进度
  ├─已消费→不重复动作
  ├─不满足当前许可/配置→保存等待依据，不创建动作
  └─业务状态 + 消费进度 + 下一步Outbox 同事务提交/回滚
       |
  框架Sender → 外部系统 → 原结果留存/结算 [BASE TEST]
  ├─UNKNOWN/HELD→不新键重发、不猜成功/失败
  └─提交后唤醒或持久待办扫描→业务独立事务推进 [PLANNED]
       ├─崩溃/丢唤醒/重复唤醒→恢复且不重复意图
       ├─配置变更/同编码重建→原结果仍归原执行
       └─报告待投递≠重新物理执行

具体业务 [PLANNED]
  manual：issued→prepare→plan；货架/供箱/人工/回程并行协作→完成规则
  coarse：读码→验证绑定→OK/NG分流→FORWARD成功→储位→机械臂→报告
  边界：NG/未知/等待/失败/迟到、本次经过与消息身份分开、两条线不串线
```

现有平台测试证据：test_roles.py 覆盖按角色分发、先查重、绑定后不回放、临时失败回滚；test_receiver.py 覆盖非法输入、诊断、处理器回滚及当前旧身份规则；test_role_delivery.py 覆盖配置与发送门禁并发（需要 PostgreSQL）和 loopback；test_registration_discovery.py 覆盖正式／local 注册边界；test_workline_bindings.py 覆盖迁移保留数据。它们不能证明业务工厂、business_code、完整业务流程或新身份兼容已经实现。

| 后续验收组（均未实现） | 拟落点／复用 | 必须断言 |
| --- | --- | --- |
| 1 纯定义与清单 | tests/businesses/test_definitions.py、test_catalog.py | 不可变、非法类型/重复key/引用/重复code失败；纯导入不加载服务、ORM或网络 |
| 2 合同装配 | tests/businesses/test_composition.py | 角色属于本定义、命令缺附录/冲突失败，工厂只构造，必需runtime缺失启动失败 |
| 3 发现与来源 | 原 tests/integrations/test_registration_discovery.py | 新根固定路径加载；缺模块失败；文件名检查；正式先加载；local链不能误标正式 |
| 4 管理与迁移 | 原 tests/controllers/test_device_management_flow.py、tests/migrations/ | 必选业务、旧null只明确选择一次、读取过滤审计、原权限、历史保留及单一迁移head |
| 5 绑定与多线 | tests/businesses/test_workline_bindings.py | 缺项可存不可执行；误角色/跨线/重复设备拒绝；同业务两线隔离 |
| 6 WMS单据 | tests/businesses/manual_picking/test_issued.py | 错线拒绝、重复原任务不重建、后续计划依原执行且修订不倒退 |
| 7 ECS受理 | tests/businesses/coarse_sorting/test_scan.py | 有效受理；稳定不适用诊断ACK不执行；临时失败503且无收据/业务残留 |
| 8 身份清理 | 原 tests/integrations/ecs_callback/test_envelope.py、test_receiver.py、test_reliability.py、控制器测试 | 无is_debug正式字段；额外字段忽略；三种旧来件重报及双历史收据均不重执；新并发仅一次 |
| 9 原子推进 | tests/businesses/test_execution_transactions.py | 状态、消费标记、意图全部提交或回滚；重复结果仅一次动作 |
| 10 崩溃恢复 | tests/businesses/test_recovery.py | 提交前/后崩溃、丢失/重复唤醒，多次扫描仍关联原动作，不另发未知物理动作 |
| 11 并发与配置 | tests/businesses/test_execution_concurrency.py + 原 role_delivery 测试 | 配置变更/发送门禁/两消费者/唯一键等待交叉执行不死锁、不串线；PostgreSQL证明 |
| 12 历史归属 | tests/businesses/test_execution_history.py | 同码删除重建不接管旧结果；历史不CASCADE；继续动作重新取得许可 |
| 13 manual流程 | tests/businesses/manual_picking/ | prepare ACK非计划；供箱/退箱/换面/离场的直接依赖、回程FIFO、本轮隔离；以确认移植范围为准 |
| 14 coarse流程 | tests/businesses/coarse_sorting/ | 明确OK/NG；FORWARD成功才分配；原分配驱动机械臂；成功后报告失败不重复搬运；未知结果不自动补偿 |

覆盖结论：14 个计划验收组，已有平台部分行为测试；没有新增“实现未存在所以测试缺失”的代码缺陷计数。R1 的清理回归是用户已批准行为的必要证明，其余来自 §16，未新增可选测试深度。当前只有后端设计，无前端交互实现；重试创建、过期版本、权限失效和并发配置以管理API集成用例验证。无 LLM／prompt 功能，因此无 eval 范围。

### 4. Performance review

No issues found（设计层）：定义与合同只在受信装配中加载，管理选项不构造通信客户端；网络不占用业务事务；沿用一份发送运行时。工作线行锁会将同线短事务串行化，本文不承诺吞吐量。未来持久待办扫描需在业务模型落地时落实可索引的待办条件、稳定分批及并发幂等；不能通过重扫全表接管Outbox重试。无产品实现或流量证据，不虚构延迟／容量数字，也不因此新增消息中间件或缓存。


### NOT in scope

- 不引入插件市场、动态安装／卸载、热加载、通用 Saga／步骤 DSL：当前仅应用内业务组织，不需要这些运行成本。
- 不增加通用物理占用平台、设备状态轮询或消息中间件：本次没有相应资源主账或吞吐依据。
- 不实现未知事件自动回放、自动补偿、业务热切换：不改变既有身份、当前许可和明确结果边界。
- 不实现产品代码、迁移、发布、真实调度或现场验收：本轮只审设计；后续开发验收和供应商／现场验收分别记录。

### What already exists

- app/services/device_bindings.py 的 resolve_binding／resolve_role 与 app/services/workline_service.py：复用线→设备锁和版本复核，增加业务适用性及配置校验，不重建设备目录。
- app/integrations/ecs_command/roles.py 和 service.py：复用严格角色合同、冻结路由、get_command／get_by_call，禁止用查询到ACK代替完成。
- app/integrations/registration.py：扩展其明确根与路径检查，不复制装饰器或另建业务可执行路由。
- app/integrations/ecs_callback、wms_callback：原协议、收据和事务机制；runtime 为待增加注入接缝。R1只删除调试参数，不改变这些职责。
- app/integrations/wms_request/service.py 与 app/reliable_runtime.py：共用事务登记、可靠发送与应用生命周期；WMS／Transport正式结果读取仍须补齐，不放进协议无关reliable。
- Dockerfile 已 COPY app／migrations；CI已有 ruff和pytest及独立测试数据库，不需要业务独立安装渠道。本轮没有触发CI或构建新镜像。

### 调研依据与取舍

[Layer 1] 使用 Python 标准库 dataclass 声明及普通显式 import；冻结对象内部使用 tuple，避免“冻结对象内仍含可变容器”。官方参考：https://docs.python.org/3.13/library/dataclasses.html 。

[Layer 2] 使用 FastAPI 现有应用 lifespan 装配运行资源，而非导入声明时创建连接；官方参考：https://fastapi.tiangolo.com/advanced/events/ 。数据库锁采用一致顺序并针对交叉路径实际验证；官方参考：https://www.postgresql.org/docs/13/explicit-locking.html 。已有项目机制足够，没有提出新的第三方依赖。Aside不可用时使用了主环境网页检索作为只读调研替代。

### Failure modes

| 路径 | 现实故障 | 处理与用户可见结果 | 验收依据 |
| --- | --- | --- | --- |
| 清单／启动 | 少导入业务入口或模型 | 必需项启动失败，不能管理选项存在而执行器缺失；迁移模型显式加载 | §16、验收组1–3及迁移组4 |
| 工作线配置 | 两个管理员并发编辑或绑定别线设备 | 原版本冲突与权限机制；错误配置明确拒绝，缺项可存但不能执行 | 验收组4–5 |
| WMS单据 | 单据指定错误业务线或旧计划重报 | 明确Rejected／Conflict或原执行幂等，不能自动换线 | 验收组6 |
| ECS受理 | 配置缺项或数据库故障 | 稳定问题持久诊断ACK不执行；临时错误回滚503，可重报 | 验收组7 |
| 身份清理 | 历史已ACK来件在升级后换摘要 | 查旧摘要保持原ACK，不回放、不改写历史；接收器算法统一升级 | R1、验收组8 |
| 事务推进 | 进程在登记下一步时崩溃 | 同事务全部提交或回滚；持久待办恢复，不靠单次唤醒 | 验收组9–10 |
| 结果消费 | 重复结果、配置已变或设备执行结果未知 | 原执行消费一次；新动作重验许可；UNKNOWN／HELD保留，不新键重发 | 验收组11–12 |
| manual | 供箱已结束但退箱／换面直接依赖未满足 | 独立协作状态等待，不误判整单完成；具体条件沿确认合同 | 验收组13 |
| coarse | 机械臂已成功但报告丢失 | 只履行原报告，不重新搬运；报告待交付与物理完成分开 | 验收组14 |

未发现同时“无错误处理、无计划验收且会静默失败”的设计路径，critical gaps=0。这里的测试均为计划要求，不能替代产品验证。复杂业务服务及其状态模型实施时应在各子包内维护与实际代码一致的流程／状态ASCII图；平台文件不塞入具体业务流程。

### Worktree parallelization strategy（仅后续实施建议，不启动代理或工作树）

| Step | Modules touched | Depends on |
| --- | --- | --- |
| S0 字段清理与收据兼容 | integrations/ecs_callback、controllers、tests/integrations | — |
| S1 业务公共接缝与管理集成 | businesses公共层、integrations发现／上下文、services、schemas、models、controllers、migrations、tests | S0；共享入口文件串行整合 |
| S2 manual业务 | businesses/manual_picking、tests/businesses/manual_picking | S1；本业务合同与移植范围明确 |
| S3 coarse业务 | businesses/coarse_sorting、tests/businesses/coarse_sorting | S1；本业务过线／操作／结果合同明确 |
| S4 应用整合验收 | main／应用装配、migrations、tests | S2、S3及各自已确认范围 |

先完成共享接缝，再可在独立worktree并行S2/S3；两者合入后做S4。共2条可并行业务通道，S0/S1/S4为3个串行阶段。显式清单、统一迁移head和通用发现／装配是共享冲突点，由整合阶段顺序处理，不让两条业务通道分别改一套平台运行时。没有当前实现授权，也没有启动这些工作。

### Outside Voice

外部提供方 Claude Code CLI 已尝试（--access none，最多300000ms），结果 unavailable：error.code=timeout，消息“Claude Code timed out after 300000ms.”；诊断同时包含 `[claude-code:unrecognized_model] {"model":"glm-5.2","query_source":"sdk"}`。无有效完成文本、无modelUsage；不能推断模型实际完成审阅。进程已退出，exit=1。没有成功的外部意见，不作模型一致性结论。

按技能回退要求核查，当前宿主没有声明 Agent/Plan＋TaskOutput＋TaskStop 的组合能力，不启动其他通用代理替代。因此outside_status=unavailable；该缺失不等于主审失败，也不提供独立复核覆盖。

### Approval readiness

Approval readiness: PASS。已逐项核对D1/A的目录决定，以及R1用户直接答复的字段清理；R1不套用旧A/B选项。未采纳调试门禁、无未答复修正、无新增TODO选择；必要历史查重与测试是字段清理及原“不自动回放”合同的必要证明。本轮仅修改设计，后续实现任务不代表当前实现已获启动授权。

## Implementation Tasks

仅从本轮发现推导，不把原方案全部功能重复列为新发现。

- [ ] **T1（P1，人工约3小时／CC＋gstack约20分钟）** — ECS事件接入 — 清理 WES is_debug，保留历史事件查重。
  - Surfaced by：Architecture R1；用户对D2直接提出字段删除方案；§9.3。
  - Files：app/integrations/ecs_callback/{envelope,registry,receiver,__init__}.py、app/controllers/ecs_callback.py、相关信封／收据／上下文／并发／控制器测试与协议文档。
  - Verify：额外字段不改变新身份；旧省略／false／true及双历史收据重报不再执行；新事件并发一次；历史未注册事件不自动回放。执行 pytest 对应模块，PostgreSQL并发用独立测试库。
  - 本轮只修订设计，未实现。估时含必要兼容回归，不含测试环境搭建、合同确认或部署。

_No new tasks from Code Quality, Test or Performance；原 §16 验收继续作为实现要求。_

QA Test Plan：/Users/kaizhou/.gstack/projects/kaizhoumasha-maintenance-admin/kaizhou-feature-workline-business-design-eng-review-test-plan-20261027-212312.md。
任务JSONL：/Users/kaizhou/.gstack/projects/kaizhoumasha-maintenance-admin/tasks-eng-review-20261027-212405.jsonl。

### 验证与边界

本轮既有机制定向测试：52 passed、26 skipped（禁用 TEST_DATABASE_URL／TEST_REDIS_URL；未连接真实WMS/ECS）。覆盖 ECS角色／收据、注册发现与分层；跳过数据库参数化分支。7段设计Python示例通过AST语法检查。没有实现或执行新的业务、字段清理、迁移、PostgreSQL并发或供应商现场验收；不得据此标记产品可上线。

### TODOS.md

无新增TODO提案；既有结果消费、回放与合同保留期待办仍按原边界处理。R1是本次设计明确的后续实现任务，不再重复登记一份待办。本轮未改TODOS.md。


### Unresolved decisions that may bite you later

本轮待答复设计决定：0。原方案仍有合同事实未确认：粗分本次经过、operation／字段及NG／失败／等待闭合，manual移植范围、位置与命令附录、迟到首次事件归属。它们限制正式业务落地，不得用示例占位注册；本文没有将它们推定为已确认。

### Completion summary

- Step 0: Scope Challenge — scope accepted as-is；D1/A保留原组织，无功能裁剪。
- Architecture Review: 1 issue found（R1）；用户改为清理is_debug，设计已修订，产品任务T1尚未实现。
- Code Quality Review: 0 issues found。
- Test Review: diagram produced；14个未来验收组，0项额外设计测试缺口；新业务尚不存在，不能声称这些场景已通过。
- Performance Review: 0 issues found；未做性能或真实数据库并发验证。
- NOT in scope: written。
- What already exists: written。
- TODOS.md updates: 0 items proposed；复用已有待办，无重复登记。
- Failure modes: 0 critical gaps flagged；均有设计处理和计划验收，尚非实现验证。
- Unresolved decisions: 0 in this review；具体业务合同仍待确认。
- Outside voice: Claude Code CLI unavailable（300秒超时，unrecognized_model诊断）；宿主不具备技能要求的原生回退组合能力，无独立复核信用。
- Parallelization: 2条未来业务lane可并行，3个共享串行阶段；仅建议，未启动。
- Lake Score: N/A（都是职责／组织类别选择，无覆盖深度选择）。
- Current checks: 52 passed、26 skipped；7段Python示例语法检查通过；没有产品改动、迁移、部署或真实发送。

### Suppressed findings

无需要隐藏的低置信度发现。未把明确待确认的供应商合同、尚未实现代码或未实测容量报告为已发生缺陷。

## 19. 实施前评审修订与复核（2026-10-27）

本轮仅按用户批准的两项评审意见修订 SPEC 与 PLAN，不修改产品或测试代码。复核范围为这些修订的设计一致性、当前接口依据和测试要求；没有重新运行完整 gstack 流程或独立外部复核。

| 评审项 | SPEC落点 | PLAN负责人及验收 | 文档复核结论 |
| --- | --- | --- | --- |
| P1：声明与实际合同交叉校验缺少明确步骤 | §6.5、§14.1、§16 | Task 5 的 validate_business_contracts／test_contract_assembly；Task 6 的启动顺序集成用例 | 已明确输入、角色和来源归属、初始入口匹配、命令／附录冲突、资源构造前失败；文档缺口关闭 |
| P2：正式入口冷启动作用域与全局顺序未落实 | §14.2、§16 | Task 3 的协调器／受信formal_registration／test_registration_bootstrap；Task 6 全量顺序 | 已明确跨两个根及三种注册器的正式先行、local内首次request发现、成功／异常还原、直接dev导入保护与复入失败；文档缺口关闭 |

当前代码依据仍是：RoleRoutes不接收业务定义；import_registration_module(False)继承外层local作用域；三类发现和main需按新计划改造。它们没有在本轮被修复。未来新增测试名称和断言均为实施要求，不是已运行通过的测试。上一轮22项既有机制测试通过作为背景证据保留，本轮不以重复运行这些未改的测试冒充新增能力验证。

本次针对修订的自检未发现新的阻断性设计问题。公共接缝和具体业务范围不变：Task 1可独立交付；Task 6仍需真实确认业务包；具体业务合同、结果消费与并发验收尚未完成。暂存区保持原版本，本轮修订只在工作区，未提交、推送或部署。

## 20. Task 0修订与定向复核（2026-10-27）

按用户明确要求，将出站结果消费契约和最小接口由“随首个消费者再定”提升为Task 0。修订范围仅SPEC／PLAN，不开发模块、迁移或测试，不修改TODOS既有记录。当前权威依赖以§10.2–10.6与PLAN Task 0为准；§18–19及文末gstack记录是先前审查背景，不代替本次复核。

| 核对项 | 文档落点 | 对应实施验收 |
| --- | --- | --- |
| 原身份、投递／业务响应分离 | §10.2–10.3 | Task 0 Step 1：原消息ID及operation核对、配置变化不改选、last响应不作决定、PREPARE_ACCEPTED非计划 |
| 已应用结果与闭合矩阵 | §10.3–10.4 | Task 0 Step 2：矩阵逐行；pending／settle DONE不作物理完成，UNKNOWN更高版本可收敛 |
| 前序范围、成员投影与结果完整性 | §10.5 | Task 0 Step 3：明确ID、原caller／cycle、整架、相关成员，错范围不伪装空集合、100条上限不截断、CLOSED项保留证据引用 |
| 只读快照、消费唯一性与保留 | §10.6 | Task 0 Steps 4–6：单语句快照、无隐式写锁／flush、动作内原子消费、保留期边界、多动作不共用全局消费位 |
| 前置依赖没有环 | §10.6及PLAN依赖图 | Task 0的消费恢复测试在tests/integrations独立完成，不等待Task 6或app.businesses；Task 6仅追加运行时整合 |

复核依据为现有WmsRequests.send／EnqueueResult、ReliableMessage与清理规则、TransportTask冻结事实与applied／pending结果、TaskCallbacks、Transport适配器及settle_submitted。现有snapshot生命周期仅服务快照投递，不为本契约改造WMS STATIC路径或另接一套调度。首版用现有唯一身份与有界ID查询，不为读接口新建平台消费表、关联表或结果档案。

本次复核已修正独立验收反向依赖、批次与资源成员闭合混淆、只返回未闭合项导致无法保存闭合证据三处草稿问题；修订后未发现新的阻断性文档问题。这里仅说明契约和任务可执行，新增读接口／测试仍未实现；不声称运行行为、数据库并发、供应商合同或真实业务流程已通过验收。原Task 6真实业务门槛保持。

## 21. 统一快照与历史里程碑修订复核（2026-10-27）

本轮按后续实施前评审补齐§20之后发现的两处缺口，仅修订SPEC与PLAN；下表为本次定向复核，不重记完整gstack审查。

| 评审项 | 修订与实施验收 | 定向复核结论 |
| --- | --- | --- |
| P1：多批／逐个前序读取可拼接不同时点的闭合 | §10.5–10.6、§16；Task 0 Steps 3–6明确首条SQL前由调用方建立REPEATABLE READ、固定完整身份范围、同session／事务汇总，读方法不接管事务 | 超100条、逐个跨轮及混合读取规则一致；旧事务片段不能跨失败／重启汇总，新事务重读最新；文档缺口关闭 |
| P2：member_facts可能被用来覆盖更新UNKNOWN | §10.2、§10.5、§16；Task 0 Steps 2、5要求历史TARGET_PLACED保留而相关成员高版本UNKNOWN仍不可判定，且不生成下一意图 | 历史事件可见性与当前应用结果分开；不改原回调位置前置条件、不建位置台账；文档缺口关闭 |

复核覆盖闭合矩阵、局部成员投影、单语句与跨查询快照、原身份、消费恢复及任务依赖的一致性，未发现新的阻断性文档问题。上述并发与行为测试是后续实施要求，本轮未实现或运行；统一快照不是当前执行授权或物理锁。Task 0仍先于结果接线和真实业务编排，Task 6真实业务门槛不变。

## 22. 事务建立入口与并发验收修订复核（2026-10-27）

本轮按后续评审修订§9.4、§10.1、§10.6及§16，补齐§21规则成立所需的实际入口与验收约束；不开发产品／测试代码，不重记完整gstack审查。

| 评审项 | SPEC与PLAN落点 | 定向复核结论 |
| --- | --- | --- |
| P1：已执行SQL的ctx.session不能再建立统一快照 | §9.4、§10.1、§10.6；Task 0独立事务示例与恢复验收，Task 5仅受理接线，Task 6真实接收→提交→新session推进 | 明确事务拥有者、隔离配置时点、受理与推进各自回滚边界、事务入口整组重试及耗尽恢复责任；不改全局隔离或在受理内另开推进写事务 |
| P2：并发测试可能使用非法终态变化或过弱断言 | §16；Task 0 Step 5合法回调时间线及版本／成员／ID断言，Task 6整合 | A／B始终整体UNKNOWN，以相关成员投影变化复现风险；固定旧／新快照准确版本及全闭合正向对照，不用改列制造已确定终态重开 |

复核确认：member_facts仍为历史里程碑，不覆盖更新已应用UNKNOWN；独立推进入口建立统一快照，业务推进服务与平台读取不接管事务；原框架结果接收、Outbox投递重试和受理协议保持各自职责。Task 0不反向依赖Task 6，不新增平台消费表、通用推进器或虚构业务包。复核另将Task 6旧“收据／消费／意图全部同事务”的断言改为受理与推进各自原子，避免与两阶段边界冲突。修订后的文档未发现新的阻断性问题；新增入口和并发测试仍为实施要求，尚未通过运行验收。

## 23. Task 0拒绝冲突与消费恢复修复复核（2026-10-27）

本轮将提交拒绝与同任务有效位置里程碑的冲突判定落实为UNDETERMINED／INVALID_EVIDENCE，补齐读取整组旧版本且暂存意图后发生真实40001的恢复验收。契约落点为§10.4–10.5；测试侧完整记录两前序版本，失败事务回滚后在新session重读v2／v2，分别验证UNKNOWN不推进与仍闭合仅一次。历史member_facts不授权完成，已应用结果优先及原POSITION_UNKNOWN诊断语义保持。

相关PostgreSQL回归776 passed／4 skipped，全库本地回归1972 passed／702 skipped；跳过不算通过。完成后的独立上下文只读复核及实现自检未发现新的阻断性问题。详细修复证据见PLAN末尾；本记录不是完整gstack重审或真实业务／供应商验收。本轮不实施后续任务、不提交推送，既有SPEC代码示例格式差异未顺带修改。

## GSTACK REVIEW REPORT

以下是首轮 gstack 审查的历史快照，不代表本轮修订后的当前复核结论；前次交叉校验修订见 §19，Task 0初次修订见 §20，统一快照与历史里程碑修订见 §21，事务入口及并发验收修订见 §22，最新Task 0修复复核见 §23；未重记一次完整 gstack 审查。

| Review | Trigger | Why | Runs | Status | Findings |
| --- | --- | --- | --- | --- | --- |
| CEO Review | /plan-ceo-review | Scope & strategy | 0 | 未运行 | 不提供该项覆盖 |
| Outside Review | Claude Code CLI / plan-eng-review | Independent 2nd opinion | 1 | unavailable | 300秒超时；unrecognized_model诊断；无完成结果 |
| Eng Review | /plan-eng-review | Architecture & tests | 1 | ISSUES OPEN (PLAN) | 1项已决定的设计发现，0关键缺口；T1待实现，非产品验收 |
| Design Review | /plan-design-review | UI/UX gaps | 0 | 未运行 | 当前后端设计，无前端实现审查 |
| DX Review | /plan-devex-review | Developer experience gaps | 0 | 未运行 | 未执行独立DX审查 |

**OUTSIDE COVERAGE:** host=codex；provider=claude-code；phase=plan-review；outside_status=unavailable；无独立复核完成证据。

**VERDICT:** 设计主审已完成，R1已按用户决定修订；按工具计数规则issues_found包含已决定发现，故保留ISSUES OPEN，T1和具体业务待后续实现及验收，不能认定产品可上线；eng review required。

NO UNRESOLVED DECISIONS
