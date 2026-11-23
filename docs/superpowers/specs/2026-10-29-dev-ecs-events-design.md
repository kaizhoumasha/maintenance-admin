# 通用 ECS Event 调试与粗分本地验收设计

> 当前业务范围见[验证与首个 MOVE_FORWARD / Result 增量](2026-10-29-coarse-sorting-move-forward-design.md)和 [ECS Event 操作指南](../../integrations/ecs-event-debugging.md)。下文全部为当时阶段设计及验收记录（含当时合同示例），其中“当前”指该历史时点；现行合同以上述增量为准。

## 当前决定

用户确认撤销空生产 catalog 约束。粗分扫码使用唯一正式 `ecs_event/scan_completed.py`、所属 definition/composition 和正式扫码表；API/worker/迁移共享同一 catalog。各环境装配相同，验证数据库、设备/工作线、MOCK 对端由已有部署配置与测试准备，启动不自动生成测试数据。

`SCAN_COMPLETED.data` 只包含 ECS 可提供且受理需要的事实：barcode、scan_time、reel_diameter、reel_thickness、result。移除 box_barcodes。未知扩展字段沿用接收器身份合同，不进入业务模型。

Swagger 通用事件调试沿用既有路由、登录和原始接收流程；直接显示正式注册事件。查询在配置数据库内读取收据及正式粗分投影。保留项目既有 dev 控制台挂载策略，不为粗分处理器增加环境限制。

删除此前专用 local 处理器、私有注册收集/上下文、设备前缀分流、额外 SQLite 生命周期/目录配置及 scope 分支。公共回调与调试投递使用同一处理器、会话工厂和业务运行时。

补齐正式模型及单一 Alembic 迁移，保存原始事实、绑定快照与持久去重/待办，不增加 WMS/ECS 出站或业务推进。验收使用可丢弃数据库与 MOCK；正式登记不代表后续阶段或现场完成。

## 本轮实施顺序

1. 公开合同与主应用正式装配失败回归。
2. 正式 definition/composition/model/迁移，删除多余 local 分支，同步测试。
3. 更新指南及业务草案，按 MOCK/内部验证运行相关和全量回归、独立复评；后续提交按用户明确授权执行，不推送或部署。

## 此前设计（历史，已撤销）


### 授权与目标

2026-10-29 用户授权将粗分隔离装配接入主应用 local 模式，无需另启验证应用。继续在当前功能分支实施，不提交、不推送、不部署，保留已有修改和暂存状态；此前扫码受理提交 a101ea7 保持不变。

开发人员在 Swagger 选择 ECS Event、修改样例并投递，查看真实接收结果，再按事件摘要查询已持久化的收据与可选业务投影。粗分验证使用 local `SCAN_COMPLETED` 薄入口和实际受理服务，不接真实设备或 WMS/ECS。

### 此前主应用 local 集成方案（已撤销）

普通 `app.main:app` 在 ENVIRONMENT=local 时自动装配粗分隔离运行时，原 Swagger 下拉直接显示 INPUT_ARM / SCAN_COMPLETED；沿用主应用登录，不再要求第二个进程、端口或验证令牌。生产 catalog、正式资源目录和正式业务运行时不改变。

粗分 local 入口声明放在 `app/businesses/coarse_sorting/dev_ecs_event.py`，标记 local_only，仅转交同一 CoarseSortingService 和 ScanCompletedData。真实正式入口及其 catalog 所有权防护保留，开发入口不冒充正式注册。业务声明由 local 组合固定 ENTRY_MODULES 导入并验证来源/角色/初始事件；不扫描目录、不改函数来源、不使用 monkeypatch 修改 catalog。

local 声明通过隔离的注册收集作用域加载，结果不可变，正式全局事件注册表不变。Swagger 与开发注册目录合并显示两组实际声明；重复角色/事件键拒绝合并。正式回调继续使用原全局注册表，不能执行隔离处理器。

开发投递仅把保留命名空间 LOCAL_INPUT_ARM 的设备事件交给隔离上下文；其他开发事件沿用主应用原上下文。隔离上下文携带自己的会话工厂、绑定解析器、业务运行时与事件映射，共享原接收函数和原事务流程。原始请求只读取一次，路由判断不修改 data、摘要或重报顺序。投递结果及查询增加 scope（application/local_isolated）明确说明数据范围；scope 不进入设备协议或身份。

### 通用调试 API

仅 `ENVIRONMENT=local` 挂载，要求既有 `get_current_user` 登录。普通应用接收原公共接口 `POST /api/v1/callback/event` 的合同不变。

- `POST /api/v1/dev/ecs/events`：请求仍为 `device_code/event_type/timestamp/data`；保留原始字节与全部 data，不先使用业务模型过滤或校验，避免改变完整摘要、重复请求先查收据的顺序。Swagger Examples 从现有 `(device_role,event_type)` 注册表生成，标签带角色；数据取实际 Payload 的首个 JSON Schema examples，缺少样例时显示需填写 data。样例设备码为 `LOCAL_<role>`，仅在对应隔离装配中有绑定；时间戳为可编辑的样例值。
- POST 复用原 HTTP 接收处理，连同大小限制、事务、失败回滚、快照及日志；调试请求完成一次接收尝试后 HTTP 为 200，响应包含 `scope/digest/status_code/outcome/response/diagnostic`，其中 status_code 保留接收器原始的 200/400/413/503。登录、限流等调试入口错误仍使用项目统一 HTTP 错误及 ErrorResponse。原 ACK 放在 response 内，不把已执行处理器等同业务受理或物理完成。
- `GET /api/v1/dev/ecs/events/{digest}`：摘要必须为 64 位小写十六进制；返回 `scope/digest/receipt/business`。receipt 只表示对应上下文收据已持久化，含原状态码、响应、诊断与创建时间，不推断原处理器执行结果。不存在收据且无业务投影时为 404；收据已清理但业务投影存在时仍可查到业务记录。
- 通用查询只读取平台收据。可由可信本地装配注入一个只读业务投影回调，平台不识别业务表或推进业务。查询用独立事务；PostgreSQL 在首条 SQL 设置 REPEATABLE READ、READ ONLY，保证收据和业务投影的一致视图。投递响应不追加提交后数据库查询，避免查询失败掩盖已经提交的投递结果。
- 事件只由已有发现/注册链路加载；不扫描源码目录，不动态启用未登记事件。设备角色和工作线始终由可信绑定解析。

投递示例（在主应用 local 隔离装配中有效）：

```json
{"device_code":"LOCAL_INPUT_ARM","event_type":"SCAN_COMPLETED","timestamp":1793206799000,"data":{"barcode":"TEST-REEL-001","scan_time":1793206798000,"reel_diameter":15,"reel_thickness":20,"result":"OK"}}
```

新事件修改 timestamp；验证幂等重报时保持整个请求不变。缺失箱码与显式 null、额外 data 字段及原事件身份均沿用既有冻结合同。

### 粗分隔离资源与生命周期

- main 的 local 分支在挂载开发路由前准备声明元数据，在应用 lifespan 内启动/关闭专用 SQLite；注册准备不连接数据库。主应用仍按既有方式运行，不替换 DATABASE_URL、Redis、用户、设备或 Sender 配置。
- 可选 DEV_ECS_DATA_DIR 指定专用目录；省略时使用用户数据目录下按仓库路径区分的稳定位置，无需额外配置。目录标记及 events.sqlite3 沿用既有隔离格式，重启保留扫码与绑定。非空无有效标记或数据符号链接拒绝启动，不覆盖已有文件。
- 只创建接收器/测试设备所需平台表和独立 MetaData 扫码表，不建立生产业务表或迁移；初始化 LOCAL_INPUT_ARM、LOCAL_INPUT_ARM_B 的两条测试绑定，仅补建缺失项。
- 隔离运行时没有 Sender、外部客户端或出站表；主应用原 Sender 无法读取这份 SQLite。只使用真实受理服务，待办仍停在 AWAITING_VALIDATION_STAGE。
- 摘要查询先查隔离存储，未命中再查主应用；隔离存储错误不能伪装成未找到。隔离收据清理后 business 投影仍可返回。返回 scope 防止把同一摘要在不同存储中的事实混在一起。
- 旧 tests.manual 独立应用和专用令牌用法移除；其目录保护、重启、清理后去重和无外发断言迁入主应用 local 集成测试，原测试业务装配仍独立。

### 验收与边界

1. 普通 local 主应用直接有粗分事件下拉；生产环境不导入/注册/创建粗分隔离资源，且没有 dev 路由。下拉覆盖实际事件注册，不依赖第二份维护清单。
2. 通用调试与公共回调使用相同摘要、原事务、诊断和重报语义。无效输入、未知绑定、回滚及请求大小限制均有 HTTP 回归。
3. 摘要查询只读，未知摘要 404、非法摘要 400；收据与业务投影在同一一致视图中读取。
4. 主应用 local 模式在真实 HTTP/Swagger 下保存隔离扫码待办；原样重投不新增，修改事件事实形成新记录，重启仍可查；不产生外发。
5. 现有用户文件、生产 catalog、模型导出和 Alembic head 保持原状。未做真实设备、真实 WMS/ECS、供应商或现场验收；待办仍停在 AWAITING_VALIDATION_STAGE。
