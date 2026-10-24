import re

from app.integrations.ecs_callback import registry as ecs_events
from app.integrations.ecs_callback.discovery import load_event_modules
from app.integrations.registration import registration_filename
from app.integrations.wms_callback import registry as callbacks
from app.integrations.wms_callback.discovery import load_operation_modules as load_callbacks
from app.integrations.wms_request import operation as requests

IN_PACKAGE = re.compile(r"^app\.integrations\.\w+\.(callback|request|ecs_event)\.(\w+)$")


def violation(name: str, module: str) -> str | None:
    """`a.b@v1` 必须写在 `a_b_v1.py`:文件名与注册名是两份信息,靠这条规则防止对不上。
    只检查目录形式(`<业务>/callback/`、`<业务>/request/`)里的文件。"""
    match = IN_PACKAGE.match(module)
    if match and match.group(2) != registration_filename(name).removesuffix(".py"):
        return f"{name} is in {module}"
    return None


def registered_modules():
    load_callbacks("production")
    for name, handler in callbacks.operations().items():
        yield name, handler.registration_source.module
    for name, registered in requests.operations().items():
        yield name, registered.registration_source.module


def test_registered_operation_files_are_named_after_the_operation():
    assert [v for name, module in registered_modules() if (v := violation(name, module))] == []


def test_the_rule_covers_both_directions_and_ignores_single_modules():
    for kind in ("callback", "request"):
        base = f"app.integrations.biz.{kind}"
        assert violation("biz.thing.done@v1", f"{base}.biz_thing_done_v1") is None
        assert violation("biz.thing.done@v1", f"{base}.done") is not None
        assert violation("biz.thing.done@v2", f"{base}.biz_thing_done_v1") is not None
        assert violation("biz.thing.done@v1", base) is None  # 单个模块形式不检查
    assert violation("example.fact.demo@v1", "app.integrations.wms_request.dev") is None


def test_registered_ecs_event_files_are_named_after_the_event_type():
    load_event_modules("production")
    assert [
        v
        for event_type, registered in ecs_events.events().items()
        if (v := violation(event_type, registered.registration_source.module))
    ] == []


def test_the_ecs_rule_covers_both_directions_and_ignores_single_modules():
    base = "app.integrations.biz.ecs_event"
    assert violation("SCAN_DONE", f"{base}.scan_done") is None
    assert violation("SCAN_DONE", f"{base}.done") is not None
    assert violation("SCAN_DONE", base) is None  # 单个模块形式不检查
