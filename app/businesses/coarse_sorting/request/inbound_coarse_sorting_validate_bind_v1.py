"""Forward raw scan facts to WMS and validate its binding decision."""

from typing import Annotated

from pydantic import ConfigDict, Field, FiniteFloat

from app.integrations.wms_request import Endpoint, RequestPayload, request_operation

NonemptyText = Annotated[str, Field(min_length=1)]


@request_operation("inbound.coarse_sorting.validate_bind@v1", Endpoint.DECISIONS)
class ValidateBind(RequestPayload):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "workline_code": "COARSE-A",
                    "barcode": "TEST-REEL-001",
                    "reel_diameter": 15,
                    "reel_thickness": 20,
                }
            ]
        }
    )

    workline_code: NonemptyText
    barcode: str
    reel_diameter: int | FiniteFloat
    reel_thickness: int | FiniteFloat
