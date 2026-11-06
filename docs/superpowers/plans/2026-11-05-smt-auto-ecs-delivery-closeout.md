# SMT AUTO ECS 剩余交付实施计划

**阶段状态**　`feature/smt-auto-s1` 开发阶段已收尾。出库空取、NG 和复审修复已通过隔离回归。AUTO 已接入正式运行目录。供应商实报文、对端联调、实机签收和现场启用按下文验收。

**架构**　沿用 AUTO 宿主、原命令、公开结果读取、可靠交付与现有待办入口。机械臂动作由 ECS 执行，进架、换面、离场继续走 RackDispatch 和 WMS/RCS；业务只消费归属明确的原始结果。

**技术栈**　Python、Pydantic、SQLAlchemy、PostgreSQL、Redis、Celery、Alembic、pytest。

**规格**　[项目 AUTO API](../../integrations/smt-auto-api.md)、[ECS 公共命令合同](../../integrations/ecs-command.md)、[总计划 D1–D8](2026-10-31-smt-auto-implementation.md#delivery-dependencies)和用户提供的供应商《SMT 分拣机 ECS 接口调用说明书》。PDF 版本表为 1.0.0，日期 2026-03-18，页码按文件 1～42 计。PDF 描述接口及供应商示例，项目接口差异以双方签认附录为准。原命令身份、正文、路由、结果附录和期限在登记后保持冻结。

## 基线与签认范围

历史开发基线为 `feature/smt-auto-s1`，入库和正常出库已通过隔离回归。KT11（本地 ID 16）与 KT14（17）均已保存，版本 1，停用，五层架容量 1 为本地默认。数据库在 `smtautofoundation`，代码 head 为 `smtautosources`。当时正式 catalog 只有粗分和 SMT 手工。[既有软件与两线专项证据](../../testing/smt-auto.md)

| 供应商说明书证据 | 收尾要求 |
| --- | --- |
| 第 6–12、37–38 页给出 PICK_AND_PUT 正常路径、位置枚举及字段表。场景 5/6 的示例为 A 取、B 放；项目已确认两线实际布局为 **B 退料取、A 转运放** | 保留项目 KT11、KT14 映射。对端逐项核验 `params.source/target`、`rack_face` 与 PDF 的 `rack_side`、格位和尺寸的数字类型、`bin_type` 码值、`STATION_WORK1` 与枚举表拼法差异、成功回调里的 `barcode/scanned_at`，以及失败 `error_detail.code/msg` 与 PDF `error_code/error_message` 的差异。确认交付口径后冻结版本 |
| 第 12–13 页说明重复和参数异常也可能 ACK。第 18–20 页的 Result 才关联原 command_code/device_code、结果及毫秒完成时间。第 14–18 页的 Status 是观测 | 现场用已应用的原 Result 判定取放。用命令首次发送后的真实证据测试 ACK 丢失、Result 先到和重报。分别验证报警期间保持原未结束命令、设备重启后只恢复未完成阶段、最终 FAILED 后保留原责任。第 13–14 页取消接口及附录重试示例不构成本业务自动取消或重发授权 |
| 第 19 页仅列 `PUT_FINISHED/PUT_FAILED/PICK_FAILED`。第 23 页 `1002` 表示取料失败。第 18 页 `finish_time` 是命令完成时间 | 本方确认原取盘 Result 顶层 `FAILED`、`data.pick_and_put_result=PICK_FAILED`、`data.actual_qty=0` 即为空取。原 `finish_time` 作为 `observed_at`。供应商对齐 BIN Cell 和 RackSlot 的原报文及重报，验证超时、掉线和未知结果继续原身份恢复 |
| 第 6 页列出 `STATION_NG/SCAN_NG`。第 42 页 §9.6 确实画出出库扫码平台通向 NG 暂存区，但箭头未标执行臂。§9.7 只有移库标题。第 37–38 页未列 NG 命令必填字段 | 本方确认 KT11 用 ARM01、KT14 用 ARM03，BIN 与 RackSlot 共用本线料箱侧臂。ECS 对齐扫码台到 NG 两端的 `location_id`、`location_type` 及原成功 Result，现场确认放置，WMS 确认 `ng_locator` 区域 |
| 第 6 页的 `30000` 是命令超时示例。第 21–22 页的 `SCAN_COMPLETED` 是独立事件示例 | 逐线确认两臂 timeout；首版计划等待另行可选配置。签认取盘扫码是否随同一个原 Result 回传，核对完整条码和原扫码时间。设备若只提供独立事件，需先交付稳定的原盘/原命令关联及重启、迟到、重放证据 |

## 1. 后续联调验收

**责任**　ECS 供应商和现场集成提供原始报文与设备日志。WMS、RCS 负责人分别核对业务授权、库存入账、位置及搬运模板。WES 维护 [ECS 命令声明](../../../app/businesses/smt_auto/ecs_command.py)和对应合同测试。

- [ ] 四条证据链齐备后冻结附录版本。每条线的入库、BIN 出库、RackSlot 出库及失败样本均记录脱敏原请求、ACK、原 Result、工位、设备和固件版本。包含取盘成功后扫码失败的物理事实，按项目已定 `FAILED/1/PUT_FINISHED` 核对原附录与实机支持。对端若仍用 PDF 旧字段，先确认适配边界，保留已有冻结命令的历史校验入口。
- [ ] 验证正常路径与位置。KT11 使用 ARM01/ARM02，KT14 使用 ARM03/ARM04。核对入库单层 A/B、出库 B 退料取与 A 转运放，以及设备内部扫码台与 SCAN2 设备码的区分。现场见证 F01、CK04、CTU 与真实到位、面向、滚筒交接；严格原 DEPART 提交接纳单列为调度证据。
- [ ] 验证原身份恢复。覆盖 ACK 无最终结果、Result 早于 ACK、重复/矛盾回报、报警与人工处理、原命令内重启恢复、最终 FAILED。核对原 code/device/阶段、无额外取放、无提前报告或放箱。真实对端另验 WMS 同源同码授权、报告去重与库存入账原子性、完成确认与计划追加交错。

- [ ] **后续联调复验**　覆盖报告交接间重启、WAIT 到期与重复通知竞争、整事务回滚、身份漂移、旧结果晚到。未结异常阻止原箱释放和面/任务完成；来源架需求与物理交出分别核对，独立正常报告保持原解耦。复用现有事务和公开端口。
- [ ] 追加签认真实报文的回归，分别标记模拟和现场证据。

<a id="kt11-kt14-integration-checklist"></a>
### KT11 / KT14 联调留档清单

每条线覆盖 BIN Cell 空取、RackSlot 空取、BIN 来源 NG、RackSlot 来源 NG。KT11 的 BIN 取盘及 NG 用 ARM01，RackSlot 取盘用 ARM02。KT14 分别用 ARM03、ARM04。每例保留脱敏 HTTP 请求与响应、原命令号和 WMS 操作号、首次与重报时间、固件版本、实际动作次数和 WMS 入账次数。

| 待确认方与样本 | 通过标准 |
| --- | --- |
| ECS 原取盘命令、首次空取 Result、ACK 丢失与重启后的重报 | 原命令和设备匹配，原 Result 为 `FAILED/0/PICK_FAILED`。重报保持原命令号、设备号、`finish_time`、`data` 和失败详情。首次与同内容重报均收 HTTP 200、`{"code":200,"message":"ACK"}`。每个原 Result 只触发一次空取首问。确认补报触发、间隔和停止条件，并记录同命令矛盾 Result 的冲突处理。 |
| ECS 失败详情原报文 | 到达 WES 的 `error_detail` 采用两个字符串 `code/msg`，保留原厂码和原文。供应商 PDF 的 `error_code/error_message` 直接进入当前 WES 合同会收到 HTTP 400 `INVALID_ENVELOPE`。 |
| WMS 空取请求、决定和重放 | `outbound.source.empty_decide@v1` 的 `observed_at` 等于原取盘 Result 的 `finish_time`。BIN 带箱码和 Cell，RackSlot 带原架、面、Slot 及原成员版本。同号重放返回原完整决定。WAIT 到期续问，RETRY 只登记一次新取盘，SOURCE_DONE 只关闭原 Cell 或 Slot。无成功移动时报告数为零。 |
| WMS 原 REJECT、ECS NG 命令及原 Result | 按原 `ng_locator` 核对两线实际区域。本线料箱侧臂按 `STATION_SCAN/SCAN_PLATFORM → STATION_NG/SCAN_NG` 放置，原 NG 命令的 `SUCCESS/1/PUT_FINISHED` 确认完成。以现场见证核对同一盘实际到位。 |
| WMS NG 报告、响应丢失后的重报及续问 | `outbound.material.movement_report@v1` 保留原任务、来源和完整条码，`to_locator` 等于原 `ng_locator`，`occurred_at` 等于原 NG Result 的 `finish_time`。首次 `RECORDED`，同号同正文重报 `DUPLICATE`，沿用首次响应时间且只入账一次。WES 消费报告确认后只登记一次原箱或原架面续问。 |

完整报文示例见 [WMS 空取](../../integrations/smt-auto-api.md#outbound-empty)、[WMS NG 报告与续问](../../integrations/smt-auto-api.md#outbound-exception-example-1) 和 [ECS NG 结果](../../integrations/smt-auto-api.md#outbound-ng)。

## 2. 开发阶段收尾记录

**改动边界**　复用 `app/businesses/smt_auto/outbound.py`、`outbound_contracts.py`、已登记的 `outbound.source.empty_decide@v1`、`outbound.material.movement_report@v1`、原盘/求值模型与 `task_flow.py`、`rack_ports.py` 的证明入口。产品触发器使用上表确认的原 Result。对端报文样本和实机行为单列验收。

- [x] **空取**　写 RED 覆盖其他失败与未知结果保留原责任、原取料失败与观察时间精确匹配、BIN Cell 与 RackSlot 的原来源身份。消费后按原上下文登记 `empty_decide`，WAIT 固定原数据和到期点，RETRY 只创建一次新取盘，SOURCE_DONE 只关闭原 Cell/SLOT；无成功移动时不报 movement_report。下一盘从原箱或面合法重问并取得完整新授权。
- [x] **NG**　写 RED 覆盖原 REJECT 唯一执行、失败或 UNKNOWN 保留责任、原成功后只登记一次 NG 报告。按上表映射生成本盘 NG 命令和冻结参数，使用原成功 Result 的 `finish_time` 作为报告发生时间。报告经原请求确认并在 RR 消费后，由 SSI 待办登记唯一异常续问。BIN CONTINUE/CLOSE 和 RackSlot CLOSE 各按原 Cell/SLOT 处理，正常终局后不重开。
- [x] 隔离 PostgreSQL/Redis 组合回归 681 项通过，含原 Result 迟到冲突时拦截续问授权的新取盘。[验证范围](../../testing/smt-auto.md)

复审回归同时核对原 READY 在计划成员撤回后仍能按原货架和到位事实继续、可靠清理保留 AUTO 引用的原 WMS 结果、退料架原到位可跨停用和配置变化入账，以及停用或配置失配时拦截新的 DEPART。入库异常的原结果已经收口时，续问条件暂未满足则保留原盘待办。原异常 Result 随后进入待对账时，入库续问 READY（含 WAIT 后续轮次）在登记新取盘前重新校验原证据。

## 3. 后续交付环境配置复验

**可与合同签认并行。** 采用隔离 PostgreSQL/Redis、已保存的 KT11/KT14 配置和受控对端地址。实际本地库迁移、外部命令及现场启用各沿单独的操作授权执行。

- [ ] 从 `smtautofoundation` 升级副本到唯一 `smtautosources`，核对两线 ID、版本、停启状态、设备、路由、点位、容量与原公共历史。验证有新证据时受保护降级在改表前拒绝。记录升级前后与恢复演练证据。
- [ ] 填写 `SMT_AUTO_WORKLINES` 的本地线 ID、版本和单层 OUTxx→STATION_A/B 映射。启线前核对运行配置与数据库版本一致，左侧绑定到 STATION_A，右侧绑定到 STATION_B。每臂可选配执行超时，省略时发送 `timeout: 0`。Transport 调用方取数据库工作线 ID。首版等待仅采用已确认值。测试配置版本错误或缺失阻止新动作，旧原 Result 仍可消费。使用实际 API、worker、Beat 和正式组合代码，在隔离 catalog 中核对绑定、提交后唤醒、重启恢复及手工线回归。
- [ ] 运行 `rtk uv run pytest -q tests/tasks/test_smt_auto_worker.py tests/migrations/test_smt_auto_state.py tests/migrations/test_smt_auto_rack_sources.py`，显式配置测试 PostgreSQL、Redis 和独立迁移库。把本地已保存配置与历史隔离配置分开记入验收记录。

## 4. 正式准入和两线验收

- [ ] 正式 catalog 接入和部署在交付阶段单独评审。锁定交付构建、附录、WorkLine 配置与 migration 版本，核对三组件、共享注册、粗分及手工回归。
- [ ] 逐线签核实机结果。KT11、KT14 各至少记录正常入库、BIN/RackSlot 正常出库、权威空取、NG 报告及续问、原命令内故障恢复。验收表保存实际设备、工位、时间、原 command/operation ID、结果、报告和释放证据。真实 WMS/RCS 对接与物理动作分别记录，所有必要项闭合后再按授权启用工作线。
