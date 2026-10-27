# 新增工作线业务包

业务流程与消息先看[工作线业务 API 索引](workline-business-api.md)：人工分拣为旧系统参考合同，粗分机为待确认草案；
降低新增接入开发门槛的[正式重构设计](../superpowers/specs/2026-10-27-business-development-simplification-design.md)为待评审、未实施的目标方案，列明现有/新增接口、声明与调用示例、兼容迁移、分步顺序及验收。
其中 @ecs_command、compile_commands、模型形式的 WMS send/ECS create 和声明感知角色解析尚不存在；下文仍是现行接入规则。

当前已实现公共声明、发现、合同校验、工作线配置和受限运行时接缝，正式业务清单仍为空。
隔离测试中的两份业务不进入生产清单。没有合同与资源完整的真实业务包前，整体接入交付未完成，
不能发布要求新建工作线必选业务的版本；manual-picking、coarse-sorting 仍需各自的实施计划。

以下按 definition → 严格合同 → composition → 子包 service/model → 薄入口 → catalog →
模型加载 → 两线测试说明最小接入方式。示例中的 `<已确认…>`、`Confirmed…` 都是待替换的标记，
不是可注册、可发送的协议或完整业务。尤其不能用空 Payload、空工厂或测试合同补齐正式清单。
可运行的隔离接缝用例见 `tests/businesses/test_workline_business_flow.py`、
`tests/businesses/test_result_consumption.py`；其中业务表由夹具独立创建、最终删除，不加入生产 Base。

## 1. 纯声明 definition.py

只依赖标准库和 `app.businesses.definitions`。设备角色与实际设备码分开，位置槽与实际位置编码分开；
角色/槽 key 使用大写字母开头的大写字母、数字、下划线，最长100字符。对象不可变，汇总必须为 tuple。
不导入服务、Pydantic、ORM、网络或当前配置，不在声明时构造服务。

```python
from app.businesses.definitions import (
    BusinessDefinition,
    DeviceRole,
    InitialEntry,
    PositionSlot,
)

SCAN = DeviceRole(role_key="SCAN", display_name="扫码设备")
INLET = PositionSlot(slot_key="INLET", display_name="入口")
# 此标记不能通过声明校验；确认实际事件合同后才替换。
SCAN_EVENT = "<已确认事件名>"
DEFINITION = BusinessDefinition(
    code="my_business",
    display_name="业务名称",
    device_roles=(SCAN,),
    position_slots=(INLET,),
    initial_entries=(InitialEntry(kind="ecs_event", name=SCAN_EVENT, device_role=SCAN.role_key),),
)
```

初始 WMS 回调使用 `InitialEntry(kind="wms_callback", name=已确认operation)`，不指定设备角色。
初始入口必须是本业务正式注册；请求 operation 不等于回调入口。不要为展示效果添加未经确认的入口。

## 2. 严格合同 contracts.py / command_contracts.py

先确定入口、业务经过身份、每条请求/结果的完整字段和错误/超时边界，再定义严格模型。
ECS/WMS 入站模型复用 `CallbackPayload`，严格校验类型、默认忽略多余字段；WMS 出站模型复用
`RequestPayload`，严格校验类型并拒绝多余字段。具体业务须按已确认合同确定是否收紧入站字段规则。
ECS 命令参数/结果/错误模型应同样严格；每个 `DeviceAnnex` 明确版本和超时范围。
`command_contracts.py` 通过同一个 `SCAN.role_key` 构造 `CommandBinding`，不复制角色字符串或合同模型。
`from app.integrations.ecs_command import CommandBinding, DeviceAnnex` 只加载声明元数据；
`RoleRoutes`、`EcsCommands` 和 `configure_ecs` 在显式引用时才加载运行设施。
没有确认合同的命令和 operation 不注册。`history_annexes` 仅保留原结果解析能力，不扩大新命令权限。

消息身份用于原收据重放和出站去重；本次业务经过身份用于业务执行/动作和消费进度。保存两者的关联，
原消息重放保持原执行；相同物料明确的新经过使用新的业务经过身份，不能仅按物料码吞掉后续作业。
也不能因等待 UNKNOWN、消息清理或进程重启换身份重发。

## 3. 可信装配 composition.py

固定入口及模型模块路径，引用同一份 `DEFINITION`、命令声明和附录。工厂只构造无作业状态的服务，
不保存活动 session、不访问网络。平台只注入 `Capabilities.ecs / wms / transport`；不新增通用 execute。
真实业务的独立推进入口还需要 session factory，由 composition 的可信工厂明确注入：

```python
from .command_contracts import ANNEXES, COMMAND_BINDINGS
from .definition import DEFINITION

ENTRY_MODULES = ("app.businesses.my_business.ecs_event",)
MODEL_MODULES = ("app.businesses.my_business.models",)


def build_service(capabilities):
    from app.core.database import session_factory

    from .service import MyBusinessService

    return MyBusinessService(capabilities, session_factory=session_factory)
```

按实际合同补充 `callback` / `request` 固定模块；不接受用户输入动态 import。目录读取不会调用工厂。
应用先加载全部正式入口，再交叉校验全部合同，之后才加载 local 入口、构造资源和业务工厂，最后受理请求。
错误业务包使整体启动失败。API 与 worker 读取同一显式清单；非 local 不加载 dev 入口。

## 4. 子包 service.py / models.py

具体业务自己的表保存执行/动作、原设备/工作线/消息/请求/任务身份、持久待办、等待原因和消费证据。
普通模型继承项目 Base，沿唯一 Alembic 链增加迁移；这里不提供通用流程表、补偿器或后台推进 worker。

受理方法只使用 `ctx.session`：解析器已按工作线→设备加锁，再锁业务行。
用 `business_configuration_issue(..., definition_lookup=...)` 校验线与声明；继续核对当前业务许可和合同身份。
稳定拒绝沿原协议返回 Rejected/诊断；临时存储故障向外抛出，由接收器回滚。
原收据、受理状态和待推进依据在受理事务内一起提交。只依赖当前输入、无需前序结果的初始意图，
才可按该业务合同在受理事务登记。处理器不 commit/rollback、不升级隔离级别，也不另开推进写事务。

提交后的通知只携带原 execution_id，可丢失或重复；持久待办扫描复用同一个具体业务推进入口。
该入口创建新的 session，在任意业务定位/锁定 SQL 之前配置本事务 REPEATABLE READ：

```python
async with session_factory() as session, session.begin():
    await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    await service.advance_pending(session, execution_id)
```

`advance_pending` 是该业务的方法，不是平台接口。先无锁定位原执行的配置，再按工作线（ID顺序）→
设备（ID顺序）→业务动作（固定顺序）取锁、重读和校验当前许可。不得在这些锁内改写/settle/hold
既有命令或消息，避免和先锁命令的发送门禁构成反向等待。结果接收事务先提交，之后再独立推进。
网络请求始终在事务外，下一意图复用原 Outbox 和唯一 Sender。

只通过 `WmsRequests.get_result`、`TransportService.get_result` 和
`list_unclosed_predecessors` 按原身份读取结果，不读取 ReliableMessage/TransportTask 列或 response_raw。
每次前序判断固定完整身份集合及 caller/cycle/资源范围；超过100条分批，跨轮逐个或混合读取也在同一
REPEATABLE READ 事务内进行。快照保证事实曾同时成立，不替代当前许可或物理资源门禁。

在同一推进事务内保存业务更新、已消费的原身份/结果版本/成员范围/必要决定，再登记下一意图。
40001 序列化失败必须回滚并关闭失败 session，新 session 重读完整尚需集合及当前许可，整事务有界重试。
其他临时故障同样不提交部分消费；重试耗尽保留原待办/等待原因，供受控恢复，不能无限忙重试。
推进失败不撤销已提交受理，也不改原 ACK；Outbox 负责提交后的投递重试。

结果保留沿用原期限。消费后保存的整体确定决定可跨重启/消息清理继续使用；尚未保存的证据若已清理，
按 NOT_FOUND/UNAVAILABLE 待核对，不能默认为成功或重新下发。整批 UNKNOWN 的成员投影不能当永久整体
闭合证据，新到更高已应用版本必须重新核对。详细矩阵见[出站结果](outbound-results.md)。

## 5. 薄入口

在业务包内用既有装饰器注册异步双参数函数，处理器只把当前上下文交给具体服务：

```python
from app.businesses.runtime import require_runtime
from app.integrations.ecs_callback import ecs_event

from .contracts import ConfirmedScan
from .definition import DEFINITION, SCAN, SCAN_EVENT


@ecs_event(SCAN_EVENT, device_role=SCAN.role_key, data=ConfirmedScan)
async def scanned(ctx, payload):
    await require_runtime(ctx).services[DEFINITION.code].accept_scan(ctx, payload)
```

ECS 在受理事务提交后 ACK，诊断与受理依据同事务保存，不从 ACK 推断业务推进；WMS 回调处理器返回原 Accepted/Rejected/Conflict。
两种上下文都由控制器可信注入同一 BusinessRuntime。入口不自行创建运行时，不从请求选择服务对象。
同名 ECS 事件可由不同角色使用各自严格 Payload。包式入口按事件小写名/operation 规范化名分文件；
完整规则见[注册指南](registration.md)。

## 6. 显式 catalog

只有完整真实业务准备好后，才在 `app/businesses/catalog.py:entries()` 显式导入其 composition 并返回 tuple。
不要登记本文的 `my_business` 标记、测试业务或空包。未知/重复 code、未声明角色、缺附录、重复命令键、
冲突附录、错误来源/角色/未注册初始入口会使启动失败；历史附录不增加新命令权限。
`GET /api/v1/businesses` 仅为管理员工作线表单提供声明，要求 `asset:workline:read`，不返回工厂或源码。

## 7. 模型加载与迁移

在同一 composition 中列出全部模型模块，`load_business_models()` 由 Alembic env 和测试建表前调用；
不按当前配置工作线决定是否加载模型，不在应用启动 create_all。新增生产模型要加正式迁移，保持单 head。
测试夹具使用独立 MetaData 与显式创建/finally 清理，不能把测试表挂进生产 Base 或迁移链。

## 8. 两线及恢复验收

至少用两条线复用同一业务，绑定不同设备、地址和位置；管理 API 可暂存不完整配置，业务不能开始执行。
再用第二份测试业务验证同名事件按角色选择正确 Payload，单据指定错误业务线应拒绝，不自动换线。

SQLite 覆盖配置 API 和注册/路由接缝；显式独立 PostgreSQL 覆盖实际隔离级别、连接生命周期、受理原子性、
独立推进原子性、合法高版本结果、完整前序/成员/revision、消费前后崩溃、丢通知、重复唤醒、清理、
40001 整事务恢复、重试耗尽，以及配置更新/发送门禁交叉并发。外部只 fake/loopback，默认不连接真实 WMS/ECS。

配置保存成功只代表配置已持久化；可执行还需要业务许可、完整合同/资源和原发送门禁；物理完成要依据
该真实业务的结果/完成合同。测试通过、工厂可构造、ACK、消息 DONE 都不能替代这三个边界。
开发验收与供应商/现场验收分别记录；当前 Step5 真实业务接入门槛仍未满足。
