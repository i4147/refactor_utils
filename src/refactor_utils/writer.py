"""Render the consolidated module and its ``__init__`` re-export stub."""

from __future__ import annotations
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


def _sep(prev_kind: str, cur_kind: str, prev_text: str, cur_text: str) -> str:
    simple = ("const", "misc")
    if prev_kind in simple and cur_kind in simple:
        return "\n" if "\n" not in prev_text and "\n" not in cur_text else "\n\n"
    return "\n\n\n"


def _format_with_black(text: str) -> str:
    """Try to format with black, fallback to original."""
    try:
        import importlib

        black = importlib.import_module("black")
        return black.format_str(text, mode=black.Mode())
    except Exception:
        return text


def _format_with_ruff(text: str) -> str:
    """Try ruff format via subprocess."""
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


def render_consolidated(
    plan: Plan,
    source_name: str,
    do_format: bool = False,
    formatter: str = "black",
) -> str:
    parts: list[str] = [
        f'"""Consolidated from ``{source_name}`` by pyrefactor (generated file)."""'
    ]
    head: list[str] = []
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
    seen_sections: set[str] = set()
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
