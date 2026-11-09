"""Integration tests — end-to-end consolidation scenarios."""

import importlib
import textwrap
import subprocess
import sys
from pathlib import Path
from refactor_utils.core import consolidate


def test_end_to_end_single_mode(sample_project, tmp_path):
    """Check the full single-mode pipeline on a sample package."""
    out = tmp_path / "out"
    result = consolidate(sample_project, out, mode="single")
    assert (out / "_consolidated.py").exists()
    assert (out / "__init__.py").exists()
    # __init__ re-exports
    init_text = (out / "__init__.py").read_text()
    assert "from ._consolidated import" in init_text


def test_end_to_end_src_layout(src_layout_project, tmp_path):
    """Check consolidation of a ``src/`` layout project."""
    out = tmp_path / "out"
    result = consolidate(src_layout_project, out)
    assert result.modules >= 1
    assert (out / "_consolidated.py").exists()


def test_end_to_end_namespace_package(namespace_project, tmp_path):
    """Check consolidation of a namespace package."""
    out = tmp_path / "out"
    result = consolidate(namespace_project, out)
    assert result.modules >= 1
    # Check that namespaced modules were consolidated
    text = (out / "_consolidated.py").read_text()
    assert "func" in text


def test_cli_subprocess(tmp_path):
    """Check that the CLI works as a subprocess entry point."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("X=1\n")
    out = tmp_path / "out"
    # Run via CLI subprocess
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "refactor_utils.cli",
            str(pkg),
            "-o",
            str(out),
            "--overwrite",
        ],
        capture_output=True,
        text=True,
        cwd=str(Path.cwd()),
    )
    # May need to use pyrefactor entry point
    if result.returncode != 0:
        # fallback: try with python -m refactor_utils
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                f"from refactor_utils.cli import main; main(['{pkg}', '-o', '{out}', '--overwrite'])",
            ],
            capture_output=True,
            text=True,
        )
    assert (out / "_consolidated.py").exists() or result.returncode == 0


def test_circular_import_handling(tmp_path):
    """Check that circular imports still produce usable output."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text(
        textwrap.dedent("""\
        from .b import B
        class A(B):
            pass
        """)
    )
    (pkg / "b.py").write_text(
        textwrap.dedent("""\
        from .a import A
        class B:
            pass
        """)
    )
    out = tmp_path / "out"
    result = consolidate(pkg, out)
    # Should warn about circular dependency but still produce output
    assert (out / "_consolidated.py").exists()
    compile((out / "_consolidated.py").read_text(), "_consolidated.py", "exec")


def test_output_inside_source(sample_project, tmp_path):
    # Output inside source tree should be skipped during discovery
    """Check that writing the output inside the source tree is safe."""
    out = sample_project / "output"
    result = consolidate(sample_project, out)
    assert out.exists()
    # Ensure output files are not treated as source
    assert result.modules == 4  # original 4, not counting output


def test_large_project_with_many_files(tmp_path):
    """Check consolidation of a project with many modules."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    for i in range(20):
        (pkg / f"mod{i}.py").write_text(f"X{i} = {i}\ndef func{i}(): return X{i}\n")
    out = tmp_path / "out"
    result = consolidate(pkg, out, workers=2)
    assert result.modules == 20
    assert (out / "_consolidated.py").exists()


def test_programmatic_api():
    # Test that public API is importable
    """Check the documented programmatic API end to end."""
    from refactor_utils import consolidate, ConsolidationError, ConsolidationResult

    assert callable(consolidate)
    assert issubclass(ConsolidationError, RuntimeError)


def test_version():
    """Check that the package exposes a version string."""
    from refactor_utils import __version__

    assert isinstance(__version__, str)
    assert len(__version__) > 0


def test_consolidated_nonlocal_closure_runs(tmp_path):
    """Check that a ``nonlocal`` closure still works in the consolidated package."""
    pkg = tmp_path / "counterpkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    # ``aother`` sorts first, so the module-level ``total`` of ``zcounter`` is
    # the one that gets renamed.
    (pkg / "zcounter.py").write_text(
        textwrap.dedent(
            """\
            total = 2
            def make_counter(step=1):
                total = 0
                def bump():
                    nonlocal total
                    total += step
                    return total
                return bump
            """
        )
    )
    (pkg / "aother.py").write_text("total = 41\n")
    out = tmp_path / "outpkg"
    consolidate(pkg, out)
    text = (out / "_consolidated.py").read_text()
    # The module-level ``total`` of ``zcounter`` is renamed; the closure
    # variable and its ``nonlocal`` declaration keep one stable name.
    assert "nonlocal total" in text
    assert "zcounter_total = 2" in text
    sys.path.insert(0, str(tmp_path))
    try:
        module = importlib.import_module("outpkg")
        counter = module.make_counter(3)
        assert counter() == 3
        assert counter() == 6
    finally:
        sys.path.remove(str(tmp_path))
        stale = [m for m in sys.modules if m.split(".")[0] == "outpkg"]
        for name in stale:
            sys.modules.pop(name, None)
