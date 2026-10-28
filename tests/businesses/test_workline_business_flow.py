"""Public seams only: disposable test businesses, never a production catalog entry."""

import pytest
from sqlalchemy import select

from app.businesses.runtime import BusinessRuntime
from app.integrations.ecs_command.declaration import command_declaration
from app.models import CallbackReceipt
from tests.helpers import FlowMove, business_flow_fixture, flow_request


@pytest.fixture
async def flow(client, admin, monkeypatch):
    from app.main import app

    async with business_flow_fixture(app.state.audit_session_factory, monkeypatch) as state:
        await state.configure(client, admin)
        yield state


async def test_same_business_two_lines_never_cross_routes(flow, client):
    for index in (0, 1):
        path, body = flow_request(flow, index=index)
        assert (await client.post(path, json=body)).status_code == 200
    rows = await flow.rows()
    assert len(rows) == 2
    for row in rows:
        result = await flow.runtime.services[row["business"]].advance(row["id"])
        assert result == "advanced"
    for row in await flow.rows():
        index = flow.codes.index(row["device_code"])
        assert row["next_intent"]["device_code"] == flow.codes[index]
        assert row["next_intent"]["position"] == f"POSITION_{index}"
        async with flow.factory() as session:
            command = await flow.commands.get_by_call(session, owner=flow.owner, call_key=row["id"])
            assert command.command_code == row["next_intent"]["command_code"]
            route = await flow.commands.routes.resolve_declared(
                session, flow.codes[index], command_declaration(FlowMove)
            )
            assert route.origin == flow.origins[index]


async def test_business_mismatch_never_creates_intent(flow, client):
    path, body = flow_request(flow, index=2, kind="wms", business="seam_a")
    response = await client.post(path, json=body)
    assert response.status_code == 422, response.text
    assert await flow.rows() == []
    async with flow.factory() as session:
        receipt = await session.scalar(select(CallbackReceipt))
        assert receipt.status_code == 422


async def test_two_businesses_same_event_name_use_own_role_payload(flow, client):
    for index in (0, 2):
        path, body = flow_request(flow, index=index)
        assert (await client.post(path, json=body)).status_code == 200
    rows = await flow.rows()
    assert {r["business"] for r in rows} == {"seam_a", "seam_b"}
    assert {r["accepted_payload"]["token"] for r in rows if r["business"] == "seam_a"} == {"A"}
    assert {r["accepted_payload"]["count"] for r in rows if r["business"] == "seam_b"} == {7}
    path, body = flow_request(flow, index=2)
    body["data"].pop("count")
    body["data"]["token"] = "wrong role payload"
    assert (await client.post(path, json=body)).status_code == 400
    assert len(await flow.rows()) == 2


@pytest.mark.parametrize("kind", ["ecs", "wms"])
async def test_callback_restart_preserves_original_receipt(flow, client, monkeypatch, kind):
    from app.main import app

    path, body = flow_request(flow, kind=kind)
    assert (await client.post(path, json=body)).status_code == (200 if kind == "ecs" else 202)
    before = (await flow.rows())[0]
    # Rebuild stateless services from the same capabilities, as after process restart.
    runtime = flow.rebuild()
    assert isinstance(runtime, BusinessRuntime) and runtime is not flow.runtime
    monkeypatch.setattr(app.state, "business_runtime", runtime)
    assert (await client.post(path, json=body)).status_code == 200
    assert await flow.rows() == [before]
    async with flow.factory() as session:
        assert len(list(await session.scalars(select(CallbackReceipt)))) == 1
    assert await runtime.services["seam_a"].advance(before["id"]) == "advanced"
    assert await runtime.services["seam_a"].advance(before["id"]) == "done"


@pytest.mark.parametrize("environment", ["local", "production"])
@pytest.mark.parametrize("invalid", [False, True])
def test_app_checks_all_business_contracts_before_any_local_or_runtime_assembly(
    tmp_path, environment, invalid
):
    import os
    import subprocess
    import sys

    script = r'''
import asyncio
import builtins
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import app.businesses
from app.businesses import catalog
from app.services import device_routes
from app.reliable import Outbox
from tests.helpers import business_composition

root, environment, invalid = Path(sys.argv[1]), sys.argv[2], sys.argv[3] == "True"
trace = builtins.business_startup_trace = []
app.businesses.__path__.append(str(root))
entries = []
for index, code in enumerate(("startup_a", "startup_b")):
    folder = root / code
    folder.mkdir()
    (folder / "__init__.py").write_text("")
    role = "STARTUP_A" if index == 0 else "STARTUP_B"
    event_name = "WRONG_EVENT" if invalid and index == 1 else "SCAN_COMPLETED"
    (folder / "ecs_event.py").write_text(f"""
import builtins
from app.integrations.ecs_callback import CallbackPayload, ecs_event
builtins.business_startup_trace.append('formal:{code}')
class Scan(CallbackPayload):
    code: str
@ecs_event('{event_name}', device_role='{role}', data=Scan)
async def scan(ctx, payload):
    pass
""")
    (folder / "dev_ecs_event.py").write_text(
        f"import builtins; builtins.business_startup_trace.append('local:{code}')"
    )
    entry = business_composition(code, role)
    entry.ENTRY_MODULES += (f"app.businesses.{code}.dev_ecs_event",)
    def factory(capabilities, code=code):
        trace.append('factory:' + code)
        return object()
    entry.build_service = factory
    entries.append(entry)
catalog.entries = lambda: tuple(entries)
validate = catalog.validate_business_contracts
def check():
    result = validate()
    trace.append('crosscheck')
    return result
catalog.validate_business_contracts = check
routes = device_routes.DeviceRoutes
def assemble_routes(*args, **kwargs):
    trace.append('resources:routes')
    return routes(*args, **kwargs)
device_routes.DeviceRoutes = assemble_routes
try:
    import app.main as main
except ValueError:
    assert invalid
    assert trace == ['formal:startup_a', 'formal:startup_b'], trace
    assert not any(
        name.startswith('app.businesses.startup_') and '.dev_' in name for name in sys.modules
    )
else:
    assert not invalid
    @asynccontextmanager
    async def reliable(*args, **kwargs):
        trace.append('resources:outbox')
        yield Outbox({}, {})
    main.reliable_lifespan = reliable
    async def start():
        async with main.lifespan(main.app):
            trace.append('accepting')
    asyncio.run(start())
    expected = ['formal:startup_a', 'formal:startup_b', 'crosscheck']
    if environment == 'local':
        expected += ['local:startup_a', 'local:startup_b', 'resources:routes']
    else:
        assert not any(
        name.startswith('app.businesses.startup_') and '.dev_' in name for name in sys.modules
    )
        from app.integrations.ecs_callback.registry import events
        assert not any(item.registration_source.local_only for item in events().values())
    expected += ['resources:outbox', 'factory:startup_a', 'factory:startup_b', 'accepting']
    assert trace == expected, trace
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), environment, str(invalid)],
        capture_output=True,
        text=True,
        env={**os.environ, "ENVIRONMENT": environment, "DATABASE_URL": "sqlite+aiosqlite://"},
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("setup_fails", [False, True])
async def test_business_fixture_owns_and_cleans_only_its_temporary_table(
    asset_factory, monkeypatch, setup_fails
):
    from sqlalchemy import inspect

    from app.businesses import catalog
    from app.models import Base

    factory = asset_factory
    assert not any(t.name == "business_seam_test_execution" for t in Base.metadata.tables.values())
    with monkeypatch.context() as patch:
        if setup_fails:

            def fail():
                raise RuntimeError("setup failed")

            patch.setattr(catalog, "validate_business_contracts", fail)
        with pytest.raises(RuntimeError, match="setup failed" if setup_fails else "body failed"):
            async with business_flow_fixture(factory, patch) as flow:
                assert await flow.rows() == []
                raise RuntimeError("body failed")
    async with factory.kw["bind"].connect() as connection:
        names = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
    assert "business_seam_test_execution" not in names
    assert "work_lines" in names


@pytest.mark.parametrize("failure_point", ["line_commit", "device_commit"])
async def test_partial_configuration_cleanup_preserves_unrelated_rows(
    client, admin, monkeypatch, failure_point
):
    from sqlalchemy import delete

    from app.main import app
    from app.models import Device, DeviceRoute, WorkLine
    from tests.helpers import add_bound_device

    factory = app.state.audit_session_factory
    async with factory.begin() as session:
        line, device, route = await add_bound_device(
            session, "UNRELATED_SEAM", "http://127.0.0.1:45991"
        )
        unrelated = (line.id, device.id, route.id)
    original_post = client.post
    created = []

    async def fail_after_line_commit(path, **kwargs):
        response = await original_post(path, **kwargs)
        if path == "/api/v1/work-lines":
            assert response.status_code == 201
            created.append(int(response.json()["id"]))
            if failure_point == "line_commit":
                raise RuntimeError("after configuration commit")
        return response

    async def fail_after_device_commit(path, **kwargs):
        raise RuntimeError("after configuration commit")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(client, "post", fail_after_line_commit)
            if failure_point == "device_commit":
                patch.setattr(client, "put", fail_after_device_commit)
            with pytest.raises(RuntimeError, match="after configuration commit"):
                async with business_flow_fixture(factory, patch) as flow:
                    await flow.configure(client, admin)
        assert len(created) == 1
        async with factory() as session:
            assert await session.get(WorkLine, created[0]) is None
            assert list(await session.scalars(select(WorkLine.id))) == [unrelated[0]]
            assert list(await session.scalars(select(Device.id))) == [unrelated[1]]
            assert list(await session.scalars(select(DeviceRoute.id))) == [unrelated[2]]
    finally:
        async with factory.begin() as session:
            await session.execute(delete(Device).where(Device.id == unrelated[1]))
            await session.execute(delete(WorkLine).where(WorkLine.id.in_([*created, unrelated[0]])))
            await session.execute(delete(DeviceRoute).where(DeviceRoute.id == unrelated[2]))
