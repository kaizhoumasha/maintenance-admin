# 粗分实际扫码测量字段 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 接收实际 size_inch/thickness_mm 报文，向 WMS 原值映射为 reel_diameter/reel_thickness。

**Architecture:** 沿用唯一正式事件处理器；在受理时归一为既有持久业务事实，不改后续请求、历史待办或身份算法。

**Tech Stack:** Python、Pydantic、SQLAlchemy、FastAPI、pytest、独立 PostgreSQL/Redis、回环 MOCK 对端。

**Spec:** [实际扫码测量字段设计](../specs/2026-10-29-coarse-sorting-scan-measurements-design.md)

## Global Constraints

- 用户已确认按实际字段同步实施；直径为英寸原值 7，厚度为毫米原值 15.3，不换算。
- 保留当前功能分支、未提交文档及暂存状态；不提交、推送、部署或请求现场设备。
- 业务 Payload 仅必填 barcode、size_inch、thickness_mm；保留严格数值类型和额外字段忽略策略。
- 持久事实和 WMS 字段仍为 barcode、reel_diameter、reel_thickness；不新增旧输入别名、兼容分支或迁移。
- 原双机计划、P3 输出来源修复、历史验收和服务器交付快照保持各自原范围。

## Review Focus

- 完整实际报文被受理，location/scan_time/result 不泄漏到 WMS 业务 data。
- 直径 7 和厚度 15.3 原值发送，不因字段改名发生单位换算或字符串化。
- 缺字段、非法类型及非有限测量值不能生成收据或业务待办。
- 重启后的待办仍可推进；原事件身份继续包含完整额外 data，不重算历史摘要。
- 当前合同和 Swagger 已切换，历史文档和已部署制品不被误标为本轮验证结果。

## Task 1: 实际扫码合同、受理映射与验证

**Files:** `app/businesses/coarse_sorting/{contracts.py,service.py,request/inbound_coarse_sorting_validate_bind_v1.py}`；`tests/businesses/{conftest.py,test_coarse_sorting_scan.py,test_coarse_sorting_forward.py,test_coarse_sorting_application.py}`；`docs/integrations/{coarse-sorting-api-draft.md,ecs-event-debugging.md}`；`CHANGELOG.md`；本设计与计划。

**Interfaces:** `ScanCompletedData` 输入 barcode/size_inch/thickness_mm；`accept_scan` 写入既有 barcode/reel_diameter/reel_thickness；`advance` 和 WMS 请求接口保持不变。

- [x] 更新入站测试数据和数值校验回归，新增完整实际报文受理及重建后 WMS 请求原值回归；先运行相关测试，观察当前代码因缺少旧测量字段而失败。
  ```python
  assert (await flow.post(body)).status_code == 200
  flow.rebuild()
  assert await flow.service.advance(row["id"]) == "AWAITING_VALIDATION_RESULT"
  assert request["data"]["reel_diameter"] == 7
  assert request["data"]["reel_thickness"] == 15.3
  ```
- [x] 最小修改 Payload 字段及完整 Swagger 样例，在 accept_scan 显式归一测量字段；不改后续推进或历史记录。
- [x] 同步当前 ECS/WMS 合同的字段及单位，保留历史设计和原双机实施计划的已验收范围；标明本轮尚未部署。
- [x] 在本轮独立测试数据库和 Redis 上运行粗分、公开调试及相关集成测试，再运行完整 pytest、Ruff、格式检查，证据写仓库外。
- [x] 对本轮差异及原有文档回填做独立复评，处理实际问题，核对暂存状态、已有文件和交付快照未变；保留未提交结果。

## 完成状态与证据（2026-10-29）

以下完成状态为 2026-10-29 内部开发验收时的交付前快照；后续提交、制品及部署状态另以相应记录为准，原测试统计和阶段约束保持原范围。

本轮实现及独立复评已完成，成果保留在未提交工作区；`6f0f86c` 是原有基线，不作为本轮修改已提交的证明。原双机计划和 P3 修复保留原样，服务器仍使用原制品；本轮不提交、推送或部署。

- [针对性测试](</Users/kaizhou/.codex/recovery/maintenance-admin/scan-measurements-20261029T231252Z/target-green.log>)：197 passed / 7 skipped，跳过项为 SQLite 下不适用的 PostgreSQL 专用语义检查。
- [完整测试](</Users/kaizhou/.codex/recovery/maintenance-admin/scan-measurements-20261029T231252Z/full.log>)：3537 passed / 37 skipped。35 项为上述 SQLite 语义检查；另两项最初未配置专用 PostgreSQL 迁移库，随后在本轮临时 PostgreSQL 内各建独立空库，[迁移专项补跑](</Users/kaizhou/.codex/recovery/maintenance-admin/scan-measurements-20261029T231252Z/migration-extra.log>)的 4 个 SQLite/PostgreSQL 参数用例全部通过，不将原完整测试记录改写为另一组统计。
- Ruff 检查通过，475 个文件格式检查通过；当前 ECS/WMS 文档 JSON 样例通过实际模型校验。
- [独立复评](</Users/kaizhou/.codex/recovery/maintenance-admin/scan-measurements-20261029T231252Z/final-review.md>)：13 文件快照核验，无 Critical/Important/Minor；评审时完整测试尚在运行，该条件已由上面的最终日志解除。评审后仅回填本计划状态及证据，没有代码变更。
- [本轮验收与保留记录](</Users/kaizhou/.codex/recovery/maintenance-admin/scan-measurements-20261029T231252Z/acceptance.md>)：原文件、暂存状态、stash、交付快照及既有本地容器保留；只清理本轮自建的两个临时测试容器。
