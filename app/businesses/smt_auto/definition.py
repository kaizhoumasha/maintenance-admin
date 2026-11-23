"""Immutable AUTO role and direction position declarations."""

from app.businesses.definitions import BusinessDefinition, DeviceRole, InitialEntry, PositionSlot

DEFINITION = BusinessDefinition(
    code="smt_auto",
    display_name="SMT 自动上下架",
    device_roles=(
        DeviceRole(role_key="BIN_ARM", display_name="料箱侧机械臂"),
        DeviceRole(role_key="RACK_ARM", display_name="货架侧机械臂"),
        DeviceRole(role_key="PLATFORM_SCANNER", display_name="扫码平台", required=False),
    ),
    position_slots=tuple(
        PositionSlot(position_type="RACK_POSITION", slot_key=key, display_name=name, required=False)
        for key, name in (
            ("INBOUND_LEFT", "入库左侧来源架"),
            ("INBOUND_RIGHT", "入库右侧来源架"),
            ("RETURN_RACK", "出库退料架"),
            ("TRANSFER_RACK", "出库转运架"),
        )
    ),
    initial_entries=(
        InitialEntry(kind="wms_callback", name="putaway.source.rack_exhausted@v1"),
        InitialEntry(kind="ecs_event", name="SCAN_COMPLETED", device_role="PLATFORM_SCANNER"),
    ),
)
