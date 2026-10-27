"""分层约束:controllers → services → models,不得反向依赖(README 约定,这里用测试强制)。"""

import ast
import sys
from importlib.util import resolve_name
from pathlib import Path

import pytest

APP = Path(__file__).parents[2] / "app"

# 目录 → 它不能导入的包
FORBIDDEN = {
    "integrations/ecs_status": (
        "app.core",
        "app.integrations.registration",
        "app.integrations.ecs_command",
        "app.models",
        "app.services",
        "app.controllers",
        "app.schemas",
        "app.tasks",
        "app.reliable",
        "sqlalchemy",
        "redis",
        "fastapi",
        "celery",
    ),
    "http_client": (
        "app.businesses",
        "app.core",
        "app.reliable",
        "app.integrations",
        "app.services",
        "app.controllers",
        "app.models",
        "app.schemas",
        "app.tasks",
        "sqlalchemy",
        "redis",
        "celery",
        "fastapi",
    ),
    "reliable": (
        "app.businesses",
        "app.controllers",
        "app.integrations",
        "app.services",
        "app.schemas",
        "fastapi",
        "celery",
    ),
    "integrations": (
        "app.businesses",
        "app.controllers",
        "app.schemas",
        "app.services",
        "fastapi",
        "celery",
    ),
    "models": ("app.services", "app.controllers", "app.schemas"),
    "schemas": ("app.services", "app.controllers"),
    "services": ("app.controllers",),
    "core": ("app.services", "app.controllers", "app.businesses"),
}


def imports(path: Path, root: Path):
    module = "app." + ".".join(path.relative_to(root).with_suffix("").parts)
    package = module.rsplit(".", 1)[0]
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            name = node.module or ""
            if node.level:
                name = resolve_name("." * node.level + name, package)
            if (
                not node.module
                or name in {"app", "app.businesses"}
                or (
                    module.startswith("app.businesses.") and name == ".".join(module.split(".")[:3])
                )
            ):
                yield from (f"{name}.{alias.name}" for alias in node.names)
            else:
                yield name


def violations(root: Path) -> list[str]:
    found = []
    for layer, banned in FORBIDDEN.items():
        for path in (root / layer).rglob("*.py"):
            for name in imports(path, root):
                if (
                    path.relative_to(root).as_posix() == "integrations/discovery.py"
                    and name == "app.businesses.catalog"
                ):
                    continue
                if any(name == b or name.startswith(b + ".") for b in banned):
                    found.append(f"{path.relative_to(root.parent)}: {name}")
    for path in (root / "businesses").rglob("*.py"):
        parts = path.relative_to(root).parts
        business = ".".join(("app", *parts[:2]))
        layer = parts[2].removesuffix(".py") if len(parts) > 2 else ""
        banned = ["app.controllers", "app.tasks"]
        entries = ("ecs_event", "callback", "request", "dev", "dev_callback", "dev_ecs_event")
        if layer in {"service", "services", "models"}:
            banned += [f"{business}.{entry}" for entry in (*entries, "composition")]
        if layer == "models":
            banned += [
                f"{business}.service",
                f"{business}.services",
                "app.services",
                "app.schemas",
            ]
        for name in imports(path, root):
            pure = path.name in {"definition.py", "definitions.py"}
            pure_violation = (
                pure
                and name.split(".")[0] not in sys.stdlib_module_names
                and name not in {"app.businesses.definitions", f"{business}.constants"}
            )
            if pure_violation or any(name == b or name.startswith(b + ".") for b in banned):
                found.append(f"{path.relative_to(root.parent)}: {name}")
    return found


def test_no_reverse_dependencies():
    assert violations(APP) == []


def test_checker_detects_violation(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "bad.py").write_text("from app.services import user_service\n")
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "bad.py").write_text("import app.controllers.users\n")
    (tmp_path / "services" / "ok.py").write_text("from app.models import User\n")
    assert [v.split(": ")[1] for v in violations(tmp_path)] == [
        "app.services",
        "app.controllers.users",
    ]


def test_http_client_cannot_import_application_infrastructure(tmp_path):
    (tmp_path / "http_client").mkdir()
    (tmp_path / "http_client" / "bad.py").write_text(
        "from app.core import logger\nimport sqlalchemy\n"
    )
    assert len(violations(tmp_path)) == 2


def test_integrations_cannot_import_web_or_service_layers(tmp_path):
    (tmp_path / "integrations").mkdir()
    (tmp_path / "integrations" / "bad.py").write_text(
        "from app.services import user_service\nimport fastapi\nfrom app.reliable import Outbox\n"
    )
    assert [v.split(": ")[1] for v in violations(tmp_path)] == ["app.services", "fastapi"]


def test_generic_layers_cannot_import_concrete_businesses(tmp_path):
    for layer in ("http_client", "reliable", "core", "integrations"):
        folder = tmp_path / layer
        folder.mkdir()
        (folder / "bad.py").write_text("from app.businesses.probe import composition\n")
    assert len(violations(tmp_path)) == 4


def test_discovery_can_read_catalog_but_not_concrete_business(tmp_path):
    folder = tmp_path / "integrations"
    folder.mkdir()
    (folder / "discovery.py").write_text(
        "from app.businesses import catalog\nfrom app.businesses.probe import composition\n"
    )
    assert len(violations(tmp_path)) == 1
    assert "app.businesses.probe" in violations(tmp_path)[0]


@pytest.mark.parametrize("service_layer", ["service", "services"])
def test_business_services_models_and_definition_keep_their_layers(tmp_path, service_layer):
    business = tmp_path / "businesses" / "probe"
    (business / "models").mkdir(parents=True)
    (business / service_layer).mkdir()
    (business / "models" / "bad.py").write_text(f"from ..{service_layer} import worker\n")
    (business / service_layer / "bad.py").write_text("from ..callback import handler\n")
    (business / "definition.py").write_text("from sqlalchemy import Column\n")
    (business / "callback.py").write_text(f"from .{service_layer} import worker\n")
    assert len(violations(tmp_path)) == 3


@pytest.mark.parametrize("service_layer", ["service", "services"])
@pytest.mark.parametrize("layout", ["module", "package"])
@pytest.mark.parametrize(
    "layer,dependency,forbidden",
    [
        ("models", "{service}", True),
        ("{service}", "callback", True),
        ("{service}", "composition", True),
        ("{service}", "models", False),
        ("callback", "{service}", False),
    ],
)
def test_business_package_imports_preserve_layer_boundaries(
    tmp_path, service_layer, layout, layer, dependency, forbidden
):
    business = tmp_path / "businesses" / "probe"
    layer = layer.format(service=service_layer)
    dependency = dependency.format(service=service_layer)
    path = business / (f"{layer}.py" if layout == "module" else f"{layer}/worker.py")
    path.parent.mkdir(parents=True)
    path.write_text(f"from app.businesses.probe import {dependency}\n")
    assert bool(violations(tmp_path)) is forbidden
