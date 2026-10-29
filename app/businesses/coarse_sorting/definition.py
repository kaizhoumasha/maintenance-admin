"""The implemented coarse scan acceptance entry and its required device role."""

from app.businesses.definitions import BusinessDefinition, DeviceRole, InitialEntry

DEFINITION = BusinessDefinition(
    code="coarse_sorting",
    display_name="粗分扫码受理",
    device_roles=(DeviceRole(role_key="INPUT_ARM", display_name="入料机械臂"),),
    initial_entries=(
        InitialEntry(kind="ecs_event", name="SCAN_COMPLETED", device_role="INPUT_ARM"),
    ),
)
