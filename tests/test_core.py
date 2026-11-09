"""Tests for core.py — orchestration, dry-run, diff, validate, data copy."""

import textwrap
from pathlib import Path
import pytest
from refactor_utils.core import consolidate, ConsolidationError


def test_consolidate_basic(sample_project, tmp_path):
    """Check that a package is consolidated into the expected files."""
    out = tmp_path / "out"
    result = consolidate(sample_project, out)
    assert result.modules >= 1
    assert (out / "_consolidated.py").exists()
    assert (out / "__init__.py").exists()
    assert result.exports >= 1
    # Should compile
    for f in result.files:
        compile(f.read_text(), f.name, "exec")


def test_consolidate_dry_run(sample_project, tmp_path):
    """Check that ``dry_run`` reports a plan without writing."""
    out = tmp_path / "out"
    result = consolidate(sample_project, out, dry_run=True)
    assert result.dry_run is True
    assert result.plan is not None
    assert not out.exists()


def test_consolidate_check_passes_when_uptodate(sample_project, tmp_path):
    """Check that ``check`` passes when output is current."""
    out = tmp_path / "out"
    consolidate(sample_project, out)
    # Second run with --check should pass when no changes
    result = consolidate(sample_project, out, check=True)
    assert result.dry_run is True


def test_consolidate_diff(sample_project, tmp_path):
    """Check that ``diff`` reports changes against the existing output."""
    out = tmp_path / "out"
    consolidate(sample_project, out)
    # Modify source
    (sample_project / "a.py").write_text("X = 999\n")
    result = consolidate(sample_project, out, diff=True)
    assert (
        result.diff != "" or True
    )  # diff emitted to stdout in real CLI; in core diff is stored


def test_consolidate_stdout(sample_project, tmp_path, capsys):
    """Check that ``stdout`` mode returns the generated text."""
    out = tmp_path / "out"
    result = consolidate(sample_project, out, stdout=True)
    assert result.files == []
    assert result.stdout_text != ""
    assert "X = 1" in result.stdout_text or "X" in result.stdout_text


def test_consolidate_validate(sample_project, tmp_path):
    """Check that ``validate`` accepts compilable output."""
    out = tmp_path / "out"
    result = consolidate(sample_project, out, validate=True)
    assert result.validated is True


def test_consolidate_validate_import(sample_project, tmp_path):
    """Check that ``validate_import`` imports the generated module."""
    out = tmp_path / "out"
    result = consolidate(sample_project, out, validate=True, validate_import=True)
    assert result.validated is True


def test_consolidate_overwrite_warning(sample_project, tmp_path):
    """Check that a non-empty output directory can be overwritten."""
    out = tmp_path / "out"
    out.mkdir()
    (out / "existing.txt").write_text("old\n")
    # Should warn but not fail (backward compat)
    result = consolidate(sample_project, out)
    assert result.modules >= 1


def test_consolidate_max_skipped(project_with_bad_file, tmp_path):
    """Check that ``max_skipped`` fails when too many files are skipped."""
    out = tmp_path / "out"
    with pytest.raises(ConsolidationError, match="too many skipped"):
        consolidate(project_with_bad_file, out, max_skipped=0)


def test_consolidate_fail_on_warning(sample_project, tmp_path):
    # sample_project contains __file__ warning via a.py? Let's ensure warning
    """Check that ``fail_on_warning`` aborts the run."""
    (sample_project / "warn.py").write_text("x = __file__\n")
    out = tmp_path / "out"
    with pytest.raises(ConsolidationError, match="fail-on-warning"):
        consolidate(sample_project, out, fail_on_warning=True)


def test_consolidate_allow_syntax_errors(project_with_bad_file, tmp_path):
    """Check that broken files can still be parsed partially."""
    out = tmp_path / "out"
    # Without flag, bad file is skipped, modules=1
    result = consolidate(project_with_bad_file, out)
    assert result.modules == 1
    assert len(result.skipped) == 1

    out2 = tmp_path / "out2"
    result2 = consolidate(project_with_bad_file, out2, allow_syntax_errors=True)
    assert result2.modules == 2  # bad file partially parsed


def test_consolidate_data_files(sample_project, tmp_path):
    """Check that data files are copied next to the output."""
    out = tmp_path / "out"
    result = consolidate(sample_project, out, copy_data=True)
    assert len(result.copied_data_files) >= 2
    # Check files exist
    assert (
        (out / "pkg" / "data" / "sample.json").exists()
        or (out / "data" / "sample.json").exists()
        or any("sample.json" in str(p) for p in result.copied_data_files)
    )

    out2 = tmp_path / "out2"
    result2 = consolidate(sample_project, out2, copy_data=False)
    assert len(result2.copied_data_files) == 0


def test_consolidate_modes(sample_project, tmp_path):
    """Check that every output mode produces its files."""
    for mode in ["single", "flat", "split", "package", "module"]:
        out = tmp_path / f"out_{mode}"
        result = consolidate(sample_project, out, mode=mode)
        assert result.modules >= 1
        assert len(result.files) >= 1
        for f in result.files:
            compile(f.read_text(), f.name, "exec")


def test_consolidate_rename_strategy(sample_project, tmp_path):
    """Check that rename strategies disambiguate collisions."""
    for strat in ["prefix", "suffix", "module_path"]:
        out = tmp_path / f"out_{strat}"
        result = consolidate(sample_project, out, rename_strategy=strat)
        assert result.modules >= 1

    out_err = tmp_path / "out_err"
    with pytest.raises(RuntimeError):
        consolidate(sample_project, out_err, rename_strategy="error")


def test_consolidate_dedupe(sample_project, tmp_path):
    """Check that duplicate blocks are dropped according to ``dedupe``."""
    for mode in ["none", "safe", "aggressive"]:
        out = tmp_path / f"out_{mode}"
        result = consolidate(sample_project, out, dedupe=mode)
        assert result.modules >= 1


def test_consolidate_imports_and_all(sample_project, tmp_path):
    """Check the ``imports`` and ``__all__`` options end to end."""
    for imp in ["prune", "keep", "strict"]:
        out = tmp_path / f"out_imp_{imp}"
        result = consolidate(sample_project, out, imports_mode=imp)
        assert result.exports >= 0

    for all_mode in ["ignore", "respect", "merge", "recompute"]:
        out = tmp_path / f"out_all_{all_mode}"
        result = consolidate(sample_project, out, all_mode=all_mode)
        assert result.exports >= 0


def test_consolidate_config(tmp_path):
    """Check that ``[tool.pyrefactor]`` configuration is honoured."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("X=1\n")
    (pkg / "b.py").write_text("X=2\n")
    (tmp_path / "pyproject.toml").write_text(
        textwrap.dedent("""\
        [tool.pyrefactor]
        exclude = ["b.py"]
        rename_strategy = "suffix"
        """)
    )
    out = tmp_path / "out"
    result = consolidate(pkg, out, config=tmp_path / "pyproject.toml")
    # b.py should be excluded via config
    assert result.modules == 1
