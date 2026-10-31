# 出站结果读取与前序闭合

业务首次接入见[快速开始](business-development-quickstart.md)；受理 A、推进 B、失败恢复与对应 PostgreSQL 断言见[事务配方](business-transaction-recipes.md)。

本文是正式读接口参考。WMS／Transport 的协议身份、回调应用、重试、TTL 和终态清理规则保持；本地原归属与提交后通知见下文。读接口不发HTTP、不修改行、不加框架写锁、不提交／回滚／flush；调用方未提交的修改不会被隐式flush。返回冻结的数据对象外壳及独立数据副本，不返回ORM、原始响应字节或pending结果内容。数据库临时错误原样抛出，不能当作未找到或全部闭合。

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
| TransportTaskResult | 原任务／请求／operation身份、workline_id／station_id、owner_domain、cycle_id、kind、availability；submission_state及原提交message_id／target／投递状态／不确定／失败原因／响应字段；outcome／outcome_revision、member_facts、has_pending_outcome／pending_revision、closure／closure_reason／closure_basis |
| PredecessorQueryResult | 原caller／cycle／rack_id或container_id、requested_ids、完整items、派生unresolved、complete；每项保留原任务／请求、成员、资源closure、task_closure、reason、outcome_revision和closure_basis |

availability为FOUND／NOT_FOUND／IDENTITY_MISMATCH／INVALID。身份不符不返回另一身份的响应。response_state为NONE／VALID／INVALID／UNAVAILABLE；非终局／LOCAL_ERROR不提供业务决定，终局响应缺失为UNAVAILABLE，身份／协议／投递证据矛盾为INVALID。未知字段为None，不造PENDING。

WMS仅读取原消息的终局响应，沿用facts／decisions的适配器信封及分类规则，不读取last_response_raw。端点合同取已部署operation的注册声明；该声明已移除时响应为UNAVAILABLE，不从当前目标配置猜端点。response_data仍须由具体业务严格解析。RECORDED／DUPLICATE只表示事实收妥；PREPARE_ACCEPTED仍等待后续计划；DECIDED才可按业务合同解释决定。DONE、FAILED和EXPIRED均不直接表示业务或物理完成。

响应中任意层的 JSON 数字若解码溢出为非有限数，同步分类为 UNKNOWN；读取已保存的终局响应时为 INVALID，不暴露失真的 response_data。原始响应字节继续保留，不改写历史消息身份或投递状态。

Transport只返回有效已应用结果及版本；pending只提供存在标记和版本，回调使提交DONE也不证明搬运完成。member_facts是原任务累积的历史里程碑，可能在更新UNKNOWN后继续保留TARGET_PLACED；不能据此覆盖较新已应用UNKNOWN、绕过pending、推导当前占用或创建下一动作。原回调前置条件不变。只有拒绝证据而没有已应用结果时，同任务已接纳的SOURCE_PICKED／与冻结目标匹配的TARGET_PLACED会使拒绝闭合变为UNDETERMINED／INVALID_EVIDENCE，保留原拒绝及历史里程碑供核对；不据此推导当前完成。原POSITION_UNKNOWN事件仅记诊断，不存为里程碑。后续有效已应用结果仍按矩阵优先，不把历史冲突变成永久阻塞。

**BIN_Roller B1/B7 窄适配已实现：** [实现计划](../superpowers/plans/2026-10-30-bin-roller-implementation.md)直接复用 outcome/member_facts；原 TransportTask 新增 nullable JSON 字段 position_observation，公开结果读口返回其原 task/kind/rack 身份正确、终位明确但目标/面向不符的架结果 data 副本及原 outcome_revision。读口不解析诊断或生成“最新位置”；身份/位置未知及原终态冲突不提升为事实。观察只更新位置，不授权动作或改变 outcome/closure，也不构成额外等待；新异常架位置保存后经既有 after_commit 唤醒原 owner。组件本地行为与测试宿主已验证，正式生产接入及组件状态迁移仍未交付。

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

默认由事件／结果提交后立即投递具体 Celery 推进任务，Beat 扫描持久待办兜底；两者调用 composition.PROGRESSION 声明的同一业务待办入口，再由业务按原 execution_id 独立推进。通知只携带 business_code，调度壳不查询业务表或消费结果。待办扫描只用短会话定位候选业务 ID，不消费结果，也不为整轮扫描持有推进事务。每条记录的 `advance` 拥有独立的新 session 与事务，通用基线在首条 SQL 前配置 REPEATABLE READ，按工作线→设备→业务执行锁序核对当前许可和全部前序。任务内复用数据库引擎和连接不等于跨记录复用 session 或事务。平台结果读取接口不接管事务；结果接收／应用仍留在原框架事务，提交后才能被推进消费。

应用装配注入结果通知端口；`MessageStore.on_result(operation, business_key)` 在非快照请求终态成功保存后的最外层提交后通知，覆盖 WMS 决策 HTTP 响应。ECS 命令 ACK 仍只是投递证据，业务唤醒来自新 Result 证据提交后的 `owner_domain`。通用机制不查询业务表或直接调用业务；平台按原本地业务关联／原命令 owner 路由到统一业务待办任务；仅旧专属 WMS 的 None 关联沿原注册源。事务回滚、陈旧完成不通知；发布失败不撤销结果或伪装接收失败，由原 pending 扫描恢复。UNKNOWN/HELD 仍按原协议和门禁核对，不因唤醒换身份重发。

当前自动通知覆盖业务所属 ECS Event、Accepted WMS 入站回调、普通非快照 WMS 请求有效终态和新 ECS Result。WMS 请求原本地 business_key=business:<业务码> 与结果一并沿原事务处理；本地 EXPIRED（含过期租约维护）也是提交后通知，不改变原不确定性或重试规则。TransportTask 保存 nullable owner_domain；原 submit 有效 RECEIVED/202 或 DUPLICATE/200 保存 DONE 时，平台在同事务按原 operation_id 和 business_key=transport_task_id 精确读取原 owner，经 after_commit 通知原业务。原回调使 applied_outcome_revision 实际前进或新明确异常架位置保存时也通知，位置补齐暂存结果同样覆盖。接纳通知不冒充 applied outcome 或物理完成；失败/未知、重复完成、失效租约、回滚及无已知归属不猜测或广播。原消息身份/字节和既有终态通知不变；丢通知由同一业务 pending/Beat 兜底。

**BIN_Roller 目标业务的隔离例外（测试宿主已验证，正式生产宿主未交付）：** 仅会进入组件架流/正常求架检查的原宿主单条独立 advance 在首 SQL 前设 SERIALIZABLE，复用原 session_factory、业务幂等及 Outbox/after_commit。集合判定、登记和业务消费均在该入口整笔执行，不能只升 DRAIN 分支；RR/请求级待决唯一不足以覆盖旧快照跨 WAIT/READY 消费的首登。receiver 原受理/结果应用、短扫描、已有无关纯生产及其他业务保持原隔离，不修改共享 factory/engine/池默认，不新增选择器/执行器。既有 40001 重试须包住外层 commit；非 40001 提交未知沿原待办进入完整 SERIALIZABLE 推进，查空不证明回滚也不直接授权新登记；原业务判定/唯一性、集合依赖及最终提交共同校验，不换已提交身份或新增确认状态。原连接执行选项关闭/归还时恢复，不新增重置钩子。结果读口仍只用调用方 session。见[BIN_Roller U06 范围及恢复](../superpowers/specs/2026-10-30-bin-roller-capability-design.md#roller-u06-minimal-transaction)。

单个Transport结果和有界前序批次各用一条SQL取得任务及全部提交关联；同一次判断需要多次分批、跨轮逐个或混合读取时，全部查询必须在同一 session／同一统一快照事务中完成：通用基线 REPEATABLE READ，上述 BIN_Roller 相关独立 advance 采用 SERIALIZABLE。固定完整原身份和资源范围，不跨失败、事务或重启拼接片段。READ COMMITTED即使共用事务也不是统一快照。需要更新事实时换新事务重读整组；同一快照内不会看到外部新提交。快照不提供物理锁或派发时的执行授权。

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

阶段二本地归属不进入 WMS/Transport 协议字段，也不改变冻结请求、input_digest、operation_id、client_request_id 或 caller/cycle。
运行时绑定业务实例负责新请求消费者核对和原关联重放冲突；默认未绑定实例不能创建正式共享 WMS 请求。
旧 None 不回填；公开 Transport owner_domain 只是原关联，不授权新动作。该列迁移有非空证据时阻止降级删除。
测试与部署边界见[阶段二验收](../testing/business-components-phase2.md)，BIN_Roller 阶段三已完成本地组件及隔离测试装配，正式生产宿主/catalog、组件状态迁移及 SMT 接入仍未完成。
