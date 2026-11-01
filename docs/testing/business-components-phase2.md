# WMS / Transport 原业务归属与即时唤醒：阶段二验收

> **历史时点说明：** 本文执行、测试、迁移 head 及“本轮”状态按各节标明的 2026-10-30 时点解读；“当时未实施阶段三”等记录不代表当前 BIN_Roller 状态。当前交付边界统一见 [BIN_Roller B1～B7 实施计划](../superpowers/plans/2026-10-30-bin-roller-implementation.md)。

日期：2026-10-30。阶段二实现及隔离验收已完成；已按用户追加授权复审并提交 a34b28d。未部署，阶段三未实施，bin_return/SMT 未注册。

## 实现与兼容边界

工作线只关联业务；一个业务可组合多个内部代码能力。每个业务独立绑定 WmsRequests/TransportService，内部能力复用本业务 capabilities，共享原 Outbox、ECS 能力和 session_factory。没有组件 owner、独立 progression、事件总线或第二套 Sender/任务调度。

| 能力 | 验证的行为 |
|---|---|
| WMS 原归属 | 本地 business_key 保存 business:<业务码>；发送前核对合同消费者。新建、串行重放和 PostgreSQL 并发胜者返回持久化原关联，同一身份不能换业务或接管旧 None；wire/TTL 不变 |
| WMS 结果通知 | 有效非快照终态保存后，经最外层 after_commit 通知原业务；普通完成、本地 EXPIRED、过期租约恢复/释放均覆盖。重试、失效租约完成或回滚不通知 |
| Transport 原归属 | 四种意图统一保存/核对 nullable owner_domain；串行及唯一约束竞争不改原 owner。原 business_key=transport_task_id、input_digest、冻结字节、caller/cycle 和外部身份保持 |
| Transport 应用通知 | 仅 applied_outcome_revision 实际前进时通知原 owner。结果/位置两种到达顺序均覆盖；ACK、单独位置事实、暂存结果、重复/冲突、无冻结或未知任务不冒充应用 |
| 装配及恢复 | 真实主应用 lifespan/HTTP、Celery/Kombu 内存消息及实际任务体串联。即时通知与 5 秒 Beat 复用同一业务 scan_pending；发布失败保留事实/ACK/pending，恢复仍使用原意图 |

旧业务专属 WMS 的 None 关联沿原 RegistrationSource 通知；共享历史请求缺关联只留安全诊断，不猜测或广播。
Transport 历史 None 不回填、不自动唤醒；已知原身份仍可通过公开结果读口消费，由原 pending/Beat 兜底。绑定实例不能以同一身份接管旧 None。owner 投影只是原关联，不授予新动作权限。

WMS 消费者查询由可信装配端口注入，通用集成层不依赖业务 catalog。自定义或测试 Outbox 端口也需返回完整 EnqueueResult（含原 business_key）。Transport 通知复用主应用既有 business_committed，由 WMS 控制器透传，业务组件不自行发布任务。

## 迁移

新增 revision **f0b120261030**，down_revision **ea8b20261029**；增加 nullable Text owner_domain，无历史回填。旧行、冻结请求及请求字节保持。

有非空 owner 时降级明确拒绝删除该列；PostgreSQL 降级先持表锁，防止检查后并发写入。SQLite 验证本迁移往返，完整历史 base/head 往返在独立 PostgreSQL 验证；历史 audit 的 SQLite LOCK 限制不在本阶段修改范围。

运行环境尚未应用新迁移；后续部署须先迁移，再启用依赖新列的进程。

## 复审与提交边界

主审与一次独立只读复审检查 34 个相关路径，未发现 P0/P1/P2/P3 级可行动缺陷；现有生产改动集中在原接缝，无需额外抽象或拆分。此次优化集中在验收记录：合并重复历史日志、明确旧空归属行为、迁移方向与降级保护，更新实际提交授权。

提交仅包含阶段二生产代码、迁移、测试和本记录共 31 个路径。混有其他修改的集成指南、规划及业务接口文档保留原工作树和暂存内容，不并入此提交；不提交配置或测试凭据。阶段一基线为 90861b7。

测试业务只存在于 fixture；独立 MetaData 表 finally 清理，不加入生产 Base。通知保存点、rollback/close/提交失败、UNKNOWN 新版本、确定终态冲突及发布失败均有对应测试。

## 验证记录

初次实现全量：3788 passed / 39 skipped（299.81 秒）。提交前复审使用重新创建的可丢弃 PostgreSQL 17 / Redis 7，普通测试库与四套迁移专用库相互独立。提交前复审全量：**3791 passed / 36 skipped，322.09 秒，exit 0**。此前未配置的三套既有迁移专用库本轮已配置并通过；36 项跳过均为 SQLite 不适用的 PostgreSQL 锁/并发分支，对应 PostgreSQL 形态已执行。

专用库串行 upgrade head、alembic check（无结构差异）、唯一 head f0b120261030 及 downgrade base 全部通过。普通测试库实际版本同为该 head；所有 schema 中本阶段组件测试表已清理。提交前复审的两个有任务标签的测试容器及匿名卷已删除，既有服务未动。

Ruff 检查及格式检查通过（507 个文件无需重新格式化）；源码与此次全量测试使用的集合及 hash 保持一致。

日志及本轮原暂存/工作树保全快照保留在 .superpowers/sdd/2026-10-30-phase2-review/；初次实现日志保留在 .superpowers/sdd/2026-10-30-business-components-phase2/。

隔离验收未覆盖真实 WMS/ECS、独立生产 worker 进程或现场物理链路；内存 broker 与真实任务体不作为这些环境的验收证明。

计划验收复核（2026-10-30）：P2.1～P2.4 全部满足阶段出口，逐项证据见[阶段二计划验收索引](../superpowers/plans/2026-10-30-business-components-phase2.md#按计划验收复核与当前进度2026-10-30)。本轮核对已提交源码与上次 3791/36 全量版本一致，仅同步进度，不重复全量或新增提交。

联合 ship 验证（2026-10-30）：补充共享入口与组件声明拒绝覆盖（提交 f14edbc）后，全量为 **3806 passed / 36 skipped**。此前 3791/36 是补充这 15 项用例前的历史结果；四套独立迁移库及串行 upgrade/check/唯一 head f0b120261030/downgrade 均通过。该次联合 PR 包含阶段一、二及这些回归；当时运行环境尚未迁移，部署、真实对端和现场验收未覆盖。

<a id="pr41-merge-verification"></a>
## PR #41 合并核验（2026-10-30）

本节集中保存阶段一、阶段二在 2026-10-30 的 PR #41 联合合并核验证据。两份实施计划和阶段一验收记录引用本节；各阶段原始测试结果及日志位置仍保留在各自记录中。

阶段一 P1.1～P1.4、阶段二 P2.1～P2.4 已随 [PR #41](https://github.com/kaizhoumasha/maintenance-admin/pull/41) 合入 develop。合并提交 `f7a96288c8592107a6b5b455c06c432b5ac4c75e`，已核对其源码树与获准 PR 头 `ebb7c31` 一致；90861b7、a34b28d 及后续测试/文档提交保留。

合并前该版本两轮 GitHub CI 通过，Codex review 已完成且无待处理意见；本机复核无新增缺陷。隔离 PostgreSQL 17 / Redis 7 全量重新执行：**3806 passed / 36 skipped，309.27 秒，exit 0**；四套迁移专用库已配置。36 项为 SQLite 不适用的 PostgreSQL 分支，对应 PG 形态已执行；Ruff check/format（502 文件）通过。历史验收计数保持原记录。

合并后 develop CI：[41129070543](https://github.com/kaizhoumasha/maintenance-admin/actions/runs/41129070543)，**SUCCESS**，已核对运行版本为合并提交 `f7a9628`（3802 passed / 40 skipped；四套迁移专用库未在 CI 配置，已由本机隔离全量补齐）。

截至 2026-10-30 该次合并核验，按用户当时要求不部署、不迁移运行库、不实施阶段三；bin_return/SMT 未注册，真实对端、独立生产 worker 与现场验收未覆盖。进度文档在该核验时点尚未另提交；当时主目录分支、外部工作树及暂存内容保留，该次合并核验不表示当时主目录已切换到合并版本。
