# 工作位容量与四表整合 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement task-by-task. Steps use checkbox (`- [x]`) syntax. 用户已确认终态规则；本轮继续既有 SHIP 的本地整合、测试、提交/推送和 PR 更新。

**Goal:** 在四表分支保留容量行为，并停止旧 TASK 投递但不漏掉原架未决责任。
**Architecture:** 复用原 RackJob/Action、原公开 Transport 读口和原 pending/retention；停止状态的 pending 仅消费事实，不能执行动作。容量候选改用四表原字段；迁移单链。
**Tech Stack:** 既有 Python/FastAPI/SQLAlchemy/Alembic/PostgreSQL/Redis/Celery/pytest/Ruff，不新增依赖。
**Spec:** `docs/superpowers/specs/2026-11-04-capacity-four-table-integration-design.md`。

## Global Constraints

- 用户选择：停投递，保留未决原架责任。不得恢复旧任务、新建责任表或伪造物理交出。
- 原身份、冻结请求、接纳与结果保留；独立 LINE、事实报告、未标签历史不扩范围。
- 不动主工作区、运行库或部署；只整合现有 feature worktree，所有 scratch/log 在 /tmp/maintenance-ship-integration 或本计划独占忽略目录。
- 迁移单链 sharedrackdispatch → positioncapacity → rollerfoundation → rollerfour；只调整尚未合入的新 revision。
- 锁序/首 SQL SERIALIZABLE/40001 三次新 session 整事务恢复、公开结果读口不退回。

## Review Focus

- gate 后终态的原 ENTER：不能误判未发并让新任务穿过；Task 2。
- CLOSED_BY_SCOPE + pending 的原事实推进：任何旧 decide/send/create/prepare 均不得发生；Task 2/4。
- 保存期清理：原未决架仍保护原消息；解除依据不会因后续清理丢失而重开；Task 2/4。
- 四表候选混合与已绑定 waiter：任务原顺序、同架单份额度、额满后原 pending 补位；Task 3/4。
- SQLite batch 和 PG 旧库：容量配置/admission_order 不被 A/B 重建丢失，B 旧责任拒绝保留；Task 1/4。

### Task 1: 接入容量与串联 schema

**Files:** 两分支实际冲突的 source/tests/docs；`migrations/versions/2026-11-03_bin_roller_runtime_foundation.py`；`tests/migrations/test_{roller_runtime_foundation,bin_roller_four_tables,workline_position_capacity,project_schema,shared_rack_dispatch}.py`。
**Interfaces:** 沿用 #53 positions / TaskAdmission / admission_order；四表模型继续唯一。
- [x] 用 Git merge --no-commit --no-ff 接入 48e6a180；记录所有冲突，保留四表字段并逐项移植容量行为，不取一侧覆盖整个组件。
- [x] 新迁移组合用例先证明原 A/B 会产生双 head/旧假设，再改 rollerfoundation 前序为 positioncapacity；保留新列/唯一索引与降级保护。
- [x] 只完成明确的结构冲突；与终态相关逻辑等 Task 2 的失败用例后实现。迁移原断言适配实际新链，不能删断言过关。
- [x] 检查一个 head、空库与 SQLite/PG 组合迁移；保留旧数据及责任拒绝证据。

### Task 2: 停调度后保留原架事实责任

**Files:** `app/businesses/_components/rack_dispatch/{models,service}.py`；新 foundation 约束；`app/businesses/_components/bin_roller/rack_flow.py`；`tests/businesses/rack_dispatch/test_scope_capacity.py`；`tests/businesses/bin_roller/test_scope_close.py`。
**Interfaces:** `RackDispatch.close_scope(session, scope, reason)`、`advance(...)` 原签名；CLOSED_BY_SCOPE/pending 为独立语义，原公开结果消费。
- [x] 先写原 ENTER queued/gated/accepted/arrived 后终态与新任务准入回归，证明现有 close_scope 丢 pending 门禁或持续投递缺陷。
- [x] 停止状态在动作分支前仅消费事实；无原物理登记或严格未派发才结束 pending，已发/未知/在位保持；严格原 DEPART 接纳只收口事实，不登记新请求。
- [x] 同步模型和新 schema 约束；保留停止原因与严格解除依据。消费晚到事实不改原范围、不产生后继。
- [x] 公共调度、scope close、生命周期、容量和 retention 专项绿灯，含普通/snapshot 原 gate 行为。

### Task 3: 把任务容量候选移植到四表

**Files:** `bin_roller/{ports,rack_flow,feed_flow}.py`、`smt_manual/{task_flow,repository,roller_ports,manual_rack_flow,service}.py`；相关 conftest/helpers/capacity/position_capacity tests。
**Interfaces:** read_supply_order 原 TaskAdmission；SUPPLY face 的 scope_id/id/rack_job_id 替代旧 task_id/demand_id/cycle_id 读取；外部服务签名不换。
- [x] 先运行容量已有行为测试及新增四表混合候选顺序/补位用例，保留真实失败；只适配 fixture 物理模型，不放宽原可观察断言。
- [x] 移植完整候选物化、当前任务门禁、LOCAL_ONLY、混合 source 顺序；保留四表 FIFO/结果/取消/停止语义。
- [x] 两业务全组、公共 RackDispatch、配置 API、排序/重试/报告与 worker 相关用例通过。

### Task 4: 组合事务、清理与交付

**Files:** 原 tests/businesses/rack_dispatch、bin_roller scope/retention、tests/tasks/test_smt_manual_worker.py、迁移测试及两份合同/验收文档。
**Interfaces:** 原 WorkLine 锁、公开结果、pending/Beat、Sender；不加新调度器/维护端点。
- [x] 补终态/登记或 gate 竞争及关闭回滚、清理保护 pending 原架、严格交出后下一候选补位/重启恢复的直接组合回归，观察红后实现最小修复。
- [x] 冻结源码，独占可丢弃 PG/Redis，迁移库分组隔离。运行 `rtk uv run ruff check .`、`rtk uv run ruff format --check .`、迁移全组和其余 pytest（不重叠并集等同全仓）、`rtk uv run alembic heads`、diff check。
- [x] 新鲜整体审查；修复有证据的缺陷并重验受影响输入。保留旧计划尚未补齐的直接验收缺口。
- [x] 提交整合和文档、正常推送 #54，更新 PR/原合同与真实结果；只有组合验证/评审结束才移出 Draft。#53 → #54 的实际合入仍受 required test 和发布授权约束；不部署。

## 最终执行证据

已完成容量 merge、四表字段移植、终态事实责任、独立整体审查及修复。全仓初验有 10 个失败（9 个迁移样例重复配置、1 个双线样例未先交出原架），如实保留；初验后仅这两处测试文件变化，产品/迁移/helper 源码未变。完整受影响文件复验 56+2 passed；按每项最新结果去重覆盖 5725 passed/44 SQLite 条件跳过。该并集不是一次全仓 exit 0，提交后的 CI 将重验整个树。Ruff、702 文件格式、差异及唯一 head 通过。实际 develop 合入及部署未执行；工作树保留供评审。
