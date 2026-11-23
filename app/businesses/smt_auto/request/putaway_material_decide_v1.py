from typing import Annotated

from pydantic import Field

from app.businesses._components.sorting_task.contracts import Identifier, NonNegative
from app.integrations.wms_request import Endpoint, RequestPayload, request_operation

from ..inbound_contracts import Barcode, Source
from .putaway_work_plan_v1 import SourceRack


@request_operation("putaway.material.decide@v1", Endpoint.DECISIONS)
class MaterialDecide(RequestPayload):
    task_id: Identifier
    target_bin_code: Identifier
    source: Source
    barcode: Barcode
    scanned_at: NonNegative
    source_racks: Annotated[list[SourceRack], Field(max_length=2)]
