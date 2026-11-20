# ECS 测试说明

这是普通pytest回归的入口说明，不是业务接入阶段、产品能力或独立隔离方案。开发者直接使用[业务接入与Swagger教程](../integrations/ecs-command-tutorial.md)；本地Mock或开发者指定地址均可用于调试。

## 运行已有测试

```bash
uv run pytest tests/controllers/test_dev_ecs_command.py tests/integrations/ecs_command/ -q -rs
```

环境准备见[开发测试](README.md)。沿用项目通用测试约定：SQLite/fake Redis用于单元测试；TEST_DATABASE_URL和TEST_REDIS_URL显式指向可清理的测试库/实例。PG fixture会迁移并清理记录，不能指向开发业务库；未配置时相应用例跳过，配置后的连接失败必须失败。SQLite通过不代表PG锁/并发通过。

## 复用现有夹具

- conftest.py中的Reference用于事务内登记、安全消费、当前权威许可及业务竞争；不复制为生产SDK。
- MockTransport或本次用例自行持有的回环服务验证网络边界，不连接真实设备。测试出口保护留在tests/helpers.py等夹具，不增加业务地址清单或开发启动开关。
- test_dev.py直接运行app.main的真实lifespan；ecs_simulators夹具只提供本次测试的模拟端及受限客户端，不包装应用启动、不向应用状态写入模拟器数据。开发服务使用app.main:app。
- test_service/test_query/test_flow/test_concurrency/test_dev/test_restart及控制器用例保留两条许可、路由P2、消费P2、改址P3、冻结期限，以及交付未知/合同暂停的恢复边界断言。具体映射见下表。

已有通过且执行输入未变的范围复用证据；只改说明不重复回归。行为缺陷先用原公开入口复现，再最小修复并运行受影响测试，不为文档整理或夹具接线新增用例。不要在同一测试库仍有运行中的联调API/Sender时运行会清理数据库的测试。

重启恢复测试区分命令记录的创建期限、ECS 投递窗口和单轮维护的真实时间预算。`Sender.tick()` 返回本轮领取的发送数；返回 `0` 不保证维护已完成。状态门禁尚未产生 `dispatch_started_at` 时，设备未恢复 `IDLE` 不消耗投递窗口；已开始派发的 ECS 消息在窗口到期或交付未知时，若冻结路由带 `retry_window`，则以原命令身份回到 `PENDING` 并滚动等待 ACK/Result。没有可续期窗口或属于合同暂停的消息才保持 `HELD`/`EXPIRED`。测试同时检查冻结身份、请求字节、原始创建期限和派发证据不被改写。

生产与现场验证独立安排，不加入开发测试流程；测试通过不证明真实ECS或物理动作符合性。

## 必须保持的回归入口

以下文件位于 `tests/integrations/ecs_command/`，控制器用例位于 `tests/controllers/`。只维护行为与现有入口，不把历史执行数字写成本轮新证据。

| 行为 | 现有位置及必须保持的断言 |
|---|---|
| 两条当前许可 | test_service.py：test_new_reference_intent_requires_current_permission_but_replay_keeps_identity、test_new_authorized_reference_intent_uses_current_version；原身份重入保持原记录，新动作读取当前许可/版本 |
| 路由P2与P3 | test_service.py：test_pending_route_keeps_active_route_and_revocation_only_fences_old_call，revoked=False/True；A撤销B有效允许新意图走B，A未撤销时旧命令仍按原授权走A |
| 消费P2与权威事实 | test_query.py：test_consumption_and_business_change_follow_lock_order，cancel/rebind/version×两种顺序；test_consumer_requires_current_authority_even_when_action_is_unchanged；原结果保留，拒绝错误业务推进 |
| 窗口/原期限/停启 | test_service.py、test_restart.py、test_dev.py；新正式/调试300秒，不一致配置拒绝；历史60/600秒及原退避/Retry-After不重算，窗口到期或交付未知按冻结 `retry_window` 以原身份回队列，合同暂停仍保持 `HELD` |
| 状态门禁与过期恢复 | test_status_gate.py、test_restart.py：设备未 `IDLE` 时续期派发前窗口；维护或重启跨过窗口后，ECS 消息按原身份重新排队并可继续发送，原创建期限、请求字节和派发证据保持不变；无续期策略的消息仍按原过期/暂停语义处理 |
| 启动/历史/出口 | test_dev.py与test_dev_ecs_command.py；唯一装配/冲突、原附录严格校验/缺失503/重复原响应；test_isolated_sender_rejects_unowned_origin_before_tcp直接断言零TCP |

复用覆盖，只有稳定复现的行为缺口才在原文件补回归；不删参数、放宽断言或复制Reference。没有缺口可以零代码结项。生产验证不加入此表。
