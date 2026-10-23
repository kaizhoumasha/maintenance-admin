import logging

import pytest
from sqlalchemy import event, select

from app.controllers import ecs_callback as ecs_controller
from app.core.identifiers import uuid7
from app.integrations.ecs_callback import registry
from app.integrations.ecs_callback.receiver import MAX_BODY_BYTES
from app.integrations.ecs_callback.registry import ecs_event
from app.integrations.wms_callback import snapshots
from app.models import CallbackReceipt, CallbackSnapshot

PATH = "/api/v1/callback/event"
WMS_PATH = "/api/v1/wms/events"
ACK = {"code": 200, "message": "ACK"}


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(registry, "_registry", {})


def body(**changes):
    return {"device_code": "D1", "event_type": "SCAN_COMPLETED", "timestamp": 1} | changes


async def rows(factory, model):
    async with factory() as session:
        return list(await session.scalars(select(model).order_by(model.id)))


async def test_valid_event_is_acked(client, callback_store):
    calls = []

    @ecs_event("SCAN_COMPLETED")
    async def handler(ctx, data):
        calls.append(ctx.device_code)

    response = await client.post(PATH, json=body())
    assert response.status_code == 200 and response.json() == ACK
    assert calls == ["D1"]


async def test_bad_envelope_returns_issues_without_echoing_values(client, callback_store):
    response = await client.post(PATH, json=body(device_code="SECRET-DEV", timestamp="x"))
    assert response.status_code == 400
    assert response.json()["error_detail"]["issues"]
    assert "SECRET-DEV" not in response.text


async def test_oversize_is_413_with_json_body(client, callback_store):
    response = await client.post(PATH, content=b" " * (MAX_BODY_BYTES + 1))
    assert response.status_code == 413
    assert response.json() == {"code": 413, "message": "PAYLOAD_TOO_LARGE"}
    assert await rows(callback_store, CallbackReceipt) == []


async def test_commit_failure_returns_503_and_logs_error_outcome(client, callback_store, caplog):
    engine = callback_store.kw["bind"]

    def fail_once(connection):
        event.remove(engine.sync_engine, "commit", fail_once)
        raise RuntimeError("commit unavailable")

    event.listen(engine.sync_engine, "commit", fail_once)
    caplog.set_level(logging.INFO)
    response = await client.post(PATH, json=body())
    assert response.status_code == 503
    assert response.json() == {"code": 503, "message": "TEMPORARILY_UNAVAILABLE"}
    assert await rows(callback_store, CallbackReceipt) == []
    finals = [r for r in caplog.records if hasattr(r, "outcome")]
    assert [r.outcome for r in finals] == ["error"]
    assert finals[0].event_type == "'SCAN_COMPLETED'" and finals[0].device_code == "'D1'"
    assert finals[0].status == 503 and "outcome=error" in finals[0].getMessage()


@pytest.mark.parametrize("raw", [None, b"\xff"])
async def test_receive_failure_log_identity_is_best_effort(
    client, callback_store, caplog, monkeypatch, raw
):
    async def broken(session, data):
        raise RuntimeError("secret-commit-detail")

    monkeypatch.setattr(ecs_controller, "receive_ecs_event", broken)
    caplog.set_level(logging.INFO)
    response = await client.post(PATH, content=raw) if raw else await client.post(PATH, json=body())
    assert response.status_code == 503
    [final] = [r for r in caplog.records if hasattr(r, "outcome")]
    assert final.outcome == "error"
    if raw:
        assert final.event_type is None and final.device_code is None
    else:
        assert final.event_type == "'SCAN_COMPLETED'" and final.device_code == "'D1'"
    assert all("secret-commit-detail" not in r.getMessage() for r in caplog.records)
    assert all(r.exc_info is None for r in caplog.records)


async def test_final_log_has_rendered_fields(client, callback_store, caplog):
    caplog.set_level(logging.INFO)
    await client.post(PATH, json=body())
    [record] = [r for r in caplog.records if hasattr(r, "outcome")]
    assert record.outcome == "unregistered" and record.event_type == "'SCAN_COMPLETED'"
    assert record.device_code == "'D1'" and len(record.digest) == 8
    assert isinstance(record.duration_ms, int | float)


async def test_snapshot_is_recorded_with_ecs_source_and_wms_quota_unaffected(
    client, callback_store, monkeypatch
):
    monkeypatch.setattr(snapshots, "SNAPSHOTS_PER_MINUTE", 2)
    for n in range(3):
        assert (await client.post(PATH, json=body(timestamp=n))).status_code == 200
    wms = dict(operation_id=str(uuid7()), operation="x@v1", timestamp=1, data={})
    await client.post(WMS_PATH, json=wms)
    sources = [s.source for s in await rows(callback_store, CallbackSnapshot)]
    assert sources.count("ecs") == 2 and sources.count(None) == 1


async def test_wms_events_endpoint_cannot_reach_ecs_handlers(client, callback_store):
    calls = []

    @ecs_event("SCAN_COMPLETED")
    async def handler(ctx, data):
        calls.append(1)

    wms = dict(operation_id=str(uuid7()), operation="ecs.event", timestamp=1, data={})
    response = await client.post(WMS_PATH, json=wms)
    assert response.status_code == 422
    assert response.json()["data"] == {"reason_code": "UNSUPPORTED_OPERATION"}
    assert calls == []
