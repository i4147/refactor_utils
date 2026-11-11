#!/usr/bin/env python3
"""Validate Python files with the validator pyrefactor uses on its output.

    tools/check_syntax.py FILE_OR_DIR [FILE_OR_DIR ...]

Directories are searched recursively for ``*.py``.  Exit code 0 when every
file parses, 1 when at least one does not, 2 on usage errors.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from refactor_utils import ConsolidationError, validate_syntax  # noqa: E402

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", "site"}


def iter_files(paths: list[Path]):
    for path in paths:
        if path.is_dir():
            for sub in sorted(path.rglob("*.py")):
                if not SKIP_DIRS.intersection(sub.parts):
                    yield sub
        else:
            yield path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+", type=Path, help="files or directories")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print errors")
    args = parser.parse_args(argv)

    checked = failed = 0
    for path in iter_files(args.paths):
        if not path.is_file():
            print(f"error: {path}: no such file", file=sys.stderr)
            return 2
        checked += 1
        try:
            validate_syntax(path.read_text(encoding="utf-8"), str(path))
        except (ConsolidationError, UnicodeDecodeError) as exc:
            failed += 1
            print(f"FAIL {exc}", file=sys.stderr)
        else:
            if not args.quiet:
                print(f"ok   {path}")
    print(f"{checked} file(s) checked, {failed} invalid")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
