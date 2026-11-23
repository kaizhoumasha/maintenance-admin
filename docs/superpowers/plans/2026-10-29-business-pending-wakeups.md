# 按业务唤醒 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 按业务通知即时调用原待办入口，业务只登记一次推进方法，平台统一装配和恢复调度。

**Architecture:** catalog 的 PROGRESSION 声明绑定服务异步方法；接收/结果端口在提交后根据可信归属发布 business_code；统一 Celery 任务与自动 Beat 调度共用业务 scan_pending。

**Tech Stack:** Python、SQLAlchemy、Celery、pytest、独立 SQLite/PostgreSQL/Redis、回环 MOCK。

**Spec:** [按业务唤醒设计](../specs/2026-10-29-business-pending-wakeups-design.md)

**最新已验证状态（2026-10-30）：** 按业务唤醒、Beat 发布修复及相关测试和文档已通过 [PR #39](https://github.com/kaizhoumasha/maintenance-admin/pull/39)，以 merge commit [`a662fb8`](https://github.com/kaizhoumasha/maintenance-admin/commit/a662fb872799501d815a1ad71d5cdada8839aec3) 合入 develop；[该合并提交的 develop CI](https://github.com/kaizhoumasha/maintenance-admin/actions/runs/41063027336) 已通过。业务验收仍限定为 MOCK/内部验证，该次合入未执行部署，不代表现场业务或物理闭环验收。

**历史范围说明：** 下文约束、Task 1–2 和执行记录保留原实施与复评时点，包括当时的未提交、不合并限制及测试统计，不作为当前 Git 状态。原三份部署计划中的 `6f0f86c` / `b68afec` 交付属于较早阶段的独立历史，不作为本次按业务唤醒实现已部署的证据。

## Global Constraints（原实施阶段）

- 在当前 feature/coarse-sorting-site-configuration 原位实施；用户已明确选择方案并授权设计、计划后继续，无提交步骤。
- 不提交、推送、合并、部署、触发设备或真实 WMS；原三份已暂存计划、index、stash、历史记录及交付快照保持原样。
- 业务事务与持久 pending 保持原语义；允许平台共享装配/调度壳，不增加业务状态机、第二 Sender、数据库表或迁移。
- 通知只含 business_code；即时发布沿用 ignore_result=True、retry=False、独立连接、0 次连接重试及 1 秒连接/socket 预算；共享任务及 Beat 调度选项均显式 ignore_result=True，Beat 每 5 秒调用同一任务，其发布连接由 Celery 调度器管理。
- 本轮不合并跨事务通知；明确测量重复扫描成本，不将本机 MOCK 耗时作为现场容量承诺。

## Review Focus

- 归属来自已校验 catalog/注册源；未知、local、平台声明与无推进入口的业务不被错误路由。
- 保存点/外层提交、拒绝与重放路径严格区分；通知失败不得变更已提交 ACK 或事实。
- 即时与 Beat 必须调用同一业务待办方法；不同业务调度相互独立，不能接管业务查询和事务。
- 重复、并发、密集通知与重启恢复保持原 WMS/命令身份，后页与新待办能被处理。
- 清除旧公共用法及任务注册；历史记录仅加后续索引，不重写旧测试或交付事实。

## Task 1: 平台调度及提交接缝收敛

**Files:** 修改 app/businesses/catalog.py、runtime.py、coarse_sorting/composition.py、service.py、app/business_wakeups.py、main.py、可靠发送及 ECS/WMS 接收端口；新增 app/tasks/business_tasks.py，删除 coarse_sorting_tasks.py；迁移相关 tests/businesses、tests/reliable、tests/tasks 测试。

**Interfaces:** composition.PROGRESSION="scan_pending"；catalog.progressions() 返回 business→方法名；catalog.progression_owner(source) 返回正式业务或 None；BusinessRuntime.progressions 绑定异步方法；enqueue_business(business_code)；advance_pending(factory, business_code)；Celery business.advance_pending(business_code)。结果端口分别接收 operation / owner_domain；输入端口接收 RegistrationSource。

- [x] 编写归属、统一任务、自动受理、三个结果边界和原发布合同测试；运行确认缺少新行为的 RED。
- [x] 实现声明校验、统一任务/自动 Beat/通知端口；删除逐条反查、手动唤醒能力、硬编码业务路由与专属任务。
- [x] 更新原回滚、失败、重复、并发、重建恢复和主应用测试，补齐入站 Accepted 通知及拒绝/重放负向；专项 GREEN。

## Task 2: 代表性 MOCK 验证、文档与完整复评

**Files:** tests/businesses/test_coarse_sorting_wakeups.py、相关负载测试、app/tasks/_db.py、tests/tasks/test_task_database.py；AGENTS.md、docs/integrations 下开发约定/事务配方/结果消费/调试文档；原提交唤醒设计与计划仅追加历史索引，本设计与计划回填本轮证据。

**Interfaces:** 1/25/100 条待办与 1/10/20 次业务通知；记录扫码到原 WMS/命令/结果的状态、遍历数、外部意图数量及耗时。

- [x] 先新增多条待办由一次业务通知处理、重复密集通知不重复登记意图、阻塞前页、多业务隔离及持续入站时单次扫描有界的行为测试，RED→GREEN。
- [x] 执行代表性 MOCK 并记录实测成本与边界；只使用本轮独立测试资源。
- [x] 全量 pytest、Ruff、格式、单 head/check；核对旧用法仅出现在明确历史记录。
- [x] 一次独立最终评审，按实际问题修复并验证；归档证据，核对原计划/index/stash/历史/容器保全，只清理本轮资源。

## 历史执行记录（实施与复评阶段）

平台与原边界专项 286 passed / 4 skipped，含实际主应用、两个业务隔离、发布失败与保存点合同。最新通知/负载专项 33 passed / 1 skipped，覆盖起始 ID 上界、101 条阻塞待办后的后页、三组双数据库 MOCK。以下保留早期快照；最终复评及完整回归已在文末闭环；原提交 4b7fb80 属于历史基线，不作为本轮新行为的验证结果。


### 历史：首次全量与复用测试工厂的矩阵

- 复评修复前全量 **3637 passed / 36 skipped**；Ruff 通过，489 文件格式检查通过；单 head/current 为 ea8b20261029，check 无新增升级操作，无新迁移。跳过为 SQLite 不适用的 PostgreSQL 专用语义，对应 PostgreSQL 用例已运行。
- 起始 ID 上界测试先观察 2 个 RED，再通过固定本轮候选上界结束扫描；新增事实仍有独立通知与 Beat 恢复。101 条绑定失效待办不会挡住后页另一工作线。
- 本节首轮矩阵使用实际 HTTP 受理、共享推进函数（复用测试会话工厂）、回环 WMS/ECS、公开 Result 入口；将同一业务信号连续执行指定次数。在待办阶段暂不发送 WMS 请求，刻意测量结果未到时的重复检查；随后发送原请求并完成 Result 消费至等待分配，原消息/命令均只各生成一次。

| 待办 | 连续重复通知 | 实际检查 | SQLite 待办阶段秒 | PostgreSQL 待办阶段秒 | PostgreSQL 闭环秒 | 原 WMS/命令数 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 1 | 1 | 0.007 | 0.02 | 0.145 | 1/1 |
| 25 | 10 | 250 | 0.406 | 1.864 | 2.756 | 25/25 |
| 100 | 20 | 2000 | 3.133 | 12.853 | 15.941 | 100/100 |

计时从首个待办任务开始，排除准备扫码受理；待办阶段包含首次登记验证意图及等待结果的重复扫描，闭环包含后续 MOCK 发送、结果消费和空扫描。表格不是单条事件延迟分位数、生产吞吐或现场容量保证。100×20 实际检查 2000 次，重复扫描有可见成本；本轮没有引入通知合并或额外协调状态。

证据根目录：`/Users/kaizhou/.codex/recovery/maintenance-admin/business-pending-wakeups-20261030T023942Z`。

| 项目 | 证据 |
| --- | --- |
| 全量 | [full.log](</Users/kaizhou/.codex/recovery/maintenance-admin/business-pending-wakeups-20261030T023942Z/full.log>) |
| 原边界与平台专项 | [task1-final.log](</Users/kaizhou/.codex/recovery/maintenance-admin/business-pending-wakeups-20261030T023942Z/task1-final.log>)、[task1-green2.log](</Users/kaizhou/.codex/recovery/maintenance-admin/business-pending-wakeups-20261030T023942Z/task1-green2.log>) |
| 最终通知/负载矩阵 | [task2-final-mock.log](</Users/kaizhou/.codex/recovery/maintenance-admin/business-pending-wakeups-20261030T023942Z/task2-final-mock.log>)、[mock-metrics.json](</Users/kaizhou/.codex/recovery/maintenance-admin/business-pending-wakeups-20261030T023942Z/mock-metrics.json>) |
| 静态与迁移检查 | [lint](</Users/kaizhou/.codex/recovery/maintenance-admin/business-pending-wakeups-20261030T023942Z/lint.log>)、[format](</Users/kaizhou/.codex/recovery/maintenance-admin/business-pending-wakeups-20261030T023942Z/format.log>)、[heads](</Users/kaizhou/.codex/recovery/maintenance-admin/business-pending-wakeups-20261030T023942Z/heads.log>)、[current](</Users/kaizhou/.codex/recovery/maintenance-admin/business-pending-wakeups-20261030T023942Z/current.log>)、[check](</Users/kaizhou/.codex/recovery/maintenance-admin/business-pending-wakeups-20261030T023942Z/schema-check.log>) |

本轮测试资源初始就绪探测误命中 PostgreSQL 初始化临时服务，创建第三个可丢弃数据库时连接中断；随后等待本轮容器的 TCP 服务就绪并只补建缺失数据库。该基础设施准备偏差未涉及原数据库、容器或现场；保留过程输出，不作为业务测试失败。原三份计划及交付快照不回填新实现状态，本轮仍未提交、推送、合并或部署。


### 最终复评修复与真实任务体矩阵

独立 gpt-6-astra 对 42 项未提交增量只读复评，发现 1 个 Important/P2：异步推进入口带必填参数时启动接受、后续即时/Beat 持续失败。已用位置参数/keyword-only 参数四例观察 RED，再在 runtime 以 signature(bound_method).bind() 校验无参调用；推进专项 14 passed，可选 limit 的原业务方法仍通过。

负载矩阵现执行注册 Celery 任务的实际 run 任务体，在独立线程中逐次创建/释放该任务自己的引擎，计入任务资源生命周期；未计真实 broker 排队或 worker 进程调度。首次真实任务体 PG100×20 受强制 NullPool 影响，待办阶段 46.274 秒、闭环54.478秒。补实际连接创建/跨循环隔离/异常释放测试，先观察 6 连接而非预期 2 的 RED，再改为任务内使用默认连接池；引擎仍每任务新建并在同一事件循环释放，无跨任务/API 复用。资源与推进/发布专项共19 passed。

| 待办 | 连续通知 | 实际检查 | SQLite任务体待办秒 | PostgreSQL任务体待办秒 | PostgreSQL任务体闭环秒 | WMS/命令数 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 1 | 1 | 0.009 | 0.036 | 0.196 | 1/1 |
| 25 | 10 | 250 | 0.469 | 1.983 | 3.052 | 25/25 |
| 100 | 20 | 2000 | 3.16 | 14.635 | 18.142 | 100/100 |

上述最终真实任务体矩阵 6 passed；计时口径与上节一致，但包含逐任务资源成本。100×20 依然检查2000次，14.635秒为整个连续待办检查阶段累计值，不能当作单条结果延迟或吞吐保证。旧首轮及 NullPool 计时保留为历史，不覆盖。

最终修复后的完整回归 **3643 passed / 36 skipped**；Ruff 通过、490 文件格式检查通过，最终 head/current 仍为 ea8b20261029，check 无新增升级操作。上述首轮3637统计保留为历史。

| 复评/改进项 | 证据 |
| --- | --- |
| 独立复评与必填参数回归 | [final-review.md](</Users/kaizhou/.codex/recovery/maintenance-admin/business-pending-wakeups-20261030T023942Z/final-review.md>)、[RED](</Users/kaizhou/.codex/recovery/maintenance-admin/business-pending-wakeups-20261030T023942Z/final-arity-red.log>)、[GREEN](</Users/kaizhou/.codex/recovery/maintenance-admin/business-pending-wakeups-20261030T023942Z/final-arity-green.log>) |
| 资源生命周期回归 | [RED](</Users/kaizhou/.codex/recovery/maintenance-admin/business-pending-wakeups-20261030T023942Z/task-pool-red.log>)、[GREEN](</Users/kaizhou/.codex/recovery/maintenance-admin/business-pending-wakeups-20261030T023942Z/task-pool-green.log>) |
| 真实任务体首轮与最终 | [首轮](</Users/kaizhou/.codex/recovery/maintenance-admin/business-pending-wakeups-20261030T023942Z/final-native-mock.log>)、[最终](</Users/kaizhou/.codex/recovery/maintenance-admin/business-pending-wakeups-20261030T023942Z/final-native-pooled.log>)、[最终指标](</Users/kaizhou/.codex/recovery/maintenance-admin/business-pending-wakeups-20261030T023942Z/mock-native-final-metrics.json>) |

评审未判断的生产队列/吞吐SLA、真实杀进程/broker故障、Transport自动归属及现场部署验收继续按边界保留：MOCK 不承诺容量，重建恢复不冒充真实故障演练，Transport 无归属映射仍扫描兜底，本轮不部署。没有推迟的 Minor，也不新增业务级操作或配置项。


### 最终证据与保全

- [最终全量](</Users/kaizhou/.codex/recovery/maintenance-admin/business-pending-wakeups-20261030T023942Z/full-final.log>)、[最终 Ruff](</Users/kaizhou/.codex/recovery/maintenance-admin/business-pending-wakeups-20261030T023942Z/lint-final.log>)、[最终格式](</Users/kaizhou/.codex/recovery/maintenance-admin/business-pending-wakeups-20261030T023942Z/format-final.log>)、[head](</Users/kaizhou/.codex/recovery/maintenance-admin/business-pending-wakeups-20261030T023942Z/schema-final-heads.log>)、[current](</Users/kaizhou/.codex/recovery/maintenance-admin/business-pending-wakeups-20261030T023942Z/schema-final-current.log>)、[check](</Users/kaizhou/.codex/recovery/maintenance-admin/business-pending-wakeups-20261030T023942Z/schema-final-check.log>)。
- [评审问题闭环](</Users/kaizhou/.codex/recovery/maintenance-admin/business-pending-wakeups-20261030T023942Z/resolution.md>)、[最终源码哈希](</Users/kaizhou/.codex/recovery/maintenance-admin/business-pending-wakeups-20261030T023942Z/final-source-hashes.json>)、[保全核对](</Users/kaizhou/.codex/recovery/maintenance-admin/business-pending-wakeups-20261030T023942Z/preservation.json>)。独立复评之后的一次修复经负向回归及全量关闭，没有把后续改动冒充已经第二次独立评审。
- 原三份计划工作副本/index 和8个stash保持原样，269个原历史/交付文件哈希一致，其他原本地文件保留；11个原容器基线一致，仅清理本轮两个带任务标签的测试容器。
- 成果仍在当前功能分支未提交；不推送、不合并、不部署，不声称现场业务闭环完成。


### 2026-10-30 复评增量：Beat 发布与事务表述

- [x] 复现 P2：实际 Celery `Scheduler.apply_async` 在任务已注册和未注册（`send_task`）两条路径下，因结果后端订阅异常而在消息入队前失败；共享任务默认发布同样失败。新增负向回归先取得 **3 failed / 3 passed**，未访问真实 Redis 结果后端。
- [x] 仅在共享任务声明和自动生成的 Beat 调度选项补 `ignore_result=True`；保留即时发布的原连接预算及失败处理，不增加配置、调度层或业务接线。专项取得 **6 passed**，并通过真实调度计算验证第 4 秒未到期、第 5 秒到期及实际消息入队。
- [x] 关闭 P3 表述歧义：`scan_pending` 的短会话只定位候选 ID；每条 `advance` 才创建独立 session，在首条 SQL 前配置 REPEATABLE READ 并按原锁序消费结果。任务内连接复用不表示跨记录复用事务。公共结果说明、业务包指南、事务配方及设计同步，指南与配方示例由业务 `advance` 自己拥有事务。

上述负向回归使用实际 Celery/Kombu 和内存 broker，只将结果后端订阅接缝替换为异常；不启动现场 worker，不发真实命令。它证明 broker 可用时发布不因结果后端订阅而失败，不代表 broker/Redis 本身宕机时仍可投递。即时发布的独立连接预算与 Beat 自身的连接管理分开描述。原三份计划、历史验收记录、计时和交付包快照不重写；本节只记录本次后续修复。

本次全量 **3646 passed / 36 skipped**（306.95 秒），最终推进/发布/资源专项 **22 passed**；Ruff 通过，490 文件格式检查通过。迁移 head/current 均为 `ea8b20261029`，串行 check 无新增升级操作，无新迁移。本次复核时点（UTC）：`2026-10-30T11:20:54+00:00`；具体输出来源如下，不以既有部署快照代替本轮验证。

| 本次证据 | 输出来源 |
| --- | --- |
| 实际 Beat 发布的负向与通过回归 | [RED](</Users/kaizhou/.codex/recovery/maintenance-admin/beat-result-review-fixes-20261030T111101Z/red.log>)、[GREEN](</Users/kaizhou/.codex/recovery/maintenance-admin/beat-result-review-fixes-20261030T111101Z/green.log>)、[最终专项](</Users/kaizhou/.codex/recovery/maintenance-admin/beat-result-review-fixes-20261030T111101Z/final-focused.log>) |
| 当前工作区全量与静态检查 | [全量](</Users/kaizhou/.codex/recovery/maintenance-admin/beat-result-review-fixes-20261030T111101Z/full.log>)、[Ruff](</Users/kaizhou/.codex/recovery/maintenance-admin/beat-result-review-fixes-20261030T111101Z/lint.log>)、[格式](</Users/kaizhou/.codex/recovery/maintenance-admin/beat-result-review-fixes-20261030T111101Z/format.log>) |
| 迁移状态与最终差异检查 | [head](</Users/kaizhou/.codex/recovery/maintenance-admin/beat-result-review-fixes-20261030T111101Z/schema-heads.log>)、[current](</Users/kaizhou/.codex/recovery/maintenance-admin/beat-result-review-fixes-20261030T111101Z/schema-current.log>)、[串行 check](</Users/kaizhou/.codex/recovery/maintenance-admin/beat-result-review-fixes-20261030T111101Z/schema-check-final.log>) |
| 本次复评与保全 | [复评记录](</Users/kaizhou/.codex/recovery/maintenance-admin/beat-result-review-fixes-20261030T111101Z/review.md>)、[保全核对](</Users/kaizhou/.codex/recovery/maintenance-admin/beat-result-review-fixes-20261030T111101Z/preservation.json>)、[最终文件哈希](</Users/kaizhou/.codex/recovery/maintenance-admin/beat-result-review-fixes-20261030T111101Z/final-tree.json>) |

执行偏差：收尾时将迁移反射检查与会创建/删除独立业务测试表的专项并行，临时表在反射期间被清理，首次 check 发生 `NoSuchTableError`。专项 22 例结束后串行重跑 check 通过；[首次失败输出](</Users/kaizhou/.codex/recovery/maintenance-admin/beat-result-review-fixes-20261030T111101Z/schema-check.log>)保留，不冒充业务失败或数据库迁移变更。仅涉及本轮可丢弃测试库，不涉及原有数据服务。

本次为主代理复评，检查本轮八个文件增量及即时发布、Beat、任务资源、扫描和单条推进边界，未发现新的需修复问题；不将本轮记录称为第二次独立评审。原三份计划及 index、历史和交付文件保全结果以本次核对文件为准。保持当前功能分支，不提交、推送、合并或部署。
