"""Transport 回调的业务字段(合同 §5.2、§5.3):校验即规范化,非法一律 422。

模型只做「这条消息自身是否合法」的校验(字段互斥、枚举、长度);消息与冻结任务是否相符
(成员、目标、货架编号)在 `apply.py` 里按冻结事实判断。
"""

from typing import Annotated, Literal

from pydantic import AfterValidator, ConfigDict, Field, field_validator, model_validator

from app.integrations.wms_callback import CallbackPayload


def _no_nul(value: str) -> str:
    if "\x00" in value:
        raise ValueError("NUL is not allowed")
    value.encode(
        "utf-8"
    )  # 孤立代理字符会让响应无法渲染,按非法值拒绝(UnicodeEncodeError 是 ValueError)
    return value


Text = Annotated[str, Field(min_length=1, max_length=256), AfterValidator(_no_nul)]
Face = Annotated[str, Field(min_length=1, max_length=10), AfterValidator(_no_nul)]
FailureCode = Literal[
    "RCS_TASK_REJECTED", "RCS_EXECUTION_FAILED", "POSITION_UNKNOWN", "MANUAL_ABORTED"
]


class RackPositionRef(CallbackPayload):
    kind: Literal["RACK_POSITION"]
    location_code: Text


class RackBinSlotRef(CallbackPayload):
    kind: Literal["RACK_BIN_SLOT"]
    rack_id: Text
    rack_face: Face
    slot_id: Text


class HandoffPositionRef(CallbackPayload):
    kind: Literal["HANDOFF_POSITION"]
    location_code: Text


BinPosition = Annotated[RackBinSlotRef | HandoffPositionRef, Field(discriminator="kind")]


class PositionChanged(CallbackPayload):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "transport_task_id": "transport-019f3400-0e17-7d2a-b944-3cf7953804da",
                    "container_id": "A000002701",
                    "milestone": "TARGET_PLACED",
                    "final_position": {
                        "kind": "RACK_BIN_SLOT",
                        "rack_id": "510021",
                        "rack_face": "90",
                        "slot_id": "A2F1C101",
                    },
                }
            ]
        }
    )

    transport_task_id: Text
    container_id: Text
    milestone: Literal["SOURCE_PICKED", "TARGET_PLACED", "POSITION_UNKNOWN"]
    final_position: BinPosition | None = None

    @model_validator(mode="after")
    def _final_position_only_when_placed(self):
        if (self.milestone == "TARGET_PLACED") != (self.final_position is not None):
            raise ValueError("final_position is required exactly for TARGET_PLACED")
        return self


def _position_or_unknown(model) -> None:
    """FAILED 的位置二选一:明确的 final_position,或 position_unknown=true(配 POSITION_UNKNOWN)。"""
    known, unknown = model.final_position is not None, model.position_unknown is True
    if known == unknown:
        raise ValueError("exactly one of final_position and position_unknown=true is required")
    if unknown != (model.failure_code == "POSITION_UNKNOWN"):
        raise ValueError("position_unknown=true pairs with failure_code=POSITION_UNKNOWN")


class BinResult(CallbackPayload):
    container_id: Text
    status: Literal["SUCCEEDED", "FAILED"]
    final_position: BinPosition | None = None
    position_unknown: Literal[True] | None = None
    failure_code: FailureCode | None = None

    @model_validator(mode="after")
    def _status_rules(self):
        if self.status == "SUCCEEDED":
            if self.final_position is None or self.failure_code or self.position_unknown:
                raise ValueError("SUCCEEDED needs final_position and nothing else")
        else:
            if self.failure_code is None:
                raise ValueError("FAILED needs failure_code")
            _position_or_unknown(self)
        return self


class Resulted(CallbackPayload):
    """货架族(RACK_*)用 rack_id/status 等顶层字段,料箱族(BIN_*)用 results[]。"""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "transport_task_id": "transport-019f3400-0e17-7d2a-b944-3cf7953804da",
                    "kind": "RACK_MOVE",
                    "outcome_revision": 1,
                    "rack_id": "RACK-5F-001",
                    "status": "SUCCEEDED",
                    "final_position": {"kind": "RACK_POSITION", "location_code": "CTU01"},
                }
            ]
        }
    )

    transport_task_id: Text
    kind: Literal["RACK_MOVE", "RACK_ROTATE", "BIN_MOVE", "BIN_EXCHANGE"]
    outcome_revision: Annotated[int, Field(ge=1, le=2**63 - 1)]
    rack_id: Text | None = None
    status: Literal["SUCCEEDED", "FAILED", "CANCELLED"] | None = None
    final_position: RackPositionRef | None = None
    position_unknown: Literal[True] | None = None
    failure_code: FailureCode | None = None
    arrival_face: Face | None = None
    results: list[BinResult] | None = Field(default=None, min_length=1, max_length=4)

    @field_validator("arrival_face", mode="before")
    @classmethod
    def _empty_face_is_absent(cls, value):
        return None if value == "" else value  # 省略、null、空字符串三者等价

    @model_validator(mode="after")
    def _family_rules(self):
        if self.kind in ("RACK_MOVE", "RACK_ROTATE"):
            self._check_rack()
        else:
            self._check_bin()
        return self

    def _check_rack(self) -> None:
        if self.results is not None or self.rack_id is None or self.status is None:
            raise ValueError("rack results need rack_id and status, and no results[]")
        if self.status == "SUCCEEDED":
            if self.final_position is None or self.failure_code or self.position_unknown:
                raise ValueError("SUCCEEDED needs final_position and nothing else")
        elif self.status == "CANCELLED":
            if (
                self.final_position is None
                or self.failure_code
                or self.position_unknown
                or self.arrival_face
            ):
                raise ValueError("CANCELLED needs final_position only")
        else:
            if self.failure_code is None:
                raise ValueError("FAILED needs failure_code")
            _position_or_unknown(self)
            if self.position_unknown and self.arrival_face:
                raise ValueError("unknown position cannot carry arrival_face")

    def _check_bin(self) -> None:
        rack_only = (
            self.rack_id,
            self.status,
            self.final_position,
            self.position_unknown,
            self.failure_code,
            self.arrival_face,
        )
        if self.results is None or any(item is not None for item in rack_only):
            raise ValueError("bin results need results[] and no rack-level fields")
        ids = [item.container_id for item in self.results]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate container_id")
        self.results = sorted(self.results, key=lambda item: item.container_id)
