# Quickstart

## Basic Consolidation

```bash
pyrefactor src/mypackage -o output
tree output
# output/
#   __init__.py
#   _consolidated.py
```

## Dry-Run & Check (CI)

```bash
pyrefactor src/mypackage -o output --dry-run --log-level INFO
pyrefactor src/mypackage -o output --check --diff   # fails if drift
```

## Stdout & Diff

```bash
pyrefactor src/mypackage -o output --stdout > consolidated.py
pyrefactor src/mypackage -o output --diff
```

## Data Files

```bash
pyrefactor src/mypackage -o output --copy-data
pyrefactor src/mypackage -o output --data-glob "*.json"
pyrefactor src/mypackage -o output --no-copy-data
```

Data files (`.json`, `.yaml`, `.csv`, `.txt`, `.ini`, `.toml`, etc.) are discovered via `discovery.discover_data_files()` and copied preserving relative paths.

## Formatting & Validation

```bash
pyrefactor src/mypackage -o output --format --formatter black --validate
pyrefactor src/mypackage -o output --validate-import  # smoke-import
```

## Include / Exclude

Precedence: `default excludes < --include < --exclude`

```bash
pyrefactor src/mypackage -o output --exclude "tests/*" --include "conftest.py"
```

## Entry Points

```bash
pyrefactor src/mypackage -o output --entry mypackage.cli
pyrefactor src/mypackage -o output --entry-keep-all
pyrefactor src/mypackage -o output --main-name cli_main --generate-guard
```

## Namespaces & Source Maps (Debugging)

```bash
pyrefactor src/mypackage -o output --source-map --preserve-modules --namespace
```

Adds per-block:

```python
# Original module: pkg.sub.mod
# source: src/pkg/sub/mod.py:10-25
# namespace: pkg.sub.mod
def foo(): ...
```

## Different Modes

```bash
pyrefactor src/mypackage -o output --mode package  # output/mypackage/...
pyrefactor src/mypackage -o output --mode flat     # no section headers
pyrefactor src/mypackage -o output --mode split    # const.py, class.py, func.py, main.py
pyrefactor src/mypackage -o output --mode module   # single file
```

## Config File

`pyproject.toml`:

```toml
[tool.pyrefactor]
exclude = ["tests/*", "ignored.py"]
rename_strategy = "suffix"
dedupe = "safe"
imports = "prune"
all = "respect"
```

```bash
pyrefactor src/mypackage -o output --config pyproject.toml
pyrefactor src/mypackage -o output --pyproject  # auto-detect
```

## Python API

```python
from pathlib import Path
from refactor_utils import consolidate

result = consolidate(
    Path("src/mypackage"), Path("output"), mode="single", validate=True
)
print(result.modules, result.exports, result.copied_data_files)
```
