"""Render a :class:`~refactor_utils.resolver.Plan` into final source files.

Holds the layout-specific renderers (single, package, module, flat, split) plus
the generated ``__init__.py`` that re-exports the public API.
"""

from __future__ import annotations
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


def _sep(prev_kind: str, cur_kind: str, prev_text: str, cur_text: str) -> str:
    """Choose the vertical spacing between two consecutive blocks.

    Args:
        prev_kind: Section kind of the previous block.
        cur_kind: Section kind of the current block.
        prev_text: Rendered previous block.
        cur_text: Rendered current block.

    Returns:
        The separator string to insert (one or two blank lines).

    """
    simple = ("const", "misc")
    if prev_kind in simple and cur_kind in simple:
        return "\n" if "\n" not in prev_text and "\n" not in cur_text else "\n\n"
    return "\n\n\n"


def _format_with_black(text: str) -> str:
    """Try to format with black, fallback to original."""
    try:
        import black  # type: ignore

        mode = black.Mode()
        return black.format_str(text, mode=mode)
    except Exception:
        return text


def _format_with_ruff(text: str) -> str:
    """Try ruff format via subprocess."""
    try:
        import subprocess, tempfile, os

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(text)
            tmp_path = tmp.name
        # Try ruff format
        subprocess.run(["ruff", "format", tmp_path], capture_output=True, timeout=5)
        out = Path(tmp_path).read_text(encoding="utf-8")
        os.unlink(tmp_path)
        return out
    except Exception:
        return text


def render_consolidated(
    plan: Plan,
    source_name: str,
    namespace: bool = False,
    preserve_modules: bool = False,
    source_map: bool = False,
    do_format: bool = False,
    formatter: str = "black",
) -> str:
    """Render the plan as one dependency-ordered module.

    Args:
        plan: Plan produced by the resolver.
        source_name: Name of the consolidated source, used in the docstring.
        namespace: Emit ``# namespace:`` comments per block.
        preserve_modules: Emit ``# Original module:`` comments per block.
        source_map: Emit ``# source: file:line`` comments per block.
        do_format: Run an external formatter over the result.
        formatter: ``"black"`` or ``"ruff"``.

    Returns:
        The generated module source.

    """
    parts: list[str] = [
        f'"""Consolidated from ``{source_name}`` by pyrefactor (generated file)."""'
    ]
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
        # Namespace / preserve-modules handling: emit module-like comments
        prefix_comments = []
        if preserve_modules and blk.module:
            prefix_comments.append(f"# Original module: {blk.module}")
        if source_map and blk.origin:
            prefix_comments.append(f"# source: {blk.origin}")
        if namespace and blk.module:
            # Emit a pseudo-namespace comment (not actual class to preserve semantics)
            prefix_comments.append(f"# namespace: {blk.module}")
        # Section header
        if prev is None or blk.section != prev.section:
            piece += f"{_RULE}\n# {_TITLES[blk.section]}\n{_RULE}\n\n\n"
            sep = "\n\n\n" if prev is not None else ""
        else:
            sep = _sep(prev.kind, blk.kind, prev.text, blk.text)
        # Add prefix comments before block text
        if prefix_comments:
            piece += "\n".join(prefix_comments) + "\n"
        piece += blk.text
        body.append(sep + piece)
        prev = blk
    parts.append("".join(body))
    text = "\n\n\n".join(p for p in parts if p).rstrip() + "\n"
    if do_format:
        if formatter == "ruff":
            text = _format_with_ruff(text)
        else:
            # Try black first, then ruff
            formatted = _format_with_black(text)
            if formatted == text:
                formatted = _format_with_ruff(text)
            text = formatted
    return text


def render_init(plan: Plan, source_name: str, module: str = "_consolidated") -> str:
    """Render the package ``__init__.py`` that re-exports the public API.

    Args:
        plan: Plan produced by the resolver.
        source_name: Name of the consolidated source, used in the docstring.
        module: Module inside the package to import the names from.

    Returns:
        The generated ``__init__.py`` source.

    """
    groups = [
        ("constants", plan.exports["constants"]),
        ("classes", plan.exports["classes"]),
        ("functions", plan.exports["functions"]),
    ]
    names = [n for _, ns in groups for n in ns]
    out = [f'"""Public API of ``{source_name}`` (generated by pyrefactor)."""\n']
    if names:
        imports = ""
        for name in names:
            # Aliased re-exports (``from .sub import helper as public_helper``)
            # need the target name imported under the public name.
            target = plan.reexports.get(name, name)
            imports += (
                f"    {target} as {name},\n" if target != name else f"    {name},\n"
            )
        out.append(f"from .{module} import (\n{imports})\n")
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


def render_flat(plan: Plan, source_name: str, **kwargs) -> str:
    """Render blocks without section headers.

    Args:
        plan: Plan produced by the resolver.
        source_name: Name of the consolidated source.
        **kwargs: ``preserve_modules`` and ``source_map`` comment toggles.

    Returns:
        The generated flat module source.

    """
    parts: list[str] = [f'"""Flat consolidated from ``{source_name}``."""']
    if plan.future_names:
        parts.append(future_section(plan.future_names))
    if plan.imports_text:
        parts.append(plan.imports_text)
    for blk in plan.blocks:
        hdr = ""
        if kwargs.get("preserve_modules") and blk.module:
            hdr = f"# {blk.module}\n"
        if kwargs.get("source_map") and blk.origin:
            hdr += f"# {blk.origin}\n"
        parts.append(hdr + blk.text)
    text = "\n\n".join(parts).rstrip() + "\n"
    if kwargs.get("do_format"):
        text = (
            _format_with_black(text)
            if kwargs.get("formatter") != "ruff"
            else _format_with_ruff(text)
        )
    return text


def render_split(plan: Plan, source_name: str) -> dict[str, str]:
    """Split mode: return mapping filename -> content for const/class/func/entry splits."""
    from collections import defaultdict

    buckets: dict[str, list[str]] = defaultdict(list)
    for blk in plan.blocks:
        # Map section to file
        fname = {
            "const": "const.py",
            "class": "class.py",
            "func": "func.py",
            "entry": "main.py",
        }.get(blk.section, "misc.py")
        buckets[fname].append(blk.text)
    # Build each file with header
    out: dict[str, str] = {}
    header = f'"""Split from ``{source_name}`` by pyrefactor."""\n'
    for fname, texts in buckets.items():
        content = header
        if plan.future_names and fname == "const.py":
            content += future_section(plan.future_names) + "\n\n"
        if plan.imports_text and fname == "const.py":
            content += plan.imports_text + "\n\n"
        content += "\n\n".join(texts) + "\n"
        out[fname] = content
    # Also generate an __init__.py that re-exports
    init = render_init(plan, source_name, module="const")
    # In split mode, init should import from each split file; simplify to import from const
    out["__init__.py"] = init
    return out


def render_package(plan: Plan, source_name: str, **kwargs) -> str:
    """Render the consolidated module for package mode.

    Args:
        plan: Plan produced by the resolver.
        source_name: Name of the consolidated source.
        **kwargs: Forwarded to :func:`render_consolidated`.

    Returns:
        The generated module source.

    """
    # For now identical to consolidated but with different module docstring
    return render_consolidated(plan, source_name, **kwargs)


def render_module(plan: Plan, source_name: str, **kwargs) -> str:
    """Render a single module file without a generated ``__init__.py``.

    Args:
        plan: Plan produced by the resolver.
        source_name: Name of the consolidated source.
        **kwargs: Forwarded to :func:`render_consolidated`.

    Returns:
        The generated module source.

    """
    return render_consolidated(plan, source_name, **kwargs)
