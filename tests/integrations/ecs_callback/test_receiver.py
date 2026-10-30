import json
import logging
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.core.strict_json import canonical_digest
from app.integrations.ecs_callback import registry
from app.integrations.ecs_callback.envelope import EcsEventEnvelope
from app.integrations.ecs_callback.receiver import (
    ACK,
    MAX_BODY_BYTES,
    MAX_DIAGNOSTIC_BYTES,
    PREVIEW_BYTES,
    event_identity,
    receive_ecs_event,
)
from app.integrations.ecs_callback.registry import ecs_event
from app.integrations.wms_callback.operation import CallbackPayload
from app.models import CallbackReceipt, CallbackSnapshot
from tests.helpers import ecs_binding, legacy_ecs_receipt

BASE = {"device_code": "D1", "event_type": "SCAN_COMPLETED", "timestamp": 1}


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(registry, "_registry", {})


async def unexpected_handler(ctx, payload):
    raise AssertionError("validation or replay must stop before execution")


async def receive(factory, raw: bytes):
    async with factory() as session, session.begin():
        return await receive_ecs_event(session, raw, resolve_binding=ecs_binding)


async def receipt_count(factory) -> int:
    async with factory() as session:
        return await session.scalar(select(func.count()).select_from(CallbackReceipt))


@pytest.mark.parametrize("registered", [False, True])
async def test_receipts_are_searchable_by_original_device_without_truncation(
    callback_store, registered
):
    import hashlib

    if registered:

        async def accept(ctx, payload):
            pass

        ecs_event("SCAN_COMPLETED", device_role="SCAN1", data=CallbackPayload)(accept)
    long_code = "设备-" + "".join(hashlib.sha256(str(n).encode()).hexdigest() for n in range(128))
    for code in ("D1", long_code):
        response = await receive(callback_store, json.dumps({**BASE, "device_code": code}).encode())
        assert response.status_code == 200
    before = receipt_values(await receipts(callback_store))
    replay = await receive(callback_store, json.dumps(BASE).encode())
    assert replay.outcome == "duplicate"
    assert receipt_values(await receipts(callback_store)) == before
    async with callback_store() as session:
        for code in ("D1", long_code):
            matches = list(
                await session.scalars(select(CallbackReceipt).filter_by(device_code=code))
            )
            assert len(matches) == 1 and matches[0].device_code == code


@pytest.mark.parametrize("code", ["D\x00", "D\ud800"])
async def test_nonpersistable_device_text_is_rejected_before_receipt(callback_store, code):
    response = await receive(callback_store, json.dumps({**BASE, "device_code": code}).encode())
    assert response.status_code == 400
    assert response.body["error_detail"]["issues"] == [
        {"field": "device_code", "code": "INVALID_VALUE"}
    ]
    assert await receipt_count(callback_store) == 0


async def test_legacy_receipt_replay_precedes_new_device_storage_validation(callback_store):
    body = {**BASE, "device_code": "D\x00"}
    await legacy_ecs_receipt(callback_store, body)
    before = receipt_values(await receipts(callback_store))
    response = await receive(callback_store, json.dumps(body).encode())
    assert (response.status_code, response.body, response.outcome) == (200, ACK, "duplicate")
    assert receipt_values(await receipts(callback_store)) == before


async def test_oversize_body_is_413_with_code_and_message(callback_store):
    resp = await receive(callback_store, b" " * (MAX_BODY_BYTES + 1))
    assert (resp.status_code, resp.outcome) == (413, "too_large")
    assert resp.body == {"code": 413, "message": "PAYLOAD_TOO_LARGE"}


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\xff\xfe",
        b"[]",
        b'{"a":1,"a":2}',
        b'{"device_code":"D1","event_type":"E","timestamp":1,"data":{"n":1e400}}',
    ],
)
async def test_bad_json_variants_are_400_invalid_json(callback_store, raw):
    resp = await receive(callback_store, raw)
    assert (resp.status_code, resp.outcome) == (400, "invalid")
    assert resp.body["code"] == 400
    assert resp.body["message"] == "INVALID_ENVELOPE"
    assert resp.body["error_detail"]["issues"] == [{"field": "$", "code": "INVALID_JSON"}]
    assert await receipt_count(callback_store) == 0


async def test_envelope_errors_are_400_without_receipt(callback_store):
    body = {"device_code": "SECRET-DEV", "timestamp": 1}
    resp = await receive(callback_store, json.dumps(body).encode())
    assert resp.status_code == 400
    assert {"field": "event_type", "code": "FIELD_REQUIRED"} in resp.body["error_detail"]["issues"]
    assert "SECRET-DEV" not in json.dumps(resp.body)
    assert await receipt_count(callback_store) == 0


def test_extra_fields_do_not_change_identity():
    expected = canonical_digest({**BASE, "data": {}})
    for extra in (
        {},
        {"is_debug": False},
        {"is_debug": True},
        {"is_debug": "anything"},
        {"junk": "x"},
        {"data": {}},
    ):
        assert event_identity(EcsEventEnvelope.model_validate({**BASE, **extra})) == expected


async def test_extra_fields_do_not_split_event_identity(callback_store):
    calls = []

    async def handler(ctx, data):
        calls.append(ctx.receipt_id)

    ecs_event("SCAN_COMPLETED", device_role="SCAN1", data=CallbackPayload)(handler)
    for extra in (
        {},
        {"is_debug": False},
        {"is_debug": True},
        {"is_debug": None},
        {"is_debug": "true", "extra": [1]},
    ):
        response = await receive(callback_store, json.dumps({**BASE, **extra}).encode())
        assert (response.status_code, response.body) == (200, ACK)
    assert len(calls) == 1
    assert await receipt_count(callback_store) == 1


def receipt_values(rows):
    return [
        {column.name: getattr(row, column.name) for column in CallbackReceipt.__table__.columns}
        for row in rows
    ]


@pytest.mark.parametrize("original_extra", [{}, {"is_debug": False}, {"is_debug": True}])
async def test_legacy_receipt_replay_never_invokes_handler(callback_store, original_extra):
    original = {**BASE, **original_extra}
    await legacy_ecs_receipt(callback_store, original)
    before = receipt_values(await receipts(callback_store))
    calls = []

    async def handler(ctx, data):
        calls.append(ctx.receipt_id)

    ecs_event("SCAN_COMPLETED", device_role="SCAN1", data=CallbackPayload)(handler)
    for extra in (
        original_extra,
        {"is_debug": True},
        {"is_debug": False},
        {"is_debug": "ignored", "trace": 1},
    ):
        response = await receive(callback_store, json.dumps({**BASE, **extra}).encode())
        assert (response.status_code, response.body, response.outcome) == (200, ACK, "duplicate")
        assert response.digest == before[0]["operation_id"]
    assert calls == []
    assert receipt_values(await receipts(callback_store)) == before


@pytest.mark.parametrize("older_debug,equal_time", [(False, False), (True, False), (True, True)])
async def test_two_legacy_receipts_do_not_create_a_third_execution(
    callback_store, older_debug, equal_time
):
    for debug in (True, False):
        created_at = datetime(2026, 1, 1 if equal_time or debug == older_debug else 2, tzinfo=UTC)
        await legacy_ecs_receipt(callback_store, {**BASE, "is_debug": debug}, created_at=created_at)
    before = receipt_values(await receipts(callback_store))
    ecs_event("SCAN_COMPLETED", device_role="SCAN1", data=CallbackPayload)(unexpected_handler)
    response = await receive(callback_store, json.dumps(BASE).encode())
    assert (response.status_code, response.body, response.outcome) == (200, ACK, "duplicate")
    expected = before[0 if equal_time or older_debug else 1]
    assert response.digest == expected["operation_id"]
    assert receipt_values(await receipts(callback_store)) == before


async def test_legacy_receipt_precedes_new_handler_payload_validation(callback_store):
    await legacy_ecs_receipt(callback_store, BASE)

    class Model(CallbackPayload):
        barcode: str

    ecs_event("SCAN_COMPLETED", device_role="SCAN1", data=Model)(unexpected_handler)
    response = await receive(callback_store, json.dumps({**BASE, "is_debug": True}).encode())
    assert (response.status_code, response.body, response.outcome) == (200, ACK, "duplicate")
    assert await receipt_count(callback_store) == 1


async def test_registered_data_failure_is_400_with_data_path(callback_store):
    class Model(CallbackPayload):
        bin_code: str

    ecs_event("SCAN_COMPLETED", data=Model, device_role="SCAN1")(unexpected_handler)
    resp = await receive(callback_store, json.dumps({**BASE, "data": {}}).encode())
    assert resp.status_code == 400
    assert resp.body["error_detail"]["issues"] == [
        {"field": "data.bin_code", "code": "FIELD_REQUIRED"}
    ]
    assert await receipt_count(callback_store) == 0


async def test_existing_receipt_short_circuits_before_data_validation(callback_store):
    class Model(CallbackPayload):
        bin_code: str

    ecs_event("SCAN_COMPLETED", data=Model, device_role="SCAN1")(unexpected_handler)
    envelope = EcsEventEnvelope.model_validate(BASE)
    identity = event_identity(envelope)
    async with callback_store() as session, session.begin():
        session.add(
            CallbackReceipt(
                operation="ecs.event",
                operation_id=identity,
                raw_digest="x",
                compare_mode="RAW",
                status_code=200,
                body=dict(ACK),
                resp_timestamp=1,
                created_at=datetime.now(UTC),
            )
        )
    resp = await receive(callback_store, json.dumps(BASE).encode())
    assert (resp.status_code, resp.body, resp.outcome) == (200, ACK, "duplicate")


EVENT = json.dumps({**BASE, "data": {"barcode": "B1"}}).encode()


async def receipts(factory) -> list[CallbackReceipt]:
    async with factory() as session:
        return list(
            (await session.scalars(select(CallbackReceipt).order_by(CallbackReceipt.id))).all()
        )


async def test_new_event_gets_ack_and_one_receipt(callback_store):
    resp = await receive(callback_store, EVENT)
    assert (resp.status_code, resp.body, resp.outcome) == (200, ACK, "unregistered")
    [receipt] = await receipts(callback_store)
    assert receipt.operation == "ecs.event"
    assert len(receipt.operation_id) == 64 and receipt.operation_id == resp.digest
    assert (receipt.compare_mode, receipt.effective_digest) == ("RAW", None)
    assert receipt.body == ACK and receipt.resp_timestamp > 0
    assert receipt.raw_digest == canonical_digest(json.loads(EVENT))


async def test_replay_returns_same_ack_and_handler_runs_once(callback_store):
    calls = []

    async def handler(ctx, data):
        calls.append(data)

    ecs_event("SCAN_COMPLETED", device_role="SCAN1", data=CallbackPayload)(handler)
    first = await receive(callback_store, EVENT)
    second = await receive(callback_store, EVENT)
    assert (first.body, first.outcome) == (ACK, "received")
    assert (second.body, second.outcome) == (ACK, "duplicate")
    assert len(calls) == 1 and await receipt_count(callback_store) == 1


async def test_replay_after_handler_validation_tightened_still_acks(callback_store):
    raw = json.dumps(BASE).encode()
    assert (await receive(callback_store, raw)).outcome == "unregistered"

    class Model(CallbackPayload):
        barcode: str

    ecs_event("SCAN_COMPLETED", data=Model, device_role="SCAN1")(unexpected_handler)
    resp = await receive(callback_store, raw)
    assert (resp.status_code, resp.body, resp.outcome) == (200, ACK, "duplicate")


async def test_handler_exception_rolls_back_receipt_and_business_write(callback_store):
    async def handler(ctx, data):
        ctx.session.add(
            CallbackSnapshot(
                request_id="r",
                received_at=datetime.now(UTC),
                body_b64="",
                truncated=False,
                http_status=200,
                outcome="x",
            )
        )
        await ctx.session.flush()
        raise RuntimeError("boom")

    ecs_event("SCAN_COMPLETED", device_role="SCAN1", data=CallbackPayload)(handler)
    async with callback_store() as session:
        with pytest.raises(RuntimeError, match="boom"):
            async with session.begin():
                await receive_ecs_event(session, EVENT, resolve_binding=ecs_binding)
    assert await receipt_count(callback_store) == 0
    async with callback_store() as session:
        assert await session.scalar(select(func.count()).select_from(CallbackSnapshot)) == 0


async def test_unregistered_event_is_acked_with_full_event_in_diagnostic(callback_store):
    resp = await receive(callback_store, EVENT)
    assert (resp.status_code, resp.outcome) == (200, "unregistered")
    [receipt] = await receipts(callback_store)
    [note] = receipt.diagnostic
    assert note["reason_code"] == "UNREGISTERED_EVENT_TYPE"
    assert note["data"] == {
        "device_code": "D1",
        "event_type": "SCAN_COMPLETED",
        "timestamp": 1,
        "data": {"barcode": "B1"},
    }


async def test_oversized_unregistered_event_keeps_digest_size_preview_only(callback_store):
    body = {**BASE, "data": {"blob": "x" * 40 * 1024}}
    await receive(callback_store, json.dumps(body).encode())
    [receipt] = await receipts(callback_store)
    [note] = receipt.diagnostic
    assert set(note["data"]) == {"digest", "size", "preview"}
    assert len(note["data"]["preview"]) == PREVIEW_BYTES and note["data"]["size"] > 40 * 1024
    assert len(json.dumps(receipt.diagnostic).encode()) <= MAX_DIAGNOSTIC_BYTES
    assert "x" * 2048 not in json.dumps(receipt.diagnostic)


async def test_long_device_code_with_tiny_data_stays_within_cap(callback_store):
    long = "D" * 200_000
    await receive(callback_store, json.dumps({**BASE, "device_code": long, "data": {}}).encode())
    [receipt] = await receipts(callback_store)
    [note] = receipt.diagnostic
    assert note["device_code"] == "D" * 128
    assert len(json.dumps(receipt.diagnostic).encode()) <= MAX_DIAGNOSTIC_BYTES
    assert long not in json.dumps(receipt.diagnostic)


async def test_log_alert_renders_untrusted_fields_bounded(callback_store, caplog):
    caplog.set_level(logging.WARNING)
    code = "A\nFORGED " + "D" * 5000
    await receive(
        callback_store, json.dumps({**BASE, "device_code": code, "event_type": code}).encode()
    )
    [alert] = [r for r in caplog.records if hasattr(r, "diagnostic")]
    assert (alert.operation, len(alert.operation_id)) == ("ecs.event", 64)
    assert "data" not in alert.diagnostic
    for key in ("device_code", "event_type"):
        assert len(alert.diagnostic[key]) <= 70 and "\n" not in alert.diagnostic[key]
        assert alert.diagnostic[key].startswith("'A\\nFORGED")


@pytest.mark.parametrize("fail", [False, True])
async def test_ctx_note_is_persisted_and_alert_follows_commit(callback_store, caplog, fail):
    async def handler(ctx, data):
        ctx.note("X", k=1)
        if fail:
            raise RuntimeError("boom")

    ecs_event("SCAN_COMPLETED", device_role="SCAN1", data=CallbackPayload)(handler)
    caplog.set_level(logging.WARNING)

    def alerts():
        return [r for r in caplog.records if hasattr(r, "diagnostic")]

    async with callback_store() as session:
        try:
            async with session.begin():
                await receive_ecs_event(session, EVENT, resolve_binding=ecs_binding)
                assert alerts() == []  # 提交前不输出
        except RuntimeError:
            assert fail
    if fail:
        assert alerts() == [] and await receipt_count(callback_store) == 0
    else:
        [alert] = alerts()
        assert alert.diagnostic == {"reason_code": "X", "k": 1}
        assert (await receipts(callback_store))[0].diagnostic == [{"reason_code": "X", "k": 1}]


def _errors(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_oversized_unregistered_event_logs_error_without_data(callback_store, caplog):
    caplog.set_level(logging.INFO)
    await receive(callback_store, EVENT)
    assert _errors(caplog) == []
    body = {**BASE, "timestamp": 2, "data": {"blob": "SECRETDATA" * 5000}}
    await receive(callback_store, json.dumps(body).encode())
    [record] = _errors(caplog)
    assert "digest=" in record.getMessage() and "SECRETDATA" * 3 not in record.getMessage()
    assert all("SECRETDATA" * 3 not in r.getMessage() for r in caplog.records)


async def test_alert_renders_every_string_value_of_handler_notes(callback_store, caplog):
    async def handler(ctx, data):
        ctx.note("X", detail="Z" * 5000, n=1)

    ecs_event("SCAN_COMPLETED", device_role="SCAN1", data=CallbackPayload)(handler)
    caplog.set_level(logging.WARNING)
    await receive(callback_store, EVENT)
    [alert] = [r for r in caplog.records if hasattr(r, "diagnostic")]
    assert alert.diagnostic["reason_code"] == "X" and alert.diagnostic["n"] == 1
    assert len(alert.diagnostic["detail"]) <= 70
    assert (await receipts(callback_store))[0].diagnostic[0]["detail"] == "Z" * 5000


async def test_cap_applies_to_persisted_list_at_boundary(callback_store):
    def sized(pad: int) -> bytes:
        return json.dumps({**BASE, "timestamp": pad, "data": {"p": "x" * pad}}).encode()

    def persisted_size(pad: int) -> int:
        event = {
            "device_code": "D1",
            "event_type": "SCAN_COMPLETED",
            "timestamp": pad,
            "data": {"p": "x" * pad},
        }
        note = {
            "reason_code": "UNREGISTERED_EVENT_TYPE",
            "device_code": "D1",
            "event_type": "SCAN_COMPLETED",
            "data": event,
        }
        return len(json.dumps([note]).encode())

    # timestamp 随 pad 变化,位数变化会让大小不单调;找到恰好等于上限的 pad
    pad = next(p for p in range(30_000, 33_000) if persisted_size(p) == MAX_DIAGNOSTIC_BYTES)
    for p, reduced in ((pad, False), (pad + 1, True)):
        assert (persisted_size(p) > MAX_DIAGNOSTIC_BYTES) == reduced
        await receive(callback_store, sized(p))
    stored = {r.operation_id: r.diagnostic for r in await receipts(callback_store)}
    assert len(stored) == 2
    for diagnostic in stored.values():
        assert len(json.dumps(diagnostic).encode()) <= MAX_DIAGNOSTIC_BYTES
    assert sorted("digest" in d[0]["data"] for d in stored.values()) == [False, True]
