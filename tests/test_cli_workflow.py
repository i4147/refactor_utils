"""End-to-end tests for the ``pyrefactor`` command line workflow.

These tests drive :func:`refactor_utils.cli.main` exactly like a user would:

* ``pyrefactor`` with no arguments consolidates the current directory;
* ``pyrefactor <pkg>`` from a parent directory writes
  ``<pkg>/consolidated/<pkgname>.py``;
* ``[tool.pyrefactor]`` configuration is picked up automatically (and can be
  overridden with ``--config``);
* ``--check``/``--stdout`` never write files and return documented exit codes.
"""

import textwrap
from pathlib import Path

import pytest

from refactor_utils.cli import main


def make_pkg(root: Path, name: str = "demo") -> Path:
    """Create a small but realistic package under ``root/name``.

    Args:
        root: Directory that will contain the package.
        name: Package (folder) name.

    Returns:
        The package directory.
    """
    pkg = root / name
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text('"""demo package."""\nfrom .core import run\n')
    (pkg / "core.py").write_text(textwrap.dedent('''\
            """Core module."""
            import json
            from .consts import LIMIT

            def run():
                """Run the demo."""
                return json.dumps({"limit": LIMIT})

            def main():
                """Entry point."""
                return run()

            if __name__ == "__main__":
                main()
            '''))
    (pkg / "consts.py").write_text('"""Constants."""\nLIMIT = 3\n')
    return pkg


def test_no_arguments_consolidates_current_directory(tmp_path, monkeypatch):
    """``pyrefactor`` with no arguments uses ``.`` and writes one file."""
    pkg = make_pkg(tmp_path)
    monkeypatch.chdir(pkg)

    assert main(["--log-level", "ERROR"]) == 0

    out = pkg / "consolidated"
    assert [p.name for p in out.iterdir()] == ["demo.py"]
    text = (out / "demo.py").read_text()
    assert "LIMIT = 3" in text
    assert "__all__" in text
    compile(text, "demo.py", "exec")


def test_explicit_package_path_from_parent_directory(tmp_path, monkeypatch):
    """``pyrefactor pkg`` writes inside ``pkg/consolidated``."""
    pkg = make_pkg(tmp_path / "work", "demo")
    monkeypatch.chdir(tmp_path / "work")

    assert main(["demo", "--log-level", "ERROR"]) == 0
    assert (pkg / "consolidated" / "demo.py").is_file()


def test_output_option_and_default_single_file(tmp_path, monkeypatch):
    """``-o`` redirects the single generated file."""
    pkg = make_pkg(tmp_path / "work", "demo")
    monkeypatch.chdir(tmp_path / "work")

    assert main(["demo", "-o", "build", "--log-level", "ERROR"]) == 0
    assert [p.name for p in (tmp_path / "work" / "build").iterdir()] == ["demo.py"]
    assert not (pkg / "consolidated").exists()


def test_pyproject_tool_section_is_applied(tmp_path, monkeypatch):
    """``[tool.pyrefactor]`` supplies defaults for CLI flags."""
    pkg = make_pkg(tmp_path / "work", "demo")
    (tmp_path / "work" / "pyproject.toml").write_text(textwrap.dedent("""\
            [project]
            name = "demo-dist"
            version = "1.0"

            [tool.pyrefactor]
            entry = ["core"]
            main_name = "run_it"
            """))
    monkeypatch.chdir(tmp_path / "work")

    assert main(["demo", "--log-level", "ERROR"]) == 0
    text = (pkg / "consolidated" / "demo_dist.py").read_text()
    # Distribution name with a dash is normalised for the file name
    assert "def run_it():" in text


def test_explicit_config_path_overrides_auto_detection(tmp_path, monkeypatch):
    """``--config`` reads another ``pyproject.toml``."""
    pkg = make_pkg(tmp_path / "work", "demo")
    config = tmp_path / "custom.toml"
    config.write_text(textwrap.dedent("""\
            [tool.pyrefactor]
            rename_strategy = "suffix"
            main_name = "renamed_entry"
            """))
    monkeypatch.chdir(tmp_path / "work")

    assert main(["demo", "--config", str(config), "--log-level", "ERROR"]) == 0
    text = (pkg / "consolidated" / "demo.py").read_text()
    assert "def renamed_entry():" in text


def test_missing_config_file_is_an_error(tmp_path, monkeypatch):
    """A non-existent ``--config`` file exits with code 2."""
    make_pkg(tmp_path / "work", "demo")
    monkeypatch.chdir(tmp_path / "work")
    assert main(["demo", "--config", "nope.toml"]) == 2


def test_check_returns_non_zero_when_output_would_change(tmp_path, monkeypatch):
    """``--check`` is usable from CI (exit code 2 when out of date)."""
    pkg = make_pkg(tmp_path / "work", "demo")
    monkeypatch.chdir(tmp_path / "work")
    assert main(["demo", "--log-level", "ERROR"]) == 0
    assert main(["demo", "--check", "--log-level", "ERROR"]) == 0

    (pkg / "consts.py").write_text("LIMIT = 4\n")
    assert main(["demo", "--check", "--log-level", "ERROR"]) == 2


def test_stdout_writes_no_files(tmp_path, monkeypatch, capsys):
    """``--stdout`` prints the module and leaves the tree untouched."""
    pkg = make_pkg(tmp_path / "work", "demo")
    monkeypatch.chdir(tmp_path / "work")
    assert main(["demo", "--stdout", "--log-level", "ERROR"]) == 0
    out = capsys.readouterr().out
    assert "LIMIT = 3" in out
    assert "__all__" in out
    assert not (pkg / "consolidated").exists()


def test_dry_run_writes_no_files(tmp_path, monkeypatch):
    """``--dry-run`` reports without touching the output directory."""
    pkg = make_pkg(tmp_path / "work", "demo")
    monkeypatch.chdir(tmp_path / "work")
    assert main(["demo", "--dry-run", "--log-level", "ERROR"]) == 0
    assert not (pkg / "consolidated").exists()


def test_generated_module_can_be_imported(tmp_path, monkeypatch):
    """The consolidated file is a working drop-in module."""
    import importlib.util

    pkg = make_pkg(tmp_path / "work", "demo")
    monkeypatch.chdir(tmp_path / "work")
    assert main(["demo", "--log-level", "ERROR"]) == 0

    target = pkg / "consolidated" / "demo.py"
    spec = importlib.util.spec_from_file_location("demo_consolidated", target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.run() == '{"limit": 3}'
    assert module.__all__ == ["LIMIT", "main", "run"]


def test_missing_source_returns_error_code(tmp_path, monkeypatch):
    """A non-existent package path exits with code 2 instead of crashing."""
    monkeypatch.chdir(tmp_path)
    assert main(["does-not-exist", "--log-level", "ERROR"]) == 2


@pytest.mark.parametrize("extra", [["--no-parallel"], ["--executor", "thread"]])
def test_parallel_switches_work(tmp_path, monkeypatch, extra):
    """``--no-parallel``/``--executor`` do not change the result."""
    pkg = make_pkg(tmp_path / "work", "demo")
    monkeypatch.chdir(tmp_path / "work")
    assert main(["demo", *extra, "--log-level", "ERROR"]) == 0
    assert (pkg / "consolidated" / "demo.py").is_file()
