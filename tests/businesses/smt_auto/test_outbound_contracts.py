import pytest
from pydantic import ValidationError

from app.businesses.smt_auto.ecs_command import PickAndPut
from app.businesses.smt_auto.outbound_contracts import BinAccept, RackReject


def test_outbound_paths_keep_source_and_target_dimensions():
    scan = dict(location_id="STATION_SCAN", location_type="SCAN_PLATFORM")
    rack = dict(
        location_id="STATION_B",
        location_type="FIVE_LAYER_RACK",
        rack_id="R1",
        rack_face="A",
        rack_layer="2",
        rack_column="3",
        reel_thickness=15.3,
        reel_diameter=7,
    )
    assert PickAndPut(source=rack, target=scan).source.reel_thickness == 15.3
    assert (
        PickAndPut(source=scan, target={**rack, "location_id": "STATION_A"}).target.rack_layer
        == "2"
    )
    with pytest.raises(ValidationError):
        PickAndPut(source=rack, target={**rack, "location_id": "STATION_A"})


def test_accept_and_reject_are_closed_branch_shapes():
    target = dict(
        type="RACK_SLOT", rack_id="T1", rack_face="A", slot_id="S1", rack_layer=1, rack_column=2
    )
    assert BinAccept(result="ACCEPT", target_locator=target).target_locator.slot_id == "S1"
    with pytest.raises(ValidationError):
        BinAccept(result="ACCEPT", target_locator=target, next_source_action=None)
    with pytest.raises(ValidationError):
        RackReject(
            result="REJECT",
            business_exception_code="MATERIAL_REJECTED",
            ng_locator=dict(type="NG_ZONE", zone_code="NG1"),
            source_disposition="CONTINUE",
        )
