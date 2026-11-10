import pytest
from pydantic import ValidationError

from app.businesses.smt_auto.ecs_command import PickAndPut


@pytest.mark.parametrize(
    ("millimeters", "inches"),
    [
        (177.8, 7),
        (179, 7),
        (253.9, 7),
        (254, 13),
        (330.2, 13),
        (355.5, 13),
        (355.6, 15),
        (381, 15),
        (400, 15),
    ],
)
@pytest.mark.parametrize(
    ("location", "side"),
    [
        ("BIN", "source"),
        ("BIN", "target"),
        ("ONE_LAYER_RACK", "source"),
        ("FIVE_LAYER_RACK", "source"),
        ("FIVE_LAYER_RACK", "target"),
    ],
)
def test_arm_commands_use_nearest_supported_inches(millimeters, inches, side, location):
    scan = dict(location_id="STATION_SCAN", location_type="SCAN_PLATFORM")
    if location == "FIVE_LAYER_RACK":
        reel = dict(
            location_id="STATION_B" if side == "source" else "STATION_A",
            location_type=location,
            rack_id="R1",
            rack_face="90",
            rack_layer="2",
            rack_column="3",
            reel_thickness=10,
            reel_diameter=millimeters,
        )
    else:
        reel = dict(
            location_id="STATION_WORK1",
            location_type="BIN",
            bin_type="3",
            bin_cell_location=2,
            reel_layer=3,
            reel_thickness=10,
            reel_diameter=millimeters,
            reel_totalthickness=30,
        )
        if location == "ONE_LAYER_RACK":
            reel.update(
                location_id="STATION_A",
                location_type=location,
                rack_id="R1",
                bin_id="B1",
                cell_id="C1",
                bin_location="1",
            )
    params = dict(source=reel, target=scan) if side == "source" else dict(source=scan, target=reel)
    command = PickAndPut.from_millimeters(**params)
    wire = command.model_dump(mode="json")
    expected = {**reel, "reel_diameter": inches}
    if location == "FIVE_LAYER_RACK":
        del expected["rack_face"]
        expected["rack_side"] = "A"
    assert wire[side] == expected
    assert reel["reel_diameter"] == millimeters
    assert PickAndPut.model_validate(wire).model_dump(mode="json") == wire


@pytest.mark.parametrize("diameter", ["179", True, float("inf"), float("nan")])
def test_arm_conversion_preserves_strict_diameter_validation(diameter):
    with pytest.raises(ValidationError):
        PickAndPut.from_millimeters(
            source=dict(
                location_id="STATION_WORK1",
                location_type="BIN",
                bin_type="3",
                bin_cell_location=2,
                reel_layer=3,
                reel_thickness=10,
                reel_diameter=diameter,
                reel_totalthickness=30,
            ),
            target=dict(location_id="STATION_SCAN", location_type="SCAN_PLATFORM"),
        )
