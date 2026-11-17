"""Import-section rendering helpers."""
from __future__ import annotations

import sys
from collections import defaultdict
from typing import Iterable

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


def sort_key(name: str) -> tuple:
    stripped = name.lstrip("_")
    return (0 if name.startswith("_") else 1, stripped.lower(), name)


def _item_key(item: str) -> tuple:
    return sort_key(item.split(" as ")[0])


def _wrap(module: str, items: list[str]) -> str:
    line = f"from {module} import {', '.join(items)}"
    if len(line) <= _LINE:
        return line
    body = "".join(f"    {i},\n" for i in items)
    return f"from {module} import (\n{body})"


def _dedupe_aliases(items: Iterable[str]) -> set[str]:
    plain = {i for i in items if " as " not in i}
    aliased = {i for i in items if " as " in i}
    return {
        i if i.split(" as ", 1)[1] != i.split(" as ", 1)[0] else i.split(" as ", 1)[0]
        for i in aliased
    } | plain


def future_section(names: Iterable[str]) -> str:
    names = sorted(set(names))
    return f"from __future__ import {', '.join(names)}" if names else ""


def build_import_section(keys: set, ext_final: dict) -> str:
    """Render the top-of-file import section from external import keys."""
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
    chunks = []
    for grp in (0, 1):
        lines = []
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
            items = _dedupe_aliases(items)
            if items:
                lines.append(_wrap(mod, sorted(items, key=_item_key)))
        if lines:
            chunks.append("\n".join(lines))
    return "\n\n".join(chunks)
