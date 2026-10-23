import json
from typing import Literal

import pytest
from pydantic import ValidationError

from app.integrations.ecs_callback.envelope import EcsEventEnvelope, issues_from
from app.integrations.wms_callback.operation import CallbackPayload

VALID = {"device_code": "D1", "event_type": "T", "timestamp": 1}


def test_minimal_body_applies_defaults_and_ignores_extras():
    env = EcsEventEnvelope.model_validate({**VALID, "extra": 1})
    assert env.is_debug is False
    assert env.data == {}
    assert not hasattr(env, "extra")


@pytest.mark.parametrize(
    ("patch", "expected"),
    [
        ({"device_code": None}, [{"field": "device_code", "code": "FIELD_REQUIRED"}]),
        (
            {"timestamp": True},
            [{"field": "timestamp", "code": "INVALID_TYPE", "expected": "integer"}],
        ),
        ({"timestamp": -1}, [{"field": "timestamp", "code": "INVALID_VALUE"}]),
        ({"timestamp": 2**63}, [{"field": "timestamp", "code": "INVALID_VALUE"}]),
        (
            {"is_debug": "true"},
            [{"field": "is_debug", "code": "INVALID_TYPE", "expected": "boolean"}],
        ),
        ({"is_debug": 1}, [{"field": "is_debug", "code": "INVALID_TYPE", "expected": "boolean"}]),
        (
            {"is_debug": None},
            [{"field": "is_debug", "code": "INVALID_TYPE", "expected": "boolean"}],
        ),
        ({"data": None}, [{"field": "data", "code": "INVALID_TYPE", "expected": "object"}]),
        ({"device_code": ""}, [{"field": "device_code", "code": "INVALID_VALUE"}]),
    ],
)
def test_invalid_envelope_issues(patch, expected):
    body = {**VALID, **patch}
    body = {k: v for k, v in body.items() if not (k == "device_code" and v is None)}
    with pytest.raises(ValidationError) as ei:
        EcsEventEnvelope.model_validate(body)
    assert issues_from(ei.value) == expected


def test_nested_array_error_uses_index_path_without_client_values():
    class M(CallbackPayload):
        samples: list[float]

    with pytest.raises(ValidationError) as ei:
        M.model_validate({"samples": [1.0, "SECRET"]})
    issues = issues_from(ei.value, "data")
    assert issues == [{"field": "data.samples[1]", "code": "INVALID_TYPE", "expected": "number"}]
    assert "SECRET" not in json.dumps(issues)


def test_unknown_pydantic_error_falls_back_to_invalid_value():
    class M(CallbackPayload):
        mode: Literal["a", "b"]

    with pytest.raises(ValidationError) as ei:
        M.model_validate({"mode": "SECRET"})
    assert issues_from(ei.value, "data") == [{"field": "data.mode", "code": "INVALID_VALUE"}]
