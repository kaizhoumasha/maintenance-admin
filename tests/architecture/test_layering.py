"""分层约束:controllers → services → models,不得反向依赖(README 约定,这里用测试强制)。"""

import ast
from pathlib import Path

APP = Path(__file__).parents[2] / "app"

# 目录 → 它不能导入的包
FORBIDDEN = {
    "http_client": (
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
        "app.controllers",
        "app.integrations",
        "app.services",
        "app.schemas",
        "fastapi",
        "celery",
    ),
    "models": ("app.services", "app.controllers", "app.schemas"),
    "schemas": ("app.services", "app.controllers"),
    "services": ("app.controllers",),
    "core": ("app.services", "app.controllers"),
}


def violations(root: Path) -> list[str]:
    found = []
    for layer, banned in FORBIDDEN.items():
        for path in (root / layer).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                elif isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                else:
                    continue
                found += [
                    f"{path.relative_to(root.parent)}: {n}"
                    for n in names
                    if any(n == b or n.startswith(b + ".") for b in banned)
                ]
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
