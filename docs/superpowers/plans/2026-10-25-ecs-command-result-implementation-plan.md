# ECS P1 实现结项

**状态：已完成。** P1、全局安全诊断及日志修复随PR #22合并；后续单表参考、通用Swagger与自动装配随PR #23合并。当前文档与测试夹具收尾待合入develop，不代表生产部署。

| 完成模块 | 行为边界 |
|---|---|
| 事务创建/查询 | EcsCommands及可信owner；稳定调用键，冻结请求/地址/合同/期限，同事务Outbox与只读DTO |
| 快照投递 | 唯一Sender、动态客户端、原授权门禁、逐次尝试证据；UNKNOWN/HELD不自动重放，WMS STATIC不变 |
| 结果 | 严格信封/原附录、首次合法在途Result可先于ACK；证据/收据/投影/settle同事务，长期重复响应 |
| 竞争与维护 | 命令→原授权→消息→收据锁序；不持锁发HTTP；维护共享整轮预算/有界清理，不永久阻断健康消息 |
| 安全诊断 | HTTP/Sender/维护/Celery统一脱敏诊断，各自降噪与恢复；日志不改变重试/事务/交付决策 |
| 迁移兼容 | 旧STATIC可读；存在快照/HELD/ECS证据时拒绝破坏性downgrade。代码合并不表示本机数据库已迁移 |

## 证据与后续记录

PR #22合并基线ed9005f，历史CI2054通过/11跳过；原P1外部双路审查超时不计通过。PR #23合并基线479c48b，CI2322通过/25跳过。后续隔离404/18专项、清理与独立复核统一见[当前结项PLAN](2026-10-25-ecs-minimal-business-tutorial-plan.md)，不混称不同阶段数字为本轮重跑。

当前协议依据为[P1 SPEC](../specs/2026-10-25-ecs-command-result-design.md)，新命令W与通用接入见[当前设计](../specs/2026-10-25-ecs-minimal-business-tutorial-design.md)。普通开发者直接从[短教程](../../integrations/ecs-command-tutorial.md)开始，不需要读历史Task1–8。

本开发计划不列生产验证待办；真实供应商、现场、部署和prefork环境验证独立安排，见[接入清单](../../integrations/ecs-acceptance-checklist.md)。本页只保留当前交付摘要，不附旧方案与逐轮过程。
