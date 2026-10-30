# 粗分双机配置与联调部署 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 配置 RS01→CLN01、RS02→CLN02，沿用角色驱动流程并完成独立应用联调部署。

**Architecture:** 复用 WorkLine.device_bindings、position_bindings、现有事件注册与命令路由。粗分声明 OUTPUT_RACK；Result 按原命令及设备身份关联，MOVE_FORWARD Result.data 仅包含 actual_qty。

**Tech Stack:** Python 3.13、FastAPI、SQLAlchemy、pytest、PostgreSQL、Redis、Docker Compose 固定镜像。

**Spec:** [双机配置设计](../specs/2026-10-29-coarse-sorting-site-configuration-design.md)

**后续交付索引（截至 2026-10-29 17:16:21 PDT）：** 实际扫码字段映射与收据设备码已随 `b68afec` 制品完成后续联调部署，head 为 `ea8b20261029`，见文末[后续交付：实际扫码字段与收据设备码](#后续交付实际扫码字段与收据设备码)。下列 Task 1–3、`6f0f86c`/`d7cf20261029` 及重复迁移说明继续代表原双机交付历史，不作为后续制品的现状。

**完成状态（2026-10-29 回填）：** Task 1–3 的交付目标均已完成。Task 3 存在一次重复执行但无变更的迁移偏差，详见文末说明；不将其表述为迁移命令仅执行一次。

**验收基准：** 本地实现提交 `6f0f86c36b61a65845dacc36c90fa2523303542a`，对应固定制品 `integration-6f0f86c36b61-amd64-20261029-01`。本计划按已验证证据回填，原始执行记录和服务器交付包中的未勾选计划快照保留不变。本次仅修改仓库工作副本中的计划，不提交、推送或部署。

## Global Constraints

- ECS origin 为 http://10.24.209.32:8080；工作线、设备、货架位取设计表的确切配置。
- 唯一位置槽为 OUTPUT_RACK，不增加输送线出入口配置或设备编号分支。
- 保留原身份、字节、路由和历史验收；不自动重放旧命令。
- 用户已授权测试和复评通过后仅提交本轮改动，再生成固定制品部署；不推送、不合并，不再重复请求许可。
- 现有文件、stash、站点配置、数据卷及共机 WES 保留；验证不向现场设备发命令。

## Review Focus

- 两条工作线仅回传数量，原 SUCCESS + actual_qty=1 均可正确推进。
- 另一条线的 device_code 不能完成当前原命令。
- 缺数量、错误数量、FAILED、待核对结果不能被放行。
- 工作线仅配置 OUTPUT_RACK 可保存并受理扫码；缺必需资源仍被业务完整性检查拦截。
- 升级保留原两条 DONE 消息、设备路由、数据库与 WES 容器，新增配置不产生业务命令。

## Task 1: 双线配置与角色结果回归

**Files:** app/businesses/coarse_sorting/{definition.py,ecs_command.py,service.py}；tests/businesses/{conftest.py,test_coarse_sorting_forward.py,test_coarse_sorting_contracts.py,test_coarse_sorting_application.py}；tests/controllers/test_workline_business.py。

**Interfaces:** 保持现有事件、create、get_command、工作线和设备 API；新增 position_bindings.OUTPUT_RACK，当前命令附录版本为 2。

- [x] 先新增角色与公开配置行为回归，再运行观察旧实现失败。

  ```python
  assert response.json()["position_bindings"] == {"OUTPUT_RACK": "CLN02"}
  assert (await flow.result(row, data={"actual_qty": 1})).status_code == 200
  assert await flow.service.advance(row["id"]) == "AWAITING_ALLOCATION_STAGE"
  ```

- [x] 将另一条线输送设备码用于原 command_code，断言接收器拒绝且原扫描未完成；覆盖仅数量、缺数量、FAILED、错误数量及非合同字段。
- [x] 最小实现：声明必需 OUTPUT_RACK；Result.data 仅声明 actual_qty；保持数量约束；消费判断只依据原命令完成与数量；新附录版本 2。
- [x] 同步粗分夹具的货架位及附录身份，保持通用平台测试资源声明不变。
- [x] 用独立测试数据库/Redis运行粗分、工作线及 ECS Result 相关测试，再运行完整 pytest、Ruff及格式检查；证据写仓库外。

## Task 2: 文档同步与复评

**Files:** docs/integrations/{coarse-sorting-api-draft.md,device-management.md,ecs-event-debugging.md}、docs/deploy.md、CHANGELOG.md，本设计与计划。

- [x] 更新当前合同及两条线配置实例；修正“仅 INPUT_ARM”“catalog 为空”等当前用法漂移，保留历史验收段落。
- [x] 复评旧固定出口、按设备编号分支、角色/附录/文档一致性；核对全部变更均属于本轮。
- [x] 测试和复评通过后仅提交本轮已验证文件；不暂存或提交其他文件。执行状态以本轮 ledger 及站点 records 为准，交付包中的本计划保留构建时快照。

## Task 3: 固定制品部署与验收

**Interfaces:** scripts/release_bundle.py；既有 shared/compose.sh 的 dc；当前站点管理员 API。

- [x] 在构建机生成新的 linux/amd64 固定镜像和干净源码交付包，验证清单和架构；保留旧包，服务器不构建。
- [x] 保存站点配置、旧镜像及本项目/WES容器基线；只停止本项目应用，最终备份校验并异机保留，备份失败不升级。
- [x] 沿用原 Compose 项目及完整站点覆盖文件，迁移一次，启动同一镜像的 API/worker/beat/migrate；保留基础设施实例、卷和凭据。**交付结果已验收，实际执行次数存在偏差，见下文。**
- [x] 用现有管理员认证和带审计的服务接口创建 RS01/RS02，按返回版本分配原六台设备并写入 INPUT_ARM、CONVEYOR 和 OUTPUT_RACK；不重建原路由、不生成命令。
- [x] 核对 /ready、Swagger 事件/命令、单迁移 head/check、worker ping、beat单实例；配置读回与六台设备 Status 只读查询；比较原消息/路由及 WES 容器基线。
- [x] 完整验收通过后切换 current 指针，记录制品、迁移、配置及验收边界；本地提交/推送/部署各自如实报告，不声称现场业务完成。

## 完成核对与验收证据索引

以下为原实现、部署及后续只读复核的证据，不代表本次文档回填重新执行了测试或部署。本机证据根目录为 `/Users/kaizhou/.codex/recovery/maintenance-admin/coarse-site-20261029T212438Z`；站点记录目录为 `/srv/enterprise-backend/records/integration-6f0f86c36b61-amd64-20261029-01`。

| 验收项 | 已验证结果 | 本机证据索引 |
| --- | --- | --- |
| Task 1：回归与最终数量合同 | 针对性测试 184 passed / 6 skipped；完整测试 3537 passed / 35 skipped；跳过项为 SQLite 参数下的 PostgreSQL 专用语义检查。Ruff 与 473 文件格式检查通过。 | [针对性测试](</Users/kaizhou/.codex/recovery/maintenance-admin/coarse-site-20261029T212438Z/quantity-only-target.log>)、[完整测试](</Users/kaizhou/.codex/recovery/maintenance-admin/coarse-site-20261029T212438Z/quantity-only-full.log>)、[验收汇总](</Users/kaizhou/.codex/recovery/maintenance-admin/coarse-site-20261029T212438Z/acceptance.md>) |
| Task 2：复评与提交范围 | 最终数量合同的 17 文件哈希快照复评无未解决问题；完整测试通过后创建本地提交 `6f0f86c`，未推送或合并。 | [最终增量复评](</Users/kaizhou/.codex/recovery/maintenance-admin/coarse-site-20261029T212438Z/final-review.md>)、[本地保留证明](</Users/kaizhou/.codex/recovery/maintenance-admin/coarse-site-20261029T212438Z/local-preservation-proof.json>) |
| Task 3：固定制品与保留检查 | linux/amd64、干净源码构建；231 个运行文件哈希匹配；四个应用服务镜像一致。原消息、命令、路由及 shared 配置保留，19 个共机或基础设施容器基线核对通过；备份校验并异机保留。 | [制品清单](</Users/kaizhou/.codex/recovery/maintenance-admin/coarse-site-20261029T212438Z/bundle/manifest.json>)、[部署完成证明](</Users/kaizhou/.codex/recovery/maintenance-admin/coarse-site-20261029T212438Z/completion-proof.json>)、[备份异机副本校验](</Users/kaizhou/.codex/recovery/maintenance-admin/coarse-site-20261029T212438Z/backup-local-copy.json>) |
| Task 3：配置与运行验收 | RS01→CLN01、RS02→CLN02；原六台设备归属及角色绑定读回正确。迁移 head 为 `d7cf20261029`，check 无新升级操作；worker ping 成功、beat 单实例；两套入口的 `/ready`、`/docs` 均为 200，current 已切换至本轮制品。 | [资产配置及 Status](</Users/kaizhou/.codex/recovery/maintenance-admin/coarse-site-20261029T212438Z/asset-configuration.json>)、[运行证明](</Users/kaizhou/.codex/recovery/maintenance-admin/coarse-site-20261029T212438Z/runtime-proof.json>)、[切换后入口核验](</Users/kaizhou/.codex/recovery/maintenance-admin/coarse-site-20261029T212438Z/public-entry-proof.json>) |

2026-10-29 15:46:49 PDT（UTC−07:00；同日 22:46:49 UTC）的后续只读复核再次确认：本机源码仍对应上述提交；交付包 66 个文件哈希通过；服务器 current、应用镜像、迁移 head、两条工作线及六台设备归属保持正确；两套入口四项 HTTP 检查均为 200。该复核时点的粗分扫码记录为 0，原命令、可靠消息各 2 条。此次只读复核未重新向 ECS 查询 Status。

**后续复核输出来源：** 本会话中用户要求对本计划“验证完成状态”后执行的只读工具输出。服务器检查结果的定位字段为 `verified_at_utc="2026-10-29T22:46:49.698204+00:00"`，同一 JSON 包含 `current`、`source`、`services`、`http`、`database` 及 `archived_status`，命令退出码为 0；本机检查输出包含 `Verified bundle files: 66`、`Verified current tracked files vs committed tree: 496` 和 `READ_ONLY_LOCAL_PRESERVATION_PASS`。上述时点取自服务器 JSON；本机输出为同轮核验，不据此声称它们具有相同采集时间。这次后续复核输出保留在该轮会话工具记录中，未另行落盘为站点证明；前表中的 `public-entry-proof.json` 为部署后 22:22:47 UTC（15:22:47 PDT）的入口检查，`completion-proof.json` 为部署完成证明，均不作为 22:46 后续复核的新输出，也不改写其原始时间戳。

原始 RED、历次范围调整和测试、评审及 Task 1–3 完成记录保留在[执行归档](</Users/kaizhou/.codex/recovery/maintenance-admin/coarse-site-20261029T212438Z/execution-workspace.tar.gz>)内的 `2026-10-29-coarse-sorting-site-configuration/progress.md`。最终数量合同以本节的 quantity-only 测试和最终增量复评为准；早期范围的记录不删除、不改写。

## 执行偏差：迁移重复执行但无变更

原计划要求“迁移一次”。实际先完成有效升级，随后通过 Compose 启动 nginx 时，连带再次启动了已处于目标 head 的迁移依赖，重复调用 `alembic upgrade head`，第二次未产生模式或数据变更。没有并行迁移，最终为一个正常迁移容器且退出码为 0。

该执行次数不符合原计划字面要求，按已记录偏差完成验收，不将其改写为“仅执行过一次”。最终 head/check、旧表 OID 与数据摘要及原消息、命令、路由保留校验均通过，未发现需回退的变更。证据见[迁移日志](</Users/kaizhou/.codex/recovery/maintenance-admin/coarse-site-20261029T212438Z/site-migrate.log>)、[启动日志](</Users/kaizhou/.codex/recovery/maintenance-admin/coarse-site-20261029T212438Z/site-start.log>)、上述执行归档和部署完成证明。后续单独启动 nginx 时应直接启动其既有容器，避免连带启动迁移依赖。

## 验收边界与快照保留

- 业务验收仍为 MOCK 和内部验证；部署时六台 Status 均返回 `is_online=true`、`status=UNKNOWN`，不据此认定设备就绪或动作完成。未发送现场测试命令、未注入扫码或触发真实 WMS 业务请求；未做完整备份恢复演练。
- 本轮流程仍止于等待箱格分配或 NG 阶段，未实现后续 PUT_TO_BIN、MOVE_TO_NG 或 OUTPUT_ARM 动作。ECS 对端仍需按当前数量合同返回结果，含 `location` 的旧 MOVE_FORWARD 结果不符合严格合同。
- 服务器交付包中的本计划、制品清单、镜像及原验收记录保持构建和部署时原样；本次状态回填不替换交付包，不改变上述测试、评审和部署证据对应的源码提交。
- 本次只更新本机仓库中的计划完成状态与证据索引，保留现有本地文件、历史记录及暂存状态；不提交、推送或部署。

## 后续交付：实际扫码字段与收据设备码

后续获用户授权，仅在本地提交 `b68afec828c308a53520ec9db7a62a6e02bcef77` 后生成 `integration-b68afec828c3-amd64-20261029-01` 并部署；未推送、合并或打标签。部署完成证明采集于 `2026-10-30T00:16:21.966502+00:00`（洛杉矶 2026-10-29 17:16:21 PDT）。本次文档回填只读该次归档，不连接服务器、重跑测试或重新部署。

- [实际扫码字段映射的部署验收](2026-10-29-coarse-sorting-scan-measurements.md#后续本地提交与联调部署验收)：实际报文尺寸保持 7 英寸/15.3 毫米原值，Swagger 与纯合同模型校验通过。
- [收据设备码及共同交付证据](2026-10-29-callback-receipt-device-code.md#共同交付证据与执行偏差)：服务器已到 `ea8b20261029`，nullable text 列与 hash 索引就绪；旧 4 份收据保持 NULL，设备写入和摘要查询行为采用内部测试证明，不声称注入过现场新收据。
- [部署完成证明](</Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z/server-evidence/completion-proof.json>)与[数据保留证明](</Users/kaizhou/.codex/recovery/maintenance-admin/review-deploy-20261029T235031Z/server-evidence/data-preservation-proof.json>)：RS01→CLN01、RS02→CLN02、原设备归属及路由、共享配置和 19 个非目标容器基线保留。该时点已有 1 条待核对扫码、2 条命令、3 条可靠消息；原失败验证消息未重发，不改写上方 15:46 只读复核时的 0 条扫码历史。

本次后续交付迁移仅执行一次，通过直接启动既有 nginx 容器避免依赖重启；传输方式调整、旧备份脚本权限重试、Timescale 提示及空迁移日志的证据限制，均在上述共同交付节单独记录。原 `6f0f86c` 的重复迁移偏差、原测试统计、六台设备 Status 记录及服务器交付包快照保持不变；本次后续交付未重新查询设备 Status 或发送测试命令。验收仍为 MOCK/内部验证与部署健康，不代表现场物理业务完成。
