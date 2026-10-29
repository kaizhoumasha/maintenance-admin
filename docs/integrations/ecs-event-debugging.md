# ECS Event 的 Swagger 调试

## 使用同一套业务装配

粗分扫码通过正式 catalog 的 definition/composition 加载唯一 `ecs_event/scan_completed.py`，在各环境使用同一处理器、配置数据库及业务表。主应用启动不创建测试设备/工作线，也不另建 SQLite 或发送器。验证所用数据库、设备绑定和 MOCK 对端由测试或部署配置准备。

项目既有 Swagger 和 dev 控制台在 `ENVIRONMENT=local` 时挂载；这个展示策略不限制正式扫码处理器。其他环境可通过公共 `POST /api/v1/callback/event` 验证同一业务，验证资源和对端按其部署配置选择。

| 方法与路径 | 用途 |
| --- | --- |
| `GET /api/v1/dev/integrations/registry` | 查看实际注册事件、角色、来源和 Payload Schema |
| `POST /api/v1/dev/ecs/events` | Examples 下拉选择事件、修改请求并投递 |
| `GET /api/v1/dev/ecs/events/{digest}` | 查看持久收据与业务投影 |

下拉来自实际事件注册表及 Payload JSON Schema examples，不扫描目录或维护第二份事件名单。粗分项为 **INPUT_ARM / SCAN_COMPLETED**，来源 `app.businesses.coarse_sorting.ecs_event.scan_completed`、local_only=false。示例设备码仅是可编辑样例，实际角色和工作线由配置数据库的可信绑定决定。

## 操作步骤

1. 对选定的验证数据库执行已有迁移流程至 head，正常启动主应用 `app.main:app`。
2. 使用管理员入口配置粗分扫码工作线：business_code=coarse_sorting，将 INPUT_ARM、CONVEYOR 绑定到各自实际设备，并配置唯一位置槽 OUTPUT_RACK（出料单层货架位）。例如 RS01→CLN01、RS02→CLN02；不配置输送线入口、出口或面向。设备角色和版本由已有资产配置确定，代码不会自动补建或改写绑定。
3. 正常登录，在 `/docs` 的 **Authorize** 填入 access_token，不加 Bearer 前缀。
4. 展开 **dev-ecs-event → POST /api/v1/dev/ecs/events → Try it out**，选择 **INPUT_ARM / SCAN_COMPLETED**。
5. 将 device_code 改为已配置的设备码，修改条码和 timestamp 后 Execute。新事件用新 timestamp，验证重报则保留整份请求。

请求示例：

```json
{
  "device_code": "INPUT_ARM-01",
  "event_type": "SCAN_COMPLETED",
  "timestamp": 1793206799000,
  "data": {
    "barcode": "TEST-REEL-001",
    "reel_diameter": 15,
    "reel_thickness": 20
  }
}
```

## 结果与摘要查询

调试与公共回调使用同一受理函数，保留原始 body、大小限制、完整 data 摘要、原收据优先查重和接收事务。不要先过滤业务字段再计算身份。

POST 完成一次接收尝试后 HTTP 为 200；原接收器状态在 status_code 中，不能仅据调试 HTTP 200 判断成功。原协议 400/413/503 也保留在 status_code；登录、限流等入口错误继续使用标准 HTTP 错误。

| 字段 | 含义 |
| --- | --- |
| `digest` | 实际事件身份，无效输入或事务错误可能为空 |
| `status_code` / `response` | 原接收状态与协议响应，例如 200 / ACK、400 或 503 |
| `outcome` | received、duplicate、unregistered、invalid、error 等，不代表物理完成 |
| `diagnostic` | 当前或原收据诊断，包括未注册事件、业务配置不匹配 |

将 digest 复制到 GET 查询：

- receipt 是配置数据库内已持久化的原响应及诊断。
- 粗分 business 投影是该数据库内的正式扫码行，核对原始条码、工作线、绑定版本、原验证/命令引用和 wait_reason。初始为 pending=true / AWAITING_VALIDATION_STAGE。
- 同一事务读取收据和业务投影；PostgreSQL 使用 REPEATABLE READ、READ ONLY。两者均无记录返回 404，非法摘要返回 400；数据库故障不会伪装成未找到。
- 原样重投保留原业务 ID；新事件身份或不同工作线分别受理。重启使用同一数据库后仍可查到原记录；公共收据清理不删除扫码去重和待办。
- 未注册事件的 ACK 不会自动补业务，查询不重放事件或推进状态。

当前粗分已实现扫码、验证和首个 MOVE_FORWARD / Result 消费；分配、入箱和 WMS 业务 NG 搬运仍待后续阶段。正式业务登记、MOCK/内部验证与真实对端、设备动作及现场验收是不同事实。

## 单独调试命令样例

如需单独检查命令协议，在 Swagger 展开 **dev-ecs-command → POST /api/v1/dev/ecs/commands → Try it out**，从 **Examples** 选择 **CONVEYOR / MOVE_FORWARD**。下拉取启动时 catalog 已编译且实际生效的角色/动作，`params` 来自原模型的 JSON Schema examples；不扫描业务目录，也不修改 `task_type` 为枚举。

```json
{
  "client_request_id": "demo-CONVEYOR-MOVE_FORWARD-1",
  "ecs_url": "http://127.0.0.1:8000",
  "device_code": "CONVEYOR-01",
  "task_type": "MOVE_FORWARD",
  "timeout_ms": 30000,
  "params": {"barcode": "RAW-001"}
}
```

设备码、地址和请求标识为占位；修改为支持该动作的模拟 ECS 及测试设备，新调用换请求标识，重报同次调用保留原输入。平台 **MOVE** 演示仍可选，内置 Mock 仅支持 `D1/MOVE` ACK；`MOVE_FORWARD` 需另有支持它的模拟 ECS。没有模型对象样例的动作仍可选，按提示到注册目录查看参数 schema 后填写 `params`。

这个入口需要登录和 `dev:ecs:send_real` 新登记权限，沿用通用 v3 调试合同；选择样例不登记粗分业务动作、不推进扫码行，也不改用正式严格 Result 合同。粗分完整流程仍按下面的扫码 → WMS 验证 → 正式命令 → 原结果消费验证。登记、ACK 与结果含义及查询步骤见 [ECS 最小业务接入教程](ecs-command-tutorial.md#2-无设备表直接用swagger调试)。


## 验证与首个输送动作

配置已有 RELIABLE_TARGETS 中唯一的 wms-decisions 目标，以及 CONVEYOR 设备的 ECS 路由。隔离验证须使用可丢弃数据库和明确的回环 MOCK 地址。正常运行主应用（唯一 Sender）、Celery worker 和唯一 Beat；具体任务 `coarse_sorting.advance_pending` 每 5 秒按 ID 分页处理待办，每页 100 条，阻塞的前页不会饿死后续扫码。任务自己的会话工厂逐条开启新的 PostgreSQL REPEATABLE READ 事务，按工作线、设备 ID、扫码动作加锁；40001 最多尝试 3 次，失败回滚并保留原待办。

扫码进入原验证，发送到 WMS `POST /api/v1/wes/decisions`，例如：

```json
{
  "operation_id": "019c0000-0000-7000-8000-000000000001",
  "operation": "inbound.coarse_sorting.validate_bind@v1",
  "timestamp": 1793206800000,
  "data": {
    "workline_code": "COARSE-A", "barcode": "TEST-REEL-001",
    "reel_diameter": 15, "reel_thickness": 20
  }
}
```

WMS 请求包络 timestamp 为本次请求登记时间，随原请求冻结；原扫码事件的外层 timestamp 保存在扫码事实中。

唯一允许继续的响应为 HTTP 200 / DECIDED 和合法业务 OK：

```json
{
  "operation_id": "019c0000-0000-7000-8000-000000000001",
  "code": "DECIDED", "timestamp": 1793206800100,
  "data": {"result": "OK", "binding_id": "BIND-001"}
}
```

原请求身份和响应会持久保存；业务 NG 的 data 为 `{"result":"NG","reason_code":"BARCODE_INVALID"}`，保存后停在 AWAITING_NG_STAGE。协议拒绝、未知、原消息已清理或非法响应保留原请求引用等待核对，不换号请求、不伪造 NG。

合法 OK 注册唯一 CONVEYOR 命令，向该设备冻结路由的 `POST /api/v1/device/command` 发送：

```json
{
  "device_code": "CONVEYOR-01",
  "command_code": "019c0000-0000-7000-8000-000000000002",
  "task_type": "MOVE_FORWARD", "priority": 1, "timeout": 30000,
  "timestamp": 1793206800200, "params": {"barcode": "TEST-REEL-001"}
}
```

ECS HTTP 200 `{"code":200,"message":"ACK"}` 只确认接收。原设备通过公共 `POST /api/v1/callback/result` 报告：

```json
{
  "device_code": "CONVEYOR-01",
  "command_code": "019c0000-0000-7000-8000-000000000002",
  "result": "SUCCESS", "finish_time": 1793206800500,
  "data": {"actual_qty": 1},
  "error_detail": null
}
```

接收器按原 command_code + device_code 关联结果；任务消费原已应用 SUCCESS、严格整数 actual_qty=1 且无待核对项的结果，保存 move_result 和 binding_id，进入 AWAITING_ALLOCATION_STAGE（pending=true 表示后续业务义务仍在）。MOVE_FORWARD 的 Result.data 仅包含 actual_qty；执行位置由 ECS 按设备和动作管理。新正式调用使用内部附录 coarse_sorting.move_forward/2，通用调试合同仍为 v3。

本轮不请求箱格、不执行 PUT_TO_BIN 或 MOVE_TO_NG。FAILED 可使用空 data 和严格 error_detail；SUCCESS 缺少数量仍只保存原证据，停在 MOVE_RECONCILIATION_REQUIRED，不能当作完成或另发命令。错误数量、错误字段类型或错误原命令/设备身份在原 Result 接收边界拒绝。

以上超时和 MOCK 响应是内部开发基线。供应商合同确认、部署与现场物理验收分别记录。
