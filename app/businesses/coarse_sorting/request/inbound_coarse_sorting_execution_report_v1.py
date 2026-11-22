"""Candidate original completion report. Formal registration awaits WMS signoff."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from app.integrations.ecs_command.contracts import ErrorDetail
from app.integrations.wms_request import RequestPayload

from ..contracts import BinLocation, CellIndex, Reference


class ExecutionReport(RequestPayload):
    workline_code: Reference
    binding_id: Reference
    bin_code: Reference
    bin_location: BinLocation
    cell_id: Reference
    cell_index: CellIndex
    completed_at: Annotated[int, Field(ge=0, le=2**63 - 1)]
    result: Literal["SUCCESS", "FAILED"] = "SUCCESS"
    error_detail: ErrorDetail | None = None
    placement_status: Literal["CONFIRMED_PLACED", "CONFIRMED_NOT_PLACED", "UNKNOWN"] = (
        "CONFIRMED_PLACED"
    )

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.result == "SUCCESS":
            if self.error_detail is not None or self.placement_status != "CONFIRMED_PLACED":
                raise ValueError("SUCCESS requires confirmed placement and forbids error_detail")
        elif self.error_detail is None or self.placement_status == "CONFIRMED_PLACED":
            raise ValueError("FAILED requires error_detail and a non-placed status")
        return self
