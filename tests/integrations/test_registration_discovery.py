import importlib
import sys

import pytest

from app import integrations
from app.integrations.ecs_callback import registry as ecs
from app.integrations.registration import import_registration_module, local_registration
from app.integrations.wms_callback import registry as callbacks
from app.integrations.wms_request import discovery as request_discovery
from app.integrations.wms_request import operation as requests


@pytest.fixture(autouse=True)
def isolated_registries(monkeypatch):
    for registry in (ecs, callbacks, requests):
        monkeypatch.setattr(registry, "_registry", {})
    # 执行真实发现，但不污染进程级发现缓存。
    monkeypatch.setattr(
        request_discovery,
        "load_operation_modules",
        request_discovery.load_operation_modules.__wrapped__,
    )


@pytest.fixture
def business_modules(tmp_path, monkeypatch):
    monkeypatch.setattr(integrations, "__path__", [str(tmp_path)])
    prefixes = set()

    def write(path, source=""):
        parts = path.split(".")
        prefix = f"app.integrations.{parts[0]}"
        if prefix not in prefixes:
            monkeypatch.setattr(integrations, parts[0], None, raising=False)
            prefixes.add(prefix)
        parent = tmp_path
        for part in parts[:-1]:
            parent /= part
            parent.mkdir(exist_ok=True)
            (parent / "__init__.py").touch()
        (parent / f"{parts[-1]}.py").write_text(source)
        importlib.invalidate_caches()
        return f"app.integrations.{path}"

    yield write
    for name in list(sys.modules):
        if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes):
            sys.modules.pop(name)


def declaration(kind, name="example.done@v1"):
    if kind == "ecs_event":
        return (
            "from app.integrations.ecs_callback import ecs_event\n"
            f"@ecs_event({name!r})\n"
            "async def handler(ctx, payload): pass\n"
        )
    if kind == "callback":
        return (
            "from app.integrations.wms_callback import CallbackPayload, callback_operation\n"
            f"@callback_operation({name!r}, data=CallbackPayload)\n"
            "async def handler(ctx, payload): pass\n"
        )
    return (
        "from app.integrations.wms_request import Endpoint, RequestPayload, request_operation\n"
        f"@request_operation({name!r}, Endpoint.FACTS)\n"
        "class Payload(RequestPayload): pass\n"
    )


def registered(kind, name):
    return {"ecs_event": ecs.lookup, "callback": callbacks.lookup, "request": requests.lookup}[
        kind
    ](name)


@pytest.mark.parametrize(
    "kind,path,name",
    [
        ("ecs_event", "guard_probe.ecs_event.dev", "DEV"),
        ("ecs_event", "dev.ecs_event.scan_done", "SCAN_DONE"),
        ("callback", "guard_probe.callback.dev.example_done_v1", "example.done@v1"),
        ("request", "guard_probe.request.dev.example_done_v1", "example.done@v1"),
    ],
)
def test_dev_named_production_paths_cannot_register_as_local(business_modules, kind, path, name):
    module = business_modules(path, declaration(kind, name))
    with pytest.raises(RuntimeError, match="修复"), local_registration():
        # 普通 import 也必须受到注册边界保护。
        importlib.import_module(module)


@pytest.mark.parametrize(
    "kind,entry,name,file",
    [
        ("ecs_event", "dev_ecs_event", "SCAN_DONE", "scan_done"),
        ("callback", "dev_callback", "example.done@v1", "example_done_v1"),
        ("request", "dev", "example.done@v1", "example_done_v1"),
    ],
)
def test_actual_dev_entrypoints_register_as_local(business_modules, kind, entry, name, file):
    module = business_modules(f"guard_probe.{entry}.{file}", declaration(kind, name))
    import_registration_module(module, local_only=True)
    assert registered(kind, name).registration_source.local_only is True


@pytest.mark.parametrize("kind", ["ecs_event", "callback", "request"])
def test_cached_production_import_is_safe_in_local_scope(business_modules, kind):
    name = "SCAN_DONE" if kind == "ecs_event" else "example.done@v1"
    file = "scan_done" if kind == "ecs_event" else "example_done_v1"
    module = business_modules(f"guard_probe.{kind}.{file}", declaration(kind, name))
    import_registration_module(module)
    original = registered(kind, name)
    with local_registration():
        import_registration_module(module)
    assert registered(kind, name) is original
    assert original.registration_source.local_only is False


@pytest.mark.parametrize("cached", [False, True])
def test_request_catalog_can_be_discovered_from_dev_scope(business_modules, cached):
    if cached:
        module = business_modules("guard_probe.request.example_done_v1", declaration("request"))
        import_registration_module(module)
    else:
        business_modules("guard_probe.helper")  # 没有 request 入口，发现应跳过。
    with local_registration():
        items = requests.operations()
    assert set(items) == ({"example.done@v1"} if cached else set())
    assert all(not item.registration_source.local_only for item in items.values())


@pytest.mark.parametrize("kind", ["ecs_event", "callback", "request"])
def test_fresh_production_registration_is_blocked_and_scope_recovers(business_modules, kind):
    name = "SCAN_DONE" if kind == "ecs_event" else "example.done@v1"
    file = "scan_done" if kind == "ecs_event" else "example_done_v1"
    module = business_modules(f"guard_probe.{kind}.{file}", declaration(kind, name))
    with pytest.raises(RuntimeError, match="修复"), local_registration():
        import_registration_module(module)
    assert module not in sys.modules
    import_registration_module(module)
    assert registered(kind, name).registration_source.local_only is False


@pytest.mark.parametrize("kind", ["ecs_event", "callback", "request"])
@pytest.mark.parametrize("package", [False, True])
def test_business_entrypoints_can_register_without_per_operation_files(
    business_modules, kind, package
):
    name = "SCAN_DONE" if kind == "ecs_event" else "example.done@v1"
    entry = f"guard_probe.{kind}"
    module = business_modules(entry + (".__init__" if package else ""), declaration(kind, name))
    import_registration_module(module.removesuffix(".__init__"))
    assert registered(kind, name).registration_source.module == f"app.integrations.{entry}"
    assert registered(kind, name).registration_source.local_only is False
