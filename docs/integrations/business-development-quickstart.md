# 业务开发快速开始

先确认一条业务怎样受理、等待、推进和恢复，再按下面路线落到现有接口。
这里的示例名称与测试合同均是讨论示例，不代表已确认的 WMS/ECS 合同，也不自动注册。
正式 `catalog.entries()` 仍为空；只有合同、资源、实现与恢复完整且获准的真实业务才可加入。
本文覆盖当前接入与检查能力；真实业务接入及后续阶段验收分别记录。

## 最短接入路线

| 顺序 | 开发者写什么 | 本步检查 | 继续阅读 |
| --- | --- | --- | --- |
| 1. 确认业务经过 | 在业务计划写明入口、稳定 execution/cycle 身份、资源、当前许可、直接前序、成功条件、等待及恢复责任；逐项确认 WMS operation、ECS Event/Command/Result 合同 | 新经过有独立业务身份；重报、重启与等待复用原身份；ACK 不作为完成依据 | [业务 API 索引](workline-business-api.md)（区分参考合同与草案） |
| 2. 写纯声明 | `app/businesses/<business>/definition.py` 定义角色、位置槽及已确认初始入口 | 标准库和声明类型依赖、不可变 tuple；不导入配置/服务/数据库 | [definition](business-packages.md#1-纯声明-definitionpy) |
| 3. 写相邻合同和薄入口 | `contracts.py` 放共享模型；按需建 `ecs_event.py`、`callback.py`、`request.py` 或对应入口包；接收器用 `require_runtime(ctx).services[DEFINITION.code]` 调具体业务受理方法 | 原类型/扩展字段规则保持；文件名、来源和角色正确；未确认名称不登记 | [注册与命名](registration.md)、[薄入口](business-packages.md#5-薄入口) |
| 4. 手工绑定命令 | 需要 ECS 命令时在 `command_contracts.py` 写 `CommandBinding` 与 `DeviceAnnex`，复用同一角色和共享模型 | 附录身份/版本、Result/错误字段、超时范围已确认；历史附录只验证原 Result | [命令合同](ecs-command.md)、[角色装配](ecs-roles.md) |
| 5. 固定 composition | 列出 `ENTRY_MODULES`、`MODEL_MODULES`、`DEFINITION`、`COMMAND_BINDINGS`、`ANNEXES`；无状态工厂接收 Capabilities，并为具体推进服务注入 session factory | API/worker 用同一显式清单；全部正式入口 → 合同校验 → local 入口 → 资源/工厂 | [composition](business-packages.md#3-可信装配-compositionpy) |
| 6. 实现持久业务 | `models.py/service.py` 保存执行、动作、原身份/资源、持久待办、等待原因和消费依据；分别实现受理与独立推进/待办发现 | 受理复用 ctx.session；推进新 session + RR；业务进度、消费依据与下一 Outbox 意图原子 | [事务配方 A/B 与恢复](business-transaction-recipes.md) |
| 7. 验证后接入 | 用隔离两线/两业务及 PostgreSQL 恢复用例验证；完整真实业务获准后才加入 `catalog.entries()`，生产模型走单 head 迁移 | 正式目录不放测试/占位包；静态检查、隔离业务验证和供应商/现场验收分别记录 | [清单及模型加载](business-packages.md#6-显式-catalog)、[配方证据](business-transaction-recipes.md#可执行证据) |

入口支持 `app/integrations/<business>` 与 `app/businesses/<business>` 两种来源和单文件/入口包。
新增工作线业务优先使用后者，使合同、模型、服务相邻；沿用旧目录无需迁移身份或另建发现树。
`accept`、`advance`、`scan_pending` 等均是具体业务自己命名的方法，平台不提供通用执行/事务 runner、
业务 worker、第二 Sender、补偿器或脚手架生成器。

### WMS operation 流程卡

1. 先确定方向：WMS → 本项目用 `callback`；本项目 → WMS 用 `request`，确认 FACTS 或 DECISIONS。
2. 用现有 `@callback_operation(name, data=Payload)` 异步处理器或
   `@request_operation(name, Endpoint)` Payload 模型注册；不要把请求 operation 当作初始回调入口。
3. 回调在 `ctx.session` 受理；出站在调用方事务内用 `Capabilities.wms.send`，保存原
   operation/operation_id/message_id；提交后由既有 Outbox/Sender 投递。
4. 用 `Capabilities.wms.get_result` 读原终局响应，按已确认业务合同解析决定；RECORDED、
   PREPARE_ACCEPTED 和 DONE 均不能单独判成功。详见[注册](registration.md)、[结果](outbound-results.md)。

### ECS Event 流程卡

1. 确认事件名、角色及 Payload，建 `ecs_event` 文件/包，使用
   `@ecs_event(name, device_role=ROLE, data=Payload)` 异步处理器。
2. 可信装配注入 BindingResolver；静态角色不查询设备或 Status。薄入口将 `ctx` 交给具体业务服务，
   依据 `ctx.binding` 检查当前线、角色、许可和本次经过，在原事务内保存业务待办。
3. 原收据提交后 ACK；提交后通知仅携带原 execution_id，丢通知靠业务持久待办恢复。
   未绑定/未知事件沿原诊断规则处理，不自行补注册或回放。详见[角色](ecs-roles.md)及[配方 A](business-transaction-recipes.md#受理-a原接收事务)。

### ECS Command / Result 流程卡

1. 确认 params、Result、error_detail 和超时范围；手工构造 `CommandBinding/DeviceAnnex`，
   在 composition 声明并由可信装配配置路由。新命令使用 `PROTOCOL_RETRY_WINDOW`（300 秒）。
2. 在业务事务中用 `Capabilities.ecs.create`，保存原 owner/call_key/command_code 和输入；
   相同身份重入复用原命令与冻结路由/字节/期限，当前改址只作用于新命令。
3. Result 经原接收器校验并提交，再由具体业务用 `get_by_call` / `get_command` 读取核对；
   ACK、投递完成和设备物理完成分别判断，UNKNOWN/HELD 不自动换号重发。
   无 `@ecs_command` 或 `@ecs_result`。详见[命令与结果](ecs-command.md)及[配方 B](business-transaction-recipes.md#独立推进-b新事务)。

新 Transport 场景通常复用 `Capabilities.transport` 现有创建/结果/前序能力，
不重写 Transport 回调、结算、投递或另一套 Sender。先确认冻结资源与 caller/cycle 范围；
具体创建参数见[Transport 请求](wms-transport.md)，结果消费见[结果接口](outbound-results.md)。

## 当前兼容清单

以下固定当前已实现的入口和旧合同，供上述接入及后续语法简化复用。

### 可用入口

| 用途 | 当前形式 | 调用和装配边界 |
| --- | --- | --- |
| WES → WMS 请求 | `@request_operation(name, Endpoint.FACTS/DECISIONS)` 声明 `RequestPayload` | `await wms.send(session, operation, data, operation_id=saved_id)`；`session`、`operation`、`data` 也可按关键字传入。字符串 operation 和字典 data 仍有效 |
| WMS → WES 回调 | `@callback_operation(name, data=Payload)` 装饰 `async def handler(ctx, payload)` | 在原 `ctx.session` 内写业务和待办；`CallbackPayload` 保留严格类型及忽略扩展字段的原规则 |
| ECS → WES 事件 | `@ecs_event(name, device_role=ROLE, data=Payload)` 装饰异步处理器 | 角色合同与运行时 BindingResolver 分开；持久收据和业务受理使用原 `ctx.session` |
| WES → ECS 命令 | 手工 `CommandBinding(device_role, task_type, annex_id, annex_version, source)`、`DeviceAnnex(...)` | 保留 `await ecs.create(session, owner=owner, call_key=saved_key, device_code=..., task_type=..., timeout_ms=..., params=...)`；当前路由只用于新命令 |

入口继续支持 `app/integrations/<business>` 与 `app/businesses/<business>`，均可使用单文件或入口包。
入口包可在 `__init__.py` 注册；分文件登记时按 operation/event 名称匹配文件名。
正式业务通过固定 `ENTRY_MODULES` 显式声明，
local 入口保持环境限制。API/worker 共用显式清单，顺序为全部正式入口 → 合同交叉校验 →
local 入口 → 资源/工厂。正式 `catalog.entries()` 当前保持为空，直到完整真实业务获准接入；
隔离示例不进入生产目录、生产 Base 或迁移。

### 启动应用前检查声明

使用项目 Python 3.13+ 和锁定应用依赖，在仓库根目录每次启动一个新进程：

```sh
rtk uv run --frozen --no-sync python -m scripts.check_integration_contracts --environment production
rtk uv run --frozen --no-sync python -m scripts.check_integration_contracts --environment local
```

`--environment` 必填，仅接受 `production` / `local`，参数决定声明范围，不读取继承的
`ENVIRONMENT`。`production` 表示正式静态范围，其他非 local 部署也使用它，不表示连接生产。
命令不要求数据库 URL、Redis URL、JWT 或 `.env`；只按现有发现器加载两种来源的正式声明，
调用原 `validate_business_contracts()`，通过后才在 local 范围加载原 dev 三类入口。
它不启动 API/worker，不调用处理器、业务工厂、运行时装配、模型加载、建表或消息回放。

成功退出 `0`，stdout 仅输出一个 JSON；失败退出 `1`，stdout 不输出部分清单，stderr 提供
固定失败阶段、错误类型、安全源码位置和修复指引，不回显原异常、配置或 Payload。
参数缺失/非法退出 `2`，`--help` 退出 `0`；这些参数处理均先于应用导入。

失败诊断的 origin/安全堆栈可能指向公共校验器，而非出错业务的 composition 或原绑定。
此时先按 phase 检查显式 catalog 和对应入口/绑定声明。当前工具不保证直接给出具体业务源项；
不要通过输出原异常、Payload 或局部变量绕过这个边界。

JSON 顶层固定为 `environment`、`businesses`、`entries`、`command_contracts`、`scope`：

- `businesses` 是排序后的显式 catalog 业务 code 数组；当前为 `[]`，仍检查 Transport 等正式入口。
- `entries` 保留三类公开注册表的 `kind/name/device_role` 及原 `module/source_file/symbol/local_only`，
  按类型、角色、名称排序，不包含地址、Payload/schema 或业务记录。
- `command_contracts` 按角色、动作、附录身份排序；每项含 `kind=ecs_command`、`name=task_type`、
  `device_role`、`annex_id/annex_version`、原绑定来源字段、原附录的 `timeout_min_ms/timeout_max_ms`。
  复用原校验结果，不增加旧 DeviceAnnex 的严格性要求，也不合成不存在的附录来源。
- `scope.checked/excluded` 明示已检查和未检查范围。只覆盖已发现且导入的声明，正式业务来自显式
  `catalog.entries()`；不承诺任意源码已加载。local 也不装配通用 v3 调试命令、ECS Status 或模型资源。

检查覆盖以原注册器和业务合同交叉校验为准；成功不证明路由、迁移、网络、许可、业务或物理完成，
也不证明未来新语法具备迁移资格。空命令合同数组不表示调试命令已经验证。无同进程清缓存/重扫接口。
详细入口规则及 HTTP 目录见[注册排障指南](registration.md)。

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
`CallbackPayload` 的 `strict=True, extra="ignore"` 及其他合法旧模型配置继续有效。

结果消费只使用上述公开读接口，不直接读写框架表。ACK、投递 DONE、任务 CLOSED 均不替代
业务成功、当前许可或物理完成。独立推进使用新 session，首条 SQL 前设置 REPEATABLE READ；
按工作线 ID → 设备 ID → 业务动作固定顺序加锁。完整直接前序每批最多 100 条，分批和混合读取
保持同一快照。业务进度、消费依据和下一 Outbox 意图原子提交；40001 关闭失败 session，
在新 session 有界重跑整个事务，耗尽保留待办和原等待依据。见[结果读取约定](outbound-results.md)
及[业务包事务边界](business-packages.md)。

### 回归证据与补测范围

以下名称可直接交给 pytest 的 `-k` 定位；路径均相对仓库根目录。测试通过仅证明对应断言，
不替代供应商合同确认、现场验证或生产验收。

| 兼容项 | 可执行证据 | 本次处理 |
| --- | --- | --- |
| WMS 调用与规范 wire | `tests/integrations/wms_request/test_service.py::test_legacy_send_forms_preserve_canonical_payload_and_reference` | 补齐位置/关键字两种调用顺序、别名输入、默认值、显式 null、嵌套 JSON、原 target/message_id/字节/expires_at |
| WMS 错误与调用方事务 | 同文件 `test_invalid_requests_never_reach_the_outbox`、`test_same_identity_with_changed_content_is_a_duplicate_message`、`test_rollback_leaves_no_message` | 复用未注册/字段/身份错误不入队、异输入原异常、回滚无残留；不复制 |
| 原 ECS 重入及期限 | `tests/integrations/ecs_command/test_service.py::test_duplicate_preserves_route_identity_and_bytes`、`test_existing_calls_preserve_frozen_retry_window`、`test_new_commands_require_protocol_retry_window` | 复用当前路由不可用/期限变化时仍返回原命令，原 route/bytes/expires_at 不变、异输入拒绝；不另增重复用例 |
| 共享及宽松旧附录 | `tests/businesses/test_contract_assembly.py::test_identical_annex_contract_can_be_shared`；`tests/integrations/ecs_command/test_result.py::test_shared_legacy_models_keep_original_result_contract` | 复用两角色共用模型/附录装配；补齐原宽松模型的扩展 params、保留历史附录后的迟到 Result、严格类型仍拒绝、原模型对象/版本及冻结内容不变 |
| 历史附录权限边界 | `tests/businesses/test_contract_assembly.py::test_history_annex_does_not_grant_new_role_command_permission`、`test_history_annex_conflict_never_constructs_communication_resources` | 复用只保留原 Result 校验、不授权新命令、冲突在资源创建前失败 |
| 两类来源、文件/包、正式/local 顺序及元数据 | `tests/integrations/test_registration_bootstrap.py::test_all_formal_kinds_load_before_any_local_entry`、`test_cold_application_loads_formal_before_local`；`tests/integrations/test_registration_discovery.py::test_business_entrypoints_can_register_without_per_operation_files` | 扩充现有冷启动用例的文件/包矩阵，实际导入两种目录的三类正式/local 入口；复用应用启动顺序与来源元数据断言 |
| 缺项、内部错误、冲突失败 | bootstrap 的 `test_required_business_entry_missing_fails`、`test_internal_import_error_is_not_skipped`；contract_assembly 的 `test_initial_entry_requires_own_formal_registration`、`test_invalid_contract_never_constructs_routes_sender_or_factory`；`tests/integrations/test_registration.py::test_invalid_names_and_duplicates_include_remedies` | 复用错误必须失败且不创建资源/工厂的证据 |
| WMS/Transport 结果身份和状态 | `tests/integrations/wms_request/test_results.py::test_request_result_uses_original_message_identity`、`test_delivery_terminal_is_not_business_completion`；`tests/integrations/wms_transport/test_results.py::test_identity_missing_frozen_pending_and_ambiguous_submission`、`test_transport_result_uses_applied_outcome_only`、`test_transport_closure_matrix` | 复用原任务/请求/caller 及 applied outcome；投递和业务结果分离 |
| 完整前序、版本、成员和 UNKNOWN | `tests/integrations/wms_transport/test_predecessors.py::test_predecessors_use_explicit_ids_and_original_scope`、`test_historical_member_facts_do_not_override_newer_unknown`；`tests/businesses/test_result_consumption.py::test_runtime_snapshot_keeps_full_101_member_versions`、`test_serialization_failure_reopens_and_rereads_complete_business_group` | 复用固定范围、100 条分批同快照、版本/成员、40001 整事务重试和耗尽保留待办；不重新定义协议 |

可重复运行的快速检查（未配置临时存储时，PostgreSQL/Redis 专用用例按既有规则跳过）：

```sh
rtk uv run --frozen --no-sync pytest -q tests/integrations/wms_request \
  tests/integrations/ecs_command/test_service.py tests/integrations/ecs_command/test_result.py \
  tests/integrations/test_registration.py tests/integrations/test_registration_bootstrap.py \
  tests/integrations/test_registration_discovery.py tests/businesses/test_contract_assembly.py \
  tests/integrations/wms_transport/test_results.py tests/integrations/wms_transport/test_predecessors.py \
  tests/businesses/test_result_consumption.py
```

数据库专项需显式 `TEST_DATABASE_URL` / `TEST_REDIS_URL` 指向可丢弃的隔离存储；配置了但连接失败
应视为失败。2026-10-28 本轮首次清单基线使用任务拥有的隔离 PostgreSQL/Redis：408 通过、7 跳过；
补测后对应回归（另含注册错误和回调合同）452 通过、7 跳过：1 个 SQLite 并发引用登记用例及 6 个 SQLite 行锁用例跳过，
相关 PostgreSQL 变体已运行；无测试存储全套为 2250 通过、749 跳过（主要为未配置专用 PostgreSQL/Redis）。
跳过原因应与结果一起保留，不能按通过计算。

### 后续迁移资格

只有原入口、原身份、规范 wire、冻结路由/附录/期限、原异常和事务边界均能维持，才可把新语法
作为同一语义的替代写法；后续阶段增加新旧等价断言，不以当前测试禁止未来接口。
`@ecs_command`、`compile_commands`、模型形式 send/create、`resolve_declared`、`@ecs_result`
当前均不可用。目标改址、target 改名、角色/任务改标签、业务身份或附录版本变更均需各自的合同和
数据迁移判断，不属于语法迁移，不能据此改写原请求、重算身份或重发 UNKNOWN/HELD。
