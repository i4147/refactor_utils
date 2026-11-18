from __future__ import annotations

import ast
import re
from pathlib import Path

import libcst as cst
import libcst.metadata as md

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
    Ref,
)

_MAIN_RE = re.compile(r"""^(?:__name__\s*==\s*(['"])__main__\1|(['"])__main__\2\s*==\s*__name__)$""")
_TC_NAMES = {
    "TYPE_CHECKING",
    "typing.TYPE_CHECKING",
    "t.TYPE_CHECKING",
    "typing_extensions.TYPE_CHECKING",
}
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
_STR_RE = re.compile(r"""['"]([A-Za-z_][A-Za-z0-9_]*)['"]""")
_COMPOUND = (
    cst.If,
    cst.Try,
    cst.For,
    cst.While,
    cst.With,
    cst.Match,
)


def render_stmt(node: cst.CSTNode) -> str:
    return cst.Module(body=[node]).code


def render_expr(node: cst.Expression) -> str:
    return render_stmt(cst.SimpleStatementLine(body=[cst.Expr(value=node)])).strip()


def _dotted(node) -> str | None:
    if isinstance(node, cst.Name):
        return node.value
    if isinstance(node, cst.Attribute):
        left = _dotted(node.value)
        return f"{left}.{node.attr.value}" if left else None
    return None


def _resolve_relative(current: str, is_pkg: bool, level: int, rest: str) -> str:
    parts = current.split(".") if is_pkg else current.split(".")[:-1]
    strip = level - 1
    base = [] if strip > len(parts) else parts[: len(parts) - strip]
    return ".".join(base + ([rest] if rest else []))


def parse_import(stmt: cst.CSTNode, module: str, is_pkg: bool, type_checking: bool = False) -> list[ImportSpec]:
    if isinstance(stmt, cst.Import):
        specs = []
        for alias in stmt.names:
            name = _dotted(alias.name) or ""
            alias_str = alias.asname.name.value if alias.asname else None
            specs.append(ImportSpec(name, alias=alias_str, type_checking=type_checking))
        return specs
    if isinstance(stmt, cst.ImportFrom):
        level = len(stmt.relative)
        rest = _dotted(stmt.module) if stmt.module is not None else ""
        mod = _resolve_relative(module, is_pkg, level, rest) if level else rest
        if isinstance(stmt.names, cst.ImportStar):
            return [
                ImportSpec(
                    mod,
                    is_from=True,
                    star=True,
                    relative=bool(level),
                    type_checking=type_checking,
                )
            ]
        specs = []
        for alias in stmt.names:
            name = _dotted(alias.name) or ""
            alias_str = alias.asname.name.value if alias.asname else None
            specs.append(
                ImportSpec(
                    mod,
                    name=name,
                    alias=alias_str,
                    is_from=True,
                    relative=bool(level),
                    type_checking=type_checking,
                )
            )
        return specs
    return []


def _is_bare_string(value) -> bool:
    if isinstance(value, cst.SimpleString):
        return True
    if isinstance(value, cst.FormattedString):
        return not any(isinstance(p, cst.FormattedStringExpression) for p in value.parts)
    if isinstance(value, cst.ConcatenatedString):
        return _is_bare_string(value.left) and _is_bare_string(value.right)
    return False


def _param_names(params: cst.Parameters) -> set[str]:
    names: set[str] = set()
    for group in (params.params, params.posonly_params, params.kwonly_params):
        for p in group:
            names.add(p.name.value)
    for p in (params.star_arg, params.star_kwarg):
        if isinstance(p, cst.Param):
            names.add(p.name.value)
    return names


def _lhs_names(node) -> list[str] | None:
    if isinstance(node, cst.Name):
        return [node.value]
    if isinstance(node, (cst.Tuple, cst.List)):
        out: list[str] = []
        for element in node.elements:
            inner = element.value if isinstance(element, cst.Element) else element
            got = _lhs_names(inner)
            if got is None:
                return None
            out += got
        return out
    if isinstance(node, cst.StarredElement):
        return _lhs_names(node.value)
    return None


def _target_names(stmt) -> list[str] | None:
    if isinstance(stmt, cst.Assign):
        names: list[str] = []
        for target in stmt.targets:
            got = _lhs_names(target.target)
            if got is None:
                return None
            names += got
        return names
    if isinstance(stmt, (cst.AugAssign, cst.AnnAssign)):
        return _lhs_names(stmt.target)
    return None


def _name_ids(node) -> set[int]:
    out: set[int] = set()
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, cst.Name):
            out.add(id(cur))
        for child in cur.children:
            stack.append(child)
    return out


def _match_binding_ids(pattern) -> set[int]:
    out: set[int] = set()
    stack = [pattern]
    while stack:
        cur = stack.pop()
        if isinstance(cur, (cst.MatchAs, cst.MatchStar)):
            if cur.name is not None:
                out.add(id(cur.name))
        elif isinstance(cur, cst.MatchMapping):
            if cur.rest is not None:
                out.add(id(cur.rest))
        for child in cur.children:
            stack.append(child)
    return out


def _scan_imports_defs(node) -> tuple[list, bool]:
    imports: list = []
    has_def = False
    stack = [node]
    while stack:
        cur = stack.pop()
        for child in cur.children:
            if isinstance(child, (cst.Import, cst.ImportFrom)):
                imports.append(child)
            elif isinstance(child, (cst.FunctionDef, cst.ClassDef)):
                has_def = True
            else:
                stack.append(child)
    return imports, has_def


def _bound_names(node) -> list[str]:
    names: list[str] = []
    stack = [node]
    while stack:
        cur = stack.pop()
        for child in cur.children:
            if isinstance(child, (cst.FunctionDef, cst.ClassDef)):
                names.append(child.name.value)
            elif isinstance(child, (cst.Assign, cst.AugAssign, cst.AnnAssign)):
                got = _target_names(child)
                if got:
                    names += got
            else:
                stack.append(child)
    return list(dict.fromkeys(names))


def _string_literals(node) -> list[str]:
    found: list[str] = []
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, cst.SimpleString):
            try:
                lit = ast.literal_eval(cur.value)
                if isinstance(lit, str):
                    found.append(lit)
            except (ValueError, SyntaxError):
                pass
        for child in cur.children:
            stack.append(child)
    return found


def _collect_all_stmt(small, info: ModuleInfo) -> bool:
    if isinstance(small, cst.Assign) and len(small.targets) == 1:
        target = small.targets[0].target
        if isinstance(target, cst.Name) and target.value == "__all__":
            info.declared_all = (info.declared_all or []) + _string_literals(small.value)
            return True
        return False
    if isinstance(small, cst.Expr) and isinstance(small.value, cst.Call):
        func = small.value.func
        if isinstance(func, cst.Attribute) and func.attr.value in _ALL_METHODS:
            if _dotted(func.value) == "__all__":
                found: list[str] = []
                for arg in small.value.args:
                    found += _string_literals(arg.value)
                info.declared_all = (info.declared_all or []) + found
                info.warnings.append(
                    f"{info.module}: mutated __all__ ('{func.attr.value}') — static analysis "
                    "may be incomplete; exported names are recomputed from definitions"
                )
                return True
    return False


class _DocstringStripper(cst.CSTTransformer):
    @staticmethod
    def _is_doc_line(stmt) -> bool:
        return (
            isinstance(stmt, cst.SimpleStatementLine)
            and len(stmt.body) == 1
            and isinstance(stmt.body[0], cst.Expr)
            and _is_bare_string(stmt.body[0].value)
        )

    def _strip_stmts(self, stmts):
        if stmts and self._is_doc_line(stmts[0]):
            return stmts[1:]
        return stmts

    def leave_Module(self, original_node, updated_node):
        return updated_node.with_changes(body=self._strip_stmts(updated_node.body))

    def _leave_suite_owner(self, updated_node):
        body = updated_node.body
        if isinstance(body, cst.IndentedBlock):
            stmts = self._strip_stmts(body.body)
            if stmts is not body.body:
                if not stmts:
                    stmts = [cst.SimpleStatementLine(body=[cst.Pass()])]
                return updated_node.with_changes(body=body.with_changes(body=stmts))
        elif isinstance(body, cst.SimpleStatementSuite):
            smalls = body.body
            if smalls and isinstance(smalls[0], cst.Expr) and _is_bare_string(smalls[0].value):
                smalls = smalls[1:] or [cst.Pass()]
                return updated_node.with_changes(body=body.with_changes(body=smalls))
        return updated_node

    def leave_ClassDef(self, original_node, updated_node):
        return self._leave_suite_owner(updated_node)

    def leave_FunctionDef(self, original_node, updated_node):
        return self._leave_suite_owner(updated_node)


class _CommentStripper(cst.CSTTransformer):
    def leave_EmptyLine(self, original_node, updated_node):
        if updated_node.comment is not None:
            return cst.RemovalSentinel.REMOVE
        return updated_node

    def leave_TrailingWhitespace(self, original_node, updated_node):
        if updated_node.comment is not None:
            return updated_node.with_changes(comment=None, whitespace=cst.SimpleWhitespace(""))
        return updated_node


class _Ctx:
    __slots__ = ("lazy", "shadow", "cls")

    def __init__(self, lazy: bool, shadow: frozenset, cls: bool):
        self.lazy = lazy
        self.shadow = shadow
        self.cls = cls


class RefWalker(cst.CSTTransformer):
    def __init__(
        self,
        module: str,
        is_pkg: bool,
        collect: bool,
        info: ModuleInfo | None = None,
        resolver=None,
        env: dict | None = None,
        warned: set | None = None,
    ):
        self.module = module
        self.is_pkg = is_pkg
        self.collect = collect
        self.info = info
        self.resolver = resolver
        self.env = env or {}
        self.warned = warned if warned is not None else set()
        self.refs: list[Ref] = []
        self._skip: set[int] = set()
        self._ctxs: list[_Ctx] = [_Ctx(False, frozenset(), False)]
        self._frames: list[dict] = []
        self._cls_pops: list[int] = []

    def visit_FunctionDef(self, node) -> bool:
        self._skip.add(id(node.name))
        self._frames.append(
            {
                "kind": "func",
                "params": None,
                "scope": False,
                "base_shadow": self._ctxs[-1].shadow,
            }
        )
        return True

    def _maybe_rename_def_name(self, original_node, updated_node):
        if self.collect:
            return updated_node
        name = original_node.name.value
        target = self.env.get(name)
        if target is None:
            return updated_node
        final = self.resolver._target_name(target, name)
        if final is None or final == name:
            return updated_node
        return updated_node.with_changes(name=cst.Name(final))

    def leave_FunctionDef(self, original_node, updated_node):
        frame = self._frames.pop()
        if frame["scope"]:
            self._ctxs.pop()
        return self._maybe_rename_def_name(original_node, updated_node)

    def visit_Parameters(self, node) -> bool:
        if self._frames and self._frames[-1]["kind"] == "func":
            frame = self._frames[-1]
            if frame["params"] is None:
                frame["params"] = frozenset(_param_names(node))
        for group in (node.params, node.posonly_params, node.kwonly_params):
            for p in group:
                self._skip.add(id(p.name))
        for p in (node.star_arg, node.star_kwarg):
            if isinstance(p, cst.Param):
                self._skip.add(id(p.name))
        return True

    def _push_func_scope(self) -> bool:
        if self._frames and self._frames[-1]["kind"] == "func":
            frame = self._frames[-1]
            if not frame["scope"]:
                shadow = frame["base_shadow"] | (frame["params"] or frozenset())
                self._ctxs.append(_Ctx(True, frozenset(shadow), False))
                frame["scope"] = True
                return True
        return False

    def visit_IndentedBlock(self, node) -> bool:
        self._push_func_scope()
        return True

    def visit_SimpleStatementSuite(self, node) -> bool:
        self._push_func_scope()
        return True

    def visit_ClassDef(self, node) -> bool:
        self._skip.add(id(node.name))
        cur = self._ctxs[-1]
        self._ctxs.append(_Ctx(cur.lazy, cur.shadow, True))
        self._cls_pops.append(id(node))
        return True

    def leave_ClassDef(self, original_node, updated_node):
        if self._cls_pops and self._cls_pops[-1] == id(original_node):
            self._cls_pops.pop()
            self._ctxs.pop()
        return self._maybe_rename_def_name(original_node, updated_node)

    def visit_Lambda(self, node) -> bool:
        cur = self._ctxs[-1]
        shadow = cur.shadow | frozenset(_param_names(node.params))
        self._ctxs.append(_Ctx(True, frozenset(shadow), False))
        self._frames.append({"kind": "lambda", "scope": True})
        return True

    def leave_Lambda(self, original_node, updated_node):
        self._frames.pop()
        self._ctxs.pop()
        return updated_node

    def visit_ExceptHandler(self, node) -> bool:
        if node.name is not None:
            self._skip.add(id(node.name))
        return True

    def visit_For(self, node) -> bool:
        self._skip |= _name_ids(node.target)
        return True

    def visit_With(self, node) -> bool:
        for item in node.items:
            if item.asname is not None:
                self._skip.add(id(item.asname.name))
        return True

    def visit_NamedExpr(self, node) -> bool:
        self._skip.add(id(node.target))
        return True

    def visit_MatchCase(self, node) -> bool:
        self._skip |= _match_binding_ids(node.pattern)
        return True

    def visit_Assign(self, node) -> bool:
        if self._ctxs[-1].cls:
            for target in node.targets:
                self._skip |= _name_ids(target.target)
        return True

    def visit_AnnAssign(self, node) -> bool:
        if self._ctxs[-1].cls:
            self._skip |= _name_ids(node.target)
        return True

    visit_AugAssign = visit_AnnAssign

    def visit_Attribute(self, node) -> bool:
        self._skip.add(id(node.attr))
        return True

    def visit_Arg(self, node) -> bool:
        if node.keyword is not None:
            self._skip.add(id(node.keyword))
        return True

    def visit_Import(self, node) -> bool:
        self._skip |= _name_ids(node)
        return True

    def visit_ImportFrom(self, node) -> bool:
        self._skip |= _name_ids(node)
        return True

    def leave_Name(self, original_node, updated_node):
        if id(original_node) in self._skip:
            return updated_node
        name = original_node.value
        ctx = self._ctxs[-1]
        if name in ctx.shadow:
            return updated_node
        if name == "__file__" and self.collect and "file" not in self.warned:
            self.warned.add("file")
            self.info.warnings.append(f"{self.module}: uses __file__, which now points at the consolidated file")
        if self.collect:
            self.refs.append(Ref(name, "name", eager=not ctx.lazy))
            return updated_node
        target = self.env.get(name)
        if target is None:
            return updated_node
        final = self.resolver._target_name(target, name)
        if final is None or final == name:
            return updated_node
        return updated_node.with_changes(value=final)

    def leave_Attribute(self, original_node, updated_node):
        chain = _dotted(original_node.value)
        if chain is None:
            return updated_node
        ctx = self._ctxs[-1]
        if chain.split(".")[0] in ctx.shadow:
            return updated_node
        attr = original_node.attr.value
        if self.collect:
            self.refs.append(Ref(attr, "attr", eager=not ctx.lazy, obj=chain))
            return updated_node
        target = self.resolver._resolve_dotted(self.module, chain, attr)
        if target is None:
            return updated_node
        final = self.resolver._target_name(target, None)
        if final is None:
            return updated_node
        return updated_node.with_changes(attr=cst.Name(final))

    def _nested_ref(self, original_node) -> None:
        specs = tuple(parse_import(original_node, self.module, self.is_pkg))
        self.refs.append(Ref("", "nested_import", specs=specs))

    def leave_Import(self, original_node, updated_node):
        if self.collect:
            self._nested_ref(original_node)
            return updated_node
        specs = parse_import(original_node, self.module, self.is_pkg)
        if not any(self.resolver._is_internal(s) for s in specs):
            return updated_node
        kept = [alias for alias, spec in zip(updated_node.names, specs) if not self.resolver._is_internal(spec)]
        return cst.Import(names=kept) if kept else cst.Pass()

    def leave_ImportFrom(self, original_node, updated_node):
        if self.collect:
            self._nested_ref(original_node)
            return updated_node
        specs = parse_import(original_node, self.module, self.is_pkg)
        if not any(self.resolver._is_internal(s) for s in specs):
            return updated_node
        return cst.Pass()


def _detect_dynamic_patterns(text: str, info: ModuleInfo) -> None:
    for name, pat in _DYNAMIC_PATTERNS.items():
        if pat.search(text):
            if name in ("importlib", "__import__"):
                info.warnings.append(
                    f"{info.module}: uses {name} for dynamic imports — consolidation may miss dependencies"
                )
            elif name in ("globals", "locals", "eval", "exec", "getattr"):
                info.warnings.append(f"{info.module}: uses {name}() — name resolution may be incomplete")
            elif name in ("__getattr__", "__dir__", "monkeypatch"):
                info.warnings.append(
                    f"{info.module}: defines/uses {name} — module-level attribute access may change semantics"
                )


def _collect_block_refs(info, node, warned) -> list[Ref]:
    walker = RefWalker(info.module, info.is_pkg, collect=True, info=info, warned=warned)
    node.visit(walker)
    return walker.refs


def _make_block(info, by_name, warned, kind, names, node, start, end) -> None:
    block = Block(info.module, kind, list(names), [node])
    block.order = len(info.blocks)
    block.orig_path = info.path
    block.start_line = start
    block.end_line = end
    block.refs = _collect_block_refs(info, node, warned)
    target = next((by_name[n] for n in names if n in by_name), None)
    if target is not None:
        target.merge(block)
        for n in names:
            by_name.setdefault(n, target)
        return
    info.blocks.append(block)
    for n in names:
        by_name[n] = block


def _add_compound(info, by_name, warned, stmt, start, end) -> None:
    code = render_stmt(stmt)
    imports, has_def = _scan_imports_defs(stmt)
    if imports and not has_def:
        specs = [spec for imp in imports for spec in parse_import(imp, info.module, info.is_pkg)]
        info.guards.append(GuardBlock(code, specs, _bound_names(stmt), True))
        return
    info.warnings.append(
        f"{info.module}: module-level {type(stmt).__name__} "
        "combines imports and definitions — emitted verbatim, "
        "names bound there are not rewritten"
    )
    _make_block(info, by_name, warned, MISC, _bound_names(stmt), stmt, start, end)


def _add_simple(info, by_name, warned, small, start, end, lead, trailing) -> None:
    if isinstance(small, (cst.Import, cst.ImportFrom)):
        info.imports.extend(parse_import(small, info.module, info.is_pkg))
        return
    if isinstance(small, cst.Expr) and _collect_all_stmt(small, info):
        return
    if isinstance(small, (cst.Assign, cst.AugAssign, cst.AnnAssign)):
        if _collect_all_stmt(small, info):
            return
        names = _target_names(small)
        if names:
            node = cst.SimpleStatementLine(
                body=[small],
                leading_lines=lead,
                trailing_whitespace=trailing,
            )
            _make_block(info, by_name, warned, CONST, names, node, start, end)
            return
    if isinstance(small, cst.Expr) and _is_bare_string(small.value):
        return
    if isinstance(small, cst.Pass):
        return
    node = cst.SimpleStatementLine(
        body=[small],
        leading_lines=lead,
        trailing_whitespace=trailing,
    )
    _make_block(info, by_name, warned, MISC, [], node, start, end)


def _add_type_checking(info, by_name, warned, node, positions) -> None:
    if isinstance(node.body, cst.IndentedBlock):
        body_start = positions[node.body].start.line
        body_stmts = list(node.body.body)
    else:
        body_start = positions[node].start.line
        body_stmts = [cst.SimpleStatementLine(body=[s]) for s in node.body.body]
    for s in body_stmts:
        code = render_stmt(s)
        nlines = code.count("\n")
        start = body_start
        body_start += nlines
        end = body_start - 1
        smalls = s.body if isinstance(s, cst.SimpleStatementLine) else None
        if smalls is not None:
            for small in smalls:
                if isinstance(small, (cst.Import, cst.ImportFrom)):
                    info.imports.extend(parse_import(small, info.module, info.is_pkg, type_checking=True))
                elif isinstance(small, cst.Pass):
                    continue
                elif _collect_all_stmt(small, info):
                    continue
                elif isinstance(small, cst.Expr) and _is_bare_string(small.value):
                    continue
                else:
                    names = _target_names(small) or []
                    kind = CONST if names else MISC
                    wrapped = cst.SimpleStatementLine(
                        body=[small],
                        leading_lines=s.leading_lines,
                        trailing_whitespace=s.trailing_whitespace,
                    )
                    _make_block(info, by_name, warned, kind, names, wrapped, start, end)
            continue
        if isinstance(s, (cst.FunctionDef, cst.ClassDef)):
            kind = CLASS if isinstance(s, cst.ClassDef) else FUNC
            _make_block(info, by_name, warned, kind, [s.name.value], s, start, end)
            continue
        imports, has_def = _scan_imports_defs(s)
        if imports and not has_def:
            specs = [
                spec for imp in imports for spec in parse_import(imp, info.module, info.is_pkg, type_checking=True)
            ]
            info.guards.append(GuardBlock(code, specs, _bound_names(s), True))
            continue
        _make_block(info, by_name, warned, MISC, _bound_names(s), s, start, end)


def _extract(info: ModuleInfo, tree: cst.Module, positions: dict) -> None:
    body = list(tree.body)
    starts = {id(s): positions[s].start.line for s in body}
    if tree.header and body:
        first = body[0]
        body[0] = first.with_changes(leading_lines=(*tree.header, *first.leading_lines))
        starts[id(body[0])] = starts[id(first)]
    by_name: dict[str, Block] = {}
    warned: set = set()

    for stmt in body:
        start = starts[id(stmt)]
        nlines = render_stmt(stmt).count("\n")
        end = start + nlines - 1
        if isinstance(stmt, cst.SimpleStatementLine):
            smalls = stmt.body
            if len(smalls) == 1:
                _add_simple(
                    info,
                    by_name,
                    warned,
                    smalls[0],
                    start,
                    end,
                    stmt.leading_lines,
                    stmt.trailing_whitespace,
                )
            else:
                for i, small in enumerate(smalls):
                    _add_simple(
                        info,
                        by_name,
                        warned,
                        small,
                        start,
                        end,
                        stmt.leading_lines if i == 0 else (),
                        stmt.trailing_whitespace if i == len(smalls) - 1 else None,
                    )
        elif isinstance(stmt, (cst.FunctionDef, cst.ClassDef)):
            kind = CLASS if isinstance(stmt, cst.ClassDef) else FUNC
            _make_block(info, by_name, warned, kind, [stmt.name.value], stmt, start, end)
        elif isinstance(stmt, cst.If):
            cond = render_expr(stmt.test)
            if _MAIN_RE.match(cond):
                _make_block(info, by_name, warned, MAIN, [], stmt, start, end)
                continue
            if cond in _TC_NAMES:
                _add_type_checking(info, by_name, warned, stmt, positions)
                continue
            if "sys.platform" in cond or "sys.version" in cond or "TYPE_CHECKING" in cond:
                info.warnings.append(f"{info.module}: conditional import guarded by '{cond}' kept verbatim")
            _add_compound(info, by_name, warned, stmt, start, end)
        elif isinstance(stmt, _COMPOUND):
            _add_compound(info, by_name, warned, stmt, start, end)
        elif isinstance(stmt, cst.SimpleStatementSuite):
            for small in stmt.body:
                _add_simple(info, by_name, warned, small, start, end, (), None)
        else:
            _make_block(info, by_name, warned, MISC, [], stmt, start, end)
    _detect_dynamic_patterns(tree.code, info)
    _detect_runtime_semantics(info, tree.code)


def _detect_runtime_semantics(info: ModuleInfo, text: str) -> None:
    seen: set[str] = set()
    for block in info.blocks:
        if block.kind == MAIN:
            continue
        for ref in block.refs:
            if ref.kind == "name" and ref.name in (
                "__name__",
                "__package__",
                "__path__",
            ):
                if ref.name not in seen:
                    seen.add(ref.name)
                    info.warnings.append(
                        f"{info.module}: uses {ref.name} — semantics change in consolidated file "
                        "(it now points at the consolidated module)"
                    )
    if "importlib.resources" in text or "pkg_resources" in text or ("open(" in text and "__file__" in text):
        info.warnings.append(
            f"{info.module}: does resource loading relative to __file__ — verify output "
            "handles bundled resources (see --embed-data)"
        )


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
        info.warnings.append(f"{path_str}: contains CRLF line endings — normalized to LF in output")
        src = src.replace(b"\r\n", b"\n")
    try:
        src.decode("utf-8")
    except UnicodeDecodeError:
        info.warnings.append(f"{path_str}: non-UTF8 encoding detected — decoded with replacement characters")
    text = src.decode("utf-8", "replace")
    try:
        tree = cst.parse_module(text)
    except cst.ParserSyntaxError as exc:
        detail = getattr(exc, "message", None) or str(exc)
        info.errors.append(f"{path_str}: syntax error: {detail}")
        return info
    except Exception as exc:
        info.errors.append(f"{path_str}: parse failed: {exc!r}")
        return info
    try:
        if not keep_comments:
            tree = tree.visit(_CommentStripper())
        if not keep_docstrings:
            tree = tree.visit(_DocstringStripper())
        wrapper = md.MetadataWrapper(tree, unsafe_skip_copy=True)
        positions = wrapper.resolve(md.PositionProvider)
        _extract(info, tree, positions)
    except Exception as exc:
        info.errors.append(f"{path_str}: extraction failed: {exc!r}")
    return info
