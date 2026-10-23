import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.integrations.ecs_callback import registry
from app.integrations.ecs_callback.envelope import EcsEventEnvelope
from app.integrations.ecs_callback.receiver import (
    ACK,
    MAX_BODY_BYTES,
    event_identity,
    receive_ecs_event,
)
from app.integrations.ecs_callback.registry import ecs_event
from app.integrations.wms_callback.operation import CallbackPayload
from app.models import CallbackReceipt

BASE = {"device_code": "D1", "event_type": "SCAN_COMPLETED", "timestamp": 1}


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(registry, "_registry", {})


async def receive(factory, raw: bytes):
    async with factory() as session, session.begin():
        return await receive_ecs_event(session, raw)


async def receipt_count(factory) -> int:
    async with factory() as session:
        return await session.scalar(select(func.count()).select_from(CallbackReceipt))


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


def test_identity_ignores_extra_top_level_fields_and_default_is_debug():
    def ident(**extra):
        return event_identity(EcsEventEnvelope.model_validate({**BASE, **extra}))

    base = ident()
    assert ident(is_debug=False) == base
    assert ident(data={}) == base
    assert ident(junk="x") == base
    assert ident(is_debug=True) != base


async def test_registered_data_failure_is_400_with_data_path(callback_store):
    class Model(CallbackPayload):
        bin_code: str

    ecs_event("SCAN_COMPLETED", data=Model)(lambda ctx, data: None)
    resp = await receive(callback_store, json.dumps({**BASE, "data": {}}).encode())
    assert resp.status_code == 400
    assert resp.body["error_detail"]["issues"] == [
        {"field": "data.bin_code", "code": "FIELD_REQUIRED"}
    ]
    assert await receipt_count(callback_store) == 0


async def test_existing_receipt_short_circuits_before_data_validation(callback_store):
    class Model(CallbackPayload):
        bin_code: str

    ecs_event("SCAN_COMPLETED", data=Model)(lambda ctx, data: None)
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
