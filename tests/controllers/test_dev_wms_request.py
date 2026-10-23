"""WMS 请求端点联调界面:目录、触发、状态可见,Mock WMS 与适配器判定一致。"""

import json
from datetime import UTC, datetime

import pytest

from app.core.identifiers import uuid7
from app.http_client import DeliveryState, HttpResult
from app.integrations.wms_request import (
    ADAPTERS,
    DECISIONS_PROTOCOL,
    FACTS_PROTOCOL,
    wms_requests,
)
from app.integrations.wms_request.operation import operations
from app.reliable import Outbox, OutgoingMessage, VerdictKind
from app.reliable.config import TargetConfig

BASE = "/api/v1/dev/wms/requests"
MOCK = "/api/v1/dev/mock-wms/mode"
PATHS = {FACTS_PROTOCOL: "/api/v1/wes/facts", DECISIONS_PROTOCOL: "/api/v1/wes/decisions"}
FACT, DECISION = "example.fact.demo@v1", "example.decision.demo@v1"


@pytest.fixture
def wms(monkeypatch):
    targets = {
        protocol: TargetConfig(protocol, "http://example.test", protocol) for protocol in PATHS
    }
    monkeypatch.setattr(wms_requests, "_outbox", Outbox(targets, ADAPTERS))


def trigger(operation, **extra):
    example = operations()[operation].operation.example
    return {"operation": operation, "data": example, **extra}


async def test_the_routes_require_login(client):
    assert (await client.get(f"{BASE}/operations")).status_code == 401
    assert (await client.post(BASE, json=trigger(FACT))).status_code == 401
    assert (await client.get(f"{BASE}/status/{uuid7()}")).status_code == 401


async def test_the_catalog_describes_every_registered_operation(client, admin):
    listed = (await client.get(f"{BASE}/operations", headers=admin)).json()
    assert listed
    assert {item["name"] for item in listed} == set(operations())
    for item in listed:
        registered = operations()[item["name"]]
        assert item["endpoint"] == registered.endpoint.value
        assert item["example"] == registered.operation.example
        assert item["payload_schema"]["properties"]


@pytest.mark.parametrize("operation", sorted(operations()))
async def test_every_operation_example_registers_a_visible_pending_message(
    client, admin, wms, operation
):
    sent = await client.post(BASE, json=trigger(operation), headers=admin)
    assert sent.status_code == 200
    out = sent.json()
    protocol = {"facts": FACTS_PROTOCOL, "decisions": DECISIONS_PROTOCOL}[
        operations()[operation].endpoint.value
    ]
    assert out["target"] == protocol and out["status"] == "PENDING"
    rows = (await client.get(f"{BASE}/status/{out['operation_id']}", headers=admin)).json()
    assert [row["message_id"] for row in rows] == [out["message_id"]]
    assert rows[0]["request"]["operation"] == operation
    assert rows[0]["request"]["data"] == operations()[operation].operation.example


async def test_replay_returns_the_same_message_and_changed_input_conflicts(client, admin, wms):
    operation_id = str(uuid7())
    first = await client.post(BASE, json=trigger(FACT, operation_id=operation_id), headers=admin)
    again = await client.post(BASE, json=trigger(FACT, operation_id=operation_id), headers=admin)
    assert again.status_code == 200 and again.json()["message_id"] == first.json()["message_id"]
    changed = trigger(FACT, operation_id=operation_id)
    changed["data"] = {**changed["data"], "qty": 99}
    assert (await client.post(BASE, json=changed, headers=admin)).status_code == 409


async def test_invalid_input_is_rejected_with_400(client, admin, wms):
    bad_payload = trigger(FACT)
    bad_payload["data"] = {"task_id": "T-1"}
    assert (await client.post(BASE, json=bad_payload, headers=admin)).status_code == 400
    bad_id = trigger(FACT, operation_id="not-a-uuid")
    assert (await client.post(BASE, json=bad_id, headers=admin)).status_code == 400
    unknown = {"operation": "example.nothing@v1", "data": {}}
    assert (await client.post(BASE, json=unknown, headers=admin)).status_code == 400


async def test_status_of_an_unknown_operation_id_is_404(client, admin):
    assert (await client.get(f"{BASE}/status/{uuid7()}", headers=admin)).status_code == 404


@pytest.fixture
async def mock_mode(client, admin):
    async def use(mode):
        r = await client.put(MOCK, json={"mode": mode}, headers=admin)
        assert r.status_code == 200

    yield use
    await use("accept")


@pytest.mark.parametrize("protocol", [FACTS_PROTOCOL, DECISIONS_PROTOCOL])
@pytest.mark.parametrize(
    ("mode", "verdict"),
    [
        ("accept", VerdictKind.DELIVERED),
        ("duplicate", VerdictKind.DELIVERED),
        ("prepare_accepted", VerdictKind.DELIVERED),
        ("conflict", VerdictKind.PEER_TERMINAL),
        ("reject", VerdictKind.PEER_TERMINAL),
        ("unavailable", VerdictKind.NOT_RECEIVED),
        ("busy", VerdictKind.NOT_RECEIVED),
        ("garbage", VerdictKind.UNKNOWN),
    ],
)
async def test_the_mock_wms_answers_what_the_request_adapter_expects(
    client, mock_mode, protocol, mode, verdict
):
    operation_id = str(uuid7())
    raw = json.dumps(
        {"operation_id": operation_id, "operation": "x.y@v1", "timestamp": 1, "data": {}}
    ).encode()
    await mock_mode(mode)
    r = await client.post(PATHS[protocol], content=raw)
    message = OutgoingMessage(
        protocol, "x.y@v1", operation_id, raw, None, None, datetime.now(UTC), None
    )
    result = HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, r.status_code, (), r.content)
    classified = ADAPTERS[protocol].classify_response(message, result)
    assert classified.kind == verdict
    if mode == "busy":
        assert classified.retry_after_ms == 1000


async def test_the_mock_wms_refuses_an_unparsable_envelope_with_an_empty_400(client):
    for path in PATHS.values():
        r = await client.post(path, content=b'{"data": []}')
        assert (r.status_code, r.content) == (400, b"")
