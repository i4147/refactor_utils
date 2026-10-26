"""
test_helpers.py — Manual discovery/parser/resolver/writer checks run via bash in session.

These reproduce the inline python tests we executed before creating tests/test_*.py.
They can be run as: python tools/arena/test_helpers.py
"""

from __future__ import annotations
import textwrap
import tempfile
from pathlib import Path


def test_discovery():
    print("=== discovery manual ===")
    from refactor_utils.discovery import discover, discover_data_files, detect_base

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "pkg"
        base.mkdir()
        (base / "a.py").write_text("X=1\n")
        files = discover(base)
        print(f"discovered {len(files)}: {[f.module for f in files]}")
        assert len(files) == 1
        data = discover_data_files(base, copy_data=True)
        print(f"data files: {data}")
        print("discovery OK")


def test_parser():
    print("=== parser manual ===")
    from refactor_utils.parser import parse_module_file

    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "mod.py"
        p.write_text("X=1\nimport importlib\nx=__file__\n")
        info = parse_module_file(str(p), "mod", False)
        print(f"warnings: {info.warnings}")
        print(f"errors: {info.errors}")
        print(f"blocks: {len(info.blocks)}")
        assert not info.errors
        assert any("__file__" in w for w in info.warnings)
        print("parser OK")

        # strip tests
        p2 = Path(tmp) / "doc.py"
        p2.write_text('def foo():\n    """docstring"""\n    return 1\n')
        info_keep = parse_module_file(str(p2), "doc", False, keep_docstrings=True)
        info_strip = parse_module_file(str(p2), "doc", False, keep_docstrings=False)
        print(
            f"keep len {len(b''.join(b.source for b in info_keep.blocks))}, strip len {len(b''.join(b.source for b in info_strip.blocks))}"
        )
        print("parser strip OK")


def test_resolver_writer():
    print("=== resolver/writer manual ===")
    from refactor_utils.parser import parse_module_file
    from refactor_utils.resolver import Resolver
    from refactor_utils.writer import render_consolidated

    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp)
        files = {"pkg/a.py": "X=1", "pkg/b.py": "X=2"}
        mods = {}
        for fname, src in files.items():
            full = p / fname
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(src)
            mods[fname.replace("/", ".").replace(".py", "")] = parse_module_file(
                str(full), fname.replace("/", ".").replace(".py", ""), False
            )
        r = Resolver(mods, rename_strategy="prefix")
        plan = r.build()
        print(f"renamed: {plan.renamed}")
        text = render_consolidated(plan, "pkg", source_map=True, preserve_modules=True)
        print(text[:200])
        compile(text, "_consolidated.py", "exec")
        print("resolver/writer OK")


def test_core():
    print("=== core manual (data files) ===")
    import sys
    from pathlib import Path as _Path

    sys.path.insert(0, str(_Path(__file__).parents[2]))
    # Fallback: try local helpers
    try:
        from tools.arena.helpers import create_sample_project
    except ModuleNotFoundError:
        import importlib.util, pathlib

        spec = importlib.util.spec_from_file_location(
            "helpers", pathlib.Path(__file__).parent / "helpers.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        create_sample_project = mod.create_sample_project
    from refactor_utils.core import consolidate

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        pkg = create_sample_project(base)
        out = base / "out"
        result = consolidate(pkg, out)
        print(
            f"modules={result.modules}, exports={result.exports}, copied={result.copied_data_files}"
        )
        assert (out / "_consolidated.py").exists()
        print("core OK")


def test_cli():
    print("=== cli manual ===")
    from refactor_utils.cli import build_parser

    p = build_parser()
    help_text = p.format_help()
    assert "--dry-run" in help_text
    assert "--mode" in help_text
    print("cli help OK")
    # dry-run
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "pkg"
        base.mkdir()
        (base / "a.py").write_text("X=1\n")
        out = Path(tmp) / "out"
        from refactor_utils.cli import main

        rc = main([str(base), "-o", str(out), "--dry-run"])
        print(f"dry-run rc={rc}, out exists={out.exists()}")
        assert rc == 0
        print("cli dry-run OK")


if __name__ == "__main__":
    test_discovery()
    test_parser()
    test_resolver_writer()
    test_core()
    test_cli()
    print("✔ all manual helpers passed")
