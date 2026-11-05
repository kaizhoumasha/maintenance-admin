# SMT AUTO 实施与验收

AUTO 的实施工作区提供入库与出库共用的宿主、待办入口和原身份恢复。隔离测试将这份实际 composition 与粗分、SMT 手工装配在一起。部署用 catalog 仍以原有业务为准，待双方合同与设备、现场证据齐备后再按总计划评审准入。

| 范围 | 可核对的软件证据 | 验收边界 |
| --- | --- | --- |
| 共同任务与恢复 | [原 WMS 收据](../../tests/businesses/smt_auto/test_receiver.py)、[原 SCAN2 释放](../../tests/businesses/smt_auto/test_release.py)、[版本与完成确认](../../tests/businesses/smt_auto/test_completion.py)覆盖原身份、回滚、重放、晚到、无任务旁路、真实命令结果及完成交错 | 使用隔离数据和模拟对端 |
| 入库 | [入库回归](../../tests/businesses/smt_auto/test_inbound.py)覆盖两组机械臂的正常链、NG、PICK_EMPTY、WAIT/RETRY、来源取尽与原离场；[归档回归](../../tests/businesses/smt_auto/test_archive.py)覆盖本地关闭后七天、晚到 PREPARE、清理外键及原收据 | 供应商原命令内恢复及实际工位动作待对端验收 |
| 出库 | [出库回归](../../tests/businesses/smt_auto/test_outbound.py)覆盖 BIN、RackSlot、WAIT、原报告、退料架换面和目标架交接；[合同回归](../../tests/businesses/smt_auto/test_outbound_contracts.py)覆盖已确认的 WMS 空取请求和决定结构 | 按原取盘 FAILED/0/PICK_FAILED 及 finish_time 处理空取。KT11 ARM01、KT14 ARM03 按原 NG Result 成功后报告，再续问。真实报文、设备执行与 NG 区域待对端验收 |
| 装配与重启 | [真实 worker/Beat 回归](../../tests/tasks/test_smt_auto_worker.py)使用隔离 PostgreSQL 和 Redis，通过真实 WMS 接收入口在提交后唤醒 worker，并在重启后恢复同一条原 PREPARE；还通过真实 SCAN2 接收入口验证并发首问去重、NO_WORK、任务取消后原释放命令和 ECS SUCCESS 恢复。测试进程显式选入 AUTO composition | WMS 响应由测试写入原交付记录，物理设备联调待验收 |
| 迁移 | [来源约束升降级回归](../../tests/migrations/test_smt_auto_rack_sources.py)覆盖 SQLite/PostgreSQL、原公共 RackJob/RackAction 保留、有 AUTO 原证据时拒绝有损降级 | 仅应用于隔离测试数据库 |

此前启用隔离 PostgreSQL 和 Redis 的受影响回归为 **1906 通过、4 跳过**。整仓默认运行 **4101 通过、2104 跳过**，一项既有粗分测试因环境缺少 `TEST_REDIS_URL` 失败，提供隔离 Redis 后单独重跑通过。Ruff、格式、差异检查与单 head 核验通过，代码 head 为 `smtautosources`。

本轮收尾在独立 PostgreSQL 17 与 Redis 8 上通过 **681 项**，覆盖 SMT AUTO、RackDispatch、BIN_Roller 原证据保留、两线模拟绑定、迁移、worker/Beat 和清理任务。Ruff、格式与差异检查通过。

开发阶段已收尾。后续对端报文和实机证据按 [KT11 / KT14 联调留档清单](../superpowers/plans/2026-11-05-smt-auto-ecs-delivery-closeout.md#kt11-kt14-integration-checklist) 收集。设备与现场验收沿[总计划 D1–D8](../superpowers/plans/2026-10-31-smt-auto-implementation.md#delivery-dependencies)核对。包括 WMS 去重和库存事务、两组机械臂原命令阶段恢复、出库空取与 NG 设备合同，以及工作线角色、点位、容量、端点和真机联调。原 ACK、测试模拟结果、离线 worker 及迁移通过只作为各自软件层证据。

## KT11 / KT14 配置复验

以[已确认配置基线](../integrations/smt-auto-api.md#auto-config)填充隔离库。该轮隔离复验早于本地四线配置写入。试验环境使用临时超时，点位容量留空，WMS 决定及 ECS Result 使用模拟响应。

| 工作线 | 已核对的绑定 | 隔离验证 |
| --- | --- | --- |
| KT11 | ARM01 / ARM02，SCAN1～4 对应 STATION_SCAN1～4，交接 CNV0101 / CNV0102，单层 OUT51 / OUT52，转运 OUT61，退料 OUT62 | 6 个角色路由、7 个点位、12 个错误宿主或角色的拒绝检查通过。原 SCAN2 放行冻结 STATION_SCAN2 及共用滚筒路由 |
| KT14 | ARM03 / ARM04，SCAN1～4 对应 STATION_SCAN5～8，交接 CNV0201 / CNV0202，单层 OUT53 / OUT54，转运 OUT63，退料 OUT64 | 6 个角色路由、7 个点位、12 个错误宿主或角色的拒绝检查通过。原 SCAN2 放行冻结 STATION_SCAN6 及共用滚筒路由 |

方向与收尾组合 **208 项通过**，包含实际点位代入后的入库、出库、原释放与确认、归档、事务、目录和 SQLite/PostgreSQL 迁移验证。两线实际 worker/Beat 回归在修正测试目标名后分别通过，共 **2 项通过**。每线都验证并发首问唯一、原 PREPARE 重启恢复、NO_WORK、取消及原 ECS SUCCESS 恢复。Alembic 代码 head 仍为 `smtautosources`。

现场收尾仍需核对实际工作线配置、容量、超时和设备连通性。出库空取原 Result、NG 放置和结果重报、WMS 入账与 RCS 到位继续按 D2–D8 联调验收。部署目录当前仍为粗分和 SMT 手工。
