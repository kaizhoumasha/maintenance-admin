# 粗分双机配置与联调部署设计

状态：用户已确认，实施中。依据：用户提供的联调配置及随后确认的 device_code → 工作线角色绑定 → 对应流程；保留数量校验，清除固定出口依赖。

## 目标与方案

沿用现有设备绑定、角色事件分发和命令路由，业务代码不根据 RS01/RS02 或设备编号后缀选择流程。扫码按已登记设备的工作线角色进入 INPUT_ARM / SCAN_COMPLETED；业务按同一工作线的 CONVEYOR 角色选取输送设备。Result 继续按原 command_code + device_code 关联原命令并消费，不按接收时的新绑定重定向历史命令。

采用用户确认的角色方案。按设备编号或输送线固定出口编写分支会把现场编号耦合到业务代码，故不采用。

## 现场配置

| 工作线 | INPUT_ARM | CONVEYOR | 已登记下料设备归属 | OUTPUT_RACK：出料单层货架位 |
| --- | --- | --- | --- | --- |
| RS01 | RS-INPUT-ARM-01 | RS-CONVEYOR-01 | RS-OUTPUT-ARM-01 | CLN01 |
| RS02 | RS-INPUT-ARM-02 | RS-CONVEYOR-02 | RS-OUTPUT-ARM-02 | CLN02 |

- business_code 为 coarse_sorting。增加唯一位置槽 OUTPUT_RACK，不配置输送线入口、出口或面向。
- 下料设备归属对应工作线；本轮不增加未实现的 PUT_TO_BIN 命令或 OUTPUT_ARM 业务角色。
- ECS origin 为 http://10.24.209.32:8080；Status 路径仍为 /api/v1/device/status，命令路径沿用 /api/v1/device/command。
- 服务器已有六台设备及同一 origin 的不可变路由，沿用原设备和路由，不重复登记。

## Result 与阶段边界

MOVE_FORWARD 的 Result.data 仅声明 actual_qty，不携带 location。原命令与设备确定执行动作，通用原始报文证据照常保存。SUCCESS、原命令状态无待核对项、actual_qty 严格整数 1 的既定数量语义保持；失败、缺数量、未知结果继续保留待核对义务。

新调用使用 coarse_sorting.move_forward/2；不改写历史命令、冻结路由、字节及证据，不自动重放。当前联调旧镜像尚未装配正式粗分业务，无既有正式粗分命令需要转版；部署前核对原 ECS 记录仍为原身份。

成功仍止于 AWAITING_ALLOCATION_STAGE，NG 仍止于 AWAITING_NG_STAGE，不增加箱格分配、入箱或真实业务任务。

## 实施与验证范围

仅改粗分资源声明、Result 合同及消费判断；补齐公开接口配置、双线角色路由及错设备 Result 回归，更新实际使用文档和本轮验收记录。历史设计、计划和验收结论保留原时间范围。

先在独立 PostgreSQL/Redis 和 MOCK 对端验证，再按既有固定制品流程更新 /srv/enterprise-backend，保持项目 enterprise-backend-int、原数据卷、共享配置、端口和 WMS 配置。保留旧制品与备份，不操作共机 WES。

部署制品要求干净源码提交。用户已明确授权：测试和复评通过后仅提交本轮改动以生成固定制品，随后部署；不推送、不合并。

部署验收覆盖镜像/源码一致、迁移、API/worker/beat、业务配置和只读 Status。业务闭环用隔离 MOCK 验证，不向现场设备发送测试命令，不把部署成功表述为现场业务验收。
