#!/usr/bin/env python3
"""Regenerate ``mytest/result.py`` from ``mytest/1.py`` and ``mytest/2.py``.

    tools/regen_sample.py            rewrite mytest/result.py
    tools/regen_sample.py --check    exit 1 when it is missing or out of date

The two inputs are copied to a temporary directory first, so an existing
``result.py`` is never picked up as a source file.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from refactor_utils import consolidate  # noqa: E402
from refactor_utils.logging_setup import setup_logging  # noqa: E402

SAMPLE = ROOT / "mytest"
INPUTS = ("1.py", "2.py")
TARGET = SAMPLE / "result.py"


def generate() -> tuple[str, list[tuple[str, str, str]]]:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / SAMPLE.name
        src.mkdir()
        for name in INPUTS:
            shutil.copy(SAMPLE / name, src / name)
        out = Path(tmp) / "out"
        # An empty config keeps the repository's own [tool.pyrefactor] out.
        cfg = Path(tmp) / "pyproject.toml"
        cfg.write_text("", encoding="utf-8")
        result = consolidate(
            src, out, filename="result", workers=1, copy_data=False, config=cfg
        )
        text = (out / "result.py").read_text(encoding="utf-8")
    return text, list(result.plan.renamed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="verify, do not write")
    parser.add_argument("-v", "--verbose", action="store_true", help="show the log")
    args = parser.parse_args(argv)
    setup_logging("INFO" if args.verbose else "ERROR", None)

    text, renamed = generate()
    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.is_file() else None
        if current != text:
            print(f"{TARGET.relative_to(ROOT)} is out of date; run tools/regen_sample.py")
            return 1
        print(f"{TARGET.relative_to(ROOT)} is up to date")
        return 0
    TARGET.write_text(text, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)} ({text.count(chr(10))} lines)")
    for module, old, new in renamed:
        print(f"  renamed {module}:{old} -> {new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
