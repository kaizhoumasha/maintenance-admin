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


async def _seed_outbound(callback_store, *, status, operation="demo.op@v1", operation_id=None):
    import json
    from datetime import UTC, datetime

    from app.core.identifiers import uuid7
    from tests.helpers import seed_reliable_message

    oid = operation_id or str(uuid7())
    now = datetime.now(UTC)
    async with callback_store.begin() as session:
        message = await seed_reliable_message(
            session,
            target="ecs-request-test",
            operation=operation,
            operation_id=oid,
            request_raw=json.dumps(
                {"operation": operation, "operation_id": oid, "data": {}},
                separators=(",", ":"),
            ).encode(),
            status=status,
            attempts=0,
            next_attempt_at=None if status == "ABANDONED" else now,
            created_at=now,
            updated_at=now,
        )
    return message, oid


async def _pending_state(callback_store, payload):
    from app.integrations.callback_receipts import find_receipt_state
    from app.integrations.ecs_request.envelope import EcsRequestEnvelope
    from app.integrations.ecs_request.receiver import request_identity

    identity = request_identity(EcsRequestEnvelope.model_validate(payload))
    async with callback_store() as session:
        return await find_receipt_state(session, "ecs.request", identity), identity


async def test_wait_budget_exhaustion_keeps_deferred_receipt(
    callback_client, callback_store, monkeypatch
):
    message, oid = await _seed_outbound(callback_store, status="PENDING")
    sends = []

    @ecs_request("CARTON_VALIDATE", device_role="SCAN1", data=CallbackPayload, response=Decision)
    async def handler(ctx, data):
        if ctx.deferred is None:
            sends.append(ctx.receipt_id)
            ctx.defer(message.id, message.operation, oid)
            return
        ctx.respond(Decision(result="ALLOW"))

    async def _budget(*a, **k):
        return 0

    monkeypatch.setattr(ecs_request_controller, "request_budget_ms", _budget)

    async def _sleep(_):
        return None

    monkeypatch.setattr(ecs_request_controller.asyncio, "sleep", _sleep)

    payload = body()
    response = await callback_client.post(PATH, json=payload)

    assert response.status_code == 503
    assert response.json() == {"code": 503, "message": "TEMPORARILY_UNAVAILABLE"}
    assert len(sends) == 1
    state, _ = await _pending_state(callback_store, payload)
    assert state is not None
    assert state.status_code == 503
    assert state.prepared_body is None
    assert state.event_context["deferred_request"] == {
        "message_id": message.id,
        "operation": message.operation,
        "operation_id": oid,
    }
    async with callback_store() as session:
        from sqlalchemy import select

        from app.models import ReliableMessage

        rows = list(await session.scalars(select(ReliableMessage)))
        assert len(rows) == 1 and rows[0].id == message.id


async def test_post_wait_valid_continues_to_decided(callback_client, callback_store, monkeypatch):
    message, oid = await _seed_outbound(callback_store, status="PENDING")
    sends = []

    @ecs_request("CARTON_VALIDATE", device_role="SCAN1", data=CallbackPayload, response=Decision)
    async def handler(ctx, data):
        if ctx.deferred is None:
            sends.append(1)
            ctx.defer(message.id, message.operation, oid)
            return
        ctx.respond(Decision(result="ALLOW"))

    async def valid_result(*args, **kwargs):
        return SimpleNamespace(
            delivery_uncertain=False,
            response_state="VALID",
            delivery_terminal=True,
            response_data={"ok": True},
        )

    monkeypatch.setattr(ecs_request_controller, "get_result", valid_result)

    async def _budget(*a, **k):
        return 1000

    monkeypatch.setattr(ecs_request_controller, "request_budget_ms", _budget)

    response = await callback_client.post(PATH, json=body())
    replay = await callback_client.post(PATH, json=body())

    assert response.status_code == replay.status_code == 200
    assert response.json() == {"code": 200, "message": "DECIDED", "data": {"result": "ALLOW"}}
    assert replay.content == response.content
    assert sends == [1]
    async with callback_store() as session:
        rows = await read_receipts(session)
        assert len(rows) == 1
        assert rows[0].status_code == 200


async def test_expired_delivery_continues_to_decided(callback_client, callback_store, monkeypatch):
    message, oid = await _seed_outbound(callback_store, status="EXPIRED")
    sends = []

    @ecs_request("CARTON_VALIDATE", device_role="SCAN1", data=CallbackPayload, response=Decision)
    async def handler(ctx, data):
        if ctx.deferred is None:
            sends.append(1)
            ctx.defer(message.id, message.operation, oid)
            return
        ctx.respond(Decision(result="DENY"))

    async def _budget(*a, **k):
        return 1000

    monkeypatch.setattr(ecs_request_controller, "request_budget_ms", _budget)

    response = await callback_client.post(PATH, json=body())

    assert response.status_code == 200
    assert response.json() == {"code": 200, "message": "DECIDED", "data": {"result": "DENY"}}
    assert sends == [1]
    async with callback_store() as session:
        rows = await read_receipts(session)
        assert len(rows) == 1 and rows[0].status_code == 200


async def test_terminal_redefer_rolls_back_and_records_incomplete(
    callback_client, callback_store, monkeypatch
):
    message, oid = await _seed_outbound(callback_store, status="EXPIRED")

    @ecs_request("CARTON_VALIDATE", device_role="SCAN1", data=CallbackPayload, response=Decision)
    async def handler(ctx, data):
        ctx.defer(message.id, message.operation, oid)

    async def _budget(*a, **k):
        return 1000

    monkeypatch.setattr(ecs_request_controller, "request_budget_ms", _budget)

    payload = body()
    response = await callback_client.post(PATH, json=payload)

    assert response.status_code == 503
    assert response.json() == {"code": 503, "message": "TEMPORARILY_UNAVAILABLE"}
    state, _ = await _pending_state(callback_store, payload)
    assert state is not None
    assert state.prepared_body is None
    assert state.status_code == 503
    assert state.diagnostic == [{"reason_code": "REQUEST_HANDLER_INCOMPLETE"}]
    assert state.event_context["deferred_request"] == {
        "message_id": message.id,
        "operation": message.operation,
        "operation_id": oid,
    }


async def test_terminal_handler_without_respond_records_incomplete(
    callback_client, callback_store, monkeypatch
):
    message, oid = await _seed_outbound(callback_store, status="EXPIRED")

    @ecs_request("CARTON_VALIDATE", device_role="SCAN1", data=CallbackPayload, response=Decision)
    async def handler(ctx, data):
        if ctx.deferred is None:
            ctx.defer(message.id, message.operation, oid)

    async def _budget(*a, **k):
        return 1000

    monkeypatch.setattr(ecs_request_controller, "request_budget_ms", _budget)

    payload = body()
    response = await callback_client.post(PATH, json=payload)

    assert response.status_code == 503
    state, _ = await _pending_state(callback_store, payload)
    assert state.prepared_body is None
    assert state.diagnostic == [{"reason_code": "REQUEST_HANDLER_INCOMPLETE"}]
