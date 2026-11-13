"""The first coarse sorting action preserves raw facts and strict peer contracts."""

import os
import subprocess
import sys

import pytest
from pydantic import ValidationError


def test_move_forward_serializes_only_the_original_barcode():
    from app.businesses.coarse_sorting.ecs_command import MoveForward

    assert MoveForward(barcode=" RAW,条码 ").model_dump(mode="json") == {"barcode": " RAW,条码 "}


@pytest.mark.parametrize("data", [{"barcode": 123}, {"barcode": "A", "location": "B"}, {}])
def test_move_forward_rejects_noncontract_parameters(data):
    from app.businesses.coarse_sorting.ecs_command import MoveForward

    with pytest.raises(ValidationError):
        MoveForward.model_validate(data)


def test_move_result_accepts_empty_failure_data_and_physical_completion():
    from app.businesses.coarse_sorting.ecs_command import MoveForwardResult

    MoveForwardResult.model_validate({})
    result = MoveForwardResult.model_validate({"actual_qty": 1})
    assert result.actual_qty == 1


@pytest.mark.parametrize(
    "data",
    [
        {"actual_qty": True},
        {"actual_qty": "1"},
        {"actual_qty": 1.0},
        {"actual_qty": 0},
        {"actual_qty": 2},
    ],
)
def test_move_result_rejects_invalid_completion_facts(data):
    from app.businesses.coarse_sorting.ecs_command import MoveForwardResult

    with pytest.raises(ValidationError):
        MoveForwardResult.model_validate(data)


def test_validate_bind_preserves_raw_scan_facts():
    from app.businesses.coarse_sorting.request.inbound_coarse_sorting_validate_bind_v1 import (
        ValidateBind,
    )

    data = {
        "workline_code": "COARSE-A",
        "barcode": " RAW,条码 ",
        "reel_diameter": 15,
        "reel_thickness": 20.5,
    }
    assert ValidateBind.model_validate(data).model_dump(mode="json") == data
    for changes in ({"workline_code": ""}, {"reel_thickness": "20"}, {"box_code": "BOX"}):
        with pytest.raises(ValidationError):
            ValidateBind.model_validate({**data, **changes})


@pytest.mark.parametrize(
    "data", [{"result": "OK", "binding_id": "BIND-1"}, {"result": "NG", "reason_code": "BAD"}]
)
def test_validate_bind_decision_requires_its_original_business_reference(data):
    from app.businesses.coarse_sorting.contracts import ValidateBindDecision

    assert ValidateBindDecision.model_validate(data).model_dump(exclude_none=True) == data


@pytest.mark.parametrize(
    "data",
    [
        {"result": "OK"},
        {"result": "OK", "binding_id": ""},
        {"result": "OK", "binding_id": 1},
        {"result": "NG"},
        {"result": "NG", "reason_code": ""},
        {"result": "NG", "reason_code": 1},
        {"result": "NG", "reason_code": "BAD", "binding_id": "BIND-1"},
        {"result": "UNKNOWN"},
        {"result": "OK", "binding_id": "BIND-1", "extra": True},
    ],
)
def test_validate_bind_rejects_incomplete_or_conflicting_decisions(data):
    from app.businesses.coarse_sorting.contracts import ValidateBindDecision

    with pytest.raises(ValidationError):
        ValidateBindDecision.model_validate(data)


@pytest.mark.parametrize("environment", ["production", "local"])
def test_cold_catalog_exposes_formal_forward_contract_without_runtime_resources(environment):
    code = """
import sys
from app.businesses import catalog
from app.integrations.discovery import load_formal_modules, load_local_modules
from app.integrations.wms_request.operation import lookup, Endpoint
load_formal_modules()
bindings, annexes = catalog.validate_business_contracts()
load_local_modules(environment=ENVIRONMENT)
definition = catalog.get_definition('coarse_sorting')
required = {role.role_key for role in definition.device_roles if role.required}
assert required == {'INPUT_ARM','CONVEYOR'}
assert not any(
    b.task_type in {'PUT_TO_BIN', 'MOVE_TO_NG'}
    and 'coarse_sorting' in catalog.contract_consumers(
        ('ecs_command', b.task_type, b.device_role)
    )
    for b in bindings
)
assert lookup('inbound.coarse_sorting.storage_allocate@v1') is None
assert lookup('inbound.coarse_sorting.execution_report@v1') is None
binding = next(b for b in bindings if b.device_role == 'CONVEYOR' and b.task_type == 'MOVE_FORWARD')
assert (binding.annex_id, binding.annex_version) == ('coarse_sorting.move_forward','2')
annex = annexes[('coarse_sorting.move_forward','2')]
assert (annex.timeout_min_ms, annex.timeout_max_ms) == (1, 60000)
request = lookup('inbound.coarse_sorting.validate_bind@v1')
assert request.endpoint is Endpoint.DECISIONS
assert not request.registration_source.local_only
assert 'app.businesses.coarse_sorting.service' not in sys.modules
assert 'app.core.database' not in sys.modules
""".replace("ENVIRONMENT", repr(environment))
    result = subprocess.run(
        [sys.executable, "-c", code], env=os.environ.copy(), capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("location", ["A", "B", "C", "D"])
@pytest.mark.parametrize("bin_type", ["3", "2", "008", "WMS-CUSTOM", 2, 42])
def test_candidate_put_accepts_rack_bin_locations_and_original_index(location, bin_type):
    from app.businesses.coarse_sorting.ecs_command import PutToBin

    command = PutToBin(
        barcode="RAW-1",
        bin_location=location,
        bin_type=bin_type,
        bin_cell_location=7,
        reel_totalthickness=120,
    )
    assert command.bin_location == location
    assert command.bin_cell_location == 7
    assert command.bin_type == str(bin_type)


@pytest.mark.parametrize("bin_type", [None, True, 3.0, ""])
def test_put_bin_type_rejects_invalid_codes(bin_type):
    from pydantic import ValidationError

    from app.businesses.coarse_sorting.ecs_command import PutToBin

    with pytest.raises(ValidationError):
        PutToBin(
            barcode="RAW-1",
            bin_location="A",
            bin_type=bin_type,
            bin_cell_location=1,
            reel_totalthickness=0,
        )
