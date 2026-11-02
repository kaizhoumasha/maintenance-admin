from app.businesses._components.sorting_task.contracts import Identifier, Positive
from app.integrations.wms_request import Endpoint, RequestPayload, request_operation

OPERATION = "manual_bin.work_admission_decide@v1"


@request_operation(OPERATION, Endpoint.DECISIONS)
class AdmissionRequest(RequestPayload):
    task_id: Identifier
    bin_code: Identifier
    scanned_at: Positive
