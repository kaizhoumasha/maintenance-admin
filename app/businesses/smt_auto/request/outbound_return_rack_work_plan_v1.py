from app.businesses._components.sorting_task.contracts import Face, Identifier, NonNegative
from app.integrations.wms_request import Endpoint, RequestPayload, request_operation


@request_operation("outbound.return_rack.work_plan@v1", Endpoint.DECISIONS)
class WorkPlan(RequestPayload):
    task_id: Identifier
    rack_id: Identifier
    rack_face: Face
    arrived_at: NonNegative
