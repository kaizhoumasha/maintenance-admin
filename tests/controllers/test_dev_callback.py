"""联调控制台的回调投递:样例回调走真实接收管线,任务状态可见,连续投递可看到收敛。"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.core.identifiers import uuid7
from app.models.transport import TransportTask

DEV = "/api/v1/dev/transport"
RACK = {
    "kind": "RACK_MOVE",
    "rack_id": "RACK-1",
    "source": {"kind": "RACK_POSITION", "location_code": "P1"},
    "target": {"kind": "RACK_POSITION", "location_code": "CTU01"},
}
SLOT = {"kind": "RACK_BIN_SLOT", "rack_id": "R1", "rack_face": "90", "slot_id": "S1"}
BIN = {
    "kind": "BIN_MOVE",
    "moves": [
        {
            "container_id": "C1",
            "source": {"kind": "HANDOFF_POSITION", "location_code": "CNV1"},
            "target": SLOT,
        }
    ],
}


async def add_task(factory, frozen):
    task_id = "transport-" + str(uuid7())
    async with factory.begin() as session:
        session.add(
            TransportTask(
                client_request_id=str(uuid7()),
                transport_task_id=task_id,
                operation_id=str(uuid7()),
                kind=frozen["kind"],
                input_digest="a" * 64,
                workline_id="line",
                created_at=datetime.now(UTC),
                frozen=frozen,
            )
        )
    return task_id


async def deliver(client, admin, task_id, scenario):
    return await client.post(
        f"{DEV}/{task_id}/callbacks", json={"scenario": scenario}, headers=admin
    )


async def test_the_routes_require_login(client):
    assert (await client.get(f"{DEV}/x")).status_code == 401


@pytest.mark.parametrize("frozen", [RACK, BIN], ids=["rack", "bin"])
async def test_sample_success_is_applied_through_the_real_pipeline(
    client, admin, callback_db, frozen
):
    task_id = await add_task(callback_db, frozen)
    r = await deliver(client, admin, task_id, "succeeded")
    assert r.status_code == 200, r.text
    assert {item["status_code"] for item in r.json()} == {202}
    state = (await client.get(f"{DEV}/{task_id}", headers=admin)).json()
    assert state["outcome_status"] == "SUCCEEDED"
    assert state["applied_outcome_revision"] == 1


async def test_unknown_position_then_success_converges_with_a_higher_revision(
    client, admin, callback_db
):
    task_id = await add_task(callback_db, BIN)
    await deliver(client, admin, task_id, "position_unknown")
    state = (await client.get(f"{DEV}/{task_id}", headers=admin)).json()
    assert state["outcome_status"] == "UNKNOWN"
    await deliver(client, admin, task_id, "succeeded")
    state = (await client.get(f"{DEV}/{task_id}", headers=admin)).json()
    assert (state["outcome_status"], state["applied_outcome_revision"]) == ("SUCCEEDED", 2)


async def test_unknown_task_is_not_found_and_missing_frozen_is_rejected(client, admin, callback_db):
    assert (await deliver(client, admin, "transport-nope", "succeeded")).status_code == 404
    task_id = await add_task(callback_db, RACK)
    async with callback_db.begin() as session:
        task = await session.scalar(
            select(TransportTask).where(TransportTask.transport_task_id == task_id)
        )
        task.frozen = None
    assert (await deliver(client, admin, task_id, "succeeded")).status_code == 400
