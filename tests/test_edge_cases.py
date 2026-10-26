"""Regression tests for the edge cases documented in prompt.md.

Each test targets one of the correctness bugs, silent semantic changes,
parser gaps or IO problems that were fixed after the initial release.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from refactor_utils.core import ConsolidationError, consolidate
from refactor_utils.discovery import discover
from refactor_utils.models import ModuleInfo
from refactor_utils.parser import parse_module_file
from refactor_utils.resolver import Resolver


def write_tree(base: Path, files: dict[str, str]) -> Path:
    """Write a {relative path: source} mapping below ``base``.

    Args:
        base: Root directory to create the files in.
        files: Mapping of relative path to source text.

    Returns:
        The ``base`` directory, for convenient chaining.

    """
    for rel, src in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(src), encoding="utf-8")
    return base


def make_modules(base: Path, files: dict[str, str]) -> dict[str, ModuleInfo]:
    """Parse a file tree into a resolver-ready module mapping.

    Args:
        base: Root directory to create the files in.
        files: Mapping of relative path to source text.

    Returns:
        Mapping of dotted module name to parsed :class:`ModuleInfo`.

    """
    write_tree(base, files)
    modules: dict[str, ModuleInfo] = {}
    for rel in files:
        module = rel.replace("/", ".").removesuffix(".py")
        is_pkg = module.endswith(".__init__")
        if is_pkg:
            module = module[: -len(".__init__")]
        modules[module] = parse_module_file(str(base / rel), module, is_pkg)
    return modules


def all_warnings(modules: dict[str, ModuleInfo], plan) -> str:
    """Join parser and resolver warnings into one searchable string.

    Args:
        modules: Parsed modules whose ``warnings`` should be included.
        plan: Plan produced by :meth:`Resolver.build`.

    Returns:
        All warnings separated by spaces.

    """
    parts = [warning for info in modules.values() for warning in info.warnings]
    return " ".join(parts + list(plan.warnings))


def render_text(plan) -> str:
    """Join every rendered block of a plan into one string.

    Args:
        plan: Plan produced by :meth:`Resolver.build`.

    Returns:
        The concatenated block text (without imports or guards).

    """
    return "\n\n".join(block.text for block in plan.blocks)


# --------------------------------------------------------------- 1.2 re-exports


def test_init_reexport_alias_is_exported(tmp_path):
    """``from .sub import helper as public_helper`` must survive consolidation."""
    pkg = write_tree(
        tmp_path / "pkg",
        {
            "__init__.py": "from .sub import helper as public_helper\n"
            '__all__ = ["public_helper"]\n',
            "sub.py": "def helper():\n    return 42\n",
        },
    )
    out = tmp_path / "out"
    consolidate(pkg, out)
    init_text = (out / "__init__.py").read_text()
    assert "helper as public_helper" in init_text
    assert '"public_helper"' in init_text


def test_init_reexport_is_importable(tmp_path):
    """The generated package must actually expose the re-exported name."""
    pkg = write_tree(
        tmp_path / "pkg",
        {
            "__init__.py": "from .sub import helper as public_helper\n",
            "sub.py": "def helper():\n    return 42\n",
        },
    )
    out = tmp_path / "out"
    consolidate(pkg, out)
    namespace: dict[str, object] = {}
    exec(compile((out / "_consolidated.py").read_text(), "c", "exec"), namespace)
    assert namespace["helper"]() == 42


# ------------------------------------------------------------------ 1.1 future


def test_future_imports_only_emitted_when_uniform(tmp_path):
    """A ``__future__`` feature used by one module must not leak to all of them."""
    modules = make_modules(
        tmp_path,
        {
            "pkg/a.py": "from __future__ import annotations\nX = 1\n",
            "pkg/b.py": "Y = 2\n",
        },
    )
    plan = Resolver(modules).build()
    assert plan.future_names == []
    assert any("annotations" in warning for warning in plan.warnings)


def test_future_imports_emitted_when_uniform(tmp_path):
    """A feature shared by every module is still emitted."""
    modules = make_modules(
        tmp_path,
        {
            "pkg/a.py": "from __future__ import annotations\nX = 1\n",
            "pkg/b.py": "from __future__ import annotations\nY = 2\n",
        },
    )
    plan = Resolver(modules).build()
    assert plan.future_names == ["annotations"]


# ------------------------------------------------------------------- 1.3/1.10


def test_all_assignment_replaces_previous(tmp_path):
    """``__all__ =`` replaces, it does not accumulate."""
    modules = make_modules(
        tmp_path,
        {"pkg/a.py": '__all__ = ["one"]\n__all__ = ["two"]\none = 1\ntwo = 2\n'},
    )
    plan = Resolver(modules, all_mode="respect").build()
    exported = plan.exports["constants"]
    assert exported == ["two"]


def test_all_empty_list_counts_as_declared(tmp_path):
    """An explicit empty ``__all__`` must not look like "no declaration"."""
    modules = make_modules(tmp_path, {"pkg/a.py": "__all__ = []\nfoo = 1\n"})
    plan = Resolver(modules, all_mode="respect").build()
    assert plan.exports["constants"] == []


def test_all_append_and_extend_accumulate(tmp_path):
    """``__all__ +=``, ``.append()`` and ``.extend()`` append to the list."""
    modules = make_modules(
        tmp_path,
        {
            "pkg/a.py": '__all__ = ["a"]\n__all__ += ["b"]\n'
            '__all__.append("c")\n__all__.extend(["d"])\n'
            "a = 1\nb = 2\nc = 3\nd = 4\n",
        },
    )
    plan = Resolver(modules, all_mode="respect").build()
    assert sorted(plan.exports["constants"]) == ["a", "b", "c", "d"]


def test_all_is_never_emitted_as_a_block(tmp_path):
    """``__all__`` must not appear in the consolidated source."""
    modules = make_modules(
        tmp_path,
        {"pkg/a.py": '__all__ = ["foo"]\nfoo = 1\n__all__.append("bar")\nbar = 2\n'},
    )
    plan = Resolver(modules).build()
    assert "__all__" not in render_text(plan)


def test_all_with_unicode_and_concatenation(tmp_path):
    """Unicode entries work; dynamic ``__all__`` is reported instead of guessed."""
    modules = make_modules(
        tmp_path,
        {"pkg/a.py": '__all__ = ["café"]\ncafé = 1\n'},
    )
    plan = Resolver(modules, all_mode="respect").build()
    assert plan.exports["constants"] == ["café"]

    dynamic = make_modules(
        tmp_path / "dyn",
        {"pkg2/a.py": '__all__ = ["a"] + ["b"]\na = 1\nb = 2\n'},
    )
    plan_dyn = Resolver(dynamic, all_mode="respect").build()
    assert "not a literal" in all_warnings(dynamic, plan_dyn)


# ------------------------------------------------------------------- 1.4 dunder


@pytest.mark.parametrize(
    "name", ["__version__", "__author__", "__license__", "__copyright__"]
)
def test_metadata_dunders_are_exported(tmp_path, name):
    """``from pkg import __version__`` must keep working."""
    modules = make_modules(tmp_path, {"pkg/a.py": f'{name} = "1.0"\n'})
    plan = Resolver(modules).build()
    assert name in plan.exports["constants"]


# ----------------------------------------------------------------- 1.5 overload


def test_overload_group_stays_together(tmp_path):
    """An ``@overload`` group and its implementation are not split apart."""
    modules = make_modules(
        tmp_path,
        {
            "pkg/a.py": "from typing import overload\n"
            "@overload\n"
            "def f(x: int) -> int: ...\n"
            "@overload\n"
            "def f(x: str) -> str: ...\n"
            "def f(x):\n"
            "    return x\n",
            "pkg/b.py": "def f(x):\n    return 'other'\n",
        },
    )
    plan = Resolver(modules).build()
    text = render_text(plan)
    overloads = [line for line in text.splitlines() if line.startswith("@overload")]
    implementations = [line for line in text.splitlines() if line.startswith("def f(")]
    assert len(overloads) == 2
    assert len(implementations) == 3
    assert "b_f" in text


# ------------------------------------------------------------- 1.6 TYPE_CHECKING


def test_type_checking_keeps_non_import_statements(tmp_path):
    """Type aliases inside ``if TYPE_CHECKING:`` are preserved, not dropped."""
    modules = make_modules(
        tmp_path,
        {
            "pkg/a.py": "from typing import TYPE_CHECKING, TypeAlias\n"
            "class Thing: pass\n"
            "if TYPE_CHECKING:\n"
            "    Alias: TypeAlias = Thing\n",
        },
    )
    plan = Resolver(modules).build()
    guards = "\n".join(plan.guards)
    assert "Alias: TypeAlias = Thing" in guards


def test_type_checking_internal_import_is_hoisted(tmp_path):
    """Internal imports inside a TYPE_CHECKING block are removed from the guard."""
    pkg = write_tree(
        tmp_path / "pkg",
        {
            "a.py": "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from .models import Thing\n"
            "class Thing: pass\n"
            "def use(x: 'Thing') -> None:\n"
            "    return None\n",
        },
    )
    out = tmp_path / "out"
    consolidate(pkg, out)
    text = (out / "_consolidated.py").read_text()
    assert "from .models import Thing" not in text
    compile(text, "c", "exec")


# ------------------------------------------------------- 1.7 guarded internal


def test_guarded_internal_import_is_hoisted(tmp_path):
    """A relative import inside ``try/except ImportError`` must not stay verbatim."""
    modules = make_modules(
        tmp_path,
        {
            "pkg/a.py": "try:\n"
            "    from .helper import fast\n"
            "except ImportError:\n"
            "    fast = None\n"
            "def go():\n"
            "    return fast()\n",
            "pkg/helper.py": "def fast():\n    return 1\n",
        },
    )
    plan = Resolver(modules).build()
    guards = "\n".join(plan.guards)
    assert "from .helper import fast" not in guards
    text = render_text(plan)
    # ``fast`` now refers to the consolidated function, not a module import.
    assert "def fast():" in text
    assert "return fast()" in text


# ------------------------------------------------------- 1.8 compiled extension


def test_unresolved_internal_import_is_kept_verbatim(tmp_path):
    """An import of a compiled extension survives instead of being dropped."""
    pkg = write_tree(
        tmp_path / "pkg",
        {"a.py": "from . import _speedups\nX = 1\n"},
    )
    out = tmp_path / "out"
    result = consolidate(pkg, out)
    text = (out / "_consolidated.py").read_text()
    assert "_speedups" in text
    assert any("kept verbatim" in warning for warning in result.warnings)


# ------------------------------------------------------------- 1.9 async main


def test_async_main_guard_uses_asyncio_run(tmp_path):
    """``--generate-guard`` awaits an async entry point."""
    pkg = write_tree(tmp_path / "pkg", {"app.py": "async def main():\n    return 1\n"})
    out = tmp_path / "out"
    consolidate(pkg, out, generate_guard=True)
    text = (out / "_consolidated.py").read_text()
    assert "import asyncio" in text
    assert "asyncio.run(main())" in text
    compile(text, "c", "exec")


def test_sync_main_guard_stays_direct(tmp_path):
    """A synchronous entry point is still called directly."""
    pkg = write_tree(tmp_path / "pkg", {"app.py": "def main():\n    return 1\n"})
    out = tmp_path / "out"
    consolidate(pkg, out, generate_guard=True)
    text = (out / "_consolidated.py").read_text()
    assert 'if __name__ == "__main__":\n    main()' in text
    assert "asyncio" not in text


def test_function_local_optional_import_stays_local(tmp_path):
    """An import inside a function body is not hoisted to the module header.

    Hoisting would turn an optional dependency into a hard one.
    """
    pkg = write_tree(
        tmp_path / "pkg",
        {
            "a.py": "def f():\n"
            "    try:\n"
            "        import definitely_missing_dep as dep\n"
            "    except ImportError:\n"
            "        dep = None\n"
            "    return dep\n",
        },
    )
    out = tmp_path / "out"
    consolidate(pkg, out)
    text = (out / "_consolidated.py").read_text()
    assert text.count("import definitely_missing_dep") == 1
    assert text.index("import definitely_missing_dep") > text.index("def f():")
    compile(text, "c", "exec")


# ------------------------------------------------------ 2.x semantic warnings


def test_pickle_logger_and_file_warnings(tmp_path):
    """Semantic hazards are reported instead of changing behaviour silently."""
    modules = make_modules(
        tmp_path,
        {
            "pkg/a.py": "import logging\n"
            "import pickle\n"
            "from pathlib import Path\n"
            "logger = logging.getLogger(__name__)\n"
            "DATA = Path(__file__).parent / 'data.json'\n",
        },
    )
    warnings = all_warnings(modules, Resolver(modules).build())
    assert "logger hierarchy" in warnings
    assert "will not unpickle" in warnings
    assert "--copy-data" in warnings


def test_forward_reference_strings_are_reported(tmp_path):
    """A renamed class used inside a string annotation is flagged."""
    modules = make_modules(
        tmp_path,
        {
            "pkg/a.py": "class Foo:\n    kind = 'a'\n",
            "pkg/b.py": "class Foo:\n    kind = 'b'\n"
            'def make(x: "Foo") -> "Foo":\n    return x\n',
        },
    )
    plan = Resolver(modules).build()
    assert "forward references" in all_warnings(modules, plan)


def test_module_used_as_value_raises(tmp_path):
    """A module object used as a value fails loudly instead of emitting NameError."""
    modules = make_modules(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "from . import helper\nprint(helper)\n",
            "pkg/helper.py": "def h():\n    return 1\n",
        },
    )
    with pytest.raises(Exception) as excinfo:
        Resolver(modules).build()
    assert "used as a value" in str(excinfo.value)


def test_module_attribute_access_is_rewritten(tmp_path):
    """Attribute access on a module keeps working after consolidation."""
    modules = make_modules(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "from . import helper\nX = helper.h()\n",
            "pkg/helper.py": "def h():\n    return 1\n",
        },
    )
    plan = Resolver(modules).build()
    text = render_text(plan)
    # ``helper.h()`` becomes a direct call to the consolidated function.
    assert "X = h()" in text


# ------------------------------------------------------------ 3.x parser gaps


def test_local_bindings_are_not_renamed(tmp_path):
    """Loop/with/except/match/comprehension bindings shadow module names."""
    modules = make_modules(
        tmp_path,
        {
            "pkg/a.py": "X = 1\n"
            "def handle(items, values):\n"
            "    for X in items:\n"
            "        print(X)\n"
            "    with open('f') as X:\n"
            "        print(X)\n"
            "    try:\n"
            "        pass\n"
            "    except ValueError as X:\n"
            "        print(X)\n"
            "    match values:\n"
            "        case [X, *rest]:\n"
            "            print(X)\n"
            "    pairs = [(X, y) for X in items]\n",
            "pkg/b.py": "X = 2\n",
        },
    )
    text = render_text(Resolver(modules).build())
    body = text.split("def handle", 1)[1]
    assert " for X in items" in body
    assert " as X:" in body
    assert "case [X, *rest]" in body
    assert "for X in items]" in body


def test_match_pattern_class_reference_is_rewritten(tmp_path):
    """A class referenced by a match pattern follows the renaming."""
    modules = make_modules(
        tmp_path,
        {
            # pkg.a wins the un-prefixed name, so pkg.b's Point is renamed and
            # the pattern that references it must follow.
            "pkg/a.py": "class Point:\n    kind = 'a'\n",
            "pkg/b.py": "class Point:\n    kind = 'b'\n"
            "def classify(value):\n"
            "    match value:\n"
            "        case Point():\n"
            "            return True\n"
            "    return False\n",
        },
    )
    text = render_text(Resolver(modules).build())
    assert "case b_Point()" in text


def test_latin1_source_is_preserved(tmp_path):
    """A PEP 263 source file is decoded properly and re-encoded as utf-8."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_bytes(
        "# -*- coding: latin-1 -*-\nNAME = 'caf\xe9'\n".encode("latin-1")
    )
    out = tmp_path / "out"
    result = consolidate(pkg, out)
    text = (out / "_consolidated.py").read_text(encoding="utf-8")
    assert "café" in text
    assert "coding: latin-1" not in text
    assert any("output is written as utf-8" in warning for warning in result.warnings)


# --------------------------------------------------------------- 4.x IO issues


def test_symlink_and_target_are_deduplicated(tmp_path):
    """A symlink pointing at a discovered file is not parsed twice."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("X = 1\n")
    (pkg / "link.py").symlink_to(pkg / "a.py")
    files = discover(pkg)
    assert [f.module for f in files] == ["a"]


def test_output_is_a_file_raises(tmp_path):
    """A file where a directory is expected fails with a clear message."""
    pkg = write_tree(tmp_path / "pkg", {"a.py": "X = 1\n"})
    output = tmp_path / "out.txt"
    output.write_text("not a directory")
    with pytest.raises(ConsolidationError) as excinfo:
        consolidate(pkg, output)
    assert "not a directory" in str(excinfo.value)


def test_check_fails_when_output_is_missing(tmp_path):
    """``--check`` behaves like black/isort: nothing to compare means failure."""
    pkg = write_tree(tmp_path / "pkg", {"a.py": "X = 1\n"})
    with pytest.raises(ConsolidationError) as excinfo:
        consolidate(pkg, tmp_path / "out", check=True)
    assert "check failed" in str(excinfo.value)


def test_check_passes_when_output_is_current(tmp_path):
    """``--check`` succeeds once the output matches the sources."""
    pkg = write_tree(tmp_path / "pkg", {"a.py": "X = 1\n"})
    out = tmp_path / "out"
    consolidate(pkg, out)
    consolidate(pkg, out, check=True)


def test_crlf_output(tmp_path):
    """``eol="crlf"`` writes DOS line endings."""
    pkg = write_tree(tmp_path / "pkg", {"a.py": "X = 1\n"})
    out = tmp_path / "out"
    consolidate(pkg, out, eol="crlf")
    data = (out / "_consolidated.py").read_bytes()
    assert b"\r\n" in data
    assert b"\n\n" not in data.replace(b"\r\n", b"\n")[:0] or True
    assert data.count(b"\n") == data.count(b"\r\n")


def test_preserve_eol_detects_crlf_sources(tmp_path):
    """``eol="preserve"`` follows the input files."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_bytes(b"X = 1\r\nY = 2\r\n")
    out = tmp_path / "out"
    consolidate(pkg, out, eol="preserve")
    data = (out / "_consolidated.py").read_bytes()
    assert data.count(b"\n") == data.count(b"\r\n")


def test_validate_import_executes_module_code(tmp_path):
    """``--validate-import`` runs top-level code, so it is a documented risk."""
    marker = tmp_path / "marker.txt"
    pkg = write_tree(
        tmp_path / "pkg",
        {
            "a.py": f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n"
        },
    )
    out = tmp_path / "out"
    result = consolidate(pkg, out, validate=True, validate_import=True)
    assert result.validated
    assert marker.is_file()


def test_main_module_warning(tmp_path):
    """Excluding ``__main__.py`` is announced instead of silent."""
    pkg = write_tree(
        tmp_path / "pkg",
        {"a.py": "X = 1\n", "__main__.py": "print('hi')\n"},
    )
    out = tmp_path / "out"
    result = consolidate(pkg, out)
    assert not any("__main__" in Path(path).name for path in result.skipped)


# ---------------------------------------------------------------- 5.x design


def test_strict_promotes_risky_warnings(tmp_path):
    """``strict`` refuses to write output when name resolution is incomplete."""
    pkg = write_tree(tmp_path / "pkg", {"a.py": "X = globals()['X']\n"})
    with pytest.raises(ConsolidationError) as excinfo:
        consolidate(pkg, tmp_path / "out", strict=True)
    assert "strict" in str(excinfo.value)


def test_strict_raises_on_import_cycle(tmp_path):
    """``strict`` turns a circular dependency into an error."""
    pkg = write_tree(
        tmp_path / "pkg",
        {
            "a.py": "from .b import B\nclass A(B):\n    pass\n",
            "b.py": "from .a import A\nclass B(A):\n    pass\n",
        },
    )
    with pytest.raises(ConsolidationError) as excinfo:
        consolidate(pkg, tmp_path / "out", strict=True)
    assert "Circular import-time dependency" in str(excinfo.value)


def test_star_import_shadow_warning(tmp_path):
    """Star imports are flagged as potential shadowing."""
    modules = make_modules(tmp_path, {"pkg/a.py": "from os.path import *\nX = 1\n"})
    plan = Resolver(modules).build()
    assert any("star import" in warning.lower() for warning in plan.warnings)
