"""Optional modernization passes applied to the consolidated tree.

These are libcst transformers (so formatting and comments are preserved) ported
from ``consolidate.py``'s ast-based transformers.  They are opt-in via the
``--modernize`` flag, which accepts a comma-separated list of passes:

* ``six``      — rewrite ``six`` imports/attributes/calls to their Python 3
                 equivalents (``six PY3``→``True``, ``six.iteritems(x)``→
                 ``x.items()``, ``six.moves.urllib``→``urllib``, ...).
* ``ospath``   — rewrite ``os.path`` calls to ``pathlib.Path`` equivalents
                 (``os.path.join(a, b)``→``Path(a).joinpath(b)``,
                 ``os.path.exists(p)``→``Path(p).exists()``,
                 ``os.path.splitext(p)[1]``→``Path(p).suffix``, ...).

The ``logging`` and ``executor`` transforms in ``consolidate.py`` were not
ported: they assume a specific runtime (``loguru``-as-``logger`` and a
``multiprocessing.Pool``/``MAX_WORKERS`` convention) that is not generally
applicable to arbitrary projects.
"""

from __future__ import annotations

from dataclasses import dataclass

import libcst as cst

from .logging_setup import logger

SIX_ATTR_MAP: dict[str, str] = {
    "text_type": "str",
    "binary_type": "bytes",
    "class_types": "type",
    "integer_types": "int",
    "string_types": "str",
}
SIX_MOVES_MAP: dict[str, str] = {
    "six.moves.urllib.request": "urllib.request",
    "six.moves.urllib.error": "urllib.error",
    "six.moves.urllib.parse": "urllib.parse",
    "six.moves.urllib.response": "urllib.response",
    "six.moves.urllib": "urllib",
    "six.moves.http_client": "http.client",
    "six.moves.cPickle": "pickle",
    "six.moves.copyreg": "copyreg",
    "six.moves.queue": "queue",
    "six.moves.range": "builtins",
    "six.moves.map": "builtins",
    "six.moves.zip": "builtins",
    "six.moves.input": "builtins",
    "six.moves": "builtins",
}
OSPATH_PROPERTY_MAP: dict[str, str] = {
    # True pathlib properties (no parentheses) — these are the only os.path
    # helpers that map to a Path attribute rather than a method call.
    "dirname": "parent",
    "basename": "name",
}
OSPATH_METHOD_MAP: dict[str, str] = {
    # No-argument methods (require parentheses). ``exists``/``isdir``/``isfile``
    # /``islink`` are methods on Path, not properties — they MUST be called.
    "exists": "exists",
    "isdir": "is_dir",
    "isfile": "is_file",
    "islink": "is_symlink",
    "abspath": "resolve",
    "realpath": "resolve",
    "expanduser": "expanduser",
    "remove": "unlink",
    "unlink": "unlink",
    "stat": "stat",
    "mkdir": "mkdir",
}
OSPATH_MAKEDIRS = "makedirs"
OSPATH_GETSIZE = "getsize"

_PASSES = ("six", "ospath")


def _dotted(node: cst.BaseExpression) -> str | None:
    if isinstance(node, cst.Name):
        return node.value
    if isinstance(node, cst.Attribute):
        left = _dotted(node.value)
        return f"{left}.{node.attr.value}" if left else None
    return None


def _name(value: str) -> cst.Name:
    return cst.Name(value=value)


def _attr(value: cst.BaseExpression, name: str) -> cst.Attribute:
    return cst.Attribute(value=value, attr=_name(name))


def _call(func: cst.BaseExpression, args: list[cst.Arg] | None = None) -> cst.Call:
    return cst.Call(func=func, args=args or [])


def _arg(value: cst.BaseExpression) -> cst.Arg:
    return cst.Arg(value=value)


def _wrap_path(arg: cst.BaseExpression) -> cst.Call:
    return _call(_name("Path"), [_arg(arg)])


def _is_os_path(node: cst.BaseExpression, bare_path: bool) -> bool:
    if isinstance(node, cst.Attribute) and isinstance(node.value, cst.Name):
        if node.value.value == "os" and node.attr.value == "path":
            return True
        if bare_path and node.value.value == "path":
            return True
    return False


def _module_bare_path(tree: cst.Module) -> bool:
    """True if the module imports ``path`` from ``os`` (``from os import path``)."""
    for stmt in tree.body:
        small = stmt.body[0] if isinstance(stmt, cst.SimpleStatementLine) else stmt
        if isinstance(small, cst.ImportFrom) and _dotted(small.module) == "os" and not small.relative:
            for alias in small.names:
                if isinstance(alias, cst.ImportAlias) and alias.name.value == "path":
                    return True
    return False


class _SixImportRewriter(cst.CSTTransformer):
    def leave_ImportFrom(self, original_node: cst.ImportFrom, updated_node: cst.ImportFrom) -> cst.BaseSmallStatement:
        module = _dotted(updated_node.module) if updated_node.module else ""
        if module in SIX_MOVES_MAP:
            mapped = SIX_MOVES_MAP[module]
            if mapped == "builtins":
                return cst.RemoveFromParent()
            return updated_node.with_changes(module=_name(mapped))
        if module == "six":
            return cst.RemoveFromParent()
        return updated_node

    def leave_Import(self, original_node: cst.Import, updated_node: cst.Import) -> cst.BaseSmallStatement:
        kept = [a for a in updated_node.names if a.name.value != "six"]
        if not kept:
            return cst.RemoveFromParent()
        if len(kept) == len(updated_node.names):
            return updated_node
        return updated_node.with_changes(names=tuple(kept))


class _SixTransformer(cst.CSTTransformer):
    def leave_Attribute(self, original_node: cst.Attribute, updated_node: cst.Attribute) -> cst.BaseExpression:
        if isinstance(updated_node.value, cst.Name) and updated_node.value.value == "six":
            attr = updated_node.attr.value
            if attr == "PY2":
                return _name("False")
            if attr == "PY3":
                return _name("True")
            if attr in SIX_ATTR_MAP:
                return _name(SIX_ATTR_MAP[attr])
        return updated_node

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.BaseExpression:
        func = updated_node.func
        if not (isinstance(func, cst.Attribute) and isinstance(func.value, cst.Name) and func.value.value == "six"):
            return updated_node
        method = func.attr.value
        args = list(updated_node.args)
        if method in ("iteritems", "iterkeys", "itervalues") and len(args) == 1:
            mapped = {"iteritems": "items", "iterkeys": "keys", "itervalues": "values"}[method]
            return _call(_attr(args[0].value, mapped))
        if method == "u" and len(args) == 1:
            return args[0].value
        if method == "string_types" and not args:
            return _name("str")
        return updated_node


class _OsPathTransformer(cst.CSTTransformer):
    def __init__(self, bare_path: bool = False) -> None:
        self.bare_path = bare_path

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.BaseExpression:
        func = updated_node.func
        if not isinstance(func, cst.Attribute) or not _is_os_path(func.value, self.bare_path):
            return updated_node
        method = func.attr.value
        args = list(updated_node.args)
        if method == "join" and len(args) >= 1:
            base = _wrap_path(args[0].value)
            if len(args) == 1:
                return base
            return _call(
                _attr(base, "joinpath"),
                [_arg(a.value) for a in args[1:]],
            )
        if method in OSPATH_PROPERTY_MAP and len(args) == 1:
            return _attr(_wrap_path(args[0].value), OSPATH_PROPERTY_MAP[method])
        if method in OSPATH_METHOD_MAP and len(args) == 1:
            return _call(_attr(_wrap_path(args[0].value), OSPATH_METHOD_MAP[method]))
        if method == OSPATH_MAKEDIRS and len(args) == 1:
            return _call(
                _attr(_wrap_path(args[0].value), "mkdir"),
                [cst.Arg(value=_name("True"), keyword=_name("parents"))],
            )
        if method == OSPATH_GETSIZE and len(args) == 1:
            stat_call = _call(_attr(_wrap_path(args[0].value), "stat"))
            return _attr(stat_call, "st_size")
        return updated_node

    def leave_Subscript(self, original_node: cst.Subscript, updated_node: cst.Subscript) -> cst.BaseExpression:
        # os.path.splitext(x)[1] -> Path(x).suffix
        slices = list(updated_node.slice)
        if len(slices) != 1:
            return updated_node
        elem = slices[0]
        index = elem.slice
        if not (isinstance(index, cst.Index) and isinstance(index.value, cst.Integer) and index.value.value == "1"):
            return updated_node
        call = updated_node.value
        if not (isinstance(call, cst.Call) and len(call.args) == 1):
            return updated_node
        if not (
            isinstance(call.func, cst.Attribute)
            and call.func.attr.value == "splitext"
            and _is_os_path(call.func.value, self.bare_path)
        ):
            return updated_node
        return _attr(_wrap_path(call.args[0].value), "suffix")


@dataclass
class ModernizeReport:
    applied: tuple[str, ...]
    changed: bool


def modernize_tree(tree: cst.Module, passes: str | list[str] | tuple[str, ...]) -> tuple[cst.Module, ModernizeReport]:
    """Apply the requested modernization *passes* to *tree* and return the new tree.

    Unknown passes raise ``ValueError``.
    """
    if isinstance(passes, str):
        wanted = [p.strip() for p in passes.split(",") if p.strip()]
    else:
        wanted = list(passes)
    bad = [p for p in wanted if p not in _PASSES]
    if bad:
        raise ValueError(f"unknown modernize pass(es): {bad!r}; valid: {list(_PASSES)}")
    if not wanted:
        return tree, ModernizeReport(applied=(), changed=False)

    before = tree.code
    current = tree
    if "ospath" in wanted:
        bare = _module_bare_path(current)
        current = current.visit(_OsPathTransformer(bare_path=bare))
    if "six" in wanted:
        current = current.visit(_SixImportRewriter())
        current = current.visit(_SixTransformer())
    changed = current.code != before
    if changed:
        logger.info("Applied modernization pass(es): {}", ", ".join(wanted))
    return current, ModernizeReport(applied=tuple(wanted), changed=changed)
