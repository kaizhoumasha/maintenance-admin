import json
from typing import Literal

import pytest
from pydantic import ConfigDict, ValidationError

from app.integrations.ecs_callback.envelope import EcsEventEnvelope, issues_from
from app.integrations.wms_callback.operation import CallbackPayload

VALID = {"device_code": "D1", "event_type": "T", "timestamp": 1}


def test_minimal_body_applies_defaults_and_ignores_extras():
    env = EcsEventEnvelope.model_validate({**VALID, "extra": 1})
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
        ({"data": None}, [{"field": "data", "code": "INVALID_TYPE", "expected": "object"}]),
        ({"device_code": ""}, [{"field": "device_code", "code": "INVALID_VALUE"}]),
    ],
)
def test_invalid_envelope_issues(patch, expected):
    body = {**VALID, **patch}
    body = {k: v for k, v in body.items() if not (k == "device_code" and v is None)}
    with pytest.raises(ValidationError) as ei:
        EcsEventEnvelope.model_validate(body)
    assert issues_from(ei.value, EcsEventEnvelope) == expected


def test_nested_array_error_uses_index_path_without_client_values():
    class M(CallbackPayload):
        samples: list[float]

    with pytest.raises(ValidationError) as ei:
        M.model_validate({"samples": [1.0, "SECRET"]})
    issues = issues_from(ei.value, M, "data")
    assert issues == [{"field": "data.samples[1]", "code": "INVALID_TYPE", "expected": "number"}]
    assert "SECRET" not in json.dumps(issues)


def test_unknown_pydantic_error_falls_back_to_invalid_value():
    class M(CallbackPayload):
        mode: Literal["a", "b"]

    with pytest.raises(ValidationError) as ei:
        M.model_validate({"mode": "SECRET"})
    assert issues_from(ei.value, M, "data") == [{"field": "data.mode", "code": "INVALID_VALUE"}]


def test_null_device_code_is_invalid_type():
    with pytest.raises(ValidationError) as ei:
        EcsEventEnvelope.model_validate({**VALID, "device_code": None})
    assert issues_from(ei.value, EcsEventEnvelope) == [
        {"field": "device_code", "code": "INVALID_TYPE", "expected": "string"}
    ]


def test_client_controlled_dict_key_is_masked():
    class M(CallbackPayload):
        d: dict[str, int]

    with pytest.raises(ValidationError) as ei:
        M.model_validate({"d": {"SECRETKEY": "x"}})
    issues = issues_from(ei.value, M, "data")
    assert issues == [{"field": "data.d.*", "code": "INVALID_TYPE", "expected": "integer"}]
    assert "SECRETKEY" not in json.dumps(issues)


def test_client_chosen_extra_key_is_masked():
    class M(CallbackPayload):
        model_config = ConfigDict(strict=True, extra="forbid")
        a: int

    with pytest.raises(ValidationError) as ei:
        M.model_validate({"a": 1, "SECRETKEY": 1})
    issues = issues_from(ei.value, M, "data")
    assert issues == [{"field": "data.*", "code": "INVALID_VALUE"}]


def test_union_tags_do_not_leak_into_path():
    class M(CallbackPayload):
        u: int | str

    with pytest.raises(ValidationError) as ei:
        M.model_validate({"u": [1]})
    issues = issues_from(ei.value, M, "data")
    assert all(i["field"] == "data.u.*" for i in issues)
    assert "int" not in json.dumps([i["field"] for i in issues])
