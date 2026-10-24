import json
from datetime import UTC, datetime

import pytest

from app.http_client import DeliveryState, HttpResult


@pytest.mark.parametrize(
    "status,message,kind",
    [
        (200, "ACK", "DELIVERED"),
        (400, "INVALID_ENVELOPE", "PEER_TERMINAL"),
        (404, "DEVICE_NOT_FOUND", "PEER_TERMINAL"),
        (405, "METHOD_NOT_ALLOWED", "PEER_TERMINAL"),
        (413, "PAYLOAD_TOO_LARGE", "PEER_TERMINAL"),
        (422, "ANNEX_VALIDATION_FAILED", "PEER_TERMINAL"),
        (409, "IDEMPOTENCY_CONFLICT", "HOLD"),
        (503, "TEMPORARILY_UNAVAILABLE", "NOT_RECEIVED"),
        (500, "TEMPORARILY_UNAVAILABLE", "UNKNOWN"),
        (200, "OK", "UNKNOWN"),
    ],
)
def test_complete_response_matrix(status, message, kind):
    from app.integrations.ecs_command.protocol import EcsCommandAdapter

    result = HttpResult(
        DeliveryState.HTTP_RESPONSE_COMPLETE,
        None,
        status,
        (("content-type", "application/json; charset=utf-8"),),
        json.dumps({"code": status, "message": message}).encode(),
    )
    assert EcsCommandAdapter().classify_response(None, result).kind == kind


@pytest.mark.parametrize(
    "body",
    [
        b'{"code":true,"message":"ACK"}',
        b'{"code":[],"message":"ACK"}',
        b'{"code":200,"message":{}}',
        b'{"code":200,"code":200,"message":"ACK"}',
        b'{"code":200,"message":"ACK","trace_id":null}',
    ],
)
def test_malformed_ack_never_delivers(body):
    from app.integrations.ecs_command.protocol import EcsCommandAdapter

    result = HttpResult(
        DeliveryState.HTTP_RESPONSE_COMPLETE,
        None,
        200,
        (("content-type", "application/json"),),
        body,
    )
    assert EcsCommandAdapter().classify_response(None, result).kind == "UNKNOWN"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("0", 0),
        ("120", 120),
        ("-1", None),
        ("1.5", None),
        ("١", None),
        ("Sun, 25 Oct 2026 00:01:00 GMT", 60),
        ("Sun, 25 Oct 2026 00:00:00 GMT", None),
    ],
)
def test_retry_after(value, expected):
    from app.integrations.ecs_command.protocol import retry_after

    assert retry_after((("Retry-After", value),), datetime(2026, 10, 25, tzinfo=UTC)) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("HTTP://EXAMPLE.test:80/", "http://example.test"),
        ("https://example.test:443", "https://example.test"),
    ],
)
def test_origin_normalized(value, expected):
    from app.integrations.ecs_command.routing import normalize_origin

    assert normalize_origin(value) == expected


@pytest.mark.parametrize(
    "value", ["http://a/path", "http://USER:PASSWORD@a", "http://a/?x=1", "file:///a"]
)
def test_origin_rejects_non_origins(value):
    from app.integrations.ecs_command.routing import normalize_origin

    with pytest.raises(ValueError):
        normalize_origin(value)


def test_nonfinite_ack_extension_is_not_delivered():
    from app.integrations.ecs_command.protocol import EcsCommandAdapter

    result = HttpResult(
        DeliveryState.HTTP_RESPONSE_COMPLETE,
        None,
        200,
        (("content-type", "application/json"),),
        b'{"code":200,"message":"ACK","extra":1e400}',
    )
    assert EcsCommandAdapter().classify_response(None, result).kind == "UNKNOWN"


def test_snapshot_rejects_unimplemented_retry_policy():
    from app.reliable.types import DeliverySnapshot

    with pytest.raises(ValueError):
        DeliverySnapshot("http://ecs.test", "ecs-command", "RETRY")
