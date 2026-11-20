import pytest
from pydantic import BaseModel, ConfigDict

from app.integrations.ecs_request import CallbackPayload, ecs_request, registry
from app.main import app
from tests.helpers import ecs_binding, read_receipts

PATH = "/api/v1/callback/request"


class Decision(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    result: str


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(registry, "_registry", {})
    monkeypatch.setattr(app.state, "ecs_event_binding_resolver", ecs_binding)


def body(**changes):
    return {"device_code": "D1", "request_type": "CARTON_VALIDATE", "timestamp": 1} | changes


async def test_immediate_decision_is_persisted_and_replayed(callback_client, callback_store):
    calls = []

    @ecs_request("CARTON_VALIDATE", device_role="SCAN1", data=CallbackPayload, response=Decision)
    async def handler(ctx, data):
        calls.append(ctx.receipt_id)
        ctx.respond(Decision(result="ALLOW"))

    first = await callback_client.post(PATH, json=body())
    replay = await callback_client.post(PATH, json=body())

    assert first.status_code == replay.status_code == 200
    assert first.json() == {"code": 200, "message": "DECIDED", "data": {"result": "ALLOW"}}
    assert replay.content == first.content
    assert len(calls) == 1
    async with callback_store() as session:
        rows = await read_receipts(session)
        assert len(rows) == 1


async def test_unknown_request_is_rejected_without_receipt(callback_client, callback_store):
    response = await callback_client.post(PATH, json=body(request_type="UNKNOWN"))
    assert response.status_code == 400
    assert response.json() == {"code": 400, "message": "UNKNOWN_REQUEST"}
    async with callback_store() as session:
        assert await read_receipts(session) == []


async def test_handler_without_decision_returns_503_without_receipt(
    callback_client, callback_store
):
    @ecs_request("CARTON_VALIDATE", device_role="SCAN1", data=CallbackPayload, response=Decision)
    async def handler(ctx, data):
        return None

    response = await callback_client.post(PATH, json=body())
    assert response.status_code == 503
    assert response.json() == {"code": 503, "message": "TEMPORARILY_UNAVAILABLE"}
    async with callback_store() as session:
        assert await read_receipts(session) == []
