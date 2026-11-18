from app.businesses.definitions import BusinessDefinition, DeviceRole, InitialEntry, PositionSlot

XRAY = "XRAY"
OUTPUT_ARM = "XRAY_OUTPUT_ARM"
IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$"

DEFINITION = BusinessDefinition(
    code="xray",
    display_name="X-Ray",
    line_code_pattern=IDENTIFIER_PATTERN,
    device_roles=(
        DeviceRole(role_key=XRAY, display_name="X-Ray 清点机"),
        DeviceRole(role_key=OUTPUT_ARM, display_name="X-Ray 出口机械臂"),
    ),
    position_slots=(
        PositionSlot(
            position_type="RACK_POSITION",
            slot_key="RETURN_RACK",
            display_name="五层退料架工作位",
            required=False,
        ),
        PositionSlot(
            position_type="HANDOFF_POSITION",
            slot_key="TRANSIT_BIN",
            display_name="单层通箱架工作位",
            required=False,
        ),
    ),
    initial_entries=(InitialEntry(kind="ecs_event", name="SCAN_COMPLETED", device_role=XRAY),),
)
