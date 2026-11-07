# 粗分机业务子包分阶段实施计划

> 实施时逐项验证。经授权后使用 `superpowers:executing-plans`，或在明确授权分派后使用 `superpowers:subagent-driven-development`。复选框用于记录实施状态。

**目标** 从当前扫码、验证及原 `MOVE_FORWARD` Result 的停点继续推进，依次交付直分入箱、NG 搬运、补换架和仅退架。

**架构** 单盘作业负责原料盘和业务动作，架位准备负责等待盘、旧新架方案及分配代次。货架动作复用公共 RackDispatch，沿原 Transport 和 ECS Result 推进。通过现有 API、worker 和 5 秒 Beat 执行粗分业务待办。

**技术基线** Python 3.13+、FastAPI、SQLAlchemy、Alembic、PostgreSQL、Celery、pytest。

**设计依据** [接口草案](../../integrations/coarse-sorting-api-draft.md) · [状态机设计](../specs/2026-11-02-coarse-sorting-data-model-design.md) · [公共货架调度](../specs/2026-11-02-shared-rack-dispatch-design.md)

**状态** 本计划基于 `develop` 的 `da4f5bd` 评审，四阶段已完成隔离实现和独立评审。正式目录仍只启用扫码、WMS 验证和首个 `MOVE_FORWARD` / Result。四阶段复选框记录项目内 WES 开发及本地验收。对端签认和现场验收由联调阶段负责。

## 全局约束

- 沿用原扫码 ID、事件摘要、工作线、`binding_id`、验证请求和 `{原扫码id}:MOVE_FORWARD` 的原引用。迁移保留已保存事实及缺失状态。原请求未知时只恢复或核对原身份，明确业务 WAIT 到期或架位准备结清后才发新一轮分配。
- 保持 WMS 决定、ECS 物理结果、Transport 提交接纳和实际到位各自的权威来源。入箱结果与反馈义务分开。原结果可以在配置改绑或停用后按原引用消费，任何新物理动作及新分配仍检查当前许可。
- 在原业务事务内同时保存结果消费、状态、原动作引用及下一条出站意图。只从框架公开结果读口消费，沿现有 Sender、提交后唤醒及同一 `scan_pending` 的 Beat 兜底推进。
- 单盘与架位相互授权的推进事务在 PostgreSQL 首 SQL 前使用 `SERIALIZABLE`，按 [状态机锁序](../specs/2026-11-02-coarse-sorting-data-model-design.md#9-推进职责锁序与幂等)锁定并读取同点位活动准备和未完成动作。遇 `40001`（含提交）以新事务整体重试，最多三次。
- 架位策略只登记 F01 进场和退场。旧架原 F01 DEPART 的有效提交接纳完成 WES 调度，允许合法后继。新架必须按原任务、架号、点位和方向确认实际到位才可用于分配和放料。
- 测试使用模拟对端和独立数据库。正式注册沿用签认后的严格合同。当前装配未启用的合法业务分支保存原决定及待处理原因，无物理后继，也不按 `WAIT` 重求值。相应阶段启用后，按原身份及当前工作位事实复核原决定。示例字段及测试通过不代表现场已启用。
- 箱格合同调整后的隔离验证使用独立测试库。旧候选库中已登记的入箱命令与完成报告仍使用原身份和冻结正文。旧库和发送队列与新合同验证环境分开，原消息在其原合同下核对。缺少料格字段对的原入箱动作保留待核对。

## 联调阶段验收边界

| 边界 | 对应开发阶段 | 联调阶段需取得的证据 |
| --- | --- | --- |
| B1 进料背压与原盘身份 | 1、2 | ECS 明确出料抓取位占用、实际缓存满位及恢复规则，并能在同码双盘时按原盘执行 |
| B2 预留退出与真实高度 | 1 验证高度及反馈，3、4 验证旧预留退出 | WMS 确认同格可再次分配的高度依据、未使用旧目标的预留退出及仍有效预留对退架的限制 |
| B3 退架接纳与物理换架交接 | 3、4 | 旧架接纳后仍在位时物理互锁有效，新架到位后 WMS 使用可信的新架状态，冲突有明确核对及恢复入口 |
| B4 占位异常的恢复与责任退出 | 1 的入箱、2 的 NG、3 的 F01 进场 | 对端确认各原动作的证据来源、恢复负责人和安全交出条件。原任务仍未知时保持原责任 |

先为各阶段的正向和异常路径写可观察行为测试，确认现有实现缺少对应后继，再实现最小推进并复跑。迁移基于实施时唯一 Alembic head。文件路径为预计落点，以届时仓库布局为准。

阶段 1 提供共用的单盘作业与动作记录。阶段 2 复用该记录，验收时独立于货架流程。阶段 3 在阶段 1 的分配、入箱和反馈上加入共享准备。阶段 4 复用阶段 3 的原退场链及重分配。

## 阶段 1 直分入箱

**交付** 已有可用在位架时，原输送成功 → 原绑定申请分配 → `ALLOCATED` → 唯一 `PUT_TO_BIN` → 原成功 → 唯一完成反馈 → `COMPLETED`。

**文件** 修改 `app/businesses/coarse_sorting/{definition,composition,ecs_command,models,service}.py`。新增该业务 `request/inbound_coarse_sorting_storage_allocate_v1.py`、`request/inbound_coarse_sorting_execution_report_v1.py`、Job/Action 演进迁移和 `tests/businesses/test_coarse_sorting_direct_put.py`。

- [x] 从正式扫码入口构造已有的待分配记录，测试迁移前后的扫码 ID、原验证和输送引用一致。已有足够的原输送成功依据时可进入分配，缺失或不充分时保留待核对。迁移和推进都不重做原验证或输送。
- [x] 演进 `CoarseSortingScan` 为 Job，并引入单盘 Action 及原动作引用。使 `CoarseSortingService.advance(scan_id)` 和 `scan_pending()` 识别原待分配记录，保持 NG 停点，补换架和仅退架阶段再引入准备作业关联。
- [x] 接入 `storage_allocate@v1`、`OUTPUT_ARM`、`PUT_TO_BIN` 及 `execution_report@v1`。固定原工作线、绑定、目标架、单层架箱位 A/B/C/D、WMS 成对返回的料格储位编码和整数格位 1～7，以及当前高度。对合法 `WAIT` 保存一次到期点，按设计的 5000 ms 提案验证到期只登记一个新轮次，正式间隔以对端确认为准。响应未知时保持原请求。阶段未覆盖的架位决定按全局约束留证。
- [x] 覆盖错线、错箱格、错设备和 ACK 提前到达。只有原命令明确成功且能证明指定箱格已放料才登记报告。模拟报告响应丢失、原结果迟到、设备重新在线、配置改绑及重启，断言物理入箱至多一次，成功后只恢复同一报告。在模拟报文中用 120 mm 加 20 mm 的两盘同格场景核对 WMS 高度透传，并验证满位及同码双盘时原命令身份保留。对端未证明安全退出时，保留未知入箱的原占位及预留。
- [x] 运行 `rtk uv run pytest tests/businesses/test_coarse_sorting_direct_put.py tests/businesses/test_coarse_sorting_migration.py tests/businesses/test_coarse_sorting_forward.py -q -ra`。使用独立 PostgreSQL 验证迁移、重复推进和原身份保留。

## 阶段 2 NG 搬运

**交付** 原 WMS 业务 NG → 原入口机械臂唯一 `MOVE_TO_NG` → 原明确成功 → `NG_COMPLETED`。

**文件** 修改 `app/businesses/coarse_sorting/{composition,ecs_command,service}.py`，新增 `tests/businesses/test_coarse_sorting_ng.py`。复用阶段 1 的 Job/Action。

- [x] 通过原业务 NG 决定和历史待 NG 记录验证原原因保留。WMS 协议拒绝、通信未知及不合法结果产生零条 NG 命令。
- [x] 为 `INPUT_ARM` 装配严格命令和结果合同。用原作业、原原因、原工作线和首次登记时核验的设备冻结一个动作。原可信成功闭合 NG，出站计数中正常输送、分配、入箱和报告均为零，也不另报 NG 结果。
- [x] 注入同码双盘、错设备或错 NG 目的地、满位、迟到成功及失败/未知。隔离测试核对原盘、设备、冻结工位及重复唤醒的命令数量。失败或未知时保留原占位责任。
- [x] 运行 `rtk uv run pytest tests/businesses/test_coarse_sorting_ng.py tests/businesses/test_coarse_sorting_wakeups.py -q -ra`。

## 阶段 3 补架与换架

**交付** 同工作线/工作位的多盘共享唯一活动准备作业。补架只走原新架 F01 进场。换架先等既有直接动作收口，再求旧架去向，原 F01 退场有效接纳后登记新架进场。原新架实际到位才恢复各盘分配。

**文件** 修改 `app/businesses/coarse_sorting/{composition,models,service}.py` 和分配请求模型，新增准备作业及代次迁移、`tests/businesses/test_coarse_sorting_rack_preparation.py`。公共变更落在 `app/businesses/_components/rack_dispatch/`；原任务结果更正按[独立设计](../specs/2026-11-02-transport-result-correction-design.md)在 Transport 自身交付。

- [x] 通过真实推进入口测试两盘并发建准备、旧分配迟到以及新入箱与退架交叉。验证同点位仅一份活动准备，新入箱与退架不能同时取得冲突授权，已登记入箱按原身份收口，另一工作线继续独立推进。
- [x] 粗分装配 RackDispatch 的单一共用离场合同与粗分 F01 来源策略。实现 `advance_preparation(preparation_id)` 并接入业务待办。准备记录保存方案、代次和前序，搬架动作引用原公共 RackJob/RackAction。 `rack.departure_decide@v1` 的合法 WAIT 按原到期点续问，原响应未知保持原请求。
- [x] 新架到位须匹配原架、工作位、方向及已应用成功结果。隔离验证覆盖明确错向、仅接纳和新架到位后 WMS 返回旧架。通过模拟 Transport 公开读口的更正结果验证同一原 ENTER 恢复。旧架仍在位时的实物互锁留待现场验收。更正能力交付前保留原失败任务待核对。普通进场成功若可能缺省方向，确认其 SUCCESS 是否明确保证原目标面，否则不能凭补齐字段授予生产资格。
- [x] 原准备就绪后，各盘沿原 `binding_id`、新 `operation_id` 重新分配；迟到旧目标只留证。旧预留实际结清留待 B2 验收。测试已放料待反馈不阻塞安全交接，未知入箱仍阻止退架。
- [x] 运行 `rtk uv run pytest tests/businesses/test_coarse_sorting_rack_preparation.py tests/businesses/rack_dispatch -q -ra`。在独立 PostgreSQL 验证交叉授权、回滚和并发去重。

## 阶段 4 仅退架

**交付** `DEPART_REQUIRED` → 唯一 `DEPART_ONLY` 准备 → 原架安全交出 → 原 F01 DEPART 有效接纳 → `DEPART_ACCEPTED` → 各盘重新分配。

**文件** 修改 `app/businesses/coarse_sorting/{models,service}.py`、分配结果模型及阶段 3 的架位推进，新增 `tests/businesses/test_coarse_sorting_depart_only.py`。

- [x] 严格校验原决定的工作线、旧架和字段。同点位两盘并发只能关联同一准备作业。已登记的入箱/搬运按原身份收口后，复用阶段 3 的离场求值和唯一 DEPART。
- [x] 以原有效 202/`RECEIVED` 或 200/`DUPLICATE` 进入 `DEPART_ACCEPTED`，覆盖库区及不同工作位去向。分别验证接纳缺失、合法接纳后无最终回调和接纳后迟到失败。接纳缺失保持原动作；接纳后的调度保留，WMS/RCS/ECS 按 B3 处置物理占位。
- [x] 各等待盘沿原 `workline_code`、`binding_id` 分别登记一轮新分配。仍无新架走明确 `WAIT`，有新架走阶段 3 的 `REPLENISH_REQUIRED`。仅退架的原动作链止于 DEPART，迟到决定留证，仍把旧架当作可分配或可再退场的决定记 `RACK_STATE_CONFLICT`，零重复退架、零旧架放料。
- [x] 按状态机 §8 验证空闲退架的独立触发。原宿主确认无本架有效需求、无未决直接动作且可安全交出时，沿原公共 RackJob 离场。尚未知的需求或占位继续核对，已放料的独立报告继续恢复。
- [x] 运行 `rtk uv run pytest tests/businesses/test_coarse_sorting_depart_only.py tests/businesses/test_coarse_sorting_rack_preparation.py -q -ra`。

## 阶段验收记录

代码验证基线为 `f533b50`，Alembic 唯一头为 `coarserackstage3`。按仓库 CI 三组划分，在隔离 PostgreSQL、Redis 和 SQLite 上执行全量回归。

| 验证组 | 通过 | 按条件跳过 | 失败 |
| --- | ---: | ---: | ---: |
| 迁移 | 245 | 85 | 0 |
| BIN_Roller 与 SMT 手工 | 1023 | 3 | 0 |
| 其余用例 | 5064 | 50 | 0 |
| 合计 | 6332 | 138 | 0 |

Ruff、格式及差异检查通过。仅退架已有并发、原接纳、迟到结果、重分配和空闲退架回归。错误工作线、额外新架字段和原旧架来源冲突由现有合同及授权检查处理，尚无逐项专用负向用例。

### 完整流程隔离回归

以 `develop` 的 `eb69399` 为基线，在同一隔离装配启用直分入箱、NG 和共享货架推进，复用实际接收器、Sender 与 `scan_pending`。验证有新架方案的换架，以及仅退架获接纳后经 `WAIT` 再补架。两条路径均在同码正常盘等架期间，按独立原命令完成 NG。正常盘在原新架到位后沿原绑定重新分配，原入箱成功后回传 WMS 格位字段对，报告收妥后闭合。

粗分业务和合同检查回归共 483 项通过、16 项按条件跳过。两条新增路径在 SQLite 和独立 PostgreSQL 各执行一次，4 项均通过。正式目录仍只启用原首段，旧候选队列保持隔离，Transport 原任务结果更正独立交付。

### WES 联调装配

在既有 `enterprise-backend-int` 保持 `ENVIRONMENT=local`，显式配置 `COARSE_SORTING_TAIL_ENABLED=true`。API、worker 和 Beat 从同一份站点配置加载联调合同，与其他业务子包共用本项目数据库、Redis、Sender、提交后唤醒和 `scan_pending`。粗分新增分配、完成报告、共享离场求值，以及 `PUT_TO_BIN` 和 `MOVE_TO_NG`。开关默认关闭，`production` 拒绝启用，正式目录继续只装配原首段。

- 升级前核对本项目队列中没有旧候选箱格合同的消息。旧候选库、队列和旧 WES 继续使用各自原环境，首段待办留在本项目原库并按原身份继续。
- `RELIABLE_TARGETS` 分别配置唯一一个 `wms-decisions`、`wms-facts` 和 `wms-transport` 静态目标。缺少、重复或使用快照地址模式时，API 和 worker 拒绝启动。ECS 地址沿用设备路由。模拟回归使用临时数据及模拟对端，现场联调沿用站点配置。
- 通过工作线配置绑定 `INPUT_ARM`、`CONVEYOR`、`OUTPUT_ARM` 和 `OUTPUT_RACK`。用 JSON 对象 `COARSE_SORTING_OUTPUT_LOCATIONS`、`COARSE_SORTING_NG_LOCATIONS` 配置工作线编码到 ECS 结果位置的映射，例如 `{"COARSE-A":"OUTPUT-POINT"}` 与 `{"COARSE-A":"NG-POINT"}`。非法映射在启动时拦截。缺少某条线的末端配置时保持原待办，不登记相应后续命令。
- 粗分分配和执行报告必须由粗分业务推进发送。调试控制台可查看合同，但不提供这两项的手工触发。
- 备份现有联调库后执行迁移，再协调重启 API、worker 和单个 Beat。原身份恢复仍核对原证据及当前授权，Transport 原任务结果更正继续独立交付。

## 联调与正式启用前置

以下事项按归属分别跟踪。相应流程在启用前须满足这些条件。

- 联调阶段完成 B1–B4 对端签认与现场验收。按对应阶段核对 WMS 分配及反馈、ECS 入箱及 NG 成功证据、`DEPART_REQUIRED` 报文，以及退架接纳后仍无新架的等待。
- 独立开发交付 Transport 原任务结果更正。项目内隔离测试已覆盖粗分消费更正后的公开结果。联调阶段须覆盖已启用更正策略的原 ENTER 返回终态 `FAILED`、报告架位于明确外部位置、随后更正为 `SUCCESS` 的场景，核对共享调度持续保有原责任，原作业位置与粗分准备状态依据同一更正结果推进。缺少合法原结果时继续保留原任务及责任。
- 启用阶段协调数据库、API 和 worker 升级，按已签认合同启用相应流程。
