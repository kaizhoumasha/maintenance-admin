"""已发送过的料箱记录(投料到 CNV0401、回架自 CNV0402)在当前实现下的冻结合同。

记录里的 workline_id 不进入线上请求,这里用夹具的唯一值代替,避免污染共享库;
client_request_id、料箱、货架、面、储位、交接位与历史记录一致。
时钟与 UUID 确定,所以整份字节可以逐字比较。
"""

import pytest
from sqlalchemy import select

from app.integrations.wms_transport import (
    BinMove,
    BinScenarios,
    HandoffPosition,
    InvalidTransportRequest,
    RackBinSlot,
    TransportService,
)
from app.models import TransportTask

from .helpers import caller, counts, deterministic_uuid7, frozen_requests, golden_head

FEED_ID = "01a1602b-7420-7a2d-9b0f-783d4cf9e420"
RETURN_ID = "01a16041-89e5-7146-8015-94146f66e7f8"
SLOT = RackBinSlot("510046", "270", "510046B3F2C101")
FEED = [BinMove("A000002186", SLOT, HandoffPosition("CNV0401"))]
RETURN = [BinMove("A000002186", HandoffPosition("CNV0402"), SLOT)]

MOVE_START = b'"kind":"BIN_MOVE","moves":[{"container_id":"A000002186",'
SLOT_JSON = (
    b'{"kind":"RACK_BIN_SLOT","rack_id":"510046","rack_face":"270","slot_id":"510046B3F2C101"}'
)
GOLDEN_FEED = (
    golden_head(b"000000000001", b"000000000002")
    + MOVE_START
    + b'"source":'
    + SLOT_JSON
    + b',"target":{"kind":"HANDOFF_POSITION","location_code":"CNV0401"}}]}}'
)
GOLDEN_RETURN = (
    golden_head(b"000000000003", b"000000000004")
    + MOVE_START
    + b'"source":{"kind":"HANDOFF_POSITION","location_code":"CNV0402"},"target":'
    + SLOT_JSON
    + b"}]}}"
)


def service_for(rig):
    return TransportService(
        rig.outbox, target=rig.target.name, clock=rig.clock, uuid7=deterministic_uuid7(rig)
    )


async def register_through_scenarios(rig, session):
    scenarios = BinScenarios(service_for(rig))
    return [
        await scenarios.feed_bins(
            session, client_request_id=FEED_ID, caller=caller(rig), moves=FEED
        ),
        await scenarios.return_bins(
            session, client_request_id=RETURN_ID, caller=caller(rig), moves=RETURN
        ),
    ]


async def test_the_historical_feed_and_return_freeze_the_exact_contract_bytes(transport_storage):
    rig = transport_storage
    async with rig.factory() as session:
        await register_through_scenarios(rig, session)
        await session.commit()
        assert await frozen_requests(session, rig) == [GOLDEN_FEED, GOLDEN_RETURN]


async def test_the_capability_layer_freezes_the_same_bytes_as_the_scenarios(transport_storage):
    rig = transport_storage
    service = service_for(rig)
    async with rig.factory() as session:
        await service.move_bins(session, client_request_id=FEED_ID, caller=caller(rig), moves=FEED)
        await service.move_bins(
            session, client_request_id=RETURN_ID, caller=caller(rig), moves=RETURN
        )
        assert await frozen_requests(session, rig) == [GOLDEN_FEED, GOLDEN_RETURN]


async def test_replaying_the_historical_records_returns_the_same_tasks(transport_storage):
    rig = transport_storage
    async with rig.factory() as session:
        first = await register_through_scenarios(rig, session)
        await session.commit()
        again = await register_through_scenarios(rig, session)
        assert again == first
        assert await counts(session, rig) == (2, 2)
        assert await frozen_requests(session, rig) == [GOLDEN_FEED, GOLDEN_RETURN]
        task = await session.scalar(
            select(TransportTask).where(TransportTask.client_request_id == FEED_ID)
        )
        assert task.kind == "BIN_MOVE" and task.station_id is None


@pytest.mark.parametrize("scenario,moves", [("feed_bins", RETURN), ("return_bins", FEED)])
async def test_each_historical_record_belongs_to_exactly_one_direction(
    transport_storage, scenario, moves
):
    rig = transport_storage
    async with rig.factory() as session:
        with pytest.raises(InvalidTransportRequest):
            await getattr(BinScenarios(service_for(rig)), scenario)(
                session, client_request_id=FEED_ID, caller=caller(rig), moves=moves
            )
        assert await counts(session, rig) == (0, 0)
