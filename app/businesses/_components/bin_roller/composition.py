"""Shared declarations; the consuming business owns assembly and progression."""

from app.businesses.components import ComponentDefinition
from app.businesses.definitions import DeviceRole, InitialEntry, PositionSlot

from .ecs_command import MoveForward, MoveLeft, MoveRight

COMPONENT = ComponentDefinition(
    key="bin_roller",
    device_roles=tuple(
        DeviceRole(role_key=point, display_name=point)
        for point in ("SCAN1", "SCAN2", "SCAN3", "SCAN4")
    ),
    position_slots=(
        PositionSlot(slot_key="FIVE_RACK", display_name="料架工作位"),
        PositionSlot(slot_key="INLET", display_name="投箱交接位"),
        PositionSlot(slot_key="OUTLET", display_name="回流交接位"),
    ),
    initial_entries=tuple(
        InitialEntry(kind="ecs_event", name="SCAN_COMPLETED", device_role=point)
        for point in ("SCAN1", "SCAN2", "SCAN3", "SCAN4")
    ),
    entry_modules=(
        "app.businesses._components.bin_roller.ecs_event.scan_completed",
        "app.businesses._components.bin_roller.request.outbound_bin_inbound_batch_v1",
        "app.businesses._components.bin_roller.request.outbound_bin_return_batch_v1",
        "app.businesses._components.bin_roller.request.workline_return_buffer_drain_rack_decide_v1",
        "app.businesses._components.bin_roller.request.rack_departure_decide_v1",
    ),
    model_modules=("app.businesses._components.bin_roller.models",),
    commands=(MoveForward, MoveLeft, MoveRight),
    required_methods=(
        "accept_scan",
        "get_pending_upper_id",
        "list_pending",
        "advance",
        "apply_supply_change",
        "get_supply_progress",
        "release_scan2",
        "get_scan2_release_result",
    ),
    requires_progression=True,
)
