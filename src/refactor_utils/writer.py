from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from .imports import future_section
from .logging_setup import logger
from .resolver import Plan, RenderedBlock

_RULE = "# " + "-" * 75
_TITLES = {
    "resources": "Bundled resources",
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
    try:
        import black
    except ImportError:
        logger.warning("--format: black is not installed; leaving output unformatted")
        return text
    try:
        return black.format_str(text, mode=black.Mode())
    except Exception as exc:
        # black raises InvalidInput on syntax problems in the generated code,
        # which is a real signal rather than something to swallow silently.
        logger.warning("--format: black failed ({}); leaving output unformatted", exc)
        return text


def _format_with_ruff(text: str) -> str:
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(text)
            tmp_path = tmp.name
        proc = subprocess.run(
            ["ruff", "format", tmp_path], capture_output=True, timeout=30
        )
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", "replace").strip()
            logger.warning(
                "--format: ruff exited {}{}; leaving output unformatted",
                proc.returncode,
                f" ({stderr})" if stderr else "",
            )
        return Path(tmp_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("--format: ruff is not installed; leaving output unformatted")
        return text
    except Exception as exc:
        logger.warning("--format: ruff failed ({}); leaving output unformatted", exc)
        return text
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _all_section(names: list) -> str:
    items = ", ".join(repr(n) for n in names)
    if len(names) <= 5 and len(items) <= 72:
        return f"__all__ = [{items}]"
    body = "".join(f"    {n!r},\n" for n in names)
    return f"__all__ = [\n{body}]"


def render_consolidated(
    plan: Plan,
    source_name: str,
    do_format: bool = False,
    formatter: str = "black",
) -> str:
    parts = [
        f'"""Consolidated from ``{source_name}`` by pyrefactor (generated file)."""'
    ]
    head = []
    if plan.future_names:
        head.append(future_section(plan.future_names))
    if plan.imports_text:
        head.append(plan.imports_text)
    if plan.guards:
        head.append("\n\n".join(plan.guards))
    if plan.all_names:
        head.append(_all_section(plan.all_names))
    if head:
        parts.append("\n\n".join(head))
    body = []
    prev = None
    seen_sections: set = set()
    blocks = list(plan.blocks)
    if plan.resources_code:
        blocks.insert(0, RenderedBlock("resources", "resources", plan.resources_code))
    for blk in blocks:
        piece = ""
        first_of_section = blk.section not in seen_sections
        if prev is None or blk.section != prev.section:
            if first_of_section:
                piece += (
                    f"{_RULE}\n# {_TITLES.get(blk.section, blk.section)}\n{_RULE}\n\n\n"
                )
            sep = "\n\n\n" if prev is not None else ""
        else:
            sep = _sep(prev.kind, blk.kind, prev.text, blk.text)
        seen_sections.add(blk.section)
        piece += blk.text
        body.append(sep + piece)
        prev = blk
    parts.append("".join(body))
    head_text = "\n\n\n".join(p for p in parts[1:] if p)
    text = parts[0] + "\n\n" + head_text
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
