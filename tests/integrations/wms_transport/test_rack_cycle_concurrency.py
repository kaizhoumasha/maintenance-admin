"""多个不同角色的货架并发走各自的生命周期:互不串扰(真实 PostgreSQL)。"""

import asyncio
import json

from sqlalchemy import select

from app.core.identifiers import uuid7
from app.integrations.wms_transport import (
    RackPosition,
    RackRole,
    RackScenarios,
    TransportCaller,
    TransportService,
    Zone,
)
from app.models import ReliableMessage, TransportTask

TEMPLATES = {
    RackRole.TRANSFER: ("F01", "CK04", "F01"),
    RackRole.RETURN: ("F01", "CK04", "F01"),
    RackRole.FIVE_LAYER: ("CTU01", "CTU02", "CTU03"),
}


def scenarios_for(rig):
    return RackScenarios(TransportService(rig.outbox, target=rig.target.name, clock=rig.clock))


async def run_cycle(rig, scenarios, role, rack_id):
    cycle = scenarios.cycle(role=role, rack_id=rack_id, caller=TransportCaller(rig.target.name))
    async with rig.factory() as session:
        await cycle.enter(session, client_request_id=str(uuid7()), target=RackPosition("work"))
        await cycle.rotate(
            session,
            client_request_id=str(uuid7()),
            position=RackPosition("work"),
            target_face="new-face",
        )
        await cycle.depart(session, client_request_id=str(uuid7()), target=Zone("storage"))
        await session.commit()


async def templates_of(rig, rack_id):
    async with rig.factory() as session:
        rows = await session.execute(
            select(TransportTask.kind, ReliableMessage.request_raw, TransportTask.created_at)
            .join(ReliableMessage, ReliableMessage.operation_id == TransportTask.operation_id)
            .where(TransportTask.workline_id == rig.target.name)
        )
        found = []
        for _kind, raw, _created in rows:
            data = json.loads(raw)["data"]
            if data["rack_id"] == rack_id:
                found.append(data["rcs_template_id"])
        return sorted(found)


async def test_many_racks_with_different_roles_keep_their_own_paired_templates(pg_transport_rig):
    rig = pg_transport_rig
    scenarios = scenarios_for(rig)
    racks = {
        f"rack-{number}": role
        for number, role in enumerate([*TEMPLATES, *TEMPLATES, *TEMPLATES, *TEMPLATES])
    }
    await asyncio.gather(*(run_cycle(rig, scenarios, role, rack) for rack, role in racks.items()))
    for rack_id, role in racks.items():
        assert await templates_of(rig, rack_id) == sorted(TEMPLATES[role]), rack_id


async def test_one_handle_can_be_shared_by_concurrent_callers(pg_transport_rig):
    rig = pg_transport_rig
    cycle = scenarios_for(rig).cycle(
        role=RackRole.FIVE_LAYER, rack_id="shared", caller=TransportCaller(rig.target.name)
    )

    async def enter():
        async with rig.factory() as session:
            await cycle.enter(session, client_request_id=str(uuid7()), target=Zone("work"))
            await session.commit()

    await asyncio.gather(*(enter() for _ in range(8)))
    assert await templates_of(rig, "shared") == ["CTU01"] * 8
