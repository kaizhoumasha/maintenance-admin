from app.businesses._components.sorting_task.contracts import Identifier, NonNegative
from app.integrations.wms_request import Endpoint, RequestPayload, request_operation

from ..inbound_contracts import Barcode, Source
from ..work_plan_contracts import Data


class Target(Data):
    bin_code: Identifier
    cell_id: Identifier


@request_operation("putaway.material.movement_report@v1", Endpoint.FACTS)
class MovementReport(RequestPayload):
    task_id: Identifier
    source: Source
    barcode: Barcode
    target: Target
    occurred_at: NonNegative
