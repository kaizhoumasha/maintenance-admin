import pytest

from app.integrations.wms_callback import CallbackPayload, registry
from app.integrations.wms_callback.registry import callback_operation


async def handler(ctx, payload):
    pass


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    monkeypatch.setattr(registry, "_registry", {})


def test_registered_handler_is_found_by_operation_name():
    callback_operation("example.thing@v1", data=CallbackPayload)(handler)
    assert registry.lookup("example.thing@v1").func is handler
    assert registry.lookup("example.other@v1") is None


@pytest.mark.parametrize("name", ["single@v1", "A.b@v1", "a.b", "a.b@v0", "a..b@v1", "a.b@1"])
def test_invalid_names_are_rejected_at_registration(name):
    with pytest.raises(ValueError, match="invalid operation name"):
        callback_operation(name, data=CallbackPayload)(handler)


def test_duplicate_names_are_rejected_and_name_both_handlers():
    callback_operation("example.thing@v1", data=CallbackPayload)(handler)

    async def other(ctx, payload):
        pass

    with pytest.raises(ValueError, match="duplicate.*handler.*other"):
        callback_operation("example.thing@v1", data=CallbackPayload)(other)
