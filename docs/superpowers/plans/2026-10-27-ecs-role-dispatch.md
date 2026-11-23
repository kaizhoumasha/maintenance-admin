# ECS角色分发与配置失效停发 Implementation Plan

> **接入代码历史说明（2026-10-28）。** 本文的旧业务目录、发现器、字符串/字典 SDK 或手写绑定示例已被[单一 API 接入](../../integrations/business-development-quickstart.md)及[验收记录](../../integrations/business-development-single-api-acceptance.md)替代；三类 registry 的独立 discovery.py 已删除，当前使用共享加载器和 catalog/固定平台 manifest。原协议、冻结数据、身份、结果与历史执行记录保持其原有语境，本文不作为当前 SDK 示例。


> **当前交付：** 角色分发已实施；独立RoleRoutes及路由校验已在develop落地，本分支保留DeviceRoutes数据库使用方。用户已授权提交、推送及创建PR，部署与真实ECS/WMS接入另行安排。下列实施步骤、原授权和验证数字为历史记录。

**Goal:** 工作线逻辑角色驱动事件处理与命令附录选择，配置变化后停止旧命令后续发送，并继续查询记录和接收原结果。

**Architecture:** 工作线增加两个绑定JSON字段；单一绑定解析实现维护工作线→设备锁序，ECS通过最小异步契约注入。事件组合键复用原注册与接收器，命令映射注入原DeviceRoutes，原EcsCommands/Sender/Result继续负责发送与结果。

**Tech Stack:** 现有Python/FastAPI/Pydantic/SQLAlchemy/Alembic/pytest/PostgreSQL/Redis；不加依赖。

**Spec:** [设计§10–§10.6](../specs/2026-10-27-device-workline-status-design.md#10-与现有-ecs-命令链路的边界)。

## Global Constraints

- 当前交付在feature/ecs-status-query-ship，基于develop复用独立角色框架；保持原工作区、暂存区、stash、配置和无关改动。
- 只用注入Mock/loopback HTTP与新建的一次性PostgreSQL/Redis；不连接真实ECS/WMS，不迁移现有数据库。
- 角色键为(device_role,event_type/task_type)，不依data形态自动选合同、不增加业务命令处理器注册框架。
- 配置门禁复用RouteSnapshot.version和现有管理版本；包括资料编辑的管理版本变化也停发，Status轮询不升版本。
- 历史只供查询及原合同结果接收，无历史发送授权清单；UNKNOWN/HELD不自动重放。
- 所有新迁移串接当前11e090516bf1，保持一条head。

## Review Focus

- 同设备同task_type换角色后附录不同，旧Result仍用原模型：Task 3。
- 换绑/删除与事件、发送门禁交错，锁序一致且不误ACK：Task 1/4。
- 未绑定事件ACK后补绑定的重复事件不执行业务：Task 2。
- 配置改回原值不恢复旧发送，原布局缺版本不猜测：Task 3/4。
- 原有工作区及配置不变，未执行的测试不算通过：Task 4。

## Task 1: 工作线绑定与共享解析

**Files:** models/workline.py、schemas/workline.py、services/workline_service.py、device_service.py；新增integrations/ecs_callback/binding.py、services/device_bindings.py及迁移；tests/services/test_device_bindings.py。

**Interfaces:** DeviceBinding不可变上下文含设备/工作线身份及版本、逻辑角色、设备和位置绑定；BindingResolver为async(session,device_code)->DeviceBinding|None；resolve_binding由设备服务实现，resolve_role(session,work_line_id,device_role)核对期望上下文。

- [x] RED：通过服务更新绑定后可解析角色；跨线/重复设备角色拒绝，已绑定设备移出/删除拒绝，解绑后成功。
- [x] GREEN：添加JSON字段与严格映射校验、管理事务及审计，绑定解析工作线先锁再锁设备并刷新复核。
- [x] 验证：相关服务/HTTP/迁移测试通过；新增迁移支持现有数据默认空绑定，有绑定时拒绝破坏性降级。

## Task 2: 事件组合键与注入分发

**Files:** ecs_callback/registry.py、receiver.py、dev_ecs_event.py、controllers/ecs_callback.py、main.py、dev_registry.py、schemas/dev_console.py；现有相关测试和文档。

**Interfaces:** ecs_event(event_type,*,device_role,data)；lookup(device_role,event_type)；receive_ecs_event(session,raw,*,resolve_binding=None)；include_ecs_callback(...,resolve_binding=...)。

- [x] RED：两角色同事件用互斥Payload处理；重复事件不访问绑定；稳定未知绑定ACK，解析故障503，错误Payload400且无receipt。
- [x] GREEN：复用原receipt和事务，扩展上下文与目录，正式处理器无解析器启动失败；local显式静态绑定不连接外部系统。
- [x] 验证：注册来源/发现/目录及事件全部相关回归通过，WMS注册不变。

## Task 3: 单一角色任务映射、门禁和迟到结果

**Files:** ecs_command/routing.py、services/device_routes.py、ecs_command/result.py、dev_registry.py及相关命令测试。

**Interfaces:** CommandBinding声明角色/task/附录身份和来源；DeviceRoutes接收声明序列及annexes，校验重复和缺失附录；resolve使用共享绑定解析，authorize_frozen核对当前配置版本，不读取历史发送授权。

- [x] RED：配置仍有效可发送，管理版本/改址/解绑/删除使门禁拒绝，旧Result在AUTHORIZATION_REVOKED暂停后仍可接收；无交付依据维持冲突。
- [x] GREEN：RouteSnapshot.version封装现有管理版本摘要，旧布局UNKNOWN，明确变化REVOKED，基础设施故障上抛；Result只放宽本次配置失效暂停，不改变其他身份/封锁规则。
- [x] 验证：命令/Result/重启/local回归，目录读取同一声明，零新增Sender或队列。

## Task 4: 独立库并发与整体验证

**Files:** tests中的现有并发/迁移测试及使用文档，必要的回归修复。

- [x] RED→GREEN：PostgreSQL交错事务覆盖配置先提交/门禁先提交、绑定迁移复核、A→B→A不恢复旧许可。
- [x] 运行一次性PostgreSQL/Redis迁移、相关集成与全量pytest、Ruff；只测试loopback Mock。
- [x] 全量增量复核；保存未提交验证记录，核对配置/index/stash及无关文件哈希；仅清理本任务新建测试资源。

## Execution record（历史记录）

- Baseline: 48 passed, 2 skipped, 38 deselected（相关SQLite测试；未运行PostgreSQL参数）。
- Ruling: 复用当前feature工作区而不另建空worktree；本次依赖已有未提交设备管理代码，整树基线已备份到任务私有目录，避免丢失或重做无关改动。

- Task 1: RED为WorkLineUpdate拒绝绑定字段；GREEN服务7项通过。新增字段沿用工作线版本/审计，原配置和历史表不删除。
- Task 2: RED为ecs_event缺device_role；GREEN事件/来源/发现/控制器110 passed。追加错设备解析回归先失败后通过；重复收据始终先于解析。
- Task 3: RED为删配置仍ALLOW、配置暂停Result返回409、缺附录仍部分装配；实现后相应用例通过。普通授权撤销仍拒绝结果，例外仅正式目录版本2配置暂停。
- Task 4: Mock/并发/迁移/目录定向16 passed、1 skipped（SQLite行锁项，PostgreSQL对应项通过）；SQLite迁移ALTER需显式无schema，已覆盖升级/拒绝有绑定降级/空绑定往返。
- Ruling: 各测试进程串行使用本任务独立数据库；初次误并行时清理夹具互相影响，已中断该次全量，结果不计验收。后续全量使用单进程新运行。
- Ruling: 工作区同步存在另一任务的Status简化（device_observation.py、device_status_service.py）；保留其内容，独立评审范围排除这些无关改动。

- Final review: fresh-context gpt-6-astra只读复核完成，无Critical，2个实现问题及1个验收缺口。已分别RED→GREEN：损坏origin/空task保持UNKNOWN；旧identity-map转移不能绕过解绑；等待锁期间删除也复核原归属。普通授权撤销Result限制保留。
- Final acceptance coverage: PostgreSQL补充了转移期间解析冲突且不持久ACK、门禁先持锁提交后配置变更再阻止后续尝试。对应定向6 passed、2 skipped（仅SQLite行锁参数）。
- Final: Ruling: 不放宽布局1/PRE_GATE_VALIDATION的通用Result规则；合法状态流中可能交付尝试进入HELD/DONE，不会作为PENDING再次领取，无可达证据要求新例外。错误扩大例外可能应用无依据结果，因此保持原证据和封锁判定。
- Final review declined scopes: 未独立重复主代理测试；未评审另一任务的Status简化/其他基线；未验证真实ECS/WMS或现场动作。上述范围保持用户原授权边界，测试由主代理最终完整运行给出证据。

- Final: fixed malformed snapshot classification — empty task/invalid origin/empty device code tests RED→GREEN; incomplete-field cases also remain UNKNOWN.
- Final: fixed stale-membership guards — cached session transfer and membership-changing delete lock-wait tests RED→GREEN; locks refresh candidate membership and reject changed original association.
- Final: completed acceptance gaps — PG transfer/delete during resolution has no receipt, gate-first/config-first ordering retains at most the original attempt, two real worklines reuse one role handler with distinct positions.
- Deferred minors: none. No additional independent re-review requested; fixes are verified by regression tests and the final full suite.

## 完成与验证

以下为角色增量当时的验证；当前整体验证与交付边界见[按需查询计划§5](2026-10-27-device-workline-status.md#5-当前文档自检与交付边界)。

2026-10-27，本次角色增量已完成，未提交/推送/部署。最终完整运行：2525 passed、27 skipped，160.58秒；27项均为SQLite参数下不适用的PostgreSQL专项，对应PG参数已通过。使用任务新建的loopback PostgreSQL 17、Redis 8和本地HTTP Mock，未连接真实ECS/WMS，未迁移现有业务库。

- Ruff check通过；387个Python文件格式检查通过；git diff --check通过。
- 单一Alembic head：c31f20261027，接11e090516bf1；SQLite及独立PG验证已有行默认空绑定、非空绑定保护降级及空绑定升降级往返。
- R01/R02：角色组合键、互斥Payload、两条真实数据库工作线复用同一处理器及各自位置上下文。
- R03/R04：跨线/重复角色/解绑保护、旧session转移及删除竞态、PG转移/删除期间解析回滚无receipt、重复事件先于绑定和Payload校验。
- R05/R06：同设备换角色选择不同附录；旧命令不重入新发送，旧Result按冻结模型校验；配置停发不取消设备，也不弱化普通授权/身份封锁。
- R07/R08/R09：目录及来源、local显式绑定、分层检查、单一Sender；PG双方锁先后；版本改回不恢复许可；损坏/缺失快照UNKNOWN、明确配置变化REVOKED。
- 独立代码复核提出的问题已全部通过RED→GREEN修复，验收缺口已补齐；未声称修复后又有独立复审。无遗留阻塞项或延后minor。
- .env、.env.example、git配置、原暂存区和stash已核对保留；另一任务的设备状态简化未回滚或纳入本次评审。

当前feature/ecs-status-query和全部未提交工作保留。仅清理本任务创建的两个独立测试容器，不清理用户原容器、配置、数据或工作区。
