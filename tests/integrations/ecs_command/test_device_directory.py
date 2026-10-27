from app.core.identifiers import uuid7
from app.integrations.ecs_command import CommandBinding
from app.models import Device, DeviceRoute, EcsCommand, WorkLine
from app.services.asset_audit import AssetActor
from app.services.device_routes import DeviceRoutes
from app.services.device_service import delete_device
from tests.helpers import TrialPayload, add_bound_device, workline_definition_lookup


async def test_role_switch_selects_new_contract_and_preserves_original_result_contract(ecs):
    import json

    import pytest
    from pydantic import BaseModel, ConfigDict
    from sqlalchemy import delete

    from app.integrations.ecs_command import DeviceAnnex
    from app.integrations.ecs_command.contracts import ErrorDetail
    from app.integrations.ecs_command.lifecycle import EcsLifecycle
    from app.integrations.ecs_command.protocol import PROTOCOL
    from app.integrations.ecs_command.result import receive_ecs_result
    from app.reliable.store import MessageStore
    from app.schemas.workline import WorkLineUpdate
    from app.services.workline_service import update_workline

    class ExitPayload(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")
        exit_code: str

    ecs.service.annexes[("exit", "1")] = DeviceAnnex(
        ExitPayload, ExitPayload, ErrorDetail, 1, 60000
    )
    ecs.service.routes = DeviceRoutes(
        [
            CommandBinding("SCAN1", "MOVE", "test", "1", TrialPayload),
            CommandBinding("SCAN4", "MOVE", "exit", "1", ExitPayload),
        ],
        annexes=ecs.service.annexes,
    )
    code = "role-" + uuid7().hex
    async with ecs.factory.begin() as session:
        line, device, route = await add_bound_device(session, code, "http://127.0.0.1:45731")
        line_id, device_id, route_id = line.id, device.id, route.id

    async def create(session, key, params):
        return await ecs.service.create(
            session,
            owner=ecs.owner,
            call_key=key,
            device_code=code,
            task_type="MOVE",
            timeout_ms=30000,
            params=params,
        )

    try:
        async with ecs.factory.begin() as session:
            original = await create(session, "entry", {"qty": 1})
        store = MessageStore(ecs.factory, lifecycles={PROTOCOL: EcsLifecycle(ecs.service)})
        claim = (await store.claim(1, ecs.targets))[0]
        assert await store.before_send(claim, ecs.targets["ecs"])
        async with ecs.factory() as session:
            await update_workline(
                session,
                line_id,
                WorkLineUpdate(
                    line_name="Exit line",
                    version=1,
                    device_bindings={"SCAN4": code},
                ),
                actor=AssetActor(1, "role-contract", None, None),
                definition_lookup=workline_definition_lookup,
            )
        async with ecs.factory.begin() as session:
            with pytest.raises(ValueError):
                await create(session, "invalid-exit", {"qty": 2})
            new = await create(session, "exit", {"exit_code": "RETURN"})
            assert (await session.get(EcsCommand, new.command_code)).route["annex_id"] == "exit"
            assert (
                await create(session, "entry", {"qty": 1})
            ).command_code == original.command_code
            old = await session.get(EcsCommand, original.command_code)
            assert (
                await ecs.service.routes.authorize_frozen(session, old.route)
            ).value == "REVOKED"
            response = await receive_ecs_result(
                session,
                json.dumps(
                    {
                        "command_code": original.command_code,
                        "device_code": code,
                        "result": "SUCCESS",
                        "finish_time": 1,
                        "data": {"qty": 1},
                    }
                ).encode(),
                ecs.service,
            )
            assert response.status_code == 200
    finally:
        async with ecs.factory.begin() as session:
            await session.execute(delete(Device).where(Device.id == device_id))
            await session.execute(delete(DeviceRoute).where(DeviceRoute.id == route_id))
            await session.execute(delete(WorkLine).where(WorkLine.id == line_id))


async def test_new_directory_identity_preserves_frozen_command_and_independent_intent(ecs):
    from sqlalchemy import delete

    code = "directory-" + uuid7().hex
    routes = DeviceRoutes(
        [CommandBinding("SCAN1", "MOVE", "test", "1", TrialPayload)], annexes=ecs.service.annexes
    )
    ecs.service.routes = routes
    route_ids = []
    device_ids = []
    line_ids = []

    async def add(origin):
        async with ecs.factory.begin() as session:
            line, row, route = await add_bound_device(session, code, origin)
            route_ids.append(route.id)
            device_ids.append(row.id)
            line_ids.append(line.id)

    async def command(session, key):
        return await ecs.service.create(
            session,
            owner=ecs.owner,
            call_key=key,
            device_code=code,
            task_type="MOVE",
            timeout_ms=30000,
            params={"qty": 1},
        )

    try:
        await add("http://127.0.0.1:45691")
        async with ecs.factory.begin() as session:
            first = await command(session, "first")
            row = await session.get(EcsCommand, first.command_code)
            frozen = (dict(row.route), row.request_raw, row.delivery_snapshot, row.expires_at)
            row.status = "HELD"
            row.needs_reconciliation = True
            row.hold_reason = "UNKNOWN_DELIVERY"
        async with ecs.factory.begin() as session:
            line = await session.get(WorkLine, line_ids[0])
            line.device_bindings = {}
            line.version += 1
        async with ecs.factory() as session:
            await delete_device(
                session, device_ids[0], version=1, actor=AssetActor(1, "directory", None, None)
            )
        await add("http://127.0.0.1:45692")
        async with ecs.factory.begin() as session:
            assert (await command(session, "first")).command_code == first.command_code
            row = await session.get(EcsCommand, first.command_code)
            assert (row.route, row.request_raw, row.delivery_snapshot, row.expires_at) == frozen
            assert row.status == "HELD" and row.needs_reconciliation
            assert (await routes.authorize_frozen(session, row.route)).value == "REVOKED"
            new = await command(session, "independent")
            current = await session.get(EcsCommand, new.command_code)
            assert new.command_code != first.command_code
            assert current.route["origin"] == "http://127.0.0.1:45692"
    finally:
        async with ecs.factory.begin() as session:
            await session.execute(delete(Device).where(Device.id.in_(device_ids)))
            await session.execute(delete(DeviceRoute).where(DeviceRoute.id.in_(route_ids)))
            await session.execute(delete(WorkLine).where(WorkLine.id.in_(line_ids)))


async def test_late_original_result_survives_configuration_deletion(ecs):
    import json

    from sqlalchemy import delete, select

    from app.integrations.ecs_command.lifecycle import EcsLifecycle
    from app.integrations.ecs_command.protocol import PROTOCOL
    from app.integrations.ecs_command.result import receive_ecs_result
    from app.models import EcsResultEvidence, EcsSendEvidence
    from app.reliable.store import MessageStore

    code = "late-directory-" + uuid7().hex

    ecs.service.routes = DeviceRoutes(
        [CommandBinding("SCAN1", "MOVE", "test", "1", TrialPayload)], annexes=ecs.service.annexes
    )
    try:
        async with ecs.factory.begin() as session:
            line, device, route = await add_bound_device(session, code, "http://127.0.0.1:45721")
            device_id, line_id = device.id, line.id
            command = await ecs.service.create(
                session,
                owner=ecs.owner,
                call_key="late-result",
                device_code=code,
                task_type="MOVE",
                timeout_ms=30000,
                params={"qty": 1},
            )
        store = MessageStore(ecs.factory, lifecycles={PROTOCOL: EcsLifecycle(ecs.service)})
        claim = (await store.claim(1, ecs.targets))[0]
        assert await store.before_send(claim, ecs.targets["ecs"])
        async with ecs.factory.begin() as session:
            line = await session.get(WorkLine, line_id)
            line.device_bindings = {}
            line.version += 1
        async with ecs.factory() as session:
            await delete_device(
                session, device_id, version=1, actor=AssetActor(1, "late-result", None, None)
            )
        raw = json.dumps(
            dict(
                command_code=command.command_code,
                device_code=code,
                result="SUCCESS",
                finish_time=1,
                data={"qty": 1},
            )
        ).encode()
        async with ecs.factory.begin() as session:
            assert (await receive_ecs_result(session, raw, ecs.service)).status_code == 200
        async with ecs.factory() as session:
            saved = await session.get(EcsCommand, command.command_code)
            assert saved.status == "SUCCEEDED" and saved.route["origin"] == route.ecs_origin
            assert await session.scalar(
                select(EcsResultEvidence.id).where(
                    EcsResultEvidence.command_code == command.command_code
                )
            )
            assert await session.scalar(
                select(EcsSendEvidence.id).where(
                    EcsSendEvidence.command_code == command.command_code
                )
            )
            assert await session.get(Device, device_id) is None
            assert await session.get(DeviceRoute, route.id) is not None
    finally:
        async with ecs.factory.begin() as session:
            await session.execute(delete(Device).where(Device.device_code == code))
            await session.execute(delete(DeviceRoute).where(DeviceRoute.id == route.id))
            await session.execute(delete(WorkLine).where(WorkLine.id == line_id))
