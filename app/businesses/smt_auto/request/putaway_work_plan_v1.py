from typing import Annotated

from pydantic import Field, model_validator

from app.businesses._components.sorting_task.contracts import Identifier, NonNegative
from app.integrations.wms_request import Endpoint, RequestPayload, request_operation


class SourceRack(RequestPayload):
    rack_id: Identifier
    location_code: Identifier


@request_operation("putaway.work_plan@v1", Endpoint.DECISIONS)
class WorkPlan(RequestPayload):
    task_id: Identifier
    target_bin_code: Identifier
    scanned_at: NonNegative
    source_racks: Annotated[list[SourceRack], Field(max_length=2)]

    @model_validator(mode="after")
    def unique_sources(self):
        if any(
            len({getattr(rack, field) for rack in self.source_racks}) != len(self.source_racks)
            for field in ("rack_id", "location_code")
        ):
            raise ValueError("duplicate source rack or position")
        return self
