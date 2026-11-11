# Configuration

`pyrefactor` reads a `[tool.pyrefactor]` table from `pyproject.toml`. The file
is looked up next to the source, or given explicitly with `--config PATH`.

```toml
[tool.pyrefactor]
exclude   = ["vendor/*", "scripts/*"]
include   = []
entry     = ["cli"]
imports   = "prune"        # prune | keep | strict
format    = false
formatter = "black"        # black | ruff
copy_data = true
data_glob = ["*.json"]
```

## Keys

| Key | Type | CLI equivalent |
|-----|------|----------------|
| `exclude` | list of globs | `-x / --exclude` |
| `include` | list of globs | `--include` |
| `entry` | list of module names | `--entry` |
| `imports` | string | `--imports` |
| `format` | bool | `--format` |
| `formatter` | string | `--formatter` |
| `copy_data` | bool | `--copy-data / --no-copy-data` |
| `data_glob` | list of globs | `--data-glob` |

A single string is accepted wherever a list is expected.

## Precedence

- `exclude`, `include`, `entry`: the command line wins when it passes at least
  one value; otherwise the config value is used.
- `imports`, `formatter`, `copy_data`, `data_glob`: when the key is present in
  the config file, **the config value is used**, also if a flag was passed.
- `format = true` in the config enables formatting; it cannot be switched off
  from the command line.

!!! note
    To override a config value for a single run, point `--config` at a
    different `pyproject.toml`.
