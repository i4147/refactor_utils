# File: __init__.py
"""refactor_utils: consolidate python packages into a single file."""

from .core import ConsolidationError, ConsolidationResult, consolidate

__version__ = "0.1.0"
__all__ = ["ConsolidationError", "ConsolidationResult", "consolidate", "__version__"]
# File: cli.py
from __future__ import annotations
import argparse
import sys
from pathlib import Path
from loguru import logger
from . import __version__
from .core import ConsolidationError, consolidate
from .logging_setup import setup_logging


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pyrefactor",
        description="Consolidate a python module/package/project into a single, dependency-ordered file.",
    )
    p.add_argument("source", type=Path, help="package directory, project root or single .py file")
    p.add_argument("-o", "--output", type=Path, default=Path("output"), help="output directory (default: ./output)")
    p.add_argument(
        "-m", "--mode", choices=["single"], default="single", help="single: output/(__init__.py, _consolidated.py)"
    )
    p.add_argument("-j", "--workers", type=int, default=None, help="parallel workers (default: CPU count)")
    p.add_argument("--executor", choices=["process", "thread"], default="process")
    p.add_argument(
        "-x", "--exclude", action="append", default=[], metavar="GLOB", help="extra path glob to skip (repeatable)"
    )
    p.add_argument(
        "--entry",
        action="append",
        default=[],
        metavar="MODULE",
        help="only keep the __main__ guard of this module (repeatable)",
    )
    p.add_argument("--keep-unused-imports", action="store_true", help="do not drop unused third-party/stdlib imports")
    p.add_argument(
        "--respect-all",
        action="store_true",
        help="restrict exported names to those listed in the original __all__ declarations",
    )
    p.add_argument("--strict", action="store_true", help="exit non-zero if any file had to be skipped")
    p.add_argument("--log-level", default="INFO", choices=["TRACE", "DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--log-file", type=Path, default=None)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level, args.log_file)
    try:
        result = consolidate(
            args.source,
            args.output,
            mode=args.mode,
            workers=args.workers,
            executor=args.executor,
            exclude=tuple(args.exclude),
            keep_unused_imports=args.keep_unused_imports,
            respect_all=args.respect_all,
            entry=tuple(args.entry),
        )
    except ConsolidationError as exc:
        logger.error("{}", exc)
        return 2
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected failure")
        return 1
    logger.success("{} modules -> {} ({} exported names)", result.modules, result.output_dir, result.exports)
    if result.skipped:
        logger.error("{} file(s) skipped because of errors", len(result.skipped))
        if args.strict:
            return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
# File: core.py
"""Orchestration: discover -> parse (parallel) -> resolve -> write."""

from __future__ import annotations
import os
from concurrent.futures import BrokenExecutor, Executor, ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence
from loguru import logger
from .discovery import discover
from .models import ModuleInfo, SourceFile
from .parser import parse_module_file
from .resolver import Plan, Resolver
from .writer import render_consolidated, render_init


class ConsolidationError(RuntimeError):
    pass


@dataclass
class ConsolidationResult:
    output_dir: Path
    files: list[Path]
    modules: int
    skipped: list[str] = field(default_factory=list)
    exports: int = 0


def _parse_all(files: Sequence[SourceFile], workers: int, executor: str) -> dict[str, ModuleInfo]:
    results: dict[str, ModuleInfo] = {}

    def run_pool(pool_cls: Callable[..., Executor], todo: Sequence[SourceFile]) -> None:
        with pool_cls(max_workers=workers) as pool:
            futures = {pool.submit(parse_module_file, str(f.path), f.module, f.is_pkg): f for f in todo}
            for fut in as_completed(futures):
                src = futures[fut]
                try:
                    results[src.module] = fut.result()
                except BrokenExecutor:
                    raise
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to parse {}", src.path)

    if workers <= 1 or len(files) <= 1:
        for f in files:
            try:
                results[f.module] = parse_module_file(str(f.path), f.module, f.is_pkg)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to parse {}", f.path)
        return results
    pool_cls = ProcessPoolExecutor if executor == "process" else ThreadPoolExecutor
    try:
        run_pool(pool_cls, files)
    except BrokenExecutor:
        logger.warning("Process pool broke; retrying remaining files with threads")
        run_pool(ThreadPoolExecutor, [f for f in files if f.module not in results])
    return results


def consolidate(
    source: Path,
    output: Path,
    *,
    mode: str = "single",
    workers: int | None = None,
    executor: str = "process",
    exclude: tuple[str, ...] = (),
    keep_unused_imports: bool = False,
    respect_all: bool = False,
    entry: tuple[str, ...] = (),
) -> ConsolidationResult:
    if mode != "single":
        raise ConsolidationError(f"unknown mode {mode!r}")
    source, output = Path(source), Path(output)
    if not source.exists():
        raise ConsolidationError(f"source does not exist: {source}")
    workers = workers or os.cpu_count() or 1
    files = discover(source, exclude=exclude, skip_dirs=[output])
    if not files:
        raise ConsolidationError(f"no python files found under {source}")
    logger.info("Parsing {} files with {} {} worker(s)", len(files), workers, executor)
    parsed = _parse_all(files, workers, executor)
    modules: dict[str, ModuleInfo] = {}
    skipped: list[str] = []
    for name, info in parsed.items():
        for w in info.warnings:
            logger.warning(w)
        if info.errors:
            for e in info.errors:
                logger.error(e)
            skipped.append(info.path)
            continue
        modules[name] = info
    if not modules:
        raise ConsolidationError("no module could be parsed")
    resolver = Resolver(
        modules, keep_unused_imports=keep_unused_imports, respect_all=respect_all, entry=entry, workers=workers
    )
    plan: Plan = resolver.build()
    source_name = source.resolve().name if source.is_dir() else source.name
    mod_text = render_consolidated(plan, source_name)
    init_text = render_init(plan, source_name)
    for fname, text in (("_consolidated.py", mod_text), ("__init__.py", init_text)):
        try:
            compile(text, fname, "exec")
        except SyntaxError as exc:
            logger.error("Generated {} is not valid python: {} (line {})", fname, exc.msg, exc.lineno)
    output.mkdir(parents=True, exist_ok=True)
    targets = [(output / "_consolidated.py", mod_text), (output / "__init__.py", init_text)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda t: t[0].write_text(t[1], encoding="utf-8"), targets))
    exports = sum(len(v) for v in plan.exports.values())
    return ConsolidationResult(output, [p for p, _ in targets], len(modules), skipped, exports)
# File: discovery.py
"""Find the python files of a project and derive their dotted module names."""

from __future__ import annotations
import fnmatch
from pathlib import Path
from typing import Iterable
from loguru import logger
from .models import SourceFile

EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    ".env",
    "build",
    "dist",
    "node_modules",
    ".tox",
    ".nox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".eggs",
    "site-packages",
    "tests",
    "test",
    "testing",
    "docs",
    "doc",
    ".idea",
    ".vscode",
}
EXCLUDED_FILE_GLOBS = ("__main__.py", "conftest.py", "test_*.py", "*_test.py", "setup.py")


def detect_base(root: Path) -> Path:
    if (root / "__init__.py").is_file():
        return root.parent
    src = root / "src"
    if src.is_dir() and any(p.is_dir() and (p / "__init__.py").is_file() for p in src.iterdir()):
        return src
    return root


def _walk(directory: Path, skip: set[Path], extra_globs: tuple[str, ...]) -> Iterable[Path]:
    try:
        entries = sorted(directory.iterdir())
    except OSError as exc:
        logger.error("Cannot list {}: {}", directory, exc)
        return
    for entry in entries:
        if entry.is_dir():
            if entry.name in EXCLUDED_DIRS or entry.name.endswith(".egg-info"):
                continue
            if entry.resolve() in skip:
                continue
            yield from _walk(entry, skip, extra_globs)
        elif entry.suffix == ".py":
            if any(fnmatch.fnmatch(entry.name, g) for g in EXCLUDED_FILE_GLOBS):
                logger.debug("Skipping {}", entry)
                continue
            if any(fnmatch.fnmatch(entry.as_posix(), g) for g in extra_globs):
                logger.debug("Excluded by pattern: {}", entry)
                continue
            yield entry


def discover(root: Path, exclude: tuple[str, ...] = (), skip_dirs: Iterable[Path] = ()) -> list[SourceFile]:
    root = root.resolve()
    if root.is_file():
        module = root.stem
        return [SourceFile(root, module, False)] if root.suffix == ".py" else []
    base = detect_base(root)
    walk_root = base if base != root.parent else root
    skip = {p.resolve() for p in skip_dirs}
    files: list[SourceFile] = []
    for path in _walk(walk_root, skip, exclude):
        parts = list(path.relative_to(base).with_suffix("").parts)
        is_pkg = parts[-1] == "__init__"
        if is_pkg:
            parts = parts[:-1]
        if not parts:
            parts = [base.name]
        files.append(SourceFile(path, ".".join(parts), is_pkg))
    return files
# File: imports.py
"""Merge, de-duplicate, sort and render the import section."""

from __future__ import annotations
import sys
from collections import defaultdict
from typing import Iterable, Mapping
from .models import ImportSpec

_STDLIB = getattr(sys, "stdlib_module_names", frozenset())
_LINE = 88


def render_import_line(spec: ImportSpec) -> str:
    if spec.star:
        return f"from {spec.module} import *"
    if not spec.is_from:
        return f"import {spec.module}" + (f" as {spec.alias}" if spec.alias else "")
    item = spec.name if not spec.alias else f"{spec.name} as {spec.alias}"
    return f"from {spec.module} import {item}"


def _group(module: str) -> int:
    return 0 if module.split(".")[0] in _STDLIB else 1


def _item_key(item: str) -> tuple:
    base = item.split(" as ")[0]
    kind = 0 if (base.isupper() and len(base) > 1) else 1 if base[:1].isupper() else 2
    return (kind, base.lower(), item)


def _wrap(module: str, items: list[str]) -> str:
    line = f"from {module} import {', '.join(items)}"
    if len(line) <= _LINE:
        return line
    body = "".join(f"    {i},\n" for i in items)
    return f"from {module} import (\n{body})"


def future_section(names: Iterable[str]) -> str:
    names = sorted(set(names))
    return f"from __future__ import {', '.join(names)}" if names else ""


def build_import_section(keys: Iterable[tuple], ext_final: Mapping[tuple, str]) -> str:
    plain: dict[int, set[tuple[str, str | None]]] = defaultdict(set)
    froms: dict[int, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for key in keys:
        kind = key[0]
        if kind == "import":
            _, mod, alias = key
            final = ext_final.get(key, alias)
            plain[_group(mod)].add((mod, final))
        elif kind == "from":
            _, mod, name, alias = key
            final = ext_final.get(key, alias or name)
            froms[_group(mod)][mod].add(name if final == name else f"{name} as {final}")
        elif kind == "star":
            froms[_group(key[1])][key[1]].add("*")
    chunks: list[str] = []
    for grp in (0, 1):
        lines: list[str] = []
        entries = plain.get(grp, set())
        dotted_tops = {m.split(".")[0] for m, a in entries if a is None and "." in m}
        for mod, alias in sorted(entries, key=lambda e: (e[0].lower(), e[1] or "")):
            if alias is None and "." not in mod and mod in dotted_tops:
                continue
            lines.append(f"import {mod}" + (f" as {alias}" if alias else ""))
        for mod in sorted(froms.get(grp, {}), key=str.lower):
            items = froms[grp][mod]
            if "*" in items:
                lines.append(f"from {mod} import *")
                items = items - {"*"}
            if items:
                lines.append(_wrap(mod, sorted(items, key=_item_key)))
        if lines:
            chunks.append("\n".join(lines))
    return "\n\n".join(chunks)
# File: logging_setup.py
"""loguru configuration."""

from __future__ import annotations
import sys
from pathlib import Path
from loguru import logger

_FMT = "<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>"


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    logger.remove()
    logger.add(sys.stderr, level=level.upper(), format=_FMT)
    if log_file is not None:
        logger.add(log_file, level="DEBUG", rotation="5 MB", enqueue=True)
# File: models.py
"""Plain, picklable data structures shared by the parser and the resolver."""

from __future__ import annotations
from dataclasses import dataclass, field

CONST = "const"
CLASS = "class"
FUNC = "func"
MISC = "misc"
MAIN = "main"


@dataclass(frozen=True)
class ImportSpec:
    module: str
    name: str | None = None
    alias: str | None = None
    is_from: bool = False
    star: bool = False
    relative: bool = False

    @property
    def local_name(self) -> str | None:
        if self.star:
            return None
        if self.is_from:
            return self.alias or self.name
        return self.alias or self.module.split(".")[0]


@dataclass(slots=True)
class Occ:
    start: int
    end: int
    name: str
    kind: str
    eager: bool = True
    obj: str = ""
    specs: tuple[ImportSpec, ...] = ()


@dataclass(eq=False)
class Block:
    module: str
    kind: str
    names: list[str]
    source: bytes
    occs: list[Occ]
    order: int
    bid: int = -1

    @property
    def primary(self) -> str:
        return self.names[0] if self.names else ""

    def merge(self, other: "Block") -> None:
        shift = len(self.source) + 1
        for occ in other.occs:
            occ.start += shift
            occ.end += shift
        self.source += b"\n" + other.source
        self.occs.extend(other.occs)
        for name in other.names:
            if name not in self.names:
                self.names.append(name)


@dataclass
class GuardBlock:
    text: str
    specs: list[ImportSpec]


@dataclass
class ModuleInfo:
    module: str
    path: str
    is_pkg: bool
    imports: list[ImportSpec] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    guards: list[GuardBlock] = field(default_factory=list)
    declared_all: list[str] | None = None
    future_annotations: bool = False
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SourceFile:
    path: "object"
    module: str
    is_pkg: bool
# File: ordering.py
"""Dependency-aware ordering of blocks."""

from __future__ import annotations
import heapq
from collections import defaultdict
from typing import Callable, Mapping, Sequence
from loguru import logger
from .models import Block


def topo_order(
    nodes: Sequence[Block],
    deps: Mapping[int, set[int]],
    rank: Callable[[Block], int],
    key: Callable[[Block], tuple],
) -> list[Block]:
    by_id = {b.bid: b for b in nodes}
    pending = {b.bid: {d for d in deps.get(b.bid, ()) if d in by_id and d != b.bid} for b in nodes}
    dependents: dict[int, list[int]] = defaultdict(list)
    for bid, ds in pending.items():
        for d in ds:
            dependents[d].append(bid)
    heaps: dict[int, list] = defaultdict(list)
    queued: set[int] = set()
    done: set[int] = set()
    out: list[Block] = []

    def push(bid: int) -> None:
        b = by_id[bid]
        heapq.heappush(heaps[rank(b)], (key(b), bid))
        queued.add(bid)

    for bid, ds in pending.items():
        if not ds:
            push(bid)
    cur = 0
    while len(done) < len(nodes):
        ranks = [r for r, h in heaps.items() if h]
        if not ranks:
            cand = min(
                (i for i in pending if i not in done and i not in queued),
                key=lambda i: (len(pending[i]), rank(by_id[i]), key(by_id[i])),
            )
            blocked = ", ".join(sorted(by_id[d].primary or f"<{by_id[d].kind}>" for d in pending[cand]))
            logger.warning(
                "Circular import-time dependency: emitting {} ({}) before {}",
                by_id[cand].primary or f"<{by_id[cand].kind}>",
                by_id[cand].module,
                blocked,
            )
            push(cand)
            continue
        r = cur if heaps[cur] else min(ranks)
        cur = r
        _, bid = heapq.heappop(heaps[r])
        done.add(bid)
        out.append(by_id[bid])
        for dep in dependents[bid]:
            pending[dep].discard(bid)
            if not pending[dep] and dep not in queued and dep not in done:
                push(dep)
    return out
# File: parser.py
"""tree-sitter based extraction of top-level blocks (runs inside worker processes)."""

from __future__ import annotations
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
        occs = _collect_occs(node, start, src, own, info)
        block = Block(info.module, kind, list(names), src[start:end], occs, len(info.blocks))
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


def parse_module_file(path_str: str, module: str, is_pkg: bool) -> ModuleInfo:
    info = ModuleInfo(module=module, path=path_str, is_pkg=is_pkg)
    try:
        src = Path(path_str).read_bytes()
    except OSError as exc:
        info.errors.append(f"cannot read {path_str}: {exc}")
        return info
    if src.startswith(b"\xef\xbb\xbf"):
        src = src[3:]
    try:
        root = _get_parser().parse(src).root_node
        if root.has_error:
            info.errors.append(f"{path_str}: syntax error near line {_first_error_line(root)}")
            return info
        _extract(info, root, src)
    except Exception as exc:  # noqa: BLE001 - reported to the caller, never crashes a worker
        info.errors.append(f"{path_str}: extraction failed: {exc!r}")
    return info
# File: resolver.py
"""Cross-module resolution: name collisions, import rewriting, dedupe, ordering."""

from __future__ import annotations
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Iterable
from loguru import logger
from .imports import build_import_section, future_section, render_import_line
from .models import CLASS, CONST, FUNC, MAIN, MISC, Block, ImportSpec, ModuleInfo
from .ordering import topo_order

# Target = ("def", Block, original_name) | ("ext", key) | ("mod", module_name)
Target = tuple
_RANK = {CONST: 0, MISC: 0, CLASS: 1, FUNC: 2}
_SECTION = {CONST: "const", MISC: "const", CLASS: "class", FUNC: "func"}


@dataclass
class RenderedBlock:
    section: str
    kind: str
    text: str


@dataclass
class Plan:
    future_names: list[str]
    imports_text: str
    guards: list[str]
    blocks: list[RenderedBlock]
    exports: dict[str, list[str]] = field(default_factory=lambda: {"constants": [], "classes": [], "functions": []})


def _ext_local(key: tuple) -> str | None:
    if key[0] == "import":
        return key[2] or key[1].split(".")[0]
    if key[0] == "from":
        return key[3] or key[2]
    return None


def _ext_identity(key: tuple) -> tuple:
    if key[0] == "import":
        return ("modalias", key[1]) if key[2] else ("mod", key[1].split(".")[0])
    return ("from", key[1], key[2])


def _sanitize(text: str) -> str:
    return re.sub(r"\W", "_", text).strip("_")


class Resolver:
    def __init__(
        self,
        modules: dict[str, ModuleInfo],
        *,
        keep_unused_imports: bool = False,
        respect_all: bool = False,
        entry: Iterable[str] = (),
        workers: int = 1,
    ) -> None:
        self.modules = dict(sorted(modules.items()))
        self.keep_unused = keep_unused_imports
        self.respect_all = respect_all
        self.entry = tuple(entry)
        self.workers = max(1, workers)
        self.known: set[str] = set(self.modules)
        for m in list(self.modules):
            parts = m.split(".")
            for i in range(1, len(parts)):
                self.known.add(".".join(parts[:i]))
        self.tops = {m.split(".")[0] for m in self.modules}
        self.mod_idx = {m: i for i, m in enumerate(self.modules)}
        tops_all = {m.split(".")[0] for m in self.modules}
        self.single_top = next(iter(tops_all)) if len(tops_all) == 1 else None
        self.blocks: list[Block] = []
        for mi in self.modules.values():
            for b in mi.blocks:
                if b.kind == MAIN and self.entry and not self._entry_match(b.module):
                    continue
                b.bid = len(self.blocks)
                self.blocks.append(b)
        self.def_block: dict[tuple[str, str], Block] = {}
        for b in self.blocks:
            for n in b.names:
                self.def_block[(b.module, n)] = b
        self.future_names: set[str] = set()
        self.import_table: dict[str, dict[str, ImportSpec]] = {}
        self.star_specs: dict[str, list[ImportSpec]] = {}
        for m, mi in self.modules.items():
            table: dict[str, ImportSpec] = {}
            stars: list[ImportSpec] = []
            for spec in mi.imports:
                if spec.module == "__future__":
                    if spec.name:
                        self.future_names.add(spec.name)
                    continue
                if spec.star:
                    stars.append(spec)
                else:
                    table.setdefault(spec.local_name, spec)
            self.import_table[m] = table
            self.star_specs[m] = stars
        self.env: dict[str, dict[str, Target]] = {}
        self.eager: dict[int, set[Block]] = {}
        self.proj_refs: dict[int, set[Block]] = {}
        self.ext_keys: dict[int, set[tuple]] = {}
        self.ext_refs: dict[int, frozenset] = {}
        self.canon: dict[int, Block] = {}
        self.final: dict[tuple[int, str], str] = {}
        self.ext_final: dict[tuple, str] = {}
        self.star_keys: set[tuple] = set()
        self._warned: set[str] = set()

    def _entry_match(self, module: str) -> bool:
        return any(module == e or module.endswith("." + e) for e in self.entry)

    def _warn_once(self, msg: str) -> None:
        if msg not in self._warned:
            self._warned.add(msg)
            logger.warning(msg)

    def _is_internal(self, spec: ImportSpec) -> bool:
        if spec.relative:
            return True
        return bool(spec.module) and (spec.module in self.known or spec.module.split(".")[0] in self.tops)

    def _canon(self, b: Block) -> Block:
        return self.canon.get(b.bid, b)

    def _prefix(self, module: str) -> str:
        parts = module.split(".")
        if self.single_top and len(parts) > 1 and parts[0] == self.single_top:
            parts = parts[1:]
        return _sanitize("_".join(parts)) or "mod"

    def _star_names(self, module: str) -> list[str]:
        mi = self.modules.get(module)
        if mi is None:
            return []
        if mi.declared_all is not None:
            return list(mi.declared_all)
        names = [n for (m, n) in self.def_block if m == module and not n.startswith("_")]
        names += [n for n in self.import_table.get(module, {}) if not n.startswith("_")]
        return names

    def _spec_target(self, spec: ImportSpec, seen: set) -> Target | None:
        internal = self._is_internal(spec)
        if not spec.is_from:
            if internal:
                return ("mod", spec.module) if spec.alias else ("mod", spec.module.split(".")[0])
            return ("ext", ("import", spec.module, spec.alias))
        if internal:
            tgt = self._resolve_symbol(spec.module, spec.name, seen)
            if tgt is None and f"{spec.module}.{spec.name}" in self.known:
                tgt = ("mod", f"{spec.module}.{spec.name}")
            return tgt
        return ("ext", ("from", spec.module, spec.name, spec.alias))

    def _resolve_symbol(self, module: str, name: str, seen: set | None = None) -> Target | None:
        blk = self.def_block.get((module, name))
        if blk is not None:
            return ("def", blk, name)
        seen = set() if seen is None else seen
        if (module, name) in seen:
            return None
        seen.add((module, name))
        spec = self.import_table.get(module, {}).get(name)
        if spec is not None:
            tgt = self._spec_target(spec, seen)
            if tgt is not None:
                return tgt
        for star in self.star_specs.get(module, ()):
            if self._is_internal(star) and name in self._star_names(star.module):
                tgt = self._resolve_symbol(star.module, name, seen)
                if tgt is not None:
                    return tgt
        sub = f"{module}.{name}"
        if sub in self.known:
            return ("mod", sub)
        return None

    def _resolve_dotted(self, module: str, obj: str, attr: str) -> Target | None:
        parts = obj.split(".")
        tgt = self.env[module].get(parts[0])
        if not tgt or tgt[0] != "mod":
            return None
        mod = tgt[1]
        for seg in parts[1:]:
            cand = f"{mod}.{seg}"
            if cand not in self.known:
                return None
            mod = cand
        res = self._resolve_symbol(mod, attr)
        return res if res is not None and res[0] in ("def", "ext") else None

    def build(self) -> Plan:
        self._build_envs()
        self._analyse()
        self._dedupe()
        kept = [b for b in self.blocks if b.bid not in self.canon]
        used = set().union(*(self.ext_keys[b.bid] for b in kept)) if kept else set()
        emit_keys = set(used)
        if self.keep_unused:
            for env in self.env.values():
                emit_keys |= {t[1] for t in env.values() if t[0] == "ext"}
        emit_keys |= self.star_keys
        self._assign_names(kept, emit_keys)
        self._resolve_ext_conflicts(emit_keys)
        rendered = self._render_all(kept)
        return self._assemble(kept, rendered, emit_keys)

    def _build_envs(self) -> None:
        for m, mi in self.modules.items():
            env: dict[str, Target] = {}
            for (mm, n), blk in self.def_block.items():
                if mm == m:
                    env[n] = ("def", blk, n)
            specs: list[ImportSpec] = [s for s in mi.imports if s.module != "__future__"]
            for b in mi.blocks:
                for occ in b.occs:
                    if occ.kind == "nested_import":
                        specs.extend(s for s in occ.specs if s.module != "__future__")
            for spec in specs:
                if spec.star:
                    if self._is_internal(spec):
                        for name in self._star_names(spec.module):
                            tgt = self._resolve_symbol(spec.module, name)
                            if tgt is not None:
                                env.setdefault(name, tgt)
                    else:
                        self.star_keys.add(("star", spec.module))
                    continue
                tgt = self._spec_target(spec, set())
                if tgt is None:
                    if self._is_internal(spec):
                        self._warn_once(
                            f"{m}: cannot resolve internal import '{spec.module}"
                            f"{'.' + spec.name if spec.name else ''}' (module skipped, missing or excluded?)"
                        )
                    continue
                env.setdefault(spec.local_name, tgt)
            for g in mi.guards:
                for spec in g.specs:
                    if self._is_internal(spec):
                        self._warn_once(
                            f"{m}: guarded import of internal module '{spec.module}' is kept verbatim and will not resolve"
                        )
            self.env[m] = env

    def _analyse(self) -> None:
        for b in self.blocks:
            env = self.env[b.module]
            eager: set[Block] = set()
            proj: set[Block] = set()
            ext_keys: set[tuple] = set()
            ext_refs: set[tuple] = set()
            for occ in b.occs:
                if occ.kind == "name":
                    tgt = env.get(occ.name)
                    label = occ.name
                elif occ.kind == "attr":
                    tgt = self._resolve_dotted(b.module, occ.obj, occ.name)
                    label = f"{occ.obj}.{occ.name}"
                else:
                    continue
                if tgt is None:
                    continue
                if tgt[0] == "def":
                    if tgt[1] is not b:
                        proj.add(tgt[1])
                        if occ.eager:
                            eager.add(tgt[1])
                elif tgt[0] == "ext":
                    ext_keys.add(tgt[1])
                    ext_refs.add((label, tgt[1]))
            self.eager[b.bid], self.proj_refs[b.bid] = eager, proj
            self.ext_keys[b.bid], self.ext_refs[b.bid] = ext_keys, frozenset(ext_refs)

    def _dedupe(self) -> None:
        groups: dict[tuple, list[Block]] = defaultdict(list)
        for b in self.blocks:
            if b.kind in (CLASS, FUNC) and not self.proj_refs[b.bid]:
                groups[(b.kind, b.primary, b.source.strip(), self.ext_refs[b.bid])].append(b)
        for blocks in groups.values():
            if len(blocks) < 2:
                continue
            blocks.sort(key=lambda x: (x.module.count("."), x.module, x.order))
            for dup in blocks[1:]:
                self.canon[dup.bid] = blocks[0]
                logger.info(
                    "Dropping duplicate {} '{}' from {} (identical to {})",
                    dup.kind,
                    dup.primary,
                    dup.module,
                    blocks[0].module,
                )

    def _assign_names(self, kept: list[Block], emit_keys: set[tuple]) -> None:
        ext_loc_modules: dict[str, set[str]] = defaultdict(set)
        for m, env in self.env.items():
            for local, tgt in env.items():
                if tgt[0] == "ext" and tgt[1] in emit_keys:
                    ext_loc_modules[local].add(m)
        ext_locals = {_ext_local(k) for k in emit_keys if _ext_local(k)}
        guard_names = {s.local_name for mi in self.modules.values() for g in mi.guards for s in g.specs if s.local_name}
        group: dict[str, list[Block]] = defaultdict(list)
        for b in kept:
            for n in b.names:
                group[n].append(b)
        taken: set[str] = set(ext_locals) | guard_names
        for name in sorted(group):
            blocks = sorted(group[name], key=lambda x: (x.module.count("."), x.module, x.order))
            for i, b in enumerate(blocks):
                clash = bool(ext_loc_modules.get(name, set()) - {b.module}) or name in guard_names
                if i == 0 and not clash and name not in taken:
                    cand = name
                else:
                    lead = name[: len(name) - len(name.lstrip("_"))]
                    cand = f"{lead}{self._prefix(b.module)}_{name.lstrip('_')}"
                    base, n = cand, 2
                    while cand in taken or cand in group:
                        cand, n = f"{base}_{n}", n + 1
                    logger.info("Renaming {} '{}' in {} -> '{}'", b.kind, name, b.module, cand)
                taken.add(cand)
                self.final[(b.bid, name)] = cand
        self._taken = taken

    def _resolve_ext_conflicts(self, emit_keys: set[tuple]) -> None:
        by_local: dict[str, list[tuple]] = defaultdict(list)
        for key in emit_keys:
            loc = _ext_local(key)
            if loc:
                by_local[loc].append(key)
        taken = self._taken
        for loc, keys in by_local.items():
            if len({_ext_identity(k) for k in keys}) <= 1:
                continue
            keys.sort(key=lambda k: (not (k[0] == "import" and k[2] is None), repr(k)))
            keep = _ext_identity(keys[0])
            aliased: dict[tuple, str] = {}
            for k in keys:
                ident = _ext_identity(k)
                if ident == keep:
                    continue
                if ident in aliased:
                    self.ext_final[k] = aliased[ident]
                    continue
                base = _sanitize(k[1]) + (f"_{k[2]}" if k[0] == "from" else "")
                cand, n = base, 2
                while cand in taken:
                    cand, n = f"{base}_{n}", n + 1
                taken.add(cand)
                aliased[ident] = cand
                self.ext_final[k] = cand
                logger.info("Import conflict on '{}': aliasing {} as '{}'", loc, k[1:], cand)

    def _target_name(self, tgt: Target | None, local: str | None) -> str | None:
        if tgt is None:
            return None
        if tgt[0] == "def":
            blk = self._canon(tgt[1])
            return self.final.get((blk.bid, tgt[2]))
        if tgt[0] == "ext":
            return self.ext_final.get(tgt[1], local)
        return None

    def _render_block(self, b: Block) -> str:
        env = self.env[b.module]
        edits: list[tuple[int, int, str]] = []
        for occ in b.occs:
            if occ.kind == "nested_import":
                internal = [s for s in occ.specs if self._is_internal(s)]
                if internal:
                    lines = [render_import_line(s) for s in occ.specs if not self._is_internal(s)]
                    edits.append((occ.start, occ.end, ("\n" + occ.obj).join(lines) if lines else "pass"))
                continue
            if occ.kind == "name":
                new = self._target_name(env.get(occ.name), occ.name)
                if new is None or new == occ.name:
                    continue
            else:
                new = self._target_name(self._resolve_dotted(b.module, occ.obj, occ.name), None)
                if new is None:
                    continue
            edits.append((occ.start, occ.end, new))
        edits.sort(key=lambda e: (e[0], -e[1]))
        out, pos, last_end = [], 0, -1
        for s, e, r in edits:
            if s < last_end:
                continue
            out.append(b.source[pos:s])
            out.append(r.encode())
            pos = last_end = e
        out.append(b.source[pos:])
        return b"".join(out).decode("utf-8", "replace").strip("\n").rstrip()

    def _render_all(self, kept: list[Block]) -> dict[int, str]:
        if self.workers > 1 and len(kept) > 32:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                texts = list(pool.map(self._render_block, kept))
        else:
            texts = [self._render_block(b) for b in kept]
        return {b.bid: t for b, t in zip(kept, texts)}

    def _assemble(self, kept: list[Block], texts: dict[int, str], emit_keys: set[tuple]) -> Plan:
        keyfn = lambda b: (self.mod_idx[b.module], b.order)  # noqa: E731
        body = [b for b in kept if b.kind != MAIN]
        mains = sorted((b for b in kept if b.kind == MAIN), key=keyfn)
        deps: dict[int, set[int]] = {b.bid: {self._canon(d).bid for d in self.eager[b.bid]} - {b.bid} for b in body}
        dependents: dict[int, set[int]] = defaultdict(set)
        for bid, ds in deps.items():
            for d in ds:
                dependents[d].add(bid)
        tail = [b for b in body if b.kind == FUNC and b.primary == "main" and not dependents[b.bid]]
        tail_ids = {b.bid for b in tail}
        head = [b for b in body if b.bid not in tail_ids]
        ordered = topo_order(head, deps, lambda b: _RANK[b.kind], lambda b: (_RANK[b.kind], *keyfn(b)))
        tail_ordered = topo_order(tail, deps, lambda b: 0, keyfn)
        rendered = [RenderedBlock(_SECTION[b.kind], b.kind, texts[b.bid]) for b in ordered]
        rendered += [RenderedBlock("entry", b.kind, texts[b.bid]) for b in tail_ordered + mains]
        if len(mains) > 1:
            logger.warning("{} '__main__' guards were kept; use --entry to keep only one", len(mains))
        guards: list[str] = []
        for mi in self.modules.values():
            for g in mi.guards:
                if g.text not in guards:
                    guards.append(g.text)
        plan = Plan(
            future_names=sorted(self.future_names),
            imports_text=build_import_section(emit_keys, self.ext_final),
            guards=guards,
            blocks=rendered,
        )
        self._exports(plan, kept)
        return plan

    def _exports(self, plan: Plan, kept: list[Block]) -> None:
        allowed: set[str] | None = None
        if self.respect_all and any(mi.declared_all is not None for mi in self.modules.values()):
            allowed = set()
            for m, mi in self.modules.items():
                for n in mi.declared_all or ():
                    tgt = self._resolve_symbol(m, n)
                    if tgt and tgt[0] == "def":
                        fin = self.final.get((self._canon(tgt[1]).bid, tgt[2]))
                        if fin:
                            allowed.add(fin)
        groups = {CLASS: "classes", FUNC: "functions"}
        seen: set[str] = set()
        for b in kept:
            if b.kind == MAIN:
                continue
            for n in b.names:
                fin = self.final[(b.bid, n)]
                if fin.startswith("_") or fin in seen or (allowed is not None and fin not in allowed):
                    continue
                seen.add(fin)
                plan.exports[groups.get(b.kind, "constants")].append(fin)
        for names in plan.exports.values():
            names.sort(key=lambda x: (x.lower(), x))
# File: writer.py
from __future__ import annotations
from .imports import future_section
from .resolver import Plan

_RULE = "# " + "-" * 75
_TITLES = {"const": "Constants", "class": "Classes", "func": "Functions", "entry": "Entry points"}


def _sep(prev_kind: str, cur_kind: str, prev_text: str, cur_text: str) -> str:
    simple = ("const", "misc")
    if prev_kind in simple and cur_kind in simple:
        return "\n" if "\n" not in prev_text and "\n" not in cur_text else "\n\n"
    return "\n\n\n"


def render_consolidated(plan: Plan, source_name: str) -> str:
    parts: list[str] = [f'"""Consolidated from ``{source_name}`` by pyrefactor (generated file)."""']
    head = []
    if plan.future_names:
        head.append(future_section(plan.future_names))
    if plan.imports_text:
        head.append(plan.imports_text)
    if plan.guards:
        head.append("\n\n".join(plan.guards))
    if head:
        parts.append("\n\n".join(head))
    body: list[str] = []
    prev = None
    for blk in plan.blocks:
        piece = ""
        if prev is None or blk.section != prev.section:
            piece += f"{_RULE}\n# {_TITLES[blk.section]}\n{_RULE}\n\n\n"
            sep = "\n\n\n" if prev is not None else ""
        else:
            sep = _sep(prev.kind, blk.kind, prev.text, blk.text)
        piece += blk.text
        body.append(sep + piece)
        prev = blk
    parts.append("".join(body))
    return "\n\n\n".join(p for p in parts if p).rstrip() + "\n"


def render_init(plan: Plan, source_name: str, module: str = "_consolidated") -> str:
    groups = [
        ("constants", plan.exports["constants"]),
        ("classes", plan.exports["classes"]),
        ("functions", plan.exports["functions"]),
    ]
    names = [n for _, ns in groups for n in ns]
    out = [f'"""Public API of ``{source_name}`` (generated by pyrefactor)."""\n']
    if names:
        out.append(f"from .{module} import (\n" + "".join(f"    {n},\n" for n in names) + ")\n")
        lines = ["__all__ = ["]
        for title, ns in groups:
            if ns:
                lines.append(f"    # {title}")
                lines += [f'    "{n}",' for n in ns]
        lines.append("]")
        out.append("\n".join(lines) + "\n")
    else:
        out.append("__all__: list[str] = []\n")
    return "\n".join(out)
