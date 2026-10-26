# Data Files Handling

`pyrefactor` preserves non-Python assets alongside code — JSON configs, CSV datasets, templates, etc.

## Discovery

`discovery.discover_data_files(root, exclude, include, skip_dirs, copy_data)`:

- Walks source tree (respecting `--include`/`--exclude` precedence `default < include < exclude`).
- Identifies data via `DATA_EXTENSIONS` and `DATA_FILENAMES`:

```python
DATA_EXTENSIONS = {
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".txt",
    ".csv",
    ".tsv",
    ".xml",
    ".html",
    ".css",
    ".js",
    ".md",
    ".pickle",
    ".db",
    ...,
}
DATA_FILENAMES = {
    "requirements.txt",
    "Pipfile",
    "setup.cfg",
    "pyproject.toml",
    "LICENSE",
    ...,
}
```

- Returns `list[DataFile(path, relative)]` where `relative` is `path.relative_to(base)` (base = `detect_base(root)` → project root or `src/`).

If the project had no data files, returns `[]` (no-op).

## Copy Semantics

`core.consolidate(..., copy_data=True, data_glob=())`:

```python
for df in data_files:
    dest = output / df.relative  # single/flat/split: output/data.json
    # package mode: output/<pkg>/data.json
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(df.path, dest)  # preserves mtime
```

Filtered by `--data-glob` (repeatable) via `fnmatch`:

```bash
pyrefactor src/app -o output --data-glob "*.json" --data-glob "data/*.csv"
```

With `--no-copy-data`, discovery returns `[]` and nothing is copied.

## Example

```
source/
  pkg/
    __init__.py
    core.py
    data/
      config.json
    settings.ini

output/
  __init__.py
  _consolidated.py
  pkg/
    data/config.json      # preserved path
    settings.ini
```

From repo's own `src/refactor_utils/data/`:

```bash
pyrefactor src/refactor_utils -o /tmp/out --copy-data --validate
ls /tmp/out/refactor_utils/data/
# README.md  config.ini  example.csv  sample.json
```

## Examples & Tests

- `examples/data_files_example/` — runnable demo with `mypackage/data/config.json`.
- `examples/config_example/` — TOML filtering.
- `tests/test_data_files.py` — 6 cases (copy, no-copy, glob, structure, modes, exclude).
- `tests/test_discovery.py::test_discover_data_files*` — unit.

## When to Disable

- Binary assets already managed externally.
- Output is a single file (`--mode module`) where data not needed (use `--no-copy-data`).

## See Also

- `discovery.py` — `DATA_EXTENSIONS`, `discover_data_files()`
- `core.py` — copy loop + `copy_data`/`data_glob` params
- `src/refactor_utils/data/README.md` — included example data
