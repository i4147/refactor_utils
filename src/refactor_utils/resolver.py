"""Cross-module resolution: name collisions, import rewriting, dedupe, ordering."""

from __future__ import annotations
import re
import textwrap
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Iterable
from loguru import logger
from .imports import build_import_section, render_import_line
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
    dotted_sort_key,
    ident_sort_key,
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
        self.modules = dict(
            sorted(modules.items(), key=lambda kv: dotted_sort_key(kv[0]))
        )
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
        self.tc_blocks: list[Block] = []
        for mi in self.modules.values():
            for b in mi.blocks:
                if b.kind == MAIN and self.entry and not self._entry_match(b.module):
                    # If entry_keep_all, keep all mains regardless; otherwise filter
                    if not self.entry_keep_all:
                        continue
                b.bid = len(self.blocks)
                self.blocks.append(b)
        for mi in self.modules.values():
            for tb in mi.tc_blocks:
                tb.bid = len(self.blocks) + len(self.tc_blocks)
                self.tc_blocks.append(tb)
        self.defs_in_mod: dict[tuple[str, str], list[Block]] = defaultdict(list)
        self.def_block: dict[tuple[str, str], Block] = {}
        for b in self.blocks:
            for n in b.names:
                self.defs_in_mod[(b.module, n)].append(b)
                self.def_block[(b.module, n)] = b
        self.multi_def_keys: set[tuple[str, str]] = {
            k for k, bs in self.defs_in_mod.items() if len(bs) > 1
        }
        self.tc_def_block: dict[tuple[str, str], Block] = {}
        for tb in self.tc_blocks:
            for n in tb.names:
                self.tc_def_block[(tb.module, n)] = tb
        self.future_names: set[str] = set()
        self.module_futures: dict[str, set[str]] = {}
        self.import_table: dict[str, dict[str, ImportSpec]] = {}
        self.star_specs: dict[str, list[ImportSpec]] = {}
        self.tc_import_table: dict[str, dict[str, ImportSpec]] = {}
        self.tc_star_specs: dict[str, list[ImportSpec]] = {}
        self._reexport_specs: list[tuple[str, str, Target]] = []
        self._verbatim_keys: set[tuple] = set()
        # Names bound by imports *inside* blocks (function bodies) must not be
        # hoisted to the module import section: keeping them local preserves
        # optional-dependency degradation (try/except around the import).
        self.nested_locals: dict[int, set[str]] = {}
        self._nested_ext_keys: set[tuple] = set()
        self._module_nested_guard_names: set[str] = set()
        self._dropped_edits = 0
        for m, mi in self.modules.items():
            table: dict[str, ImportSpec] = {}
            stars: list[ImportSpec] = []
            tc_table: dict[str, ImportSpec] = {}
            tc_stars: list[ImportSpec] = []
            futures = set(mi.future_names)
            for spec in mi.imports:
                if spec.module == "__future__":
                    if spec.name:
                        self.future_names.add(spec.name)
                        futures.add(spec.name)
                    continue
                if spec.type_checking:
                    if spec.star:
                        tc_stars.append(spec)
                    else:
                        tc_table.setdefault(spec.local_name, spec)
                elif spec.star:
                    stars.append(spec)
                else:
                    table.setdefault(spec.local_name, spec)
            self.import_table[m] = table
            self.star_specs[m] = stars
            self.tc_import_table[m] = tc_table
            self.tc_star_specs[m] = tc_stars
            self.module_futures[m] = futures
        self.env: dict[str, dict[str, Target]] = {}
        self.tc_env: dict[str, dict[str, Target]] = {}
        self.tc_ext_keys: set[tuple] = set()
        self._tc_only_env_names: dict[str, set[str]] = defaultdict(set)
        self._tc_used_runtime_ext_keys: set[tuple] = set()
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
            return [
                n
                for n in mi.declared_all
                if n != "__all__" and n not in mi.deleted_names
            ]
        names = [
            n
            for (m, n), blk in self.def_block.items()
            if m == module
            and blk.binds_value
            and not n.startswith("_")
            and n not in mi.deleted_names
        ]
        names += [
            n
            for n, spec in self.import_table.get(module, {}).items()
            if n
            and not n.startswith("_")
            and n not in mi.deleted_names
            and self._is_internal(spec)
        ]
        return list(dict.fromkeys(names))

    def _can_resolve_internal_spec(self, spec: ImportSpec) -> bool:
        """Return whether an internal import resolves within consolidated modules."""
        if not self._is_internal(spec):
            return False
        if spec.star:
            return spec.module in self.modules
        if spec.is_from and spec.name == "__all__":
            return spec.module in self.modules
        return self._spec_target(spec, set()) is not None

    def _spec_target(
        self, spec: ImportSpec, seen: set, *, include_tc: bool = False
    ) -> Target | None:
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
            tgt = self._resolve_symbol(
                spec.module, spec.name, seen, include_tc=include_tc
            )
            if tgt is None and f"{spec.module}.{spec.name}" in self.known:
                tgt = ("mod", f"{spec.module}.{spec.name}")
            return tgt
        return ("ext", ("from", spec.module, spec.name, spec.alias))

    def _resolve_symbol(
        self,
        module: str,
        name: str,
        seen: set | None = None,
        *,
        include_tc: bool = False,
    ) -> Target | None:
        if name == "__all__":
            return None
        blk = self.def_block.get((module, name))
        if blk is not None:
            return ("def", blk, name)
        if include_tc:
            tc_blk = self.tc_def_block.get((module, name))
            if tc_blk is not None:
                return ("def", tc_blk, name)
        seen = set() if seen is None else seen
        if (module, name) in seen:
            return None
        seen.add((module, name))
        spec = self.import_table.get(module, {}).get(name)
        if spec is None and include_tc:
            spec = self.tc_import_table.get(module, {}).get(name)
        if spec is not None:
            tgt = self._spec_target(spec, seen, include_tc=include_tc)
            if tgt is not None:
                return tgt
        stars = list(self.star_specs.get(module, ()))
        if include_tc:
            stars.extend(self.tc_star_specs.get(module, ()))
        for star in stars:
            if self._is_internal(star) and name in self._star_names(star.module):
                tgt = self._resolve_symbol(
                    star.module, name, seen, include_tc=include_tc
                )
                if tgt is not None:
                    return tgt
        sub = f"{module}.{name}"
        if sub in self.known:
            return ("mod", sub)
        return None

    def _resolve_dotted(
        self,
        module: str,
        obj: str,
        attr: str,
        *,
        env_map: dict[str, Target] | None = None,
        include_tc: bool = False,
    ) -> Target | None:
        parts = obj.split(".")
        lookup = env_map if env_map is not None else self.env[module]
        tgt = lookup.get(parts[0])
        if not tgt or tgt[0] != "mod":
            return None
        mod = tgt[1]
        for seg in parts[1:]:
            cand = f"{mod}.{seg}"
            if cand not in self.known:
                return None
            mod = cand
        res = self._resolve_symbol(mod, attr, include_tc=include_tc)
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
        modules = sorted({b.module for b in kept}, key=dotted_sort_key)
        if not modules:
            return []
        sets = {m: self.module_futures.get(m, set()) for m in modules}
        common = set.intersection(*(s for s in sets.values()))
        for name in sorted(set().union(*sets.values()) - common, key=ident_sort_key):
            missing = [m for m in modules if name not in sets[m]]
            self._warn_once(
                f"from __future__ import {name}: not used by every module "
                f"({', '.join(missing)}) — not emitted for the consolidated file"
            )
        return sorted(common, key=ident_sort_key)

    def _collect_reexports(self) -> set[tuple]:
        """Collect public re-exports performed by package ``__init__`` modules.

        The canonical pattern ``from .sub import helper as public_helper`` in
        ``__init__.py`` never creates a block, so the name used to disappear
        from the generated ``__init__.py``. Those specs are recorded here and
        turned into explicit exports in :meth:`_exports`. Plain external imports
        in ``__init__.py`` (``import os``, ``from typing import TYPE_CHECKING``)
        are only treated as re-exports when explicitly listed in ``__all__`` or
        written with PEP 484 redundant aliasing (``from pkg import X as X``).

        Returns:
            External import keys that must be emitted so the re-exports resolve.

        """
        self._reexport_specs = []
        ext_keys: set[tuple] = set()
        for module, mi in self.modules.items():
            declared = (
                set(mi.declared_all or ()) if self.all_mode != "recompute" else set()
            )
            if mi.is_pkg:
                for local, spec in self.import_table.get(module, {}).items():
                    if not local or local == "__all__" or spec.star:
                        continue
                    if (
                        local.startswith("_")
                        and local not in declared
                        and local not in DUNDER_EXPORTS
                    ):
                        continue
                    tgt = self._spec_target(spec, set())
                    if tgt is None or tgt[0] == "mod":
                        continue
                    if tgt[0] == "ext":
                        explicit_alias = (
                            spec.is_from
                            and spec.alias is not None
                            and spec.alias == spec.name
                        )
                        if local not in declared and not explicit_alias:
                            continue
                        ext_keys.add(tgt[1])
                    self._reexport_specs.append((module, local, tgt))
                for star in self.star_specs.get(module, ()):
                    if not self._is_internal(star):
                        continue
                    for nm in self._star_names(star.module):
                        if nm == "__all__" or (
                            nm.startswith("_")
                            and nm not in declared
                            and nm not in DUNDER_EXPORTS
                        ):
                            continue
                        tgt = self._resolve_symbol(star.module, nm)
                        if tgt is None or tgt[0] == "mod":
                            continue
                        if tgt[0] == "ext":
                            ext_keys.add(tgt[1])
                        self._reexport_specs.append((module, nm, tgt))
            elif declared:
                # Submodule explicitly re-exporting an imported symbol via __all__
                for local, spec in self.import_table.get(module, {}).items():
                    if not local or local == "__all__" or local not in declared:
                        continue
                    tgt = self._spec_target(spec, set())
                    if tgt is None or tgt[0] == "mod":
                        continue
                    if tgt[0] == "ext":
                        ext_keys.add(tgt[1])
                        self._reexport_specs.append((module, local, tgt))
                    elif tgt[0] == "def" and local != tgt[2]:
                        self._reexport_specs.append((module, local, tgt))
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

    def _harvest_internal_all_imports(self) -> None:
        """Propagate ``from .sub import __all__`` declarations across modules."""
        changed = True
        while changed:
            changed = False
            for m, mi in self.modules.items():
                for spec in mi.imports:
                    if (
                        not spec.is_from
                        or spec.star
                        or spec.name != "__all__"
                        or spec.alias is not None
                        or not self._is_internal(spec)
                    ):
                        continue
                    src_mi = self.modules.get(spec.module)
                    if src_mi is None or src_mi.declared_all is None:
                        continue
                    cur = list(mi.declared_all or [])
                    before = len(cur)
                    for item in src_mi.declared_all:
                        if (
                            item != "__all__"
                            and item not in cur
                            and self._resolve_symbol(m, item) is not None
                        ):
                            cur.append(item)
                    if (mi.declared_all is None and cur) or len(cur) != before:
                        mi.declared_all = cur
                        changed = True

    def _build_envs(self) -> None:
        self._harvest_internal_all_imports()
        for m, mi in self.modules.items():
            env: dict[str, Target] = {}
            for (mm, n), blk in self.def_block.items():
                if mm == m:
                    env[n] = ("def", blk, n)
            specs: list[ImportSpec] = [
                s
                for s in mi.imports
                if s.module != "__future__" and not s.type_checking
            ]
            for b in mi.blocks:
                for occ in b.occs:
                    if occ.kind == "nested_import":
                        specs.extend(
                            s
                            for s in occ.specs
                            if s.module != "__future__"
                            and self._can_resolve_internal_spec(s)
                        )
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
                        and not self._can_resolve_internal_spec(spec)
                    }
                    for spec in occ.specs:
                        if (
                            spec.module == "__future__"
                            or self._can_resolve_internal_spec(spec)
                        ):
                            continue
                        self._nested_ext_keys.add(
                            ("from", spec.module, spec.name, spec.alias)
                            if spec.is_from
                            else ("import", spec.module, spec.alias)
                        )
                self.nested_locals[b.bid] = local
                if b.kind == MISC and local:
                    self._module_nested_guard_names |= local
            for spec in specs:
                if (
                    spec.is_from
                    and not spec.star
                    and spec.name == "__all__"
                    and self._is_internal(spec)
                ):
                    continue
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
                        if spec.is_from and not spec.star and spec.name == "__all__":
                            continue
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

        # Second pass: build TYPE_CHECKING environments without polluting runtime imports
        for m, mi in self.modules.items():
            env = self.env[m]
            tc_env: dict[str, Target] = dict(env)
            for (mm, n), tblk in self.tc_def_block.items():
                if mm == m:
                    tc_env.setdefault(n, ("def", tblk, n))
            tc_specs = [
                s for s in mi.imports if s.module != "__future__" and s.type_checking
            ]
            for tb in mi.tc_blocks:
                for occ in tb.occs:
                    if occ.kind == "nested_import":
                        tc_specs.extend(
                            s
                            for s in occ.specs
                            if s.module != "__future__"
                            and self._can_resolve_internal_spec(s)
                        )
            for spec in tc_specs:
                if (
                    spec.is_from
                    and not spec.star
                    and spec.name == "__all__"
                    and self._is_internal(spec)
                ):
                    continue
                if spec.star:
                    if self._is_internal(spec):
                        for name in self._star_names(spec.module):
                            tgt = self._resolve_symbol(
                                spec.module, name, include_tc=True
                            )
                            if tgt is not None:
                                tc_env.setdefault(name, tgt)
                                if tgt[0] == "def" and name not in env:
                                    env[name] = tgt
                                    self._tc_only_env_names[m].add(name)
                    else:
                        self.tc_ext_keys.add(("star", spec.module))
                    continue
                tgt = self._spec_target(spec, set(), include_tc=True)
                if tgt is None:
                    if self._is_internal(spec):
                        tgt = self._verbatim_ext_target(spec)
                        self.tc_ext_keys.add(tgt[1])
                    else:
                        continue
                tc_env.setdefault(spec.local_name, tgt)
                if tgt[0] == "ext":
                    self.tc_ext_keys.add(tgt[1])
                elif (
                    tgt[0] == "def"
                    and spec.local_name
                    and spec.local_name not in env
                    and tgt[1] in self.blocks
                ):
                    env[spec.local_name] = tgt
                    self._tc_only_env_names[m].add(spec.local_name)
            self.tc_env[m] = tc_env

    def _analyse(self) -> None:
        extra_eager: dict[int, set[Block]] = defaultdict(set)
        for b in self.blocks:
            env = self.env[b.module]
            tc_only = self._tc_only_env_names.get(b.module, set())
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
                    def_blk: Block = tgt[1]
                    orig_nm: str = tgt[2]
                    if (
                        def_blk.module == b.module
                        and (b.module, orig_nm) in self.multi_def_keys
                    ):
                        defs = self.defs_in_mod[(b.module, orig_nm)]
                        if b in defs:
                            idx = defs.index(b)
                            if idx > 0:
                                prev_def = defs[idx - 1]
                                proj.add(prev_def)
                                eager.add(prev_def)
                        else:
                            active = next(
                                (d for d in reversed(defs) if d.order < b.order),
                                None,
                            )
                            if active is not None:
                                proj.add(active)
                                if occ.eager:
                                    eager.add(active)
                                    nxt = next(
                                        (d for d in defs if d.order > b.order),
                                        None,
                                    )
                                    if nxt is not None:
                                        extra_eager[nxt.bid].add(b)
                            else:
                                proj.add(defs[-1])
                    elif def_blk is not b:
                        proj.add(def_blk)
                        if occ.eager and not (
                            occ.kind == "name" and occ.name in tc_only
                        ):
                            eager.add(def_blk)
                elif tgt[0] == "ext":
                    ext_keys.add(tgt[1])
                    ext_refs.add((label, tgt[1]))
            self.eager[b.bid], self.proj_refs[b.bid] = eager, proj
            self.ext_keys[b.bid], self.ext_refs[b.bid] = ext_keys, frozenset(ext_refs)
        for bid, deps in extra_eager.items():
            self.eager[bid].update(deps)
        # Track runtime external imports referenced inside TYPE_CHECKING blocks
        self._tc_used_runtime_ext_keys = set()
        for tb in self.tc_blocks:
            env = self.env[tb.module]
            tc_env = self.tc_env[tb.module]
            for occ in tb.occs:
                if occ.kind == "name":
                    tgt = env.get(occ.name)
                elif occ.kind == "attr":
                    tgt = self._resolve_dotted(
                        tb.module, occ.obj, occ.name, env_map=tc_env, include_tc=True
                    )
                else:
                    continue
                if (
                    tgt is not None
                    and tgt[0] == "ext"
                    and tgt[1] not in self.tc_ext_keys
                ):
                    self._tc_used_runtime_ext_keys.add(tgt[1])

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
        used |= self._tc_used_runtime_ext_keys
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

    def _root_reexported_modules(self) -> dict[str, set[str]]:
        """Map symbol name -> defining modules re-exported by root ``__init__``."""
        pkg_mods = [m for m, mi in self.modules.items() if mi.is_pkg]
        if not pkg_mods:
            return {}
        min_depth = min(m.count(".") for m in pkg_mods)
        roots = [m for m in pkg_mods if m.count(".") == min_depth]
        out: dict[str, set[str]] = defaultdict(set)
        for root_mod in roots:
            for local, spec in self.import_table.get(root_mod, {}).items():
                if not local or local == "__all__" or spec.star:
                    continue
                tgt = self._spec_target(spec, set())
                if tgt is not None and tgt[0] == "def" and tgt[2] == local:
                    out[local].add(tgt[1].module)
            for star in self.star_specs.get(root_mod, ()):
                if not self._is_internal(star):
                    continue
                for nm in self._star_names(star.module):
                    tgt = self._resolve_symbol(star.module, nm)
                    if tgt is not None and tgt[0] == "def" and tgt[2] == nm:
                        out[nm].add(tgt[1].module)
        return out

    def _dedupe(self) -> None:
        if self.dedupe_mode == "none":
            logger.info("Dedupe disabled (--dedupe none)")
            return
        root_reexp = self._root_reexported_modules()
        groups: dict[tuple, list[Block]] = defaultdict(list)
        for b in self.blocks:
            if any((b.module, n) in self.multi_def_keys for n in b.names):
                continue
            if self.proj_refs[b.bid]:
                continue
            if self.dedupe_mode == "safe":
                if b.kind in (CONST, CLASS, FUNC):
                    groups[
                        (
                            b.kind,
                            tuple(b.names),
                            b.binds_value,
                            b.source.strip(),
                            self.ext_refs[b.bid],
                        )
                    ].append(b)
            elif self.dedupe_mode == "aggressive":
                if b.kind in (CLASS, FUNC, CONST, MISC):
                    norm = re.sub(rb"\s+", b" ", b.source.strip())
                    key = (
                        b.kind,
                        tuple(b.names),
                        b.binds_value,
                        norm,
                        self.ext_refs[b.bid],
                    )
                    groups[key].append(b)
        for blocks in groups.values():
            if len(blocks) < 2:
                continue
            blocks.sort(
                key=lambda x: (
                    (
                        0
                        if (x.primary and x.module in root_reexp.get(x.primary, ()))
                        else 1
                    ),
                    x.module.count("."),
                    dotted_sort_key(x.module),
                    x.order,
                )
            )
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
        root_reexp = self._root_reexported_modules()
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
                if any((b.module, n) in self.multi_def_keys for n in b.names):
                    continue
                groups[
                    (
                        b.kind,
                        tuple(b.names),
                        b.binds_value,
                        self._block_signature(b),
                    )
                ].append(b)
            changed = False
            for blocks in groups.values():
                if len(blocks) < 2:
                    continue
                blocks.sort(
                    key=lambda x: (
                        (
                            0
                            if (x.primary and x.module in root_reexp.get(x.primary, ()))
                            else 1
                        ),
                        x.module.count("."),
                        dotted_sort_key(x.module),
                        x.order,
                    )
                )
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

    def _disambiguate_name(
        self, name: str, module: str, taken: set[str], reserved: set[str]
    ) -> str:
        """Build a module-scoped name according to ``self.rename_strategy``."""
        lead = name[: len(name) - len(name.lstrip("_"))]
        raw = name.lstrip("_")
        if self.rename_strategy == "prefix":
            cand = f"{lead}{self._prefix(module)}_{raw}"
        elif self.rename_strategy == "suffix":
            cand = f"{lead}{raw}_{self._suffix(module)}"
        elif self.rename_strategy == "module_path":
            cand = f"{lead}{self._module_path_prefix(module)}_{raw}"
        else:
            cand = f"{lead}{self._prefix(module)}_{raw}"
        base, n = cand, 2
        while cand in taken or cand in reserved:
            cand, n = f"{base}_{n}", n + 1
        return cand

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
        } | self._module_nested_guard_names
        group: dict[str, dict[str, list[Block]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for b in kept:
            for n in b.names:
                group[n][b.module].append(b)
        taken: set[str] = set(ext_locals) | guard_names
        reserved_names = set(group)
        root_reexp = self._root_reexported_modules()
        main_override = self.main_name

        # Detect collisions between external and local names
        for name in sorted(group, key=ident_sort_key):
            mod_entries = sorted(
                group[name].items(),
                key=lambda kv: (
                    0 if kv[0] in root_reexp.get(name, ()) else 1,
                    kv[0].count("."),
                    dotted_sort_key(kv[0]),
                    kv[1][0].order,
                ),
            )
            is_main_override = name == "main" and main_override is not None
            for i, (mod, mod_blocks) in enumerate(mod_entries):
                b0 = mod_blocks[0]
                clash = (
                    bool(ext_loc_modules.get(name, set()) - {mod})
                    or name in guard_names
                )
                if is_main_override:
                    if i == 0 and not clash and main_override not in taken:
                        cand = main_override
                    else:
                        base_name = main_override
                        cand = (
                            f"{base_name}_{self._prefix(mod)}" if i != 0 else base_name
                        )
                        base, n = cand, 2
                        while cand in taken or cand in reserved_names:
                            cand, n = f"{base}_{n}", n + 1
                        logger.info(
                            "Renaming {} 'main' in {} -> '{}' (main-name override)",
                            b0.kind,
                            mod,
                            cand,
                        )
                        if i == 0:
                            logger.info("Renaming main -> {} (via --main-name)", cand)
                    taken.add(cand)
                    for blk in mod_blocks:
                        self.final[(blk.bid, name)] = cand
                    if cand != name:
                        self._renamed_log.append((mod, name, cand))
                    continue

                if i == 0 and not clash and name not in taken:
                    cand = name
                else:
                    if self.rename_strategy == "error":
                        mods_str = ", ".join(m for m, _ in mod_entries)
                        raise RuntimeError(
                            f"Name collision on '{name}' between {mods_str} "
                            "and strategy is 'error' — aborting"
                        )
                    cand = self._disambiguate_name(name, mod, taken, reserved_names)
                    logger.info(
                        "Renaming {} '{}' in {} -> '{}' (strategy={})",
                        b0.kind,
                        name,
                        mod,
                        cand,
                        self.rename_strategy,
                    )
                taken.add(cand)
                for blk in mod_blocks:
                    self.final[(blk.bid, name)] = cand
                if cand != name:
                    self._renamed_log.append((mod, name, cand))

        # Assign names for declarations inside TYPE_CHECKING blocks as well
        tc_group: dict[str, dict[str, list[Block]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for tb in self.tc_blocks:
            for n in tb.names:
                tc_group[n][tb.module].append(tb)
        tc_reserved = reserved_names | set(tc_group)
        for name in sorted(tc_group, key=ident_sort_key):
            mod_entries = sorted(
                tc_group[name].items(),
                key=lambda kv: (
                    kv[0].count("."),
                    dotted_sort_key(kv[0]),
                    kv[1][0].order,
                ),
            )
            for i, (mod, mod_blocks) in enumerate(mod_entries):
                if i == 0 and name not in taken:
                    cand = name
                else:
                    cand = self._disambiguate_name(name, mod, taken, tc_reserved)
                taken.add(cand)
                for tb in mod_blocks:
                    self.final[(tb.bid, name)] = cand
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

    def _target_name(
        self,
        tgt: Target | None,
        local: str | None,
        ext_map: dict[tuple, str] | None = None,
    ) -> str | None:
        if tgt is None:
            return None
        if tgt[0] == "def":
            blk = self._canon(tgt[1])
            return self.final.get((blk.bid, tgt[2]))
        if tgt[0] == "ext":
            mapping = ext_map if ext_map is not None else self.ext_final
            return mapping.get(tgt[1], local)
        return None

    def _render_block_with_env(
        self,
        b: Block,
        env: dict[str, Target],
        *,
        ext_map: dict[tuple, str] | None = None,
        include_tc: bool = False,
    ) -> str:
        edits: list[tuple[int, int, str]] = []
        for occ in b.occs:
            if occ.kind == "nested_import":
                internal_any = [s for s in occ.specs if self._is_internal(s)]
                if internal_any:
                    lines = [
                        render_import_line(s)
                        for s in occ.specs
                        if not self._can_resolve_internal_spec(s)
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
                new = self._target_name(env.get(occ.name), occ.name, ext_map)
                if new is None or new == occ.name:
                    continue
            else:
                new = self._target_name(
                    self._resolve_dotted(
                        b.module,
                        occ.obj,
                        occ.name,
                        env_map=env,
                        include_tc=include_tc,
                    ),
                    None,
                    ext_map,
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

    def _render_block(self, b: Block) -> str:
        return self._render_block_with_env(b, self.env[b.module])

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

    def _is_satisfied_by_normal_imports(
        self, tc_key: tuple, emit_keys: set[tuple]
    ) -> bool:
        """Return whether ``tc_key`` is already imported by normal ``emit_keys``."""
        kind = tc_key[0]
        if kind == "star":
            return tc_key in emit_keys
        if kind == "from":
            _, mod, name, alias = tc_key
            tc_local = alias or name
            for k in emit_keys:
                if (
                    k[0] == "from"
                    and k[1] == mod
                    and k[2] == name
                    and self.ext_final.get(k, k[3] or k[2]) == tc_local
                ):
                    return True
            return False
        if kind == "import":
            _, mod, alias = tc_key
            if alias is not None:
                return any(
                    k[0] == "import"
                    and k[1] == mod
                    and self.ext_final.get(k, k[2]) == alias
                    for k in emit_keys
                )
            return any(
                k[0] == "import"
                and self.ext_final.get(k, k[2]) is None
                and (k[1] == mod or k[1].startswith(mod + "."))
                for k in emit_keys
            )
        return False

    def _build_merged_tc_guard(self, emit_keys: set[tuple]) -> str | None:
        """Build a single merged ``if TYPE_CHECKING:`` guard, or ``None`` if empty."""
        remaining_tc = {
            k
            for k in self.tc_ext_keys
            if not self._is_satisfied_by_normal_imports(k, emit_keys)
        }
        if not remaining_tc and not self.tc_blocks:
            return None

        # Ensure TYPE_CHECKING (or typing.TYPE_CHECKING) is bound in normal imports
        has_tc_name = any(
            k[0] == "from"
            and k[2] == "TYPE_CHECKING"
            and self.ext_final.get(k, k[3] or k[2]) == "TYPE_CHECKING"
            for k in emit_keys
        )
        tc_cond = "TYPE_CHECKING"
        if not has_tc_name:
            if "TYPE_CHECKING" not in self._taken:
                # Prefer typing_extensions if a module originally imported it from there
                tc_src_mod = "typing"
                for mi in self.modules.values():
                    if any(
                        s.is_from
                        and s.name == "TYPE_CHECKING"
                        and s.module == "typing_extensions"
                        for s in mi.imports
                    ):
                        tc_src_mod = "typing_extensions"
                        break
                emit_keys.add(("from", tc_src_mod, "TYPE_CHECKING", None))
                remaining_tc = {
                    k
                    for k in remaining_tc
                    if not self._is_satisfied_by_normal_imports(k, emit_keys)
                }
            else:
                emit_keys.add(("import", "typing", None))
                tc_cond = "typing.TYPE_CHECKING"

        if not remaining_tc and not self.tc_blocks:
            return None

        # Resolve any alias collisions inside remaining_tc
        tc_ext_final: dict[tuple, str] = dict(self.ext_final)
        by_local: dict[str, list[tuple]] = defaultdict(list)
        for key in remaining_tc:
            loc = _ext_local(key)
            if loc:
                by_local[loc].append(key)
        taken_tc = set(self._taken)
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
                    tc_ext_final[k] = aliased[ident]
                    continue
                base = _sanitize(k[1]) + (f"_{k[2]}" if k[0] == "from" else "")
                cand, n = base, 2
                while cand in taken_tc:
                    cand, n = f"{base}_{n}", n + 1
                taken_tc.add(cand)
                aliased[ident] = cand
                tc_ext_final[k] = cand

        suite_parts: list[str] = []
        if remaining_tc:
            tc_imp = build_import_section(remaining_tc, tc_ext_final)
            if tc_imp:
                suite_parts.append(textwrap.indent(tc_imp, "    "))
        if self.tc_blocks:
            tc_rendered: list[str] = []
            seen_tc_texts: set[str] = set()
            for tb in self.tc_blocks:
                txt = self._render_block_with_env(
                    tb,
                    self.tc_env[tb.module],
                    ext_map=tc_ext_final,
                    include_tc=True,
                )
                if txt and txt not in seen_tc_texts:
                    seen_tc_texts.add(txt)
                    tc_rendered.append(textwrap.indent(txt, "    "))
            if tc_rendered:
                suite_parts.append("\n".join(tc_rendered))
        if not suite_parts:
            return None
        return f"if {tc_cond}:\n" + "\n\n".join(suite_parts)

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
        merged_tc = self._build_merged_tc_guard(emit_keys)
        if merged_tc:
            guards.append(merged_tc)
        for mi in self.modules.values():
            for g in mi.guards:
                text = self._strip_internal_imports(g) if g.internal else g.text
                if not text:
                    continue
                if text not in guards:
                    guards.append(text)
        plan = Plan(
            future_names=sorted(self.future_names, key=ident_sort_key),
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
            return f'if __name__ == "__main__":\n    asyncio.run({name}())'
        return f'if __name__ == "__main__":\n    {name}()'

    def _exports(self, plan: Plan, kept: list[Block]) -> None:
        """Compute the public API re-exported by the generated ``__init__.py``.

        Applies ``--all`` modes, keeps dunder metadata attributes, honours
        explicitly declared names, excludes private ``_``-prefixed names unless
        explicitly declared in ``__all__`` (or metadata dunders), and records
        aliased re-exports from package ``__init__`` modules.

        Args:
            plan: Plan being assembled; ``exports`` and ``reexports`` are filled.
            kept: Blocks that will be emitted.

        """
        declared_all_union: set[str] = set()
        declared_any = any(mi.declared_all is not None for mi in self.modules.values())
        explicit_defs: set[tuple[int, str]] = set()
        explicit_reexports: set[tuple[str, str]] = set()

        for m, mi in self.modules.items():
            if mi.declared_all is None:
                continue
            for n in mi.declared_all:
                if n == "__all__":
                    continue
                declared_all_union.add(n)
                if self.all_mode == "recompute":
                    continue
                tgt = self._resolve_symbol(m, n)
                if tgt and tgt[0] == "def":
                    canon_blk = self._canon(tgt[1])
                    if canon_blk.binds_value:
                        explicit_defs.add((canon_blk.bid, tgt[2]))
                    if n != tgt[2]:
                        explicit_reexports.add((m, n))
                elif tgt and tgt[0] == "ext":
                    explicit_reexports.add((m, n))

        use_respect = self.all_mode == "respect" or (
            self.respect_all and self.all_mode == "ignore" and declared_any
        )
        allowed_defs: set[tuple[int, str]] | None = None
        allowed_reexports: set[tuple[str, str]] | None = None
        if use_respect:
            allowed_defs = set()
            allowed_reexports = set()
            for m, mi in self.modules.items():
                for n in mi.declared_all or ():
                    if n == "__all__":
                        continue
                    tgt = self._resolve_symbol(m, n)
                    if tgt and tgt[0] == "def":
                        canon_blk = self._canon(tgt[1])
                        if canon_blk.binds_value:
                            if n == tgt[2] or not mi.is_pkg:
                                allowed_defs.add((canon_blk.bid, tgt[2]))
                            allowed_reexports.add((m, n))
                    elif tgt and tgt[0] == "ext":
                        allowed_reexports.add((m, n))
                    else:
                        self._warn_once(
                            f"__all__ member '{n}' in {m} cannot be resolved — skipped"
                        )
            if not allowed_defs and not allowed_reexports and declared_all_union:
                logger.warning(
                    "No __all__ members could be resolved; exports will be empty"
                )

        def is_def_visible(blk_id: int, orig: str, fin: str) -> bool:
            if orig == "__all__" or fin == "__all__":
                return False
            if fin in DUNDER_EXPORTS:
                return True
            if (blk_id, orig) in explicit_defs:
                return True
            return not orig.startswith("_") and not fin.startswith("_")

        def is_reexport_visible(mod: str, local: str) -> bool:
            if local == "__all__":
                return False
            if local in DUNDER_EXPORTS:
                return True
            if (mod, local) in explicit_reexports:
                return True
            return not local.startswith("_")

        groups = {CLASS: "classes", FUNC: "functions"}
        seen: set[str] = set()
        for b in kept:
            if b.kind == MAIN or not b.binds_value:
                continue
            mod_deleted = self.modules[b.module].deleted_names
            for n in b.names:
                if n in mod_deleted:
                    continue
                fin = self.final.get((b.bid, n))
                if fin is None or fin in seen:
                    continue
                if not is_def_visible(b.bid, n, fin):
                    continue
                if allowed_defs is not None and (b.bid, n) not in allowed_defs:
                    continue
                seen.add(fin)
                nm_kind = b.name_kinds.get(n, b.kind)
                plan.exports[groups.get(nm_kind, "constants")].append(fin)

        # Re-exports from package __init__ modules and explicit __all__ lists.
        for module, local, tgt in self._reexport_specs:
            if tgt[0] == "def":
                block = self._canon(tgt[1])
                if not block.binds_value:
                    continue
                fin = self.final.get((block.bid, tgt[2]))
                if not fin:
                    continue
                is_expl = (module, local) in explicit_reexports or (
                    local == tgt[2] and (block.bid, tgt[2]) in explicit_defs
                )
                if local == "__all__":
                    continue
                if (
                    local.startswith("_")
                    and local not in DUNDER_EXPORTS
                    and not is_expl
                ):
                    continue
                if (
                    allowed_reexports is not None
                    and (module, local) not in allowed_reexports
                ):
                    if not (
                        local == tgt[2]
                        and allowed_defs is not None
                        and (block.bid, tgt[2]) in allowed_defs
                    ):
                        continue
                nm_kind = block.name_kinds.get(tgt[2], block.kind)
                group = groups.get(nm_kind, "constants")
            elif tgt[0] == "ext":
                if not is_reexport_visible(module, local):
                    continue
                if (
                    allowed_reexports is not None
                    and (module, local) not in allowed_reexports
                ):
                    continue
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
            for n in sorted(declared_all_union, key=ident_sort_key):
                for m, mi in self.modules.items():
                    if mi.declared_all and n in mi.declared_all:
                        tgt = self._resolve_symbol(m, n)
                        if tgt and tgt[0] == "def":
                            blk = self._canon(tgt[1])
                            if not blk.binds_value:
                                continue
                            fin = self.final.get((blk.bid, tgt[2]))
                            if fin and fin not in seen:
                                seen.add(fin)
                                nm_kind = blk.name_kinds.get(tgt[2], blk.kind)
                                plan.exports[groups.get(nm_kind, "constants")].append(
                                    fin
                                )
                        elif tgt and tgt[0] == "ext":
                            fin = self.ext_final.get(tgt[1], n)
                            if n not in seen:
                                seen.add(n)
                                plan.exports["constants"].append(n)
                                if fin != n:
                                    plan.reexports[n] = fin
        for names in plan.exports.values():
            names.sort(key=ident_sort_key)
