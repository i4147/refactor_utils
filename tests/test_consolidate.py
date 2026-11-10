"""End-to-end and unit tests for the consolidated-mode pyrefactor.

Run with:  .venv/bin/python -m pytest tests -q     (or plain `python tests/run_checks.py`)
"""

from __future__ import annotations
import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def write_tree(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


def run_cli(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = {"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"}
    return subprocess.run(
        [sys.executable, "-m", "refactor_utils.cli", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    write_tree(
        tmp_path,
        {
            "pkg/__init__.py": '''
                """Demo package."""
                from .core import helper
                __all__ = ["helper", "LIMIT"]
            ''',
            "pkg/core.py": '''
                """Core module."""
                from __future__ import annotations
                import os

                LIMIT = 10
                NAME: str = "demo"

                def helper(a: int = LIMIT) -> int:
                    """Return a value."""
                    return a + len(os.sep)
            ''',
            "pkg/other.py": """
                LIMIT = 99

                def other():
                    return LIMIT
            """,
            "data/settings.json": "{}",
        },
    )
    return tmp_path


# --------------------------------------------------------------------------- #
# output layout
# --------------------------------------------------------------------------- #
def test_default_run_emits_one_file_named_after_cwd(project: Path, monkeypatch):
    monkeypatch.chdir(project)
    result = run_cli(project, "pkg", "--no-copy-data")
    assert result.returncode == 0, result.stderr
    out = project / "output"
    assert [p.name for p in out.iterdir()] == [f"{project.name}.py"]
    text = (out / f"{project.name}.py").read_text()
    ast.parse(text)
    assert "# Constants" in text and "# Functions" in text


def test_default_filename_uses_cwd_not_source_name(project: Path, monkeypatch):
    monkeypatch.chdir(project)
    run_cli(project, "pkg", "--no-copy-data")
    assert (project / "output" / f"{project.name}.py").is_file()


def test_custom_name_flag(project: Path):
    result = run_cli(project, "pkg", "--no-copy-data", "--name", "mymod")
    assert result.returncode == 0, result.stderr
    assert (project / "output" / "mymod.py").is_file()


def test_data_files_copied_by_default(project: Path):
    result = run_cli(project, ".")
    assert result.returncode == 0, result.stderr
    assert (project / "output" / "data" / "settings.json").is_file()


# --------------------------------------------------------------------------- #
# constants / __all__
# --------------------------------------------------------------------------- #
def test_all_constants_emitted(project: Path):
    run_cli(project, "pkg", "--no-copy-data")
    text = (project / "output" / f"{project.name}.py").read_text()
    assert "LIMIT = 10" in text
    assert 'NAME: str = "demo"' in text
    assert "other_LIMIT = 99" in text


def test_dunder_all_not_emitted(tmp_path: Path):
    write_tree(
        tmp_path,
        {
            "pkg/__init__.py": """
                __all__ = ["a"]
                __all__ += ["b"]
                __all__.append("c")
            """,
            "pkg/mod.py": """
                a = 1
                b = 2
                c = 3
            """,
        },
    )
    result = run_cli(tmp_path, "pkg", "--no-copy-data")
    assert result.returncode == 0, result.stderr
    text = (tmp_path / "output" / f"{tmp_path.name}.py").read_text()
    assert "__all__" not in text
    assert "a = 1" in text


# --------------------------------------------------------------------------- #
# name resolution
# --------------------------------------------------------------------------- #
def test_identical_definitions_deduped(tmp_path: Path):
    write_tree(
        tmp_path,
        {
            "pkg/a.py": """
                def shared():
                    return 1
            """,
            "pkg/b.py": """
                def shared():
                    return 1
            """,
        },
    )
    result = run_cli(tmp_path, "pkg", "--no-copy-data")
    assert result.returncode == 0, result.stderr
    text = (tmp_path / "output" / f"{tmp_path.name}.py").read_text()
    assert text.count("def shared") == 1


def test_different_definitions_with_same_name_are_renamed(tmp_path: Path):
    write_tree(
        tmp_path,
        {
            "pkg/a.py": """
                def shared():
                    return 1
            """,
            "pkg/b.py": """
                def shared():
                    return 2

                def use():
                    return shared()
            """,
        },
    )
    result = run_cli(tmp_path, "pkg", "--no-copy-data")
    assert result.returncode == 0, result.stderr
    text = (tmp_path / "output" / f"{tmp_path.name}.py").read_text()
    # one module keeps the plain name, the other is disambiguated by module
    assert text.count("def shared(") + text.count("def b_shared(") == 2
    assert "def b_shared(" in text
    assert "return b_shared()" in text


# --------------------------------------------------------------------------- #
# TYPE_CHECKING
# --------------------------------------------------------------------------- #
def test_type_checking_imports_merged_into_head(tmp_path: Path):
    write_tree(
        tmp_path,
        {
            "pkg/__init__.py": "from .core import run\n",
            "pkg/core.py": """
                from __future__ import annotations
                from typing import TYPE_CHECKING

                if TYPE_CHECKING:
                    from typing import List
                    from .types import Thing

                def run(t: Thing, xs: List[int]) -> int:
                    return len(xs)
            """,
            "pkg/types.py": """
                class Thing:
                    pass
            """,
        },
    )
    result = run_cli(tmp_path, "pkg", "--no-copy-data")
    assert result.returncode == 0, result.stderr
    text = (tmp_path / "output" / f"{tmp_path.name}.py").read_text()
    assert "TYPE_CHECKING" not in text
    assert "from typing import List" in text
    ast.parse(text)


def test_type_checking_definitions_flattened(tmp_path: Path):
    write_tree(
        tmp_path,
        {
            "pkg/mod.py": """
                from typing import TYPE_CHECKING

                if TYPE_CHECKING:
                    PROTO_LIMIT = 10

                    class Proto:
                        pass
            """,
        },
    )
    result = run_cli(tmp_path, "pkg", "--no-copy-data")
    assert result.returncode == 0, result.stderr
    text = (tmp_path / "output" / f"{tmp_path.name}.py").read_text()
    assert "PROTO_LIMIT = 10" in text
    assert "class Proto:\n    pass" in text


# --------------------------------------------------------------------------- #
# sorting
# --------------------------------------------------------------------------- #
def test_import_sorting_private_names_first():
    sys.path.insert(0, str(SRC))
    from refactor_utils.imports import sort_key

    names = ["Beta", "_alpha", "alpha", "Zulu", "_Zulu"]
    assert sorted(names, key=sort_key) == ["_alpha", "_Zulu", "alpha", "Beta", "Zulu"]
    assert sorted(["b", "_a", "A"], key=sort_key) == ["_a", "A", "b"]


def test_export_sorting_private_names_first():
    sys.path.insert(0, str(SRC))
    from refactor_utils.resolver import Plan

    plan = Plan([], "", "", [])
    names = ["zebra", "_lead", "Apple"]
    plan.exports["functions"] = sorted(
        names, key=__import__("refactor_utils.imports", fromlist=["sort_key"]).sort_key
    )
    assert plan.exports["functions"] == ["_lead", "Apple", "zebra"]


# --------------------------------------------------------------------------- #
# validation is mandatory
# --------------------------------------------------------------------------- #
def test_broken_source_is_not_written(tmp_path: Path):
    write_tree(
        tmp_path,
        {"pkg/good.py": "A = 1\n", "pkg/broken.py": "def f(:\n    pass\n"},
    )
    result = run_cli(tmp_path, "pkg", "--no-copy-data")
    assert result.returncode != 0
    assert not (tmp_path / "output" / f"{tmp_path.name}.py").exists()


def test_generated_module_is_importable(project: Path):
    result = run_cli(project, "pkg", "--no-copy-data")
    assert result.returncode == 0, result.stderr
    out = project / "output" / f"{project.name}.py"
    code = (
        "import importlib.util, sys;"
        f"spec=importlib.util.spec_from_file_location('gen', {str(out)!r});"
        "m=importlib.util.module_from_spec(spec);"
        "sys.modules['gen']=m;"
        "spec.loader.exec_module(m);"
        "print(m.helper())"
    )
    check = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr
    assert check.stdout.strip() == "11"


# --------------------------------------------------------------------------- #
# removed flags
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "flag",
    [
        "--mode",
        "--validate",
        "--validate-import",
        "--allow-syntax-errors",
        "--max-skipped",
        "--source-map",
        "--preserve-modules",
        "--all",
        "--respect-all",
        "--no-parallel",
    ],
)
def test_removed_flags_are_gone(project: Path, flag: str):
    result = run_cli(project, "pkg", flag)
    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr


# --------------------------------------------------------------------------- #
# discovery / filtering
# --------------------------------------------------------------------------- #
def test_exclude_glob_is_source_relative(tmp_path: Path):
    write_tree(
        tmp_path,
        {"pkg/keep.py": "KEEP = 1\n", "pkg/skipme.py": "DROP = 2\n"},
    )
    result = run_cli(tmp_path, ".", "-x", "pkg/skipme.py", "--no-copy-data")
    assert result.returncode == 0, result.stderr
    text = (tmp_path / "output" / f"{tmp_path.name}.py").read_text()
    assert "KEEP = 1" in text and "DROP" not in text


def test_default_excluded_tests_dir_can_be_included(tmp_path: Path):
    write_tree(
        tmp_path,
        {"pkg/a.py": "A = 1\n", "tests/test_x.py": "TESTED = 3\n"},
    )
    run_cli(tmp_path, ".", "--no-copy-data")
    assert "TESTED" not in (tmp_path / "output" / f"{tmp_path.name}.py").read_text()
    result = run_cli(tmp_path, ".", "--include", "tests/*", "--no-copy-data")
    assert result.returncode == 0, result.stderr
    assert "TESTED = 3" in (tmp_path / "output" / f"{tmp_path.name}.py").read_text()


def test_output_dir_inside_source_is_not_ingested(tmp_path: Path):
    write_tree(tmp_path, {"pkg/a.py": "A = 1\n"})
    run_cli(tmp_path, ".", "--no-copy-data")
    first = (tmp_path / "output" / f"{tmp_path.name}.py").read_text()
    result = run_cli(tmp_path, ".", "--no-copy-data")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "output" / f"{tmp_path.name}.py").read_text() == first


def test_parallel_and_serial_runs_are_identical(tmp_path: Path):
    write_tree(
        tmp_path,
        {
            "pkg/a.py": "LIMIT = 1\n\ndef f():\n    return LIMIT\n",
            "pkg/b.py": "LIMIT = 2\n\ndef g():\n    return LIMIT\n",
        },
    )
    run_cli(tmp_path, "pkg", "--no-copy-data", "-j", "1")
    serial = (tmp_path / "output" / f"{tmp_path.name}.py").read_text()
    run_cli(tmp_path, "pkg", "--no-copy-data", "--executor", "thread")
    threaded = (tmp_path / "output" / f"{tmp_path.name}.py").read_text()
    assert serial == threaded


# --------------------------------------------------------------------------- #
# imports inside functions stay nested
# --------------------------------------------------------------------------- #
def test_nested_imports_are_not_hoisted(tmp_path: Path):
    write_tree(
        tmp_path,
        {
            "pkg/a.py": """
                def loose(raw):
                    import json
                    return json.loads(raw)

                def tight():
                    try:
                        import tomllib
                    except ImportError:
                        import tomli as tomllib
                    return tomllib
            """,
        },
    )
    result = run_cli(tmp_path, "pkg", "--no-copy-data")
    assert result.returncode == 0, result.stderr
    text = (tmp_path / "output" / f"{tmp_path.name}.py").read_text()
    head, _sep, _rest = text.partition("# " + "-" * 75)
    assert "import json" not in head
    assert "tomli" not in head
    assert "    import json" in text
    assert "    import tomllib" in text


def test_remaining_files_after_error_are_reported(tmp_path: Path):
    write_tree(tmp_path, {"pkg/ok.py": "A = 1\n", "pkg/bad.py": "def f(:\n"})
    result = run_cli(tmp_path, "pkg", "--no-copy-data")
    assert result.returncode == 2
    assert "bad.py" in (result.stderr + result.stdout)
    assert "refusing" in result.stderr


# --------------------------------------------------------------------------- #
# self-consolidation smoke test
# --------------------------------------------------------------------------- #
def test_self_consolidation_compiles_and_imports(tmp_path: Path):
    from refactor_utils.core import consolidate

    source = REPO / "src" / "refactor_utils"
    src_files = list(source.glob("*.py"))
    if not src_files:  # pragma: no cover - defensive
        pytest.skip("source tree not available")
    result = consolidate(source, tmp_path, filename="gen", copy_data=False)
    assert result.validated
    text = (tmp_path / "gen.py").read_text()
    tree = ast.parse(text)
    compile(text, "gen.py", "exec")
    bound = [
        target.id
        for node in ast.walk(tree)
        for target in getattr(node, "targets", [])
        if isinstance(target, ast.Name)
    ]
    assert "__all__" not in bound


def test_conditional_import_guards_are_not_hoisted(tmp_path: Path):
    write_tree(
        tmp_path,
        {
            "pkg/a.py": """
                try:
                    import tomllib
                except ImportError:
                    tomllib = None

                def load(raw):
                    return tomllib.loads(raw)
            """,
            "pkg/b.py": """
                import tomllib

                def other(raw):
                    return tomllib.loads(raw)
            """,
        },
    )
    result = run_cli(tmp_path, "pkg", "--no-copy-data")
    assert result.returncode == 0, result.stderr
    text = (tmp_path / "output" / f"{tmp_path.name}.py").read_text()
    head, _sep, _rest = text.partition("# " + "-" * 75)
    assert "try:" in head and "import tomllib" in head
    # the plain import from b.py must not be hoisted next to the guarded one
    assert head.count("import tomllib") == 1
    compile(text, "gen.py", "exec")


def test_guard_bound_names_are_not_shadowed(tmp_path: Path):
    write_tree(
        tmp_path,
        {
            "pkg/a.py": """
                try:
                    import tomllib
                    HAS = True
                except ImportError:
                    HAS = False
            """,
            "pkg/b.py": """
                HAS = "other"
            """,
        },
    )
    result = run_cli(tmp_path, "pkg", "--no-copy-data")
    assert result.returncode == 0, result.stderr
    text = (tmp_path / "output" / f"{tmp_path.name}.py").read_text()
    compile(text, "gen.py", "exec")
    # the guarded HAS must keep its name; the other module's HAS is renamed
    assert "HAS = True" in text
    assert 'b_HAS = "other"' in text


def test_guard_with_import_and_class_warns(tmp_path: Path):
    write_tree(
        tmp_path,
        {
            "pkg/a.py": """
                try:
                    import tomllib

                    class Loader:
                        pass
                except ImportError:
                    Loader = None
            """,
        },
    )
    result = run_cli(tmp_path, "pkg", "--no-copy-data")
    assert result.returncode == 0, result.stderr
    assert "combines imports and definitions" in result.stderr
    text = (tmp_path / "output" / f"{tmp_path.name}.py").read_text()
    assert "class Loader" in text


def test_section_headers_are_not_repeated(tmp_path: Path):
    write_tree(
        tmp_path,
        {
            "pkg/a.py": """
                class NS:
                    pass

                NS.attr = 3
            """,
        },
    )
    result = run_cli(tmp_path, "pkg", "--no-copy-data")
    assert result.returncode == 0, result.stderr
    text = (tmp_path / "output" / f"{tmp_path.name}.py").read_text()
    assert text.count("# Classes") == 1
    assert text.count("# Constants") == 1


# --------------------------------------------------------------------------- #
# multiple __main__ guards
# --------------------------------------------------------------------------- #
def test_multiple_main_guards_are_merged_into_one_block(tmp_path: Path):
    write_tree(
        tmp_path,
        {
            "pkg/a.py": """
                def first():
                    print("first ran")

                if __name__ == "__main__":
                    first()
            """,
            "pkg/b.py": """
                import sys

                def second():
                    print("second ran")

                if __name__ == "__main__":
                    try:
                        second()
                    except KeyboardInterrupt:
                        sys.exit(0)
            """,
        },
    )
    result = run_cli(tmp_path, "pkg", "--no-copy-data")
    assert result.returncode == 0, result.stderr
    text = (tmp_path / "output" / f"{tmp_path.name}.py").read_text()
    tree = ast.parse(text)
    guards = [
        node
        for node in tree.body
        if isinstance(node, ast.If) and "__main__" in ast.dump(node.test)
    ]
    # stacked guards would mean everything but the first is dead code
    assert len(guards) == 1
    assert "# from a" in text and "# from b" in text
    assert "Merged 2 '__main__' guards" in result.stderr


def test_merged_guard_runs_every_entry_point(tmp_path: Path):
    write_tree(
        tmp_path,
        {
            "pkg/a.py": """
                def first():
                    print("first ran")

                if __name__ == "__main__":
                    first()
            """,
            "pkg/b.py": """
                def second():
                    print("second ran")

                if __name__ == "__main__":
                    second()
            """,
        },
    )
    result = run_cli(tmp_path, "pkg", "--no-copy-data")
    assert result.returncode == 0, result.stderr
    generated = tmp_path / "output" / f"{tmp_path.name}.py"
    run = subprocess.run(
        [sys.executable, str(generated)], capture_output=True, text=True
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.split() == ["first", "ran", "second", "ran"]


def test_entry_flag_keeps_a_single_guard(tmp_path: Path):
    write_tree(
        tmp_path,
        {
            "pkg/a.py": 'if __name__ == "__main__":\n    pass\n',
            "pkg/b.py": 'if __name__ == "__main__":\n    pass\n',
        },
    )
    result = run_cli(tmp_path, "pkg", "--no-copy-data", "--entry", "a")
    assert result.returncode == 0, result.stderr
    text = (tmp_path / "output" / f"{tmp_path.name}.py").read_text()
    assert text.count('if __name__ == "__main__"') == 1
    assert "# from a" in text and "# from b" not in text


def test_main_function_is_kept_before_the_merged_guard(tmp_path: Path):
    write_tree(
        tmp_path,
        {
            "pkg/cli.py": """
                def main():
                    return 1

                if __name__ == "__main__":
                    main()
            """,
            "pkg/other.py": 'if __name__ == "__main__":\n    print("other")\n',
        },
    )
    result = run_cli(tmp_path, "pkg", "--no-copy-data")
    assert result.returncode == 0, result.stderr
    text = (tmp_path / "output" / f"{tmp_path.name}.py").read_text()
    tail = text[text.index("# Entry points") :]
    assert tail.count('if __name__ == "__main__"') == 1
    assert tail.index("def main()") < tail.index('if __name__ == "__main__"')


def test_guard_header_with_trailing_comment_is_merged_correctly(tmp_path: Path):
    write_tree(
        tmp_path,
        {
            "pkg/a.py": """
                # module main entry
                if __name__ == "__main__":  # pragma: no cover
                    print("a")
            """,
            "pkg/b.py": 'if __name__ == "__main__":\n    print("b")\n',
        },
    )
    result = run_cli(tmp_path, "pkg", "--no-copy-data")
    assert result.returncode == 0, result.stderr
    text = (tmp_path / "output" / f"{tmp_path.name}.py").read_text()
    tail = text[text.index("# Entry points") :]
    # the header itself must not be re-emitted inside the merged body
    assert tail.count('if __name__ == "__main__"') == 1
    run = subprocess.run(
        [sys.executable, str(tmp_path / "output" / f"{tmp_path.name}.py")],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.split() == ["a", "b"]
