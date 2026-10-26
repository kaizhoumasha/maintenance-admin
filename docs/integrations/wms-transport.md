# Transport 接入参考

Transport 是 WES→WMS→RCS 的静态目标协议；ECS 按[独立参考](ecs-command.md)处理快照路由和结果，两者不混用。正式能力、适配器和回调已实现；业务结果消费接口仍按第一个消费方定形态，见 [TODO](../../TODOS.md)。技术接纳不代表业务/物理完成。

## 请求与身份


本接口是 WES → WMS，WMS 再对接 RCS，不按 `device_code` 查询 ECS。静态指部署期间固定，地址可以现场填写，不要求打包软件时就知道。合同允许部署配置指定提交路径；本案例使用 `/api/v1/wes/transport-requests`。

下面为 Transport 合同中的完整 `RACK_MOVE` 请求示例，**不执行，不重放历史身份**：

```json
{
  "operation_id": "01a13b00-cabd-7390-890a-0c093e7c07a1",
  "operation": "transport.task.submit@v1",
  "timestamp": 1791991270077,
  "data": {
    "transport_task_id": "transport-9af2751b-4d01-4203-88a7-649bbf69e810",
    "kind": "RACK_MOVE",
    "rack_id": "510004",
    "rcs_template_id": "CTU01",
    "source": {"kind": "RACK", "location_code": "510004"},
    "target": {"kind": "RACK_POSITION", "location_code": "KT16"},
    "target_face": "90"
  }
}
```

`operation_id` 由 submit 发起方生成并保存，格式为带标准连字符的小写 UUIDv7。`timestamp` 是 UTC Unix 毫秒整数。`transport_task_id` 标识业务搬运任务；`client_request_id` 是 WES 内部业务调用身份，不替代 submit `operation_id`，也不加入固定请求信封。技术恢复复用原身份和原始字节，不能重新生成时间或 JSON。

当前可靠层的示例映射是 `(target="wms-transport", operation="transport.task.submit@v1", operation_id=原 submit operation_id)`。它展示公开入队接口，不能沿用 DeviceCommand 的设备联合键。

## 配置与正式适配器

目标在 RELIABLE_TARGETS 中使用 protocol=wms-transport，目标名仅为标签；须匹配 `[a-z0-9][a-z0-9_.-]{0,63}`。base_url 不带尾部 `/`，提交路径保持大小写。适配器已经随 app 创建注册，不需要重复登记；默认空目标不发网，客户端在启动时装配，不在运行中修改注册表。

请求 DTO/UUIDv7/位置与成员校验在 TransportService 的 types.py/wire.py 中完成；正式 adapter.py 同步、无副作用，核对冻结身份并原样传递正文，不查库、不发网。新入队与发送前复用 validate_request，不复制 HTTP 规则。示例写法与通用配置见[可靠投递](reliable-delivery.md)。已有 TransportService.move_rack 等入口同事务登记任务与 Outbox，不需要另抄一份业务模型/适配器；实验只用于学习。

提交 SUBMIT_TTL 为 1 小时（暂定）；是否启用 max_not_received_attempts（如 3 次）、fixed_retry_interval_s 及对端最长去重/重发窗口仍需 WMS 合同确认。预算只数确认未送达，耗尽进 STALLED，不伪造业务拒绝；默认不设预算/固定间隔。

## ACK 与合同边界


分类器按 Transport 合同 §4.2 实现，不按 HTTP 状态码单独判断。下面的具体响应可以直接转成测试输入，都针对 上面的请求示例 那条带合法任务 ID 的请求，JSON 响应头为 `Content-Type: application/json`。空响应体按原始字节 `b""` 判断，不能用 `{}`、`null` 或空白替代。

| HTTP | 完整响应 body | 期望 Verdict |
|---|---|---|
| 202 | `{"operation_id":"01a13b00-cabd-7390-890a-0c093e7c07a1","code":"RECEIVED","timestamp":1791992148964,"data":{"transport_task_id":"transport-9af2751b-4d01-4203-88a7-649bbf69e810"}}` | `DELIVERED`，response 等于原 body 字节 |
| 200 | `{"operation_id":"01a13b00-cabd-7390-890a-0c093e7c07a1","code":"DUPLICATE","timestamp":1791992148964,"data":{"transport_task_id":"transport-9af2751b-4d01-4203-88a7-649bbf69e810"}}` | `DELIVERED`，response 等于原 body 字节 |
| 409 | `{"operation_id":"01a13b00-cabd-7390-890a-0c093e7c07a1","code":"CONFLICT","timestamp":1791992148964,"data":{"transport_task_id":"transport-9af2751b-4d01-4203-88a7-649bbf69e810"}}` | `PEER_TERMINAL` / `FailReason.CONFLICT` |
| 422 | `{"operation_id":"01a13b00-cabd-7390-890a-0c093e7c07a1","code":"REJECTED","timestamp":1791992148964,"data":{"transport_task_id":"transport-9af2751b-4d01-4203-88a7-649bbf69e810","reason_code":"INVALID_DATA"}}` | `PEER_TERMINAL` / `FailReason.REJECTED` |
| 503 | `{"operation_id":"01a13b00-cabd-7390-890a-0c093e7c07a1","code":"UNAVAILABLE","timestamp":1791992148964,"data":{"transport_task_id":"transport-9af2751b-4d01-4203-88a7-649bbf69e810"}}` | `NOT_RECEIVED`，无等待提示 |
| 400 | 空字节 `b""` | `PEER_TERMINAL` / `FailReason.REJECTED` |
| 413 | 空字节 `b""` | `PEER_TERMINAL` / `FailReason.REJECTED` |
| 503 | `<html/>` | `UNKNOWN` |
| 200 | `{"operation_id":"01a13b00-cabd-7390-890a-0c093e7c07a1","code":"RECEIVED","timestamp":1791992148964,"data":{"transport_task_id":"transport-9af2751b-4d01-4203-88a7-649bbf69e810"}}` | `UNKNOWN`，HTTP/code 组合不合法 |

实验中的 `ack_result` 会生成完整 body，参数化测试还覆盖错误消息 ID、错误/缺少任务 ID、未知 code、未知拒绝码、未定义 429/502、非法 UTF-8、重复 JSON key、非对象 body 和布尔时间戳。正式适配器的同类用例在 `tests/integrations/wms_transport/test_adapter.py`，另外覆盖时间戳 int64 边界、必填字段缺失、嵌套重复 key、400/413 非空体仍为未知，以及提交路径校验。

`422` 的 reason_code 只能使用合同列明的值，任务 ID 也只能在合同允许的分支省略。不要用子串或翻译匹配。submit 不定义 `429 / BUSY` 或等待提示。回调 `503` 可能已有收据/Evidence 提交，不能套用 submit 的明确未接纳语义。

## 仍需确认的合同差异



- 换面模板 `CK04` 由现场确认补入，合同 0.4.0 只列 `CTU01/CTU02/CTU03/F01`，合同文本尚未同步；WMS 若不接受会以 `422 REJECTED` 返回。
- 创建前不做可信位置/当前面校验，是有意偏离，由 WMS 在接纳时兜底（回调接收设计决定 14）。
- 适配器对所有 ACK（含 `422`）一律要求回显同一 `transport_task_id`，请求在冻结前已校验，因此没有实现「非法原请求的 `422` 可不带任务 ID」的分支；若 WMS 对非法请求返回不带任务 ID 的 `422`，当前会判为 `UNKNOWN` 并按退避重试到 TTL。合同是否要求该分支待确认：Transport 合同写合法请求的拒绝须回显任务 ID，而外发给 WMS 的接口说明书（`wes_backend/docs/integration/wes-wms-interface-development-specification.md` REQ-SUB-02）写拒绝响应不得携带 `transport_task_id`，两份文档互相矛盾，联调前必须与 WMS 确认；若 WMS 按后者实现，合法的 `422` 当前会被判为 `UNKNOWN`。
- 正式适配器忽略冗余字段中的浮点数，只要求已定义字段为整数，与离线实验一律拒绝浮点不同。
- 拒绝码以精确字面量集合匹配，含中英文拼接原文；集合有测试固定，变更须同步合同。

已有 `wes_backend` TransportTask/Worker 不叠加本模板发送器。这里不授权迁移。分类器抛异常时消息进入 `FAILED(LOCAL_ERROR)`，而对端此时可能已执行，这条既有策略未变，接入真实对端前须单独评审。寻址与恢复按本页及基础合同处理，不由适配器副作用绕过。

## 回调与业务重试


按 Transport 合同 §5，WMS 回调到 WES 的 `/api/v1/wms/events`，不是原 submit 路径，也不是 ECS 完成通知格式。接收端已实现：通用接收在 `app/integrations/wms_callback/`，Transport 的两个 operation 在 `app/integrations/wms_transport/callback/`，设计与决定记录见[回调接收设计](../superpowers/specs/2026-10-22-transport-callback-design.md)：

- `transport.task.member_position_changed@v1` 与 `transport.task.resulted@v1` 分别承载位置事实和最终结果；每条回调有 WMS 自己生成的 `operation_id`，不沿用 submit ID，技术重试保持该消息原身份和完整信封。
- `transport_task_id` 关联原任务；最终结果另以 `transport_task_id + outcome_revision` 登记版本（`transport_task.applied_outcome_revision`、`outcome_digests`）。同版本相同 `data` 判重复，不同 `data` 判冲突；不需要另造设备 `event_id`。
- 与合同的内部顺序不同、对端可见行为不变：不设证据表和后台应用，回调在请求事务内锁任务行并直接应用后再 ACK。未知任务、事实矛盾也只记收据诊断和告警日志，仍返回 `202`，不能套用 ECS 讨论样例的未知命令拒绝规则；应用逻辑对通过 DTO 校验的消息不抛异常。
- 低版本不回退事实；更高版本只在未确定结果上收敛，不改写已确定成功/失败。料箱成功结果先于位置事件到达时暂存在 `pending_outcome`，位置补齐后在同一事务内完成。BIN 回架等前置位置条件按业务合同验收，不能仅凭聚合结果推断完成。
- 任务收到任何回调即结束其提交消息的发送（`settle_submitted`）；对端长期不可用或回调缺失，靠 `transport.scan_stuck` 的卡住任务告警暴露，不做定时盲目重发。

技术重提与业务重试必须分开。确定 `REJECTED/CANCELLED` 后，原任务保持终态；只有原业务需求仍有效，负责该业务的模块才按合同以全新 `client_request_id` 创建新任务及 submit ID。这不是第二套技术发送调度，不能因原请求超时或配置改址就自动创建新动作。


完整请求能力见[Transport 出站设计](../superpowers/specs/2026-10-21-transport-outbound-design.md)，回调见[接收设计](../superpowers/specs/2026-10-22-transport-callback-design.md)。[离线实验](../reliable-transport-lab.md)与[开发测试](../testing/README.md)验证本方软件；真实 WMS 去重、合同矛盾、来源认证、时钟/容量和物理完成另行验收，不叠加 wes_backend 自带派发器。
