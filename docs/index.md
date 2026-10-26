# refactor_utils

**Consolidate a small/medium Python module, library or project into one dependency-ordered file.**

`pyrefactor` parses every file with **tree-sitter** (parallel workers), orders blocks by import-time dependencies, resolves collisions, merges imports, and emits `output/__init__.py` + `output/_consolidated.py` — with full support for data files, source maps, and multiple output layouts.

```bash
pip install -e .
pyrefactor path/to/package -o output
```

Output (`--mode single`, default):

```
output/
  __init__.py        # re-exports public constants, classes, functions + __all__
  _consolidated.py   # imports -> constants -> classes -> functions -> entry points
```

## What it does

- **Parses** every file with tree-sitter in parallel; skips tests/`conftest.py`/`setup.py`/`__main__.py` (overridable via `--include`).
- **Orders** blocks by import-time deps (base classes, decorators, defaults, class bodies), preferring constants → classes → functions; cycles warned.
- **Resolves** same-named objects: shallowest keeps name, others become `<module>_<name>`; identical classes/functions optionally deduped (`--dedupe`).
- **Optimizes** imports: internal removed, external merged/de-duplicated/conflict-aliased, unused pruned (`--imports prune|keep|strict`), grouped future/stdlib/third-party.
- **Handles** `main()` and `if __name__ == "__main__":` guards (`--entry`, `--main-name`, `--generate-guard`).
- **Preserves** data files (`.json`, `.csv`, `.ini`, etc.) via `--copy-data`.
- **Logs** with loguru (`--log-level`, `--log-file`).

## Quick Links

- [Installation](installation.md)
- [Quickstart](quickstart.md)
- [CLI Reference](cli-reference.md)
- [Project Files](project-files.md) — every source file documented
- [Architecture](architecture.md)
- [Examples](examples.md)
- [API Reference](api/index.md)

## Features at a Glance

| Capability | Flag | Default |
|------------|------|---------|
| Dry-run / check | `--dry-run` / `--check` | off |
| Stdout | `--stdout` | off |
| Config | `--config pyproject.toml` / `--pyproject` | auto |
| Formatting | `--format` / `--formatter black|ruff` | off |
| Diff | `--diff` | off |
| Overwrite | `--overwrite` / `--force` | warn |
| Include vs exclude | `--include GLOB` / `--exclude GLOB` | precedence: default < include < exclude |
| Data files | `--copy-data` / `--data-glob` | on |
| Docstrings/comments | `--strip-docstrings` / `--strip-comments` | keep |
| Collisions | `--rename-strategy prefix|sufix|module_path|error` | prefix |
| Dedupe | `--dedupe none|safe|aggressive` | safe |
| Imports | `--imports prune|keep|strict` | prune |
| `__all__` | `--all ignore|respect|merge|recompute` | ignore |
| Entry | `--entry` / `--main-name` / `--generate-guard` | — |
| Namespaces | `--namespace` / `--preserve-modules` / `--source-map` | off |
| Strictness | `--fail-on-warning` / `--max-skipped` / `--allow-syntax-errors` | off |
| Parallelism | `--workers` / `--jobs` / `--no-parallel` / `--progress` | cpu count |
| Validation | `--validate` / `--validate-import` | off |
| Modes | `--mode single|package|module|flat|split` | single |

## License

MIT — see [GitHub](https://github.com/i4147/refactor_utils).
