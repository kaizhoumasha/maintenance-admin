import re

from app.integrations.wms_callback import registry as callbacks
from app.integrations.wms_callback.discovery import load_operation_modules as load_callbacks
from app.integrations.wms_request import operation as requests
from app.integrations.wms_request.discovery import load_operation_modules as load_requests

IN_PACKAGE = re.compile(r"^app\.integrations\.\w+\.(callback|request)\.(\w+)$")


def registered_modules():
    load_callbacks("production")
    load_requests()
    for name, handler in callbacks.operations().items():
        yield name, type(handler).__module__
    for name, registered in requests.operations().items():
        yield name, type(registered.operation).__module__


def test_operation_files_in_a_package_are_named_after_the_operation():
    """`a.b@v1` 必须写在 `a_b_v1.py`:文件名与注册名是两份信息,靠这条规则防止对不上。"""
    checked = 0
    for name, module in registered_modules():
        match = IN_PACKAGE.match(module)
        if match:
            checked += 1
            assert match.group(2) == re.sub(r"[.@]", "_", name), f"{name} is in {module}"
    assert checked  # 至少有一个按目录组织的 operation,规则才算被执行过
