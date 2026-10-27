import subprocess
import sys
from dataclasses import FrozenInstanceError

import pytest

from app.businesses.definitions import BusinessDefinition, DeviceRole, InitialEntry, PositionSlot


@pytest.mark.parametrize(
    "constructor, fields",
    [
        (DeviceRole, {"role_key": key, "display_name": "设备"})
        for key in ("scan", "1SCAN", "SCAN-1", "A" * 101, "SCAN\n", None)
    ]
    + [
        (PositionSlot, {"slot_key": key, "display_name": "位置"})
        for key in ("inlet", "1INLET", "INLET-1", "A" * 101, None)
    ]
    + [
        (DeviceRole, {"role_key": "SCAN", "display_name": name})
        for name in ("", "  ", "名" * 101, None)
    ]
    + [
        (PositionSlot, {"slot_key": "INLET", "display_name": "位置", "required": value})
        for value in (0, 1, "true", None)
    ]
    + [
        (DeviceRole, {"role_key": "SCAN", "display_name": "设备", "required": 1}),
        (BusinessDefinition, {"code": "Upper", "display_name": "业务"}),
        (BusinessDefinition, {"code": "has.dot", "display_name": "业务"}),
        (BusinessDefinition, {"code": "a" * 101, "display_name": "业务"}),
        (BusinessDefinition, {"code": "test", "display_name": " "}),
        (BusinessDefinition, {"code": "test", "display_name": "名" * 101}),
        (InitialEntry, {"kind": "unknown", "name": "SCAN"}),
        (InitialEntry, {"kind": "ecs_event", "name": "scan", "device_role": "SCAN"}),
        (InitialEntry, {"kind": "ecs_event", "name": "SCAN"}),
        (InitialEntry, {"kind": "ecs_event", "name": "SCAN", "device_role": "scan"}),
        (InitialEntry, {"kind": "wms_callback", "name": ""}),
        (InitialEntry, {"kind": "wms_callback", "name": "a" * 257}),
        (InitialEntry, {"kind": "wms_callback", "name": "a.b@v1", "device_role": "SCAN"}),
    ],
)
def test_definition_rejects_invalid_fields(constructor, fields):
    with pytest.raises(ValueError):
        constructor(**fields)


@pytest.mark.parametrize("field", ["device_roles", "position_slots", "initial_entries"])
@pytest.mark.parametrize("value", [[], ("wrong type",)])
def test_definition_rejects_mutable_or_wrong_type_collections(field, value):
    with pytest.raises(ValueError):
        BusinessDefinition(code="test", display_name="业务", **{field: value})


def test_definition_rejects_invalid_or_duplicate_keys():
    role = DeviceRole(role_key="SCAN", display_name="设备")
    position = PositionSlot(slot_key="INLET", display_name="位置")
    entry = InitialEntry(kind="ecs_event", name="SCAN_COMPLETED", device_role="SCAN")
    for fields in (
        {"device_roles": (role, role)},
        {"position_slots": (position, position)},
        {"device_roles": (role,), "initial_entries": (entry, entry)},
        {"initial_entries": (entry,)},
    ):
        with pytest.raises(ValueError):
            BusinessDefinition(code="test", display_name="业务", **fields)


def test_definition_keeps_distinct_role_event_keys_and_immutable_values():
    roles = tuple(
        DeviceRole(role_key=key, display_name="设备", required=False)
        for key in ("SCAN_A", "SCAN_B", "A" * 100)
    )
    entries = tuple(
        InitialEntry(kind="ecs_event", name="SCAN_COMPLETED", device_role=role.role_key)
        for role in roles
    )
    definition = BusinessDefinition(
        code="test_2",
        display_name="名" * 100,
        device_roles=roles,
        position_slots=(PositionSlot(slot_key="INLET", display_name="位置"),),
        initial_entries=entries,
    )
    assert len(definition.initial_entries) == 3
    assert definition.device_roles[0].required is False
    for value, field in (
        (definition, "code"),
        (roles[0], "role_key"),
        (definition.position_slots[0], "slot_key"),
        (entries[0], "name"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, "CHANGED")


def test_definition_import_has_no_runtime_side_effects():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib.abc
import sys

class BlockRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if (fullname.split('.')[0] in {'sqlalchemy', 'pydantic', 'httpx', 'socket'}
            or fullname.startswith(('app.core', 'app.integrations', 'app.models',
                                    'app.services', 'app.main'))
            or fullname.endswith(('.composition', '.contracts', '.command_contracts'))):
            raise AssertionError('runtime import: ' + fullname)

sys.meta_path.insert(0, BlockRuntime())
from app.businesses.definitions import BusinessDefinition, DeviceRole
value = BusinessDefinition(code='isolated', display_name='业务',
    device_roles=(DeviceRole(role_key='SCAN', display_name='设备'),))
assert value.code == 'isolated'
""",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
