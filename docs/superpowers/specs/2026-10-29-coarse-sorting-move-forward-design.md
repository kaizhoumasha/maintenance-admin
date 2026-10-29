# 粗分验证与首个 ECS Command / Result 设计

## 授权与范围

用户已确认业务闭环：SCAN_COMPLETED → MOCK WMS 验证 → MOVE_FORWARD → 原命令 Result → 等待箱格分配。继续当前功能分支，保留历史验收及本地文件；本轮不提交、不推送、不部署。

使用同一套正式业务装配；数据库、设备绑定和对端地址由部署配置决定，测试使用可丢弃存储及 MOCK 对端。没有专用 local 处理器、设备码前缀分流或私有数据库。正式注册只代表本阶段代码装配，不代表完整粗分流程或供应商/现场验收。

## 方案与取舍

沿用已有接收器、公开模型 API、Outbox/唯一 Sender、Result 接收器及 Celery。粗分自己的具体服务负责持久待办推进；既有 Celery Beat 调度一个粗分任务，不创建通用业务 worker、第二 Sender 或新调度系统。直接在扫码 HTTP 中等待 WMS/设备响应会破坏受理事务边界；只做命令模型无法完成用户选择的业务闭环，因此均不采用。

继续以现有扫码行为表承载本阶段的原验证请求、命令关联和消费证据；不预建完整粗分状态机、多动作表或补偿平台。扫码 ACK 后才由具体推进任务登记验证请求；ACK 与物理完成分开。

## 内部开发合同基线

- SCAN_COMPLETED 的业务数据包含原 barcode、reel_diameter、reel_thickness，沿用公共事件内容身份，保存原事件外层 timestamp 及原工作线、设备快照。
- WMS operation 使用草案 §3.1 的 inbound.coarse_sorting.validate_bind@v1 / decisions。严格请求包含 workline_code、原 barcode、reel_diameter、reel_thickness；包络 timestamp 为请求登记时间并随原请求冻结。不解析条码、不增加箱码字段。
- 仅原请求的 HTTP 200 / DECIDED、合法 OK 且非空 binding_id 允许下一动作。NG 保存原 reason_code，停在等待 NG 阶段；未知、拒绝、错误或非法响应保留原请求，不换号重求验证。
- WMS 验证返回的业务 NG 保存原决定与原因并停在 AWAITING_NG_STAGE，后续 MOVE_TO_NG 由 WES 下发，留待下一阶段实现。
- 首个命令为输送线角色 CONVEYOR 的 MOVE_FORWARD；INPUT_ARM 仍负责扫码。两角色都属于粗分 definition，按各线实际设备绑定，绝不从条码、输入设备码或设备名前缀推断输送线。
- 命令参数只有原 barcode。沿用既有附录对 SUCCESS/FAILED 共用 data 模型的机制，允许空 data 以接收草案中的 FAILED + error_detail；完成字段使用严格类型及允许值约束。业务消费 SUCCESS 时必须再次确认 actual_qty=1、location=STATION_PIPELINE1_OUTPUT1；缺少完成字段只保存原已应用证据并等待核对，不能推进或换命令重做。FAILED 使用现有严格 ErrorDetail。ECS 共用核心包络及框架结果校验机制保持原样。
- 设备附录内部基线为 coarse_sorting.move_forward / 1，timeout 采用 30000ms，允许范围 1–60000ms；可靠协议窗口沿用 PROTOCOL_RETRY_WINDOW=300 秒。位置常量和超时范围是本轮内部实现选择，供应商核对义务保留，后续差异通过版本化合同调整。
- 成功 Result 只证明本次原输送动作完成。保存原 binding_id、command_code 及消费结果后进入 AWAITING_ALLOCATION_STAGE，不请求箱格、不执行入箱或 NG 搬运。

## 持久化与事务

扫码表增加可空的验证 operation_id/message_id、已消费验证响应、binding_id、原输送线设备/版本关联、command_code 和已消费移动 Result。旧行保持原事实和待办，由同一推进方法处理；不保留两套调用方式或重复装配。

具体服务通过可信工厂注入 session factory。平台 Capabilities 增加该事务资源，统一迁移 API/测试调用；worker 使用任务自己的 factory，避免跨 Celery 事件循环复用连接池。工厂不保存活动 session、不发网络，并注入固定的已注册验证请求模型；具体服务不导入注册入口，避免先于 catalog 校验发生注册。

每次 advance 在新 session 的首条 SQL 前设置 PostgreSQL REPEATABLE READ；先定位原行，再按 WorkLine → 按 ID 排序的 Device → 扫码动作加锁、重读。核对原工作线/扫码绑定与输送线身份和版本；设备改绑、禁用、错业务等不能让旧动作迁线或切换设备。

1. 未登记验证：用公开 WMS send 登记冻结请求，同事务保存原请求引用及输送线快照。
2. 验证已登记：仅通过 WMS get_result 读取原引用。合法 OK 的消费、binding_id、公开 ECS create 的原 owner/call_key/command_code 与等待结果状态在同一事务提交。
3. 命令已登记：仅通过 ECS get_command/get_by_call 读取本业务原命令。无 Result、ACK 或 UNKNOWN/HELD 都不代表完成；匹配的已应用 SUCCESS 才保存消费证据并进入待分配。FAILED 保存原结果并等待核对，不换键重发。

序列化失败最多整事务重试 3 次，每次新 session、完整重读；耗尽仍保留原待办。其他存储错误回滚，不提交半份消费或下一命令。重新启动、重复任务、收据清理均使用原业务 ID 和已保存请求/命令身份。

粗分扫描任务使用有序分页遍历可推进待办，避免前一批未知或失败记录饿死后续记录；每个动作独立事务。任务只登记持久意图，由原应用 Sender 发送。Celery Beat 固定每 5 秒触发，分页 100；不增加配置项，延迟/规模需要变更时再依据实际数据调整。

## 涉及范围

- 粗分 definition/composition、WMS request 模块、ECS 命令模型、具体 service、原模型及新增 Alembic 迁移。
- Capabilities/runtime、主应用和具体 Celery 任务的可信 factory 装配；迁移现有测试调用，不留兼容参数。
- 隔离夹具、公开 HTTP/服务回归、契约冷检查、草案和调试指南。旧验收章节保留并标明本阶段增量。
- 无额外管理 API、权限、新依赖、真实对端调用或现有数据库升级。

## 验收

使用任务独占 PostgreSQL/Redis、真实事件与 Result 接收器、原模型 API 和 Sender 对 MOCK WMS/ECS 执行闭环。覆盖两工作线隔离、重报/重启/收据清理、只有合法 OK 才建唯一 MOVE_FORWARD、ACK 不推进、Result 先于 ACK、重复/错身份/非法 Result、NG/失败/未知、配置变动、事务失败/并发及 RR 序列化重试。Result 消费后待分配状态稳定重入；不产生 storage_allocate、PUT_TO_BIN 或真实设备义务。

迁移要求唯一 Alembic head、模型无漂移、空库升级及既有扫码记录保留；存在本阶段出站/消费依据时拒绝破坏性降级。全仓 Ruff、相关及全量 pytest、production/local 冷合同检查完成后独立复评。对端附录及现场物理动作继续作为独立待确认项。

## 设计独立检查

失败 Result 按既有框架能力允许空 data；业务成功消费仍严格要求数量和位置，不扩展通用框架。事务、身份恢复、正式装配及调度边界按本文执行。
