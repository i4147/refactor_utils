# refactor_utils

Consolidate a small/medium Python module, library or project into **one
dependency-ordered file**.

```bash
pip install -e .
pip install -e autoall        # sibling package that writes __all__

cd path/to/mypackage
pyrefactor                    # or: pyrefactor path/to/mypackage
```

```
path/to/mypackage/
  consolidated/
    mypackage.py              # the single generated module
```

Full documentation: [`docs/`](docs/index.md) — build the site with
`mkdocs build --strict`, the single-file PDF with `python arena/build_pdf.py`
(see [`docs/README.md`](docs/README.md)).

---

## What it does

`pyrefactor` parses every file with **tree-sitter** (in parallel worker
processes), orders the blocks by import-time dependencies, resolves name
collisions, merges imports, and emits a single module:

```
docstring
__future__ + optimized imports      (internal/relative imports stripped, deduped)
constants
classes
functions
entry points / __main__ guard
__all__                             (written in place by autoall)
```

Details:

- **Ordering** — base classes, decorators, default values, class bodies and
  module-level calls are dependency-ordered; cycles are reported.
- **Collisions** — the shallowest module keeps the name, the others become
  `<module>_<name>`; references (including `from .x import y as z` and
  `mod.attr`) are rewritten. `--rename-strategy` and `--dedupe` control it.
- **Imports** — merged, de-duplicated, unused ones pruned (`--imports
  prune|keep|strict`), grouped future/stdlib/third-party.
- **Entry points** — `main()`/`if __name__ == "__main__":` handled by
  `--entry`, `--entry-keep-all`, `--main-name`, `--generate-guard`.
- **Data files** — `--copy-data` copies assets package-relative so
  `Path(__file__).parent / "data/…"` keeps working.
- **`__all__`** — [`autoall`](autoall/README.md) appends the marker-delimited
  block to the generated file (idempotent, CRLF-safe, respects a user
  `__all__`).
- **Safety** — `--dry-run`, `--check` (CI gate), `--diff`, `--validate`,
  `--validate-import`, `--strict`, `--fail-on-warning`, `--max-skipped`,
  `--allow-syntax-errors`, `--eol`.

## Package name detection

The name used for `consolidated/<pkgname>.py` comes from packaging metadata
(`pyproject.toml` → `setup.cfg` → `setup.py`) in the package folder **and up
to two directory levels above it**, which covers `src/<pkgname>` layouts. When
nothing is found, the folder that was pointed at (`.` by default) gives the
name. Names are normalised to importable identifiers: `my-dist` → `my_dist`.

## Quick start

```bash
# default: current directory
cd src/mypackage && pyrefactor

# explicit path, custom output directory
pyrefactor src/mypackage
pyrefactor src/mypackage -o dist          # -> dist/mypackage.py

# CI: fail when the generated file is out of date
pyrefactor src/mypackage --check --diff

# inspect without writing
pyrefactor src/mypackage --dry-run
pyrefactor src/mypackage --stdout > /tmp/mypackage.py
```

## Configuration

`[tool.pyrefactor]` in `pyproject.toml` is read automatically (or from
`--config PATH`); explicit command line flags win.

```toml
[tool.pyrefactor]
exclude = ["tests/*", "docs/*"]
include = []
entry = ["mypackage.cli"]
main_name = "run"
rename_strategy = "prefix"      # prefix|suffix|module_path|error
dedupe = "safe"                 # none|safe|aggressive
imports = "prune"               # prune|keep|strict
all = "ignore"                  # ignore|respect|merge|recompute
format = true
fail_on_warning = false
max_skipped = 10
```

See [docs/configuration.md](docs/configuration.md) for every supported key.

## CLI reference

```
usage: pyrefactor [-h] [-o OUTPUT] [-j WORKERS] [--jobs JOBS]
                  [--executor {process,thread}] [--no-parallel] [--progress]
                  [--progress-bar] [-x GLOB] [--include GLOB] [--copy-data]
                  [--no-copy-data] [--data-glob GLOB] [--entry MODULE]
                  [--entry-keep-all] [--main-name NAME] [--generate-guard]
                  [--keep-all-mains] [--keep-unused-imports]
                  [--imports {prune,keep,strict}] [--respect-all]
                  [--all {ignore,respect,merge,recompute}]
                  [--rename-strategy {prefix,suffix,module_path,error}]
                  [--dedupe {none,safe,aggressive}] [--keep-docstrings]
                  [--strip-docstrings] [--keep-comments] [--strip-comments]
                  [--namespace] [--preserve-modules] [--source-map]
                  [--dry-run] [--check] [--stdout] [--diff] [--overwrite]
                  [--force] [--config PATH] [--pyproject] [--setup-cfg]
                  [--format] [--post-format] [--formatter {black,ruff}]
                  [--validate] [--validate-import] [--strict]
                  [--fail-on-warning] [--max-skipped N]
                  [--allow-syntax-errors] [--eol {lf,crlf,preserve}]
                  [--log-level {TRACE,DEBUG,INFO,WARNING,ERROR}]
                  [--log-file LOG_FILE] [--version]
                  [source]
```

Every flag's help names the module that implements it. Exit codes: `0`
success, `1` unexpected failure or strictness violation, `2` bad input or a
failing `--check`.

## Python API

```python
from pathlib import Path
from refactor_utils.core import ConsolidationError, consolidate_package

result = consolidate_package(Path("src/mypackage"), validate=True)
print(result.pkg_name, result.modules, result.exports, result.files)
```

`refactor_utils.core.consolidate()` remains available for library users who
need the legacy multi-file layouts (`mode="single"|"package"|"module"|"flat"|"split"`);
the CLI always produces the single-file `consolidated/<pkgname>.py` layout.

## Project layout

| Path | Content |
|------|---------|
| `src/refactor_utils/` | the package: `cli.py`, `core.py`, `discovery.py`, `parser.py`, `resolver.py`, `writer.py`, `ordering.py`, `imports.py`, `models.py`, `logging_setup.py` |
| `autoall/` | sibling package that generates `__all__` (`ensure_all`, `ensure_all_file`) |
| `tests/` | pytest suite (incl. `test_consolidated.py`, `test_cli_workflow.py`, `test_autoall.py`) |
| `docs/` | MkDocs site + `docs/README.md` (how to build HTML and the PDF) |
| `examples/` | runnable demos |
| `arena/` | helper scripts used to build/test/document this session |

Every module, class and function carries a Google-style docstring
(`python arena/docstring_audit.py` verifies it); the API reference is rendered
by mkdocstrings from those docstrings.

## Development

```bash
./arena/setup.sh          # venv + editable installs + test/docs deps
./arena/run_tests.sh      # pytest
./arena/check_all.sh      # tests + docstring audit + docs + PDF + demos
```

`docs/project-files.md` documents every file, and `docs/architecture.md`
describes the discover → parse → resolve → write pipeline.

## License

MIT.
