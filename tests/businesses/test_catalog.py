import subprocess
import sys
from dataclasses import replace

import pytest

from app.businesses import catalog
from app.businesses.definitions import InitialEntry
from tests.helpers import business_composition


def test_listing_does_not_call_factories(monkeypatch):
    first = business_composition("test_sorting", "FIRST_SCAN")
    second = business_composition("test_picking", "SECOND_SCAN")
    monkeypatch.setattr(catalog, "entries", lambda: (first, second))

    assert catalog.definitions() == (first.DEFINITION, second.DEFINITION)
    assert catalog.get_definition("test_sorting") is first.DEFINITION
    with pytest.raises(LookupError):
        catalog.get_definition("not_deployed")


def test_catalog_rejects_duplicate_business_codes(monkeypatch):
    monkeypatch.setattr(
        catalog,
        "entries",
        lambda: (business_composition("same", "SCAN_A"), business_composition("same", "SCAN_B")),
    )
    with pytest.raises(ValueError, match="same"):
        catalog.definitions()


@pytest.mark.parametrize("operation", ["not_an_operation", "UPPER.name@v1", "a.b@v0"])
def test_catalog_validates_wms_operations_at_assembly(monkeypatch, operation):
    entry = business_composition()
    entry.DEFINITION = replace(
        entry.DEFINITION,
        initial_entries=(InitialEntry(kind="wms_callback", name=operation),),
    )
    monkeypatch.setattr(catalog, "entries", lambda: (entry,))
    with pytest.raises(ValueError, match=entry.DEFINITION.code):
        catalog.definitions()


def test_catalog_accepts_wms_operation_contract(monkeypatch):
    entry = business_composition()
    entry.DEFINITION = replace(
        entry.DEFINITION,
        initial_entries=(InitialEntry(kind="wms_callback", name="outbound.task.issued@v1"),),
    )
    monkeypatch.setattr(catalog, "entries", lambda: (entry,))
    assert (
        catalog.get_definition("test_sorting").initial_entries[0].name == "outbound.task.issued@v1"
    )


def test_listing_imports_no_orm_or_business_runtime():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib.abc
import sys
from types import ModuleType

class BlockRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if (fullname.split('.')[0] in {'sqlalchemy', 'pydantic', 'httpx', 'socket'}
            or fullname.startswith(('app.core', 'app.integrations', 'app.models',
                                    'app.services', 'app.main'))):
            raise AssertionError('runtime import: ' + fullname)

sys.meta_path.insert(0, BlockRuntime())
from app.businesses import catalog
from app.businesses.definitions import BusinessDefinition, InitialEntry
entry = ModuleType('test_composition')
entry.DEFINITION = BusinessDefinition(code='isolated', display_name='业务',
    initial_entries=(InitialEntry(kind='wms_callback', name='test.task.issued@v1'),))
catalog.entries = lambda: (entry,)
assert catalog.definitions() == (entry.DEFINITION,)
assert catalog.get_definition('isolated') is entry.DEFINITION
""",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("load_models", [False, True])
def test_real_composition_contract_imports_stay_metadata_only(tmp_path, load_models):
    package = tmp_path / "metadata_probe"
    package.mkdir()
    (package / "__init__.py").touch()
    (package / "definition.py").write_text(
        "from app.businesses.definitions import BusinessDefinition, DeviceRole\n"
        "DEFINITION = BusinessDefinition(code='metadata_probe', display_name='Probe', "
        "device_roles=(DeviceRole(role_key='SCAN', display_name='Scan'),))\n"
    )
    (package / "command_contracts.py").write_text(
        "from pydantic import BaseModel, ConfigDict\n"
        "from app.integrations.ecs_command import CommandBinding, DeviceAnnex\n"
        "from app.integrations.ecs_command.contracts import ErrorDetail\n"
        "class Payload(BaseModel):\n"
        "    model_config = ConfigDict(strict=True, extra='forbid')\n"
        "COMMAND_BINDINGS = (CommandBinding('SCAN', 'MOVE', 'probe', '1', Payload),)\n"
        "ANNEXES = {('probe', '1'): DeviceAnnex(Payload, Payload, ErrorDetail, 0, 100)}\n"
    )
    (package / "composition.py").write_text(
        "from .definition import DEFINITION\n"
        "from .command_contracts import COMMAND_BINDINGS, ANNEXES\n"
        "ENTRY_MODULES = ()\n"
        "MODEL_MODULES = ('app.businesses.metadata_probe.models',)\n"
        "def build_service(capabilities):\n"
        "    raise AssertionError('metadata must not construct service')\n"
    )
    (package / "models.py").write_text(
        "from sqlalchemy.orm import Mapped, mapped_column\n"
        "from app.models import Base\n"
        "class ManagedHistory(Base):\n"
        "    __tablename__ = 'metadata_probe_history'\n"
        "    id: Mapped[int] = mapped_column(primary_key=True)\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib.abc
import sys
loading_models = False
class BlockRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if ((not loading_models and fullname.split('.')[0] == 'sqlalchemy')
            or (not loading_models and fullname.startswith(('app.models', 'app.database')))
            or fullname.startswith(('app.core', 'app.services', 'app.main',
            'app.integrations.ecs_command.runtime', 'app.integrations.ecs_command.service',
            'app.integrations.ecs_command.roles'))):
            raise AssertionError('runtime import: ' + fullname)
sys.meta_path.insert(0, BlockRuntime())
from app import businesses
businesses.__path__.insert(0, sys.argv[1])
from app.businesses import catalog
def entries():
    from app.businesses.metadata_probe import composition
    return (composition,)
catalog.entries = entries
assert 'app.businesses.metadata_probe.composition' not in sys.modules
if sys.argv[2] == 'True':
    loading_models = True
    from sqlalchemy.ext import asyncio
    def no_engine(*args, **kwargs):
        raise AssertionError('metadata must not create engine')
    asyncio.create_async_engine = no_engine
    catalog.load_business_models()
    from app.businesses.metadata_probe import models
    assert models.ManagedHistory.__tablename__ == 'metadata_probe_history'
else:
    assert catalog.get_definition('metadata_probe').code == 'metadata_probe'
    assert 'app.businesses.metadata_probe.models' not in sys.modules
composition, = entries()
assert catalog.definitions() == (composition.DEFINITION,)
assert catalog.get_definition('metadata_probe') is composition.DEFINITION
assert len(composition.COMMAND_BINDINGS) == len(composition.ANNEXES) == 1
""",
            str(tmp_path),
            str(load_models),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_command_binding_public_and_routing_imports_share_identity():
    from app.integrations.ecs_command import CommandBinding
    from app.integrations.ecs_command.routing import CommandBinding as RoutingBinding

    assert CommandBinding is RoutingBinding
