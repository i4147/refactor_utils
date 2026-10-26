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
