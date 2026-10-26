# CLI Reference

Generated from `pyrefactor --help`. Every flag lists the module it touches.

```
usage: pyrefactor [-h] [-o OUTPUT] [-m {single,package,module,flat,split}]
                  [-j WORKERS] [--jobs JOBS] [--executor {process,thread}]
                  [--no-parallel] [--progress] [--progress-bar] [-x GLOB]
                  [--include GLOB] [--copy-data] [--no-copy-data]
                  [--data-glob GLOB] [--entry MODULE] [--entry-keep-all]
                  [--main-name NAME] [--generate-guard] [--keep-all-mains]
                  [--keep-unused-imports] [--imports {prune,keep,strict}]
                  [--respect-all] [--all {ignore,respect,merge,recompute}]
                  [--rename-strategy {prefix,suffix,module_path,error}]
                  [--dedupe {none,safe,aggressive}] [--keep-docstrings]
                  [--strip-docstrings] [--keep-comments] [--strip-comments]
                  [--namespace] [--preserve-modules] [--source-map]
                  [--dry-run] [--check] [--stdout] [--diff] [--overwrite]
                  [--force] [--config PATH] [--pyproject] [--setup-cfg]
                  [--format] [--post-format] [--formatter {black,ruff}]
                  [--validate] [--validate-import] [--strict]
                  [--fail-on-warning] [--max-skipped N]
                  [--allow-syntax-errors]
                  [--log-level {TRACE,DEBUG,INFO,WARNING,ERROR}]
                  [--log-file LOG_FILE] [--version]
                  source
```

## Positional

- `source` — package directory, project root or single `.py` file.

## Output & Mode

- `-o OUTPUT, --output OUTPUT` — output directory (default `./output`).
- `-m {single,package,module,flat,split}, --mode ...` — layout; `single` is `output/__init__.py`+`_consolidated.py`; others in `core.py`/`writer.py`.

## Parallelism

- `-j WORKERS, --workers WORKERS` — workers (default CPU).
- `--jobs JOBS` — alias for `--workers` (`cli.py`).
- `--executor {process,thread}` — executor type.
- `--no-parallel` — disable parallel (`workers=1`).
- `--progress` / `--progress-bar` — tqdm bar (falls back to log).

## Discovery

- `-x GLOB, --exclude GLOB` — skip glob (repeatable) — `discovery.py`.
- `--include GLOB` — re-include; precedence `default < --include < --exclude`.
- `--copy-data` (default) / `--no-copy-data` — copy data files preserving relative paths.
- `--data-glob GLOB` — only copy data matching glob.

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
- `--overwrite` / `--force` — allow overwrite non-empty.

## Config / Format / Validate

- `--config PATH` — `pyproject.toml` with `[tool.pyrefactor]` — `cli.py`/`core.py`/`discovery.py`.
- `--pyproject` / `--setup-cfg` — auto-detect near source.
- `--format` / `--post-format` — `black`/`ruff` — `writer.py`.
- `--formatter {black,ruff}`.
- `--validate` — compile; `--validate-import` also smoke-import — `core.py`.

## Strictness

- `--strict` — exit 1 if any skipped.
- `--fail-on-warning` — exit 2 if warnings.
- `--max-skipped N` — fail if `> N` skipped.
- `--allow-syntax-errors` — partial parse — `parser.py`.

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
