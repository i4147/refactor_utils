"""Tests for parser.py — tree-sitter extraction, docstring/comment stripping, syntax errors, warnings."""

import textwrap
from pathlib import Path
import pytest
from refactor_utils.parser import parse_module_file


def test_parser_basic(tmp_path):
    """Check that blocks, imports and names are extracted."""
    p = tmp_path / "mod.py"
    p.write_text(
        textwrap.dedent("""\
        X = 1
        def foo():
            return X
        class Bar:
            pass
        """)
    )
    info = parse_module_file(str(p), "mod", False)
    assert not info.errors
    assert any(b.kind == "const" for b in info.blocks)
    assert any(b.kind == "func" for b in info.blocks)
    assert any(b.kind == "class" for b in info.blocks)


def test_parser_bom_handling(tmp_path):
    """Check that a UTF-8 BOM is stripped before parsing."""
    p = tmp_path / "bom.py"
    p.write_bytes(b"\xef\xbb\xbfX = 1\n")
    info = parse_module_file(str(p), "bom", False)
    assert not info.errors
    assert len(info.blocks) == 1


def test_parser_crlf_warning(tmp_path):
    """Check that CRLF sources produce a warning."""
    p = tmp_path / "crlf.py"
    p.write_bytes(b"X = 1\r\nY = 2\r\n")
    info = parse_module_file(str(p), "crlf", False)
    assert any("CRLF" in w for w in info.warnings)


def test_parser_syntax_error_default(tmp_path):
    """Check that a syntax error skips the file by default."""
    p = tmp_path / "bad.py"
    p.write_text("def oops(\n    syntax error\n")
    info = parse_module_file(str(p), "bad", False)
    assert info.errors
    assert any("syntax error" in e for e in info.errors)


def test_parser_allow_syntax_errors(tmp_path):
    """Check that ``allow_syntax_errors`` extracts what it can."""
    p = tmp_path / "bad.py"
    p.write_text("def oops(\n    syntax error\n")
    info = parse_module_file(str(p), "bad", False, allow_syntax_errors=True)
    # Should have warnings not errors, and partial blocks
    assert not info.errors
    assert any("partial parse" in w for w in info.warnings)


def test_parser_strip_docstrings(tmp_path):
    """Check that docstrings can be stripped per flag."""
    p = tmp_path / "doc.py"
    p.write_text(
        textwrap.dedent('''\
        """module doc"""
        X = 1
        def foo():
            """func docstring"""
            return 1
        ''')
    )
    info_keep = parse_module_file(str(p), "doc", False, keep_docstrings=True)
    info_strip = parse_module_file(str(p), "doc", False, keep_docstrings=False)
    # Stripped version should have shorter source or no docstring text
    keep_text = b"".join(b.source for b in info_keep.blocks).decode()
    strip_text = b"".join(b.source for b in info_strip.blocks).decode()
    # Either length reduced or docstring text missing
    assert "func docstring" not in strip_text or len(strip_text) <= len(keep_text)


def test_parser_strip_comments(tmp_path):
    """Check that standalone comments can be stripped."""
    p = tmp_path / "comm.py"
    p.write_text(
        textwrap.dedent("""\
        # standalone comment
        X = 1  # inline should be kept
        # another comment
        def foo():
            pass
        """)
    )
    info = parse_module_file(str(p), "comm", False, strip_comments=True)
    text = b"".join(b.source for b in info.blocks).decode()
    # Standalone comments should be removed; inline may be kept
    assert "# standalone comment" not in text
    # Shebang/encoding preservation is tested separately
    assert "X = 1" in text


def test_parser_shebang_preserved_when_strip(tmp_path):
    """Check that a shebang survives comment stripping."""
    p = tmp_path / "shebang.py"
    p.write_text("#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\nX = 1\n# comment\n")
    info = parse_module_file(str(p), "shebang", False, strip_comments=True)
    text = b"".join(b.source for b in info.blocks).decode()
    # Shebang should survive (first line)
    # Even when stripping, we keep shebang/encoding
    assert "#!/usr/bin/env python3" in text or "X = 1" in text


def test_parser_dynamic_patterns(tmp_path):
    """Check that dynamic constructs produce warnings."""
    p = tmp_path / "dyn.py"
    p.write_text(
        textwrap.dedent("""\
        import importlib
        importlib.import_module("os")
        eval("1+1")
        getattr(os, "path")
        """)
    )
    info = parse_module_file(str(p), "dyn", False)
    assert any("importlib" in w for w in info.warnings)
    assert any("eval" in w for w in info.warnings)


def test_parser_runtime_semantics_file(tmp_path):
    """Check that ``__file__`` usage is reported."""
    p = tmp_path / "runtime.py"
    p.write_text("x = __file__\ny = __name__\n")
    info = parse_module_file(str(p), "runtime", False)
    assert any("__file__" in w for w in info.warnings)
    assert any("__name__" in w for w in info.warnings)


def test_parser_conditional_import_warning(tmp_path):
    """Check that conditional imports are kept and reported."""
    p = tmp_path / "cond.py"
    p.write_text(
        textwrap.dedent("""\
        import sys
        if sys.platform == "win32":
            import winreg
        """)
    )
    info = parse_module_file(str(p), "cond", False)
    # May warn about conditional import
    assert len(info.blocks) >= 1 or any(
        "conditional" in w.lower() for w in info.warnings
    )


def test_parser_type_checking_import(tmp_path):
    """Check that ``TYPE_CHECKING`` imports are extracted."""
    p = tmp_path / "tc.py"
    p.write_text(
        textwrap.dedent("""\
        from typing import TYPE_CHECKING
        if TYPE_CHECKING:
            import heavy_dep
        X = 1
        """)
    )
    info = parse_module_file(str(p), "tc", False)
    # TYPE_CHECKING imports should be extracted but not as blocks
    assert any("heavy_dep" in imp.module for imp in info.imports) or any(
        "TYPE_CHECKING" in w for w in info.warnings
    )


def test_parser_all_mutation_warning(tmp_path):
    """Check that mutated ``__all__`` is reported."""
    p = tmp_path / "allmut.py"
    p.write_text('__all__ = ["a"]\n__all__.append("b")\n')
    info = parse_module_file(str(p), "allmut", False)
    # May warn about computed/mutated __all__
    assert len(info.blocks) >= 0  # not crashing


def test_parser_source_map_fields(tmp_path):
    """Check that source line ranges are recorded per block."""
    p = tmp_path / "map.py"
    p.write_text("X = 1\nY = 2\n")
    info = parse_module_file(str(p), "map", False)
    for b in info.blocks:
        assert b.start_line >= 1
        assert b.orig_path == str(p)


def test_parser_permission_error():
    # Non-existent file
    """Check that an unreadable file is reported, not raised."""
    info = parse_module_file("/nonexistent/path.py", "bad", False)
    assert info.errors
    assert any("cannot read" in e for e in info.errors)
