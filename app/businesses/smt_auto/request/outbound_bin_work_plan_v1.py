from app.businesses._components.sorting_task.contracts import Identifier, NonNegative
from app.integrations.wms_request import Endpoint, RequestPayload, request_operation


@request_operation("outbound.bin.work_plan@v1", Endpoint.DECISIONS)
class WorkPlan(RequestPayload):
    task_id: Identifier
    bin_code: Identifier
    scanned_at: NonNegative
