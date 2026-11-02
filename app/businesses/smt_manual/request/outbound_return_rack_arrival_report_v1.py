from typing import Annotated, Literal

from pydantic import Field

from app.businesses._components.sorting_task.contracts import Face, Identifier, Positive
from app.integrations.wms_request import Endpoint, RequestPayload, request_operation

OPERATION = "outbound.return_rack.arrival_report@v1"


class Position(RequestPayload):
    type: Literal["RACK_POSITION"]
    location_code: Identifier


@request_operation(OPERATION, Endpoint.FACTS)
class ArrivalReport(RequestPayload):
    task_id: Identifier
    transport_task_id: Annotated[str, Field(min_length=1, max_length=80)]
    outcome_revision: Positive
    rack_id: Identifier
    final_position: Position
    arrival_face: Face
