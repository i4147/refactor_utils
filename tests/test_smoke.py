"""End-to-end smoke tests for the merged refactor-utils package."""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1] / "src"
if str(PKG) not in sys.path:
    sys.path.insert(0, str(PKG))


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


@pytest.fixture()
def demo_pkg(tmp_path: Path) -> Path:
    root = tmp_path / "demo"
    _write(root / "__init__.py", '"""demo package."""\n')
    _write(
        root / "util.py",
        """
        def greet(name: str) -> str:
            return f"hello {name}"


        def helper():
            return 42
        """,
    )
    _write(
        root / "app.py",
        """
        from .util import greet

        def main():
            print(greet("world"))
            return 0


        if __name__ == "__main__":
            raise SystemExit(main())
        """,
    )
    return root


def _run(args, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "refactor_utils", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_help():
    proc = _run(["--help"])
    assert proc.returncode == 0, proc.stderr
    assert "pyrefactor" in proc.stdout
    assert "--bundle" in proc.stdout
    assert "--modernize" in proc.stdout
    assert "--merged-file" in proc.stdout


def test_source_consolidation_round_trip(demo_pkg: Path, tmp_path: Path):
    out_dir = tmp_path / "out"
    proc = _run([str(demo_pkg), "-o", str(out_dir)])
    assert proc.returncode == 0, proc.stderr
    merged = out_dir / "demo.py"
    assert merged.is_file()
    text = merged.read_text(encoding="utf-8")
    # output is valid python
    ast.parse(text)
    assert "def main" in text
    assert "def greet" in text
    # no internal relative imports left
    assert "from .util" not in text


def test_modernize_six(tmp_path: Path):
    pkg = tmp_path / "sixy"
    _write(pkg / "__init__.py", "")
    _write(
        pkg / "m.py",
        """
        import six
        from six.moves.urllib.parse import quote

        def u(x):
            return six.u(x)

        def is_text(v):
            return isinstance(v, six.text_type)

        def items(d):
            return list(six.iteritems(d))
        """,
    )
    out_dir = tmp_path / "out"
    proc = _run([str(pkg), "-o", str(out_dir), "--modernize", "six"])
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "sixy.py").read_text(encoding="utf-8")
    ast.parse(text)
    assert "import six" not in text
    assert "six.iteritems" not in text
    assert "six.text_type" not in text
    assert "isinstance(v, str)" in text


def test_modernize_ospath(tmp_path: Path):
    pkg = tmp_path / "osish"
    _write(pkg / "__init__.py", "")
    _write(
        pkg / "m.py",
        """
        import os.path

        def exists(p):
            return os.path.exists(p)

        def join(a, b):
            return os.path.join(a, b)

        def ext(p):
            return os.path.splitext(p)[1]
        """,
    )
    out_dir = tmp_path / "out"
    proc = _run([str(pkg), "-o", str(out_dir), "--modernize", "ospath"])
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "osish.py").read_text(encoding="utf-8")
    ast.parse(text)
    assert "Path(p).exists()" in text
    assert "Path(a).joinpath(b)" in text
    assert "Path(p).suffix" in text


def test_bundle_mode_runs(demo_pkg: Path, tmp_path: Path):
    out = tmp_path / "bundle.py"
    proc = _run([str(demo_pkg), "--bundle", "-o", str(out)])
    assert proc.returncode == 0, proc.stderr
    assert out.is_file()
    text = out.read_text(encoding="utf-8")
    assert text.startswith("#!")
    assert "_BUNDLE" in text
    # actually run the bundle
    run = subprocess.run(
        [sys.executable, str(out)], capture_output=True, text=True, check=False
    )
    assert run.returncode == 0, run.stderr
    assert "hello world" in run.stdout


def test_merged_file_input(tmp_path: Path):
    merged = tmp_path / "merged.txt"
    merged.write_text(
        textwrap.dedent(
            '''\
            # File: demo/__init__.py
            """demo."""
            # File: demo/util.py
            def add(a, b):
                return a + b
            # File: demo/app.py
            from .util import add

            def main():
                return add(2, 3)

            if __name__ == "__main__":
                raise SystemExit(main())
            '''
        ),
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    proc = _run(["-f", str(merged), "-o", str(out_dir)])
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "merged.py").read_text(encoding="utf-8")
    ast.parse(text)
    assert "def add" in text
    assert "def main" in text


def test_trace_logging_level(monkeypatch):
    # Importing sets up the logger; verify TRACE records propagate to a handler.
    import logging as _logging

    from refactor_utils.logging_setup import logger, setup_logging

    captured: list[str] = []

    class _Capture(_logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    setup_logging("TRACE")
    handler = _Capture()
    handler.setLevel(5)
    logger._inner.addHandler(handler)
    try:
        logger.trace("trace-marker-{}", 1)
        logger.debug("debug-marker")
    finally:
        logger._inner.removeHandler(handler)
    assert any("trace-marker-1" in m for m in captured), captured
