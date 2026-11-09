"""tree-sitter based extraction of top-level blocks (runs inside worker processes)."""

from __future__ import annotations
import ast
import re
import threading
from pathlib import Path
import tree_sitter_python as _tspy
from tree_sitter import Language, Node, Parser
from .models import CLASS, CONST, FUNC, MAIN, MISC, Block, GuardBlock, ImportSpec, ModuleInfo, Occ

_LOCAL = threading.local()
_MAIN_RE = re.compile(r"""^\s*(?:__name__\s*==\s*(['"])__main__\1|(['"])__main__\2\s*==\s*__name__)\s*$""")
_TC_NAMES = {"TYPE_CHECKING", "typing.TYPE_CHECKING", "t.TYPE_CHECKING", "typing_extensions.TYPE_CHECKING"}
_COMPOUND = {"if_statement", "try_statement", "with_statement", "for_statement", "while_statement", "match_statement"}
_IMPORT_TYPES = {"import_statement", "import_from_statement", "future_import_statement"}
_DEF_TYPES = {"function_definition", "class_definition", "decorated_definition"}
_STR_RE = re.compile(r"""['"]([A-Za-z_][A-Za-z0-9_]*)['"]""")
# Dynamic & metaprogramming detection patterns
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


def _get_parser() -> Parser:
    parser = getattr(_LOCAL, "parser", None)
    if parser is None:
        parser = Parser(Language(_tspy.language()))
        _LOCAL.parser = parser
    return parser


def _t(node: Node | None) -> str:
    return node.text.decode("utf-8", "replace") if node is not None else ""


def _resolve_relative(current: str, is_pkg: bool, level: int, rest: str) -> str:
    parts = current.split(".") if is_pkg else current.split(".")[:-1]
    strip = level - 1
    base = [] if strip > len(parts) else parts[: len(parts) - strip]
    return ".".join(base + ([rest] if rest else []))


def parse_import(node: Node, module: str, is_pkg: bool) -> list[ImportSpec]:
    specs: list[ImportSpec] = []
    if node.type == "import_statement":
        for ch in node.children_by_field_name("name"):
            if ch.type == "aliased_import":
                specs.append(ImportSpec(_t(ch.child_by_field_name("name")), alias=_t(ch.child_by_field_name("alias"))))
            else:
                specs.append(ImportSpec(_t(ch)))
        return specs
    relative = False
    mod_node = node.child_by_field_name("module_name")
    if node.type == "future_import_statement":
        mod = "__future__"
    elif mod_node is not None and mod_node.type == "relative_import":
        prefix = next((c for c in mod_node.children if c.type == "import_prefix"), None)
        level = len(_t(prefix))
        rest = next((_t(c) for c in mod_node.named_children if c.type == "dotted_name"), "")
        mod = _resolve_relative(module, is_pkg, level, rest)
        relative = True
    else:
        mod = _t(mod_node)
    if any(c.type == "wildcard_import" for c in node.children):
        return [ImportSpec(mod, is_from=True, star=True, relative=relative)]
    for ch in node.children_by_field_name("name"):
        if ch.type == "aliased_import":
            name = _t(ch.child_by_field_name("name"))
            alias = _t(ch.child_by_field_name("alias"))
        else:
            name, alias = _t(ch), None
        specs.append(ImportSpec(mod, name=name, alias=alias, is_from=True, relative=relative))
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
    if t in ("typed_parameter", "list_splat_pattern", "dictionary_splat_pattern", "tuple_pattern", "list_pattern"):
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


def _push_param_exprs(stack: list, params: Node | None, lazy: bool, shadow: frozenset[str]) -> None:
    if params is None:
        return
    for ch in params.named_children:
        if ch.type in ("default_parameter", "typed_default_parameter", "typed_parameter"):
            for field in ("type", "value"):
                sub = ch.child_by_field_name(field)
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
                names.append(_t(ch.child_by_field_name("definition").child_by_field_name("name")))
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
    if nxt is not None and nxt.type == "comment" and nxt.start_point[0] == node.end_point[0]:
        end = nxt.end_byte
    return start, end


def _first_error_line(root: Node) -> int:
    stack = [root]
    while stack:
        cur = stack.pop()
        if cur.type == "ERROR" or cur.is_missing:
            return cur.start_point[0] + 1
        stack.extend(reversed([c for c in cur.children if c.has_error or c.is_missing]))
    return 0


def _detect_dynamic_patterns(text: str, info: ModuleInfo) -> None:
    """Emit warnings for dynamic/metaprogramming constructs."""
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
    # __file__, __name__, __package__, __path__, resource loading
    for sym in ("__file__", "__name__", "__package__", "__path__"):
        if sym in text:
            # __file__ already warned in _collect_occs, add broader warning
            if sym != "__file__":
                info.warnings.append(
                    f"{info.module}: uses {sym} — semantics change in consolidated file (now points at _consolidated.py)"
                )
    if "importlib.resources" in text or "pkg_resources" in text or "open(" in text and "__file__" in text:
        info.warnings.append(
            f"{info.module}: does resource loading relative to __file__ — verify output handles bundled resources"
        )


def _strip_docstring_from_source(src: bytes) -> bytes:
    """Remove docstrings from a block's source using AST."""
    try:
        text = src.decode("utf-8", "replace")
        tree = ast.parse(text)
        # Find docstring nodes and remove them via line intervals
        lines = text.splitlines()
        # Simple approach: if first statement is Expr(Str), remove it
        if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, (ast.Constant, ast.Str)):
            # docstring found
            node = tree.body[0]
            start = (node.lineno or 1) - 1
            end = (node.end_lineno or start + 1)
            del lines[start:end]
            return "\n".join(lines).encode("utf-8")
        return src
    except Exception:
        return src


def _strip_comments_from_source(src: bytes) -> bytes:
    """Strip # comments preserving shebang, encoding cookies, and strings."""
    try:
        text = src.decode("utf-8", "replace")
        lines = text.splitlines()
        out = []
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            # Keep shebang and encoding cookie even when stripping
            if i == 0 and stripped.startswith("#!"):
                out.append(line)
                continue
            if i <= 1 and "coding" in stripped and stripped.startswith("#"):
                out.append(line)
                continue
            # If line is pure comment, drop it
            if stripped.startswith("#"):
                continue
            # Inline comments: naive but safe - keep them unless --strip-comments aggressive
            # We preserve inline comments to avoid breaking # type: ignore etc.
            out.append(line)
        return "\n".join(out).encode("utf-8")
    except Exception:
        return src


def _collect_occs(root: Node, base: int, src: bytes, own_def: Node | None, info: ModuleInfo) -> list[Occ]:
    occs: list[Occ] = []
    own_id = own_def.id if own_def is not None else None
    future = info.future_annotations
    warned_file = False
    stack: list[tuple[Node, bool, frozenset[str], bool]] = [(root, False, frozenset(), False)]
    while stack:
        node, lazy, shadow, cls = stack.pop()
        t = node.type
        if t == "identifier":
            name = _t(node)
            if name in shadow:
                continue
            if name == "__file__" and not warned_file:
                info.warnings.append(f"{info.module}: uses __file__, which now points at the consolidated file")
                warned_file = True
            # Additional runtime semantics warnings handled via _detect_runtime_semantics on full text
            occs.append(Occ(node.start_byte - base, node.end_byte - base, name, "name", not lazy))
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
            occs.append(Occ(node.start_byte - base, node.end_byte - base, "", "nested_import", not lazy, indent, specs))
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
                        left = inner_stmt.child_by_field_name("left")
                        for fld in ("type", "right"):
                            sub = inner_stmt.child_by_field_name(fld)
                            if sub is not None:
                                stack.append((sub, lazy, shadow, False))
                        if left is not None and left.type != "identifier":
                            stack.append((left, lazy, shadow, False))
                        continue
                stack.append((ch, lazy, shadow, False))
            continue
        child_lazy = True if (t == "type" and future) else lazy
        for ch in node.named_children:
            stack.append((ch, child_lazy, shadow, False))
    return occs


def _extract(info: ModuleInfo, root: Node, src: bytes) -> None:
    info.future_annotations = any(
        n.type == "future_import_statement" and b"annotations" in n.text for n in root.named_children
    )
    by_name: dict[str, Block] = {}

    def add(kind: str, names: list[str], node: Node, own: Node | None = None) -> None:
        start, end = _extent(node)
        # Capture original location for source_map
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        raw_source = src[start:end]
        # Detect dynamic patterns per block but also aggregate at module level via full src later
        occs = _collect_occs(node, start, src, own, info)
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

    first = True
    for n in root.named_children:
        t = n.type
        if t == "comment":
            continue
        is_first, first = first, False
        if t in _IMPORT_TYPES:
            info.imports.extend(parse_import(n, info.module, info.is_pkg))
        elif t in _DEF_TYPES:
            d = n.child_by_field_name("definition") if t == "decorated_definition" else n
            kind = CLASS if d.type == "class_definition" else FUNC
            add(kind, [_t(d.child_by_field_name("name"))], n, own=d)
        elif t == "expression_statement":
            e = n.named_children[0] if n.named_children else None
            if e is None or (is_first and e.type == "string"):
                continue
            if e.type in ("assignment", "augmented_assignment"):
                names = _target_names(e)
                if names == ["__all__"]:
                    info.declared_all = (info.declared_all or []) + _STR_RE.findall(_t(e.child_by_field_name("right")))
                    continue
                # Warn on computed/mutated __all__
                if names and any(n.startswith("__all__") for n in names):
                    # e.g., __all__ += [...], __all__.append(...)
                    info.warnings.append(f"{info.module}: computed or mutated __all__ — static analysis may be incomplete")
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
                    continue
                if cond in _TC_NAMES:
                    body = n.child_by_field_name("consequence")
                    for ch in body.named_children if body is not None else []:
                        if ch.type in _IMPORT_TYPES:
                            info.imports.extend(parse_import(ch, info.module, info.is_pkg))
                    continue
                # Detect conditional/platform/optional imports
                if "sys.platform" in cond or "sys.version" in cond or "TYPE_CHECKING" in cond:
                    info.warnings.append(f"{info.module}: conditional import guarded by '{cond.strip()}' kept verbatim")
            imports, has_def = _scan(n)
            if imports and not has_def:
                start, end = _extent(n)
                specs = [s for imp in imports for s in parse_import(imp, info.module, info.is_pkg)]
                info.guards.append(GuardBlock(src[start:end].decode("utf-8", "replace"), specs))
            else:
                add(MISC, _bound_names(n), n)
        elif t == "pass_statement":
            continue
        else:
            add(MISC, [], n)
    # After extraction, run module-level dynamic detection on full text
    full_text = src.decode("utf-8", "replace")
    _detect_dynamic_patterns(full_text, info)
    _detect_runtime_semantics(full_text, info)
    # Additional edge: walrus, match, global/nonlocal are handled via scoping in _collect_occs (lazy, shadow)
    # Comprehensions, lambdas already handled via lazy propagation


def parse_module_file(
    path_str: str,
    module: str,
    is_pkg: bool,
    keep_docstrings: bool = True,
    strip_comments: bool = False,
    keep_comments: bool = True,
    allow_syntax_errors: bool = False,
) -> ModuleInfo:
    info = ModuleInfo(module=module, path=path_str, is_pkg=is_pkg)
    try:
        src = Path(path_str).read_bytes()
    except OSError as exc:
        info.errors.append(f"cannot read {path_str}: {exc}")
        return info
    if src.startswith(b"\xef\xbb\xbf"):
        src = src[3:]
    # Handle encoding/BOM/CRLF/tabs/shebang/encoding cookies
    # BOM already stripped; detect encoding cookie (PEP 263) but we read as bytes and decode as utf-8 replace
    # CRLF normalization is handled during rendering; keep original for fidelity
    # Check for non-UTF8: if decode fails, log warning but continue with replace
    try:
        text_probe = src.decode("utf-8")
        if "\r" in text_probe:
            info.warnings.append(f"{path_str}: contains CRLF line endings — normalized to LF in output")
    except UnicodeDecodeError:
        info.warnings.append(f"{path_str}: non-UTF8 encoding detected — decoded with replacement characters")
    try:
        root = _get_parser().parse(src).root_node
        if root.has_error:
            err_line = _first_error_line(root)
            msg = f"{path_str}: syntax error near line {err_line}"
            if allow_syntax_errors:
                info.warnings.append(msg + " — attempting partial parse (allow-syntax-errors)")
                # Try to extract anyway; tree-sitter still produces partial tree
                try:
                    _extract(info, root, src)
                    info.warnings.append(f"{path_str}: partial parse produced {len(info.blocks)} blocks")
                except Exception as exc:
                    info.errors.append(f"{path_str}: extraction failed after syntax error: {exc!r}")
                # Apply post-processing even on partial parse
                if not keep_docstrings:
                    for b in info.blocks:
                        b.source = _strip_docstring_from_source(b.source)
                        b.stripped_docstring = True
                if strip_comments or not keep_comments:
                    for b in info.blocks:
                        b.source = _strip_comments_from_source(b.source)
                return info
            else:
                info.errors.append(msg)
                return info
        _extract(info, root, src)
        # Post-process per flags
        if not keep_docstrings:
            for b in info.blocks:
                b.source = _strip_docstring_from_source(b.source)
                b.stripped_docstring = True
        if strip_comments or not keep_comments:
            for b in info.blocks:
                b.source = _strip_comments_from_source(b.source)
    except Exception as exc:  # noqa: BLE001 - reported to the caller, never crashes a worker
        info.errors.append(f"{path_str}: extraction failed: {exc!r}")
    return info
