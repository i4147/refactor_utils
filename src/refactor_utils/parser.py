"""tree-sitter based extraction of top-level blocks (runs inside worker processes)."""

from __future__ import annotations
import ast
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
#: Nodes that open a new Python scope, so ``global``/``nonlocal`` statements
#: inside them belong to that scope, not to the scope being inspected.
_SCOPE_TYPES = {"function_definition", "class_definition", "lambda"}
_COMPREHENSIONS = {
    "list_comprehension",
    "set_comprehension",
    "dictionary_comprehension",
    "generator_expression",
}
_CODING_COOKIE_RE = re.compile(r"(coding[:=]\s*)[-\w.]+")
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
    """Convert an import statement node into :class:`ImportSpec` objects.

    Relative imports are resolved against ``module`` so downstream code only
    ever deals with absolute module names.

    Args:
        node: ``import_statement``, ``import_from_statement`` or
            ``future_import_statement`` node.
        module: Dotted name of the module the statement appears in.
        is_pkg: Whether that module is a package ``__init__``.

    Returns:
        One specification per imported name (or a single star specification).

    """
    specs: list[ImportSpec] = []
    if node.type == "import_statement":
        for ch in node.children_by_field_name("name"):
            if ch.type == "aliased_import":
                specs.append(
                    ImportSpec(
                        _t(ch.child_by_field_name("name")),
                        alias=_t(ch.child_by_field_name("alias")),
                    )
                )
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
        rest = next(
            (_t(c) for c in mod_node.named_children if c.type == "dotted_name"), ""
        )
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
        specs.append(
            ImportSpec(mod, name=name, alias=alias, is_from=True, relative=relative)
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
    """Collect the identifier names bound by a parameter or target pattern.

    Handles plain identifiers, defaulted/annotated parameters and the tuple,
    list and splat patterns that may appear in ``with ... as``, ``for`` and
    ``match`` construct positions.

    Args:
        node: Pattern or parameter node to inspect.

    Returns:
        The set of names bound by ``node`` (empty for non-binding nodes).

    """
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
        "tuple",
        "list",
        "pattern_list",
        "typed_pattern",
        "parenthesized_expression",
    ):
        out: set[str] = set()
        for ch in node.named_children:
            if ch.type != "type":
                out |= _binding_names(ch)
        return out
    return set()


def _as_bindings(node: Node | None) -> set[str]:
    """Collect names bound by every ``as`` pattern inside ``node``.

    Args:
        node: A ``with_item``/``with_clause``/``except_clause`` subtree.

    Returns:
        Names introduced by ``as`` clauses (empty when there is none).

    """
    names: set[str] = set()
    if node is None:
        return names
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.type == "as_pattern":
            for ch in cur.named_children:
                if ch.type == "as_pattern_target":
                    names |= _binding_names(ch)
                elif ch.type not in ("identifier",):
                    stack.append(ch)
            continue
        stack.extend(cur.named_children)
    return names


def _pattern_scan(node: Node | None, refs: list[Node], bound: set[str]) -> None:
    """Split a ``match`` pattern into referenced nodes and bound names.

    Bare names in a ``match`` pattern *bind* (capture), whereas dotted names
    and the class name of a class pattern *refer* to existing objects. The
    resolver must therefore rewrite the references but never the captures.

    Args:
        node: Pattern node to inspect (``case_pattern`` and friends).
        refs: Output list, appended with nodes that are name references.
        bound: Output set, updated with the names captured by the pattern.

    """
    if node is None:
        return
    t = node.type
    if t == "dotted_name":
        text = _t(node)
        if "." in text:
            first = node.named_children[0] if node.named_children else node
            refs.append(first)
        else:
            bound.add(text)
        return
    if t == "class_pattern":
        cls = next((c for c in node.named_children if c.type == "dotted_name"), None)
        if cls is not None:
            refs.append(cls.named_children[0] if cls.named_children else cls)
        for ch in node.named_children:
            if ch is not cls:
                _pattern_scan(ch, refs, bound)
        return
    if t == "keyword_pattern":
        # ``Point(x=px)``: ``x`` is an attribute name, ``px`` a sub-pattern.
        for ch in node.named_children[1:]:
            _pattern_scan(ch, refs, bound)
        return
    if t == "splat_pattern":
        for ch in node.named_children:
            if ch.type == "identifier":
                bound.add(_t(ch))
        return
    if t == "as_pattern":
        for ch in node.named_children:
            if ch.type == "identifier":
                bound.add(_t(ch))
            elif ch.type != "as_pattern_target":
                _pattern_scan(ch, refs, bound)
        return
    for ch in node.named_children:
        _pattern_scan(ch, refs, bound)


def _comprehension_bindings(node: Node) -> set[str]:
    """Collect names local to a comprehension or generator expression.

    Args:
        node: A comprehension node as listed in ``_COMPREHENSIONS``.

    Returns:
        Names bound by ``for`` clauses and by walrus expressions inside it.

    """
    names: set[str] = set()
    for ch in node.named_children:
        if ch.type == "for_in_clause":
            names |= _binding_names(ch.child_by_field_name("left"))
        elif ch.type == "named_expression" and ch.named_children:
            names |= _binding_names(ch.named_children[0])
    return names


def _param_names(params: Node | None) -> set[str]:
    names: set[str] = set()
    if params is not None:
        for ch in params.named_children:
            names |= _binding_names(ch)
    return names


def _scope_declarations(node: Node | None) -> tuple[set[str], set[str]]:
    """Collect the ``global`` and ``nonlocal`` declarations of a single scope.

    Nested ``def``/``class``/``lambda`` bodies are boundaries: their
    declarations belong to their own scope and are skipped here.

    Args:
        node: Body node of the scope to inspect (usually a ``block``).

    Returns:
        ``(globals, nonlocals)``: the names declared by ``global`` and
        ``nonlocal`` statements that belong to this scope.

    """
    globals_: set[str] = set()
    nonlocals_: set[str] = set()
    if node is None:
        return globals_, nonlocals_
    stack = [node]
    while stack:
        cur = stack.pop()
        for ch in cur.named_children:
            if ch.type == "global_statement":
                globals_.update(
                    _t(c) for c in ch.named_children if c.type == "identifier"
                )
            elif ch.type == "nonlocal_statement":
                nonlocals_.update(
                    _t(c) for c in ch.named_children if c.type == "identifier"
                )
            elif ch.type in _SCOPE_TYPES:
                continue
            else:
                stack.append(ch)
    return globals_, nonlocals_


def _nested_nonlocals(node: Node | None) -> set[str]:
    """Collect ``nonlocal`` names declared by scopes nested inside ``node``.

    A nested ``nonlocal x`` can target this scope (or any scope above it), so
    every name it declares must be treated as a live binding of this scope.
    Nested classes are included: a class body opens a namespace, but a
    ``nonlocal`` written inside it still targets the enclosing function.

    Args:
        node: Body node of the enclosing scope (usually a ``block``).

    Returns:
        Names declared via ``nonlocal`` anywhere below ``node``.

    """
    names: set[str] = set()
    if node is None:
        return names
    stack = [node]
    while stack:
        cur = stack.pop()
        for ch in cur.named_children:
            if ch.type in ("function_definition", "lambda", "class_definition"):
                names |= _scope_declarations(ch.child_by_field_name("body"))[1]
            # Descend into the nested scope anyway: a deeper ``nonlocal`` may
            # target this scope as well.
            stack.append(ch)
    return names


def _function_shadow(
    shadow: frozenset[str], params: Node | None, body: Node | None
) -> frozenset[str]:
    """Compute the shadow set used while walking a function body.

    ``nonlocal`` declarations make a name a binding of an *enclosing* function,
    so the enclosing scope must keep that name untouched; otherwise renaming
    the binding while the nested ``nonlocal x`` stays put silently rebinds it
    (or turns the declaration into a ``SyntaxError``). ``nonlocal`` names are
    therefore added to this scope's shadow set, which propagates into nested
    scopes as well.

    ``global`` declarations do the opposite: the name is a module-level
    binding again, so it is removed from the inherited shadow set and renamed
    together with the module-level object.

    Args:
        shadow: Shadow set inherited from the enclosing scope.
        params: Parameter node of the function (``None`` for none).
        body: Body node of the function (``None`` when unavailable).

    Returns:
        The shadow set to use for the function body and its nested scopes.

    """
    own_globals, own_nonlocals = _scope_declarations(body)
    return frozenset(
        (shadow | _param_names(params) | own_nonlocals | _nested_nonlocals(body))
        - own_globals
    )


def _push_param_exprs(
    stack: list, params: Node | None, lazy: bool, shadow: frozenset[str]
) -> None:
    if params is None:
        return
    for ch in params.named_children:
        if ch.type in (
            "default_parameter",
            "typed_default_parameter",
            "typed_parameter",
        ):
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


_PICKLE_RE = re.compile(r"\b(pickle|dill|cloudpickle|shelve|marshal)\b")
_LOGGER_RE = re.compile(r"\bgetLogger\s*\(\s*__name__\s*\)")
_FILE_PARENT_RE = re.compile(
    r"(__file__\s*\)\s*\.parent|Path\s*\(\s*__file__\s*\)|dirname\s*\(\s*__file__\s*\))"
)


def _detect_runtime_semantics(text: str, info: ModuleInfo) -> None:
    """Warn about module-level runtime semantics that consolidation changes.

    Flags ``__name__``/``__package__``/``__path__`` usage, ``__file__`` based
    resource loading, per-module logger names and serialization of
    module-level objects (pickle changes ``__module__``).

    Args:
        text: Full decoded module source.
        info: Module being parsed; warnings are appended to it.

    """
    # __file__, __name__, __package__, __path__, resource loading
    for sym in ("__file__", "__name__", "__package__", "__path__"):
        if sym in text:
            # __file__ already warned in _collect_occs, add broader warning
            if sym != "__file__":
                info.warnings.append(
                    f"{info.module}: uses {sym} — semantics change in consolidated file (now points at _consolidated.py)"
                )
    if (
        "importlib.resources" in text
        or "pkg_resources" in text
        or "open(" in text
        and "__file__" in text
    ):
        info.warnings.append(
            f"{info.module}: does resource loading relative to __file__ — verify output handles bundled resources"
        )
    if _FILE_PARENT_RE.search(text):
        info.warnings.append(
            f"{info.module}: resolves paths relative to __file__ — after consolidation "
            "__file__ points at the output file, so these paths must be adjusted "
            "(use --copy-data and rewrite the path, or keep the module layout)"
        )
    if _LOGGER_RE.search(text):
        info.warnings.append(
            f"{info.module}: logging.getLogger(__name__) — __name__ becomes "
            "'_consolidated' for every block, so the logger hierarchy collapses"
        )
    if _PICKLE_RE.search(text):
        info.warnings.append(
            f"{info.module}: pickles/serializes module objects — __module__ and "
            "__qualname__ change after consolidation, so previously pickled "
            "artifacts will not unpickle"
        )
    if re.search(r"\bmultiprocessing\b", text) and "spawn" in text:
        info.warnings.append(
            f"{info.module}: uses multiprocessing spawn — entry-point and "
            "__main__ semantics differ in the consolidated file"
        )


def _strip_docstring_from_source(src: bytes) -> bytes:
    """Remove docstrings from a block's source using AST."""
    try:
        text = src.decode("utf-8", "replace")
        tree = ast.parse(text)
        # Find docstring nodes and remove them via line intervals
        lines = text.splitlines()
        # Simple approach: if first statement is Expr(Str), remove it
        if (
            tree.body
            and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, (ast.Constant, ast.Str))
        ):
            # docstring found
            node = tree.body[0]
            start = (node.lineno or 1) - 1
            end = node.end_lineno or start + 1
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


def _collect_occs(
    root: Node, base: int, src: bytes, own_def: Node | None, info: ModuleInfo
) -> list[Occ]:
    """Collect every renameable occurrence and nested import of a block.

    Walks the block's subtree and records identifier/attribute references as
    :class:`Occ` objects with offsets relative to the block start. Scope
    bindings are tracked so that locally bound names (parameters, ``for``
    targets, ``with``/``except`` aliases, ``match`` captures and comprehension
    variables) are never confused with module-level objects of the same name.
    ``global`` declarations make a name follow the module-level object again,
    while ``nonlocal`` declarations pin the enclosing function binding *and*
    the declaration to one stable name (see :func:`_function_shadow`).

    Args:
        root: Root node of the block (the top-level statement).
        base: Byte offset of the block inside ``src``; occurrences are stored
            relative to it.
        src: The complete module source the block was sliced from.
        own_def: The ``def``/``class`` node whose name belongs to the block, so
            that only the block's own definition name is treated as a
            reference (decorated definitions wrap it).
        info: Module being parsed; warnings are appended to it.

    Returns:
        Occurrences in traversal order, including ``nested_import`` markers
        that carry the import specifications found inside the block.

    """
    occs: list[Occ] = []
    own_id = own_def.id if own_def is not None else None
    future = info.future_annotations
    warned_file = False
    stack: list[tuple[Node, bool, frozenset[str], bool]] = [
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
            # Additional runtime semantics warnings handled via _detect_runtime_semantics on full text
            occs.append(
                Occ(
                    node.start_byte - base, node.end_byte - base, name, "name", not lazy
                )
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
            body = node.child_by_field_name("body")
            inner = _function_shadow(shadow, params, body)
            _push_param_exprs(stack, params, lazy, shadow)
            for fld in ("return_type", "type_parameters"):
                sub = node.child_by_field_name(fld)
                if sub is not None:
                    stack.append((sub, lazy, shadow, False))
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
        if t == "as_pattern":
            # ``with ... as target`` / ``except E as target``: the target binds.
            for ch in node.named_children:
                if ch.type != "as_pattern_target":
                    stack.append((ch, lazy, shadow, False))
            continue
        if t == "with_statement":
            body = node.child_by_field_name("body")
            clause = next(
                (c for c in node.named_children if c.type == "with_clause"), None
            )
            bound = _as_bindings(clause)
            if clause is not None:
                stack.append((clause, lazy, shadow, False))
            if body is not None:
                stack.append((body, lazy, shadow | bound, False))
            continue
        if t == "except_clause":
            bound = _as_bindings(node)
            for ch in node.named_children:
                if ch.type == "block":
                    stack.append((ch, lazy, shadow | bound, False))
                elif ch.type == "as_pattern":
                    for sub in ch.named_children:
                        if sub.type != "as_pattern_target":
                            stack.append((sub, lazy, shadow, False))
                else:
                    stack.append((ch, lazy, shadow, False))
            continue
        if t == "for_statement":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            body = node.child_by_field_name("body")
            bound = _binding_names(left)
            if right is not None:
                stack.append((right, lazy, shadow, False))
            if body is not None:
                stack.append((body, lazy, shadow | bound, False))
            continue
        if t == "case_clause":
            pattern = next(
                (c for c in node.named_children if c.type == "case_pattern"), None
            )
            refs: list[Node] = []
            bound: set[str] = set()
            _pattern_scan(pattern, refs, bound)
            for ref in refs:
                stack.append((ref, lazy, shadow, False))
            for ch in node.named_children:
                if ch is pattern:
                    continue
                stack.append((ch, lazy, shadow | bound, False))
            continue
        if t in _COMPREHENSIONS:
            inner = shadow | _comprehension_bindings(node)
            for ch in node.named_children:
                if ch.type == "for_in_clause":
                    right = ch.child_by_field_name("right")
                    if right is not None:
                        stack.append((right, lazy, inner, False))
                    continue
                stack.append((ch, lazy, inner, False))
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


def _literal_str_list(node: Node | None) -> list[str] | None:
    """Evaluate a node as a literal list/tuple/set of strings.

    ``__all__`` is parsed through :func:`ast.literal_eval` rather than a
    regular expression so that unicode identifiers are accepted and dynamic
    expressions (``["a"] + other``) are reported instead of silently
    mis-parsed.

    Args:
        node: The right-hand side of an ``__all__`` assignment.

    Returns:
        The literal entries, or ``None`` when the node is not a literal
        list/tuple/set made only of strings.

    """
    if node is None:
        return None
    try:
        value = ast.literal_eval(_t(node))
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None
    if isinstance(value, (list, tuple, set, frozenset)) and all(
        isinstance(item, str) for item in value
    ):
        return list(value)
    return None


def _handle_all_statement(node: Node, info: ModuleInfo) -> bool:
    """Record an ``__all__`` definition or mutation on ``info``.

    ``__all__ = [...]`` *replaces* the export list while ``__all__ += [...]``
    and ``__all__.append()/extend()`` *append* to it. Treating every
    assignment as an append silently produced a wrong export list.

    Args:
        node: A top-level expression node (assignment or call).
        info: Module being parsed; ``declared_all`` is updated in place.

    Returns:
        ``True`` when the statement was an ``__all__`` definition or mutation
        and must therefore not become a regular source block.

    """
    if node.type in ("assignment", "augmented_assignment"):
        left = node.child_by_field_name("left")
        if left is None or _t(left) != "__all__":
            return False
        entries = _literal_str_list(node.child_by_field_name("right"))
        if entries is None:
            info.warnings.append(
                f"{info.module}: __all__ is not a literal list/tuple/set of strings "
                "— static analysis may be incomplete"
            )
            return True
        if node.type == "assignment":
            info.declared_all = entries
        else:
            info.declared_all = (info.declared_all or []) + entries
        return True
    if node.type != "call":
        return False
    func = node.child_by_field_name("function")
    if func is None or func.type != "attribute":
        return False
    if _t(func.child_by_field_name("object")) != "__all__":
        return False
    method = _t(func.child_by_field_name("attribute"))
    arguments = node.child_by_field_name("arguments")
    entry_node = (
        arguments.named_children[0]
        if (arguments is not None and arguments.named_children)
        else None
    )
    entries: list[str] | None
    if method == "append":
        entries = _literal_str_list(None if entry_node is None else entry_node)
        if entries is not None and len(entries) != 1:
            entries = None
        if entries is None and entry_node is not None:
            try:
                literal = ast.literal_eval(_t(entry_node))
            except (ValueError, SyntaxError, MemoryError, RecursionError):
                literal = None
            entries = [literal] if isinstance(literal, str) else None
    elif method == "extend":
        entries = _literal_str_list(entry_node)
    else:
        entries = None
    info.warnings.append(
        f"{info.module}: __all__.{method}() mutation — merged statically, "
        "verify the exported name list"
    )
    if entries:
        info.declared_all = (info.declared_all or []) + entries
    return True


def _guard_from_node(node: Node, src: bytes, info: ModuleInfo) -> GuardBlock:
    """Build a :class:`GuardBlock` for a conditional import statement.

    Args:
        node: The ``if``/``try`` statement to keep verbatim.
        src: Module source the node belongs to.
        info: Module being parsed, used to parse the guarded imports.

    Returns:
        A guard block whose ``specs`` describe the imports it contains.

    """
    start, end = _extent(node)
    imports, _ = _scan(node)
    specs: list[ImportSpec] = []
    spec_lines: list[tuple[ImportSpec, str]] = []
    for imp in imports:
        text = _t(imp)
        for spec in parse_import(imp, info.module, info.is_pkg):
            specs.append(spec)
            spec_lines.append((spec, text))
    return GuardBlock(
        src[start:end].decode("utf-8", "replace"), specs, spec_lines=spec_lines
    )


def _extract(info: ModuleInfo, root: Node, src: bytes) -> None:
    """Populate ``info`` with blocks, imports, guards and ``__all__``.

    Args:
        info: Module metadata to fill in (mutated in place).
        root: tree-sitter root node of the module.
        src: Raw module source (already decoded/re-encoded as needed).

    """
    futures: list[str] = []
    for n in root.named_children:
        if n.type != "future_import_statement":
            continue
        for ch in n.children_by_field_name("name"):
            name = _t(ch)
            if name and name not in futures:
                futures.append(name)
    info.future_names = futures
    info.future_annotations = "annotations" in futures
    by_name: dict[str, Block] = {}

    def add(kind: str, names: list[str], node: Node, own: Node | None = None) -> None:
        """Append a block, merging it into an existing block of the same name."""
        start, end = _extent(node)
        # Capture original location for source_map
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        raw_source = src[start:end]
        # Detect dynamic patterns per block but also aggregate at module level via full src later
        occs = _collect_occs(node, start, src, own, info)
        block = Block(
            info.module, kind, list(names), raw_source, occs, len(info.blocks)
        )
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
            d = (
                n.child_by_field_name("definition")
                if t == "decorated_definition"
                else n
            )
            kind = CLASS if d.type == "class_definition" else FUNC
            add(kind, [_t(d.child_by_field_name("name"))], n, own=d)
        elif t == "expression_statement":
            e = n.named_children[0] if n.named_children else None
            if e is None or (is_first and e.type == "string"):
                continue
            if _handle_all_statement(e, info):
                continue
            if e.type in ("assignment", "augmented_assignment"):
                names = _target_names(e)
                if names == ["__all__"]:
                    # Unreachable safety net: __all__ is never emitted as a block.
                    continue
                if names and "__all__" in names:
                    # Never emit an __all__ binding into the consolidated file;
                    # only the generated __init__.py should define it.
                    info.warnings.append(
                        f"{info.module}: __all__ assignment target skipped in output"
                    )
                    names = [name for name in names if name != "__all__"]
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
                    # Keep non-import statements (type aliases, Protocol stubs,
                    # TypeVar definitions, ...) instead of dropping them.
                    for ch in _scan(n)[0]:
                        info.imports.extend(parse_import(ch, info.module, info.is_pkg))
                    info.guards.append(_guard_from_node(n, src, info))
                    info.warnings.append(
                        f"{info.module}: TYPE_CHECKING block kept verbatim "
                        "(it may contain non-import declarations)"
                    )
                    continue
                # Detect conditional/platform/optional imports
                if (
                    "sys.platform" in cond
                    or "sys.version" in cond
                    or "TYPE_CHECKING" in cond
                ):
                    info.warnings.append(
                        f"{info.module}: conditional import guarded by '{cond.strip()}' kept verbatim"
                    )
            imports, has_def = _scan(n)
            if imports and not has_def:
                info.guards.append(_guard_from_node(n, src, info))
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
    # Additional edge: walrus, match and nonlocal/global declarations are
    # handled via the scope tracking in _collect_occs (lazy, shadow).
    # Comprehensions and lambdas are already handled via lazy propagation.


def parse_module_file(
    path_str: str,
    module: str,
    is_pkg: bool,
    keep_docstrings: bool = True,
    strip_comments: bool = False,
    keep_comments: bool = True,
    allow_syntax_errors: bool = False,
) -> ModuleInfo:
    """Parse one python file into a :class:`ModuleInfo`.

    Reads the file honouring PEP 263 encoding cookies, re-encodes the text as
    utf-8 (so byte offsets stay consistent), extracts blocks, imports, guards
    and ``__all__``, and collects warnings. Failures are reported through
    ``ModuleInfo.errors`` rather than raised, so a worker never crashes.

    Args:
        path_str: Path to the file to parse.
        module: Dotted module name the file maps to.
        is_pkg: Whether the file is a package ``__init__.py``.
        keep_docstrings: Keep docstrings in the extracted block sources.
        strip_comments: Remove standalone comment lines from blocks.
        keep_comments: Keep comments at all.
        allow_syntax_errors: Extract what is possible from a broken file.

    Returns:
        The parsed module, with ``errors`` populated when parsing failed.

    """
    info = ModuleInfo(module=module, path=path_str, is_pkg=is_pkg)
    try:
        src = Path(path_str).read_bytes()
    except OSError as exc:
        info.errors.append(f"cannot read {path_str}: {exc}")
        return info
    if src.startswith(b"\xef\xbb\xbf"):
        src = src[3:]
    # Honour PEP 263 encoding cookies instead of assuming utf-8, and re-encode
    # the decoded text as utf-8 so byte offsets stay consistent downstream.
    encoding = "utf-8"
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(src).readline)
    except (SyntaxError, LookupError):
        info.warnings.append(
            f"{path_str}: could not detect source encoding — assuming utf-8"
        )
    info.encoding = encoding
    try:
        decoded = src.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        decoded = src.decode(encoding, "replace")
        info.warnings.append(
            f"{path_str}: could not decode as {encoding} — replacement characters inserted"
        )
    if encoding.lower().replace("_", "-") not in ("utf-8", "utf8"):
        info.warnings.append(
            f"{path_str}: source encoding is {encoding} — output is written as utf-8"
        )
        # The stale PEP 263 cookie would make the (now utf-8) output invalid.
        lines = decoded.splitlines(keepends=True)
        for index in range(min(2, len(lines))):
            if _CODING_COOKIE_RE.search(lines[index]):
                lines[index] = _CODING_COOKIE_RE.sub(r"\1utf-8", lines[index], count=1)
        decoded = "".join(lines)
    if "\r" in decoded:
        info.warnings.append(
            f"{path_str}: contains CRLF line endings — normalized per --eol in output"
        )
    src = decoded.encode("utf-8")
    try:
        root = _get_parser().parse(src).root_node
        if root.has_error:
            err_line = _first_error_line(root)
            msg = f"{path_str}: syntax error near line {err_line}"
            if allow_syntax_errors:
                info.warnings.append(
                    msg + " — attempting partial parse (allow-syntax-errors)"
                )
                # Try to extract anyway; tree-sitter still produces partial tree
                try:
                    _extract(info, root, src)
                    info.warnings.append(
                        f"{path_str}: partial parse produced {len(info.blocks)} blocks"
                    )
                except Exception as exc:
                    info.errors.append(
                        f"{path_str}: extraction failed after syntax error: {exc!r}"
                    )
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
    except (
        Exception
    ) as exc:  # noqa: BLE001 - reported to the caller, never crashes a worker
        info.errors.append(f"{path_str}: extraction failed: {exc!r}")
    return info
