"""Parser tests: blocks, imports, docstrings, comments, guards."""

from pyrefactor.parser import parse_module_file
from pyrefactor.models import CONST, CLASS, FUNC, MAIN


def parse(tmp_path, code, name="mod", **kw):
    path = tmp_path / f"{name}.py"
    path.write_text(code, encoding="utf-8")
    return parse_module_file(str(path), name, False, **kw)


def test_import_specs(tmp_path):
    path = tmp_path / "core.py"
    path.write_text(
        "import os\nimport os.path as osp\nfrom typing import List, Dict as D\n"
        "from . import sibling\nfrom .rel.mod import thing\n",
        encoding="utf-8",
    )
    info = parse_module_file(str(path), "pkg.core", False)
    mods = {s.module for s in info.imports}
    assert "os" in mods and "os.path" in mods
    assert "typing" in mods
    # relative imports are resolved to absolute module paths
    assert "pkg" in mods  # from . import sibling
    assert "pkg.rel.mod" in mods  # from .rel.mod import thing
    rel = [s for s in info.imports if s.relative]
    assert len(rel) == 2


def test_blocks_kinds(tmp_path):
    info = parse(
        tmp_path,
        "X = 1\n\nclass A:\n    pass\n\ndef f():\n    return X\n\n"
        'if __name__ == "__main__":\n    f()\n',
    )
    kinds = {b.kind for b in info.blocks}
    assert CONST in kinds and CLASS in kinds and FUNC in kinds and MAIN in kinds


def test_all_mutation_collected(tmp_path):
    info = parse(
        tmp_path,
        'def a():\n    pass\n\n__all__ = ["a"]\n__all__.append("b")\n',
    )
    assert info.declared_all == ["a", "b"]


def test_docstring_strip_module_function_class(tmp_path):
    info = parse(
        tmp_path,
        '"""Module doc."""\n\n'
        'def f():\n    """Func doc."""\n    return 1\n\n'
        'class C:\n    """Class doc."""\n    def m(self):\n        return 2\n',
        keep_docstrings=False,
    )
    text = b"\n".join(b.source for b in info.blocks).decode()
    assert "Module doc" not in text
    assert "Func doc" not in text
    assert "Class doc" not in text
    assert "return 1" in text and "return 2" in text


def test_docstring_strip_keeps_offsets_consistent(tmp_path):
    """Regression: stripping docstrings used to corrupt byte-offset rewrites."""
    info = parse(
        tmp_path,
        'PREFIX = "p"\n\n'
        'def run():\n    """Long docstring\n\n    spanning lines.\n    """\n'
        "    return PREFIX\n",
        keep_docstrings=False,
    )
    # Rendered via resolver to prove offsets land correctly.
    from pyrefactor.resolver import Resolver

    plan = Resolver({info.module: info}, workers=1).build()
    joined = "\n".join(b.text for b in plan.blocks)
    assert "return PREFIX" in joined
    assert "Long docstring" not in joined


def test_comment_strip(tmp_path):
    info = parse(
        tmp_path,
        "# leading comment\nX = 1  # trailing comment\nY = 2\n",
        keep_comments=False,
    )
    text = b"\n".join(b.source for b in info.blocks).decode()
    assert "#" not in text
    assert "X = 1" in text


def test_comment_strip_keeps_hash_inside_strings(tmp_path):
    """Regression: a line starting with # inside a triple-quoted string."""
    info = parse(
        tmp_path,
        'S = """\n# not a comment\n"""\nX = 1\n',
        keep_comments=False,
    )
    text = b"\n".join(b.source for b in info.blocks).decode()
    assert "# not a comment" in text
    assert "X = 1" in text


def test_syntax_error_reported(tmp_path):
    info = parse(tmp_path, "def broken(:\n    pass\n")
    assert info.errors
    assert not info.blocks


def test_crlf_normalised(tmp_path):
    path = tmp_path / "crlf.py"
    path.write_bytes(b"X = 1\r\nY = 2\r\n")
    info = parse_module_file(str(path), "crlf", False)
    assert any("CRLF" in w for w in info.warnings)
    assert all(b"\r" not in b.source for b in info.blocks)


def test_main_guard_and_try_guard(tmp_path):
    info = parse(
        tmp_path,
        "try:\n    import jsons\nexcept ImportError:\n    jsons = None\n\n"
        'if __name__ == "__main__":\n    print("hi")\n',
    )
    assert len(info.guards) == 1
    assert info.guards[0].conditional
    assert any(b.kind == MAIN for b in info.blocks)


def test_nested_import_rewritten_to_external_only(tmp_path):
    info = parse(
        tmp_path,
        "def f():\n    import os\n    return os.sep\n",
    )
    fn = next(b for b in info.blocks if b.kind == FUNC)
    nested = [r for r in fn.refs if r.kind == "nested_import"]
    assert len(nested) == 1
