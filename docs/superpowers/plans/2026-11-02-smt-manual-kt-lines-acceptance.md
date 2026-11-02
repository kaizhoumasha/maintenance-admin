# KT16 / KT18 SMT 手工配置验收计划

> **For agentic workers:** Use superpowers:subagent-driven-development for this acceptance-only task. No product implementation, commit or deployment is authorized by this plan.

**Goal:** 使用用户提供的北京时间2026-11-02 09:29配置快照，验收本项目当前实际代码，给出逐项PASS/FAIL/未验及可复现证据。

**Architecture:** 原正式catalog/runtime、receiver、SmtManualService、BIN_Roller、WmsRequests、Transport和Sender连接独占可丢弃PostgreSQL与loopback模拟端。冻结原快照并明确映射到本项目的数据模型，禁止把未消费字段存在于测试字典当作实现支持。

**Spec:** 本次用户完整配置快照（保存于本计划工作区source-profile.json）；既有人工API、SMT设计与BIN_Roller公开合同。源配置事实优先于旧快照中KT18停用等历史状态。

## Global Constraints

- KT16、KT18均启用，业务MANUAL；源运行模式AUTO不能变成AUTO任务。当前business_code=smt_manual、task_type=MANUAL，主验收任务方向为outbound；若补入库兼容场景另标为合成软件场景。
- 使用源设备/工作点编码及版本31/7。源大整数主键为审计引用，不是旧库数据迁移；若本项目自动生成本地主键，保存明确映射，不伪称复制成功。
- 每线6设备关联、仅SCAN1～4是命令角色；入/出料设备未列device_contracts；无需设备上下游链或扫码工作点。INLET/OUTLET源STATION按目标HANDOFF_POSITION合同验证实际请求，不按字面相同硬复制。
- capacity=3是每线CTU01请求队列并发上限，不是同时物理在位3架。至少4（建议5）个不同来源架验证先准入3、第四等待；CTU03有效submit接纳后补充恰好1个，不等最终物理Result；重复接纳不超额，两线额度独立。
- 原地址http://10.24.209.26:8080只作配置映射审计；一切实际I/O必须在意图冻结前绑定loopback模拟端，发送前断言全部target origin。不得改写已经冻结的intent/原请求/路由来重定向。
- third_party_integration@1.1、Status10秒、command30秒分别映射/核对；不能宣称本地mock证明真实供应商合同兼容。无本项目消费入口的字段明确写出，不新增假字段或模拟计数器凑支持。
- 产品源码、schema、迁移、模型收敛均只读。仅增加本轮验收harness/报告；保留全部暂存区、配置与既有未提交工作。无commit/push/merge/deploy/运行库操作或真实WMS/ECS/RCS调用。
- 验收允许FAIL；必须执行并保留失败证据，不xfail/skip失败条件。未覆盖写未验，不能用前次4933通过替代本次配置验收。所有pytest串行，独占资源仅本轮创建/清理。

## Review Focus

1. AUTO运行模式误映射为AUTO任务，或KT18误用旧停用事实。
2. 仅测试3个需求无法发现无并发上限；模拟peer自己限流会掩盖产品缺陷。
3. CTU03接纳/周期释放与最终物理结果混淆；新CTU01补位身份不唯一。
4. 六设备/四合同、交接位种类差异被照抄或增补虚构绑定后才通过。
5. 私网地址在冻结后改写、外部真实发送，或大ID/合同/状态有效期只有原字典值而未实际消费。

## Task AT1：运行配置驱动的实际代码验收

**Files:** 本计划工作区source-profile.json、test_kt_lines_acceptance.py、必要kt_profile_fixture.py及原始日志；创建docs/testing/smt-manual-kt16-kt18.md。生产代码只读，不改原通用tests/helpers.py。

**Consumes:** 实际catalog、原business_tasks._runtime(factory)、receiver、SmtManualService.scan_pending/advance、两组件公开接口、公开结果读口和原Sender。可复用tests/helpers.py的loopback_app/simulator_client_factory等装配，不复制业务算法。

**Produces:** 每项断言的真实结果、输入到本项目字段/模型的映射、原请求/命令/Transport及线别关联，容量缺口和未验边界；独立复核单一完整验收增量。

- [x] 保存完整源快照及映射，核对实际定义/配置模型，生产代码与暂存区基线留摘要。
- [x] 在本轮独占库迁移到实际head，用实际模型建立KT16/KT18配置、12关联设备和角色/交接位绑定；保留源地址→loopback测试地址的显式映射，意图创建前完成。
- [x] 通过实际receiver/runtime跑两线MANUAL出库、准入WAIT/到期/WORK_REQUIRED/完工/原释放、独立SCAN3/SCAN4、真实交接位及RETURN/TRANSFER位置；同箱码不串线，无上游链/扫码工作点也正常。
- [x] 用至少4个不同架实际产生CTU01，原Sender/Transport保存submit接纳；按capacity3及CTU03释放补位断言记录PASS/FAIL。缺配置入口与实际行为分别留证，模拟端不承担并发控制。
- [x] 串行运行全部验收harness，保留失败、请求/结果原身份、清理证明及报告。只读产品摘要保持一致，现有未提交/暂存工作保留；无产品改动不重复全仓。
- [x] 独立复核验收的输入覆盖、实际代码路径、阳性/阴性证明及结论；补齐证据问题，不擅自修产品能力缺口。

最终输出为真实验收结论；FAIL不意味着本任务未执行，报告必须直说不满足条件及未覆盖范围。

执行结果：12项专项11通过、1容量失败、0跳过；独立复核验收执行与证据Approved（2项Minor），实际业务验收FAIL。详见 `../../testing/smt-manual-kt16-kt18.md` 及W/AT1-review.md。执行完成不等于容量验收通过；产品未修订、未提交、未部署，专用资源已清理。
