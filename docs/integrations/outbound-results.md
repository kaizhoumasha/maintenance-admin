# 出站结果读取与前序闭合

业务首次接入见[快速开始](business-development-quickstart.md)；受理 A、推进 B、失败恢复与对应 PostgreSQL 断言见[事务配方](business-transaction-recipes.md)。

本文是正式读接口参考。WMS／Transport的发送、回调、重试、TTL和终态清理保持原行为。读接口不发HTTP、不修改行、不加框架写锁、不提交／回滚／flush；调用方未提交的修改不会被隐式flush。返回冻结的数据对象外壳及独立数据副本，不返回ORM、原始响应字节或pending结果内容。数据库临时错误原样抛出，不能当作未找到或全部闭合。

## 原身份查询

```python
wms_result = await wms_requests.get_result(
    session,
    message_id=saved_message_id,
    operation=saved_operation,
    operation_id=saved_operation_id,
)
transport_result = await transport_service.get_result(
    session,
    transport_task_id=saved_transport_task_id,
    client_request_id=saved_client_request_id,
    caller=saved_caller,
)
```

身份必须来自业务登记时保存的原引用。读取不调用当前target解析，不因工作线／目标改名、删除或同编码重建而改选历史；caller为原TransportCaller，包含workline_id和station_id，不是当前执行许可。

| 类型 | 字段与解释 |
| --- | --- |
| WmsRequestResult | message_id、operation、operation_id、原target、availability；delivery_status／delivery_terminal／delivery_uncertain／fail_reason；response_state、http_status、response_code、response_timestamp、response_data |
| TransportTaskResult | 原任务／请求／operation身份、workline_id／station_id、cycle_id、kind、availability；submission_state及原提交message_id／target／投递状态／不确定／失败原因／响应字段；outcome／outcome_revision、member_facts、has_pending_outcome／pending_revision、closure／closure_reason／closure_basis |
| PredecessorQueryResult | 原caller／cycle／rack_id或container_id、requested_ids、完整items、派生unresolved、complete；每项保留原任务／请求、成员、资源closure、task_closure、reason、outcome_revision和closure_basis |

availability为FOUND／NOT_FOUND／IDENTITY_MISMATCH／INVALID。身份不符不返回另一身份的响应。response_state为NONE／VALID／INVALID／UNAVAILABLE；非终局／LOCAL_ERROR不提供业务决定，终局响应缺失为UNAVAILABLE，身份／协议／投递证据矛盾为INVALID。未知字段为None，不造PENDING。

WMS仅读取原消息的终局响应，沿用facts／decisions的适配器信封及分类规则，不读取last_response_raw。端点合同取已部署operation的注册声明；该声明已移除时响应为UNAVAILABLE，不从当前目标配置猜端点。response_data仍须由具体业务严格解析。RECORDED／DUPLICATE只表示事实收妥；PREPARE_ACCEPTED仍等待后续计划；DECIDED才可按业务合同解释决定。DONE、FAILED和EXPIRED均不直接表示业务或物理完成。

响应中任意层的 JSON 数字若解码溢出为非有限数，同步分类为 UNKNOWN；读取已保存的终局响应时为 INVALID，不暴露失真的 response_data。原始响应字节继续保留，不改写历史消息身份或投递状态。

Transport只返回有效已应用结果及版本；pending只提供存在标记和版本，回调使提交DONE也不证明搬运完成。member_facts是原任务累积的历史里程碑，可能在更新UNKNOWN后继续保留TARGET_PLACED；不能据此覆盖较新已应用UNKNOWN、绕过pending、推导当前占用或创建下一动作。原回调前置条件不变。只有拒绝证据而没有已应用结果时，同任务已接纳的SOURCE_PICKED／与冻结目标匹配的TARGET_PLACED会使拒绝闭合变为UNDETERMINED／INVALID_EVIDENCE，保留原拒绝及历史里程碑供核对；不据此推导当前完成。原POSITION_UNKNOWN事件仅记诊断，不存为里程碑。后续有效已应用结果仍按矩阵优先，不把历史冲突变成永久阻塞。

Transport提交关联按原operation_id＋transport.task.submit@v1＋business_key=transport_task_id跨target查询，零条UNAVAILABLE、多条AMBIGUOUS，不能挑最新。有效已应用终态不依赖提交消息继续保留。

## 闭合与前序范围

| 原事实 | closure |
| --- | --- |
| 已应用成功、明确位置的失败或货架取消 | CLOSED／APPLIED_RESULT；不表示成功或授权新动作 |
| 已应用UNKNOWN／相关成员未知 | UNDETERMINED／UNKNOWN_POSITION |
| pending等待位置，尚无已应用结果 | OPEN／AWAITING_POSITION |
| 原提交明确409／422拒绝，且无矛盾的同任务有效位置里程碑或执行结果 | CLOSED／SUBMIT_REJECTION |
| 等待提交或执行结果，包括回调settle的DONE | OPEN |
| 投递不确定、HELD、LOCAL_ERROR、EXPIRED、缺失／错范围／非法证据或关联歧义 | UNDETERMINED；不推导从未下发 |

```python
predecessors = await transport_service.list_unclosed_predecessors(
    session,
    caller=saved_caller,
    cycle_id=saved_cycle_id,
    predecessor_ids=tuple(saved_direct_predecessor_ids),
    rack_id="R1",
)
```

每次最多100个不重复明确ID；必须原UUIDv7 cycle_id，rack_id与container_id二选一。空元组表示未列前序，不自动扫历史。货架按整架及料箱冻结来源／目标架槽匹配，不按面缩小。错caller／cycle／资源、缺行／缺冻结不会被过滤成空集合。items保留全部ID及CLOSED证据，unresolved含OPEN／UNDETERMINED；只有complete且unresolved为空才说明本次明确集合全闭合。

料箱批次按相关成员投影，不因无关架／箱UNKNOWN误挡；整批task_closure仍保留。整批UNKNOWN时的局部闭合可被合法高版本改变，不能永久移出待核集合。跨轮的真实直接依赖保存各自原身份并逐个get_result读取，按公开已应用outcome判断相关成员，不把整任务UNKNOWN改成成功。

## 受理、统一快照与恢复

受理处理器继续用原ctx.session，同事务保存原收据、受理状态和持久待推进依据；原协议与稳定拒绝／临时回滚语义保持。不得在已执行SQL的处理器中换隔离、提交／回滚，或另开写事务尝试推进。只依赖本次输入及当前许可的初始意图可按业务合同登记。

提交后通知／持久待办扫描以原execution_id调用具体业务独立推进入口。该入口拥有新session与事务，在首条SQL前配置REPEATABLE READ，按工作线→设备→业务执行锁序核对当前许可和全部前序。服务及平台读接口不接管事务；结果接收／应用仍留在原框架事务，提交后才能被推进消费。

单个Transport结果和有界前序批次各用一条SQL取得任务及全部提交关联；同一次判断需要多次分批、跨轮逐个或混合读取时，全部查询必须在同一session／同一REPEATABLE READ事务中完成。固定完整原身份和资源范围，不跨失败、事务或重启拼接片段。READ COMMITTED即使共用事务也不是统一快照。需要更新事实时换新事务重读整组；同一快照内不会看到外部新提交。快照不提供物理锁或派发时的执行授权。

下面是独立推进入口的事务示意；advance_owned_business是具体业务自己的服务，并非平台新增接口。次数3仅用于说明有界重试，真实业务计划须明确策略和耗尽后的待办恢复／受控失败行为。

```python
from sqlalchemy.exc import DBAPIError


async def wake_execution(session_factory, execution_id, advance_owned_business):
    for _ in range(3):
        try:
            async with session_factory() as session, session.begin():
                await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                await advance_owned_business(session, execution_id)
            return "ADVANCED"
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) != "40001":
                raise
    return "RETRY_LATER"  # 原持久待办必须仍可被后续唤醒发现。
```

复用既有引擎及连接池，不改全局session_factory。序列化失败关闭原session，重试整个事务及尚需核对的完整前序组，不能只重试最后一批／commit或复用旧DTO；保留原执行、动作和请求身份。其他数据库错误抛给本业务恢复入口，不据此确认UNKNOWN或另外发送。提交结果未知时也按原身份重新核对动作／消费进度，不假定上一事务必然未提交。读接口无重试，Outbox只负责已提交意图的投递。

推进状态、消费标记与下一Outbox意图在独立推进事务内原子；受理事务与推进事务各自原子，阶段间由持久待办恢复。消费唯一性属于业务动作，不是平台全局消费位：两个动作可分别消费同一结果版本。成员证据需含资源、member_ids及准确版本。

沿用RELIABLE_RETENTION_DAYS（默认90天）。业务在窗口内保存所需结果与消费证据；清理后未保存的WMS决定／拒绝证据不能恢复，NOT_FOUND／UNAVAILABLE不等于成功或重新发送。Transport已应用事实仍从任务读取；整体确定终态与仍可变化的成员投影分别处理。本接口不增加无界档案、平台消费表、业务worker或第二个重试调度。

验收用例见tests/integrations/wms_request/test_results.py、wms_transport/test_results.py、test_predecessors.py及tests/integrations/test_result_consumption.py；快照和序列化重试必须用显式独立PostgreSQL验证，SQLite不能代替。
