import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import CheckConstraint, select

from app.core.identifiers import uuid7
from app.integrations import callback_receipts
from app.integrations.callback_receipts import insert_receipt
from app.integrations.ecs_request.receiver import EcsRequestResponse
from app.integrations.http_exchanges import capture_callback
from app.models import EcsHttpExchange, IntegrationExchange


def test_ecs_http_exchange_allows_request_interaction_kind():
    checks = [c for c in EcsHttpExchange.__table__.constraints if isinstance(c, CheckConstraint)]
    assert any("REQUEST" in str(check.sqltext) for check in checks)


@pytest.mark.parametrize("runtime", [False, True])
async def test_ecs_request_receipt_uses_event_digest_identity_kind(
    callback_store, monkeypatch, runtime
):
    monkeypatch.setattr(callback_receipts, "enabled", lambda: runtime)
    operation_id = str(uuid7())
    async with callback_store.begin() as session:
        await insert_receipt(
            session,
            operation="ecs.request",
            operation_id=operation_id,
            device_code="D1",
            raw_digest="a" * 64,
            effective_digest=None,
            compare_mode="RAW",
            status_code=503,
            body={"code": 503},
            resp_timestamp=1,
            created_at=datetime.now(UTC),
            raw_body=b"{}",
            peer_system="ECS",
            protocol="ecs-request",
            interaction_kind="REQUEST",
            defer_response=True,
        )
    if runtime:
        pytest.skip("runtime authority stores identity_kind outside IntegrationExchange")
    async with callback_store() as session:
        row = await session.scalar(
            select(IntegrationExchange).where(IntegrationExchange.operation_id == operation_id)
        )
        assert row.identity_kind == "EVENT_DIGEST"


async def test_ecs_request_callback_projects_request_type(monkeypatch):
    captured = []

    async def save(_factory, model, values):
        captured.append((model, values))

    monkeypatch.setattr("app.integrations.http_exchanges.save_observation", save)
    raw = json.dumps(
        {"device_code": "D1", "request_type": "CARTON_VALIDATE", "timestamp": 1, "data": {}}
    ).encode()
    result = EcsRequestResponse(
        200,
        {"code": 200, "message": "DECIDED", "data": {"result": "ALLOW"}},
        "decided",
    )
    request = SimpleNamespace(
        method="POST",
        url=SimpleNamespace(path="/api/v1/callback/request"),
        headers={},
    )
    await capture_callback(
        None,
        identity=uuid7(),
        source="ecs_request",
        request=request,
        started_at=datetime.now(UTC),
        raw=raw,
        limit=256 * 1024,
        result=result,
    )
    assert captured and captured[0][0] is EcsHttpExchange
    assert captured[0][1]["interaction_kind"] == "REQUEST"
    assert captured[0][1]["event_type"] == "CARTON_VALIDATE"
