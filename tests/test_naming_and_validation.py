"""Regression tests: valid generated names, syntax validation, docstring pruning."""

import ast
import keyword
import shutil
import textwrap
from pathlib import Path

import pytest

from refactor_utils import ConsolidationError, consolidate, validate_syntax
from refactor_utils import core
from refactor_utils.resolver import is_valid_identifier, make_identifier

REPO = Path(__file__).resolve().parents[1]


def write_tree(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


def build(tmp_path: Path, files: dict[str, str], **kwargs) -> tuple[str, object]:
    src = tmp_path / "src"
    write_tree(src, files)
    out = tmp_path / "out"
    result = consolidate(src, out, filename="gen", workers=1, **kwargs)
    return (out / "gen.py").read_text(encoding="utf-8"), result


def bound_names(text: str) -> list[str]:
    """Every name bound at module level by class/def/assignment."""
    names: list[str] = []
    for node in ast.parse(text).body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            names += [t.id for t in node.targets if isinstance(t, ast.Name)]
    return names


# --------------------------------------------------------------------------- #
# make_identifier
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2", "m2"),
        ("2_Tokenizer", "m2_Tokenizer"),
        ("123abc", "m123abc"),
        ("my-mod", "my_mod"),
        ("a.b c", "a_b_c"),
        ("class", "class_"),
        ("None", "None_"),
        ("ok_name", "ok_name"),
        ("_private", "_private"),
        ("", "mod"),
        ("---", "mod"),
    ],
)
def test_make_identifier(raw, expected):
    got = make_identifier(raw)
    assert got == expected
    assert is_valid_identifier(got)


@pytest.mark.parametrize("raw", ["9", "0x", "a-b", "é2", "²", "for", "x y", "💥", "1.5"])
def test_make_identifier_is_always_valid(raw):
    got = make_identifier(raw)
    assert got.isidentifier() and not keyword.iskeyword(got)
    ast.parse(f"class {got}: pass")


# --------------------------------------------------------------------------- #
# collision renames stay valid python
# --------------------------------------------------------------------------- #
def test_numeric_module_names_produce_valid_renames(tmp_path: Path):
    """The reported bug: ``1.py`` + ``2.py`` produced ``class 2_Tokenizer``."""
    text, result = build(
        tmp_path,
        {
            "1.py": """
                __version__ = "1"

                class Tokenizer:
                    kind = "one"

                def make():
                    return Tokenizer()
            """,
            "2.py": """
                __version__ = "2"

                class Tokenizer:
                    kind = "two"

                def _helper():
                    return Tokenizer()

                def make():
                    return _helper()
            """,
        },
    )
    assert "2_Tokenizer:" not in text.replace("m2_Tokenizer:", "")
    names = bound_names(text)
    assert all(is_valid_identifier(n) for n in names), names
    assert len(names) == len(set(names)), names
    assert {"Tokenizer", "m2_Tokenizer", "make", "m2_make"} <= set(names)
    for _mod, _old, new in result.plan.renamed:
        assert is_valid_identifier(new)
    # references follow the rename and the module really works
    ns: dict = {}
    exec(compile(text, "gen.py", "exec"), ns)
    assert ns["make"]().kind == "one"
    assert ns["m2_make"]().kind == "two"


@pytest.mark.parametrize("stem", ["2", "my-mod", "class", "3d.utils", "été"])
def test_odd_file_names_never_break_the_output(tmp_path: Path, stem: str):
    text, result = build(
        tmp_path,
        {
            "a.py": "def run():\n    return 'a'\n\n_x = 1\n",
            f"{stem}.py": "def run():\n    return 'other'\n\n_x = 2\n",
        },
    )
    names = bound_names(text)
    assert all(is_valid_identifier(n) for n in names), names
    assert len(names) == len(set(names)), names
    assert result.validated


def test_reported_mytest_sample_is_valid(tmp_path: Path):
    src = tmp_path / "mytest"
    src.mkdir()
    for name in ("1.py", "2.py"):
        shutil.copy(REPO / "mytest" / name, src / name)
    out = tmp_path / "out"
    result = consolidate(src, out, filename="result", workers=1)
    text = (out / "result.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
    assert "Tokenizer" in classes and "m2_Tokenizer" in classes
    assert "Encoding" in classes and "m2_Encoding" in classes
    assert all(is_valid_identifier(new) for _m, _o, new in result.plan.renamed)
    assert ast.get_docstring(tree).startswith("Consolidated from")


def test_main_name_must_be_an_identifier(tmp_path: Path):
    write_tree(tmp_path / "src", {"a.py": "def main():\n    pass\n"})
    for bad in ("2main", "my-main", "class", ""):
        with pytest.raises(ConsolidationError, match="identifier"):
            consolidate(
                tmp_path / "src", tmp_path / "out", filename="gen", main_name=bad
            )
    assert not (tmp_path / "out").exists()


# --------------------------------------------------------------------------- #
# validate_syntax
# --------------------------------------------------------------------------- #
def test_validate_syntax_accepts_valid_code():
    tree = validate_syntax("x = 1\n\ndef f():\n    return x\n", "ok.py")
    assert isinstance(tree, ast.Module)


def test_validate_syntax_does_not_execute_code():
    # would raise at import time; a syntax-only check must not care
    validate_syntax("import module_that_does_not_exist_xyz\nraise SystemExit(1)\n")


def test_validate_syntax_rejects_invalid_code():
    with pytest.raises(ConsolidationError) as exc:
        validate_syntax("x = 1\nclass 2_Tokenizer:\n    pass\n", "bad.py")
    msg = str(exc.value)
    assert "bad.py" in msg and "line 2" in msg and "2_Tokenizer" in msg


def test_invalid_result_is_never_written(tmp_path: Path, monkeypatch):
    write_tree(tmp_path / "src", {"a.py": "X = 1\n"})
    out = tmp_path / "out"
    monkeypatch.setattr(core, "render_consolidated", lambda *a, **k: "class 2_X:\n")
    with pytest.raises(ConsolidationError, match="not valid python"):
        consolidate(tmp_path / "src", out, filename="gen", workers=1)
    assert not out.exists()


# --------------------------------------------------------------------------- #
# module docstrings are pruned
# --------------------------------------------------------------------------- #
def test_module_docstrings_are_pruned(tmp_path: Path):
    text, _ = build(
        tmp_path,
        {
            "pkg/__init__.py": 'r"""Package DOCSTRING_INIT."""\n',
            "pkg/a.py": '''
                #!/usr/bin/env python
                # coding: utf-8
                """DOCSTRING_A
                spanning lines."""
                X = 1
            ''',
            "pkg/b.py": '''
                """DOCSTRING_B part one """ \\
                    "DOCSTRING_B part two"
                from .a import X

                """DOCSTRING_STRAY"""
                Y = X + 1
            ''',
            "pkg/c.py": '''
                from __future__ import annotations
                """DOCSTRING_AFTER_FUTURE"""

                def f():
                    """Function docs stay."""
                    return 1

                class K:
                    """Class docs stay."""
            ''',
        },
    )
    assert "DOCSTRING" not in text
    assert "Function docs stay." in text and "Class docs stay." in text
    tree = ast.parse(text)
    assert ast.get_docstring(tree).startswith("Consolidated from")
    stray = [
        n
        for n in tree.body[1:]
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
    ]
    assert stray == []
    ns: dict = {}
    exec(compile(text, "gen.py", "exec"), ns)
    assert ns["Y"] == 2


def test_fstring_statements_are_not_treated_as_docstrings(tmp_path: Path):
    text, _ = build(
        tmp_path,
        {"a.py": 'CALLS = []\nf"{CALLS.append(1)}"\n'},
    )
    ns: dict = {}
    exec(compile(text, "gen.py", "exec"), ns)
    assert ns["CALLS"] == [1]
