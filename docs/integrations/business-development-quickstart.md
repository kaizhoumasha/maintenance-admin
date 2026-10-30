# 业务开发快速开始

先确认业务怎样受理、等待、推进和恢复，再接入唯一的模型 API。
示例名称、字段和测试合同不是已确认的 WMS/ECS 合同，也不会自动注册。
正式 `catalog.entries()` 已登记粗分扫码、WMS 验证及首个 MOVE_FORWARD / Result 阶段；新增业务或阶段须具备获准的合同、资源和实现。
模型 API 的历史变更与验证快照见[单一 API 验收记录](business-development-single-api-acceptance.md)；当前异步调度用法见[按业务唤醒设计](../superpowers/specs/2026-10-29-business-pending-wakeups-design.md)。
旧阶段设计、计划与验收记录已移出仓库归档，当前调用形式与验证证据统一使用上述文档。

## 最短接入路线

| 顺序 | 开发者写什么 | 本步检查 | 继续阅读 |
| --- | --- | --- | --- |
| 1. 确认业务经过 | 写明入口、稳定 execution/cycle 身份、资源、当前许可、直接前序、成功条件、等待和恢复责任；确认 WMS/ECS 合同 | 重报、重启和等待复用原身份；ACK 不作为完成依据 | [业务 API 索引](workline-business-api.md) |
| 2. 写纯声明 | `app/businesses/<business>/definition.py` 定义角色、位置槽和初始入口 | 只依赖标准库和声明类型；不可变 tuple；不访问运行设施 | [definition](business-packages.md#1-纯声明-definitionpy) |
| 3. 写合同和薄入口 | 在业务包放 `contracts.py` 及 `ecs_event` / `callback` / `request` 文件或入口包；处理器转交具体服务 | 类型、扩展字段、文件名和来源正确；未确认名称不登记 | [注册指南](registration.md) |
| 4. 声明命令 | 用 `@ecs_command(..., device_roles=(ROLE, ...))` 装饰参数模型 | 严格参数/结果/错误类型及超时；一个模型可供多个角色共享 | [命令声明](ecs-command.md#模型声明与显式编译) |
| 5. 固定 composition | 列出 `DEFINITION`、`ENTRY_MODULES`、`MODEL_MODULES`、`COMMANDS=(Model, ...)`，异步业务加 `PROGRESSION="scan_pending"`；工厂注入能力和 session factory | catalog 内部编译，只激活本业务声明角色的交集；API/worker 共用清单 | [composition](business-packages.md#3-可信装配-compositionpy) |
| 6. 实现持久业务 | 具体 `service.py/models.py` 保存执行、原身份、持久待办、等待原因和消费依据 | 原事务受理；新 session/RR 推进；进度、消费与下一 Outbox 意图原子 | [事务配方](business-transaction-recipes.md) |
| 7. 验证后接入 | 隔离验证两线/两业务、并发和恢复；完整真实业务获准后才加入 catalog | 测试使用独立 MetaData 和 finally 清理；生产迁移保持单 head | [显式 catalog](business-packages.md#6-显式-catalog) |

业务只有 `app/businesses/<business>/` 这一目录来源。将入口文件或包显式加入所属
`composition.ENTRY_MODULES`，并由 `catalog.entries()` 列出该 composition；仅新建文件不生效。
平台固定 Transport/本地演示入口由共享 manifest 加载，不是新增业务的目录选项。
`accept`、`advance`、`scan_pending` 是具体业务自己的方法；平台提供共享任务装配与调度壳，按 composition.PROGRESSION 调用待办入口，不接管业务判断、事务或提供第二 Sender。

### WMS operation 流程卡

1. WMS → 本项目用 `callback`；本项目 → WMS 用 `request`，确认 FACTS 或 DECISIONS。
2. 用 `@callback_operation(name, data=Payload)` 异步处理器或 `@request_operation(name, Endpoint)` 模型声明，加入显式入口清单。
3. 回调在 `ctx.session` 受理；出站只用 `await capabilities.wms.send(session, Payload(...), operation_id=saved_id)`。
   保存原 operation、operation_id、message_id；由既有 Outbox/Sender 投递。
4. 用 `get_result` 读原终局响应并严格解析业务决定；RECORDED、PREPARE_ACCEPTED、DONE 不能单独判成功。

### ECS Event 流程卡

1. 在业务包 `ecs_event` 文件/包使用 `@ecs_event(name, device_role=ROLE, data=Payload)`，列入 `ENTRY_MODULES`。
2. 应用注入 BindingResolver；处理器依据 `ctx.binding` 核对线、角色、当前许可和本次经过，在原事务保存待办。
3. 处理器在原事务保存事实和持久待办。需要异步推进的业务在原 composition 声明 `PROGRESSION = "scan_pending"`，实现该异步服务方法；平台依据正式声明归属，在提交后自动唤醒所属业务。即时和 5 秒 Beat 共用 `business.advance_pending(business_code)` 与原业务待办入口；回滚不投递，发布失败不改变 ACK。新增业务无需编写 Celery 任务、结果 ID 反查或平台发布路由，见[业务包默认推进约定](business-packages.md#4-子包-servicepy--modelspy)。未知事件不自动补注册或回放。

### ECS Command / Result 流程卡

1. 确认参数、Result、error_detail 及超时，在精确模型上声明 `device_roles`，将模型列入 `COMMANDS`。
2. 业务事务中只用 `await capabilities.ecs.create(session, owner=owner, call_key=saved_key, device_code=device_code, timeout_ms=timeout_ms, command=Payload(...))`。
   保存原 owner/call_key/command_code 和输入；新命令使用 `PROTOCOL_RETRY_WINDOW`（300 秒）。
3. Result 经原接收器提交；业务通过 `get_by_call` / `get_command` 核对原命令后独立推进。
   没有 `@ecs_result`；UNKNOWN/HELD 不自动换号、改址重发。

Transport 复用 `Capabilities.transport` 的现有创建、结果和完整前序读取，不重写回调、结算或 Sender。
具体合同见[Transport 请求](wms-transport.md)及[出站结果](outbound-results.md)。

## 当前接入清单

| 用途 | 唯一业务形式 | 边界 |
| --- | --- | --- |
| WES → WMS | `send(session, Payload(...), operation_id=...)` | 精确注册类唯一对应已加载 operation；实际字段重验后按模型 JSON 序列化 |
| WMS → WES | `@callback_operation(name, data=Payload)` + 异步处理器 | 原 `ctx.session` 保存业务和待办；入站模型保留原扩展字段策略 |
| ECS → WES | `@ecs_event(name, device_role=ROLE, data=Payload)` + 异步处理器 | 事件声明仍是单角色；绑定解析与静态声明分开 |
| WES → ECS | `@ecs_command(..., device_roles=(...))` + `COMMANDS=(Model, ...)` + 模型 `create` | catalog 编译，正式路由只用 `resolve_declared`；业务不手写编译产物 |

模型支持及不支持的 dataclass/泛型合同见[模型约束](ecs-command.md#模型声明与显式编译)。
local Swagger 的 `operation/data`、`task_type/params` 是动态 HTTP 协议字段，经受信任适配器解析后进入同一核心；
不能作为业务 SDK 调用模板。新字节遵循模型别名、默认值、序列化器及 extra 策略，`extra="ignore"` 丢弃字段不被暗藏或恢复。
历史冻结数据和 Result 合同继续保留；表达不了原输入的存量动作按原引用读取、核对，不另换身份发送。

### 启动应用前检查声明

使用项目 Python 3.13+ 和锁定依赖，在仓库根目录每次运行一个新进程：

```sh
rtk uv run --frozen --no-sync python -m scripts.check_integration_contracts --environment production
rtk uv run --frozen --no-sync python -m scripts.check_integration_contracts --environment local
```

`--environment` 必填，只接受 `production` / `local`，不读取继承的 `ENVIRONMENT`。
production 表示正式声明范围，不连接生产；无需数据库、Redis、JWT 或 `.env`。
唯一加载器 `app.integrations.discovery.load_formal_modules/load_local_modules` 合并 catalog 显式入口与固定平台 manifest。
顺序为全部正式入口 → 业务合同交叉校验 → local 入口 → 应用资源/工厂；CLI 不执行最后一步，也不执行 handler、建表或回放。
声明缺失/内部 import 错误会失败，local 不能修补正式缺项；API/worker 使用同一声明来源。

成功退出 0，stdout 为一个 JSON；失败退出 1，仅 stderr 提供固定阶段、错误类型和安全源码位置；
参数错误退出 2，help 退出 0，参数处理先于应用导入。失败时先核对 catalog、ENTRY_MODULES、COMMANDS 和角色声明，不输出原异常或 Payload。

JSON 包含 `environment`、`businesses`、`entries`、`command_contracts`、`scope`。
`entries` 保留 kind/name/device_role 与原 module/source_file/symbol/local_only；命令合同显示 task_type、角色、附录版本、超时和原模型来源。
当前 `businesses=["coarse_sorting"]`，命令合同包含 `CONVEYOR / MOVE_FORWARD`；同时检查粗分 `INPUT_ARM / SCAN_COMPLETED`、`inbound.coarse_sorting.validate_bind@v1` 和两个正式 Transport 回调。local 保留 ECS/WMS 演示及六个 Transport 场景。
通用 v3 调试命令、Status、模型资源、路由、迁移、网络、许可和物理完成不在静态检查范围。
清单只证明实际加载的声明，不扫描任意源码；环境切换需新进程，不提供缓存清理/重扫接口。

### 原身份与结果读取

| 对象 | 业务必须保存并复用的依据 | 当前公开读取方式 |
| --- | --- | --- |
| WMS 请求 | 原 `operation`、`operation_id`、返回的 `message_id`；原 target、规范请求字节和期限由框架冻结 | `WmsRequests.get_result(session, message_id=..., operation=..., operation_id=...)` |
| ECS 命令 | 原 owner、call_key、command_code、原输入；路由、附录身份/版本、字节和期限由框架冻结 | `EcsCommands.get_by_call(session, owner=..., call_key=...)` / `get_command(session, owner=..., command_code=...)` |
| Transport 任务及直接前序 | 原 task_id、client_request_id、caller（原工作线/站点）、cycle_id、完整前序 ID、货架/料箱范围；业务另存已消费版本/成员和进度 | `TransportService.get_result(...)` / `list_unclosed_predecessors(...)` |

固定 target 下，同一 WMS 身份和规范 payload 重入返回原 message_id；默认值、别名输入、显式 null、嵌套 JSON
沿用模型的原序列化。同身份改变内容仍抛 `DuplicateMessage`。ECS 同 owner/call_key/原输入重入
先返回历史命令，不重新解析变化后的当前路由；异输入仍拒绝。新 ECS 命令使用
`PROTOCOL_RETRY_WINDOW`（300 秒），历史命令保留原期限。

两角色可共享原模型/附录；不能在同一 annex_id/version 下收紧原合法合同。
可信装配保留的 `history_annexes` 仅用于原 Result 校验，不授予新角色命令权限。
`CallbackPayload` 的 `strict=True, extra="ignore"` 及其他合法模型配置继续有效；新请求内容按该模型策略序列化。

结果消费只使用上述公开读接口，不直接读写框架表。ACK、投递 DONE、任务 CLOSED 均不替代
业务成功、当前许可或物理完成。独立推进使用新 session，首条 SQL 前设置 REPEATABLE READ；
按工作线 ID → 设备 ID → 业务动作固定顺序加锁。完整直接前序每批最多 100 条，分批和混合读取
保持同一快照。业务进度、消费依据和下一 Outbox 意图原子提交；40001 关闭失败 session，
在新 session 有界重跑整个事务，耗尽保留待办和原等待依据。见[结果读取约定](outbound-results.md)
及[业务包事务边界](business-packages.md)。

### 回归证据与边界

当前行为映射、任务专项实际结果及最终全仓状态统一见[单一 API 验收记录](business-development-single-api-acceptance.md)。
旧阶段数字保存在仓库外归档中，不跨批次累加。隔离存储测试须显式配置可丢弃的
`TEST_DATABASE_URL` / `TEST_REDIS_URL`，未配置时跳过，配置后连接失败必须失败。
测试通过只证明本方断言，不替代供应商合同、真实物理结果、部署或现场验收。
