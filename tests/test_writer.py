"""Tests for writer.py — rendering modes, formatting, headers."""

import textwrap
from pathlib import Path
from refactor_utils.parser import parse_module_file
from refactor_utils.resolver import Resolver
from refactor_utils.writer import (
    render_consolidated,
    render_init,
    render_flat,
    render_split,
    render_package,
    render_module,
)


def make_plan(tmp_path, files, **resolver_kwargs):
    mods = {}
    for fname, src in files.items():
        p = tmp_path / fname
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
        module = fname.replace("/", ".").replace(".py", "")
        if module.endswith(".__init__"):
            module = module[:-9]
        mods[module] = parse_module_file(str(p), module, fname.endswith("__init__.py"))
    r = Resolver(mods, **resolver_kwargs)
    plan = r.build()
    return plan


def test_render_consolidated_basic(tmp_path):
    plan = make_plan(tmp_path, {"pkg/a.py": "X=1\n"})
    text = render_consolidated(plan, "pkg")
    assert "Consolidated from ``pkg``" in text
    assert "X" in text and "1" in text
    assert "Constants" in text
    # Should compile
    compile(text, "_consolidated.py", "exec")


def test_render_consolidated_source_map(tmp_path):
    plan = make_plan(tmp_path, {"pkg/a.py": "X=1\n"})
    text = render_consolidated(
        plan, "pkg", source_map=True, preserve_modules=True, namespace=True
    )
    assert "# source:" in text
    assert "# Original module:" in text
    assert "# namespace:" in text


def test_render_init(tmp_path):
    plan = make_plan(tmp_path, {"pkg/a.py": "X=1\ndef foo(): pass\n"})
    init = render_init(plan, "pkg")
    assert "from ._consolidated import" in init
    assert "__all__" in init
    assert '"X"' in init or "'X'" in init or "X" in init
    compile(init, "__init__.py", "exec")


def test_render_flat(tmp_path):
    plan = make_plan(tmp_path, {"pkg/a.py": "X=1\nY=2\n"})
    text = render_flat(plan, "pkg")
    # Flat should not have section headers
    assert (
        "# ---------------------------------------------------------------------------"
        not in text
    )
    assert "X" in text and "1" in text
    compile(text, "_consolidated.py", "exec")


def test_render_split(tmp_path):
    plan = make_plan(
        tmp_path,
        {
            "pkg/a.py": "X=1\n",
            "pkg/b.py": "class Bar: pass\n",
            "pkg/c.py": "def foo(): pass\n",
        },
    )
    files = render_split(plan, "pkg")
    assert "const.py" in files
    assert "class.py" in files
    assert "func.py" in files
    for fname, content in files.items():
        compile(content, fname, "exec")


def test_render_package(tmp_path):
    plan = make_plan(tmp_path, {"pkg/a.py": "X=1\n"})
    text = render_package(plan, "pkg")
    assert "pkg" in text
    compile(text, "_consolidated.py", "exec")


def test_render_module(tmp_path):
    plan = make_plan(tmp_path, {"pkg/a.py": "X=1\n"})
    text = render_module(plan, "pkg")
    compile(text, "_consolidated.py", "exec")


def test_render_format(tmp_path):
    plan = make_plan(tmp_path, {"pkg/a.py": "X=1\n"})
    text = render_consolidated(plan, "pkg", do_format=True, formatter="black")
    # Should still compile even if black missing
    compile(text, "_consolidated.py", "exec")
