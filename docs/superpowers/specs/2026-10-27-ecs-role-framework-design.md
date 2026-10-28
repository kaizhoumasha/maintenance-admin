# 独立 ECS 角色框架与运行时注入设计

> **接入代码历史说明（2026-10-28）。** 本文的旧业务目录、发现器、字符串/字典 SDK 或手写绑定示例已被[单一 API 接入](../../integrations/business-development-quickstart.md)及[验收记录](../../integrations/business-development-single-api-acceptance.md)替代；三类 registry 的独立 discovery.py 已删除，当前使用共享加载器和 catalog/固定平台 manifest。原协议、冻结数据、身份、结果与历史执行记录保持其原有语境，本文不作为当前 SDK 示例。


状态：按用户确认的拆分边界实施；本次 PR 只包含独立角色框架，不包含设备、工作线管理或 Status。

## 范围

- 业务代码定义角色字符串常量，不新增角色字典、设备角色主数据或通用仓库角色枚举。
- 事件按 (device_role, event_type) 注册严格 Payload 与处理函数；命令按 (device_role, task_type) 声明现有 DeviceAnnex 身份及版本。注册期不查询数据库、设备、ECS 或工作线。
- 复用现有模块发现、命名校验和来源追踪；重复组合键报双方来源；命令缺少附录在装配前失败。

## 运行时契约

- BindingResolver(session, device_code) 返回 DeviceBinding 或 None。上下文含设备／工作线身份及管理版本、角色、本线设备和位置绑定；身份不符拒绝执行，基础设施错误不能降为稳定未绑定。
- 事件先查原 receipt，再通过注入绑定按组合键选择 Payload／处理器；绑定不进入原事件摘要。已接收重报不重新分发，稳定未绑定／未注册仍持久 ACK，无自动回放。
- RoleRoutes 注入同一绑定解析器及 resolve_route(session, binding)，后者返回 (origin, route_id)。使用方负责工作线读取、锁序及管理一致性；框架不导入其管理服务。
- 继续使用 device.route.<UUIDv7> 和版本2摘要的冻结格式。每次管理变更递增版本，配置改回不恢复旧许可；明确失效 REVOKED，损坏／旧布局 UNKNOWN，基础设施异常上抛。
- 原 EcsCommands/outbox/Sender/Result 保持单一链路；停止旧命令后续发送但不伪造设备取消或成功。有可能交付的原结果仍按原附录接收，不转交当前角色；历史查询不重建发送。

## 调试和交付

- local 原协议调试可单独工作，或通过 LocalEcsRoutes 与正式角色路由共用同一服务／Sender；真实地址和新登记权限保护保持。
- 模拟器与真实 ECS 复用原 HTTP 客户端，origin 由使用方明确指定；默认不配置真实设备地址。
- 统一目录显示角色及事件／命令合同，应用可显式注入独立能力元数据；Status 不是框架前置，不增加第四套业务注册平台。
- 本次不提交设备／工作线管理、具体数据库适配、Status、管理权限／菜单及其迁移；不改变默认 app 的设备装配。

## 验收

独立常量注册、同事件／任务多角色严格合同、未知／错误绑定、原收据重报、配置停发及迟到结果、原 local 权限／来源／WMS 回归；使用 SQLite、独立 PostgreSQL/Redis 和 loopback HTTP Mock。不连接真实 ECS/WMS，不部署。

接入步骤见 [角色接入](../../integrations/ecs-roles.md)。
