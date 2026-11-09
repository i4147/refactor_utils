"""tree-sitter based extraction of top-level blocks (runs inside worker processes)."""

import ast
import io
import re
import textwrap
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
_TC_RE = re.compile(r"^(?:[A-Za-z_]\w*\.)?TYPE_CHECKING$")
_DISCARD_ALL_TMP = "_pyrefactor_discard_all_"
_CLAUSE_TYPES = {
    "elif_clause",
    "else_clause",
    "except_clause",
    "finally_clause",
    "block",
    "case_clause",
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
    """Return the process-local tree-sitter parser, creating it once."""
    parser = getattr(_LOCAL, "parser", None)
    if parser is None:
        parser = Parser(Language(_tspy.language()))
        _LOCAL.parser = parser
    return parser


def _t(node: Node | None) -> str:
    """Return the decoded text of a tree-sitter node (``""`` for None)."""
    return node.text.decode("utf-8", "replace") if node is not None else ""


def _resolve_relative(current: str, is_pkg: bool, level: int, rest: str) -> str:
    """Resolve a relative import to an absolute dotted module name."""
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
    """Return the dotted name of an identifier/attribute node, or None."""
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
    """Return the parameter names declared by ``params``."""
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
    """Push the annotations and default values of ``params`` onto the dependency stack."""
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
    """Return the names bound by an assignment target, or None when the target is not a plain name/pattern."""
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
    """Return the names bound by a (possibly chained) assignment."""
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
    """Return the first identifier found in ``node`` (best effort)."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.type == "identifier":
            return _t(cur)
        stack.extend(reversed(cur.named_children))
    return ""


def _compound_defs(node: Node) -> tuple[list[str], dict[str, str], bool]:
    """Collect module-scope definitions inside a compound statement.

    Walks control-flow clauses without descending into ``def`` or ``class``
    bodies so inner local variables and class attributes are ignored.

    Args:
        node: Compound statement node (``if``, ``try``, ``with``, ``for``, ...).

    Returns:
        ``(names, name_kinds, binds_value)`` where ``names`` are unique bound
        names in appearance order (excluding ``__all__``), ``name_kinds`` maps
        each name to ``CONST``/``CLASS``/``FUNC``, and ``binds_value`` indicates
        whether at least one runtime binding (not a bare annotation) occurred.

    """
    names: list[str] = []
    kinds: dict[str, str] = {}
    has_value = False
    stack: list[Node] = [node]
    while stack:
        cur = stack.pop()
        for ch in reversed(cur.named_children):
            t = ch.type
            if t in ("function_definition", "class_definition"):
                nm = _t(ch.child_by_field_name("name"))
                if nm and nm not in ("__all__", _DISCARD_ALL_TMP):
                    names.append(nm)
                    kinds[nm] = CLASS if t == "class_definition" else FUNC
                    has_value = True
            elif t == "decorated_definition":
                defn = ch.child_by_field_name("definition")
                if defn is not None:
                    nm = _t(defn.child_by_field_name("name"))
                    if nm and nm not in ("__all__", _DISCARD_ALL_TMP):
                        names.append(nm)
                        kinds[nm] = CLASS if defn.type == "class_definition" else FUNC
                        has_value = True
            elif t == "type_alias_statement":
                nm = _first_identifier(ch)
                if nm and nm not in ("__all__", _DISCARD_ALL_TMP):
                    names.append(nm)
                    kinds.setdefault(nm, CONST)
                    has_value = True
            elif t == "expression_statement" and ch.named_children:
                e = ch.named_children[0]
                if e.type in ("assignment", "augmented_assignment"):
                    has_rhs = (
                        e.type == "augmented_assignment"
                        or e.child_by_field_name("right") is not None
                    )
                    for nm in _target_names(e) or []:
                        if nm in ("__all__", _DISCARD_ALL_TMP):
                            continue
                        names.append(nm)
                        kinds.setdefault(nm, CONST)
                        if has_rhs:
                            has_value = True
            elif t in _COMPOUND or t in _CLAUSE_TYPES:
                stack.append(ch)
    unique = list(dict.fromkeys(names))
    return unique, kinds, has_value


def _bound_names(node: Node) -> list[str]:
    """Return the module-scope names bound by a compound statement."""
    names, _, _ = _compound_defs(node)
    return names


def _scan_module_imports(node: Node) -> list[Node]:
    """Return import statements at module control-flow scope inside ``node``."""
    imports: list[Node] = []
    stack: list[Node] = [node]
    while stack:
        cur = stack.pop()
        for ch in reversed(cur.named_children):
            if ch.type in _IMPORT_TYPES:
                imports.append(ch)
            elif ch.type in _COMPOUND or ch.type in _CLAUSE_TYPES:
                stack.append(ch)
    return imports


def _scan(node: Node) -> tuple[list[Node], bool]:
    """Collect the imports and ``def``/``class`` presence of a subtree."""
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
    """Return the byte range of a node plus its attached comments."""
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
    """Return the 1-based line of the first syntax error, or 0."""
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


def _append_all_entries(info: ModuleInfo, entries: list[str], *, replace: bool) -> None:
    """Update ``info.declared_all`` in place while avoiding duplicates."""
    if replace:
        info.declared_all = list(dict.fromkeys(entries))
        return
    current = list(info.declared_all or [])
    for item in entries:
        if item not in current:
            current.append(item)
    info.declared_all = current


def _is_all_target(node: Node | None) -> bool:
    """Return whether ``node`` is ``__all__`` or a subscript ``__all__[...]``."""
    if node is None:
        return False
    if node.type == "identifier":
        return _t(node) == "__all__"
    if node.type == "subscript":
        return _t(node.child_by_field_name("value")) == "__all__"
    return False


def _replace_all_ident_in_slice(lhs: Node, base_byte: int, seg_bytes: bytes) -> bytes:
    """Replace ``__all__`` identifiers inside an unpacking LHS slice."""
    spans: list[tuple[int, int]] = []
    stack = [lhs]
    while stack:
        cur = stack.pop()
        if cur.type == "identifier" and _t(cur) == "__all__":
            spans.append((cur.start_byte - base_byte, cur.end_byte - base_byte))
            continue
        stack.extend(cur.named_children)
    spans.sort()
    out: list[bytes] = []
    pos = 0
    repl = _DISCARD_ALL_TMP.encode("utf-8")
    for s, e in spans:
        out.append(seg_bytes[pos:s])
        out.append(repl)
        pos = e
    out.append(seg_bytes[pos:])
    return b"".join(out)


def _literal_str_arg(node: Node | None) -> str | None:
    """Evaluate ``node`` as a single literal Python string, if possible."""
    if node is None:
        return None
    try:
        val = ast.literal_eval(_t(node))
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None
    return val if isinstance(val, str) else None


def _rewrite_all_statement(
    stmt: Node, src: bytes, info: ModuleInfo, *, in_compound: bool = False
) -> bytes | None:
    """Harvest and strip ``__all__`` from a simple statement.

    Args:
        stmt: Statement node to inspect.
        src: Source bytes ``stmt`` belongs to.
        info: Module metadata whose ``declared_all`` and ``warnings`` are updated.
        in_compound: Whether ``stmt`` sits inside a conditional/compound suite.

    Returns:
        ``None`` when ``stmt`` does not define or mutate ``__all__``; ``b""``
        when ``stmt`` must be dropped completely; otherwise replacement bytes
        with the ``__all__`` target stripped out.

    """
    if b"__all__" not in src[stmt.start_byte : stmt.end_byte]:
        return None
    if stmt.type == "type_alias_statement":
        if _first_identifier(stmt) == "__all__":
            info.warnings.append(f"{info.module}: __all__ type alias skipped in output")
            return b""
        return None
    if stmt.type == "delete_statement":
        targets: list[Node] = []
        for ch in stmt.named_children:
            if ch.type == "expression_list":
                targets.extend(ch.named_children)
            else:
                targets.append(ch)
        if not any(_is_all_target(t) for t in targets):
            return None
        if all(_is_all_target(t) for t in targets):
            if not in_compound and any(
                t.type == "identifier" and _t(t) == "__all__" for t in targets
            ):
                info.declared_all = None
            info.warnings.append(
                f"{info.module}: del __all__ — static analysis may be incomplete"
            )
            return b""
        kept = [_t(t) for t in targets if not _is_all_target(t)]
        info.warnings.append(f"{info.module}: __all__ target skipped in del statement")
        return f"del {', '.join(kept)}".encode("utf-8")
    if stmt.type != "expression_statement" or not stmt.named_children:
        return None
    e = stmt.named_children[0]
    if e.type == "call":
        func = e.child_by_field_name("function")
        if (
            func is None
            or func.type != "attribute"
            or _t(func.child_by_field_name("object")) != "__all__"
        ):
            return None
        method = _t(func.child_by_field_name("attribute"))
        arguments = e.child_by_field_name("arguments")
        args = arguments.named_children if arguments is not None else []
        entry_node = args[0] if args else None
        entries: list[str] | None = None
        if method == "append":
            lit = _literal_str_arg(entry_node)
            entries = [lit] if lit is not None else None
        elif method == "extend":
            entries = _literal_str_list(entry_node)
        elif method == "insert" and len(args) >= 2:
            lit = _literal_str_arg(args[1])
            entries = [lit] if lit is not None else None
        elif method == "remove" and entry_node is not None and not in_compound:
            lit = _literal_str_arg(entry_node)
            if lit is not None and info.declared_all is not None:
                info.declared_all = [x for x in info.declared_all if x != lit]
        elif method == "clear" and not in_compound:
            info.declared_all = []
        info.warnings.append(
            f"{info.module}: __all__.{method}() mutation — merged statically, "
            "verify the exported name list"
        )
        if entries:
            _append_all_entries(info, entries, replace=False)
        return b""
    if e.type == "augmented_assignment":
        left = e.child_by_field_name("left")
        if not _is_all_target(left):
            return None
        if left is not None and left.type == "identifier":
            op = next((_t(c) for c in e.children if not c.is_named), "+=")
            entries = (
                _literal_str_list(e.child_by_field_name("right"))
                if op in ("+=", "|=")
                else None
            )
            if entries is None:
                info.warnings.append(
                    f"{info.module}: __all__ is not a literal list/tuple/set of "
                    "strings — static analysis may be incomplete"
                )
            else:
                _append_all_entries(info, entries, replace=False)
        else:
            info.warnings.append(
                f"{info.module}: __all__ subscript mutation "
                "— static analysis may be incomplete"
            )
        return b""
    if e.type != "assignment":
        return None
    chain: list[tuple[Node, Node]] = []
    cur: Node | None = e
    while cur is not None and cur.type == "assignment":
        lhs = cur.child_by_field_name("left")
        if lhs is None:
            break
        chain.append((cur, lhs))
        cur = cur.child_by_field_name("right")
    rhs = cur
    has_all = any(
        _is_all_target(lhs) or "__all__" in (_lhs_names(lhs) or []) for _, lhs in chain
    )
    if not has_all:
        return None
    if len(chain) == 1 and _is_all_target(chain[0][1]):
        lhs = chain[0][1]
        if lhs.type == "subscript":
            info.warnings.append(
                f"{info.module}: __all__ subscript mutation "
                "— static analysis may be incomplete"
            )
            return b""
        if rhs is None:
            return b""
        entries = _literal_str_list(rhs)
        if entries is None:
            info.warnings.append(
                f"{info.module}: __all__ is not a literal list/tuple/set of strings "
                "— static analysis may be incomplete"
            )
            return b""
        if in_compound:
            info.warnings.append(
                f"{info.module}: conditional __all__ — merged statically, "
                "verify the exported name list"
            )
            _append_all_entries(info, entries, replace=False)
        else:
            _append_all_entries(info, entries, replace=True)
        return b""
    if (
        any(_is_all_target(lhs) and lhs.type == "identifier" for _, lhs in chain)
        and rhs is not None
    ):
        entries = _literal_str_list(rhs)
        if entries is not None:
            _append_all_entries(info, entries, replace=not in_compound)
        else:
            info.warnings.append(
                f"{info.module}: __all__ is not a literal list/tuple/set of strings "
                "— static analysis may be incomplete"
            )
    elif len(chain) == 1 and rhs is not None:
        # Unpacking like ``__all__, OTHER = ["a"], 1``: harvest literal list/tuple.
        lhs_list = _lhs_names(chain[0][1]) or []
        if rhs.type in ("expression_list", "tuple", "list") and len(
            rhs.named_children
        ) == len(lhs_list):
            for nm, val_node in zip(lhs_list, rhs.named_children):
                if nm == "__all__":
                    entries = _literal_str_list(val_node)
                    if entries is not None:
                        _append_all_entries(info, entries, replace=not in_compound)
    info.warnings.append(f"{info.module}: __all__ assignment target skipped in output")
    kept_segs: list[bytes] = []
    used_discard_tmp = False
    for idx, (assign_node, lhs) in enumerate(chain):
        if _is_all_target(lhs):
            continue
        next_start = (
            chain[idx + 1][0].start_byte
            if idx + 1 < len(chain)
            else (rhs.start_byte if rhs is not None else assign_node.end_byte)
        )
        seg_bytes = src[assign_node.start_byte : next_start]
        if "__all__" in (_lhs_names(lhs) or []):
            seg_bytes = _replace_all_ident_in_slice(
                lhs, assign_node.start_byte, seg_bytes
            )
            used_discard_tmp = True
        kept_segs.append(seg_bytes)
    if not kept_segs:
        return b""
    rhs_bytes = src[rhs.start_byte : e.end_byte] if rhs is not None else b""
    out_bytes = b"".join(kept_segs) + rhs_bytes
    if used_discard_tmp:
        ls = src.rfind(b"\n", 0, stmt.start_byte) + 1
        indent = src[ls : stmt.start_byte]
        if indent.strip():
            indent = b""
        out_bytes += b"\n" + indent + f"del {_DISCARD_ALL_TMP}".encode("utf-8")
    return out_bytes


def _clean_compound_node(node: Node, src: bytes, info: ModuleInfo) -> bytes | None:
    """Harvest and strip ``__all__`` statements inside a compound statement.

    Args:
        node: Compound statement node (``if``, ``try``, ``with``, ...).
        src: Source bytes ``node`` belongs to.
        info: Module metadata to update.

    Returns:
        ``None`` when nothing inside ``node`` touched ``__all__``; ``b""`` when
        every branch inside ``node`` only touched ``__all__``; otherwise the
        rewritten compound statement bytes.

    """
    if b"__all__" not in src[node.start_byte : node.end_byte]:
        return None
    suites: list[Node] = []
    stack: list[Node] = [node]
    while stack:
        cur = stack.pop()
        for ch in reversed(cur.named_children):
            if ch.type == "block":
                suites.append(ch)
            elif ch.type in _CLAUSE_TYPES and ch.type != "block":
                stack.append(ch)
    if not suites:
        return None
    edits: list[tuple[int, int, bytes]] = []
    changed = False
    any_real_code = False
    for blk in suites:
        stmts = [c for c in blk.named_children if c.type != "comment"]
        if not stmts:
            continue
        results: list[tuple[Node, bytes | None]] = []
        for st in stmts:
            if st.type in _COMPOUND:
                res = _clean_compound_node(st, src, info)
            else:
                res = _rewrite_all_statement(st, src, info, in_compound=True)
            if res is not None:
                changed = True
            results.append((st, res))
        if all(
            res == b"" or (res is None and st.type == "pass_statement")
            for st, res in results
        ):
            if any(res == b"" for _, res in results):
                # Suite had only __all__: replace with pass when a sibling survives
                edits.append((stmts[0].start_byte, stmts[-1].end_byte, b"pass"))
            continue
        any_real_code = True
        for st, res in results:
            if res is None:
                continue
            if res == b"":
                # Remove full line if standalone on its line, otherwise statement span
                ls = src.rfind(b"\n", 0, st.start_byte)
                le = src.find(b"\n", st.end_byte)
                before_on_line = src[ls + 1 if ls != -1 else 0 : st.start_byte]
                after_on_line = src[st.end_byte : le if le != -1 else len(src)]
                if (
                    ls != -1
                    and not before_on_line.strip()
                    and (
                        not after_on_line.strip()
                        or after_on_line.lstrip().startswith(b"#")
                    )
                ):
                    edits.append((ls, le if le != -1 else st.end_byte, b""))
                else:
                    edits.append((st.start_byte, st.end_byte, b"pass"))
            else:
                edits.append((st.start_byte, st.end_byte, res))
    if not changed:
        return None
    if not any_real_code:
        return b""
    edits.sort(key=lambda item: item[0])
    out: list[bytes] = []
    pos = node.start_byte
    for s, e, repl in edits:
        if s < pos:
            continue
        out.append(src[pos:s])
        out.append(repl)
        pos = e
    out.append(src[pos : node.end_byte])
    return b"".join(out)


def _handle_all_statement(node: Node, info: ModuleInfo) -> bool:
    """Backward-compatible wrapper around :func:`_rewrite_all_statement`."""
    stmt = node.parent if node.parent is not None else node
    src = stmt.text or b""
    # Re-parse single statement when called standalone so byte offsets are local
    parsed = _get_parser().parse(src).root_node
    if not parsed.named_children:
        return False
    return _rewrite_all_statement(parsed.named_children[0], src, info) == b""


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


def _can_merge_blocks(target: Block, block: Block, existing: list[Block]) -> bool:
    """Return whether ``block`` can safely be merged up into ``target``.

    Merging moves ``block`` to ``target``'s position. That is only safe when no
    intervening block observes the pre-reassignment value or defines a symbol
    referenced by ``block``.
    """
    try:
        idx = existing.index(target)
    except ValueError:
        return False
    intervening = existing[idx + 1 :]
    if not intervening:
        return True
    shared = set(target.names) | set(block.names)
    intervening_defs = {nm for ib in intervening for nm in ib.names}
    if target.kind == FUNC and block.kind == FUNC:
        for ib in intervening:
            if any(o.kind == "name" and o.eager and o.name in shared for o in ib.occs):
                return False
        if any(
            o.kind == "name" and o.eager and o.name in intervening_defs
            for o in block.occs
        ):
            return False
        return True
    if block.kind == MISC and not block.names:
        return False
    for ib in intervening:
        if ib.kind == MISC and not ib.names:
            return False
        if any(o.kind == "name" and o.name in shared for o in ib.occs):
            return False
    if any(o.kind == "name" and o.name in intervening_defs for o in block.occs):
        return False
    return True


def _extract_tc_suite(node: Node, src: bytes, info: ModuleInfo) -> None:
    """Extract imports and declarations from an ``if TYPE_CHECKING:`` suite."""
    body = node.child_by_field_name("consequence") or node.child_by_field_name("body")
    if body is None:
        return
    for stmt in body.named_children:
        if stmt.type in ("comment", "pass_statement"):
            continue
        if stmt.type in _IMPORT_TYPES:
            for spec in parse_import(stmt, info.module, info.is_pkg):
                info.imports.append(
                    ImportSpec(
                        spec.module,
                        spec.name,
                        spec.alias,
                        spec.is_from,
                        spec.star,
                        spec.relative,
                        type_checking=True,
                    )
                )
            continue
        rewritten = (
            _clean_compound_node(stmt, src, info)
            if stmt.type in _COMPOUND
            else _rewrite_all_statement(stmt, src, info, in_compound=True)
        )
        if rewritten == b"":
            continue
        if rewritten is not None:
            raw_text = rewritten.decode("utf-8", "replace")
        else:
            s_start, s_end = _extent(stmt)
            raw_text = src[s_start:s_end].decode("utf-8", "replace")
        dedented = textwrap.dedent(raw_text).strip("\n")
        if not dedented.strip():
            continue
        dedented_bytes = dedented.encode("utf-8")
        sub_root = _get_parser().parse(dedented_bytes).root_node
        if not sub_root.named_children:
            continue
        sub_node = next(
            (c for c in sub_root.named_children if c.type != "comment"),
            sub_root.named_children[0],
        )
        own_sub: Node | None = None
        tc_names: list[str] = []
        tc_kinds: dict[str, str] = {}
        if sub_node.type in _DEF_TYPES:
            own_sub = (
                sub_node.child_by_field_name("definition")
                if sub_node.type == "decorated_definition"
                else sub_node
            )
            if own_sub is not None:
                nm = _t(own_sub.child_by_field_name("name"))
                if nm and nm != "__all__":
                    tc_names = [nm]
                    tc_kinds[nm] = CLASS if own_sub.type == "class_definition" else FUNC
        elif sub_node.type == "type_alias_statement":
            nm = _first_identifier(sub_node)
            if nm and nm != "__all__":
                tc_names = [nm]
                tc_kinds[nm] = CONST
        elif sub_node.type == "expression_statement" and sub_node.named_children:
            expr = sub_node.named_children[0]
            if expr.type in ("assignment", "augmented_assignment"):
                tc_names = [
                    nm
                    for nm in (_target_names(expr) or [])
                    if nm not in ("__all__", _DISCARD_ALL_TMP)
                ]
                for nm in tc_names:
                    tc_kinds[nm] = CONST
        elif sub_node.type in _COMPOUND:
            tc_names, tc_kinds, _ = _compound_defs(sub_node)
        occs = _collect_occs(sub_root, 0, dedented_bytes, own_sub, info)
        blk = Block(
            info.module,
            MISC,
            tc_names,
            dedented_bytes,
            occs,
            len(info.tc_blocks),
            orig_path=info.path,
            start_line=stmt.start_point[0] + 1,
            end_line=stmt.end_point[0] + 1,
            name_kinds=tc_kinds,
            binds_value=False,
        )
        info.tc_blocks.append(blk)


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
    deleted_at_end: set[str] = set()

    def add(
        kind: str,
        names: list[str],
        node: Node,
        own: Node | None = None,
        *,
        custom_src: bytes | None = None,
        name_kinds: dict[str, str] | None = None,
        binds_value: bool = True,
        orig_node: Node | None = None,
    ) -> None:
        """Append a block, merging it into an existing block when safe."""
        loc_node = orig_node if orig_node is not None else node
        start_line = loc_node.start_point[0] + 1
        end_line = loc_node.end_point[0] + 1
        if custom_src is not None:
            raw_source = custom_src
            occs = _collect_occs(node, 0, raw_source, own, info)
        else:
            start, end = _extent(node)
            raw_source = src[start:end]
            occs = _collect_occs(node, start, src, own, info)
        clean_names = [
            nm for nm in names if nm and nm not in ("__all__", _DISCARD_ALL_TMP)
        ]
        if binds_value:
            for nm in clean_names:
                deleted_at_end.discard(nm)
        block = Block(
            info.module,
            kind,
            clean_names,
            raw_source,
            occs,
            len(info.blocks),
            orig_path=info.path,
            start_line=start_line,
            end_line=end_line,
            name_kinds=dict(name_kinds or {}),
            binds_value=binds_value,
        )
        target = next((by_name[nm] for nm in clean_names if nm in by_name), None)
        if target is not None and _can_merge_blocks(target, block, info.blocks):
            target.merge(block)
            for nm in clean_names:
                by_name.setdefault(nm, target)
            return
        info.blocks.append(block)
        for nm in clean_names:
            by_name[nm] = block

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
            nm = _t(d.child_by_field_name("name"))
            add(kind, [nm], n, own=d, name_kinds={nm: kind})
        elif t == "expression_statement":
            e = n.named_children[0] if n.named_children else None
            if e is None or (is_first and e.type == "string"):
                continue
            rewritten = _rewrite_all_statement(n, src, info)
            if rewritten == b"":
                continue
            if rewritten is not None:
                rep_root = _get_parser().parse(rewritten).root_node
                first_stmt = next(
                    (c for c in rep_root.named_children if c.type != "comment"),
                    None,
                )
                rep_expr = (
                    first_stmt.named_children[0]
                    if (
                        first_stmt is not None
                        and first_stmt.type == "expression_statement"
                        and first_stmt.named_children
                    )
                    else None
                )
                names = (
                    [
                        nm
                        for nm in (_target_names(rep_expr) or [])
                        if nm not in ("__all__", _DISCARD_ALL_TMP)
                    ]
                    if rep_expr is not None
                    and rep_expr.type in ("assignment", "augmented_assignment")
                    else []
                )
                if names:
                    add(
                        CONST,
                        names,
                        rep_root,
                        custom_src=rewritten,
                        name_kinds={nm: CONST for nm in names},
                        orig_node=n,
                    )
                else:
                    add(MISC, [], rep_root, custom_src=rewritten, orig_node=n)
                continue
            if e.type in ("assignment", "augmented_assignment"):
                names = [
                    nm
                    for nm in (_target_names(e) or [])
                    if nm not in ("__all__", _DISCARD_ALL_TMP)
                ]
                has_rhs = (
                    e.type == "augmented_assignment"
                    or e.child_by_field_name("right") is not None
                )
                if names:
                    add(
                        CONST,
                        names,
                        n,
                        name_kinds={nm: CONST for nm in names},
                        binds_value=has_rhs,
                    )
                    continue
            add(MISC, [], n)
        elif t == "type_alias_statement":
            rewritten = _rewrite_all_statement(n, src, info)
            if rewritten == b"":
                continue
            nm = _first_identifier(n)
            add(CONST, [nm], n, name_kinds={nm: CONST})
        elif t == "delete_statement":
            for ch in n.named_children:
                items = ch.named_children if ch.type == "expression_list" else [ch]
                for item in items:
                    if item.type == "identifier":
                        nm_del = _t(item)
                        if nm_del and nm_del != "__all__":
                            deleted_at_end.add(nm_del)
                            by_name.pop(nm_del, None)
            rewritten = _rewrite_all_statement(n, src, info)
            if rewritten == b"":
                continue
            if rewritten is not None:
                rep_root = _get_parser().parse(rewritten).root_node
                add(MISC, [], rep_root, custom_src=rewritten, orig_node=n)
            else:
                add(MISC, [], n)
        elif t in _COMPOUND:
            if t == "if_statement":
                cond = _t(n.child_by_field_name("condition")).strip()
                if _MAIN_RE.match(cond):
                    add(MAIN, [], n)
                    continue
                has_alt = any(
                    c.type in ("elif_clause", "else_clause") for c in n.children
                )
                if _TC_RE.match(cond) and not has_alt:
                    _extract_tc_suite(n, src, info)
                    continue
            cleaned = _clean_compound_node(n, src, info)
            if cleaned == b"":
                continue
            cur_node = n
            cur_src = src
            custom_bytes: bytes | None = None
            if cleaned is not None:
                custom_bytes = cleaned
                cur_src = cleaned
                rep_root = _get_parser().parse(cleaned).root_node
                cur_node = next(
                    (c for c in rep_root.named_children if c.type != "comment"),
                    rep_root,
                )
            imports = _scan_module_imports(cur_node)
            imported_locals = {
                spec.local_name
                for imp in imports
                for spec in parse_import(imp, info.module, info.is_pkg)
                if spec.local_name
            }
            bound_names, name_kinds, has_bound_val = _compound_defs(cur_node)
            real_defs = [
                nm
                for nm in bound_names
                if name_kinds.get(nm) in (CLASS, FUNC) or nm not in imported_locals
            ]
            if cur_node.type == "if_statement":
                cond = _t(cur_node.child_by_field_name("condition")).strip()
                if imports and (
                    "sys.platform" in cond
                    or "sys.version" in cond
                    or "TYPE_CHECKING" in cond
                ):
                    info.warnings.append(
                        f"{info.module}: conditional import guarded by '{cond.strip()}' kept verbatim"
                    )
            if imports and not real_defs:
                info.guards.append(_guard_from_node(cur_node, cur_src, info))
            else:
                def_kinds = {nm: name_kinds[nm] for nm in real_defs if nm in name_kinds}
                add(
                    MISC,
                    real_defs,
                    cur_node,
                    custom_src=custom_bytes,
                    name_kinds=def_kinds,
                    binds_value=(has_bound_val or not real_defs),
                    orig_node=n,
                )
        elif t == "pass_statement":
            continue
        else:
            add(MISC, [], n)
    info.deleted_names = deleted_at_end
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
                    for b in info.blocks + info.tc_blocks:
                        b.source = _strip_docstring_from_source(b.source)
                        b.stripped_docstring = True
                if strip_comments or not keep_comments:
                    for b in info.blocks + info.tc_blocks:
                        b.source = _strip_comments_from_source(b.source)
                return info
            else:
                info.errors.append(msg)
                return info
        _extract(info, root, src)
        # Post-process per flags
        if not keep_docstrings:
            for b in info.blocks + info.tc_blocks:
                b.source = _strip_docstring_from_source(b.source)
                b.stripped_docstring = True
        if strip_comments or not keep_comments:
            for b in info.blocks + info.tc_blocks:
                b.source = _strip_comments_from_source(b.source)
    except Exception as exc:  # noqa: BLE001 - reported to the caller, never crashes a worker
        info.errors.append(f"{path_str}: extraction failed: {exc!r}")
    return info
