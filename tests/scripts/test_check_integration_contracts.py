import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers import METADATA_ONLY_GUARD, run_isolated_module, write_python_modules

MODULE = "scripts.check_integration_contracts"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def contract_packages(tmp_path, *, packaged=True):
    """Complete real declarations, with shared permissive legacy annex models."""
    files = {"legacy/__init__.py": ""}
    for code, role in (("alpha", "ALPHA_SCAN"), ("beta", "BETA_SCAN")):
        prefix = f"businesses/{code}"
        files[f"{prefix}/__init__.py"] = ""
        files[f"{prefix}/composition.py"] = f"""
from app.businesses.definitions import BusinessDefinition, DeviceRole, InitialEntry
from app.businesses.alpha.contracts import Payload, ANNEX
from app.integrations.ecs_command import CommandBinding
DEFINITION = BusinessDefinition(code={code!r}, display_name='Probe',
    device_roles=(DeviceRole(role_key={role!r}, display_name='Scan'),),
    initial_entries=(InitialEntry(kind='wms_callback', name='{code}.done@v1'),))
ENTRY_MODULES = tuple('app.businesses.{code}.' + name for name in
    ('ecs_event', 'callback', 'request', 'dev_ecs_event', 'dev_callback', 'dev'))
MODEL_MODULES = ('app.businesses.{code}.models',)
COMMAND_BINDINGS = (CommandBinding({role!r}, 'MOVE', 'shared', '1', Payload),)
ANNEXES = {{('shared', '1'): ANNEX}}
def build_service(capabilities):
    raise AssertionError('business factory must not run')
"""
        files[f"{prefix}/models.py"] = "raise AssertionError('business models must not load')\n"
    files["businesses/alpha/contracts.py"] = """
from pydantic import BaseModel
from app.integrations.ecs_command import DeviceAnnex
from app.integrations.ecs_command.contracts import ErrorDetail
class Payload(BaseModel):
    value: int = 1
ANNEX = DeviceAnnex(Payload, Payload, ErrorDetail, 0, 1000)
"""
    for owner, code, role, use_package in (
        ("businesses/alpha", "alpha", "ALPHA_SCAN", packaged),
        ("businesses/beta", "beta", "BETA_SCAN", not packaged),
        ("legacy", "legacy", "LEGACY_SCAN", not packaged),
    ):
        for local in (False, True):
            suffix = "preview" if local else "done"
            event = f"{code.upper()}_{suffix.upper()}"
            sources = {
                "dev_ecs_event" if local else "ecs_event": (
                    event.lower(),
                    f"""
from app.integrations.ecs_callback import CallbackPayload, ecs_event
@ecs_event({event!r}, device_role={role!r}, data=CallbackPayload)
async def handle(ctx, payload):
    raise AssertionError('ECS handler must not run')
""",
                ),
                "dev_callback" if local else "callback": (
                    f"{code}_{suffix}_v1",
                    f"""
from app.integrations.wms_callback import CallbackPayload, callback_operation
@callback_operation('{code}.{suffix}@v1', data=CallbackPayload)
async def handle(ctx, payload):
    raise AssertionError('WMS handler must not run')
""",
                ),
                "dev" if local else "request": (
                    f"{code}_{suffix}_report_v1",
                    f"""
from app.integrations.wms_request import Endpoint, RequestPayload, request_operation
@request_operation('{code}.{suffix}.report@v1', Endpoint.FACTS)
class Report(RequestPayload):
    value: int = 1
""",
                ),
            }
            for entry, (filename, source) in sources.items():
                if use_package:
                    files[f"{owner}/{entry}/__init__.py"] = ""
                    files[f"{owner}/{entry}/{filename}.py"] = source
                else:
                    files[f"{owner}/{entry}.py"] = source
    write_python_modules(tmp_path, files)
    return f"""
from app import businesses, integrations
businesses.__path__.insert(0, {str(tmp_path / "businesses")!r})
integrations.__path__.insert(0, {str(tmp_path)!r})
from app.businesses import catalog
def entries():
    from app.businesses.alpha import composition as alpha
    from app.businesses.beta import composition as beta
    return (beta, alpha)
catalog.entries = entries
"""


def check(tmp_path, environment="production", *, bootstrap="", inherited=None):
    return run_isolated_module(
        MODULE,
        ["--environment", environment],
        cwd=tmp_path,
        bootstrap=(
            f"allow_ecs_definition_imports = {environment == 'local'!r}\n"
            + METADATA_ONLY_GUARD
            + bootstrap
        ),
        environment=inherited,
    )


def test_empty_business_catalog_reports_checked_formal_scope():
    clean_env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(PROJECT_ROOT)}
    result = subprocess.run(
        [sys.executable, "-m", MODULE, "--environment", "production"],
        cwd=PROJECT_ROOT,
        env=clean_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert set(report) == {"environment", "businesses", "entries", "command_contracts", "scope"}
    assert report["environment"] == "production"
    assert report["businesses"] == []
    assert report["command_contracts"] == []
    assert any(row["name"] == "transport.task.resulted@v1" for row in report["entries"])
    assert report["scope"]["checked"]
    assert report["scope"]["excluded"]


@pytest.mark.parametrize("environment", ["production", "local"])
@pytest.mark.parametrize("packaged", [False, True])
def test_environment_selects_scope_after_formal_validation(tmp_path, environment, packaged):
    bootstrap = contract_packages(tmp_path, packaged=packaged)
    bootstrap += """
original_validate = catalog.validate_business_contracts
def validate():
    from app.integrations.ecs_callback.registry import events
    from app.integrations.wms_callback.registry import operations
    from app.integrations.wms_request.operation import operations as requests
    assert all(not item.registration_source.local_only
        for reader in (events, operations, requests) for item in reader().values())
    return original_validate()
catalog.validate_business_contracts = validate
"""
    result = check(
        tmp_path,
        environment,
        bootstrap=bootstrap,
        inherited={"ENVIRONMENT": "local" if environment == "production" else "production"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    report = json.loads(result.stdout)
    assert report["environment"] == environment
    assert report["businesses"] == ["alpha", "beta"]
    rows = report["entries"]
    for prefix, code, role in (
        ("app.businesses.alpha", "alpha", "ALPHA_SCAN"),
        ("app.businesses.beta", "beta", "BETA_SCAN"),
        ("app.integrations.legacy", "legacy", "LEGACY_SCAN"),
    ):
        for kind, name, device_role in (
            ("ecs_event", f"{code.upper()}_DONE", role),
            ("wms_callback", f"{code}.done@v1", None),
            ("wms_request", f"{code}.done.report@v1", None),
        ):
            row = next(row for row in rows if row["kind"] == kind and row["name"] == name)
            assert row["device_role"] == device_role
            assert row["module"].startswith(prefix + ".")
            assert Path(row["source_file"]).is_file()
            assert row["symbol"] in {"handle", "Report"}
            assert row["local_only"] is False
    assert rows == sorted(
        rows, key=lambda row: (row["kind"], row["device_role"] or "", row["name"])
    )
    local_rows = [row for row in rows if row["local_only"]]
    if environment == "local":
        assert {row["kind"] for row in local_rows} == {"ecs_event", "wms_callback", "wms_request"}
        assert any(row["name"] == "alpha.preview@v1" for row in local_rows)
    else:
        assert local_rows == []
    commands = report["command_contracts"]
    assert [row["device_role"] for row in commands] == ["ALPHA_SCAN", "BETA_SCAN"]
    for row in commands:
        assert row["name"] == "MOVE"
        assert row["kind"] == "ecs_command"
        assert (row["annex_id"], row["annex_version"]) == ("shared", "1")
        assert row["module"] == "app.businesses.alpha.contracts"
        assert row["symbol"] == "Payload"
        assert row["timeout_min_ms"] == 0
        assert row["timeout_max_ms"] == 1000
    assert "payload_schema" not in result.stdout


@pytest.mark.parametrize(
    ("problem", "phase", "error_type"),
    [
        ("duplicate", "load_formal_modules", "ValueError"),
        ("filename", "load_formal_modules", "ValueError"),
        ("role", "validate_business_contracts", "ValueError"),
        ("missing_annex", "validate_business_contracts", "ValueError"),
        ("annex_conflict", "validate_business_contracts", "ValueError"),
        ("missing_module", "load_formal_modules", "ModuleNotFoundError"),
        ("internal_import", "load_formal_modules", "ImportError"),
    ],
)
@pytest.mark.parametrize("environment", ["production", "local"])
def test_invalid_contract_never_reports_success(tmp_path, problem, phase, error_type, environment):
    bootstrap = contract_packages(tmp_path)
    callback = tmp_path / "businesses/alpha/callback/alpha_done_v1.py"
    composition = tmp_path / "businesses/alpha/composition.py"
    if problem == "duplicate":
        callback.write_text(
            callback.read_text()
            + "\ncallback_operation('alpha.done@v1', data=CallbackPayload)(handle)\n"
        )
    elif problem == "filename":
        callback.rename(callback.with_name("wrong.py"))
    elif problem == "role":
        composition.write_text(
            composition.read_text()
            + "\nCOMMAND_BINDINGS = (CommandBinding('WRONG', 'MOVE', 'shared', '1', Payload),)\n"
        )
    elif problem == "missing_annex":
        composition.write_text(composition.read_text() + "\nANNEXES = {}\n")
    elif problem == "annex_conflict":
        other = tmp_path / "businesses/beta/composition.py"
        other.write_text(
            other.read_text() + "\nfrom dataclasses import replace\n"
            "ANNEXES = {('shared', '1'): replace(ANNEX, timeout_max_ms=2000)}\n"
        )
    elif problem == "missing_module":
        composition.write_text(
            composition.read_text()
            + "\nENTRY_MODULES += ('app.businesses.alpha.ecs_event.missing',)\n"
        )
    else:
        callback.write_text("raise ImportError('internal contract dependency failed')\n")
    result = check(tmp_path, environment, bootstrap=bootstrap)
    assert result.returncode == 1, result.stderr
    assert result.stdout == ""
    assert f"phase={phase}" in result.stderr
    assert f"error_type={error_type}" in result.stderr
    assert "origin=" in result.stderr
    assert "app/" in result.stderr
    assert "修复" in result.stderr


@pytest.mark.parametrize("environment", ["production", "local"])
def test_invalid_local_contract_is_checked_only_in_local_scope(tmp_path, environment):
    bootstrap = contract_packages(tmp_path)
    path = tmp_path / "businesses/alpha/dev_callback/alpha_preview_v1.py"
    path.write_text("raise ValueError('invalid local declaration')\n")
    result = check(tmp_path, environment, bootstrap=bootstrap)
    if environment == "production":
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["environment"] == "production"
        assert result.stderr == ""
    else:
        assert result.returncode == 1, result.stderr
        assert result.stdout == ""
        assert "phase=load_local_modules" in result.stderr
        assert "error_type=ValueError" in result.stderr


def test_invalid_formal_entry_cannot_be_repaired_by_local_entry(tmp_path):
    bootstrap = contract_packages(tmp_path)
    (tmp_path / "businesses/alpha/callback/alpha_done_v1.py").unlink()
    local = tmp_path / "businesses/alpha/dev_callback/alpha_preview_v1.py"
    local.rename(local.with_name("alpha_done_v1.py"))
    local = local.with_name("alpha_done_v1.py")
    local.write_text(local.read_text().replace("alpha.preview@v1", "alpha.done@v1"))
    result = check(tmp_path, "local", bootstrap=bootstrap)
    assert result.returncode == 1, result.stderr
    assert result.stdout == ""
    assert "phase=validate_business_contracts" in result.stderr
    assert "factory must not run" not in result.stderr


@pytest.mark.parametrize("environment", ["production", "local"])
def test_check_does_not_invoke_runtime_or_business_handlers(tmp_path, environment):
    result = check(tmp_path, environment, bootstrap=contract_packages(tmp_path))
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    report = json.loads(result.stdout)
    assert report["businesses"] == ["alpha", "beta"]
    excluded = " ".join(report["scope"]["excluded"]).lower()
    for unverified in ("runtime", "status", "v3", "models", "network", "permissions", "physical"):
        assert unverified in excluded


@pytest.mark.parametrize(
    ("arguments", "status"),
    [(["--help"], 0), ([], 2), (["--environment", "staging"], 2)],
)
def test_cli_arguments_are_validated_before_application_imports(tmp_path, arguments, status):
    result = run_isolated_module(
        MODULE,
        arguments,
        cwd=tmp_path,
        bootstrap="""
import importlib.abc
class NoApplication(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'app' or fullname.startswith('app.'):
            raise AssertionError('application imported before argument validation')
sys.meta_path.insert(0, NoApplication())
""",
    )
    assert result.returncode == status, result.stderr
    assert "application imported" not in result.stderr
    assert "--environment" in result.stdout + result.stderr


def test_failure_diagnostics_never_echo_configuration_or_payload(tmp_path):
    bootstrap = contract_packages(tmp_path)
    secret = "SENSITIVE_CONTRACT_PAYLOAD_730194"
    path = tmp_path / "businesses/alpha/callback/alpha_done_v1.py"
    path.write_text(f"raise ValueError({secret!r})\n")
    result = check(tmp_path, bootstrap=bootstrap, inherited={"JWT_SECRET_KEY": secret})
    assert result.returncode == 1, result.stderr
    assert result.stdout == ""
    assert secret not in result.stdout + result.stderr
    assert "phase=load_formal_modules" in result.stderr
    assert "error_type=ValueError" in result.stderr
    assert "alpha_done_v1.py" in result.stderr
