# 独立 ECS 角色框架交付计划

Spec: [独立框架设计](../specs/2026-10-27-ecs-role-framework-design.md)。本计划只对应本次拆出的独立分支，不把设备管理或 Status 任务纳入交付。

## 实施项

- [x] 事件组合键、角色常量示例、严格模型及重复来源校验；同步原事件调用与测试。
- [x] BindingResolver 注入与原 receipt 优先去重；处理器上下文仅持有解析结果，不导入工作线服务。
- [x] RoleRoutes 通过绑定及路由解析器选命令附录，沿用冻结格式和配置版本门禁。
- [x] 配置撤销暂停保留原结果处理；未知状态和历史重放限制不放宽。
- [x] LocalEcsRoutes 归入原 dev 装配，保留登录／发送权限及同一 Sender；不新增默认业务设备。
- [x] 目录按角色展示合同，从应用读取可选能力描述，不导入 Status。
- [x] 独立角色测试覆盖多角色严格合同、停发和迟到结果，loopback Mock 验证原 HTTP 客户端链路。
- [x] 独立接入说明和设计，明确使用方锁序责任、调试及真实执行边界。

## Verification

- 对本独立分支运行 uv run pytest（独立 TEST_DATABASE_URL/TEST_REDIS_URL），以及 Ruff check/format 和 git diff --check。
- 验证分支中没有设备／工作线管理、Status 模块、功能迁移或默认 app 装配改动。
- 原工作区文件、index、配置与 stash 保持；不连接真实 ECS/WMS，不合并、不部署。

本计划的命令行／HTTP 合同验收由自动化测试执行，不要求浏览器或现有业务服务参与。测试与评审的最终证据记录在 PR。
