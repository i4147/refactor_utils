# Command line

```text
usage: pyrefactor [-h] [-o OUTPUT] [--name NAME] [-j WORKERS]
                  [--executor {process,thread}] [--progress] [-x GLOB]
                  [--include GLOB] [--copy-data | --no-copy-data]
                  [--data-glob GLOB] [--entry MODULE] [--entry-keep-all]
                  [--main-name NAME] [--generate-guard]
                  [--imports {prune,keep,strict}]
                  [--keep-docstrings | --no-keep-docstrings]
                  [--keep-comments | --no-keep-comments] [--dry-run] [--check]
                  [--stdout] [--diff] [--force] [--config PATH] [--format]
                  [--formatter {black,ruff}]
                  [--log-level {TRACE,DEBUG,INFO,WARNING,ERROR}]
                  [--log-file LOG_FILE] [--version]
                  source
```

## Source and output

| Flag | Default | Effect |
|------|---------|--------|
| `source` | required | package directory, project root or a single `.py` file |
| `-o, --output DIR` | `output` | output directory |
| `--name NAME` | current directory name | name of the generated file (`.py` is appended when missing) |

## Discovery

| Flag | Default | Effect |
|------|---------|--------|
| `-x, --exclude GLOB` | none | extra glob to skip, relative to the source; repeatable |
| `--include GLOB` | none | re-include something excluded by default (`tests/*`, `conftest.py`, ...); repeatable |
| `--copy-data / --no-copy-data` | on | copy data files to the output, keeping relative paths |
| `--data-glob GLOB` | none | only copy data files that match; repeatable |

## Parsing

| Flag | Default | Effect |
|------|---------|--------|
| `-j, --workers N` | CPU count | parallel parsing workers |
| `--executor` | `process` | `process` or `thread` |
| `--progress` | off | tqdm progress bar; falls back to log lines when tqdm is missing |

## Entry points

| Flag | Default | Effect |
|------|---------|--------|
| `--entry MODULE` | none | keep only the `__main__` guard of this module; repeatable |
| `--entry-keep-all` | off | keep every guard even when `--entry` is given |
| `--main-name NAME` | none | rename `main` to `NAME`; must be a valid Python identifier |
| `--generate-guard` | off | add `if __name__ == "__main__": main()` when no guard exists |

## Content

| Flag | Default | Effect |
|------|---------|--------|
| `--imports` | `prune` | `prune` drops unused imports, `keep` keeps all, `strict` prunes and warns |
| `--keep-docstrings / --no-keep-docstrings` | on | keep docstrings of classes and functions. Module docstrings are always pruned |
| `--keep-comments / --no-keep-comments` | on | keep comments |
| `--format` | off | run a formatter on the result |
| `--formatter` | `black` | `black` or `ruff` |

## Output control

| Flag | Default | Effect |
|------|---------|--------|
| `--dry-run` | off | log the plan, write nothing |
| `--check` | off | exit 2 when the existing output would change |
| `--stdout` | off | print the generated module instead of writing it |
| `--diff` | off | print a unified diff against the existing output |
| `--force` | off | silence the overwrite warning |
| `--config PATH` | auto | `pyproject.toml` with a `[tool.pyrefactor]` table |

## Logging

| Flag | Default | Effect |
|------|---------|--------|
| `--log-level` | `INFO` | `TRACE`, `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `--log-file PATH` | none | also log to a file |
| `--version` | | print the version |

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | success (including a successful `--dry-run` or `--check`) |
| 1 | unexpected internal failure (traceback is logged) |
| 2 | consolidation error: missing source, unparsable input file, `--check` mismatch, invalid `--main-name`, or a generated module that is not valid Python |

## Removed flags

Removed in 2.0: `--mode`, `--validate`, `--validate-import`,
`--allow-syntax-errors`, `--max-skipped`, `--source-map`, `--preserve-modules`,
`--namespace`, `--all`, `--respect-all`, `--rename-strategy`, `--dedupe`,
`--no-parallel`, `--jobs`, `--progress-bar`, `--overwrite`, `--keep-all-mains`,
`--keep-unused-imports`, `--post-format`, `--pyproject`, `--setup-cfg`,
`--strict`, `--fail-on-warning`.

There is one layout, one dedupe strategy (content hash), one rename strategy
(`<module>_<name>`) and one validation step (syntax check with `ast`), which
always runs and needs no flag. See [Validation](../internals/validation.md).
