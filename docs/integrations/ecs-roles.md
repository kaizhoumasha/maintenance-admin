# ECS 角色注册与运行时接入

角色由业务模块定义为常量。定义事件、Payload 和命令附录时不读取工作线、设备表或 Status；运行时才注入实际绑定及路由。框架继续使用原 ECS 接收器、EcsCommands、outbox、Sender 和 Result，不新增发送队列。

## 1. 静态声明

在业务模块定义角色，例如 `SCAN1 = "SCAN1"`；通用框架不内置仓库角色枚举或角色字典表。

```python
from app.integrations.ecs_callback import CallbackPayload, ecs_event
from app.integrations.ecs_command import CommandBinding, DeviceAnnex
from app.integrations.ecs_command.contracts import ErrorDetail

SCAN1 = "SCAN1"


class ScanCompleted(CallbackPayload):
    barcode: str


@ecs_event("SCAN_COMPLETED", device_role=SCAN1, data=ScanCompleted)
async def scan_completed(ctx, payload):
    # 在当前事务写业务；本组位置从 ctx.binding.position_bindings 读取。
    ctx.note("SCAN_RECORDED", barcode=payload.barcode)


annexes = {("scan", "1"): DeviceAnnex(ScanCompleted, ScanCompleted, ErrorDetail, 1, 60000)}
bindings = [CommandBinding(SCAN1, "SCAN", "scan", "1", ScanCompleted)]
```

事件注册在原自动发现入口 `app/integrations/<business>/ecs_event.py` 或其包内；事件多时用 `ecs_event/scan1/scan_completed.py`。命令合同由可信启动模块提供。上例中的业务名称、Payload、task 和超时仅作说明，真实接入必须使用已确认合同。

事件键为 `(device_role, event_type)`，命令键为 `(device_role, task_type)`。角色字符串常量可跨工作线复用；不根据 data 形状、设备编码后缀或 ECS 描述角色推断合同。重复键或缺失附录在注册／装配时失败，不查询外部系统。

## 2. 运行时注入

```python
from app.controllers.ecs_callback import include_ecs_callback
from app.integrations.ecs_command import RoleRoutes, configure_ecs

routes = RoleRoutes(
    bindings,
    annexes=annexes,
    resolve_binding=resolve_binding,
    resolve_route=resolve_route,
)
include_ecs_callback(app, environment=environment, resolve_binding=resolve_binding)
service = configure_ecs(app, routes=routes, annexes=annexes)
```

两个异步函数由使用方提供：

- `resolve_binding(session, device_code) -> DeviceBinding | None`：返回设备身份／版本、工作线身份／版本、逻辑角色及本线绑定上下文。稳定未绑定返回 None；并发变化和数据库故障抛出，不能伪装成永久未绑定。正式实现须保持绑定锁到当前事务完成。
- `resolve_route(session, binding) -> tuple[str, str]`：对已保护的绑定返回 `(ecs_origin, route_id)`。沿用当前冻结格式，route_id 为 `device.route.<UUIDv7>`；身份在换址／删除重登记时不能复用。框架从同一角色任务声明选择附录并使用 binding.revision 冻结管理版本。

工作线使用方在自己的服务模块实现这两个函数，查询并锁定自己的绑定和地址配置。正式调用先按工作线＋角色取得设备并核对期望上下文，再将 device_code 传入原 `service.create`。独立框架不提供或导入设备／工作线表、管理服务及其迁移；这些具体适配在单独的设备管理改动中接入。

新事件按 `device_code → 当前绑定角色 → 处理器` 分发；命令由 `工作线＋角色 → device_code → 角色任务附录` 创建。原 receipt 始终先查重；已接收的重报不重新分发。未绑定或未注册的新事件仍持久 ACK，绑定后不自动回放。

默认 app 只装配原 local 通用协议调试，不预装工作线实现。独立接入在自己的 FastAPI 应用中调用以上原装配入口，不需要导入 `app.main`、设备管理控制器或 Status。正式角色事件缺少解析器时启动失败，不能静默按未知事件处理。

## 3. Mock、模拟器与真实 ECS 调试

自动测试注入内存绑定和路由解析器；业务角色及附录与正式接入相同，不需要设备／工作线表。测试解析器使用明确的设备映射和每次配置变更递增的版本，不从请求或 Status 猜测角色。

模拟器和真实 ECS 都复用上述装配及原 HTTP 客户端：运行时解析器返回开发者指定的 origin，事件回调发送到原 `/api/v1/callback/event`，命令通过原 `EcsCommands.create → outbox → Sender` 发往 ECS。独立测试使用 loopback 模拟器，不保存默认真实地址。

local 环境需要同时保留 Swagger 协议调试时，使用原 local 装配：

```python
from app.integrations.ecs_command.dev import configure_local_ecs

service = configure_local_ecs(
    app,
    environment="local",
    routes=routes,
    history_annexes=annexes,
)
```

它将正式角色路由与 dev v3 调试路由组合到同一服务、同一 Sender。正式命令仍经角色附录校验；Swagger `/api/v1/dev/ecs/commands` 保持通用协议调试语义，填写 `ecs_url`、device_code、task_type、params，新登记仍需 `dev:ecs:send_real` 权限。Swagger 的宽 JSON 合同不代表某个角色的严格业务合同已通过；验证角色业务须走上面的正式路由并注入对应绑定。

配置变化后，原门禁阻止旧命令后续 HTTP，进入原配置撤销暂停；不取消设备、不恢复历史发送。已可能交付的原 Result 继续按冻结身份和附录接收，不按设备当前角色重新分发；UNKNOWN/HELD 的不自动重放规则保持。

## 4. 独立交付与验证边界

角色框架涉及 ECS callback 的注册／绑定 DTO／接收器，ECS command 的角色路由／合同／Result 适配，原调试入口、目录及角色字段。设备／工作线数据库适配、管理 API／迁移和 Status 属于使用方接入，不是定义注册或 Mock 测试的前置。

统一目录从 `app.state.integration_capabilities` 读取应用显式提供的内置能力元数据；未装配 Status 时仍能查看业务事件和命令合同。本 PR 不装配 Status；后续使用方可显式提供内置能力描述，目录自身不查询外部系统。

运行 `uv run pytest tests/integrations/ecs_command/test_roles.py tests/integrations/ecs_callback/test_roles.py` 验证常量声明、注入分发、配置停发、迟到结果与 loopback 命令。PostgreSQL 参数仍需显式独立测试库；SQLite 可以独立运行。使用方提供的数据库适配器须在其自己的独立测试库中验证真实行锁与管理一致性，不以 Mock 证明数据库并发。

本轮验收只使用本地 Mock／loopback 和独立测试库；未连接真实 ECS/WMS。真实联调可使用上述入口，执行记录与现场物理结果仍分别验收。
