# smt_auto 规划对照 PR #49 复审

评审日期：2026-11-03（以下PR #49表数、迁移及软件结论均为当时历史快照）。远端已核实 [PR #49](https://github.com/kaizhoumasha/maintenance-admin/pull/49) 于2026-11-03T18:56:53Z合入develop，merge commit为7ab5e3104ff448a27dd25552a774e3235a3fdbe3；本轮以该提交的实际代码为基线。评审及文档修订位于feature/smt-auto-plan-review-pr49，未实施AUTO、未运行产品测试/迁移、未提交或部署。

结论：原AUTO规划不能原样实施。#49已统一手工/BIN_Roller的架调度，但AUTO尚非其正式消费者；仅新增业务包/更换owner不足以接入。本轮直接修订[API](../../integrations/smt-auto-api.md)、[业务设计](../specs/2026-10-31-smt-auto-business-design.md)、[数据模型](../specs/2026-11-02-smt-auto-data-model-design.md)、[总实施计划](../plans/2026-10-31-smt-auto-implementation.md)及[sorting_task共享计划的宿主锁序/当前引用](../plans/2026-10-31-sorting-task-implementation.md#sorting-task-recheck)，删除冲突步骤，保留两方向合同、完成条件及用户已确认规则。以下问题级别指旧规划被照搬实施的风险，不表示当前手工/roller已发生故障。

当前补记（2026-11-04）：develop `98008ff`；BIN_Roller已收敛为4表，RackDispatch仍2表，AUTO已有7表foundation；唯一head `smtautofoundation`（父 `smtmanualoprename`）。局部S1宿主修正不代表队列领取、prepare/原结果消费、共享receiver重验、PostgreSQL并发或正式AUTO装配完成。后续由既有7表保留数据演进到11表，不在S6重建全套表。原评审结论/历史证据保留。

## 1. 已交付边界与可直接复用部分

| 范围 | 当前代码事实 | AUTO规划边界 |
| --- | --- | --- |
| sorting_task | 六类严格合同、可信原归属/薄分派、原plan重验及可选归档guard已交付 | 直接消费ST1/ST2；自动任务状态/队列/生产仍自有，不重写receiver |
| RackDispatch | 公共RackJob/RackAction、原进场/换面结果、唯一离场决定链、严格DEPART提交接纳 | 引用原公共job/action，不建影子架周期/Transport链；补AUTO真实事实及来源支持 |
| BIN_Roller | 8张表；原SCAN/供回箱/FIFO/drain/SCAN2释放，架job/action转交公共调度 | 复用公开ports及具名ID，不再读取私有rack_cycle/rack_action |
| smt_manual | 5张宿主表；显式组合3组件，同一owner-scoped RackDispatch注入roller | 参考组合/锁序/结果消费，不继承手工repository或人工完工合同 |
| 数据库 | 唯一head为sharedrackdispatch，父racksubmitack；公共调度2表 | AUTO新增11张必要宿主表及3类来源CHECK扩充，接实施时最新head |

代码入口：[手工组合](../../../app/businesses/smt_manual/composition.py)、[同实例装配及事务推进](../../../app/businesses/smt_manual/service.py)、[公共合同](../../../app/businesses/_components/rack_dispatch/contracts.py)、[公共模型](../../../app/businesses/_components/rack_dispatch/models.py)、[BIN_Roller公开读取](../../../app/businesses/_components/bin_roller/service.py)。#49既有5490 passed/47 SQLite专属跳过等软件记录见[公共计划最终验收](../plans/2026-11-03-shared-rack-dispatch-implementation.md#最终验收2026-11-03)；本轮未重跑，不计为AUTO或现场证据。

## 2. 详细发现与已修订位置

### R01 · P1：私有架模型与公共调度重复，恢复真相会分裂

旧模型仍规划两张方向rack_visit及统一rack_action，I/O步骤另建离场根、WAIT唯一链与Transport引用。若新架动作已经在公共job提交，而私有visit未同步或重启先恢复另一份链，就可能重复登记、错认位置或永远等待已完成动作。

依据：公共[models.py](../../../app/businesses/_components/rack_dispatch/models.py)第23行起RackJob、第80行起RackAction，以及112～124行的原移动/离场根/后继唯一约束，已经拥有这些责任。

修订：模型从14张降至11张，成员/盘只保存原rack_job_id及preparation_action_id；取尽/撤回保持宿主事实，公共作业/动作及离场链只保留一份。总计划S2/I1/O1/S6改为公共门面适配及真实接入验收，不重复开发调度链。覆盖M08/M12、B06。

### R02 · P1：公共组件存在不等于AUTO来源及模板已经支持

现有RackJob CHECK仅支持MANUAL_RETURN、MANUAL_TRANSFER、ROLLER_ORDINARY、ROLLER_DRAIN。直接传AUTO会约束失败；冒用ROLLER或仅放宽CHECK，会被现有非MANUAL分支选成CTU模板，离场接纳校验同样不匹配F01。

依据：[models.py](../../../app/businesses/_components/rack_dispatch/models.py)第38行；[service.py](../../../app/businesses/_components/rack_dispatch/service.py)第414行_move及995行_accept仍按MANUAL_前缀分流。这是未来AUTO接入缺口，当前四类业务并无由此产生的缺陷结论。

修订：S2只窄扩充AUTO_INBOUND_SOURCE、AUTO_OUTBOUND_RETURN、AUTO_OUTBOUND_TARGET三个来源；固定ENTER/ROTATE/DEPART分别为F01/不支持/F01、F01/CK04/F01、F01/CK04/F01。同步来源CHECK、创建/绑定/原接纳校验和真实模板测试；S6一次迁移，已有AUTO公共记录时有损降级先拒绝。不得仅在文档宣称“直接接入”，也不抽象任意模板引擎。覆盖S2步骤2b～2c、S6、M12及D6/D7。

### R03 · P1：旧DEPART物理结果门槛与已合入完成语义冲突

旧规划在F01去ZONE、最终位置仍为原点、最终FAILED/无回调等情况下继续等待或拒绝完成；部分目标架还依赖父任务COMPLETED。照搬将使共享调度已完成而AUTO仍占架；反过来把此接纳规则用于ENTER/ROTATE/机械臂又会提前生产。

依据：[service.py](../../../app/businesses/_components/rack_dispatch/service.py)第985～1055行_accept：严格匹配原owner/caller/cycle/Transport/client、submit operation/message/target、business_key及frozen，且原202/RECEIVED或200/DUPLICATE后，action ACCEPTED、job COMPLETED/DEPARTURE_ACK。第669行起已有DEPART独立接纳分支，不依赖最终物理回调。

修订：DEPART/RACK_MOVE+CTU03或F01，去ZONE/不同RACK_POSITION（含生产线）统一严格原接纳完成，迟到FAILED/同源终位仅备查，不回退或重搬；ENTER/ROTATE及自动取放、SCAN2释放仍保留各自严格结果条件。来源/目标无需求且安全交出不等父task终态或独立报告ACK，报告和业务完成仍独立收口。覆盖API§6、I1/O1/O4/S5、B06/M08。

### R04 · P1：两组件装配和旧锁序不能保证新公共边界

旧计划只组合sorting_task/bin_roller，并按WorkLine→Devices→私有动作锁定。新公共架实体及roller已共享，若方向先锁盘/成员再锁RackJob，可能与公共推进交错；只升级inbound隔离还漏掉outbound目标PUT登记与退架竞争。

依据：[composition.py](../../../app/businesses/smt_manual/composition.py)第9行的3组件；[service.py](../../../app/businesses/smt_manual/service.py)第37～39行同实例注入及157行起事务矩阵；[repository.py](../../../app/businesses/smt_manual/repository.py)第44～68行发现job→锁公共job→宿主/roller主体→复核集合→锁actions。40001含commit和RackLockSetChanged均整笔新事务重试。

修订：S6精确3组件，创建一个RackDispatch并注入roller及两个方向；统一WorkLines→Devices→RackJobs→固定类型/PK宿主主体→RackActions。TASK、两方向RACK/BIN/PLATE及roller架流首SQL前SERIALIZABLE，独立方向REPORT/SCAN2_RELEASE/纯归档RR，候选只读RR。相关事实变化原事务note_facts_changed，锁集合变化/40001最多3次全事务重试。只修AUTO计划，不改receiver全局或现有手工隔离。覆盖模型§7/M11、S2/S6/G01～G03。

### R05 · P1：NONE/CLEAR必须来自完整原事实，退架暂定标记不是永久切点

若只看已绑定成员或空查询，全撤回后、离场根登记前到达的新有效同架需求可能被漏读；若把release_requested当不可逆切点，则公共服务仍可取消退架而宿主永久拒绝新成员。相反，已有RELEASING原链后继续绑定新成员会把新需求挂到即将退出的架。

依据：[service.py](../../../app/businesses/_components/rack_dispatch/service.py)第104～115行仅校验Coverage结构/作用域；第691～695行ACTIVE下NONE设release_requested、CONTINUE清除；第698行要求LOCAL_ONLY动作的确切authorized_original_id；第981行登记离场根才设RELEASING。[manual_rack_flow.py](../../../app/businesses/smt_manual/manual_rack_flow.py)第774～782行在无离场根时包含未绑定同架成员，可参考其事实边界，不能直接借用其私有读取。

修订：AUTO在原事务覆盖连续/未处理计划、全部有效成员/撤回/取尽、原授权/潜在决定、物理未知及直接交接，缺证UNKNOWN；合法空集合也有真实anchor/key/version。离场根登记前将本job可服务的未绑定新成员纳入求值，完整CONTINUE由公共advance清标后关联；RELEASING后新成员留未选中。已有LOCAL_ONLY动作的恢复显式核对原授权；正常新prepare的调用前门禁及同事务Transport意图见本次追加R09，不以动作ID证明可靠接纳或到位。覆盖API§3.6/§6.2、模型§5/§7、I1/O1/S2、I02/M08。

### R06 · P2：公共job结束后独立报告和原待办仍须发现

若方向pending只以架job.pending或当前任务/配置为筛选，严格离场接纳后尚未收妥的movement_report会被遗漏。若仍用original_record("rack_cycle")恢复roller原架流，则新代码无法定位。

依据：[bin_roller/service.py](../../../app/businesses/_components/bin_roller/service.py)第131～162行original_record已无rack_cycle/rack_action键；[手工service.py](../../../app/businesses/smt_manual/service.py)第30行及229行保留独立RACK_REPORT。公共job完成与业务报告完成属于不同真相。

修订：方向RACK分页原AUTO来源公共job，roller RACK_CYCLE分页ROLLER来源公共job；其余六类组件ID保持原具名语义。INBOUND_REPORT/OUTBOUND_REPORT按原方向plate ID独立RR恢复，不依赖job/父task仍pending，仍由同一scan_pending/Beat推进；原生产待办也发现已物理成功但尚未形成报告意图的记录。不新增报告表、独立Celery任务或AUTO货架到位operation。覆盖S6/G03、A55/M11。

### R07 · P2：归档不能删除仍需向共享调度供给的宿主证明

旧方案保留私有visit/action恢复子图；取消这些表后，若仍按7天清掉成员/撤回输入，再从查空生成NONE/CLEAR，原公共离场既缺真凭据也可能错放架。公共job.source_ref也不得随业务清理改号。

修订：公共job/action及Transport历史不删不改。仅在inbound直接义务全闭合、完整保留期满后，按原公共job冻结task.rack_retention_basis最小范围/原输入/非空成员撤回或取尽/直接交接/原动作证明，再清业务明细；归档适配器凭固定证明恢复原Coverage，缺证则不清理且返回UNKNOWN。原报警/取放直接未决不能归为独立离场以绕过保留；outbound不套7天政策。该摘要不是第二份公共状态或重执行许可。覆盖模型§8/M10、I4/S6、I07/A55。

### R08 · P2：不能把MANUAL专属提前续问当成AUTO通用能力

旧计划称离场WAIT可由任意新事实提前续问，且未区分公共能力与宿主规则。若AUTO仅新增source_kind，现有代码实际仍会按非MANUAL路径等待原到期。

依据：[service.py](../../../app/businesses/_components/rack_dispatch/service.py)第935～950行：到期前的原位置/任务状态变化提前续问仅支持MANUAL_，也不是任意事实变化。

修订：AUTO基线沿非MANUAL固定due_at续问，不因普通唤醒/重复撤回/取尽刷新期限或另起根；未知/503仍恢复原请求。若以后确需提前续问，单独确认窄业务触发及实证，当前不扩通用策略。覆盖模型§5.3、API§3.6、B06/I1/O1。

## 3. 三项已确认业务规则的复核

| 规则 | 本轮保留/补齐 | 验收位置 |
| --- | --- | --- |
| 当前任务资格沿手工 | PREPARING/EXECUTING、原prepare可靠接纳、线启用及原宿主/编码匹配；无plan_revision>0附加条件。prepare未知到箱保留CONTEXT_NOT_READY，不因后续prepare成功自动回放，与原确认暂缓恢复区分 | 模型§10.1/M04；S1/S2/S5/S6、I02/G04 |
| inbound全撤回安全退架 | 非空完整同次覆盖、其他有效版本及潜在原授权核对，取料/直接物理交接结清后给出NONE/CLEAR；不伪造取尽。沿公共唯一链/暂定撤标/不可逆切点，不反向增加父任务终态或独立报告门槛 | 模型§10.2/M08；I1步骤1～1a、B06/I02 |
| ECS原命令内恢复 | 硬件停机报警/人工处理期间不先报终态FAILED，ECS保留原已接纳未结束命令及阶段，核对原盘/实际位置/原目标，仅恢复未完成部分；原command_code最终严格Result。已报FAILED不更正SUCCESS；UNKNOWN/HELD不自动重做；WES无新恢复接口/枚举/报警表 | 模型§10.3/M06；S7步骤1a～1b、A27/I04及D3/D4 |

RackDispatch DEPART接纳只结束调度，不能解除ECS盘的未知/失败物理责任。必报、SCAN2释放、面/整单完成及inbound清理均保持本方向条件。设备原命令持久恢复、outbound权威无料及NG位置映射仍分别由D3/D4/D5交付，文档收敛不代表这些能力已验证。

## 4. 修订后场景复查：并发、异常与重启

本次继续检查上轮修订后的工作区，保留原改动；代码仍以#49/7ab5e31为准。按同线多个worker、通知与Beat重复推进、不同线独立、对端响应乱序及进程重建推演。下列R09～R12是追加发现，已同步正文及原S/I/O任务；不增加新的实施阶段或API验收编号。

### R09 · P1：prepare已登记Transport，不能在下一轮才补授权

上轮模型§5.3将prepare写成“先登记原动作”，并把LOCAL_ONLY原恢复授权与正常新动作混在相邻步骤。实际[service.py](../../../app/businesses/_components/rack_dispatch/service.py)第411行prepare调用_move，第443行_move立即_bind_move，第450行起登记Transport及原submit引用；正常新动作不是等待advance授权的LOCAL_ONLY。若先prepare提交，再在下一轮查到取消/旧面PUT未闭，原搬运意图已经进入Outbox，后置门禁无法阻止发送。

修订：S2步骤2d在真实公共门面验调用前授权/当前新作业资源/交接及原关联同事务；撤回与prepare交错要么没有新意图，要么提交为完整原责任，回滚不留下半个动作。LOCAL_ONLY只恢复已有证明，不制造第二阶段发送协议。register幂等返回旧job和prepare复用原动作时，核对原冻结出处、用途及点面，不以非空ID当新许可；原准备引用先持久化，实际生产仍须公共当前position_action_id严格成功。模型§5.2～5.4、API§8.3和总计划S2/I1/O1已改正。

### R10 · P1：准备需求与物理交接混用，会自锁或挪走前盘目标

案例一：首版目标/本盘B面授权等待ENTER/ROTATE，若把“本盘尚未完成PUT”直接算作BLOCKED，准备永远不能发生。案例二：A盘还有旧A面PUT授权或原命令报警未终结，B盘已获B面ROTATE/REPLACE；若只看B盘需求，架会在A盘恢复前被挪走。若只验证历史ENTER成功，也可能在已换B面时误执行A面PUT。

依据：公共[prepare](../../../app/businesses/_components/rack_dispatch/service.py)第357行起不接收AUTO授权/交接事实；现有原LOCAL_ONLY恢复的advance第696～702行要求交接CLEAR，ROTATE又在737～758行核对原位置。AUTO必须自己在调用前按拟议动作分清“需求等待准备”与“旧面仍有直接执行责任”，不能靠公共类型替它推断。REPLACE同点的竞争job也由宿主事实覆盖，单靠公共_position_conflict并不构成同点调度排队策略。

修订：唯一阶段矩阵在[模型§5.4](../specs/2026-11-02-smt-auto-data-model-design.md#54-准备交接与异常续问的事实边界)。等待其自身所需准备的授权保留CONTINUE，不凭该等待本身永久BLOCKED；原旧面授权/取放/报警/未知保护受影响换面/离场，原物理与交接闭合后仅纯报告不占架。REPLACE先将目标授权留原盘/决定，旧job严格离场接纳后才登记同点新job/ENTER；不以READY或submit未知提前制造相互竞争的两份job。O1步骤1b覆盖正反交错和重启，S2步骤2d验证历史成功/原ID不能替代当前准备。

### R11 · P1：NG原面续问需求及RR→SSI交接仍可能漏掉

上轮“独立报告不占架”没有足够区分outbound NG：若本面未取料终局，NG报告收妥后仍应按API§5.9.2复用原return_rack.work_plan。这是原面潜在取料需求，不是只剩库存报告；若先退架，报告恢复后就无法合法沿原rack/face/arrived_at续问。另一个重启窗口是报告RR提交、原NG续问尚未登记：若报告消费者清了全部生产pending，既无通知也无Beat候选恢复；若RR直接登记下一求值，则绕过既定生产SSI交接。

修订：material.decide可授同面后继、empty WAIT/RETRY、待NG报告后合法续问都保留点位需求/未知，直到原条件明确收口；已有面取料终局不重开，只有纯报告不占架。独立REPORT/RR仅存原报告交付事实，SSI生产再一次消费原异常触发、登记唯一阶段及下一求值，二者间崩溃仍从原NG/报告及未消费阶段发现候选。原触发消费和下一意图继续同事务，未把报告RR提交当成触发已消费。模型§7.1、O3步骤1a、S6步骤2a及A16/A26/A35/M08/M11补齐该交接。

### R12 · P1：原PUT已提交但尚未形成报告时，配置门禁不能吞掉恢复

上轮补了“已有report意图不按job.pending过滤”，仍需覆盖更早的窗口：PUT已提交，进程在消费成功/登记报告前退出，之后工作线被停用、删除或改绑。若原PLATE推进先检查当前配置再读原Result，已有REPORT扫描找不到消息，原成功也永远不再产生必报。

修订：原结果消费/必报登记先沿原task/plate/command及冻结依据恢复，不能因当前配置缺失被整体跳过；严格原成功才一次登记movement_report。当前配置门禁继续禁止新的取盘/PUT/prepare，既有报告与原结果消费不依赖当前方向/工作线仍有效。缺原合法事实仍留核对，不凭空补报。S6步骤2a、模型§7.1及A55/M11覆盖PUT前后提交、缺报告行、任务改向/配置删除与重复推进，沿现有PLATE/REPORT入口，不加新恢复表/任务。

### 反例矩阵及验收归属

此表是本轮文档推演与未来验收定位，**没有执行AUTO并发/重启测试**。T编号只用于阅读，不替代原72项API及M编号。

| 场景 | 必须保持的结果 | 原计划/验收定位 |
| --- | --- | --- |
| T01 全撤回提交后，原冻结求值仍未知，随后READY/NEXT/RETRY到达 | 不丢原请求/许可，不伪造取尽或提前NONE；原许可按实际执行条件收口，其他有效版本仍阻退架 | I1步骤1b；I02/M08 |
| T02 新同架成员与首次离场根竞争，叠加通知/Beat | 根前完整CONTINUE可撤暂定标记；根先提交则新成员不绑旧job，始终唯一原链 | I1步骤1a；B06/I02 |
| T03 全撤回与取盘报警恢复交错，盘在目标处又报警 | 无合法取离/交接结果前仍保护来源；成功应用且无潜在原处取料后才退架，目标处原命令/报告独立继续 | I1步骤1b；S7步骤1a～1b、I04/M06/M08 |
| T04 首版目标或B盘授权等待其自身ENTER/ROTATE | 有需求且无旧面直接责任时可准备；prepare前核对、原引用/Transport意图同提交，回滚无半个意图 | S2步骤2d；A46/M08 |
| T05 A盘旧面PUT许可/在途/报警，B盘要求换面或换架 | B不能挪走A目标，旧物理责任闭合后纯报告不占架；旧成功action不能证明新面/新架可PUT | O1步骤1b；A46/M06 |
| T06 REPLACE时旧离场WAIT/submit未知，新盘/worker重启 | 新目标授权保留，旧严格接纳后仅一份同点新job/ENTER；不等旧最终落地，旧迟到FAILED不回退新作业 | O1步骤1a～1b；B06/A46 |
| T07 RackSlot NG待报告/续问，对照末盘终局仅待报告 | 前者仍有原面潜在取料，后者不重开续问且纯报告不占架；不把NG/CLOSE/SOURCE_DONE当面终局 | O3步骤1a/O4；A16/A26/A48 |
| T08 报告RR提交后、SSI触发/续问提交前后分别崩溃 | RR不发新生产；未消费触发仍被Beat发现，SSI回滚无半个阶段，已提交未知只恢复原求值 | S6步骤2a；A26/A35/M11 |
| T09 PUT提交后、报告生成前重启，再停用/删线/改绑/新方向 | 原成功与必报沿原plate/owner恢复；新物理意图仍拒绝，报告/当前新任务不串 | S6步骤2a；A55/C01/M11 |
| T10 ECS原Result早于ACK、重复/迟到、超时或矛盾终态 | 只消费合法已应用原结果一次；未知/HELD及人工口头处理不解保护，既成FAILED不改SUCCESS，不延长原发送窗/换号重做 | S7步骤1a～1b；A27/I04/M06 |

## 5. 验证及实施剩余项

首轮静态核对：6份Markdown的457个本地链接/锚点有效、围栏成对；API的121段JSON示例逐段保持原文，API与总计划72项验收编号/顺序一致，M01～M12保留，未来实施步骤未勾选。git diff --check通过，暂存区未改变，改动仅为上述文档及本报告。sorting_task共享计划只补当前宿主引用和锁序，ST1/ST2及#49历史实施/验收记录不重写，不借共享软件测试替AUTO通过。

本次场景复查静态核对通过：原6份工作区文档中增量修订5份（共享sorting_task计划保持本轮开始时原文），没有新增路径或产品改动；467个本地链接/锚点及445处指向已审文档的引用锚点有效，121段JSON示例与本轮开始时逐段相同且均可解析，API/总计划72项编号和M01～M12完整，未来步骤未勾选。暂存内容及索引与本轮开始时一致，git diff --check通过。T01～T10仅为文档反例推演/未来验收要求，不计作实际并发、数据库、worker或设备验证。

后续实施仍按总计划S1→S2→I/O→S5→S6→S7：先完成AUTO来源/模板及真实事实适配，再正式装配和保留既有foundation数据的增量迁移；独占数据库的并发/升级保留验证、非eager worker/Beat恢复、真实WMS/ECS/RCS及现场物理证据分别记录。本轮没有实施或运行这些验证，也未授权提交、部署或真实发送。
