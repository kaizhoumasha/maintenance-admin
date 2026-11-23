from app.businesses._components.sorting_task.contracts import Identifier, NonNegative
from app.integrations.wms_request import Endpoint, RequestPayload, request_operation

from ..inbound_contracts import Barcode
from ..outbound_contracts import NgLocator, RackLocator, SourceLocator


@request_operation("outbound.material.movement_report@v1", Endpoint.FACTS)
class MovementReport(RequestPayload):
    task_id: Identifier
    source_locator: SourceLocator
    barcode: Barcode
    to_locator: RackLocator | NgLocator
    occurred_at: NonNegative
