"""The three work-plan requests preserve the documented wire contracts."""

import importlib

import pytest
from pydantic import ValidationError

from app.businesses import catalog
from app.businesses.definitions import BusinessDefinition
from tests.helpers import business_composition


@pytest.fixture(autouse=True)
def isolated_contracts(monkeypatch):
    entry = business_composition("smt_auto")
    entry.DEFINITION = BusinessDefinition(code="smt_auto", display_name="Isolated AUTO")
    entry.ENTRY_MODULES = ("app.businesses.smt_auto.request",)
    monkeypatch.setattr(catalog, "entries", lambda: (entry,))
    monkeypatch.setattr(catalog, "_validated_entries", None)


@pytest.mark.parametrize(
    ("module", "data"),
    [
        (
            "putaway_work_plan_v1",
            {"task_id": "t", "target_bin_code": "b", "scanned_at": 0, "source_racks": []},
        ),
        ("outbound_bin_work_plan_v1", {"task_id": "t", "bin_code": "b", "scanned_at": 0}),
        (
            "outbound_return_rack_work_plan_v1",
            {"task_id": "t", "rack_id": "r", "rack_face": "A", "arrived_at": 0},
        ),
    ],
)
def test_work_plan_request_preserves_original_fields(module, data):
    contract = importlib.import_module(f"app.businesses.smt_auto.request.{module}").WorkPlan
    assert contract.model_validate(data).model_dump() == data
    with pytest.raises(ValidationError):
        contract.model_validate({**data, "unknown": 1})
    time = "arrived_at" if "arrived_at" in data else "scanned_at"
    with pytest.raises(ValidationError):
        contract.model_validate({**data, time: True})


def test_inbound_source_scope_is_bounded_and_unique():
    contract = importlib.import_module(
        "app.businesses.smt_auto.request.putaway_work_plan_v1"
    ).WorkPlan
    data = {"task_id": "t", "target_bin_code": "b", "scanned_at": 0}
    for racks in (
        [{"rack_id": "r", "location_code": "L"}] * 2,
        [{"rack_id": "r1", "location_code": "L"}, {"rack_id": "r2", "location_code": "L"}],
        [{"rack_id": str(i), "location_code": str(i)} for i in range(3)],
    ):
        with pytest.raises(ValidationError):
            contract.model_validate({**data, "source_racks": racks})


def test_outbound_member_retains_original_first_round_and_terminal_facts():
    from app.businesses.smt_auto.models import AutoOutboundSourceMember

    assert {"first_work_plan_round_id", "terminal_basis", "terminal_at_ms"} <= set(
        AutoOutboundSourceMember.__table__.columns.keys()
    )


def test_ready_bin_type_requires_json_integer():
    from app.businesses.smt_auto.work_plan_contracts import DECISIONS

    with pytest.raises(ValidationError):
        DECISIONS["bin"].validate_python(
            {
                "result": "READY",
                "cell_id": "B1",
                "cell_index": 1,
                "bin_type": 2.0,
                "reel_layer": 1,
                "reel_thickness": 1,
                "reel_diameter": 1,
            }
        )
