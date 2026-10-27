import os
import subprocess
import sys

import pytest

from app.businesses import catalog
from tests.helpers import business_composition


def test_entry_candidates_are_fixed_metadata(monkeypatch):
    entry = business_composition()
    entry.ENTRY_MODULES += (
        "app.businesses.test_sorting.request",
        "app.businesses.test_sorting.dev",
        "app.businesses.test_sorting.dev_callback",
    )
    monkeypatch.setattr(catalog, "entries", lambda: (entry,))
    assert catalog.entry_modules("request", environment="production") == (
        "app.businesses.test_sorting.request",
    )
    assert catalog.entry_modules("request", environment="local") == (
        "app.businesses.test_sorting.request",
        "app.businesses.test_sorting.dev",
    )
    assert "app.businesses.test_sorting.request" not in sys.modules


def test_model_loading_is_independent_of_configured_worklines(tmp_path):
    package = tmp_path / "managed"
    package.mkdir()
    (package / "__init__.py").touch()
    (package / "models.py").write_text("""
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base
class ManagedHistory(Base):
    __tablename__ = 'managed_history'
    id: Mapped[int] = mapped_column(primary_key=True)
""")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib.abc
import sys
from types import ModuleType
from app import businesses
from app.businesses import catalog
from app.models import Base
businesses.__path__ = [sys.argv[1]]
class BlockRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('app.services', 'app.main', 'app.integrations',
                                'app.businesses.managed.services')):
            raise AssertionError('runtime import: ' + fullname)
sys.meta_path.insert(0, BlockRuntime())
entry = ModuleType('app.businesses.managed.composition')
entry.MODEL_MODULES = ('app.businesses.managed.models',)
def factory(*args): raise AssertionError('factory called')
entry.build_service = factory
catalog.entries = lambda: (entry,)
assert not any(table.name == 'managed_history' for table in Base.metadata.tables.values())
catalog.load_business_models()
catalog.load_business_models()
assert any(table.name == 'managed_history' for table in Base.metadata.tables.values())
""",
            str(tmp_path),
        ],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_missing_explicit_model_fails(monkeypatch):
    entry = business_composition("missing")
    entry.MODEL_MODULES = ("app.businesses.missing.models",)
    monkeypatch.setattr(catalog, "entries", lambda: (entry,))
    with pytest.raises(ModuleNotFoundError):
        catalog.load_business_models()


@pytest.mark.parametrize("environment", ["production", "local"])
@pytest.mark.parametrize("kind", ["ecs_event", "callback", "request"])
@pytest.mark.parametrize(
    "path",
    [
        "app.businesses.test_sorting.reqeust",
        "app.businesses.test_sorting.dev_ecs_events",
        "app.businesses.other.request",
        "app.businesses.other.dev",
        "app.integrations.test_sorting.request",
        "app.businesses.test_sorting",
        "app.businesses.test_sorting.request..followup",
        None,
    ],
)
def test_every_declared_entry_layout_and_owner_is_checked_before_filtering(
    monkeypatch, environment, kind, path
):
    entry = business_composition()
    entry.ENTRY_MODULES += (path,)
    monkeypatch.setattr(catalog, "entries", lambda: (entry,))
    with pytest.raises(ValueError) as error:
        catalog.entry_modules(kind, environment=environment)
    assert entry.__name__ in str(error.value)
    assert str(path) in str(error.value)


def test_formal_and_local_package_members_keep_supported_layouts(monkeypatch):
    entry = business_composition()
    entry.ENTRY_MODULES = (
        "app.businesses.test_sorting.request.followup_v1",
        "app.businesses.test_sorting.dev.extra",
    )
    monkeypatch.setattr(catalog, "entries", lambda: (entry,))
    assert catalog.entry_modules("request", environment="production") == entry.ENTRY_MODULES[:1]
    assert catalog.entry_modules("request", environment="local") == entry.ENTRY_MODULES
