"""货架 510046 的五层货架完整生命周期:进场 KT18 → 换面 → 离场库区 WH01(面 90/270)。

固化的是线上合同:模板配对、源/目标形态、面值原样、键序与整份冻结字节。
时钟与 UUID 均确定,所以字节可以逐字比较。
"""

import json

import pytest

from app.integrations.wms_transport import (
    RackPosition,
    RackRole,
    RackScenarios,
    TransportService,
    Zone,
)

from .helpers import caller, counts, deterministic_uuid7, frozen_requests, golden_head

RACK, POINT, ZONE = "510046", "KT18", "WH01"
STEPS = [f"01a13b00-cabd-7390-890a-0c093e7c07a{number}" for number in range(1, 5)]
GOLDEN = [
    golden_head(b"000000000001", b"000000000002")
    + b'"kind":"RACK_MOVE","rack_id":"510046","rcs_template_id":"CTU01",'
    b'"source":{"kind":"RACK","location_code":"510046"},'
    b'"target":{"kind":"RACK_POSITION","location_code":"KT18"},"target_face":"270"}}',
    golden_head(b"000000000003", b"000000000004")
    + b'"kind":"RACK_ROTATE","rack_id":"510046","rcs_template_id":"CTU02",'
    b'"source":{"kind":"RACK_POSITION","location_code":"KT18"},'
    b'"target":{"kind":"RACK_POSITION","location_code":"KT18"},"target_face":"90"}}',
    golden_head(b"000000000005", b"000000000006")
    + b'"kind":"RACK_ROTATE","rack_id":"510046","rcs_template_id":"CTU02",'
    b'"source":{"kind":"RACK_POSITION","location_code":"KT18"},'
    b'"target":{"kind":"RACK_POSITION","location_code":"KT18"},"target_face":"270"}}',
    golden_head(b"000000000007", b"000000000008")
    + b'"kind":"RACK_MOVE","rack_id":"510046","rcs_template_id":"CTU03",'
    b'"source":{"kind":"RACK","location_code":"510046"},'
    b'"target":{"kind":"ZONE","location_code":"WH01"}}}',
]


def lifecycle(rig):
    service = TransportService(
        rig.outbox, target=rig.target.name, clock=rig.clock, uuid7=deterministic_uuid7(rig)
    )
    return RackScenarios(service).cycle(role=RackRole.FIVE_LAYER, rack_id=RACK, caller=caller(rig))


async def run_lifecycle(cycle, session):
    return [
        await cycle.enter(
            session, client_request_id=STEPS[0], target=RackPosition(POINT), target_face="270"
        ),
        await cycle.rotate(
            session, client_request_id=STEPS[1], position=RackPosition(POINT), target_face="90"
        ),
        await cycle.rotate(
            session, client_request_id=STEPS[2], position=RackPosition(POINT), target_face="270"
        ),
        await cycle.depart(session, client_request_id=STEPS[3], target=Zone(ZONE)),
    ]


async def test_five_layer_lifecycle_freezes_the_exact_contract_bytes(transport_storage):
    rig = transport_storage
    async with rig.factory() as session:
        await run_lifecycle(lifecycle(rig), session)
        await session.commit()
        assert await frozen_requests(session, rig) == GOLDEN


async def test_replaying_the_whole_lifecycle_returns_the_same_tasks_and_bytes(transport_storage):
    rig = transport_storage
    async with rig.factory() as session:
        first = await run_lifecycle(lifecycle(rig), session)
        await session.commit()
        again = await run_lifecycle(lifecycle(rig), session)
        assert again == first
        assert await counts(session, rig) == (4, 4)
        assert await frozen_requests(session, rig) == GOLDEN


@pytest.mark.parametrize("face", ["90", "270"])
@pytest.mark.parametrize("step", ["enter", "rotate"])
async def test_faces_are_opaque_strings_that_pass_through_unchanged(transport_storage, face, step):
    rig = transport_storage
    cycle = lifecycle(rig)
    async with rig.factory() as session:
        if step == "enter":
            await cycle.enter(
                session, client_request_id=STEPS[0], target=RackPosition(POINT), target_face=face
            )
        else:
            await cycle.rotate(
                session, client_request_id=STEPS[0], position=RackPosition(POINT), target_face=face
            )
        (raw,) = await frozen_requests(session, rig)
    sent = json.loads(raw)["data"]["target_face"]
    assert sent == face and isinstance(sent, str)
    assert f'"target_face":"{face}"'.encode() in raw  # 不是数字,也没有被改写


async def test_the_zone_departure_carries_no_face(transport_storage):
    rig = transport_storage
    async with rig.factory() as session:
        await lifecycle(rig).depart(session, client_request_id=STEPS[0], target=Zone(ZONE))
        (raw,) = await frozen_requests(session, rig)
    assert b"target_face" not in raw
