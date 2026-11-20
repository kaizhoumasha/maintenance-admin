from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict

from app.controllers import ecs_request as ecs_request_controller
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


async def test_invalid_request_data_is_rejected_without_receipt(callback_client, callback_store):
    class RequiredPayload(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")
        required: str

    @ecs_request("CARTON_VALIDATE", device_role="SCAN1", data=RequiredPayload, response=Decision)
    async def handler(ctx, data):
        ctx.respond(Decision(result="ALLOW"))

    response = await callback_client.post(PATH, json=body(data={}))

    assert response.status_code == 400
    assert response.json()["message"] == "INVALID_ENVELOPE"
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


async def test_request_capture_uses_ecs_request_projection(callback_client, monkeypatch):
    captured = {}

    @ecs_request("CARTON_VALIDATE", device_role="SCAN1", data=CallbackPayload, response=Decision)
    async def handler(ctx, data):
        ctx.respond(Decision(result="ALLOW"))

    async def capture(*args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)

    monkeypatch.setattr(ecs_request_controller, "capture_callback", capture)
    response = await callback_client.post(PATH, json=body())

    assert response.status_code == 200
    assert captured["source"] == "ecs_request"
    assert captured["result"].status_code == 200


async def test_wait_budget_uses_message_target_timeout(monkeypatch):
    class Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, model, message_id):
            return SimpleNamespace(target="wms")

    monkeypatch.setattr(
        ecs_request_controller,
        "get_settings",
        lambda: object(),
    )
    monkeypatch.setattr(
        ecs_request_controller,
        "parse_reliable_config",
        lambda settings: SimpleNamespace(
            poll_interval_s=0.25,
            targets={"wms": SimpleNamespace(timeout_ms=2750)},
        ),
    )

    budget = await ecs_request_controller.request_budget_ms(Factory(), 7)

    assert budget == 3000


async def test_request_body_read_failure_returns_503_and_captures(monkeypatch):
    from types import SimpleNamespace

    captured = {}
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(callback_session_factory=lambda: None),
        ),
        client=None,
    )

    async def read_body(*args):
        raise OSError("disconnect")

    async def capture(*args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)

    monkeypatch.setattr(ecs_request_controller, "read_body_capped", read_body)
    monkeypatch.setattr(ecs_request_controller, "_capture_request", capture)

    result = await ecs_request_controller.receive_request(request)

    assert result.status_code == 503
    assert captured["args"][4] is None
    assert captured["commit_unknown"] is False


async def test_capture_request_propagates_commit_uncertainty(monkeypatch):
    captured = {}

    async def capture(*args, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(ecs_request_controller, "capture_callback", capture)
    await ecs_request_controller._capture_request(
        None,
        object(),
        None,
        "exchange",
        b"{}",
        SimpleNamespace(status_code=503),
        commit_unknown=True,
    )

    assert captured["commit_unknown"] is True
