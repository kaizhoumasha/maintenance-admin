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
