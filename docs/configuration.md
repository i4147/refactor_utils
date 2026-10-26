# Configuration

Persist settings in `pyproject.toml` under `[tool.pyrefactor]`.

## Example `pyproject.toml`

```toml
[tool.pyrefactor]
exclude = ["tests/*", "docs/*", "ignored.py"]
include = ["tests/fixtures/*.py"]
entry = ["mypackage.cli"]
rename_strategy = "suffix"      # prefix|sufix|module_path|error
dedupe = "safe"                 # none|safe|aggressive
imports = "prune"               # prune|keep|strict
all = "respect"                 # ignore|respect|merge|recompute
format = false
fail_on_warning = false
max_skipped = 5
allow_syntax_errors = false
```

## Loading

```bash
pyrefactor src/pkg -o output --config pyproject.toml
pyrefactor src/pkg -o output --pyproject   # auto-detect near source
```

`core._load_config_file()`:

1. If `--config PATH` given, load that file's `[tool.pyrefactor]`.
2. Else if `--pyproject`/`--setup-cfg` or auto-detection enabled, search upwards for `pyproject.toml` via `discovery.detect_pyproject_config()`.
3. Merge: CLI wins if non-default, else config value fills.

Supported keys: `exclude`, `include`, `entry`, `rename_strategy`, `dedupe`, `imports`, `all`, `format`, `fail_on_warning`, `max_skipped`, `allow_syntax_errors`.

## Example for `refactor_utils` Itself

See repo root `pyproject.toml`:

```toml
[tool.pyrefactor]
exclude = ["tests/*", "test_*.py", "iwgfyrkugf.py"]
rename_strategy = "prefix"
dedupe = "safe"
imports = "prune"
all = "ignore"
max_skipped = 10
```

## See Also

- `discovery.detect_pyproject_config()` — loads TOML (tomllib/tomli).
- `core.consolidate(..., config=Path)` — merging logic.
