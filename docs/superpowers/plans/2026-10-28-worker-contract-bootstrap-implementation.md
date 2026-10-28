# Worker 正式业务合同启动校验实施计划

> **For agentic workers:** Use superpowers:executing-plans and test-driven-development. 本次在用户指定的主目录功能分支执行，不提交或推送。

**Goal:** 关闭验收 A2 的 worker 缺口：错误正式合同阻止 worker 初始化，合法合同按正式发现 → 交叉校验 → local 入口顺序加载。

**Architecture:** 在 `app/tasks/celery_app.py` 创建 Celery 应用前，直接复用 API 使用的 `load_formal_modules()`、`catalog.validate_business_contracts()` 和 `load_local_modules(environment=...)`。不增加启动框架、业务 worker、Sender 或运行资源装配。

**Tech Stack:** 现有 Python、Celery、pytest 和独立冷进程测试，不新增依赖。

**Spec:** [开发简化设计 §6.3 / A2](../specs/2026-10-27-business-development-simplification-design.md)。本轮授权来自验收评审后的继续实施请求，仅修复阶段 1 启动校验，不实施阶段 2–6。

## Global Constraints

- 在主目录由 `develop` 新建 `feature/worker-contract-bootstrap`；保留两份原有文档内容及暂存状态。
- 正式 catalog 保持为空；不改配置、迁移、持久格式、协议或原消息，不连接真实对端。
- 只校验声明，不调用业务工厂/处理器，不装配业务运行时、路由或第二 Sender。
- 本轮新增文件保持未暂存；不提交、推送、合并或部署。

## Review Focus

- production 不加载 local 入口；local 入口不能修补正式合同缺项。
- 缺失入口和已加载但不匹配的初始入口均须拒绝启动。
- 全部三类正式入口须先于交叉校验；local 入口看到的是已验证清单。
- 正式校验失败须早于 Celery 应用构造；合法启动不能执行业务工厂或处理器。
- API 和 worker 同进程导入仍兼容；既有清理任务及独立合同 CLI 不受影响。

## Task 1: 修复启动缺口并验证

**Files:** 修改 `app/tasks/celery_app.py`、`tests/integrations/test_registration_bootstrap.py`；在 `docs/integrations/business-development-quickstart.md` 记录启动检查边界。

**Interfaces:** 仅消费现有发现/校验函数，不新增公共 API 或导出。

- [x] 扩展 API/worker 冷进程顺序测试，增加 worker 缺入口/初始合同不匹配失败用例；先运行并观察缺口导致的失败。
- [x] 在 Celery 应用构造前复用上述三个调用；运行专项测试，预期全部通过。
- [x] 运行现有架构、合同发现、业务装配、后台任务及独立检查回归；失败则定位共享根因。
- [x] 使用任务专属 PostgreSQL/Redis 和两个迁移库运行完整 pytest，报告实际通过/跳过；运行 Ruff、格式和差异检查。
- [x] 复核 diff、更新执行记录，并逐字节验证两份原文档和暂存补丁与操作前快照一致。

## 执行记录

2026-10-28，在主目录从 `develop` / `bfb82cafda37` 切出
`feature/worker-contract-bootstrap`，未创建额外工作区。操作前将两份文档内容、暂存及未暂存补丁
保存在 `/Users/kaizhou/.codex/recovery/maintenance-admin/worker-contract-bootstrap-f3krdhk6`；
操作后逐字节验证文档工作区内容、索引内容及整份暂存补丁一致。

- RED：新增/扩展的冷进程矩阵为 6 failed、2 passed；失败均来自 worker 缺少正式发现/校验，API 对照通过。
- GREEN：架构、发现、合同装配、后台任务和独立 CLI 专项为 111 passed、2 skipped；两项专项跳过需临时 PostgreSQL/Redis，由完整复验补足。
- 完整复验：`uv run --frozen --no-sync pytest -ra`，显式配置任务专属
  `TEST_DATABASE_URL`、`TEST_REDIS_URL`、`DEVICE_MIGRATION_DATABASE_URL`、
  `BINDING_MIGRATION_DATABASE_URL`，结果为 **3052 passed、28 skipped，191.21 秒，退出 0**。
  28 项仅为 SQLite 无法表达的 PostgreSQL 并发/锁/独立连接分支；PostgreSQL 对照、两个迁移库及 worker 存储用例均已执行。
- Ruff、格式及差异空白检查通过；本轮两份文档的 22 个本地文件链接目标检查通过。
- 独立只读复核未发现待修问题；另以禁止联网冷进程验证 local/production × API/worker 两种导入顺序，既有五类任务可加载，正式 catalog 仍为空。

本次仅修改 worker 启动校验、对应回归和边界说明；原有两份已暂存文档未改动，新增改动保持未暂存。
无提交、推送、合并、部署或真实 WMS/ECS 请求。结论只覆盖 A2 的本次启动缺口和隔离回归，
不代表阶段 2–6、真实业务、供应商或现场验收。
