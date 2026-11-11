from app.integrations.wms_request import Endpoint, request_operation

from ..contracts import DECISIONS, NonnegativeInt, Text
from ..request_data import WorklineRequest

OPERATION = "inbound.xray.pkg_decide@v1"


@request_operation(
    OPERATION, Endpoint.DECISIONS, requires_business_owner=True, decision_data=DECISIONS["ENTRY"]
)
class PkgDecide(WorklineRequest):
    barcode: Text
    scanned_at: NonnegativeInt
