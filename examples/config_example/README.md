# Config Example

Persist settings in `pyproject.toml`:

```toml
[tool.pyrefactor]
exclude = ["tests/*"]
rename_strategy = "suffix"
imports = "keep"
all = "merge"
```

Run with:

```bash
pyrefactor ./src -o output --config pyproject.toml
# or auto-detect:
pyrefactor ./src -o output --pyproject
```
