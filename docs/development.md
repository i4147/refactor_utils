# Development

## Setup

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev,docs]"
```

## Layout

```text
src/refactor_utils/   the package
tests/                pytest suite
mytest/               sample input (1.py, 2.py) and its consolidated result.py
docs/                 this documentation (MkDocs)
mkdocs.yml            MkDocs configuration and navigation
docs.pdf              the whole documentation as a single PDF
tools/                helper scripts
```

## Helper scripts

All scripts can be run from any directory. They use `.venv/bin/python` when it
exists and fall back to `python3`.

| Script | Purpose |
|--------|---------|
| `tools/run_checks.sh` | run the test suite, check the sample and build the docs in strict mode. `--lint` adds ruff, `--no-docs` skips the docs |
| `tools/check_syntax.py PATH...` | validate Python files or directories with the same `validate_syntax` used by `pyrefactor`; exit code 1 when any file is invalid |
| `tools/regen_sample.py` | re-create `mytest/result.py` from `mytest/1.py` and `mytest/2.py`. `--check` only verifies it is up to date |
| `tools/build_docs.sh` | build the HTML site into `site/` and regenerate `docs.pdf` |
| `tools/build_pdf.py` | build `docs.pdf` from the `nav` in `mkdocs.yml`. `-o PATH` changes the target |
| `tools/serve_docs.sh` | live preview with `mkdocs serve` (default `0.0.0.0:8000`) |

## Tests

```bash
tools/run_checks.sh
# or just
.venv/bin/python -m pytest tests -q
```

`tests/test_consolidate.py` holds the end-to-end suite.
`tests/test_naming_and_validation.py` covers identifier validity of generated
names, the syntax validator and docstring pruning.

## Documentation

```bash
tools/serve_docs.sh       # preview
tools/build_docs.sh       # site/ and docs.pdf
```

Add a page by creating the Markdown file under `docs/` and listing it in the
`nav` of `mkdocs.yml`. The PDF follows the same `nav`, so it needs no extra
configuration. Stick to plain Markdown, tables, fenced code and admonitions;
those render in both outputs.

`site/` is build output and is ignored by git. `docs.pdf` is committed.

## Adding a rename rule

Every name the tool invents must pass through
`refactor_utils.resolver.make_identifier`. Add a case to
`test_make_identifier` and an end-to-end case next to
`test_numeric_module_names_produce_valid_renames`.
