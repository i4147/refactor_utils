"""Build a single-file PDF of the MkDocs documentation.

Reads the page order from ``mkdocs.yml``'s ``nav``, converts every Markdown
page to HTML with python-markdown, assembles one styled HTML document and
renders it to a single ``refactor_utils.pdf`` next to the repository root
using fpdf2 (pure Python -- no system libraries such as pango/cairo required).

Run from the repository root:

    python tools/arena/build_pdf.py

Output: ``/home/user/refactor_utils/refactor_utils.pdf``
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from markdown import markdown

# mkdocs.yml contains !!python/name tags (pymdownx custom fences); ignore them
_yaml_loader = yaml.SafeLoader

def _python_name_constructor(loader, suffix, node):
    return loader.construct_scalar(node)

_yaml_loader.add_multi_constructor(
    "tag:yaml.org,2002:python/name", _python_name_constructor
)
from fpdf import FPDF
from fpdf.html import HTMLMixin

ROOT = Path(__file__).resolve().parents[2]
MKDOCS_YML = ROOT / "mkdocs.yml"
DOCS_DIR = ROOT / "docs"
OUTPUT_PDF = ROOT / "refactor_utils.pdf"

FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")

def _collect_nav_pages(nav) -> list[str]:
    """Flatten the mkdocs nav into a list of relative page paths (deduped)."""
    pages: list[str] = []
    seen: set[str] = set()

    def walk(item):
        if isinstance(item, str):
            if item not in seen:
                seen.add(item)
                pages.append(item)
        elif isinstance(item, dict):
            for value in item.values():
                walk(value)
        elif isinstance(item, list):
            for value in item:
                walk(value)

    walk(nav)
    return pages


# Common Unicode punctuation outside core-font (latin-1) range, mapped to
# ASCII equivalents so fpdf2's built-in fonts can render them.
_UNICODE_TO_ASCII = {
    "\u2014": "-",  # em dash
    "\u2013": "-",  # en dash
    "\u2212": "-",  # minus sign
    "\u201c": '"',  # left double quotation mark
    "\u201d": '"',  # right double quotation mark
    "\u201e": '"',  # double low-9 quotation mark
    "\u2018": "'",  # left single quotation mark
    "\u2019": "'",  # right single quotation mark
    "\u201a": "'",  # single low-9 quotation mark
    "\u2022": "*",  # bullet
    "\u2023": "*",  # triangular bullet
    "\u00b7": "*",  # middle dot
    "\u2026": "...",  # horizontal ellipsis
    "\u2248": "~",  # almost equal to
    "\u2260": "!=",  # not equal to
    "\u2264": "<=",  # less-than or equal to
    "\u2265": ">=",  # greater-than or equal to
    "\u00d7": "x",  # multiplication sign
    "\u2192": "->",  # rightwards arrow
    "\u2190": "<-",  # leftwards arrow
    "\u2191": "^",  # upwards arrow
    "\u2193": "v",  # downwards arrow
    "\u00a0": " ",  # non-breaking space
}
_UNICODE_MAP = str.maketrans(_UNICODE_TO_ASCII)


def _sanitize(text: str) -> str:
    return text.translate(_UNICODE_MAP)


def _md_to_html(markdown_text: str) -> str:
    """Convert Markdown to HTML (extensions mirror mkdocs.yml)."""
    return markdown(
        _sanitize(markdown_text),
        extensions=[
            "admonition",
            "pymdownx.details",
            "pymdownx.superfences",
            "pymdownx.highlight",
            "pymdownx.inlinehilite",
            "pymdownx.tabbed",
            "toc",
        ],
    )


def build_pdf(output: Path = OUTPUT_PDF) -> Path:
    """Build the single-file PDF and return its path."""
    config = yaml.load(
        MKDOCS_YML.read_text(encoding="utf-8"), Loader=_yaml_loader
    )
    pages = _collect_nav_pages(config.get("nav", [])) or ["index.md"]

    class PDF(FPDF, HTMLMixin):
        pass

    pdf = PDF()
    # Unicode fonts so em-dashes, quotes, etc. render correctly.
    # Override the core "helvetica" so HTMLMixin's default text is Unicode too.
    pdf.add_font("helvetica", "", str(FONT_DIR / "DejaVuSans.ttf"))
    pdf.add_font("helvetica", "B", str(FONT_DIR / "DejaVuSans-Bold.ttf"))
    pdf.add_font("helvetica", "I", str(FONT_DIR / "DejaVuSans.ttf"))
    pdf.add_font("dejavu", "", str(FONT_DIR / "DejaVuSans.ttf"))
    pdf.add_font("dejavu", "B", str(FONT_DIR / "DejaVuSans-Bold.ttf"))
    pdf.add_font("dejavumono", "", str(FONT_DIR / "DejaVuSansMono.ttf"))
    pdf.add_font("dejavumono", "B", str(FONT_DIR / "DejaVuSansMono-Bold.ttf"))
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_margins(15, 15, 15)
    pdf.set_font("helvetica", "", 9)
    pdf.add_page()

    for page in pages:
        page_path = DOCS_DIR / page
        if not page_path.is_file():
            continue
        title = page_path.stem.replace("-", " ").title()
        source_md = page_path.read_text(encoding="utf-8")
        # Strip a leading H1 so we control the heading ourselves
        body_md = re.sub(r"^# .+\n?", "", source_md, count=1)
        pdf.set_font("helvetica", "B", 12)
        pdf.multi_cell(0, 6, f"{title}  ({page})")
        pdf.ln(2)
        pdf.write_html(_md_to_html(body_md))
        pdf.ln(4)

    pdf.output(str(output))
    return output


if __name__ == "__main__":
    out = build_pdf()
    print(f"Wrote {out} ({out.stat().st_size:,} bytes)")
