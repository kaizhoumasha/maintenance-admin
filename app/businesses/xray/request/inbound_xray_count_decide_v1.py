from app.integrations.wms_request import Endpoint, request_operation

from ..contracts import DECISIONS, Identifier, NonnegativeInt, Text
from ..request_data import WorklineRequest

OPERATION = "inbound.xray.count_decide@v1"


@request_operation(
    OPERATION, Endpoint.DECISIONS, requires_business_owner=True, decision_data=DECISIONS["COUNT"]
)
class CountDecide(WorklineRequest):
    barcode: Text
    actual_qty: NonnegativeInt
    template_id: Identifier
    observed_at: NonnegativeInt
