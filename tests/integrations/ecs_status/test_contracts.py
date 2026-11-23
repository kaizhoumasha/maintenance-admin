import json

import pytest
from pydantic import ValidationError

from app.integrations.ecs_status import EcsStatusResponse, normalize_status_origin


def parse(document):
    return EcsStatusResponse.model_validate_json(json.dumps(document))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("is_online", 1),
        ("updated_at", True),
        ("updated_at", 1.5),
        ("updated_at", "1793029419104"),
        ("updated_at", 0),
        ("updated_at", 2**63),
        ("mode", "REMOTE"),
        ("status", "READY"),
        ("device_code", " ARM03"),
    ],
)
def test_known_fields_are_strict(status_document, field, value):
    document = status_document()
    document["devices"][0]["state"][field] = value
    with pytest.raises(ValidationError):
        parse(document)


def test_required_nullable_fields_and_extensions(status_document):
    document = status_document()
    for key in ("device_name", "device_type", "role", "supported_commands", "supported_events"):
        document["devices"][0]["device"][key] = None
    document["extension"] = {"supplier": 1}
    parsed = parse(document)
    assert parsed.devices[0].device.device_name is None
    assert parsed.devices[0].state.status == "OFFLINE"
    assert parsed.devices[0].state.is_online is False
    del document["devices"][0]["device"]["device_name"]
    with pytest.raises(ValidationError):
        parse(document)


def test_identity_is_preserved(status_document):
    with pytest.raises(ValidationError):
        parse(status_document("ARM03", "ARM03"))
    document = status_document()
    document["devices"][0]["state"]["device_code"] = "ARM04"
    with pytest.raises(ValidationError):
        parse(document)
    parsed = parse(status_document("ARM03", "arm03"))
    assert tuple(item.device.device_code for item in parsed.devices) == ("ARM03", "arm03")
    assert parse({"devices": []}).devices == ()
    with pytest.raises(ValidationError):
        parsed.devices[0].state.status = "IDLE"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://10.24.209.27:8080/", "http://10.24.209.27:8080"),
        ("http://127.0.0.1:80", "http://127.0.0.1"),
        ("https://[::1]:443/", "https://[::1]"),
    ],
)
def test_origin_normalization(value, expected):
    assert normalize_status_origin(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost:8000",
        "http://0.0.0.0",
        "http://224.0.0.1",
        "http://[fe80::1%en0]",
        "http://user:PASSWORD@127.0.0.1",
        "http://127.0.0.1/api/v1/device/status",
        "http://127.0.0.1?x=1",
        "http://127.0.0.1#fragment",
        "ftp://127.0.0.1",
        "http://127.0.0.1:0",
    ],
)
def test_invalid_origin_is_rejected(value):
    with pytest.raises(ValueError):
        normalize_status_origin(value)
