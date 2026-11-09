"""Comprehensive regression tests for the five consolidation issues."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from refactor_utils.core import consolidate
from refactor_utils.imports import build_import_section, future_section
from refactor_utils.models import dotted_sort_key, ident_sort_key
from refactor_utils.parser import parse_module_file
from refactor_utils.resolver import Resolver
from refactor_utils.writer import render_consolidated, render_init


def _write_pkg(root: Path, files: dict[str, str]) -> Path:
    """Create a package tree under ``root`` from relative-path -> source mappings."""
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def _consolidate_and_import(
    tmp_path: Path, pkg_name: str, files: dict[str, str], **kwargs
):
    """Consolidate ``files`` into an output package and import it at runtime."""
    src_dir = _write_pkg(tmp_path / "src" / pkg_name, files)
    out_pkg_name = f"out_{pkg_name}"
    out_dir = tmp_path / "dist" / out_pkg_name
    result = consolidate(src_dir, out_dir, **kwargs)
    cons_text = (out_dir / "_consolidated.py").read_text(encoding="utf-8")
    init_text = (out_dir / "__init__.py").read_text(encoding="utf-8")
    compile(cons_text, f"{out_pkg_name}/_consolidated.py", "exec")
    compile(init_text, f"{out_pkg_name}/__init__.py", "exec")

    dist_parent = str(tmp_path / "dist")
    sys.path.insert(0, dist_parent)
    try:
        sys.modules.pop(out_pkg_name, None)
        sys.modules.pop(f"{out_pkg_name}._consolidated", None)
        imported = importlib.import_module(out_pkg_name)
        return imported, cons_text, init_text, result
    finally:
        sys.path.remove(dist_parent)
        sys.modules.pop(out_pkg_name, None)
        sys.modules.pop(f"{out_pkg_name}._consolidated", None)


# ===========================================================================
# Issue 1: __all__ must not be treated as a constant or emitted
# ===========================================================================


def test_all_top_level_and_annotated_never_emitted(tmp_path: Path) -> None:
    """Top-level, type-annotated, and bare ``__all__`` never appear in output."""
    mod, cons_text, _init_text, _res = _consolidate_and_import(
        tmp_path,
        "all_basic",
        {
            "__init__.py": '__all__: list[str] = ["ALPHA", "BETA"]\n',
            "a.py": '__all__: list[str]\n__all__ = ["ALPHA"]\nALPHA = 10\n',
            "b.py": '__all__ = ("BETA",)\nBETA = 20\n',
        },
        all_mode="respect",
    )
    assert "__all__" not in cons_text
    assert mod.ALPHA == 10
    assert mod.BETA == 20
    assert mod.__all__ == ["ALPHA", "BETA"]


def test_all_chained_and_tuple_unpacking_strip_all_keep_siblings(
    tmp_path: Path,
) -> None:
    """Chained and unpacked assignments strip ``__all__`` while keeping siblings."""
    mod, cons_text, _init_text, _res = _consolidate_and_import(
        tmp_path,
        "all_chained",
        {
            "__init__.py": "",
            "a.py": ('EXPORTED = __all__ = ["FOO", "EXPORTED"]\nFOO = 1\n'),
            "b.py": (
                '__all__ = NAMES = ["BAR", "NAMES", "EXTRA"]\n'
                '__all__, EXTRA = ["BAR", "NAMES", "EXTRA"], 99\n'
                "BAR = 2\n"
            ),
        },
        all_mode="respect",
    )
    assert "__all__" not in cons_text
    assert mod.EXPORTED == ["FOO", "EXPORTED"]
    assert mod.NAMES == ["BAR", "NAMES", "EXTRA"]
    assert mod.EXTRA == 99
    assert mod.FOO == 1
    assert mod.BAR == 2


def test_all_conditional_and_guarded_never_emitted(tmp_path: Path) -> None:
    """Conditional and ``try/except`` ``__all__`` blocks are stripped cleanly."""
    mod, cons_text, _init_text, _res = _consolidate_and_import(
        tmp_path,
        "all_cond",
        {
            "__init__.py": "",
            "a.py": (
                "import sys\n"
                'if sys.platform == "win32":\n'
                '    __all__ = ["A_VAL"]\n'
                "else:\n"
                '    __all__ = ["A_VAL"]\n'
                "A_VAL = 11\n"
            ),
            "b.py": (
                '__all__ = ["B_VAL"]\n'
                "if True:\n"
                '    __all__.append("B_EXTRA")\n'
                "    B_EXTRA = 22\n"
                "B_VAL = 21\n"
            ),
            "c.py": (
                "try:\n"
                "    import math\n"
                '    __all__ = ["C_VAL"]\n'
                "except ImportError:\n"
                "    __all__ = []\n"
                "C_VAL = 31\n"
            ),
        },
        all_mode="respect",
    )
    assert "__all__" not in cons_text
    assert mod.A_VAL == 11
    assert mod.B_VAL == 21
    assert mod.B_EXTRA == 22
    assert mod.C_VAL == 31


def test_all_mutations_del_and_internal_import_never_emitted(
    tmp_path: Path,
) -> None:
    """Method/subscript mutations, ``del __all__``, and ``from .a import __all__``."""
    mod, cons_text, _init_text, res = _consolidate_and_import(
        tmp_path,
        "all_mut",
        {
            "__init__.py": "from .a import *\nfrom .a import __all__\n",
            "a.py": (
                '__all__ = ["ONE", "DROP_ME"]\n'
                '__all__.insert(0, "ZERO")\n'
                '__all__.remove("DROP_ME")\n'
                "__all__[0] = 'ZERO'\n"
                "ZERO = 0\n"
                "ONE = 1\n"
                "DROP_ME = -1\n"
            ),
            "b.py": ('__all__ = ["TEMP"]\nTMP = 1\ndel TMP, __all__\nPUBLIC_B = 5\n'),
        },
        all_mode="merge",
    )
    assert "__all__" not in cons_text
    assert not any("cannot resolve internal import" in w for w in res.warnings)
    assert mod.ZERO == 0
    assert mod.ONE == 1
    assert mod.PUBLIC_B == 5


# ===========================================================================
# Issue 2: Names starting with '_' sort before normal alphabetic characters
# ===========================================================================


def test_ident_and_dotted_sort_key_orders_underscore_first() -> None:
    """Underscore precedes uppercase, lowercase, and digits at any position."""
    raw = ["alpha", "Alpha", "_alpha", "_Alpha", "__alpha", "_1", "A_B", "AA"]
    ordered = sorted(raw, key=ident_sort_key)
    assert ordered == [
        "__alpha",
        "_1",
        "_Alpha",
        "_alpha",
        "A_B",
        "AA",
        "Alpha",
        "alpha",
    ]

    mods = ["pkg.alpha", "pkg.Alpha", "pkg._base", "pkg.__init__"]
    assert sorted(mods, key=dotted_sort_key) == [
        "pkg.__init__",
        "pkg._base",
        "pkg.Alpha",
        "pkg.alpha",
    ]


def test_import_section_sorts_underscore_first_within_each_category() -> None:
    """Import rendering places ``_`` names first inside const/class/func groups."""
    keys = [
        ("from", "pkg_ext", "Z_CONST", None),
        ("from", "pkg_ext", "A_CONST", None),
        ("from", "pkg_ext", "_Z_CONST", None),
        ("from", "pkg_ext", "__A_CONST", None),
        ("from", "pkg_ext", "AlphaClass", None),
        ("from", "pkg_ext", "_ZetaClass", None),
        ("from", "pkg_ext", "alpha_fn", None),
        ("from", "pkg_ext", "_zeta_fn", None),
        ("import", "pkg_b", None),
        ("import", "_pkg_a", None),
    ]
    rendered = build_import_section(keys, {})
    lines = [ln for ln in rendered.splitlines() if ln.strip()]
    assert lines[0] == "import _pkg_a"
    assert lines[1] == "import pkg_b"
    # Extract imported item names from the from-import block
    import re

    from_body = "\n".join(lines[2:])
    from_body = re.sub(r"^from\s+\S+\s+import\s*\(?", "", from_body)
    items = [
        tok.strip() for tok in from_body.replace(")", "").split(",") if tok.strip()
    ]
    assert items == [
        "__A_CONST",
        "_Z_CONST",
        "A_CONST",
        "Z_CONST",
        "_ZetaClass",
        "AlphaClass",
        "_zeta_fn",
        "alpha_fn",
    ]
    assert future_section(["annotations", "_feature"]) == (
        "from __future__ import _feature, annotations"
    )


def test_init_and_module_discovery_sort_underscore_first(tmp_path: Path) -> None:
    """``__init__.py`` exports and module priority order ``_`` before ``A-Z``."""
    mod, _cons_text, _init_text, _res = _consolidate_and_import(
        tmp_path,
        "sort_pkg",
        {
            "__init__.py": '__version__ = "1.0.0"\n',
            "_base.py": (
                '__all__ = ["_EXP_CONST", "_ExpClass", "_exp_fn", "SHARED"]\n'
                "_EXP_CONST = 1\n"
                "A_B = 2\n"
                "AB = 3\n"
                "SHARED = 'from__base'\n"
                "class _ExpClass: pass\n"
                "class AlphaClass: pass\n"
                "def _exp_fn(): return 1\n"
                "def alpha_fn(): return 2\n"
            ),
            "Alpha.py": "SHARED = 'from_Alpha'\n",
        },
        all_mode="merge",
    )
    assert mod.SHARED == "from__base"
    assert mod.__all__ == [
        "__version__",
        "_EXP_CONST",
        "A_B",
        "AB",
        "Alpha_SHARED",
        "SHARED",
        "_ExpClass",
        "AlphaClass",
        "_exp_fn",
        "alpha_fn",
    ]


# ===========================================================================
# Issue 3: if TYPE_CHECKING: merging and deduplication against normal imports
# ===========================================================================


def test_type_checking_merged_and_deduped_against_normal_imports(
    tmp_path: Path,
) -> None:
    """``if TYPE_CHECKING:`` blocks merge and omit imports already imported normally."""
    _mod, cons_text, _init_text, _res = _consolidate_and_import(
        tmp_path,
        "tc_pkg",
        {
            "__init__.py": "",
            "a.py": (
                "import os.path\n"
                "from typing import TYPE_CHECKING, Any, Optional as Opt\n"
                "if TYPE_CHECKING:\n"
                "    import os\n"
                "    import json\n"
                "    from typing import Any, Optional as Opt, Sequence\n"
                "    from .b import Helper\n"
                "VAL: Any = os.path.sep\n"
                "MAYBE: Opt[str] = None\n"
            ),
            "b.py": (
                "import typing as t\n"
                "if t.TYPE_CHECKING:\n"
                "    import pathlib\n"
                "    from typing import Mapping\n"
                "class Helper:\n"
                "    pass\n"
            ),
        },
    )
    assert cons_text.count("if TYPE_CHECKING:") == 1
    assert "if t.TYPE_CHECKING:" not in cons_text
    before_tc, after_tc = cons_text.split("if TYPE_CHECKING:", 1)
    tc_section = after_tc.split("# ---", 1)[0]

    # Normal imports appear at top level
    assert "import os.path" in before_tc
    assert "Any" in before_tc
    assert "Optional as Opt" in before_tc
    # Type-only imports stay inside TYPE_CHECKING, not hoisted to top level
    assert "import json" not in before_tc
    assert "import pathlib" not in before_tc
    assert "Sequence" not in before_tc
    assert "Mapping" not in before_tc
    # Merged TYPE_CHECKING contains only non-duplicate external imports
    assert "    import json" in tc_section
    assert "    import pathlib" in tc_section
    assert "    from typing import Mapping, Sequence" in tc_section
    assert "import os" not in tc_section
    assert "Any" not in tc_section
    assert "Opt" not in tc_section
    assert "from .b" not in tc_section


def test_type_checking_keeps_distinct_aliases_and_submodules(
    tmp_path: Path,
) -> None:
    """Distinct aliases or dotted submodules stay in ``if TYPE_CHECKING:``."""
    _mod, cons_text, _init_text, _res = _consolidate_and_import(
        tmp_path,
        "tc_alias",
        {
            "__init__.py": "",
            "a.py": (
                "import os\n"
                "import json as j\n"
                "from typing import TYPE_CHECKING\n"
                "if TYPE_CHECKING:\n"
                "    import os.path\n"
                "    import json\n"
                "CUR = os.name + j.dumps({})\n"
            ),
        },
    )
    tc_section = cons_text.split("if TYPE_CHECKING:", 1)[1].split("# ---", 1)[0]
    assert "    import json" in tc_section
    assert "    import os.path" in tc_section


def test_type_checking_omitted_when_all_imports_already_present(
    tmp_path: Path,
) -> None:
    """Empty ``if TYPE_CHECKING:`` guard and unused ``TYPE_CHECKING`` are dropped."""
    _mod, cons_text, _init_text, _res = _consolidate_and_import(
        tmp_path,
        "tc_empty",
        {
            "__init__.py": "",
            "a.py": (
                "from typing import TYPE_CHECKING, Any\n"
                "if TYPE_CHECKING:\n"
                "    from typing import Any\n"
                "    from .b import Item\n"
                "X: Any = 1\n"
            ),
            "b.py": "class Item:\n    pass\n",
        },
    )
    assert "if TYPE_CHECKING:" not in cons_text
    assert "TYPE_CHECKING" not in cons_text


def test_type_checking_declarations_rewritten_and_not_exported_at_runtime(
    tmp_path: Path,
) -> None:
    """``if TYPE_CHECKING:`` aliases track renames and stay out of ``__init__.py``."""
    mod, cons_text, init_text, _res = _consolidate_and_import(
        tmp_path,
        "tc_decl",
        {
            "__init__.py": "",
            "a.py": "class Node:\n    kind = 'a'\n",
            "b.py": (
                "from typing import TYPE_CHECKING, TypeAlias\n"
                "class Node:\n"
                "    kind = 'b'\n"
                "if TYPE_CHECKING:\n"
                "    NodeList: TypeAlias = list[Node]\n"
            ),
        },
    )
    assert "NodeList: TypeAlias = list[b_Node]" in cons_text
    assert "NodeList" not in init_text
    assert not hasattr(mod, "NodeList")


# ===========================================================================
# Issue 4: Private '_' names excluded from __init__.py & all public constants emitted
# ===========================================================================


def test_private_symbols_excluded_from_init(tmp_path: Path) -> None:
    """``_``-prefixed constants, classes, and functions stay out of ``__init__.py``."""
    mod, cons_text, init_text, _res = _consolidate_and_import(
        tmp_path,
        "priv_pkg",
        {
            "__init__.py": (
                'import os\nfrom typing import TYPE_CHECKING\n__version__ = "2.1.0"\n'
            ),
            "a.py": (
                "_SECRET_CONST = 42\n"
                "__INTERNAL_CONST = 99\n"
                "PUBLIC_CONST = _SECRET_CONST + 1\n"
                "class _PrivateHelper:\n"
                "    pass\n"
                "class PublicService(_PrivateHelper):\n"
                "    pass\n"
                "def _private_fn() -> int:\n"
                "    return __INTERNAL_CONST\n"
                "def public_fn() -> int:\n"
                "    return _private_fn()\n"
                "def main() -> int:\n"
                "    return 0\n"
            ),
            "b.py": '__version__ = "0.0.1"\n_SECRET_CONST = 100\n',
        },
        main_name="_cli_entry",
    )
    assert "_SECRET_CONST" in cons_text
    assert "_PrivateHelper" in cons_text
    assert "_private_fn" in cons_text
    assert mod.__version__ == "2.1.0"
    assert mod.PUBLIC_CONST == 43
    assert mod.public_fn() == 99
    assert mod.__all__ == [
        "__version__",
        "PUBLIC_CONST",
        "PublicService",
        "public_fn",
    ]
    for forbidden in (
        "_SECRET_CONST",
        "_b_SECRET_CONST",
        "__INTERNAL_CONST",
        "_PrivateHelper",
        "_private_fn",
        "__b_version__",
        "_cli_entry",
        "os",
        "TYPE_CHECKING",
    ):
        assert forbidden not in init_text
        assert forbidden not in mod.__all__


def test_private_in_one_module_all_does_not_leak_other_module_or_recompute(
    tmp_path: Path,
) -> None:
    """Explicit ``__all__ = ["_x"]`` scopes to that symbol and not ``recompute``."""
    files = {
        "__init__.py": "",
        "a.py": '__all__ = ["_shared"]\n_shared = "allowed"\n',
        "b.py": '_shared = "still_private"\nPUBLIC = 1\n',
    }
    mod_merge, _, _, _ = _consolidate_and_import(
        tmp_path / "m1", "p1", files, all_mode="merge"
    )
    assert "_shared" in mod_merge.__all__
    assert "_b_shared" not in mod_merge.__all__

    mod_recomp, _, _, _ = _consolidate_and_import(
        tmp_path / "m2", "p2", files, all_mode="recompute"
    )
    assert "_shared" not in mod_recomp.__all__
    assert "_b_shared" not in mod_recomp.__all__
    assert mod_recomp.__all__ == ["PUBLIC"]


def test_all_public_constants_including_conditional_and_guarded_in_init(
    tmp_path: Path,
) -> None:
    """Plain, annotated, unpacked, conditional, and guarded constants export."""
    mod, _cons_text, init_text, _res = _consolidate_and_import(
        tmp_path,
        "consts_pkg",
        {
            "__init__.py": "from .sub import _INTERNAL_DEFAULT as PUBLIC_DEFAULT\n",
            "sub.py": (
                "PLAIN_CONST = 10\n"
                "TYPED_CONST: int = 20\n"
                "SPLIT_TYPED: int\n"
                "SPLIT_TYPED = 30\n"
                "BARE_ANNOTATION_ONLY: str\n"
                "UNPACK_A, UNPACK_B = 40, 50\n"
                "_INTERNAL_DEFAULT = 60\n"
                "if True:\n"
                "    COND_CONST = 70\n"
                "else:\n"
                "    COND_CONST = -1\n"
                "try:\n"
                "    import definitely_missing_optional_dep_12345\n"
                "    HAS_OPTIONAL = True\n"
                "except ImportError:\n"
                "    HAS_OPTIONAL = False\n"
            ),
        },
    )
    assert mod.PLAIN_CONST == 10
    assert mod.TYPED_CONST == 20
    assert mod.SPLIT_TYPED == 30
    assert mod.UNPACK_A == 40
    assert mod.UNPACK_B == 50
    assert mod.PUBLIC_DEFAULT == 60
    assert mod.COND_CONST == 70
    assert mod.HAS_OPTIONAL is False
    assert "BARE_ANNOTATION_ONLY" not in init_text
    assert "_INTERNAL_DEFAULT" not in mod.__all__
    for expected in (
        "COND_CONST",
        "HAS_OPTIONAL",
        "PLAIN_CONST",
        "PUBLIC_DEFAULT",
        "SPLIT_TYPED",
        "TYPED_CONST",
        "UNPACK_A",
        "UNPACK_B",
    ):
        assert expected in mod.__all__


# ===========================================================================
# Issue 5: Constants defined in multiple places with different values
# ===========================================================================


@pytest.mark.parametrize("strategy", ["prefix", "suffix", "module_path"])
def test_conflicting_constants_with_different_values_across_strategies(
    tmp_path: Path, strategy: str
) -> None:
    """Conflicting constants keep distinct values and rewrite refs per strategy."""
    mod, _cons_text, _init_text, _res = _consolidate_and_import(
        tmp_path / strategy,
        "conflict_pkg",
        {
            "__init__.py": "",
            "a.py": (
                "TIMEOUT = 10\n"
                "SHARED_SAME = 999\n"
                "def read_a() -> tuple[int, int]:\n"
                "    return (TIMEOUT, SHARED_SAME)\n"
            ),
            "b.py": (
                "TIMEOUT = 60\n"
                "SHARED_SAME = 999\n"
                "def read_b() -> tuple[int, int]:\n"
                "    return (TIMEOUT, SHARED_SAME)\n"
            ),
            "c.py": (
                "from .b import TIMEOUT as B_TIMEOUT\n"
                "from . import a\n"
                "TIMEOUT = 120\n"
                "COMBINED = a.TIMEOUT + B_TIMEOUT + TIMEOUT\n"
                "def read_c() -> int:\n"
                "    return COMBINED\n"
            ),
        },
        rename_strategy=strategy,
    )
    assert mod.read_a() == (10, 999)
    assert mod.read_b() == (60, 999)
    assert mod.read_c() == 190
    assert mod.COMBINED == 190


def test_conflicting_constants_error_strategy_raises(tmp_path: Path) -> None:
    """``rename_strategy='error'`` raises on constants with different values."""
    p1 = tmp_path / "a.py"
    p2 = tmp_path / "b.py"
    p1.write_text("LIMIT = 10\n", encoding="utf-8")
    p2.write_text("LIMIT = 20\n", encoding="utf-8")
    mods = {
        "pkg.a": parse_module_file(str(p1), "pkg.a", False),
        "pkg.b": parse_module_file(str(p2), "pkg.b", False),
    }
    with pytest.raises(RuntimeError, match="Name collision on 'LIMIT'"):
        Resolver(mods, rename_strategy="error").build()


@pytest.mark.parametrize("dedupe_mode", ["safe", "aggressive"])
def test_derived_constants_with_different_base_values_not_deduped(
    tmp_path: Path, dedupe_mode: str
) -> None:
    """Identical ``DERIVED = BASE * 3`` must not merge when ``BASE`` differs."""
    mod, _cons_text, _init_text, _res = _consolidate_and_import(
        tmp_path / dedupe_mode,
        "derived_pkg",
        {
            "__init__.py": "",
            "a.py": (
                "BASE = 5\n"
                "DERIVED = BASE * 3\n"
                "def get_a() -> int:\n"
                "    return DERIVED\n"
            ),
            "b.py": (
                "BASE = 100\n"
                "DERIVED = BASE * 3\n"
                "def get_b() -> int:\n"
                "    return DERIVED\n"
            ),
        },
        dedupe=dedupe_mode,
    )
    assert mod.get_a() == 15
    assert mod.get_b() == 300


def test_conditional_and_try_except_conflicting_constants(
    tmp_path: Path,
) -> None:
    """Guarded constants across modules do not overwrite one another."""
    mod, cons_text, _init_text, _res = _consolidate_and_import(
        tmp_path,
        "guarded_conflict",
        {
            "__init__.py": "",
            "a.py": (
                "try:\n"
                "    import math\n"
                "    BACKEND = 'math_a'\n"
                "except ImportError:\n"
                "    BACKEND = 'fallback_a'\n"
                "def backend_a() -> str:\n"
                "    return BACKEND\n"
            ),
            "b.py": (
                "try:\n"
                "    import missing_backend_xyz_987\n"
                "    BACKEND = 'xyz_b'\n"
                "except ImportError:\n"
                "    BACKEND = 'fallback_b'\n"
                "def backend_b() -> str:\n"
                "    return BACKEND\n"
            ),
        },
    )
    # Optional missing import must stay inside try:, never hoisted to top level
    top_imports = cons_text.split("# ---", 1)[0]
    assert "missing_backend_xyz_987" not in top_imports
    assert mod.backend_a() == "math_a"
    assert mod.backend_b() == "fallback_b"


def test_intra_module_reassigned_constant_preserves_intermediate_readers(
    tmp_path: Path,
) -> None:
    """Reassigning a constant after reading it preserves exact intermediate values."""
    mod, _cons_text, _init_text, _res = _consolidate_and_import(
        tmp_path,
        "reassign_pkg",
        {
            "__init__.py": "from .b import LEVEL\n",
            "a.py": (
                "LEVEL = 1\n"
                "EARLY_SNAPSHOT = LEVEL * 10\n"
                "LEVEL = 2\n"
                "LATE_SNAPSHOT = LEVEL * 10\n"
                "def from_a() -> tuple[int, int, int]:\n"
                "    return (EARLY_SNAPSHOT, LATE_SNAPSHOT, LEVEL)\n"
            ),
            "b.py": ("LEVEL = 50\ndef from_b() -> int:\n    return LEVEL\n"),
        },
    )
    assert mod.LEVEL == 50
    assert mod.from_a() == (10, 20, 2)
    assert mod.from_b() == 50


def test_flat_mode_preserves_merged_type_checking_guard(tmp_path: Path) -> None:
    """Flat layout also renders guards like the merged ``if TYPE_CHECKING:`` block."""
    p = tmp_path / "m.py"
    p.write_text(
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import json\nX = 1\n",
        encoding="utf-8",
    )
    plan = Resolver({"m": parse_module_file(str(p), "m", False)}).build()
    from refactor_utils.writer import render_flat

    flat = render_flat(plan, "m")
    assert "if TYPE_CHECKING:\n    import json" in flat
    assert "if TYPE_CHECKING:\n    import json" in render_consolidated(plan, "m")
    assert '"X"' in render_init(plan, "m")
