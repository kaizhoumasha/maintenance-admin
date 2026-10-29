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
2. 使用管理员入口配置粗分扫码工作线：business_code=coarse_sorting，将 INPUT_ARM 绑定到该线设备；设备角色和版本由已有资产配置确定。代码不会自动补建或改写绑定。
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
    "scan_time": 1793206798000,
    "reel_diameter": 15,
    "reel_thickness": 20,
    "result": "OK"
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
- 粗分 business 投影是该数据库内的正式扫码行，核对原始条码、工作线、绑定版本以及 pending=true / AWAITING_VALIDATION_STAGE。
- 同一事务读取收据和业务投影；PostgreSQL 使用 REPEATABLE READ、READ ONLY。两者均无记录返回 404，非法摘要返回 400；数据库故障不会伪装成未找到。
- 原样重投保留原业务 ID；新事件身份或不同工作线分别受理。重启使用同一数据库后仍可查到原记录；公共收据清理不删除扫码去重和待办。
- 未注册事件的 ACK 不会自动补业务，查询不重放事件或推进状态。

当前粗分只完成扫码事实与待办受理，不向 WMS 发送请求或创建 ECS 命令；result=NG 同样保存事实。正式业务登记、MOCK/内部验证与真实对端、设备动作及现场验收是不同事实。
