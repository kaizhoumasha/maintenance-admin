import subprocess
import sys


def test_catalog_loads_real_manual_and_shared_inputs_once():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os
os.environ['SMT_MANUAL_FIRST_PLAN_WAIT_MS'] = '4321'
from app.businesses import catalog
from app.businesses.runtime import build_runtime
from app.integrations.discovery import load_formal_modules
from app.businesses._components.bin_roller.models import TABLES as roller_tables
from app.businesses.smt_manual.models import TABLES

assert 'smt_manual' in {e.DEFINITION.code for e in catalog.entries()}
load_formal_modules()
catalog.validate_business_contracts()
runtime = build_runtime(outbox=object(), ecs_commands=object(), session_factory=lambda: None)
host = runtime.services['smt_manual']
assert host.first_plan_wait_ms == 4321
assert host.repository.tables is TABLES
assert host.components['bin_roller'].tables is roller_tables
assert set(host.components) == {'sorting_task', 'bin_roller', 'rack_dispatch'}
assert host.components['bin_roller'].rack_dispatch is host.components['rack_dispatch']
departure = ('wms_request', 'rack.departure_decide@v1', None)
assert {'smt_manual', 'smt_auto'} <= catalog.contract_consumers(departure)
assert host.components['sorting_task'].production is host
assert host.components['bin_roller'].context.domain == 'smt_manual'
assert host.capabilities.wms.owner_domain == 'smt_manual'
assert host.capabilities.transport.owner_domain == 'smt_manual'
entry = next(e for e in catalog.entries() if e.DEFINITION.code == 'smt_manual')
assert entry.COMMANDS == ()
assert entry.PROGRESSION == 'scan_pending'
assert runtime.progressions['smt_auto'].__self__ is runtime.services['smt_auto']
for name in ('issued', 'queue_changed', 'plan_delta', 'cancel'):
    key = ('wms_callback', f'sorting.task.{name}@v1', None)
    assert catalog.contract_consumers(key) == {'smt_manual', 'smt_auto'}
    assert catalog.input_target('smt_manual', key).component_key == 'sorting_task'
for name in ('prepare', 'completion_confirm'):
    key = ('wms_request', f'sorting.task.{name}@v1', None)
    assert catalog.contract_consumers(key) == {'smt_manual', 'smt_auto'}
for point in ('SCAN1', 'SCAN2', 'SCAN3', 'SCAN4'):
    key = ('ecs_event', 'SCAN_COMPLETED', point)
    assert catalog.input_target('smt_manual', key).component_key == 'bin_roller'
slots = {s.slot_key: s for s in runtime.definitions['smt_manual'].position_slots}
assert not slots['RETURN_RACK'].required and not slots['TRANSFER_RACK'].required
assert runtime.progressions['smt_manual'].__self__ is host
""",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
