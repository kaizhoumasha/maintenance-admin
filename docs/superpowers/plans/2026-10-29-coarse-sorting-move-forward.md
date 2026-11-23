# 粗分验证与 MOVE_FORWARD / Result 实施计划

> **后续开发约定：** 本文保留原方案与验收历史。当前调度已改为[按业务唤醒、共用待办入口](../specs/2026-10-29-business-pending-wakeups-design.md)，允许仅负责资源装配与委托的共享任务壳；业务仍持有推进规则和事务。旧任务名称及禁止通用 worker 的原文属于当时范围，不作为新业务开发用法。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 完成真实扫码经配置的 WMS 验证、唯一 MOVE_FORWARD、原 Result 消费，止于等待箱格分配。

**Architecture:** 保留原接收事务，只保存事实和待办；具体粗分服务通过可信 factory，在独立 RR 事务中消费公开结果并登记下一意图。复用已有 Celery、Outbox、唯一 Sender 和 Result 接收器，不创建 local 分支或第二装配。

**Tech Stack:** 现有 Python 3.13+、Pydantic、SQLAlchemy、Alembic、Celery、pytest、PostgreSQL/Redis；不增加依赖。

**Spec:** [设计与用户确认规则](../specs/2026-10-29-coarse-sorting-move-forward-design.md)

## Global Constraints

- 当前 feature/coarse-sorting-workline-context 分支实施；不提交、不推送、不部署，原历史记录及本地文件保留。任务分解不再重复请求执行许可。
- 同一套正式装配；MOCK 由测试/部署数据库、绑定和对端配置决定，无 local 专用业务、私有 SQLite、前缀分流。
- WMS 业务 NG 保存原响应、原因，等待 AWAITING_NG_STAGE，MOVE_TO_NG 留下一阶段。
- WMS operation=inbound.coarse_sorting.validate_bind@v1，decisions，字段 workline_code/barcode/reel_diameter/reel_thickness；包络 timestamp 为请求登记时间，原事件外层 timestamp 保存在扫码事实。
- COMMANDS=(MoveForward,)，CONVEYOR 角色；params 仅原 barcode。附录 coarse_sorting.move_forward/1，timeout 30000ms，范围 1–60000ms，PROTOCOL_RETRY_WINDOW=300秒。
- FAILED 允许空 data；SUCCESS 消费需 actual_qty=1、location=STATION_PIPELINE1_OUTPUT1。缺完成字段、失败或 UNKNOWN/HELD 不正常推进，不换号重发。
- 首条 SQL 前设置 PostgreSQL REPEATABLE READ；WorkLine → 同类按 ID 排序的 Device → 业务行锁序；40001 整事务最多3次、新 session 重读，耗尽保留待办。
- 成功停 AWAITING_ALLOCATION_STAGE；不创建 storage_allocate、PUT_TO_BIN、补换架、真实设备请求或当前用户库升级义务。
- Celery 具体任务 coarse_sorting.advance_pending，每5秒，分页100；只登记意图，由原 Sender 发送。

## Review Focus

1. 原扫码重复、进程重建或收据清理，不得新建验证或移动命令；Task 2 验证稳定身份与消费证据。
2. 多工作线、改绑、禁用、配置/版本变动，不得串设备或把原动作搬到新线；Task 2 负向与真实 PG 并发验证。
3. 只有原 WMS 合法 OK + 非空 binding_id 才建命令；WMS NG、拒绝、未知、非法响应均不得放行；Task 2 分支回归。
4. 失败空 data、ACK、Result早于ACK、缺完成字段、错设备/命令、重复结果不能误推进；Task 1 合同、Task 2 真实接收器回归。
5. 注册归属、worker/API factory、旧扫码行迁移、序列化失败及扫描分页不能丢待办/污染元数据/饿死后续动作；Task 1 元数据/迁移、Task 2 恢复及任务回归。

## Task 1: 正式合同、模型与可信装配

**Files:** app/businesses/coarse_sorting/{ecs_command.py,request/__init__.py,request/inbound_coarse_sorting_validate_bind_v1.py,definition.py,composition.py,models.py,service.py}; app/businesses/runtime.py; app/main.py; migrations/versions/2026-10-29_coarse_sorting_move_forward.py; tests/businesses/{test_coarse_sorting_contracts.py,test_runtime.py,conftest.py,test_coarse_sorting_application.py}; tests/helpers.py; tests/integrations/test_registration_bootstrap.py。

**Interfaces:** 新增严格 MoveForward、MoveForwardResult、ValidateBind、ValidateBindDecision 模型；decision 对 OK 要求非空 binding_id，对 NG 要求非空 reason_code。Capabilities 增加 session_factory；build_runtime(*,outbox,ecs_commands,session_factory) 统一必传。CoarseSortingService(scan_table,capabilities,validate_bind) 由 composition 注入固定的已注册请求模型，服务不反向导入注册入口；保持无活动 session 的服务，Task 2 从 capabilities.session_factory 获取独立事务资源。

- [x] 先写合同回归：合法命令序列化只含 barcode，严格拒绝错误类型/额外参数；FAILED 空 data 可校验，成功完成字段按附录约束；合法/非法 WMS 决定；生产/local 冷声明均包含 CONVEYOR/MOVE_FORWARD 与正式验证请求。运行目标测试，记录因缺失合同/装配失败的 RED。
- [x] 实现模型和 registration_filename 对应请求模块，固定 ENTRY_MODULES、COMMANDS 及 INPUT_ARM/CONVEYOR required 角色；元数据发现不创建资源、不导入运行时服务。
- [x] 注入 factory，迁移所有 build_runtime/Capabilities 调用和受理测试夹具；测试表继续独立 MetaData、显式创建/finally清理，测试装配用真实声明。
- [x] 扩展原扫码表：validation_operation_id、validation_message_id、validation_response、binding_id、conveyor_device_id/code/version、command_code、move_result，均可空。新增迁移 d7cf20261029，down_revision=c5ca20261029。只删除空附加列时允许 downgrade，有原出站/消费依据则拒绝破坏性降级。
- [x] 运行合同、架构、runtime、原扫码及元数据 CLI 回归；检查唯一 Alembic head。隔离存储配置由控制器提供，不使用用户库，不提交。

## Task 2: 业务推进、具体调度与内部验收

**Files:** app/businesses/coarse_sorting/service.py; app/tasks/coarse_sorting_tasks.py; app/tasks/celery_app.py; tests/businesses/{conftest.py,test_coarse_sorting_forward.py,test_coarse_sorting_scan.py}; tests/tasks/test_coarse_sorting_tasks.py; 必要的现有合同/装配回归。README.md、AGENTS.md、docs/integrations 的当前粗分范围及原计划/设计页首指向本增量，不改历史验收正文。

**Interfaces:** CoarseSortingService.advance(scan_id: str)->str 独立事务返回持久等待/结果状态；scan_pending(*,limit=100)->int 对可推进原ID有序分页；async app.tasks.coarse_sorting_tasks.advance_pending(factory)->int 装配同 catalog 资源，Celery advance_coarse_sorting() 以 run_with_factory 调用。owner=CommandOwner('coarse_sorting')，call_key=f'{scan_id}:MOVE_FORWARD'。

- [x] 先经真实 HTTP 收到扫码，用真实 Outbox/Sender + MOCK WMS/ECS 驱动预期闭环，记录缺少 advance/调度或分支错误的 RED。
- [x] 扫码保持原 receiver 事务。推进时核对原绑定、业务、配置/版本，在新 RR 事务中发唯一验证；保存输送线身份及版本，不从扫码设备名推导。
- [x] 通过 WMS get_result 读原引用；保存合法决定，只有 OK 登记唯一公开 ECS create 并原子保存命令；NG 等待下一阶段，未知/拒绝/非法响应留原待办及身份。
- [x] 通过 ECS get_command/get_by_call 消费原已应用 Result；SUCCESS 数量/位置符合时保存原证据到 AWAITING_ALLOCATION_STAGE；FAILED、缺字段、未知、ACK 不推进；稳定重入不登记新命令。
- [x] 增加3次整事务40001重试、有序分页与具体 Celery任务；API/worker 都注入自己的 factory。逐条业务失败回滚，不增加第二 Sender；配置故障有可定位诊断，不伪造 NG。
- [x] 回归覆盖 Review Focus 的5类风险及真实 PG 锁序/重复唤醒/改绑/40001，模拟请求字节及目标隔离，无未授权网络调用。验证正常主应用/具体任务实际接缝，而非测试替代入口。
- [x] 同步当前指南与草案，增加带具体请求/响应的操作说明，记录实际验收；保留旧阶段验收，并明确本轮不实现 NG 搬运和后续分配。
- [x] 控制器执行相关及全量 pytest、Ruff、local/production 冷合同、唯一迁移链/隔离迁移/模型无漂移检查，独立最终评审并修复；保持 index/HEAD/stash 和现有本地数据，清理仅本轮自有测试资源。

## 计划自检与裁决

设计各节均由两项任务覆盖；公开 API、固定身份、事务及调度的接口已对齐，未引入新依赖或通用流程框架。用户已选择完整闭环，沿其此前子代理实施方式持续执行，不再重复要求确认同一范围。

## 2026-10-29 最终实施与内部验收

- 当前扫码 data 仅为 barcode、reel_diameter、reel_thickness；WMS 验证 data 仅为 workline_code、barcode、reel_diameter、reel_thickness。时间沿用各自原公共包络 timestamp，不增加业务字段或兼容调用。
- 已实现同一正式装配的验证请求、唯一 MOVE_FORWARD 和原 Result 消费；成功停在 AWAITING_ALLOCATION_STAGE，业务 NG 停在 AWAITING_NG_STAGE，失败/未知/不完整结果保留原身份及证据等待核对。后续分配、NG 搬运和入箱未实施。
- 具体 Celery 推进复用现有调度与唯一 Sender；请求模型由 composition 明确注入，服务不导入注册入口或保留活动 session。公开调用、原事务/独立 RR、固定锁序及有界重试均有回归。
- 最终全量 pytest：3512 通过、37 跳过，251.62 秒，无失败。35 项跳过为 SQLite 不适用的并发、锁或快照变体，其 PostgreSQL 对应项已执行；2 项既有设备/绑定迁移测试缺少各自专用迁移库，未记为通过。
- 新增请求的 Swagger 样例缺失由全量回归发现并修复，原公开 HTTP 回归及合同测试 51 通过；最终全量包含该修复。Ruff 检查/格式检查通过（469 文件）；75 个 JSON 样例及 159 个本地文件链接目标检查通过。
- 普通 app.main 实进程分别以 local、production 验证同一正式配置数据库，使用不依赖前缀的两线设备。实际任务与唯一 Sender 向回环 MOCK 对端执行共4次验证、3条移动命令；原 Result 先于 ACK、FAILED 空 data、业务 NG、重启与收据清理后身份复用均验证通过，未创建后续操作。
- PostgreSQL 专用迁移库验证旧扫码事实升级保留、9 个可空附加字段、无依据时安全降级、有依据时拒绝破坏性降级且数据保留、模型无漂移；唯一 head 为 d7cf20261029。未对用户现有数据库执行迁移。
- production/local 冷合同检查通过，均含验证请求及 CONVEYOR/MOVE_FORWARD；原元数据/归属校验不变。任务分步及整体独立评审均关闭，终审唯一文档范围问题已修正并复核，无剩余审查问题。
- 全量、正常进程、迁移原始日志及冻结清单已保存于本地恢复目录。首次普通进程验证因仓库 dotenv 与显式配置合并触发重复目标保护，未创建验证请求；改为独立配置目录后重试通过，未修改用户配置。
- 所有本轮自有测试进程已退出、两项临时数据库/Redis 容器已移除。原 HEAD、功能分支、index 和 stash 保持；并行修改的业务草案格式及字段删改保留，其他本地文件/服务不清理。本轮不提交、不推送、不部署。

内部验证不代表供应商或现场物理验收；固定扫描周期和分页已作功能验证，未作大规模积压容量测试。启用本阶段需执行新增迁移、配置 INPUT_ARM/CONVEYOR 及恰好一个 wms-decisions 对端；API Sender 与 worker 使用同一部署配置。
