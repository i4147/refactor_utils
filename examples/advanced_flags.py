"""
Advanced flags demo: rename strategies, dedupe, all modes, source maps.

Run:
    python examples/advanced_flags.py
"""

import tempfile
import textwrap
from pathlib import Path
from refactor_utils.core import consolidate


def demo_rename_strategy():
    print("=== Rename Strategy: prefix vs suffix vs module_path ===")
    with tempfile.TemporaryDirectory() as tmp:
        pkg = Path(tmp) / "pkg"
        pkg.mkdir()
        (pkg / "a.py").write_text("X = 1")
        (pkg / "b.py").write_text("X = 2")
        for strat in ["prefix", "suffix", "module_path"]:
            out = Path(tmp) / f"out_{strat}"
            result = consolidate(pkg, out, rename_strategy=strat)
            text = (out / "_consolidated.py").read_text()
            print(f"[{strat}] renamed: {result.plan.renamed}" if result.plan else strat)
            print(text.splitlines()[:5])


def demo_dedupe():
    print("\n=== Dedupe: none vs safe vs aggressive ===")
    with tempfile.TemporaryDirectory() as tmp:
        pkg = Path(tmp) / "pkg"
        pkg.mkdir()
        (pkg / "a.py").write_text("def foo(): return 1")
        (pkg / "b.py").write_text("def foo(): return 1")
        for mode in ["none", "safe", "aggressive"]:
            out = Path(tmp) / f"out_{mode}"
            result = consolidate(pkg, out, dedupe=mode)
            print(f"[{mode}] deduped={len(result.plan.deduped) if result.plan else 0}")


def demo_all_modes():
    print("\n=== __all__ handling: ignore / respect / merge / recompute ===")
    with tempfile.TemporaryDirectory() as tmp:
        pkg = Path(tmp) / "pkg"
        pkg.mkdir()
        (pkg / "a.py").write_text('__all__ = ["foo"]\nfoo=1\nbar=2\n')
        for mode in ["ignore", "respect", "merge", "recompute"]:
            out = Path(tmp) / f"out_{mode}"
            result = consolidate(pkg, out, all_mode=mode)
            print(
                f"[{mode}] exports={result.exports} -> {result.plan.exports if result.plan else {}}"
            )


def demo_source_map():
    print("\n=== Source map + preserve-modules ===")
    with tempfile.TemporaryDirectory() as tmp:
        pkg = Path(tmp) / "pkg"
        pkg.mkdir()
        (pkg / "a.py").write_text("X=1\nY=2\n")
        out = Path(tmp) / "out"
        consolidate(pkg, out, source_map=True, preserve_modules=True, namespace=True)
        print((out / "_consolidated.py").read_text())


if __name__ == "__main__":
    demo_rename_strategy()
    demo_dedupe()
    demo_all_modes()
    demo_source_map()
