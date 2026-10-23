"""联调用的演示 operation:照着它写一个真实的 WMS 请求 operation。

本文件名是 `dev.py`,只在 `ENVIRONMENT=local` 被联调控制台自动发现,生产不导入、不注册。
联调:在 Swagger 的 `POST /api/v1/dev/wms/requests` 选演示 operation 的示例触发,再用
`GET .../status/{operation_id}` 看投递结果;`GET .../operations` 是目录。

新增一个真实 operation(通常只新建一个文件,不改控制器、不改 `main.py`)
=========================================================================
1. 新建 `app/integrations/<业务>/request/<域>_<名称>_v1.py`,照下面写一个类。文件名是注册名把
   `.` 和 `@` 换成 `_`,有测试检查两者一致。
2. `@request_operation("域.名称@vN", Endpoint.FACTS 或 Endpoint.DECISIONS)` 注册;字段语义不兼容
   变化时升版本,不要在同一版本上悄悄改字段。
3. 内嵌 `Payload(RequestPayload)` 声明字段:严格 JSON 类型、禁止多余字段。要在联调下拉里出现示例,
   给 `model_config` 加 `json_schema_extra={"examples": [{...}]}`;示例不合法会在导入时报错。
4. 调用方在自己的事务里调用 `wms_requests.send(session, "域.名称@vN", {...}, operation_id=...)`。
"""

from pydantic import ConfigDict

from .operation import Endpoint, RequestOperation, RequestPayload, request_operation


class _DemoPayload(RequestPayload):
    task_id: str
    qty: int
    model_config = ConfigDict(json_schema_extra={"examples": [{"task_id": "T-1", "qty": 2}]})


@request_operation("example.fact.demo@v1", Endpoint.FACTS)
class DemoFact(RequestOperation):
    title = "演示事实上报(facts)"
    Payload = _DemoPayload


@request_operation("example.decision.demo@v1", Endpoint.DECISIONS)
class DemoDecision(RequestOperation):
    title = "演示业务决策(decisions)"
    Payload = _DemoPayload
