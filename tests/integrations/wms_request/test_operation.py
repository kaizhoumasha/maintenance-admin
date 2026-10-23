import json
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from app.integrations.wms_request import Endpoint, RequestOperation, request_operation
from app.integrations.wms_request import operation as registry
from app.integrations.wms_request.discovery import candidate_modules


class Plain(RequestOperation):
    class Payload(registry.RequestPayload):
        pass


@pytest.mark.parametrize("name", ["single@v1", "A.b@v1", "a.b", "a.b@v0", "a..b@v1"])
def test_invalid_names_are_rejected_at_registration(name):
    with pytest.raises(ValueError, match="invalid operation name"):
        request_operation(name, Endpoint.FACTS)(Plain)


def test_duplicate_names_are_rejected_even_across_endpoints():
    with pytest.raises(ValueError, match="duplicate operation"):
        request_operation("example.fact.report@v1", Endpoint.DECISIONS)(Plain)


def test_operations_must_declare_a_payload():
    with pytest.raises(TypeError, match="Payload"):
        request_operation("example.other.thing@v1", Endpoint.FACTS)(RequestOperation)


def test_business_packages_are_discovered_by_request_module_name():
    assert "app.integrations.wms_transport.request" in candidate_modules()


def test_describe_exposes_what_every_display_needs():
    info = registry.lookup("example.fact.report@v1").describe()
    assert info["name"] == "example.fact.report@v1" and info["endpoint"] == "facts"
    assert info["title"] == "example.fact.report@v1"  # 没有标题时用 operation 名
    assert info["ttl_seconds"] is None and info["example"] is None
    assert set(info["payload_schema"]["properties"]) == {"task_id", "qty"}


def test_an_invalid_example_fails_at_registration():
    class Bad(RequestOperation):
        class Payload(registry.RequestPayload):
            qty: int
            model_config = registry.ConfigDict(
                strict=True, extra="forbid", json_schema_extra={"examples": [{"qty": "x"}]}
            )

    with pytest.raises(ValueError, match="example is invalid"):
        request_operation("example.bad.example@v1", Endpoint.FACTS)(Bad)


@pytest.mark.parametrize(
    "annotation,value",
    [
        (datetime, "2026-10-24T00:00:00Z"),
        (date, "2026-10-24"),
        (UUID, "019b76da-a800-7000-8000-000000000001"),
        (Decimal, "1.25"),
    ],
)
def test_json_schema_examples_use_json_validation_at_registration(annotation, value):
    class WithExample(RequestOperation):
        class Payload(registry.RequestPayload):
            field: annotation
            model_config = registry.ConfigDict(json_schema_extra={"examples": [{"field": value}]})

    WithExample.Payload.model_validate_json(json.dumps({"field": value}))
    request_operation("example.json.fields@v1", Endpoint.FACTS)(WithExample)
    assert registry.lookup("example.json.fields@v1").describe()["example"] == {"field": value}
