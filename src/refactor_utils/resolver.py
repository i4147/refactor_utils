from __future__ import annotations

import hashlib
import keyword
import re
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import libcst as cst

from .imports import build_import_section, sort_key
from .logging_setup import logger
from .models import CLASS, CONST, FUNC, MAIN, MISC, Block, ModuleInfo
from .ordering import topo_order
from .parser import RefWalker

Target = tuple
_RANK = {CONST: 0, MISC: 0, CLASS: 1, FUNC: 2}
_SECTION = {CONST: "const", MISC: "const", CLASS: "class", FUNC: "func"}


@dataclass
class RenderedBlock:
    section: str
    kind: str
    text: str
    origin: str = ""
    module: str = ""


@dataclass
class Plan:
    exports: dict = field(default_factory=lambda: {"constants": [], "classes": [], "functions": []})
    renamed: list = field(default_factory=list)
    deduped: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    future_names: list = field(default_factory=list)
    imports_text: str = ""
    guards: list = field(default_factory=list)
    blocks: list = field(default_factory=list)
    resources_code: str = ""
    all_names: list = field(default_factory=list)


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


def is_valid_identifier(name: str) -> bool:
    return name.isidentifier() and not keyword.iskeyword(name)


def make_identifier(text: str, fallback: str = "mod") -> str:
    text = unicodedata.normalize("NFKC", text)
    out = "".join(ch if (ch == "_" or ch.isalnum()) and f"a{ch}".isidentifier() else "_" for ch in text)
    if not out.strip("_"):
        out = fallback
    if not out[0].isidentifier():
        out = f"m{out}"
    if keyword.iskeyword(out):
        out += "_"
    return out


def _content_hash(block: Block) -> str:
    normalised = "\n".join(line.rstrip() for line in block.code().strip().splitlines() if line.strip())
    return hashlib.sha1(normalised.encode("utf-8")).hexdigest()


def _guard_body(text: str) -> list[str]:
    lines = text.splitlines()
    header = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("if ") and "__main__" in stripped:
            header = i
            break
    body = lines[header + 1 :] if header is not None else lines
    indents = [len(ln) - len(ln.lstrip()) for ln in body if ln.strip()]
    cut = min(indents) if indents else 0
    return [ln[cut:] if ln.strip() else "" for ln in body]


class Resolver:
    def __init__(
        self,
        modules: dict[str, ModuleInfo],
        *,
        entry: tuple = (),
        workers: int = 1,
        imports_mode: str = "prune",
        entry_keep_all: bool = False,
        main_name: str | None = None,
        generate_guard: bool = False,
        resources: list[tuple[str, bytes]] | None = None,
    ):
        self.modules = dict(sorted(modules.items()))
        self.entry = tuple(entry)
        self.workers = max(1, workers)
        self.imports_mode = imports_mode
        self.entry_keep_all = entry_keep_all
        self.main_name = main_name
        self.generate_guard = generate_guard
        self.resources = resources
        self.known = set(self.modules)
        for m in list(self.modules):
            parts = m.split(".")
            for i in range(1, len(parts)):
                self.known.add(".".join(parts[:i]))
        self.tops = {m.split(".")[0] for m in self.modules}
        self.mod_idx = {m: i for i, m in enumerate(self.modules)}
        self.single_top = next(iter(self.tops)) if len(self.tops) == 1 else None
        self.blocks: list[Block] = []
        for mi in self.modules.values():
            for b in mi.blocks:
                if b.kind == MAIN and self.entry and not self._entry_match(b.module):
                    if not self.entry_keep_all:
                        continue
                b.bid = len(self.blocks)
                self.blocks.append(b)
        self.def_block: dict[tuple[str, str], Block] = {}
        for b in self.blocks:
            for n in b.names:
                self.def_block[(b.module, n)] = b
        self.future_names: set[str] = set()
        self.import_table: dict[str, dict] = {}
        self.star_specs: dict[str, list] = {}
        for m, mi in self.modules.items():
            table: dict = {}
            stars: list = []
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
        self.env: dict[str, dict] = {}
        self.eager: dict[int, set] = {}
        self.proj_refs: dict[int, set] = {}
        self.ext_keys: dict[int, set] = {}
        self.ext_refs: dict[int, frozenset] = {}
        self.canon: dict[int, Block] = {}
        self.final: dict[tuple[int, str], str] = {}
        self.ext_final: dict[tuple, str] = {}
        self.star_keys: set = set()
        self.cond_specs: dict[str, list] = {}
        self.nested_locals: dict[str, set] = {}
        self._warned: set = set()
        self._renamed_log: list = []
        self._deduped_log: list = []

    def _entry_match(self, module: str) -> bool:
        return any(module == e or module.endswith("." + e) for e in self.entry)

    def _warn_once(self, msg: str) -> None:
        if msg not in self._warned:
            self._warned.add(msg)
            logger.warning(msg)

    def _is_internal(self, spec) -> bool:
        if spec.relative:
            return True
        return bool(spec.module) and (spec.module in self.known or spec.module.split(".")[0] in self.tops)

    def _canon(self, b: Block) -> Block:
        return self.canon.get(b.bid, b)

    def _prefix(self, module: str) -> str:
        parts = module.split(".")
        if self.single_top and len(parts) > 1 and parts[0] == self.single_top:
            parts = parts[1:]
        return make_identifier(_sanitize("_".join(parts)) or "mod")

    def _star_names(self, module: str) -> list[str]:
        mi = self.modules.get(module)
        if mi is None:
            return []
        if mi.declared_all is not None:
            return list(mi.declared_all)
        names = [n for (m, n) in self.def_block if m == module and not n.startswith("_")]
        names += [n for n in self.import_table.get(module, {}) if not n.startswith("_")]
        return names

    def _spec_target(self, spec, seen: set | None = None):
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

    def _resolve_symbol(self, module: str, name: str, seen: set | None = None):
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

    def _resolve_dotted(self, module: str, obj: str, attr: str):
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
        if self.imports_mode == "keep":
            for env in self.env.values():
                emit_keys |= {t[1] for t in env.values() if t[0] == "ext"}
        elif self.imports_mode == "strict":
            all_ext = {t[1] for env in self.env.values() for t in env.values() if t[0] == "ext"}
            for k in sorted(all_ext - used, key=repr):
                self._warn_once(f"Strict imports: unused import {k} would be pruned")
        emit_keys |= self.star_keys
        emit_keys = self._drop_guarded_keys(emit_keys)
        self._assign_names(kept, emit_keys)
        self._resolve_ext_conflicts(emit_keys)
        rendered = self._render_all(kept)
        return self._assemble(kept, rendered, emit_keys)

    def _guard_identity(self, spec) -> tuple:
        if spec.star:
            return ("star", spec.module)
        if not spec.is_from:
            return ("import", spec.module, spec.alias)
        return ("from", spec.module, spec.name, spec.alias)

    def _drop_guarded_keys(self, emit_keys: set) -> set:
        provided = {self._guard_identity(spec) for specs in self.cond_specs.values() for spec in specs}
        if not provided:
            return emit_keys
        kept = set()
        for key in emit_keys:
            if key[0] == "star":
                ident = ("star", key[1])
            elif key[0] == "import":
                ident = ("import", key[1], key[2])
            else:
                ident = ("from", key[1], key[2], key[3])
            if ident in provided:
                logger.debug("Import {} already provided by a guard", key)
                continue
            kept.add(key)
        return kept

    def _build_envs(self) -> None:
        defs_by_module: dict[str, dict[str, Block]] = defaultdict(dict)
        for (mm, n), blk in self.def_block.items():
            defs_by_module[mm][n] = blk
        for m, mi in self.modules.items():
            env: dict = {n: ("def", blk, n) for n, blk in defs_by_module.get(m, {}).items()}
            specs = [s for s in mi.imports if s.module != "__future__"]
            nested: list = []
            for b in mi.blocks:
                for ref in b.refs:
                    if ref.kind == "nested_import":
                        nested.extend(s for s in ref.specs if s.module != "__future__")
            nested_set = set(nested)
            nested_locals = self.nested_locals.setdefault(m, set())
            for spec in specs + nested:
                is_nested = spec in nested_set
                if is_nested and spec.local_name in env:
                    continue
                if spec.star:
                    if self._is_internal(spec):
                        for name in self._star_names(spec.module):
                            tgt = self._resolve_symbol(spec.module, name)
                            if tgt is not None:
                                env.setdefault(name, tgt)
                        self._warn_once(
                            f"{m}: local star import from {spec.module} expanded to "
                            f"{len(self._star_names(spec.module))} names"
                        )
                    else:
                        self._warn_once(f"{m}: external star import from {spec.module} — kept as is")
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
                if spec.local_name in env:
                    continue
                env[spec.local_name] = tgt
                if is_nested and tgt[0] == "ext":
                    nested_locals.add(spec.local_name)
            if mi.guards:
                self.cond_specs[m] = [s for g in mi.guards for s in g.specs]
            for g in mi.guards:
                for spec in g.specs:
                    if self._is_internal(spec):
                        self._warn_once(
                            f"{m}: guarded import of internal module '{spec.module}' is kept "
                            "verbatim and will not resolve"
                        )
                    if "ImportError" in g.text or "try:" in g.text:
                        self._warn_once(f"{m}: conditional/optional import guard kept verbatim (may have side effects)")
            self.env[m] = env

    def _analyse(self) -> None:
        for b in self.blocks:
            env = self.env[b.module]
            eager: set = set()
            proj: set = set()
            ext_keys: set = set()
            ext_refs: set = set()
            for ref in b.refs:
                if ref.kind == "name":
                    tgt = env.get(ref.name)
                    label = ref.name
                elif ref.kind == "attr":
                    tgt = self._resolve_dotted(b.module, ref.obj, ref.name)
                    label = f"{ref.obj}.{ref.name}"
                else:
                    continue
                if tgt is None:
                    continue
                if tgt[0] == "def":
                    if tgt[1] is not b:
                        proj.add(tgt[1])
                        if ref.eager:
                            eager.add(tgt[1])
                elif tgt[0] == "ext":
                    if ref.kind == "name" and ref.name in self.nested_locals.get(b.module, ()):
                        continue
                    ext_keys.add(tgt[1])
                    ext_refs.add((label, tgt[1]))
            self.eager[b.bid], self.proj_refs[b.bid] = eager, proj
            self.ext_keys[b.bid], self.ext_refs[b.bid] = ext_keys, frozenset(ext_refs)

    def _dedupe(self) -> None:
        groups: dict = defaultdict(list)
        for b in self.blocks:
            if not b.code().strip():
                continue
            groups[(b.kind, tuple(b.names), _content_hash(b))].append(b)
        for key, blocks in groups.items():
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

    def _assign_names(self, kept: list, emit_keys: set) -> None:
        ext_loc_modules: dict = defaultdict(set)
        for m, env in self.env.items():
            for local, tgt in env.items():
                if tgt[0] == "ext" and tgt[1] in emit_keys:
                    ext_loc_modules[local].add(m)
        ext_locals = {_ext_local(k) for k in emit_keys if _ext_local(k)}
        guard_names = {
            name
            for mi in self.modules.values()
            for g in mi.guards
            for name in ([s.local_name for s in g.specs] + list(g.bound_names))
            if name
        }
        group: dict = defaultdict(list)
        for b in kept:
            for n in b.names:
                group[n].append(b)
        taken: set = set(ext_locals) | guard_names
        main_override = self.main_name
        for name in sorted(group):
            blocks = sorted(group[name], key=lambda x: (x.module.count("."), x.module, x.order))
            is_main_override = name == "main" and main_override is not None
            for i, b in enumerate(blocks):
                clash = bool(ext_loc_modules.get(name, set()) - {b.module}) or name in guard_names
                if is_main_override:
                    if i == 0 and not clash and main_override not in taken:
                        cand = main_override
                    else:
                        base_name = main_override
                        cand = make_identifier(f"{base_name}_{self._prefix(b.module)}")
                        base, n = cand, 2
                        while cand in taken or cand in group:
                            cand, n = f"{base}_{n}", n + 1
                        logger.info(
                            "Renaming {} 'main' in {} -> '{}' (main-name override)",
                            b.kind,
                            b.module,
                            cand,
                        )
                    taken.add(cand)
                    self.final[(b.bid, name)] = cand
                    if cand != name:
                        self._renamed_log.append((b.module, name, cand))
                    continue
                if i == 0 and not clash and name not in taken:
                    cand = name
                else:
                    lead = name[: len(name) - len(name.lstrip("_"))]
                    raw = name.lstrip("_")
                    cand = make_identifier(f"{lead}{self._prefix(b.module)}_{raw}")
                    base, n = cand, 2
                    while cand in taken or cand in group:
                        cand, n = f"{base}_{n}", n + 1
                    logger.info(
                        "Renaming {} '{}' in {} -> '{}'",
                        b.kind,
                        name,
                        b.module,
                        cand,
                    )
                taken.add(cand)
                self.final[(b.bid, name)] = cand
                if cand != name:
                    self._renamed_log.append((b.module, name, cand))
        self._taken = taken

    def _resolve_ext_conflicts(self, emit_keys: set) -> None:
        by_local: dict = defaultdict(list)
        for key in sorted(emit_keys, key=repr):
            loc = _ext_local(key)
            if loc:
                by_local[loc].append(key)
        taken = self._taken
        for loc, keys in by_local.items():
            if len({_ext_identity(k) for k in keys}) <= 1:
                continue
            keys.sort(key=lambda k: (not (k[0] == "import" and k[2] is None), repr(k)))
            keep = _ext_identity(keys[0])
            aliased: dict = {}
            for k in keys:
                ident = _ext_identity(k)
                if ident == keep:
                    continue
                if ident in aliased:
                    self.ext_final[k] = aliased[ident]
                    continue
                base = make_identifier(_sanitize(k[1]) + (f"_{k[2]}" if k[0] == "from" else ""))
                cand, n = base, 2
                while cand in taken:
                    cand, n = f"{base}_{n}", n + 1
                taken.add(cand)
                aliased[ident] = cand
                self.ext_final[k] = cand
                logger.info("Import conflict on '{}': aliasing {} as '{}'", loc, k[1:], cand)

    def _target_name(self, tgt, local: str | None) -> str | None:
        if tgt is None:
            return None
        if tgt[0] == "def":
            blk = self._canon(tgt[1])
            return self.final.get((blk.bid, tgt[2]))
        if tgt[0] == "ext":
            return self.ext_final.get(tgt[1], local)
        return None

    def _render_block(self, b: Block) -> str:
        walker = RefWalker(
            b.module,
            self.modules[b.module].is_pkg,
            collect=False,
            resolver=self,
            env=self.env[b.module],
        )
        tree = cst.Module(body=list(b.nodes))
        rewritten = tree.visit(walker)
        return rewritten.code.strip("\n").rstrip()

    def _render_all(self, kept: list) -> dict[int, str]:
        if self.workers > 1 and len(kept) > 32:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                texts = list(pool.map(self._render_block, kept))
        else:
            texts = [self._render_block(b) for b in kept]
        return {b.bid: t for b, t in zip(kept, texts)}

    def _merge_mains(self, mains: list, texts: dict[int, str]) -> str | None:
        sections: list = []
        seen: set = set()
        for b in mains:
            body = _guard_body(texts[b.bid])
            key_body = "\n".join(line.rstrip() for line in body).strip()
            if not key_body:
                continue
            if key_body in seen:
                continue
            seen.add(key_body)
            sections.append((b.module, body))
        if not sections:
            return None
        indent = "    "
        out = ['if __name__ == "__main__":']
        for i, (module, body) in enumerate(sections):
            if i:
                out.append("")
            out.append(f"{indent}# from {module}")
            for line in body:
                out.append(f"{indent}{line}" if line.strip() else "")
        return "\n".join(out).rstrip()

    def _assemble(self, kept: list, texts: dict[int, str], emit_keys: set) -> Plan:
        keyfn = lambda b: (self.mod_idx[b.module], b.order)
        body = [b for b in kept if b.kind != MAIN]
        mains = sorted((b for b in kept if b.kind == MAIN), key=keyfn)
        deps = {b.bid: {self._canon(d).bid for d in self.eager[b.bid]} - {b.bid} for b in body}
        dependents: dict = defaultdict(set)
        for bid, ds in deps.items():
            for d in ds:
                dependents[d].add(bid)
        tail = [b for b in body if b.kind == FUNC and b.primary == "main" and not dependents[b.bid]]
        tail_ids = {b.bid for b in tail}
        head = [b for b in body if b.bid not in tail_ids]
        ordered = topo_order(head, deps, lambda b: _RANK[b.kind], lambda b: (_RANK[b.kind], *keyfn(b)))
        tail_ordered = topo_order(tail, deps, lambda b: 0, keyfn)
        rendered: list[RenderedBlock] = []
        for b in ordered:
            origin = f"{b.orig_path}:{b.start_line}-{b.end_line}" if b.orig_path else ""
            rendered.append(
                RenderedBlock(
                    _SECTION[b.kind],
                    b.kind,
                    texts[b.bid],
                    origin=origin,
                    module=b.module,
                )
            )
        for b in tail_ordered:
            origin = f"{b.orig_path}:{b.start_line}-{b.end_line}" if b.orig_path else ""
            rendered.append(RenderedBlock("entry", b.kind, texts[b.bid], origin=origin, module=b.module))
        merged_guard = self._merge_mains(mains, texts)
        if merged_guard is not None:
            modules = ", ".join(b.module for b in mains)
            rendered.append(
                RenderedBlock(
                    "entry",
                    MAIN,
                    merged_guard,
                    origin=f"merged from {modules}",
                    module=mains[0].module,
                )
            )
        if self.generate_guard and not mains:
            mains_funcs = [b for b in kept if b.kind == FUNC and b.primary in ("main", self.main_name or "main")]
            if mains_funcs:
                guard_text = (
                    'if __name__ == "__main__":\n    main()'
                    if not self.main_name
                    else f'if __name__ == "__main__":\n    {self.main_name}()'
                )
                rendered.append(RenderedBlock("entry", MAIN, guard_text, origin="generated", module="__main__"))
                logger.info("Auto-generated __main__ guard")
        if len(mains) > 1:
            logger.info(
                "Merged {} '__main__' guards into a single entry block (from {})",
                len(mains),
                ", ".join(b.module for b in mains),
            )
            logger.warning(
                "{} modules define their own entry point; only one can run at a "
                "time. Use --entry MODULE to keep a single one.",
                len(mains),
            )
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
            renamed=self._renamed_log,
            deduped=self._deduped_log,
            all_names=self._module_all_names(),
        )
        if self.resources:
            plan.resources_code = self._build_resources()
        self._exports(plan, kept)
        return plan

    def _module_all_names(self) -> list[str]:
        candidates = [(m, mi) for m, mi in self.modules.items() if mi.declared_all is not None]
        if not candidates:
            return []
        candidates.sort(key=lambda item: item[0].count("."))
        m, mi = candidates[0]
        env = self.env[m]
        out: list[str] = []
        for name in mi.declared_all:
            tgt = env.get(name)
            fin = self._target_name(tgt, name)
            out.append(fin or name)
        seen: set = set()
        result: list[str] = []
        for n in out:
            if n not in seen:
                seen.add(n)
                result.append(n)
        return result

    def _build_resources(self) -> str:
        from .resources import build_resources_code

        taken = set(getattr(self, "_taken", set()) or set())
        for b in self.blocks:
            for n in b.names:
                taken.add(self.final.get((b.bid, n), n))
        taken -= {None}
        return build_resources_code(self.resources or [], taken_names=taken)

    def _exports(self, plan: Plan, kept: list) -> None:
        groups = {
            CONST: "constants",
            MISC: "constants",
            CLASS: "classes",
            FUNC: "functions",
        }
        seen: set = set()
        for b in kept:
            if b.kind == MAIN:
                continue
            for n in b.names:
                fin = self.final.get((b.bid, n))
                if fin is None or fin in seen:
                    continue
                if fin.startswith("_"):
                    continue
                seen.add(fin)
                plan.exports[groups.get(b.kind, "constants")].append(fin)
        for names in plan.exports.values():
            names.sort(key=sort_key)
