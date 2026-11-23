from app.businesses._components.sorting_task.contracts import Identifier, NonNegative
from app.integrations.wms_request import Endpoint, RequestPayload, request_operation

from ..inbound_contracts import Source


@request_operation("putaway.source.empty_decide@v1", Endpoint.DECISIONS)
class EmptyDecide(RequestPayload):
    task_id: Identifier
    source: Source
    observed_at: NonNegative
