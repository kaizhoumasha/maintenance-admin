"""Transport(搬运)业务对接:WES 向 WMS 提交搬运任务,并处理 WMS 回传的位置事件和最终结果。

依据合同 `wes_backend/docs/contracts/transport-fulfillment-contract.md`(0.4.0)。
提交走 `transport.task.submit@v1`,默认路径 `/api/v1/wes/transport-requests`,协议名
`wms-transport`。回传由 `wms_callback` 接收,本包注册两个 operation,见 `callback/`。

## 调用方怎么用

用 `transport_service`(`TransportService`)的四个方法:`move_rack`、`rotate_rack`、
`move_bins`、`exchange_bins`。它们在调用方传入的 `AsyncSession` 里同事务写入 `transport_task`
并入队,不提交,不发网。

- 调用方生成并保存 `client_request_id`(小写 UUIDv7)。同号同输入返回原句柄,同号不同输入抛
  `IdempotencyConflict`。
- 请求在入队时冻结成字节,重试时原样重发。提交消息带 1 小时 TTL(暂定),到期转 `EXPIRED`。
- 返回的 `TransportHandle` 只证明登记了发送义务,不代表 WMS 已接纳或搬运已完成。

多数调用方应该用场景封装,而不是直接拼请求:

- `RackScenarios` 和 `RackCycle`:货架从进场、换面到离场的一轮生命周期。角色在创建时给一次,
  进场和离场的模板配对由此保证。同一轮共用一个本地分组键 `cycle_id`,它不进入对端可见的报文。
- `BinScenarios`:料箱投料、回架和交换。

## 文件分工

- `protocol.py`:operation 名、协议名、默认路径、模板集合、批准的拒绝码、`SUBMIT_TTL`。
- `types.py`、`wire.py`:入参类型与校验,冻结请求字节。校验放在这里,适配器不重复做。
- `adapter.py`:`TransportSubmitAdapter`,按合同 §4.2 分类 WMS 对提交的同步应答。
- `service.py`:入队服务。
- `rack_scenarios.py`、`bin_scenarios.py`:场景封装。
- `callback/`:位置事件和最终结果的应用,规则在 `callback/apply.py`。
- `settle.py`:任务一收到回调,就结束它的提交消息的发送。
- `dev.py`、`callback_samples.py`:联调控制台的场景和回调样例,只在 `ENVIRONMENT=local` 使用。

结果和卡住任务告警写在 `transport_task` 上(`outcome_status`、`applied_outcome`、`cycle_id`),
告警任务在 `app/tasks/transport_tasks.py`。

## 要知道的限制

- 搬运层不预查、不阻塞。同一货架上要不要等前序任务结束,由调用方按场景决定。
- 目前没有公开的结果查询或发布接口,调用方只能直接读 `transport_task`。第一个消费方出现后再定,
  见 `TODOS.md`。
- 创建前不校验货架的可信位置和当前面,由 WMS 在接纳时兜底。换面模板 `CK04` 是现场确认补入的,
  合同文本尚未同步。
- 适配器要求所有应答回显 `transport_task_id`,包括 422。外发给 WMS 的接口说明书对此写法不同,
  联调前要和 WMS 确认。
- 默认 `RELIABLE_TARGETS` 为空,不会发送。联调和上线的前置条件见
  docs/superpowers/specs/device-command-integration.md 的 §5.3。

设计记录:docs/superpowers/specs 下的 transport-outbound-design、rack-transport-integration-design、
transport-callback-design。
"""

from .adapter import ADAPTERS, TransportSubmitAdapter
from .bin_scenarios import BinScenarios, bin_scenarios
from .protocol import OPERATION, PROTOCOL
from .rack_scenarios import RackCycle, RackDestination, RackRole, RackScenarios, rack_scenarios
from .service import TransportService, transport_service
from .types import (
    BinExchangePair,
    BinMove,
    HandoffPosition,
    IdempotencyConflict,
    InvalidTransportRequest,
    Rack,
    RackBinSlot,
    RackPosition,
    TransportCaller,
    TransportHandle,
    Zone,
)

__all__ = [
    "ADAPTERS",
    "OPERATION",
    "PROTOCOL",
    "TransportSubmitAdapter",
    "TransportService",
    "transport_service",
    "BinExchangePair",
    "BinMove",
    "BinScenarios",
    "HandoffPosition",
    "IdempotencyConflict",
    "InvalidTransportRequest",
    "Rack",
    "RackBinSlot",
    "RackCycle",
    "RackDestination",
    "RackPosition",
    "RackRole",
    "RackScenarios",
    "bin_scenarios",
    "rack_scenarios",
    "TransportCaller",
    "TransportHandle",
    "Zone",
]
