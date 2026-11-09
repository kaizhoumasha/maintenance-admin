from app.integrations.wms_request import Endpoint, request_operation

from ..contracts import OperationId, RackLocator, TransitLocator
from ..request_data import ReportRequest

OPERATION = "inbound.xray.put_report@v1"


@request_operation(OPERATION, Endpoint.FACTS, requires_business_owner=True)
class PutReport(ReportRequest):
    target_operation_id: OperationId
    to_locator: RackLocator | TransitLocator
