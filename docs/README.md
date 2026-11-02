# Building the documentation

This directory holds the MkDocs site for `refactor_utils`. Two artifacts can be
produced:

1. **HTML** — the full docs site (`site/` directory), built with
   [MkDocs](https://www.mkdocs.org/) + [Material for MkDocs](https://squidfunk.github.io/mkdocs-material/)
   and [mkdocstrings](https://mkdocstrings.github.io/) API pages.
2. **PDF** — a single-file PDF rendering of all documentation pages
   (`refactor_utils.pdf` at the repository root).

## Prerequisites

Install the docs extras from the repository root:

```bash
pip install -e .[docs]
```

which pulls in `mkdocs`, `mkdocs-material` and `mkdocstrings[python]`. The PDF
build additionally needs `weasyprint` (HTML/CSS → PDF):

```bash
pip install weasyprint
```

## 1. HTML docs

From the repository root:

```bash
mkdocs build           # strict build; fails on warnings/broken links
```

The static site is written to `site/`. To preview locally:

```bash
mkdocs serve           # http://127.0.0.1:8000
```

Helper scripts (also in `arena/`):

- `tools/arena/build_docs.sh` — `mkdocs build --strict`
- `tools/arena/serve_docs.sh` — `mkdocs serve`

## 2. Single-file PDF

From the repository root:

```bash
python tools/arena/build_pdf.py
```

This concatenates every page in `mkdocs.yml`'s `nav` order, converts the
combined Markdown to HTML (Material theme stylesheet), and renders it to a
single `refactor_utils.pdf` next to the repository root with WeasyPrint.

If WeasyPrint or its system libraries are unavailable, fall back to printing
the `site/` pages to PDF from a browser, or install `fdf2`/`reportlab` and
render the combined Markdown manually.

## What gets documented

- Every public module/class/function in `src/refactor_utils/` carries a
  module/class/function docstring (Google style), picked up by mkdocstrings
  under `docs/api/*.md`.
- Project files are described in `docs/project-files.md`.
- The new consolidated layout (`consolidated/<pkgname>.py`) and the `autoall`
  `__all__` generation are covered in `docs/index.md`, `docs/quickstart.md`
  and `docs/project-files.md`.

## Verifying the build

```bash
mkdocs build --strict
ls site/index.html
ls refactor_utils.pdf
```
