# Examples

Runnable examples in `examples/` (see `examples/README.md`).

## Basic Usage (`examples/basic_usage.py`)

CLI:

```bash
pyrefactor examples/data_files_example/mypackage -o /tmp/basic_out --overwrite --validate
```

Python API:

```python
from pathlib import Path
from refactor_utils import consolidate

result = consolidate(Path("mypackage"), Path("output"))
print(result.modules, result.exports)
```

Run:

```bash
python examples/basic_usage.py
```

## Advanced Flags (`examples/advanced_flags.py`)

```python
consolidate(pkg, out, rename_strategy="suffix")
consolidate(pkg, out, dedupe="aggressive")
consolidate(pkg, out, all_mode="merge")
consolidate(pkg, out, source_map=True, preserve_modules=True, namespace=True)
```

Run:

```bash
python examples/advanced_flags.py
```

## Programmatic API (`examples/programmatic_api.py`)

- `dry_run=True` — plan without writes.
- `validate=True` + `validate_import=True`.
- `diff=True` vs existing.
- `data_glob=("*.json",)`.

```bash
python examples/programmatic_api.py
```

## Data Files Example (`examples/data_files_example/`)

```
mypackage/
  __init__.py
  core.py      # uses Path(__file__).parent / "data" / "config.json"
  utils.py
  data/
    config.json
    sample.csv
```

```bash
pyrefactor examples/data_files_example/mypackage -o /tmp/out --copy-data
ls /tmp/out/mypackage/data/config.json
pyrefactor examples/data_files_example/mypackage -o /tmp/out --data-glob "*.json"
```

## Config Example (`examples/config_example/`)

`pyproject.toml`:

```toml
[tool.pyrefactor]
exclude = ["tests/*", "ignored.py"]
rename_strategy = "suffix"
```

```bash
pyrefactor ./src -o output --config examples/config_example/pyproject.toml
pyrefactor ./src -o output --pyproject  # auto-detect
```

## Integration Tests as Examples

`tests/test_integration.py` shows:

- `src/` layout, namespace packages, circular imports, large projects (20 files), output-inside-source, programmatic API.

Run:

```bash
pytest tests/test_integration.py -v
```
