"""Tests for resolver.py — renaming, dedupe, imports, __all__, entry."""

from pathlib import Path
import textwrap
import pytest
from refactor_utils.parser import parse_module_file
from refactor_utils.resolver import Resolver
from refactor_utils.models import ModuleInfo


def make_modules(tmp_path, files: dict[str, str]) -> dict[str, ModuleInfo]:
    """Helper to parse multiple files into ModuleInfo dict."""
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
    mods = make_modules(tmp_path, {
        "pkg/a.py": "X = 1",
        "pkg/b.py": "X = 2",
    })
    r = Resolver(mods, rename_strategy="prefix")
    plan = r.build()
    # Should have renamed one X to prefix
    assert any("X" in fin for fin in plan.exports["constants"])
    # Check renamed log
    assert len(plan.renamed) == 1
    assert plan.renamed[0][2].startswith("b_") or "a_" in plan.renamed[0][2]


def test_rename_suffix(tmp_path):
    mods = make_modules(tmp_path, {
        "pkg/a.py": "X = 1",
        "pkg/b.py": "X = 2",
    })
    r = Resolver(mods, rename_strategy="suffix")
    plan = r.build()
    assert len(plan.renamed) == 1
    assert plan.renamed[0][2].endswith("_b") or "_a" in plan.renamed[0][2]


def test_rename_module_path(tmp_path):
    mods = make_modules(tmp_path, {
        "pkg/a.py": "X = 1",
        "pkg/b.py": "X = 2",
    })
    r = Resolver(mods, rename_strategy="module_path")
    plan = r.build()
    assert any("pkg" in name for _, _, name in plan.renamed)


def test_rename_error_raises(tmp_path):
    mods = make_modules(tmp_path, {
        "pkg/a.py": "X = 1",
        "pkg/b.py": "X = 2",
    })
    r = Resolver(mods, rename_strategy="error")
    with pytest.raises(RuntimeError, match="Name collision"):
        r.build()


def test_dedupe_none(tmp_path):
    mods = make_modules(tmp_path, {
        "pkg/a.py": "def foo(): return 1",
        "pkg/b.py": "def foo(): return 1",
    })
    r = Resolver(mods, dedupe="none")
    plan = r.build()
    # Both should be kept but renamed
    assert len(plan.renamed) == 1
    assert len(plan.deduped) == 0


def test_dedupe_safe(tmp_path):
    mods = make_modules(tmp_path, {
        "pkg/a.py": "def foo(): return 1",
        "pkg/b.py": "def foo(): return 1",
    })
    r = Resolver(mods, dedupe="safe")
    plan = r.build()
    assert len(plan.deduped) == 1


def test_dedupe_aggressive(tmp_path):
    mods = make_modules(tmp_path, {
        "pkg/a.py": "X = 1",
        "pkg/b.py": "X = 1",
    })
    r = Resolver(mods, dedupe="aggressive")
    plan = r.build()
    # Aggressive should dedupe consts
    assert len(plan.deduped) >= 1

    r2 = Resolver(mods, dedupe="safe")
    plan2 = r2.build()
    # Safe should not dedupe consts
    assert len(plan2.deduped) == 0


def test_imports_prune_vs_keep(tmp_path):
    mods = make_modules(tmp_path, {
        "pkg/a.py": "import os\nimport sys\nX = 1\n",
    })
    # prune should drop unused sys/os if not referenced
    r_prune = Resolver(mods, imports_mode="prune")
    plan_prune = r_prune.build()
    r_keep = Resolver(mods, imports_mode="keep")
    plan_keep = r_keep.build()
    # keep should have at least as many imports as prune
    # imports_text may contain both, but keep forces emit
    assert len(plan_keep.imports_text) >= len(plan_prune.imports_text) or plan_keep.imports_text == plan_prune.imports_text


def test_all_ignore_vs_respect(tmp_path):
    mods = make_modules(tmp_path, {
        "pkg/a.py": '__all__ = ["foo"]\nfoo = 1\nbar = 2\n',
        "pkg/b.py": 'baz = 3\n',
    })
    r_ignore = Resolver(mods, all_mode="ignore")
    plan_ignore = r_ignore.build()
    assert "foo" in plan_ignore.exports["constants"]
    assert "bar" in plan_ignore.exports["constants"]

    r_respect = Resolver(mods, all_mode="respect")
    plan_respect = r_respect.build()
    assert "foo" in plan_respect.exports["constants"]
    assert "bar" not in plan_respect.exports["constants"]


def test_all_merge(tmp_path):
    mods = make_modules(tmp_path, {
        "pkg/a.py": '__all__ = ["foo"]\nfoo = 1\nbar = 2\n',
    })
    r = Resolver(mods, all_mode="merge")
    plan = r.build()
    assert "foo" in plan.exports["constants"]
    assert "bar" in plan.exports["constants"]


def test_all_recompute(tmp_path):
    mods = make_modules(tmp_path, {
        "pkg/a.py": '__all__ = ["foo"]\nfoo = 1\nbar = 2\n_baz = 3\n',
    })
    r = Resolver(mods, all_mode="recompute")
    plan = r.build()
    assert "_baz" not in plan.exports["constants"]
    assert "foo" in plan.exports["constants"]
    assert "bar" in plan.exports["constants"]


def test_entry_filter(tmp_path):
    mods = make_modules(tmp_path, {
        "pkg/a.py": 'if __name__ == "__main__": print("a")',
        "pkg/b.py": 'if __name__ == "__main__": print("b")',
    })
    r_all = Resolver(mods, entry=())
    plan_all = r_all.build()
    assert len([b for b in plan_all.blocks if b.section == "entry"]) == 2

    r_filtered = Resolver(mods, entry=("pkg.a",))
    plan_f = r_filtered.build()
    assert len([b for b in plan_f.blocks if b.section == "entry"]) == 1


def test_main_name_rename(tmp_path):
    mods = make_modules(tmp_path, {
        "pkg/a.py": "def main(): return 42",
    })
    r = Resolver(mods, main_name="cli_main")
    plan = r.build()
    # final mapping should be cli_main
    assert any("cli_main" in v for v in plan.exports.values()) or any("cli_main" == fin for fin in r.final.values())


def test_generate_guard(tmp_path):
    mods = make_modules(tmp_path, {
        "pkg/a.py": "def my_main(): return 1",
    })
    r = Resolver(mods, generate_guard=True)
    plan = r.build()
    # Should not generate guard because no main func named "main"
    # Change to have main
    mods2 = make_modules(tmp_path, {"pkg/b.py": "def main(): return 1"})
    r2 = Resolver(mods2, generate_guard=True)
    plan2 = r2.build()
    assert any("generated" in b.origin for b in plan2.blocks) or any('__main__' in b.text for b in plan2.blocks)


def test_namespace_source_map_flags(tmp_path):
    mods = make_modules(tmp_path, {"pkg/a.py": "X=1"})
    r = Resolver(mods, namespace=True, preserve_modules=True, source_map=True)
    plan = r.build()
    # Check that blocks have origin info
    assert any(b.origin for b in plan.blocks)
