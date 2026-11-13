from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    TypeAdapter,
    model_serializer,
    model_validator,
)

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")]
Text = Annotated[str, Field(pattern=r"^[^\x00]+$")]
UUIDText = Annotated[
    str,
    Field(
        pattern=r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$",
    ),
]
OperationId = Annotated[
    str,
    Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    ),
]
NonnegativeInt = Annotated[int, Field(ge=0, le=2**63 - 1)]
PositiveInt = Annotated[int, Field(gt=0, le=2**63 - 1)]
PositiveNumber = Annotated[FiniteFloat, Field(gt=0)]
Diameter = Annotated[int, Field(ge=7, le=15)]
Face = Literal["A", "B"]
BinSlot = Literal["A", "B", "C", "D"]
Mode = Literal["COUNTED", "BYPASS"]
NgStage = Literal["ENTRY", "COUNT", "LABEL", "VERIFY"]


class Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class OmitAbsent:
    @model_serializer(mode="wrap")
    def omit_absent(self, handler):
        return {key: value for key, value in handler(self).items() if value is not None}


class Incoming(Strict):
    @model_validator(mode="before")
    @classmethod
    def no_null(cls, value):
        if isinstance(value, dict) and any(
            item is None and key in cls.model_fields for key, item in value.items()
        ):
            raise ValueError("omit absent fields")
        return value


class LabelVariables(Strict):
    ID: Literal["XX123456789"]
    PHHPN: Text
    SPKG: Text
    MMfrPN: Text
    VVendor: Text
    QQTYPC: Text
    Location: Annotated[str, Field(pattern=r"^[^\x00]*$")]
    DDC: Text
    DateTime: Text
    LLC: Text


class RackPosition(Strict):
    rack_id: Identifier
    rack_face: Face
    slot_id: Identifier
    rack_layer: Annotated[int, Field(ge=1, le=5)]
    rack_column: PositiveInt


class RackLocator(RackPosition):
    type: Literal["RACK_SLOT"]
    location_code: Identifier


class TransitLocator(Strict):
    type: Literal["TRANSIT_BIN"]


class RackActual(RackPosition):
    location_type: Literal["RETURN_RACK"]
    location_id: Identifier


class RackTarget(RackActual):
    reel_diameter: Diameter | FiniteFloat
    reel_thickness: PositiveNumber

    @model_validator(mode="after")
    def check_diameter(self) -> Self:
        if self.reel_diameter not in (7, 13, 15):
            raise ValueError("reel_diameter must be 7, 13 or 15")
        return self


class BinActual(Strict):
    location_id: Text
    location_type: Literal["ONE_LAYER_RACK"]
    bin_type: Literal["1"]
    bin_location: BinSlot
    bin_cell_location: Annotated[int, Field(ge=8, le=8)]


class LegacyBinActual(BinActual):
    rack_id: Text
    bin_id: Text


class LegacyBinTarget(OmitAbsent, LegacyBinActual):
    reel_layer: NonnegativeInt | None = None
    reel_thickness: PositiveNumber | None = None
    reel_diameter: Diameter | FiniteFloat | None = None
    reel_totalthickness: Annotated[FiniteFloat, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def check_diameter(self) -> Self:
        if self.reel_diameter is not None and self.reel_diameter not in (7, 13, 15):
            raise ValueError("reel_diameter must be 7, 13 or 15")
        return self


class Wait(Strict):
    result: Literal["WAIT"]
    retry_after_ms: Annotated[int, Field(ge=1, le=60000)]


class Ng(Strict):
    result: Literal["NG"]
    reason_code: Text
    reason_message: Text


class CountRequired(Strict):
    result: Literal["COUNT_REQUIRED"]
    reel_thickness: PositiveNumber


class Bypass(Strict):
    result: Literal["BYPASS_COUNT"]


class VerifyOk(Strict):
    result: Literal["OK"]


class CountOk(VerifyOk):
    label_variables: LabelVariables


class TransitAccept(Strict):
    result: Literal["ACCEPT"]
    target_locator: TransitLocator


class RackAccept(OmitAbsent, Incoming):
    result: Literal["ACCEPT"]
    target_locator: RackLocator
    reel_diameter: Diameter
    reel_thickness: PositiveNumber
    target_preparation: Literal["ROTATE", "REPLACE"] | None = None

    @model_validator(mode="after")
    def check_diameter(self) -> Self:
        if self.reel_diameter not in (7, 13, 15):
            raise ValueError("reel_diameter must be 7, 13 or 15")
        return self


class CurrentRack(Strict):
    rack_id: Identifier


class IncomingRack(CurrentRack):
    target_face: Face


class RackPreparation(Strict):
    location_code: Identifier


class Replenish(RackPreparation):
    result: Literal["REPLENISH_REQUIRED"]
    incoming_rack: IncomingRack


class Rotate(RackPreparation):
    result: Literal["ROTATE_REQUIRED"]
    current_rack: CurrentRack
    target_face: Face


class Replace(RackPreparation):
    result: Literal["REPLACE_REQUIRED"]
    current_rack: CurrentRack
    incoming_rack: IncomingRack

    @model_validator(mode="after")
    def different_racks(self) -> Self:
        if self.current_rack.rack_id == self.incoming_rack.rack_id:
            raise ValueError("replacement requires two different racks")
        return self


class Depart(RackPreparation):
    result: Literal["DEPART_REQUIRED"]
    current_rack: CurrentRack


DECISIONS = {
    "ENTRY": TypeAdapter(CountRequired | Bypass | Ng | Wait),
    "COUNT": TypeAdapter(CountOk | Ng | Wait),
    "VERIFY": TypeAdapter(VerifyOk | Ng | Wait),
    "OUTLET": TypeAdapter(
        TransitAccept | RackAccept | Replenish | Rotate | Replace | Depart | Wait
    ),
}
