"""Cross-module resolution: name collisions, import rewriting, dedupe, ordering."""

from __future__ import annotations
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Iterable
from loguru import logger
from .imports import build_import_section, future_section, render_import_line
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
    ResolverError,
)
from .ordering import topo_order

#: Dunder module attributes that are commonly re-exported and must survive the
#: "private names are not exported" filter.
DUNDER_EXPORTS = frozenset(
    {"__version__", "__author__", "__email__", "__license__", "__copyright__"}
)
#: Suffixes that make a guard body meaningless (headers without statements).
_GUARD_HEADER_RE = re.compile(
    r"^\s*(?:try|except|else|finally|if|elif|with|async|pass)\b"
)
_ASYNC_DEF_RE = re.compile(rb"^\s*(?:@[^\n]*\n\s*)*async\s+def\b")
_STRING_RE = re.compile(r"""(?P<q>['"])(?P<body>[^'"\n]{1,120})(?P=q)""")
_ANNOTATION_LIKE_RE = re.compile(r"^[\w\s\[\],.|]+$")
_IDENT_RE = re.compile(r"[A-Za-z_]\w*")

# Target = ("def", Block, original_name) | ("ext", key) | ("mod", module_name)
Target = tuple
_RANK = {CONST: 0, MISC: 0, CLASS: 1, FUNC: 2}
_SECTION = {CONST: "const", MISC: "const", CLASS: "class", FUNC: "func"}


@dataclass
class RenderedBlock:
    """A block after rewriting, ready to be emitted.

    Attributes:
        section: Target section: ``const``, ``class``, ``func`` or ``entry``.
        kind: Original block kind.
        text: Rewritten block source.
        origin: ``file:line-range`` for source maps.
        module: Module the block came from.

    """

    section: str
    kind: str
    text: str
    # Extended for source-map / preserve-modules
    origin: str = ""  # e.g., "pkg/mod.py:10-20"
    module: str = ""


@dataclass
class Plan:
    """Everything needed to render the consolidated output.

    Attributes:
        future_names: ``__future__`` features to emit (uniform across modules).
        imports_text: Rendered external import section.
        guards: Verbatim import guards, in output order.
        blocks: Rendered blocks in dependency order.
        exports: Public API split into constants, classes and functions.
        renamed: ``(module, original, final)`` rename log.
        deduped: ``(module, name)`` log of dropped duplicates.
        reexports: Exported name to the name inside the consolidated module.
        warnings: Warnings collected while building the plan.

    """

    future_names: list[str]
    imports_text: str
    guards: list[str]
    blocks: list[RenderedBlock]
    exports: dict[str, list[str]] = field(
        default_factory=lambda: {"constants": [], "classes": [], "functions": []}
    )
    # Extended metadata for reporting / --dry-run
    renamed: list[tuple[str, str, str]] = field(
        default_factory=list
    )  # (module, original, final)
    deduped: list[tuple[str, str]] = field(default_factory=list)  # (module, name)
    # exported name -> name inside the consolidated module (aliased re-exports)
    reexports: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


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
    """Turn parsed modules into a single, dependency-ordered plan.

    The resolver owns all cross-module reasoning: environment construction,
    reference analysis, dedupe, renaming, import pruning and rendering.
    """

    def __init__(
        self,
        modules: dict[str, ModuleInfo],
        *,
        keep_unused_imports: bool = False,
        respect_all: bool = False,
        entry: Iterable[str] = (),
        workers: int = 1,
        rename_strategy: str = "prefix",
        dedupe: str = "safe",
        imports_mode: str = "prune",
        all_mode: str = "ignore",
        namespace: bool = False,
        preserve_modules: bool = False,
        source_map: bool = False,
        entry_keep_all: bool = False,
        main_name: str | None = None,
        generate_guard: bool = False,
        strict: bool = False,
    ) -> None:
        """Initialize the resolver.

        Args:
            modules: Parsed modules keyed by dotted module name.
            keep_unused_imports: Deprecated alias for ``imports_mode="keep"``.
            respect_all: Deprecated alias for ``all_mode="respect"``.
            entry: Modules whose ``__main__`` guard should be kept.
            workers: Worker count used when rendering blocks.
            rename_strategy: Collision policy: ``prefix``, ``suffix``,
                ``module_path`` or ``error``.
            dedupe: Duplicate removal mode: ``none``, ``safe`` or
                ``aggressive``.
            imports_mode: Import handling: ``prune``, ``keep`` or ``strict``.
            all_mode: ``__all__`` handling: ``ignore``, ``respect``, ``merge``
                or ``recompute``.
            namespace: Emit module namespace comments (used by the writer).
            preserve_modules: Emit per-block origin comments.
            source_map: Emit ``file:line`` comments per block.
            entry_keep_all: Keep every ``__main__`` guard.
            main_name: Name to give the ``main`` entry point.
            generate_guard: Synthesize a ``__main__`` guard when missing.
            strict: Promote risky warnings and import cycles to errors.

        """
        self.modules = dict(sorted(modules.items()))
        self.keep_unused = keep_unused_imports
        self.respect_all = respect_all
        self.entry = tuple(entry)
        self.workers = max(1, workers)
        # New options
        self.rename_strategy = rename_strategy
        self.dedupe_mode = dedupe
        self.imports_mode = imports_mode
        self.all_mode = all_mode
        self.namespace = namespace
        self.preserve_modules = preserve_modules
        self.source_map = source_map
        self.entry_keep_all = entry_keep_all
        self.main_name = main_name
        self.generate_guard = generate_guard
        self.strict = strict
        # Compatibility: keep_unused_imports -> imports_mode
        if self.keep_unused and self.imports_mode == "prune":
            self.imports_mode = "keep"
        if self.respect_all and self.all_mode == "ignore":
            self.all_mode = "respect"

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
                    # If entry_keep_all, keep all mains regardless; otherwise filter
                    if not self.entry_keep_all:
                        continue
                b.bid = len(self.blocks)
                self.blocks.append(b)
        self.def_block: dict[tuple[str, str], Block] = {}
        for b in self.blocks:
            for n in b.names:
                self.def_block[(b.module, n)] = b
        self.future_names: set[str] = set()
        self.module_futures: dict[str, set[str]] = {}
        self.import_table: dict[str, dict[str, ImportSpec]] = {}
        self.star_specs: dict[str, list[ImportSpec]] = {}
        self._reexport_specs: list[tuple[str, str, Target]] = []
        self._verbatim_keys: set[tuple] = set()
        # Names bound by imports *inside* blocks (function bodies) must not be
        # hoisted to the module import section: keeping them local preserves
        # optional-dependency degradation (try/except around the import).
        self.nested_locals: dict[int, set[str]] = {}
        self._nested_ext_keys: set[tuple] = set()
        self._dropped_edits = 0
        for m, mi in self.modules.items():
            table: dict[str, ImportSpec] = {}
            stars: list[ImportSpec] = []
            futures = set(mi.future_names)
            for spec in mi.imports:
                if spec.module == "__future__":
                    if spec.name:
                        self.future_names.add(spec.name)
                        futures.add(spec.name)
                    continue
                if spec.star:
                    stars.append(spec)
                else:
                    table.setdefault(spec.local_name, spec)
            self.import_table[m] = table
            self.star_specs[m] = stars
            self.module_futures[m] = futures
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
        self._warned_list: list[str] = []
        # For reporting
        self._renamed_log: list[tuple[str, str, str]] = []
        self._deduped_log: list[tuple[str, str]] = []

    def _entry_match(self, module: str) -> bool:
        return any(module == e or module.endswith("." + e) for e in self.entry)

    def _warn_once(self, msg: str) -> None:
        """Log ``msg`` once, and record it on the plan.

        Args:
            msg: Warning text. Duplicates are ignored.

        """
        if msg not in self._warned:
            self._warned.add(msg)
            self._warned_list.append(msg)
            logger.warning(msg)

    def _is_internal(self, spec: ImportSpec) -> bool:
        if spec.relative:
            return True
        return bool(spec.module) and (
            spec.module in self.known or spec.module.split(".")[0] in self.tops
        )

    def _canon(self, b: Block) -> Block:
        """Return the block ``b`` was deduplicated into (``b`` when kept).

        Chained mappings are followed defensively: ``_flatten_canon`` keeps
        them flat, but rendering must never loop even if a future pass adds a
        link.

        Args:
            b: Block to look up.

        Returns:
            The surviving block that replaced ``b``.

        """
        seen = 0
        while b.bid in self.canon and seen <= len(self.canon):
            b = self.canon[b.bid]
            seen += 1
        return b

    def _flatten_canon(self) -> None:
        """Collapse canonical chains so :meth:`_canon` is a single lookup.

        Repeated dedupe passes can drop a block that was itself the canonical
        replacement for an earlier duplicate. Rewriting every entry to point
        straight at the surviving block keeps lookups constant time (and keeps
        the dict mutation out of the parallel rendering phase).
        """
        for bid in list(self.canon):
            target = self.canon[bid]
            seen = 0
            while target.bid in self.canon and seen <= len(self.canon):
                target = self.canon[target.bid]
                seen += 1
            self.canon[bid] = target

    def _prefix(self, module: str) -> str:
        parts = module.split(".")
        if self.single_top and len(parts) > 1 and parts[0] == self.single_top:
            parts = parts[1:]
        return _sanitize("_".join(parts)) or "mod"

    def _suffix(self, module: str) -> str:
        return self._prefix(module)

    def _module_path_prefix(self, module: str) -> str:
        # Full module path with dots -> underscores
        return _sanitize(module.replace(".", "_")) or "mod"

    def _star_names(self, module: str) -> list[str]:
        mi = self.modules.get(module)
        if mi is None:
            return []
        if mi.declared_all is not None:
            return list(mi.declared_all)
        names = [
            n for (m, n) in self.def_block if m == module and not n.startswith("_")
        ]
        names += [n for n in self.import_table.get(module, {}) if not n.startswith("_")]
        return names

    def _spec_target(self, spec: ImportSpec, seen: set) -> Target | None:
        internal = self._is_internal(spec)
        if not spec.is_from:
            if internal:
                return (
                    ("mod", spec.module)
                    if spec.alias
                    else ("mod", spec.module.split(".")[0])
                )
            return ("ext", ("import", spec.module, spec.alias))
        if internal:
            tgt = self._resolve_symbol(spec.module, spec.name, seen)
            if tgt is None and f"{spec.module}.{spec.name}" in self.known:
                tgt = ("mod", f"{spec.module}.{spec.name}")
            return tgt
        return ("ext", ("from", spec.module, spec.name, spec.alias))

    def _resolve_symbol(
        self, module: str, name: str, seen: set | None = None
    ) -> Target | None:
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
        """Build the consolidation plan.

        Runs environment construction, reference analysis, dedupe, renaming,
        import resolution, a second (post-rename) dedupe and rendering, in that
        order. All mutation happens here, before any rendering is parallelised.

        Returns:
            The complete :class:`Plan` ready to be rendered.

        Raises:
            ResolverError: If the plan cannot be produced safely (for example a
                module object is used as a value, or name collisions are
                requested to be fatal via ``rename_strategy="error"``).

        """
        self._build_envs()
        self._analyse()
        self._dedupe()
        kept = [b for b in self.blocks if b.bid not in self.canon]
        emit_keys = self._emit_keys(kept)
        self._assign_names(kept, emit_keys)
        self._resolve_ext_conflicts(emit_keys)
        # Renaming can turn two formerly distinct blocks into the same code
        # (aliased imports, project references that now share a name). Dedupe
        # again with post-rename identities and drop imports only they used.
        before_dedupe = len(kept)
        kept = self._dedupe_post_rename(kept)
        if len(kept) != before_dedupe:
            emit_keys = self._emit_keys(kept, report_strict=False)
        self._check_module_value_usage(kept)
        self._warn_stale_forward_refs(kept)
        if self.star_keys:
            self._warn_once(
                "external star import(s) "
                + ", ".join(sorted(k[1] for k in self.star_keys))
                + " are emitted verbatim and may shadow consolidated names"
            )
        rendered = self._render_all(kept)
        plan = self._assemble(kept, rendered, emit_keys)
        plan.future_names = self._uniform_futures(kept)
        plan.warnings = list(self._warned_list)
        return plan

    def _uniform_futures(self, kept: list[Block]) -> list[str]:
        """Return the ``__future__`` features every kept module agrees on.

        Unioning ``__future__`` imports across modules silently changed the
        semantics of modules that did not opt in (most notably
        ``annotations``), so only the intersection is emitted and the
        difference is reported.

        Args:
            kept: Blocks that will be part of the output.

        Returns:
            Sorted list of feature names that are safe to emit.

        """
        modules = sorted({b.module for b in kept})
        if not modules:
            return []
        sets = {m: self.module_futures.get(m, set()) for m in modules}
        common = set.intersection(*(s for s in sets.values()))
        for name in sorted(set().union(*sets.values()) - common):
            missing = [m for m in modules if name not in sets[m]]
            self._warn_once(
                f"from __future__ import {name}: not used by every module "
                f"({', '.join(missing)}) — not emitted for the consolidated file"
            )
        return sorted(common)

    def _collect_reexports(self) -> set[tuple]:
        """Collect public re-exports performed by package ``__init__`` modules.

        The canonical pattern ``from .sub import helper as public_helper`` in
        ``__init__.py`` never creates a block, so the name used to disappear
        from the generated ``__init__.py``. Those specs are recorded here and
        turned into explicit exports in :meth:`_exports`.

        Returns:
            External import keys that must be emitted so the re-exports resolve.

        """
        self._reexport_specs = []
        ext_keys: set[tuple] = set()
        for module, mi in self.modules.items():
            if not mi.is_pkg:
                continue
            declared = set(mi.declared_all or ())
            for local, spec in self.import_table.get(module, {}).items():
                if not local or local == "__all__" or spec.star:
                    continue
                if local.startswith("_") and local not in declared:
                    continue
                tgt = self._spec_target(spec, set())
                if tgt is None:
                    continue
                self._reexport_specs.append((module, local, tgt))
                if tgt[0] == "ext":
                    ext_keys.add(tgt[1])
        return ext_keys

    def _guard_used_keys(self) -> set[tuple]:
        """Return external import keys referenced by kept guards.

        Guards are copied verbatim, so any name they mention must still be
        imported in the consolidated module — otherwise pruning breaks them
        (``TYPE_CHECKING`` and ``TypeAlias`` are the common cases).

        Returns:
            External import keys to force into the import section.

        """
        needed: set[tuple] = set()
        for guard in (g for mi in self.modules.values() for g in mi.guards):
            words = set(_IDENT_RE.findall(guard.text))
            if not words:
                continue
            for table in self.import_table.values():
                for local, spec in table.items():
                    if not local or local not in words:
                        continue
                    tgt = self._spec_target(spec, set())
                    if tgt is not None and tgt[0] == "ext":
                        needed.add(tgt[1])
        return needed

    def _verbatim_ext_target(self, spec: ImportSpec) -> Target:
        """Represent an unresolvable internal import as a verbatim import.

        Internal imports whose target cannot be resolved (compiled extensions,
        excluded or dynamically generated modules) used to be dropped, which
        turned working code into ``NameError``s. Keeping them verbatim and
        emitting a warning is strictly better.

        Args:
            spec: The import specification to keep.

        Returns:
            An ``("ext", key)`` target that renders back to the original import.

        """
        if spec.is_from:
            return ("ext", ("from", spec.module, spec.name, spec.alias))
        return ("ext", ("import", spec.module, spec.alias))

    def _build_envs(self) -> None:
        for m, mi in self.modules.items():
            env: dict[str, Target] = {}
            for (mm, n), blk in self.def_block.items():
                if mm == m:
                    env[n] = ("def", blk, n)
            specs: list[ImportSpec] = [
                s for s in mi.imports if s.module != "__future__"
            ]
            for b in mi.blocks:
                for occ in b.occs:
                    if occ.kind == "nested_import":
                        specs.extend(s for s in occ.specs if s.module != "__future__")
            for b in mi.blocks:
                local: set[str] = set()
                for occ in b.occs:
                    if occ.kind != "nested_import":
                        continue
                    local |= {
                        spec.local_name
                        for spec in occ.specs
                        if spec.local_name
                        and spec.module != "__future__"
                        and not self._is_internal(spec)
                    }
                    for spec in occ.specs:
                        if spec.module == "__future__" or self._is_internal(spec):
                            continue
                        self._nested_ext_keys.add(
                            ("from", spec.module, spec.name, spec.alias)
                            if spec.is_from
                            else ("import", spec.module, spec.alias)
                        )
                self.nested_locals[b.bid] = local
            for spec in specs:
                if spec.star:
                    if self._is_internal(spec):
                        for name in self._star_names(spec.module):
                            tgt = self._resolve_symbol(spec.module, name)
                            if tgt is not None:
                                env.setdefault(name, tgt)
                        # Warn on local star imports (re-exports)
                        self._warn_once(
                            f"{m}: local star import from {spec.module} expanded to {len(self._star_names(spec.module))} names"
                        )
                    else:
                        # External star imports: log and keep
                        self._warn_once(
                            f"{m}: external star import from {spec.module} — kept as is"
                        )
                        self.star_keys.add(("star", spec.module))
                    continue
                tgt = self._spec_target(spec, set())
                if tgt is None:
                    if self._is_internal(spec):
                        self._warn_once(
                            f"{m}: cannot resolve internal import '{spec.module}"
                            f"{'.' + spec.name if spec.name else ''}' (module skipped, "
                            "missing, excluded or a compiled extension) — kept verbatim"
                        )
                        tgt = self._verbatim_ext_target(spec)
                        # Compiled extensions register themselves on import, so
                        # the statement must survive even when unreferenced.
                        self._verbatim_keys.add(tgt[1])
                    else:
                        continue
                # Handle import aliases and potential collisions later
                env.setdefault(spec.local_name, tgt)
            for g in mi.guards:
                internal = [s for s in g.specs if self._is_internal(s)]
                if internal:
                    # Relative imports cannot work in a consolidated file: hoist
                    # them to module level and drop them from the guard text.
                    g.internal = True
                    for spec in internal:
                        tgt = self._spec_target(spec, set())
                        if tgt is None:
                            tgt = self._verbatim_ext_target(spec)
                            # Unresolvable (e.g. compiled) targets are kept in
                            # the import section even when unused.
                            self._verbatim_keys.add(tgt[1])
                        if spec.local_name:
                            env.setdefault(spec.local_name, tgt)
                    self._warn_once(
                        f"{m}: internal import(s) inside a guarded block were "
                        "hoisted to module level"
                    )
                elif "ImportError" in g.text or "try:" in g.text:
                    self._warn_once(
                        f"{m}: optional import guard kept verbatim (may have side effects)"
                    )
            self.env[m] = env

    def _analyse(self) -> None:
        for b in self.blocks:
            env = self.env[b.module]
            eager: set[Block] = set()
            proj: set[Block] = set()
            ext_keys: set[tuple] = set()
            ext_refs: set[tuple] = set()
            local_names = self.nested_locals.get(b.bid, set())
            for occ in b.occs:
                if occ.name and occ.name in local_names:
                    # Resolved by an import inside this block, not by the
                    # module-level environment.
                    continue
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
                # Circular import detection: if tgt is mod and that mod depends back, it will be caught in topo_order
            self.eager[b.bid], self.proj_refs[b.bid] = eager, proj
            self.ext_keys[b.bid], self.ext_refs[b.bid] = ext_keys, frozenset(ext_refs)
            # Warn on import-time circular suspects already via ordering
            # Side-effect-only imports are those where import is never referenced but kept for side effect
            # We keep guards verbatim, so they are not lost

    def _emit_keys(self, kept: list[Block], report_strict: bool = True) -> set[tuple]:
        """Collect the external import keys that must be emitted.

        Args:
            kept: Blocks that will be rendered.
            report_strict: Emit the ``imports=strict`` warnings for unused
                imports. Suppressed when recomputing after a dedupe pass, so
                the same imports are not reported twice.

        Returns:
            External import keys (plus star/guard/verbatim extras) to render.

        """
        used = set().union(*(self.ext_keys[b.bid] for b in kept)) if kept else set()
        emit_keys = set(used)
        # imports_mode handling
        if self.imports_mode == "keep":
            for env in self.env.values():
                emit_keys |= {
                    target[1]
                    for target in env.values()
                    if target[0] == "ext" and target[1] not in self._nested_ext_keys
                }
        elif self.imports_mode == "strict" and report_strict:
            # In strict, we prune but also warn if unused imports exist
            all_ext = {
                t[1] for env in self.env.values() for t in env.values() if t[0] == "ext"
            }
            unused = all_ext - used
            for k in sorted(unused, key=repr):
                self._warn_once(f"Strict imports: unused import {k} would be pruned")
        # keep_unused legacy already mapped
        emit_keys |= self.star_keys
        emit_keys |= self._collect_reexports()
        emit_keys |= self._guard_used_keys()
        emit_keys |= self._verbatim_keys
        return emit_keys

    def _dedupe(self) -> None:
        if self.dedupe_mode == "none":
            logger.info("Dedupe disabled (--dedupe none)")
            return
        groups: dict[tuple, list[Block]] = defaultdict(list)
        for b in self.blocks:
            # safe: only CLASS/FUNC with identical bodies and no external refs differences
            if self.dedupe_mode == "safe":
                if b.kind == CONST:
                    # Identical constant definitions are safe to collapse: the
                    # parser already merged repeated assignments of one name
                    # inside a module, so equal text means equal semantics.
                    groups[
                        (b.kind, b.primary, b.source.strip(), self.ext_refs[b.bid])
                    ].append(b)
                elif b.kind in (CLASS, FUNC) and not self.proj_refs[b.bid]:
                    groups[
                        (b.kind, b.primary, b.source.strip(), self.ext_refs[b.bid])
                    ].append(b)
            elif self.dedupe_mode == "aggressive":
                # Also dedupe CONST and MISC if identical source, and allow whitespace-insensitive matching
                norm = re.sub(rb"\s+", b" ", b.source.strip())
                key = (b.kind, b.primary, norm, self.ext_refs[b.bid])
                # For aggressive, also include const/misc
                if b.kind in (CLASS, FUNC, CONST, MISC):
                    # Only dedupe if not having unique side effects (proj_refs empty for funcs/classes, but for const/misc we check)
                    if not self.proj_refs[b.bid] or b.kind in (CONST, MISC):
                        groups[key].append(b)
        for blocks in groups.values():
            if len(blocks) < 2:
                continue
            blocks.sort(key=lambda x: (x.module.count("."), x.module, x.order))
            for dup in blocks[1:]:
                self.canon[dup.bid] = blocks[0]
                self._deduped_log.append((dup.module, dup.primary or dup.kind))
                logger.info(
                    "Dropping duplicate {} '{}' from {} (identical to {})",
                    dup.kind,
                    dup.primary,
                    dup.module,
                    blocks[0].module,
                )
                # Aggressive may also warn about differing bodies with same name (not deduped, handled in _assign_names)

    def _block_signature(self, b: Block) -> tuple[str, ...]:
        """Build a rename-invariant fingerprint of a block.

        Every occurrence that resolves to a project definition is replaced by
        a token made of its canonical block id and original name; every
        external occurrence is replaced by the identity of the imported
        object. Two blocks with the same fingerprint render to the same code
        once their own names are ignored, no matter how differently they
        imported or aliased what they use.

        Args:
            b: Block to fingerprint.

        Returns:
            Literal text chunks interleaved with identity tokens.

        """
        env = self.env[b.module]
        parts: list[str] = []
        pos = 0
        for occ in sorted(b.occs, key=lambda o: (o.start, o.end)):
            if occ.kind == "nested_import":
                # Nested imports are kept verbatim when rendering, so leaving
                # them in the literal text keeps the comparison conservative.
                continue
            if occ.kind == "name":
                tgt = env.get(occ.name)
            else:
                tgt = self._resolve_dotted(b.module, occ.obj, occ.name)
            token: str | None = None
            if tgt is not None and tgt[0] == "def":
                canon = self._canon(tgt[1])
                token = (
                    f"\x00self\x00{tgt[2]}\x00"
                    if canon is b
                    else f"\x00def\x00{canon.bid}\x00{tgt[2]}\x00"
                )
            elif tgt is not None and tgt[0] == "ext":
                token = f"\x00ext\x00{_ext_identity(tgt[1])!r}\x00"
            if token is None or occ.start < pos:
                continue
            parts.append(b.source[pos : occ.start].decode("utf-8", "replace"))
            parts.append(token)
            pos = occ.end
        parts.append(b.source[pos:].decode("utf-8", "replace"))
        return tuple(parts)

    def _dedupe_post_rename(self, kept: list[Block]) -> list[Block]:
        """Drop duplicates that only become identical *after* renaming.

        :meth:`_dedupe` runs before names are assigned, so it compares
        pre-rename external references. Two blocks can therefore be textually
        different and still end up as the exact same code: ``from .models
        import User as U`` and ``from .models import User`` resolve to the same
        project object, and two imports that were aliased apart during
        ``_resolve_ext_conflicts`` may resolve to the same external name.

        This pass keys on :meth:`_block_signature`, which embeds those
        post-rename identities, so it recognises exactly those duplicates and
        leaves genuinely different blocks alone. It runs to a fixed point
        because dropping a duplicate can make its callers duplicates too.

        Args:
            kept: Blocks that survived the pre-rename dedupe.

        Returns:
            ``kept`` without the blocks dropped by this pass.

        """
        if self.dedupe_mode == "none":
            return kept
        kinds = (
            (CONST, CLASS, FUNC)
            if self.dedupe_mode == "safe"
            else (CONST, CLASS, FUNC, MISC)
        )
        while True:
            groups: dict[tuple, list[Block]] = defaultdict(list)
            for b in kept:
                if b.kind not in kinds:
                    continue
                groups[(b.kind, tuple(b.names), self._block_signature(b))].append(b)
            changed = False
            for blocks in groups.values():
                if len(blocks) < 2:
                    continue
                blocks.sort(key=lambda x: (x.module.count("."), x.module, x.order))
                canon = blocks[0]
                for dup in blocks[1:]:
                    self.canon[dup.bid] = canon
                    self._deduped_log.append((dup.module, dup.primary or dup.kind))
                    logger.info(
                        "Dropping duplicate {} '{}' from {} (identical to {} "
                        "after renaming)",
                        dup.kind,
                        dup.primary,
                        dup.module,
                        canon.module,
                    )
                    changed = True
            if not changed:
                break
            kept = [b for b in kept if b.bid not in self.canon]
        if self.canon:
            self._flatten_canon()
        # Renames of dropped blocks are no longer visible in the output.
        live = {
            fin
            for b in kept
            for n in b.names
            if (fin := self.final.get((b.bid, n))) is not None
        }
        self._renamed_log = [r for r in self._renamed_log if r[2] in live]
        return kept

    def _assign_names(self, kept: list[Block], emit_keys: set[tuple]) -> None:
        ext_loc_modules: dict[str, set[str]] = defaultdict(set)
        for m, env in self.env.items():
            for local, tgt in env.items():
                if tgt[0] == "ext" and tgt[1] in emit_keys:
                    ext_loc_modules[local].add(m)
        ext_locals = {_ext_local(k) for k in emit_keys if _ext_local(k)}
        guard_names = {
            s.local_name
            for mi in self.modules.values()
            for g in mi.guards
            for s in g.specs
            if s.local_name and not self._is_internal(s)
        }
        group: dict[str, list[Block]] = defaultdict(list)
        for b in kept:
            for n in b.names:
                group[n].append(b)
        taken: set[str] = set(ext_locals) | guard_names
        # Handle main_name override: treat 'main' as needing rename to main_name
        # First, if main_name is set, we will ensure that any block named 'main' is mapped to main_name
        # (with collision handling if multiple mains exist)
        main_override = self.main_name

        # Detect collisions between external and local names
        for name in sorted(group):
            blocks = sorted(
                group[name], key=lambda x: (x.module.count("."), x.module, x.order)
            )
            # If this is 'main' and we have an override, we want final name to be main_name (not original)
            # But still need collision handling for multiple mains
            is_main_override = name == "main" and main_override is not None
            for i, b in enumerate(blocks):
                clash = (
                    bool(ext_loc_modules.get(name, set()) - {b.module})
                    or name in guard_names
                )
                # For main override, we always want to map to main_name (or with prefix if collision)
                if is_main_override:
                    if i == 0 and not clash and main_override not in taken:
                        cand = main_override
                    else:
                        # Multiple mains with override: need disambiguation
                        base_name = main_override
                        cand = (
                            f"{base_name}_{self._prefix(b.module)}"
                            if i != 0
                            else base_name
                        )
                        base, n = cand, 2
                        while cand in taken or cand in group:
                            cand, n = f"{base}_{n}", n + 1
                        logger.info(
                            "Renaming {} 'main' in {} -> '{}' (main-name override)",
                            b.kind,
                            b.module,
                            cand,
                        )
                        if i == 0:
                            logger.info("Renaming main -> {} (via --main-name)", cand)
                    taken.add(cand)
                    self.final[(b.bid, name)] = cand
                    if cand != name:
                        self._renamed_log.append((b.module, name, cand))
                    continue

                if i == 0 and not clash and name not in taken:
                    cand = name
                else:
                    # Apply rename_strategy
                    if self.rename_strategy == "error":
                        raise RuntimeError(
                            f"Name collision on '{name}' between {', '.join(x.module for x in blocks)} "
                            f"and strategy is 'error' — aborting"
                        )
                    lead = name[: len(name) - len(name.lstrip("_"))]
                    raw = name.lstrip("_")
                    if self.rename_strategy == "prefix":
                        cand = f"{lead}{self._prefix(b.module)}_{raw}"
                    elif self.rename_strategy == "suffix":
                        cand = f"{lead}{raw}_{self._suffix(b.module)}"
                    elif self.rename_strategy == "module_path":
                        cand = f"{lead}{self._module_path_prefix(b.module)}_{raw}"
                    else:
                        cand = f"{lead}{self._prefix(b.module)}_{raw}"
                    base, n = cand, 2
                    while cand in taken or cand in group:
                        cand, n = f"{base}_{n}", n + 1
                    logger.info(
                        "Renaming {} '{}' in {} -> '{}' (strategy={})",
                        b.kind,
                        name,
                        b.module,
                        cand,
                        self.rename_strategy,
                    )
                taken.add(cand)
                self.final[(b.bid, name)] = cand
                if cand != name:
                    self._renamed_log.append((b.module, name, cand))
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
                logger.info(
                    "Import conflict on '{}': aliasing {} as '{}'", loc, k[1:], cand
                )

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
                    lines = [
                        render_import_line(s)
                        for s in occ.specs
                        if not self._is_internal(s)
                    ]
                    edits.append(
                        (
                            occ.start,
                            occ.end,
                            ("\n" + occ.obj).join(lines) if lines else "pass",
                        )
                    )
                continue
            if occ.kind == "name":
                new = self._target_name(env.get(occ.name), occ.name)
                if new is None or new == occ.name:
                    continue
            else:
                new = self._target_name(
                    self._resolve_dotted(b.module, occ.obj, occ.name), None
                )
                if new is None:
                    continue
            edits.append((occ.start, occ.end, new))
        edits.sort(key=lambda e: (e[0], -e[1]))
        out, pos, last_end = [], 0, -1
        for s, e, r in edits:
            if s < last_end:
                # Overlapping edit: keep the outermost rewrite (the inner one
                # was already covered) but never drop silently.
                self._dropped_edits += 1
                logger.debug(
                    "Dropping overlapping rewrite in {} at bytes {}-{} ({!r})",
                    b.module,
                    s,
                    e,
                    r,
                )
                continue
            out.append(b.source[pos:s])
            out.append(r.encode())
            pos = last_end = e
        out.append(b.source[pos:])
        text = b"".join(out).decode("utf-8", "replace").strip("\n").rstrip()
        return text

    def _render_all(self, kept: list[Block]) -> dict[int, str]:
        """Render every kept block, optionally in parallel.

        Thread safety relies on a strict phase separation: :meth:`build` does
        all mutation (environments, renaming, ``final``/``ext_final`` maps)
        before this method runs, and rendering then only *reads* that state.
        Any future change that mutates shared state during rendering must not
        use the thread pool.

        Args:
            kept: Blocks that survive dedupe, in discovery order.

        Returns:
            Mapping of block id to rendered source text.

        """
        if self.workers > 1 and len(kept) > 32:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                texts = list(pool.map(self._render_block, kept))
        else:
            texts = [self._render_block(b) for b in kept]
        return {b.bid: t for b, t in zip(kept, texts)}

    def _check_module_value_usage(self, kept: list[Block]) -> None:
        """Fail when a project module object is used as a plain value.

        Attribute access on an imported module (``utils.helper()``) is rewritten
        to the consolidated name, but a bare use (``print(utils)``,
        ``vars(utils)``) has no consolidated equivalent: the module is pruned
        and the name would raise ``NameError`` at runtime. Failing loudly is
        the only safe behaviour.

        Args:
            kept: Blocks that will be emitted.

        Raises:
            ResolverError: If a module alias is referenced without an attribute.

        """
        reported: set[str] = set()
        for b in kept:
            attr_starts = {occ.start for occ in b.occs if occ.kind == "attr"}
            for occ in b.occs:
                if occ.kind != "name" or occ.start in attr_starts:
                    continue
                target = self.env[b.module].get(occ.name)
                if target is None or target[0] != "mod":
                    continue
                if self._target_name(target, occ.name) is not None:
                    continue
                key = f"{b.module}:{occ.name}"
                if key in reported:
                    continue
                reported.add(key)
                raise ResolverError(
                    f"{b.module}: module '{occ.name}' (from '{target[1]}') is used as a "
                    "value, which cannot survive consolidation — use --preserve-modules "
                    "or replace it with explicit attribute access"
                )

    def _warn_stale_forward_refs(self, kept: list[Block]) -> None:
        """Warn about string annotations that mention a renamed object.

        ``def f(x: "Foo")`` keeps ``Foo`` inside a string, so renaming the
        class leaves a stale reference that breaks ``typing.get_type_hints``.

        Args:
            kept: Blocks that will be emitted.

        """
        renamed = {orig: final for _mod, orig, final in self._renamed_log}
        if not renamed:
            return
        for b in kept:
            text = b.source.decode("utf-8", "replace")
            hits: set[str] = set()
            for match in _STRING_RE.finditer(text):
                body = match.group("body")
                if not _ANNOTATION_LIKE_RE.match(body):
                    continue
                hits.update(n for n in _IDENT_RE.findall(body) if n in renamed)
            if hits:
                self._warn_once(
                    f"{b.module}: string literal(s) reference renamed name(s) "
                    f"{', '.join(sorted(hits))} — forward references in annotations "
                    "may be stale"
                )

    def _strip_internal_imports(self, guard: GuardBlock) -> str:
        """Remove hoisted internal imports from a guard's verbatim text.

        Args:
            guard: The guard whose internal import lines should be removed.

        Returns:
            The remaining guard text, or an empty string when nothing
            meaningful is left (the guard should then be dropped).

        """
        internal = [
            (spec, snippet)
            for spec, snippet in guard.spec_lines
            if self._is_internal(spec)
        ]
        if not internal:
            return guard.text
        internal.sort(key=lambda pair: -len(pair[1]))
        # Removing a statement can leave an empty suite (``try:``/``except:``),
        # so try the clean removal first and fall back to ``pass``.
        for replacement in ("", "pass"):
            text = guard.text
            for _spec, snippet in internal:
                if replacement:
                    index = text.find(snippet)
                    indent = ""
                    if index != -1:
                        line_start = text.rfind("\n", 0, index) + 1
                        indent = text[line_start:index]
                    text = text.replace(indent + snippet, indent + replacement, 1)
                else:
                    text = text.replace(snippet, "", 1)
            lines = [line for line in text.splitlines() if line.strip()]
            meaningful = [line for line in lines if not _GUARD_HEADER_RE.match(line)]
            if not meaningful:
                return ""
            candidate = "\n".join(lines).rstrip()
            try:
                compile(candidate + "\n", "<guarded imports>", "exec")
            except SyntaxError:
                continue
            return candidate
        return ""

    def _assemble(
        self, kept: list[Block], texts: dict[int, str], emit_keys: set[tuple]
    ) -> Plan:
        keyfn = lambda b: (self.mod_idx[b.module], b.order)  # noqa: E731
        body = [b for b in kept if b.kind != MAIN]
        mains = sorted((b for b in kept if b.kind == MAIN), key=keyfn)
        deps: dict[int, set[int]] = {
            b.bid: {self._canon(d).bid for d in self.eager[b.bid]} - {b.bid}
            for b in body
        }
        dependents: dict[int, set[int]] = defaultdict(set)
        for bid, ds in deps.items():
            for d in ds:
                dependents[d].add(bid)
        tail = [
            b
            for b in body
            if b.kind == FUNC and b.primary == "main" and not dependents[b.bid]
        ]
        tail_ids = {b.bid for b in tail}
        head = [b for b in body if b.bid not in tail_ids]
        ordered = topo_order(
            head,
            deps,
            lambda b: _RANK[b.kind],
            lambda b: (_RANK[b.kind], *keyfn(b)),
            strict=self.strict,
        )
        tail_ordered = topo_order(tail, deps, lambda b: 0, keyfn, strict=self.strict)
        rendered: list[RenderedBlock] = []
        for b in ordered:
            txt = texts[b.bid]
            # Preserve-modules / source_map prefixes are added in writer; but we can embed origin here
            origin = f"{b.orig_path}:{b.start_line}-{b.end_line}" if b.orig_path else ""
            rendered.append(
                RenderedBlock(
                    _SECTION[b.kind], b.kind, txt, origin=origin, module=b.module
                )
            )
        for b in tail_ordered + mains:
            txt = texts[b.bid]
            origin = f"{b.orig_path}:{b.start_line}-{b.end_line}" if b.orig_path else ""
            rendered.append(
                RenderedBlock("entry", b.kind, txt, origin=origin, module=b.module)
            )
        # Handle generate_guard: if requested and no mains, create one calling main()
        if self.generate_guard and not mains:
            generated = self._generated_guard(kept, emit_keys)
            if generated is not None:
                rendered.append(
                    RenderedBlock(
                        "entry", MAIN, generated, origin="generated", module="__main__"
                    )
                )
                logger.info("Auto-generated __main__ guard")
        if len(mains) > 1:
            if self.entry_keep_all:
                logger.info("{} '__main__' guards kept (--entry-keep-all)", len(mains))
            else:
                logger.warning(
                    "{} '__main__' guards were kept; use --entry to keep only one",
                    len(mains),
                )
        guards: list[str] = []
        for mi in self.modules.values():
            for g in mi.guards:
                text = self._strip_internal_imports(g) if g.internal else g.text
                if not text:
                    continue
                if text not in guards:
                    guards.append(text)
        plan = Plan(
            future_names=sorted(self.future_names),
            imports_text=build_import_section(emit_keys, self.ext_final),
            guards=guards,
            blocks=rendered,
            renamed=self._renamed_log,
            deduped=self._deduped_log,
        )
        self._exports(plan, kept)
        return plan

    def _generated_guard(self, kept: list[Block], emit_keys: set[tuple]) -> str | None:
        """Build a ``if __name__ == "__main__":`` guard for the output.

        Handles renamed ``main`` functions and async entry points, which must be
        driven through ``asyncio.run`` instead of being called unawaited.

        Args:
            kept: Blocks that will be emitted.
            emit_keys: Import keys to emit; ``asyncio`` is added when needed.

        Returns:
            The guard source, or ``None`` when no entry point was found.

        """
        wanted = self.main_name or "main"
        candidates = [b for b in kept if b.kind == FUNC and wanted in b.names]
        if not candidates:
            return None
        block = candidates[0]
        name = self.final.get((block.bid, wanted)) or wanted
        if _ASYNC_DEF_RE.match(block.source.strip()):
            emit_keys.add(("import", "asyncio", None))
            return 'if __name__ == "__main__":\n' f"    asyncio.run({name}())"
        return f'if __name__ == "__main__":\n    {name}()'

    def _exports(self, plan: Plan, kept: list[Block]) -> None:
        """Compute the public API re-exported by the generated ``__init__.py``.

        Applies ``--all`` modes, keeps dunder metadata attributes, honours
        explicitly declared names, and records aliased re-exports from package
        ``__init__`` modules (``from .sub import helper as public_helper``).

        Args:
            plan: Plan being assembled; ``exports`` and ``reexports`` are filled.
            kept: Blocks that will be emitted.

        """
        allowed: set[str] | None = None
        declared_all_union: set[str] = set()
        # An explicitly declared empty __all__ must not look like "no __all__".
        declared_any = any(mi.declared_all is not None for mi in self.modules.values())
        for mi in self.modules.values():
            if mi.declared_all is not None:
                declared_all_union.update(mi.declared_all)
        explicit = set(declared_all_union)

        if self.all_mode == "respect" or (
            self.respect_all and self.all_mode == "ignore" and declared_any
        ):
            # respect: only those in declared_all
            allowed = set()
            for m, mi in self.modules.items():
                for n in mi.declared_all or ():
                    tgt = self._resolve_symbol(m, n)
                    if tgt and tgt[0] == "def":
                        fin = self.final.get((self._canon(tgt[1]).bid, tgt[2]))
                        if fin:
                            allowed.add(fin)
                    elif tgt and tgt[0] == "ext":
                        # re-export case
                        allowed.add(n)
                    else:
                        # Computed __all__ member missing — warn
                        self._warn_once(
                            f"__all__ member '{n}' in {m} cannot be resolved — skipped"
                        )
            if not allowed and declared_all_union:
                logger.warning(
                    "No __all__ members could be resolved; exports will be empty"
                )

        def visible(name: str) -> bool:
            """Return whether ``name`` may appear in the generated ``__all__``."""
            if name == "__all__":
                return False
            if name in explicit or name in DUNDER_EXPORTS:
                return True
            return not name.startswith("_")

        groups = {CLASS: "classes", FUNC: "functions"}
        seen: set[str] = set()
        for b in kept:
            if b.kind == MAIN:
                continue
            for n in b.names:
                fin = self.final.get((b.bid, n))
                if fin is None:
                    continue
                if not visible(n) or fin in seen:
                    continue
                # respect filter
                if allowed is not None and fin not in allowed:
                    continue
                seen.add(fin)
                plan.exports[groups.get(b.kind, "constants")].append(fin)

        # Aliased re-exports performed by package __init__ modules.
        for _module, local, tgt in self._reexport_specs:
            if not visible(local):
                continue
            if allowed is not None and local not in allowed:
                continue
            if tgt[0] == "def":
                block = self._canon(tgt[1])
                fin = self.final.get((block.bid, tgt[2]))
                if not fin:
                    continue
                group = groups.get(block.kind, "constants")
            elif tgt[0] == "ext":
                fin = self.ext_final.get(tgt[1], local)
                group = "constants"
            else:
                continue
            exported = local
            if exported in seen:
                continue
            seen.add(exported)
            plan.exports[group].append(exported)
            if fin != exported:
                plan.reexports[exported] = fin

        # For merge, add any declared_all members not yet exported (including re-exports)
        if self.all_mode == "merge":
            for n in sorted(declared_all_union):
                for m, mi in self.modules.items():
                    if mi.declared_all and n in mi.declared_all:
                        tgt = self._resolve_symbol(m, n)
                        if tgt and tgt[0] == "def":
                            fin = self.final.get((self._canon(tgt[1]).bid, tgt[2]))
                            if fin and fin not in seen:
                                seen.add(fin)
                                blk = tgt[1]
                                plan.exports[groups.get(blk.kind, "constants")].append(
                                    fin
                                )
                        elif tgt and tgt[0] == "ext":
                            if n not in seen:
                                seen.add(n)
                                plan.exports["constants"].append(n)
        for names in plan.exports.values():
            names.sort(key=lambda x: (x.lower(), x))
        # Handle renaming of main for exports (if main is exported, use new name)
        if self.main_name:
            for k in plan.exports:
                plan.exports[k] = [
                    self.main_name if n == "main" else n for n in plan.exports[k]
                ]
