import pytest
from pydantic import ValidationError


def test_normal_target_keeps_empty_occupancy_and_rejects_coerced_numbers():
    from app.businesses.smt_auto.inbound_contracts import Target

    data = dict(
        bin_code="BIN",
        cell_id="CELL",
        bin_type=3,
        cell_index=1,
        reel_layer=0,
        reel_thickness=20,
        reel_diameter=15,
        reel_totalthickness=0,
    )
    assert Target(**data).model_dump() == data
    for key, value in (
        ("reel_layer", True),
        ("bin_type", 3.0),
        ("cell_index", "1"),
        ("reel_totalthickness", float("inf")),
    ):
        with pytest.raises(ValidationError):
            Target(**{**data, key: value})


def test_normal_commands_have_one_shared_strict_declaration():
    from app.businesses.smt_auto.ecs_command import PickAndPut
    from app.integrations.ecs_command import compile_commands

    bindings, annexes = compile_commands(PickAndPut)
    assert {b.device_role for b in bindings} == {"BIN_ARM", "RACK_ARM"}
    assert len(annexes) == 1
    with pytest.raises(ValidationError):
        PickAndPut(
            source={"location_id": "STATION_SCAN", "location_type": "SCAN_PLATFORM"},
            target={"location_id": "STATION_NG", "location_type": "NG_ZONE"},
        )


@pytest.mark.parametrize("bin_type", [None, True, 3.0, ""])
def test_arm_bin_type_rejects_invalid_codes(bin_type):
    from app.businesses.smt_auto.ecs_command import PickAndPut

    with pytest.raises(ValidationError):
        PickAndPut(
            source=dict(
                location_id="STATION_WORK1",
                location_type="BIN",
                bin_type=bin_type,
                bin_cell_location=1,
                reel_layer=1,
                reel_thickness=20,
                reel_diameter=7,
                reel_totalthickness=20,
            ),
            target=dict(location_id="STATION_SCAN", location_type="SCAN_PLATFORM"),
        )


@pytest.mark.parametrize("bin_type", [8, "8", "008", "WMS-CUSTOM"])
def test_arm_bin_type_uses_string_wire_code(bin_type):
    from app.businesses.smt_auto.ecs_command import PickAndPut

    command = PickAndPut(
        source=dict(
            location_id="STATION_WORK1",
            location_type="BIN",
            bin_type=bin_type,
            bin_cell_location=1,
            reel_layer=1,
            reel_thickness=20,
            reel_diameter=7,
            reel_totalthickness=20,
        ),
        target=dict(location_id="STATION_SCAN", location_type="SCAN_PLATFORM"),
    )
    assert command.model_dump()["source"]["bin_type"] == str(bin_type)
