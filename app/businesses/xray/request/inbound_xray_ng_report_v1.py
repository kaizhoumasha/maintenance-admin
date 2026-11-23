from app.integrations.wms_request import Endpoint, request_operation

from ..contracts import NgStage, OmitAbsent, Text
from ..request_data import ReportRequest

OPERATION = "inbound.xray.ng_report@v1"


@request_operation(OPERATION, Endpoint.FACTS, requires_business_owner=True)
class NgReport(OmitAbsent, ReportRequest):
    stage: NgStage
    barcode: Text | None = None
    reason_code: Text
    reason_message: Text
