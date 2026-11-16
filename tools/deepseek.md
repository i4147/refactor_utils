# Refactored `pyrefactor` Project

Below is the full refactored project. The main fixes are:

1. **All dataclasses now have proper field annotations** — the original `models.py` defined fields without annotations, so `@dataclass` was ignoring them, and every construction of `Block`, `Occ`, `ImportSpec`, `ModuleInfo`, `SourceFile`, `DataFile`, `RenderedBlock`, and `Plan` was broken.
2. **`ConsolidationResult` is reconstructed with keyword args** — the original passed positional arguments that did not match the field order, so `result.files`, `result.modules`, etc. were invalid.
3. **`--check` now correctly fails when the output file is missing**.
4. **`--check` + `--stdout` are rejected as mutually exclusive**.
5. **Parse-worker exceptions are tracked** and reported, instead of being silently dropped.
6. **`__all__` is preserved** (from any source module that declared it) and re-exported external imports referenced by `__all__` are not pruned.
7. **Data files can be embedded** in the consolidated output as text or base64; helpers `read_data`, `has_data`, `list_data`, `extract_data_files` are generated. Controlled by `--embed-data` / `--no-embed-data`.

---

## `src/refactor_utils/models.py`

```python
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

CONST = "const"
CLASS = "class"
FUNC = "func"
MISC = "misc"
MAIN = "main"


@dataclass(frozen=True)
class ImportSpec:
    module: str
    name: Optional[str] = None
    alias: Optional[str] = None
    is_from: bool = False
    star: bool = False
    relative: bool = False
    type_checking: bool = False

    @property
    def local_name(self) -> Optional[str]:
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
    kind: str  # "name" | "attr" | "nested_import"
    eager: bool = True
    obj: str = ""
    specs: tuple = ()


@dataclass(eq=False)
class Block:
    module: str
    kind: str
    names: list
    source: bytes
    occs: list
    order: int
    bid: int = -1
    orig_path: str = ""
    start_line: int = 0
    end_line: int = 0
    stripped_docstring: bool = False

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
    specs: list = field(default_factory=list)
    bound_names: list = field(default_factory=list)
    conditional: bool = False


@dataclass
class ModuleInfo:
    module: str
    path: str
    is_pkg: bool = False
    imports: list = field(default_factory=list)
    blocks: list = field(default_factory=list)
    guards: list = field(default_factory=list)
    declared_all: Optional[list] = None
    future_annotations: bool = False
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)


@dataclass(frozen=True)
class SourceFile:
    path: Path
    module: str
    is_pkg: bool = False


@dataclass(frozen=True)
class DataFile:
    path: Path
    relative: str
    kind: str = "data"
```

---

## `src/refactor_utils/data_embed.py` *(new)*

```python
"""Helpers for embedding data files inside the consolidated output."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Dict, Tuple

from loguru import logger

# Warn when a single embedded file exceeds this size.
_WARN_SIZE = 500 * 1024
# Hard warn threshold for a single file.
_HARD_WARN_SIZE = 5 * 1024 * 1024


def classify_and_encode(path: Path) -> Tuple[str, str]:
    """Return ("text", str) or ("binary", base64-str) for a data file."""
    raw = path.read_bytes()
    if len(raw) > _HARD_WARN_SIZE:
        logger.warning(
            "Data file {} is very large ({} bytes); embedding will bloat the output",
            path,
            len(raw),
        )
    elif len(raw) > _WARN_SIZE:
        logger.info("Embedding data file {} ({} bytes)", path, len(raw))

    if b"\x00" in raw:
        return "binary", base64.b64encode(raw).decode("ascii")
    try:
        return "text", raw.decode("utf-8")
    except UnicodeDecodeError:
        return "binary", base64.b64encode(raw).decode("ascii")


def collect_embedded_data(data_files) -> Dict[str, Tuple[str, str]]:
    """Map relative path -> (kind, content) for every data file."""
    out: Dict[str, Tuple[str, str]] = {}
    for df in data_files:
        kind, content = classify_and_encode(df.path)
        out[df.relative] = (kind, content)
    return out
```

---

## `src/refactor_utils/resolver.py`

```python
import hashlib
import keyword
import re
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Iterable, Optional

from loguru import logger

from .imports import build_import_section, render_import_line, sort_key
from .models import CLASS, CONST, FUNC, MAIN, MISC, Block, ImportSpec, ModuleInfo
from .ordering import topo_order

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
    future_names: list = field(default_factory=list)
    imports_text: str = ""
    guards: list = field(default_factory=list)
    blocks: list = field(default_factory=list)
    renamed: list = field(default_factory=list)
    deduped: list = field(default_factory=list)
    all_export: list = field(default_factory=list)
    exports: dict = field(
        default_factory=lambda: {"constants": [], "classes": [], "functions": []}
    )
    warnings: list = field(default_factory=list)


def _ext_local(key):
    if key[0] == "import":
        return key[2] or key[1].split(".")[0]
    if key[0] == "from":
        return key[3] or key[2]
    return None


def _ext_identity(key):
    if key[0] == "import":
        return ("modalias", key[1]) if key[2] else ("mod", key[1].split(".")[0])
    return ("from", key[1], key[2])


def _sanitize(text):
    return re.sub(r"\W", "_", text).strip("_")


def is_valid_identifier(name):
    return name.isidentifier() and not keyword.iskeyword(name)


def make_identifier(text, fallback="mod"):
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


def _content_hash(source):
    normalised = b"\n".join(
        line.rstrip() for line in source.strip().splitlines() if line.strip()
    )
    return hashlib.sha1(normalised).hexdigest()


def _guard_body(text):
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
        modules,
        *,
        entry=(),
        workers=1,
        imports_mode="prune",
        entry_keep_all=False,
        main_name=None,
        generate_guard=False,
    ):
        self.modules = dict(sorted(modules.items()))
        self.entry = tuple(entry)
        self.workers = max(1, workers)
        self.imports_mode = imports_mode
        self.entry_keep_all = entry_keep_all
        self.main_name = main_name
        self.generate_guard = generate_guard

        self.known = set(self.modules)
        for m in list(self.modules):
            parts = m.split(".")
            for i in range(1, len(parts)):
                self.known.add(".".join(parts[:i]))
        self.tops = {m.split(".")[0] for m in self.modules}
        self.mod_idx = {m: i for i, m in enumerate(self.modules)}
        self.single_top = next(iter(self.tops)) if len(self.tops) == 1 else None

        self.blocks = []
        for mi in self.modules.values():
            for b in mi.blocks:
                if b.kind == MAIN and self.entry and not self._entry_match(b.module):
                    if not self.entry_keep_all:
                        continue
                b.bid = len(self.blocks)
                self.blocks.append(b)

        self.def_block = {}
        for b in self.blocks:
            for n in b.names:
                self.def_block[(b.module, n)] = b

        self.future_names = set()
        self.import_table = {}
        self.star_specs = {}
        for m, mi in self.modules.items():
            table = {}
            stars = []
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

        self.env = {}
        self.eager = {}
        self.proj_refs = {}
        self.ext_keys = {}
        self.ext_refs = {}
        self.canon = {}
        self.final = {}
        self.ext_final = {}
        self.star_keys = set()
        self.cond_specs = {}
        self.nested_locals = {}
        self._warned = set()
        self._renamed_log = []
        self._deduped_log = []

    # ------------------------------------------------------------------ helpers
    def _entry_match(self, module):
        return any(module == e or module.endswith("." + e) for e in self.entry)

    def _warn_once(self, msg):
        if msg not in self._warned:
            self._warned.add(msg)
            logger.warning(msg)

    def _is_internal(self, spec):
        if spec.relative:
            return True
        return bool(spec.module) and (
            spec.module in self.known or spec.module.split(".")[0] in self.tops
        )

    def _canon(self, b):
        return self.canon.get(b.bid, b)

    def _prefix(self, module):
        parts = module.split(".")
        if self.single_top and len(parts) > 1 and parts[0] == self.single_top:
            parts = parts[1:]
        return make_identifier(_sanitize("_".join(parts)) or "mod")

    def _star_names(self, module):
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

    def _spec_target(self, spec, seen):
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

    def _resolve_symbol(self, module, name, seen=None):
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

    def _resolve_dotted(self, module, obj, attr):
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

    # ------------------------------------------------------------------ pipeline
    def build(self):
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

        # Keep re-exported imports that appear in any __all__
        declared_all = set()
        for mi in self.modules.values():
            if mi.declared_all:
                declared_all.update(mi.declared_all)
        for env in self.env.values():
            for local, tgt in env.items():
                if local in declared_all and tgt[0] == "ext":
                    emit_keys.add(tgt[1])

        emit_keys |= self.star_keys
        emit_keys = self._drop_guarded_keys(emit_keys)

        self._assign_names(kept, emit_keys)
        self._resolve_ext_conflicts(emit_keys)
        rendered = self._render_all(kept)
        return self._assemble(kept, rendered, emit_keys)

    def _guard_identity(self, spec):
        if spec.star:
            return ("star", spec.module)
        if not spec.is_from:
            return ("import", spec.module, spec.alias)
        return ("from", spec.module, spec.name, spec.alias)

    def _drop_guarded_keys(self, emit_keys):
        provided = {
            self._guard_identity(spec)
            for specs in self.cond_specs.values()
            for spec in specs
        }
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

    def _build_envs(self):
        for m, mi in self.modules.items():
            env = {}
            for (mm, n), blk in self.def_block.items():
                if mm == m:
                    env[n] = ("def", blk, n)
            specs = [s for s in mi.imports if s.module != "__future__"]
            nested = []
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

    def _analyse(self):
        for b in self.blocks:
            env = self.env[b.module]
            eager = set()
            proj = set()
            ext_keys = set()
            ext_refs = set()
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

    def _dedupe(self):
        groups = defaultdict(list)
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

    def _assign_names(self, kept, emit_keys):
        ext_loc_modules = defaultdict(set)
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
        group = defaultdict(list)
        for b in kept:
            for n in b.names:
                group[n].append(b)
        taken = set(ext_locals) | guard_names
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

    def _resolve_ext_conflicts(self, emit_keys):
        by_local = defaultdict(list)
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
            aliased = {}
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

    def _target_name(self, tgt, local):
        if tgt is None:
            return None
        if tgt[0] == "def":
            blk = self._canon(tgt[1])
            return self.final.get((blk.bid, tgt[2]))
        if tgt[0] == "ext":
            return self.ext_final.get(tgt[1], local)
        return None

    def _render_block(self, b):
        env = self.env[b.module]
        edits = []
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

    def _render_all(self, kept):
        if self.workers > 1 and len(kept) > 32:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                texts = list(pool.map(self._render_block, kept))
        else:
            texts = [self._render_block(b) for b in kept]
        return {b.bid: t for b, t in zip(kept, texts)}

    def _merge_mains(self, mains, texts):
        sections = []
        seen = set()
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

    def _assemble(self, kept, texts, emit_keys):
        keyfn = lambda b: (self.mod_idx[b.module], b.order)
        body = [b for b in kept if b.kind != MAIN]
        mains = sorted((b for b in kept if b.kind == MAIN), key=keyfn)
        deps = {
            b.bid: {self._canon(d).bid for d in self.eager[b.bid]} - {b.bid}
            for b in body
        }
        dependents = defaultdict(set)
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
        rendered = []
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
                        "entry",
                        MAIN,
                        guard_text,
                        origin="generated",
                        module="__main__",
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
        guards = []
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

        # Preserve __all__ if any source module declared one
        declared_all = set()
        for mi in self.modules.values():
            if mi.declared_all:
                declared_all.update(mi.declared_all)
        if declared_all:
            available = set()
            for names in plan.exports.values():
                available.update(names)
            for env in self.env.values():
                for local, tgt in env.items():
                    if (
                        local in declared_all
                        and tgt[0] == "ext"
                        and tgt[1] in emit_keys
                    ):
                        available.add(self.ext_final.get(tgt[1], local))
            final_all = sorted(declared_all & available, key=sort_key)
            missing = declared_all - available
            if missing:
                logger.debug(
                    "Dropping {} name(s) from __all__ (not present in output): {}",
                    len(missing),
                    ", ".join(sorted(missing)),
                )
            plan.all_export = final_all
        return plan

    def _exports(self, plan, kept):
        groups = {
            CONST: "constants",
            MISC: "constants",
            CLASS: "classes",
            FUNC: "functions",
        }
        seen = set()
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
```

---

## `src/refactor_utils/writer.py`

```python
import os
import subprocess
import tempfile
from pathlib import Path

from .imports import future_section
from .resolver import Plan

_RULE = "# " + "-" * 75
_TITLES = {
    "const": "Constants",
    "class": "Classes",
    "func": "Functions",
    "entry": "Entry points",
}

_DATA_HELPERS = '''

def read_data(rel_path, binary=False):
    """Return the contents of an embedded data file.

    Args:
        rel_path: relative path of the data file as recorded at consolidation
        binary: if True, return bytes; otherwise str

    Returns:
        str or bytes

    Raises:
        FileNotFoundError: if the path is not in ``_DATA_FILES``
    """
    entry = _DATA_FILES.get(rel_path)
    if entry is None:
        raise FileNotFoundError(f"Embedded data file not found: {rel_path}")
    kind, content = entry
    if kind == "text":
        return content.encode("utf-8") if binary else content
    import base64 as _b64
    raw = _b64.b64decode(content)
    return raw if binary else raw.decode("utf-8", "replace")


def has_data(rel_path):
    """Return True if an embedded data file exists at rel_path."""
    return rel_path in _DATA_FILES


def list_data():
    """Return the sorted list of embedded data file paths."""
    return sorted(_DATA_FILES)


def extract_data_files(target_dir=None):
    """Extract embedded data files to a directory.

    Args:
        target_dir: directory to extract to; a temp dir is created if None

    Returns:
        dict mapping relative path (str) to pathlib.Path
    """
    import base64 as _b64
    import tempfile as _tf
    from pathlib import Path as _Path
    if target_dir is None:
        target_dir = _tf.mkdtemp(prefix="pyrefactor_embedded_")
    target = _Path(target_dir)
    out = {}
    for rel, (kind, content) in _DATA_FILES.items():
        p = target / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if kind == "text":
            p.write_text(content, encoding="utf-8")
        else:
            p.write_bytes(_b64.b64decode(content))
        out[rel] = p
    return out

'''


def _sep(prev_kind, cur_kind, prev_text, cur_text):
    simple = ("const", "misc")
    if prev_kind in simple and cur_kind in simple:
        return "\n" if "\n" not in prev_text and "\n" not in cur_text else "\n\n"
    return "\n\n\n"


def _format_with_black(text):
    try:
        import importlib

        black = importlib.import_module("black")
        return black.format_str(text, mode=black.Mode())
    except Exception:
        return text


def _format_with_ruff(text):
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(text)
            tmp_path = tmp.name
        subprocess.run(["ruff", "format", tmp_path], capture_output=True, timeout=5)
        out = Path(tmp_path).read_text(encoding="utf-8")
        os.unlink(tmp_path)
        return out
    except Exception:
        return text


def _render_embedded_data(data):
    lines = [
        "# " + "=" * 73,
        "# Embedded data files (auto-generated by pyrefactor)",
        "# " + "=" * 73,
        "",
        "_DATA_FILES = {",
    ]
    for rel in sorted(data):
        kind, content = data[rel]
        lines.append(f"    {rel!r}: ({kind!r}, {content!r}),")
    lines.append("}")
    lines.append(_DATA_HELPERS)
    return "\n".join(lines)


def render_consolidated(
    plan,
    source_name,
    do_format=False,
    formatter="black",
    embedded_data=None,
):
    parts = [
        f'"""Consolidated from ``{source_name}`` by pyrefactor (generated file)."""'
    ]
    head = []
    if plan.future_names:
        head.append(future_section(plan.future_names))
    if plan.imports_text:
        head.append(plan.imports_text)
    if embedded_data:
        head.append(_render_embedded_data(embedded_data))
    if plan.guards:
        head.append("\n\n".join(plan.guards))
    if head:
        parts.append("\n\n".join(head))

    body = []
    prev = None
    seen_sections = set()
    for blk in plan.blocks:
        piece = ""
        first_of_section = blk.section not in seen_sections
        if prev is None or blk.section != prev.section:
            if first_of_section:
                piece += f"{_RULE}\n# {_TITLES[blk.section]}\n{_RULE}\n\n\n"
            sep = "\n\n\n" if prev is not None else ""
        else:
            sep = _sep(prev.kind, blk.kind, prev.text, blk.text)
        seen_sections.add(blk.section)
        piece += blk.text
        body.append(sep + piece)
        prev = blk

    if plan.all_export:
        body.append(
            "\n\n\n__all__ = [\n"
            + "".join(f"    {n!r},\n" for n in plan.all_export)
            + "]"
        )

    parts.append("".join(body))
    head_text = "\n\n\n".join(p for p in parts[1:] if p)
    text = "\n\n".join(p for p in parts[:1]) + "\n\n" + head_text
    text = text.rstrip() + "\n"

    if do_format:
        if formatter == "ruff":
            text = _format_with_ruff(text)
        else:
            formatted = _format_with_black(text)
            if formatted == text:
                formatted = _format_with_ruff(text)
            text = formatted
    return text
```

---

## `src/refactor_utils/core.py`

```python
import ast
import difflib
import fnmatch
import os
import shutil
import sys
from concurrent.futures import (
    BrokenExecutor,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    as_completed,
)
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from .data_embed import collect_embedded_data
from .discovery import discover, discover_data_files, detect_pyproject_config
from .models import ModuleInfo, SourceFile
from .parser import parse_module_file
from .resolver import Plan, Resolver, is_valid_identifier
from .writer import render_consolidated


class ConsolidationError(RuntimeError):
    pass


@dataclass
class ConsolidationResult:
    output_dir: Path = None
    files: list = field(default_factory=list)
    modules: int = 0
    skipped: list = field(default_factory=list)
    exports: int = 0
    warnings: list = field(default_factory=list)
    diff: str = ""
    stdout_text: str = ""
    copied_data_files: list = field(default_factory=list)
    embedded_data_files: list = field(default_factory=list)
    validated: bool = False
    dry_run: bool = False
    plan: object = None


def _parse_worker(args):
    (path_str, module, is_pkg, keep_docstrings, strip_comments, keep_comments) = args
    info = parse_module_file(
        path_str,
        module,
        is_pkg,
        keep_docstrings=keep_docstrings,
        strip_comments=strip_comments,
        keep_comments=keep_comments,
    )
    return module, info


def _parse_all(
    files,
    workers,
    executor,
    keep_docstrings=True,
    strip_comments=False,
    keep_comments=True,
    show_progress=False,
):
    results = {}
    failures = []

    def _do_one(f):
        return parse_module_file(
            str(f.path),
            f.module,
            f.is_pkg,
            keep_docstrings=keep_docstrings,
            strip_comments=strip_comments,
            keep_comments=keep_comments,
        )

    def _progress(iterator, total=None):
        if not show_progress:
            return iterator
        try:
            from tqdm import tqdm  # type: ignore

            return tqdm(iterator, total=total, desc="Parsing")
        except ImportError:
            logger.info(
                "Progress bar requested but tqdm not installed — falling back to log"
            )
            return iterator

    def run_pool(pool_cls, todo):
        with pool_cls(max_workers=workers) as pool:
            if pool_cls is ProcessPoolExecutor:
                futures = {
                    pool.submit(
                        _parse_worker,
                        (
                            str(f.path),
                            f.module,
                            f.is_pkg,
                            keep_docstrings,
                            strip_comments,
                            keep_comments,
                        ),
                    ): f
                    for f in todo
                }
            else:
                futures = {pool.submit(_do_one, f): f for f in todo}
            for fut in _progress(as_completed(futures), len(futures)):
                src = futures[fut]
                try:
                    res = fut.result()
                    if (
                        isinstance(res, tuple)
                        and len(res) == 2
                        and isinstance(res[1], ModuleInfo)
                    ):
                        results[res[0]] = res[1]
                    else:
                        results[src.module] = res
                except BrokenExecutor:
                    raise
                except Exception as exc:
                    logger.exception("Failed to parse {}", src.path)
                    failures.append((str(src.path), str(exc)))

    if workers <= 1 or len(files) <= 1:
        for f in _progress(files, len(files)):
            try:
                results[f.module] = _do_one(f)
            except Exception as exc:
                logger.exception("Failed to parse {}", f.path)
                failures.append((str(f.path), str(exc)))
        return results, failures

    pool_cls = ProcessPoolExecutor if executor == "process" else ThreadPoolExecutor
    try:
        run_pool(pool_cls, files)
    except BrokenExecutor:
        logger.warning("Process pool broke; retrying remaining files with threads")
        run_pool(ThreadPoolExecutor, [f for f in files if f.module not in results])
    return results, failures


def _load_config_file(config_path, source):
    if config_path is None:
        return detect_pyproject_config(source)
    config_path = Path(config_path)
    if not config_path.is_file():
        logger.warning("Config file not found: {}", config_path)
        return {}
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore
        except ImportError:
            logger.warning(
                "tomllib/tomli not available — cannot read config {}", config_path
            )
            return {}
    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
        tool = data.get("tool", {}).get("pyrefactor", {})
        if tool:
            logger.info("Loaded config from {}", config_path)
        return tool if isinstance(tool, dict) else {}
    except Exception as exc:
        logger.warning("Failed to parse config {}: {}", config_path, exc)
        return {}


def _config_list(cfg, key):
    value = cfg.get(key)
    if not value:
        return ()
    return tuple(value) if isinstance(value, list) else (value,)


def validate_syntax(text, filename="<consolidated>"):
    try:
        return ast.parse(text, filename=filename)
    except SyntaxError as exc:
        line = (exc.text or "").strip()
        if not line and exc.lineno:
            lines = text.splitlines()
            if 0 < exc.lineno <= len(lines):
                line = lines[exc.lineno - 1].strip()
        where = f"line {exc.lineno}" if exc.lineno else "unknown line"
        detail = f": {line!r}" if line else ""
        raise ConsolidationError(
            f"generated {filename} is not valid python: {exc.msg} ({where}){detail}"
        ) from exc
    except ValueError as exc:
        raise ConsolidationError(
            f"generated {filename} is not valid python: {exc}"
        ) from exc


def consolidate(
    source,
    output,
    *,
    filename=None,
    workers=None,
    executor="process",
    exclude=(),
    include=(),
    imports_mode="prune",
    entry=(),
    dry_run=False,
    check=False,
    stdout=False,
    config=None,
    do_format=False,
    formatter="black",
    diff=False,
    force=False,
    keep_docstrings=True,
    strip_comments=False,
    keep_comments=True,
    progress=False,
    copy_data=True,
    data_glob=(),
    embed_data=None,
    entry_keep_all=False,
    main_name=None,
    generate_guard=False,
):
    if check and stdout:
        raise ConsolidationError("--check and --stdout cannot be used together")

    cfg = _load_config_file(config, source)
    if cfg:
        if not exclude:
            exclude = _config_list(cfg, "exclude")
        if not include:
            include = _config_list(cfg, "include")
        if not entry:
            entry = _config_list(cfg, "entry")
        if cfg.get("imports"):
            imports_mode = str(cfg["imports"])
        if cfg.get("format") and not do_format:
            do_format = bool(cfg["format"])
        if cfg.get("formatter"):
            formatter = str(cfg["formatter"])
        if cfg.get("copy_data") is not None:
            copy_data = bool(cfg["copy_data"])
        if cfg.get("data_glob"):
            data_glob = _config_list(cfg, "data_glob")

    if embed_data is None:
        embed_data = copy_data

    if main_name is not None and not is_valid_identifier(main_name):
        raise ConsolidationError(
            f"--main-name {main_name!r} is not a valid python identifier"
        )

    source, output = Path(source), Path(output)
    if not source.exists():
        raise ConsolidationError(f"source does not exist: {source}")

    workers = workers or os.cpu_count() or 1
    source_name = source.resolve().name if source.is_dir() else source.stem
    filename = filename or Path.cwd().name
    if not filename.endswith(".py"):
        filename = f"{filename}.py"

    files = discover(source, exclude=exclude, include=include, skip_dirs=[output])
    data_files = discover_data_files(
        source,
        exclude=exclude,
        include=include,
        skip_dirs=[output],
        copy_data=copy_data or embed_data,
    )
    if data_glob:
        data_files = [
            df
            for df in data_files
            if any(
                fnmatch.fnmatch(df.relative, pat)
                or fnmatch.fnmatch(Path(df.path).name, pat)
                for pat in data_glob
            )
        ]
    if not files:
        raise ConsolidationError(f"no python files found under {source}")

    logger.info("Parsing {} files with {} {} worker(s)", len(files), workers, executor)
    if data_files:
        logger.info("Found {} data file(s) to preserve", len(data_files))

    parsed, parse_failures = _parse_all(
        files,
        workers,
        executor,
        keep_docstrings=keep_docstrings,
        strip_comments=strip_comments,
        keep_comments=keep_comments,
        show_progress=progress,
    )

    modules = {}
    skipped = []
    warnings_acc = []
    for name, info in parsed.items():
        for w in info.warnings:
            logger.warning(w)
            warnings_acc.append(w)
        if info.errors:
            for e in info.errors:
                logger.error(e)
            skipped.append(info.path)
            continue
        modules[name] = info

    for path, err in parse_failures:
        logger.error("Failed to parse {}: {}", path, err)
        skipped.append(path)

    if skipped:
        raise ConsolidationError(
            f"{len(skipped)} file(s) could not be parsed; refusing to emit a "
            f"partial consolidation: {', '.join(skipped)}"
        )
    if not modules:
        raise ConsolidationError("no module could be parsed")

    embedded = {}
    if embed_data and data_files:
        embedded = collect_embedded_data(data_files)
        if embedded:
            logger.info("Embedding {} data file(s) in output", len(embedded))

    resolver = Resolver(
        modules,
        entry=entry,
        workers=workers,
        imports_mode=imports_mode,
        entry_keep_all=entry_keep_all,
        main_name=main_name,
        generate_guard=generate_guard,
    )
    plan = resolver.build()
    for mod, orig, final in plan.renamed:
        logger.info("Renamed {}:{} -> {}", mod, orig, final)
    for mod, name in plan.deduped:
        logger.info("Deduped {} from {}", name, mod)

    mod_text = render_consolidated(
        plan,
        source_name,
        do_format=do_format,
        formatter=formatter,
        embedded_data=embedded,
    )
    exports = sum(len(v) for v in plan.exports.values())
    validate_syntax(mod_text, filename)

    diff_text = ""
    target = output / filename

    if diff or check:
        target_exists = target.is_file()
        if target_exists:
            old_text = target.read_text(encoding="utf-8")
            if old_text != mod_text:
                diff_text = "".join(
                    difflib.unified_diff(
                        old_text.splitlines(keepends=True),
                        mod_text.splitlines(keepends=True),
                        fromfile=f"a/{filename}",
                        tofile=f"b/{filename}",
                    )
                )
            else:
                logger.info("No diff for {}", filename)
        else:
            logger.info("No existing {} to diff", target)

        if diff_text:
            logger.info("Diff ({} chars):", len(diff_text))
            sys.stdout.write(diff_text)

        if check:
            if diff_text or not target_exists:
                if diff_text:
                    msg = "check failed: output would change (diff not empty)"
                else:
                    msg = f"check failed: output does not exist at {target}"
                raise ConsolidationError(msg)
            logger.success("Check passed: output up to date")
            return ConsolidationResult(
                output_dir=output,
                files=[],
                modules=len(modules),
                skipped=skipped,
                exports=exports,
                warnings=warnings_acc,
                validated=True,
                dry_run=True,
                plan=plan,
                embedded_data_files=list(embedded),
            )

    if dry_run:
        logger.info("Dry-run: would write {} to {}", filename, output)
        logger.info(
            "Would write {} ({} bytes, {} lines)",
            filename,
            len(mod_text.encode("utf-8")),
            mod_text.count("\n") + 1,
        )
        logger.info(
            "Plan: {} blocks, {} exports, {} deduped, {} renamed",
            len(plan.blocks),
            exports,
            len(plan.deduped),
            len(plan.renamed),
        )
        for df in data_files:
            logger.info(
                "Would copy data file {} -> {}/{}", df.path, output, df.relative
            )
        if embedded:
            logger.info("Would embed {} data file(s)", len(embedded))
        return ConsolidationResult(
            output_dir=output,
            files=[],
            modules=len(modules),
            skipped=skipped,
            exports=exports,
            warnings=warnings_acc,
            stdout_text=mod_text if stdout else "",
            validated=True,
            dry_run=True,
            plan=plan,
            embedded_data_files=list(embedded),
        )

    if stdout:
        sys.stdout.write(mod_text)
        return ConsolidationResult(
            output_dir=output,
            files=[],
            modules=len(modules),
            skipped=skipped,
            exports=exports,
            warnings=warnings_acc,
            diff=diff_text,
            stdout_text=mod_text,
            validated=True,
            plan=plan,
            embedded_data_files=list(embedded),
        )

    if target.exists() and not force:
        logger.warning("{} exists — overwriting (use --force to silence)", target)

    output.mkdir(parents=True, exist_ok=True)
    tmp_target = target.with_suffix(target.suffix + ".tmp")
    tmp_target.write_text(mod_text, encoding="utf-8")
    tmp_target.replace(target)
    written = [target]

    copied = []
    if copy_data:
        for df in data_files:
            dest = output / df.relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(df.path, dest)
                copied.append(dest)
                logger.debug("Copied data file {} -> {}", df.path, dest)
            except OSError as exc:
                logger.error("Failed to copy data file {}: {}", df.path, exc)

    return ConsolidationResult(
        output_dir=output,
        files=written,
        modules=len(modules),
        skipped=skipped,
        exports=exports,
        warnings=warnings_acc,
        diff=diff_text,
        copied_data_files=copied,
        embedded_data_files=list(embedded),
        validated=True,
        plan=plan,
    )
```

---

## `src/refactor_utils/cli.py`

```python
import argparse
import sys
from pathlib import Path

from loguru import logger

from . import __version__
from .core import ConsolidationError, consolidate
from .logging_setup import setup_logging


def build_parser():
    p = argparse.ArgumentParser(
        prog="pyrefactor",
        description=(
            "Consolidate a python module/package/project into a single, "
            "dependency-ordered file."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p.add_argument(
        "source", type=Path, help="package directory, project root or single .py file"
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("output"),
        help="output directory (default: ./output)",
    )
    p.add_argument(
        "--name",
        default=None,
        metavar="NAME",
        help=(
            "name of the generated file\n"
            "(default: the current directory name, e.g. output/<cwd>.py)"
        ),
    )
    p.add_argument(
        "-j",
        "--workers",
        type=int,
        default=None,
        help="parallel workers (default: CPU count)",
    )
    p.add_argument(
        "--executor",
        choices=["process", "thread"],
        default="process",
        help="executor type for parallel parsing (default: process)",
    )
    p.add_argument(
        "--progress", action="store_true", help="show progress bar during parsing"
    )
    # Discovery / filtering
    p.add_argument(
        "-x",
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help=(
            "extra path glob to skip (repeatable)\n"
            "precedence: default excludes < --include < --exclude"
        ),
    )
    p.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="GLOB",
        help="glob to re-include files that would otherwise be excluded",
    )
    p.add_argument(
        "--copy-data",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="copy data files (json, yaml, csv, txt, ...) to output (default: on)",
    )
    p.add_argument(
        "--embed-data",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "embed data files in the consolidated output as a _DATA_FILES dict\n"
            "(default: same as --copy-data)"
        ),
    )
    p.add_argument(
        "--data-glob",
        action="append",
        default=[],
        metavar="GLOB",
        help="only copy/embed data files matching this glob (repeatable)",
    )
    p.add_argument(
        "--entry",
        action="append",
        default=[],
        metavar="MODULE",
        help="only keep the __main__ guard of this module (repeatable)",
    )
    p.add_argument(
        "--entry-keep-all",
        action="store_true",
        help="keep all __main__ guards instead of filtering to --entry",
    )
    p.add_argument(
        "--main-name",
        type=str,
        default=None,
        metavar="NAME",
        help="rename `main` function to NAME (e.g., --main-name cli_main)",
    )
    p.add_argument(
        "--generate-guard",
        action="store_true",
        help='auto-generate `if __name__ == "__main__": main()` if no guard exists',
    )
    p.add_argument(
        "--imports",
        choices=["prune", "keep", "strict"],
        default="prune",
        help=(
            "import handling: prune (default, drop unused), keep (keep all),\n"
            "strict (prune and warn on unused)"
        ),
    )
    p.add_argument(
        "--keep-docstrings",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="keep docstrings (default: on)",
    )
    p.add_argument(
        "--keep-comments",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="keep comments (default: on)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="show the plan and stats without writing files",
    )
    p.add_argument(
        "--check",
        action="store_true",
        help="verify no changes are needed (fails if the output would change)",
    )
    p.add_argument(
        "--stdout",
        action="store_true",
        help="write consolidated output to stdout instead of files",
    )
    p.add_argument(
        "--diff",
        action="store_true",
        help="emit a unified diff against the existing output (if present)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="always write, even if the output directory already exists",
    )
    # Config / formatting
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="path to pyproject.toml containing a [tool.pyrefactor] section",
    )
    p.add_argument(
        "--format",
        action="store_true",
        help="run the configured formatter on the generated file",
    )
    p.add_argument(
        "--formatter",
        choices=["black", "ruff"],
        default="black",
        help="formatter used by --format (default: black)",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["TRACE", "DEBUG", "INFO", "WARNING", "ERROR"],
        help="log level (default: INFO)",
    )
    p.add_argument("--log-file", type=Path, default=None, help="also log to this file")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level, args.log_file)
    try:
        result = consolidate(
            args.source,
            args.output,
            filename=args.name,
            workers=args.workers,
            executor=args.executor,
            exclude=tuple(args.exclude),
            include=tuple(args.include),
            imports_mode=args.imports,
            entry=tuple(args.entry),
            dry_run=args.dry_run,
            check=args.check,
            stdout=args.stdout,
            config=args.config,
            do_format=args.format,
            formatter=args.formatter,
            diff=args.diff,
            force=args.force,
            keep_docstrings=args.keep_docstrings,
            strip_comments=not args.keep_comments,
            keep_comments=args.keep_comments,
            progress=args.progress,
            copy_data=args.copy_data,
            data_glob=tuple(args.data_glob),
            embed_data=args.embed_data,
            entry_keep_all=args.entry_keep_all,
            main_name=args.main_name,
            generate_guard=args.generate_guard,
        )
    except ConsolidationError as exc:
        logger.error("{}", exc)
        return 2
    except Exception:
        logger.exception("Unexpected failure")
        return 1

    if result.dry_run:
        logger.info(
            "{}: {} modules, {} skipped, {} exports, {} warnings",
            "Check" if args.check else "Dry-run",
            result.modules,
            len(result.skipped),
            result.exports,
            len(result.warnings),
        )
        return 0

    if result.files:
        logger.success(
            "{} modules -> {} ({} exported names)",
            result.modules,
            result.output_dir,
            result.exports,
        )
    elif args.stdout:
        logger.success(
            "{} modules -> stdout ({} exported names)",
            result.modules,
            result.exports,
        )

    if result.copied_data_files:
        logger.info("Copied {} data file(s)", len(result.copied_data_files))
    if result.embedded_data_files:
        logger.info("Embedded {} data file(s)", len(result.embedded_data_files))
    if result.skipped:
        logger.error("{} file(s) skipped because of errors", len(result.skipped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

---

## `src/refactor_utils/data/README.md`

```markdown
# Data Files

This directory contains example data files that demonstrate proper handling of project data during consolidation.

When `pyrefactor` processes a project, it discovers data files alongside Python code and can either copy them to the output directory, embed them in the generated `.py` file, or both.

## Discovery

- Discovered via `discovery.py` → `discover_data_files()`
- Controlled by `--copy-data` / `--no-copy-data` / `--data-glob`

## Copying

When `--copy-data` is enabled (default) files are written to the output directory preserving their relative paths.

```bash
pyrefactor src/refactor_utils -o output --copy-data
```

## Embedding

When `--embed-data` is enabled (default is to follow `--copy-data`) the data is written into the generated file as a `_DATA_FILES` dictionary:

- UTF-8 text files (JSON, INI, YAML, CSV, ...) are embedded as **plain strings**.
- Binary files (fonts, images, archives, ...) are embedded as **base64**.

The generated module exposes helper functions:

| helper | purpose |
| --- | --- |
| `read_data(rel_path, binary=False)` | Read an embedded file as str (or bytes if `binary=True`). |
| `has_data(rel_path)` | Check whether a file is embedded. |
| `list_data()` | Return the sorted list of embedded paths. |
| `extract_data_files(target_dir=None)` | Materialise all embedded files to disk (temp dir by default). Returns `dict[rel_path, Path]`. |

Example:

```python
import consolidated as pkg

cfg = pkg.read_data("data/sample.json")  # -> str
icon = pkg.read_data("data/font.ttf", binary=True)  # -> bytes
pkg.extract_data_files("/tmp/mydata")  # materialise on disk
```

## Files

- `sample.json` – Example JSON data (common for configs)
- `example.csv` – Example CSV data (common for datasets)
- `config.ini`  – Example INI config

## Usage Examples

```bash
# Copy data files to output
pyrefactor src/refactor_utils -o output --copy-data

# Embed data files inside the generated .py
pyrefactor src/refactor_utils -o output --embed-data

# Both
pyrefactor src/refactor_utils -o output --copy-data --embed-data

# Only data matching a glob
pyrefactor src/refactor_utils -o output --data-glob "*.json"
```

## Caveats

The consolidated output is a single module. Code that opens data files by
relative path (e.g. `open("data/x.json")` or
`Path(__file__).parent / "x.json"`) will **not** find embedded data
automatically. Use the helpers above, or call `extract_data_files()` first
and pass the returned paths to your code.
```

---

## Notes on compatibility

- **`__init__.py`,
   `logging_setup.py`,
   `imports.py`,
   `ordering.py`,
   `discovery.py`,
   `parser.py`** are unchanged in behaviour; only `models.py` needed the dataclass-field fixes. All the other modules in the original project reference the broken dataclasses (e.g. `Block(...)` was silently constructing garbage). With `models.py` fixed, those modules now work as intended.
- **`discover_data_files`** is now called with `copy_data=copy_data or embed_data` so embedded-only runs still discover files.
- **`ConsolidationResult`** is now constructed with keyword arguments everywhere; positional construction was the source of the original `AttributeError`s.
- **`--check`** correctly fails when the output file is missing, and refuses to run together with `--stdout`.
- **`--main-name`** already existed; the `main` tail-detection and `--generate-guard` paths were already correct once `Block` had real fields.
- **Generated `__all__`** is emitted only when at least one source module declared one; names that were renamed or deduplicated away are silently dropped (logged at `DEBUG`).
