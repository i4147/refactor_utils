"""Cross-module resolution: name collisions, import rewriting, dedupe, ordering."""

import hashlib
import keyword
import re
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Iterable
from loguru import logger
from .imports import build_import_section, render_import_line, sort_key
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
    origin: str = ""  # e.g., "pkg/mod.py:10-20"
    module: str = ""


@dataclass
class Plan:
    future_names: list[str]
    imports_text: str
    guards: list[str]
    blocks: list[RenderedBlock]
    exports: dict[str, list[str]] = field(
        default_factory=lambda: {"constants": [], "classes": [], "functions": []}
    )
    renamed: list[tuple[str, str, str]] = field(default_factory=list)
    deduped: list[tuple[str, str]] = field(default_factory=list)
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


def is_valid_identifier(name: str) -> bool:
    """True when ``name`` can be bound by ``class``/``def``/assignment."""
    return name.isidentifier() and not keyword.iskeyword(name)


def make_identifier(text: str, fallback: str = "mod") -> str:
    """Turn arbitrary text into a valid, non-keyword Python identifier.

    Every generated name (collision renames, import aliases) goes through
    this, so a module called ``2.py`` or ``my-mod.py`` can never produce
    names such as ``2_Tokenizer`` that Python rejects:

    - characters that cannot appear in an identifier become ``_``;
    - a name that would start with a digit gets an ``m`` prefix
      (``2`` -> ``m2``), keeping it public rather than ``_``-private;
    - keywords get a trailing ``_`` (``class`` -> ``class_``).
    """
    text = unicodedata.normalize("NFKC", text)
    out = "".join(
        ch if (ch == "_" or ch.isalnum()) and f"a{ch}".isidentifier() else "_"
        for ch in text
    )
    if not out.strip("_"):
        out = fallback
    if not out[0].isidentifier():
        out = f"m{out}"
    if keyword.iskeyword(out):
        out += "_"
    return out


def _content_hash(source: bytes) -> str:
    """Hash of a block body, insensitive to blank lines and trailing spaces."""
    normalised = b"\n".join(
        line.rstrip() for line in source.strip().splitlines() if line.strip()
    )
    return hashlib.sha1(normalised).hexdigest()


def _guard_body(text: str) -> list[str]:
    """Return the body lines of an ``if __name__ == "__main__":`` block.

    Leading comment lines are ignored, the header line is dropped and the body
    is de-indented so it can be re-emitted under a fresh guard header.
    """
    lines = text.splitlines()
    header = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        # The header may carry a trailing comment (``...:  # pragma: no cover``)
        # and may be preceded by comment lines picked up with the block.
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
        entry: Iterable[str] = (),
        workers: int = 1,
        imports_mode: str = "prune",
        entry_keep_all: bool = False,
        main_name: str | None = None,
        generate_guard: bool = False,
    ) -> None:
        self.modules = dict(sorted(modules.items()))
        self.entry = tuple(entry)
        self.workers = max(1, workers)
        self.imports_mode = imports_mode
        self.entry_keep_all = entry_keep_all
        self.main_name = main_name
        self.generate_guard = generate_guard

        self.known: set[str] = set(self.modules)
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
        # Names bound inside a conditional/try-EXCEPT import guard.  Those guards
        # are emitted verbatim in the head section, so import keys that would
        # repeat them must not be hoisted (it would make optional imports hard
        # requirements and could duplicate the binding).
        self.cond_specs: dict[str, list[ImportSpec]] = {}
        # local names supplied only by imports nested inside a function/class:
        # they stay nested (their statement is kept verbatim in the block) and
        # must not be hoisted into the head import section.
        self.nested_locals: dict[str, set[str]] = {}
        self._warned: set[str] = set()
        self._renamed_log: list[tuple[str, str, str]] = []
        self._deduped_log: list[tuple[str, str]] = []

    def _entry_match(self, module: str) -> bool:
        return any(module == e or module.endswith("." + e) for e in self.entry)

    def _warn_once(self, msg: str) -> None:
        if msg not in self._warned:
            self._warned.add(msg)
            logger.warning(msg)

    def _is_internal(self, spec: ImportSpec) -> bool:
        if spec.relative:
            return True
        return bool(spec.module) and (
            spec.module in self.known or spec.module.split(".")[0] in self.tops
        )

    def _canon(self, b: Block) -> Block:
        return self.canon.get(b.bid, b)

    def _prefix(self, module: str) -> str:
        parts = module.split(".")
        if self.single_top and len(parts) > 1 and parts[0] == self.single_top:
            parts = parts[1:]
        # Module names come from file names, which need not be identifiers
        # (``2.py``, ``my-mod.py``): the prefix must be usable at the *start*
        # of a name.
        return make_identifier(_sanitize("_".join(parts)) or "mod")

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
            all_ext = {
                t[1] for env in self.env.values() for t in env.values() if t[0] == "ext"
            }
            for k in sorted(all_ext - used, key=repr):
                self._warn_once(f"Strict imports: unused import {k} would be pruned")
        emit_keys |= self.star_keys
        emit_keys = self._drop_guarded_keys(emit_keys)
        self._assign_names(kept, emit_keys)
        self._resolve_ext_conflicts(emit_keys)
        rendered = self._render_all(kept)
        return self._assemble(kept, rendered, emit_keys)

    def _guard_identity(self, spec: ImportSpec) -> tuple:
        if spec.star:
            return ("star", spec.module)
        if not spec.is_from:
            return ("import", spec.module, spec.alias)
        return ("from", spec.module, spec.name, spec.alias)

    def _drop_guarded_keys(self, emit_keys: set[tuple]) -> set[tuple]:
        """Remove imports already performed by a verbatim guard block."""
        provided = {
            self._guard_identity(spec)
            for specs in self.cond_specs.values()
            for spec in specs
        }
        if not provided:
            return emit_keys
        kept: set[tuple] = set()
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
        for m, mi in self.modules.items():
            env: dict[str, Target] = {}
            for (mm, n), blk in self.def_block.items():
                if mm == m:
                    env[n] = ("def", blk, n)
            specs: list[ImportSpec] = [
                s for s in mi.imports if s.module != "__future__"
            ]
            nested: list[ImportSpec] = []
            for b in mi.blocks:
                for occ in b.occs:
                    if occ.kind == "nested_import":
                        nested.extend(s for s in occ.specs if s.module != "__future__")
            nested_locals = self.nested_locals.setdefault(m, set())
            for spec in specs + nested:
                is_nested = spec in nested
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
                            f"{m}: guarded import of internal module '{spec.module}' is kept verbatim and will not resolve"
                        )
                    if "ImportError" in g.text or "try:" in g.text:
                        self._warn_once(
                            f"{m}: conditional/optional import guard kept verbatim (may have side effects)"
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
                    if occ.kind == "name" and occ.name in self.nested_locals.get(
                        b.module, ()
                    ):
                        continue
                    ext_keys.add(tgt[1])
                    ext_refs.add((label, tgt[1]))
            self.eager[b.bid], self.proj_refs[b.bid] = eager, proj
            self.ext_keys[b.bid], self.ext_refs[b.bid] = ext_keys, frozenset(ext_refs)

    def _dedupe(self) -> None:
        """Merge byte-identical definitions, keeping the shallowest module's copy.

        Comparison is a hash of the normalised body (blank lines and trailing
        whitespace ignored), so ``def f(): ...`` copied into two modules is
        emitted once.  Bodies that differ are *not* merged; they keep both names
        via :meth:`_assign_names`.
        """
        groups: dict[tuple, list[Block]] = defaultdict(list)
        for b in self.blocks:
            if not b.source.strip():
                continue
            groups[(b.kind, tuple(b.names), _content_hash(b.source))].append(b)
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

    def _assign_names(self, kept: list[Block], emit_keys: set[tuple]) -> None:
        ext_loc_modules: dict[str, set[str]] = defaultdict(set)
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
        group: dict[str, list[Block]] = defaultdict(list)
        for b in kept:
            for n in b.names:
                group[n].append(b)
        taken: set[str] = set(ext_locals) | guard_names
        main_override = self.main_name

        for name in sorted(group):
            blocks = sorted(
                group[name], key=lambda x: (x.module.count("."), x.module, x.order)
            )
            is_main_override = name == "main" and main_override is not None
            for i, b in enumerate(blocks):
                clash = (
                    bool(ext_loc_modules.get(name, set()) - {b.module})
                    or name in guard_names
                )
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

    def _resolve_ext_conflicts(self, emit_keys: set[tuple]) -> None:
        by_local: dict[str, list[tuple]] = defaultdict(list)
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
            aliased: dict[tuple, str] = {}
            for k in keys:
                ident = _ext_identity(k)
                if ident == keep:
                    continue
                if ident in aliased:
                    self.ext_final[k] = aliased[ident]
                    continue
                base = make_identifier(
                    _sanitize(k[1]) + (f"_{k[2]}" if k[0] == "from" else "")
                )
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
                continue
            out.append(b.source[pos:s])
            out.append(r.encode())
            pos = last_end = e
        out.append(b.source[pos:])
        text = b"".join(out).decode("utf-8", "replace").strip("\n").rstrip()
        return text

    def _render_all(self, kept: list[Block]) -> dict[int, str]:
        if self.workers > 1 and len(kept) > 32:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                texts = list(pool.map(self._render_block, kept))
        else:
            texts = [self._render_block(b) for b in kept]
        return {b.bid: t for b, t in zip(kept, texts)}

    def _merge_mains(self, mains: list[Block], texts: dict[int, str]) -> str | None:
        """Build one ``if __name__ == "__main__":`` block from every guard."""
        sections: list[tuple[str, list[str]]] = []
        seen: set[str] = set()
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
            head, deps, lambda b: _RANK[b.kind], lambda b: (_RANK[b.kind], *keyfn(b))
        )
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
            rendered.append(
                RenderedBlock(
                    "entry", b.kind, texts[b.bid], origin=origin, module=b.module
                )
            )
        # Several modules usually guard their own entry point.  Emitted as-is
        # only the first guard would ever run (every guard has the same
        # condition), so all kept guards are merged into ONE block whose body
        # runs each original entry, annotated with its source module.
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
            mains_funcs = [
                b
                for b in kept
                if b.kind == FUNC and b.primary in ("main", self.main_name or "main")
            ]
            if mains_funcs:
                guard_text = (
                    'if __name__ == "__main__":\n    main()'
                    if not self.main_name
                    else f'if __name__ == "__main__":\n    {self.main_name}()'
                )
                rendered.append(
                    RenderedBlock(
                        "entry", MAIN, guard_text, origin="generated", module="__main__"
                    )
                )
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
        )
        self._exports(plan, kept)
        return plan

    def _exports(self, plan: Plan, kept: list[Block]) -> None:
        """Collect the public API of the consolidated module.

        ``__all__`` is never emitted: the export list is recomputed from the
        definitions that survive dedupe/renaming (constants, classes or
        functions), which keeps private names out.
        """
        groups = {
            CONST: "constants",
            MISC: "constants",
            CLASS: "classes",
            FUNC: "functions",
        }
        seen: set[str] = set()
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
