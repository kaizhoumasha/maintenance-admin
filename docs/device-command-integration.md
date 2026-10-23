# ECS 与 Transport 接入开发指南

本指南面向基于本模板开发业务的人员。ECS 按现场配置的设备地址下发命令，WMS Transport 使用部署时配置的固定地址。下文分别说明这两种接法，以及本模板尚不支持的部分。

## 先做什么

ECS 被动态路由、地址快照和客户端管理阻塞，任务见 E1/E2/E3。Transport 的出站适配器（T1）、回调接收与「收到回调即结束发送」（T3）已在 `app/integrations/wms_transport/` 实现，重试预算（T2）只有机制、是否启用待联调确认，业务结果消费（T4）尚未做；真实发送还须配置 `RELIABLE_TARGETS`，默认不发。初级工程师先做下面的任务，不自行改这些架构约定。

| 顺序 | 交付文件与任务 | 验收条件 |
|---|---|---|
| 1 | 按[第一个可靠投递实验](reliable-transport-lab.md)创建 `tests/reliable/test_transport_submit_example.py` | 复制完整样例后跑出 `23 passed`；看到事务提交、发送、ACK 分类、`DONE` 和业务回滚，网络连接被测试禁止 |
| 2 | 前置：技术负责人交付并确认完整 Transport 合同；之后对照合同核对已有的 `app/integrations/wms_transport/adapter.py` 与 `tests/integrations/wms_transport/test_adapter.py`，补合同差异用例 | 完成 T1 的合同核对：§5.3 的矩阵与「合同待确认项」逐条有结论；适配器没有数据库和网络副作用；合同未交付时只做第 1 项实验，不自行补猜 |
| 3 | 在业务项目的 `docs/integrations/ecs-route-cases.md` 整理现场设备路由与 A→B 用例 | 覆盖 §4 的新旧命令、binding 生效、地址不可达和重复入口；由 E1 负责人评审，不先创造生产接口 |

上表是建议的任务分工，不是已经指定了某位负责人。技术负责人确认 T/E 任务的设计和实施范围后，初级工程师再补生产代码。现有 `wes_backend` DeviceCommand/TransportTask 保持各自的派发流程。

### 全链路与归属

```text
[业务项目] controller → service：校验输入、确定业务身份
                         |
                         +→ ECS：现场设备目录 → 已生效 binding → 命令地址/正文快照
                         |       → 业务派发器 → 按地址复用客户端 → ECS
                         |       当前模板没有这条动态可靠派发链
                         |
                         +→ 静态目标：同一个业务数据库事务
                             保存业务记录 + [模板] Outbox.enqueue(MessageDraft)
                                               |
                                             提交后
                                               v
                                  [模板] Sender → MessageStore 租约领取
                                               → [业务项目] Adapter 构建请求
                                               → [模板] HttpClient 单次发送 → 对端
                                               ← Adapter 分类完整 HTTP 响应
                                               → [模板] 保存消息状态/退避

对端完成通知 → [业务项目] 回调接口 → 幂等保存结果/证据 → 关联原业务任务
                ECS 按设备/命令关联（业务项目自建），Transport 按任务/事件版本关联
                （已实现：POST /api/v1/wms/events，见 §7.2）
                不能依靠读取可靠层表完成业务流程
```

图中的静态目标链是模板的通用机制。实验只演示这条链；Transport 的正式实现见 `app/integrations/wms_transport/`，业务结果消费（T4）和真实联调条件按 T 任务验收。

### 术语和源码对照

| 名称 | 在这里指什么 | 源码 |
|---|---|---|
| target | 启动前配置的投递目标，包含名称、地址和协议名 | `app/reliable/config.py` 的 `TargetConfig` |
| origin / binding | 协议、主机、端口组成的地址 / 已生效的设备绑定快照 | ECS 业务配置，参考 §4 |
| ACK | 对端已接收的协议确认，不表示动作完成 | 业务适配器按合同判断 |
| `MessageDraft` | 业务交给可靠层的目标、身份、冻结字节及可选 TTL | `app/reliable/types.py` |
| `Outbox` | 在调用方事务内校验并保存消息，入队时不发网 | `app/reliable/outbox.py` |
| `Sender` | 后台领取、发送、回写、定时检查及停机协调 | `app/reliable/sender.py` |
| 租约 / 围栏 | 一段有期限的领取权 / 只允许匹配当前领取令牌回写 | `app/reliable/store.py`；它们不能阻止对端重复执行 |
| `Verdict` / `VerdictKind` | 适配器对完整响应的判断：送达、明确未接收、对端终局、未知 | `app/reliable/types.py` |
| `DONE` | 协议投递完成，不是业务或物理完成 | `app/reliable/delivery.py` |
| `FAILED` | 消息停止技术重试，原因见 `fail_reason`，不证明对端未执行 | 同上 |
| `EXPIRED` | 超过消息 TTL，不再发起新尝试；不取消在途请求 | 同上及 `app/reliable/store.py` |
| `PENDING` / `SENDING` | 等待领取或退避 / 已领取且正在尝试 | `app/reliable/types.py` |

生产业务只使用公开的 `Outbox`、`MessageDraft` 等接入类型；`Sender`、`MessageStore` 和可靠层模型在实验中用于基础设施装配与断言，不是业务查询接口。

## 1. 来源、前提与职责

2026-10-21 只读核对的对端文档：

- [Swagger UI](http://10.24.209.26:8080/swagger/index.html)
- [OpenAPI JSON](http://10.24.209.26:8080/swagger/v1/swagger.json)：`P9.ECS.Pipeline`，版本 `1.0`，OpenAPI `3.0.1`。
- 命令接口：`POST /api/v1/device/command`，请求模型 `MoveCommand` / `MoveLocationInfo`，HTTP 200 响应模型 `ApiResult`。
- [WMS Swagger](http://10.24.199.217:8284/swagger/ui/index#/WES) 及 [Swagger JSON](http://10.24.199.217:8284/swagger/docs/v1)：确认 `POST /api/v1/wes/transport-requests` 部署入口；其请求/响应只声明为 object，不能据此推断完整协议。
- Transport 协议基线：`wes_backend/docs/contracts/transport-fulfillment-contract.md`，版本 `0.4.0`、状态 `Approved`，重点为 §4 提交合同与 §5 回调。合同批准不等于实现或真实链路已验收。

ECS Swagger 目前只定义字段结构，没有说明 `code` 成功值、命令类型枚举、时间单位、业务必填约束、幂等语义及去重保留期。ECS 完成通知样例来自接入讨论，尚未由这份 Swagger 确认。ECS 示例都是讨论示例，不是正式合同；Transport 字段和 ACK 规则以其合同为准。不要向真实系统执行示例业务动作。

本页已包含实验所需的请求、ACK 样例和身份规则，跑实验不需要另一个仓库。来源核验信息如下，供 T1 负责人交付完整合同包时检查：

- 来源相对路径：`../wes_backend/docs/contracts/transport-fulfillment-contract.md`，适用于两个仓库同级的布局。
- 该文件内容 SHA-256：`3e631961810aa2597a1e3d51d363b393cb554d872480427918c340c87d569733`。

哈希标识本次审查的文件内容快照，其中包含来源仓库的未提交修改，不代表已提交或发布的合同版本。

真实集成前，由技术负责人提供完整合同文件并确认双方版本；初级工程师不自行选择另一个版本，也不从只声明 object 的 Swagger 补猜字段。参考源码用于解释地址冻结机制，不是实验运行依赖。

| 场景 | 地址何时确定 | 当前模板能否直接装配 |
|---|---|---|
| ECS 设备命令 | 发布时不知道有多少 ECS；现场配置设备列表后收集地址，创建命令时选择并冻结 | HttpClient 可复用；当前可靠层没有设备动态路由、逐消息地址冻结和动态客户端管理接口，不能直接按静态 target 接入 |
| WMS Transport | 现场部署时确定固定 WMS origin 和提交路径，进程启动前配置 | 静态 target 寻址已可用，适配器已默认注册；是否真发送取决于 `RELIABLE_TARGETS`。生命周期合同中业务结果消费（T4）仍缺，联调与对端确认也未完成 |

| 责任方 | 接入时必须落实的事项 |
|---|---|
| ECS 对端 | 明确请求与响应含义、认证和流量要求；确认按 `(device_code, command_code)` 去重，相同内容不重复执行、不同内容返回冲突；约定并发、重启后的去重行为和保留窗口；明确完成通知、重发及确认应答；不能要求本方从设备编号猜地址 |
| WMS/RCS 对端 | 按 Transport 合同以 `operation + operation_id` 去重，并保持已接纳 `transport_task_id` 到 submit 身份及原 RCS 请求的稳定关联；落实接纳 ACK 与位置/结果回调，不重复创建物理任务 |
| 本方业务模块 | 维护设备路由，校验业务输入及设备归属；保存命令身份、冻结地址和正文；实现协议适配；可靠记录与业务写入同事务；接收并关联结果，不把 ACK 当作业务完成 |
| 本方应用装配与运维 | 管理设备地址的现场配置、合法性和变更权限；动态 ECS 按地址管理客户端生命周期，静态 WMS 启动前注册适配器并配置 target；管理网络、凭据、流量、时钟同步和存储保留，制定存量命令迁移办法 |
| 现有基础层 | HttpClient 只负责已选定地址上的单次 HTTP；可靠层按启动时配置的 target 提供事务发件箱、原身份重试、租约和 TTL；不查设备表、不管理设备业务状态、不提供动态路由或地址快照 |

对端要求是接入合同与验收条件，不代表要把对端去重、设备业务或入站处理加入 `app/reliable/`。对端不满足去重条件时，不得启用当前可靠层的自动重试；本地幂等不能替代对端去重。

实现合同见 [HttpClient SPEC](superpowers/specs/2026-10-20-spec-019a-http-client.md)、[可靠层 SPEC](superpowers/specs/2026-10-20-spec-019b-reliable-interaction.md)；配置和装配背景见[实施与验证记录](superpowers/plans/2026-10-21-reliable-interaction-plan.md)。

## 2. 完整请求示例

下面保留全部请求字段，使用讨论中的关联身份。`EXAMPLE_*`、`string` 及数值都是占位示例，不表示对端接受这些业务值。`timestamp` 的示例按 Unix 毫秒表达，仅用于说明请求时间与完成时间不同；单位必须由对端确认。`priority=0`、`timeout=0` 的意义也尚未确认。

以下命令只展示写法，不要直接运行。样例命令编号已出现在完成通知中，不要拿它创建新的真实动作；隔离联调时使用新的命令编号和经确认的业务参数。

```bash
curl -X POST \
  'http://10.24.209.26:8080/api/v1/device/command' \
  -H 'accept: text/plain' \
  -H 'Content-Type: application/json' \
  -d '{
    "device_code": "STATION_SCAN18",
    "command_code": "01a15aa1-da90-775b-b17d-fcfb5b348f67",
    "timestamp": 1792525680000,
    "task_type": "EXAMPLE_TASK_TYPE",
    "priority": 0,
    "timeout": 0,
    "source": {
      "location_id": "EXAMPLE_LOCATION",
      "location_type": "EXAMPLE_LOCATION_TYPE",
      "bin_id": "EXAMPLE_BIN",
      "bin_type": "EXAMPLE_BIN_TYPE",
      "bin_cell_location": "string",
      "reel_layer": "string",
      "reel_thickness": "string",
      "reel_diameter": "string",
      "reel_totalthickness": "string"
    }
  }'
```

`result`、`finish_time`、`actual_qty` 等是完成通知内容，不属于该请求模型。不能用完成通知反推出 `task_type`、`source` 等原始业务参数，也不能用 `finish_time` 替代请求时间。

单次 HTTP 使用 `AsyncHttpClient.send(HttpRequest(...))`，JSON 先编码为 UTF-8 `bytes`。客户端在同一事件循环的应用生命周期内复用，不按命令新建连接池。ECS 还需完成 §4 的路由和生命周期设计，这里的固定地址只对应一个请求示例。每条命令只能由一个派发流程负责技术恢复，不能同时走业务派发器、通用可靠层或另一套 Celery 重试。

## 3. 定义联合幂等身份并冻结正文

本例约定 `command_code` 是**一次具体命令的实例编号**，不是 `MOVE` / `STOP` 这样的命令类型。新业务动作生成新编号；同一动作重试始终使用原编号。

ECS 命令及其终态通知以联合身份关联，业务记录还须保存选定地址：

```text
command_identity  = [device_code, command_code] 的稳定 JSON 编码
endpoint_base_url = 首次创建命令时确定并持久保存的 ECS origin
request_raw       = 首次确定并持久保存的完整请求字节
```

使用 JSON 数组可以避免字符串拼接的分隔歧义。保留设备名称大小写。这是业务关联键的编码示例，不是可靠层的动态寻址接口；对端是否按正文中的两个字段去重，仍须确认。

```python
import json


def command_identity(device_code: str, command_code: str) -> str:
    for value in (device_code, command_code):
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ValueError("command identity must contain non-empty strings")
    return json.dumps([device_code, command_code], ensure_ascii=False, separators=(",", ":"))


def freeze_command(payload: dict[str, object]) -> bytes:
    command_identity(payload["device_code"], payload["command_code"])
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
```

首次建命令时由业务验证完整参数、生成请求时间并冻结正文，将联合身份、选定地址和 `request_raw` 在同一事务保存在业务命令记录中。请求入口重试时先识别已有命令，复用其原地址和原字节，不重新查当前路由或生成编号、时间、签名正文或 JSON。新动作才创建新身份，并使用当前有效配置。

- ECS 业务入口重复请求应返回已有命令，同身份不同业务输入报冲突，不能覆盖旧命令或换身份掩盖冲突；其业务模型及并发约束由消费项目负责，不是本模板已实现的 DeviceCommand。
- 当前可靠层的同身份同字节返回已有引用、异字节抛 `DuplicateMessage`，属于 §5 的静态 target 入队语义，不能据此推断已支持 ECS 动态命令。
- 当前可靠层终态记录默认保留 90 天，清理后不再提供本地幂等依据；ECS 业务命令记录的保留、地址快照与旧请求重放规则由业务负责，不能假定永久去重。

## 4. ECS 现场动态路由：参考实现与当前缺口

### 4.1 现场配置、创建时选址、恢复时不漂移

发布软件时不枚举 ECS 地址；现场配置设备列表时，由受控的设备主数据维护 `device_code → endpoint_base_url`。地址不能从设备名规则推测，也不能直接信任请求正文传来的任意 URL。应校验地址格式、批准的网络范围、设备归属和修改权限；缺少或歧义配置时拒绝创建新命令。

`wes_backend` 正式业务链路提供以下参考，而非本模板已具备的能力：

```text
现场设备主数据：device_code → endpoint_base_url
  → WorkLine 启动时校验设备归属和地址，形成冻结 binding
  → 创建 DeviceCommand 时从 binding 取址
  → 同事务保存命令身份、endpoint_base_url 与命令快照
  → 派发时读取该命令的冻结地址
  → 按规范化地址取得并复用 ECS Adapter / transport
  → 发送；恢复仍使用原命令地址及身份
```

参考链路中，新命令使用当前已生效的工作线 binding，不是每次都读取设备表的最新值。修改设备主数据不等于更新已生效 binding；消费项目应明确如何重新校验并使新 binding 生效。已有命令的重复入口和恢复始终读取命令自己的地址快照。

例如，binding A 已生效后把设备主数据改成 B：旧命令仍使用 A；新 binding 尚未生效时，新命令也仍使用 A；新 binding B 生效后，新命令才使用 B。不要通过运行时改写全局地址跳过这一步。

设备改址不自动迁移存量命令。旧 ECS 可能已经执行，新 ECS 可能没有原去重记录。旧地址不可达时，保留原事实并告警，由负责人制定迁移办法，不自动改投或换号。

### 4.2 客户端复用与生命周期

参考 `wes_backend` 的 `src/app/device/composition.py` 中 `DeviceEndpointAdapterProvider`：首次访问一个规范化地址时创建 Adapter/transport，后续复用。同一 ECS 服务多个设备时共享连接池，不为每个命令建池。生产设计还要规定并发创建、地址数量上限、资源预算和创建失败后的清理办法。本模板没有这个动态客户端管理组件。

业务服务解析地址并读取数据库，应用装配管理客户端，协议适配器构建报文并分类响应。HttpClient 只接收已经确定的 origin 和相对路径，不认识 `device_code`，也不查设备表。应用关闭时，管理这些客户端的组件先协调在途请求，再关闭已创建的客户端。不要跨事件循环共享客户端。

只读参考位置（同级 `wes_backend` 仓库，不是本模板的运行依赖）：

- `src/app/device/models/device.py`：设备主数据的 `endpoint_base_url`。
- `src/app/workline/services/workline_start_service.py`：启动时形成设备 binding。
- `src/app/device/services/device_command_service.py`：先识别已有业务身份，再将 binding 地址保存到新命令。
- `src/app/device/services/device_dispatch_service.py`：使用命令保存的 endpoint 派发。
- `src/app/device/composition.py`：按 endpoint 管理 Adapter/transport 复用与关闭。

上述源码证明路由和地址冻结方式，不代表可照搬整个 ECS 协议。`wes_backend` 当前构造 `params`，本指南所读 ECS Swagger 使用 `source`；字段、状态准入和恢复保证必须按实际对端合同核对，不能合并发送或假定兼容。

### 4.3 当前可靠层不能直接承接的能力

`app/reliable_runtime.py` 仅在启动时为已配置、已注册适配器的 target 创建客户端；`MessageDraft` 只有 target，没有逐消息 endpoint 快照。发送使用 target 的当前配置，适配器没有查设备、注册动态目标或创建客户端的职责。

当前还缺少设备目录到投递路由的公开接口、命令与地址快照的原子持久化、动态客户端管理和存量路由变更策略。接入前需要单独设计这些能力，不能把参考项目里的类当成本模板现成的接口。

| 待办 | 现状与目标 | 由谁决定，初级工程师做什么 | 验收条件 | 是否阻塞 ECS 真实接入 |
|---|---|---|---|---|
| E1 路由和命令快照 | 没有动态路由接口；目标是业务身份、有效 binding、选定地址和请求快照同事务保存 | 技术负责人和设备业务负责人确定模型及接口；初级工程师先整理路由用例，获批后实现业务模块及测试 | 重复入口不重新选址；配置 A→B、新 binding 生效、旧地址不可达、事务回滚和并发创建均有测试 | 是 |
| E2 客户端管理 | 当前只启动静态客户端；目标是按规范化地址复用，并有并发、资源及停机约束 | 应用装配负责人确认缓存、地址数量和关闭策略；初级工程师按确定接口实现，不改 HttpClient 的业务边界 | 同地址多设备共享，创建失败无泄漏，关闭/取消不丢清理，客户端不跨事件循环 | 是 |
| E3 地址迁移 | 没有存量命令迁移协议；目标是改址不导致重复物理动作 | 技术负责人、现场负责人和 ECS 对端确认旧命令处置；初级工程师补变更检查和告警用例 | 旧任务保持原地址/身份，不能自动改投；迁移有明确证据、责任人和恢复步骤 | 是 |

ECS 不采用静态设备 target 列表。现场不需要人工维护第二份设备/target 映射，也不应靠重启重建静态客户端来处理设备配置。不要改写全局 base_url、在正文塞完整 URL、让适配器查库，或修改运行中的注册表来绕过动态路由设计。

已有 `wes_backend` DeviceCommand 自带可靠派发流程，继续由它负责，不能再叠加本模板发送器。可以在现有装配处复用单次 HttpClient，但这不等于迁移了可靠派发流程。

## 5. Transport：静态 target 与协议适配器

### 5.1 完整请求与身份

本接口是 WES → WMS，WMS 再对接 RCS，不按 `device_code` 查询 ECS。静态指部署期间固定，地址可以现场填写，不要求打包软件时就知道。合同允许部署配置指定提交路径；本案例使用 `/api/v1/wes/transport-requests`。

下面为 Transport 合同中的完整 `RACK_MOVE` 请求示例，**不执行，不重放历史身份**：

```json
{
  "operation_id": "01a13b00-cabd-7390-890a-0c093e7c07a1",
  "operation": "transport.task.submit@v1",
  "timestamp": 1791991270077,
  "data": {
    "transport_task_id": "transport-9af2751b-4d01-4203-88a7-649bbf69e810",
    "kind": "RACK_MOVE",
    "rack_id": "510004",
    "rcs_template_id": "CTU01",
    "source": {"kind": "RACK", "location_code": "510004"},
    "target": {"kind": "RACK_POSITION", "location_code": "KT16"},
    "target_face": "90"
  }
}
```

`operation_id` 由 submit 发起方生成并保存，格式为带标准连字符的小写 UUIDv7。`timestamp` 是 UTC Unix 毫秒整数。`transport_task_id` 标识业务搬运任务；`client_request_id` 是 WES 内部业务调用身份，不替代 submit `operation_id`，也不加入固定请求信封。技术恢复复用原身份和原始字节，不能重新生成时间或 JSON。

当前可靠层的示例映射是 `(target="wms-transport", operation="transport.task.submit@v1", operation_id=原 submit operation_id)`。它展示公开入队接口，不能沿用 DeviceCommand 的设备联合键。

### 5.2 静态配置与适配器文件

下面展示静态目标的配置格式。离线实验里的适配器只覆盖学习切片；正式适配器已在 `app/integrations/wms_transport/adapter.py`，真实启用前须完成 §5.3 中仍未关闭的任务（T4，以及 T2 是否启用预算的确认）和对端联调。

```dotenv
RELIABLE_TARGETS={"targets":{"wms-transport":{"base_url":"http://10.24.199.217:8284","protocol":"wms-transport","timeout_ms":10000,"max_body_bytes":262144}}}
```

先复制[离线实验](reliable-transport-lab.md)中的完整 `TransportSubmitAdapter` 和测试，理解响应分类；再读正式适配器及其测试 `tests/integrations/wms_transport/test_adapter.py`，两者的差异见 §5.3。正式适配器同步、无副作用，不查库、不发网，正文原样透传。

请求 UUIDv7、固定信封、kind 专属 DTO、成员及位置校验由 `TransportService`（`types.py`、`wire.py`）在冻结正文前完成，适配器只核对冻结身份自洽。`TargetConfig` 是装配/适配器参数类型，不是业务读取可靠存储的入口。新入队及发送前已经复用公开 `validate_request`；不要复制 HTTP 规则。

适配器已在 `app/main.py` 随应用创建时按协议名 `wms-transport` 注册（`app.state.reliable_adapters`），`TransportService` 的入队校验与发送器共用 `ADAPTERS` 这一份；不需要业务项目再写注册代码。注册不等于发送：只有 `RELIABLE_TARGETS` 里配置了对应目标，本进程才会创建客户端并领取消息，默认 `RELIABLE_TARGETS={}` 不发任何请求。需要 ECS 等其他协议时，才在 lifespan 启动前往 `app.state.reliable_adapters` 追加。

可选的提交预算示例：`"max_not_received_attempts":3` 让「确认未送达」累计 3 次后转 `STALLED`，默认不设，见 §5.3 的 T2。

目标名（示例用 `wms-transport`，可任取）只是 `RELIABLE_TARGETS` 里的标签，会写进已入队消息，上线后改名会让旧名字的待发消息停在 `PENDING`；`protocol` 取 `wms-transport` 才是绑定适配器的注册名，默认实例按这个协议选目标；目标名须匹配 `[a-z0-9][a-z0-9_.-]{0,63}`，base_url 不带尾部 `/`，路径保持原大小写。不要在运行中修改注册表，客户端在启动时创建。

### 5.3 ACK 与完整合同的适配边界

分类器按 Transport 合同 §4.2 实现，不按 HTTP 状态码单独判断。下面的具体响应可以直接转成测试输入，都针对 §5.1 那条带合法任务 ID 的请求，JSON 响应头为 `Content-Type: application/json`。空响应体按原始字节 `b""` 判断，不能用 `{}`、`null` 或空白替代。

| HTTP | 完整响应 body | 期望 Verdict |
|---|---|---|
| 202 | `{"operation_id":"01a13b00-cabd-7390-890a-0c093e7c07a1","code":"RECEIVED","timestamp":1791992148964,"data":{"transport_task_id":"transport-9af2751b-4d01-4203-88a7-649bbf69e810"}}` | `DELIVERED`，response 等于原 body 字节 |
| 200 | `{"operation_id":"01a13b00-cabd-7390-890a-0c093e7c07a1","code":"DUPLICATE","timestamp":1791992148964,"data":{"transport_task_id":"transport-9af2751b-4d01-4203-88a7-649bbf69e810"}}` | `DELIVERED`，response 等于原 body 字节 |
| 409 | `{"operation_id":"01a13b00-cabd-7390-890a-0c093e7c07a1","code":"CONFLICT","timestamp":1791992148964,"data":{"transport_task_id":"transport-9af2751b-4d01-4203-88a7-649bbf69e810"}}` | `PEER_TERMINAL` / `FailReason.CONFLICT` |
| 422 | `{"operation_id":"01a13b00-cabd-7390-890a-0c093e7c07a1","code":"REJECTED","timestamp":1791992148964,"data":{"transport_task_id":"transport-9af2751b-4d01-4203-88a7-649bbf69e810","reason_code":"INVALID_DATA"}}` | `PEER_TERMINAL` / `FailReason.REJECTED` |
| 503 | `{"operation_id":"01a13b00-cabd-7390-890a-0c093e7c07a1","code":"UNAVAILABLE","timestamp":1791992148964,"data":{"transport_task_id":"transport-9af2751b-4d01-4203-88a7-649bbf69e810"}}` | `NOT_RECEIVED`，无等待提示 |
| 400 | 空字节 `b""` | `PEER_TERMINAL` / `FailReason.REJECTED` |
| 413 | 空字节 `b""` | `PEER_TERMINAL` / `FailReason.REJECTED` |
| 503 | `<html/>` | `UNKNOWN` |
| 200 | `{"operation_id":"01a13b00-cabd-7390-890a-0c093e7c07a1","code":"RECEIVED","timestamp":1791992148964,"data":{"transport_task_id":"transport-9af2751b-4d01-4203-88a7-649bbf69e810"}}` | `UNKNOWN`，HTTP/code 组合不合法 |

实验中的 `ack_result` 会生成完整 body，参数化测试还覆盖错误消息 ID、错误/缺少任务 ID、未知 code、未知拒绝码、未定义 429/502、非法 UTF-8、重复 JSON key、非对象 body 和布尔时间戳。正式适配器的同类用例在 `tests/integrations/wms_transport/test_adapter.py`，另外覆盖时间戳 int64 边界、必填字段缺失、嵌套重复 key、400/413 非空体仍为未知，以及提交路径校验。

`422` 的 reason_code 只能使用合同列明的值，任务 ID 也只能在合同允许的分支省略。不要用子串或翻译匹配。submit 不定义 `429 / BUSY` 或等待提示。回调 `503` 可能已有收据/Evidence 提交，不能套用 submit 的明确未接纳语义。

静态 target 解决寻址，以下任务决定能否接真实 Transport。2026-10-23 按 develop 代码核对的现状如下；仍未关闭的项由负责人批准方案，初级工程师不自行改变基础层策略。

| 待办 | 当前状态（已核对代码） | 剩余目标与验收条件 | 决策/实施责任 | 是否阻塞真实接入 |
|---|---|---|---|---|
| T1 协议适配器 | 已实现：`wms_transport/adapter.py`（同步纯函数，严格 JSON、双重身份、任务 ID 回显、拒绝码精确匹配）；请求侧 DTO/位置校验在 `types.py`、`wire.py`；`tests/integrations/wms_transport/` 有对应测试 | 技术负责人交付完整合同并确认版本后，逐条核对下方「合同待确认项」；有差异再补用例 | 协议负责人确认版本；初级工程师核对，负责人复核 | 否（实现已有）；合同核对属联调前置 |
| T2 重试预算 | 机制已有：`TargetConfig.max_not_received_attempts`（默认不设，设为 3 即「确认未送达」累计 3 次转 `STALLED`）和 `fixed_retry_interval_s`；Transport 提交统一带 `SUBMIT_TTL`（1 小时，暂定），到期转 `EXPIRED` | 与 WMS 确认是否启用 3 次预算、1 小时窗口是否可接受；合同要求的固定重试间隔对应 `TargetConfig.fixed_retry_interval_s`（默认不设，按退避），是否配置待确认；预算耗尽不伪造业务拒绝 | 技术负责人和 Transport 负责人 | 否（默认安全）；启用与否待联调确认 |
| T3 ACK 与权威结果收敛 | 已实现：`POST /api/v1/wms/events` 在请求事务内锁任务行并应用；任何回调到达即用 `Outbox.settle` 结束该任务提交消息的发送；位置事实按集合记录与到达先后无关，结果按 `outcome_revision` 登记，迟到数据不改写已确定终态 | 联调验收：PostgreSQL 并发、回调重发与迟到场景；收据清理周期、对端最长重发时间向 WMS 确认（见 `TODOS.md`） | 技术负责人和 Transport 负责人 | 否（实现已有）；联调确认项未关闭 |
| T4 业务结果消费 | 未做：没有公开的结果发布或查询接口，调用方只能直接读 `transport_task`（`outcome_status`、`applied_outcome`、`cycle_id`） | 第一个消费方（工作线插件）出现后再定形态；重复消费不重复推进；不能读取可靠层表替代接口 | 业务负责人和技术负责人 | 是；见 `TODOS.md` 后续开发 |

合同待确认项（实现已落地，但以下偏离或空缺要在核对完整合同时给出结论）：

- 换面模板 `CK04` 由现场确认补入，合同 0.4.0 只列 `CTU01/CTU02/CTU03/F01`，合同文本尚未同步；WMS 若不接受会以 `422 REJECTED` 返回。
- 创建前不做可信位置/当前面校验，是有意偏离，由 WMS 在接纳时兜底（回调接收设计决定 14）。
- 适配器对所有 ACK（含 `422`）一律要求回显同一 `transport_task_id`，请求在冻结前已校验，因此没有实现「非法原请求的 `422` 可不带任务 ID」的分支；若 WMS 对非法请求返回不带任务 ID 的 `422`，当前会判为 `UNKNOWN` 并按退避重试到 TTL。合同是否要求该分支待确认：Transport 合同写合法请求的拒绝须回显任务 ID，而外发给 WMS 的接口说明书（`wes_backend/docs/integration/wes-wms-interface-development-specification.md` REQ-SUB-02）写拒绝响应不得携带 `transport_task_id`，两份文档互相矛盾，联调前必须与 WMS 确认；若 WMS 按后者实现，合法的 `422` 当前会被判为 `UNKNOWN`。
- 正式适配器忽略冗余字段中的浮点数，只要求已定义字段为整数，与离线实验一律拒绝浮点不同。
- 拒绝码以精确字面量集合匹配，含中英文拼接原文；集合有测试固定，变更须同步合同。

已有 `wes_backend` TransportTask/Worker 不叠加本模板发送器。这里不授权迁移。分类器抛异常时消息进入 `FAILED(LOCAL_ERROR)`，而对端此时可能已执行，这条既有策略未变，接入真实对端前须单独评审。架构选择记录在上述任务中，不由适配器副作用绕过。

### 5.4 跑通离线投递实验

按[离线实验](reliable-transport-lab.md)准备一个完整测试文件并运行。实验使用 `ScriptedHttpClient`、`FakeClock`、内存 SQLite 和真实的 Outbox/Sender，验证完整投递路径。通过后交付测试文件；取得并确认完整合同后再做 T1 的合同核对。真实 PostgreSQL/Redis 的准备见 §9，离线实验不需要启动它们。本地想在 Swagger 里手动触发场景并查看请求、响应原文，可用仅 `ENVIRONMENT=local` 的联调控制台，见 [README](../README.md#联调控制台仅-environmentlocal) 与[设计](superpowers/specs/2026-10-22-transport-dev-console-design.md)；它不替代离线测试，也不是真实对端联调。

## 6. 静态 target 的事务内入队示例

Transport 本身不需要照着本节再写一遍：`TransportService.move_rack` 等方法已在调用方的 `AsyncSession` 内同事务登记 `transport_task` 与可靠消息（用法见 [README](../README.md) 的 Transport 出站一节）。本节讲通用的静态 target 入队写法，供新增其他协议时参考，不适用于 ECS 动态路由。业务项目自行定义 SQLAlchemy 模型，保存该请求的身份和冻结字节，并建立业务唯一约束。可靠层表不能代替业务记录。

先阅读[实验](reliable-transport-lab.md)的 `BusinessTransport`、`save_and_enqueue` 和回滚测试。那里给出了具体 ORM 模型、唯一约束、会话工厂和完整调用，不需要猜 `transport_record: object` 应该传什么。

实验的 `save_and_enqueue` 在同一个 `session.begin()` 中保存业务身份及字节，再调用 `outbox.enqueue`。成功退出才提交；之后由 `Sender.tick()` 领取并发送。回滚测试同时断言两张表都没有记录。生产服务采用相同事务所有权，但业务模型、业务状态和迁移必须经业务负责人确认。

从已装配的 `app.state.outbox` 取得参数，空配置时显式拒绝业务接入，不假装入队成功。已有外层事务时直接在该事务内执行业务写入和 `enqueue`，不要重复 `begin()` 或在中途提交。异常/冲突让整个事务回滚，不在事务内直接发送 HTTP。

重复业务入口先按业务幂等身份识别已有记录，复用已保存的 submit ID 与字节；并发首次创建以数据库唯一约束收敛，冲突后在可用事务中读取原记录并核对内容。不要再次 `add` 新业务任务，或只依赖可靠层唯一约束避免重复业务动作。

`reference` 是入队引用及当时的状态，不是异步执行结果。可靠层的 `DONE` 表示协议投递成功，不是搬运完成；当前没有公开的异步结果消费接口，业务不能查询可靠层表来推进流程。业务若需获知异步 `FAILED/EXPIRED`，必须另行明确结果契约，不能把本指南当成已有能力。

TTL 从入队创建时起算，重试不延长，到期不取消已发请求；`EXPIRED`、超时或未收到回调都不能证明对端没执行。有限去重窗口下，双方约定的 TTL 加在途到达余量不得超过去重窗口；无法保证余量时不能凭 TTL 假定安全。当前窗口校验尚未程序化（Transport 提交的 1 小时 `SUBMIT_TTL` 是暂定值，需与 WMS 确认其最长重发时间同样不超过 1 小时），接入方负责配置约束与验收；无 TTL 的持续重试只适用于持久去重保证，不能由样例擅自承诺。

静态 target 在消息存续期间仍使用当前配置，没有地址快照；迁移到另一套 WMS/去重库或改变协议前须排空存量消息，不能把这一限制反过来作为 ECS 的静态配置方案。

## 7. 完成通知关联与业务幂等

### 7.1 ECS 命令结果

讨论中的完成通知如下，它不应发送到上述命令入口：

```json
{
  "command_code": "01a15aa1-da90-775b-b17d-fcfb5b348f67",
  "device_code": "STATION_SCAN18",
  "result": "SUCCESS",
  "finish_time": 1792525683851,
  "data": {
    "actual_qty": 1,
    "result": "MOVE_FINISHED"
  },
  "error_detail": null,
  "trace_id": null,
  "event_id": null,
  "causation_id": null
}
```

本方业务回调接口应按以下顺序处理，不在可靠出站层增加 Inbox 或回调路由：

1. 验证回调来源、大小、字段和协议含义；`trace_id` 只用于追踪，不作为幂等键。`finish_time` 的单位、来源及可信程度需确认，不把对端时间当成本方当前状态的权威依据。
2. 在业务数据库事务中，按 `(device_code, command_code)` 查找并锁定原命令，或使用等价的原子条件更新。核对所属对端、设备、业务对象及当前允许的状态转换；未知命令不猜测关联、不凭回调补造成功命令。
3. 将本次接受的结果证据与业务状态变更原子保存。重复通知不能再次扣减数量、搬运库存或推进下一任务。后续动作由负责它的可靠派发模块在同一事务登记，动态 ECS 不能直接套用 §6 的静态入队示例。
4. 同一终态重复通知返回合同约定的原确认。不同终态或关键结果内容冲突时，保留已接受事实，记录冲突并按合同拒绝或告警，不能用最后到达的通知覆盖原结果。协议应明确哪些字段属于结果内容，追踪元数据不应触发重复业务应用。
5. 事务提交成功后才返回接收确认；应答格式及失败重发由双方合同定义。完成通知可能先于本方保存命令入口的 HTTP 应答到达，不能要求先完成发送结果记账才允许处理。

联合键用于完成通知去重的前提是：一条命令只有一个不可变终态。如果还发送已接收、执行中或多个业务事件，需要约定稳定 `event_id` / 序号及事件类型；当前 `event_id=null` 不能用于多事件去重。重试保留原事件身份，新独立命令使用新 `command_code`，不能让旧完成通知完成新动作。

`SUCCESS` / `MOVE_FINISHED` 是对端报告的完成证据，经过身份与业务状态校验后才能应用。HTTP 成功、命令应答、可靠层 `DONE`、完成通知接收确认、业务完成分别记录，不能互相替代；涉及真实物理完成时还须满足该业务约定的权威证据。

### 7.2 Transport 的消息身份、任务关联与结果版本

按 Transport 合同 §5，WMS 回调到 WES 的 `/api/v1/wms/events`，不是原 submit 路径，也不是 ECS 完成通知格式。接收端已实现：通用接收在 `app/integrations/wms_callback/`，Transport 的两个 operation 在 `app/integrations/wms_transport/callback.py`，设计与决定记录见[回调接收设计](superpowers/specs/2026-10-22-transport-callback-design.md)：

- `transport.task.member_position_changed@v1` 与 `transport.task.resulted@v1` 分别承载位置事实和最终结果；每条回调有 WMS 自己生成的 `operation_id`，不沿用 submit ID，技术重试保持该消息原身份和完整信封。
- `transport_task_id` 关联原任务；最终结果另以 `transport_task_id + outcome_revision` 登记版本（`transport_task.applied_outcome_revision`、`outcome_digests`）。同版本相同 `data` 判重复，不同 `data` 判冲突；不需要另造设备 `event_id`。
- 与合同的内部顺序不同、对端可见行为不变：不设证据表和后台应用，回调在请求事务内锁任务行并直接应用后再 ACK。未知任务、事实矛盾也只记收据诊断和告警日志，仍返回 `202`，不能套用 ECS 讨论样例的未知命令拒绝规则；应用逻辑对通过 DTO 校验的消息不抛异常。
- 低版本不回退事实；更高版本只在未确定结果上收敛，不改写已确定成功/失败。料箱成功结果先于位置事件到达时暂存在 `pending_outcome`，位置补齐后在同一事务内完成。BIN 回架等前置位置条件按业务合同验收，不能仅凭聚合结果推断完成。
- 任务收到任何回调即结束其提交消息的发送（`settle_submitted`）；对端长期不可用或回调缺失，靠 `transport.scan_stuck` 的卡住任务告警暴露，不做定时盲目重发。

技术重提与业务重试必须分开。确定 `REJECTED/CANCELLED` 后，原任务保持终态；只有原业务需求仍有效，负责该业务的模块才按合同以全新 `client_request_id` 创建新任务及 submit ID。这不是第二套技术发送调度，不能因原请求超时或配置改址就自动创建新动作。

## 8. 验收清单与常见问题

### 离线永久测试：本方负责

- 联合身份无歧义，原身份实际出现在请求正文；不同设备/命令不混淆，重试不改正文、时间或编号。
- ECS 地址只能来自已授权现场设备配置；缺少、歧义或非法地址时拒绝创建。新命令使用有效配置，重复入口及恢复复用原地址；配置从 A 改到 B 后，旧命令不发往 B，原地址不可达不自动迁移。
- 实现动态客户端管理时，补充同地址跨设备复用、并发创建、循环归属、资源上限及停机关闭的永久测试。该能力尚未实现，下面的验收要求不代表已有通过记录。
- 离线实验的成功用例直接调用公开 `validate_request`，验证适配器构建的示例请求且不触发发送；入队与实际发送仍使用现有 HTTP 校验。覆盖完整应答的分类、身份不符、非法报文、未知码及网关错误，不靠 HTTP 200 推断成功。
- 静态 target 接入使用真实 PostgreSQL 验证业务写入与入队共同提交/回滚、并发首次创建及同身份冲突。ECS 由其业务派发模块验证命令身份、地址及快照原子保存。全量回归按项目要求显式配置可销毁的 PostgreSQL/Redis，不把跳过当通过。
- 回调并发重放只应用一次业务效果，冲突不覆盖原结果，未知身份和旧命令通知不推进新动作。业务处理失败不能留下已记为重复、却尚未应用业务变更的半状态。
- Transport 分别验证 submit ACK 和回调 ACK，覆盖空体拒绝、双重身份、结果版本、迟到与重复回调；对应永久测试在 `tests/integrations/wms_transport/`（`test_adapter.py`、`test_callback.py`、`test_concurrency.py` 等）。离线投递实验通过不等于完整生命周期兼容。

### 真实对端联调：双方负责

- 首次请求、同身份串行/并发重放、对端重启后重放：实际业务只执行一次，并返回合同约定结果；异内容冲突不再次执行。
- 对端执行后响应丢失、本方发送后保存前崩溃：用原身份和原字节恢复，允许多次 HTTP 请求，不允许重复业务执行。脚本化对端测试不能替代此项。
- 核对真实认证、TLS（若启用）、连接复用、超时、响应编码和大小限制，以及回调丢应答后的重发。一次联调无法证明长期去重保留窗口，须有合同承诺。
- 批准网络隔离与敏感原文存储方案，落实包括重试和全部实例的出口流量保护；不能用默认并发数代替速率限制，也不要领取租约后再长时间等待限流额度。
- 验证时钟偏差与调度/回写延迟合计远小于默认 5 秒租约余量；验收实际进程停机和恢复。可靠层共用首次确定的 15 秒清理预算，未确认任务留待租约过期，不强制解锁。

上述 5 秒余量和 15 秒预算属于本模板可靠层，不替代 `wes_backend` DeviceCommand/Transport 自有合同。HTTP 超时限制单次调用；ECS 正文 `timeout`、业务 deadline、消息 TTL、租约时长和终态保留期是不同约束，不能互换。地址数量、容量和安全准入必须按真实部署另验，本文明文 HTTP 示例不能暴露到不受信网络；现有入站限流也不提供出口保护。

### 排查顺序

| 现象 | 先检查 |
|---|---|
| ECS 配好设备后仍无法通过通用 outbox 派发 | 当前可靠层没有动态路由/地址冻结接口，需要先完成 §4 的接入设计；不要添加静态设备 target 或修改运行中的注册表 |
| 修改设备地址后旧命令无法恢复 | 核对命令冻结地址和旧 ECS；保留原身份与事实，按受控迁移流程处理，不自动改投新地址 |
| 静态 target 入队返回 `InvalidMessage` | 目标和适配器是否存在、消息身份和冻结正文是否一致、HTTP 校验和大小限制 |
| 同身份入队返回 `DuplicateMessage` | 是否重新生成请求时间、修改正文或改变序列化；读取原业务记录，不换身份绕过冲突 |
| 消息保持 `PENDING` | 目标是否配置、适配器是否在启动前注册、当前进程是否有客户端；也可能处于退避，不能直接认定卡死 |
| 完整 HTTP 200 后仍重试 | 检查是否为合法 `DUPLICATE` 且双重身份匹配；200 配 `RECEIVED` 仍为未知，不能为结束重试而放宽分类 |
| 超时或消息过期 | 查询业务命令与合法完成证据，不能据此重发一个新身份的动作；不要人工强制解锁租约 |
| 收到完成通知但命令未知 | 核对来源、联合身份和环境，按合同拒绝/隔离；不猜设备映射，不直接写成功 |

## 9. 环境准备与全量验证

### 9.1 开发环境

准备 Python 3.13+、`uv`；只有需要数据库/Redis 验收时才需要 Docker Engine 和 Compose。先在仓库根目录运行 `uv sync --frozen`。离线实验不需要 `.env`，直接用实验页给出的测试命令。

需要启动本地开发依赖时，保留已有 `.env`：

```bash
test -f .env || cp .env.example .env
docker compose up -d --no-recreate postgres redis
```

这只启动 PostgreSQL/Redis，不启动应用、Worker 或 Beat。`.env.example` 的本地配置要点如下，保留你已有的有效设置，不用整段覆盖文件：

```dotenv
ENVIRONMENT=local
DATABASE_URL=postgresql+asyncpg://admin:admin123@localhost:25432/enterprise_backend
REDIS_URL=redis://localhost:26379/0
RELIABLE_TARGETS={}
```

示例密码仅供本地开发，不用于交付。`JWT_SECRET_KEY` 用 `openssl rand -hex 32` 生成后填写，不提交 `.env`。开发库不是集成测试库；不能把上面的 DATABASE_URL 直接复制成 TEST_DATABASE_URL。

### 9.2 使用独立、可销毁的 PostgreSQL/Redis 跑全量

`tests/conftest.py` 读取 `TEST_DATABASE_URL` / `TEST_REDIS_URL`；PostgreSQL fixture 自动在指定测试库执行 Alembic 迁移。配置未设置会跳过对应测试，连接失败会报错。这与 SQLite 实验通过是两种证据。

下面整段在独立子 shell 中执行：Docker 自动分配容器名与宿主端口，PostgreSQL 数据放在临时内存挂载中；退出时只删除本段成功创建的容器及其匿名卷，不停止现有 Compose 服务，也不删除已有卷。镜像与本仓库 `docker-compose.yml` 一致：PostgreSQL 使用固定摘要的 `timescale/timescaledb`，Redis 使用 `redis:8-alpine`；Compose 更新镜像时同步此处，需要本机能取得这些镜像。不要把它当成部署命令。

```bash
(
  set -eu
  pg_id=
  redis_id=
  cleanup() {
    if [ -n "$redis_id" ]; then docker rm -fv "$redis_id" >/dev/null 2>&1 || true; fi
    if [ -n "$pg_id" ]; then docker rm -fv "$pg_id" >/dev/null 2>&1 || true; fi
  }
  trap cleanup EXIT
  pg_id=$(docker run -d \
    -e POSTGRES_USER=admin -e POSTGRES_PASSWORD=example-test-only \
    -e POSTGRES_DB=enterprise_backend_test \
    -p 127.0.0.1::5432 --tmpfs /var/lib/postgresql/data \
    timescale/timescaledb@sha256:6e4b469dee0395a8a6d8c818384b0226a749997a29a312f314413f98e4161f82)
  redis_id=$(docker run -d -p 127.0.0.1::6379 redis:8-alpine)
  for attempt in $(seq 1 30); do
    if docker exec "$pg_id" pg_isready -U admin >/dev/null 2>&1 \
      && [ "$(docker exec "$redis_id" redis-cli ping 2>/dev/null)" = PONG ]; then
      break
    fi
    sleep 1
  done
  docker exec "$pg_id" pg_isready -U admin
  test "$(docker exec "$redis_id" redis-cli ping)" = PONG
  pg_port=$(docker port "$pg_id" 5432/tcp)
  redis_port=$(docker port "$redis_id" 6379/tcp)
  pg_port=${pg_port##*:}
  redis_port=${redis_port##*:}
  ENVIRONMENT=local DATABASE_URL=sqlite+aiosqlite:// \
  JWT_SECRET_KEY=example-test-secret-not-for-production \
  TEST_DATABASE_URL="postgresql+asyncpg://admin:example-test-only@127.0.0.1:${pg_port}/enterprise_backend_test" \
  TEST_REDIS_URL="redis://127.0.0.1:${redis_port}/0" \
    uv run python -m pytest -ra
)
```

2026-10-21 实跑记录（历史，当时代码尚无 Transport 业务层）：按上述命令使用独立 PostgreSQL/Redis，全量 `617 passed`、无跳过。退出后核对本轮容器和匿名卷已清理，已有容器身份、启动时间及卷清单未变；`.env` 和 `uv.lock` 内容未变。

2026-10-23 在 develop 上未配置 `TEST_DATABASE_URL` / `TEST_REDIS_URL` 直接运行：`1176 passed, 366 skipped`，ruff 检查与格式检查通过。被跳过的是 PostgreSQL/Redis 用例，当前代码尚无带数据库的全量实跑记录，上线前须按本节命令补跑。这些都不替代真实对端联调或完整 Transport 合同验收。

检查 pytest 汇总和 skip 原因：要求 PostgreSQL/Redis 用例实际执行，不能将它们的跳过当作通过。不要对共享开发库运行种子或清空数据。代码任务再运行 `uv run ruff check .` 和 `uv run ruff format --check .`；文档任务检查示例、链接及 `git diff --check` 即可。示例命令在这里提供给开发者，不表示已经完成真实对端或完整 Transport 验收。

## 10. 先别碰

| 不要做的事 | 应该怎么做 |
|---|---|
| 让初级工程师自行补动态路由、启用重试预算或设计结果接口 | 先由 E/T 任务的负责人批准设计，再按验收条件实施 |
| 修改全局 base_url、在正文塞任意 URL、在运行中改 target 注册表 | ECS 在业务创建时冻结路由；静态 WMS 在启动前装配，存量消息迁移单独处理 |
| 在纯适配器中查库、发网或更新业务状态 | 适配器只构建请求、判断完整响应，业务事务和客户端生命周期由各自模块负责 |
| 给 DeviceCommand/TransportTask 再叠一套 Sender 或 Celery 技术重试 | 每条发送义务只保留一个调度负责人 |
| 用可靠层表充当业务结果 API，或让业务导入 MessageStore | 先完成 T4 的公开结果合同；内部表读取仅用于基础设施测试和运维诊断 |
| 将超时、TTL 到期、HTTP 200 或 `DONE` 当成物理成功/失败 | 保留原身份，核对合法 ACK、完成通知与业务权威事实 |
| 为重新执行旧任务换身份、覆盖已接受结果或强制解锁租约 | 区分技术恢复与新业务意图，按批准的迁移/恢复流程处理 |
| 把 `.env`、共享数据库或现有 Docker 卷当成可销毁测试环境 | 使用 §9.2 的独立实例，检查测试 URL 与清理对象 |
