# 业务开发唯一用法收敛设计

日期：2026-10-28。用户已批准统一公开调用、命令声明和业务目录，并在当前功能分支直接实施。
本设计替代此前“新旧调用兼容并存、两种业务来源、手工命令声明回退”的开发方式；已过期的设计、计划及阶段 1–6 历史记录已移出仓库原样归档。

## 目标与取舍

开发者只学习一套业务目录、声明和发送方法。当前正式业务 catalog 为空，按架构迭代方式完成不兼容收敛，
不增加弃用周期、签名别名、legacy 开关或双轨回退。外部只用 MOCK，事务/锁/恢复仍用可丢弃 PostgreSQL 验证。

已选择直接迁移所有仓库内调用方并删除旧形式。仅在文档中推荐新方式不能消除选择成本；
引入统一 execute/DSL/通用业务 worker 会增加概念，均不采用。

## 1. 唯一公开调用

```python
await wms.send(session, RequestPayload(...), operation_id=saved_id)
await ecs.create(
    session,
    owner=owner,
    call_key=saved_key,
    device_code=device_code,
    timeout_ms=timeout_ms,
    command=CommandPayload(...),
)
```

WMS `send(self, session, payload: BaseModel, *, operation_id: str)` 只接受精确注册模型；删除 operation/data 公开参数、
重载分派、_MISSING 和旧用法错误提示。注册模型对应 operation 必须唯一，歧义/子类/非法实际字段仍拒绝。
调试 WMS JSON 经原注册模型校验后直接传模型，不先 dump 再回到字符串路径。

ECS `create(self, session, *, owner, call_key, device_code, timeout_ms, command: BaseModel)` 只接受声明模型；
删除 task_type/params 公开参数、混合参数判断及“缺声明则按旧路由发送”的分支。
原身份优先返回、异内容冲突、冻结字节/路由/期限、角色授权、Result 接收与 UNKNOWN/HELD 语义保持。

平台调试台保留现有动态 JSON 与权限流程。业务 SDK 和可信调试适配器各自解析/验证其模型后，进入同一个私有入队核心；
核心负责规范化身份、冻结、合同验证和 Outbox。私有核心不提供旧签名别名、legacy 模式或业务可选原始发送入口。
正式角色解析始终感知声明；调试策略由原 local 适配器负责，不能借此授予正式角色权限或新增 Sender。

## 2. 唯一命令声明

`@ecs_command(task_type, *, device_roles: tuple[str, ...], annex_id, annex_version, result, error, timeout_min_ms, timeout_max_ms)`
为唯一业务声明；即使单角色也使用 `device_roles=(ROLE,)`，不保留单数别名。
一个参数模型可绑定多个角色，元数据保存不可变 bindings tuple，编译按角色/动作展开并检测重复。
角色 tuple 非空、无重复，原模型和来源身份不复制、不动态改类、不靠继承获取声明。

模型要求严格类型，沿其原 extra 策略解释字段；`extra='ignore'`、RootModel 和多角色共享并非旧调用形式，
无需通过手工绑定回退。既有 actual-field 重验、别名/序列化、循环拒绝及原结果合同保留。
不能安全验证的 dataclass 上下文明确提示调整模型结构，不再提示另一套发送方法。

composition 只声明 `COMMANDS: tuple[type[BaseModel], ...]`；无命令时 `COMMANDS=()`。
catalog 内部调用 `compile_commands(*entry.COMMANDS)`，不再读取手工 COMMAND_BINDINGS/ANNEXES，亦不提供兼容回退。
共享模型可被多个 composition 引用；catalog 只激活与当前业务 DEFINITION 角色相交的绑定，且每个模型须至少匹配一个本业务角色。
声明中的全部角色须在显式 catalog 的定义中存在，未知角色硬失败；其他业务未列入 COMMANDS 的角色不会因此获得命令权限。
API/worker 和无配置 CLI 共用编译结果。底层 CommandBinding/DeviceAnnex 是平台编译/结果解析表示，不再从业务公共包导出，
框架负向测试及可信历史 Result 装配仍可使用内部表示；业务测试样例必须迁到声明形式。

历史附录保留原 Result 解析，不授予新发送权限；删除 Python 旧用法不重写数据库行、历史身份或 wire。

## 3. 唯一业务目录与发现

业务代码只放 `app/businesses/<business>/`，由 composition 的 ENTRY_MODULES/MODEL_MODULES 与显式 catalog 加载。
保留单文件/入口包的业务内组织方式和原 operation 文件名规则，不建立第二个发现树。
`app/integrations/` 仅承载平台协议。删除三类注册器对其子目录的 pkgutil 业务扫描和旧业务来源兼容校验。

平台内置入口使用固定可信清单：正式只有 Transport callback 包；local 才加载现有 ECS/WMS demo 和 Transport dev 场景。
业务入口仍由 catalog 声明；正式入口完整加载 → 交叉校验 → local → 资源/工厂顺序保持。
文件/来源校验明确区分可信平台内置模块与业务 catalog；任意 app.integrations.foo.callback/request/ecs_event 不再自动注册，
也不能直接 import 绕过业务来源约束。缺失声明模块仍硬失败，不吞模块内部错误。
该来源约束覆盖仓库内 `app.*` 的实际注册，不能挪到 controllers/services 等其他应用目录绕过。
非 app 测试模块仍可在隔离夹具中显式装配；它们不会被生产发现器自动导入，也不能冒充 catalog 的业务初始入口。

正式合同交叉校验还须反查三类注册表中的实际来源：`app.businesses.*` 的正式注册必须属于
当前 catalog 所属的正式 `ENTRY_MODULES` 模块或其包内子模块。间接 import 不能启用未列业务，
也不能启用同业务未声明的入口；相似业务名前缀不等于归属。固定平台入口和隔离非 app 夹具沿用原边界。
校验失败不保留已验证 catalog，API/worker 在 local/资源/工厂前失败，CLI 不输出部分成功目录。

### 校验后首次导入的归属边界（2026-10-28 增量）

正式校验后的业务工厂和业务方法仍可能首次导入模块；校验通过不是后续注册的豁免。
三类实际注册在写入 registry 前，须沿用同一 catalog 模块/点分包归属规则：
已列完整业务之外、同业务未声明入口及相似名称来源立即失败，不能先进入目录或到达入队接口再等待人工复验。
首次启动加载阶段仍由既有最终合同交叉校验完整核对；初始化组合模块时不得递归读取尚未完整的 composition。

本增量采用共享注册边界的即时检查，复用现有归属判断；正式校验成功后以一个独立阶段标记保持防护。
后续复验失败会清空可用目录缓存，但不能把注册防护退回首次校验前的开放阶段；不增加第二注册表或发送路径。
仅在工厂末尾补查覆盖不到方法懒导入；每次发送重跑全部合同又增加重复装配与调用负担，均不采用。
已列业务合法共享、固定平台/local 和隔离非 app 夹具保持；命令声明仍由 COMMANDS 编译，不把 ENTRY_MODULES
规则误套到命令模型。被拒绝的注册不得占用注册键；原身份、冻结记录、事务和历史 Result 语义不变。
新增冷进程回归覆盖工厂/方法首次导入，外部只用 MOCK；独立 PostgreSQL 验证及最终证据记录在增量计划与验收文档。
本增量实施验收阶段从当前 develop 新建功能分支，原三项修改及暂存状态保留，阶段结束时未提交推送或部署。
用户随后授权提交并推送本增量防护、回归及文档更新，仍排除原三项修改；本轮不合并、不部署，实际交付以 Git 记录为准。

原 HTTP 路径、operation/Event 名称、权限与目录可见性不改变。正式 catalog 保持空，不放入模拟业务。

## 4. 迁移与删除范围

迁移应用内调用、dev 控制台、共享测试夹具、单元/事务/HTTP/重启测试和两个 MOCK 入口。
测试保留可靠性行为断言；移除仅验证新旧兼容/过渡签名的用例，不用“旧参数被拒绝”替代长期行为测试。
开发指南、业务包/注册/ECS 文档、包 docstring、AGENTS.md 同步到唯一形式；已被完整替代的设计、计划和旧验收原样移出仓库归档，仓库引用转向现行文档。
原人工/粗分两份已暂存业务合同文档与 .gitignore 不动；其业务 JSON/wire 不因 SDK 收敛而改名。

## 5. 验收与边界

- WMS 精确模型发送、动态调试输入、事务回滚、同身份冲突、TTL/target 和原结果读取有公开行为证据。
- ECS 单/多角色声明、严格字段/extra 策略、错误角色、调试权限/原地址、原冻结期限、迟到/冲突 Result、重启恢复均保持。
- catalog 只接受 COMMANDS，三种业务入口只从 app.businesses 加载；平台固定入口、正式/local 隔离、API/worker/CLI 目录正确。
- 两种触发的 MOCK 闭环、并发受理、消费/下一意图原子、丢通知、40001 重试/耗尽与完整 Transport 前序回归。
- 全仓 pytest、Ruff/格式、活动文档链接/示例及旧形式残留扫描通过；临时资源清理，原暂存补丁保持。

实施与 MOCK/内部验收阶段未提交或推送。2026-10-28，用户授权将本轮单一 API 收敛、MOCK 验证和文档清理提交并推送当前功能分支；两份原有业务合同文档及 .gitignore 修改不纳入提交，保留其原工作区和暂存状态。
不合并或部署，不连接真实对端、不更改生产数据，不丢弃此前未提交的工作。
现存脏文件完整快照已保存；本轮必要的调用和文档迁移在其当前内容上叠加，不能恢复旧快照覆盖并行更新。

实施与 MOCK/内部验收已完成，实际结果及限制见[本轮验收记录](../../integrations/business-development-single-api-acceptance.md)。
