from datetime import timedelta

import pytest
from pydantic import ConfigDict

from app.integrations.ecs_callback import registry as ecs
from app.integrations.wms_callback import registry as callbacks
from app.integrations.wms_callback.operation import CallbackPayload
from app.integrations.wms_request import operation as requests
from app.integrations.wms_request.discovery import load_operation_modules as discover_requests


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for registry in (ecs, callbacks, requests):
        monkeypatch.setattr(registry, "_registry", {})
    monkeypatch.setattr(
        "app.integrations.wms_request.discovery.load_operation_modules", lambda: None
    )


class Payload(CallbackPayload):
    qty: int


async def test_callback_functions_reuse_typed_validation_and_results():
    @callbacks.callback_operation("example.function@v1", data=Payload, echo_keys=("task_id",))
    async def handler(ctx, payload):
        assert isinstance(payload, Payload)
        return {"qty": payload.qty}

    registered = callbacks.lookup("example.function@v1")
    assert registered.registration_source.symbol.endswith("handler")
    assert registered.registration_source.module == __name__
    assert registered.echo({"task_id": "T-1"}) == {"task_id": "T-1"}
    parsed = registered.parse({"qty": 2, "extra": True})
    ctx = callbacks.CallbackContext(
        session=None, receipt_id=1, operation_id="id", timestamp=1, parsed=parsed
    )
    assert (await registered.handle(ctx)).data == {"qty": 2}
    assert await handler(ctx, Payload(qty=3)) == {"qty": 3}
    with pytest.raises(callbacks.InvalidPayload):
        registered.parse({"qty": "2"})


def test_request_models_reuse_schema_example_and_ttl():
    ttl = timedelta(minutes=5)

    @requests.request_operation("example.model@v1", requests.Endpoint.FACTS, title="事实", ttl=ttl)
    class Fact(requests.RequestPayload):
        qty: int
        model_config = ConfigDict(json_schema_extra={"examples": [{"qty": 2}]})

    item = requests.lookup("example.model@v1")
    assert item.data_model is Fact
    assert item.registration_source.symbol.endswith("Fact")
    assert item.describe()["title"] == "事实"
    assert item.describe()["ttl_seconds"] == 300
    assert item.describe()["example"] == {"qty": 2}


@pytest.mark.parametrize("kind", ["ecs_event", "callback", "request"])
def test_file_mismatch_explains_the_expected_name(kind):
    if kind == "ecs_event":
        name, target = "SCAN_DONE", async_handler
        decorate = ecs.ecs_event(name, data=Payload)
    elif kind == "callback":
        name, target = "example.done@v1", async_handler
        decorate = callbacks.callback_operation(name, data=Payload)
    else:
        name = "example.done@v1"
        target = type("Fact", (requests.RequestPayload,), {})
        decorate = requests.request_operation(name, requests.Endpoint.FACTS)
    original = target.__module__
    target.__module__ = f"app.integrations.biz.{kind}.wrong"
    try:
        with pytest.raises(ValueError) as error:
            decorate(target)
        assert "wrong" in str(error.value)
        assert name.lower().replace(".", "_").replace("@", "_") + ".py" in str(error.value)
        assert "修复" in str(error.value)
    finally:
        target.__module__ = original


async def async_handler(ctx, payload):
    pass


@pytest.mark.parametrize("kind", ["ecs_event", "callback", "request"])
@pytest.mark.parametrize("invalid_name", ["bad-name", None, 42])
def test_invalid_names_and_duplicates_include_remedies(kind, invalid_name):
    def decorate(name, target):
        if kind == "ecs_event":
            return ecs.ecs_event(name, data=Payload)(target)
        if kind == "callback":
            return callbacks.callback_operation(name, data=Payload)(target)
        return requests.request_operation(name, requests.Endpoint.FACTS)(target)

    target = requests.RequestPayload if kind == "request" else async_handler
    with pytest.raises(ValueError, match="修复"):
        decorate(invalid_name, target)
    name = "SCAN_DONE" if kind == "ecs_event" else "example.done@v1"
    decorate(name, target)
    with pytest.raises(ValueError) as error:
        decorate(name, target)
    assert "修复" in str(error.value) and target.__qualname__ in str(error.value)


@pytest.mark.parametrize("kind", ["ecs_event", "callback"])
def test_function_registrations_require_async_handlers_and_payload_models(kind):
    decorate = ecs.ecs_event if kind == "ecs_event" else callbacks.callback_operation
    name = "SCAN_DONE" if kind == "ecs_event" else "example.done@v1"
    with pytest.raises(TypeError, match="async"):
        decorate(name, data=Payload)(lambda ctx, payload: None)
    with pytest.raises(TypeError, match="data"):
        decorate(name, data=dict)(async_handler)


def test_local_discovery_scope_is_recorded_and_restored_after_failure():
    from app.integrations.registration import local_registration

    with pytest.raises(RuntimeError), local_registration():
        ecs.ecs_event("LOCAL")(async_handler)
        raise RuntimeError("import failed")
    ecs.ecs_event("NORMAL")(async_handler)
    assert ecs.lookup("LOCAL").registration_source.local_only is True
    assert ecs.lookup("NORMAL").registration_source.local_only is False


def test_discovery_records_local_only_from_the_loading_entrypoint(monkeypatch):
    from app.controllers.dev import _load_dev_modules
    from app.integrations import registration
    from app.integrations.ecs_callback.discovery import load_event_modules
    from app.integrations.wms_callback.discovery import load_operation_modules as load_callbacks

    seen = {}

    def import_module(name):
        event_type = f"LOAD_{len(seen)}"
        ecs.ecs_event(event_type)(async_handler)
        seen[name] = ecs.lookup(event_type).registration_source.local_only

    monkeypatch.setattr(registration, "import_module_tree", import_module)
    load_event_modules("local")
    load_callbacks("local")
    discover_requests.__wrapped__()
    _load_dev_modules()
    assert seen
    assert all(
        local_only == (name.rsplit(".", 1)[-1] in {"dev", "dev_callback", "dev_ecs_event"})
        for name, local_only in seen.items()
    )


def test_invalid_request_model_and_example_have_fix_instructions():
    from pydantic import BaseModel

    with pytest.raises(TypeError, match="修复.*RequestPayload"):
        requests.request_operation("example.invalid@v1", requests.Endpoint.FACTS)(BaseModel)

    class Bad(requests.RequestPayload):
        qty: int
        model_config = ConfigDict(json_schema_extra={"examples": [{"qty": "bad"}]})

    with pytest.raises(ValueError, match="example is invalid.*", check=lambda e: "修复" in str(e)):
        requests.request_operation("example.invalid@v1", requests.Endpoint.FACTS)(Bad)


@pytest.mark.parametrize("kind", ["ecs_event", "callback"])
@pytest.mark.parametrize("signature", ["ctx", "ctx, payload, extra", "ctx, *, payload", "*args"])
def test_handlers_with_uncallable_signatures_do_not_enter_the_registry(kind, signature):
    namespace = {"__name__": __name__}
    exec(f"async def handler({signature}): pass", namespace)
    name = "SCAN_DONE" if kind == "ecs_event" else "example.done@v1"
    decorate = ecs.ecs_event if kind == "ecs_event" else callbacks.callback_operation
    lookup = ecs.lookup if kind == "ecs_event" else callbacks.lookup

    with pytest.raises(TypeError, match="ctx, payload"):
        decorate(name, data=Payload)(namespace["handler"])
    assert lookup(name) is None
    decorate(name, data=Payload)(async_handler)
    assert lookup(name).func is async_handler


def test_invalid_request_endpoint_does_not_reserve_the_operation_name():
    name = "example.endpoint@v1"
    with pytest.raises(TypeError, match="Endpoint"):
        requests.request_operation(name, "facts")(requests.RequestPayload)
    assert requests.lookup(name) is None
    requests.request_operation(name, requests.Endpoint.FACTS)(requests.RequestPayload)
    assert requests.lookup(name).endpoint is requests.Endpoint.FACTS
