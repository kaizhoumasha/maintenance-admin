# SMT AUTO 完成确认资格修复

**目标：** 修复首版仅目标架、供箱已成功但箱尚未进入生产时的提前完成；只本地修改和验证，不提交、不部署、不修改运行数据。

**依据：** `docs/integrations/smt-auto-api.md` §3.5、§3.7，以及本次两个现场任务的原始记录。

**方案：** 复用原 TaskFlow 完成前置和响应复核。仅目标架且无来源/生产/撤销依据的首版保持等待；公共 SupplyProgress 补充原成功投放箱数，AUTO 按原任务汇总核对生产到箱数，不建立逐箱供箱映射。未知进度保持等待；无箱面结束、开始前合法撤销、原版本 0 等待期限保持原合同。无需迁移或新增外部字段。

**范围：** AUTO TaskFlow、BIN_Roller 公开供箱进度及其独立测试、AUTO 完成回归和相关接口文档。保留原工作区的所有其他修改。

- [x] 回归先红：仅目标架即使已到位也不确认；成功供箱但未到生产、部分到生产不确认。
- [x] 最小修复：公开原投放计数；发起和消费同一资格检查，保留原确认依据格式。
- [x] 正向与失败边界：全部箱完成原 NO_WORK/释放后可确认；零投箱面终局与未开始撤销可确认；未知进度和失效原响应均不关闭。
- [x] 验证：AUTO、BIN_Roller、手工共享组件回归，Ruff 与全仓 pytest；只使用本地独立测试资源。

基线：聚焦测试 34 passed / 19 skipped（未配置独占 PostgreSQL）。

审查调整：计数只作为发起/消费时的实时资格门禁，不改变保存的原确认依据结构，避免使已有合法确认和已完成任务归档永久失配。针对原保存依据补充恢复回归。

验证结果：

- 原始代码上，目标架首版及 1/2 箱供给无生产到箱均复现 `CONFIRMING` 而非预期 `WAITING`。
- 相关 PostgreSQL/Redis 配置回归：128 passed，包含真实组件分批投箱计数及手工共享供箱回归。使用本轮独占 PostgreSQL 17、Redis 8；AUTO 新场景沿既有 SQLite 夹具验证原 WMS/ECS 结果生命周期。
- 全仓：4309 passed / 2209 skipped，600.37 秒，exit 0。独占 Redis 已配置；未配置 PostgreSQL 的全仓用例按原规则跳过，不将此结果当作全量 PostgreSQL 验收。
- 全仓 Ruff、格式及 git diff 检查通过；独立只读审查完成，无遗留阻断项。
- 全仓初次未提供 TEST_REDIS_URL 时，一个现有粗分 lifespan 用例因 KeyError 报错；补齐独占 Redis 后该用例及完整套件通过，未改无关用例。
- 未提交、推送、部署或修改现场数据；现场历史终态不自动重开。

## 后续授权的联调热更新（2026-11-07）

用户随后明确要求热更新联调环境。基于原在运行的 `8bdd81ec0d11` amd64 固定镜像，仅覆盖本修复三个运行文件，生成 `enterprise-backend:integration-smt-completion-a3b0986ee994-amd64-01`，按未提交补丁如实记录 `source_dirty=true`；没有把本工作树其他提交整体部署到服务器。

303 个运行文件中恰好三处变更；API/worker/beat 的实际文件摘要与测试代码相同。应用四服务使用同一镜像，迁移入口无操作成功、head 保持 `coarserackstage3`，应用启动前所有项目表内容指纹不变。三处入口的 health/ready/docs 全部 200，客户端 WARP 地址验证通过，worker pong、Beat 单实例、启动错误标记为零。PostgreSQL、Redis、旧 WES 和共享配置保持原状。

首次因 Nginx 刚启动尚未监听触发自动回退，已验证旧入口正常；第二次增加入口就绪等待后切换成功。原镜像、停写备份、完整补丁和回退脚本均保留。记录：`/srv/enterprise-backend/records/integration-smt-completion-a3b0986ee994-amd64-01/`。未注入真实业务测试任务、补发设备指令、清理队列或重开历史任务。

## 后续统一 AUTO / MANUAL 完成资格（2026-11-07，本地未部署）

用户明确：两者都核对已供箱但尚未进入生产的责任；生产收口且原放行意图可靠保存即可确认，不等尾箱 Result；共同判断提取复用。

范围：BIN_Roller 公共完成检查、AUTO/MANUAL 任务流及 MANUAL 原放行证据读口、相关回归与合同说明。保留各方向生产终局与原目标架规则；不改外部协议、表结构、队列或现场数据。

1. 两类回归先红：MANUAL 实际供箱后没有/只有部分生产时不得确认；AUTO 已生产收口并保存原命令、不等 Result 即可确认。
2. 共用 `bin_roller.completion.supply_accounted_for` 和 `release_intent`，按原任务核对已投箱数及原 SCAN2/任务/线/设备/箱/扫码时刻/唯一命令；生产终局仍归各宿主。
3. AUTO 保存稳定的原放行命令作为新确认依据，已有 Result 摘要形式沿原证据兼容；不因迟到 Result 改变确认依据。
4. 原释放恢复保持严格 SUCCESS/SUCCEEDED；终态后未决/失败释放继续 pending，不可被新任务或归档消除。明细保留期从最后原释放结果消费重新计时。
5. 回归包括 1/2 箱供给、部分进入生产、尾 Result 在确认前/后到达、失败原 Result 仍保留原责任、错误原关联拒绝、旧确认恢复；使用独占本地 PostgreSQL/Redis，另跑全仓非 PostgreSQL 变体及 Ruff。

首轮新回归：5 failed，分别复现 MANUAL 供箱未生产提前确认与 AUTO 等待尾箱 Result；修复后初次 28 passed，两个组合夹具缺少 ECS 能力的失败已修正为显式组合既有真实 ECS/Transport。扩大回归识别四个原来断言“放行失败不得确认”的旧合同，已按新原则调整，继续严格验证原物理恢复不伪造成功。

独立复核发现并已修复两项边界：取消归档同样需要严格原放行结果，不能因共用生产资格放宽而让 None 进入保留期比较；晚 Result 消费不新增任务表写入，保持既有锁序。正常/取消归档共用 `_released_bins`，正常归档在既有完整锁集内读取最晚原释放消费时间，不增加倒序锁。

最终双分支保留期回归及既有保留期用例：3 passed / 25 deselected。独立复核最终增量无遗留阻断项；扩大验证数字待后续记录。

最终 PostgreSQL/Redis 有界回归：**584 passed / 0 skipped**，163.97 秒，exit 0。范围为全部 AUTO 用例，以及 MANUAL 的 supply_completion/task_completion/completion_notifications/confirmation_races/direct_pick/rack_reports、BIN_Roller feed/cancellation。使用独占本地 PostgreSQL 17 和 Redis 8；AUTO 既有 SQLite 夹具仍按其原设计运行，不把该组合结果宣称为全部 AUTO PostgreSQL 变体。

最终全仓回归：**4316 passed / 2214 skipped**，621.03 秒，exit 0。全仓配置本轮独占 Redis，未配置 PostgreSQL 的变体按项目规则跳过；相关有界 PostgreSQL 场景由上述 584 项覆盖。本轮最终 Ruff check、792 文件 format check、git diff --check 均通过。未提交、未推送、未部署，未修改联调数据；实际 WMS/ECS 和设备验收不包含在本轮本地证据中。

测试资源使用本轮独立 `codex-smt-unify-pg`、`codex-smt-unify-redis`、`codex-smt-unify-full-redis`，与其他任务/开发环境隔离；验证结束后仅清理这三者及其专属匿名卷。完整测试日志保留于本机 `/tmp/smt-unify-final-pg.log`、`/tmp/smt-unify-final-full.log`。

## 更新远程 develop 基线（2026-11-07）

按用户要求，将本 worktree 的 feature/smt-completion-readiness 从 1338e255 快进到远程 develop 的 8326452bd3e01fcb81b64fb3d953bd127b64c5fe（PR #64，ECS/WMS 历史证据统一）。没有创建本地业务提交，原修改仍为未提交状态；其他 worktree 和本地 develop 指针未改。

更新前完整保存19个本地文件、二进制补丁、文件摘要和原stash清单，备份目录 `/tmp/smt-completion-before-develop-dzoanequ`，恢复stash `91589019494fc3ba34f50f68227fe5caacf1ebe7` 保留。上游与本轮修改重合5个测试文件；14个未被上游改动的本地文件逐项摘要保持一致。

解开两处测试冲突：AUTO命令唯一性采用新的 IntegrationExchange，保留不等Result确认的断言；MANUAL供箱响应采用新的 update_message_bodies，保留1/2箱清单和运输结果。另将新增的“缺失原供箱响应”回归改用 set_message_bodies，不再写已删除的 ReliableMessage.response_raw。无生产规则改动或迁移改写。

基线更新后验证：独立 PostgreSQL/Redis 相关回归 **590 passed / 0 skipped**，205.29秒，exit 0；先行未配置PostgreSQL的小组 **123 passed / 49 skipped**（与590项重叠，不相加）。Ruff check、819文件format check、diff check均通过。远程 develop 经 ls-remote 再核对仍为8326452，本分支与origin/develop ahead/behind均0，索引无暂存项及未解冲突。此次未重新跑全仓测试，前文全仓数字对应更新前基线，不作为新基线全仓通过证明。未推送或部署。

本轮仅清理独占 `codex-smt-baseline-pg`、`codex-smt-baseline-redis` 及其专属匿名卷；恢复stash和更新前补丁/文件备份继续保留。日志：`/tmp/smt-baseline-regression.log`。

## Review / Ship 交付范围（2026-11-07）

用户明确调用 review 和 ship，本轮允许提交、推送及创建面向 develop 的 PR，仍不部署。前述各阶段“不提交”和测试数字保留为历史记录；最终交付以本轮 PR 的验证证据为准。按仓库 Git Flow，feature 分支保持现有 0.2.0，更新 Unreleased，不创建 VERSION 或修改发布版本。

覆盖审计补充共享检查的多来源累计、未知计数及缺原到箱/命令证据回归。对抗审查另发现：新计数依赖已关闭供箱面的原清单，而旧保留引用只保护未关闭面；旧消息先过期时会令计数变为未知并持续阻断。真实过期消息清理回归先红（1 failed / 1 skipped），补齐 SUPPLY 检查点对原 feed_message_id 的引用后专项 18 passed / 17 skipped；原检查点删除后解除保护，不改变保留天数或原架门禁。

本轮计划在六个独立运行库、迁移库和 Redis DB 上执行完整分道 pytest 并逐 node 对账，附 JUnit、退出码和内容指纹；Ruff 与独立审查同步复核。新日志保留于仓外证据目录，最终结果写入 PR，避免把旧基线全仓结果当本轮证明。没有新增网页或 UI 验收步骤，设备实机验证与部署不在本轮范围。

首轮完整分道还发现旧手工目标架取消矩阵的4个断言依赖“供箱已完成但没有生产也能确认”。已在独占诊断库复现，随后为该矩阵补齐真实 SCAN2、NO_WORK 和唯一原放行意图，保留目标架取消仍阻断的原断言；生产代码未变。ECS/Transport 组合提取为 manual_production_supply 夹具，与多箱供给回归共用。首轮日志作为诊断保留，最终绿色证明来自更新后全部分道重新执行，不以旧快照的部分通过替代。

## 最终交付验证（2026-11-07）

最终源代码的六道完整回归全部 exit 0：**6689 passed / 150 skipped**。收集与报告均为 **6839** 项，逐 node 对账的 missing、extra、duplicates 均为 **0**；不与前文聚焦或历史全仓数字相加。最终结果及六道日志、JUnit、退出码保存在 `/tmp/smt-completion-ship-f7nwfljd/final/`。未改动的 HTTP 50ms 用例在独立 18 项回归及最终完整套件均通过，未延长超时或新增跳过。

最终 Ruff check、820 文件 format check（禁用缓存）、差异检查均通过。原生独立审查最终无未决项；外部 Claude CLI 未安装，外部审查覆盖缺失，不记为通过。本轮仅文档同步后复核链接与全仓 Ruff/格式/差异，不重复执行测试套件；执行源代码与上述最终六道证据一致。

代码与合同已按本轮授权提交并推送，文档同步沿同一 feature 分支交付，面向 develop 创建 PR；版本保持 0.2.0。本轮统一完成资格增量未部署，未访问服务器或设备、未修改联调/现场数据；此前三文件热更新仅代表上文历史阶段，不代表当前统一增量已部署。WMS/ECS 实际报文、实机动作及现场验收仍待后续独立留证。
