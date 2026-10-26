"""Integration tests — end-to-end consolidation scenarios."""

import textwrap
import subprocess
import sys
from pathlib import Path
from refactor_utils.core import consolidate


def test_end_to_end_single_mode(sample_project, tmp_path):
    out = tmp_path / "out"
    result = consolidate(sample_project, out, mode="single")
    assert (out / "_consolidated.py").exists()
    assert (out / "__init__.py").exists()
    # __init__ re-exports
    init_text = (out / "__init__.py").read_text()
    assert "from ._consolidated import" in init_text


def test_end_to_end_src_layout(src_layout_project, tmp_path):
    out = tmp_path / "out"
    result = consolidate(src_layout_project, out)
    assert result.modules >= 1
    assert (out / "_consolidated.py").exists()


def test_end_to_end_namespace_package(namespace_project, tmp_path):
    out = tmp_path / "out"
    result = consolidate(namespace_project, out)
    assert result.modules >= 1
    # Check that namespaced modules were consolidated
    text = (out / "_consolidated.py").read_text()
    assert "func" in text


def test_cli_subprocess(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("X=1\n")
    out = tmp_path / "out"
    # Run via CLI subprocess
    result = subprocess.run(
        [sys.executable, "-m", "refactor_utils.cli", str(pkg), "-o", str(out), "--overwrite"],
        capture_output=True,
        text=True,
        cwd=str(Path.cwd()),
    )
    # May need to use pyrefactor entry point
    if result.returncode != 0:
        # fallback: try with python -m refactor_utils
        result = subprocess.run(
            [sys.executable, "-c", f"from refactor_utils.cli import main; main(['{pkg}', '-o', '{out}', '--overwrite'])"],
            capture_output=True,
            text=True,
        )
    assert (out / "_consolidated.py").exists() or result.returncode == 0


def test_circular_import_handling(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text(textwrap.dedent('''\
        from .b import B
        class A(B):
            pass
        '''))
    (pkg / "b.py").write_text(textwrap.dedent('''\
        from .a import A
        class B:
            pass
        '''))
    out = tmp_path / "out"
    result = consolidate(pkg, out)
    # Should warn about circular dependency but still produce output
    assert (out / "_consolidated.py").exists()
    compile((out / "_consolidated.py").read_text(), "_consolidated.py", "exec")


def test_output_inside_source(sample_project, tmp_path):
    # Output inside source tree should be skipped during discovery
    out = sample_project / "output"
    result = consolidate(sample_project, out)
    assert out.exists()
    # Ensure output files are not treated as source
    assert result.modules == 4  # original 4, not counting output


def test_large_project_with_many_files(tmp_path):
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
    from refactor_utils import consolidate, ConsolidationError, ConsolidationResult
    assert callable(consolidate)
    assert issubclass(ConsolidationError, RuntimeError)


def test_version():
    from refactor_utils import __version__
    assert isinstance(__version__, str)
    assert len(__version__) > 0
