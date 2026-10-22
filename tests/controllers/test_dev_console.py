"""联调控制台:统一入口走真实业务接入层登记出站义务,状态节点可见,Mock WMS 与适配器判定一致。"""

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI

from app.controllers.dev import include_dev_routers
from app.core.dev_scenarios import scenarios
from app.http_client import DeliveryState, HttpResult
from app.integrations.wms_transport import (
    ADAPTERS,
    PROTOCOL,
    TransportService,
    bin_scenarios,
    rack_scenarios,
)
from app.integrations.wms_transport.wire import freeze_request
from app.reliable import OutboundMessage, Outbox, VerdictKind
from app.reliable.config import TargetConfig

TASKS = "/api/v1/dev/transport/tasks"
MOCK = "/api/v1/dev/mock-wms/mode"
SUBMIT = "/api/v1/wes/transport-requests"
FEED = next(s for s in scenarios() if s.name == "bin.feed")


@pytest.fixture
def wms(monkeypatch):
    target = TargetConfig("wms-test", "http://example.test", PROTOCOL, max_not_received_attempts=3)
    service = TransportService(Outbox({target.name: target}, ADAPTERS), target=target.name)
    monkeypatch.setattr(bin_scenarios, "transport", service)
    monkeypatch.setattr(rack_scenarios, "transport", service)


def body(scenario, **extra):
    return {"scenario": scenario.name, **scenario.example, **extra}


def test_dev_routes_do_not_exist_outside_local():
    app = FastAPI()
    include_dev_routers(app, environment="production")
    assert app.openapi()["paths"] == {}


async def test_the_entry_requires_login(client):
    r = await client.post(TASKS, json=body(FEED))
    assert r.status_code == 401


@pytest.mark.parametrize("scenario", scenarios(), ids=lambda s: s.name)
async def test_every_scenario_example_registers_a_visible_pending_obligation(
    client, admin, wms, scenario
):
    r = await client.post(TASKS, json=body(scenario), headers=admin)
    assert r.status_code == 200, r.text
    key = r.json()["business_key"]
    status = await client.get(f"{TASKS}/status/{key}", headers=admin)  # 另一个请求另一个会话
    assert [m["status"] for m in status.json()] == ["PENDING"]
    assert status.json()[0]["request"]["data"]["transport_task_id"] == key


async def test_the_same_request_id_replays_and_different_input_conflicts(client, admin, wms):
    rid = "01a13b00-cabd-7390-890a-0c093e7c07a1"
    first = await client.post(TASKS, json=body(FEED, client_request_id=rid), headers=admin)
    again = await client.post(TASKS, json=body(FEED, client_request_id=rid), headers=admin)
    assert first.json()["business_key"] == again.json()["business_key"]
    other = body(FEED, client_request_id=rid)
    other["moves"] = [{**other["moves"][0], "bin_code": "BIN-OTHER"}]
    assert (await client.post(TASKS, json=other, headers=admin)).status_code == 409


async def test_invalid_input_is_rejected_with_400(client, admin, wms):
    rack = next(s for s in scenarios() if s.name == "rack.enter")
    for bad in (body(rack, target_face="x" * 11), {**body(FEED), "scenario": "nope"}):
        assert (await client.post(TASKS, json=bad, headers=admin)).status_code == 400


async def test_status_of_an_unknown_key_is_404(client, admin):
    assert (await client.get(f"{TASKS}/status/none", headers=admin)).status_code == 404


@pytest.fixture
async def mock_mode(client, admin):
    async def use(mode):
        r = await client.put(MOCK, json={"mode": mode}, headers=admin)
        assert r.status_code == 200

    yield use
    await use("accept")


@pytest.mark.parametrize(
    ("mode", "verdict"),
    [
        ("accept", VerdictKind.DELIVERED),
        ("duplicate", VerdictKind.DELIVERED),
        ("conflict", VerdictKind.PEER_TERMINAL),
        ("reject", VerdictKind.PEER_TERMINAL),
        ("unavailable", VerdictKind.NOT_RECEIVED),
        ("garbage", VerdictKind.UNKNOWN),
    ],
)
async def test_the_mock_wms_answers_what_the_transport_adapter_expects(
    client, admin, wms, mock_mode, mode, verdict
):
    key = (await client.post(TASKS, json=body(FEED), headers=admin)).json()["business_key"]
    row = (await client.get(f"{TASKS}/status/{key}", headers=admin)).json()[0]
    request = row["request"]
    frozen = freeze_request(
        request["operation_id"],
        request["data"]["transport_task_id"],
        request["timestamp"],
        request["data"],
    )
    await mock_mode(mode)
    r = await client.post(SUBMIT, content=frozen)
    message = OutboundMessage(
        row["target"],
        row["operation"],
        row["operation_id"],
        frozen,
        key,
        None,
        datetime.now(UTC),
        None,
    )
    result = HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, r.status_code, (), r.content)
    assert ADAPTERS[PROTOCOL].classify_response(message, result).kind == verdict
