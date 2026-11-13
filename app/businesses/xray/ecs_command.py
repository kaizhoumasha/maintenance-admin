from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, RootModel, ValidationInfo, model_validator

from app.integrations.ecs_command import ecs_command
from app.integrations.ecs_command.contracts import ErrorDetail

from .contracts import (
    BinActual,
    Incoming,
    LabelVariables,
    LegacyBinActual,
    LegacyBinTarget,
    NonnegativeInt,
    OmitAbsent,
    PositiveNumber,
    RackActual,
    RackTarget,
    Strict,
    Text,
    UUIDText,
)
from .definition import OUTPUT_ARM, XRAY


class XrayError(ErrorDetail):
    code: Text
    msg: Text


class ReelResult(Incoming):
    model_config = ConfigDict(extra="ignore")

    reel_id: UUIDText

    @model_validator(mode="after")
    def matches_outcome(self, info: ValidationInfo) -> Self:
        result = (info.context or {}).get("result")
        if result and (result == "FAILED") != isinstance(self, Failed):
            raise ValueError("data must match result")
        return self


class RackResultTarget(RackActual):
    model_config = ConfigDict(extra="ignore")


class BinResultTarget(BinActual):
    model_config = ConfigDict(extra="ignore")


class LegacyBinResultTarget(LegacyBinActual):
    model_config = ConfigDict(extra="ignore")


class Failed(ReelResult):
    position: Literal["SOURCE", "GRIPPER", "TARGET", "OTHER", "UNKNOWN"] | None = None
    failure_stage: Text | None = None
    actual_target: RackResultTarget | LegacyBinResultTarget | None = None


class PutFailed(Failed):
    actual_qty: Annotated[int, Field(ge=0, le=0)] | None = None


class Counted(ReelResult):
    decision: Literal["COUNT_REQUIRED"]
    qty: NonnegativeInt


class Bypassed(ReelResult):
    decision: Literal["BYPASS_COUNT"]
    mode: Literal["BYPASS"]


class Labelled(ReelResult):
    decision: Literal["OK"]
    label_status: Literal["APPLIED"]


class Verified(ReelResult):
    decision: Literal["OK"]
    mode: Literal["COUNTED"]


class Discharged(ReelResult):
    decision: Literal["NG"]
    discharged: bool

    @model_validator(mode="after")
    def actually_discharged(self) -> Self:
        if not self.discharged:
            raise ValueError("NG success requires actual discharge")
        return self


class Placed(ReelResult):
    actual_qty: Annotated[int, Field(ge=1, le=1)]
    position: Literal["TARGET"]


class LegacyPlacedInBin(Placed):
    actual_target: LegacyBinResultTarget


class Outcome[T](RootModel[T]):
    model_config = {"strict": True}


class LegacyCommand(Strict):
    reel_id: UUIDText
    source_event_id: UUIDText


class DecisionReason(OmitAbsent):
    reason: Text | None = None

    @model_validator(mode="after")
    def ng_reason(self) -> Self:
        if (self.decision == "NG") != (self.reason is not None):
            raise ValueError("only NG requires reason")
        return self


@ecs_command(
    "APPLY_PKG_DECISION",
    device_roles=(XRAY,),
    annex_id="xray.apply_pkg_decision",
    annex_version="2",
    result=Outcome[Counted | Bypassed | Discharged | Failed],
    error=XrayError,
    timeout_min_ms=1,
    timeout_max_ms=2**63 - 1,
)
class LegacyPkgDecision(DecisionReason, LegacyCommand):
    decision: Literal["COUNT_REQUIRED", "BYPASS_COUNT", "NG"]


class Label(Strict):
    variables: LabelVariables


@ecs_command(
    "APPLY_COUNT_DECISION",
    device_roles=(XRAY,),
    annex_id="xray.apply_count_decision",
    annex_version="2",
    result=Outcome[Labelled | Discharged | Failed],
    error=XrayError,
    timeout_min_ms=1,
    timeout_max_ms=2**63 - 1,
)
class LegacyCountDecision(DecisionReason, LegacyCommand):
    decision: Literal["OK", "NG"]
    label: Label | None = None

    @model_validator(mode="after")
    def ok_label(self) -> Self:
        if (self.decision == "OK") != (self.label is not None):
            raise ValueError("only OK requires label")
        return self


class MaterialCommand(Strict):
    barcode: Text
    source_event_id: UUIDText


class MaterialCounted(Counted):
    barcode: Text


class MaterialBypassed(Bypassed):
    barcode: Text


class MaterialLabelled(Labelled):
    barcode: Text


class MaterialVerified(Verified):
    barcode: Text


class MaterialDischarged(Discharged):
    barcode: Text


class MaterialFailed(Failed):
    barcode: Text
    actual_target: RackResultTarget | BinResultTarget | None = None


class MaterialPutFailed(MaterialFailed):
    actual_qty: Annotated[int, Field(ge=0, le=0)] | None = None


class PlacedInBin(Placed):
    barcode: Text
    actual_target: BinResultTarget


class LegacyPlacedInRack(Placed):
    actual_target: RackResultTarget


class PlacedInRack(LegacyPlacedInRack):
    barcode: Text


@ecs_command(
    "APPLY_PKG_DECISION",
    device_roles=(XRAY,),
    annex_id="xray.apply_pkg_decision",
    annex_version="3",
    result=Outcome[MaterialCounted | MaterialBypassed | MaterialDischarged | MaterialFailed],
    error=XrayError,
    timeout_min_ms=1,
    timeout_max_ms=2**63 - 1,
)
class ApplyPkgDecision(DecisionReason, MaterialCommand):
    decision: Literal["COUNT_REQUIRED", "BYPASS_COUNT", "NG"]
    reel_thickness: PositiveNumber | None = None

    @model_validator(mode="after")
    def count_thickness(self) -> Self:
        if (self.decision == "COUNT_REQUIRED") != (self.reel_thickness is not None):
            raise ValueError("only COUNT_REQUIRED requires reel_thickness")
        return self


@ecs_command(
    "APPLY_COUNT_DECISION",
    device_roles=(XRAY,),
    annex_id="xray.apply_count_decision",
    annex_version="3",
    result=Outcome[MaterialLabelled | MaterialDischarged | MaterialFailed],
    error=XrayError,
    timeout_min_ms=1,
    timeout_max_ms=2**63 - 1,
)
class ApplyCountDecision(DecisionReason, MaterialCommand):
    decision: Literal["OK", "NG"]
    label: Label | None = None

    @model_validator(mode="after")
    def ok_label(self) -> Self:
        if (self.decision == "OK") != (self.label is not None):
            raise ValueError("only OK requires label")
        return self


@ecs_command(
    "APPLY_VERIFY_DECISION",
    device_roles=(XRAY,),
    annex_id="xray.apply_verify_decision",
    annex_version="3",
    result=Outcome[MaterialVerified | MaterialDischarged | MaterialFailed],
    error=XrayError,
    timeout_min_ms=1,
    timeout_max_ms=2**63 - 1,
)
class ApplyVerifyDecision(DecisionReason, MaterialCommand):
    decision: Literal["OK", "NG"]


@ecs_command(
    "PUT_TO_BIN",
    device_roles=(OUTPUT_ARM,),
    annex_id="xray.put_to_bin",
    annex_version="3",
    result=Outcome[PlacedInBin | MaterialPutFailed],
    error=XrayError,
    timeout_min_ms=1,
    timeout_max_ms=2**63 - 1,
)
class PutToBin(MaterialCommand):
    target: BinActual


@ecs_command(
    "PUT_TO_RACK",
    device_roles=(OUTPUT_ARM,),
    annex_id="xray.put_to_rack",
    annex_version="3",
    result=Outcome[PlacedInRack | MaterialPutFailed],
    error=XrayError,
    timeout_min_ms=1,
    timeout_max_ms=2**63 - 1,
)
class PutToRack(MaterialCommand):
    target: RackTarget


@ecs_command(
    "APPLY_VERIFY_DECISION",
    device_roles=(XRAY,),
    annex_id="xray.apply_verify_decision",
    annex_version="2",
    result=Outcome[Verified | Discharged | Failed],
    error=XrayError,
    timeout_min_ms=1,
    timeout_max_ms=2**63 - 1,
)
class LegacyVerifyDecision(DecisionReason, LegacyCommand):
    decision: Literal["OK", "NG"]


@ecs_command(
    "PUT_TO_BIN",
    device_roles=(OUTPUT_ARM,),
    annex_id="xray.put_to_bin",
    annex_version="2",
    result=Outcome[LegacyPlacedInBin | PutFailed],
    error=XrayError,
    timeout_min_ms=1,
    timeout_max_ms=2**63 - 1,
)
class LegacyPutToBin(LegacyCommand):
    target: LegacyBinTarget


@ecs_command(
    "PUT_TO_RACK",
    device_roles=(OUTPUT_ARM,),
    annex_id="xray.put_to_rack",
    annex_version="2",
    result=Outcome[LegacyPlacedInRack | PutFailed],
    error=XrayError,
    timeout_min_ms=1,
    timeout_max_ms=2**63 - 1,
)
class LegacyPutToRack(LegacyCommand):
    target: RackTarget
