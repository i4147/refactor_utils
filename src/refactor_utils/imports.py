"""Merge, de-duplicate, sort and render the import section."""

from __future__ import annotations
import sys
from collections import defaultdict
from typing import Iterable, Mapping
from .models import ImportSpec

_STDLIB = getattr(sys, "stdlib_module_names", frozenset())
_LINE = 88


def render_import_line(spec: ImportSpec) -> str:
    """Render a single import specification as source text.

    Args:
        spec: The import to render.

    Returns:
        A one-line ``import``/``from ... import ...`` statement.

    """
    if spec.star:
        return f"from {spec.module} import *"
    if not spec.is_from:
        return f"import {spec.module}" + (f" as {spec.alias}" if spec.alias else "")
    item = spec.name if not spec.alias else f"{spec.name} as {spec.alias}"
    return f"from {spec.module} import {item}"


def _group(module: str) -> int:
    """Classify a module for import grouping.

    Args:
        module: Dotted module name.

    Returns:
        ``0`` for the standard library, ``1`` for everything else.

    """
    return 0 if module.split(".")[0] in _STDLIB else 1


def _item_key(item: str) -> tuple:
    """Build a sort key that orders constants, classes and functions.

    Args:
        item: Rendered import item, optionally with an ``as`` alias.

    Returns:
        Sort key tuple.

    """
    base = item.split(" as ")[0]
    kind = 0 if (base.isupper() and len(base) > 1) else 1 if base[:1].isupper() else 2
    return (kind, base.lower(), item)


def _wrap(module: str, items: list[str]) -> str:
    """Render a ``from`` import, wrapping it in parentheses when too long.

    Args:
        module: Module the names are imported from.
        items: Already rendered import items.

    Returns:
        A single line, or a parenthesized multi-line import.

    """
    line = f"from {module} import {', '.join(items)}"
    if len(line) <= _LINE:
        return line
    body = "".join(f"    {i},\n" for i in items)
    return f"from {module} import (\n{body})"


def future_section(names: Iterable[str]) -> str:
    """Render the ``from __future__ import ...`` line.

    Args:
        names: Feature names to enable.

    Returns:
        The import line, or an empty string when ``names`` is empty.

    """
    names = sorted(set(names))
    return f"from __future__ import {', '.join(names)}" if names else ""


def build_import_section(keys: Iterable[tuple], ext_final: Mapping[tuple, str]) -> str:
    """Merge external imports into a grouped, deduplicated import section.

    Args:
        keys: External import keys to emit.
        ext_final: Mapping of import key to the alias it must use after
            conflict resolution.

    Returns:
        The rendered import block, standard library first.

    """
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
