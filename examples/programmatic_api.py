"""
Programmatic API: using refactor_utils.consolidate() directly.

Covers dry-run, diff, validate, data files.

Run: python examples/programmatic_api.py
"""

import tempfile
import textwrap
from pathlib import Path
from refactor_utils.core import consolidate

def demo_dry_run():
    print("=== Dry-run (no files written) ===")
    with tempfile.TemporaryDirectory() as tmp:
        pkg = Path(tmp) / "pkg"
        pkg.mkdir()
        (pkg / "a.py").write_text("X=1\n")
        out = Path(tmp) / "out"
        result = consolidate(pkg, out, dry_run=True)
        print(f"dry_run={result.dry_run}, would write {len(result.plan.blocks) if result.plan else 0} blocks")
        print(f"out exists? {out.exists()}")

def demo_validate():
    print("\n=== Validate (compile + import) ===")
    with tempfile.TemporaryDirectory() as tmp:
        pkg = Path(tmp) / "pkg"
        pkg.mkdir()
        (pkg / "a.py").write_text("def hello(): return 'hi'\n")
        out = Path(tmp) / "out"
        result = consolidate(pkg, out, validate=True, validate_import=True)
        print(f"validated={result.validated}, exports={result.exports}")

def demo_diff():
    print("\n=== Diff against existing output ===")
    with tempfile.TemporaryDirectory() as tmp:
        pkg = Path(tmp) / "pkg"
        pkg.mkdir()
        (pkg / "a.py").write_text("X=1\n")
        out = Path(tmp) / "out"
        consolidate(pkg, out)
        # Modify source
        (pkg / "a.py").write_text("X=999\n")
        result = consolidate(pkg, out, diff=True)
        print(f"diff length={len(result.diff)}")
        print(result.diff[:300] if result.diff else "(no diff)")

def demo_data_files():
    print("\n=== Data files preservation ===")
    with tempfile.TemporaryDirectory() as tmp:
        pkg = Path(tmp) / "app"
        pkg.mkdir()
        (pkg / "core.py").write_text("def hi(): return 1\n")
        (pkg / "data.json").write_text('{"a":1}\n')
        (pkg / "notes.txt").write_text("hello\n")
        out = Path(tmp) / "out"
        result = consolidate(pkg, out, copy_data=True, data_glob=("*.json",))
        print(f"copied={result.copied_data_files}")
        print("files on disk:", list(out.rglob("*.json")))

if __name__ == "__main__":
    demo_dry_run()
    demo_validate()
    demo_diff()
    demo_data_files()
