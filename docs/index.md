# refactor_utils

**Consolidate a small/medium Python module, library or project into one dependency-ordered file.**

`pyrefactor` parses every file with **tree-sitter** (parallel workers), orders blocks by import-time dependencies, resolves collisions, merges imports, and emits a single `consolidated/<pkgname>.py` file — with full support for data files and source maps. (The older multi-file layouts remain available through the library API.)

```bash
pip install -e .
# Consolidate the package in the current directory (default)
pyrefactor
# Or point at a package directory
pyrefactor path/to/package
```

Output:

```
path/to/package/
  consolidated/
    <pkgname>.py   # imports -> constants -> classes -> functions -> entry points + __all__
```

The package name (`<pkgname>`) is detected from `pyproject.toml`/`setup.cfg`/`setup.py` in the package folder and up to two directory levels above it (covering `src/<pkgname>` layouts); when nothing is found, the current folder's name is used. `autoall` appends an `__all__` section to the generated file.

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
| Data files | `--copy-data` / `--data-glob` | off |
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

## License

MIT — see [GitHub](https://github.com/i4147/refactor_utils).
