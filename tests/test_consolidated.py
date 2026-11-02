"""Tests for the new ``consolidate_package`` flow (``pyrefactor [pkg]``).

Contract (prompt.md section 1 & 2):

* ``pyrefactor <pkg path>`` (path optional, defaults to ``.``) creates a
  ``consolidated`` subdir containing a single ``<pkgname>.py`` file.
* The package name is detected from ``pyproject.toml``/``setup.cfg``/
  ``setup.py`` in the package folder and up to two directory levels above it;
  when nothing is found, the current folder's name is used.
* Extraction: constants → ``const.py``, functions → ``func.py``, classes →
  ``classes.py`` (temporary, then removed), imports are deduplicated with
  local/relative imports stripped, and the optimized import section plus the
  section contents are written to ``consolidated/<pkgname>.py``.
* ``autoall`` appends an ``__all__`` section to the generated file.
"""

import textwrap
from pathlib import Path

import pytest
from refactor_utils.core import ConsolidationError, consolidate_package
from refactor_utils.discovery import detect_pkg_name

from autoall import ensure_all_file


def write_pkg(root: Path, name: str = "pkg", files: dict[str, str] | None = None) -> Path:
    """Write a small package under ``root/name`` and return its path."""
    pkg = root / name
    pkg.mkdir()
    (pkg / "__init__.py").write_text('"""pkg init."""\n')
    for fname, content in (files or {}).items():
        (pkg / fname).write_text(textwrap.dedent(content))
    return pkg


def test_consolidate_package_basic(tmp_path: Path):
    """A package consolidates into a single compilable ``<pkgname>.py`` file."""
    pkg = write_pkg(
        tmp_path,
        files={
            "a.py": '''
                """module a"""
                X = 1
                def foo(x=X):
                    """docstring to keep"""
                    return x + 1
            ''',
            "b.py": '''
                """module b"""
                import json
                Y = 2
                class Bar:
                    """Bar doc"""
                    def method(self):
                        return Y
            ''',
        },
    )
    out = tmp_path / "out"
    result = consolidate_package(pkg, out)

    assert result.modules >= 2
    assert result.pkg_name == "pkg"
    assert result.all_added is True
    target = out / "pkg.py"
    assert target.exists()
    assert result.files == [target]
    # Compiles
    compile(target.read_text(), target.name, "exec")
    text = target.read_text()
    assert "X = 1" in text
    assert "Y = 2" in text
    assert "class Bar" in text
    assert "def foo" in text
    # __all__ section added by autoall
    assert "__all__" in text
    assert "'foo'" in text and "'Bar'" in text and "'X'" in text


def test_consolidate_package_default_output(tmp_path: Path):
    """With no output, ``consolidated`` is created inside the package dir."""
    pkg = write_pkg(tmp_path, files={"a.py": "X = 1\ndef foo(): return X\n"})
    result = consolidate_package(pkg)
    assert result.output_dir == pkg / "consolidated"
    assert (pkg / "consolidated" / "pkg.py").exists()
    # No temp section files left behind
    assert not (pkg / "consolidated" / "const.py").exists()
    assert not (pkg / "consolidated" / "classes.py").exists()
    assert not (pkg / "consolidated" / "func.py").exists()


def test_pkg_name_from_pyproject(tmp_path: Path):
    """``pyproject.toml`` [project] name wins."""
    pkg = write_pkg(tmp_path, files={"a.py": "X = 1\n"})
    (pkg / "pyproject.toml").write_text(
        textwrap.dedent(
            '''\
            [project]
            name = "dist-name"
            version = "0.1.0"
            '''
        )
    )
    result = consolidate_package(pkg, tmp_path / "out")
    assert result.pkg_name == "dist-name"
    assert (tmp_path / "out" / "dist-name.py").exists()


def test_pkg_name_from_setup_cfg(tmp_path: Path):
    """``setup.cfg`` metadata name is detected."""
    pkg = write_pkg(tmp_path, files={"a.py": "X = 1\n"})
    (pkg / "setup.cfg").write_text(
        textwrap.dedent(
            '''\
            [metadata]
            name = cfg-name
            '''
        )
    )
    result = consolidate_package(pkg, tmp_path / "out")
    assert result.pkg_name == "cfg-name"
    assert (tmp_path / "out" / "cfg-name.py").exists()


def test_pkg_name_from_setup_py(tmp_path: Path):
    """``setup.py`` name= argument is detected."""
    pkg = write_pkg(tmp_path, files={"a.py": "X = 1\n"})
    (pkg / "setup.py").write_text(
        textwrap.dedent(
            '''\
            from setuptools import setup
            setup(name="setup-name", version="0.1")
            '''
        )
    )
    result = consolidate_package(pkg, tmp_path / "out")
    assert result.pkg_name == "setup-name"
    assert (tmp_path / "out" / "setup-name.py").exists()


def test_pkg_name_detection_two_levels_up(tmp_path: Path):
    """Metadata in a directory two levels up (``src/<pkg>`` layout) is used."""
    src = tmp_path / "src"
    pkg = src / "mypkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("from .core import hello\n")
    (pkg / "core.py").write_text("def hello(): return 1\n")
    (src / "pyproject.toml").write_text(
        textwrap.dedent(
            '''\
            [project]
            name = "src-layout-pkg"
            version = "0.1.0"
            '''
        )
    )
    result = consolidate_package(pkg, tmp_path / "out")
    assert result.pkg_name == "src-layout-pkg"
    assert (tmp_path / "out" / "src-layout-pkg.py").exists()


def test_pkg_name_fallback_to_dirname(tmp_path: Path):
    """No metadata anywhere → current folder's name is used."""
    pkg = write_pkg(tmp_path, files={"a.py": "X = 1\n"})
    result = consolidate_package(pkg, tmp_path / "out")
    assert result.pkg_name == "pkg"


def test_detect_pkg_name_function(tmp_path: Path):
    """Unit-level checks for ``detect_pkg_name``."""
    root = tmp_path / "root"
    root.mkdir()
    # No metadata → folder name
    assert detect_pkg_name(root) == "root"

    # pyproject.toml at root
    (root / "pyproject.toml").write_text("[project]\nname = \"proj-name\"\n")
    assert detect_pkg_name(root) == "proj-name"

    # Two levels up
    deep = root / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "__init__.py").write_text("")
    assert detect_pkg_name(deep) == "proj-name"


def test_local_imports_stripped(tmp_path: Path):
    """Relative/local imports must not survive in the consolidated file."""
    pkg = write_pkg(
        tmp_path,
        files={
            "a.py": '''
                from .b import Bar as B2
                X = 1
                def foo():
                    return B2()
            ''',
            "b.py": '''
                class Bar:
                    pass
            ''',
        },
    )
    result = consolidate_package(pkg, tmp_path / "out")
    target = tmp_path / "out" / "pkg.py"
    text = target.read_text()
    assert "from ." not in text
    assert "import ." not in text
    # The aliased reference is rewritten to the consolidated name
    assert "Bar" in text
    compile(text, target.name, "exec")


def test_name_collision_renamed(tmp_path: Path):
    """Same-named functions across modules get disambiguated."""
    pkg = write_pkg(
        tmp_path,
        files={
            "a.py": "def foo():\n    return 1\n",
            "b.py": "def foo():\n    return 2\n",
        },
    )
    result = consolidate_package(pkg, tmp_path / "out")
    text = (tmp_path / "out" / "pkg.py").read_text()
    assert text.count("def foo") == 1
    # The second definition keeps a distinct name
    assert "b_foo" in text or "pkg_b_foo" in text or "_foo" in text
    compile(text, "pkg.py", "exec")


def test_temp_files_removed(tmp_path: Path):
    """Temporary const.py/classes.py/func.py must not remain."""
    pkg = write_pkg(
        tmp_path,
        files={
            "a.py": "X = 1\ndef foo(): return X\n",
            "b.py": "Y = 2\nclass Bar: pass\n",
        },
    )
    consolidate_package(pkg, tmp_path / "out")
    out = tmp_path / "out"
    for tmp_name in ("const.py", "classes.py", "func.py", "_consolidated.py"):
        assert not (out / tmp_name).exists(), f"{tmp_name} should not remain"


def test_autoall_all_section(tmp_path: Path):
    """autoall appends a parseable ``__all__`` block to the generated file."""
    pkg = write_pkg(
        tmp_path,
        files={"a.py": "CONST_A = 1\ndef foo(): return CONST_A\nclass Bar: pass\n"},
    )
    consolidate_package(pkg, tmp_path / "out")
    text = (tmp_path / "out" / "pkg.py").read_text()
    start = text.index("# --- auto-generated __all__")
    end = text.index("# --- end auto-generated __all__", start)
    block = text[start:end]
    assert "__all__ = [" in block
    for name in ("CONST_A", "foo", "Bar"):
        assert f"'{name}'" in block


def test_consolidate_package_dry_run(tmp_path: Path):
    """``dry_run`` never writes files."""
    pkg = write_pkg(tmp_path, files={"a.py": "X = 1\n"})
    result = consolidate_package(pkg, tmp_path / "out", dry_run=True)
    assert result.dry_run is True
    assert result.files == []
    assert not (tmp_path / "out" / "pkg.py").exists()


def test_consolidate_package_stdout(tmp_path: Path, capsys):
    """``stdout`` prints the generated source and writes no files."""
    pkg = write_pkg(tmp_path, files={"a.py": "X = 1\n"})
    result = consolidate_package(pkg, tmp_path / "out", stdout=True)
    assert result.files == []
    assert result.stdout_text
    out = capsys.readouterr().out
    assert "X = 1" in out
    assert not (tmp_path / "out" / "pkg.py").exists()


def test_consolidate_package_validate(tmp_path: Path):
    """``validate`` compiles the generated file."""
    pkg = write_pkg(tmp_path, files={"a.py": "X = 1\n"})
    result = consolidate_package(pkg, tmp_path / "out", validate=True)
    assert result.validated is True


def test_consolidate_package_no_files(tmp_path: Path):
    """A directory with no python files raises."""
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ConsolidationError, match="no python files found"):
        consolidate_package(empty, tmp_path / "out")


def test_ensure_all_file_writes_block(tmp_path: Path):
    """autoall.ensure_all_file updates a single .py file inplace."""
    target = tmp_path / "mymod.py"
    target.write_text("A = 1\ndef func(): return A\nclass Thing: pass\n")
    names = ensure_all_file(target)
    assert names == ["A", "Thing", "func"]
    text = target.read_text()
    assert "# --- auto-generated __all__" in text
    assert "__all__ = [" in text
    assert "'A'" in text and "'Thing'" in text and "'func'" in text


def test_ensure_all_file_idempotent(tmp_path: Path):
    """Re-running ensure_all_file replaces the block instead of duplicating."""
    target = tmp_path / "mymod.py"
    target.write_text("A = 1\n")
    ensure_all_file(target)
    ensure_all_file(target)
    text = target.read_text()
    assert text.count("# --- auto-generated __all__") == 1
    assert text.count("__all__ = [") == 1


def test_ensure_all_file_excludes_private_and_dunder(tmp_path: Path):
    """Private and dunder names stay out of a file-level ``__all__``."""
    target = tmp_path / "mymod.py"
    target.write_text(
        textwrap.dedent(
            '''\
            PUBLIC = 1
            _private = 2
            __dunder__ = 3
            def public_func(): pass
            def _private_func(): pass
            '''
        )
    )
    names = ensure_all_file(target)
    assert names == ["PUBLIC", "public_func"]
    text = target.read_text()
    block = text.split("__all__ = [", 1)[1]
    assert "_private" not in block
    assert "__dunder__" not in block