"""Tests for cli.py — argument parsing, aliases, exit codes."""

import sys
import textwrap
from pathlib import Path
import pytest
from refactor_utils.cli import build_parser, main


def test_build_parser_exists():
    """Check that the CLI argument parser can be built."""
    p = build_parser()
    assert p.prog == "pyrefactor"


def test_help_contains_all_flags():
    """Check that ``--help`` documents every supported flag."""
    p = build_parser()
    help_text = p.format_help()
    for flag in [
        "--dry-run",
        "--check",
        "--stdout",
        "--config",
        "--format",
        "--diff",
        "--overwrite",
        "--include",
        "--rename-strategy",
        "--dedupe",
        "--imports",
        "--all",
        "--namespace",
        "--source-map",
        "--fail-on-warning",
        "--jobs",
        "--validate",
    ]:
        assert flag in help_text, f"missing flag {flag} in help"
    # ``--mode`` is no longer a CLI flag: ``pyrefactor [pkg]`` always produces
    # the consolidated single-file layout.
    assert "--mode" not in help_text


def test_cli_aliases():
    """Check that legacy alias flags map to their canonical options."""
    p = build_parser()
    # --jobs alias for --workers
    args = p.parse_args(["src", "-o", "out", "--jobs", "2"])
    assert args.jobs == 2
    # --no-parallel
    args = p.parse_args(["src", "--no-parallel"])
    assert args.no_parallel is True
    # --progress-bar alias
    args = p.parse_args(["src", "--progress-bar"])
    assert args.progress_bar is True
    # --force alias for --overwrite
    args = p.parse_args(["src", "--force"])
    assert args.force is True


def test_cli_dry_run_no_write(tmp_path):
    """Check that ``--dry-run`` never writes files."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("X=1\n")
    out = tmp_path / "out"
    rc = main([str(pkg), "-o", str(out), "--dry-run"])
    assert rc == 0
    assert not out.exists() or not any(out.iterdir())


def test_cli_stdout(tmp_path, capsys):
    """Check that ``--stdout`` prints the generated source."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("X=1\n")
    out = tmp_path / "out"
    rc = main([str(pkg), "-o", str(out), "--stdout", "--log-level", "ERROR"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "X = 1" in captured.out or "X" in captured.out
    # Should not create files
    assert not out.exists() or not (out / "_consolidated.py").exists()


def test_cli_strict_fails_on_skipped(tmp_path):
    """Check that ``--strict`` fails when files were skipped."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("X=1\n")
    (pkg / "bad.py").write_text("def oops(\n syntax\n")
    out = tmp_path / "out"
    rc = main([str(pkg), "-o", str(out), "--strict", "--log-level", "ERROR"])
    assert rc == 1


def test_cli_max_skipped(tmp_path):
    """Check that ``--max-skipped`` bounds the number of skipped files."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("X=1\n")
    (pkg / "bad.py").write_text("def oops(\n syntax\n")
    out = tmp_path / "out"
    rc = main([str(pkg), "-o", str(out), "--max-skipped", "0", "--log-level", "ERROR"])
    assert rc == 2  # ConsolidationError -> exit 2


def test_cli_fail_on_warning(tmp_path):
    """Check that ``--fail-on-warning`` turns warnings into failures."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    # __file__ triggers warning
    (pkg / "a.py").write_text("x = __file__\n")
    out = tmp_path / "out"
    rc = main([str(pkg), "-o", str(out), "--fail-on-warning", "--log-level", "ERROR"])
    assert rc == 2


def test_cli_validate(tmp_path):
    """Check that ``--validate`` compiles the generated output."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("X=1\n")
    out = tmp_path / "out"
    rc = main([str(pkg), "-o", str(out), "--validate", "--log-level", "ERROR"])
    assert rc == 0
    # Single consolidated file <pkgname>.py inside the output directory
    py_files = [p for p in out.iterdir() if p.suffix == ".py"]
    assert len(py_files) == 1
    compile(py_files[0].read_text(), py_files[0].name, "exec")


def test_cli_single_file_output(tmp_path):
    """Check the default CLI produces a single ``consolidated/<pkg>.py`` file."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("X=1\ndef hello(): return X\n")
    out = tmp_path / "out"
    rc = main([str(pkg), "-o", str(out), "--log-level", "ERROR"])
    assert rc == 0
    py_files = [p for p in out.iterdir() if p.suffix == ".py"]
    assert len(py_files) == 1
    assert py_files[0].name == "pkg.py"
    compile(py_files[0].read_text(), py_files[0].name, "exec")
    text = py_files[0].read_text().replace(" ", "")
    assert "X=1" in text
    assert "defhello" in text


def test_cli_unknown_mode_rejected(tmp_path):
    """``--mode`` is no longer a CLI flag and must be rejected."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("X=1\n")
    out = tmp_path / "out"
    with pytest.raises(SystemExit):
        main([str(pkg), "-o", str(out), "--mode", "invalid", "--log-level", "ERROR"])


def test_cli_include_exclude(tmp_path):
    """Check ``--include``/``--exclude`` precedence from the CLI."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "keep.py").write_text("X=1\n")
    (pkg / "conftest.py").write_text("Y=2\n")
    out = tmp_path / "out"
    # Without include, conftest excluded
    rc = main([str(pkg), "-o", str(out), "--overwrite", "--log-level", "ERROR"])
    assert rc == 0
    text = (out / "pkg.py").read_text()
    assert "Y=2" not in text.replace(" ", "")
    # With include, it should be included
    out2 = tmp_path / "out2"
    rc = main(
        [str(pkg), "-o", str(out2), "--include", "conftest.py", "--log-level", "ERROR"]
    )
    assert rc == 0
    text2 = (out2 / "pkg.py").read_text()
    assert "Y=2" in text2.replace(" ", "")


def test_cli_unknown_mode(tmp_path):
    """Check that an unknown ``--mode`` value is rejected."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("X=1\n")
    out = tmp_path / "out"
    # argparse should error on invalid choice
    with pytest.raises(SystemExit):
        main([str(pkg), "-o", str(out), "--mode", "invalid"])
