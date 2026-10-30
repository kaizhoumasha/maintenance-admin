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
                    "reel_diameter": 7,
                    "reel_thickness": 15.3,
                }
            ]
        }
    )

    workline_code: NonemptyText
    barcode: str
    reel_diameter: int | FiniteFloat = Field(description="料盘直径，单位为英寸")
    reel_thickness: int | FiniteFloat = Field(description="料盘厚度，单位为毫米")
