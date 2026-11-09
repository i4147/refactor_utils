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
pyrefactor src/pkg                          # auto-detects the nearest pyproject.toml
pyrefactor src/pkg --config other.toml      # explicit file
pyrefactor src/pkg --main-name run          # CLI flag wins over config
```

`cli.apply_config()`:

1. If `--config PATH` is given, load that file's `[tool.pyrefactor]`
   (`cli._read_tool_config()`).
2. Otherwise search upwards from the source path with
   `discovery.detect_pyproject_config()`.
3. Merge: a flag whose value differs from the argparse default is treated as
   explicit and wins; config values fill in everything else.

Supported keys: `exclude`, `include`, `entry`, `entry_keep_all`, `main_name`,
`generate_guard`, `rename_strategy`, `dedupe`, `imports`, `all`, `format`,
`fail_on_warning`, `max_skipped`, `copy_data`, `data_glob`, `namespace`,
`preserve_modules`, `source_map`, `keep_docstrings`, `strip_comments`,
`keep_comments`, `eol`, `strict`, `workers`.

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
- `cli.apply_config()` — merging logic (CLI wins over config).
