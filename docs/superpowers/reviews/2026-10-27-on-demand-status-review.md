# 按需 ECS Status 代码审查与修复

日期：2026-10-27。范围：本轮按需查询差异及设备管理、权限、迁移、命令/结果保留接缝。只使用本地Mock和新建的独立PostgreSQL17/Redis7，未连接真实ECS/WMS、未操作现有业务库、未提交或推送。

## 发现与修复

P0/P1：无。发现并修复1项P2，未延期问题。

### P2：列表逐条重读配置导致并发删除404或地址筛选失真

位置：[device_service.py](../../../app/services/device_service.py:25)。原列表先按筛选/分页选择设备，再按ID逐条调用read_device。如果选中之后并发删除设备，整个列表抛404；若并发将origin从A迁到B，按A筛选的列表可能返回B及新版本。

在独立PostgreSQL中，使用真实分页和独立写事务，在选择完成后分别执行删除、改路由；两种回归先稳定失败。修复后直接序列化本次选中的配置，批量读取这些配置指向的保留路由，不再逐行重读当前配置。查询使用populate_existing保证本会话先前加载的设备对象得到刷新；列表和详情复用同一纯配置DTO/路由一致性校验。每页路由批量读取，也去掉了逐条重复SQL。

该行为保留分页行选中时的配置，不承诺count与分页两条SQL之间的全局事务快照；后续GET详情仍读取当前配置，已删除ID仍404。列表不授予命令执行许可，命令仍复核当前配置及绑定，历史路由不删除。

回归：[test_device_list_preserves_selected_configuration_during_concurrent_change](../../../tests/services/test_device_status_concurrency.py:502)，delete/migrate两种情况RED → GREEN。未改变接口字段、权限、状态查询结果或配置写事务。

## 其他重点结论

| 边界 | 代码及验证结论 |
| --- | --- |
| 后台同步彻底移除 | app/migrations/scripts/Compose/Dockerfile中无device_observations、DeviceObservation、device_tasks、sync_registered、refresh_one或schedule_refresh的生产引用；实际Celery默认模块加载后无device.*任务，Beat也无相关条目。启动只装配按需服务；其他任务/Sender保留。 |
| 无状态持久化 | ORM与新装PG实际schema均无观测表；列表/详情/写响应仅配置，刷新为DeviceStatusRead；状态筛选400；SQL与HTTP流验证零状态DML、无自动投递。Redis仅用于预览和协调。 |
| 查询并发 | 初始配置读取与HTTP分离；成功/上游失败均复核原ID/version/route及查询token。删除后新ID不接管旧请求，ABA/版本变化409；代次丢失/被更新请求替代409；迟到429仅延长等待，不释放新租约；取消释放和Redis故障拒绝保持。 |
| 迁移精简 | 按用户确认的未发布、未入库前提审查11e090516bf1，删除的仅观测DDL/计数。c31f20261027绑定迁移未改；配置/路由/绑定的非空降级保护保留。独立SQLite/PG真实升降级与守卫验证通过，唯一head不变。本轮不探测外部数据库；已应用旧版者仍需单独前向移除。 |
| 管理员权限 | 设备/工作线所有入口要求admin成员+操作码，read不隐含refresh；非管理员误授asset权限仍403，菜单隐藏；通用ecs:status:query独立。权限/撤权/HTTP合同随全量重跑。 |
| 历史与原路由 | 删除只改当前配置和事务审计，原路由/命令/请求/结果/证据保留。NEW预览历史计数、编码锁、绝对期限及回滚保持；角色解绑/配置失效停发、迟到Result按原严格合同处理；不以历史记录继续授权新发送。 |

## 验证

- 新增PG并发回归：修复前2 failed；修复后相关状态、配置、HTTP管理流专项45 passed。
- 独立SQLite/PG设备迁移、工作线绑定、事务守卫专项6 passed。
- 实际OpenAPI字段/原POST入口、ORM元数据、Celery注册表/Beat检查通过；独立PG最终为c31f20261027，三张配置表存在、无观测表。
- 最终全量：**2533 passed、27 skipped、0 failed，160.99秒**。27项为原有后端/场景限制；本轮两个新增PG回归、独立PG/Redis及两类迁移均实际执行。Ruff、格式及完整差异检查均通过；Alembic唯一head为c31f20261027。

全量进程和Python子进程使用socket保护，仅允许loopback连接；显式TEST_DATABASE_URL、TEST_REDIS_URL及两个独立迁移数据库URL。测试只使用本轮自有容器（35433/36380），不清共享队列或用户数据。暂存区/HEAD保持，修复代码和本报告不暂存。

未验收真实供应商/现场、前端页面、生产容量和Redis持久化恢复。旧版数据库的升级前提不能由本轮新装测试代替；实际发布需另行核对。
