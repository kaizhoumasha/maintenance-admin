from typing import Self

from pydantic import model_validator

from app.integrations.wms_request import Endpoint, request_operation

from ..contracts import DECISIONS, Mode, NonnegativeInt, OmitAbsent, Text
from ..request_data import EventRequest

OPERATION = "inbound.xray.put_target_decide@v1"


@request_operation(
    OPERATION, Endpoint.DECISIONS, requires_business_owner=True, decision_data=DECISIONS["OUTLET"]
)
class PutTargetDecide(OmitAbsent, EventRequest):
    mode: Mode
    barcode: Text
    new_barcode: Text | None = None
    observed_at: NonnegativeInt

    @model_validator(mode="after")
    def mode_fields(self) -> Self:
        if (self.mode == "COUNTED") != (self.new_barcode is not None):
            raise ValueError("only COUNTED requires new_barcode")
        return self
