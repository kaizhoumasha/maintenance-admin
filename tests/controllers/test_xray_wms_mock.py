import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

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

        async def decide(
            stage, barcode, *, reel_id="REEL-1", operation_id=None, signed=True, **data
        ):
            raw = json.dumps(
                {
                    "operation_id": operation_id or str(uuid7()),
                    "operation": f"inbound.xray.{stage}_decide@v1",
                    "timestamp": 1000,
                    "data": {
                        "workline_code": "XRAY",
                        "reel_id": reel_id,
                        "source_event_id": "EVT-1",
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
    assert accepted.json()["data"] == {"result": "COUNT_REQUIRED"}


async def test_count_and_ccd_use_measured_quantity_and_this_reels_label(mock_peer):
    decide, _ = mock_peer
    labels = []
    for barcode, qty in zip(BARCODES, (0, 612), strict=True):
        response = await decide("pkg", barcode, scanned_at=1)
        assert response.json()["data"] == {"result": "COUNT_REQUIRED"}
        response = await decide(
            "count",
            barcode,
            actual_qty=qty,
            template_id="XX123456789",
            observed_at=1000,
        )
        label = response.json()["data"]["label_variables"]
        assert label["QQTYPC"] == str(qty)
        assert label["PHHPN"] == barcode.split(",")[0][1:]
        labels.append(label["SPKG"])
        verified = await decide(
            "verify",
            barcode,
            scanned_at=1,
            scan_status="OK",
            new_barcode=label["SPKG"],
        )
        assert verified.json()["data"] == {"result": "OK"}
    assert labels[0] != labels[1]
    for barcode, reel_id, other_label in (
        (BARCODES[0], "REEL-1", labels[1]),
        (BARCODES[0], "REEL-2", labels[0]),
    ):
        response = await decide(
            "verify",
            barcode,
            reel_id=reel_id,
            scanned_at=1,
            scan_status="OK",
            new_barcode=other_label,
        )
        assert response.json()["data"]["result"] == "NG"


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
        if column == 1:
            operation_id = str(uuid7())
            first = await decide("put_target", barcode, operation_id=operation_id, **args)
            assert first.json()["data"] == {
                "result": "REPLENISH_REQUIRED",
                "location_code": "POINT",
                "incoming_rack": {"rack_id": "RACK", "target_face": "A"},
            }
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
    labels = {}
    for reel_id in ("REEL-1", "REEL-2"):
        reply = await decide(
            "count",
            barcode,
            reel_id=reel_id,
            actual_qty=10,
            template_id="XX123456789",
            observed_at=1,
        )
        labels[reel_id] = reply.json()["data"]["label_variables"]["SPKG"]
    wrong = await decide(
        "put_target", barcode, mode="COUNTED", observed_at=1, new_barcode=labels["REEL-2"]
    )
    assert wrong.status_code == 422
    first = dict(mode="COUNTED", observed_at=1, new_barcode=labels["REEL-1"])
    assert (await decide("put_target", barcode, **first)).json()["data"]["result"] == "WAIT"
    dispatch.ready[0].actual_face = "A"
    assert (await decide("put_target", barcode, **first)).json()["data"]["result"] == "ACCEPT"
    second = await decide(
        "put_target",
        barcode,
        reel_id="REEL-2",
        mode="COUNTED",
        observed_at=1,
        new_barcode=labels["REEL-2"],
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
