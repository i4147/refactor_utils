#!/usr/bin/env python3
"""Build the whole documentation as one PDF (``docs.pdf``).

    tools/build_pdf.py [-o docs.pdf] [--html-out combined.html]

The pages and their order come from the ``nav`` of ``mkdocs.yml``, so the PDF
always matches the MkDocs site.  Each page is converted from Markdown to HTML,
links between pages are turned into links inside the PDF, and the result is
rendered with xhtml2pdf (pure Python, no browser or LaTeX needed).

Requires: ``pip install -e ".[docs]"``.
"""

from __future__ import annotations

import argparse
import html
import posixpath
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

try:
    import markdown
    import yaml
    from xhtml2pdf import pisa
except ImportError as exc:  # pragma: no cover - environment problem
    sys.exit(f"error: {exc}. Install the docs extra: pip install -e '.[docs]'")

MD_EXTENSIONS = ["tables", "fenced_code", "admonition", "attr_list", "sane_lists"]

# Unicode fonts, tried in order.  Without one the PDF core fonts are used and
# the few non Latin-1 characters are transliterated (see ASCII_FALLBACK).
FONT_DIRS = [
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/dejavu"),
    Path("/usr/share/fonts/TTF"),
    Path("/Library/Fonts"),
    Path.home() / ".fonts",
]
FONT_FILES = {
    ("DocSans", "normal", "normal"): "DejaVuSans.ttf",
    ("DocSans", "bold", "normal"): "DejaVuSans-Bold.ttf",
    ("DocSans", "normal", "italic"): "DejaVuSans-Oblique.ttf",
    ("DocSans", "bold", "italic"): "DejaVuSans-BoldOblique.ttf",
    ("DocMono", "normal", "normal"): "DejaVuSansMono.ttf",
    ("DocMono", "bold", "normal"): "DejaVuSansMono-Bold.ttf",
}
ASCII_FALLBACK = {
    "\u2192": "->", "\u2190": "<-", "\u2014": "-", "\u2013": "-", "\u2026": "...",
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2022": "*",
}

CSS = """
@page {
    size: a4 portrait;
    margin: 2cm 1.8cm 2.2cm 1.8cm;
    @frame footer {
        -pdf-frame-content: footer;
        bottom: 0.9cm; margin-left: 1.8cm; margin-right: 1.8cm; height: 0.8cm;
    }
}
body { font-family: %(sans)s; font-size: 10pt; line-height: 1.35; color: #1f2328; }
h1 { font-size: 21pt; color: #283593; margin: 0 0 10pt 0; padding-bottom: 4pt;
     border-bottom: 1.5pt solid #283593; -pdf-outline-level: 0; }
h2 { font-size: 14pt; color: #283593; margin: 16pt 0 6pt 0; -pdf-outline-level: 1;
     -pdf-keep-with-next: true; }
h3 { font-size: 11.5pt; color: #333333; margin: 12pt 0 4pt 0; -pdf-outline-level: 2;
     -pdf-keep-with-next: true; }
p { margin: 0 0 7pt 0; }
a { color: #1a56db; text-decoration: none; }
ul, ol { margin: 0 0 7pt 0; }
li { margin-bottom: 2pt; }
code { font-family: %(mono)s; font-size: 8.8pt; color: #7a1f3d; }
pre { font-family: %(mono)s; font-size: 7.6pt; line-height: 1.3; color: #1f2328;
      background-color: #f4f5f7; border: 0.5pt solid #d5d8de; padding: 6pt;
      margin: 0 0 9pt 0; white-space: pre-wrap; }
pre code { font-size: 7.6pt; color: #1f2328; }
table { width: 100%%; border: 0.5pt solid #c9cdd4; margin: 0 0 10pt 0; }
th { background-color: #e8eaf6; font-weight: bold; text-align: left;
     padding: 4pt 5pt 2pt 5pt; border: 0.5pt solid #c9cdd4; font-size: 9pt; }
td { padding: 4pt 5pt 2pt 5pt; border: 0.5pt solid #c9cdd4; font-size: 9pt;
     vertical-align: top; }
div.admonition { border-left: 3pt solid #448aff; background-color: #eef4ff;
                 padding: 6pt 8pt 2pt 8pt; margin: 0 0 10pt 0; }
div.warning { border-left: 3pt solid #ff9100; background-color: #fff6e8; }
p.admonition-title { font-weight: bold; margin-bottom: 4pt; }
div.cover { text-align: center; }
p.cover-title { font-size: 34pt; font-weight: bold; color: #283593;
                margin-top: 170pt; margin-bottom: 14pt; }
p.cover-sub { font-size: 13pt; color: #444444; margin-bottom: 6pt; }
p.cover-meta { font-size: 10pt; color: #777777; margin-top: 60pt; }
p.toc-title { font-size: 21pt; font-weight: bold; color: #283593; margin-bottom: 12pt; }
p.section { font-size: 9pt; color: #777777; margin: 0 0 2pt 0; }
div#footer { font-size: 8pt; color: #777777; text-align: center; }
pdftoc { color: #1f2328; }
pdftoc.pdftoclevel0 { font-weight: bold; margin-top: 6pt; }
pdftoc.pdftoclevel1 { margin-left: 14pt; font-size: 9.5pt; font-weight: normal; }
pdftoc.pdftoclevel2 { margin-left: 28pt; font-size: 9pt; color: #555555; }
"""


def flatten_nav(nav: list, section: str = "") -> list[tuple[str, str, str]]:
    """Return ``(section, title, path)`` for every page, in nav order."""
    pages: list[tuple[str, str, str]] = []
    for item in nav:
        if isinstance(item, str):
            pages.append((section, "", item))
            continue
        for title, value in item.items():
            if isinstance(value, list):
                pages += flatten_nav(value, title)
            else:
                pages.append((section, title, value))
    return pages


def anchor_for(path: str, fragment: str = "") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", path.lower().removesuffix(".md")).strip("-")
    return f"{slug}--{fragment}" if fragment else f"page-{slug}"


def slugify(text: str) -> str:
    """Same slugs as the MkDocs ``toc`` extension produces for our headings."""
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).lower()
    text = re.sub(r"[^\w\s-]", "", text).strip()
    return re.sub(r"[\s-]+", "-", text)


def convert_page(path: str, docs_dir: Path, known: set[str]) -> str:
    text = (docs_dir / path).read_text(encoding="utf-8")
    body = markdown.markdown(text, extensions=MD_EXTENSIONS, output_format="html")

    # Give headings an anchor so cross-page "#fragment" links resolve.
    def heading(match: re.Match) -> str:
        level, inner = match.group(1), match.group(2)
        if level == "1":
            return match.group(0)
        name = anchor_for(path, slugify(inner))
        return f'<h{level}><a name="{name}"></a>{inner}</h{level}>'

    body = re.sub(r"<h([1-6])[^>]*>(.*?)</h\1>", heading, body, flags=re.S)

    # Links to other pages become links inside the PDF.
    def link(match: re.Match) -> str:
        href = html.unescape(match.group(1))
        if re.match(r"^[a-z][a-z0-9+.-]*:", href):
            return match.group(0)
        target, _, fragment = href.partition("#")
        if not target:
            return f'href="#{anchor_for(path, fragment)}"'
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
        if resolved not in known:
            raise SystemExit(f"error: {path}: link to unknown page {href!r}")
        return f'href="#{anchor_for(resolved, fragment)}"'

    body = re.sub(r'href="([^"]+)"', link, body)
    return f'<a name="{anchor_for(path)}"></a>\n{body}'


def find_fonts() -> dict[tuple[str, str, str], Path] | None:
    """Locate the fonts; missing bold/italic variants fall back to regular."""
    for directory in FONT_DIRS:
        found = {key: directory / name for key, name in FONT_FILES.items()}
        regular = {fam: found[(fam, "normal", "normal")] for fam in ("DocSans", "DocMono")}
        if not all(p.is_file() for p in regular.values()):
            continue
        for (fam, weight, style), path in list(found.items()):
            if not path.is_file():
                bold = found[(fam, "bold", "normal")]
                use_bold = weight == "bold" and bold.is_file()
                found[(fam, weight, style)] = bold if use_bold else regular[fam]
        return found
    return None


def register_fonts(fonts: dict[tuple[str, str, str], Path]) -> None:
    """Register the TrueType fonts with reportlab and announce them to xhtml2pdf.

    Done in code instead of ``@font-face`` because xhtml2pdf refuses to read
    font files that live outside the working directory.
    """
    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from xhtml2pdf.default import DEFAULT_FONT

    for (family, weight, style), path in fonts.items():
        bold, italic = int(weight == "bold"), int(style == "italic")
        name = f"{family}-{bold}{italic}"
        pdfmetrics.registerFont(TTFont(name, str(path)))
        addMapping(family, bold, italic, name)
        addMapping(family.lower(), bold, italic, name)
    for family in {key[0] for key in fonts}:
        DEFAULT_FONT[family.lower()] = family


def build_html(config: dict, docs_dir: Path, version: str) -> str:
    pages = flatten_nav(config["nav"])
    known = {p for _, _, p in pages}
    fonts = find_fonts()
    if fonts:
        register_fonts(fonts)
        families = {"sans": "DocSans", "mono": "DocMono"}
    else:
        print("note: DejaVu fonts not found, using PDF core fonts", file=sys.stderr)
        families = {"sans": "Helvetica", "mono": "Courier"}
    font_css = ""

    parts = [
        "<html><head><meta charset='utf-8'>",
        f"<title>{html.escape(config['site_name'])} documentation</title>",
        f"<style>{font_css}{CSS % families}</style></head><body>",
        '<div id="footer">'
        f"{html.escape(config['site_name'])} documentation &nbsp;-&nbsp; "
        "page <pdf:pagenumber/></div>",
        '<div class="cover">',
        f'<p class="cover-title">{html.escape(config["site_name"])}</p>',
        f'<p class="cover-sub">{html.escape(config.get("site_description", ""))}</p>',
        '<p class="cover-sub">Documentation</p>',
        f'<p class="cover-meta">Version {html.escape(version)}<br/>'
        f'{html.escape(config.get("repo_url", ""))}</p>',
        "</div><pdf:nextpage/>",
        '<p class="toc-title">Contents</p><pdf:toc/>',
    ]
    for section, _title, path in pages:
        parts.append("<pdf:nextpage/>")
        if section:
            parts.append(f'<p class="section">{html.escape(section)}</p>')
        parts.append(convert_page(path, docs_dir, known))
    parts.append("</body></html>")
    doc = "\n".join(parts)
    if not fonts:
        for char, repl in ASCII_FALLBACK.items():
            doc = doc.replace(char, repl)
        doc = doc.encode("latin-1", "replace").decode("latin-1")
    return doc


def project_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.M)
    return match.group(1) if match else "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-o", "--output", type=Path, default=ROOT / "docs.pdf")
    parser.add_argument("--config", type=Path, default=ROOT / "mkdocs.yml")
    parser.add_argument("--html-out", type=Path, help="also save the combined HTML")
    args = parser.parse_args(argv)

    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    docs_dir = args.config.parent / config.get("docs_dir", "docs")
    doc = build_html(config, docs_dir, project_version())
    if args.html_out:
        args.html_out.write_text(doc, encoding="utf-8")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "wb") as handle:
        status = pisa.CreatePDF(doc, dest=handle, encoding="utf-8")
    if status.err:
        print(f"error: PDF generation reported {status.err} error(s)", file=sys.stderr)
        return 1
    size_kb = args.output.stat().st_size / 1024
    print(f"wrote {args.output} ({size_kb:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
