"""
helpers.py — Python helpers for project creation, data file checks, validation.

These are the python snippets executed via `bash -c "python ..."` or imported in tests/examples
throughout the arena session.

Reuse: `from tools.arena.helpers import create_sample_project, verify_data_copy`
"""

from __future__ import annotations
import filecmp
import textwrap
import shutil
import tempfile
from pathlib import Path
from typing import List

# ---------------------------------------------------------------------------
# Sample project creation (mirrors tests/conftest.py + bash heredocs)
# ---------------------------------------------------------------------------

def create_sample_project(base: Path) -> Path:
    """Create minimal pkg with data files, returns pkg path."""
    pkg = base / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(textwrap.dedent('''\
        """pkg init"""
        from .a import foo
        from .b import Bar
        __all__ = ["foo", "Bar"]
        '''))
    (pkg / "a.py").write_text(textwrap.dedent('''\
        """module a"""
        import os
        from .b import Bar as B2
        X = 1
        def foo(x=X):
            """docstring to keep/strip"""
            # comment to test
            return x + 1
        if __name__ == "__main__":
            print(foo())
        '''))
    (pkg / "b.py").write_text(textwrap.dedent('''\
        """module b"""
        import json
        Y = 2
        class Bar:
            """Bar doc"""
            def method(self): return Y
        '''))
    (pkg / "c.py").write_text('X = 1\ndef foo(): return 99\n')
    data_dir = pkg / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "sample.json").write_text('{"key": "value"}\n')
    (data_dir / "sample.csv").write_text('id,name\n1,alpha\n')
    (pkg / "config.yaml").write_text('info: test\n')
    return pkg

def create_src_layout(base: Path) -> Path:
    """src/ layout: base/src/mypkg/*"""
    src = base / "src"
    pkg = src / "mypkg"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text('from .core import hello\n')
    (pkg / "core.py").write_text('def hello(): return "hi"\n')
    return base

def create_namespace_package(base: Path) -> Path:
    """PEP 420 namespace (no __init__.py)."""
    ns = base / "ns_pkg"
    ns.mkdir(parents=True, exist_ok=True)
    (ns / "mod.py").write_text('def func(): return 42\n')
    sub = ns / "sub"
    sub.mkdir(exist_ok=True)
    (sub / "other.py").write_text('def other(): return 1\n')
    return base

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def verify_data_copy(src: Path, out: Path, expected: List[str] = None) -> bool:
    """Check that data files were copied preserving relative structure."""
    expected = expected or ["sample.json"]
    for name in expected:
        found = list(out.rglob(name))
        if not found:
            print(f"Missing {name} in {out}")
            return False
        src_candidates = list(src.rglob(name))
        if src_candidates and not filecmp.cmp(src_candidates[0], found[0], shallow=False):
            print(f"Content mismatch for {name}")
            return False
    return True

def check_compile(path: Path) -> bool:
    """Verify python file compiles."""
    try:
        compile(path.read_text(), path.name, "exec")
        return True
    except SyntaxError as e:
        print(f"Compile failed for {path}: {e}")
        return False

def check_help_flags() -> bool:
    """Verify all prompt.md flags appear in --help."""
    from refactor_utils.cli import build_parser
    help_text = build_parser().format_help()
    flags = [
        "--dry-run", "--check", "--stdout", "--config", "--format", "--diff",
        "--overwrite", "--include", "--rename-strategy", "--dedupe", "--imports",
        "--all", "--namespace", "--source-map", "--fail-on-warning", "--jobs",
        "--validate", "--mode",
    ]
    missing = [f for f in flags if f not in help_text]
    if missing:
        print(f"Missing flags: {missing}")
        return False
    return True

# ---------------------------------------------------------------------------
# CLI wrappers (used in bash heredocs)
# ---------------------------------------------------------------------------

def run_consolidate(project: Path, output: Path, **kwargs):
    """Thin wrapper around refactor_utils.core.consolidate for manual testing."""
    from refactor_utils.core import consolidate
    return consolidate(project, output, **kwargs)

if __name__ == "__main__":
    # Self-test when executed directly
    print("=== helpers.py self-test ===")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        pkg = create_sample_project(base)
        out = base / "out"
        result = run_consolidate(pkg, out)
        print(f"modules={result.modules}, exports={result.exports}, copied={result.copied_data_files}")
        assert check_compile(out / "_consolidated.py")
        assert verify_data_copy(pkg, out, ["sample.json", "sample.csv"])
        assert check_help_flags()
        print("✔ helpers.py OK")
