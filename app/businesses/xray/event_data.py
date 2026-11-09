from typing import Literal, Self

from pydantic import model_validator

from app.integrations.wms_callback.operation import CallbackPayload

from .contracts import Incoming, Mode, NgStage, NonnegativeInt, Text, UUIDText


class ReelEventData(Incoming, CallbackPayload):
    model_config = Incoming.model_config

    eventId: UUIDText
    reelId: UUIDText


class DeviceOutcome(ReelEventData):
    reasonCode: Text | None = None
    reasonMessage: Text | None = None

    def check_reason(self, succeeded: bool):
        if succeeded:
            valid = self.reasonCode is None and self.reasonMessage is None
        else:
            valid = self.reasonCode is not None and self.reasonMessage is not None
        if not valid:
            raise ValueError("only NG requires both reason fields")


class ScanData(DeviceOutcome):
    scanStage: Literal["ENTRY", "VERIFY"]
    scanStatus: Literal["OK", "NG"]
    barcode: Text | None = None
    new_barcode: Text | None = None
    templateId: Literal["XX123456789"] | None = None

    @model_validator(mode="after")
    def stage_fields(self) -> Self:
        ok = self.scanStatus == "OK"
        self.check_reason(ok)
        if self.scanStage == "ENTRY":
            valid = self.templateId is not None and self.new_barcode is None
            valid = valid and (not ok or self.barcode is not None)
        else:
            valid = self.barcode is not None and (not ok or self.new_barcode is not None)
        if not valid:
            raise ValueError("invalid scan fields for this stage")
        return self


class CountData(DeviceOutcome):
    barcode: Text
    templateId: Literal["XX123456789"]
    countStatus: Literal["OK", "NG"]
    qty: NonnegativeInt | None = None

    @model_validator(mode="after")
    def quantity_and_reason(self) -> Self:
        ok = self.countStatus == "OK"
        self.check_reason(ok)
        if ok != (self.qty is not None):
            raise ValueError("only OK requires qty")
        return self


class ArrivalData(ReelEventData):
    barcode: Text
    mode: Mode
    readyForPick: bool
    new_barcode: Text | None = None

    @model_validator(mode="after")
    def pickup_evidence(self) -> Self:
        if not self.readyForPick or (self.mode == "COUNTED") != (self.new_barcode is not None):
            raise ValueError("expected a ready reel with mode-specific barcodes")
        return self


class NgDischargedData(ReelEventData):
    stage: NgStage
    barcode: Text | None = None
    reason: Text
    reasonCode: Text | None = None


class NgRequestedData(ReelEventData):
    sourceEventId: UUIDText
    reasonCode: Text
    reasonMessage: Text
