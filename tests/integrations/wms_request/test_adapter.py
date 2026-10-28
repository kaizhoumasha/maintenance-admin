import json
from dataclasses import replace

import pytest

from app.http_client import DeliveryState, HttpResult
from app.integrations.wms_request import ADAPTERS, DECISIONS_PROTOCOL, FACTS_PROTOCOL
from app.reliable import OutgoingMessage
from app.reliable.config import TargetConfig
from app.reliable.testing import FakeClock

OPERATION_ID = "019b76da-a800-7000-8000-000000000001"
FACT, DECISION = "example.fact.report@v1", "example.decision.ask@v1"


def message(operation=FACT, operation_id=OPERATION_ID, request_operation=None):
    raw = json.dumps(
        {
            "operation_id": operation_id,
            "operation": request_operation or operation,
            "timestamp": 1,
            "data": {},
        }
    ).encode()
    return OutgoingMessage("t", operation, OPERATION_ID, raw, None, None, FakeClock()(), None)


def ack(status, code, *, operation_id=OPERATION_ID, timestamp=1791992148964, **data):
    body = {"operation_id": operation_id, "code": code, "timestamp": timestamp, "data": data}
    raw = json.dumps(body).encode()
    return HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, status, (), raw)


def classify(protocol, result, operation=FACT):
    return ADAPTERS[protocol].classify_response(message(operation), result)


@pytest.mark.parametrize(
    "protocol,status,code,kind",
    [
        (FACTS_PROTOCOL, 200, "RECORDED", "DELIVERED"),
        (FACTS_PROTOCOL, 200, "DUPLICATE", "DELIVERED"),
        (FACTS_PROTOCOL, 200, "DECIDED", "UNKNOWN"),
        (DECISIONS_PROTOCOL, 200, "DECIDED", "DELIVERED"),
        (DECISIONS_PROTOCOL, 202, "PREPARE_ACCEPTED", "DELIVERED"),
        (DECISIONS_PROTOCOL, 200, "RECORDED", "UNKNOWN"),
        *[(p, 503, "UNAVAILABLE", "NOT_RECEIVED") for p in (FACTS_PROTOCOL, DECISIONS_PROTOCOL)],
        *[(p, 404, "UNAVAILABLE", "UNKNOWN") for p in (FACTS_PROTOCOL, DECISIONS_PROTOCOL)],
    ],
)
def test_success_codes_belong_to_their_endpoint(protocol, status, code, kind):
    assert classify(protocol, ack(status, code)).kind == kind


@pytest.mark.parametrize("protocol", [FACTS_PROTOCOL, DECISIONS_PROTOCOL])
def test_common_error_union(protocol):
    assert classify(protocol, ack(409, "CONFLICT", reason_code="STATE_CONFLICT")).reason == (
        "CONFLICT"
    )
    assert classify(protocol, ack(409, "CONFLICT", reason_code="OTHER")).kind == "UNKNOWN"
    assert classify(protocol, ack(422, "REJECTED", reason_code="INVALID_DATA")).reason == (
        "REJECTED"
    )
    assert classify(protocol, ack(422, "REJECTED", reason_code="OTHER")).kind == "UNKNOWN"
    busy = classify(protocol, ack(429, "BUSY", retry_after_ms=1500))
    assert (busy.kind, busy.retry_after_ms) == ("NOT_RECEIVED", 1500)
    for wait in (0, 60001, "5", True, None):
        assert classify(protocol, ack(429, "BUSY", retry_after_ms=wait)).kind == "UNKNOWN"
    for status in (400, 413):
        empty = HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, status, (), b"")
        assert classify(protocol, empty).reason == "REJECTED"


@pytest.mark.parametrize("protocol", [FACTS_PROTOCOL, DECISIONS_PROTOCOL])
@pytest.mark.parametrize("value", [[], {}, None, True, 1])
def test_non_string_response_codes_and_reasons_remain_unknown(protocol, value):
    for result in (
        ack(200, value),
        ack(409, "CONFLICT", reason_code=value),
        ack(422, "REJECTED", reason_code=value),
    ):
        assert classify(protocol, result).kind == "UNKNOWN"


@pytest.mark.parametrize("protocol", [FACTS_PROTOCOL, DECISIONS_PROTOCOL])
@pytest.mark.parametrize(
    "status,code,data",
    [
        (503, "UNAVAILABLE", {"unexpected": True}),
        (429, "BUSY", {"retry_after_ms": 1000, "unexpected": True}),
        (409, "CONFLICT", {"reason_code": "STATE_CONFLICT", "unexpected": True}),
        (422, "REJECTED", {"reason_code": "INVALID_DATA", "unexpected": True}),
        (422, "REJECTED", {"reason_code": "UNSUPPORTED_OPERATION", "field_path": "/data"}),
        (422, "REJECTED", {"reason_code": "INVALID_ENVELOPE", "field_path": None}),
        *[
            (422, "REJECTED", {"reason_code": "INVALID_DATA", "field_path": path})
            for path in (None, [], {}, 1, "", "data/x", "/~", "/~2", "/" + "x" * 256, "/\ud800")
        ],
    ],
)
def test_invalid_common_error_fields_remain_unknown(protocol, status, code, data):
    assert classify(protocol, ack(status, code, **data)).kind == "UNKNOWN"


@pytest.mark.parametrize("path", ["/", "/data/qty", "/data/a~1b~0c", "/" + "x" * 255])
def test_invalid_data_rejections_accept_a_valid_optional_field_path(path):
    for protocol in (FACTS_PROTOCOL, DECISIONS_PROTOCOL):
        result = classify(
            protocol, ack(422, "REJECTED", reason_code="INVALID_DATA", field_path=path)
        )
        assert (result.kind, result.reason) == ("PEER_TERMINAL", "REJECTED")


@pytest.mark.parametrize(
    "protocol,status,code",
    [
        (FACTS_PROTOCOL, 200, "RECORDED"),
        (FACTS_PROTOCOL, 200, "DUPLICATE"),
        (DECISIONS_PROTOCOL, 202, "PREPARE_ACCEPTED"),
    ],
)
def test_success_responses_without_business_fields_require_empty_data(protocol, status, code):
    assert classify(protocol, ack(status, code, unexpected=True)).kind == "UNKNOWN"


@pytest.mark.parametrize(
    "protocol,code", [(FACTS_PROTOCOL, "RECORDED"), (DECISIONS_PROTOCOL, "DECIDED")]
)
def test_unknown_response_envelope_fields_remain_unknown(protocol, code):
    result = ack(200, code)
    body = {**json.loads(result.body), "unexpected": True}
    assert classify(protocol, replace(result, body=json.dumps(body).encode())).kind == "UNKNOWN"


@pytest.mark.parametrize("data", [{"decision": "ready"}, {"amount": 1e300, "nested": [-2.5, None]}])
def test_decision_success_preserves_business_data_without_interpreting_it(data):
    result = ack(200, "DECIDED", **data)
    verdict = classify(DECISIONS_PROTOCOL, result)
    assert verdict.kind == "DELIVERED" and verdict.response == result.body


@pytest.mark.parametrize("data", [b'{"amount":1e400}', b'{"amount":-1e400}', b'{"nested":[1e400]}'])
def test_overflowed_decision_numbers_remain_unknown(data):
    result = ack(200, "DECIDED")
    raw = result.body.replace(b'"data": {}', b'"data": ' + data)
    assert classify(DECISIONS_PROTOCOL, replace(result, body=raw)).kind == "UNKNOWN"


def test_response_must_echo_the_request_identity_with_an_int64_timestamp():
    other = "019b76da-a800-7000-8000-000000000002"
    assert classify(FACTS_PROTOCOL, ack(200, "RECORDED", operation_id=other)).kind == "UNKNOWN"
    assert classify(FACTS_PROTOCOL, ack(200, "RECORDED", timestamp=True)).kind == "UNKNOWN"
    assert classify(FACTS_PROTOCOL, ack(200, "RECORDED", timestamp=2**63)).kind == "UNKNOWN"


def test_delivered_verdict_keeps_the_response_bytes():
    result = ack(200, "RECORDED")
    assert classify(FACTS_PROTOCOL, result).response == result.body


def test_request_is_posted_to_the_endpoint_path_with_frozen_bytes():
    target = TargetConfig("t", "http://example.test", FACTS_PROTOCOL)
    request = ADAPTERS[FACTS_PROTOCOL].build_request(message(FACT), target)
    assert (request.method, request.path) == ("POST", "/api/v1/wes/facts")
    assert request.body == message(FACT).request_raw
    decision = ADAPTERS[DECISIONS_PROTOCOL].build_request(message(DECISION), target)
    assert decision.path == "/api/v1/wes/decisions"


@pytest.mark.parametrize(
    "protocol,msg",
    [
        (FACTS_PROTOCOL, message(DECISION)),  # operation 属于另一个端点
        (DECISIONS_PROTOCOL, message(FACT)),
        (FACTS_PROTOCOL, message("example.unregistered.op@v1")),
        (FACTS_PROTOCOL, message(FACT, request_operation=DECISION)),
        (FACTS_PROTOCOL, message(FACT, operation_id="019b76da-a800-7000-8000-000000000009")),
    ],
)
def test_build_request_refuses_a_message_that_does_not_belong_to_the_endpoint(protocol, msg):
    target = TargetConfig("t", "http://example.test", protocol)
    with pytest.raises(ValueError):
        ADAPTERS[protocol].build_request(msg, target)


def test_registry_is_loaded_before_an_existing_message_is_sent(monkeypatch):
    """重启后发送器会在任何 `send` 之前领取存量消息,不能因注册表为空而把它判为失败。"""
    from app.integrations import discovery
    from app.integrations.wms_request import operation as registry

    snapshot = dict(registry._registry)
    monkeypatch.setattr(registry, "_registry", {})
    monkeypatch.setattr(
        discovery, "load_formal_modules", lambda **kwargs: registry._registry.update(snapshot)
    )
    target = TargetConfig("t", "http://example.test", FACTS_PROTOCOL)
    request = ADAPTERS[FACTS_PROTOCOL].build_request(message(FACT), target)
    assert request.path == "/api/v1/wes/facts"
