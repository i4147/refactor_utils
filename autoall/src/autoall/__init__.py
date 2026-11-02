import argparse
import ast
import importlib
import os
import sys
import warnings
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
__version__ = "0.2.0"
__all__ = [
    "__version__",
    "ensure_all",
    "install",
    "uninstall",
    "is_installed",
    "regenerate",
    "main",
]
_MARKER_START = "# --- auto-generated __all__ (delete to regenerate) ---"
_MARKER_END = "# --- end auto-generated __all__ ---"
_INSTALL_MARKER = "# --- autoall: ensure __all__ ---"
_INSTALL_END_MARKER = "# --- end autoall ---"
def _install_block(include_submodules: bool = False, include_dunders: bool = False) -> str:
    flags = ""
    if include_submodules:
        flags += ", include_submodules=True"
    if include_dunders:
        flags += ", include_dunders=True"
    return (
        "\n"
        f"{_INSTALL_MARKER}\n"
        "from autoall import ensure_all as _ensure_all\n"
        f"_ensure_all(__package__ or __name__{flags})\n"
        f"{_INSTALL_END_MARKER}\n"
    )
def _init_path(pkg: ModuleType) -> Path | None:
    f = getattr(pkg, "__file__", None)
    if not f:
        return None
    p = Path(f)
    return p if p.name == "__init__.py" else None
def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".autoall.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
def _strip_between(src: str, start: str, end: str) -> str:
    if start not in src:
        return src
    before, rest = src.split(start, 1)
    after = rest.split(end, 1)[1] if end in rest else ""
    return (before.rstrip() + "\n" + after.lstrip("\n")).rstrip() + "\n"
def _extract_between(src: str, start: str, end: str) -> str | None:
    if start not in src or end not in src.split(start, 1)[1]:
        return None
    return src.split(start, 1)[1].split(end, 1)[0]
def _parse_all_literal(src: str) -> list[str] | None:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        for t in targets:
            if isinstance(t, ast.Name) and t.id == "__all__":
                try:
                    val = ast.literal_eval(value)
                except (ValueError, TypeError, SyntaxError):
                    return None
                return [str(x) for x in val] if isinstance(val, (list, tuple)) else None
    return None
def _has_all_binding(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
                return True
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if isinstance(node.target, ast.Name) and node.target.id == "__all__":
                return True
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                if a.asname == "__all__":
                    return True
                if isinstance(node, ast.ImportFrom) and a.name == "__all__" and a.asname is None:
                    return True
    return False
def _public(name: str, include_dunders: bool) -> bool:
    if not name.isidentifier():
        return False
    if name.startswith("__") and name.endswith("__"):
        return include_dunders
    return not name.startswith("_")
def _is_own_module(name: str, root: str) -> bool:
    return name == root or name.startswith(root + ".")
def _relative_target(pkg: ModuleType, node: ast.ImportFrom) -> str:
    base = pkg.__name__.split(".")
    if node.level > 1:
        base = base[: len(base) - (node.level - 1)]
    parts = base + ([node.module] if node.module else [])
    return ".".join(parts)
def _star_names(pkg: ModuleType, node: ast.ImportFrom) -> list[str]:
    root = pkg.__name__.split(".")[0]
    modname = _relative_target(pkg, node)
    if not modname or modname == pkg.__name__ or not _is_own_module(modname, root):
        return []
    try:
        mod = importlib.import_module(modname)
    except Exception:
        return []
    explicit = getattr(mod, "__all__", None)
    if isinstance(explicit, (list, tuple)):
        return [n for n in explicit if isinstance(n, str) and _public(n, False)]
    out: list[str] = []
    for name, value in vars(mod).items():
        if not _public(name, False):
            continue
        if getattr(value, "__module__", None) == modname:
            out.append(name)
    return out
def _collect_names(
    pkg: ModuleType,
    tree: ast.Module,
    *,
    include_submodules: bool,
    include_dunders: bool,
) -> list[str]:
    root = pkg.__name__.split(".")[0]
    names: list[str] = []
    seen: set[str] = set()
    def add(name: str) -> None:
        if name in seen or not _public(name, include_dunders):
            return
        if not include_submodules and isinstance(pkg.__dict__.get(name), ModuleType):
            return
        seen.add(name)
        names.append(name)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            add(node.name)
        elif isinstance(node, ast.TypeAlias):
            if isinstance(node.name, ast.Name):
                add(node.name.id)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                for sub in ast.walk(target):
                    if isinstance(sub, ast.Name):
                        add(sub.id)
        elif isinstance(node, ast.AnnAssign):
            if node.value is not None and isinstance(node.target, ast.Name):
                add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            own = node.level > 0 or (node.module is not None and _is_own_module(node.module, root))
            if not own:
                continue
            for alias in node.names:
                if alias.name == "*":
                    for name in _star_names(pkg, node):
                        add(name)
                else:
                    add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname and _is_own_module(alias.name, root):
                    add(alias.asname)
    return sorted(names)
def _build_block(names: Iterable[str]) -> str:
    lines = ["", _MARKER_START, "__all__ = ["]
    lines.extend(f"    {n!r}," for n in names)
    lines.extend(["]", _MARKER_END, ""])
    return "\n".join(lines)
def _resolve_path(target: str | Path) -> Path:
    p = Path(target)
    if p.is_dir():
        init = p / "__init__.py"
        if init.is_file():
            return init
    elif p.is_file() and p.name == "__init__.py":
        return p
    try:
        mod = importlib.import_module(str(target))
    except ImportError as exc:
        raise FileNotFoundError(f"cannot resolve {target!r}: {exc}") from exc
    init = _init_path(mod)
    if init is None:
        raise FileNotFoundError(f"{target!r} is not a file-based package")
    return init
def ensure_all(
    pkg_name: str,
    *,
    persist: bool = True,
    include_submodules: bool = False,
    include_dunders: bool = False,
) -> list[str]:
    pkg = sys.modules.get(pkg_name) or importlib.import_module(pkg_name)
    if "__all__" in pkg.__dict__:
        return list(pkg.__all__)
    init_py = _init_path(pkg)
    if init_py is None:
        warnings.warn(f"autoall: {pkg_name!r} is not a file-based package", stacklevel=2)
        return []
    try:
        src = init_py.read_text(encoding="utf-8")
    except OSError:
        src = ""
    user_src = _strip_between(src, _MARKER_START, _MARKER_END)
    user_all = _parse_all_literal(user_src)
    if user_all is not None:
        pkg.__all__ = user_all
        return user_all
    try:
        user_tree = ast.parse(user_src)
    except SyntaxError as exc:
        warnings.warn(f"autoall: cannot parse {init_py}: {exc}", stacklevel=2)
        return []
    if _has_all_binding(user_tree):
        existing = pkg.__dict__.get("__all__")
        if isinstance(existing, (list, tuple)):
            return [str(x) for x in existing]
        warnings.warn(
            f"autoall: {init_py} defines a dynamic __all__; leaving it untouched",
            stacklevel=2,
        )
        return []
    block = _extract_between(src, _MARKER_START, _MARKER_END)
    if block is not None:
        block_all = _parse_all_literal(block)
        if block_all is not None:
            pkg.__all__ = block_all
            return block_all
    names = _collect_names(
        pkg,
        user_tree,
        include_submodules=include_submodules,
        include_dunders=include_dunders,
    )
    pkg.__all__ = names
    if persist and names:
        base = user_src.rstrip() + "\n"
        try:
            _atomic_write(init_py, base + _build_block(names))
        except OSError as exc:
            warnings.warn(f"autoall: cannot persist to {init_py}: {exc}", stacklevel=2)
    return names
def install(
    target: str | Path,
    *,
    include_submodules: bool = False,
    include_dunders: bool = False,
) -> Path:
    init_py = _resolve_path(target)
    src = init_py.read_text(encoding="utf-8")
    if _INSTALL_MARKER in src:
        return init_py
    _atomic_write(init_py, src.rstrip() + "\n" + _install_block(include_submodules, include_dunders))
    return init_py
def uninstall(target: str | Path) -> Path:
    init_py = _resolve_path(target)
    src = init_py.read_text(encoding="utf-8")
    new_src = _strip_between(
        _strip_between(src, _INSTALL_MARKER, _INSTALL_END_MARKER),
        _MARKER_START,
        _MARKER_END,
    )
    if new_src != src:
        _atomic_write(init_py, new_src)
    return init_py
def is_installed(target: str | Path) -> bool:
    try:
        init_py = _resolve_path(target)
    except FileNotFoundError:
        return False
    return _INSTALL_MARKER in init_py.read_text(encoding="utf-8")
def regenerate(
    pkg_name: str,
    *,
    include_submodules: bool = False,
    include_dunders: bool = False,
) -> list[str]:
    pkg = sys.modules.get(pkg_name) or importlib.import_module(pkg_name)
    init_py = _init_path(pkg)
    if init_py is None:
        raise RuntimeError(f"{pkg_name!r} is not a file-based package")
    src = init_py.read_text(encoding="utf-8")
    _atomic_write(init_py, _strip_between(src, _MARKER_START, _MARKER_END))
    pkg.__dict__.pop("__all__", None)
    return ensure_all(
        pkg_name,
        include_submodules=include_submodules,
        include_dunders=include_dunders,
    )
def _cli_install(args: argparse.Namespace) -> int:
    for t in args.targets:
        try:
            print(
                f"patched:   {install(t, include_submodules=args.submodules, include_dunders=args.dunders)}"
            )
        except FileNotFoundError as exc:
            print(f"skipped:   {exc}", file=sys.stderr)
    return 0
def _cli_uninstall(args: argparse.Namespace) -> int:
    for t in args.targets:
        try:
            print(f"unpatched: {uninstall(t)}")
        except FileNotFoundError as exc:
            print(f"skipped:   {exc}", file=sys.stderr)
    return 0
def _cli_regen(args: argparse.Namespace) -> int:
    for n in args.names:
        print(
            f"{n}: {regenerate(n, include_submodules=args.submodules, include_dunders=args.dunders)}"
        )
    return 0
def _cli_status(args: argparse.Namespace) -> int:
    rc = 0
    for t in args.targets:
        try:
            init_py = _resolve_path(t)
            state = (
                "installed"
                if _INSTALL_MARKER in init_py.read_text(encoding="utf-8")
                else "not installed"
            )
            print(f"{state:<13} {init_py}")
        except FileNotFoundError as exc:
            print(f"missing       {exc}", file=sys.stderr)
            rc = 1
    return rc
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="autoall",
        description="Auto-generate __all__ for Python packages.",
    )
    parser.add_argument("--version", action="version", version=f"autoall {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, fn, help_ in (
        ("install", _cli_install, "append ensure_all() to a package's __init__.py"),
        ("uninstall", _cli_uninstall, "remove the injection and generated block"),
        ("status", _cli_status, "show whether a package is instrumented"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument(
            "targets",
            nargs="+",
            metavar="TARGET",
            help="package dir, path to __init__.py, or dotted module name",
        )
        if name == "install":
            p.add_argument("--submodules", action="store_true", help="also list bound submodules")
            p.add_argument("--dunders", action="store_true", help="also list dunder constants")
        p.set_defaults(func=fn)
    p_regen = sub.add_parser("regen", help="force regeneration for imported packages")
    p_regen.add_argument("names", nargs="+", metavar="NAME", help="dotted package names")
    p_regen.add_argument("--submodules", action="store_true", help="also list bound submodules")
    p_regen.add_argument("--dunders", action="store_true", help="also list dunder constants")
    p_regen.set_defaults(func=_cli_regen)
    args = parser.parse_args(argv)
    return args.func(args) or 0
if __name__ == "__main__":
    raise SystemExit(main())