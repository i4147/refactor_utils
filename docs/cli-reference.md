# CLI Reference

Generated from `pyrefactor --help`. Every flag lists the module it touches.

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

## Positional

- `source` — package directory (or single `.py` file) to consolidate;
  defaults to the current directory.

## Output

- `-o OUTPUT, --output OUTPUT` — output directory (default:
  `<pkg>/consolidated`). A single `<pkgname>.py` file is written inside it.

The CLI no longer accepts `--mode`; it always produces the consolidated
single-file layout (`consolidated/<pkgname>.py`) described in Quickstart.
The legacy multi-file layouts (`mode="package"|"module"|"flat"|"split"`) remain
available through the library API (`refactor_utils.core.consolidate`).

`[tool.pyrefactor]` in `pyproject.toml` is read automatically (or from
`--config PATH`); values only fill in flags that were not passed explicitly.

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | success (or `--check` found no drift) |
| `1` | unexpected failure, or strictness rule triggered (`--strict`, `--fail-on-warning`, `--max-skipped`) |
| `2` | bad input / build problem (`ConsolidationError`, including a failing `--check`) |

## Parallelism

- `-j WORKERS, --workers WORKERS` — workers (default CPU).
- `--jobs JOBS` — alias for `--workers` (`cli.py`).
- `--executor {process,thread}` — executor type.
- `--no-parallel` — disable parallel (`workers=1`).
- `--progress` / `--progress-bar` — tqdm bar (falls back to log).

## Discovery

- `-x GLOB, --exclude GLOB` — skip glob (repeatable) — `discovery.py`.
- `--include GLOB` — re-include; precedence `default < --include < --exclude`.
- `--copy-data` / `--no-copy-data` — copy data files preserving paths relative to the package (opt-in; off by default for the `consolidated/` layout).
- `--data-glob GLOB` — only copy data matching glob (repeatable, matches file name or relative path).

## Entry

- `--entry MODULE` — keep only guards of listed modules (repeatable) — `resolver.py`.
- `--entry-keep-all` / `--keep-all-mains` — keep all guards.
- `--main-name NAME` — rename `main()` → `NAME`.
- `--generate-guard` — auto `if __name__ == "__main__": main()`.

## Imports / Exports

- `--keep-unused-imports` — deprecated alias for `--imports keep`.
- `--imports {prune,keep,strict}` — `prune` drop unused (default), `keep` retain, `strict` warn.
- `--respect-all` — deprecated alias for `--all respect`.
- `--all {ignore,respect,merge,recompute}` — `__all__` policy.

## Collisions

- `--rename-strategy {prefix,suffix,module_path,error}` — `prefix` → `<mod>_name` (default).
- `--dedupe {none,safe,aggressive}` — duplicate removal.

## Comments / Namespaces

- `--keep-docstrings` (default) / `--strip-docstrings` — via AST — `parser.py`.
- `--keep-comments` (default) / `--strip-comments` — keep shebang/encoding even when stripping.
- `--namespace` — `# namespace: module` per block — `writer.py`.
- `--preserve-modules` — `# Original module: ...`.
- `--source-map` — `# source: path:line-line`.

## Output Control

- `--dry-run` — plan only, never write — `core.py`.
- `--check` — like dry-run but fail if diff non-empty.
- `--stdout` — to stdout not files.
- `--diff` — unified diff vs existing.
- `--check` — fail when the output would change **or does not exist yet**
  (like `black --check` / `isort --check`).
- `--overwrite` / `--force` — allow overwrite non-empty.
- `--eol {lf,crlf,preserve}` — line endings for generated files; `preserve`
  matches the dominant ending of the input files — `core.py`.

## Config / Format / Validate

- `--config PATH` — `pyproject.toml` with `[tool.pyrefactor]` — `cli.py`/`core.py`/`discovery.py`.
- `--pyproject` / `--setup-cfg` — auto-detect near source.
- `--format` / `--post-format` — `black`/`ruff` — `writer.py`.
- `--formatter {black,ruff}`.
- `--validate` — compile; `--validate-import` also imports the generated module.
  The import **executes top-level code** and logs a prominent warning first —
  `core.py`.

## Strictness

- `--strict` — exit 1 if any file was skipped, and refuse to write output when
  a warning means the result may be wrong (unresolved name lookups, computed
  `__all__`, unresolvable internal imports) or when an import cycle has to be
  broken.
- `--fail-on-warning` — exit 2 if warnings.
- `--max-skipped N` — fail if `> N` skipped.
- `--allow-syntax-errors` — partial parse — `parser.py`.
- `--eol` — `lf` (default), `crlf`, or `preserve` — `core.py`.

## Logging

- `--log-level {TRACE,DEBUG,INFO,WARNING,ERROR}` (default `INFO`).
- `--log-file LOG_FILE`.

## Examples

```bash
pyrefactor src/pkg -o out --dry-run --log-level INFO
pyrefactor src/pkg -o out --include "conftest.py" --exclude "tests/*"
pyrefactor src/pkg -o out --rename-strategy suffix --dedupe aggressive --all merge
pyrefactor src/pkg -o out --source-map --preserve-modules --validate
pyrefactor src/pkg -o out --mode split --copy-data --format
```
