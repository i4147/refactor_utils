"""Tests for resolver.py — renaming, dedupe, imports, __all__, entry."""

from pathlib import Path
import textwrap
import pytest
from refactor_utils.parser import parse_module_file
from refactor_utils.resolver import Resolver
from refactor_utils.models import ModuleInfo


def make_modules(tmp_path, files: dict[str, str]) -> dict[str, ModuleInfo]:
    """Parse multiple files into a mapping of module name to ``ModuleInfo``.

    Args:
        tmp_path: Directory the files are written to.
        files: Mapping of relative path to source text.

    Returns:
        Parsed modules keyed by dotted module name.

    """
    mods = {}
    for fname, src in files.items():
        p = tmp_path / fname
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
        module = fname.replace("/", ".").replace(".py", "")
        if module.endswith(".__init__"):
            module = module[:-9]
        is_pkg = fname.endswith("__init__.py")
        info = parse_module_file(str(p), module, is_pkg)
        # Ignore errors for test setup (should be clean)
        mods[module] = info
    return mods


def test_rename_prefix(tmp_path):
    """Check prefix renaming of colliding names."""
    mods = make_modules(
        tmp_path,
        {
            "pkg/a.py": "X = 1",
            "pkg/b.py": "X = 2",
        },
    )
    r = Resolver(mods, rename_strategy="prefix")
    plan = r.build()
    # Should have renamed one X to prefix
    assert any("X" in fin for fin in plan.exports["constants"])
    # Check renamed log
    assert len(plan.renamed) == 1
    assert plan.renamed[0][2].startswith("b_") or "a_" in plan.renamed[0][2]


def test_rename_suffix(tmp_path):
    """Check suffix renaming of colliding names."""
    mods = make_modules(
        tmp_path,
        {
            "pkg/a.py": "X = 1",
            "pkg/b.py": "X = 2",
        },
    )
    r = Resolver(mods, rename_strategy="suffix")
    plan = r.build()
    assert len(plan.renamed) == 1
    assert plan.renamed[0][2].endswith("_b") or "_a" in plan.renamed[0][2]


def test_rename_module_path(tmp_path):
    """Check full-module-path renaming of colliding names."""
    mods = make_modules(
        tmp_path,
        {
            "pkg/a.py": "X = 1",
            "pkg/b.py": "X = 2",
        },
    )
    r = Resolver(mods, rename_strategy="module_path")
    plan = r.build()
    assert any("pkg" in name for _, _, name in plan.renamed)


def test_rename_error_raises(tmp_path):
    """Check that the ``error`` rename strategy fails on collisions."""
    mods = make_modules(
        tmp_path,
        {
            "pkg/a.py": "X = 1",
            "pkg/b.py": "X = 2",
        },
    )
    r = Resolver(mods, rename_strategy="error")
    with pytest.raises(RuntimeError, match="Name collision"):
        r.build()


def test_dedupe_none(tmp_path):
    """Check that ``dedupe='none'`` keeps duplicate blocks."""
    mods = make_modules(
        tmp_path,
        {
            "pkg/a.py": "def foo(): return 1",
            "pkg/b.py": "def foo(): return 1",
        },
    )
    r = Resolver(mods, dedupe="none")
    plan = r.build()
    # Both should be kept but renamed
    assert len(plan.renamed) == 1
    assert len(plan.deduped) == 0


def test_dedupe_safe(tmp_path):
    """Check that ``dedupe='safe'`` drops identical functions."""
    mods = make_modules(
        tmp_path,
        {
            "pkg/a.py": "def foo(): return 1",
            "pkg/b.py": "def foo(): return 1",
        },
    )
    r = Resolver(mods, dedupe="safe")
    plan = r.build()
    assert len(plan.deduped) == 1


def test_dedupe_aggressive(tmp_path):
    """Check how each dedupe mode treats duplicate constants."""
    mods = make_modules(
        tmp_path,
        {
            "pkg/a.py": "X = 1",
            "pkg/b.py": "X = 1",
        },
    )
    r = Resolver(mods, dedupe="aggressive")
    plan = r.build()
    # Aggressive should dedupe consts
    assert len(plan.deduped) >= 1

    r2 = Resolver(mods, dedupe="safe")
    plan2 = r2.build()
    # Safe dedupes byte-identical constant definitions too
    assert len(plan2.deduped) == 1

    mods_diff = make_modules(
        tmp_path / "diff",
        {
            "pkg2/a.py": "X = 1",
            "pkg2/b.py": "X = 2",
        },
    )
    plan3 = Resolver(mods_diff, dedupe="safe").build()
    # ... but never constants whose source differs
    assert len(plan3.deduped) == 0


def test_const_named_all_is_never_emitted(tmp_path):
    """Check that ``__all__`` never leaks into the output."""
    mods = make_modules(
        tmp_path,
        {
            "pkg/a.py": '__all__ = ["foo"]\nfoo = 1\nbar = 2\n',
        },
    )
    plan = Resolver(mods).build()
    texts = [b.text for b in plan.blocks]
    assert not any("__all__ =" in t for t in texts)
    assert "__all__" not in plan.exports["constants"]


def test_imports_prune_vs_keep(tmp_path):
    """Check that ``keep`` emits at least as many imports as ``prune``."""
    mods = make_modules(
        tmp_path,
        {
            "pkg/a.py": "import os\nimport sys\nX = 1\n",
        },
    )
    # prune should drop unused sys/os if not referenced
    r_prune = Resolver(mods, imports_mode="prune")
    plan_prune = r_prune.build()
    r_keep = Resolver(mods, imports_mode="keep")
    plan_keep = r_keep.build()
    # keep should have at least as many imports as prune
    # imports_text may contain both, but keep forces emit
    assert (
        len(plan_keep.imports_text) >= len(plan_prune.imports_text)
        or plan_keep.imports_text == plan_prune.imports_text
    )


def test_all_ignore_vs_respect(tmp_path):
    """Check the difference between ``ignore`` and ``respect`` export modes."""
    mods = make_modules(
        tmp_path,
        {
            "pkg/a.py": '__all__ = ["foo"]\nfoo = 1\nbar = 2\n',
            "pkg/b.py": "baz = 3\n",
        },
    )
    r_ignore = Resolver(mods, all_mode="ignore")
    plan_ignore = r_ignore.build()
    assert "foo" in plan_ignore.exports["constants"]
    assert "bar" in plan_ignore.exports["constants"]

    r_respect = Resolver(mods, all_mode="respect")
    plan_respect = r_respect.build()
    assert "foo" in plan_respect.exports["constants"]
    assert "bar" not in plan_respect.exports["constants"]


def test_all_merge(tmp_path):
    """Check that ``merge`` unions declared and public names."""
    mods = make_modules(
        tmp_path,
        {
            "pkg/a.py": '__all__ = ["foo"]\nfoo = 1\nbar = 2\n',
        },
    )
    r = Resolver(mods, all_mode="merge")
    plan = r.build()
    assert "foo" in plan.exports["constants"]
    assert "bar" in plan.exports["constants"]


def test_all_recompute(tmp_path):
    """Check that ``recompute`` ignores declarations and exports public names."""
    mods = make_modules(
        tmp_path,
        {
            "pkg/a.py": '__all__ = ["foo"]\nfoo = 1\nbar = 2\n_baz = 3\n',
        },
    )
    r = Resolver(mods, all_mode="recompute")
    plan = r.build()
    assert "_baz" not in plan.exports["constants"]
    assert "foo" in plan.exports["constants"]
    assert "bar" in plan.exports["constants"]


def test_entry_filter(tmp_path):
    """Check that only the requested ``__main__`` guard is kept."""
    mods = make_modules(
        tmp_path,
        {
            "pkg/a.py": 'if __name__ == "__main__": print("a")',
            "pkg/b.py": 'if __name__ == "__main__": print("b")',
        },
    )
    r_all = Resolver(mods, entry=())
    plan_all = r_all.build()
    assert len([b for b in plan_all.blocks if b.section == "entry"]) == 2

    r_filtered = Resolver(mods, entry=("pkg.a",))
    plan_f = r_filtered.build()
    assert len([b for b in plan_f.blocks if b.section == "entry"]) == 1


def test_main_name_rename(tmp_path):
    """Check the ``main_name`` override, including collisions."""
    mods = make_modules(
        tmp_path,
        {
            "pkg/a.py": "def main(): return 42",
        },
    )
    r = Resolver(mods, main_name="cli_main")
    plan = r.build()
    # final mapping should be cli_main
    assert any("cli_main" in v for v in plan.exports.values()) or any(
        "cli_main" == fin for fin in r.final.values()
    )


def test_generate_guard(tmp_path):
    """Check that a missing ``__main__`` guard can be generated."""
    mods = make_modules(
        tmp_path,
        {
            "pkg/a.py": "def my_main(): return 1",
        },
    )
    r = Resolver(mods, generate_guard=True)
    plan = r.build()
    # Should not generate guard because no main func named "main"
    # Change to have main
    mods2 = make_modules(tmp_path, {"pkg/b.py": "def main(): return 1"})
    r2 = Resolver(mods2, generate_guard=True)
    plan2 = r2.build()
    assert any("generated" in b.origin for b in plan2.blocks) or any(
        "__main__" in b.text for b in plan2.blocks
    )


def test_namespace_source_map_flags(tmp_path):
    """Check that namespace and source-map flags reach the plan."""
    mods = make_modules(tmp_path, {"pkg/a.py": "X=1"})
    r = Resolver(mods, namespace=True, preserve_modules=True, source_map=True)
    plan = r.build()
    # Check that blocks have origin info
    assert any(b.origin for b in plan.blocks)


def _block_texts(plan) -> str:
    """Join every rendered block of ``plan`` into one string.

    Args:
        plan: Plan returned by ``Resolver.build``.

    Returns:
        All block texts separated by blank lines.

    """
    return "\n\n".join(b.text for b in plan.blocks)


def test_nonlocal_declaration_keeps_enclosing_binding_stable(tmp_path):
    """Check that a ``nonlocal`` target is never renamed apart from its binding."""
    mods = make_modules(
        tmp_path,
        {
            "pkg/a.py": "x = 1\n",
            "pkg/b.py": textwrap.dedent(
                """\
                x = 2
                def make_counter(lock):
                    x = 0
                    with lock as x:
                        def inc():
                            nonlocal x
                            x += 1
                            return x
                        return inc
                """
            ),
        },
    )
    plan = Resolver(mods).build()
    text = _block_texts(plan)
    # The module-level ``x`` of pkg.b is renamed; the closure variable and the
    # ``nonlocal`` declaration must stay one single binding.
    assert "b_x = 2" in text
    assert "nonlocal x" in text
    assert "    x = 0" in text
    assert "b_x = 0" not in text
    assert "nonlocal b_x" not in text


def test_nonlocal_counter_is_not_renamed(tmp_path):
    """Check that a plain ``nonlocal`` counter keeps its local name."""
    mods = make_modules(
        tmp_path,
        {
            "pkg/a.py": "total = 1\n",
            "pkg/b.py": textwrap.dedent(
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
            ),
        },
    )
    plan = Resolver(mods).build()
    text = _block_texts(plan)
    assert "b_total = 2" in text
    assert "nonlocal total" in text
    assert "nonlocal b_total" not in text
    assert "b_total = 0" not in text


def test_global_declaration_follows_rename(tmp_path):
    """Check that ``global`` names are renamed with the module-level object."""
    mods = make_modules(
        tmp_path,
        {
            "pkg/a.py": "x = 1\n",
            "pkg/b.py": textwrap.dedent(
                """\
                x = 2
                def bump(items):
                    for x in items:
                        def inner():
                            global x
                            x += 1
                        inner()
                """
            ),
        },
    )
    plan = Resolver(mods).build()
    text = _block_texts(plan)
    # ``x`` inside ``inner`` refers to the module-level object, which is now
    # called ``b_x``; the loop variable of ``bump`` stays local.
    assert "global b_x" in text
    assert "b_x += 1" in text
    assert "for x in items" in text


def test_dedupe_post_rename_merges_project_references(tmp_path):
    """Check that blocks differing only by aliasing are deduped after renaming."""
    mods = make_modules(
        tmp_path,
        {
            "pkg/models.py": "class User:\n    pass\n",
            "pkg/a.py": textwrap.dedent(
                """\
                from .models import User as U
                class Box:
                    def get(self):
                        return U()
                """
            ),
            "pkg/b.py": textwrap.dedent(
                """\
                from .models import User
                class Box:
                    def get(self):
                        return User()
                """
            ),
        },
    )
    plan = Resolver(mods).build()
    texts = _block_texts(plan)
    assert "b_Box" not in texts
    assert texts.count("class Box:") == 1
    assert ("pkg.b", "Box") in plan.deduped
    # The dropped duplicate's rename must not be reported any more.
    assert all(final != "b_Box" for _mod, _orig, final in plan.renamed)


def test_dedupe_post_rename_merges_external_aliases(tmp_path):
    """Check that aliased external imports do not hide duplicates."""
    mods = make_modules(
        tmp_path,
        {
            "pkg/a.py": textwrap.dedent(
                """\
                from json import dumps
                def render():
                    return dumps({})
                """
            ),
            "pkg/b.py": textwrap.dedent(
                """\
                from json import dumps as d
                def render():
                    return d({})
                """
            ),
        },
    )
    plan = Resolver(mods).build()
    assert plan.deduped == [("pkg.b", "render")]
    assert [b.module for b in plan.blocks if b.kind == "func"] == ["pkg.a"]
    # The alias was only needed by the dropped duplicate.
    assert plan.imports_text == "from json import dumps"


def test_dedupe_post_rename_keeps_different_bodies(tmp_path):
    """Check that different bodies are never merged by the post-rename pass."""
    mods = make_modules(
        tmp_path,
        {
            "pkg/a.py": textwrap.dedent(
                """\
                from json import dumps
                def render():
                    return dumps({"a": 1})
                """
            ),
            "pkg/b.py": textwrap.dedent(
                """\
                from json import dumps as d
                def render():
                    return d({"b": 2})
                """
            ),
        },
    )
    plan = Resolver(mods).build()
    assert plan.deduped == []
    assert [b.module for b in plan.blocks if b.kind == "func"] == ["pkg.a", "pkg.b"]
    assert "b_render" in _block_texts(plan)


def test_dedupe_post_rename_disabled_by_none(tmp_path):
    """Check that ``dedupe='none'`` also disables the post-rename pass."""
    mods = make_modules(
        tmp_path,
        {
            "pkg/a.py": textwrap.dedent(
                """\
                from json import dumps
                def render():
                    return dumps({})
                """
            ),
            "pkg/b.py": textwrap.dedent(
                """\
                from json import dumps as d
                def render():
                    return d({})
                """
            ),
        },
    )
    plan = Resolver(mods, dedupe="none").build()
    assert plan.deduped == []
    assert [b.module for b in plan.blocks if b.kind == "func"] == ["pkg.a", "pkg.b"]


def test_dedupe_post_rename_misc_only_aggressive(tmp_path):
    """Check that side-effect blocks are only deduped in ``aggressive`` mode."""
    mods = make_modules(
        tmp_path,
        {
            "pkg/a.py": "from json import dumps\nprint(dumps({}))\n",
            "pkg/b.py": "from json import dumps as d\nprint(d({}))\n",
        },
    )
    safe = Resolver(mods, dedupe="safe").build()
    assert safe.deduped == []
    aggressive = Resolver(mods, dedupe="aggressive").build()
    assert [(m, n) for m, n in aggressive.deduped] == [("pkg.b", "misc")]


def test_dedupe_post_rename_reaches_fixed_point(tmp_path):
    """Check that merging a helper also merges its callers in a later pass."""
    mods = make_modules(
        tmp_path,
        {
            "pkg/m1.py": textwrap.dedent(
                """\
                from json import dumps
                def helper():
                    return dumps({})
                class Box:
                    def get(self):
                        return helper()
                """
            ),
            "pkg/m2.py": textwrap.dedent(
                """\
                from json import dumps as d
                def helper():
                    return d({})
                class Box:
                    def get(self):
                        return helper()
                """
            ),
        },
    )
    plan = Resolver(mods).build()
    # ``helper`` only becomes identical after the import alias is resolved,
    # `Box` only after ``helper`` has been merged: that needs two passes.
    assert ("pkg.m2", "helper") in plan.deduped
    assert ("pkg.m2", "Box") in plan.deduped
    assert [b.module for b in plan.blocks] == ["pkg.m1", "pkg.m1"]
    assert "m2_Box" not in _block_texts(plan)


def test_nonlocal_in_class_body_keeps_binding(tmp_path):
    """Check that a ``nonlocal`` inside a class body pins the enclosing binding."""
    mods = make_modules(
        tmp_path,
        {
            "pkg/a.py": "x = 1\n",
            "pkg/b.py": textwrap.dedent(
                """\
                x = 2
                def outer(lock):
                    x = 0
                    with lock as x:
                        class C:
                            nonlocal x
                            x = 2
                    return C
                """
            ),
        },
    )
    plan = Resolver(mods).build()
    text = _block_texts(plan)
    assert "nonlocal x" in text
    assert "    x = 0" in text
    assert "b_x = 0" not in text
