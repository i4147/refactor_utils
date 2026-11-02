"""Python helpers used during the arena session.

Small utilities to create demo packages, run the consolidation flow, and
validate the generated ``consolidated/<pkgname>.py`` files. Run from the
repository root with ``PYTHONPATH=src``.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from refactor_utils.core import ConsolidationError, consolidate_package  # noqa: E402


def make_demo_pkg(root: Path, name: str = "demopkg") -> Path:
    """Create a small multi-module package under ``root/name``."""
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text('"""demo package."""\n')
    (pkg / "a.py").write_text(
        textwrap.dedent(
            """\
            \"\"\"module a\"\"\"
            CONST_A = 1

            def foo(x=CONST_A):
                return x + 1
            """
        )
    )
    (pkg / "b.py").write_text(
        textwrap.dedent(
            """\
            \"\"\"module b\"\"\"
            CONST_B = 2

            class Bar:
                def value(self):
                    return CONST_B

            def foo():
                return 99  # collides with a.foo -> gets renamed
            """
        )
    )
    return pkg


def consolidate_and_validate(source: Path, output: Path | None = None) -> Path:
    """Consolidate ``source`` and compile-check the generated file.

    Returns the path to ``consolidated/<pkgname>.py``.
    """
    result = consolidate_package(source, output, validate=True)
    target = Path(result.files[0])
    compile(target.read_text(), target.name, "exec")
    return target


def main(argv: list[str] | None = None) -> int:
    sources = [Path(p) for p in (argv or sys.argv[1:])]
    if not sources:
        sources = [Path("examples/data_files_example/mypackage")]
    for src in sources:
        try:
            target = consolidate_and_validate(src)
            print(f"OK  {src} -> {target}")
        except ConsolidationError as exc:
            print(f"FAIL {src}: {exc}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
