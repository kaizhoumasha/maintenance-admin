# 业务多能力组合：阶段一验收记录

日期：2026-10-30。范围：业务子包多能力组合、唯一注册与分派（P1.1～P1.4）。
本次实现位于 feature/business-components-phase1；经阶段一独立复审后已提交 90861b7（代码、测试与本记录），原暂存文档保留，不部署。

## 已实现与覆盖

| 范围 | 实现与验收 |
|---|---|
| 静态多能力组合 | ComponentDefinition 不可变声明、有效资源合并、显式入口/模型路径去重、元数据变动使已校验运行时失效；冲突资源、重复 key、嵌套及双重输入认领拒绝 |
| 唯一合同与分派 | 真实 RegistrationSource 保留，每业务按显式模块及角色编译消费者/唯一目标；共享源模型只编译一次，真实重复键仍失败；未声明消费者不执行或广播 |
| 新命令授权 | 实际工作线业务及消费者关系校验，仅在原身份查重之后执行；已有调用保留冻结身份、参数、地址、严格附录及期限，Result 沿原记录处理 |
| 业务工厂与资源 | components 映射/必要方法校验，只有原业务服务、scan_pending 和统一任务；两内部能力按各自固定预算推进；固定资源子集保留业务/版本/绑定约束 |
| 可信输入和通知 | ECS 从绑定解析原业务，通过应用注入的目标解析器取得内部对象；WMS 入站回调按原关联单次绑定业务，缺失/矛盾拒绝；after_commit 只通知原业务 |
| 回归和本地边界 | 原收据优先重放、业务去重跨收据清理、回滚/通知/原粗分回归；本地入口保持原演示语义，dev 命令不可进入正式编译 |

主要新增用例：tests/businesses/test_components.py、test_component_dispatch.py；新动作授权/重放见
tests/integrations/ecs_command/test_model_create.py，工作线授权/资源子集见 tests/services/test_workline_business.py。
测试业务表使用独立 MetaData，finally 清理；最终串行查询确认所有 schema 中均无 test_component_inputs。

## 实际结果

- 新建一次性 PostgreSQL 17 / Redis 7，只供本次显式 TEST_DATABASE_URL/TEST_REDIS_URL 使用。
- 最终全量 `rtk proxy uv run pytest -q -rs`：**3697 passed、39 skipped，298.72 秒，exit 0**。
- `rtk proxy uv run ruff check .`：通过；`rtk proxy uv run ruff format --check .`：501 文件已符合格式；`rtk proxy git diff --check`：通过。
- `rtk proxy uv run alembic heads`：唯一 head ea8b20261029；无新增迁移、生产业务表或依赖。
- 正式清单只含 coarse_sorting；生产 COMPONENTS 为空，app/businesses/_components 目录未创建；无 bin_return/SMT 业务或占位合同。
- 结束时仅删除本次创建的 codex-components-p1-pg / codex-components-p1-redis 容器及其匿名测试卷，原服务未改动。

39 项跳过中，36 项是 SQLite 形态不适用的 PostgreSQL 锁/并发/独立连接分支，对应 PostgreSQL 形态实际执行；
另 3 项需单独的 DEVICE_MIGRATION_DATABASE_URL、RECEIPT_DEVICE_MIGRATION_DATABASE_URL、BINDING_MIGRATION_DATABASE_URL，
本次未配置。本阶段没有迁移改动，这三项不列为已验收。

## 审查、证据与边界

执行计划要求的一次独立只读审查发现两项 formal/local 问题：共享 dev 命令路径层级、共享本地入口被当作正式分派。
两项均有失败复现和修复回归，专项 73 passed / 13 条件跳过；最终全量包含其 PostgreSQL 形态。未声称做过第二轮独立审查。

真实 HTTP 接收器、原业务通知函数、生产 advance_pending 任务体、业务内部对象及数据库写入已串联验证；
测试在 enqueue 边界截获业务码后调用真实任务体，未验证独立 broker/worker 进程之间的传输。
日志保存在工作区 `.superpowers/sdd/2026-10-30-business-components-phase1/`：final-full-suite.log、
review-red.log、review-green.log、progress.md 及逐步 RED/GREEN 记录；该目录不进入版本控制。

实施期间外部操作合入 PR #40 并切回 develop，仅提交了既有 ECS 重启测试及验收文档；本轮没有提交这些外部变化。
随后在相同 HEAD 创建 feature/business-components-phase1，逻辑暂存区逐条核对保持一致，最终测试期间 Python 文件哈希保持一致。

阶段一验收时阶段二尚未实施；当前阶段二已提交 a34b28d，见[阶段二验收](business-components-phase2.md)。阶段三 bin_return/SMT 仍未接入。
本次无真实 WMS/ECS 发送、现场数据库访问或物理业务验收，不能据此启用回流业务。

阶段一提交前复审：独立审查检查组合、来源、唯一目标、命令权限/重放、回调保存点与装配；
删除 catalog 两处恒定/冗余分支，功能行为保持。提交前复测为 3697 passed、39 skipped，298.72 秒；静态检查通过。

进度复核（2026-10-30）：P1.1～P1.4 按计划出口逐项通过，详见[阶段一计划验收索引](../superpowers/plans/2026-10-30-business-components-phase1.md#按计划验收复核与当前进度2026-10-30)。后续联合全量 3791 passed / 36 skipped 已补齐原三套未配置迁移库；上述阶段一历史计数保留，不改写为新的阶段一重跑结果。当前 Alembic head 随阶段二迁移变为 f0b120261030；本轮仅同步文档，未重复测试或新增提交。

联合 ship 验证（2026-10-30）：补充共享入口与组件声明拒绝覆盖（提交 f14edbc）后，全量为 **3806 passed / 36 skipped**。此前 3791/36 是补充这 15 项用例前的历史结果；四套独立迁移库及串行 upgrade/check/唯一 head f0b120261030/downgrade 均通过。此处记录联合验证，不改写阶段一历史验收，也不代表部署或现场验收。
