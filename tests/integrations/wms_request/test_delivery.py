import json
from dataclasses import replace

import pytest

from app.core.identifiers import uuid7
from app.http_client import DeliveryState, HttpResult
from app.integrations.wms_request import DECISIONS_PROTOCOL, FACTS_PROTOCOL
from app.integrations.wms_request.operation import lookup
from app.models import ReliableMessage
from app.reliable.delivery import send_one
from app.reliable.store import MessageStore
from app.reliable.testing import ScriptedHttpClient


@pytest.mark.parametrize(
    "protocol,operation,success",
    [
        (FACTS_PROTOCOL, "example.fact.report@v1", "RECORDED"),
        (DECISIONS_PROTOCOL, "example.decision.ask@v1", "DECIDED"),
    ],
)
@pytest.mark.parametrize(
    "status,code,data",
    [
        (200, [], {}),
        (200, {}, {}),
        (409, "CONFLICT", {"reason_code": []}),
        (422, "REJECTED", {"reason_code": {}}),
        (503, "UNAVAILABLE", {"unexpected": True}),
        (429, "BUSY", {"retry_after_ms": 1000, "unexpected": True}),
        (422, "REJECTED", {"reason_code": "UNSUPPORTED_OPERATION", "field_path": None}),
    ],
)
async def test_invalid_ack_preserves_uncertainty_and_retries_the_frozen_request(
    sessions, wms, clock, protocol, operation, success, status, code, data
):
    operation_id = str(uuid7(clock()))
    outbox = wms.outbox
    target = wms.target(protocol)
    outbox.targets[target] = replace(outbox.targets[target], max_not_received_attempts=1)
    async with sessions() as session, session.begin():
        reference = await wms.send(
            session, lookup(operation).data_model(task_id="T-1", qty=2), operation_id=operation_id
        )
    store = MessageStore(sessions, clock=clock, uniform=lambda low, high: high)
    responses = [
        HttpResult(
            DeliveryState.HTTP_RESPONSE_COMPLETE,
            None,
            http_status,
            (),
            json.dumps(
                {
                    "operation_id": operation_id,
                    "code": response_code,
                    "timestamp": 1,
                    "data": fields,
                }
            ).encode(),
        )
        for http_status, response_code, fields in [(status, code, data), (200, success, {})]
    ]
    client = ScriptedHttpClient(*responses)
    for attempt, expected_status in enumerate(("PENDING", "DONE"), start=1):
        (claim,) = await store.claim(1, outbox.targets)
        await send_one(
            claim,
            store=store,
            targets=outbox.targets,
            adapters=outbox.adapters,
            clients={target: client},
            clock=clock,
            uniform=lambda low, high: high,
        )
        async with sessions() as session:
            row = await session.get(ReliableMessage, reference.message_id)
            assert row.status == expected_status and row.attempts == attempt
            assert row.fail_reason is None
            assert row.delivery_uncertain and row.not_received_count == 0
            if expected_status == "PENDING":
                assert row.last_verdict == "UNKNOWN" and row.last_response_raw == responses[0].body
            else:
                assert row.response_raw == responses[1].body
        clock.advance(2)
    assert [request.body for request in client.requests] == [row.request_raw, row.request_raw]
