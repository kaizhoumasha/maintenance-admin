# ECS Command Swagger 声明样例设计

## 已确认目标

用户授权按已讨论建议，为 POST /api/v1/dev/ecs/commands 提供与 Event 相同的 Swagger Examples 下拉及完整请求样例。继续当前功能分支，保留原修改和 index；本轮不提交、不推送、不部署，只做 MOCK/内部验证。

## 方案与边界

采用声明驱动的 OpenAPI examples，沿用既有 DevCommandIn 六字段、通用协议合同、URL/权限检查、调用身份、投递和结果查询。task_type 保持自由字符串；不增加模式、选择器字段、API、注册表、配置项或 Sender。单改 task_type 为枚举会限制现有通用调试，且无法补全参数；控制器硬编码业务动作会产生第二份名单，因此不用。

主应用在现有 catalog 合同校验后，将已编译且按归属生效的 command_bindings 传给开发路由装配。控制器不扫描目录、不重新编译或扩展角色，不读取业务数据库。改为 ecs_command_router(command_bindings) 工厂，在挂载时生成样例；当前 GET/POST 的地址、名称、请求/响应及授权逻辑保持。

- 平台原通用 MOVE 演示保留，直接复用 DevCommandIn 的既有模型示例作为唯一来源。
- 每个有效角色/动作生成一个稳定键和展示名，例如 CONVEYOR:MOVE_FORWARD / CONVEYOR / MOVE_FORWARD；来源为编译绑定的 source 原命令模型。
- 参数从模型 JSON Schema 的 examples 取首个对象样例。MoveForward 增加 barcode 样例，仍为原严格模型。没有可用对象样例时，保留选项并给 params={}、明确提示按注册目录参数 schema 填写，不编造字段；不收紧正式声明规则或调试参数校验。
- 完整 value 包含 client_request_id、ecs_url、device_code、task_type、timeout_ms、params。声明示例用角色占位设备码，目标地址为可修改的 MOCK 占位地址；请求标识按角色/动作区分，避免切换选项意外复用同一身份。超时以现有30000ms为样例值并约束在该声明范围内。
- 样例为展示内容，不决定正式设备绑定或改用严格附录；发送仍使用原通用合同。内置 Mock 仍只支持 D1/MOVE ACK，其他动作需配置支持对应任务的 MOCK ECS。

## 验证

通过真实 OpenAPI 和公开 HTTP 断言：平台演示与实际 MOVE_FORWARD 选项、有效角色交集、不同角色的同名动作、无样例提示、窄超时范围、完整 JSON 请求及任意未登记动作仍可发送。选择声明样例后沿用原通用v3快照，稳定身份重入、异参冲突、权限、结果查询和通用 Result 语义保持。

补齐当前 ECS 参考、教程和粗分操作说明；历史验收不改。使用当前分支代码运行相关及全量回归、Ruff、冷合同检查，并确认 index/HEAD/stash 保持。普通 Swagger 仅做展示检查，不向用户现有对端投递命令。
