# SMT 手工 M2 软件验收

核对日期：2026-11-01（America/Los_Angeles）。在 `feature/smt-manual-m1` 隔离工作区、已验收但未提交的[M1](smt-manual-m1.md)基础上继续。**M2实现、任务规格/质量复核、整体评审和文档修复定向复核全部完成，最终全量4778 passed/52 skipped通过，M2软件验收完成。** 不暂存、不提交、不推送、不部署，不访问运行库或真实WMS/ECS/RCS。

当前交付状态请以[SMT 手工统一验收](smt-manual.md)为准；本页保留本阶段当时的数字、范围及未验项，后续 M3～M6 已按新授权继续实施。

## 1. 范围与合同

- 两方向使用同一 `manual_bin.work_admission_decide@v1` 和 `manual_bin.work_completed@v1`；原任务/工作线来自M1。
- BIN_Roller保存原SCAN2到位；人工业务保存原arrival、实际箱码、raw、首次scanned_at及原task。准入请求data仅task_id/bin_code/scanned_at。
- WORK_REQUIRED核对原任务后等待，NO_WORK登记原释放；WAIT从首次可靠保存决定的本地时间固定到期，到期才创建一次新求值，原task/bin/scanned_at不变。未知、503及畸形响应不能新建求值或放行。
- 完工ACK只证明原通知已保存，独立应用先判重再唯一匹配原WORK_REQUIRED；NORMAL/NG均经组件release_scan2登记唯一原命令。首次独立应用时早到、无匹配、歧义和冲突保留待核对，不自动补绑。
- 原收据、业务事实、已消费证据、释放决定及组件原命令保持事务原子性；结果只通过公开读口消费。

本轮交付真实flow/Production端口及隔离组合验证。M3生产架、M4整单确认及新箱暂缓、M5正式装配/状态迁移、M6真实worker和现场验收保持未实施；不重复声明SCAN2事件或方向命令。

## 2. 匹配与生命周期裁定

原回调身份始终是(operation, operation_id)，保留冻结正文和首次时间。应用还先查已消费的四个有效字段(task_id/bin_code/result/completed_at)，相同内容沿首次原经过或待核对处置；这仅为本地防重，不另造协议身份或外部经过字段。completed_at不参与按时间挑选经过，后来准入不重新绑定首次应用已判定为待核对的早到证据。匹配门槛取首次独立应用时的原上下文；收据先到、首次应用晚于准入不额外按收据时间拒绝，也不新增收据时资格快照。

当前API只允许唯一匹配仍等待完成的WORK_REQUIRED原经过；已释放作业留证但不作为新的开放候选，不增SCAN3到位或放行Result门槛。多个仍等待的同箱原经过保持待核对；旧消息/相同有效内容优先关联旧证据。

技术重试必须保持原operation/operation_id、timestamp和冻结有效字段。若对端换新身份并改写旧通知的result或completed_at，合同会将其视为不同业务内容；同箱存在唯一新待完成作业时，当前wire没有外部经过选择器可区分这种错误改写与合法新完工。本轮不以时间猜测或增加wire字段弥补，对端保留原重试身份/正文是集成前置。

## 3. 验证记录

使用仅本轮创建的PostgreSQL16/Redis7；业务及组件表用独立MetaData创建并finally清理。实际收发/组件路径在隔离catalog装配，不增加正式生产宿主。

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| M2前依赖基线 | 102 passed / 0 skipped，13.80秒，退出0 | M1及组件test_scan2_port/test_scan2_release |
| M2功能RED/GREEN | 最终专项62 passed，12.49秒 | 原准入、WAIT、完工及释放；保留功能RED日志 |
| PostgreSQL事务与竞争 | 强制重叠快照专项2 passed，0.86秒 | 外层回滚、到期竞争、最多3次全新会话整事务重试及原命令原子性 |
| 受影响回归 | 262 passed / 4 skipped，35.52秒，退出0 | M1/M2、真实组件与receiver；4项为既有SQLite行锁限制，PG变体实际运行 |
| 独立评审与修复复核 | 全部通过；无未关闭问题 | 任务评审通过，整体仅1项P3状态文档，单轮修复及定向复核已关闭 |
| 最终全量 | 4778 passed / 52 skipped，455.48秒，退出0 | M1+M2最终完整代码；无失败、无警告 |
| 静态与迁移 | Ruff check、603文件format check通过；唯一head a39a1988a81a | M2未新增迁移，不代表运行库已升级 |

最终全量前后520份产品、测试、迁移及配置输入逐文件一致，摘要为 `7888ba1252a0cf9eb20651e34f3db02c6e132f637869e85f1132d0cb4964eb1d`；全量运行期间及之后只更新状态文档，未改产品/测试输入。52项跳过沿既有SQLite行锁/独立连接/根SAVEPOINT限制及需额外独占库的迁移项逐项记录，不计为通过；M2的PostgreSQL竞争与回滚用例实际运行。本轮未新增跳过项。

实现、任务复核、整体评审和修复复核分别见忽略目录内 `task-M2-report.md`、`task-M2-review-v1.md`、`m2-whole-review.md`、`m2-final-wave-review.md`；完整测试日志为 `m2-final-full.log`。原始报告、日志、未提交快照和控制裁定保存在本计划忽略目录 `.superpowers/sdd/2026-10-31-smt-manual-implementation/`，不把忽略目录当作已提交交付。

本轮专用PostgreSQL/Redis测试容器在最终检查后清理；隔离工作区、M1/M2未提交源码及账本/原始证据保留。不清理其他运行资源，也不删除未提交证据。

## 4. 尚未验收

正式API/worker catalog、组件和宿主表迁移、整单完成/确认期间新箱处理、出库生产架及实际worker/Beat/两线恢复仍需后续阶段；不以隔离调用或ACK证明物理完成。WMS/ECS/RCS签认、真实发送、运行库迁移、部署和现场动作均未执行。

相关要求：[人工线API](../integrations/manual-picking-api.md#manual-admission)、[设计§5](../superpowers/specs/2026-10-31-smt-manual-business-design.md#5-人工箱级接口)、[实施计划M2](../superpowers/plans/2026-10-31-smt-manual-implementation.md#smt-manual-m2)。

## 5. 本轮执行裁定

| 裁定 | 理由及判断错误时的代价 |
| --- | --- |
| 只接续M2，不重做M1或扩展M3～M6 | 用户明确本阶段；后续正式交付仍需继续实施 |
| 保留现有隔离worktree和未提交M1 | 不能用空HEAD差异遗漏实现；须保留完整未提交快照/账本，不能依靠Git历史恢复 |
| 先核对原通知身份和已消费四字段内容 | 保留原收据/正文/首次时间，不新增协议身份或去重系统；同样内容不能改绑后来的经过 |
| 新完工只匹配仍等待完成的原WORK_REQUIRED | 已释放作业保留原履约，不增加SCAN3或旧放行Result门槛；多候选保持待核对 |
| 以首次独立应用判断早到，不新增收据时资格快照 | API明确先受理后应用；首次待核对不因后来准入重开，收据先到但首次应用时已合法的通知仍可核对 |
| 对端改号并改写有效内容不做时间推断 | 当前wire没有经过选择器；对端原身份/正文纪律必须单独签认，软件不能识别所有错误改写 |

M1阶段的必要平台启停迁移、非消费者409和保守UNKNOWN裁定仍见[M1验收§6](smt-manual-m1.md#6-本轮执行裁定)，不将本次M2视为正式停机门禁或权威无接续交付。
