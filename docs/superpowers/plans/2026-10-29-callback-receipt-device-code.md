# 回调收据设备检索 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 回调收据保存 ECS 设备码并支持按设备等值检索。

**Architecture:** 共享表增加可空文本列和索引；仅两个 ECS 写入点记录原信封设备码，WMS 与历史收据保持 NULL；复用原摘要查询。

**Tech Stack:** SQLAlchemy、Alembic、Pydantic、pytest、独立 PostgreSQL/Redis、SQLite。

**Spec:** [回调收据设备检索设计](../specs/2026-10-29-callback-receipt-device-code-design.md)

**最新已验证状态（2026-10-29 17:16:21 PDT）：** 已随本地提交 `b68afec` 和固定制品 `integration-b68afec828c3-amd64-20261029-01` 完成联调部署，服务器已升级至 `ea8b20261029`。详见[后续本地提交与联调部署验收](#后续本地提交与联调部署验收)。下列原实施约束、Task 1 和交付前快照保留当时范围；后续仅本地提交和部署已获用户单独授权。本次仅回填文档，不提交、推送或部署。

## Global Constraints（原实施阶段）

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

## 历史：交付前完成状态与证据（2026-10-29）

以下完成状态为 2026-10-29 内部开发验收时的交付前快照；后续提交、制品及部署状态另以相应记录为准，原测试统计和阶段约束保持原范围。

本轮已完成实现、保留数据迁移及独立复评，成果保留在当前功能分支的未提交工作区；基线提交仍为 `6f0f86c`，不表示本轮新增字段已经提交或部署。

- [针对性验证](</Users/kaizhou/.codex/recovery/maintenance-admin/receipt-device-20261029T232920Z/target-green.log>)：279 passed / 2 skipped；[完整测试](</Users/kaizhou/.codex/recovery/maintenance-admin/receipt-device-20261029T232920Z/full.log>)：3555 passed / 35 skipped。跳过项均为 SQLite 下不适用的 PostgreSQL 专用语义检查，两个后端迁移与专用迁移数据库均已配置并实际运行。
- Ruff、479 文件格式及差异检查通过；单 head/current 为 `ea8b20261029`，[迁移一致性检查](</Users/kaizhou/.codex/recovery/maintenance-admin/receipt-device-20261029T232920Z/alembic-final-check.log>)无新升级操作。
- [独立只读复评](</Users/kaizhou/.codex/recovery/maintenance-admin/receipt-device-20261029T232920Z/final-review.md>)核对全部 26 文件快照，无 Critical/Important/Minor；评审中完整测试尚在运行的条件已由上述最终日志解除。评审后仅回填本计划和外部证据，无产品代码变更。
- [本轮验收记录](</Users/kaizhou/.codex/recovery/maintenance-admin/receipt-device-20261029T232920Z/acceptance.md>)记录文件、暂存状态、stash、历史交付快照及本地原容器的保留检查；仅移除本轮自建的两个测试容器。

新字段及索引只在独立验证库应用过，未更新现有本地服务或联调服务器。后续部署须先应用迁移再运行依赖新列的应用；历史设备码为空的收据不回填，已写入设备码证据后降级按设计拒绝。

## 后续本地提交与联调部署验收

用户后续授权将 27 个已审文件仅在本地提交后生成固定制品部署，未推送、合并或打标签。对应源码为 `b68afec828c308a53520ec9db7a62a6e02bcef77`，部署完成证明的采集时点为 `2026-10-30T00:16:21.966502+00:00`（洛杉矶 2026-10-29 17:16:21 PDT，UTC−07:00）。以下均引用该次归档，本次文档回填未重新测试或连接服务器，不作为新的实时运行检查。

- [x] 新 ECS Event/Result 收据写入、原收据重放优先及摘要查询的 nullable device_code 投影已随正式装配交付；行为由上方 279/2 专项及 3555/35 全量内部测试快照证明。
- [x] 按停应用→最终备份与异机校验→迁移→启动新应用的顺序交付，服务器 head 为 `ea8b20261029`，check 返回 `No new upgrade operations detected.`。
- [x] 服务器列为 nullable text、无默认值；`callback_receipt_device_idx` 为有效且就绪的 hash 索引。该索引用于设备等值检索；本轮沿用原摘要查询，不新增列表入口。
- [x] 原 4 份收据的 device_code 保持 NULL，不从 data 或诊断推测设备；原消息、命令、路由及业务事实不改写。

**证据边界：** 服务器该次验收未注入回调，新设备码写入及摘要查询行为由内部测试证明，不能将列、索引和 OpenAPI 已部署表述为“现场新收据已写入并查回设备码”。原历史 NULL 保持 NULL；新增非 NULL 设备证据后，破坏性降级按设计拒绝。

## 共同交付证据与执行偏差

此节同时适用于[实际扫码字段映射计划](2026-10-29-coarse-sorting-scan-measurements.md#后续本地提交与联调部署验收)。本机归档根目录为 `/Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z`；服务器记录目录为 `/srv/enterprise-backend/records/integration-b68afec828c3-amd64-20261029-01`，current 切换至同名 releases 目录。

固定制品为 linux/amd64，四个应用服务使用镜像 `sha256:89c00ac4534a82648ca988f48f9e0f8e9c9fed288e819a61a4c0323ced1a8276`；232 个运行源码、迁移和脚本文件逐文件哈希核对通过。联合开发验收统计沿用 279 passed / 2 skipped、3555 passed / 35 skipped，跳过项为 SQLite 下不适用的 PostgreSQL 专用语义检查；不是本次文档回填重跑结果。

| 验收项 | 已验证结果 | 证据索引 |
| --- | --- | --- |
| 提交、制品与切换时点 | 源码 `b68afec`、干净源码固定制品、上述完成时点；未推送、合并或打标签。 | [交付汇总](</Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z/review-and-deployment.md>)、[制品清单](</Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z/bundle/manifest.json>)、[部署完成证明](</Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z/server-evidence/completion-proof.json>) |
| 迁移执行与模式一致性 | 迁移容器在 `2026-10-30T00:14:19.333020889Z` 启动，`00:14:20.868579744Z` 退出 0，restart_count=0；head 为 ea8b20261029，check 无新升级操作，设备列及 hash 索引读回正确。 | [迁移执行输出](</Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z/site-migrate.log>)、[容器运行信息](</Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z/server-evidence/migration-run.json>)、[head](</Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z/server-evidence/alembic_current.log>)、[check](</Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z/server-evidence/alembic_check.log>)、上述部署完成证明 |
| 旧行、历史 NULL 与业务身份保留 | 20 张原表 OID 与 175 条记录数量保留，其中 174 条非 Alembic 记录逐行完整哈希一致，另 1 条版本记录按迁移推进；旧收据 4 条保持 NULL。原扫码 1 条待核对、原验证消息 FAILED，命令 2 条、可靠消息 3 条，未重发。 | [数据保留证明](</Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z/server-evidence/data-preservation-proof.json>)、[迁移前](</Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z/server-evidence/db-before.json>)、[迁移后](</Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z/server-evidence/db-after-migration.json>)、[恢复服务后](</Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z/server-evidence/db-after-validation.json>)、上述部署完成证明 |
| 服务、Swagger 与保留范围 | 两套入口 `/ready`、`/docs`、`/openapi.json` 均为 200；worker ping、Beat 单实例通过。RS01→CLN01、RS02→CLN02、shared 文件哈希及 WES/数据库/Redis 等 19 个非目标容器基线不变。 | [运行检查](</Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z/server-evidence/runtime-proof.json>)、[检查命令来源](</Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z/site-finalize.py>)、上述部署完成证明 |
| 备份及异机校验 | 最终备份 82231 字节，归档目录校验通过；服务器和本机副本 SHA-256 均为 `f069e97b24dd11a63813e2792c4e15789a14925d0ff9a6a92511e248108029cd`。本机副本权限 0600，未做恢复演练。 | [备份证明](</Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z/server-evidence/backup-proof.json>)、[归档目录](</Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z/server-evidence/backup-contents.txt>)、[异机副本校验](</Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z/server-evidence/backup-local-copy.json>) |

执行偏差与证据限制按该次交付分别记录，保留失败与恢复输出，不覆盖旧记录：

1. **传输方式调整。** 完整 SCP 传输较慢，在停机前中止；复用旧镜像相同内容并补传差异，在服务器还原完整制品后重新校验。完整镜像归档 SHA-256 为 `6e938917f885b7cd162cc07c61b18615d18a8b8408653ac182d579d1f2b2dfd9`，旧交付包保持不变。见[完整制品还原证明](</Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z/server-evidence/transport-proof.json>)、[还原与校验输出](</Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z/site-reconstruct.log>)。
2. **备份入口重试。** 停止本项目应用后，直接执行旧包 `scripts/backup_pg.sh` 因无执行权限退出 126，数据库尚未迁移；随后通过 bash 运行同一脚本，未修改旧脚本内容或权限。备份及异机校验成功后才迁移。见[首次失败](</Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z/site-backup.log>)、[重试备份输出](</Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z/site-backup-resume.log>)和上表备份证明。
3. **Timescale 备份提示。** 备份重试输出含 `continuous_agg` 循环外键提示；备份生成、归档目录及异机哈希检查通过，但未执行完整恢复演练，不据此声称备份已恢复验证。
4. **迁移次数与输出来源。** 本次仅启动一次迁移，之后直接启动既有 nginx 容器，未通过 Compose 连带启动 migrate。执行依据为上表迁移运行信息、[执行脚本](</Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z/site-migrate.py>)及[应用启动脚本](</Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z/site-start.py>)；单个容器、退出 0 或 restart_count=0 本身不单独证明调用次数。归档 `server-evidence/migration.log` 为空，不作为迁移 SQL 或次数的证明，模式变化另由迁移前后摘要、head/check 和部署完成证明支持。原双机 `6f0f86c` 的重复执行但无变更偏差仍按原记录保留。

两份功能计划及本次部署继续以 MOCK/内部验证为业务验收口径；实际报文只在新容器做纯合同模型校验，没有现场扫码注入、设备测试命令或真实 WMS/ECS 物理闭环验收。本次仅更新仓库计划，保留历史测试、原验收记录、服务器交付包快照、既有本地文件及暂存状态，不提交、推送或部署。
