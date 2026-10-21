import json

import pytest
from sqlalchemy import select

from app.core.identifiers import uuid7
from app.integrations.wms_transport import (
    IdempotencyConflict,
    InvalidTransportRequest,
    Rack,
    RackPosition,
    RackRole,
    RackScenarios,
    RackState,
    RackStateUnavailable,
    TransportService,
    Zone,
)
from app.models import Menu, ReliableMessage, TransportTask
from app.reliable import InvalidMessage

from .helpers import counts

SCENARIOS = ["enter_rack", "rotate_rack", "depart_rack"]


class CurrentRackState:
    async def rack_state(self, rack_id):
        assert rack_id == "rack"
        return RackState("actual-position", "old-face")


@pytest.fixture
def scenarios(transport_storage):
    rig = transport_storage
    return RackScenarios(
        TransportService(
            rig.outbox,
            target=rig.target.name,
            clock=rig.clock,
            state_reader=CurrentRackState(),
        )
    )


@pytest.fixture
def scenario_request(transport_request):
    return {key: value for key, value in transport_request.items() if key != "source"} | {
        "role": RackRole.FIVE_LAYER,
        "target_face": " 侧A🧭 ",
    }


async def register(scenarios, action, session, request):
    if action == "rotate_rack":
        request = {key: value for key, value in request.items() if key != "target"}
    return await getattr(scenarios, action)(session, **request)


@pytest.mark.parametrize(
    "role,templates",
    [
        (RackRole.TRANSFER, ["F01", "CK04", "F01"]),
        (RackRole.RETURN, ["F01", "CK04", "F01"]),
        (RackRole.FIVE_LAYER, ["CTU01", "CTU02", "CTU03"]),
    ],
)
@pytest.mark.parametrize("target", [RackPosition("destination"), Zone("destination")])
async def test_role_selects_paired_templates_and_preserves_locations_and_face(
    transport_storage, scenarios, scenario_request, role, templates, target
):
    rig = transport_storage
    async with rig.factory() as session:
        for action, template in zip(SCENARIOS, templates, strict=True):
            handle = await register(
                scenarios,
                action,
                session,
                scenario_request
                | {"role": role, "target": target, "client_request_id": str(uuid7())},
            )
            task = await session.scalar(
                select(TransportTask).where(
                    TransportTask.transport_task_id == handle.transport_task_id
                )
            )
            message = await session.scalar(
                select(ReliableMessage).where(ReliableMessage.operation_id == task.operation_id)
            )
            data = json.loads(message.request_raw)["data"]
            assert data["rcs_template_id"] == template
            assert data["source"] == {"kind": "RACK", "location_code": "rack"}
            assert data["target_face"] == " 侧A🧭 "
            assert data["target"] == {
                "kind": "ZONE"
                if isinstance(target, Zone) and action != "rotate_rack"
                else "RACK_POSITION",
                "location_code": "actual-position" if action == "rotate_rack" else "destination",
            }
        await session.commit()
        assert await counts(session, rig) == (3, 3)


@pytest.mark.parametrize("action", ["enter_rack", "depart_rack"])
async def test_move_allows_omitting_target_face(
    transport_storage, scenarios, scenario_request, action
):
    request = {key: value for key, value in scenario_request.items() if key != "target_face"}
    async with transport_storage.factory() as session:
        await register(scenarios, action, session, request)
        message = await session.scalar(
            select(ReliableMessage).where(ReliableMessage.target == transport_storage.target.name)
        )
        assert "target_face" not in json.loads(message.request_raw)["data"]


@pytest.mark.parametrize("action", SCENARIOS)
@pytest.mark.parametrize("role", ["unknown", None, []])
async def test_unknown_role_leaves_no_task_or_message(
    transport_storage, scenarios, scenario_request, action, role
):
    async with transport_storage.factory() as session:
        with pytest.raises(InvalidTransportRequest):
            await register(scenarios, action, session, scenario_request | {"role": role})
        await session.commit()
        assert await counts(session, transport_storage) == (0, 0)


@pytest.mark.parametrize("action", ["enter_rack", "depart_rack"])
@pytest.mark.parametrize("target", [Rack("rack"), "destination", None])
async def test_move_rejects_non_destination_types_without_writes(
    transport_storage, scenarios, scenario_request, action, target
):
    async with transport_storage.factory() as session:
        with pytest.raises(InvalidTransportRequest):
            await register(scenarios, action, session, scenario_request | {"target": target})
        await session.commit()
        assert await counts(session, transport_storage) == (0, 0)


@pytest.mark.parametrize("action", SCENARIOS)
@pytest.mark.parametrize("commit", [True, False], ids=["commit", "rollback"])
async def test_scenario_obligations_share_the_callers_transaction(
    transport_storage, scenarios, scenario_request, action, commit
):
    rig = transport_storage
    async with rig.factory() as session:
        session.add(Menu(name=rig.target.name, path="/caller", title="Caller", component="Page"))
        await register(scenarios, action, session, scenario_request)
        if commit:
            await session.commit()
        else:
            await session.rollback()
    async with rig.factory() as session:
        assert await counts(session, rig) == ((1, 1) if commit else (0, 0))
        business_id = await session.scalar(select(Menu.id).where(Menu.name == rig.target.name))
        assert (business_id is not None) == commit


@pytest.mark.parametrize("action", SCENARIOS)
async def test_failed_enqueue_preserves_only_the_callers_business_write(
    transport_storage, scenarios, scenario_request, action
):
    rig = transport_storage
    async with rig.factory() as session:
        session.add(Menu(name=rig.target.name, path="/caller", title="Caller", component="Page"))
        rig.outbox.targets = {}
        with pytest.raises(InvalidMessage):
            await register(scenarios, action, session, scenario_request)
        await session.commit()
    async with rig.factory() as session:
        assert await counts(session, rig) == (0, 0)
        assert await session.scalar(select(Menu.id).where(Menu.name == rig.target.name)) is not None


@pytest.mark.parametrize("action", SCENARIOS)
async def test_replay_returns_original_without_new_identity_or_state_read(
    transport_storage, scenarios, scenario_request, action
):
    rig = transport_storage
    async with rig.factory() as session:
        original = await register(scenarios, action, session, scenario_request)
        await session.commit()

    def unexpected():
        raise AssertionError("replay must not generate identities or timestamps")

    replay = RackScenarios(
        TransportService(rig.outbox, target=rig.target.name, clock=unexpected, uuid7=unexpected)
    )
    async with rig.factory() as session:
        assert await register(replay, action, session, scenario_request) == original
        await session.commit()
        assert await counts(session, rig) == (1, 1)


@pytest.mark.parametrize("action", SCENARIOS)
@pytest.mark.parametrize("changes", [{"target_face": "other-face"}, {"role": RackRole.RETURN}])
async def test_changed_capability_input_conflicts_without_a_second_obligation(
    transport_storage, scenarios, scenario_request, action, changes
):
    rig = transport_storage
    async with rig.factory() as session:
        original = await register(scenarios, action, session, scenario_request)
        with pytest.raises(IdempotencyConflict):
            await register(scenarios, action, session, scenario_request | changes)
        await session.commit()
        assert await register(scenarios, action, session, scenario_request) == original
        assert await counts(session, rig) == (1, 1)


async def test_rotate_propagates_missing_trusted_state_without_writes(
    transport_storage, scenario_request
):
    rig = transport_storage
    async with rig.factory() as session:
        with pytest.raises(RackStateUnavailable):
            await register(RackScenarios(rig.service), "rotate_rack", session, scenario_request)
        await session.commit()
        assert await counts(session, rig) == (0, 0)


@pytest.mark.parametrize("action", SCENARIOS)
@pytest.mark.parametrize("face", ["", 90, "face\x00", "x" * 11])
async def test_invalid_face_propagates_the_capability_error_without_writes(
    transport_storage, scenarios, scenario_request, action, face
):
    async with transport_storage.factory() as session:
        with pytest.raises(InvalidTransportRequest):
            await register(scenarios, action, session, scenario_request | {"target_face": face})
        await session.commit()
        assert await counts(session, transport_storage) == (0, 0)


async def test_rotate_rejects_the_current_face_without_writes(
    transport_storage, scenarios, scenario_request
):
    async with transport_storage.factory() as session:
        with pytest.raises(InvalidTransportRequest):
            await register(
                scenarios, "rotate_rack", session, scenario_request | {"target_face": "old-face"}
            )
        await session.commit()
        assert await counts(session, transport_storage) == (0, 0)
