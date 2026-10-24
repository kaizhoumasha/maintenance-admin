# Unified Integration Registration Implementation Plan

> 当前版本已按用户后续要求移除旧类式注册并 simplify；最初兼容过渡方案已被本版替代。在当前会话完成，不提交、不推送。

**Goal:** 三类接入共用声明心智、文件名规则和本地清单；消除旧类入口和动态适配，保留业务、协议及事务语义。

**Architecture:** ECS 和 WMS 回调注册异步函数，WMS 请求直接注册 RequestPayload 模型。注册项直接保存函数/模型及元数据，不创建动态类。三类注册项提供 data_model 和 registration_source；Transport 的共享业务流程用普通函数复用。

**Tech Stack:** Python 3.13、Pydantic、FastAPI、pytest，零新增依赖。

**Spec:** 本会话批准的统一声明、注册清单和报错路标；后续明确授权清理旧兼容方式。接入细节见 docs/integrations/registration.md。

## Global Constraints

- ECS 保持 @ecs_event(name, data=Model)，data 可省略以接收原始 dict。
- WMS 回调只用 @callback_operation(name, data=Model, title=..., echo_keys=...) 装饰 async def handler(ctx, payload)。
- 请求只用 @request_operation(name, Endpoint, title=..., ttl=...) 装饰 RequestPayload 模型。
- 装饰器返回原函数/模型；移除 CallbackOperation、RequestOperation 和 TransportCallback 类及其导出、测试分支。
- 统一文件名 name.lower().replace('.', '_').replace('@', '_') + '.py'；单模块及入口包根声明规则维持现状，不扩大本轮 API 清理范围。
- GET /api/v1/dev/integrations/registry 仅 local 且需登录；字段、排序、空表行为不变，来源仍为原声明。
- local_only 按发现作用域记录；内部导入错误不吞掉；请求仍惰性发现。
- 信封、ACK、身份、规范化、回显、保存点、回滚、TTL、可靠重试不变；Transport 的加锁→结算→冻结检查→应用结果顺序不变。
- 无数据库迁移；不提交、不推送、不部署、不发送真实 WMS/ECS 请求。

## Implementation Tasks

### Task 1: 移除旧声明及动态适配

- [x] 调用范围：三类注册表、callback operation、request service、dev request service/controller、registry controller、包导出、Transport 回调、所有相关夹具和文档。
- [x] Callback Registered 直接保存 func/data_model/title/echo_keys/source，复用原 parse/handle/echo 的实现。
- [x] Request Registered 合并原 RequestOperation 元数据、schema、example、describe，发送及 dev 调用直接读取注册项。
- [x] Transport 基类迁移为共享 apply_callback(ctx, apply)，两个回调迁移为带 data= 的函数。
- [x] 合并两个 WMS 注册器重复的名称/文件名校验，保留相同报错路标。

### Task 2: 测试与文档收敛

- [x] 保留注册格式、重复名称、文件名、来源、local-only、schema/JSON 示例验证等长期合同测试。
- [x] 移除测试中的旧类注册夹具和新旧双模式重复运行；保留函数 return/raise 四结果的 HTTP、回滚和并发回归。
- [x] 请求夹具全部直接注册模型，继续覆盖严格数据校验、冻结身份、幂等重入、TTL、外层事务回滚。
- [x] 更新指南、README、AGENTS、包说明，明确仓库外旧类调用须迁移。

### Task 3: 验证与复核

- [x] 改动前聚焦基线：365 passed, 85 skipped（未配置 PostgreSQL）。
- [x] 改动后含 Transport 的聚焦测试：632 passed, 269 skipped（未配置 PostgreSQL）。
- [x] 使用独立临时 PostgreSQL/Redis 运行完整 pytest：1840 passed, 7 skipped in 99.52s。7 个 skip 均为 SQLite 参数下的 PostgreSQL 专属锁/并发检查，对应 PostgreSQL 参数已通过。
- [x] Ruff check、format check、diff check 均通过；独立只读复核 Critical/Important/Minor 均为 0。
- [x] 清理本轮临时实例及匿名卷、记录最终验证和代码量；保持已有暂存区现状，不提交、不推送。

## Review Focus

- Typed payload 严格规范化、安全回显、None/dict/Outcome 转换及 return/raise 结果均保持原行为。
- Rejected 保存点回滚、普通异常整体回滚及 503、历史收据重放和并发同身份只执行一次。
- Transport 的未知任务、frozen 缺失、revision 判重/冲突、late callback 和任务行串行化。
- 请求同事务入队、TTL、冻结字节、恶意 ACK 分类和重试链均不改变。
- 清单来源/模型不丢失，匿名拒绝、生产隔离及空表行为不变。

## Boundaries

旧类注册属于本轮授权移除的开发接口；业务 wire 与持久状态不迁移。单模块和入口包初始化文件仍不受逐项文件名约束，请求模块仍惰性加载。仓库外调用需按指南迁移，不能继续导入已删除的类。

## Simplify Result

- 相对上一轮兼容实现，应用 Python 代码净减少 75 行，测试净减少 73 行；没有新增依赖或配置。
- 全仓 app/tests/scripts 已无旧类及嵌套 operation.Payload/ttl/example/title 调用；保留的测试覆盖长期协议与事务合同。
- 测试数量减少来自移除旧类兼容断言和新旧双模式重复参数；函数 return/raise、回滚、重放及并发断言仍保留。
- 暂存区在执行期间已有上一轮快照，本轮未操作 index；简化改动在工作区，提交前须按最终工作区重新审阅及暂存。

- 最后调整后再次使用独立 PostgreSQL/Redis 聚焦验证：215 passed, 1 skipped in 8.93s；skip 为 SQLite 参数下的 PostgreSQL unique-index waiting 检查，对应 PostgreSQL 参数已通过。
- 临时 PostgreSQL/Redis 及其匿名卷已清理；未连接运行库，未部署、未发送真实外部请求，未提交或推送。
