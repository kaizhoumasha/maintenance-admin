import sys

import pytest

from app.core.module_tree import import_module_tree


@pytest.fixture
def tree(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    yield tmp_path
    for name in [n for n in sys.modules if n.startswith("modtree_")]:
        del sys.modules[name]


def test_package_is_imported_with_every_submodule(tree):
    package = tree / "modtree_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "one.py").write_text("")
    (package / "two.py").write_text("")
    import_module_tree("modtree_pkg")
    assert {"modtree_pkg.one", "modtree_pkg.two"} <= set(sys.modules)


def test_plain_module_is_imported(tree):
    (tree / "modtree_plain.py").write_text("")
    import_module_tree("modtree_plain")
    assert "modtree_plain" in sys.modules


def test_missing_module_is_skipped_but_inner_import_errors_surface(tree):
    import_module_tree("modtree_absent")
    (tree / "modtree_broken.py").write_text("import modtree_not_there\n")
    with pytest.raises(ModuleNotFoundError, match="modtree_not_there"):
        import_module_tree("modtree_broken")
