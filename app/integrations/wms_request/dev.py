"""联调用的演示 operation:照着它写一个真实的 WMS 请求 operation。

本文件名是 `dev.py`,只由固定平台清单在 `ENVIRONMENT=local` 加载,生产不导入、不注册。
联调:在 Swagger 的 `POST /api/v1/dev/wms/requests` 选演示 operation 的示例触发,再用
`GET .../status/{operation_id}` 看投递结果;`GET .../operations` 是目录。

新增一个真实 operation(显式入口由 composition 和 catalog 共同拥有)
=========================================================================
1. 新建 `app/businesses/<业务>/request/<域>_<名称>_v1.py`,照下面写一个类。文件名是注册名把
   `.` 和 `@` 换成 `_`,导入时检查两者一致。所属 composition.ENTRY_MODULES 须列出入口，
   且完整真实业务的 composition 必须由 catalog.entries() 列出；仅新建文件不会加载。
2. `@request_operation("域.名称@vN", Endpoint.FACTS 或 Endpoint.DECISIONS)` 注册;字段语义不兼容
   变化时升版本,不要在同一版本上悄悄改字段。
3. 直接用被装饰的 `RequestPayload` 模型声明字段:严格 JSON 类型、禁止多余字段。
   要在联调下拉里出现示例,
   给 `model_config` 加 `json_schema_extra={"examples": [{...}]}`;示例不合法会在导入时报错。
4. 调用方在自己的事务里调用 `wms_requests.send(session, 已实例化的注册模型, operation_id=...)`。
"""

from pydantic import ConfigDict

from .operation import Endpoint, RequestPayload, request_operation


class _DemoPayload(RequestPayload):
    task_id: str
    qty: int
    model_config = ConfigDict(json_schema_extra={"examples": [{"task_id": "T-1", "qty": 2}]})


@request_operation("example.fact.demo@v1", Endpoint.FACTS, title="演示事实上报(facts)")
class DemoFact(_DemoPayload):
    pass


@request_operation("example.decision.demo@v1", Endpoint.DECISIONS, title="演示业务决策(decisions)")
class DemoDecision(_DemoPayload):
    pass
