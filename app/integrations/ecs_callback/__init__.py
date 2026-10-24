"""接收 ECS 设备事件回调(ECS → 本项目),入口是 `POST /api/v1/callback/event`。

wire 按白皮书不变:请求体是 `device_code`、`event_type`、`timestamp` 加可选的 `is_debug`、`data`,
成功回 `{"code":200,"message":"ACK"}`。这个包只认识信封和 `event_type`,不认识任何具体事件;
具体事件写在 `app/integrations/<业务>/ecs_event/` 里,启动时自动发现。
设计见 docs/superpowers/specs/2026-10-24-ecs-event-callback-design.md。

## 它负责什么

- 路由 `controllers/ecs_callback.py`:最多读 256 KiB,超限回 413。每个请求在一个数据库事务里
  调用 `receive_ecs_event`,提交后才输出最终请求日志并后台写诊断快照。
- `receive_ecs_event`(receiver.py)按顺序:
  - 校验信封,失败回 400 `INVALID_ENVELOPE` 加 `error_detail.issues[]`(只含路径、码、期望类型,
    不回显值)。不写收据。
  - 身份是核心字段 `device_code`、`event_type`、`timestamp`、`is_debug`(缺省 false)、
    `data`(缺省 `{}`)的规范化摘要,其他顶层字段不进身份。摘要全长 64 位十六进制,
    作为 `callback_receipt` 的 `operation_id`,`operation` 恒为 `"ecs.event"`。
  - 先按身份查重,再校验 `data`:已收过的事件原样重报直接回首次的 200 ACK,
    处理器后来收紧校验也不会让它变成 400。
  - 新身份才查处理器并校验 `data`(失败回 400,路径为 `data.<字段>`)。通过后写收据、
    在同一事务里执行处理器、ACK。
  - 处理器抛普通异常,或 PostgreSQL 行锁等待超过 5 秒:整个事务回滚,回 503,ECS 应原样重报。
- 未注册的 `event_type` 回 200 ACK:ACK 表示持久接收,所以事件完整存入收据诊断
  (规范化后至多 32 KiB;更大时只存摘要、大小和前 1 KiB 预览,并输出 error 日志)。
  告警日志里不含 `data`。
- 收据按 `RELIABLE_RETENTION_DAYS`(90 天)每天清理。超期后同一身份再来会被当作新消息。

## 它不负责什么

来源认证(白皮书不规定,入口只靠网关限制来源)、业务规则、事件顺序、重试(由 ECS 重报)。
还有三点是有意不做的:

- 不内置 `ESTOP_PRESSED` 回 400 的规则(白皮书 §4.2,条款待供应商确认)。出现对应处理器之前,
  它会按未注册类型 ACK 并落库。
- 不回放未注册的事件。它们只留在收据诊断里。
- 同毫秒、同内容的两个真实事件与重报无法区分,第二个会被当作重复(白皮书已登记为 UNKNOWN)。

## 怎么新增一个事件类型

在 `app/integrations/<业务>/ecs_event/` 目录里加一个文件,不用改控制器和 `main.py`。
一个事件类型一个文件,文件名是小写的 `event_type`(`SCAN_COMPLETED` 对应 `scan_completed.py`),
有测试检查两者一致。事件类型很少时也可以用单个 `ecs_event.py`。`event_type` 必须匹配
`^[A-Z][A-Z0-9_]*$`,否则导入期报错。`dev_ecs_event` 只在 `ENVIRONMENT=local` 导入,放联调样例。

    class ScanCompletedData(CallbackPayload):   # 严格 JSON 类型,冗余字段忽略
        barcode: str

    @ecs_event("SCAN_COMPLETED", data=ScanCompletedData)
    async def scan_completed(ctx: EcsEventContext, data: ScanCompletedData) -> None:
        ...                                     # 在请求事务内写业务;返回 None 即 ACK

`data=` 可省略,此时不校验 `data`,处理器收到原始 dict(缺省 `{}`)。
注册表是模块级全局状态,同名重复注册会报错并指出两个函数。测试里用 `monkeypatch` 换成空 dict。

## 处理器契约

- 只写当前数据库事务。不得在处理器里直接调用设备、HTTP 或其他外部服务;外发动作只能用
  同事务的 `Outbox.enqueue` 登记,提交后由发送器执行。不要自己 commit 或 rollback。
- 对已通过校验的消息不得抛异常。冲突、未知设备这类业务情况用 `ctx.note(reason_code, ...)`
  记录并返回(仍 ACK)。抛异常会让这条消息永远回 503,变成越不过去的毒消息。
  `ctx.note` 随收据同事务保存,提交后输出告警日志(字符串值在日志副本里转义并截断);
  处理器的 note 不限大小,自己控制体量。
- 自带持久的幂等键。收据 90 天后清理,之后同一事件重报会被当作新事件,
  处理器必须靠自己的业务行保证重入安全,不能依赖收据。

## 发布顺序

新增事件类型时,先把处理器部署到所有接收实例,再让 ECS 侧启用该事件类型。否则尚未升级的实例
会把它按未注册 ACK 并占住身份,升级后重报只会得到重复,业务效果补不上。
"""

from .registry import EcsEventContext, ecs_event

__all__ = ["EcsEventContext", "ecs_event"]
