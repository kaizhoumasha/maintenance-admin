"""Cold processes keep discovery assertions independent of app.main's imported catalog."""

import os
import subprocess
import sys
import textwrap

import pytest

from tests.helpers import business_composition


def run_cold(tmp_path, source):
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source), str(tmp_path)],
        capture_output=True,
        text=True,
        env={**os.environ, "ENVIRONMENT": "local"},
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def write_module(tmp_path, root, module, source=""):
    folder = tmp_path / root
    folder.mkdir(exist_ok=True)
    parts = module.split(".")
    for part in parts[:-1]:
        folder /= part
        folder.mkdir(exist_ok=True)
        (folder / "__init__.py").touch()
    (folder / (parts[-1] + ".py")).write_text(source)


SETUP = """
import builtins
import sys
from pathlib import Path
from types import ModuleType
from app import businesses, integrations
from app.businesses import catalog
root = Path(sys.argv[1])
businesses.__path__ = [str(root / 'businesses'), *businesses.__path__]
integrations.__path__ = [str(root / 'integrations'), *integrations.__path__]
entry = ModuleType('app.businesses.probe.composition')
entry.ENTRY_MODULES = tuple('app.businesses.probe.' + kind
                            for kind in ('ecs_event', 'callback', 'request'))
catalog.entries = lambda: (entry,)
builtins.loaded_entries = []
"""


REQUEST = """
from app.integrations.wms_request.operation import Endpoint, RequestPayload, request_operation
@request_operation('probe.done@v1', Endpoint.FACTS)
class Payload(RequestPayload): pass
"""


@pytest.mark.parametrize("query", ["operations", "lookup"])
def test_cold_request_discovery_inside_local_scope_stays_formal(tmp_path, query):
    root = "businesses"
    write_module(tmp_path, root, "probe.request", REQUEST)
    write_module(tmp_path, "integrations", "probe.helper")
    run_cold(
        tmp_path,
        SETUP
        + f"""
from app.integrations.registration import local_registration
from app.integrations.wms_request import operation
assert 'app.{root}.probe.request' not in sys.modules
with local_registration():
    result = operation.{query}({'"probe.done@v1"' if query == "lookup" else ""})
    item = result if {query!r} == 'lookup' else result['probe.done@v1']
    assert item.registration_source.local_only is False
    @operation.request_operation('probe.dev@v1', operation.Endpoint.FACTS)
    class Dev(operation.RequestPayload): pass
    assert operation._registry['probe.dev@v1'].registration_source.local_only is True
assert not any(name in sys.modules for name in (
    'app.main', 'app.businesses.probe.callback', 'app.businesses.probe.ecs_event'))
""",
    )


def test_standalone_request_lookup_loads_business_contract(tmp_path):
    write_module(tmp_path, "businesses", "probe.request", REQUEST)
    write_module(tmp_path, "integrations", "probe.helper")
    run_cold(
        tmp_path,
        SETUP
        + """
from app.integrations.wms_request.operation import lookup
assert lookup('probe.done@v1').registration_source.module == 'app.businesses.probe.request'
assert 'app.main' not in sys.modules
""",
    )


@pytest.mark.parametrize("environment", ["local", "production"])
@pytest.mark.parametrize("package", [False, True])
def test_all_formal_kinds_load_before_any_local_entry(tmp_path, environment, package):
    for root in ("businesses",):
        for kind in ("ecs_event", "callback", "request"):
            name = f"{root}.done@v1"
            if kind == "request":
                declaration = REQUEST.replace("probe.done@v1", name)
            elif kind == "callback":
                declaration = (
                    "from app.integrations.wms_callback import "
                    "CallbackPayload, callback_operation\n"
                    f"@callback_operation({name!r}, data=CallbackPayload)\n"
                    "async def handler(ctx, payload): pass\n"
                )
            else:
                declaration = (
                    "from app.integrations.ecs_callback import CallbackPayload, ecs_event\n"
                    f"@ecs_event('DONE', device_role={root.upper()!r}, data=CallbackPayload)\n"
                    "async def handler(ctx, payload): pass\n"
                )
            write_module(
                tmp_path,
                root,
                f"probe.{kind}" + (".__init__" if package else ""),
                declaration + f"import builtins\nbuiltins.loaded_entries.append('{root}.{kind}')\n",
            )
        for kind in ("dev_ecs_event", "dev_callback", "dev"):
            write_module(
                tmp_path,
                root,
                f"probe.{kind}" + (".__init__" if package else ""),
                "import builtins\n"
                "assert len([x for x in builtins.loaded_entries if '.dev' not in x]) == 3\n"
                f"builtins.loaded_entries.append('{root}.{kind}')\n",
            )
    run_cold(
        tmp_path,
        SETUP
        + f"""
entry.ENTRY_MODULES += tuple('app.businesses.probe.' + kind
                            for kind in ('dev_ecs_event', 'dev_callback', 'dev'))
from app.integrations.discovery import load_formal_modules, load_local_modules
load_formal_modules()
load_local_modules(environment={environment!r})
load_formal_modules()
load_local_modules(environment={environment!r})
assert len(builtins.loaded_entries) == {6 if environment == "local" else 3}
from app.integrations.ecs_callback.registry import events
from app.integrations.wms_callback.registry import operations as callbacks
from app.integrations.wms_request.operation import operations as requests
for items in (events(), callbacks(), requests()):
    sources = [item.registration_source for item in items.values()
               if '.probe.' in item.registration_source.module]
    assert len(sources) == 1
    assert all(source.local_only is False for source in sources)
""",
    )


def test_required_business_entry_missing_fails(monkeypatch):
    from app.businesses import catalog
    from app.integrations import discovery

    monkeypatch.setattr(catalog, "entries", lambda: (business_composition("missing"),))
    monkeypatch.setattr(discovery, "_loaded_formal", set())
    with pytest.raises(ModuleNotFoundError, match="app.businesses.missing"):
        discovery.load_formal_modules()


def test_internal_import_error_is_not_skipped(tmp_path):
    write_module(tmp_path, "businesses", "probe.request", "import absent_inner_dependency\n")
    write_module(tmp_path, "integrations", "probe.helper")
    run_cold(
        tmp_path,
        SETUP
        + """
from app.integrations.discovery import load_formal_modules
try:
    load_formal_modules(kind='request')
except ModuleNotFoundError as error:
    assert error.name == 'absent_inner_dependency'
else:
    raise AssertionError('internal import failure was skipped')
""",
    )


@pytest.mark.parametrize("fails", [False, True])
def test_formal_scope_restores_on_success_and_error(tmp_path, fails):
    write_module(
        tmp_path,
        "businesses",
        "probe.request",
        REQUEST + ("raise LookupError('formal import failed')\n" if fails else ""),
    )
    write_module(tmp_path, "integrations", "probe.helper")
    run_cold(
        tmp_path,
        SETUP
        + f"""
from app.integrations.registration import local_registration
from app.integrations import discovery
from app.integrations.wms_request import operation
with local_registration():
    try:
        discovery.load_formal_modules(kind='request')
    except LookupError as error:
        assert {fails!r} and str(error) == 'formal import failed'
        assert 'request' not in discovery._loaded_formal
    else:
        assert not {fails!r}
    @operation.request_operation('probe.dev@v1', operation.Endpoint.FACTS)
    class Dev(operation.RequestPayload): pass
    assert operation._registry['probe.dev@v1'].registration_source.local_only is True
assert operation._registry['probe.done@v1'].registration_source.local_only is False
""",
    )


def test_dev_plain_import_of_unloaded_formal_entry_still_fails(tmp_path):
    write_module(tmp_path, "businesses", "probe.request", REQUEST)
    write_module(
        tmp_path,
        "businesses",
        "probe.dev",
        "import app.businesses.probe.request\n",
    )
    write_module(tmp_path, "integrations", "probe.helper")
    run_cold(
        tmp_path,
        SETUP
        + """
from app.integrations.registration import import_registration_module
try:
    import_registration_module('app.businesses.probe.dev', local_only=True)
except RuntimeError as error:
    assert 'local_registration' in str(error)
else:
    raise AssertionError('dev plain import bypassed formal registration guard')
""",
    )


@pytest.mark.parametrize("query", ["operations()", "lookup('probe.done@v1')"])
def test_reentrant_discovery_does_not_return_partial_catalog(tmp_path, query):
    write_module(
        tmp_path,
        "businesses",
        "probe.request",
        REQUEST
        + f"from app.integrations.wms_request.operation import operations, lookup\n{query}\n",
    )
    write_module(tmp_path, "integrations", "probe.helper")
    run_cold(
        tmp_path,
        SETUP
        + """
from app.integrations import discovery
from app.integrations.wms_request.operation import operations
try:
    operations()
except RuntimeError as error:
    assert 'reentrant' in str(error)
    assert not discovery._loaded_formal
else:
    raise AssertionError('partial catalog escaped discovery')
""",
    )


@pytest.mark.parametrize("environment", ["local", "production"])
@pytest.mark.parametrize("application", ["app.main", "app.tasks.celery_app"])
def test_cold_application_loads_formal_before_local(tmp_path, environment, application):
    for kind in ("ecs_event", "callback", "request"):
        write_module(
            tmp_path,
            "businesses",
            f"probe.{kind}",
            f"import builtins\nbuiltins.loaded_entries.append('businesses.{kind}')\n",
        )
    write_module(
        tmp_path,
        "businesses",
        "probe.dev_callback",
        "import builtins\nassert len(builtins.loaded_entries) == 3\n"
        "from app.businesses import catalog\nassert len(catalog.validated_entries()) == 1\n"
        "builtins.loaded_entries.append('local')\n",
    )
    run_cold(
        tmp_path,
        SETUP
        + f"""
import os
os.environ['ENVIRONMENT'] = {environment!r}
from app.businesses.definitions import BusinessDefinition
entry.DEFINITION = BusinessDefinition(code='probe', display_name='Probe')
entry.COMMANDS = ()
entry.ENTRY_MODULES += ('app.businesses.probe.dev_callback',)
import importlib
importlib.import_module({application!r})
assert catalog.validated_entries() == (entry,)
assert len(builtins.loaded_entries) == {4 if environment == "local" else 3}
assert ('app.businesses.probe.dev_callback' in sys.modules) == {environment == "local"!r}
""",
    )


@pytest.mark.parametrize("environment", ["local", "production"])
@pytest.mark.parametrize("problem", ["missing_module", "missing_initial_entry"])
def test_invalid_formal_business_prevents_worker_initialization(tmp_path, environment, problem):
    for kind in ("ecs_event", "callback", "request"):
        if problem == "missing_module" and kind == "request":
            continue
        write_module(tmp_path, "businesses", f"probe.{kind}")
    write_module(
        tmp_path,
        "businesses",
        "probe.dev_callback",
        "raise AssertionError('local entry loaded before formal validation')\n",
    )
    run_cold(
        tmp_path,
        SETUP
        + f"""
import os
os.environ['ENVIRONMENT'] = {environment!r}
import celery
from app.businesses.definitions import BusinessDefinition, DeviceRole, InitialEntry
entry.DEFINITION = BusinessDefinition(
    code='probe', display_name='Probe',
    device_roles=(DeviceRole(role_key='PROBE_SCAN', display_name='Scan'),),
    initial_entries=(InitialEntry(kind='ecs_event', name='DONE', device_role='PROBE_SCAN'),),
)
entry.COMMANDS = ()
entry.ENTRY_MODULES += ('app.businesses.probe.dev_callback',)
original_celery = celery.Celery
def construct_celery(*args, **kwargs):
    builtins.loaded_entries.append('resources:celery')
    return original_celery(*args, **kwargs)
celery.Celery = construct_celery
try:
    from app.tasks.celery_app import celery_app
    celery_app.loader.import_default_modules()
except {"ModuleNotFoundError" if problem == "missing_module" else "ValueError"}:
    pass
else:
    raise AssertionError('worker accepted an invalid formal business')
assert 'resources:celery' not in builtins.loaded_entries
assert 'app.businesses.probe.dev_callback' not in sys.modules
""",
    )


@pytest.mark.parametrize("declared", ["reqeust", "ecs_events"])
def test_cold_noninitial_entry_typo_rejects_startup_without_success_cache(tmp_path, declared):
    write_module(tmp_path, "integrations", "probe.helper")
    run_cold(
        tmp_path,
        SETUP
        + f"""
from app.businesses.definitions import BusinessDefinition
entry.DEFINITION = BusinessDefinition(code='probe', display_name='Probe')
entry.COMMANDS = ()
entry.ENTRY_MODULES = ('app.businesses.probe.{declared}',)
from app.integrations import discovery
try:
    discovery.load_formal_modules()
    catalog.validate_business_contracts()
except ValueError as error:
    assert entry.__name__ in str(error)
    assert entry.ENTRY_MODULES[0] in str(error)
    assert not discovery._loaded_formal and discovery._loading is False
else:
    raise AssertionError('declared noninitial entry was silently omitted')
""",
    )


@pytest.mark.parametrize("environment", ["production", "local"])
def test_fixed_platform_entries_are_required_and_do_not_scan_package_roots(tmp_path, environment):
    write_module(tmp_path, "businesses", "probe.helper")
    write_module(
        tmp_path,
        "integrations",
        "unlisted.request",
        "raise AssertionError('unlisted platform import')\n",
    )
    run_cold(
        tmp_path,
        SETUP
        + f"""
entry.ENTRY_MODULES = ()
from app.integrations import discovery
from app.core import module_tree
original_import = module_tree.import_module_tree
seen = []
def importing(name, *, required=False):
    seen.append((name, required))
    original_import(name, required=required)
module_tree.import_module_tree = importing
discovery.load_formal_modules()
discovery.load_local_modules(environment={environment!r})
expected = {{'app.integrations.wms_transport.callback'}}
if {environment == "local"!r}:
    expected.update((
        'app.integrations.ecs_callback.dev_ecs_event',
        'app.integrations.wms_callback.dev_callback',
        'app.integrations.wms_request.dev',
        'app.integrations.wms_transport.dev',
    ))
assert {{name for name, required in seen}} == expected, seen
assert all(required for name, required in seen), seen
from app.integrations.wms_callback.registry import operations
assert 'transport.task.resulted@v1' in operations()
assert 'transport.task.member_position_changed@v1' in operations()
if {environment == "local"!r}:
    from app.core.dev_scenarios import scenarios
    assert {{item.name for item in scenarios()}} == {{
        "rack.enter", "rack.rotate", "rack.depart", "bin.feed", "bin.return", "bin.exchange"
    }}
""",
    )


@pytest.mark.parametrize("environment", ["production", "local"])
def test_missing_fixed_platform_entry_fails_instead_of_skipping(monkeypatch, environment):
    from app.integrations import discovery
    from app.integrations.registration import PLATFORM_FORMAL_MODULES, PLATFORM_LOCAL_MODULES

    monkeypatch.setattr(discovery, "_loaded_formal", set())
    manifest = PLATFORM_LOCAL_MODULES if environment == "local" else PLATFORM_FORMAL_MODULES
    suffix = "dev.absent_platform" if environment == "local" else "absent_platform"
    monkeypatch.setitem(manifest, "request", (f"app.integrations.wms_request.{suffix}",))
    with pytest.raises(ModuleNotFoundError, match="absent_platform"):
        discovery.load_formal_modules()
        discovery.load_local_modules(environment=environment)


@pytest.mark.parametrize("kind", ["ecs_event", "callback", "request", "ecs_command"])
def test_direct_application_import_enforces_registration_source(tmp_path, kind):
    sources = {
        "ecs_event": (
            "from app.integrations.ecs_callback import ecs_event, CallbackPayload\n"
            "@ecs_event('DONE', device_role='SCAN', data=CallbackPayload)\n"
            "async def handler(ctx, payload): pass\n"
        ),
        "callback": (
            "from app.integrations.wms_callback import callback_operation, CallbackPayload\n"
            "@callback_operation('probe.done@v1', data=CallbackPayload)\n"
            "async def handler(ctx, payload): pass\n"
        ),
        "request": REQUEST,
        "ecs_command": (
            "from app.integrations.ecs_command import ecs_command\n"
            "from app.integrations.ecs_command.contracts import ErrorDetail\n"
            "from pydantic import BaseModel, ConfigDict\n"
            "class Payload(BaseModel):\n    model_config = ConfigDict(strict=True)\n"
            "@ecs_command('MOVE', device_roles=('SCAN',), annex_id='probe', annex_version='1',\n"
            "    result=Payload, error=ErrorDetail, timeout_min_ms=0, timeout_max_ms=1000)\n"
            "class Move(Payload): pass\n"
        ),
    }
    write_module(tmp_path, "integrations", f"unlisted.{kind}", sources[kind])
    run_cold(
        tmp_path,
        SETUP
        + f"""
import importlib
try:
    importlib.import_module('app.integrations.unlisted.{kind}')
except ValueError as error:
    assert 'invalid registration source' in str(error)
    assert 'app.integrations.unlisted.{kind}' in str(error)
    assert '修复' in str(error)
else:
    raise AssertionError('ordinary import bypassed registration source boundary')
""",
    )


@pytest.mark.parametrize("kind", ["ecs_event", "callback", "request"])
@pytest.mark.parametrize("owner", ["unlisted", "probe_shadow", "probe", "other"])
@pytest.mark.parametrize("consumer", ["catalog", "cli"])
def test_indirect_registrations_require_a_declared_catalog_entry(tmp_path, kind, owner, consumer):
    sources = {
        "ecs_event": (
            "from app.integrations.ecs_callback import ecs_event, CallbackPayload\n"
            "@ecs_event('DONE', device_role='PROBE_SCAN', data=CallbackPayload)\n"
            "async def handler(ctx, payload): pass\n"
        ),
        "callback": (
            "from app.integrations.wms_callback import callback_operation, CallbackPayload\n"
            "@callback_operation('probe.done@v1', data=CallbackPayload)\n"
            "async def handler(ctx, payload): pass\n"
        ),
        "request": REQUEST,
    }
    filename = "done" if kind == "ecs_event" else "probe_done_v1"
    module = f"app.businesses.{owner}.{kind}.{filename}"
    write_module(tmp_path, "businesses", f"{owner}.{kind}.{filename}", sources[kind])
    write_module(
        tmp_path,
        "businesses",
        "probe.request.trigger",
        f"import {module}\n",
    )
    write_module(
        tmp_path,
        "businesses",
        "probe.dev_callback",
        "raise AssertionError('local entry loaded before formal validation')\n",
    )
    run_cold(
        tmp_path,
        SETUP
        + f"""
from app.businesses.definitions import BusinessDefinition, DeviceRole
entry.DEFINITION = BusinessDefinition(
    code='probe', display_name='Probe',
    device_roles=(DeviceRole(role_key='PROBE_SCAN', display_name='Scan'),),
)
entry.COMMANDS = ()
entry.ENTRY_MODULES = ('app.businesses.probe.request.trigger',
                       'app.businesses.probe.dev_callback')
if {owner == "other"!r}:
    other = ModuleType('app.businesses.other.composition')
    other.DEFINITION = BusinessDefinition(
        code='other', display_name='Other', device_roles=entry.DEFINITION.device_roles,
    )
    other.COMMANDS = ()
    other.ENTRY_MODULES = ('app.businesses.other.{kind}',)
    catalog.entries = lambda: (entry, other)
if {consumer == "cli"!r}:
    import contextlib
    import io
    from scripts.check_integration_contracts import main
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        result = main(['--environment', 'production'])
    assert result == {0 if owner == "other" else 1}, (stdout.getvalue(), stderr.getvalue())
    if {owner != "other"!r}:
        assert stdout.getvalue() == ''
else:
    from app.integrations.discovery import load_formal_modules
    load_formal_modules()
    try:
        catalog.validate_business_contracts()
    except ValueError as error:
        assert {owner != "other"!r}, str(error)
        assert {module!r} in str(error)
    else:
        assert {owner == "other"!r}, 'undeclared indirect registration was accepted'
assert 'app.businesses.probe.dev_callback' not in sys.modules
if {owner != "other"!r}:
    try:
        catalog.validated_entries()
    except RuntimeError:
        pass
    else:
        raise AssertionError('invalid business catalog remained usable')
""",
    )
