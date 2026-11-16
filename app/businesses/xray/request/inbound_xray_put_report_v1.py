from app.integrations.wms_request import Endpoint, request_operation

from ..contracts import RackLocator, Text, TransitLocator
from ..request_data import ReportRequest

OPERATION = "inbound.xray.put_report@v1"


@request_operation(OPERATION, Endpoint.FACTS, requires_business_owner=True)
class PutReport(ReportRequest):
    barcode: Text
    to_locator: RackLocator | TransitLocator
