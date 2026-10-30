"""Frozen scan-acceptance payload; business validation and commands are separate stages."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from app.integrations.ecs_callback import CallbackPayload


class ScanCompletedData(CallbackPayload):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "location": "RS-INPUT-ARM-01",
                    "barcode": "TEST-REEL-001",
                    "scan_time": 1793313931285,
                    "size_inch": 7,
                    "thickness_mm": 15.3,
                    "result": "OK",
                }
            ]
        }
    )

    barcode: str
    size_inch: int | FiniteFloat = Field(description="料盘直径，单位为英寸")
    thickness_mm: int | FiniteFloat = Field(description="料盘厚度，单位为毫米")


NonemptyText = Annotated[str, Field(min_length=1)]


class ValidateBindDecision(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    result: Literal["OK", "NG"]
    binding_id: NonemptyText | None = None
    reason_code: NonemptyText | None = None

    @model_validator(mode="after")
    def check_decision(self) -> Self:
        if self.result == "OK":
            if self.binding_id is None or self.reason_code is not None:
                raise ValueError("OK requires binding_id and forbids reason_code")
        elif self.reason_code is None or self.binding_id is not None:
            raise ValueError("NG requires reason_code and forbids binding_id")
        return self
