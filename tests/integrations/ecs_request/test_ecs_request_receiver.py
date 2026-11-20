from datetime import UTC, datetime

import pytest

from app.core.identifiers import uuid7
from app.integrations import callback_receipts, history
from app.integrations.callback_receipts import find_receipt, find_receipt_state, insert_receipt
from app.integrations.ecs_request.receiver import _binding_matches_snapshot
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
