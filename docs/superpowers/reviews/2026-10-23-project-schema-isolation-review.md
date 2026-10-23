# 本机 PostgreSQL Schema 隔离实施与验证

日期：2026-10-23（北京时间）。范围仅为本机 `maintenance-admin` / Compose 项目 `enterprise-backend`，数据库仍为 `enterprise_backend`；没有生产部署、Git 提交或推送。

## 实施范围

- ORM 元数据显式指定 `enterprise_backend`，统一引擎入口覆盖 API、可靠发送器、审计归档、可靠消息清理、回调清理及 Transport 告警任务；PostgreSQL 连接只搜索项目 schema，SQLite 将 schema 映射为空。保留现有自动索引名称。
- Alembic 在线入口同事务执行归属校验和原地移动，版本表也隔离；不修改历史 revision，head 保持 `a023a20261024`。仅接受完整该基线旧库，目标同名、缺表、早期 revision 均拒绝；未知外部 ledger 留在 public。无 CLI 命令上下文的 PostgreSQL Python API 调用、离线 `--sql` 均明确拒绝。
- 只移动部署文档列出的 12 张当前 ORM 表和 `alembic_version`。public 中的历史 `inbound_receipt`、`inbound_snapshot` 不在本轮归属白名单内，保持原状；其他系统表不迁、不删、不合并。
- 不改数据库/角色全局配置；原始连接的 `search_path` 实测仍为 `"$user", public`。没有修改连接凭据、数据卷、Redis 数据，也没有重跑管理员种子。

## 本机迁移证据

1. 先构建新应用镜像，停止本项目容器 API、worker、beat；迁移核对窗口暂挂并恢复已有宿主机 8001 reload 进程，没有关闭其用户终端。共享 PostgreSQL、Redis、WES 和其他项目容器不停止。
2. 停写窗口内记录两次只读一致性视图，仅保存对象身份、行数及内容摘要，不导出业务行。执行在线 `alembic upgrade head` 后逐对象比较，75 张表的 OID、行数、逐行排序摘要全部一致；其中 13 张仅 schema 改为 `enterprise_backend`，剩余 62 张 public 表位置及内容不变。
3. 全部 384 个 public/项目关系对象 OID 保持；仅项目表及所属索引、序列改变 namespace，其他对象的名称和 namespace 保持。47 个序列的 `last_value`、`is_called` 不变。
4. `alembic current` 为 `a023a20261024 (head)`；`alembic check` 返回 `No new upgrade operations detected.`，无索引重建或外键差异。
5. 容器 API 的 ORM 查询可读取原 4 个用户，连接 `current_schema()` 为 `enterprise_backend`；容器 `/ready` 与宿主机 8001 `/ready` 均返回 200。worker `inspect ping` 返回单节点 `pong`，beat 正常启动。仅更新本项目三个应用服务，不启用真实 WMS 目标。

## 审阅与回归范围

独立只读审阅发现并修复了四项：早期 revision 静态白名单可能误搬外部同名表、离线 SQL 落入 public、schema 导致索引名漂移、只读命令误认外部 ledger。随后再次收紧缺表校验及无 CLI 上下文的程序化调用；复核未发现新的阻塞项。审阅者未操作共享数据库，运行验证由实施方独立完成。

回归覆盖旧库数据/序列 OID 保全、冲突和缺表整笔回滚、早期版本拒绝、外部同名表及 ledger 隔离、只读命令不迁移、运行时 ORM/原始 SQL 的 schema 选择、离线与程序化调用拒绝，以及 `alembic check` 无差异。PostgreSQL/Redis 使用一次性独立测试容器，不复用本机开发数据。

最终完整套件：**1533 passed、5 skipped，85.38 秒**。5 个跳过均为 SQLite 不支持的 PostgreSQL 唯一索引等待/行锁参数分支，对应 PostgreSQL 分支实际运行通过；没有端口绑定或 Docker 权限失败。最终 `ruff check .`、`ruff format --check .`（240 个文件）、`git diff --check` 通过。一次性 PostgreSQL/Redis 测试容器已清理。

## 边界

本轮验收只证明本机 schema 隔离、数据保全及应用回归，不等于真实 WMS 或物理设备验收。业务 downgrade 不迁回 namespace，旧版应用不能直接连接迁移后的表；回退需要单独批准的反向迁移方案。早于 `a023a20261024` 的 public 旧库须先用旧代码升级到该基线。
