# 可靠投递接入参考

先运行[第一个可靠投递实验](../reliable-transport-lab.md)。本页说明现有配置和事务边界；Transport 的 wire、ACK 和回调见 [Transport 参考](wms-transport.md)，ECS 的快照路由与 UNKNOWN/HELD 规则见 [ECS 参考](ecs-command.md)。下述静态目标规则不能覆盖 ECS 的恢复策略。

## 配置与事务内入队

完整默认值见 `.env.example`。`RELIABLE_TARGETS` 的 JSON 形状如下，示例目标不会被默认启用：

```json
{"defaults":{"retry_base_s":1,"retry_cap_s":300},"targets":{"peer":{"base_url":"https://example.test","protocol":"my-protocol","max_retry_after_s":1200}}}
```

环境默认 `RELIABLE_MAX_RETRY_AFTER_S=3600`，JSON defaults 覆盖环境默认，target 再覆盖 defaults。STATIC 退避为 `max(local_jitter, min(peer_hint_ms / 1000, max_retry_after_s))`，本地 cap 默认 300 秒；只有提示超过采纳上限才截断告警，等待不延长原 TTL。

协议适配器的 `build_request` / `classify_response` 同步且无副作用。启动前向 `app.state.reliable_adapters` 追加新协议，保留已有注册；不在运行中变更。`build_request` 返回 `HttpRequest`，正文必须等于冻结字节，稳定身份必须实际出现在对端可去重的位置；`classify_response` 只接收完整响应。DELIVERED 的 response 必须是实际响应原字节，未知网关响应及身份不匹配不能按 HTTP 状态码猜结果。

启用目标后 lifespan 提供 `app.state.outbox`；空配置时为 None，但有预算的专用数据库池仍执行后台检查，不创建空 HTTP 客户端。调用方持有自己的事务：

```python
from app.reliable import MessageDraft

async with session.begin():
    reference = await app.state.outbox.enqueue(
        session,
        MessageDraft(
            target="peer", operation="opaque", operation_id=stable_id, request_raw=frozen_bytes
        ),
    )
```

reference 只表示入队引用/状态，不是异步业务结果。Celery/脚本可用同样的配置与适配器构造 Outbox，不创建 HTTP 客户端；已有 Celery 发送任务不因引入 Outbox 自动迁移。业务和入队一起提交/回滚，插入竞争不破坏外层事务；存续身份先比冻结字节，同字节返回原引用、异字节冲突，只对新消息做 HTTP 校验。

## 保留的投递和恢复边界

- 单逻辑消息只有可靠发送器负责技术重试；HttpClient 只做单次请求，不叠加其他发送器或调度器。
- STATIC 使用当前目标配置，目标地址/协议迁到新的去重库前先排空存量消息；目标改名会使旧名消息保持 PENDING。ECS 按创建时冻结快照寻址，旧命令不可改投。
- 正常运行中按检查间隔扫描 TTL 和回收租约，默认间隔 1 秒；发送完成唤醒补位，间隔未到时只领取。到期 PENDING 与过期 SENDING 各有独立批量上限，默认各 100，槽位满或空目标也检查。
- TTL 从入队创建起算，重试/提示/重复入队不重置；到期不发新请求，不撤销在途动作。未知租约恢复也不证明对端未执行；ECS 的未知或中断租约进入 HELD，不自动重放。
- Sender 停机共用一个 monotonic deadline，默认总预算 15 秒；各阶段分配剩余预算，仅释放已确认停止且仍属当前租约的任务，未确认者留待租约恢复，不强行解锁。这不是整个进程/API/Redis 的退出 SLA。
- 数据库行进入消息/Claim 时统一 UTC-aware；只为本层已知 UTC 写入的 SQLite naive 读值补 UTC，不接受外部未知 naive 时钟。主机仍需时钟同步，时区统一不校正偏差；实例偏差及调度/回写延迟须远小于默认 5 秒租约余量。
- DONE/PEER_TERMINAL 的终态原文只在 response_raw 保存，last_response_raw 清空；其余尝试保留实际取得的有界诊断响应，分类失败不伪造终态正文。
- 仅 DONE/FAILED/EXPIRED 按进入终态后不再刷新的 updated_at 保留，默认 90 天；清理后同身份可成为新投递，没有跨清理本地幂等。可靠层表不能代替业务任务记录或公开结果消费接口。
- 消息原文明文存储。对端去重、访问控制、出口限流/熔断、公平调度和生产容量按启用范围另验；执行并发预算不等于出口限流。非空表及存在快照/证据时的迁移降级守卫不可绕过。

固定阶段异常诊断见[日志说明](../logging.md)，验证入口见[开发测试](../testing/README.md)。完整基础合同见 [SPEC-019B](../superpowers/specs/2026-10-20-spec-019b-reliable-interaction.md)；协议送达、业务完成和物理完成分别判断。
