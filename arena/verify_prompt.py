"""Verify the ``prompt.md`` contract end to end.

Checks, against throw-away packages in a temporary directory:

1. ``pyrefactor`` with **no arguments** consolidates the current directory and
   writes exactly one file: ``consolidated/<pkgname>.py``;
2. the package name is detected from ``pyproject.toml`` up to two levels above
   (``src/<pkgname>`` layout) and falls back to the folder name;
3. output order is imports → constants → classes → functions → entry points →
   ``__all__``;
4. the temporary ``const.py``/``classes.py``/``func.py`` extraction files are
   gone afterwards;
5. ``autoall`` added a parseable ``__all__`` section listing the public API;
6. ``--check`` passes immediately after a build (idempotent) and fails after a
   source change;
7. ``-o DIR`` redirects the single file.

Run from the repository root::

    python arena/verify_prompt.py

Exit code ``0`` means every check passed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
CHECKS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    """Record one check result.

    Args:
        name: Short description of the check.
        ok: Whether it passed.
        detail: Extra information printed on failure.
    """
    CHECKS.append((name, ok, detail))
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not ok else ""))


def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run ``pyrefactor`` and capture its output.

    Args:
        args: Command line arguments.
        cwd: Working directory.

    Returns:
        The completed process.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [PY, "-m", "refactor_utils.cli", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


def make_package(base: Path, name: str = "demo") -> Path:
    """Create a small package exercising constants, classes, functions, imports.

    Args:
        base: Directory that will contain the package.
        name: Package folder name.

    Returns:
        The package path.
    """
    pkg = base / name
    (pkg / "sub").mkdir(parents=True)
    (pkg / "__init__.py").write_text('"""Demo package."""\nfrom .core import run\n')
    (pkg / "core.py").write_text(textwrap.dedent('''\
            """Core."""
            import json
            from .consts import LIMIT

            def run():
                """Run."""
                return json.dumps({"limit": LIMIT})

            def main():
                """Entry point."""
                return run()

            if __name__ == "__main__":
                main()
            '''))
    (pkg / "consts.py").write_text('"""Constants."""\nLIMIT = 3\n')
    (pkg / "classes.py").write_text(textwrap.dedent('''\
            """Classes."""


            class Demo:
                """A demo class."""

                def value(self):
                    """Return a value."""
                    return 1
            '''))
    (pkg / "sub" / "__init__.py").write_text("")
    (pkg / "sub" / "deep.py").write_text(textwrap.dedent('''\
            """Deep module."""
            from ..consts import LIMIT


            def doubled():
                """Double the limit."""
                return LIMIT * 2
            '''))
    return pkg


def main() -> int:
    """Run every check.

    Returns:
        ``0`` when all checks pass, ``1`` otherwise.
    """
    work = Path(tempfile.mkdtemp(prefix="verify-prompt-"))
    try:
        # --- 1. no-argument invocation -----------------------------------
        pkg = make_package(work)
        proc = run(["--log-level", "ERROR"], cwd=pkg)
        record("no-arg run exits 0", proc.returncode == 0, proc.stderr.strip()[-400:])
        out_dir = pkg / "consolidated"
        files = sorted(p.name for p in out_dir.iterdir()) if out_dir.is_dir() else []
        record(
            "consolidated/ holds exactly one file",
            files == ["demo.py"],
            f"found {files}",
        )
        text = (out_dir / "demo.py").read_text() if files else ""
        record("generated file compiles", bool(text) and _compiles(text))

        # --- 2. package name detection -----------------------------------
        record(
            "folder-name fallback", out_dir.is_dir() and (out_dir / "demo.py").is_file()
        )

        src_layout = work / "src_layout"
        deep_pkg = make_package(src_layout / "src", "mypkg")
        (src_layout / "pyproject.toml").write_text(
            '[project]\nname = "src-layout-dist"\nversion = "0.1"\n'
        )
        proc = run([str(deep_pkg), "--log-level", "ERROR"], cwd=src_layout)
        record(
            "src/<pkg> metadata detection",
            proc.returncode == 0
            and (deep_pkg / "consolidated" / "src_layout_dist.py").is_file(),
            proc.stderr.strip()[-400:],
        )

        # --- 3. section ordering -----------------------------------------
        order = [
            text.find("# Constants"),
            text.find("# Classes"),
            text.find("# Functions"),
            text.find("# Entry points"),
            text.find("# --- auto-generated __all__"),
        ]
        record(
            "sections ordered const→classes→functions→entry→__all__",
            all(i >= 0 for i in order) and order == sorted(order),
            f"positions {order}",
        )
        record(
            "relative imports stripped",
            "from ." not in text.split("# Constants")[0] and "from  import" not in text,
        )
        record("external import kept once", text.count("import json") == 1)

        # --- 4. temporary files removed ----------------------------------
        record(
            "temporary section files removed",
            not any(
                (out_dir / n).exists() for n in ("const.py", "classes.py", "func.py")
            ),
        )

        # --- 5. __all__ ---------------------------------------------------
        record(
            "__all__ block present at EOF",
            text.rstrip().endswith("# --- end auto-generated __all__ ---"),
        )
        try:
            import ast

            all_names = None
            for node in ast.parse(text).body:
                if isinstance(node, ast.Assign) and any(
                    getattr(t, "id", "") == "__all__" for t in node.targets
                ):
                    all_names = ast.literal_eval(node.value)
            record(
                "__all__ is a literal list with public names",
                isinstance(all_names, list)
                and {"LIMIT", "Demo", "run", "doubled"} <= set(all_names or []),
                f"got {all_names}",
            )
        except SyntaxError as exc:  # pragma: no cover - reported above
            record("__all__ is a literal list with public names", False, str(exc))

        # --- 6. --check idempotency --------------------------------------
        proc = run(["--check", "--log-level", "ERROR"], cwd=pkg)
        record(
            "--check passes right after a build",
            proc.returncode == 0,
            proc.stderr[-400:],
        )
        (pkg / "consts.py").write_text("LIMIT = 9\n")
        proc = run(["--check", "--log-level", "ERROR"], cwd=pkg)
        record("--check fails after a source change", proc.returncode == 2)
        run(["--log-level", "ERROR"], cwd=pkg)
        proc = run(["--check", "--log-level", "ERROR"], cwd=pkg)
        record("--check passes after rebuilding", proc.returncode == 0)

        # --- 7. -o redirection -------------------------------------------
        other = work / "custom-out"
        proc = run([str(pkg), "-o", str(other), "--log-level", "ERROR"], cwd=work)
        record(
            "-o writes <pkgname>.py into the given directory",
            proc.returncode == 0 and (other / "demo.py").is_file(),
            proc.stderr[-400:],
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)

    failures = [name for name, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failures)}/{len(CHECKS)} checks passed")
    if failures:
        print("failed: " + ", ".join(failures))
        return 1
    return 0


def _compiles(text: str) -> bool:
    """Return ``True`` when ``text`` is valid python.

    Args:
        text: Module source.
    """
    try:
        compile(text, "<generated>", "exec")
    except SyntaxError:
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
