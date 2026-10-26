# ECS 接入与开发者体验结项

**状态：本批次本方开发与自动化回归已完成，不再新增Task。** 应用实现已随PR #22/#23合并；本批文档与测试夹具收尾在当前feature分支，待合入develop，不代表生产部署。生产验证不纳入开发计划，另见[真实ECS接入清单](../../integrations/ecs-acceptance-checklist.md)，状态未启动。

当前使用入口：[短教程](../../integrations/ecs-command-tutorial.md)，直接选择业务接入或Swagger调试。回归复用[现有pytest说明](../../testing/ecs-command-verification.md)，不再设置ECS专属隔离设计或实施阶段。合同见[设计](../specs/2026-10-25-ecs-minimal-business-tutorial-design.md)，无需阅读历史执行过程才能使用。

## 已完成范围

下列Task 17–22等编号仅对应已执行的历史范围；其中临时环境/模拟端操作不再作为后续开发阶段或产品接入要求。保留原数字和过程偏差，不重写为新的验收结论。

| 阶段 | 交付 |
|---|---|
| 基础与参考 | P1事务命令/结果证据、公开查询；本项目测试参考闭环；单表登记/消费及当前权威许可 |
| Swagger及统一装配 | 请求必填ecs_url、专用权限、通用v3合同、唯一逻辑通道；清除旧地址配置、开关、清单与逐动作登记 |
| Task 17–19 | W=300秒最低合同与共享创建校验；可信历史附录、旧策略围栏、旧调用/冻结期限；实际启动及受限出口 |
| Task 20 | 入门路径、职责/文件及现有测试映射；不新增SDK或示例副本 |
| Task 21 | 一次隔离自动化、双模拟端HTTP及API/Sender停止交接 |
| Task 22 | 按执行输入复用证据，正常/故意失败清理与结项；零新应用代码、零新增测试 |

原Task 10已移出开发任务计数；外部对端符合性、现场和生产验证均未启动，不阻塞以上本方交付关闭。按需设备管理、真实业务许可、来源控制或诊断功能另定开发范围，不与生产验证混成未完成Task。

## 保留的回归入口

| 行为 | 现有位置及必须保持的断言 |
|---|---|
| 两条当前许可 | test_service.py：test_new_reference_intent_requires_current_permission_but_replay_keeps_identity、test_new_authorized_reference_intent_uses_current_version；旧调用保留原身份，新动作读取当前许可/版本 |
| 路由P2与P3 | test_service.py：test_pending_route_keeps_active_route_and_revocation_only_fences_old_call，revoked=False/True；A撤销B有效允许新意图走B，A未撤销时旧命令仍按原授权走A |
| 消费P2与权威事实 | test_query.py：test_consumption_and_business_change_follow_lock_order，cancel/rebind/version×两种顺序；test_consumer_requires_current_authority_even_when_action_is_unchanged；原结果保留，拒绝错误业务推进 |
| 窗口/原期限/停启 | test_service.py、test_restart.py、test_dev.py；新正式/调试300秒，不一致配置拒绝；历史60/600秒及原退避/Retry-After不重算，UNKNOWN/HELD不解封 |
| 启动/历史/出口 | test_dev.py与test_dev_ecs_command.py；唯一装配/冲突、原附录严格校验/缺失503/重复原响应；test_isolated_sender_rejects_unowned_origin_before_tcp直接断言零TCP |

复用覆盖，只有稳定复现的行为缺口才在原文件补回归；不删参数、放宽断言或复制Reference。没有缺口可以零代码结项。生产验证不加入此表。

## 验证证据（2026-10-26）

| 证据 | 结果与限制 |
|---|---|
| PR #23合并CI | [479c48b](https://github.com/kaizhoumasha/maintenance-admin/actions/runs/40275200539)：2322通过/25跳过；Ruff及341文件格式检查通过。这是合并时历史证据 |
| 本批SQLite理解路径 | 4通过/4排除，1.82秒；不证明PG锁 |
| 本批控制器+ECS专项 | 404通过/18跳过，32.54秒，只运行一次；跳过为SQLite不适用的PG锁/独立连接，对应PG参数通过 |
| 本批Ruff | 隔离允许副本check通过，337文件格式正确；不冒充合并CI341文件的新跑结果 |
| 双端真实HTTP | 真实登录/OpenAPI；D1/MOVE/qty与D2/LIFT/嵌套JSON；先断言ACK后WAITING_RESULT且result为空，再模拟SUCCESS/FAILED与重复原ACK；原身份重入、换址/异参409、匿名401 |
| 出口/消费 | 手工非本轮地址无ACK；零TCP由同次回归直接断言，不能从无ACK推断。安全消费由Reference回归证明，Swagger不建业务动作表 |
| 生命周期 | API停止而runner/PG/Redis仍在，然后结项清理；故意exit42触发同一cleanup函数。标签容器/网络/镜像及记录匿名卷均无残留，既有容器集合不变 |
| 过程修正 | 首次残留核查发生在解除trap之后，如实保留；补教程实际核查块，清理专用演练exit0后才解除trap。API仅证明已停止，未采集优雅退出/强制终止判据 |
| 独立终审 | 两处过程证据问题关闭，无剩余P1/P2/P3；未重复业务专项或全仓 |

原始本机日志根目录：`/var/folders/bk/mwmtsjpn10dbcmzzp5zn21wr0000gn/T/ecs-dx-closeout-lp31ntq9`。execution.log记录专项/HTTP/清理；cleanup-check.log与cleanup-check.exit记录补验；verification.json记录源码、HEAD/index/stash/配置保护。临时目录是原始诊断辅助，不是长期可用的测试服务。

未验收真实浏览器点击/渲染、供应商持久去重/跨重启断电、现场物理行为或生产部署。未迁移本机数据库、连接真实设备或停止既有服务。历史阶段数字不累计为本批新跑结果。

## 当前开发边界

独立隔离SPEC/PLAN及长篇临时环境操作说明已退出项目，原记录保存在项目外。测试仍保护数据和网络出口，但只通过现有pytest/夹具承担；开发环境使用本地Mock或开发者指定地址，现场目标与许可由现场接入/运维确认。

## 文档保留规则

只保留当前交付、回归入口和最终证据，不继续追加讨论与逐轮命令。历史过程保存在项目外，项目文档不链接旧记录，避免作为AGENT当前上下文；需要追溯时由维护者按需提供。以后新增能力另开有明确范围的开发计划；实际生产验证独立安排，不回挂到本批次。
