from app.businesses._components.sorting_task.contracts import Face, Identifier, NonNegative
from app.integrations.wms_request import Endpoint, RequestPayload, request_operation

from ..inbound_contracts import Barcode


@request_operation("outbound.rackslot.material.decide@v1", Endpoint.DECISIONS)
class MaterialDecide(RequestPayload):
    task_id: Identifier
    rack_id: Identifier
    rack_face: Face
    slot_id: Identifier
    barcode: Barcode
    scanned_at: NonNegative
