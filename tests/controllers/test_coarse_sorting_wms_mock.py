import asyncio
import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.businesses.coarse_sorting.mock_storage import CoarseSortingMockRound
from app.controllers import dev
from app.core import get_settings
from app.core.database import get_session
from app.core.identifiers import uuid7
from app.integrations.wms_request.adapter import (
    COARSE_MOCK_ROUND_HEADER,
    MOCK_SIGNATURE_HEADER,
    coarse_mock_signature,
)
from app.models import WorkLine

PREFIX = "inbound.coarse_sorting."
ALLOCATION = {
    "result": "ALLOCATED",
    "workline_code": "RS01",
    "rack_id": "TEST-RACK",
    "bin_code": "TEST-BIN",
    "bin_location": "A",
    "cell_id": "TEST-CELL",
    "cell_index": 1,
    "bin_type": "3",
    "reel_totalthickness": 0,
}


@pytest.fixture
async def mock_sessions(request, asset_factory, postgres_tables):
    if getattr(request, "param", "sqlite") == "sqlite":
        yield asset_factory
        return
    pg_url = request.getfixturevalue("pg_url")
    metadata = MetaData()
    WorkLine.__table__.to_metadata(metadata)
    async with postgres_tables(pg_url, "coarse_mock", metadata) as engine:
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def mock_app(mock_sessions, monkeypatch):
    settings = get_settings().model_copy(
        update={
            "COARSE_SORTING_WMS_MOCK": {
                "round_id": str(uuid7()),
                "ecs_source_ips": ["127.0.0.1"],
                "normal_barcode": "RAW-OK",
                "ng_barcode": "RAW-NG",
                "rack_position": "CLN01",
                "allocation": ALLOCATION,
            }
        }
    )
    monkeypatch.setattr(dev, "get_settings", lambda: settings)

    async with mock_sessions.begin() as session:
        session.add(
            WorkLine(
                line_code="RS01",
                line_name="粗分一线",
                business_code="coarse_sorting",
                position_bindings={"OUTPUT_RACK": "CLN01"},
            )
        )

    async def get_test_session():
        async with mock_sessions() as session:
            yield session

    def build(**changes):
        if "empty_cells" in changes:
            for key in ("normal_barcode", "ng_barcode", "allocation"):
                settings.COARSE_SORTING_WMS_MOCK.pop(key, None)
        settings.COARSE_SORTING_WMS_MOCK.update(changes)
        app = FastAPI()
        app.dependency_overrides[get_session] = get_test_session
        dev.include_dev_routers(app, environment="local", command_bindings=())
        return app

    engine = mock_sessions.kw["bind"]
    async with engine.begin() as connection:
        await connection.run_sync(CoarseSortingMockRound.__table__.create)
    try:
        yield build
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(CoarseSortingMockRound.__table__.drop)


async def send(
    app, operation, data, *, identity=None, signed=True, round_id=None, header_round=None
):
    endpoint = "facts" if operation == "execution_report" else "decisions"
    raw = json.dumps(
        {
            "operation_id": identity or str(uuid7()),
            "operation": PREFIX + operation + "@v1",
            "timestamp": 1000,
            "data": {"workline_code": "RS01", **data},
        }
    ).encode()
    round_id = round_id or app.state.coarse_sorting_mock_round.round_id
    headers = {
        COARSE_MOCK_ROUND_HEADER: header_round or round_id,
        **(
            {MOCK_SIGNATURE_HEADER: coarse_mock_signature(raw, round_id).decode()} if signed else {}
        ),
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(
            f"/api/v1/dev/coarse-sorting/mock-wms/{endpoint}", content=raw, headers=headers
        )


def scan(barcode):
    return {"barcode": barcode, "reel_diameter": 7, "reel_thickness": 15.3}


async def test_mock_requires_a_signed_request(mock_app):
    response = await send(mock_app(), "validate_bind", scan("RAW-OK"), signed=False)
    assert response.status_code == 401


async def test_mock_round_header_is_covered_by_the_signature(mock_app):
    response = await send(mock_app(), "validate_bind", scan("RAW-OK"), header_round="altered")
    assert response.status_code == 401


async def test_mock_admits_only_the_configured_workline_and_test_barcodes(mock_app):
    app = mock_app()
    for data in (scan("UNKNOWN"), {**scan("RAW-OK"), "workline_code": "RS02"}):
        response = await send(app, "validate_bind", data)
        assert response.status_code == 422
    ng = await send(app, "validate_bind", scan("RAW-NG"))
    assert ng.json()["data"] == {"result": "NG", "reason_code": "BARCODE_INVALID"}
    assert (await send(app, "validate_bind", scan("RAW-NG"))).status_code == 409


async def test_mock_preserves_the_original_slot_and_report_across_restart(mock_app):
    app = mock_app()
    valid_id, allocation_id, report_id = (str(uuid7()) for _ in range(3))
    validation = await send(app, "validate_bind", scan("RAW-OK"), identity=valid_id)
    assert validation.status_code == 200
    binding = {"binding_id": validation.json()["data"]["binding_id"]}
    allocated = await send(app, "storage_allocate", binding, identity=allocation_id)
    assert allocated.json()["data"] == ALLOCATION
    occupied = await send(app, "storage_allocate", binding)
    assert occupied.json()["data"] == {"result": "WAIT", "workline_code": "RS01"}
    report = (
        binding
        | {key: ALLOCATION[key] for key in ("bin_code", "bin_location", "cell_id", "cell_index")}
        | {
            "completed_at": 2000,
        }
    )
    assert (await send(app, "execution_report", report | {"cell_index": 2})).status_code == 409
    assert (await send(app, "execution_report", report, identity=report_id)).json()[
        "code"
    ] == "RECORDED"

    restarted = mock_app()
    replayed = await send(restarted, "validate_bind", scan("RAW-OK"), identity=valid_id)
    assert replayed.json() == validation.json()
    replayed = await send(restarted, "storage_allocate", binding, identity=allocation_id)
    assert replayed.json() == allocated.json()
    assert (await send(restarted, "validate_bind", scan("RAW-OK"))).status_code == 409
    assert (await send(restarted, "execution_report", report, identity=report_id)).json()[
        "code"
    ] == "DUPLICATE"
    assert (
        await send(
            restarted, "execution_report", report | {"completed_at": 3000}, identity=report_id
        )
    ).status_code == 409


async def test_concurrent_scans_can_authorize_only_one_normal_test_reel(mock_app):
    app = mock_app()
    results = await asyncio.gather(
        send(app, "validate_bind", scan("RAW-OK")),
        send(app, "validate_bind", scan("RAW-OK")),
    )
    assert sorted(response.status_code for response in results) == [200, 409]


async def test_previous_round_replays_without_claiming_the_new_round(mock_app):
    first = mock_app(round_id="review-a")
    validation_id, allocation_id = str(uuid7()), str(uuid7())
    validation = await send(first, "validate_bind", scan("RAW-OK"), identity=validation_id)
    binding = {"binding_id": validation.json()["data"]["binding_id"]}
    original = await send(first, "storage_allocate", binding, identity=allocation_id)
    next_round = mock_app(
        round_id="review-b",
        allocation=ALLOCATION | {"cell_id": "TEST-CELL-2", "cell_index": 2},
    )
    replay = await send(
        next_round, "validate_bind", scan("RAW-OK"), identity=validation_id, round_id="review-a"
    )
    assert replay.json() == validation.json()
    replay = await send(
        next_round, "storage_allocate", binding, identity=allocation_id, round_id="review-a"
    )
    assert replay.json() == original.json()
    assert (
        await send(next_round, "validate_bind", scan("RAW-OK"), round_id="review-a")
    ).status_code == 409
    assert (await send(next_round, "validate_bind", scan("RAW-OK"))).status_code == 200


async def test_additional_barcodes_share_the_rounds_one_normal_slot(mock_app):
    app = mock_app(additional_normal_barcodes=["RAW-ALT-1", "RAW-ALT-2"])
    accepted = await send(app, "validate_bind", scan("RAW-ALT-2"))
    assert accepted.status_code == 200
    assert accepted.json()["data"]["result"] == "OK"
    for barcode in ("RAW-OK", "RAW-ALT-1"):
        assert (await send(app, "validate_bind", scan(barcode))).status_code == 409
    ng = await send(app, "validate_bind", scan("RAW-NG"))
    assert ng.json()["data"]["result"] == "NG"


def reel(identity):
    return scan(f"PANY-PART-{identity},Q2000,MMAKER,D2610,LLOT,SREEL-{identity}")


@pytest.mark.parametrize("mock_sessions", ["sqlite", "postgres"], indirect=True)
async def test_random_mock_reserves_distinct_cells_and_replays_original_decisions(
    mock_app, monkeypatch
):
    from app.businesses.coarse_sorting import mock_wms

    draws = iter((0, 1, 9, 1))
    bounds = []

    def draw(upper):
        bounds.append(upper)
        return next(draws)

    monkeypatch.setattr(mock_wms, "randbelow", draw, raising=False)
    first_cell = ALLOCATION
    second_cell = ALLOCATION | {"cell_id": "TEST-CELL-2", "cell_index": 2}
    app = mock_app(
        empty_cells=[first_cell, second_cell],
    )
    assert (await send(app, "validate_bind", scan("manufacturer-only"))).status_code == 422

    ng_id = str(uuid7())
    ng = await send(app, "validate_bind", reel("ng"), identity=ng_id)
    assert ng.json()["data"] == {"result": "NG", "reason_code": "MOCK_RANDOM_NG"}
    again = await send(app, "validate_bind", reel("ng"), identity=ng_id)
    assert again.json() == ng.json()

    ids = [str(uuid7()), str(uuid7())]
    outcomes = await asyncio.gather(
        *(send(app, "validate_bind", reel(identity), identity=identity) for identity in ids)
    )
    assert all(response.status_code == 200 for response in outcomes)
    assert all(response.json()["data"]["result"] == "OK" for response in outcomes)

    restarted = mock_app()
    cells = []
    for identity, response in zip(ids, outcomes, strict=True):
        replay = await send(restarted, "validate_bind", reel(identity), identity=identity)
        assert replay.json() == response.json()
        binding = {"binding_id": response.json()["data"]["binding_id"]}
        allocation_id = str(uuid7())
        allocated = await send(restarted, "storage_allocate", binding, identity=allocation_id)
        cell = allocated.json()["data"]
        cells.append(cell)
        same = await send(restarted, "storage_allocate", binding, identity=allocation_id)
        assert same.json() == allocated.json()
        report = (
            binding
            | {key: cell[key] for key in ("bin_code", "bin_location", "cell_id", "cell_index")}
            | {"completed_at": 2000}
        )
        assert (await send(restarted, "execution_report", report)).json()["code"] == "RECORDED"
    assert sorted(cells, key=lambda cell: cell["cell_index"]) == [first_cell, second_cell]

    full_id = str(uuid7())
    full = await send(restarted, "validate_bind", reel("full"), identity=full_id)
    assert full.status_code == 409
    same = await send(restarted, "validate_bind", reel("full"), identity=full_id)
    assert same.status_code == 409
    assert same.json() == full.json()
    round_id = restarted.state.coarse_sorting_mock_round.round_id
    rotated = mock_app(round_id="next-round")
    old = await send(rotated, "validate_bind", reel("full"), identity=full_id, round_id=round_id)
    assert old.status_code == 409
    assert old.json() == full.json()
    assert bounds == [10, 10, 10, 10]


def test_random_mock_rejects_duplicate_physical_cells(mock_app):
    duplicate = ALLOCATION | {"cell_id": "DIFFERENT-CODE-FOR-SAME-CELL"}
    with pytest.raises(ValueError, match="distinct empty cells"):
        mock_app(empty_cells=[ALLOCATION, duplicate])
