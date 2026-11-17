"""Discovery tests: python sources, data files, excludes/includes."""

from pyrefactor.discovery import (
    discover,
    discover_data_files,
    detect_base,
    detect_pyproject_config,
)


# content of a single package directory (no prefix)
PKG = {
    "__init__.py": "from .core import app\n",
    "core.py": "app = 'core'\n",
    "helpers.py": "value = 1\n",
    "data/settings.json": '{"a": 1}\n',
    "data/config.ini": "[m]\nk = v\n",
    "assets/font.ttf": b"\x00\x01binary\x00",
    "tests/test_core.py": "def test_x():\n    assert True\n",
}

# content of a project root that *contains* a package
PROJ = {
    "pkg/__init__.py": "from .core import app\n",
    "pkg/core.py": "app = 'core'\n",
    "pkg/helpers.py": "value = 1\n",
    "pkg/tests/test_core.py": "def test_x():\n    assert True\n",
}


def test_single_file_discovery(make_project):
    src = make_project({"tool.py": "x = 1\n"}, root_name="single")
    files = discover(src / "tool.py")
    assert len(files) == 1
    assert files[0].module == "tool"
    assert files[0].is_pkg is False


def test_package_discovery_and_module_names(make_project):
    src = make_project(PROJ, root_name="proj")
    files = discover(src)
    modules = {f.module for f in files}
    assert "pkg" in modules
    assert "pkg.core" in modules
    assert "pkg.helpers" in modules
    # default excludes: tests/
    assert not any("test" in m for m in modules)
    init = next(f for f in files if f.module == "pkg")
    assert init.is_pkg is True


def test_excluded_test_files_can_be_reincluded(make_project):
    src = make_project(PROJ, root_name="proj")
    files = discover(src, include=["pkg/tests/*"])
    assert any("test_core" in f.module for f in files)


def test_data_files_relative_to_source_root(make_project):
    """Data rel paths must match Path(__file__).parent layout, not add pkg prefix."""
    src = make_project(PKG)  # src IS the package dir
    data = {d.relative: d for d in discover_data_files(src)}
    assert "data/settings.json" in data
    assert "data/config.ini" in data
    assert "assets/font.ttf" in data
    assert "tests/test_core.py" not in data
    # glob excludes use fnmatch, so a `--exclude data/test_*` filter works
    src2 = make_project({**PKG, "data/test_stuff.txt": "x\n"}, root_name="pkg2")
    rels = {d.relative for d in discover_data_files(src2, exclude=["data/test_*"])}
    assert "data/test_stuff.txt" not in rels


def test_data_files_in_project_layout_keep_package_prefix(make_project):
    src = make_project(
        {**PROJ, "pkg/data/cfg.json": "{}\n"}, root_name="proj2"
    )
    rels = {d.relative for d in discover_data_files(src)}
    assert "pkg/data/cfg.json" in rels


def test_data_discovery_disabled(make_project):
    src = make_project(PKG, root_name="pkg3")
    assert discover_data_files(src, copy_data=False) == []
    assert discover_data_files(src, copy_data=False, collect=True) != []


def test_data_glob_filter(make_project):
    src = make_project(PKG, root_name="pkg4")
    data = discover_data_files(src)
    kept = [d for d in data if d.relative.endswith(".json")]
    assert len(kept) == 1
    assert kept[0].relative == "data/settings.json"


def test_detect_base_variants(make_project):
    pkg = make_project(PKG, root_name="d1")
    assert detect_base(pkg) == pkg.parent  # package -> parent
    proj = make_project(PROJ, root_name="proj3")
    assert detect_base(proj) == proj  # project root
    src2 = make_project(
        {
            "src/other/__init__.py": "",
            "src/other/mod.py": "x = 1\n",
        },
        root_name="proj4",
    )
    assert detect_base(src2) == src2 / "src"  # src layout


def test_detect_pyproject_config(make_project):
    src = make_project(
        {
            **PKG,
            "pyproject.toml": '[tool.pyrefactor]\nexclude = ["helpers/*"]\nimports = "keep"\n',
        },
        root_name="proj5",
    )
    cfg = detect_pyproject_config(src)
    assert cfg["imports"] == "keep"
    assert cfg["exclude"] == ["helpers/*"]
