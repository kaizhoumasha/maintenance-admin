# 设备、工作线与 ECS 按需状态查询

设备目录是当前正式 ECS 路由的地址来源。工作线维护逻辑分组及设备/位置角色绑定；每台设备最多归属一条工作线，也可未分组。管理入口全部要求 `admin` 角色成员身份及对应 `asset:*` 操作权限；普通用户即使误获这些权限，也不能访问管理接口或看到设备管理菜单。通用 `GET /api/v1/ecs/status` 仍独立使用 `ecs:status:query`。

## 初始化与使用

部署时按既有流程执行 `uv run alembic upgrade head`，增加三张配置表：`work_lines`、`devices`、`device_routes`，不建状态观测表。基础表revision为`11e090516bf1`；本轮按用户确认的未入库、未发布前提直接精简该迁移，删除观测表DDL，不新增先建后删的revision；后续`c31f20261027`给工作线增加device_bindings/position_bindings，已有线默认为空对象；存在非空绑定时拒绝删除绑定字段的降级。已有设备或路由历史时，降级会拒绝删除这些表。

通过既有权限初始化流程运行 `scripts.seed_admin`，补齐权限及 `asset/device/index`、`asset/workline/index` 标准菜单。已有菜单不覆盖；普通角色不自动获得权限。本仓库只提供后台接口和菜单定义，未包含前端页面。

1. 管理员调用 `POST /api/v1/devices/discovery/preview`，提交 `ecs_origin`。服务复用[通用 Status](ecs-status.md)，查询一次后返回 `NEW`、`EXISTING`、`ADDRESS_CONFLICT` 分类。预览仅在Redis保存300秒确认快照，不修改当前配置，也不作为详情状态缓存。
2. 调用 `POST /api/v1/devices/discovery/register`，提交本人的 `preview_token`、选中的 `device_codes` 和可选 `work_line_id`。只允许确认预览中的 `NEW` 条目；设备、独立路由及审计一起提交，不复制状态、不投递任务。未选中的设备不自动登记。
3. 用 `PUT /devices/{id}` 维护显示名称、说明、排序；用 `POST /devices/assignments` 提交设备 ID/version 列表和目标工作线（`null` 表示未分组），整批成功或整批回滚。编码和 ECS 状态不手改。
4. 当前同编码出现在另一 ECS 时，先获取该 ECS 的冲突预览，再调用 `POST /devices/{id}/route-migrations`，明确给出 `version`、`expected_origin`、`preview_token`、`reason`、`confirmed=true`。后台不会覆盖地址。
5. 配置错误时，调用 `DELETE /devices/{id}?version=...` 删除当前配置。旧命令、结果、证据、审计和独立原路由保留；再登记必须重新预览，生成新配置 ID 和新路由 ID。不存在归档、恢复、回收站或物理删除历史的接口。空工作线才可删除。

完整 API 输入输出与权限表见[设计 §8](../superpowers/specs/2026-10-27-device-workline-status-design.md#8-api-设计)。所有写模型拒绝额外字段；版本冲突返回 409，旧 ID 返回 404。预览缓存不存在或不属于本人统一返回 404；仍可读取但已超过绝对期限的预览返回 409 `PREVIEW_EXPIRED`。Redis TTL 到期已清除的 token 无法区别于不存在，返回 404。旧 NEW token 在登记、删除之后重放返回 409 `PREVIEW_CHANGED`；有效事务回滚后可重试。

## 配置与按需状态

`GET /api/v1/devices`与`GET /api/v1/devices/{id}`仅返回配置，管理写接口也返回相同DeviceRead：id、device_code、display_name、work_line_id、description、sort_order、current_route_id、version、created_at、updated_at、ecs_origin。无observation/freshness/time_anomaly字段；status/freshness筛选返回400，配置筛选/分页保持。配置读取不请求ECS。

详情页先GET配置，再在拥有`asset:device:refresh`时调用现有`POST /api/v1/devices/{id}/refresh`；按钮也调用同一接口，不新增GET状态入口、不轮询。GET配置需要admin+read，刷新需要admin+refresh；read不隐含refresh。刷新响应为：

```json
{
  "device_id": 1,
  "device_version": 1,
  "current_route_id": "01900000-0000-7000-8000-000000000001",
  "result": {
    "capability_name": "ecs.device.status@v1",
    "ecs_origin": "http://127.0.0.1:8001",
    "requested_device_code": "D1",
    "fetched_at": "2026-10-27T00:00:00Z",
    "devices": []
  }
}
```

result直接复用通用StatusQueryResult；设备描述、状态、ECS updated_at只存在于当次结果。无applied字段、状态落库、新鲜度或最后成功回退。200空数组表示本次未找到，不删除配置，不推断OFFLINE；对端404映射为安全502，与本地配置404区分。查询失败不影响独立配置GET。HTTP/合同错误502、超时504，诊断不含原始响应或凭据。

预览、详情状态和手动刷新共享规范化origin的Redis租约15秒、冷却5秒及限流期限；租约冲突409 QUERY_IN_PROGRESS，等待期429+Retry-After。上游429至少等待30秒，合法更长等待不截短。单次HTTP预算5秒、响应256KiB，复用阶段A严格合同及有界关闭，不重试、不改址；并非整个管理API的5秒SLA。

HTTP在数据库事务外执行；返回成功或上游失败前复核原配置ID、version、路由和Redis代次。原ID删除404；版本/路由改变（含A→B→A）409 DEVICE_CONFIG_CHANGED；被新代次替代或键丢失409 QUERY_SUPERSEDED。不能按编码接管重登记的新配置，查询不升管理版本、不写状态或业务审计。迟到429仍延长origin等待，但不替换/释放新租约。

Redis仅保存预览与协调数据，不保存详情状态。不可用503；近期重启保留30秒保护。取消数据库last_attempt恢复，键/数据丢失后不能重建旧冷却或Retry-After，不承诺跨Redis数据丢失维持原等待。删除配置事务不依赖协调缓存，HTTP仍使用原认证和全局限流。

没有设备状态Celery任务、Beat扫描或登记/迁移后的自动刷新。项目其他任务与命令Sender保留。当前迁移链为a025a20261025 → 11e090516bf1 → c31f20261027；全新升级最终无观测表。未对既有业务库执行迁移。若其他环境曾应用旧版设备迁移，不能仅替换同revision文件后运行upgrade：须单独新增前向移除方案，先退出所有旧设备任务/API消费者，再删除观测表；不清共享队列、不降级整段历史表。旧状态快照不属于执行历史，也不可从删表回退中恢复。

## 正式命令接缝

工作线通过PUT `/api/v1/work-lines/{id}`维护`device_bindings`（角色→device_code）和`position_bindings`（位置角色→位置编码）。设备必须已登记且属于该线，同设备不能兼任多个角色；设备移出/转移/删除前须先解绑。省略这两个字段的资料更新保持原绑定，显式`{}`才清空。工作线version和事务审计继续使用原机制。

正式启动装配一份角色任务声明和严格附录：

```python
from app.integrations.ecs_command import CommandBinding, configure_ecs
from app.services.device_routes import DeviceRoutes

# annexes由使用方提供已确认的DeviceAnnex；ScanPayload是相应的源模型。
bindings = [CommandBinding("SCAN1", "MOVE_FORWARD", "scanner", "1", ScanPayload)]
routes = DeviceRoutes(bindings, annexes=annexes)
configure_ecs(app, routes=routes, annexes=annexes)
```

重复角色/task声明、缺少附录在装配时失败，不维护逐设备合同副本。业务可用`app.services.device_bindings.resolve_role(session, line_id, role)`取得并锁定预期设备，再调用原EcsCommands.create；事件和路由共用resolve_binding，按工作线→设备取锁和刷新复核。Status的supported_commands不授予业务执行许可。

发送门禁复用冻结route_id和version中的配置摘要。设备/工作线管理版本变化（含资料编辑）、改址、解绑或删除后，旧命令停止后续发送并沿用HELD/AUTHORIZATION_REVOKED；恢复原值、重启和同键查询都不重新发送。旧正式布局1无法证明原管理版本，保持UNKNOWN暂停，不补猜版本。Status查询不升管理版本，不导致停发。

执行记录、原请求/地址/附录版本及结果证据继续保留；不维护historical_bindings延续旧发送许可。仅因正式目录配置失效暂停且有可能交付/ACK证据的命令，合法迟到Result仍用原附录接收并结算，不要求当前设备存在、不驱动新角色业务。原附录模型应通过可信启动保留（可复用history_annexes）。无派发依据或原身份/提前结果封锁不因此放宽。停止本地发送不代表设备已取消。

local 通过 `configure_local_ecs(routes=DeviceRoutes(...))` 装配，集成层 `LocalEcsRoutes` 组合正式目录与受保护的dev v3调试，共用一个ECS通道和Sender。`DeviceRoutes`只实现工作线/数据库适配，复用独立的`RoleRoutes`及其UUIDv7路由校验，接入见[角色框架](ecs-roles.md)。非local不传入本地调试路由；正式项目仍通过可信启动显式`configure_ecs`，不覆盖既有装配，不从dev宽合同推断正式附录。未新增发令HTTP接口。

## 本地验证与交付边界

用本地 loopback Mock 验证完整 HTTP 发现、登记、迁移、删除重登记；用独立 PostgreSQL/Redis 验证同编码竞争、预览重放、事务审计回滚、协调租约和限流。核心用例位于 `tests/services/test_device_*.py`、`tests/controllers/test_device*.py`、`tests/integrations/ecs_command/test_device_directory.py`、`tests/migrations/test_device_workline.py`。

本地开发验证与供应商/现场验收分开。真实 ECS/WMS、供应商字段/时间语义、现场规模和预算、Redis 持久化、生产严格附录及前端页面均未在本阶段验收；不将只读状态成功解释成设备或业务完成。

登记/迁移取得数据库锁后及提交前均再次核对预览绝对期限；等待锁或审计期间过期时，配置、路由和审计一起回滚。旧NEW token不能跨登记/删除重放；新预览允许重新登记。


### 工作线业务选择

`GET /api/v1/businesses`供工作线表单读取已部署业务的编码、名称、初始触发说明和设备角色／位置声明（key、display_name、required）。要求管理员及`asset:workline:read`，不返回工厂、模型或源码路径；它与local集成注册目录用途不同。

新建工作线必须传入已部署的`business_code`，例如先从业务选项选择编码，再以`{"line_code":"L1","line_name":"一线","business_code":"<所选编码>"}`创建。旧创建请求缺少此字段会失败。当前正式业务清单为空，尚不能新建工作线；在完整业务包确认部署前，不发布要求新建必选业务的版本。

迁移后的已有工作线保留`business_code: null`，仍可读取和维护。管理员可在原PUT入口首次明确选择已部署业务；已选后同值更新允许，换值或显式清空返回409。更新省略`business_code`保持原选择。列表支持该字段的`eq`、`in`、`is_null`和`not_null`过滤，选择与绑定共用原version和事务审计。

选择业务时会校验现有和本次提交的绑定。业务线的设备角色、位置key必须属于当前业务声明；缺少必需项允许暂存。跨线或重复设备继续拒绝。旧null线保留原绑定规则。位置字符串仅为配置，不能证明现场位置可用。

`business_configuration_issue(session, binding, business_code, definition_lookup=...)`在角色解析器已按工作线→设备顺序加锁的同一事务内，按`DeviceBinding`重新读取当前工作线且不重取工作线锁。工作线缺失、版本或绑定变化抛出冲突；业务不符返回`WORKLINE_BUSINESS_MISMATCH`，声明不存在、未知key或必需绑定缺失返回`WORKLINE_CONFIGURATION_INCOMPLETE`。数据库故障继续抛出。返回无问题仅说明声明配置完整，业务许可、具体位置合同、物理状态和命令发送门禁仍需原业务入口分别验证。


业务包开发与两线接缝验收见[业务包开发指南](business-packages.md)。配置保存、当前可执行许可和物理完成
分别判断；Status/ACK/工厂构造成功不表示业务闭环。受理先提交原收据和本业务持久待办，结果消费由
本业务独立 REPEATABLE READ 事务推进；重复/丢失通知通过原执行身份和待办恢复，不自动重发物理动作。
