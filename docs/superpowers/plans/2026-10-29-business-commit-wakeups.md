# 业务提交后即时唤醒 Implementation Plan

> **历史快照：** 本文记录 `4b7fb80` 的逐条唤醒方案及当时验收。后续已选用[按业务唤醒方案](../specs/2026-10-29-business-pending-wakeups-design.md)；以下原设计、统计和交付边界保留，不代表当前开发用法。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 默认结果提交后即时投递 Celery，粗分三个唤醒点复用原 advance，Beat 扫描持久待办兜底。

**Architecture:** 共享事务通知隔离回滚与发布失败；通用接收／发送机制只调用注入端口，应用装配发布具体粗分任务；任务按原引用定位并调用原业务推进。

**Tech Stack:** Python、SQLAlchemy Session events、Celery、pytest、独立 SQLite/PostgreSQL/Redis、回环 MOCK。

**Spec:** [业务提交后即时唤醒设计](../specs/2026-10-29-business-commit-wakeups-design.md)

## Global Constraints

- 当前 `feature/coarse-sorting-site-configuration` 实施；不提交、推送、合并、部署或请求现场设备。
- 三份现有已暂存计划的工作副本和 index 内容保持原样；保留历史记录、交付包和其他本地文件。
- 通用机制只调用注入端口，业务不读 ReliableMessage/TransportTask；复用唯一 advance、Sender 和持久 pending，不新增表。
- 发布在最外层成功提交之后，失败不伪装事务失败；发布不重试且 ignore_result=True，独立发布连接 max_retries=0、连接及 Redis socket 超时 1 秒；Beat 保持原 5 秒兜底。

## Review Focus

- 内层保存点成功不是外层成功；回滚和关闭不得泄漏通知。
- broker 发布失败不得让已落库事件变成 503，重建后能用原身份扫描恢复。
- Sender 的陈旧完成、重复回调和并发任务不得导致重复外部意图。
- WMS 响应提交后通知，不在发送事务中查询业务或提前消费结果。
- 主应用和具体 worker 实际装配启用端口，测试替代入口不能冒充正式接缝；无队列时仍靠原待办恢复。

## Task 1: 提交后通知与发送结果端口

**Files:** 新增 `app/transactions.py`、`tests/test_transactions.py`、`tests/reliable/test_result_notifications.py`；修改 `app/reliable/store.py`、`app/reliable_runtime.py`。

**Interfaces:** `after_commit(session, callback)`；`MessageStore(..., on_result=None)` 和 `reliable_lifespan(..., on_result=None)`，端口接收 `(message_id, operation, operation_id)`，不接收原文。仅已成功保存的非快照终态触发，本轮 WMS 使用此路径；ECS 命令通过 Result 接收端口通知。

- [x] 先写提交／回滚、嵌套事务、关闭及发布失败行为测试；运行观察正确 RED。
- [x] 实现最小事务队列与结果保存后的注入通知；覆盖提交事实可见、终态重复完成和陈旧租约不重复通知。
- [x] 运行事务与可靠发送相关测试，记录 GREEN，核对原重试／发送判定不变。

## Task 2: 粗分三点与具体即时任务

**Files:** 新增 `app/business_wakeups.py`、`tests/businesses/test_coarse_sorting_wakeups.py`；修改 `app/businesses/runtime.py`、`app/businesses/coarse_sorting/service.py`、`app/tasks/coarse_sorting_tasks.py`、`app/main.py`、`app/controllers/ecs_result.py`、`app/integrations/ecs_command/result.py`、必要共享测试夹具。

**Interfaces:** `Capabilities` 注入业务发布函数；`build_runtime(..., enqueue_advance=None)`；`receive_ecs_result(..., on_result=None)` 端口接收 `(owner_domain, command_code)`；具体 `coarse_sorting.advance_one(source, identity)` 对应 `scan/wms/ecs` 原引用，定位后调用 `advance(scan_id)`。

- [x] 新增实际公开事件、回环 WMS Sender、公开 ECS Result 的即时通知测试，先观察 RED。
- [x] 接入三点和应用装配；具体任务复用原 runtime 与独立会话工厂，原 Beat 任务保持兜底。
- [x] 补齐回滚、发布失败仍 ACK、重复／并发唤醒及重建后扫描恢复测试；正常链路无需 Beat 推进到等待分配，NG／不确定状态仍按原门槛停留。
- [x] 运行粗分、Result、可靠发送与主应用相关测试，记录 GREEN。

## Task 3: 公共约定、完整验证与独立复评

**Files:** `AGENTS.md`、`docs/integrations/business-packages.md`、`docs/integrations/business-development-quickstart.md`、`docs/integrations/business-transaction-recipes.md`、`docs/integrations/outbound-results.md`、`docs/integrations/ecs-event-debugging.md`、本设计与计划。

- [x] 同步默认推进约定、端口职责和即时／兜底边界，保留历史验收，不改现有三份已暂存计划。
- [x] 在本轮独立数据库／Redis 跑全量 pytest、Ruff、格式及单 head/check；记录真实通过和跳过统计，不沿用旧统计冒充本轮。
- [x] 对本轮未提交增量作一次独立复评，处理实际问题；证据归档仓库外。
- [x] 比对初始 index、三份计划、stash、既有证据和容器基线；仅清理本轮自建测试资源，结果保持未提交未部署。

## 执行与验收记录

本轮实现与验证已完成，成果保留为当前功能分支未提交改动；基线 `b68afec` 与既有部署状态属于上一轮，新唤醒代码未部署。用户已授权按选定方案更新文档后直接实施，本轮不包含提交步骤。


### 内部验收证据（2026-10-29T18:43:08.560327-07:00）

- 事务／可靠发送阶段：26 个预期 RED 后，253 passed；粗分三个唤醒点阶段：24 个预期 RED 后，45 passed / 1 skipped；扩大回归 1522 passed / 28 skipped。均为阶段快照，保留原统计。
- 独立复评审查 23 个文件，发现 1 个 Important/P2：仅 retry=False 仍会触发 Celery 结果后端及连接重试。已补真实 Celery/Kombu 的两个负向回归，观察结果后端访问及多次连接的 RED，再修复为 ignore_result=True、独立且有界的发布连接。修复后专项 53 passed / 1 skipped，最终全量 **3609 passed / 36 skipped**。跳过项为 SQLite 下不适用的 PostgreSQL 专用语义；对应 PostgreSQL 用例已在本轮独立库执行。
- Ruff 通过，486 个文件格式检查通过；迁移 heads/current 仍唯一 `ea8b20261029`，check 无新增升级操作，本轮无新迁移。
- 发布连接／Redis socket 超时取 1 秒，连接尝试不重试；该实现取舍的代价是瞬时发布故障交由原持久待办和 5 秒 Beat 恢复，不在 ACK 路径等待重试。独立框架测试可不注入端口，正式 API 和具体 worker 默认注入；未新增环境分流。
- 测试使用本轮专用 PostgreSQL、Redis、回环 MOCK 和内存 broker，不连接现场。一次误启动的同库重叠验证已中止并排除，重置的仅为本轮可丢弃测试库；上述最终证据来自随后顺序运行的验证，不使用重叠输出作为通过证明。

| 项目 | 仓库外证据 |
| --- | --- |
| 最终全量与发布修复专项 | [全量](</Users/kaizhou/.codex/recovery/maintenance-admin/commit-wakeups-20261030T010158Z/full-verified.log>)、[专项](</Users/kaizhou/.codex/recovery/maintenance-admin/commit-wakeups-20261030T010158Z/publisher-green-verified.log>)、[发布回归 RED](</Users/kaizhou/.codex/recovery/maintenance-admin/commit-wakeups-20261030T010158Z/publisher-red-confirmed.log>) |
| 独立复评及问题处理 | [原始复评](</Users/kaizhou/.codex/recovery/maintenance-admin/commit-wakeups-20261030T010158Z/final-review.md>)、[修复核对](</Users/kaizhou/.codex/recovery/maintenance-admin/commit-wakeups-20261030T010158Z/review-resolution.md>) |
| 静态检查与迁移状态 | [Ruff](</Users/kaizhou/.codex/recovery/maintenance-admin/commit-wakeups-20261030T010158Z/lint-verified.log>)、[格式](</Users/kaizhou/.codex/recovery/maintenance-admin/commit-wakeups-20261030T010158Z/format-verified.log>)、[head](</Users/kaizhou/.codex/recovery/maintenance-admin/commit-wakeups-20261030T010158Z/schema-heads.log>)、[current](</Users/kaizhou/.codex/recovery/maintenance-admin/commit-wakeups-20261030T010158Z/schema-current.log>)、[check](</Users/kaizhou/.codex/recovery/maintenance-admin/commit-wakeups-20261030T010158Z/schema-check.log>) |
| 原计划、index、历史及资源保留 | [验收汇总](</Users/kaizhou/.codex/recovery/maintenance-admin/commit-wakeups-20261030T010158Z/acceptance.md>)、[保留核对](</Users/kaizhou/.codex/recovery/maintenance-admin/commit-wakeups-20261030T010158Z/preservation.json>)、[容器核对](</Users/kaizhou/.codex/recovery/maintenance-admin/commit-wakeups-20261030T010158Z/container-preservation.json>)、[本轮执行归档](</Users/kaizhou/.codex/recovery/maintenance-admin/commit-wakeups-20261030T010158Z/execution-workspace.tar.gz>) |

本轮仅为 MOCK／内部验证，不包括真实 broker 故障耗时、生产吞吐或现场物理闭环验收；不提交、推送、合并或部署。原三份已暂存计划不追加新实现状态，旧交付快照和全部原验收记录保持原样。
