"""Actual shared declarations composed by test-owned consumers only."""

from app.businesses import catalog
from app.integrations.ecs_callback import registry as events
from app.integrations.wms_request import operation as requests


def test_one_component_owns_all_four_scan_inputs(roller_declaration):
    component, _ = roller_declaration
    bindings, annexes = catalog.validate_business_contracts()
    assert len(bindings) == 6
    assert len(annexes) == 3
    for point in ("SCAN1", "SCAN2", "SCAN3", "SCAN4"):
        key = ("ecs_event", "SCAN_COMPLETED", point)
        assert catalog.contract_consumers(key) == {"first", "second"}
        for owner in ("first", "second"):
            target = catalog.input_target(owner, key)
            assert target.component_key == "bin_roller"
            assert target.source == events.lookup(point, "SCAN_COMPLETED").registration_source
    assert len(requests.operations()) == 4
    for operation in requests.operations():
        assert catalog.contract_consumers(("wms_request", operation, None)) == {"first", "second"}
    for definition in catalog.definitions():
        assert {role.role_key for role in definition.device_roles} == {
            "PRODUCTION",
            "SCAN1",
            "SCAN2",
            "SCAN3",
            "SCAN4",
        }
        assert {slot.slot_key for slot in definition.position_slots} == {
            "FIVE_RACK",
            "INLET",
            "OUTLET",
        }
    assert component.model_modules == ("app.businesses._components.bin_roller.models",)
    assert component.requires_progression
    assert set(component.required_methods) == {
        "accept_scan",
        "get_pending_upper_id",
        "list_pending",
        "advance",
        "apply_supply_change",
        "get_supply_progress",
        "release_scan2",
        "get_scan2_release_result",
    }
