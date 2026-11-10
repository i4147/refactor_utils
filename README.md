# refactor_utils

Consolidate a small/medium Python module, library or project into **one**
dependency-ordered file.

```bash
pip install -e .
pyrefactor path/to/package        # writes ./output/<current-directory-name>.py
```

A run without flags produces exactly one generated file:

```
$ cd myproject
$ pyrefactor src/mypackage
output/
  myproject.py        # imports -> constants -> classes -> functions -> entry points
```

The file name is `Path.cwd().name` (override with `--name`). Data files that
ship next to the code (`.json`, `.yaml`, `.csv`, `.ini`, …) are copied with
their relative paths unless `--no-copy-data` is given.

## What it does

- Parses every file with **tree-sitter** in parallel worker processes; skips
  tests, `conftest.py`, `setup.py`, `__main__.py` and `iwgfyrkugf.py`
  (re-include anything with `--include`, skip more with `-x/--exclude`).
- Orders blocks by import-time dependencies (base classes, decorators,
  defaults, class bodies, module-level calls), preferring constants → classes →
  functions. Cycles are reported with a warning and emitted in dependency order.
- Resolves same-named objects across modules using a **content hash**: bodies
  that are byte-identical (ignoring blank lines/trailing spaces) are merged into
  a single definition; bodies that differ keep both definitions and the
  shallower module keeps the plain name while the others become
  `<module_path>_<name>`. Every reference (`from .x import y as z`, `mod.attr`,
  annotations, decorators, defaults) is rewritten accordingly.
- Merges, prunes, de-duplicates and conflict-aliases imports
  (`--imports prune|keep|strict`), grouping `__future__` / stdlib / third-party.
  Names starting with `_` sort before alphanumeric names.
- `if TYPE_CHECKING:` blocks are merged like the head import section: the
  imports inside join the normal import section (deduplicated against imports
  already present at the top of the file), and constants/classes/functions
  defined under `TYPE_CHECKING` are extracted as ordinary blocks.
- `__all__` is never emitted as code. `__all__ = […]`, `__all__ += […]`,
  `__all__.append(…)` and annotated forms are only read as hints and dropped;
  the exported surface is recomputed from the surviving definitions.
- `main` functions and `if __name__ == "__main__":` guards go to the end.
  When several modules have their own guard, they are **merged into a single
  guard** whose body runs each original entry point, annotated with the module
  it came from — stacked guards would make every one but the first dead code.
  Control which entries survive with `--entry MODULE` / `--entry-keep-all`, and
  the names with `--main-name NAME` / `--generate-guard`.
- **Validation is mandatory**: the generated module is compiled and smoke
  imported before anything is written; a broken result is never emitted. A
  source file that cannot be parsed aborts the run instead of degrading output.
- Errors and warnings are logged with **loguru** (`--log-level`, `--log-file`).

## CLI

```
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

| Flag | Default | Effect |
|------|---------|--------|
| `source` | — | package directory, project root or single `.py` file |
| `-o, --output` | `output` | output directory |
| `--name NAME` | current directory name | name of the generated file |
| `-j, --workers` | CPU count | parallel parsing workers |
| `--executor` | `process` | `process` or `thread` executor |
| `--progress` | off | tqdm progress bar (falls back to log) |
| `-x, --exclude GLOB` | — | extra globs to skip (repeatable), source-relative |
| `--include GLOB` | — | re-include files/dirs excluded by default (`tests/*`, `conftest.py`, …) |
| `--copy-data / --no-copy-data` | on | copy data files preserving relative paths |
| `--data-glob GLOB` | — | only copy matching data files (repeatable) |
| `--entry MODULE` | — | keep only the `__main__` guard of this module (repeatable) |
| `--entry-keep-all` | off | keep every `__main__` guard |
| `--main-name NAME` | — | rename `main` to `NAME` |
| `--generate-guard` | off | synthesise `if __name__ == "__main__": main()` |
| `--imports` | `prune` | `prune` = drop unused, `keep` = keep all, `strict` = prune + warn |
| `--keep-docstrings / --no-keep-docstrings` | on | keep docstrings |
| `--keep-comments / --no-keep-comments` | on | keep comments |
| `--dry-run` | off | print the plan, never write |
| `--check` | off | fail (exit 2) if the output would change |
| `--stdout` | off | print the generated module instead of writing |
| `--diff` | off | print a unified diff against the existing output |
| `--force` | off | silence the overwrite warning |
| `--config PATH` | auto | `pyproject.toml` with a `[tool.pyrefactor]` section |
| `--format` | off | run the formatter on the output |
| `--formatter` | `black` | `black` or `ruff` |
| `--log-level`, `--log-file`, `--version` | | logging and version |

Removed in 2.0: `--mode`, `--validate`, `--validate-import`,
`--allow-syntax-errors`, `--max-skipped`, `--source-map`, `--preserve-modules`,
`--namespace`, `--all`, `--respect-all`, `--rename-strategy`, `--dedupe`,
`--no-parallel`, `--jobs`, `--progress-bar`, `--overwrite`, `--keep-all-mains`,
`--keep-unused-imports`, `--post-format`, `--pyproject`, `--setup-cfg`,
`--strict`, `--fail-on-warning`, `--respect-all`. There is one layout, one
dedupe strategy (content hash), one rename strategy (`<module>_<name>`) and
validation always runs.

## Examples

```bash
# consolidate the current project into ./output/<cwd>.py
pyrefactor .

# keep the file name stable in CI
pyrefactor src/mypackage --name mypackage --check

# preview without writing anything
pyrefactor src/mypackage --dry-run

# skip vendored code, keep its data files
pyrefactor . -x "vendor/*"

# include the tests directory that is skipped by default
pyrefactor . --include "tests/*"

# format the result and drop comments
pyrefactor . --format --formatter ruff --no-keep-comments
```

## Configuration

`pyproject.toml` next to the source (or `--config PATH`):

```toml
[tool.pyrefactor]
exclude = ["tests/*", "iwgfyrkugf.py"]
include = []
entry = []
imports = "prune"
format = false
copy_data = true
data_glob = ["*.json"]
```

CLI values win where they are explicit.

## Architecture

```
cli.py → core.py orchestrates
        → discovery.py (SourceFile + DataFile, pyproject awareness)
        → parser.py (tree-sitter, parallel, TYPE_CHECKING flattening, __all__ detection)
        → resolver.py (content-hash dedupe, collision renames, imports, ordering input)
        → ordering.py (topo sort, deterministic tie-breaks)
        → writer.py (single consolidated file)
        → imports.py (import merging/sorting: `_private` names first)
logging via logging_setup.py
models.py — picklable dataclasses (Block, ModuleInfo, SourceFile, DataFile, ImportSpec, Occ)
```

The parser only emits `ModuleInfo`/`Block`/`Occ`, so worker payloads stay
picklable and the resolver never touches tree-sitter nodes.

## Known limits

- Renaming is name-based: parameters and class bodies are scoped, other locals
  are not (a full scope analysis is future work).
- `__file__`/`__name__`/`__package__` semantics change in a single file
  (warning logged; `importlib.resources` users may need `files()`).
- Guarded imports (`try/except ImportError`, `if sys.platform …`) are kept
  verbatim and may have side effects.
- Dynamic `importlib`/`__import__` dependencies are warned about, not resolved.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e .[dev]
.venv/bin/python -m pytest tests -q
.venv/bin/ruff check src tests
```

## License

MIT
