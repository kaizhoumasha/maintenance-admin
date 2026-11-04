import pytest

from app.businesses import catalog
from app.businesses.smt_manual import composition


@pytest.fixture(autouse=True)
def manual_catalog(monkeypatch):
    monkeypatch.setattr(catalog, "entries", lambda: (composition,))


def configuration(**overrides):
    return {
        "line_code": "CAPACITY",
        "line_name": "Capacity",
        "business_code": "smt_manual",
        "position_bindings": {"FIVE_RACK": "P", "RETURN_RACK": "P", "TRANSFER_RACK": "T"},
        "positions": [{"position_code": "P", "capacity": 3}, {"position_code": "T"}],
        **overrides,
    }


async def test_workline_positions_roundtrip_omission_and_version(client, admin):
    response = await client.post("/api/v1/work-lines", headers=admin, json=configuration())
    assert response.status_code == 201, response.text
    line = response.json()
    assert {p["position_code"]: p["capacity"] for p in line["positions"]} == {"P": 3, "T": 1}
    updated = await client.put(
        f"/api/v1/work-lines/{line['id']}",
        headers=admin,
        json={"line_name": "Renamed", "version": 1},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["positions"] == line["positions"]
    changed = await client.put(
        f"/api/v1/work-lines/{line['id']}",
        headers=admin,
        json={
            "line_name": "Renamed",
            "version": 2,
            "positions": [{"position_code": "P", "capacity": None}, {"position_code": "T"}],
        },
    )
    assert changed.status_code == 200, changed.text
    assert (
        next(p for p in changed.json()["positions"] if p["position_code"] == "P")["capacity"]
        is None
    )
    stale = await client.put(
        f"/api/v1/work-lines/{line['id']}",
        headers=admin,
        json={"line_name": "Stale", "version": 1, "positions": []},
    )
    assert stale.status_code == 409
    empty = await client.put(
        f"/api/v1/work-lines/{line['id']}",
        headers=admin,
        json={"line_name": "Empty", "version": 3, "positions": []},
    )
    assert empty.status_code == 409


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "3", 2147483648])
async def test_workline_position_rejects_invalid_capacity(client, admin, value):
    response = await client.post(
        "/api/v1/work-lines",
        headers=admin,
        json=configuration(
            positions=[{"position_code": "P", "capacity": value}, {"position_code": "T"}]
        ),
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "INVALID_REQUEST"


async def test_workline_position_rejects_duplicate_and_handoff_codes(client, admin):
    duplicate = await client.post(
        "/api/v1/work-lines",
        headers=admin,
        json=configuration(positions=[{"position_code": "P"}, {"position_code": "P"}]),
    )
    assert duplicate.status_code == 400
    mixed = await client.post(
        "/api/v1/work-lines",
        headers=admin,
        json=configuration(
            position_bindings={"FIVE_RACK": "P", "INLET": "P"}, positions=[{"position_code": "P"}]
        ),
    )
    assert mixed.status_code == 400


async def test_workline_position_does_not_duplicate_a_physical_point_between_lines(client, admin):
    first = await client.post("/api/v1/work-lines", headers=admin, json=configuration())
    assert first.status_code == 201, first.text
    other = await client.post(
        "/api/v1/work-lines",
        headers=admin,
        json=configuration(line_code="OTHER"),
    )
    assert other.status_code == 409, other.text


async def test_pending_rack_responsibility_allows_only_capacity_edits(client, admin):
    from app.businesses._components.rack_dispatch.models import RackJob

    response = await client.post("/api/v1/work-lines", headers=admin, json=configuration())
    line = response.json()
    from app.core.database import get_session
    from app.main import app

    generator = app.dependency_overrides[get_session]()
    db_session = await anext(generator)
    db_session.add(
        RackJob(
            id="original",
            owner_domain="smt_manual",
            work_line_id=line["id"],
            source_kind="MANUAL_RETURN",
            source_ref="source",
            rack_id="R1",
            original_cycle_id="original",
            task_id="T1",
            point="P",
            initial_face="A",
        )
    )
    await db_session.commit()
    await generator.aclose()
    endpoint = f"/api/v1/work-lines/{line['id']}"
    changed = await client.put(
        endpoint,
        headers=admin,
        json={
            "line_name": "Capacity",
            "version": 1,
            "positions": [{"position_code": "P", "capacity": 1}, {"position_code": "T"}],
        },
    )
    assert changed.status_code == 200, changed.text
    rebound = await client.put(
        endpoint,
        headers=admin,
        json={
            "line_name": "Capacity",
            "version": 2,
            "position_bindings": {"FIVE_RACK": "NEW", "RETURN_RACK": "NEW", "TRANSFER_RACK": "T"},
            "positions": [{"position_code": "NEW"}, {"position_code": "T"}],
        },
    )
    assert rebound.status_code == 409
    removed = await client.delete(endpoint, headers=admin, params={"version": 2})
    assert removed.status_code == 409
    assert (await client.get(endpoint, headers=admin)).json()["version"] == 2
