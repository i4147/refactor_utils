# refactor_utils

Consolidate a small/medium Python module, library or project into one dependency-ordered file.

```bash
pip install -e .
pyrefactor path/to/package -o output
```

Output (`--mode single`, default):

```
output/
  __init__.py        # re-exports public constants, classes, functions + __all__
  _consolidated.py   # imports -> constants -> classes -> functions -> entry points
```

## What it does

- Parses every file with **tree-sitter** (in parallel worker processes); skips tests, `conftest.py`, `setup.py` and every `__main__.py` (override with `--include`).
- Orders blocks by import-time dependencies (base classes, decorators, defaults, class bodies, module-level calls), preferring constants → classes → functions. Cycles are reported with a warning and emitted in dependency order.
- Resolves same-named objects across modules: the shallowest module keeps the name, the others become `<module_path>_<name>` and every reference (including `from .x import y as z` and `mod.attr` access) is rewritten. Identical, self-contained classes/functions are merged (controlled by `--dedupe`).
- Optimizes imports: internal imports removed, external ones merged, de-duplicated, conflict-aliased, unused ones dropped (`--imports prune` default; `--imports keep` to retain; `--keep-unused-imports` deprecated alias), grouped `__future__` / stdlib / third-party.
- `main` functions and `if __name__ == "__main__":` guards go to the end (`--entry MODULE` keeps a single guard; `--entry-keep-all` keeps all; `--main-name` renames; `--generate-guard` auto-creates).
- Errors and warnings are logged with **loguru** (`--log-file`, `--log-level`); strictness via `--strict`, `--fail-on-warning`, `--max-skipped`, `--allow-syntax-errors`.
- Data files (`.json`, `.yaml`, `.csv`, `.ini`, etc.) are discovered and copied to output preserving relative paths (`--copy-data` / `--no-copy-data` / `--data-glob`).

## Summary of Changes (from prompt.md)

This release implements all flags and edge-case hardening described in `prompt.md`:

- **15+ new CLI flags** covering dry-run, diff, config, formatting, rename strategies, dedupe, import modes, `__all__` handling, namespace/source-map, validation, etc. — all backward-compatible, `single` remains default.
- **Robust edge-case handling** for dynamic imports, `__all__` mutation, conditional imports, collisions, circular dependencies, scoping, runtime semantics, and discovery/IO (symlinks, `src/` layout, namespace packages, `pyproject.toml` awareness, BOM/CRLF/encoding, etc.).
- **Data file preservation**: non-Python assets are discovered via `discovery.discover_data_files()` and copied to output with precedence-aware filtering.
- **Parser/resolver decoupled**, worker data stays picklable (`models.py` dataclasses), output always compiles, `--validate` smoke-imports.

---

## Installation

```bash
pip install -e .
# optional extras
pip install black ruff  # for --format
pip install tqdm        # for --progress
```

Requirements: Python ≥3.10, `tree-sitter`, `tree_sitter_python`, `loguru`.

---

## Quick Start

```bash
# Basic consolidation (single mode, default)
pyrefactor src/mypackage -o output

# Dry-run: show plan without writing
pyrefactor src/mypackage -o output --dry-run --log-level INFO

# Check: fail if output would change (CI)
pyrefactor src/mypackage -o output --check --diff

# Write to stdout instead of files
pyrefactor src/mypackage -o output --stdout > consolidated.py

# Use config from pyproject.toml
pyrefactor src/mypackage -o output --config pyproject.toml
# or auto-detect [tool.pyrefactor] via --pyproject
pyrefactor src/mypackage -o output --pyproject

# Format output with black
pyrefactor src/mypackage -o output --format --formatter black

# Preserve data files
pyrefactor src/mypackage -o output --copy-data
pyrefactor src/mypackage -o output --data-glob "*.json"

# Source maps + namespaces for debugging
pyrefactor src/mypackage -o output --source-map --preserve-modules --namespace

# Strict validation
pyrefactor src/mypackage -o output --validate --strict --fail-on-warning

# Different output layouts
pyrefactor src/mypackage -o output --mode package
pyrefactor src/mypackage -o output --mode flat
pyrefactor src/mypackage -o output --mode split
```

---

## CLI Reference — `--help`

```
usage: pyrefactor [-h] [-o OUTPUT] [-m {single,package,module,flat,split}]
                  [-j WORKERS] [--jobs JOBS] [--executor {process,thread}]
                  [--no-parallel] [--progress] [--progress-bar] [-x GLOB]
                  [--include GLOB] [--copy-data] [--no-copy-data]
                  [--data-glob GLOB] [--entry MODULE] [--entry-keep-all]
                  [--main-name NAME] [--generate-guard] [--keep-all-mains]
                  [--keep-unused-imports] [--imports {prune,keep,strict}]
                  [--respect-all] [--all {ignore,respect,merge,recompute}]
                  [--rename-strategy {prefix,suffix,module_path,error}]
                  [--dedupe {none,safe,aggressive}] [--keep-docstrings]
                  [--strip-docstrings] [--keep-comments] [--strip-comments]
                  [--namespace] [--preserve-modules] [--source-map]
                  [--dry-run] [--check] [--stdout] [--diff] [--overwrite]
                  [--force] [--config PATH] [--pyproject] [--setup-cfg]
                  [--format] [--post-format] [--formatter {black,ruff}]
                  [--validate] [--validate-import] [--strict]
                  [--fail-on-warning] [--max-skipped N]
                  [--allow-syntax-errors] [--eol {lf,crlf,preserve}]
                  [--log-level {TRACE,DEBUG,INFO,WARNING,ERROR}]
                  [--log-file LOG_FILE] [--version]
                  source
```

All flags have concise help strings referencing the module they touch (e.g., `[resolver.py]`, `[parser.py]`).

---

## New Flags Table

| Flag | Default | Effect / Semantics | Module(s) | Breaking? |
|------|---------|-------------------|-----------|-----------|
| `--dry-run` | `false` | Show plan (blocks, renames, exports, data files) without writing; never touches FS | `cli.py`, `core.py` | No |
| `--check` | `false` | Like `--dry-run` but also computes diff vs existing output; exits 2 if diff non-empty (CI gate) | `core.py` | No |
| `--stdout` | `false` | Write `_consolidated.py` + `__init__.py` to stdout instead of files | `core.py`, `writer.py` | No |
| `--config PATH` | `None` | Load `[tool.pyrefactor]` from `pyproject.toml` (excludes, entry, rename, dedupe, imports, all, format, strictness) | `cli.py`, `core.py`, `discovery.py` | No |
| `--pyproject` / `--setup-cfg` | `false` | Auto-detect `pyproject.toml`/`setup.cfg` near source for `[tool.pyrefactor]` (package discovery awareness) | `discovery.py` | No |
| `--format` / `--post-format` | `false` | Run `black` (default) or `ruff format` on generated files; warns if not installed | `writer.py` | No |
| `--formatter {black,ruff}` | `black` | Choose formatter for `--format` | `writer.py` | No |
| `--diff` | `false` | Emit unified diff vs existing output (if present); with `--check` drives exit code | `core.py` | No |
| `--overwrite` / `--force` | `false` | Allow overwriting non-empty output directory (otherwise warns; future may require) | `core.py` | No |
| `--include GLOB` | `[]` | Re-include files matching glob that would be excluded by defaults; explicit `--exclude` always wins. Precedence: `default excludes < --include < --exclude` | `discovery.py` | No |
| `--exclude GLOB` | `[]` | Extra path globs to skip (repeatable) | `discovery.py` | No (existing) |
| `--copy-data` / `--no-copy-data` | `true` | Copy data files (`.json`, `.yaml`, `.csv`, `.txt`, `.ini`, etc.) preserving relative paths | `discovery.py`, `core.py` | No |
| `--data-glob GLOB` | `[]` | Only copy data files matching glob (repeatable filter) | `discovery.py` | No |
| `--keep-docstrings` | `true` | Keep docstrings (default) | `parser.py` | No |
| `--strip-docstrings` | — | Strip docstrings via AST (sets `keep_docstrings=False`) | `parser.py` | No |
| `--keep-comments` | `true` | Keep comments (default) | `parser.py` | No |
| `--strip-comments` | `false` | Strip standalone `#` comments (keeps shebang/encoding) | `parser.py` | No |
| `--rename-strategy {prefix,suffix,module_path,error}` | `prefix` | Collision policy: `prefix` → `<mod>_name`, `suffix` → `name_<mod>`, `module_path` → full path, `error` → fail | `resolver.py` | No |
| `--dedupe {none,safe,aggressive}` | `safe` | `none`=keep all, `safe`=identical class/func with same ext refs, `aggressive`=also const/misc whitespace-insensitive | `resolver.py` | No |
| `--imports {prune,keep,strict}` | `prune` | `prune`=drop unused (default), `keep`=retain all, `strict`=prune but warn on unused (implies validation) | `resolver.py`, `imports.py` | No |
| `--keep-unused-imports` | `false` | Deprecated alias for `--imports keep` | `resolver.py` | No |
| `--all {ignore,respect,merge,recompute}` | `ignore` | `ignore`=all public, `respect`=only `__all__`, `merge`=union + public, `recompute`=sorted public | `resolver.py` | No |
| `--respect-all` | `false` | Deprecated alias for `--all respect` | `resolver.py` | No |
| `--entry MODULE` | `[]` | Keep only guards of listed modules (repeatable) | `resolver.py` | No (existing) |
| `--entry-keep-all` / `--keep-all-mains` | `false` | Keep all `__main__` guards (default with no `--entry` already keeps all) | `resolver.py` | No |
| `--main-name NAME` | `None` | Rename `main()` to `NAME` (rewrites definition & references) | `resolver.py` | No |
| `--generate-guard` | `false` | Auto-generate `if __name__ == "__main__": main()` if no guard exists and a `main` exists | `resolver.py`, `writer.py` | No |
| `--namespace` | `false` | Emit `# namespace: module.name` comment before each block | `writer.py` | No |
| `--preserve-modules` | `false` | Emit `# Original module: ...` per block | `writer.py` | No |
| `--source-map` | `false` | Inline `# source: path:line-line` per block | `parser.py`, `writer.py` | No |
| `--fail-on-warning` | `false` | Exit 2 if any warning emitted | `core.py` | No |
| `--max-skipped N` | `None` | Fail if `len(skipped) > N` (e.g., `--max-skipped 0` enforces no skips) | `core.py` | No |
| `--allow-syntax-errors` | `false` | Try partial parse for syntax-error files instead of skipping (warn, may produce invalid output) | `parser.py`, `core.py` | No |
| `--eol {lf,crlf,preserve}` | `lf` | Line endings for generated files; `preserve` follows the input files | `core.py` | No |
| `--jobs N` | `None` | Alias for `--workers` | `cli.py` | No |
| `--workers / -j` | `CPU` | Parallel workers (default CPU count) | `core.py` | No (existing) |
| `--executor {process,thread}` | `process` | Executor type | `core.py` | No (existing) |
| `--no-parallel` | `false` | Disable parallel (workers=1) | `core.py` | No |
| `--progress` / `--progress-bar` | `false` | Show tqdm progress bar (falls back to log) | `core.py` | No |
| `--validate` | `false` | Compile output (`compile(..., "exec")`); with `--validate-import` also smoke-import | `core.py` | No |
| `--validate-import` | `false` | Smoke-import generated module via `importlib` (executes top-level code; a warning is logged first) | `core.py` | No |
| `--strict` | `false` | Exit 1 if any file was skipped; also refuses to write output when a warning means the result may be wrong (unresolved name lookups, computed `__all__`, unresolvable internal imports) and rejects import cycles | `core.py` | No (existing) |
| `--mode {single,package,module,flat,split}` | `single` | Output layout (keeps `single` default) | `core.py`, `writer.py` | No |

All new flags preserve `ConsolidationResult` contract (added fields, not changed) and are documented in `build_parser()`.

---

## Edge-Case Handling Plan

| Case | Detection Point | Behavior | Warning vs Error | Gated by Flag |
|------|----------------|----------|------------------|---------------|
| **Dynamic imports** (`importlib`, `__import__`, `globals()`, `locals()`, `eval`, `exec`, `getattr`) | `parser.py` `_detect_dynamic_patterns` (regex on full text + occ scan) | Emit warning: “may miss dependencies / incomplete name resolution”; never silently drop; keep guarded imports verbatim | Warning (INFO/WARNING) | Always; `--fail-on-warning` makes error |
| **`__getattr__`, `__dir__`, monkeypatching** | `parser.py` (pattern scan) | Warn about module-level attribute access semantics change | Warning | Always |
| **Computed/mutated `__all__`** (`__all__ += [...]`, `__all__.append`) | `parser.py` `_extract` (`_target_names` for `__all__`) | Warn “static analysis may be incomplete”; fallback to `ignore` | Warning | `--all` controls fallback |
| **Re-exports** (`from .x import y` where y re-exported via `__all__`) | `resolver.py` `_exports` (resolve symbol for each `__all__` entry) | Merge re-export into exports | Warning if unresolved | `--all merge` |
| **Local star imports** (`from .mod import *`) | `resolver.py` `_build_envs` (expand via `_star_names`) | Expand to listed names (or `declared_all`); warn on expansion count | Warning | Always |
| **External star imports** (`from pkg import *`) | `resolver.py` | Keep as `from pkg import *` + warn kept as is | Warning | Always |
| **Conditional/platform/optional imports** (`if sys.platform`, `try/except ImportError`, `TYPE_CHECKING`) | `parser.py` `_extract` (cond string check) + guards | `TYPE_CHECKING` imports extracted as normal imports; `try/except` guards kept verbatim with warning | Warning | Always |
| **Nested imports inside functions/classes** | `parser.py` `_collect_occs` (`nested_import` occ) | Tracked as specs per block; internal nested imports removed, external kept (rendered as `pass` if all internal) | Log INFO | Always |
| **Side-effect-only imports** | `resolver.py` (guards) | Guards kept verbatim regardless of usage | Info | Always |
| **Import aliases** (`import foo as bar`) | `parser.py` `parse_import`, `resolver.py` `_ext_local` | Preserved, conflict-aliased if needed | Info on conflict | `--rename-strategy` for conflicts |
| **Collisions external vs local** | `resolver.py` `_assign_names` (ext_loc_modules) | Renamed via strategy, logged | Info | `--rename-strategy` |
| **Circular imports / import-time cycles** | `ordering.py` `topo_order` (heap + cycle detection) | Warn “Circular import-time dependency: emitting X before Y”, break cycle by emitting shallowest | Warning | Always |
| **Duplicate definitions identical bodies** | `resolver.py` `_dedupe` (`safe`/`aggressive`) | Dropped, logged (`Dropping duplicate...`) | Info | `--dedupe` |
| **Duplicate with differing bodies** | `resolver.py` `_assign_names` | Renamed via strategy | Info | `--rename-strategy` |
| **Duplicate side-effect blocks** (`MISC` with same source) | `resolver.py` `aggressive` dedupe | Only deduped in aggressive (whitespace-insensitive) | Info | `--dedupe aggressive` |
| **Class-body scoping, comprehensions, lambdas, walrus, `match`, `global`/`nonlocal`** | `parser.py` `_collect_occs` (shadow set, lazy flag, cls block) | Params shadowing respected, class body eager/lazy handled, walrus/match treated as normal | — | Always |
| **Decorators, type annotations, default args referencing renamed names** | `parser.py` `_collect_occs` (params, return_type, type_params, defaults pushed as lazy) | Rewritten via occ edits; decorators/annotations tracked | Info on rename | Always |
| **Module-scope rebindings / shadowing by params/locals** | `parser.py` (shadow frozenset) | Param names shadow module names; not rewritten | — | Always |
| **`__file__`, `__name__`, `__package__`, `__path__`, relative resource loading** | `parser.py` `_detect_runtime_semantics` + `Occ` for `__file__` | Warn semantics change, resource loading may break | Warning | Always |
| **Imports depending on module identity/order** | `resolver.py` topo `eager` deps, `ordering.py` | Ordered by import-time deps, rank constants→classes→funcs | Warning on cycle | Always |
| **`__main__` guards, multiple entry modules, `main()` detection** | `resolver.py` `MAIN` handling, `--entry` filter | `--entry` keeps only listed; `--entry-keep-all` keeps all; multiple without flag warns; `main()` moved to tail; `--main-name` / `--generate-guard` | Warning/Info | `--entry*`, `--main-name`, `--generate-guard` |
| **Namespace packages (PEP 420)** | `discovery.py` `_walk` (recurse regardless of `__init__.py`) | Discovered via relative path, no `__init__` required | — | Always |
| **`src/` layout** | `discovery.py` `detect_base` | If `src/` contains packages, base is `src` | — | Always |
| **Single-file input** | `discovery.py` `discover` (is_file branch) | Returns single `SourceFile` | — | Always |
| **Symlinks** | `discovery.py` `_walk` (is_symlink branch) | Warn on broken symlink; include file symlink if `.py`; skip dir symlink unless `--include`; resolve `skip` to avoid loops | Warning | Always |
| **Output inside source tree** | `core.py` `skip_dirs=[output]` + `discovery.py` skip set | Output dir resolved and excluded from walk | — | Always |
| **Syntax errors, partial parse** | `parser.py` `parse_module_file` (`root.has_error`) | Without flag: `info.errors` → skipped, logged ERROR. With `--allow-syntax-errors`: warn + attempt `_extract` → may produce invalid output (compile will error, gated by `--validate`) | Error vs Warning | `--allow-syntax-errors` |
| **Encoding/BOM/CRLF/tabs/shebang/encoding cookies** | `parser.py` (BOM strip, decode probe, CRLF check) | BOM stripped, non-UTF8 decoded with replace + warning, CRLF warned normalized, shebang/encoding cookie preserved even when stripping comments | Warning | `--strip-comments` |
| **`pyproject.toml`/`setup.py` discovery** | `discovery.py` `detect_pyproject_config` | Loads `[tool.pyrefactor]` if present; logs; merges with CLI (CLI wins) | Info | `--config`, `--pyproject` |
| **Exclusion of tests/docs/vendored** | `discovery.py` `EXCLUDED_DIRS`, `EXCLUDED_FILE_GLOBS` | Skipped, logged DEBUG; overridden by `--include` but not by explicit `--exclude` | Debug | `--include`/`--exclude` |
| **Huge projects, worker crash/pickling** | `core.py` `_parse_all` (BrokenExecutor fallback) | On `BrokenExecutor` retry with threads; logs warning; huge files emitted via ThreadPool for render | Warning | `--no-parallel`, `--executor thread` |
| **Permission errors, non-UTF8 files** | `parser.py`/`discovery.py` OSError | Logged ERROR, file skipped (added to `skipped`) | Error | `--max-skipped`, `--strict` |
| **Data files** (`*.json`, `*.csv`, `*.txt`, etc.) | `discovery.py` `discover_data_files` (`_is_data_file`) | Discovered, logged, copied preserving `relative_to(base)` → `output/relative` via `shutil.copy2`; filtered by `--data-glob` | Info | `--copy-data`/`--no-copy-data` |

All dynamic/runtime cases prefer **warnings + skip**; only fail hard when `--strict`, `--fail-on-warning`, or `--validate` demands it. Every rename/dedupe/skipped file is logged at `INFO`/`WARNING`.

---

## Prioritized Implementation Plan

**P0 — Core safety & backward compat**
- Preserve `single` default; add `mode` choices with minimal distinct renderers (`writer.py`: `render_consolidated`, `render_flat`, `render_split`, etc.).
- Implement `--dry-run`/`--check`/`--stdout`/`--diff`/`--overwrite` in `core.py` (no writes on dry-run, diff via `difflib`).
- Implement `--include`/`--exclude` precedence in `discovery.py`.
- Implement `--allow-syntax-errors`, `--max-skipped`, `--fail-on-warning`, `--validate` (compile + optional import).
- Add `consolidation` data file discovery & copy (`discover_data_files` → `shutil.copy2`).
- Fix pickling for `ProcessPoolExecutor` (`_parse_worker` top-level).

**P1 — Collision & hygiene**
- `--rename-strategy {prefix,suffix,module_path,error}` in `resolver.py` (generate `taken` set, `_prefix`/`_suffix`/`_module_path_prefix`).
- `--dedupe {none,safe,aggressive}` (group by `(kind, primary, norm_source, ext_refs)`).
- `--imports {prune,keep,strict}` and `--all {ignore,respect,merge,recompute}`.
- `--entry` improvements: `--entry-keep-all`, `--main-name`, `--generate-guard` (tail handling, `main_name` override in `_assign_names`).
- `--strip-comments`/`--strip-docstrings`/`--keep-comments` in `parser.py` (AST/docstring stripping, comment line filter).
- `--source-map`/`--preserve-modules`/`--namespace` in `writer.py` (prefix comments per block using `Block.orig_path`/`start_line`).

**P2 — Ergonomics & discovery**
- `--config`/`--pyproject` awareness (`tomllib` load `[tool.pyrefactor]`; CLI wins).
- `--jobs` alias, `--no-parallel`, `--progress` (tqdm) in `cli.py`/`core.py`.
- `--format`/`--post-format`/`--formatter` in `writer.py` (`black`/`ruff` via `subprocess`).
- `src/` layout, namespace packages, symlinks, single-file input, output-inside-source already handled; add warnings for `__file__`/`__name__`, dynamic constructs.
- Huge project resilience: `BrokenExecutor` fallback, `ThreadPoolExecutor` for render, non-UTF8/BOM/CRLF handling.

---

## Test Matrix

### Unit Tests (parser / resolver / discovery / writer)

| ID | Area | Test |
|----|------|------|
| U1 | discovery | `discover` excludes `tests/`, `__pycache__`, `test_*.py`, `__main__.py` by default; `--include conftest.py` re-includes; explicit `--exclude` always wins |
| U2 | discovery | `detect_base` picks `src/` when `src/pkg/__init__.py` exists; detects namespace package (no `__init__.py` but with `.py` files) |
| U3 | discovery | `discover_data_files` finds `*.json`/`*.csv`/`*.ini` and preserves relative path; `--data-glob "*.json"` filters; `--no-copy-data` returns [] |
| U4 | discovery | `detect_pyproject_config` loads `[tool.pyrefactor]` correctly; missing file returns {} |
| U5 | parser | `parse_module_file` strips BOM, warns on CRLF, decodes non-UTF8 with replace, keeps shebang/encoding cookie even when stripping comments |
| U6 | parser | `keep_docstrings=False` removes first `Expr(Str)` block via AST; `strip_comments` removes `#` lines |
| U7 | parser | `allow_syntax_errors=False` marks `info.errors` on syntax error; `True` emits `partial parse produced N blocks` warning and still extracts |
| U8 | parser | Detects `importlib`, `__import__`, `eval`/`exec`/`getattr` and adds `info.warnings` |
| U9 | resolver | `_dedupe` `none` keeps duplicates, `safe` drops identical class/func, `aggressive` also drops const/misc whitespace-insensitively |
| U10 | resolver | `_assign_names` `prefix` → `mod_name`, `suffix` → `name_mod`, `module_path` → `full_path_name`, `error` raises `RuntimeError` on collision |
| U11 | resolver | `--imports keep` emits unused imports; `prune` drops; `strict` warns on unused |
| U12 | resolver | `--all ignore` exports all public; `respect` only `__all__`; `merge` union; `recompute` sorted public |
| U13 | resolver | `--main-name cli_main` maps `(bid, "main")` → `cli_main` and renders `def cli_main` |
| U14 | writer | `render_consolidated` with `source_map`/`preserve_modules` prefixes `# source:`/`# Original module:` before each block; `flat` has no section headers; `split` returns dict with `const.py`/`class.py`/etc. |
| U15 | writer | `--format` tries `black` then `ruff`; fallback to original if not installed; output still compiles |

### Integration Tests (core + cli)

| ID | Command | Expect |
|----|---------|--------|
| I1 | `pyrefactor pkg -o out --dry-run` | Exit 0, no files written, log “Would write …” + “Would copy data …” |
| I2 | `pyrefactor pkg -o out --check` (up-to-date) | Exit 0, “Check passed”; with changed source + no `--overwrite` but diff non-empty → exit 2 and diff printed |
| I3 | `pyrefactor pkg -o out --stdout` | Prints consolidated + `__init__` to stdout, `result.files==[]`, exit 0 |
| I4 | `pyrefactor pkg -o out --diff` | Prints `--- a/_consolidated.py` unified diff if output exists and differs; else “No diff” |
| I5 | `pyrefactor pkg -o out --overwrite` (non-empty out) | Succeeds; without flag still succeeds (warn) but future may require flag |
| I6 | `pyrefactor pkg -o out --config pyproject.toml` | Loads `exclude=["c.py"]`, excludes that file; `result.modules` reduced |
| I7 | `pyrefactor pkg -o out --format --formatter black` | Output passes `compile(..., "exec")`; if `black` missing, still compiles |
| I8 | `pyrefactor pkg -o out --validate` | Exit 0 and `result.validated==True` if compiles; fail if syntax error in generated |
| I9 | `pyrefactor pkg -o out --validate-import` | Also smoke-imports via `importlib.util.spec_from_file_location`; fail on import error |
| I10 | `pyrefactor pkg -o out --fail-on-warning` (with `__file__` usage) | Exit 1 due to warning |
| I11 | `pyrefactor pkg -o out --max-skipped 0` (with 1 bad file) | Exit 2 “too many skipped” |
| I12 | `pyrefactor pkg -o out --allow-syntax-errors` (bad.py) | `modules` includes bad file (partial parse), may generate invalid output but not skip; without flag, `skipped` contains it |
| I13 | `pyrefactor pkg -o out --jobs 1` / `--no-parallel` | Same output as parallel; workers=1 |
| I14 | `pyrefactor pkg -o out --progress` | Shows `tqdm` bar if installed else log fallback; exit 0 |
| I15 | `pyrefactor pkg -o out --mode package` | Creates `out/<pkg>/__init__.py` + `_consolidated.py` with copied data under `out/<pkg>/` |
| I16 | `pyrefactor pkg -o out --mode flat` | Output has no `Constants` sections, still compiles |
| I17 | `pyrefactor pkg -o out --mode split` | Creates `const.py`, `class.py`, `func.py`, `main.py` + `__init__.py` |
| I18 | `pyrefactor pkg -o out --include "*.py" --exclude "bad.py"` | `bad.py` excluded even though include re-included others |
| I19 | `pyrefactor pkg -o out --entry pkg.a --entry-keep-all` | Keeps all `__main__` guards (count of `if __name__` in output == number of source guards) |
| I20 | `pyrefactor pkg -o out --source-map --preserve-modules --namespace` | Each block prefixed with `# source:` and `# Original module:` and `# namespace:` comments |
| I21 | `pyrefactor single.py -o out` (single file) | Consolidates single file; module name = stem |
| I22 | Data preservation: `pyrefactor pkg -o out` (with `data/*.json`) | `out/pkg/data/*.json` exists and content equal via `filecmp` |
| I23 | Edge: file with `importlib.import_module("os")` | Warning emitted, not crash |
| I24 | Edge: file mutating `__all__` | Warning “computed or mutated __all__” |

### Manual / Existing Behavior

- Original `pyrefactor path/to/package -o output --mode single` still works unchanged (backward compat).
- Existing tests for ordering, import conflict aliasing, and `__file__` warning still pass.

---

## Data Files Handling

`pyrefactor` now properly handles project data files — a common need for real Python packages that ship JSON configs, CSV datasets, templates, or static assets alongside code.

### Discovery

`discovery.discover_data_files(root, exclude, include, skip_dirs, copy_data)` scans the source tree (respecting the same `--include`/`--exclude` precedence as Python files) and collects files whose extension or name matches `DATA_EXTENSIONS` / `DATA_FILENAMES` (see `discovery.py`):

```
.json .yaml .yml .toml .ini .cfg .txt .csv .tsv .xml .html .css .js .md
.pickle .pkl .db .sqlite .sql .pem .key ...
plus well-known names: requirements.txt, pyproject.toml, MANIFEST.in, LICENSE, etc.
```

Files already excluded as Python sources (e.g., `conftest.py`) are not considered data unless re-included.

### Copy Semantics

In `core.consolidate`, after rendering, each `DataFile(relative)` is copied via `shutil.copy2(src, output / relative)` preserving the directory structure relative to the detected base (`src/` or project root). Example:

```
source/
  pkg/
    __init__.py
    core.py
    data/
      sample.json  →  output/pkg/data/sample.json  (single/package/flat modes)
                      output/<pkg>/data/sample.json (package mode)
```

For `module` and `split` modes, data is placed adjacent to the output file/dir.

### Flags

- `--copy-data` (default) / `--no-copy-data`: enable/disable.
- `--data-glob GLOB`: only copy matching data files (repeatable), e.g., `--data-glob "*.json"`.

### Example

```bash
# With data files present
tree src/mypackage
# src/mypackage/
#   __init__.py
#   core.py
#   data/sample.json
#   config.ini

pyrefactor src/mypackage -o output --copy-data
ls output/mypackage/data/
# sample.json  (copied)
```

If the source project had no data files, no extra action is taken (discovery returns `[]`). The included `src/refactor_utils/data/` directory demonstrates this — consolidating `refactor_utils` itself copies those sample files.

---

## Architecture

```
cli.py → core.py orchestrates
        → discovery.py (SourceFile + DataFile, pyproject awareness)
        → parser.py (tree-sitter, parallel, edge-case detection)
        → resolver.py (collision/dedupe/rename, imports, __all__)
        → ordering.py (topo sort)
        → writer.py (mode-specific rendering)
        → imports.py (import consolidation)
logging via logging_setup.py
models.py — picklable dataclasses (Block, ModuleInfo, SourceFile, DataFile, ImportSpec, Occ)
```

Parser/resolver remain decoupled: parser only emits `ModuleInfo`+`Block`+`Occ`; resolver consumes them without touching tree-sitter nodes. Worker data is strictly `models.py` types.

---

## Known Limits

- Renaming is name-based (parameters respected, other locals not — a future improvement is full scope analysis).
- `__file__`/`__name__`/`__package__` semantics change in a single file (warning logged; resource loading via `importlib.resources` may need `files()` API).
- Guarded imports (`try/except ImportError`) are kept verbatim (may have side effects).
- Dynamic `importlib`/`__import__` dependencies are warned but not resolved.
- `iwgfyrkugf.py` in `src/refactor_utils/` is a vendored example of a previous consolidation and is excluded by default (via `EXCLUDED_FILE_GLOBS`) to avoid self-reference loops.

---

## Development

```bash
pip install -e . --break-system-packages
pyrefactor --help
pytest  # (add your tests per matrix above)
```

Output must compile: `compile(text, fname, "exec")`; `--validate` also smoke-imports.

---

## License

MIT
