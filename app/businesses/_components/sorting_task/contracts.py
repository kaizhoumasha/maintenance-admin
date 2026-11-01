"""Frozen v1 sorting task fields; original owners select the resource branch."""

from typing import Annotated, ClassVar, Literal, Self

from pydantic import ConfigDict, Field, model_serializer, model_validator

from app.integrations.wms_callback import CallbackPayload, Rejected

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")]
Face = Annotated[str, Field(min_length=1, max_length=10, pattern=r"^[^\x00]+$")]
Positive = Annotated[int, Field(gt=0, le=2**63 - 1)]
NonNegative = Annotated[int, Field(ge=0, le=2**63 - 1)]
Faces = Annotated[list[Face], Field(min_length=1)]

# Known fields are never discarded as if they were an unknown comment.
_TASK_FIELDS = frozenset(
    {
        "task_id",
        "task_type",
        "business_type",
        "workline_code",
        "queue_revision",
        "dispatch_sequence",
        "not_before",
        "plan_revision",
        "target_rack",
        "added_bin_source_racks",
        "added_direct_picks",
        "added_source_racks",
        "added_return_racks",
        "cancel_scope",
        "bin_source_racks",
        "source_racks",
        "direct_pick_sources",
        "last_applied_plan_revision",
        "bin_code",
        "bin_id",
        "cell_id",
        "slot_ids",
        "slot_id",
        "source_locator",
        "target",
        "transport",
        "command",
        "materials",
        "bins",
        "rack_id",
        "rack_face",
        "rack_faces",
        "position",
        "pick_params",
    }
)
_RESOURCE_FIELDS = frozenset(
    {
        "rack_id",
        "rack_face",
        "rack_faces",
        "slot_id",
        "slot_ids",
        "cell_id",
        "bin_code",
        "bin_id",
        "plan_revision",
        "source_locator",
        "type",
        "position",
        "pick_params",
        "target_rack",
        "target",
        "materials",
        "bins",
    }
)


class _Fields(CallbackPayload):
    known_fields: ClassVar[frozenset[str]] = _RESOURCE_FIELDS

    @model_validator(mode="before")
    @classmethod
    def check_defined_fields(cls, value):
        if isinstance(value, dict):
            if (value.keys() & cls.known_fields) - cls.model_fields.keys():
                raise ValueError("known field belongs to another task branch")
            if any(value[key] is None for key in value.keys() & cls.model_fields.keys()):
                raise ValueError("defined task fields cannot be null")
        return value

    @model_serializer(mode="wrap")
    def preserve_absence(self, serialize):
        return {
            key: value for key, value in serialize(self).items() if key in self.model_fields_set
        }


class _Task(_Fields):
    known_fields = _TASK_FIELDS
    task_id: Identifier


class Issued(_Task):
    task_type: Literal["MANUAL", "AUTO"]
    business_type: Literal["inbound", "outbound"]
    workline_code: Identifier
    queue_revision: Annotated[int, Field(strict=True, ge=1, le=1)]
    dispatch_sequence: Positive
    not_before: NonNegative | None = None


class QueueChanged(_Task):
    queue_revision: Annotated[int, Field(ge=2, le=2**63 - 1)]
    dispatch_sequence: Positive | None = None
    not_before: NonNegative | None = None

    @model_validator(mode="after")
    def require_update(self) -> Self:
        if not self.model_fields_set & {"dispatch_sequence", "not_before"}:
            raise ValueError("queue update requires at least one field")
        return self


class Rack(_Fields):
    rack_id: Identifier


class RackFace(Rack):
    rack_face: Face


class BinSourceRack(Rack):
    rack_face: Faces

    @model_validator(mode="after")
    def unique_faces(self) -> Self:
        if len(set(self.rack_face)) != len(self.rack_face):
            raise ValueError("duplicate rack face")
        return self


class ReturnRack(Rack):
    rack_faces: Faces

    @model_validator(mode="after")
    def unique_faces(self) -> Self:
        if len(set(self.rack_faces)) != len(self.rack_faces):
            raise ValueError("duplicate rack face")
        return self


class SourceLocator(RackFace):
    type: Literal["RACK_SLOT"]
    slot_id: Identifier


class DirectPick(_Fields):
    source_locator: SourceLocator


def _unique(values) -> None:
    if len(values) != len(set(values)):
        raise ValueError("duplicate task member or selector")


class PlanDelta(_Task):
    plan_revision: Positive
    target_rack: RackFace | None = None
    added_bin_source_racks: Annotated[list[BinSourceRack], Field(min_length=1)] | None = None
    added_direct_picks: Annotated[list[DirectPick], Field(min_length=1)] | None = None
    added_source_racks: Annotated[list[Rack], Field(min_length=1)] | None = None
    added_return_racks: Annotated[list[ReturnRack], Field(min_length=1)] | None = None

    @model_validator(mode="after")
    def unique_members(self) -> Self:
        for name, face_key in (
            ("added_bin_source_racks", "rack_face"),
            ("added_return_racks", "rack_faces"),
        ):
            _unique(
                [
                    (rack.rack_id, face)
                    for rack in getattr(self, name) or []
                    for face in getattr(rack, face_key)
                ]
            )
        _unique([rack.rack_id for rack in self.added_source_racks or []])
        _unique(
            [
                (
                    pick.source_locator.rack_id,
                    pick.source_locator.rack_face,
                    pick.source_locator.slot_id,
                )
                for pick in self.added_direct_picks or []
            ]
        )
        return self


class BinSelector(BinSourceRack):
    plan_revision: Positive


class SourceSelector(Rack):
    plan_revision: Positive


class DirectSelector(RackFace):
    plan_revision: Positive
    slot_ids: Annotated[list[Identifier], Field(min_length=1)] | None = None

    @model_validator(mode="after")
    def unique_slots(self) -> Self:
        _unique(self.slot_ids or [])
        return self


class Cancel(_Task):
    model_config = ConfigDict(strict=True, extra="forbid")
    cancel_scope: Literal["TASK", "PLAN_MEMBERS"]
    bin_source_racks: Annotated[list[BinSelector], Field(min_length=1)] | None = None
    source_racks: Annotated[list[SourceSelector], Field(min_length=1)] | None = None
    direct_pick_sources: Annotated[list[DirectSelector], Field(min_length=1)] | None = None

    @model_validator(mode="after")
    def precise_scope(self) -> Self:
        selectors = self.model_fields_set & {
            "bin_source_racks",
            "source_racks",
            "direct_pick_sources",
        }
        if (self.cancel_scope == "TASK" and selectors) or (
            self.cancel_scope == "PLAN_MEMBERS" and not selectors
        ):
            raise ValueError("cancel scope and selectors disagree")
        _unique(
            [
                (item.plan_revision, item.rack_id, face)
                for item in self.bin_source_racks or []
                for face in item.rack_face
            ]
        )
        _unique([(item.plan_revision, item.rack_id) for item in self.source_racks or []])
        _unique(
            [
                (item.plan_revision, item.rack_id, item.rack_face)
                for item in self.direct_pick_sources or []
            ]
        )
        return self


def validate_task_branch(
    payload: Issued | QueueChanged | PlanDelta | Cancel, *, task_type: str, business_type: str
) -> None:
    """Validate against the original task mode/direction without changing effective fields."""
    if task_type not in {"MANUAL", "AUTO"} or business_type not in {"inbound", "outbound"}:
        raise Rejected("INVALID_DATA")
    fields = payload.model_fields_set
    valid = True
    if isinstance(payload, Issued):
        valid = (payload.task_type, payload.business_type) == (task_type, business_type)
    elif isinstance(payload, PlanDelta):
        allowed = {"added_bin_source_racks"}
        if task_type == "AUTO":
            allowed |= (
                {"added_source_racks"}
                if business_type == "inbound"
                else {"target_rack", "added_return_racks"}
            )
        elif business_type == "outbound":
            allowed |= {"target_rack", "added_direct_picks"}
        resources = fields - {"task_id", "plan_revision"}
        valid = resources <= allowed and bool(resources)
        if "target_rack" in resources and payload.plan_revision != 1:
            valid = False
        if (
            task_type == "AUTO"
            and business_type == "outbound"
            and payload.plan_revision == 1
            and "target_rack" not in resources
        ):
            valid = False
    elif isinstance(payload, Cancel):
        allowed = {"bin_source_racks"}
        if task_type == "AUTO" and business_type == "inbound":
            allowed.add("source_racks")
        elif business_type == "outbound":
            allowed.add("direct_pick_sources")
        valid = fields - {"task_id", "cancel_scope"} <= allowed
        if task_type == "AUTO" and any(
            "slot_ids" in selector.model_fields_set
            for selector in payload.direct_pick_sources or []
        ):
            valid = False
    elif not isinstance(payload, QueueChanged):
        valid = False
    if not valid:
        raise Rejected("INVALID_DATA")


class PrepareAccepted(_Fields):
    known_fields = frozenset({"result", "current_plan_revision", "retry_after_ms"})

    @classmethod
    def parse(cls, status_code: int, code: str, data: dict) -> Self:
        if type(status_code) is not int or status_code != 202 or code != "PREPARE_ACCEPTED":
            raise ValueError("expected 202/PREPARE_ACCEPTED")
        return cls.model_validate(data)


class CompletionDecision(_Fields):
    known_fields = frozenset({"result", "current_plan_revision", "retry_after_ms"})
    result: Literal["COMPLETED", "PLAN_REVISION_STALE", "BUSINESS_IN_PROGRESS"]
    current_plan_revision: Positive | None = None
    retry_after_ms: Annotated[int, Field(ge=1, le=60000)] | None = None

    @model_validator(mode="after")
    def exclusive_branch(self) -> Self:
        expected = {"result"}
        if self.result == "PLAN_REVISION_STALE":
            expected.add("current_plan_revision")
        elif self.result == "BUSINESS_IN_PROGRESS":
            expected.add("retry_after_ms")
        if self.model_fields_set != expected:
            raise ValueError("invalid completion response branch")
        return self

    @classmethod
    def parse(cls, status_code: int, code: str, data: dict) -> Self:
        if type(status_code) is not int or status_code != 200 or code != "DECIDED":
            raise ValueError("expected 200/DECIDED")
        return cls.model_validate(data)
