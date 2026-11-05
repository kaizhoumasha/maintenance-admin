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
