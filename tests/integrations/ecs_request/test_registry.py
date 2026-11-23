import pytest
from pydantic import BaseModel, ConfigDict

from app.integrations.ecs_callback import CallbackPayload
from app.integrations.ecs_request import DeferredRequest, EcsRequestContext, ecs_request, registry
from app.integrations.ecs_request.envelope import EcsRequestEnvelope


class Decision(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    result: str


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(registry, "_registry", {})


def test_registered_request_is_found_by_role_and_type():
    @ecs_request("CARTON_VALIDATE", device_role="SCAN1", data=CallbackPayload, response=Decision)
    async def handler(ctx, data): ...

    item = registry.lookup("SCAN1", "CARTON_VALIDATE")
    assert item.func is handler
    assert item.data_model is CallbackPayload
    assert item.response_model is Decision
    assert list(registry.requests()) == [("SCAN1", "CARTON_VALIDATE")]
    assert registry.lookup("SCAN1", "OTHER") is None


@pytest.mark.parametrize("name", ["carton_validate", "1A", "A-B", ""])
def test_invalid_request_types_are_rejected(name):
    with pytest.raises(ValueError, match="invalid request type"):
        ecs_request(name, device_role="SCAN1", data=CallbackPayload, response=Decision)(
            lambda ctx, data: None
        )


def test_duplicate_request_bindings_are_rejected():
    @ecs_request("DUP", device_role="SCAN1", data=CallbackPayload, response=Decision)
    async def first(ctx, data): ...

    with pytest.raises(ValueError) as error:

        @ecs_request("DUP", device_role="SCAN1", data=CallbackPayload, response=Decision)
        async def second(ctx, data): ...

    assert f"{first.__module__}.{first.__qualname__}" in str(error.value)
    assert "second" in str(error.value)


def test_response_model_must_forbid_extra_fields():
    class LooseResponse(BaseModel):
        model_config = ConfigDict(extra="ignore")
        result: str

    async def handler(ctx, data):
        return None

    registered = ecs_request(
        "DUP", device_role="SCAN1", data=CallbackPayload, response=LooseResponse
    )(handler)
    assert registered is handler


def test_context_enforces_one_final_result_and_allows_same_defer():
    ctx = EcsRequestContext(
        session=None,
        receipt_id=1,
        digest="digest",
        device_code="D1",
        request_type="DUP",
        timestamp=1,
        binding=None,
        response_model=Decision,
    )
    ctx.respond(Decision(result="ALLOW"))
    with pytest.raises(ValueError, match="already has a result"):
        ctx.respond(Decision(result="REJECT"))
    with pytest.raises(ValueError, match="already has a result"):
        ctx.defer(1, "wms-facts", "op-1")

    waiting = EcsRequestContext(
        session=None,
        receipt_id=1,
        digest="digest",
        device_code="D1",
        request_type="DUP",
        timestamp=1,
        binding=None,
        response_model=Decision,
    )
    waiting.defer(1, "wms-facts", "op-1")
    waiting.defer(1, "wms-facts", "op-1")
    with pytest.raises(ValueError, match="different deferred request"):
        waiting.defer(2, "wms-facts", "op-2")
    with pytest.raises(ValueError, match="already has a result"):
        waiting.respond(Decision(result="ALLOW"))

    resumed = EcsRequestContext(
        session=None,
        receipt_id=1,
        digest="digest",
        device_code="D1",
        request_type="DUP",
        timestamp=1,
        binding=None,
        response_model=Decision,
        deferred=DeferredRequest(message_id=1, operation="wms-facts", operation_id="op-1"),
    )
    resumed.respond(Decision(result="ALLOW"))
    assert resumed.response == Decision(result="ALLOW")
    mixed = EcsRequestContext(
        session=None,
        receipt_id=1,
        digest="digest",
        device_code="D1",
        request_type="DUP",
        timestamp=1,
        binding=None,
        response_model=Decision,
        deferred=DeferredRequest(message_id=1, operation="wms-facts", operation_id="op-1"),
    )
    mixed.defer(1, "wms-facts", "op-1")
    with pytest.raises(ValueError, match="already has a result"):
        mixed.respond(Decision(result="REJECT"))


def test_envelope_defaults_data_and_ignores_extra_fields():
    envelope = EcsRequestEnvelope.model_validate(
        {"device_code": "D1", "request_type": "DUP", "timestamp": 1, "unknown": "ignored"}
    )
    assert envelope.data == {}


def test_deferred_request_is_small_and_immutable():
    deferred = DeferredRequest(message_id=1, operation="wms-facts", operation_id="op-1")
    with pytest.raises(AttributeError):
        deferred.message_id = 2  # type: ignore[misc]
