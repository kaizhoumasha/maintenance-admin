import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.businesses.xray.mock_storage import MockBase
from app.businesses.xray.mock_wms import XrayWmsMock
from app.controllers.dev_mock_wms import router
from app.core.database import get_session
from app.core.identifiers import uuid7
from app.integrations.wms_request.adapter import MOCK_SIGNATURE_HEADER, mock_signature

BARCODES = (
    "PTEST-PART-01,Q3000,MTEST-MPN-01,D260101,LTEST-LOT-01,STEST-SERIAL-01",
    "PTEST-PART-02,Q50000,MTEST-MPN-02,D260102,LTEST-LOT-02,STEST-SERIAL-02",
)


@pytest.fixture
async def mock_peer():
    def locator(column):
        return {
            "result": "ACCEPT",
            "target_locator": {
                "type": "RACK_SLOT",
                "location_code": "POINT",
                "rack_id": "RACK",
                "rack_face": "A",
                "slot_id": f"S{column}",
                "rack_layer": 1,
                "rack_column": column,
            },
            "reel_diameter": 7,
            "reel_thickness": 8,
        }

    class RackDispatch:
        ready = ()
        pending = ()

        async def get_point_preparations(self, session, **kwargs):
            return self.ready

        async def jobs(self, session, **kwargs):
            return self.pending

        async def has_position_conflict(self, session, **kwargs):
            return False

    class Session:
        async def scalar(self, statement):
            return SimpleNamespace(id=1, position_bindings={"RETURN_RACK": "POINT"})

    dispatch = RackDispatch()
    app = FastAPI()
    app.state.xray_wms_mock = XrayWmsMock(
        {"XRAY": {BARCODES[0]: locator(1), BARCODES[1]: locator(2)}},
        {
            "XRAY": {
                "location_code": "POINT",
                "location_id": "ECS-RACK",
                "templates": {"ENTER": "F01", "ROTATE": "CTU03", "DEPART": "F01"},
                "slots": {
                    "A": {
                        "S1": {"rack_layer": 1, "rack_column": 1},
                        "S2": {"rack_layer": 1, "rack_column": 2},
                    },
                    "B": {"S1": {"rack_layer": 1, "rack_column": 1}},
                },
            }
        },
    )
    app.state.business_runtime = SimpleNamespace(
        services={"xray": SimpleNamespace(components={"rack_dispatch": dispatch})}
    )
    app.dependency_overrides[get_session] = Session
    app.include_router(router, prefix="/api/v1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:

        async def decide(stage, barcode, *, operation_id=None, signed=True, **data):
            raw = json.dumps(
                {
                    "operation_id": operation_id or str(uuid7()),
                    "operation": f"inbound.xray.{stage}_decide@v1",
                    "timestamp": 1000,
                    "data": {
                        "workline_code": "XRAY",
                        "barcode": barcode,
                        **data,
                    },
                }
            ).encode()
            headers = {MOCK_SIGNATURE_HEADER: mock_signature(raw).decode()} if signed else {}
            return await client.post("/api/v1/wes/decisions", content=raw, headers=headers)

        yield decide, dispatch


async def test_unsigned_decision_cannot_claim_an_operation(mock_peer):
    decide, _ = mock_peer
    operation_id = str(uuid7())
    rejected = await decide(
        "pkg", BARCODES[0], scanned_at=1, signed=False, operation_id=operation_id
    )
    assert rejected.status_code == 401
    accepted = await decide("pkg", BARCODES[1], scanned_at=1, operation_id=operation_id)
    assert accepted.json()["data"] == {"result": "COUNT_REQUIRED", "reel_thickness": 8}


async def test_count_and_ccd_use_measured_quantity_and_this_reels_label(mock_peer):
    decide, _ = mock_peer
    labels = []
    barcode = BARCODES[0]
    for qty in (0, 612):
        response = await decide("pkg", barcode, scanned_at=1)
        assert response.json()["data"] == {"result": "COUNT_REQUIRED", "reel_thickness": 8}
        count_data = dict(actual_qty=qty, template_id="XX123456789", observed_at=1000)
        operation_id = str(uuid7())
        response = await decide("count", barcode, operation_id=operation_id, **count_data)
        replay = await decide("count", barcode, operation_id=operation_id, **count_data)
        assert replay.json() == response.json()
        conflict = await decide(
            "count",
            barcode,
            operation_id=operation_id,
            actual_qty=qty + 1,
            template_id="XX123456789",
            observed_at=1000,
        )
        assert conflict.status_code == 409
        label = response.json()["data"]["label_variables"]
        assert label["QQTYPC"] == str(qty)
        assert label["PHHPN"] == barcode.split(",")[0][1:]
        labels.append(label["SPKG"])
        verified = await decide(
            "verify",
            barcode,
            scanned_at=1,
            scan_status="OK",
            actual_qty=qty,
            new_barcode=label["SPKG"],
        )
        assert verified.json()["data"] == {"result": "OK"}
    assert labels[0] != labels[1]
    unread = await decide(
        "verify",
        barcode,
        actual_qty=0,
        scanned_at=1,
        scan_status="NG",
        reason_code="NO_READ",
        reason_message="unreadable",
    )
    assert unread.json()["data"]["result"] == "NG"
    for actual_qty, other_label in ((0, labels[1]), (612, labels[0])):
        response = await decide(
            "verify",
            barcode,
            actual_qty=actual_qty,
            scanned_at=1,
            scan_status="OK",
            new_barcode=other_label,
        )
        assert response.json()["data"]["result"] == "NG"

    stale = []
    for _ in range(2):
        response = await decide(
            "count", barcode, actual_qty=10, template_id="XX123456789", observed_at=1
        )
        stale.append(response.json()["data"]["label_variables"]["SPKG"])
    old_label = await decide(
        "verify",
        barcode,
        actual_qty=10,
        scanned_at=1,
        scan_status="OK",
        new_barcode=stale[0],
    )
    current_label = await decide(
        "verify",
        barcode,
        actual_qty=10,
        scanned_at=1,
        scan_status="OK",
        new_barcode=stale[1],
    )
    assert old_label.json()["data"]["result"] == "NG"
    assert current_label.json()["data"] == {"result": "OK"}
    verify_operation_id = str(uuid7())
    first, duplicate = await asyncio.gather(
        *(
            decide(
                "verify",
                barcode,
                operation_id=verify_operation_id,
                actual_qty=10,
                scanned_at=1,
                scan_status="OK",
                new_barcode=stale[1],
            )
            for _ in range(2)
        )
    )
    assert first.json() == duplicate.json()

    other_quantity = await decide(
        "count", barcode, actual_qty=11, template_id="XX123456789", observed_at=1
    )
    other_label = other_quantity.json()["data"]["label_variables"]["SPKG"]
    changed_quantity = await decide(
        "verify",
        barcode,
        actual_qty=10,
        scanned_at=1,
        scan_status="OK",
        new_barcode=other_label,
    )
    assert changed_quantity.json()["data"]["result"] == "NG"

    other_material = await decide(
        "verify",
        BARCODES[1],
        actual_qty=10,
        scanned_at=1,
        scan_status="OK",
        new_barcode=stale[1],
    )
    other_line = await decide(
        "verify",
        barcode,
        workline_code="OTHER",
        actual_qty=10,
        scanned_at=1,
        scan_status="OK",
        new_barcode=stale[1],
    )
    assert other_material.json()["data"]["result"] == "NG"
    assert other_line.json()["data"] == {}


async def test_only_exact_barcodes_count_and_valid_others_go_to_transit(mock_peer):
    decide, _ = mock_peer
    barcode = BARCODES[0].replace("Q3000", "Q3001")
    response = await decide("pkg", barcode, scanned_at=1)
    assert response.json()["data"] == {"result": "BYPASS_COUNT"}
    response = await decide("put_target", barcode, mode="BYPASS", observed_at=1)
    assert response.json()["data"] == {
        "result": "ACCEPT",
        "target_locator": {"type": "TRANSIT_BIN"},
    }
    assert (await decide("pkg", "", scanned_at=1)).status_code == 422
    assert (
        await decide("put_target", BARCODES[0], mode="BYPASS", observed_at=1)
    ).status_code == 422
    other_line = await decide("pkg", BARCODES[0], workline_code="OTHER", scanned_at=1)
    assert other_line.json()["data"] == {}


async def test_counted_outlet_replays_original_and_waits_for_applied_rack_arrival(mock_peer):
    decide, dispatch = mock_peer
    for column, barcode in enumerate(BARCODES, 1):
        counted = await decide(
            "count",
            barcode,
            actual_qty=2,
            template_id="XX123456789",
            observed_at=1,
        )
        args = {
            "mode": "COUNTED",
            "observed_at": 1,
            "new_barcode": counted.json()["data"]["label_variables"]["SPKG"],
        }
        barcode = args["new_barcode"]
        if column == 1:
            operation_id = str(uuid7())
            first = await decide("put_target", barcode, operation_id=operation_id, **args)
            data = first.json()["data"]
            assert data["result"] == "ACCEPT" and data["target_preparation"] == "REPLACE"
            assert data["target_locator"]["rack_id"] == "RACK"
            assert data["target_locator"]["rack_face"] == "A"
            assert data["target_locator"]["slot_id"] == "S1"
            dispatch.pending = (object(),)
            waiting = await decide("put_target", barcode, **args)
            assert waiting.json()["data"]["result"] == "WAIT"
            dispatch.ready = (
                SimpleNamespace(
                    origin=SimpleNamespace(rack_id="RACK"),
                    actual_face="A",
                ),
            )
            replay = await decide("put_target", barcode, operation_id=operation_id, **args)
            assert replay.json() == first.json()
            conflict = await decide(
                "put_target", barcode, operation_id=operation_id, **{**args, "observed_at": 2}
            )
            assert conflict.status_code == 409
        response = await decide("put_target", barcode, **args)
        assert response.json()["data"]["target_locator"]["slot_id"] == f"S{column}"


async def test_outlet_rejects_wrong_label_and_waits_when_the_slot_is_reserved(mock_peer):
    decide, dispatch = mock_peer
    barcode = BARCODES[0]
    dispatch.ready = (SimpleNamespace(origin=SimpleNamespace(rack_id="RACK"), actual_face="B"),)
    labels = []
    for _ in range(2):
        reply = await decide(
            "count",
            barcode,
            actual_qty=10,
            template_id="XX123456789",
            observed_at=1,
        )
        labels.append(reply.json()["data"]["label_variables"]["SPKG"])
    wrong = await decide(
        "put_target", labels[0], mode="COUNTED", observed_at=1, new_barcode=labels[1]
    )
    assert wrong.status_code == 422
    unknown = await decide(
        "put_target", "UNKNOWN-SPKG", mode="COUNTED", observed_at=1, new_barcode="UNKNOWN-SPKG"
    )
    assert unknown.status_code == 422
    first = dict(mode="COUNTED", observed_at=1, new_barcode=labels[0])
    barcode = labels[0]
    assert (await decide("put_target", barcode, **first)).json()["data"]["result"] == "WAIT"
    dispatch.ready[0].actual_face = "A"
    assert (await decide("put_target", barcode, **first)).json()["data"]["result"] == "ACCEPT"
    second = await decide(
        "put_target",
        labels[1],
        mode="COUNTED",
        observed_at=1,
        new_barcode=labels[1],
    )
    assert second.json()["data"]["result"] == "WAIT"


def test_xray_mock_is_rejected_outside_local():
    from app.core.config import Settings

    with pytest.raises(ValueError, match="available only in the local integration mode"):
        Settings(
            ENVIRONMENT="production",
            XRAY_WMS_MOCK_TARGETS={"XRAY": {}},
            DATABASE_URL="sqlite+aiosqlite://",
            REDIS_URL="redis://localhost:6379/0",
            JWT_SECRET_KEY="isolated-test-key-with-at-least-32-characters",
        )


@pytest.fixture
async def random_mock_peers(callback_db):
    async def session_override():
        async with callback_db() as session:
            yield session

    def make_peer(thickness=8.1):
        app = FastAPI()
        targets = {
            line: {"random": {"reel_thickness": thickness}} for line in ("XRAY", "XRAY-OTHER")
        }
        app.state.xray_wms_mock = XrayWmsMock(targets, {})
        app.dependency_overrides[get_session] = session_override
        app.include_router(router, prefix="/api/v1")
        return app

    async def decide(app, stage, barcode, *, operation_id=None, signed=True, **data):
        raw = json.dumps(
            {
                "operation_id": operation_id or str(uuid7()),
                "operation": f"inbound.xray.{stage}_decide@v1",
                "timestamp": 1000,
                "data": {"workline_code": "XRAY", "barcode": barcode, **data},
            }
        ).encode()
        headers = {MOCK_SIGNATURE_HEADER: mock_signature(raw).decode()} if signed else {}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/api/v1/wes/decisions", content=raw, headers=headers)

    engine = callback_db.kw["bind"]
    async with engine.begin() as connection:
        await connection.run_sync(MockBase.metadata.create_all)
    try:
        yield make_peer, decide
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(MockBase.metadata.drop_all)


async def test_random_same_intake_replays_across_workers_and_restart(
    random_mock_peers, monkeypatch
):
    make_peer, decide = random_mock_peers
    apps = [make_peer(), make_peer()]
    barcode = "MOCK-REEL-001"
    original_id = str(uuid7())
    draw = 9

    def choose(upper):
        assert upper == 10
        return draw

    monkeypatch.setattr("app.businesses.xray.mock_wms.randbelow", choose)
    rejected = await decide(
        apps[0], "pkg", "UNSIGNED", operation_id=original_id, scanned_at=1, signed=False
    )
    assert rejected.status_code == 401
    first, duplicate = await asyncio.gather(
        *(decide(app, "pkg", barcode, scanned_at=1, operation_id=original_id) for app in apps)
    )
    assert first.status_code == duplicate.status_code == 200, (first.text, duplicate.text)
    assert first.json() == duplicate.json()
    assert first.json()["data"] == {"result": "COUNT_REQUIRED", "reel_thickness": 8.1}

    draw = 0
    replacement = make_peer(thickness=9.2)
    restarted = await decide(replacement, "pkg", barcode, scanned_at=1, operation_id=original_id)
    assert restarted.json() == first.json()
    assert (
        await decide(replacement, "pkg", barcode, scanned_at=2, operation_id=original_id)
    ).status_code == 409
    second = await decide(replacement, "pkg", barcode, scanned_at=2)
    assert second.json()["data"] == {"result": "BYPASS_COUNT"}
    assert (await decide(replacement, "pkg", "", scanned_at=1)).status_code == 422


async def test_random_only_accepts_labels_for_the_original_reel_and_line(random_mock_peers):
    make_peer, decide = random_mock_peers
    original = "ORIGINAL-REEL"
    counted = await decide(
        make_peer(), "count", original, actual_qty=1, template_id="XX123456789", observed_at=1
    )
    label = counted.json()["data"]["label_variables"]["SPKG"]
    counted_again = await decide(
        make_peer(), "count", original, actual_qty=1, template_id="XX123456789", observed_at=2
    )
    current_label = counted_again.json()["data"]["label_variables"]["SPKG"]
    stale = await decide(
        make_peer(),
        "verify",
        original,
        workline_code="XRAY",
        scan_status="OK",
        scanned_at=2,
        actual_qty=1,
        new_barcode=label,
    )
    current = await decide(
        make_peer(),
        "verify",
        original,
        workline_code="XRAY",
        scan_status="OK",
        scanned_at=2,
        actual_qty=1,
        new_barcode=current_label,
    )
    assert stale.json()["data"]["result"] == "NG"
    assert current.json()["data"] == {"result": "OK"}
    verify_operation_id = str(uuid7())
    first, duplicate = await asyncio.gather(
        *(
            decide(
                make_peer(),
                "verify",
                original,
                operation_id=verify_operation_id,
                workline_code="XRAY",
                scan_status="OK",
                scanned_at=2,
                actual_qty=1,
                new_barcode=current_label,
            )
            for _ in range(2)
        )
    )
    assert first.json() == duplicate.json()
    peer = make_peer()
    for barcode, spkg, workline in (
        ("OTHER-REEL", label, "XRAY"),
        (original, "UNISSUED-LABEL", "XRAY"),
        (original, label, "XRAY-OTHER"),
    ):
        verified = await decide(
            peer,
            "verify",
            barcode,
            workline_code=workline,
            scan_status="OK",
            scanned_at=1,
            actual_qty=1,
            new_barcode=spkg,
        )
        assert verified.status_code == 200
        assert verified.json()["data"]["reason_code"] == "VERIFY_MISMATCH"
    for spkg, workline in (("UNISSUED-LABEL", "XRAY"), (label, "XRAY-OTHER")):
        outlet = await decide(
            peer,
            "put_target",
            spkg,
            workline_code=workline,
            mode="COUNTED",
            observed_at=1,
            new_barcode=spkg,
        )
        assert outlet.status_code == 422


async def test_verify_requires_measured_quantity_and_rejects_legacy_label_field(mock_peer):
    decide, _ = mock_peer
    missing = await decide(
        "verify",
        BARCODES[0],
        scanned_at=1,
        scan_status="OK",
        expected_spkg="LEGACY-LABEL",
        new_barcode="NEW-LABEL",
    )
    negative = await decide(
        "verify",
        BARCODES[0],
        scanned_at=1,
        scan_status="OK",
        actual_qty=-1,
        new_barcode="NEW-LABEL",
    )
    assert missing.status_code == negative.status_code == 422


def test_xray_mock_rejects_the_example_signing_key():
    from app.core.config import Settings

    with pytest.raises(ValueError, match="mock requires JWT_SECRET_KEY"):
        Settings(
            ENVIRONMENT="local",
            XRAY_WMS_MOCK_TARGETS={"XRAY": {}},
            DATABASE_URL="sqlite+aiosqlite://",
            REDIS_URL="redis://localhost:6379/0",
            JWT_SECRET_KEY="secret-string",
        )
