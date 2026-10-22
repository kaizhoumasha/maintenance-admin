import pytest

from app.integrations.wms_callback import registry
from app.integrations.wms_callback.registry import Accepted, callback_operation


class Handler:
    def parse(self, data):
        return data

    async def handle(self, ctx):
        return Accepted()


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    monkeypatch.setattr(registry, "_registry", {})


def test_registered_handler_is_found_by_operation_name():
    callback_operation("example.thing@v1")(Handler)
    assert isinstance(registry.lookup("example.thing@v1"), Handler)
    assert registry.lookup("example.other@v1") is None


@pytest.mark.parametrize("name", ["single@v1", "A.b@v1", "a.b", "a.b@v0", "a..b@v1", "a.b@1"])
def test_invalid_names_are_rejected_at_registration(name):
    with pytest.raises(ValueError, match="invalid operation name"):
        callback_operation(name)(Handler)


def test_duplicate_names_are_rejected_and_name_both_handlers():
    callback_operation("example.thing@v1")(Handler)

    class Other(Handler):
        pass

    with pytest.raises(ValueError, match="duplicate.*Handler.*Other"):
        callback_operation("example.thing@v1")(Other)


@pytest.mark.parametrize("missing", ["parse", "handle"])
def test_handlers_must_define_both_phases(missing):
    broken = type("Broken", (), {"parse": lambda s, d: d, "handle": lambda s, c: None})
    delattr(broken, missing)
    with pytest.raises(TypeError, match=missing):
        callback_operation("example.thing@v1")(broken)
