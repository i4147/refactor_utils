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


def write_pkg(
    root: Path, name: str = "pkg", files: dict[str, str] | None = None
) -> Path:
    """Write a small package under ``root/name`` and return its path."""
    pkg = root / name
    pkg.mkdir()
    (pkg / "__init__.py").write_text('"""pkg init."""\n')
    for fname, content in (files or {}).items():
        path = pkg / fname
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content))
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
    (pkg / "pyproject.toml").write_text(textwrap.dedent("""\
            [project]
            name = "dist-name"
            version = "0.1.0"
            """))
    result = consolidate_package(pkg, tmp_path / "out")
    # Distribution names are normalised to importable module names
    assert result.pkg_name == "dist_name"
    assert (tmp_path / "out" / "dist_name.py").exists()


def test_pkg_name_from_setup_cfg(tmp_path: Path):
    """``setup.cfg`` metadata name is detected."""
    pkg = write_pkg(tmp_path, files={"a.py": "X = 1\n"})
    (pkg / "setup.cfg").write_text(textwrap.dedent("""\
            [metadata]
            name = cfg-name
            """))
    result = consolidate_package(pkg, tmp_path / "out")
    assert result.pkg_name == "cfg_name"
    assert (tmp_path / "out" / "cfg_name.py").exists()


def test_pkg_name_from_setup_py(tmp_path: Path):
    """``setup.py`` name= argument is detected."""
    pkg = write_pkg(tmp_path, files={"a.py": "X = 1\n"})
    (pkg / "setup.py").write_text(textwrap.dedent("""\
            from setuptools import setup
            setup(name="setup-name", version="0.1")
            """))
    result = consolidate_package(pkg, tmp_path / "out")
    assert result.pkg_name == "setup_name"
    assert (tmp_path / "out" / "setup_name.py").exists()


def test_pkg_name_detection_two_levels_up(tmp_path: Path):
    """Metadata in a directory two levels up (``src/<pkg>`` layout) is used."""
    src = tmp_path / "src"
    pkg = src / "mypkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("from .core import hello\n")
    (pkg / "core.py").write_text("def hello(): return 1\n")
    (src / "pyproject.toml").write_text(textwrap.dedent("""\
            [project]
            name = "src-layout-pkg"
            version = "0.1.0"
            """))
    result = consolidate_package(pkg, tmp_path / "out")
    assert result.pkg_name == "src_layout_pkg"
    assert (tmp_path / "out" / "src_layout_pkg.py").exists()


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
    (root / "pyproject.toml").write_text('[project]\nname = "proj-name"\n')
    assert detect_pkg_name(root) == "proj_name"

    # Two levels up
    deep = root / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "__init__.py").write_text("")
    assert detect_pkg_name(deep) == "proj_name"


def test_local_imports_stripped(tmp_path: Path):
    """Relative/local imports must not survive in the consolidated file."""
    pkg = write_pkg(
        tmp_path,
        files={
            "a.py": """
                from .b import Bar as B2
                X = 1
                def foo():
                    return B2()
            """,
            "b.py": """
                class Bar:
                    pass
            """,
        },
    )
    _ = consolidate_package(pkg, tmp_path / "out")
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
    _ = consolidate_package(pkg, tmp_path / "out")
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
    target.write_text(textwrap.dedent("""\
            PUBLIC = 1
            _private = 2
            __dunder__ = 3
            def public_func(): pass
            def _private_func(): pass
            """))
    names = ensure_all_file(target)
    assert names == ["PUBLIC", "public_func"]
    text = target.read_text()
    block = text.split("__all__ = [", 1)[1]
    assert "_private" not in block
    assert "__dunder__" not in block


# ---------------------------------------------------------------------------
# Output layout guarantees (prompt.md §1)
# ---------------------------------------------------------------------------


def test_consolidated_dir_contains_exactly_one_file(tmp_path: Path):
    """``consolidated/`` holds a single ``<pkgname>.py`` after a run."""
    pkg = write_pkg(
        tmp_path,
        files={
            "a.py": "X = 1\ndef foo(): return X\n",
            "b.py": "class Bar:\n    pass\n",
            "data.json": '{"a": 1}\n',
        },
    )
    consolidate_package(pkg)
    contents = sorted(p.name for p in (pkg / "consolidated").iterdir())
    assert contents == ["pkg.py"]


def test_stale_output_files_are_removed(tmp_path: Path):
    """Leftovers from the legacy layout / earlier runs do not survive."""
    pkg = write_pkg(tmp_path, files={"a.py": "X = 1\n"})
    out = pkg / "consolidated"
    out.mkdir()
    (out / "__init__.py").write_text("from ._consolidated import *\n")
    (out / "_consolidated.py").write_text('"""Consolidated from ``pkg``."""\n')
    (out / "const.py").write_text("OLD = 1\n")
    (out / "old_pkg_name.py").write_text(
        '"""Consolidated from ``old`` by pyrefactor (generated file)."""\n'
    )
    result = consolidate_package(pkg)
    assert result.pkg_name == "pkg"
    assert sorted(p.name for p in out.iterdir()) == ["pkg.py"]


def test_custom_output_file_names_are_kept(tmp_path: Path):
    """Files that were not written by pyrefactor are never deleted."""
    pkg = write_pkg(tmp_path, files={"a.py": "X = 1\n"})
    out = tmp_path / "out"
    out.mkdir()
    keep = out / "notes.py"
    keep.write_text("# hand-written notes\n")
    consolidate_package(pkg, out)
    assert keep.exists()
    assert (out / "pkg.py").exists()


# ---------------------------------------------------------------------------
# Extraction order and assembly (prompt.md §2)
# ---------------------------------------------------------------------------


def test_sections_come_in_order_const_classes_func(tmp_path: Path):
    """Output order is imports -> constants -> classes -> functions."""
    pkg = write_pkg(
        tmp_path,
        files={
            "a.py": "def foo():\n    return 1\n",
            "b.py": "class Bar:\n    pass\n",
            "c.py": "CONST = 42\n",
        },
    )
    text = (consolidate_package(pkg, tmp_path / "out") and False) or (
        tmp_path / "out" / "pkg.py"
    ).read_text()
    const_at = text.index("# Constants")
    class_at = text.index("# Classes")
    func_at = text.index("# Functions")
    assert const_at < class_at < func_at
    assert text.index("CONST = 42") < text.index("class Bar") < text.index("def foo")


def test_section_files_are_written_then_removed(tmp_path: Path, monkeypatch):
    """``const.py``/``classes.py``/``func.py`` are created and deleted again."""
    import refactor_utils.core as core_mod

    seen: list[str] = []
    original = core_mod._merge_body

    def spy(header, section_paths, entry_text):
        """Record the section files before delegating to the real merge."""
        seen.extend(p.name for p in section_paths)
        seen.extend("exists" for p in section_paths if p.exists())
        return original(header, section_paths, entry_text)

    monkeypatch.setattr(core_mod, "_merge_body", spy)
    pkg = write_pkg(
        tmp_path,
        files={
            "a.py": "CONST = 1\ndef foo(): return CONST\n",
            "b.py": "class Bar:\n    pass\n",
        },
    )
    consolidate_package(pkg, tmp_path / "out")
    assert seen[:3] == ["const.py", "classes.py", "func.py"]
    assert seen.count("exists") == 3
    for name in ("const.py", "classes.py", "func.py"):
        assert not (tmp_path / "out" / name).exists()


def test_imports_are_deduped_and_local_imports_stripped(tmp_path: Path):
    """The optimized import section has no duplicates or relative imports."""
    pkg = write_pkg(
        tmp_path,
        files={
            "a.py": "import os\nimport json\nfrom .b import helper\n\ndef one():\n    return os.sep + str(len(json.dumps({})))\n",
            "b.py": "import json\nimport os\n\ndef helper():\n    return json.dumps({'b': 1}) + os.sep\n",
        },
    )
    text = (
        consolidate_package(pkg, tmp_path / "out"),
        (tmp_path / "out" / "pkg.py").read_text(),
    )[1]
    header = text.split("# Constants", 1)[0]
    assert header.count("import json") == 1
    assert header.count("import os") == 1
    assert "from ." not in header
    assert "import ." not in header
    compile(text, "pkg.py", "exec")


# ---------------------------------------------------------------------------
# __all__ via autoall
# ---------------------------------------------------------------------------


def test_all_section_is_last_and_lists_public_names(tmp_path: Path):
    """``__all__`` is appended at the end of the file and excludes privates."""
    pkg = write_pkg(
        tmp_path,
        files={
            "a.py": (
                "PUBLIC = 1\n"
                "_PRIVATE = 2\n"
                "def public_func(): return PUBLIC\n"
                "def _private_func(): return _PRIVATE\n"
                "class PublicClass:\n    pass\n"
            )
        },
    )
    target = tmp_path / "out" / "pkg.py"
    result = consolidate_package(pkg, tmp_path / "out")
    text = target.read_text()
    assert result.all_added is True
    assert text.rstrip().endswith("# --- end auto-generated __all__ ---")
    block = text.split("__all__ = [", 1)[1]
    assert "'PUBLIC'" in block
    assert "'public_func'" in block
    assert "'PublicClass'" in block
    assert "_PRIVATE" not in block
    assert "_private_func" not in block
    namespace: dict = {}
    exec(compile(text, "pkg.py", "exec"), namespace)
    assert namespace["__all__"] == ["PUBLIC", "PublicClass", "public_func"]


# ---------------------------------------------------------------------------
# --check / --diff / --format / --eol
# ---------------------------------------------------------------------------


def test_check_passes_after_generation_and_fails_on_change(tmp_path: Path):
    """``check`` is an exact comparison against the current generated file."""
    pkg = write_pkg(tmp_path, files={"a.py": "X = 1\n"})
    consolidate_package(pkg, tmp_path / "out")
    # Unchanged sources: check succeeds.
    assert consolidate_package(pkg, tmp_path / "out", check=True).dry_run is True
    (pkg / "a.py").write_text("X = 2\n")
    with pytest.raises(ConsolidationError, match="check failed"):
        consolidate_package(pkg, tmp_path / "out", check=True)


def test_check_fails_when_output_is_missing(tmp_path: Path):
    """``check`` reports a missing output instead of silently passing."""
    pkg = write_pkg(tmp_path, files={"a.py": "X = 1\n"})
    with pytest.raises(ConsolidationError, match="does not exist"):
        consolidate_package(pkg, tmp_path / "out", check=True)


def test_diff_reports_changes_without_writing(tmp_path: Path):
    """``diff`` fills ``result.diff`` and leaves the file untouched."""
    pkg = write_pkg(tmp_path, files={"a.py": "X = 1\n"})
    out = tmp_path / "out"
    consolidate_package(pkg, out)
    before = (out / "pkg.py").read_text()
    (pkg / "a.py").write_text("X = 2\n")
    result = consolidate_package(pkg, out, dry_run=True, diff=True)
    assert "+X = 2" in result.diff
    assert (out / "pkg.py").read_text() == before


def test_eol_crlf_and_check_roundtrip(tmp_path: Path):
    """CRLF output is reproducible, so ``check`` passes on a second run."""
    pkg = write_pkg(tmp_path, files={"a.py": "X = 1\ndef foo(): return X\n"})
    out = tmp_path / "out"
    consolidate_package(pkg, out, eol="crlf")
    raw = (out / "pkg.py").read_bytes()
    assert raw.count(b"\r\n") > 0
    assert raw.count(b"\n") == raw.count(b"\r\n")
    assert consolidate_package(pkg, out, eol="crlf", check=True).modules == 2


def test_format_is_idempotent(tmp_path: Path):
    """``--format`` produces a stable file (second run writes nothing new)."""
    pytest.importorskip("black")
    pkg = write_pkg(
        tmp_path,
        files={"a.py": "def f(a,b):\n    return a+b\n", "b.py": "X=1\n"},
    )
    out = tmp_path / "out"
    consolidate_package(pkg, out, do_format=True)
    first = (out / "pkg.py").read_text()
    consolidate_package(pkg, out, do_format=True)
    assert (out / "pkg.py").read_text() == first
    assert "def f(a, b):" in first


# ---------------------------------------------------------------------------
# Entry point / collision options
# ---------------------------------------------------------------------------


def test_entry_and_main_name_options(tmp_path: Path):
    """``entry`` keeps one guard and ``main_name`` renames its entry point."""
    pkg = write_pkg(
        tmp_path,
        files={
            "app.py": (
                "def main():\n"
                "    print('app')\n"
                "\n"
                "if __name__ == '__main__':\n"
                "    main()\n"
            ),
            "tool.py": (
                "def main():\n"
                "    print('tool')\n"
                "\n"
                "if __name__ == '__main__':\n"
                "    main()\n"
            ),
        },
    )
    out = tmp_path / "out"
    consolidate_package(pkg, out, entry=("app",), main_name="run")
    text = (out / "pkg.py").read_text()
    assert "def run():" in text
    assert "if __name__ ==" in text and "    run()" in text
    # Both `main` functions survive (only the guard is filtered/renamed)
    assert "def run():" in text and "def run_tool():" in text
    compile(text, "pkg.py", "exec")


def test_generate_guard_creates_main_guard(tmp_path: Path):
    """``generate_guard`` synthesises a ``__main__`` guard when missing."""
    pkg = write_pkg(tmp_path, files={"a.py": "def main():\n    return 1\n"})
    text = (
        consolidate_package(pkg, tmp_path / "out", generate_guard=True)
        and (tmp_path / "out" / "pkg.py").read_text()
    )
    assert 'if __name__ == "__main__":' in text
    assert "main()" in text


# ---------------------------------------------------------------------------
# Data files
# ---------------------------------------------------------------------------


def test_copy_data_paths_are_relative_to_the_package(tmp_path: Path):
    """Copied data keeps working with ``Path(__file__).parent / ...``."""
    pkg = write_pkg(
        tmp_path,
        files={
            "a.py": 'from pathlib import Path\nDATA = Path(__file__).parent / "data" / "x.json"\n',
            "data/x.json": '{"a": 1}\n',
        },
    )
    result = consolidate_package(pkg, tmp_path / "out", copy_data=True)
    copied = tmp_path / "out" / "data" / "x.json"
    assert copied.is_file()
    assert copied in result.copied_data_files
    assert not (tmp_path / "out" / "pkg" / "data").exists()


def test_copy_data_glob_filters_files(tmp_path: Path):
    """``data_glob`` restricts which data files are copied."""
    pkg = write_pkg(
        tmp_path,
        files={"a.py": "X = 1\n", "data/x.json": "{}\n", "data/y.txt": "hi\n"},
    )
    consolidate_package(pkg, tmp_path / "out", copy_data=True, data_glob=("*.json",))
    assert (tmp_path / "out" / "data" / "x.json").is_file()
    assert not (tmp_path / "out" / "data" / "y.txt").exists()
