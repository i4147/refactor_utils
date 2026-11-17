"""Tree-sitter based module parser producing blocks and occurrences.

Fixes compared to the previous revision:
* CRLF line endings are actually normalised (previously only warned about),
  which kept stray ``\\r`` characters out of block sources.
* Docstrings are stripped on the whole module BEFORE occurrence collection.
  The old per-block stripping deleted lines without remapping ``Occ`` byte
  offsets, which corrupted every rewrite when ``--no-keep-docstrings`` was
  used, and it only removed module-level docstrings, never the ones inside
  functions/classes.
* Comment stripping is token-based, so a ``#`` that begins a line inside a
  multi-line string literal is no longer removed (which used to produce
  broken string literals).
* The ``__name__`` semantic warning no longer fires for every module that
  merely has an ``if __name__ == "__main__":`` guard.
"""
from __future__ import annotations

import io
import re
import threading
import tokenize
from pathlib import Path

import tree_sitter_python as _tspy
from tree_sitter import Language, Node, Parser

from .models import (
    CLASS,
    CONST,
    FUNC,
    MAIN,
    MISC,
    Block,
    GuardBlock,
    ImportSpec,
    ModuleInfo,
    Occ,
)

_LOCAL = threading.local()
_MAIN_RE = re.compile(
    r"""^\s*(?:__name__\s*==\s*(['"])__main__\1|(['"])__main__\2\s*==\s*__name__)\s*$"""
)
_TC_NAMES = {
    "TYPE_CHECKING",
    "typing.TYPE_CHECKING",
    "t.TYPE_CHECKING",
    "typing_extensions.TYPE_CHECKING",
}
_COMPOUND = {
    "if_statement",
    "try_statement",
    "with_statement",
    "for_statement",
    "while_statement",
    "match_statement",
}
_IMPORT_TYPES = {"import_statement", "import_from_statement", "future_import_statement"}
_DEF_TYPES = {"function_definition", "class_definition", "decorated_definition"}
_STR_RE = re.compile(r"""['"]([A-Za-z_][A-Za-z0-9_]*)['"]""")
_ALL_METHODS = {
    "append",
    "extend",
    "insert",
    "remove",
    "pop",
    "clear",
    "sort",
    "reverse",
}
_DYNAMIC_PATTERNS = {
    "importlib": re.compile(r"\bimportlib\b"),
    "__import__": re.compile(r"\b__import__\b"),
    "globals": re.compile(r"\bglobals\s*\("),
    "locals": re.compile(r"\blocals\s*\("),
    "eval": re.compile(r"\beval\s*\("),
    "exec": re.compile(r"\bexec\s*\("),
    "getattr": re.compile(r"\bgetattr\s*\("),
    "__getattr__": re.compile(r"__getattr__"),
    "__dir__": re.compile(r"__dir__"),
    "monkeypatch": re.compile(r"monkeypatch"),
}


def _make_parser() -> Parser:
    lang = Language(_tspy.language())
    try:
        return Parser(lang)  # tree_sitter >= 0.22
    except TypeError:  # pragma: no cover - tree_sitter 0.21
        parser = Parser()
        parser.set_language(lang)
        return parser


def _get_parser() -> Parser:
    parser = getattr(_LOCAL, "parser", None)
    if parser is None:
        parser = _make_parser()
        _LOCAL.parser = parser
    return parser


def _t(node: Node | None) -> str:
    return node.text.decode("utf-8", "replace") if node is not None else ""


def _resolve_relative(current: str, is_pkg: bool, level: int, rest: str) -> str:
    parts = current.split(".") if is_pkg else current.split(".")[:-1]
    strip = level - 1
    base = [] if strip > len(parts) else parts[: len(parts) - strip]
    return ".".join(base + ([rest] if rest else []))


def parse_import(
    node: Node, module: str, is_pkg: bool, type_checking: bool = False
) -> list[ImportSpec]:
    specs: list[ImportSpec] = []
    if node.type == "import_statement":
        for ch in node.children_by_field_name("name"):
            if ch.type == "aliased_import":
                specs.append(
                    ImportSpec(
                        _t(ch.child_by_field_name("name")),
                        alias=_t(ch.child_by_field_name("alias")),
                        type_checking=type_checking,
                    )
                )
            else:
                specs.append(ImportSpec(_t(ch), type_checking=type_checking))
        return specs
    relative = False
    mod_node = node.child_by_field_name("module_name")
    if node.type == "future_import_statement":
        mod = "__future__"
    elif mod_node is not None and mod_node.type == "relative_import":
        prefix = next((c for c in mod_node.children if c.type == "import_prefix"), None)
        level = len(_t(prefix))
        rest = next(
            (_t(c) for c in mod_node.named_children if c.type == "dotted_name"), ""
        )
        mod = _resolve_relative(module, is_pkg, level, rest)
        relative = True
    else:
        mod = _t(mod_node)
    if any(c.type == "wildcard_import" for c in node.children):
        return [
            ImportSpec(
                mod,
                is_from=True,
                star=True,
                relative=relative,
                type_checking=type_checking,
            )
        ]
    for ch in node.children_by_field_name("name"):
        if ch.type == "aliased_import":
            name = _t(ch.child_by_field_name("name"))
            alias = _t(ch.child_by_field_name("alias"))
        else:
            name, alias = _t(ch), None
        specs.append(
            ImportSpec(
                mod,
                name=name,
                alias=alias,
                is_from=True,
                relative=relative,
                type_checking=type_checking,
            )
        )
    return specs


def _dotted(node: Node | None) -> str | None:
    if node is None:
        return None
    if node.type == "identifier":
        return _t(node)
    if node.type == "attribute":
        left = _dotted(node.child_by_field_name("object"))
        return f"{left}.{_t(node.child_by_field_name('attribute'))}" if left else None
    return None


def _binding_names(node: Node | None) -> set[str]:
    if node is None:
        return set()
    t = node.type
    if t == "identifier":
        return {_t(node)}
    if t in ("default_parameter", "typed_default_parameter"):
        return _binding_names(node.child_by_field_name("name"))
    if t in (
        "typed_parameter",
        "list_splat_pattern",
        "dictionary_splat_pattern",
        "tuple_pattern",
        "list_pattern",
    ):
        out: set[str] = set()
        for ch in node.named_children:
            if ch.type != "type":
                out |= _binding_names(ch)
        return out
    return set()


def _param_names(params: Node | None) -> set[str]:
    names: set[str] = set()
    if params is not None:
        for ch in params.named_children:
            names |= _binding_names(ch)
    return names


def _push_param_exprs(stack: list, params: Node | None, lazy: bool, shadow: frozenset) -> None:
    if params is None:
        return
    for ch in params.named_children:
        if ch.type in (
            "default_parameter",
            "typed_default_parameter",
            "typed_parameter",
        ):
            for field_name in ("type", "value"):
                sub = ch.child_by_field_name(field_name)
                if sub is not None:
                    stack.append((sub, lazy, shadow, False))


def _lhs_names(node: Node | None) -> list[str] | None:
    if node is None:
        return None
    t = node.type
    if t == "identifier":
        return [_t(node)]
    if t in ("pattern_list", "tuple_pattern", "list_pattern"):
        out: list[str] = []
        for ch in node.named_children:
            got = _lhs_names(ch)
            if got is None:
                return None
            out += got
        return out
    if t == "list_splat_pattern":
        return _lhs_names(node.named_children[0]) if node.named_children else None
    return None


def _target_names(assign: Node) -> list[str] | None:
    names: list[str] = []
    cur: Node | None = assign
    while cur is not None and cur.type in ("assignment", "augmented_assignment"):
        got = _lhs_names(cur.child_by_field_name("left"))
        if got is None:
            return None
        names += got
        cur = cur.child_by_field_name("right")
    return names


def _first_identifier(node: Node) -> str:
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.type == "identifier":
            return _t(cur)
        stack.extend(reversed(cur.named_children))
    return ""


def _bound_names(node: Node) -> list[str]:
    names: list[str] = []
    stack = [node]
    while stack:
        cur = stack.pop()
        for ch in cur.named_children:
            t = ch.type
            if t in ("function_definition", "class_definition"):
                names.append(_t(ch.child_by_field_name("name")))
            elif t == "decorated_definition":
                names.append(
                    _t(ch.child_by_field_name("definition").child_by_field_name("name"))
                )
            elif t == "expression_statement" and ch.named_children:
                e = ch.named_children[0]
                if e.type in ("assignment", "augmented_assignment"):
                    names += _target_names(e) or []
            elif t in _COMPOUND or t in (
                "elif_clause",
                "else_clause",
                "except_clause",
                "finally_clause",
                "block",
                "case_clause",
            ):
                stack.append(ch)
    return list(dict.fromkeys(names))


def _scan(node: Node) -> tuple[list[Node], bool]:
    imports: list[Node] = []
    has_def = False
    stack = [node]
    while stack:
        cur = stack.pop()
        for ch in cur.named_children:
            if ch.type in _IMPORT_TYPES:
                imports.append(ch)
            elif ch.type in _DEF_TYPES:
                has_def = True
            else:
                stack.append(ch)
    return imports, has_def


def _extent(node: Node) -> tuple[int, int]:
    """Byte extent of *node* plus any comments attached above / beside it."""
    start, end = node.start_byte, node.end_byte
    row = node.start_point[0]
    prev = node.prev_sibling
    while prev is not None and prev.type == "comment" and prev.end_point[0] + 1 == row:
        before = prev.prev_sibling
        if before is not None and before.end_point[0] == prev.start_point[0]:
            break
        start, row = prev.start_byte, prev.start_point[0]
        prev = prev.prev_sibling
    nxt = node.next_sibling
    if (
        nxt is not None
        and nxt.type == "comment"
        and nxt.start_point[0] == node.end_point[0]
    ):
        end = nxt.end_byte
    return start, end


def _dedent_block(raw: bytes, cut: int) -> tuple[bytes, callable]:
    if cut <= 0:
        return raw, (lambda off: off)
    lines = raw.split(b"\n")
    stripped_lines = []
    removed = []
    for line in lines:
        strip = 0
        while strip < cut and line[strip : strip + 1] == b" ":
            strip += 1
        removed.append(strip)
        stripped_lines.append(line[strip:])
    starts_old = []
    starts_new = []
    old_pos = new_pos = 0
    for line, strip_, kept in zip(lines, removed, stripped_lines):
        starts_old.append(old_pos)
        starts_new.append(new_pos)
        old_pos += len(line) + 1
        new_pos += len(kept) + 1

    def remap(offset: int) -> int:
        lo, hi = 0, len(starts_old) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if starts_old[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        rel = offset - starts_old[lo]
        return starts_new[lo] + max(0, rel - removed[lo])

    return b"\n".join(stripped_lines), remap


def _first_error_line(root: Node) -> int:
    stack = [root]
    while stack:
        cur = stack.pop()
        if cur.type == "ERROR" or cur.is_missing:
            return cur.start_point[0] + 1
        stack.extend(reversed([c for c in cur.children if c.has_error or c.is_missing]))
    return 0


def _detect_dynamic_patterns(text: str, info: ModuleInfo) -> None:
    for name, pat in _DYNAMIC_PATTERNS.items():
        if pat.search(text):
            if name in ("importlib", "__import__"):
                info.warnings.append(
                    f"{info.module}: uses {name} for dynamic imports — consolidation may miss dependencies"
                )
            elif name in ("globals", "locals", "eval", "exec", "getattr"):
                info.warnings.append(
                    f"{info.module}: uses {name}() — name resolution may be incomplete"
                )
            elif name in ("__getattr__", "__dir__", "monkeypatch"):
                info.warnings.append(
                    f"{info.module}: defines/uses {name} — module-level attribute access may change semantics"
                )


def _detect_runtime_semantics(text: str, info: ModuleInfo) -> None:
    """Warn about dunder usage that changes meaning in a consolidated file.

    *text* should exclude ``__main__`` guards — every module that has one
    legitimately references ``__name__`` there.
    """
    for sym in ("__file__", "__name__", "__package__", "__path__"):
        if sym in text:
            if sym != "__file__":
                info.warnings.append(
                    f"{info.module}: uses {sym} — semantics change in consolidated file "
                    "(it now points at the consolidated module)"
                )
    if (
        "importlib.resources" in text
        or "pkg_resources" in text
        or ("open(" in text and "__file__" in text)
    ):
        info.warnings.append(
            f"{info.module}: does resource loading relative to __file__ — verify output "
            "handles bundled resources (see --embed-data)"
        )


def _strip_comments_from_source(src: bytes) -> bytes:
    """Remove comment lines / trailing comments using ``tokenize``.

    Token-based removal never touches ``#`` characters that live inside
    string literals; if tokenization fails we fall back to returning the
    source untouched rather than risk corrupting it.
    """
    text = src.decode("utf-8", "replace")
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        return src
    comment_cols: dict[int, tuple[int, int]] = {}
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            comment_cols[tok.start[0]] = (tok.start[1], tok.end[1])
    if not comment_cols:
        return src
    out: list[str] = []
    for i, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if i == 1 and stripped.startswith("#!"):
            out.append(line)
            continue
        if i <= 2 and "coding" in stripped and stripped.startswith("#"):
            out.append(line)
            continue
        if i in comment_cols:
            c1, c2 = comment_cols[i]
            line = line[:c1] + line[c2:]
            if not line.strip():
                continue
        out.append(line)
    result = "\n".join(out) + "\n"
    return result.encode("utf-8")


def _docstring_ranges(root: Node, src: bytes) -> list[tuple[int, int]]:
    """Byte ranges of module/class/function docstrings in *src*."""
    ranges: list[tuple[int, int]] = []

    def visit_body(parent: Node) -> None:
        children = [
            c for c in parent.named_children if c.type not in ("comment",)
        ]
        if not children:
            return
        first = children[0]
        if first.type != "expression_statement" or not first.named_children:
            return
        expr = first.named_children[0]
        if expr.type in ("string", "concatenated_string"):
            # Check there is no interpolation / no f-string prefix.
            pure = all(
                c.type in ("string_start", "string_content", "string_end")
                for c in expr.named_children
            ) if expr.named_children else True
            if pure:
                ranges.append((first.start_byte, first.end_byte))

    visit_body(root)
    stack = [root]
    while stack:
        cur = stack.pop()
        for ch in cur.named_children:
            if ch.type == "block":
                visit_body(ch)
            stack.append(ch)
    return ranges


def _strip_docstrings_modulewide(root: Node, src: bytes) -> bytes:
    """Delete every docstring from *src*; byte offsets stay consistent.

    The deletions are applied to the whole module source and the module is
    re-parsed afterwards, so occurrence offsets collected later always match
    the final block sources.
    """
    ranges = _docstring_ranges(root, src)
    if not ranges:
        return src
    # Cut from the end so earlier offsets remain valid.
    ranges.sort(reverse=True)
    for start, end in ranges:
        # Extend to swallow the indentation before and the newline after.
        line_start = src.rfind(b"\n", 0, start) + 1
        if not src[line_start:start].strip():
            start = line_start
        if src[end : end + 1] == b"\n":
            end += 1
        src = src[:start] + src[end:]
    return src


def _collect_occs(
    root: Node, base: int, src: bytes, own_def: Node | None, info: ModuleInfo
) -> list[Occ]:
    occs: list[Occ] = []
    own_id = own_def.id if own_def is not None else None
    future = info.future_annotations
    warned_file = False
    stack: list[tuple[Node, bool, frozenset, bool]] = [
        (root, False, frozenset(), False)
    ]
    while stack:
        node, lazy, shadow, cls = stack.pop()
        t = node.type
        if t == "identifier":
            name = _t(node)
            if name in shadow:
                continue
            if name == "__file__" and not warned_file:
                info.warnings.append(
                    f"{info.module}: uses __file__, which now points at the consolidated file"
                )
                warned_file = True
            occs.append(
                Occ(node.start_byte - base, node.end_byte - base, name, "name", not lazy)
            )
            continue
        if t == "attribute":
            obj = node.child_by_field_name("object")
            chain = _dotted(obj)
            if chain is not None and chain.split(".")[0] not in shadow:
                occs.append(
                    Occ(
                        node.start_byte - base,
                        node.end_byte - base,
                        _t(node.child_by_field_name("attribute")),
                        "attr",
                        not lazy,
                        chain,
                    )
                )
            if obj is not None:
                stack.append((obj, lazy, shadow, False))
            continue
        if t == "keyword_argument":
            value = node.child_by_field_name("value")
            if value is not None:
                stack.append((value, lazy, shadow, False))
            continue
        if t in _IMPORT_TYPES:
            specs = tuple(parse_import(node, info.module, info.is_pkg))
            ls = src.rfind(b"\n", 0, node.start_byte) + 1
            indent = src[ls : node.start_byte].decode("utf-8", "replace")
            if indent.strip():
                indent = ""
            occs.append(
                Occ(
                    node.start_byte - base,
                    node.end_byte - base,
                    "",
                    "nested_import",
                    not lazy,
                    indent,
                    specs,
                )
            )
            continue
        if t == "function_definition":
            name_node = node.child_by_field_name("name")
            if name_node is not None and node.id == own_id:
                stack.append((name_node, lazy, shadow, False))
            params = node.child_by_field_name("parameters")
            inner = shadow | _param_names(params)
            _push_param_exprs(stack, params, lazy, shadow)
            for fld in ("return_type", "type_parameters"):
                sub = node.child_by_field_name(fld)
                if sub is not None:
                    stack.append((sub, lazy, shadow, False))
            body = node.child_by_field_name("body")
            if body is not None:
                stack.append((body, True, inner, False))
            continue
        if t == "lambda":
            params = node.child_by_field_name("parameters")
            inner = shadow | _param_names(params)
            _push_param_exprs(stack, params, lazy, shadow)
            body = node.child_by_field_name("body")
            if body is not None:
                stack.append((body, True, inner, False))
            continue
        if t == "class_definition":
            name_node = node.child_by_field_name("name")
            if name_node is not None and node.id == own_id:
                stack.append((name_node, lazy, shadow, False))
            for fld in ("superclasses", "type_parameters"):
                sub = node.child_by_field_name(fld)
                if sub is not None:
                    stack.append((sub, lazy, shadow, False))
            body = node.child_by_field_name("body")
            if body is not None:
                stack.append((body, lazy, shadow, True))
            continue
        if cls and t == "block":
            for ch in node.named_children:
                if ch.type == "expression_statement" and ch.named_children:
                    inner_stmt = ch.named_children[0]
                    if inner_stmt.type in ("assignment", "augmented_assignment"):
                        # Only the annotated type and the RHS are references;
                        # the attribute name itself must NOT be rewritten or
                        # instance attribute access (``self.x``) would break.
                        for fld in ("type", "right"):
                            sub = inner_stmt.child_by_field_name(fld)
                            if sub is not None:
                                stack.append((sub, lazy, shadow, False))
                        continue
                stack.append((ch, lazy, shadow, False))
            continue
        child_lazy = True if (t == "type" and future) else lazy
        for ch in node.named_children:
            stack.append((ch, child_lazy, shadow, False))
    return occs


def _collect_all_stmt(node: Node, info: ModuleInfo) -> bool:
    if node.type != "expression_statement" or not node.named_children:
        return False
    e = node.named_children[0]
    if e.type in ("assignment", "augmented_assignment"):
        names = _target_names(e)
        if names == ["__all__"]:
            info.declared_all = (info.declared_all or []) + _STR_RE.findall(
                _t(e.child_by_field_name("right"))
            )
            return True
        return False
    if e.type == "call":
        fn = e.child_by_field_name("function")
        if fn is not None and fn.type == "attribute":
            if _dotted(fn.child_by_field_name("object")) == "__all__":
                method = _t(fn.child_by_field_name("attribute"))
                if method in _ALL_METHODS:
                    args = e.child_by_field_name("arguments")
                    for arg in args.named_children if args is not None else ():
                        if arg.type == "argument_list":
                            continue
                        info.declared_all = (info.declared_all or []) + _STR_RE.findall(
                            _t(arg)
                        )
                    info.warnings.append(
                        f"{info.module}: mutated __all__ ('{method}') — static analysis "
                        "may be incomplete; exported names are recomputed from definitions"
                    )
                    return True
    return False


def _is_bare_string(node: Node | None) -> bool:
    if node is None or node.type not in ("string", "concatenated_string"):
        return False
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.type == "interpolation":
            return False
        stack.extend(cur.children)
    return True


def _extract(info: ModuleInfo, root: Node, src: bytes) -> None:
    info.future_annotations = any(
        n.type == "future_import_statement" and b"annotations" in n.text
        for n in root.named_children
    )
    by_name: dict[str, Block] = {}
    main_ranges: list[tuple[int, int]] = []

    def add(
        kind: str,
        names: list[str],
        node: Node,
        own: Node | None = None,
        dedent: bool = False,
    ) -> None:
        start, end = _extent(node)
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        raw_source = src[start:end]
        occs = _collect_occs(node, start, src, own, info)
        if dedent:
            cut = node.start_point[1]
            raw_source, remap = _dedent_block(raw_source, cut)
            for occ in occs:
                occ.start = remap(occ.start)
                occ.end = remap(occ.end)
        block = Block(info.module, kind, list(names), raw_source, occs, len(info.blocks))
        block.orig_path = info.path
        block.start_line = start_line
        block.end_line = end_line
        target = next((by_name[n] for n in names if n in by_name), None)
        if target is not None:
            target.merge(block)
            for n in names:
                by_name.setdefault(n, target)
            return
        info.blocks.append(block)
        for n in names:
            by_name[n] = block

    def add_type_checking(node: Node) -> None:
        body = node.child_by_field_name("consequence")
        if body is None:
            return
        for ch in body.named_children:
            if ch.type == "comment":
                continue
            if ch.type in _IMPORT_TYPES:
                info.imports.extend(
                    parse_import(ch, info.module, info.is_pkg, type_checking=True)
                )
            elif ch.type in _DEF_TYPES:
                d = (
                    ch.child_by_field_name("definition")
                    if ch.type == "decorated_definition"
                    else ch
                )
                kind = CLASS if d.type == "class_definition" else FUNC
                add(kind, [_t(d.child_by_field_name("name"))], ch, own=d, dedent=True)
            elif ch.type == "expression_statement":
                if _collect_all_stmt(ch, info):
                    continue
                e = ch.named_children[0] if ch.named_children else None
                if _is_bare_string(e):
                    continue
                if e is not None and e.type in ("assignment", "augmented_assignment"):
                    names = _target_names(e)
                    if names:
                        add(CONST, names, ch, dedent=True)
                        continue
                add(MISC, [], ch, dedent=True)
            elif ch.type == "pass_statement":
                continue
            else:
                add(MISC, _bound_names(ch), ch, dedent=True)

    for n in root.named_children:
        t = n.type
        if t == "comment":
            continue
        if t in _IMPORT_TYPES:
            info.imports.extend(parse_import(n, info.module, info.is_pkg))
        elif t in _DEF_TYPES:
            d = (
                n.child_by_field_name("definition")
                if t == "decorated_definition"
                else n
            )
            kind = CLASS if d.type == "class_definition" else FUNC
            add(kind, [_t(d.child_by_field_name("name"))], n, own=d)
        elif t == "expression_statement":
            e = n.named_children[0] if n.named_children else None
            if e is None or _is_bare_string(e):
                continue
            if _collect_all_stmt(n, info):
                continue
            if e.type in ("assignment", "augmented_assignment"):
                names = _target_names(e)
                if names:
                    add(CONST, names, n)
                    continue
            add(MISC, [], n)
        elif t == "type_alias_statement":
            add(CONST, [_first_identifier(n)], n)
        elif t in _COMPOUND:
            if t == "if_statement":
                cond = _t(n.child_by_field_name("condition")).strip()
                if _MAIN_RE.match(cond):
                    add(MAIN, [], n)
                    main_ranges.append((n.start_byte, n.end_byte))
                    continue
                if cond in _TC_NAMES:
                    add_type_checking(n)
                    continue
                if (
                    "sys.platform" in cond
                    or "sys.version" in cond
                    or "TYPE_CHECKING" in cond
                ):
                    info.warnings.append(
                        f"{info.module}: conditional import guarded by '{cond.strip()}' kept verbatim"
                    )
            imports, has_def = _scan(n)
            is_try_guard = (
                imports
                and not has_def
                and t
                in (
                    "if_statement",
                    "try_statement",
                )
            )
            if is_try_guard:
                start, end = _extent(n)
                specs = [
                    s
                    for imp in imports
                    for s in parse_import(imp, info.module, info.is_pkg)
                ]
                info.guards.append(
                    GuardBlock(
                        src[start:end].decode("utf-8", "replace"),
                        specs,
                        _bound_names(n),
                        conditional=True,
                    )
                )
            else:
                info.warnings.append(
                    f"{info.module}: module-level {t.replace('_statement', '')} "
                    f"block combines imports and definitions — emitted verbatim, "
                    "names bound there are not rewritten"
                )
                add(MISC, _bound_names(n), n)
        elif t == "pass_statement":
            continue
        else:
            add(MISC, [], n)
    full_text = src.decode("utf-8", "replace")
    _detect_dynamic_patterns(full_text, info)
    # Remove main-guard regions before scanning for __name__ etc. so a
    # plain `if __name__ == "__main__":` guard does not trigger a warning.
    scanned = full_text
    if main_ranges:
        pieces = []
        pos = 0
        for s, e in sorted(main_ranges):
            pieces.append(scanned[pos:s])
            pos = e
        pieces.append(scanned[pos:])
        scanned = "".join(pieces)
    _detect_runtime_semantics(scanned, info)


def parse_module_file(
    path_str: str,
    module: str,
    is_pkg: bool,
    keep_docstrings: bool = True,
    keep_comments: bool = True,
) -> ModuleInfo:
    info = ModuleInfo(module=module, path=path_str, is_pkg=is_pkg)
    try:
        src = Path(path_str).read_bytes()
    except OSError as exc:
        info.errors.append(f"cannot read {path_str}: {exc}")
        return info
    if src.startswith(b"\xef\xbb\xbf"):
        src = src[3:]
    if b"\r\n" in src:
        info.warnings.append(
            f"{path_str}: contains CRLF line endings — normalized to LF in output"
        )
        src = src.replace(b"\r\n", b"\n")
    try:
        src.decode("utf-8")
    except UnicodeDecodeError:
        info.warnings.append(
            f"{path_str}: non-UTF8 encoding detected — decoded with replacement characters"
        )
    try:
        if not keep_comments:
            src = _strip_comments_from_source(src)
        root = _get_parser().parse(src).root_node
        if root.has_error:
            err_line = _first_error_line(root)
            info.errors.append(f"{path_str}: syntax error near line {err_line}")
            return info
        if not keep_docstrings:
            src = _strip_docstrings_modulewide(root, src)
            root = _get_parser().parse(src).root_node
            if root.has_error:
                err_line = _first_error_line(root)
                info.errors.append(
                    f"{path_str}: syntax error near line {err_line} (after docstring removal)"
                )
                return info
            for b in info.blocks:
                b.stripped_docstring = True
        _extract(info, root, src)
    except Exception as exc:
        info.errors.append(f"{path_str}: extraction failed: {exc!r}")
    return info
