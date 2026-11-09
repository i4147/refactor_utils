"""Audit (and optionally report) docstring coverage of the project sources.

Checks, for every ``.py`` file under ``src/`` (and ``autoall/src/`` unless
``--skip-autoall`` is given) plus the test-suite helpers:

* the module has a docstring;
* every top-level and nested function has a docstring;
* every class and every method has a docstring.

Run from the repository root::

    python arena/docs/../../../arena/docstring_audit.py
    python arena/docstring_audit.py --tests

Exit code is ``0`` when everything is documented, ``1`` otherwise, so the
script can be used in CI.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check_file(path: Path) -> list[str]:
    """Return a list of missing-docstring descriptions for ``path``.

    Args:
        path: Python file to inspect.

    Returns:
        Human readable problems (empty when the file is fully documented).
    """
    problems: list[str] = []
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:  # pragma: no cover - defensive
        return [f"{path}: cannot parse: {exc}"]
    if ast.get_docstring(tree) is None:
        problems.append(f"{path}: module docstring missing")

    def visit(node: ast.AST, prefix: str = "") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                kind = "class" if isinstance(child, ast.ClassDef) else "function"
                name = f"{prefix}{child.name}"
                if ast.get_docstring(child) is None:
                    problems.append(f"{path}: {kind} '{name}' docstring missing")
                visit(child, prefix=f"{name}.")

    visit(tree)
    return problems


def main(argv: list[str] | None = None) -> int:
    """Run the audit over the project sources.

    Args:
        argv: Command line arguments (``--tests``, ``--skip-autoall``,
            ``--quiet``).

    Returns:
        ``0`` when fully documented, ``1`` otherwise.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    include_tests = "--tests" in args
    include_autoall = "--skip-autoall" not in args
    quiet = "--quiet" in args

    targets = sorted((ROOT / "src").rglob("*.py"))
    if include_autoall:
        targets += sorted((ROOT / "autoall" / "src").rglob("*.py"))
    if include_tests:
        targets += sorted((ROOT / "tests").rglob("*.py"))
    targets = [t for t in targets if ".egg-info" not in str(t)]

    problems: list[str] = []
    for path in targets:
        problems.extend(check_file(path))

    if problems:
        if not quiet:
            print(f"{len(problems)} docstring problem(s):")
            for problem in problems:
                print(f"  - {problem}")
        return 1
    if not quiet:
        print(f"OK: all {len(targets)} file(s) documented")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
