"""Generate and maintain ``__all__`` for python packages.

``autoall`` can patch a package (``autoall install``), compute ``__all__`` for
an already imported package (:func:`ensure_all`), or update a plain module
file in place (:func:`ensure_all_file`) — the mode used by ``pyrefactor`` for
its ``consolidated/<pkgname>.py`` output.
"""

import argparse
import ast
import importlib
import os
import re
import sys
import warnings
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType

__version__ = "0.2.0"
__all__ = [
    "__version__",
    "build_all_block",
    "ensure_all",
    "ensure_all_file",
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


def _install_block(
    include_submodules: bool = False, include_dunders: bool = False
) -> str:
    """Return the two-line ``ensure_all`` snippet appended by :func:`install`."""
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
    """Return the ``__init__.py`` path of ``pkg`` (None for namespace packages)."""
    f = getattr(pkg, "__file__", None)
    if not f:
        return None
    p = Path(f)
    return p if p.name == "__init__.py" else None


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file + ``os.replace``).

    Args:
        path: Destination file.
        text: Full file content; written verbatim (no newline translation).
    """
    tmp = path.with_name(path.name + ".autoall.tmp")
    try:
        tmp.write_text(text, encoding="utf-8", newline="")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _marker_pattern(marker: str) -> re.Pattern[str]:
    """Return a regex matching ``marker`` as a whole line.

    Markers are matched only when they appear as standalone comment lines, so
    the same text stored in a string constant (for example autoall's own
    ``_MARKER_START``) is never mistaken for a generated block.

    Args:
        marker: Marker comment text (for example ``_MARKER_START``).

    Returns:
        Compiled pattern (``MULTILINE``, CRLF tolerant).
    """
    return re.compile(r"^" + re.escape(marker) + r"\r?$", re.MULTILINE)


def _find_marker_block(src: str, start: str, end: str) -> tuple[int, int] | None:
    """Locate a marker-delimited block in ``src``.

    Searches for the last standalone ``start`` line and the first standalone
    ``end`` line after it.

    Args:
        src: File content to search.
        start: Opening marker comment.
        end: Closing marker comment.

    Returns:
        ``(start_index, end_index)`` of the block (markers included), or
        ``None`` when the block is missing.
    """
    start_re = _marker_pattern(start)
    end_re = _marker_pattern(end)
    for match in reversed(list(start_re.finditer(src))):
        end_match = end_re.search(src, match.end())
        if end_match is not None:
            return match.start(), end_match.end()
    return None


def _strip_between(src: str, start: str, end: str) -> str:
    """Remove the text between ``start`` and ``end`` markers."""
    block = _find_marker_block(src, start, end)
    if block is None:
        return src
    before, after = src[: block[0]], src[block[1] :]
    return (before.rstrip() + "\n" + after.lstrip("\r\n")).rstrip() + "\n"


def _extract_between(src: str, start: str, end: str) -> str | None:
    """Return the text between two markers, or None when absent."""
    block = _find_marker_block(src, start, end)
    if block is None:
        return None
    start_re = _marker_pattern(start)
    match = start_re.search(src, block[0])
    if match is None:  # pragma: no cover - defensive
        return None
    return src[match.end() : block[1]].split(end, 1)[0]


def _parse_all_literal(src: str) -> list[str] | None:
    """Parse a literal ``__all__`` assignment from ``src``, or None."""
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
    """Return ``True`` when the module already binds ``__all__``."""
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
                if (
                    isinstance(node, ast.ImportFrom)
                    and a.name == "__all__"
                    and a.asname is None
                ):
                    return True
    return False


def _public(name: str, include_dunders: bool) -> bool:
    """Return ``True`` when ``name`` is part of the public API."""
    if not name.isidentifier():
        return False
    if name.startswith("__") and name.endswith("__"):
        return include_dunders
    return not name.startswith("_")


def _is_own_module(name: str, root: str) -> bool:
    """Return ``True`` when ``name`` belongs to the ``root`` package."""
    return name == root or name.startswith(root + ".")


def _relative_target(pkg: ModuleType, node: ast.ImportFrom) -> str:
    """Return the absolute dotted target of a ``from . import ...`` node."""
    base = pkg.__name__.split(".")
    if node.level > 1:
        base = base[: len(base) - (node.level - 1)]
    parts = base + ([node.module] if node.module else [])
    return ".".join(parts)


def _star_names(pkg: ModuleType, node: ast.ImportFrom) -> list[str]:
    """Return the public names a ``from .mod import *`` would bind."""
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
    """Collect the public names defined or re-exported by ``pkg``."""
    root = pkg.__name__.split(".")[0]
    names: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        """Record ``name`` if it is public and new."""
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
            own = node.level > 0 or (
                node.module is not None and _is_own_module(node.module, root)
            )
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
    """Build the marker-delimited ``__all__`` block for ``names``.

    The returned text starts and ends with a newline so it can be appended to
    an existing module body verbatim. ``names`` are emitted in the order given
    (one per line, trailing commas) so the block is stable across runs.

    Args:
        names: Public names to list in ``__all__``.

    Returns:
        The rendered block, including the ``autoall`` start/end markers.
    """
    lines = ["", _MARKER_START, "__all__ = ["]
    lines.extend(f"    {n!r}," for n in names)
    lines.extend(["]", _MARKER_END, ""])
    return "\n".join(lines)


def build_all_block(names: Iterable[str]) -> str:
    """Return the ``__all__`` block that :func:`ensure_all` would write.

    Public helper used by tools (for example ``pyrefactor``) that need to know
    the exact bytes of the generated block *before* touching the file on disk,
    so ``--check``/``--stdout`` can show what will be written.

    Args:
        names: Public names to list in ``__all__``.

    Returns:
        The rendered marker block (see :func:`_build_block`).
    """
    return _build_block(names)


def _collect_names_module(tree: ast.Module) -> list[str]:
    """Collect public top-level names from a single module's AST.

    Args:
        tree: Parsed module tree.

    Returns:
        Sorted, de-duplicated public names (functions, classes, top-level
        constants/assignments). Imported symbols are not included — they are
        external dependencies of the consolidated module, not its own API.
    """
    names: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        """Record ``name`` if it is public and not ``__all__`` itself."""
        if name == "__all__" or name in seen or not _public(name, False):
            return
        seen.add(name)
        names.append(name)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(node.name)
        elif isinstance(node, ast.ClassDef):
            add(node.name)
        elif isinstance(node, getattr(ast, "TypeAlias", ())):
            if isinstance(node.name, ast.Name):
                add(node.name.id)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                for sub in ast.walk(target):
                    if isinstance(sub, ast.Name):
                        add(sub.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                add(node.target.id)
    return sorted(names)


def _detect_eol(src: str) -> str:
    """Return the dominant line ending of ``src`` (``"\r\n"`` or ``"\n"``)."""
    crlf = src.count("\r\n")
    lf = src.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def ensure_all_file(
    path: str | Path,
    *,
    persist: bool = True,
) -> list[str]:
    """Scan a single ``.py`` module file and (re)generate its ``__all__`` block.

    Works like :func:`ensure_all` but on a plain module file instead of an
    importable package: the file is parsed with :mod:`ast`, public top-level
    names (functions, classes, constants) are collected, and an
    ``__all__ = [...]`` block between the autoall markers is written back to
    the same file, in place and atomically (via :func:`_atomic_write`).

    The rewrite is idempotent: when the block is already present and up to
    date the file is left untouched, and the module's own line endings
    (LF or CRLF) are preserved. Existing user ``__all__`` definitions outside
    the marker block are respected and not modified.

    Args:
        path: Path to the ``.py`` file to process.
        persist: When ``True`` (default), write the block back to the file;
            otherwise only return the computed names.

    Returns:
        The list of public names placed in ``__all__``.
    """
    p = Path(path)
    try:
        # ``newline=""`` keeps the file's own line endings so the rewrite is
        # byte-for-byte idempotent (CRLF stays CRLF).
        with open(p, encoding="utf-8", newline="") as handle:
            src = handle.read()
    except OSError as exc:
        warnings.warn(f"autoall: cannot read {p}: {exc}", stacklevel=2)
        return []
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        warnings.warn(f"autoall: cannot parse {p}: {exc}", stacklevel=2)
        return []
    # A user-written ``__all__`` outside the marker block always wins: never
    # add a second, conflicting binding.
    user_src = _strip_between(src, _MARKER_START, _MARKER_END)
    try:
        user_tree = ast.parse(user_src)
    except SyntaxError:  # pragma: no cover - src already parsed fine
        user_tree = None
    if user_tree is not None and _has_all_binding(user_tree):
        existing = _parse_all_literal(user_src)
        return list(existing) if existing is not None else []
    names = _collect_names_module(tree)
    eol = _detect_eol(src)
    block = _build_block(names).replace("\n", eol)
    existing = _find_marker_block(src, _MARKER_START, _MARKER_END)
    if existing is not None:
        before, after = src[: existing[0]], src[existing[1] :]
        new_src = (before.rstrip() + eol + block + after.lstrip("\r\n")).rstrip() + eol
    else:
        new_src = src.rstrip() + eol + block
    if persist and new_src != src:
        try:
            _atomic_write(p, new_src)
        except OSError as exc:
            warnings.warn(f"autoall: cannot persist to {p}: {exc}", stacklevel=2)
    return names


def _resolve_path(target: str | Path) -> Path:
    """Resolve a package directory, ``__init__.py`` or dotted name to its ``__init__.py``."""
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
    """Set ``__all__`` on an imported package, generating it once.

    The block is written to ``__init__.py`` between the autoall markers when the
    package does not already define ``__all__`` itself.

    Args:
        pkg_name: Dotted name of an importable package.
        persist: Write the generated block back to ``__init__.py``.
        include_submodules: Also list imported submodules.
        include_dunders: Also list dunder constants.

    Returns:
        The names placed in ``__all__``.
    """
    pkg = sys.modules.get(pkg_name) or importlib.import_module(pkg_name)
    if "__all__" in pkg.__dict__:
        return list(pkg.__all__)
    init_py = _init_path(pkg)
    if init_py is None:
        warnings.warn(
            f"autoall: {pkg_name!r} is not a file-based package", stacklevel=2
        )
        return []
    try:
        with open(init_py, encoding="utf-8", newline="") as handle:
            src = handle.read()
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
    """Append the ``ensure_all()`` snippet to a package's ``__init__.py``.

    Args:
        target: Package directory, ``__init__.py`` or dotted name.
        include_submodules: Passed to the generated call.
        include_dunders: Passed to the generated call.

    Returns:
        The patched ``__init__.py`` path.
    """
    init_py = _resolve_path(target)
    src = init_py.read_text(encoding="utf-8")
    if _INSTALL_MARKER in src:
        return init_py
    _atomic_write(
        init_py,
        src.rstrip() + "\n" + _install_block(include_submodules, include_dunders),
    )
    return init_py


def uninstall(target: str | Path) -> Path:
    """Remove the autoall injection and generated block.

    Args:
        target: Package directory, ``__init__.py`` or dotted name.

    Returns:
        The cleaned ``__init__.py`` path.
    """
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
    """Return ``True`` when the package's ``__init__.py`` carries the autoall snippet."""
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
    """Regenerate ``__all__`` for an already imported package.

    Args:
        pkg_name: Dotted name of the package.
        include_submodules: Also list imported submodules.
        include_dunders: Also list dunder constants.

    Returns:
        The regenerated ``__all__`` names.
    """
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
    """Handle ``autoall install``."""
    for t in args.targets:
        try:
            print(
                f"patched:   {install(t, include_submodules=args.submodules, include_dunders=args.dunders)}"
            )
        except FileNotFoundError as exc:
            print(f"skipped:   {exc}", file=sys.stderr)
    return 0


def _cli_uninstall(args: argparse.Namespace) -> int:
    """Handle ``autoall uninstall``."""
    for t in args.targets:
        try:
            print(f"unpatched: {uninstall(t)}")
        except FileNotFoundError as exc:
            print(f"skipped:   {exc}", file=sys.stderr)
    return 0


def _cli_regen(args: argparse.Namespace) -> int:
    """Handle ``autoall regen``."""
    for n in args.names:
        print(
            f"{n}: {regenerate(n, include_submodules=args.submodules, include_dunders=args.dunders)}"
        )
    return 0


def _cli_status(args: argparse.Namespace) -> int:
    """Handle ``autoall status``."""
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
    """Run the ``autoall`` command line interface.

    Args:
        argv: Argument list to parse (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code.
    """
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
            p.add_argument(
                "--submodules", action="store_true", help="also list bound submodules"
            )
            p.add_argument(
                "--dunders", action="store_true", help="also list dunder constants"
            )
        p.set_defaults(func=fn)
    p_regen = sub.add_parser("regen", help="force regeneration for imported packages")
    p_regen.add_argument(
        "names", nargs="+", metavar="NAME", help="dotted package names"
    )
    p_regen.add_argument(
        "--submodules", action="store_true", help="also list bound submodules"
    )
    p_regen.add_argument(
        "--dunders", action="store_true", help="also list dunder constants"
    )
    p_regen.set_defaults(func=_cli_regen)
    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
