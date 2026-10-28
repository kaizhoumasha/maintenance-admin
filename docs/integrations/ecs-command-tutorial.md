# ECS 最小业务接入教程

本项目已提供事务内命令登记、可靠投递、结果回调及授权查询。普通开发者可直接使用Swagger调试，或按业务接入路径编写调用代码；两者都不要求先搭建临时测试容器或运行框架验收套件。

## 1. 先分清三个结果

| 观察到什么 | 能说明什么 |
|---|---|
| 登记接口返回命令号 | 本方已保存发送义务 |
| ack_received=true | ECS已按协议接纳；尚无执行结果 |
| 合法Result的SUCCESS/FAILED | 设备报告终态；业务是否推进仍须安全消费 |

UNKNOWN表示可能已经交付；HELD保持暂停，不换命令号、改址或普通resume重发。HTTP超时、执行timeout_ms、投递窗口W与等待Result是不同时间边界；等待太久不自动变成失败。

## 2. 无设备表：直接用Swagger调试

前提是ENVIRONMENT=local、已登录并具备dev:ecs:send_real。主lifespan自动装配通用合同与唯一ECS发送通道，无需逐动作登记、联网开关、地址清单或RELIABLE_TARGETS中的ECS条目。

使用项目正常开发环境启动app.main:app，在/docs登录并填写授权凭证，打开POST /api/v1/dev/ecs/commands。准备好要连接的模拟ECS，将其实际IP地址填入ecs_url：

```json
{
  "client_request_id": "demo-1",
  "ecs_url": "http://127.0.0.1:8000",
  "device_code": "D1",
  "task_type": "MOVE",
  "timeout_ms": 30000,
  "params": {"qty": 1}
}
```

ecs_url填写你指定的模拟ECS实际地址；上面只展示字段形状，不要求修改启动地址配置，也不要求使用框架测试专用模拟端。仅接受合法HTTP(S) IP origin，固定命令路径，不带凭据、path、query或fragment，不使用DNS、重定向或环境代理。task_type不查白名单，params保留合法JSON对象；具体动作语义由对端校验。

client_request_id标识一次业务调用，重试时保持不变；同键同输入同规范化地址返回原命令，异输入/换址409。内部重试由Sender处理，不需要再次登记。通过GET /api/v1/dev/ecs/commands/{command_code}查看状态、ACK、结果与核对标记，不能跨owner读取。

如果只想观察接纳，可连接本地开发服务的内置/api/v1/device/command mock：它仅接受回环调用（调用方IP为回环地址，如127.0.0.1或::1）、D1/MOVE、timeout 1–60000ms和严格qty:int，只ACK。其他动作使用支持对应动作的模拟ECS，不需要临时测试夹具。Swagger只操作框架命令，不创建业务动作表、不自动消费业务结果。

## 3. 接入业务：维护者装配一次

DEVICE_CODE在设备记录中全系统唯一；多设备可共用ecs_origin。不单建ECS配置表，不在命令表device_code上加唯一约束。

维护者在应用启动前提供DeviceRouteProvider与版本化DeviceAnnex，并调用一次：

```python
from app.integrations.ecs_command import configure_ecs

service = configure_ecs(app, routes=routes, annexes=annexes)
```

这是装配片段，app/routes/annexes由应用提供，不是独立程序。使用角色常量及注入绑定时按[角色接入](ecs-roles.md)构造 `RoleRoutes`；local 同时调试正式角色时，由可信启动模块调用一次 `configure_local_ecs(..., routes=routes, history_annexes=annexes)` 组合装配，共用原服务和 Sender，不再重复调用 `configure_ecs`。

resolve在创建时读取有效设备地址/授权；authorize_frozen在发送门禁检查原授权。通用路由端口由提供方决定原授权是否撤销；`RoleRoutes` 将绑定版本及地址变化视为旧配置失效，停止旧命令后续发送，保留执行记录及已可能交付命令的原身份迟到结果。正式附录约束参数、结果、错误和timeout；它与Swagger通用JSON合同分工不同。端口定义、可信history_annexes及完整协议见[ECS参考](ecs-command.md)。

## 4. 事务内登记

业务service提供可信owner、稳定调用键和当前业务许可，在同一会话中保存业务意图并创建命令，最后由调用方提交：

```python
from app.integrations.ecs_command import CommandOwner

async with session_factory.begin() as session:
    # 在此事务中读取/锁定当前业务许可，并保存业务动作。
    command = await service.create(
        session,
        owner=CommandOwner("my_business"),
        call_key=action_key,
        device_code="D1",
        task_type="MOVE",
        timeout_ms=30000,
        params={"qty": 1},
    )
    # 同事务将command.command_code关联到本次业务动作。
```

service已完成lifespan装配，session_factory和action_key由业务提供。不能在事务内直接HTTP下发；回滚必须同时撤销业务动作、命令和Outbox义务。新动作必须有当前权威许可，不能因新建READY行或换调用键而获得许可；业务版本从权威记录取得，不从路由版本、设备Status或动作计数推导。

上例旧调用继续有效。原合同符合单绑定、严格类型及禁止扩展字段等迁移资格时，可用
`command=MoveTo(qty=1)` 替换 `task_type="MOVE"` 和 `params={"qty": 1}`，二者互斥；
`MoveTo` 必须是原精确声明类，并已在 composition 显式编译。新动作还要求声明感知的正式角色路由，
通用 Swagger 路由不自动获得该能力。共享/宽松合同保留上例；完整声明、dataclass 限制及序列化资格见
[模型声明与显式编译](ecs-command.md#模型声明与显式编译)。存量动作按原 owner/call_key/command_code
恢复，不能为换语法或换目标创建新动作。

## 5. 单表安全消费

完整参考在[Reference](../../tests/integrations/ecs_command/conftest.py)，只用于测试，不是生产SDK。适用于一动作一命令、一个消费者、同库业务副作用；已有业务表满足条件就复用，不为演示增加生产表。

一行保存稳定动作ID/business_key、可信owner、原输入、command_code、当前绑定/版本、冻结原绑定/版本和业务state：READY→WAITING_RESULT→SUCCEEDED/FAILED。SUCCEEDED/FAILED同时作为本消费者的完成标记，终态行不清空重用；取消仅撤回业务许可，不代表取消设备。

消费时在自己的事务中：

1. 按统一的“权威业务记录→动作行”锁序读取当前许可；权威就在动作行时只锁该行。
2. 用公开get_command/get_by_call读取DTO，核对原命令、当前与冻结设备/版本、WAITING_RESULT、合法result，且无hold_reason/needs_reconciliation。
3. 原子转换到SUCCEEDED/FAILED，与同库业务写共同提交；业务写失败则一起回滚，随后消费原结果，不重新创建命令。

取消/换绑/版本变化先提交则旧结果不推进；消费先提交则变更不能覆盖终态。读取后迟到的矛盾保留证据核对，不自动撤销已提交业务。测试事实替身不代表生产业务聚合锁已经实现；多命令、多消费者或外部副作用按实际需求另设计。

## 6. 继续查阅

- 业务调用、协议与排障：[ECS参考](ecs-command.md)。新命令W固定300秒，旧调用/存量原期限不重算。
- 现有回归入口：[现有测试说明](../testing/ecs-command-verification.md)。本方开发及维护测试已结项，不是调用前置步骤。
- 实际供应商/现场/生产：[独立接入清单](ecs-acceptance-checklist.md)，独立于开发，未启动。

开发回归使用[已有pytest测试](../testing/ecs-command-verification.md)，Mock及测试库保护由夹具负责，不要求执行额外的隔离操作流程。调试前核对目标地址，真实ECS接入另行安排。

通用 Status 已实现：所有环境提供正式 `GET /api/v1/ecs/status`，要求登录和独立的 `ecs:status:query` 权限，见[通用 Status](ecs-status.md)；设备预览和手动刷新见[设备管理](device-management.md)。online/IDLE不能授权动作，也不能解封UNKNOWN/HELD。
