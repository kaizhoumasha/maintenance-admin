import pytest
from pydantic import ValidationError

from app.businesses.smt_auto.ecs_command import PickAndPut


def test_arm_commands_keep_reel_diameter_wire_field_and_use_wms_size_value():
    command = PickAndPut(
        source={
            "location_id": "STATION_WORK1",
            "location_type": "BIN",
            "bin_type": "3",
            "bin_cell_location": 2,
            "reel_layer": 3,
            "reel_thickness": 10,
            "reel_diameter": 13,
            "reel_totalthickness": 30,
        },
        target={"location_id": "STATION_SCAN", "location_type": "SCAN_PLATFORM"},
    )

    wire = command.model_dump(mode="json")

    assert wire["source"]["reel_diameter"] == 13
    assert "size_inch" not in wire["source"]


def test_arm_commands_reject_new_wms_field_on_ecs_wire():
    with pytest.raises(ValidationError):
        PickAndPut(
            source={
                "location_id": "STATION_WORK1",
                "location_type": "BIN",
                "bin_type": "3",
                "bin_cell_location": 2,
                "reel_layer": 3,
                "reel_thickness": 10,
                "size_inch": 13,
                "reel_totalthickness": 30,
            },
            target={"location_id": "STATION_SCAN", "location_type": "SCAN_PLATFORM"},
        )
