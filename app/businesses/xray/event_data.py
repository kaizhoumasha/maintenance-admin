from typing import Literal, Self

from pydantic import model_validator

from app.integrations.wms_callback.operation import CallbackPayload

from .contracts import Incoming, Mode, NgStage, NonnegativeInt, Text, UUIDText


class ReelEventData(Incoming, CallbackPayload):
    model_config = Incoming.model_config

    event_id: UUIDText
    reel_id: UUIDText


class DeviceOutcome(ReelEventData):
    reason_code: Text | None = None
    reason_message: Text | None = None

    def check_reason(self, succeeded: bool):
        if succeeded:
            valid = self.reason_code is None and self.reason_message is None
        else:
            valid = self.reason_code is not None and self.reason_message is not None
        if not valid:
            raise ValueError("only NG requires both reason fields")


class ScanData(DeviceOutcome):
    scan_stage: Literal["ENTRY", "VERIFY"]
    scan_status: Literal["OK", "NG"]
    barcode: Text | None = None
    new_barcode: Text | None = None
    old_barcode: Text | None = None
    template_id: Literal["XX123456789"] | None = None

    @model_validator(mode="before")
    @classmethod
    def no_null(cls, value):
        if (
            isinstance(value, dict)
            and value.get("scan_stage") == "ENTRY"
            and value.get("scan_status") == "NG"
            and value.get("barcode") is None
        ):
            value = {key: item for key, item in value.items() if key != "barcode"}
        return super().no_null(value)

    @model_validator(mode="after")
    def stage_fields(self) -> Self:
        ok = self.scan_status == "OK"
        self.check_reason(ok)
        if self.scan_stage == "ENTRY":
            valid = self.template_id is not None and self.new_barcode is self.old_barcode is None
            valid = valid and (not ok or self.barcode is not None)
        else:
            valid = self.barcode is not None and (not ok or self.new_barcode is not None)
            valid = valid and self.old_barcode == self.barcode
        if not valid:
            raise ValueError("invalid scan fields for this stage")
        return self


class CountData(DeviceOutcome):
    barcode: Text
    template_id: Literal["XX123456789"]
    count_status: Literal["OK", "NG"]
    qty: NonnegativeInt | None = None

    @model_validator(mode="after")
    def quantity_and_reason(self) -> Self:
        ok = self.count_status == "OK"
        self.check_reason(ok)
        if ok != (self.qty is not None):
            raise ValueError("only OK requires qty")
        return self


class ArrivalData(ReelEventData):
    barcode: Text
    mode: Mode
    ready_for_pick: bool
    new_barcode: Text | None = None

    @model_validator(mode="after")
    def pickup_evidence(self) -> Self:
        if not self.ready_for_pick or (self.mode == "COUNTED") != (self.new_barcode is not None):
            raise ValueError("expected a ready reel with mode-specific barcodes")
        if self.new_barcode is not None and self.barcode != self.new_barcode:
            raise ValueError("COUNTED barcode must match the read label")
        return self


class NgDischargedData(ReelEventData):
    stage: NgStage
    barcode: Text | None = None
    reason: Text
    reason_code: Text | None = None

    @model_validator(mode="before")
    @classmethod
    def no_null(cls, value):
        if isinstance(value, dict) and value.get("barcode") is None:
            value = {key: item for key, item in value.items() if key != "barcode"}
        return super().no_null(value)


class NgRequestedData(ReelEventData):
    source_event_id: UUIDText
    reason_code: Text
    reason_message: Text
