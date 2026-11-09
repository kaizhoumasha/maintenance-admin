from typing import Annotated, Literal, Self

from pydantic import Field, RootModel, ValidationInfo, model_validator

from app.integrations.ecs_command import ecs_command
from app.integrations.ecs_command.contracts import ErrorDetail

from .contracts import (
    BinActual,
    BinTarget,
    Incoming,
    LabelVariables,
    NonnegativeInt,
    OmitAbsent,
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
    reelId: UUIDText


class Failed(ReelResult):
    position: Literal["SOURCE", "GRIPPER", "TARGET", "OTHER", "UNKNOWN"] | None = None
    failureStage: Text | None = None
    actualTarget: RackActual | BinActual | None = None


class PutFailed(Failed):
    actualQty: Annotated[int, Field(ge=0, le=0)] | None = None


class Counted(ReelResult):
    decision: Literal["COUNT_REQUIRED"]
    qty: NonnegativeInt


class Bypassed(ReelResult):
    decision: Literal["BYPASS_COUNT"]
    mode: Literal["BYPASS"]


class Labelled(ReelResult):
    decision: Literal["OK"]
    labelStatus: Literal["APPLIED"]


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
    actualQty: Annotated[int, Field(ge=1, le=1)]
    position: Literal["TARGET"]


class PlacedInBin(Placed):
    actualTarget: BinActual


class PlacedInRack(Placed):
    actualTarget: RackActual


class Outcome[T](RootModel[T]):
    model_config = {"strict": True}

    @model_validator(mode="after")
    def matches_outcome(self, info: ValidationInfo) -> Self:
        result = (info.context or {}).get("result")
        if result and (result == "FAILED") != isinstance(self.root, Failed):
            raise ValueError("data must match result")
        return self


class Command(Strict):
    reelId: UUIDText
    sourceEventId: UUIDText


class Decision(OmitAbsent, Command):
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
    annex_version="1",
    result=Outcome[Counted | Bypassed | Discharged | Failed],
    error=XrayError,
    timeout_min_ms=1,
    timeout_max_ms=2**63 - 1,
)
class ApplyPkgDecision(Decision):
    decision: Literal["COUNT_REQUIRED", "BYPASS_COUNT", "NG"]


class Label(Strict):
    variables: LabelVariables


@ecs_command(
    "APPLY_COUNT_DECISION",
    device_roles=(XRAY,),
    annex_id="xray.apply_count_decision",
    annex_version="1",
    result=Outcome[Labelled | Discharged | Failed],
    error=XrayError,
    timeout_min_ms=1,
    timeout_max_ms=2**63 - 1,
)
class ApplyCountDecision(Decision):
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
    annex_version="1",
    result=Outcome[Verified | Discharged | Failed],
    error=XrayError,
    timeout_min_ms=1,
    timeout_max_ms=2**63 - 1,
)
class ApplyVerifyDecision(Decision):
    decision: Literal["OK", "NG"]


@ecs_command(
    "PUT_TO_BIN",
    device_roles=(OUTPUT_ARM,),
    annex_id="xray.put_to_bin",
    annex_version="1",
    result=Outcome[PlacedInBin | PutFailed],
    error=XrayError,
    timeout_min_ms=1,
    timeout_max_ms=2**63 - 1,
)
class PutToBin(Command):
    target: BinTarget


@ecs_command(
    "PUT_TO_RACK",
    device_roles=(OUTPUT_ARM,),
    annex_id="xray.put_to_rack",
    annex_version="1",
    result=Outcome[PlacedInRack | PutFailed],
    error=XrayError,
    timeout_min_ms=1,
    timeout_max_ms=2**63 - 1,
)
class PutToRack(Command):
    target: RackTarget
