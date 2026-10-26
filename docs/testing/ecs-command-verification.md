# ECS 测试说明

这是普通pytest回归的入口说明，不是业务接入阶段、产品能力或独立隔离方案。开发者直接使用[业务接入与Swagger教程](../integrations/ecs-command-tutorial.md)；本地Mock或开发者指定地址均可用于调试。

## 运行已有测试

```bash
uv run pytest tests/controllers/test_dev_ecs_command.py tests/integrations/ecs_command/ -q -rs
```

沿用项目通用测试约定：SQLite/fake Redis用于单元测试；TEST_DATABASE_URL和TEST_REDIS_URL显式指向可清理的测试库/实例。PG fixture会迁移并清理记录，不能指向开发业务库；未配置时相应用例跳过，配置后的连接失败必须失败。SQLite通过不代表PG锁/并发通过。

## 复用现有夹具

- conftest.py中的Reference用于事务内登记、安全消费、当前权威许可及业务竞争；不复制为生产SDK。
- MockTransport或本次用例自行持有的回环服务验证网络边界，不连接真实设备。测试出口保护留在tests/helpers.py等夹具，不增加业务地址清单或开发启动开关。
- test_dev.py直接运行app.main的真实lifespan；ecs_simulators夹具只提供本次测试的模拟端及受限客户端，不包装应用启动、不向应用状态写入模拟器数据。开发服务使用app.main:app。
- test_service/test_query/test_flow/test_concurrency/test_dev/test_restart及控制器用例保留两条许可、路由P2、消费P2、改址P3、冻结期限和UNKNOWN/HELD断言。覆盖映射见[结项记录](../superpowers/plans/2026-10-25-ecs-minimal-business-tutorial-plan.md)。

已有通过且执行输入未变的范围复用证据；只改说明不重复回归。行为缺陷先用原公开入口复现，再最小修复并运行受影响测试，不为文档整理或夹具接线新增用例。不要在同一测试库仍有运行中的联调API/Sender时运行会清理数据库的测试。

生产与现场验证独立安排，不加入开发测试流程；测试通过不证明真实ECS或物理动作符合性。
