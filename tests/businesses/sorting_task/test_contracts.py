from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.businesses._components.sorting_task.contracts import (
    Cancel,
    CompletionDecision,
    Issued,
    PlanDelta,
    PrepareAccepted,
    QueueChanged,
    validate_task_branch,
)
from app.integrations.wms_callback import Rejected

BIN = {"rack_id": "BIN-1", "rack_face": ["90", "270"]}
TARGET = {"rack_id": "TARGET-1", "rack_face": "A"}
DIRECT = {
    "source_locator": {"type": "RACK_SLOT", "rack_id": "D-1", "rack_face": "A", "slot_id": "01"}
}
RETURN = {"rack_id": "RETURN-1", "rack_faces": ["B", "A"]}


@pytest.mark.parametrize(
    "mode,direction,resources",
    [
        ("MANUAL", "inbound", {"added_bin_source_racks": [BIN]}),
        (
            "MANUAL",
            "outbound",
            {
                "target_rack": TARGET,
                "added_bin_source_racks": [BIN],
                "added_direct_picks": [DIRECT],
            },
        ),
        (
            "AUTO",
            "inbound",
            {"added_bin_source_racks": [BIN], "added_source_racks": [{"rack_id": "S-1"}]},
        ),
        (
            "AUTO",
            "outbound",
            {
                "target_rack": TARGET,
                "added_bin_source_racks": [BIN],
                "added_return_racks": [RETURN],
            },
        ),
    ],
)
def test_task_payloads_preserve_mode_fields(mode, direction, resources):
    data = {"task_id": "T-1", "plan_revision": 1, **deepcopy(resources)}
    payload = PlanDelta.model_validate(data)
    assert payload.model_dump(mode="json") == data
    validate_task_branch(payload, task_type=mode, business_type=direction)
    assert payload.model_dump(mode="json") == data


@pytest.mark.parametrize(
    "mode,direction,resources",
    [
        ("MANUAL", "inbound", {"target_rack": TARGET}),
        ("MANUAL", "inbound", {"added_direct_picks": [DIRECT]}),
        ("MANUAL", "outbound", {"added_source_racks": [{"rack_id": "S"}]}),
        ("MANUAL", "outbound", {"added_return_racks": [RETURN]}),
        ("AUTO", "inbound", {"target_rack": TARGET}),
        ("AUTO", "inbound", {"added_return_racks": [RETURN]}),
        ("AUTO", "outbound", {"target_rack": TARGET, "added_source_racks": [{"rack_id": "S"}]}),
        ("AUTO", "outbound", {"target_rack": TARGET, "added_direct_picks": [DIRECT]}),
    ],
)
def test_plan_and_cancel_reject_cross_mode_fields(mode, direction, resources):
    payload = PlanDelta.model_validate({"task_id": "T", "plan_revision": 1, **resources})
    with pytest.raises(Rejected, match="INVALID_DATA"):
        validate_task_branch(payload, task_type=mode, business_type=direction)


@pytest.mark.parametrize(
    "mode,direction,revision,resources,valid",
    [
        ("AUTO", "outbound", 1, {}, False),
        ("AUTO", "outbound", 1, {"target_rack": TARGET}, True),
        ("MANUAL", "outbound", 1, {"added_direct_picks": [DIRECT]}, True),
        ("MANUAL", "outbound", 1, {}, False),
        ("MANUAL", "inbound", 1, {}, False),
        ("AUTO", "inbound", 2, {"added_source_racks": [{"rack_id": "S"}]}, True),
        ("AUTO", "outbound", 2, {"added_return_racks": [RETURN]}, True),
        ("AUTO", "outbound", 2, {"target_rack": TARGET, "added_return_racks": [RETURN]}, False),
        ("MANUAL", "outbound", 2, {"target_rack": TARGET, "added_bin_source_racks": [BIN]}, False),
        ("AUTO", "outbound", 2, {}, False),
    ],
)
def test_plan_shape_uses_original_direction(mode, direction, revision, resources, valid):
    payload = PlanDelta.model_validate({"task_id": "T", "plan_revision": revision, **resources})
    if valid:
        validate_task_branch(payload, task_type=mode, business_type=direction)
    else:
        with pytest.raises(Rejected, match="INVALID_DATA"):
            validate_task_branch(payload, task_type=mode, business_type=direction)


@pytest.mark.parametrize(
    "model,data",
    [
        (
            Issued,
            {
                "task_id": "T",
                "task_type": "AUTO",
                "business_type": "outbound",
                "workline_code": "LINE",
                "queue_revision": 1,
                "dispatch_sequence": 1,
            },
        ),
        (QueueChanged, {"task_id": "T", "queue_revision": 2, "not_before": 0}),
        (PlanDelta, {"task_id": "T", "plan_revision": 1, "added_bin_source_racks": [BIN]}),
        (
            Cancel,
            {
                "task_id": "T",
                "cancel_scope": "PLAN_MEMBERS",
                "direct_pick_sources": [
                    {"plan_revision": 1, "rack_id": "D", "rack_face": "A", "slot_ids": ["2", "1"]}
                ],
            },
        ),
    ],
)
def test_task_normalization_preserves_absence_and_known_fields(model, data):
    assert model.model_validate(data).model_dump(mode="json") == data
    if model is not Cancel:
        assert (
            model.model_validate(data | {"unknown_note": "ignored"}).model_dump(mode="json") == data
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("dispatch_sequence", True),
        ("dispatch_sequence", "1"),
        ("dispatch_sequence", 0),
        ("dispatch_sequence", 2**63),
        ("not_before", None),
        ("not_before", -1),
        ("task_type", "auto"),
        ("business_type", "INBOUND"),
        ("task_id", "bad space"),
        ("task_id", "A" * 101),
        ("queue_revision", 2),
        ("target_rack", TARGET),
        ("added_bin_source_racks", [BIN]),
        ("bin_code", "B"),
    ],
)
def test_issued_rejects_invalid_types_and_execution_fields(field, value):
    data = {
        "task_id": "T",
        "task_type": "AUTO",
        "business_type": "inbound",
        "workline_code": "L",
        "queue_revision": 1,
        "dispatch_sequence": 1,
    }
    with pytest.raises(ValidationError):
        Issued.model_validate(data | {field: value})


@pytest.mark.parametrize(
    "data",
    [
        {"queue_revision": 1, "not_before": 0},
        {"queue_revision": 2},
        {"queue_revision": 2, "not_before": None},
        {"queue_revision": 2, "dispatch_sequence": False},
    ],
)
def test_queue_requires_valid_update(data):
    with pytest.raises(ValidationError):
        QueueChanged.model_validate({"task_id": "T", **data})


@pytest.mark.parametrize(
    "resources",
    [
        {"added_bin_source_racks": []},
        {"added_bin_source_racks": None},
        {"added_bin_source_racks": [{"rack_id": "B", "rack_face": []}]},
        {"added_bin_source_racks": [{"rack_id": "B", "rack_face": ["A", "A"]}]},
        {"added_bin_source_racks": [{"rack_id": "B", "rack_face": "A"}]},
        {"target_rack": {"rack_id": "B", "rack_face": ["A"]}},
        {"target_rack": {"rack_id": "B", "rack_face": "A" * 11}},
        {"added_return_racks": [{"rack_id": "B", "rack_faces": ["A", "A"]}]},
        {"added_source_racks": [{"rack_id": "S", "rack_face": "A"}]},
        {
            "added_direct_picks": [
                {
                    "source_locator": {
                        "type": "RACK_BIN_SLOT",
                        "rack_id": "R",
                        "rack_face": "A",
                        "slot_id": "1",
                    }
                }
            ]
        },
    ],
)
def test_plan_checks_resource_shapes(resources):
    with pytest.raises(ValidationError):
        PlanDelta.model_validate({"task_id": "T", "plan_revision": 1, **resources})


@pytest.mark.parametrize(
    "data",
    [
        {"cancel_scope": "TASK", "source_racks": [{"plan_revision": 1, "rack_id": "S"}]},
        {"cancel_scope": "PLAN_MEMBERS"},
        {"cancel_scope": "TASK", "note": "no"},
        {"cancel_scope": "PLAN_MEMBERS", "source_racks": []},
        {
            "cancel_scope": "PLAN_MEMBERS",
            "source_racks": [{"plan_revision": 1, "rack_id": "S", "rack_face": "A"}],
        },
        {
            "cancel_scope": "PLAN_MEMBERS",
            "source_racks": [{"plan_revision": 1, "rack_id": "S", "cell_id": "1"}],
        },
        {
            "cancel_scope": "PLAN_MEMBERS",
            "bin_source_racks": [{"plan_revision": 1, "rack_id": "B", "rack_face": ["A", "A"]}],
        },
        {
            "cancel_scope": "PLAN_MEMBERS",
            "direct_pick_sources": [
                {"plan_revision": 1, "rack_id": "D", "rack_face": "A", "slot_ids": ["1", "1"]}
            ],
        },
    ],
)
def test_cancel_is_precise_and_strict(data):
    with pytest.raises(ValidationError):
        Cancel.model_validate({"task_id": "T", **data})


@pytest.mark.parametrize(
    "mode,direction,selector,valid",
    [
        ("MANUAL", "inbound", "direct_pick_sources", False),
        ("MANUAL", "outbound", "direct_pick_sources", True),
        ("AUTO", "outbound", "direct_pick_sources", False),
        ("MANUAL", "inbound", "source_racks", False),
        ("AUTO", "inbound", "source_racks", True),
        ("AUTO", "outbound", "source_racks", False),
    ],
)
def test_cancel_branch_keeps_slots_without_widening(mode, direction, selector, valid):
    item = {"plan_revision": 1, "rack_id": "R"}
    if selector == "direct_pick_sources":
        item |= {"rack_face": "A", "slot_ids": ["01"]}
    payload = Cancel.model_validate(
        {"task_id": "T", "cancel_scope": "PLAN_MEMBERS", selector: [item]}
    )
    if valid:
        validate_task_branch(payload, task_type=mode, business_type=direction)
    else:
        with pytest.raises(Rejected, match="INVALID_DATA"):
            validate_task_branch(payload, task_type=mode, business_type=direction)


@pytest.mark.parametrize(
    "model,data",
    [
        ("Prepare", {"task_id": "T", "workline_code": "L"}),
        ("CompletionConfirm", {"task_id": "T", "last_applied_plan_revision": 0}),
    ],
)
def test_outbound_requests_are_exact(model, data, request_models):
    model = request_models[model]
    assert model.model_validate(data).model_dump() == data
    for extra in [{"task_type": "AUTO"}, {"task_id": "bad space"}]:
        with pytest.raises(ValidationError):
            model.model_validate(data | extra)


@pytest.mark.parametrize(
    "model,status,code,data",
    [
        (PrepareAccepted, 202, "PREPARE_ACCEPTED", {}),
        (CompletionDecision, 200, "DECIDED", {"result": "COMPLETED"}),
        (
            CompletionDecision,
            200,
            "DECIDED",
            {"result": "PLAN_REVISION_STALE", "current_plan_revision": 3},
        ),
        (
            CompletionDecision,
            200,
            "DECIDED",
            {"result": "BUSINESS_IN_PROGRESS", "retry_after_ms": 1000},
        ),
    ],
)
def test_task_response_contracts_ignore_only_unknown_fields(model, status, code, data):
    assert model.parse(status, code, data | {"unknown_note": "ok"}).model_dump(mode="json") == data
    with pytest.raises(ValueError):
        model.parse(200 if status == 202 else 202, code, data)


@pytest.mark.parametrize(
    "data",
    [
        {"result": "COMPLETED", "current_plan_revision": 1},
        {"result": "COMPLETED", "retry_after_ms": None},
        {"result": "PLAN_REVISION_STALE"},
        {"result": "PLAN_REVISION_STALE", "current_plan_revision": True},
        {"result": "PLAN_REVISION_STALE", "current_plan_revision": 2, "retry_after_ms": 1},
        {"result": "BUSINESS_IN_PROGRESS", "retry_after_ms": 0},
        {"result": "BUSINESS_IN_PROGRESS", "retry_after_ms": 60001},
        {"result": "BUSINESS_IN_PROGRESS", "retry_after_ms": 100, "current_plan_revision": 1},
    ],
)
def test_completion_response_rejects_known_conflicts(data):
    with pytest.raises(ValueError):
        CompletionDecision.parse(200, "DECIDED", data)
