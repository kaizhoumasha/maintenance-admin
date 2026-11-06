"""The coarse scan and first conveyor movement roles."""

from app.businesses.definitions import BusinessDefinition, DeviceRole, InitialEntry, PositionSlot

DEFINITION = BusinessDefinition(
    code="coarse_sorting",
    display_name="粗分",
    device_roles=(
        DeviceRole(role_key="INPUT_ARM", display_name="入料机械臂"),
        DeviceRole(role_key="CONVEYOR", display_name="输送线"),
        DeviceRole(role_key="OUTPUT_ARM", display_name="出料机械臂", required=False),
    ),
    position_slots=(
        PositionSlot(
            position_type="RACK_POSITION", slot_key="OUTPUT_RACK", display_name="出料单层货架位"
        ),
    ),
    initial_entries=(
        InitialEntry(kind="ecs_event", name="SCAN_COMPLETED", device_role="INPUT_ARM"),
    ),
)
