# 开发测试

使用 Python 3.13+ 和 `uv sync --frozen` 安装锁定的运行及开发依赖。保留已有 `.env`，不要为了测试覆盖本机配置、执行生产 SEED 或迁移运行库。

## 存储与命令

SQLite/fake Redis 用于单事务单元测试。并发、真实过期、迁移和 Celery worker 测试使用本轮可销毁的 PostgreSQL/Redis：

```bash
export TEST_DATABASE_URL='postgresql+asyncpg://postgres:test@127.0.0.1:25433/enterprise_test'
export TEST_REDIS_URL='redis://127.0.0.1:26380/1'
uv run pytest -ra
uv run ruff check .
uv run ruff format --check .
```

上面的地址仅示例，需先创建对应的一次性实例，并替换为其实际地址。`TEST_*` 不能指向开发业务库、现有 Redis 或仍有联调 API/Sender 的实例；夹具会迁移并清理测试数据。未配置时相关用例跳过，配置后的连接失败必须失败；跳过不算验收通过。显式的测试库迁移不是运行库升级。

普通测试走脚本化 HTTP 客户端、MockTransport 或测试自身持有的回环服务，不向真实 ECS/WMS 发送任务。需要使用真实地址的调试应另按接入合同授权；测试出口保护不变成生产地址白名单。

## 按行为选择已有回归

| 行为 | 入口与关键断言 |
|---|---|
| 授权与管理员保护 | `tests/services/test_rbac.py`、`test_user.py`，`tests/controllers/test_rbac.py`；不同目标/交叉入口竞争后一成功一冲突、rollback 释放、当前状态重读；SQLite 不证明 PG 并发 |
| 刷新会话 | `tests/services/test_sessions.py`、`tests/controllers/test_auth.py`；真实 Redis 覆盖索引新建/过期/永久、缩短/延长有效期、单点/多点、唯一轮换及全部登出不可复活 |
| 同码权限与接口说明 | `tests/core/test_permission_code.py`、`test_errors.py`；每条路由的鉴权/撤权/审计和 403 文档，不用唯一权限码数推断路由数 |
| 筛选与分页 | `tests/services/test_filtering.py`、`test_pagination.py`，`tests/controllers/test_filtering_postgres.py`；严格类型、全部算子、角色去重、时区/溢出、NUL、ILIKE 转义与游标无重复；合同见 [筛选设计](../superpowers/specs/2026-10-17-list-query-design.md) |
| 单次 HTTP | `tests/http_client/`；原字节、单次请求、错误映射、有界原始响应、取消归还连接、并发关闭/失败重试、导入隔离；合同见 [HttpClient](../superpowers/specs/2026-10-20-spec-019a-http-client.md) |
| 可靠投递 | `tests/reliable/`；原事务/身份/字节、PG 租约围栏、独立 TTL、双进程骤停、存储失败传播与保留期；[接入参考](../integrations/reliable-delivery.md) |
| Transport 与 WMS 请求 | `tests/integrations/wms_transport/`、`tests/integrations/wms_request/`；完整 Outbox→claim→send_one 链与回调事务，恶意 ACK 留在未知/可重试而不误终止 |
| 出站结果与消费恢复 | `tests/integrations/test_result_consumption.py`、WMS/Transport 的 `test_results.py` 与 `test_predecessors.py`；原身份和可信终局响应、拒绝与里程碑冲突、更新 UNKNOWN 优先、分批/逐个统一快照和读取后失败整组重读；[读接口契约](../integrations/outbound-results.md) |
| 工作线业务公共接缝 | `tests/businesses/`、`tests/controllers/test_workline_business.py`、`tests/services/test_workline_business.py`；正式冷启动、合同交叉校验、两线绑定、受理/独立推进事务、消费与 Outbox 原子性；[业务包开发指南](../integrations/business-packages.md)。真实业务仍待独立确认 |
| ECS 命令与结果 | [ECS 测试说明](ecs-command-verification.md)，复用实际装配、原期限、当前许可和消费者锁序，不增加独立验收平台 |
| 安全诊断 | `tests/core/test_global_diagnostics.py`、`tests/reliable/test_sender_logging.py`、delivery/snapshot 与 `tests/tasks/`；安全字段、取消/落库失败传播，各任务独立降噪/恢复；[日志说明](../logging.md) |

先固定需要修复的失败行为，再最小修改并执行受影响测试。已有通过且输入未变的验证可复用；全量统一执行后，仅在新改动、失败或未决疑点出现时重跑。人类说明文档只检查内容、链接和归档完整性，不新增永久演示测试。

这些回归证明本方软件行为，不替代供应商去重、现场物理行为或生产部署验收。
