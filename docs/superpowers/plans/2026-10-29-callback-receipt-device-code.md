# 回调收据设备检索 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 回调收据保存 ECS 设备码并支持按设备等值检索。

**Architecture:** 共享表增加可空文本列和索引；仅两个 ECS 写入点记录原信封设备码，WMS 与历史收据保持 NULL；复用原摘要查询。

**Tech Stack:** SQLAlchemy、Alembic、Pydantic、pytest、独立 PostgreSQL/Redis、SQLite。

**Spec:** [回调收据设备检索设计](../specs/2026-10-29-callback-receipt-device-code-design.md)

## Global Constraints

- 保留已有未提交源码、设计、计划/P3 文档及暂存状态；当前功能分支实施，不提交、推送或部署。
- device_code 为可空 Text，PostgreSQL 哈希等值索引，SQLite 普通索引；不关联外键、不改身份或路由、不回填历史行。
- 无设备信封的 WMS 收据保持 NULL；重报返回原行，不补写原有 NULL。
- 新增迁移延伸 d7cf20261029；有非空设备字段时禁止破坏性降级。
- 本轮只做 MOCK/内部验证，不更新服务器制品或请求现场设备。

## Review Focus

- ECS Event 注册及未注册事件均记录设备码；Result 仅在原本允许持久化的路径写入，不扩大收据范围。
- 很长且不可高压缩的设备码可保存并等值检索；无效持久文本仅在原收据查重之后对新行拒绝，不改变历史响应或产生半条收据。
- WMS、旧收据和原重报保持 NULL/原状态，不从 data 或诊断猜设备码。
- 新列、索引与模型一致；升级保留旧行，降级不丢设备证据，单迁移 head。
- 原源码/扫码合同及历史记录保留，新增查询字段不改变权限、事务或现场验收边界。

## Task 1: 收据字段、写入及迁移保护

**Files:** `app/models/callback.py`、`app/integrations/ecs_callback/receiver.py`、`app/integrations/ecs_command/result.py`、`app/{schemas,services}/dev_ecs_event*.py`、新 Alembic 迁移、回调/Result/调试/迁移测试、`docs/integrations/ecs-event-debugging.md`、`CHANGELOG.md`、本设计与计划。

**Interfaces:** receipt.device_code 为 str 或 None；insert_receipt 原接口和回调响应不变；摘要查询 receipt 增加同名可空字段。

- [x] 先补 Event/Result/WMS 设备检索及重报、查询投影、无效/长设备码回归，观察旧实现失败。
- [x] 最小增加模型列/索引、两个 ECS 写入值、查重后的新收据设备文本校验和既有摘要投影。
- [x] 新增升级及降级保护迁移，用两个后端验证旧行保留、NULL、索引及非空值拒绝降级。
- [x] 同步文档；在独立测试库运行相关测试、完整 pytest、Ruff、格式及单 head/check，保留原文件/暂存状态和交付快照。
- [x] 完成独立只读复评与结果回填，清理仅本轮创建的临时测试资源，保留未提交成果。

## 完成状态与证据（2026-10-29）

以下完成状态为 2026-10-29 内部开发验收时的交付前快照；后续提交、制品及部署状态另以相应记录为准，原测试统计和阶段约束保持原范围。

本轮已完成实现、保留数据迁移及独立复评，成果保留在当前功能分支的未提交工作区；基线提交仍为 `6f0f86c`，不表示本轮新增字段已经提交或部署。

- [针对性验证](</Users/kaizhou/.codex/recovery/maintenance-admin/receipt-device-20261029T232920Z/target-green.log>)：279 passed / 2 skipped；[完整测试](</Users/kaizhou/.codex/recovery/maintenance-admin/receipt-device-20261029T232920Z/full.log>)：3555 passed / 35 skipped。跳过项均为 SQLite 下不适用的 PostgreSQL 专用语义检查，两个后端迁移与专用迁移数据库均已配置并实际运行。
- Ruff、479 文件格式及差异检查通过；单 head/current 为 `ea8b20261029`，[迁移一致性检查](</Users/kaizhou/.codex/recovery/maintenance-admin/receipt-device-20261029T232920Z/alembic-final-check.log>)无新升级操作。
- [独立只读复评](</Users/kaizhou/.codex/recovery/maintenance-admin/receipt-device-20261029T232920Z/final-review.md>)核对全部 26 文件快照，无 Critical/Important/Minor；评审中完整测试尚在运行的条件已由上述最终日志解除。评审后仅回填本计划和外部证据，无产品代码变更。
- [本轮验收记录](</Users/kaizhou/.codex/recovery/maintenance-admin/receipt-device-20261029T232920Z/acceptance.md>)记录文件、暂存状态、stash、历史交付快照及本地原容器的保留检查；仅移除本轮自建的两个测试容器。

新字段及索引只在独立验证库应用过，未更新现有本地服务或联调服务器。后续部署须先应用迁移再运行依赖新列的应用；历史设备码为空的收据不回填，已写入设备码证据后降级按设计拒绝。
