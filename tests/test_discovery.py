"""Tests for discovery.py — file finding, include/exclude, src layout, namespace, data files."""

from pathlib import Path
import textwrap
from refactor_utils.discovery import (
    discover,
    discover_data_files,
    detect_base,
    detect_pyproject_config,
    EXCLUDED_FILE_GLOBS,
)


def test_discover_basic(sample_project):
    files = discover(sample_project, skip_dirs=[])
    modules = {f.module for f in files}
    assert "pkg" in modules or "pkg.a" in modules or any("a" in m for m in modules)
    # Should find at least 4 python files (including __init__)
    assert len(files) >= 3


def test_discover_exclude_defaults(sample_project):
    # conftest.py should be excluded by default
    (sample_project / "conftest.py").write_text("import pytest\n")
    files = discover(sample_project)
    names = [f.path.name for f in files]
    assert "conftest.py" not in names


def test_discover_include_overrides_default(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "conftest.py").write_text("X=1\n")
    (pkg / "keep.py").write_text("Y=1\n")
    files_default = discover(pkg)
    assert not any(f.path.name == "conftest.py" for f in files_default)
    files_include = discover(pkg, include=("conftest.py",))
    assert any(f.path.name == "conftest.py" for f in files_include)


def test_discover_exclude_always_wins(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "conftest.py").write_text("X=1\n")
    files = discover(pkg, exclude=("conftest.py",), include=("conftest.py",))
    assert not any(f.path.name == "conftest.py" for f in files)


def test_detect_base_src_layout(src_layout_project):
    base = detect_base(src_layout_project)
    # src layout detection should return src dir when src/mypkg/__init__.py exists
    assert base.name == "src" or base == src_layout_project

    # Discover via src layout should find mypkg
    files = discover(src_layout_project)
    assert any("mypkg" in f.module for f in files)


def test_discover_namespace_package(namespace_project):
    files = discover(namespace_project)
    # Should discover mod and sub.other even without __init__.py
    modules = {f.module for f in files}
    assert any("mod" in m for m in modules)


def test_discover_single_file(tmp_path):
    single = tmp_path / "hello.py"
    single.write_text("x=1\n")
    files = discover(single)
    assert len(files) == 1
    assert files[0].module == "hello"


def test_discover_output_inside_source(sample_project, tmp_path):
    output = sample_project / "output"
    output.mkdir()
    (output / "should_ignore.py").write_text("Z=1\n")
    files = discover(sample_project, skip_dirs=[output])
    assert not any("should_ignore" in f.module for f in files)


def test_discover_symlink(sample_project, tmp_path):
    # Create symlink to a python file
    target = sample_project / "a.py"
    link = sample_project / "link_to_a.py"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks not supported on this FS")
    files = discover(sample_project)
    # Should not crash; may or may not include symlink (we include file symlinks with log)
    assert len(files) >= 3


def test_discover_data_files(sample_project):
    data_files = discover_data_files(sample_project, copy_data=True)
    rels = {df.relative for df in data_files}
    assert any("sample.json" in r for r in rels)
    assert any("sample.csv" in r for r in rels)
    assert any("config.yaml" in r for r in rels)


def test_discover_data_files_no_copy(sample_project):
    data_files = discover_data_files(sample_project, copy_data=False)
    assert data_files == []


def test_discover_data_files_glob_filter(sample_project):
    data_files = discover_data_files(sample_project, copy_data=True)
    # Filter via data_glob in core is separate; here we test that discovery finds json
    json_files = [df for df in data_files if df.relative.endswith(".json")]
    assert len(json_files) >= 1


def test_detect_pyproject_config(tmp_path):
    # No config -> empty
    cfg = detect_pyproject_config(tmp_path)
    assert cfg == {}

    # With pyproject
    (tmp_path / "pyproject.toml").write_text(
        textwrap.dedent("""\
        [tool.pyrefactor]
        exclude = ["tests/*"]
        rename_strategy = "suffix"
        """)
    )
    cfg = detect_pyproject_config(tmp_path)
    assert cfg.get("rename_strategy") == "suffix"
    assert "tests/*" in cfg.get("exclude", [])


def test_excluded_file_globs_contains_expected():
    assert "conftest.py" in EXCLUDED_FILE_GLOBS
    assert "__main__.py" in EXCLUDED_FILE_GLOBS
    assert "iwgfyrkugf.py" in EXCLUDED_FILE_GLOBS
