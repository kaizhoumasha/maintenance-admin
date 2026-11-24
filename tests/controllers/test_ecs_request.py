import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from app.controllers import ecs_request as ecs_request_controller
from app.core.identifiers import uuid7
from app.integrations.callback_receipts import find_receipt_state
from app.integrations.ecs_request import CallbackPayload, ecs_request, registry
from app.integrations.ecs_request.envelope import EcsRequestEnvelope
from app.integrations.ecs_request.receiver import OPERATION, request_identity
from app.main import app
from app.models import ReliableMessage
from tests.helpers import ecs_binding, read_receipts

PATH = "/api/v1/callback/request"


class Decision(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    result: str


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(registry, "_registry", {})
    monkeypatch.setattr(app.state, "ecs_event_binding_resolver", ecs_binding)
    monkeypatch.setattr(app.state, "ecs_request_binding_snapshot_resolver", None)


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
        model_config = ConfigDict(strict=True, extra="ignore")
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


async def test_unknown_delivery_state_does_not_continue_to_handler(monkeypatch):
    class Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class StopPolling(Exception):
        pass

    async def stop(_):
        raise StopPolling

    async def unknown_result(*args, **kwargs):
        return SimpleNamespace(
            delivery_uncertain=False,
            response_state="UNAVAILABLE",
            delivery_terminal=None,
        )

    async def no_receipt(*args, **kwargs):
        return None

    monkeypatch.setattr(ecs_request_controller, "get_result", unknown_result)
    monkeypatch.setattr(ecs_request_controller.asyncio, "sleep", stop)
    monkeypatch.setattr("app.integrations.callback_receipts.find_receipt_state", no_receipt)

    with pytest.raises(StopPolling):
        await ecs_request_controller.wait_for_request(
            Factory(), "digest", SimpleNamespace(message_id=1, operation="wms", operation_id="op")
        )


async def enqueue(ctx, operation_id):
    now = datetime.now(UTC)
    message = ReliableMessage(
        target="wms",
        operation="wms.decide@v1",
        operation_id=operation_id,
        status="PENDING",
        exchange_id=uuid7(),
        request_record_id=uuid7(),
        created_at=now,
        updated_at=now,
        next_attempt_at=now,
    )
    ctx.session.add(message)
    await ctx.session.flush()
    return message


def terminal_delivery(response_state, delivery_status):
    async def result(*_args, **_kwargs):
        return SimpleNamespace(
            delivery_uncertain=False,
            response_state=response_state,
            delivery_terminal=True,
            delivery_status=delivery_status,
        )

    return result


async def saved_request(store, payload):
    envelope = EcsRequestEnvelope.model_validate(payload)
    async with store() as session:
        state = await find_receipt_state(session, OPERATION, request_identity(envelope))
        messages = await session.scalar(select(func.count()).select_from(ReliableMessage))
    return state, messages


@pytest.mark.parametrize(
    ("response_state", "delivery_status"),
    [("VALID", "DONE"), ("NONE", "EXPIRED"), ("NONE", "ABANDONED")],
)
async def test_terminal_result_continues_once(
    callback_client, callback_store, monkeypatch, response_state, delivery_status
):
    calls = []

    @ecs_request("WAIT_DECIDE", device_role="SCAN1", data=CallbackPayload, response=Decision)
    async def handler(ctx, data):
        calls.append(ctx.deferred)
        if ctx.deferred is None:
            message = await enqueue(ctx, f"{response_state}-{delivery_status}")
            ctx.defer(message.id, message.operation, message.operation_id)
            return
        ctx.respond(Decision(result="ALLOW"))

    monkeypatch.setattr(
        ecs_request_controller,
        "get_result",
        terminal_delivery(response_state, delivery_status),
    )
    payload = body(request_type="WAIT_DECIDE", timestamp=2)
    response = await callback_client.post(PATH, json=payload)

    assert response.status_code == 200
    assert response.json() == {"code": 200, "message": "DECIDED", "data": {"result": "ALLOW"}}
    assert len(calls) == 2
    state, messages = await saved_request(callback_store, payload)
    assert state.status_code == 200
    assert state.body["data"]["result"] == "ALLOW"
    assert messages == 1


async def test_wait_budget_keeps_pending_receipt(callback_client, callback_store, monkeypatch):
    calls = []

    @ecs_request("WAIT_BUDGET", device_role="SCAN1", data=CallbackPayload, response=Decision)
    async def handler(ctx, data):
        calls.append(ctx.receipt_id)
        message = await enqueue(ctx, "budget")
        ctx.defer(message.id, message.operation, message.operation_id)

    async def pending(*_args, **_kwargs):
        return SimpleNamespace(
            delivery_uncertain=False,
            response_state="NONE",
            delivery_terminal=False,
            delivery_status="PENDING",
        )

    original_sleep = asyncio.sleep

    async def step(_delay):
        await original_sleep(0.05)

    async def tiny_budget(*_args, **_kwargs):
        return 20

    monkeypatch.setattr(ecs_request_controller, "get_result", pending)
    monkeypatch.setattr(ecs_request_controller.asyncio, "sleep", step)
    monkeypatch.setattr(ecs_request_controller, "request_budget_ms", tiny_budget)
    payload = body(request_type="WAIT_BUDGET", timestamp=3)
    response = await callback_client.post(PATH, json=payload)

    assert response.status_code == 503
    assert response.json() == {"code": 503, "message": "TEMPORARILY_UNAVAILABLE"}
    assert len(calls) == 1
    state, messages = await saved_request(callback_store, payload)
    assert state.status_code == 503
    assert state.body is None
    assert state.prepared_body is None
    assert state.diagnostic is None
    assert state.event_context["deferred_request"]["operation_id"] == "budget"
    assert messages == 1


@pytest.mark.parametrize("repeat", ["same", "silent"])
async def test_terminal_defer_rolls_back_with_diagnostic(
    callback_client, callback_store, monkeypatch, repeat
):
    queued = {}

    @ecs_request("WAIT_INCOMPLETE", device_role="SCAN1", data=CallbackPayload, response=Decision)
    async def handler(ctx, data):
        if ctx.deferred is None:
            message = await enqueue(ctx, repeat)
            queued["id"] = message.id
            ctx.defer(message.id, message.operation, message.operation_id)
            return
        if repeat == "same":
            ctx.defer(ctx.deferred.message_id, ctx.deferred.operation, ctx.deferred.operation_id)

    monkeypatch.setattr(ecs_request_controller, "get_result", terminal_delivery("VALID", "DONE"))
    payload = body(request_type="WAIT_INCOMPLETE", timestamp=4)
    response = await callback_client.post(PATH, json=payload)

    assert response.status_code == 503
    assert response.json() == {"code": 503, "message": "TEMPORARILY_UNAVAILABLE"}
    state, messages = await saved_request(callback_store, payload)
    assert state.status_code == 503
    assert state.body is None
    assert state.prepared_body is None
    assert state.diagnostic == [{"reason_code": "REQUEST_HANDLER_INCOMPLETE"}]
    assert state.event_context["deferred_request"]["message_id"] == queued["id"]
    assert messages == 1
