"""Build a single-file PDF of the MkDocs documentation.

The builder is deliberately self-contained (no browser, no LaTeX):

1. the page order is read from ``mkdocs.yml``'s ``nav``;
2. ``::: module`` mkdocstrings directives in the API pages are expanded with
   :mod:`inspect` so the PDF contains real signatures/docstrings;
3. every page is converted to HTML with python-markdown and rendered into one
   document with :mod:`fpdf2` (pure Python, DejaVu fonts for Unicode);
4. the result is written to ``refactor_utils.pdf`` in the repository root.

Run from the repository root::

    python arena/build_pdf.py
    python arena/build_pdf.py --output docs/refactor_utils.pdf
"""

from __future__ import annotations

import datetime
import importlib
import inspect
import re
import sys
import textwrap
from pathlib import Path

import yaml
from fpdf import FPDF
from fpdf.enums import TextEmphasis
from fpdf.fonts import FontFace
from markdown import markdown

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "autoall" / "src"))

MKDOCS_YML = ROOT / "mkdocs.yml"
DOCS_DIR = ROOT / "docs"
DEFAULT_OUTPUT = ROOT / "refactor_utils.pdf"
FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")

VERSION = "1.5.0"


def _yaml_loader() -> type:
    """Return a YAML loader able to read mkdocs' ``!!python/name`` tags.

    Returns:
        A :class:`yaml.SafeLoader` subclass with the pymdownx tag registered.
    """
    loader = yaml.SafeLoader

    def _python_name(loader_, suffix, node):  # noqa: ANN001 - yaml protocol
        return loader_.construct_scalar(node)

    loader.add_multi_constructor("tag:yaml.org,2002:python/name", _python_name)
    return loader


def _collect_nav_pages(nav: object) -> list[tuple[str, str, str]]:
    """Flatten the mkdocs ``nav`` mapping into ``(section, page, title)`` tuples.

    Args:
        nav: The ``nav`` value from ``mkdocs.yml`` (nested lists/dicts).

    Returns:
        One tuple per page in menu order (deduplicated), where ``section`` is
        the enclosing nav section (``""`` for top level).
    """
    pages: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    def walk(item: object, section: str) -> None:
        if isinstance(item, str):
            if item not in seen:
                seen.add(item)
                pages.append((section, item, ""))
        elif isinstance(item, list):
            for value in item:
                walk(value, section)
        elif isinstance(item, dict):
            for title, value in item.items():
                if isinstance(value, str):
                    if value not in seen:
                        seen.add(value)
                        pages.append((section, value, title))
                else:
                    walk(value, title)

    walk(nav, "")
    return pages


def _signature(obj: object) -> str:
    """Return a compact signature string for ``obj``.

    Args:
        obj: Function, method or class to describe.

    Returns:
        The signature (without ``self``), or an empty string.
    """
    try:
        sig = inspect.signature(obj)  # type: ignore[arg-type]
    except (TypeError, ValueError):  # pragma: no cover - builtins
        return ""
    params = [p for p in sig.parameters.values() if p.name not in ("self", "cls")]
    try:
        rendered = str(sig.replace(parameters=params))
    except ValueError:  # pragma: no cover - defensive
        return ""
    return rendered


def _format_doc(doc: str | None, indent: str = "") -> str:
    """Turn a Google-style docstring into plain Markdown.

    Args:
        doc: Raw docstring (may be ``None``).
        indent: Prefix added to every line (for nested members).

    Returns:
        Markdown text, empty when there is no docstring.
    """
    if not doc:
        return ""
    lines = [line.rstrip() for line in textwrap.dedent(doc).strip().splitlines()]
    return "\n".join(indent + line for line in lines)


def _api_markdown(dotted: str) -> str:
    """Expand a mkdocstrings ``::: dotted`` directive into Markdown.

    Args:
        dotted: Dotted module (or object) path.

    Returns:
        Markdown text describing the module and its public members.
    """
    try:
        module = importlib.import_module(dotted)
    except Exception as exc:  # noqa: BLE001 - never fail the build
        return f"*Could not import `{dotted}`: {exc}*"
    parts: list[str] = []
    if inspect.ismodule(module):
        parts.append(_format_doc(inspect.getdoc(module)))
        members = []
        for name in sorted(dir(module)):
            if name.startswith("_"):
                continue
            member = getattr(module, name)
            if not (inspect.isfunction(member) or inspect.isclass(member)):
                continue
            if getattr(member, "__module__", None) != dotted:
                continue
            members.append((name, member))
        for name, member in members:
            kind = "class" if inspect.isclass(member) else "function"
            sig = _signature(member)
            parts.append(f"#### `{name}{sig}`\n\n*{kind}*")
            parts.append(_format_doc(inspect.getdoc(member)))
    else:
        parts.append(f"#### `{dotted}`")
        parts.append(_format_doc(inspect.getdoc(module)))
    return "\n\n".join(part for part in parts if part)


def expand_directives(text: str) -> str:
    """Replace ``::: module`` directives with generated Markdown.

    Args:
        text: Page Markdown.

    Returns:
        Markdown where every mkdocstrings directive was expanded.
    """
    out: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^:::\s*([\w\.]+)\s*$", line.strip())
        if match:
            out.append(_api_markdown(match.group(1)))
        else:
            out.append(line)
    return "\n".join(out)


def md_to_html(markdown_text: str) -> str:
    """Convert page Markdown to HTML that :mod:`fpdf2` can render.

    Args:
        markdown_text: Page content after directive expansion.

    Returns:
        Sanitised HTML (tables flattened, links reduced to their text).
    """
    html = markdown(
        markdown_text,
        extensions=["fenced_code", "tables", "admonition", "sane_lists"],
    )
    # fpdf2 cannot render links or nested tags inside table cells.
    html = re.sub(r"<a [^>]*>(.*?)</a>", r"\1", html, flags=re.S)
    html = re.sub(r"<details>.*?</details>", "", html, flags=re.S)
    html = re.sub(r"<summary>.*?</summary>", "", html, flags=re.S)
    html = re.sub(r'<div class="admonition[^"]*">', "<blockquote>", html)
    html = html.replace("</div>", "")
    html = re.sub(r"<span[^>]*>|</span>", "", html)
    html = re.sub(r"<br\s*/?>", " ", html)

    def _flatten_cell(match: re.Match) -> str:
        tag = match.group(1)
        inner = re.sub(r"<[^>]+>", "", match.group(2))
        return f"<{tag}>{inner}</{tag}>"

    return re.sub(r"<(td|th)>(.*?)</\1>", _flatten_cell, html, flags=re.S)


def _tag_styles() -> dict[str, FontFace]:
    """Return the HTML tag → font mapping used by fpdf2.

    Returns:
        Mapping of tag names to :class:`fpdf.fonts.FontFace`.
    """
    styles: dict[str, FontFace] = {
        tag: FontFace(family="dejavu", size_pt=size)
        for tag, size in {
            "p": 9,
            "li": 9,
            "blockquote": 9,
            "dt": 9,
            "dd": 9,
            "a": 9,
        }.items()
    }
    styles["code"] = FontFace(family="mono", size_pt=8)
    styles["pre"] = FontFace(family="mono", size_pt=7.5)
    for tag, size in {"h1": 15, "h2": 12, "h3": 10.5, "h4": 9.5}.items():
        styles[tag] = FontFace(family="dejavu", emphasis=TextEmphasis.B, size_pt=size)
    styles["b"] = FontFace(family="dejavu", emphasis=TextEmphasis.B, size_pt=9)
    styles["strong"] = styles["b"]
    styles["i"] = FontFace(family="dejavu", emphasis=TextEmphasis.I, size_pt=9)
    styles["em"] = styles["i"]
    return styles


class DocPDF(FPDF):
    """``fpdf2`` document with a footer showing the page number."""

    def footer(self) -> None:
        """Render the page footer (title + ``page / total`` once known)."""
        self.set_y(-12)
        self.set_font("dejavu", "", 7)
        self.set_text_color(120, 120, 120)
        total = getattr(self, "page_count_hint", "")
        suffix = f" / {total}" if total else ""
        self.cell(
            0,
            6,
            f"refactor_utils documentation — page {self.page_no()}{suffix}",
            align="C",
        )


def _setup_fonts(pdf: FPDF) -> None:
    """Register the DejaVu fonts used by the document.

    Args:
        pdf: Document to configure.
    """
    pdf.add_font("dejavu", "", str(FONT_DIR / "DejaVuSans.ttf"))
    pdf.add_font("dejavu", "B", str(FONT_DIR / "DejaVuSans-Bold.ttf"))
    pdf.add_font("dejavu", "I", str(FONT_DIR / "DejaVuSans.ttf"))
    pdf.add_font("mono", "", str(FONT_DIR / "DejaVuSansMono.ttf"))
    pdf.add_font("mono", "B", str(FONT_DIR / "DejaVuSansMono-Bold.ttf"))


def _render_document(
    pages: list[tuple[str, str, str]],
    toc: list[tuple[str, int]] | None = None,
) -> tuple[DocPDF, list[tuple[str, int]]]:
    """Render every documentation page into a new document.

    The table of contents needs the final page numbers, so the document is
    rendered twice: once to collect them (``toc`` is ``None``, the ToC page is
    rendered empty) and once with the collected entries. Both passes lay out
    exactly the same pages, so the numbers stay valid.

    Args:
        pages: ``(section, page path, nav title)`` tuples in navigation order.
        toc: ``(title, page number)`` entries for the table of contents, or
            ``None`` on the first pass.

    Returns:
        ``(pdf, collected)`` where ``collected`` maps titles to page numbers.
    """
    pdf = DocPDF()
    _setup_fonts(pdf)
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.set_margins(16, 16, 16)
    pdf.set_title("refactor_utils documentation")
    pdf.set_author("refactor_utils contributors")
    pdf.set_font("dejavu", "", 9)
    styles = _tag_styles()

    # --- cover -----------------------------------------------------------
    pdf.add_page()
    pdf.ln(60)
    pdf.set_font("dejavu", "B", 26)
    pdf.multi_cell(0, 12, "refactor_utils", align="C")
    pdf.ln(4)
    pdf.set_font("dejavu", "", 13)
    pdf.multi_cell(
        0,
        8,
        "Consolidate a Python package into a single,\ndependency-ordered file",
        align="C",
    )
    pdf.ln(10)
    pdf.set_font("dejavu", "", 10)
    pdf.multi_cell(
        0,
        6,
        f"Version {VERSION}\nGenerated {datetime.date.today().isoformat()}\n\n"
        "Single-file PDF of the documentation site (docs/).",
        align="C",
    )

    # --- table of contents ------------------------------------------------
    pdf.add_page()
    pdf.set_font("dejavu", "B", 14)
    pdf.cell(0, 10, "Contents", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("dejavu", "", 9)
    width = pdf.w - pdf.l_margin - pdf.r_margin
    if toc:
        for title, number in toc:
            pdf.cell(width - 14, 6, title[:110], new_x="RIGHT")
            pdf.cell(14, 6, str(number), align="R", new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.cell(
            0,
            6,
            "(page numbers are filled in after layout)",
            new_x="LMARGIN",
            new_y="NEXT",
        )

    # --- pages -----------------------------------------------------------
    collected: list[tuple[str, int]] = []
    current_part = ""
    for section, page, nav_title in pages:
        page_path = DOCS_DIR / page
        if not page_path.is_file():
            continue
        source_md = page_path.read_text(encoding="utf-8")
        title_match = re.search(r"^#\s+(.+)$", source_md, flags=re.M)
        title = nav_title or (
            title_match.group(1).strip() if title_match else page_path.stem
        )
        body = expand_directives(source_md)
        # Drop the leading H1: the page title is rendered by the builder.
        body = re.sub(r"^#\s+.+\n?", "", body, count=1)
        html = md_to_html(body)

        pdf.add_page()
        collected.append((title, pdf.page_no()))
        if section != current_part:
            current_part = section
            pdf.set_font("dejavu", "", 8)
            pdf.set_text_color(120, 120, 120)
            pdf.cell(
                0,
                5,
                (section or "refactor_utils").upper(),
                new_x="LMARGIN",
                new_y="NEXT",
            )
            pdf.set_text_color(0, 0, 0)
        pdf.start_section(title, level=0)
        pdf.set_font("dejavu", "B", 15)
        pdf.multi_cell(0, 8, title, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)
        pdf.write_html(html, tag_styles=styles)
        pdf.ln(4)

    pdf.page_count_hint = str(pdf.pages_count + 1)
    return pdf, collected


def build_pdf(output: Path = DEFAULT_OUTPUT) -> Path:
    """Build the single-file documentation PDF.

    Args:
        output: Destination PDF path.

    Returns:
        The written path.
    """
    config = yaml.load(MKDOCS_YML.read_text(encoding="utf-8"), Loader=_yaml_loader())
    pages = _collect_nav_pages(config.get("nav", [])) or [("", "index.md", "")]
    _, toc = _render_document(pages)
    pdf, _ = _render_document(pages, toc)
    pdf.output(str(output))
    return output


def main(argv: list[str] | None = None) -> int:
    """Build the PDF from the command line.

    Args:
        argv: Arguments; ``--output PATH`` selects the destination.

    Returns:
        Process exit code.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    output = DEFAULT_OUTPUT
    if "--output" in args:
        index = args.index("--output")
        output = Path(args[index + 1])
    written = build_pdf(output)
    print(f"Wrote {written} ({written.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
