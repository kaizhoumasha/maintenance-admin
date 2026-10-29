# 粗分双机配置与联调部署 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 配置 RS01→CLN01、RS02→CLN02，沿用角色驱动流程并完成独立应用联调部署。

**Architecture:** 复用 WorkLine.device_bindings、position_bindings、现有事件注册与命令路由。粗分声明 OUTPUT_RACK；Result 按原命令及设备身份关联，MOVE_FORWARD Result.data 仅包含 actual_qty。

**Tech Stack:** Python 3.13、FastAPI、SQLAlchemy、pytest、PostgreSQL、Redis、Docker Compose 固定镜像。

**Spec:** [双机配置设计](../specs/2026-10-29-coarse-sorting-site-configuration-design.md)

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

- [ ] 先新增角色与公开配置行为回归，再运行观察旧实现失败。

  ```python
  assert response.json()["position_bindings"] == {"OUTPUT_RACK": "CLN02"}
  assert (await flow.result(row, data={"actual_qty": 1})).status_code == 200
  assert await flow.service.advance(row["id"]) == "AWAITING_ALLOCATION_STAGE"
  ```

- [ ] 将另一条线输送设备码用于原 command_code，断言接收器拒绝且原扫描未完成；覆盖仅数量、缺数量、FAILED、错误数量及非合同字段。
- [ ] 最小实现：声明必需 OUTPUT_RACK；Result.data 仅声明 actual_qty；保持数量约束；消费判断只依据原命令完成与数量；新附录版本 2。
- [ ] 同步粗分夹具的货架位及附录身份，保持通用平台测试资源声明不变。
- [ ] 用独立测试数据库/Redis运行粗分、工作线及 ECS Result 相关测试，再运行完整 pytest、Ruff及格式检查；证据写仓库外。

## Task 2: 文档同步与复评

**Files:** docs/integrations/{coarse-sorting-api-draft.md,device-management.md,ecs-event-debugging.md}、docs/deploy.md、CHANGELOG.md，本设计与计划。

- [ ] 更新当前合同及两条线配置实例；修正“仅 INPUT_ARM”“catalog 为空”等当前用法漂移，保留历史验收段落。
- [ ] 复评旧固定出口、按设备编号分支、角色/附录/文档一致性；核对全部变更均属于本轮。
- [ ] 测试和复评通过后仅提交本轮已验证文件；不暂存或提交其他文件。执行状态以本轮 ledger 及站点 records 为准，交付包中的本计划保留构建时快照。

## Task 3: 固定制品部署与验收

**Interfaces:** scripts/release_bundle.py；既有 shared/compose.sh 的 dc；当前站点管理员 API。

- [ ] 在构建机生成新的 linux/amd64 固定镜像和干净源码交付包，验证清单和架构；保留旧包，服务器不构建。
- [ ] 保存站点配置、旧镜像及本项目/WES容器基线；只停止本项目应用，最终备份校验并异机保留，备份失败不升级。
- [ ] 沿用原 Compose 项目及完整站点覆盖文件，迁移一次，启动同一镜像的 API/worker/beat/migrate；保留基础设施实例、卷和凭据。
- [ ] 用现有管理员认证和带审计的服务接口创建 RS01/RS02，按返回版本分配原六台设备并写入 INPUT_ARM、CONVEYOR 和 OUTPUT_RACK；不重建原路由、不生成命令。
- [ ] 核对 /ready、Swagger 事件/命令、单迁移 head/check、worker ping、beat单实例；配置读回与六台设备 Status 只读查询；比较原消息/路由及 WES 容器基线。
- [ ] 完整验收通过后切换 current 指针，记录制品、迁移、配置及验收边界；本地提交/推送/部署各自如实报告，不声称现场业务完成。
