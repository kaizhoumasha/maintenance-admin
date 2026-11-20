import json
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ConfigDict

from app.core.identifiers import uuid7
from app.integrations import callback_receipts, history
from app.integrations.callback_receipts import find_receipt, find_receipt_state, insert_receipt
from app.integrations.ecs_request import CallbackPayload, ecs_request, registry
from app.integrations.ecs_request.envelope import EcsRequestEnvelope
from app.integrations.ecs_request.receiver import (
    DeferredRequest,
    _binding_matches_snapshot,
    accept_ecs_request,
    continue_ecs_request,
    request_identity,
)
from app.models import ReliableMessage
from tests.helpers import ecs_binding


@pytest.mark.parametrize("runtime", [False, True])
async def test_pending_receipt_state_is_readable_without_reply_wire(
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
            raw_digest="d" * 64,
            effective_digest=None,
            compare_mode="RAW",
            status_code=503,
            body={"code": 503, "message": "TEMPORARILY_UNAVAILABLE"},
            resp_timestamp=1,
            created_at=datetime.now(UTC),
            raw_body=b'{"device_code":"D1"}',
            peer_system="ECS",
            protocol="ecs-request",
            interaction_kind="REQUEST",
            defer_response=True,
        )

    async with callback_store() as session:
        state = await find_receipt_state(session, "ecs.request", operation_id)
        assert state is not None
        assert state.body is None and state.prepared_body is None
        with pytest.raises(history.EvidenceIntegrityError):
            await find_receipt(session, "ecs.request", operation_id)


async def test_continue_binding_must_match_the_acceptance_snapshot():
    binding = await ecs_binding(None, "D1")
    snapshot = {
        "owner_domain": binding.business_code,
        "device_id": binding.device_id,
        "device_version": binding.device_version,
        "work_line_id": binding.work_line_id,
        "work_line_version": binding.work_line_version,
        "device_role": binding.device_role,
    }

    assert _binding_matches_snapshot(binding, snapshot)
    assert not _binding_matches_snapshot(
        binding,
        {**snapshot, "device_version": binding.device_version + 1},
    )
    assert not _binding_matches_snapshot(binding, {**snapshot, "owner_domain": "other"})


async def test_continue_uses_the_acceptance_snapshot_binding_resolver(callback_store, monkeypatch):
    monkeypatch.setattr(registry, "_registry", {})

    class Decision(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")
        result: str

    calls = []
    original = await ecs_binding(None, "D1")
    message_ref = {}

    @ecs_request("SNAPSHOT_LOCK", device_role="SCAN1", data=CallbackPayload, response=Decision)
    async def handler(ctx, data):
        if ctx.deferred is None:
            ctx.defer(message_ref["id"], "wms.decide@v1", "snapshot-lock")
        else:
            ctx.respond(Decision(result="ALLOW"))

    raw = json.dumps(
        {
            "device_code": "D1",
            "request_type": "SNAPSHOT_LOCK",
            "timestamp": 1,
            "data": {},
        }
    ).encode()
    now = datetime.now(UTC)
    async with callback_store.begin() as session:
        message = ReliableMessage(
            target="wms",
            operation="wms.decide@v1",
            operation_id="snapshot-lock",
            status="PENDING",
            exchange_id=uuid7(),
            request_record_id=uuid7(),
            created_at=now,
            updated_at=now,
            next_attempt_at=now,
        )
        session.add(message)
        await session.flush()
        message_ref["id"] = message.id
        accepted = await accept_ecs_request(
            session,
            raw,
            resolve_binding=lambda session, code: _return(session, original),
        )

    async def current_binding(session, code):
        raise AssertionError("continue must not resolve the current binding")

    async def snapshot_binding(session, code, snapshot):
        calls.append((code, snapshot))
        return original

    envelope = EcsRequestEnvelope.model_validate(json.loads(raw))
    async with callback_store.begin() as session:
        continued = await continue_ecs_request(
            session,
            identity=request_identity(envelope),
            deferred=DeferredRequest(message.id, message.operation, message.operation_id),
            resolve_binding=current_binding,
            resolve_binding_snapshot=snapshot_binding,
        )

    assert accepted.deferred is True
    assert continued.status_code == 200
    assert calls and calls[0][0] == "D1"


async def _return(_session, value):
    return value
