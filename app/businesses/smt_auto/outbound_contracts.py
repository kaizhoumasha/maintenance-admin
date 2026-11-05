"""Confirmed outbound wire shapes; no inferred device empty or NG mapping."""

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, model_validator

from app.businesses._components.sorting_task.contracts import Face, Identifier

from .work_plan_contracts import Data, Wait


class BinLocator(Data):
    type: Literal["BIN_CELL"]
    bin_code: Identifier
    cell_id: Identifier


class RackLocator(Data):
    type: Literal["RACK_SLOT"]
    rack_id: Identifier
    rack_face: Face
    slot_id: Identifier


class NgLocator(Data):
    type: Literal["NG_ZONE"]
    zone_code: Identifier


SourceLocator = Annotated[BinLocator | RackLocator, Field(discriminator="type")]


class Target(RackLocator):
    rack_layer: int
    rack_column: int


class Accept(Data):
    result: Literal["ACCEPT"]
    target_locator: Target
    target_preparation: Literal["ROTATE", "REPLACE"] | None = None
    next_source_action: dict | None = None

    @model_validator(mode="before")
    @classmethod
    def optional_fields(cls, value):
        if isinstance(value, dict):
            for key in ("target_preparation", "next_source_action"):
                if key in value and value[key] is None:
                    raise ValueError("optional fields must be absent, not null")
        return value


class BinAccept(Accept):
    pass


class RackAccept(Accept):
    pass


class BinReject(Data):
    result: Literal["REJECT"]
    business_exception_code: Literal["MATERIAL_REJECTED", "SOURCE_CELL_MISMATCH"]
    ng_locator: NgLocator
    source_disposition: Literal["CONTINUE", "CLOSE"]

    @model_validator(mode="after")
    def mismatch(self):
        if (
            self.business_exception_code == "SOURCE_CELL_MISMATCH"
            and self.source_disposition != "CLOSE"
        ):
            raise ValueError("mismatched Cell closes")
        return self


class RackReject(Data):
    result: Literal["REJECT"]
    business_exception_code: Literal["MATERIAL_REJECTED"]
    ng_locator: NgLocator
    source_disposition: Literal["CLOSE"]


class Retry(Data):
    result: Literal["RETRY"]


class SourceDone(Data):
    result: Literal["SOURCE_DONE"]


EMPTY_DECISION = TypeAdapter(Annotated[Retry | SourceDone | Wait, Field(discriminator="result")])
DECISIONS = {
    "bin": TypeAdapter(Annotated[BinAccept | BinReject | Wait, Field(discriminator="result")]),
    "rack": TypeAdapter(Annotated[RackAccept | RackReject | Wait, Field(discriminator="result")]),
}
