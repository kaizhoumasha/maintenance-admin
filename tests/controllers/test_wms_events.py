import asyncio
import base64
import json
from typing import Literal

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, event, insert, select

from app.core.identifiers import uuid7
from app.integrations.wms_callback import (
    Accepted,
    CallbackPayload,
    Conflict,
    Duplicate,
    Rejected,
    callback_operation,
    registry,
    snapshots,
)
from app.integrations.wms_callback.dev_callback import OPERATION as EXAMPLE_OPERATION
from app.integrations.wms_callback.dev_callback import EchoPayload, echo
from app.main import app
from app.models import CallbackReceipt, CallbackSnapshot

PATH = "/api/v1/wms/events"


@pytest.fixture
def envelope():
    return dict(
        operation_id=str(uuid7()), operation="example.unimplemented@v1", timestamp=1, data={}
    )


async def stored(factory, model):
    async with factory() as session:
        return list(await session.scalars(select(model).order_by(model.id)))


async def test_unknown_operation_replays_its_original_rejection(client, callback_store, envelope):
    first = await client.post(PATH, json=envelope)
    assert first.status_code == 422
    body = first.json()
    assert body == {
        "operation_id": envelope["operation_id"],
        "code": "REJECTED",
        "timestamp": body["timestamp"],
        "data": {"reason_code": "UNSUPPORTED_OPERATION"},
    }
    assert type(body["timestamp"]) is int and body["timestamp"] > 0
    reordered = json.dumps(dict(reversed(list(envelope.items()))), indent=2)
    replay = await client.post(PATH, content=reordered)
    assert replay.status_code == 422 and replay.json() == body
    receipts = await stored(callback_store, CallbackReceipt)
    assert len(receipts) == 1 and receipts[0].body == body
    assert receipts[0].compare_mode == "RAW" and receipts[0].effective_digest is None
    assert "Retry-After" not in first.headers


async def test_wms_receipt_does_not_infer_device_from_payload(client, callback_store, envelope):
    body = envelope | {"data": {"device_code": "UNTRUSTED-DEVICE"}}
    assert (await client.post(PATH, json=body)).status_code == 422
    async with callback_store() as session:
        unknown_device = list(
            await session.scalars(select(CallbackReceipt).filter_by(device_code=None))
        )
        assert len(unknown_device) == 1
        assert unknown_device[0].operation_id == envelope["operation_id"]


@pytest.mark.parametrize("changes", [{"timestamp": 2}, {"data": {"changed": True}}, {"extra": 1}])
async def test_changed_message_conflicts_without_replacing_receipt(
    client, callback_store, envelope, changes
):
    first = await client.post(PATH, json=envelope)
    conflict = await client.post(PATH, json=envelope | changes)
    assert conflict.status_code == 409 and conflict.json()["code"] == "CONFLICT"
    assert conflict.json()["operation_id"] == envelope["operation_id"]
    assert conflict.json()["data"] == {}
    assert (await client.post(PATH, json=envelope)).json() == first.json()
    assert len(await stored(callback_store, CallbackReceipt)) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"timestamp": None},
        {"timestamp": True},
        {"timestamp": -1},
        {"timestamp": 2**63},
        {"data": []},
    ],
)
async def test_invalid_envelope_rejection_is_persisted_and_replayed(
    client, callback_store, envelope, changes
):
    request = envelope | changes
    response = await client.post(PATH, json=request)
    assert response.status_code == 422
    assert response.json()["data"] == {"reason_code": "INVALID_EVIDENCE"}
    assert (await client.post(PATH, json=request)).json() == response.json()
    assert len(await stored(callback_store, CallbackReceipt)) == 1


async def test_operation_is_part_of_message_identity(client, callback_store, envelope):
    first = await client.post(PATH, json=envelope)
    second = await client.post(PATH, json=envelope | {"operation": "another.unknown@v1"})
    assert first.status_code == second.status_code == 422
    assert len(await stored(callback_store, CallbackReceipt)) == 2


async def test_operation_at_length_limit_is_stored_and_replayed(client, callback_store, envelope):
    envelope["operation"] = "o" * 256
    first = await client.post(PATH, json=envelope)
    assert first.status_code == 422
    assert first.json()["data"] == {"reason_code": "UNSUPPORTED_OPERATION"}
    replay = await client.post(PATH, json=envelope)
    assert replay.status_code == 422 and replay.json() == first.json()
    assert len(await stored(callback_store, CallbackReceipt)) == 1


@pytest.mark.parametrize(
    "raw",
    [
        b"not-json",
        b"\xff",
        b"[]",
        b' {"operation_id":"a","operation_id":"b"}',
        b'{"data":{"x":1,"x":2}}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":-Infinity}',
        b"{}",
        b'{"operation_id":"not-uuid"}',
        b'{"operation_id":"9af2751b-4d01-4203-88a7-649bbf69e810"}',
    ],
)
async def test_unassociated_invalid_requests_return_empty_400(client, callback_store, raw):
    response = await client.post(PATH, content=raw)
    assert response.status_code == 400 and response.content == b""
    assert await stored(callback_store, CallbackReceipt) == []
    snapshot = (await stored(callback_store, CallbackSnapshot))[0]
    assert base64.b64decode(snapshot.body_b64) == raw
    assert snapshot.http_status == 400


@pytest.mark.parametrize("operation", [None, 12, [], "bad\x00operation", "\ud800", "o" * 257])
async def test_unusable_operation_returns_rejection_without_receipt(
    client, callback_store, envelope, operation
):
    envelope["operation"] = operation
    response = await client.post(PATH, content=json.dumps(envelope))
    assert response.status_code == 422
    assert response.json()["data"] == {"reason_code": "INVALID_EVIDENCE"}
    assert await stored(callback_store, CallbackReceipt) == []


async def test_missing_operation_returns_rejection_without_receipt(
    client, callback_store, envelope
):
    del envelope["operation"]
    response = await client.post(PATH, json=envelope)
    assert response.status_code == 422
    assert response.json()["operation_id"] == envelope["operation_id"]
    assert await stored(callback_store, CallbackReceipt) == []


async def test_oversize_stream_is_bounded_and_snapshotted(client, callback_store):
    async def chunks():
        yield b"a" * 262144
        yield b"!"
        raise AssertionError("oversize body must not be drained")

    response = await client.post(PATH, content=chunks())
    assert response.status_code == 413 and response.content == b""
    assert await stored(callback_store, CallbackReceipt) == []
    snapshot = (await stored(callback_store, CallbackSnapshot))[0]
    assert len(snapshot.body_b64.encode("ascii")) <= 65536 and snapshot.truncated
    assert base64.b64decode(snapshot.body_b64).startswith(b"a")


async def test_body_at_limit_is_not_rejected_as_oversize(client, callback_store, envelope):
    raw = json.dumps(envelope).encode()
    raw += b" " * (262144 - len(raw))
    response = await client.post(PATH, content=raw)
    assert response.status_code == 422


async def test_concurrent_same_identity_has_one_replayable_receipt(
    client, callback_store, envelope
):
    responses = await asyncio.gather(*(client.post(PATH, json=envelope) for _ in range(4)))
    assert all(response.status_code == 422 for response in responses)
    assert all(response.json() == responses[0].json() for response in responses)
    receipts = await stored(callback_store, CallbackReceipt)
    assert len(receipts) == 1 and receipts[0].body == responses[0].json()


async def test_callback_has_no_rate_limit_and_snapshot_budget_does_not_change_ack(
    client, callback_store, envelope, monkeypatch
):
    monkeypatch.setattr(snapshots, "SNAPSHOTS_PER_MINUTE", 2)
    monkeypatch.setattr(snapshots, "monotonic", lambda: 100.0)
    responses = [await client.post(PATH, json=envelope) for _ in range(105)]
    assert all(response.status_code == 422 for response in responses)
    assert all(response.json() == responses[0].json() for response in responses)
    assert len(await stored(callback_store, CallbackSnapshot)) == 2
    monkeypatch.setattr(snapshots, "monotonic", lambda: 161.0)
    assert (await client.post(PATH, json=envelope)).status_code == 422
    assert len(await stored(callback_store, CallbackSnapshot)) == 3


async def test_snapshot_storage_failure_does_not_change_committed_ack(
    client, callback_store, envelope, monkeypatch, caplog
):
    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("snapshot unavailable")
        return callback_store()

    monkeypatch.setattr(app.state, "callback_session_factory", factory)
    response = await client.post(PATH, json=envelope)
    assert response.status_code == 422
    assert len(await stored(callback_store, CallbackReceipt)) == 1
    assert await stored(callback_store, CallbackSnapshot) == []

    records = [r for r in caplog.records if "callback snapshot failed" in r.getMessage()]
    assert records and records[0].component == "callback" and records[0].phase == "snapshot"
    assert "snapshot unavailable" not in caplog.text


async def test_snapshot_timeout_does_not_change_ack(client, callback_store, envelope, monkeypatch):
    from contextlib import asynccontextmanager

    calls = 0

    @asynccontextmanager
    async def hanging():
        await asyncio.Event().wait()
        yield

    def factory():
        nonlocal calls
        calls += 1
        return hanging() if calls == 2 else callback_store()

    monkeypatch.setattr(app.state, "callback_session_factory", factory)
    monkeypatch.setattr(snapshots, "SNAPSHOT_TIMEOUT_S", 0.01)
    response = await client.post(PATH, json=envelope)
    assert response.status_code == 422
    assert len(await stored(callback_store, CallbackReceipt)) == 1
    assert await stored(callback_store, CallbackSnapshot) == []


async def test_waiting_identity_can_take_over_after_winner_rolls_back(
    client, callback_store, envelope, wait_for_lock
):
    if callback_store.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL unique-index waiting behavior")
    from app.integrations.wms_callback import receive

    async with callback_store() as winner:
        await receive(winner, json.dumps(envelope).encode())
        pending = asyncio.create_task(client.post(PATH, json=envelope))
        try:
            await wait_for_lock(pending)
            assert not pending.done()
        finally:
            await winner.rollback()
        async with asyncio.timeout(5):
            response = await pending
    assert response.status_code == 422
    receipts = await stored(callback_store, CallbackReceipt)
    assert len(receipts) == 1 and receipts[0].body == response.json()


async def test_commit_failure_does_not_freeze_unavailable_response(
    client, callback_store, envelope
):
    engine = callback_store.kw["bind"]

    def fail_once(connection):
        event.remove(engine.sync_engine, "commit", fail_once)
        raise RuntimeError("commit unavailable")

    event.listen(engine.sync_engine, "commit", fail_once)
    response = await client.post(PATH, json=envelope)
    assert response.status_code == 503 and response.json()["code"] == "UNAVAILABLE"
    assert response.json()["operation_id"] == envelope["operation_id"]
    assert await stored(callback_store, CallbackReceipt) == []
    retried = await client.post(PATH, json=envelope)
    assert retried.status_code == 422 and len(await stored(callback_store, CallbackReceipt)) == 1


@pytest.mark.parametrize("has_identity", [True, False])
async def test_unexpected_receiver_error_uses_protocol_503(
    client, callback_store, envelope, monkeypatch, has_identity
):
    import app.controllers.wms_events as controller

    async def broken(*args):
        raise RuntimeError("unexpected receiver failure")

    monkeypatch.setattr(controller, "receive", broken)
    if not has_identity:
        del envelope["operation_id"]
    response = await client.post(PATH, json=envelope)
    assert response.status_code == 503
    if has_identity:
        assert response.json()["operation_id"] == envelope["operation_id"]
        assert response.json()["code"] == "UNAVAILABLE" and response.json()["data"] == {}
    else:
        assert response.content == b""
    assert await stored(callback_store, CallbackReceipt) == []
    assert (await stored(callback_store, CallbackSnapshot))[0].outcome == "error"


# ---- operation 注册:处理器契约(Accepted/Duplicate/Conflict/Rejected) ----

probe_table = Table(
    "callback_probe",
    MetaData(),
    Column("id", Integer, primary_key=True),
    Column("note", String(32)),
)
OPERATION = "example.probe@v1"


@pytest.fixture(params=["return", "raise"])
async def probe(callback_store, monkeypatch, request):
    """测试 operation:data.mode 决定处理器结果,业务行写进独立的 callback_probe 表。"""
    engine = callback_store.kw["bind"]
    async with engine.begin() as connection:
        await connection.run_sync(probe_table.create, checkfirst=True)
    monkeypatch.setattr(registry, "_registry", {})

    class ProbePayload(CallbackPayload):
        mode: Literal["accept", "duplicate", "conflict", "reject", "boom", "nan"]

    @callback_operation(OPERATION, data=ProbePayload)
    async def handler(ctx, payload):
        assert isinstance(payload, ProbePayload)
        mode = payload.mode
        if mode in {"accept", "reject", "boom"}:
            await ctx.session.execute(insert(probe_table).values(note=mode))
        if mode == "boom":
            raise RuntimeError("handler failure")
        result = {
            "accept": Accepted({"task": "t1"}),
            "duplicate": Duplicate({"task": "t1"}),
            "conflict": Conflict({"task": "t1"}),
            "reject": Rejected("NOT_ALLOWED"),
            "nan": Accepted({"value": float("nan")}),
        }[mode]
        if request.param == "raise":
            raise result
        return result

    try:
        yield
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(probe_table.drop, checkfirst=True)


async def probe_rows(factory):
    async with factory() as session:
        return list(await session.scalars(select(probe_table.c.note)))


@pytest.mark.parametrize(
    ("mode", "status", "code", "data", "rows"),
    [
        ("accept", 202, "RECEIVED", {"task": "t1"}, ["accept"]),
        ("duplicate", 200, "DUPLICATE", {"task": "t1"}, []),
        ("conflict", 409, "CONFLICT", {"task": "t1"}, []),
        ("reject", 422, "REJECTED", {"reason_code": "NOT_ALLOWED"}, []),
    ],
)
async def test_handler_outcomes_map_to_protocol_responses(
    client, callback_store, probe, envelope, mode, status, code, data, rows
):
    envelope |= {"operation": OPERATION, "data": {"mode": mode}}
    response = await client.post(PATH, json=envelope)
    assert response.status_code == status
    body = response.json()
    assert body["code"] == code and body["data"] == data
    assert body["operation_id"] == envelope["operation_id"]
    assert await probe_rows(callback_store) == rows  # Rejected 回滚了处理器写入
    [receipt] = await stored(callback_store, CallbackReceipt)
    assert receipt.status_code == status and receipt.body == body
    assert receipt.compare_mode == "EFFECTIVE"
    replay = await client.post(PATH, json=envelope)
    # 首次 RECEIVED 的重放报 DUPLICATE,其余确定响应原样重放
    expected = body | {"code": "DUPLICATE"} if code == "RECEIVED" else body
    assert replay.json() == expected
    assert replay.status_code == (200 if code == "RECEIVED" else status)
    assert await probe_rows(callback_store) == rows


@pytest.mark.parametrize("mode", ["boom", "nan"])
async def test_handler_failure_rolls_back_everything_and_returns_protocol_503(
    client, callback_store, probe, envelope, mode
):
    envelope |= {"operation": OPERATION, "data": {"mode": mode}}
    response = await client.post(PATH, json=envelope)
    assert response.status_code == 503 and response.json()["code"] == "UNAVAILABLE"
    assert response.json()["operation_id"] == envelope["operation_id"]
    assert await stored(callback_store, CallbackReceipt) == []
    assert await probe_rows(callback_store) == []


async def test_invalid_payload_is_rejected_with_reason_and_replayed(
    client, callback_store, probe, envelope
):
    envelope |= {"operation": OPERATION, "data": {"mode": "nonsense"}}
    first = await client.post(PATH, json=envelope)
    assert first.status_code == 422
    assert first.json()["data"] == {"reason_code": "INVALID_EVIDENCE"}
    assert (await client.post(PATH, json=envelope)).json() == first.json()
    [receipt] = await stored(callback_store, CallbackReceipt)
    assert receipt.compare_mode == "RAW"


async def test_redundant_fields_replay_but_changed_timestamp_conflicts(
    client, callback_store, probe, envelope
):
    envelope |= {"operation": OPERATION, "data": {"mode": "accept"}}
    first = await client.post(PATH, json=envelope)
    assert first.status_code == 202
    padded = envelope | {"data": {"mode": "accept", "extra": 1}}
    replay = await client.post(PATH, json=padded)
    assert replay.status_code == 200 and replay.json()["code"] == "DUPLICATE"
    assert (await client.post(PATH, json=envelope | {"timestamp": 2})).status_code == 409
    changed = envelope | {"data": {"mode": "reject"}}
    assert (await client.post(PATH, json=changed)).status_code == 409
    broken = envelope | {"data": {"mode": "nonsense"}}
    assert (await client.post(PATH, json=broken)).status_code == 409
    assert await probe_rows(callback_store) == ["accept"]


async def test_rejection_saved_before_registration_is_replayed_after(
    client, callback_store, envelope, monkeypatch
):
    monkeypatch.setattr(registry, "_registry", {})
    envelope["operation"] = OPERATION
    envelope["data"] = {"mode": "accept"}
    first = await client.post(PATH, json=envelope)
    assert first.json()["data"] == {"reason_code": "UNSUPPORTED_OPERATION"}

    @callback_operation(OPERATION, data=CallbackPayload)
    async def late(ctx, payload):
        raise AssertionError("an already rejected message must not run the handler")

    assert (await client.post(PATH, json=envelope)).json() == first.json()


async def test_effective_receipt_without_its_handler_is_unavailable_not_unknown(
    client, callback_store, probe, envelope, monkeypatch
):
    envelope |= {"operation": OPERATION, "data": {"mode": "accept"}}
    assert (await client.post(PATH, json=envelope)).status_code == 202
    monkeypatch.setattr(registry, "_registry", {})
    padded = envelope | {"data": {"mode": "accept", "extra": 1}}
    unavailable = await client.post(PATH, json=padded)
    assert unavailable.status_code == 503 and unavailable.json()["code"] == "UNAVAILABLE"
    assert (await client.post(PATH, json=envelope)).status_code == 200
    assert len(await stored(callback_store, CallbackReceipt)) == 1


async def test_concurrent_same_identity_runs_the_handler_once(
    client, callback_store, probe, envelope
):
    envelope |= {"operation": OPERATION, "data": {"mode": "accept"}}
    responses = await asyncio.gather(*(client.post(PATH, json=envelope) for _ in range(6)))
    assert sorted(response.status_code for response in responses) == [200] * 5 + [202]
    assert await probe_rows(callback_store) == ["accept"]
    assert len(await stored(callback_store, CallbackReceipt)) == 1


@pytest.fixture
def example(callback_store, monkeypatch):
    monkeypatch.setattr(registry, "_registry", {})
    callback_operation(EXAMPLE_OPERATION, data=EchoPayload)(echo)


@pytest.mark.parametrize(
    ("outcome", "status", "code", "data"),
    [
        ("accept", 202, "RECEIVED", {"echo": "hi"}),
        ("duplicate", 200, "DUPLICATE", {"echo": "hi"}),
        ("conflict", 409, "CONFLICT", {"echo": "hi"}),
        ("reject", 422, "REJECTED", {"reason_code": "EXAMPLE_REJECTED"}),
    ],
)
async def test_example_operation_demonstrates_every_outcome(
    client, example, envelope, outcome, status, code, data
):
    envelope |= {"operation": "example.echo@v1", "data": {"message": "hi", "outcome": outcome}}
    response = await client.post(PATH, json=envelope)
    assert response.status_code == status
    assert response.json()["code"] == code and response.json()["data"] == data


@pytest.mark.parametrize(
    "data", [{}, {"message": ""}, {"message": "x" * 201}, {"message": "hi", "outcome": "nope"}]
)
async def test_example_operation_rejects_invalid_payload(client, example, envelope, data):
    envelope |= {"operation": "example.echo@v1", "data": data}
    response = await client.post(PATH, json=envelope)
    assert response.status_code == 422
    assert response.json()["data"] == {"reason_code": "INVALID_EVIDENCE"}


def test_swagger_request_examples_come_from_registered_operations():
    media = app.openapi()["paths"]["/api/v1/wms/events"]["post"]["requestBody"]["content"]
    examples = media["application/json"]["examples"]
    value = examples[EXAMPLE_OPERATION]["value"]
    assert (
        value["operation"] == EXAMPLE_OPERATION
        and value["data"] == EchoPayload.model_json_schema()["examples"][0]
    )


async def test_raised_rejection_rolls_back_rows_written_before_it(
    client, callback_store, probe, envelope
):
    @callback_operation("example.raising@v1", data=CallbackPayload)
    async def raising(ctx, payload):
        await ctx.session.execute(insert(probe_table).values(note="before"))
        raise Rejected("TOO_LATE")

    envelope |= {"operation": "example.raising@v1", "data": {}}
    response = await client.post(PATH, json=envelope)
    assert response.status_code == 422
    assert response.json()["data"] == {"reason_code": "TOO_LATE"}
    assert await probe_rows(callback_store) == []
