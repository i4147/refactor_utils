# refactor-utils (`pyrefactor`)

Consolidate a Python package / project into a **single dependency-ordered `.py`
file**, or pack it into a **self-extracting zip bundle**. Optionally apply
**modernization passes** (`six` → Python 3, `os.path` → `pathlib`) to the
output.

This is the merger of three earlier prototypes:

| origin | what it contributed |
| --- | --- |
| `ru.txt` (the original `refactor_utils`) | the libcst-based source consolidator: parallel parsing, import pruning/keep/strict, topological ordering, deduplication, name-conflict resolution, `__main__`-guard merging, data-file copy/embed, `pyproject.toml` config, black/ruff formatting |
| `consolidator.py` | **bundle mode** (`--bundle`): self-extracting zip standalone script, third-party package inclusion, `-m` module mode, `--main-module` synthesis, compression, deterministic builds |
| `consolidate.py` | **`--merged-file`** input (the `# File:` header format) and **`--modernize`** passes (`six`, `ospath`) |

## Install

```bash
pip install -e .            # core (libcst only)
pip install -e ".[format]"  # + black & ruff for --format
pip install -e ".[progress]" # + tqdm for --progress
```

Provides the `pyrefactor` console script and `python -m refactor_utils`.

## Quick start

### Source consolidation (default)

```bash
pyrefactor my_pkg/ -o out/                  # out/my_pkg.py
pyrefactor my_pkg/ --format --imports strict
pyrefactor my_pkg/ --embed-data             # fully self-contained .py
pyrefactor my_pkg/ --dry-run
pyrefactor my_pkg/ --check                  # CI: fail if output would change
pyrefactor my_pkg/ --modernize six,ospath   # modernize the output
```

### Bundle mode (`--bundle`)

Produces a runnable `*_standalone.py` that embeds the package as a base64 zip
and extracts it to a temp dir at runtime:

```bash
pyrefactor my_pkg/ --bundle -o out/run.py
pyrefactor my_pkg/ --bundle --include-package rich --site-path vendor
pyrefactor app/main.py --bundle -p src --include-data 'assets/*.json'
pyrefactor my_pkg/ --bundle --main-module cli     # synthesize __main__.py
pyrefactor -m my.pkg --bundle                     # python -m emulation
./out/run.py                                       # runs the program unchanged
```

Bundle runtime env vars: `PYREFACTOR_BUNDLE_DEBUG=1` (print extraction dir),
`PYREFACTOR_BUNDLE_KEEP=1` (keep the temp dir).

### Merged-file input (`--merged-file` / `-f`)

Read a source tree from a single file using `# File: <relpath>` section headers
(the format produced by `pyrefactor --stdout` dumps and by this project's own
`ru.txt` archive):

```bash
pyrefactor -f ru.txt -o out/ru.py
```

## Configuration

A `[tool.pyrefactor]` section in `pyproject.toml` is picked up automatically
(or pass `--config PATH`). Recognised keys: `exclude`, `include`, `entry`,
`imports`, `format`, `formatter`, `copy_data`, `embed_data`, `data_glob`,
`modernize`.

## Notable fixes since v3

* **TRACE log level actually works** — the root logger was capped at `DEBUG=10`,
  which silently dropped every `TRACE` (level 5) record before it reached the
  handler. The logger now sits at or below TRACE so the handler decides.
* **Formatter failures are no longer silent** — `--format` now logs a warning
  when black/ruff is missing or errors, and checks the ruff subprocess exit code
  instead of swallowing every exception.
* Added `__main__.py` (`python -m refactor_utils`) and a proper `pyproject.toml`
  entry point.

## What was *not* ported

`consolidate.py`'s `LoggingTransformer` (logging → loguru) and
`ExecutorTransformer` (concurrent.futures → multiprocessing.Pool) were left out:
both assume a specific runtime convention (`logger` is loguru; a `MAX_WORKERS`/
`mp.Pool` shape) that does not generalise to arbitrary projects. The general
`six` and `os.path` passes are available under `--modernize`.
