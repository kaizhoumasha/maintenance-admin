# ECS 通用接入与最小业务示例设计

> **接入代码历史说明（2026-10-28）。** 本文的旧业务目录、发现器、字符串/字典 SDK 或手写绑定示例已被[单一 API 接入](../../integrations/business-development-quickstart.md)及[验收记录](../../integrations/business-development-single-api-acceptance.md)替代；三类 registry 的独立 discovery.py 已删除，当前使用共享加载器和 catalog/固定平台 manifest。原协议、冻结数据、身份、结果与历史执行记录保持其原有语境，本文不作为当前 SDK 示例。


**状态：本方实现与开发测试已结项，收尾随 PR #24 合入 develop（1ef1725）。** 维护当前合同，不再追加实施过程。普通开发者读[教程](../../integrations/ecs-command-tutorial.md)，开发回归复用[现有测试](../../testing/ecs-command-verification.md)，不作为业务/Swagger接入门槛，回归入口见[测试说明](../../testing/ecs-command-verification.md)。生产验证不纳入开发过程，另见[真实接入清单](../../integrations/ecs-acceptance-checklist.md)。

## 职责与范围

- 复用EcsCommands、唯一可靠Sender、DynamicClients、Result持久证据，不新增SDK、调度器、ECS配置表或协议兼容层。
- DEVICE_CODE在设备记录中全系统唯一。正式地址由设备管理适配器提供，Swagger地址由请求提供；多设备可以共用地址。
- 维护者提供正式路由/严格附录；业务持有事务、当前许可与消费；框架负责冻结投递、响应分类、重试/暂停、结果与owner查询。
- Reference只在tests，一动作一命令、一个消费者、同库副作用；不冒充生产业务模型或物理完成。

## 10. 无设备表的通用协议入口

### 10.1 自动装配

ENVIRONMENT=local时主lifespan自动装配dev.ecs.network/v3与ecs.dev.protocol/1，关联同一个Outbox和唯一Sender；不要求动作登记、地址清单、联网开关或RELIABLE_TARGETS中的ECS条目。正式业务以configure_ecs提供路由和严格DeviceAnnex，不与local自动服务重复装配。冲突须拒绝，不能清空旧状态绕过。

### 10.2 请求、权限与地址

POST /api/v1/dev/ecs/commands要求登录及新登记权限dev:ecs:send_real。请求必填ecs_url、device_code、task_type、params、稳定client_request_id和非负int64执行timeout_ms；不接受owner、附录、策略、窗口或mock/real自报。

ecs_url为HTTP(S) IP origin，固定命令路径；拒绝凭据、path/query/fragment、DNS、非法IP、重定向及环境代理。task_type不查白名单，params/data保留合法JSON对象，信封/身份/类型/大小及ErrorDetail(code,msg)规则保持。动作语义由对端和正式业务校验，Swagger不创建业务表。

### 10.3 一次性装配与历史输入

configure_local_ecs(app, *, environment, history_annexes=None, client_factory=AsyncHttpClient)只接受可信启动依赖，不是Swagger或.env参数。

### 10.3.1 测试装配

测试可注入client_factory并沿实际主lifespan装配，验证冲突保护和唯一Sender；依赖须在provider/Sender创建前生效，不提前占用状态或事后补限制。出口保护由测试夹具负责，不是产品地址配置或Swagger使用门槛。

### 10.3.2 历史附录

history_annexes按原(annex_id,annex_version)映射到DeviceAnnex；复制/校验后一次合并，键必须为非空字符串二元组，内置保留键冲突拒绝，不改调用者映射或既有服务。新v3只选内置合同；旧结果仍按原严格附录。首次结果缺原附录503且零副作用，已有持久重复结果优先返回原响应；不恢复旧动作接口或v1/v2/grant发送。

### 10.4 项目最低时间与去重合同

| 要求 | 定义 |
|---|---|
| W=300秒 | 从本方首次创建起算并冻结；正式/调试新命令共用PROTOCOL_RETRY_WINDOW，不从首次发送、重试、重启或执行timeout推导 |
| D≥24小时 | 从对端持久确认确定终态起算，不用载荷finish_time或本方回调时间替代；未知terminal_at不启动清理倒计时 |
| 活动/未决 | 去重依据不按TTL清理；结果未确认时不得因D到期清理结果重报所需依据 |
| 跨重启/断电 | 持久接纳先于ACK与物理派发；同号同载荷恢复原事实、不重复执行，同号异载荷409；不确定先核对 |

真正未接纳的503/429允许原身份后续接纳，不缓存为永久拒绝；可能已接纳不能谎报未接纳。存储丢失、旧备份回滚或恢复出厂不能以空状态恢复受影响命令。该最低合同不证明实际供应商已符合，也不把本方长期证据保留改成24小时。D到期不授权重做UNKNOWN/HELD。

### 10.4.1 新创建与旧期限

EcsCommands.create先查旧调用并比较原输入，仅新调用校验路由retry_window为精确300秒；不一致、缺失或非法窗口按配置503拒绝，零新命令/Outbox义务，并提示共享常量来源。旧调用同参只读返回原身份，异参冲突，存量60/600秒等冻结期限不重算。发送/Result不套用当前窗口覆盖旧快照。

### 10.5 身份、策略与事务

owner来自可信业务装配；同键同输入同规范化地址返回原命令，异输入或换址409。并发唯一赢家返回后、提交前比较持久origin，输家零发送；URL/策略不拼入调用键或wire。业务动作、命令、Outbox同事务；Result、证据、收据、投影和settle同事务。

v1/v2、旧grant及未知策略保持围栏，不改版本/改址/补授权来恢复发送。旧owner只读重入与原严格结果能力保留；可信历史输入不等于旧策略重新获准。

### 10.6 Sender与未知交付

权限只控制新登记，撤权不暂停已登记v3命令。停止后续发送须停止全部相关Sender；共享lifespan停机会影响ECS/WMS出站与该实例HTTP/Result。停止、租约中断、shutdown unconfirmed不证明取消或未交付。

首次未发送、NOT_SENT及合法503/有效429判定NOT_RECEIVED可按原退避/Retry-After/next_attempt_at及期限恢复；到时前不发，过期停止新尝试。UNKNOWN/HELD、无效或超限Retry-After不因重启、设备空闲、换键或改址自动解封。WMS/STATIC行为不变。

### 10.7 开发测试

复用现有pytest、MockTransport、回环服务及测试库约定，覆盖公开创建/查询/Result和并发边界。测试数据清理不与同库运行中的联调API/Sender并行，不将测试保护扩展为独立隔离架构或接入流程。

## 11. 单表业务状态机

动作行合并稳定动作ID、可信owner/business_key、原输入、command_code、当前设备/版本、冻结原绑定/版本和state。READY→WAITING_RESULT与命令/Outbox共同提交；安全消费才转换SUCCEEDED/FAILED，终态同时作为本消费者的消费标记；终态行不清空重用。取消READY/WAITING_RESULT仅撤回业务许可。

新动作读取current_business(session, *, business_key)的权威许可/设备/版本，缺失或不确定拒绝；不能靠新行READY、新ID或换business_key制造许可。版本来自业务权威事实，不是设备Status、路由版本或动作计数。原调用重入保留原身份/输入/冻结版本；同业务新动作只有当前许可有效才登记，冻结当前版本。

登记、消费和变更采用“权威业务记录→动作行”统一锁序或已证明等价的版本条件；权威就在动作行时只锁该行。通过公开DTO查询结果，不私锁ECS ORM。消费要求WAITING_RESULT、原command_code、当前及冻结绑定/版本一致、合法结果且无hold_reason/needs_reconciliation，再与同库业务写共同提交；写失败一起回滚，可消费原结果重试。

取消/换绑/版本变化先提交则旧结果不推进；消费先提交则后续变更不能覆盖终态。已知矛盾阻止推进，读取后迟到矛盾留证核对、不自动撤销业务。测试事实替身不证明生产聚合锁；多命令、多消费者和外部副作用有实际需求时另设计，不扩大单表保证。

## 12. 开发者体验结项

普通开发者直接在Swagger填写模拟ECS地址，或在业务事务内登记并消费；不需要额外隔离阶段。现有测试证明当前机制，不新增试验平台、生产表或测试调度器。

已有通过且执行输入未变的范围复用证据；只改说明检查文档，行为缺陷用原公开入口复现后补最小回归。当前覆盖映射见[现有测试说明](../../testing/ecs-command-verification.md)。生产验证独立安排，不作为开发未完成项。
