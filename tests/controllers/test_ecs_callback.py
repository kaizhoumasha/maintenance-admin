import logging
from dataclasses import asdict

import pytest
from sqlalchemy import event, select

from app.controllers import ecs_callback as ecs_controller
from app.core.config import get_settings
from app.core.identifiers import uuid7
from app.integrations.ecs_callback import registry
from app.integrations.ecs_callback.receiver import MAX_BODY_BYTES
from app.integrations.ecs_callback.registry import ecs_event
from app.integrations.wms_callback import snapshots
from app.integrations.wms_callback.operation import CallbackPayload
from app.main import app
from app.models import EcsHttpExchange, IntegrationExchange, IntegrationRecord
from tests.helpers import ecs_binding, legacy_ecs_receipt, read_diagnostic_snapshots, read_receipts

PATH = "/api/v1/callback/event"
WMS_PATH = "/api/v1/wms/events"
ACK = {"code": 200, "message": "ACK"}


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(registry, "_registry", {})
    monkeypatch.setattr(app.state, "ecs_event_binding_resolver", ecs_binding)


def body(**changes):
    return {"device_code": "D1", "event_type": "SCAN_COMPLETED", "timestamp": 1} | changes


async def rows(factory, model):
    async with factory() as session:
        if model is IntegrationExchange:
            return await read_receipts(session)
        if model is IntegrationRecord:
            return await read_diagnostic_snapshots(session)
        return list(await session.scalars(select(model).order_by(model.id)))


async def test_valid_event_is_acked(callback_client, callback_store):
    calls = []

    @ecs_event("SCAN_COMPLETED", device_role="SCAN1", data=CallbackPayload)
    async def handler(ctx, data):
        calls.append(ctx.device_code)

    response = await callback_client.post(PATH, json=body())
    assert response.status_code == 200 and response.json() == ACK
    assert calls == ["D1"]


async def test_runtime_scan_is_acked_and_applied_once(callback_client, callback_store, monkeypatch):
    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", True)
    calls = []

    @ecs_event("SCAN_COMPLETED", device_role="SCAN1", data=CallbackPayload)
    async def handler(ctx, data):
        calls.append(ctx.receipt_id)

    scan = body(device_code=f"SCAN-{uuid7()}")
    first = await callback_client.post(PATH, json=scan)
    repeat = await callback_client.post(PATH, json=scan)

    assert first.status_code == repeat.status_code == 200
    assert first.json() == ACK and repeat.content == first.content
    [receipt_id] = calls
    async with callback_store() as session:
        exchanges = list(
            await session.scalars(
                select(EcsHttpExchange).where(EcsHttpExchange.receipt_id == receipt_id)
            )
        )
        assert len(exchanges) == 2
        assert all(exchange.event_type == "SCAN_COMPLETED" for exchange in exchanges)


@pytest.mark.parametrize(
    ("event_type", "projected_type"),
    [("UNKNOWN" + chr(0) + "EVENT", None), ("未注册事件", "未注册事件")],
)
async def test_unregistered_event_keeps_ack_and_original_bytes_with_safe_type_projection(
    callback_client, callback_store, event_type, projected_type
):
    import json

    from tests.helpers import read_history_body

    raw = json.dumps(body(event_type=event_type)).encode()
    first = await callback_client.post(PATH, content=raw)
    replay = await callback_client.post(PATH, content=raw)
    assert first.status_code == replay.status_code == 200
    assert first.json() == ACK and replay.content == first.content
    async with callback_store() as session:
        receipts = list(
            await session.scalars(
                select(IntegrationExchange).where(IntegrationExchange.role == "RECEIPT")
            )
        )
        assert len(receipts) == 1
        receipt = receipts[0]
        assert receipt.message_type == projected_type
        assert receipt.diagnostic[0]["event_type"] == event_type
        assert await read_history_body(session, receipt.request_record_id) == raw


async def test_pure_replay_observation_failure_preserves_protocol_reply(
    callback_client, callback_store, monkeypatch
):
    from app.integrations import history
    from app.models import IntegrationExchange
    from tests.helpers import read_history_body

    calls = []

    @ecs_event("SCAN_COMPLETED", device_role="SCAN1", data=CallbackPayload)
    async def handler(ctx, data):
        calls.append(ctx.receipt_id)

    first = await callback_client.post(PATH, json=body())
    assert first.status_code == 200
    failures = []
    original = history.write_record

    async def unavailable(session, *, draft):
        if getattr(draft, "evidence_class", None) == "DIAGNOSTIC":
            failures.append(draft.record_kind)
            raise history.EvidencePersistenceError("diagnostic unavailable")
        return await original(session, draft=draft)

    monkeypatch.setattr(history, "write_record", unavailable)
    replay = await callback_client.post(PATH, json=body())
    assert replay.status_code == 200
    assert replay.content == first.content
    assert len(calls) == 1
    assert failures
    async with callback_store() as session:
        root = await session.scalar(
            select(IntegrationExchange).where(
                IntegrationExchange.role == "RECEIPT", IntegrationExchange.operation == "ecs.event"
            )
        )
        assert root.receipt_id == calls[0]
        assert await read_history_body(session, root.current_response_record_id) == first.content


async def test_validated_event_is_applied_once_on_replay(callback_client, callback_store):
    class ScanData(CallbackPayload):
        barcode: str

    applied = []

    @ecs_event("SCAN_COMPLETED", data=ScanData, device_role="SCAN1")
    async def handler(ctx, data):
        applied.append(data)
        ctx.note("SCAN_APPLIED", barcode=data.barcode)

    for _ in range(2):
        response = await callback_client.post(PATH, json=body(data={"barcode": "BIN_104"}))
        assert response.status_code == 200 and response.json() == ACK

    [data] = applied
    assert isinstance(data, ScanData) and data.barcode == "BIN_104"
    [receipt] = await rows(callback_store, IntegrationExchange)
    assert receipt.diagnostic == [{"reason_code": "SCAN_APPLIED", "barcode": "BIN_104"}]


async def test_bad_envelope_returns_issues_without_echoing_values(callback_client, callback_store):
    response = await callback_client.post(PATH, json=body(device_code="SECRET-DEV", timestamp="x"))
    assert response.status_code == 400
    assert response.json()["error_detail"]["issues"]
    assert "SECRET-DEV" not in response.text


async def test_data_errors_keep_each_array_position_in_order(callback_client, callback_store):
    class ScanData(CallbackPayload):
        samples: list[float]

    @ecs_event("SCAN_COMPLETED", data=ScanData, device_role="SCAN1")
    async def handler(ctx, data):
        pytest.fail("invalid event data must not reach the handler")

    response = await callback_client.post(PATH, json=body(data={"samples": ["SECRET", 1.0, None]}))

    assert response.status_code == 400
    assert response.json()["error_detail"]["issues"] == [
        {"field": "data.samples[0]", "code": "INVALID_TYPE", "expected": "number"},
        {"field": "data.samples[2]", "code": "INVALID_TYPE", "expected": "number"},
    ]
    assert "SECRET" not in response.text
    assert await rows(callback_store, IntegrationExchange) == []


async def test_masked_data_errors_merge_only_identical_issues(callback_client, callback_store):
    class ScanData(CallbackPayload):
        readings: dict[str, int | float]
        barcode: str

    @ecs_event("SCAN_COMPLETED", data=ScanData, device_role="SCAN1")
    async def handler(ctx, data):
        pytest.fail("invalid event data must not reach the handler")

    response = await callback_client.post(
        PATH, json=body(data={"readings": {"SECRET_A": [], "SECRET_B": []}})
    )

    assert response.status_code == 400
    assert response.json()["error_detail"]["issues"] == [
        {"field": "data.readings.*.*", "code": "INVALID_TYPE", "expected": "integer"},
        {"field": "data.readings.*.*", "code": "INVALID_TYPE", "expected": "number"},
        {"field": "data.barcode", "code": "FIELD_REQUIRED"},
    ]
    assert "SECRET" not in response.text
    assert await rows(callback_store, IntegrationExchange) == []


async def test_oversize_is_413_with_json_body(callback_client, callback_store):
    response = await callback_client.post(PATH, content=b" " * (MAX_BODY_BYTES + 1))
    assert response.status_code == 413
    assert response.json() == {"code": 413, "message": "PAYLOAD_TOO_LARGE"}
    assert await rows(callback_store, IntegrationExchange) == []


async def test_commit_failure_returns_503_and_logs_error_outcome(
    callback_client, callback_store, caplog
):
    engine = callback_store.kw["bind"]

    def fail_once(connection):
        event.remove(engine.sync_engine, "commit", fail_once)
        raise RuntimeError("commit unavailable")

    event.listen(engine.sync_engine, "commit", fail_once)
    caplog.set_level(logging.INFO)
    response = await callback_client.post(PATH, json=body())
    assert response.status_code == 503
    assert response.json() == {"code": 503, "message": "TEMPORARILY_UNAVAILABLE"}
    assert await rows(callback_store, IntegrationExchange) == []
    finals = [r for r in caplog.records if hasattr(r, "outcome")]
    assert [r.outcome for r in finals] == ["error"]
    assert finals[0].event_type == "'SCAN_COMPLETED'" and finals[0].device_code == "'D1'"
    assert finals[0].status == 503 and "outcome=error" in finals[0].getMessage()


@pytest.mark.parametrize("raw", [None, b"\xff"])
async def test_receive_failure_log_identity_is_best_effort(
    callback_client, callback_store, caplog, monkeypatch, raw
):
    async def broken(session, data):
        raise RuntimeError("secret-commit-detail")

    monkeypatch.setattr(ecs_controller, "receive_ecs_event", broken)
    caplog.set_level(logging.INFO)
    response = (
        await callback_client.post(PATH, content=raw)
        if raw
        else await callback_client.post(PATH, json=body())
    )
    assert response.status_code == 503
    [final] = [r for r in caplog.records if hasattr(r, "outcome")]
    assert final.outcome == "error"
    if raw:
        assert final.event_type is None and final.device_code is None
    else:
        assert final.event_type == "'SCAN_COMPLETED'" and final.device_code == "'D1'"
    assert all("secret-commit-detail" not in r.getMessage() for r in caplog.records)
    assert all(r.exc_info is None for r in caplog.records)


async def test_final_log_has_rendered_fields(callback_client, callback_store, caplog):
    caplog.set_level(logging.INFO)
    await callback_client.post(PATH, json=body())
    [record] = [r for r in caplog.records if hasattr(r, "outcome")]
    assert record.outcome == "unregistered" and record.event_type == "'SCAN_COMPLETED'"
    assert record.device_code == "'D1'" and len(record.digest) == 8
    assert isinstance(record.duration_ms, int | float)


async def test_snapshot_is_recorded_with_ecs_source_and_wms_quota_unaffected(
    callback_client, callback_store, monkeypatch
):
    monkeypatch.setattr(snapshots, "SNAPSHOTS_PER_MINUTE", 2)
    for n in range(3):
        assert (await callback_client.post(PATH, json=body(timestamp=n))).status_code == 200
    wms = dict(operation_id=str(uuid7()), operation="x@v1", timestamp=1, data={})
    await callback_client.post(WMS_PATH, json=wms)
    sources = [s.source for s in await rows(callback_store, IntegrationRecord)]
    assert sources.count("ecs") == 2 and sources.count(None) == 1


async def test_wms_events_endpoint_cannot_reach_ecs_handlers(callback_client, callback_store):
    calls = []

    @ecs_event("SCAN_COMPLETED", device_role="SCAN1", data=CallbackPayload)
    async def handler(ctx, data):
        calls.append(1)

    wms = dict(operation_id=str(uuid7()), operation="ecs.event", timestamp=1, data={})
    response = await callback_client.post(WMS_PATH, json=wms)
    assert response.status_code == 422
    assert response.json()["data"] == {"reason_code": "UNSUPPORTED_OPERATION"}
    assert calls == []


async def test_local_scan_example_validates_data_and_persists_one_note(
    callback_client, callback_store
):
    from app.integrations.ecs_callback.dev_ecs_event import ExampleScan, example_scan

    if registry.lookup("EXAMPLE_SCAN", "EXAMPLE_SCAN") is None:
        ecs_event("EXAMPLE_SCAN", data=ExampleScan, device_role="EXAMPLE_SCAN")(example_scan)
    invalid = await callback_client.post(
        PATH,
        json=body(
            device_code="LOCAL_EXAMPLE_SCAN", event_type="EXAMPLE_SCAN", data={"barcode": ""}
        ),
    )
    assert invalid.status_code == 400
    assert await rows(callback_store, IntegrationExchange) == []

    for _ in range(2):
        response = await callback_client.post(
            PATH,
            json=body(
                device_code="LOCAL_EXAMPLE_SCAN",
                event_type="EXAMPLE_SCAN",
                data={"barcode": "BIN-1"},
            ),
        )
        assert response.status_code == 200 and response.json() == ACK
    [receipt] = await rows(callback_store, IntegrationExchange)
    assert receipt.diagnostic == [{"reason_code": "LOCAL_EXAMPLE", "barcode": "BIN-1"}]


@pytest.mark.parametrize("original_extra", [{}, {"is_debug": False}, {"is_debug": True}])
async def test_legacy_unregistered_event_is_not_applied_after_upgrade(
    callback_client, callback_store, original_extra
):
    await legacy_ecs_receipt(callback_store, body(**original_extra))
    [before] = await rows(callback_store, IntegrationExchange)
    saved = asdict(before)

    class ScanData(CallbackPayload):
        barcode: str

    @ecs_event("SCAN_COMPLETED", data=ScanData, device_role="SCAN1")
    async def handler(ctx, data):
        pytest.fail("historical unregistered receipt must never replay into a new handler")

    for extra in ({}, {"is_debug": True}, {"is_debug": False}, {"is_debug": "extra"}):
        response = await callback_client.post(PATH, json=body(**extra))
        assert response.status_code == 200 and response.json() == ACK
    [after] = await rows(callback_store, IntegrationExchange)
    assert asdict(after) == saved


async def test_handler_failure_returns_503_without_receipt_or_business_write(
    callback_client, callback_store, callback_effects
):
    @ecs_event("SCAN_COMPLETED", data=CallbackPayload, device_role="SCAN1")
    async def handler(ctx, data):
        await ctx.session.execute(callback_effects.insert().values(marker="failed-business-write"))
        await ctx.session.flush()
        raise RuntimeError("temporary failure")

    response = await callback_client.post(PATH, json=body(is_debug=True))
    assert response.status_code == 503
    assert response.json() == {"code": 503, "message": "TEMPORARILY_UNAVAILABLE"}
    assert await rows(callback_store, IntegrationExchange) == []
    async with callback_store() as session:
        assert list(await session.scalars(select(callback_effects.c.marker))) == []
