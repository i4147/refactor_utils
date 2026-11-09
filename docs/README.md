# Building the documentation

This directory holds the MkDocs site for `refactor_utils`. Two artifacts are
produced:

1. **HTML** — the full site in `site/` (git-ignored), built with
   [MkDocs](https://www.mkdocs.org/) + [Material for MkDocs](https://squidfunk.github.io/mkdocs-material/)
   and [mkdocstrings](https://mkdocstrings.github.io/) for the API pages.
2. **PDF** — `refactor_utils.pdf` at the repository root: **one** file
   containing every page, built by `arena/build_pdf.py`.

Everything below is run from the repository root.

---

## 1. Prerequisites

```bash
# project + runtime deps (editable install)
pip install -e .
pip install -e autoall            # sibling package used for __all__

# test + docs extras
pip install -e ".[test,docs]"
pip install fpdf2                 # PDF renderer (pure Python)
```

`arena/setup.sh` does all of the above (including creating `.venv`).

Requirements: Python ≥ 3.10. The PDF build needs the DejaVu fonts
(`/usr/share/fonts/truetype/dejavu/`, present on Debian/Ubuntu images); no
browser, LaTeX, pango or cairo is required.

---

## 2. Build the HTML site

```bash
mkdocs build --strict
```

- `--strict` turns every warning into an error. The build is expected to be
  clean; `docs/README.md` (this file) is excluded via `exclude_docs` in
  `mkdocs.yml` because it would otherwise collide with `docs/index.md`.
- Output: `site/index.html`.

Preview locally:

```bash
mkdocs serve          # http://127.0.0.1:8000
```

Helper: `./arena/build_docs.sh` (runs `mkdocs build --strict` and prints the
resulting `site/index.html` size).

### What the site contains

| Path | Content |
|------|---------|
| `docs/index.md` … `docs/examples.md` | user guide (quickstart, CLI, config, data files, examples) |
| `docs/architecture.md`, `docs/project-files.md`, `docs/edge-cases.md` | internals and per-file documentation |
| `docs/api/*.md` | API reference via `::: module` mkdocstrings directives |
| `docs/contributing.md`, `docs/changelog.md` | development notes and history |

The API pages are generated from the Google-style docstrings in
`src/refactor_utils/`. Audit coverage with:

```bash
python arena/docstring_audit.py            # sources + autoall
python arena/docstring_audit.py --tests    # also tests/
```

---

## 3. Build the single-file PDF

```bash
python arena/build_pdf.py
# Wrote /home/user/refactor_utils/refactor_utils.pdf (… bytes)
```

What the builder does:

1. reads the page order from `nav` in `mkdocs.yml` (`_collect_nav_pages`);
2. expands every `::: module` mkdocstrings directive with `inspect`, so the PDF
   contains real signatures and docstrings (`expand_directives`);
3. converts each page to HTML with python-markdown and sanitises it for fpdf2
   (table cells flattened, links reduced to their text);
4. renders a cover page, a table of contents with page numbers, and then every
   page with DejaVu fonts (Unicode-safe) using `fpdf2`;
5. writes `refactor_utils.pdf` (custom destination: `--output PATH`).

The build is deterministic apart from the "Generated <date>" line on the cover.
A full run takes a couple of seconds.

Verify the output:

```bash
python - <<'EOF'
from pypdf import PdfReader
reader = PdfReader("refactor_utils.pdf")
print(len(reader.pages), "pages")
print(reader.pages[1].extract_text()[:200])   # table of contents
EOF
```

Helper: `./arena/check_all.sh` runs tests + docstring audit + HTML + PDF +
demos in one go.

---

## 4. Troubleshooting

| Symptom | Fix |
|---------|-----|
| `WARNING - Excluding 'README.md' … conflicts with 'index.md'` | keep `exclude_docs: README.md` in `mkdocs.yml` |
| `ModuleNotFoundError: refactor_utils` during `mkdocs build` | run `pip install -e .` first |
| `fpdf.errors.FPDFException: Cannot set style for HTML tag <td>` | fpdf2 rejects styles for table cells; keep them out of `_tag_styles()` in `arena/build_pdf.py` |
| `No such file: DejaVuSans.ttf` | install `fonts-dejavu` or edit `FONT_DIR` in `arena/build_pdf.py` |
| PDF page numbers in the ToC look stale | rebuild: the ToC is filled by a second layout pass |

---

## 5. One-shot build

```bash
./arena/check_all.sh
```

This performs, in order: pytest, docstring audit, `mkdocs build --strict`,
`arena/build_pdf.py`, and the consolidation demos.
