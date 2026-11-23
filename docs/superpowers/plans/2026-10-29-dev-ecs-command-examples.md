# ECS Command Swagger 下拉与样例实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 基于实际有效命令声明，为现有通用 ECS 调试入口生成可选择的完整请求样例。

**Architecture:** catalog 已编译绑定 → 主应用传给开发路由装配 → 路由工厂生成 OpenAPI examples。仅展示与样例元数据变化；原发送、权限、冻结身份和结果机制复用。

**Tech Stack:** 现有 FastAPI/Pydantic/pytest，不增依赖。

**Spec:** [已确认设计](../specs/2026-10-29-dev-ecs-command-examples-design.md)

## Global Constraints

- 当前 feature/coarse-sorting-workline-context 分支；保留原 index、修改、历史记录及本地文件，不提交、不推送、不部署。
- HTTP 路径、操作名称、DevCommandIn 六字段及通用v3协议保持，task_type 不改枚举，不增加选择器/模式/注册表/配置/第二Sender。
- 只使用 catalog 已生效编译绑定，不从所有模型角色自行推导；示例不读取数据库、不触发业务或网络，不切换正式附录。
- 原 MOVE 平台演示保留。MoveForward 参数例只含 barcode；模型的原严格参数/Result/超时合同不变。
- 默认样例超时30000ms，限于声明范围；缺对象样例给空 params 和明确提示。样例 client_request_id 按角色/动作区分。

## Review Focus

1. 共享模型声明多个角色，但仅部分生效：只展示传入编译绑定。
2. 不同角色同名动作：样例键、请求标识互不覆盖。
3. 缺样例/窄超时：清楚提示、合法包络及范围，不编造业务参数。
4. 路由导入早于业务发现：挂载时生成，元数据不创建资源或额外注册。
5. 选择已声明动作：仍走通用调试合同及既有权限/身份/查询，不冒充业务验证。

## Task 1: 声明驱动的命令样例与回归

**Files:** app/controllers/dev_ecs_command.py、app/controllers/dev.py、app/main.py；app/businesses/coarse_sorting/ecs_command.py；tests/controllers/test_dev_ecs_command.py、tests/integrations/ecs_command/test_roles.py；必要的独立 OpenAPI 回归文件；docs/integrations/ecs-command.md、ecs-command-tutorial.md、ecs-event-debugging.md。

**Interfaces:** ecs_command_router(command_bindings) 返回 APIRouter；include_dev_routers 必须接收已编译 command_bindings，并由 main 传入同一份结果；迁移全部当前内部调用，不保留旧 router 别名。既有 create_command/get_command 函数名保持以稳定 operationId。原通用样例从 DevCommandIn 的模型 schema 读取；正式样例取 binding.source 的 JSON Schema examples。

- [x] 先补公开 OpenAPI/HTTP 回归：实际 CONVEYOR/MOVE_FORWARD、有效角色、同名不同角色、缺样例与超时范围；运行并记录缺少下拉的真实 RED。
- [x] 实现路由工厂与命令样例生成，迁移所有挂载调用；保留原 handler 与 mock 处理逻辑。给 MoveForward 增加模型例，不引入动态业务导入。
- [x] 用选出的请求经原 HTTP 调试入口验证登记与重入、通用快照、未知动作、权限与 Result；所有目标仅 MOCK/回环，记录 GREEN。
- [x] 更新参考、教程和操作说明，明确占位设备/地址/请求标识及内置 Mock 支持范围；历史验收记录保持。
- [x] 控制器执行相关与全量测试、Ruff、冷合同、Swagger 展示检查及独立最终评审。核对原 index/HEAD/stash、保存验证日志、清理仅本轮自有资源。

## 执行说明

用户已明确批准前述建议并要求更新设计后继续实施，沿此前子代理方式执行本单项任务，不再重复询问相同范围。业务代码样例生成与路由挂载相互依赖，作为一个完整测试/评审单元；不拆出通用样例平台。

## 本轮验收（2026-10-29，MOCK / 内部验证）

- 状态：本单项任务已完成，单项规格/质量评审及最终独立评审均通过，无待处理发现；历史验收记录未改写。
- RED：真实主应用 OpenAPI 缺少请求 examples，回归以 `KeyError: examples` 失败；实现后目标回归 683 passed、21 skipped。
- 全量：3517 passed、37 skipped（252.22s）。其中35项为 SQLite 不支持的 PostgreSQL 锁/并发/快照分支，对应 PostgreSQL 用例已执行；2项专用迁移数据库环境未配置。本轮没有迁移或数据库结构变化。
- 全仓 Ruff check 通过，format --check 通过（471 files）；正式及 local 声明冷检查均通过，仅检查声明，不连接生产环境。
- 现有 `localhost:8001` 的 OpenAPI 与实际 Swagger 已核对：平台 / MOVE 和 CONVEYOR / MOVE_FORWARD 可选，切换后展示完整六字段及 `params.barcode`。仅展示检查，未点击 Execute，未重启原服务。
- 发送、权限、请求身份、通用v3附录、未知动作、结果查询和 Result 行为经原公开回归验证。该下拉不代替正式业务调用，也不推进扫码业务；内置 Mock 仍仅支持 D1/MOVE ACK。
- 保全：原 HEAD、当前功能分支、stash 和其他已跟踪文件内容保持。收尾发现本轮增量已暂存，用户确认系主动暂存并要求保留；实施收尾按该指示保留当时暂存状态。后续用户授权提交，最终验收补记随同纳入；未推送或部署。
- 仅本轮自建 PostgreSQL/Redis 测试容器已停止并移除；未改变现有配置、服务或数据。验证日志、截图和评审记录归档于本机 `/Users/kaizhou/.codex/recovery/maintenance-admin/command-examples-20261029T201103Z/`。
