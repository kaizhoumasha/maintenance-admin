import json
from copy import deepcopy

import pytest
from sqlalchemy import delete, select, update

from app.core.identifiers import uuid7
from app.integrations.wms_callback import receive
from app.models import CallbackReceipt, TransportTask
from tests.helpers import result_task, transport_callback


async def read(factory, task):
    async with factory() as session:
        return await task.service.get_result(
            session,
            transport_task_id=task.handle.transport_task_id,
            client_request_id=task.handle.client_request_id,
            caller=task.caller,
        )


async def send(
    factory, task, *, revision=1, notices=None, commit=True, operation_id=None, **changes
):
    data = dict(
        transport_task_id=task.handle.transport_task_id,
        kind="RACK_MOVE",
        rack_id="R1",
        outcome_revision=revision,
        status="SUCCEEDED",
        final_position=dict(kind="RACK_POSITION", location_code="OTHER"),
        arrival_face="90",
    )
    data.update(changes)
    raw = json.dumps(
        dict(
            operation="transport.task.resulted@v1",
            operation_id=operation_id or str(uuid7()),
            timestamp=1,
            data=data,
        )
    ).encode()
    async with factory() as session:
        reply = await receive(
            session, raw, on_owner_committed=notices.append if notices is not None else None
        )
        assert reply.status_code in {200, 202}, reply
        if notices is not None:
            assert notices == []
        await (session.commit() if commit else session.rollback())
    return data


@pytest.mark.parametrize("mismatch", ["target", "face"])
async def test_known_rack_mismatch_exposes_original_position_without_applying(
    transport_rig, mismatch
):
    factory = transport_rig.factory
    task = await result_task(factory)
    changes = (
        {}
        if mismatch == "target"
        else dict(final_position=dict(kind="RACK_POSITION", location_code="TO"), arrival_face="180")
    )
    await send(factory, task, **changes)
    result = await read(factory, task)
    assert result.position_observation["outcome_revision"] == 1
    assert result.position_observation["arrival_face"] == ("90" if mismatch == "target" else "180")
    assert result.outcome is None and result.outcome_revision is None and result.closure == "OPEN"
    result.position_observation["rack_id"] = "mutated-copy"
    assert (await read(factory, task)).position_observation["rack_id"] == "R1"


@pytest.mark.parametrize("case", ["wrong_rack", "wrong_kind", "unknown", "terminal"])
async def test_identity_unknown_or_terminal_conflict_is_not_observation(transport_rig, case):
    factory = transport_rig.factory
    task = await result_task(factory)
    changes = {}
    if case == "wrong_rack":
        changes["rack_id"] = "other"
    elif case == "wrong_kind":
        changes["kind"] = "RACK_ROTATE"
    elif case == "unknown":
        changes.update(
            status="FAILED",
            final_position=None,
            arrival_face=None,
            position_unknown=True,
            failure_code="POSITION_UNKNOWN",
        )
    else:
        await transport_callback(factory, task)
    await send(factory, task, revision=2, **changes)
    assert (await read(factory, task)).position_observation is None


async def test_normal_and_bin_pending_do_not_create_observation(transport_rig):
    factory = transport_rig.factory
    normal = await result_task(factory)
    await transport_callback(factory, normal)
    assert (await read(factory, normal)).position_observation is None
    bins = await result_task(factory, bins=True)
    # Successful rack-slot results have no placement milestone yet: original pending rule stays.
    data = dict(
        transport_task_id=bins.handle.transport_task_id,
        kind="BIN_MOVE",
        outcome_revision=1,
        results=[
            dict(
                container_id="C1",
                status="SUCCEEDED",
                final_position=dict(
                    kind="RACK_BIN_SLOT", rack_id="R1", rack_face="90", slot_id="S1"
                ),
            ),
            dict(
                container_id="C2",
                status="SUCCEEDED",
                final_position=dict(
                    kind="RACK_BIN_SLOT", rack_id="R2", rack_face="90", slot_id="S2"
                ),
            ),
        ],
    )
    raw = json.dumps(
        dict(
            operation="transport.task.resulted@v1",
            operation_id=str(uuid7()),
            timestamp=1,
            data=data,
        )
    ).encode()
    async with factory.begin() as session:
        assert (await receive(session, raw)).status_code == 202
    result = await read(factory, bins)
    assert (
        result.has_pending_outcome
        and result.position_observation is None
        and result.outcome is None
    )


@pytest.mark.parametrize("location", ["OLD", "TO"])
async def test_lower_original_revision_keeps_newer_observation(transport_rig, location):
    factory = transport_rig.factory
    task = await result_task(factory)
    await send(factory, task, revision=3)
    await send(
        factory, task, revision=2, final_position=dict(kind="RACK_POSITION", location_code=location)
    )
    result = await read(factory, task)
    assert result.position_observation["outcome_revision"] == 3
    assert result.position_observation["final_position"]["location_code"] == "OTHER"
    assert result.outcome_revision == (2 if location == "TO" else None)
    if location == "TO":
        assert result.outcome["status"] == "SUCCEEDED"
        await send(
            factory, task, revision=4, final_position=dict(kind="RACK_POSITION", location_code="TO")
        )
        retained = await read(factory, task)
        assert retained.outcome == result.outcome
        assert retained.position_observation == result.position_observation


async def test_new_rack_mismatch_observation_wakes_original_owner(transport_rig):
    factory = transport_rig.factory
    task = await result_task(factory)
    async with factory.begin() as session:
        await session.execute(
            update(TransportTask)
            .where(TransportTask.transport_task_id == task.handle.transport_task_id)
            .values(owner_domain="original")
        )
    notices = []
    await send(factory, task, notices=notices, commit=False)
    assert notices == [] and (await read(factory, task)).position_observation is None
    await send(factory, task, notices=notices)
    assert notices == ["original"]
    notices.clear()
    await send(factory, task, notices=notices)
    assert notices == []
    await send(factory, task, revision=2, notices=notices)
    assert notices == ["original"]


async def test_invalid_stored_observation_fails_closed(transport_rig):
    factory = transport_rig.factory
    task = await result_task(factory)
    await send(factory, task)
    async with factory.begin() as session:
        row = await session.scalar(
            select(TransportTask).where(
                TransportTask.transport_task_id == task.handle.transport_task_id
            )
        )
        raw = deepcopy(row.position_observation)
        raw["rack_id"] = "different"
        row.position_observation = raw
    result = await read(factory, task)
    assert result.availability == "INVALID" and result.position_observation is None


async def test_migrated_postgres_observation_reader_and_owner_notification(pg_transport_rig):
    from types import SimpleNamespace

    from app.integrations.wms_transport import RackPosition, TransportCaller

    rig = pg_transport_rig
    rig.service.owner_domain = "original"
    caller = TransportCaller(rig.target.name)
    async with rig.factory.begin() as session:
        handle = await rig.service.move_rack(
            session,
            client_request_id=str(uuid7()),
            caller=caller,
            rack_id="R1",
            source=RackPosition("FROM"),
            target=RackPosition("TO"),
            target_face="90",
        )
    task = SimpleNamespace(handle=handle, service=rig.service, caller=caller)
    operation_id = str(uuid7())
    try:
        notices = []
        await send(rig.factory, task, notices=notices, operation_id=operation_id)
        assert notices == ["original"]
        result = await read(rig.factory, task)
        assert result.availability == "FOUND" and result.outcome is None
        assert result.position_observation["final_position"]["location_code"] == "OTHER"
        result.position_observation["final_position"]["location_code"] = "MUTATED"
        assert (await read(rig.factory, task)).position_observation["final_position"][
            "location_code"
        ] == "OTHER"
    finally:
        async with rig.factory.begin() as session:
            await session.execute(
                delete(CallbackReceipt).where(CallbackReceipt.operation_id == operation_id)
            )


async def test_rotate_face_mismatch_exposes_its_original_position(transport_rig):
    from types import SimpleNamespace

    from app.integrations.wms_transport import RackPosition, TransportCaller

    rig = transport_rig
    caller = TransportCaller("rotate-line")
    async with rig.factory.begin() as session:
        handle = await rig.service.rotate_rack(
            session,
            client_request_id=str(uuid7()),
            caller=caller,
            rack_id="R1",
            position=RackPosition("TO"),
            target_face="90",
            rcs_template_id="CK04",
        )
    task = SimpleNamespace(handle=handle, service=rig.service, caller=caller)
    await send(
        rig.factory,
        task,
        kind="RACK_ROTATE",
        final_position=dict(kind="RACK_POSITION", location_code="TO"),
        arrival_face="180",
    )
    result = await read(rig.factory, task)
    assert result.outcome is None and result.position_observation["kind"] == "RACK_ROTATE"
    assert result.position_observation["arrival_face"] == "180"
