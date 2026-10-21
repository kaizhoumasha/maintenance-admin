"""WMS Transport 协议适配器:请求透传与 ACK 分类(合同 §4.1/§4.2)。"""

import json

import pytest

from app.http_client import DeliveryState, FailureKind, HttpResult, validate_request
from app.integrations.wms_transport import OPERATION, TransportSubmitAdapter
from app.integrations.wms_transport.protocol import DEFAULT_SUBMIT_PATH, REJECTION_REASONS
from app.reliable import OutboundMessage
from app.reliable.config import TargetConfig
from app.reliable.testing import FakeClock

OPERATION_ID = "01a13b00-cabd-7390-890a-0c093e7c07a1"
TASK_ID = "transport-9af2751b-4d01-4203-88a7-649bbf69e810"
REQUEST = {
    "operation_id": OPERATION_ID,
    "operation": OPERATION,
    "timestamp": 1791991270077,
    "data": {
        "transport_task_id": TASK_ID,
        "kind": "RACK_MOVE",
        "rack_id": "510004",
        "rcs_template_id": "CTU01",
        "source": {"kind": "RACK", "location_code": "510004"},
        "target": {"kind": "RACK_POSITION", "location_code": "KT16"},
        "target_face": "90",
    },
}
# 合同 §4.2 完整字面量,逐字复制。
SHELF_BUSY = (
    "There are other unfinished tasks on the shelf任务下发失败："
    "There are other unfinished tasks on the shelf"
)


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def message(request=REQUEST, **changes):
    values = dict(
        target="wms",
        operation=OPERATION,
        operation_id=OPERATION_ID,
        request_raw=encode(request),
        business_key=None,
        trace_id=None,
        created_at=FakeClock()(),
        expires_at=None,
    )
    return OutboundMessage(**(values | changes))


def complete(status, body):
    return HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, status, (), body)


def ack(status, code, *, operation_id=OPERATION_ID, task_id=TASK_ID, **extra):
    data = {} if task_id is None else {"transport_task_id": task_id}
    data.update(extra.pop("data", {}))
    body = {"operation_id": operation_id, "code": code, "timestamp": 1791992148964, "data": data}
    return complete(status, encode(body | extra))


def classify(result, adapter=None):
    return (adapter or TransportSubmitAdapter()).classify_response(message(), result)


@pytest.mark.parametrize(
    "result,kind,reason",
    [
        (ack(202, "RECEIVED"), "DELIVERED", None),
        (ack(200, "DUPLICATE"), "DELIVERED", None),
        (ack(409, "CONFLICT"), "PEER_TERMINAL", "CONFLICT"),
        (ack(422, "REJECTED", data={"reason_code": "INVALID_DATA"}), "PEER_TERMINAL", "REJECTED"),
        (ack(503, "UNAVAILABLE"), "NOT_RECEIVED", None),
        (complete(400, b""), "PEER_TERMINAL", "REJECTED"),
        (complete(413, b""), "PEER_TERMINAL", "REJECTED"),
        (complete(503, b"<html/>"), "UNKNOWN", None),
        (ack(200, "RECEIVED"), "UNKNOWN", None),
    ],
)
def test_contract_ack_matrix(result, kind, reason):
    verdict = classify(result)
    assert verdict.kind == kind and verdict.reason == reason
    assert verdict.retry_after_ms is None
    if kind == "DELIVERED":
        assert verdict.response == result.body


@pytest.mark.parametrize("reason", sorted(REJECTION_REASONS))
def test_every_approved_rejection_reason_is_terminal(reason):
    verdict = classify(ack(422, "REJECTED", data={"reason_code": reason}))
    assert (verdict.kind, verdict.reason) == ("PEER_TERMINAL", "REJECTED")


def test_rejection_reason_set_is_exactly_the_contract_list():
    assert REJECTION_REASONS == {
        "INVALID_ENVELOPE",
        "UNSUPPORTED_OPERATION",
        "INVALID_DATA",
        "COORDINATED_BIN_EXCHANGE_UNSUPPORTED",
        SHELF_BUSY,
    }


@pytest.mark.parametrize(
    "reason",
    [
        "invalid_data",
        "INVALID_DATA ",
        " INVALID_DATA",
        "UNCONFIRMED_REASON",
        "There are other unfinished tasks on the shelf",
        "任务下发失败：There are other unfinished tasks on the shelf",
        SHELF_BUSY + "x",
        "",
        None,
        [],
        7,
    ],
)
def test_unapproved_rejection_reason_is_unknown(reason):
    verdict = classify(ack(422, "REJECTED", data={"reason_code": reason}))
    assert verdict.kind == "UNKNOWN"


def test_rejection_without_reason_is_unknown():
    assert classify(ack(422, "REJECTED")).kind == "UNKNOWN"


@pytest.mark.parametrize(
    "status,code",
    [
        (202, "RECEIVED"),
        (200, "DUPLICATE"),
        (409, "CONFLICT"),
        (503, "UNAVAILABLE"),
        (422, "REJECTED"),
    ],
)
@pytest.mark.parametrize("task_id", [None, "another-task", "", 7])
def test_every_ack_must_echo_the_requested_task(status, code, task_id):
    extra = {"data": {"reason_code": "INVALID_DATA"}} if code == "REJECTED" else {}
    if task_id == 7:
        result = complete(
            status,
            encode(
                {
                    "operation_id": OPERATION_ID,
                    "code": code,
                    "timestamp": 1,
                    "data": {"transport_task_id": 7, **extra.get("data", {})},
                }
            ),
        )
    else:
        result = ack(status, code, task_id=task_id, **extra)
    assert classify(result).kind == "UNKNOWN"


@pytest.mark.parametrize("operation_id", ["wrong-id", "", None, 7, OPERATION_ID.upper()])
def test_ack_must_echo_request_operation_id(operation_id):
    assert classify(ack(202, "RECEIVED", operation_id=operation_id)).kind == "UNKNOWN"


@pytest.mark.parametrize(
    "timestamp", [True, False, -1, 2**63, 1.0, 1.5, "1", None, [], {}], ids=repr
)
def test_ack_timestamp_must_be_an_int64_millisecond_integer(timestamp):
    body = {
        "operation_id": OPERATION_ID,
        "code": "RECEIVED",
        "timestamp": timestamp,
        "data": {"transport_task_id": TASK_ID},
    }
    assert classify(complete(202, encode(body))).kind == "UNKNOWN"


@pytest.mark.parametrize("timestamp", [0, 2**63 - 1])
def test_ack_timestamp_boundaries_are_accepted(timestamp):
    body = {
        "operation_id": OPERATION_ID,
        "code": "RECEIVED",
        "timestamp": timestamp,
        "data": {"transport_task_id": TASK_ID},
    }
    assert classify(complete(202, encode(body))).kind == "DELIVERED"


def test_missing_defined_fields_are_unknown():
    full = {
        "operation_id": OPERATION_ID,
        "code": "RECEIVED",
        "timestamp": 1,
        "data": {"transport_task_id": TASK_ID},
    }
    for name in full:
        partial = {key: value for key, value in full.items() if key != name}
        assert classify(complete(202, encode(partial))).kind == "UNKNOWN", name
    assert classify(complete(202, encode(full | {"data": []}))).kind == "UNKNOWN"


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"[]",
        b"null",
        b"7",
        b"\xff",
        b"\xef\xbb\xbf{}",
        b"{",
        b'{"operation_id":"a","operation_id":"b"}',
        b'{"data":{"transport_task_id":"a","transport_task_id":"a"}}',
        b'{"timestamp":NaN}',
        b'{"timestamp":Infinity}',
        b"<html/>",
    ],
)
def test_malformed_or_ambiguous_bodies_are_unknown(body):
    assert classify(complete(202, body)).kind == "UNKNOWN"


def test_nested_duplicate_key_is_unknown_even_when_defined_fields_look_valid():
    body = (
        f'{{"operation_id":"{OPERATION_ID}","code":"RECEIVED","timestamp":1,'
        f'"data":{{"transport_task_id":"{TASK_ID}","x":1,"x":2}}}}'
    ).encode()
    assert classify(complete(202, body)).kind == "UNKNOWN"


def test_redundant_fields_including_floats_are_ignored():
    body = {
        "operation_id": OPERATION_ID,
        "code": "RECEIVED",
        "timestamp": 1791992148964,
        "data": {"transport_task_id": TASK_ID, "load": 1.5, "nested": {"ratio": 0.25}},
        "trace": 1e3,
    }
    result = complete(202, encode(body))
    verdict = classify(result)
    assert verdict.kind == "DELIVERED" and verdict.response == result.body


@pytest.mark.parametrize("status", [429, 502, 401, 500, 404, 204, 301])
def test_undefined_status_with_plausible_body_is_unknown(status):
    for code in ("RECEIVED", "UNAVAILABLE", "BUSY", "REJECTED", "CONFLICT"):
        assert classify(ack(status, code, data={"reason_code": "INVALID_DATA"})).kind == "UNKNOWN"


@pytest.mark.parametrize("status", [400, 413])
def test_pre_correlation_rejections_require_an_empty_body(status):
    assert classify(complete(status, b" ")).kind == "UNKNOWN"
    assert classify(complete(status, b"{}")).kind == "UNKNOWN"
    assert classify(ack(status, "REJECTED", data={"reason_code": "INVALID_DATA"})).kind == "UNKNOWN"


@pytest.mark.parametrize("code", ["NEW_CODE", "received", "BUSY", "", None, [], 7])
def test_unknown_codes_are_unknown(code):
    assert classify(ack(202, code)).kind == "UNKNOWN"


@pytest.mark.parametrize(
    "result",
    [
        HttpResult(DeliveryState.NOT_SENT, FailureKind.CONNECT_ERROR, None, (), None),
        HttpResult(DeliveryState.DELIVERY_UNKNOWN, FailureKind.READ_TIMEOUT, None, (), None),
        HttpResult(
            DeliveryState.HTTP_RESPONSE_COMPLETE, FailureKind.TRANSPORT_ERROR, 202, (), b"{}"
        ),
        HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 202, (), None),
    ],
)
def test_incomplete_http_evidence_is_unknown(result):
    assert classify(result).kind == "UNKNOWN"


def test_build_request_posts_the_frozen_bytes_unchanged_to_the_configured_path():
    adapter = TransportSubmitAdapter("/Custom/Transport")
    target = TargetConfig("wms", "http://example.test", "wms-transport")
    frozen = message()
    request = adapter.build_request(frozen, target)
    assert request.method == "POST" and request.path == "/Custom/Transport"
    assert request.body is frozen.request_raw
    headers = dict(request.headers)
    assert headers["content-type"] == "application/json"
    assert headers["accept"] == "application/json"
    validate_request(target.http_config(), request)
    assert TransportSubmitAdapter().build_request(frozen, target).path == DEFAULT_SUBMIT_PATH


@pytest.mark.parametrize(
    "bad",
    [
        message(operation="transport.task.cancel@v1"),
        message(operation_id="01a13b00-cabd-7390-890a-0c093e7c07a2"),
        message(REQUEST | {"operation": "other@v1"}),
        message(REQUEST | {"operation_id": "01a13b00-cabd-7390-890a-0c093e7c07a2"}),
        message(REQUEST | {"data": {}}),
        message(REQUEST | {"data": {"transport_task_id": ""}}),
        message(REQUEST | {"data": {"transport_task_id": 7}}),
        message(REQUEST | {"data": []}),
        message(request_raw=b"not json"),
        message(request_raw=b"[]"),
        message(request_raw=b""),
    ],
)
def test_build_request_rejects_inconsistent_frozen_identity(bad):
    target = TargetConfig("wms", "http://example.test", "wms-transport")
    with pytest.raises((ValueError, TypeError)):
        TransportSubmitAdapter().build_request(bad, target)


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/wes/transport-requests",
        "/a",
        "/Mixed/Case_9",
        "/a%2Fb",
        "/Transport%20Submit",
        "/" + "a" * 2047,
    ],
)
def test_valid_submit_paths(path):
    assert TransportSubmitAdapter(path).submit_path == path


@pytest.mark.parametrize(
    "path",
    [
        "",
        "api",
        "http://example.test/a",
        "//example.test/a",
        "/a?b=1",
        "/a#b",
        "/a\\b",
        "/a/../b",
        "/a/./b",
        "/..",
        "/a/%2e%2e/b",
        "/a/%2E/b",
        "/a%2f..%2fb",
        "/a%5cb",
        "/%2fexample.test/a",
        "/a\nb",
        "/a%00b",
        "/a%7fb",
        "/" + "a" * 2048,
        None,
        7,
    ],
)
def test_invalid_submit_paths(path):
    with pytest.raises((ValueError, TypeError)):
        TransportSubmitAdapter(path)
