"""Candidate original completion report. Formal registration awaits WMS signoff."""

from typing import Annotated

from pydantic import Field

from app.integrations.wms_request import RequestPayload

from .inbound_coarse_sorting_storage_allocate_v1 import BinLocation, Reference

OPERATION = "inbound.coarse_sorting.execution_report@v1"


class ExecutionReport(RequestPayload):
    workline_code: Reference
    binding_id: Reference
    bin_code: Reference
    bin_location: BinLocation
    cell_id: Reference
    completed_at: Annotated[int, Field(ge=0, le=2**63 - 1)]
