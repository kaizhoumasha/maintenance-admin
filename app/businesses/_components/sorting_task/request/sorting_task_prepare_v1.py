from app.integrations.wms_request import Endpoint, RequestPayload, request_operation

from ..contracts import Identifier


@request_operation("sorting.task.prepare@v1", Endpoint.DECISIONS)
class Prepare(RequestPayload):
    task_id: Identifier
    workline_code: Identifier
