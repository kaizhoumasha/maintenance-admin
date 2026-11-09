from typing import Literal, Self

from pydantic import model_validator

from app.integrations.wms_request import Endpoint, request_operation

from ..contracts import DECISIONS, NonnegativeInt, OmitAbsent, Text
from ..request_data import EventRequest

OPERATION = "inbound.xray.verify_decide@v1"


@request_operation(
    OPERATION, Endpoint.DECISIONS, requires_business_owner=True, decision_data=DECISIONS["VERIFY"]
)
class VerifyDecide(OmitAbsent, EventRequest):
    scan_status: Literal["OK", "NG"]
    barcode: Text
    new_barcode: Text | None = None
    reason_code: Text | None = None
    reason_message: Text | None = None
    scanned_at: NonnegativeInt

    @model_validator(mode="after")
    def scan_fields(self) -> Self:
        if self.scan_status == "OK":
            valid = self.new_barcode is not None and self.reason_code is self.reason_message is None
        else:
            valid = self.reason_code is not None and self.reason_message is not None
        if not valid:
            raise ValueError("invalid verify evidence for scan_status")
        return self
