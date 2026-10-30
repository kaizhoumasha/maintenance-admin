# 业务受理、推进与恢复配方

从[快速开始](business-development-quickstart.md)进入本页；接口字段以[出站结果参考](outbound-results.md)、
[ECS 命令合同](ecs-command.md)为准。本页不定义新协议或平台 API。
`accept/advance/scan_pending` 均是具体业务方法名；可执行参照是
[隔离测试业务 FlowService](../../tests/helpers.py)和下方真实 PostgreSQL 用例，不能复制到生产清单。
正式 `catalog.entries()` 已登记获准的粗分扫码、验证、首个 MOVE_FORWARD / Result 阶段；其他业务须按各自实现范围登记。

## 先保存哪些依据

在具体业务表中保存原 execution/action/cycle 身份、工作线/设备 ID 和角色、当前经过与资源范围，
关联原 receipt_id、WMS operation/operation_id/message_id、ECS owner/call_key/command_code、
Transport task_id/client_request_id/caller。完整直接前序及各自 caller/cycle/资源来自业务计划，
不能用“当前同设备全部历史任务”代替。保存 pending、等待/核对原因、已消费版本/成员和必要业务决定。
消息去重身份与业务经过身份分别保存：重报回到原执行；有效的新经过不能被旧历史永久阻塞。

原规范 JSON、请求字节、路由、附录身份/版本和期限由原框架冻结，不由恢复代码重建。
新 ECS 命令使用 `PROTOCOL_RETRY_WINDOW=300 秒`，旧命令保留原期限；不改协议、错误类型或保留规则。
原 `CallbackPayload(strict=True, extra="ignore")`、共享模型和手工附录仍合法，不借配方收紧旧合同。

## 受理 A：原接收事务

1. 薄入口通过 `require_runtime(ctx)` 选择可信装配的具体业务服务。ECS 使用 `ctx.binding`；
   WMS 由业务按当前输入解析绑定。按工作线 ID → 设备 ID → 业务动作固定顺序加锁；
   原解析器已持有的线/设备锁沿用，不反向取锁。
2. 校验当前线的业务、配置和许可，以及本次经过、资源和入站合同；在任何业务写入前决定
   Duplicate/Conflict。原消息重放由接收器沿原收据处理，业务自己的重复执行也不能再建动作。
3. 仅用 `ctx.session` 保存原收据关联、业务行和持久待办。收据由接收器负责；业务不另造收据身份。
   只依赖当前输入和当前许可、无需前序结果的初始意图，才可在这个事务登记到原 Outbox。
4. 返回原协议结果，由接收器提交和响应。禁止业务 commit/rollback、网络调用、改变本事务隔离级别，
   或另开推进事务。Rejected 沿原 WMS 保存点回滚处理器写入并保留拒绝收据；普通存储异常向外抛出，
   由接收器整体回滚并返回 503。ECS 沿原 ACK/诊断语义，不套用 WMS Rejected 的响应规则。

ECS Event 薄入口等待业务受理，ACK/诊断仍由原接收器负责（`accept_scan` 是业务方法，模型/合同须先确认）：

```python
from app.businesses.runtime import require_runtime


async def scanned(ctx, payload):
    await require_runtime(ctx).services[DEFINITION.code].accept_scan(ctx, payload)
```

WMS callback 薄入口须把具体业务方法的原 Outcome 返回给接收器；`accept_callback` 是业务方法，
返回 Accepted/Duplicate/Conflict/Rejected。不能只 await 后丢弃结果，否则返回的业务拒绝/冲突或响应数据会丢失。
直接处理器原有 None/dict 转 Accepted 的规则仍保留，这里展示的是转交已有业务 Outcome 的适配入口：

```python
from app.businesses.runtime import require_runtime


async def callback(ctx, payload):
    return await require_runtime(ctx).services[DEFINITION.code].accept_callback(ctx, payload)
```

原接收器返回 ACK/Accepted 只证明该受理事务的结果。测试受理异常后断言收据、业务行与 pending 均无残留，
重新提交原输入可成功；重复入站/重启保留同一收据和业务执行。可执行证据见下表 A1–A3。

## 独立推进 B：新事务

默认在受理／结果最外层提交后自动唤醒所属业务，调用 composition.PROGRESSION 声明的异步待办入口；Beat 每 5 秒使用同一个 `business.advance_pending(business_code)` 任务。
平台根据可信声明归属路由，不要求业务编写通知、结果 ID 反查或 Celery 装配。
共享 after_commit 保证保存点释放不发布、回滚丢弃，发布失败不改变已提交事实或 ACK。
业务待办入口通过短会话只定位自己的 pending ID，不消费结果、不为整轮扫描持有推进事务，再逐条调用原独立推进方法；重复通知允许重复检查，必须保持原外部意图身份。
公共任务只装配与委托，不提供通用业务查询、状态机、transaction runner 或第二 Sender；实际推进仍遵循下列隔离和锁序。

1. 每条记录由具体业务 `advance` 创建新 session 和事务，复用可信注入的 session factory/连接池而不跨记录复用事务；在首条 SQL
   （包括定位 SQL）前设置 `REPEATABLE READ`，不修改全局 factory。结果接收/应用事务须先提交，
   业务不能消费另一个未提交事务的 Result。
2. 先无锁定位原执行记录中的线/设备身份，再按工作线 ID → 设备 ID → 业务动作固定顺序锁定和重读。
   多行同类锁按 ID 顺序。重新核对当前许可与原执行归属；设备改绑不能把原待办搬到新线。
   这些锁内不得直接 settle/hold/修改框架命令或消息，避免与原发送门禁反向等待。
3. 从业务保存的引用固定完整直接前序集合、原请求身份及 caller/cycle/资源范围。
   相同范围每批最多 100 个不重复明确 ID；不同范围分组，跨轮或单项使用原身份逐个读取。
   所有批次、单项、WMS/Transport 混合读取都在这个 session 的同一 RR 快照内完成。
4. 仅读公开 DTO：`Capabilities.wms.get_result`、`transport.get_result/list_unclosed_predecessors`、
   `ecs.get_by_call/get_command`。不直接读取 ReliableMessage/TransportTask 或原响应字节。
   检查 availability、原身份、已应用版本、准确 member_ids/资源、pending/UNKNOWN，以及必要业务决定。
   前序 complete 且 unresolved 为空仅表示所列范围闭合；还须按真实合同判断成功、失败位置、当前许可及下一动作条件。
5. 在同一推进事务中保存业务进度、消费身份/版本/成员/决定与下一 Outbox 意图，原子提交。
   消费唯一性属于具体业务动作，同一结果可被不同动作分别消费；重入已完成动作复用原下一意图。
   网络投递交给既有 Outbox 和唯一 Sender，不能把读取成功当作设备派发授权。

以下是具体业务推进方法内部的一段事务边界，省略的业务锁定和判断须由上述步骤实现；
`advance` 是业务单条推进入口，`_advance` 示意其事务内实现；不是平台 runner，也不能从受理处理器直接执行：

```python
async def advance(self, execution_id):
    async with self.capabilities.session_factory() as session, session.begin():
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        return await self._advance(session, execution_id)
```

公开读接口调用示例使用业务已保存的原引用；变量须从本事务锁定的业务记录取出：

```python
wms_result = await capabilities.wms.get_result(
    session,
    message_id=saved_message_id,
    operation=saved_operation,
    operation_id=saved_operation_id,
)
transport_result = await capabilities.transport.get_result(
    session,
    transport_task_id=saved_task_id,
    client_request_id=saved_request_id,
    caller=saved_caller,
)
group = await capabilities.transport.list_unclosed_predecessors(
    session,
    caller=saved_caller,
    cycle_id=saved_cycle_id,
    predecessor_ids=tuple(saved_batch_ids),
    rack_id=saved_rack_id,
)
command = await capabilities.ecs.get_by_call(
    session,
    owner=saved_owner,
    call_key=saved_call_key,
)
```

RR 快照只证明这些事实曾同时成立；READ COMMITTED 即使共用事务也不足够。
同一快照内看不到外部新提交，需要更新事实就新事务整组重读，不跨事务拼接旧 DTO。
隔离测试在 100/1 批次间提交版本变化，断言本次完整 101 项仍属同一旧快照，下次整体使用新版本，
旧 TARGET_PLACED 里程碑不能覆盖新 UNKNOWN。证据 B1–B3。

## 失败与恢复卡

| 遇到什么 | 具体业务保留/核对什么 | 下一步及证据 |
| --- | --- | --- |
| NOT_FOUND、response_state=UNAVAILABLE，或关联缺失/歧义 | 原执行、待办和原等待依据；不能补造成功或挑最新关联 | 按原身份核对；已清理且未保存证据时进入待核对，见 F1/F2 |
| 更新后的 UNKNOWN、pending、HELD/投递不确定 | 当前已应用版本、成员范围和原命令身份；历史里程碑不能永久闭合 | 等有效新结果或按既有人工核对流程处理，不自动换号重发，见 B3/F1 |
| INVALID、IDENTITY_MISMATCH、非法/不匹配结果 | 保存业务核对状态，不消费其他身份的事实或授予下一动作 | 修复/核对遵循原结果接收规则；不能自行改写原结果，见 F1 |
| ACK、DONE、RECORDED、PREPARE_ACCEPTED、CLOSED | 区分受理、投递、事实收妥、准备受理及资源闭合 | 仍核对具体业务成功条件；CLOSED 也可能是拒绝或已知位置的失败，见 F3 |
| PostgreSQL 40001 | 失败事务全部回滚、关闭 session，业务待办和原等待依据保持；不提交部分消费或下一意图 | 新 session 重新定位、按锁序重读许可及完整组，整事务有界重试，见 R1/R2 |
| 重试耗尽、进程崩溃、丢/重复通知 | 原 execution_id、pending、已提交动作/消费依据；不换号、不无限忙重试 | 业务待办恢复同一 ID；提交结果未知时先核对原动作，不假定上一提交失败，见 R1/R3 |
| 配置变化或设备转线 | 原线/设备/角色和当前许可 | 原待办不随设备改线、不多发；有许可的新经过独立受理，见 R4 |

隔离测试的 `FlowService.advance` 最多尝试 3 次，真实 PostgreSQL 40001 由并发更新触发。
第一次失败后新 session 重读整组，而非仅重试 commit、最后一批或复用失败 DTO；在“已登记下一意图”后失败，
原意图和消费也整体回滚。耗尽返回 `retry_later`，pending 及原受理/前序依据仍在，
后续有效结果到达后用原 execution_id 恢复（R1/R2）。非 40001 存储错误向业务恢复入口抛出。

真实业务计划必须另行确定重试上限、等待原因的持久字段、核对责任和调度间隔。
测试接缝的 pending/原输入证明可恢复身份，未提供完整等待原因模型或生产恢复调度。
若需记录本次等待原因，必须在成功的业务事务中原子保存；失败事务不得为记原因偷偷提交部分进度。

沿用 `RELIABLE_RETENTION_DAYS`（默认 90 天），业务须在窗口内保存后续恢复所需依据。
清理前已保存的确定决定可跨重启使用；未保存的 WMS 决定/拒绝证据清理后不能猜测恢复。
有效已应用 Transport 事实与原提交消息保留分别判断，成员投影仍可随更高合法版本改变（F2/B3）。

## 可执行证据

下表路径相对仓库根；业务用例均经真实接收器/公开服务读结果。
测试中的框架表写入仅用于构造故障/证据，业务消费服务不直接读写框架表。
测试业务表使用独立 MetaData、显式创建及 finally 清理，不加入生产 Base/迁移或生产目录。

| 卡片 | 现有用例与关键断言 |
| --- | --- |
| A1 受理/回滚 | [test_result_consumption.py](../../tests/businesses/test_result_consumption.py)：`test_accepted_execution_advances_in_its_own_snapshot_transaction`、`test_acceptance_receipt_pending_and_state_roll_back_together`；ECS/WMS 原受理事务和新 RR 推进连接分离，存储失败无收据/业务行/待办残留 |
| A2 重复/跨线 | [test_workline_business_flow.py](../../tests/businesses/test_workline_business_flow.py)：`test_same_business_two_lines_never_cross_routes`、`test_business_mismatch_never_creates_intent`、`test_callback_restart_preserves_original_receipt`；不同线的设备/位置/地址不串用，错业务不建意图，重报不新建执行 |
| A3 原响应/保存点 | [test_wms_events.py](../../tests/controllers/test_wms_events.py)：`test_handler_outcomes_map_to_protocol_responses`、`test_handler_failure_rolls_back_everything_and_returns_protocol_503`、`test_raised_rejection_rolls_back_rows_written_before_it`；Duplicate/Conflict 无业务写入，Rejected 回滚业务但持久化原拒绝响应，普通异常连收据一起回滚为 503 |
| B1 Result 先提交 | [test_result_consumption.py](../../tests/businesses/test_result_consumption.py)：`test_result_receive_must_commit_before_independent_advance`；接收未提交时 waiting，提交后 advanced |
| B2 完整快照 | 同文件 `test_runtime_snapshot_keeps_full_101_member_versions`；100/1 分批和单项混读，逐项核对 101 个身份、版本、成员、资源及 closure_basis；下次事务整组更新 |
| B3 原范围/新 UNKNOWN | [test_predecessors.py](../../tests/integrations/wms_transport/test_predecessors.py)：`test_predecessors_use_explicit_ids_and_original_scope`、`test_historical_member_facts_do_not_override_newer_unknown`；错范围不漏成空集合，历史成员里程碑不覆盖新 UNKNOWN |
| R1 40001/耗尽 | [test_result_consumption.py](../../tests/businesses/test_result_consumption.py)：`test_serialization_failure_reopens_and_rereads_complete_business_group` 的两种 exhaust 参数；新 session、整组版本重读，耗尽 3 次仍 pending，原 ID 随后可恢复 |
| R2 意图后回滚 | 同文件 `test_serialization_after_intent_rolls_back_and_rechecks_all_results`；真实 40001 后业务进度/证据/命令全无残留，再读两项新版本等待，随后原 ID 恢复 |
| R3 重启/丢通知 | 同文件 `test_business_runtime_consumption_recovery`；重建后从持久待办找到原 ID，提交前崩溃全回滚、提交后调用方崩溃不重复下一意图，原 ACK 不变 |
| R4 并发/改绑 | 同文件 `test_duplicate_wakeups_and_config_update_serialize_without_cross_line`、`test_original_pending_execution_does_not_follow_reassigned_device`；独立 PG 后端实测工作线锁等待，重复推进不多发，原执行不随设备转线，新经过在新线上独立推进 |
| F1 非可信结果 | [WMS results](../../tests/integrations/wms_request/test_results.py) 的 `test_request_result_uses_original_message_identity`、`test_invalid_terminal_envelope_is_not_a_decision`；[Transport results](../../tests/integrations/wms_transport/test_results.py) 的 `test_identity_missing_frozen_pending_and_ambiguous_submission`、`test_transport_closure_matrix`；[predecessors](../../tests/integrations/wms_transport/test_predecessors.py) 的 `test_malformed_applied_result_cannot_close_a_resource`；错身份不泄露响应，歧义/HELD/非法证据不闭合 |
| F2 清理 | [test_result_consumption.py](../../tests/businesses/test_result_consumption.py)：`test_wms_consumption_survives_cleanup_only_with_saved_business_evidence`；清理后只对事先保存决定的执行返回 done，未保存者仍 pending；[Transport results](../../tests/integrations/wms_transport/test_results.py) 的 `test_transport_result_uses_applied_outcome_only` 证明已应用事实不依赖提交消息保留 |
| F3 成功边界 | [WMS results](../../tests/integrations/wms_request/test_results.py)：`test_delivery_terminal_is_not_business_completion`、`test_fact_receipt_is_a_protocol_response`；[Transport results](../../tests/integrations/wms_transport/test_results.py)：`test_confirmed_refusal_closes_only_without_execution_evidence`、`test_pending_callback_and_settled_delivery_are_not_applied_results`；PREPARE_ACCEPTED/RECORDED 只保留协议响应、DONE 无完成字段、拒绝也可 CLOSED |

在仓库根目录、锁定依赖下可重复运行：

```sh
rtk uv run --frozen --no-sync pytest -q -rs \
  tests/businesses/test_workline_business_flow.py tests/businesses/test_result_consumption.py \
  tests/integrations/wms_request/test_results.py \
  tests/integrations/wms_transport/test_results.py tests/integrations/wms_transport/test_predecessors.py \
  tests/controllers/test_wms_events.py::test_handler_outcomes_map_to_protocol_responses \
  tests/controllers/test_wms_events.py::test_handler_failure_rolls_back_everything_and_returns_protocol_503 \
  tests/controllers/test_wms_events.py::test_raised_rejection_rolls_back_rows_written_before_it
```

必须显式配置 `TEST_DATABASE_URL` / `TEST_REDIS_URL` 指向任务拥有的可丢弃存储；配置后连接失败应失败。
未配置时按既有规则跳过，不能把 SQLite/fake 快测当作 PG 隔离、锁序、40001 通过。
本轮实际执行计数、显式配置与跳过原因见任务实施报告；证据仅是隔离测试业务，
不代表人工/粗分真实业务闭环、供应商/现场验收或开发耗时指标通过。
