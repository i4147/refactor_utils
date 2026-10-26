"""Shared fixtures for pytest."""

from __future__ import annotations
import textwrap
from pathlib import Path
import pytest


@pytest.fixture
def sample_project(tmp_path: Path) -> Path:
    """Create a minimal multi-module project with data files."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    # __init__.py
    (pkg / "__init__.py").write_text(
        textwrap.dedent('''\
        """pkg init"""
        from .a import foo
        from .b import Bar
        __all__ = ["foo", "Bar"]
        ''')
    )
    # a.py with docstring, comment, __main__ guard
    (pkg / "a.py").write_text(
        textwrap.dedent('''\
        """module a"""
        import os
        import sys
        from .b import Bar as B2
        X = 1
        def foo(x=X):
            """docstring to keep/strip"""
            # comment to test
            return x + 1

        if __name__ == "__main__":
            print(foo())
        ''')
    )
    # b.py
    (pkg / "b.py").write_text(
        textwrap.dedent('''\
        """module b"""
        import json
        Y = 2
        class Bar:
            """Bar doc"""
            def method(self):
                return Y
        ''')
    )
    # c.py duplicate name
    (pkg / "c.py").write_text(
        textwrap.dedent('''\
        """module c duplicate"""
        X = 1
        def foo():
            return 99
        ''')
    )
    # data files
    data_dir = pkg / "data"
    data_dir.mkdir()
    (data_dir / "sample.json").write_text('{"key": "value"}\n')
    (data_dir / "sample.csv").write_text("id,name\n1,alpha\n")
    (pkg / "config.yaml").write_text("info: test\n")
    return pkg


@pytest.fixture
def empty_project(tmp_path: Path) -> Path:
    """Project with single file."""
    single = tmp_path / "single.py"
    single.write_text("X = 1\ndef hello(): return X\n")
    return tmp_path


@pytest.fixture
def project_with_bad_file(tmp_path: Path) -> Path:
    """Project containing a syntax-error file."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("X = 1\n")
    (pkg / "bad.py").write_text("def oops(\n    syntax error\n")
    return pkg


@pytest.fixture
def src_layout_project(tmp_path: Path) -> Path:
    """Simulate src/ layout."""
    src = tmp_path / "src"
    pkg = src / "mypkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("from .core import hello\n")
    (pkg / "core.py").write_text('def hello(): return "hi"\n')
    return tmp_path


@pytest.fixture
def namespace_project(tmp_path: Path) -> Path:
    """PEP 420 namespace package (no __init__.py)."""
    ns = tmp_path / "ns_pkg"
    ns.mkdir()
    # No __init__.py -> namespace
    (ns / "mod.py").write_text("def func(): return 42\n")
    sub = ns / "sub"
    sub.mkdir()
    (sub / "other.py").write_text("def other(): return 1\n")
    return tmp_path
