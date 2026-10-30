# 粗分实际扫码测量字段 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 接收实际 size_inch/thickness_mm 报文，向 WMS 原值映射为 reel_diameter/reel_thickness。

**Architecture:** 沿用唯一正式事件处理器；在受理时归一为既有持久业务事实，不改后续请求、历史待办或身份算法。

**Tech Stack:** Python、Pydantic、SQLAlchemy、FastAPI、pytest、独立 PostgreSQL/Redis、回环 MOCK 对端。

**Spec:** [实际扫码测量字段设计](../specs/2026-10-29-coarse-sorting-scan-measurements-design.md)

**最新已验证状态（2026-10-29 17:16:21 PDT）：** 已随本地提交 `b68afec` 和固定制品 `integration-b68afec828c3-amd64-20261029-01` 完成联调部署。详见[后续本地提交与联调部署验收](#后续本地提交与联调部署验收)。下列原实施约束、Task 1 和交付前快照保留当时范围；后续仅本地提交和部署已获用户单独授权。本次仅回填文档，不提交、推送或部署。

## Global Constraints（原实施阶段）

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

## 历史：交付前完成状态与证据（2026-10-29）

以下完成状态为 2026-10-29 内部开发验收时的交付前快照；后续提交、制品及部署状态另以相应记录为准，原测试统计和阶段约束保持原范围。

本轮实现及独立复评已完成，成果保留在未提交工作区；`6f0f86c` 是原有基线，不作为本轮修改已提交的证明。原双机计划和 P3 修复保留原样，服务器仍使用原制品；本轮不提交、推送或部署。

- [针对性测试](</Users/kaizhou/.codex/recovery/maintenance-admin/scan-measurements-20261029T231252Z/target-green.log>)：197 passed / 7 skipped，跳过项为 SQLite 下不适用的 PostgreSQL 专用语义检查。
- [完整测试](</Users/kaizhou/.codex/recovery/maintenance-admin/scan-measurements-20261029T231252Z/full.log>)：3537 passed / 37 skipped。35 项为上述 SQLite 语义检查；另两项最初未配置专用 PostgreSQL 迁移库，随后在本轮临时 PostgreSQL 内各建独立空库，[迁移专项补跑](</Users/kaizhou/.codex/recovery/maintenance-admin/scan-measurements-20261029T231252Z/migration-extra.log>)的 4 个 SQLite/PostgreSQL 参数用例全部通过，不将原完整测试记录改写为另一组统计。
- Ruff 检查通过，475 个文件格式检查通过；当前 ECS/WMS 文档 JSON 样例通过实际模型校验。
- [独立复评](</Users/kaizhou/.codex/recovery/maintenance-admin/scan-measurements-20261029T231252Z/final-review.md>)：13 文件快照核验，无 Critical/Important/Minor；评审时完整测试尚在运行，该条件已由上面的最终日志解除。评审后仅回填本计划状态及证据，没有代码变更。
- [本轮验收与保留记录](</Users/kaizhou/.codex/recovery/maintenance-admin/scan-measurements-20261029T231252Z/acceptance.md>)：原文件、暂存状态、stash、交付快照及既有本地容器保留；只清理本轮自建的两个临时测试容器。

## 后续本地提交与联调部署验收

用户后续授权将 27 个已审文件仅在本地提交后生成固定制品部署，未推送、合并或打标签。对应源码为 `b68afec828c308a53520ec9db7a62a6e02bcef77`，部署完成证明的采集时点为 `2026-10-30T00:16:21.966502+00:00`（洛杉矶 2026-10-29 17:16:21 PDT，UTC−07:00）。以下均引用该次归档，本次文档回填未重新测试或连接服务器，不作为新的实时运行检查。

- [x] 实际扫码字段映射、正式装配、测试和合同文档随上述干净源码制品交付。
- [x] 新容器内通过完整实际报文的纯合同模型校验：直径 `size_inch=7` 映射至 `reel_diameter=7`（英寸），厚度 `thickness_mm=15.3` 映射至 `reel_thickness=15.3`（毫米），不做单位换算；未调用 receiver/handler、数据库或对端网络。
- [x] Swagger 的 `SCAN_COMPLETED` 示例切换为实际扫码字段；API/worker/beat/migrate 使用同一 linux/amd64 镜像，232 个运行文件哈希核对通过，迁移 head 为 `ea8b20261029`。
- [x] 原有待核对扫码、原始 15/20 尺寸及 FAILED 验证消息保留原身份，未被新示例值覆盖或自动重发；RS01→CLN01、RS02→CLN02 和共享配置保留。

| 验收项 | 已验证结果及边界 | 证据索引 |
| --- | --- | --- |
| 最终联合回归 | 279 passed / 2 skipped；3555 passed / 35 skipped。部署前核对运行源码与该测试快照一致；不替换上方早期 197/7、3537/37 的原始记录，也不表述为本次回填重跑。 | [专项日志](</Users/kaizhou/.codex/recovery/maintenance-admin/receipt-device-20261029T232920Z/target-green.log>)、[全量日志](</Users/kaizhou/.codex/recovery/maintenance-admin/receipt-device-20261029T232920Z/full.log>)、[部署复评与快照说明](</Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z/review-and-deployment.md>) |
| 实际报文与字段单位 | 新容器纯模型校验输出 `actual_event_contract_valid=true`、`wms_diameter_inches=7`、`wms_thickness_mm=15.3`；业务受理及 WMS 请求映射行为由内部回归证明，不将纯模型校验当作现场完整链路受理。 | [合同校验输出](</Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z/server-evidence/offline-contract-probe.log>)、[校验命令来源](</Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z/site-finalize.py>)、上述专项日志 |
| 固定制品、Swagger 与服务 | current 已指向上述制品；两套入口的 `/ready`、`/docs`、`/openapi.json` 均为 200，Swagger 含 `SCAN_COMPLETED`、`MOVE_FORWARD`，worker ping 正常、Beat 单实例。 | [固定制品清单](</Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z/bundle/manifest.json>)、[运行检查](</Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z/server-evidence/runtime-proof.json>)、[部署完成证明](</Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z/server-evidence/completion-proof.json>) |
| 原始业务事实与配置保留 | 原扫码仍为 `VALIDATION_RECONCILIATION_REQUIRED`，原验证消息仍为 FAILED；20 张原表 OID 保留，174 条非 Alembic 记录完整行哈希不变，另 1 条版本记录按迁移推进。 | [数据保留证明](</Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z/server-evidence/data-preservation-proof.json>)、上述部署完成证明 |

与收据设备码变更使用同一制品、备份和迁移，详见[共同交付证据与执行偏差](2026-10-29-callback-receipt-device-code.md#共同交付证据与执行偏差)，包括慢速传输后的完整制品还原、旧备份脚本执行权限、Timescale 提示、空迁移日志及本次迁移仅执行一次的证据。原双机 `6f0f86c` 交付中的重复迁移偏差继续保留，不套用到本次交付。

本轮开发验收仍为 MOCK/内部验证；未注入现场扫码、发送设备测试命令或完成真实 WMS/ECS 物理闭环。历史验收记录、原交付包和新制品中的计划快照保持构建时原样；本次只修改仓库工作副本。
